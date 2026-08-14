"""AudioEngine: sd.Stream callback orchestration for simultaneous record + playback."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from .recorder import Recorder
from .mixer import Mixer
from .filters import Compressor, CompressorSettings


class AudioEngine:
    """Manages the sounddevice Stream for simultaneous recording and playback.

    The audio callback captures mono input to the Recorder and reads
    mixed stereo output from the Mixer.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        buffer_size: int = 512,
        input_device: int | None = None,
        output_device: int | None = None,
        input_channels: int = 1,
        output_channels: int = 2,
        monitor_channel: int = 0,
        compressor_settings: CompressorSettings | None = None,
        monitor_instrument: bool = True,
        instrument_volume: float = 1.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.input_device = input_device
        self.output_device = output_device
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.monitor_channel = monitor_channel
        # Whether the processed instrument input is added into the live
        # headphone mix ("production" monitoring mode) or left out of it
        # entirely ("recording" mode, relying on the audio interface's own
        # zero-latency hardware direct monitor for the instrument instead).
        # Either way, the *recorded* mix (mix_recorder, below) always gets
        # the full instrument+backing blend — this only affects what's sent
        # to the output device live. See set_monitor_instrument().
        self.monitor_instrument = monitor_instrument
        # Gain applied to the instrument's live monitor feed only — never to
        # what's written to disk (recorder/session_recorder, below, capture
        # `mono` before this is applied), so cranking this to compensate for
        # a quiet pickup/DI doesn't also inflate the recorded take.
        self.instrument_volume = instrument_volume

        self.recorder: Recorder | None = None
        self.session_recorder: Recorder | None = None
        self.mix_recorder: Recorder | None = None
        # Frames captured into the session recording so far — the timeline
        # every session-log event position refers to. Counted here in the
        # callback (single writer) rather than read from the Recorder, whose
        # frames_written lags behind by whatever its writer thread hasn't
        # drained yet.
        self._session_frames: int = 0
        self.mixer = Mixer(sample_rate)
        self.compressor = Compressor(sample_rate, compressor_settings)
        self._stream: sd.Stream | None = None
        self._running = False
        self._peak_level: float = 0.0
        self._backing_peak_level: float = 0.0
        self._on_song_end: Callable[[], None] | None = None
        self._stream_sink: Callable[[np.ndarray], None] | None = None
        self._instrument_sink: Callable[[np.ndarray], None] | None = None

    @property
    def peak_level(self) -> float:
        return self._peak_level

    @property
    def backing_peak_level(self) -> float:
        return self._backing_peak_level

    def set_on_song_end(self, callback: Callable[[], None] | None) -> None:
        """Set callback invoked when mixer reaches end of backing track."""
        self._on_song_end = callback

    def set_stream_sink(self, sink: Callable[[np.ndarray], None] | None) -> None:
        """Live-safe set/clear of an extra consumer of the full production
        mix (see _callback's full_mix) — used by live streaming
        (takeloom/streaming.py's LiveAudioFeeder) to get the same audio
        mix_recorder writes to disk, just piped live too. sink is called
        from this callback's own realtime thread, so it must not block."""
        self._stream_sink = sink

    def set_instrument_sink(self, sink: Callable[[np.ndarray], None] | None) -> None:
        """Live-safe set/clear of an extra consumer of the raw captured
        instrument input (see _callback's mono — the same signal
        session_recorder/recorder write to disk and _peak_level is
        computed from) — used by the realtime instrument classifier
        (audio/instrument_classifier.py) to see exactly what's being
        recorded. Called from this callback's own realtime thread, so —
        same requirement as set_stream_sink — it must not block; the
        classifier itself only does cheap work here and defers the actual
        analysis to a background thread."""
        self._instrument_sink = sink

    def set_compressor_settings(self, settings: CompressorSettings) -> None:
        """Swap in new compressor settings, live — safe to call from any
        thread; the audio callback reads self.compressor.settings once per
        block, so this takes effect on the very next block."""
        self.compressor.settings = settings

    def set_monitor_instrument(self, enabled: bool) -> None:
        """Toggle whether the instrument is included in the live headphone
        mix — live-safe the same way set_compressor_settings() is (the
        callback reads self.monitor_instrument once per block)."""
        self.monitor_instrument = enabled

    def set_instrument_volume(self, volume: float) -> None:
        """Adjust the instrument's live monitor gain — live-safe the same
        way set_compressor_settings() is (the callback reads
        self.instrument_volume once per block). Unlike the hardware direct
        monitor fader (scarlett2_direct_monitor.py), this is plain software
        gain applied before the final clip, so it's free to go above unity."""
        self.instrument_volume = volume

    def start_session_recording(self, output_path: Path) -> None:
        """Start the continuous session-level recorder."""
        self._session_frames = 0
        self.session_recorder = Recorder(output_path, self.sample_rate, 1)
        self.session_recorder.start()

    def stop_session_recording(self) -> None:
        """Stop the continuous session-level recorder."""
        if self.session_recorder:
            self.session_recorder.stop()
            self.session_recorder = None

    def start_mix_recording(self, output_path: Path) -> None:
        """Start recording the full monitor mix (backing track + input), for
        muxing with a session video recording."""
        self.mix_recorder = Recorder(output_path, self.sample_rate, self.output_channels)
        self.mix_recorder.start()

    def stop_mix_recording(self) -> None:
        """Stop the monitor-mix recorder."""
        if self.mix_recorder:
            self.mix_recorder.stop()
            self.mix_recorder = None

    def start_recording(self, output_path: Path) -> None:
        """Create and start the per-take recorder."""
        self.recorder = Recorder(output_path, self.sample_rate, 1)
        self.recorder.start()

    def stop_recording(self) -> None:
        """Stop the per-take recorder."""
        if self.recorder:
            rec = self.recorder
            self.recorder = None  # cut off callback writes before flushing
            rec.stop()

    def _callback(
        self,
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """Audio stream callback — runs in real-time audio thread."""
        # Capture mono input from the instrument's channel
        ch = self.monitor_channel
        mono = indata[:, ch:ch+1].copy()

        # Record RAW input to disk — never through the compressor. Only
        # what's actually monitored/played back (below) reflects it; a
        # take file on disk always stays exactly what came off the input,
        # so backend.py's play_take/Mixer.add_source can apply whatever
        # that take's own instrument-label compressor settings currently
        # are at listen time, rather than whatever was baked in the day it
        # was recorded.
        if self.session_recorder:
            self.session_recorder.write(mono)
            self._session_frames += len(mono)
        if self.recorder:
            self.recorder.write(mono)

        # Update peak level for VU meter — reflects the raw input actually
        # being captured, same reasoning as recording it raw above.
        self._peak_level = float(np.max(np.abs(mono)))
        if self._instrument_sink:
            self._instrument_sink(mono)

        # Everything from here on is what gets *heard* (headphones, the
        # produced video's mix, the live stream) — this is where the
        # compressor (this instrument's own label — see backend.py's
        # AudioEngine construction sites) actually applies.
        compressed_mono = self.compressor.process(mono)

        # Playback output: mix backing track + input monitoring
        mix_position = self.mixer.position  # captured before read() advances it — see stream_mix below
        mix = self.mixer.read(frames)  # always stereo, shape (frames, 2)
        self._backing_peak_level = float(np.max(np.abs(mix)))

        # The full production mix (backing/takes + instrument), shaped to
        # match this stream's actual output_channels — what mix_recorder
        # captures regardless of monitor_instrument, since it becomes the
        # produced video's "Mix" audio track and always needs the
        # instrument in it, even when Recording Monitoring keeps it out of
        # the live headphone feed sent to outdata below.
        monitor_mono = compressed_mono * self.instrument_volume
        if self.output_channels == 2:
            full_mix = mix.copy()
            full_mix[:, 0] += monitor_mono[:, 0]
            full_mix[:, 1] += monitor_mono[:, 0]
        else:
            full_mix = mix[:, :1] + monitor_mono
        np.clip(full_mix, -1.0, 1.0, out=full_mix)

        if self.monitor_instrument:
            outdata[:] = full_mix
        else:
            outdata[:] = mix[:, :self.output_channels]

        if self.mix_recorder:
            self.mix_recorder.write(full_mix)
        if self._stream_sink:
            # The live stream never gets the backing track — only the
            # instrument being recorded now, mixed with previously
            # recorded takes of other instruments — even though it's still
            # in the musician's headphones (outdata, above) and in the
            # produced video (mix_recorder, above). Built from the same
            # [mix_position, mix_position+frames) window mixer.read()
            # above just advanced past, via read_excluding() rather than a
            # second read() call, which would double-advance playback
            # position.
            stream_mix = self.mixer.read_excluding(mix_position, frames, exclude={"backing"})
            if self.output_channels == 2:
                stream_mix[:, 0] += monitor_mono[:, 0]
                stream_mix[:, 1] += monitor_mono[:, 0]
            else:
                stream_mix = stream_mix[:, :1] + monitor_mono
            np.clip(stream_mix, -1.0, 1.0, out=stream_mix)
            self._stream_sink(stream_mix)

        # Check if song ended
        if self.mixer.is_playing and self.mixer.is_finished:
            self.mixer.set_playing(False)
            if self._on_song_end:
                # Fire callback from a separate thread to avoid blocking audio
                threading.Thread(target=self._on_song_end, daemon=True).start()

    def start(self) -> None:
        """Start the audio stream."""
        if self._running:
            return
        # Compute latency from buffer size for minimal delay
        latency = self.buffer_size / self.sample_rate
        self._stream = sd.Stream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            device=(self.input_device, self.output_device),
            channels=(self.input_channels, self.output_channels),
            dtype="float32",
            latency=latency,
            callback=self._callback,
        )
        self._stream.start()
        self._running = True

    def stop(self) -> None:
        """Stop the audio stream and all recorders."""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.stop_recording()
        self.stop_session_recording()
        self.stop_mix_recording()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def recording_elapsed(self) -> float:
        if self.recorder:
            return self.recorder.elapsed_seconds
        return 0.0

    @property
    def session_frames(self) -> int:
        """Current position (in captured frames) on the session recording's
        timeline — what session-log events are stamped with."""
        return self._session_frames
