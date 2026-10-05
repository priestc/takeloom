"""Built-in audio player bar — plays a take, or several takes mixed live,
right inside the app instead of handing the file to the OS's default
player (VLC etc.). Used by the Sessions tab (one take) and the Completed
Takes tab's mixer (ui/song_mixer.py — every take on a song, each with its
own gain, changeable while playing): the backend's get_take_playback_path
produces a local file on whichever machine the UI is running on (fetched
over Remote if needed), and this bar decodes each into memory and plays
their gain-weighted sum through this machine's default output device via
sounddevice.

Plays on the machine looking at the UI — over a Remote connection that's
the laptop's own speakers, same as VLC used to — never the studio's
interface. On Completed Takes (a persistent tab) playback keeps going
across tab switches; on Sessions (rebuilt on every switch) leaving the tab
tears it down — destroy() closes the stream — and playback stops with it.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import numpy as np

_POLL_MS = 100


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class AudioPlayerBar(ttk.Frame):
    def __init__(self, master: tk.Misc, show_title: bool = True) -> None:
        super().__init__(master)
        self._lock = threading.Lock()
        # One (frames, channels) array per track, all the same length
        # (shorter ones zero-padded), mono or stereo — the callback always
        # outputs stereo. None until something's loaded.
        self._tracks: list[np.ndarray] | None = None
        self._length = 0
        self._gains: list[float] = []  # target per-track gain, see set_gains
        self._applied_gains: list[float] = []  # what the callback last used — ramped toward _gains
        self._sample_rate = 0
        self._position = 0  # frame index, advanced by the audio callback
        self._playing = False
        self._stream = None
        self._seeking = False  # user is dragging the slider — don't fight them
        self._load_token = 0  # newest load() wins if several are in flight
        self._poll_id: str | None = None

        self._title_var = tk.StringVar(value="Nothing loaded — press ▶ Play on a take")
        if show_title:  # the Completed Takes mixer shows the song name itself
            ttk.Label(self, textvariable=self._title_var, foreground="#666666").pack(anchor="w")

        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(2, 0))
        self._play_button = ttk.Button(controls, text="▶", width=3, command=self.toggle_play)
        self._play_button.pack(side="left")
        self._stop_button = ttk.Button(controls, text="■", width=3, command=self.stop)
        self._stop_button.pack(side="left", padx=(2, 8))
        self._time_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(controls, textvariable=self._time_var, width=12, anchor="e").pack(side="right", padx=(8, 0))

        self._pos_var = tk.DoubleVar(value=0.0)
        self._scale = ttk.Scale(controls, from_=0.0, to=1.0, variable=self._pos_var, orient="horizontal")
        self._scale.pack(side="left", fill="x", expand=True)
        self._scale.bind("<ButtonPress-1>", self._on_seek_start)
        self._scale.bind("<ButtonRelease-1>", self._on_seek_end)

        self._set_controls_enabled(False)

    # --- public ---

    def load(self, path: str | Path, title: str) -> None:
        """Decode `path` off the UI thread, then start playing it from the
        top, replacing whatever was loaded before."""
        self.load_tracks([path], title)

    def load_tracks(
        self, paths: list[str | Path], title: str, gains: list[float] | None = None,
        trim_start_seconds: float = 0.0, trim_end_seconds: float = 0.0, autoplay: bool = True,
    ) -> None:
        """Decode every file in `paths` off the UI thread (resampled to the
        first one's rate), trim each by the song's non-destructive "edit
        backing track" trim — the same way backend.py's
        live session's _load_track_locked does, so takes stay in sync with each other —
        zero-pad to a common length, and play their sum with `gains`
        (default 1.0 each; see set_gains). Replaces whatever was loaded."""
        self._close_stream()
        self._playing = False
        self._play_button.configure(text="▶")
        self._load_token += 1
        token = self._load_token
        self._title_var.set(f"Loading {title}...")
        self._set_controls_enabled(False)
        gains = list(gains) if gains is not None else [1.0] * len(paths)

        def worker() -> None:
            try:
                from ..audio.formats import read_audio
                tracks: list[np.ndarray] = []
                sr = 0
                for path in paths:
                    data, sr_read = read_audio(Path(path), sr or None)
                    sr = sr or sr_read
                    start = round(trim_start_seconds * sr)
                    end = round(trim_end_seconds * sr)
                    if 0 < start < len(data):
                        data = data[start:]
                    if 0 < end < len(data):
                        data = data[:len(data) - end]
                    tracks.append(data)
                length = max((len(t) for t in tracks), default=0)
                tracks = [
                    np.ascontiguousarray(np.pad(t, ((0, length - len(t)), (0, 0))), dtype=np.float32)
                    for t in tracks
                ]
                result, error = (tracks, sr, length), None
            except Exception as e:  # noqa: BLE001 — any decode failure is shown, not raised
                result, error = None, f"{type(e).__name__}: {e}"
            self.after(0, lambda: self._on_loaded(token, title, paths, gains, autoplay, result, error))

        threading.Thread(target=worker, daemon=True).start()

    def set_gains(self, gains: list[float]) -> None:
        """Change each track's gain (1.0 = as recorded) — takes effect
        immediately, mid-playback, ramped over one audio block so a slider
        drag doesn't click."""
        with self._lock:
            self._gains = list(gains)

    def show_error(self, message: str) -> None:
        self._title_var.set(message)

    def toggle_play(self) -> None:
        if self._tracks is None:
            return
        if self._playing:
            self._pause()
        else:
            self._play()

    def stop(self) -> None:
        self._pause()
        with self._lock:
            self._position = 0
        self._refresh_position()

    def destroy(self) -> None:
        self._close_stream()
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        super().destroy()

    # --- loading ---

    def _on_loaded(
        self, token: int, title: str, paths: list, gains: list[float], autoplay: bool, result, error: str | None,
    ) -> None:
        if token != self._load_token or not self.winfo_exists():
            return  # superseded by a newer load(), or the tab is gone
        if error or result is None:
            self._title_var.set(f"Could not play {title}: {error} ({', '.join(str(p) for p in paths)})")
            return
        tracks, sr, length = result
        with self._lock:
            self._tracks = tracks
            self._length = length
            self._sample_rate = sr
            self._position = 0
            self._gains = list(gains)
            self._applied_gains = list(gains)
        self._scale.configure(to=max(length / sr, 0.001) if sr else 0.001)
        self._title_var.set(title)
        self._set_controls_enabled(True)
        self._refresh_position()
        if autoplay:
            self._play()

    # --- playback ---

    def _play(self) -> None:
        if self._tracks is None:
            return
        with self._lock:
            if self._position >= self._length:
                self._position = 0
        if self._stream is None:
            try:
                import sounddevice as sd
                self._stream = sd.OutputStream(
                    samplerate=self._sample_rate, channels=2, dtype="float32",
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as e:  # noqa: BLE001 — e.g. no output device on this machine
                self._stream = None
                self._title_var.set(f"Could not open audio output: {type(e).__name__}: {e}")
                return
        self._playing = True
        self._play_button.configure(text="⏸")
        self._schedule_poll()

    def _pause(self) -> None:
        self._playing = False
        self._play_button.configure(text="▶")
        # Closing (not just silencing) the stream on pause frees the output
        # device while nothing's playing — reopened on the next play.
        self._close_stream()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 — already gone is fine
                pass

    def _callback(self, outdata: np.ndarray, frames: int, _time, _status) -> None:
        outdata.fill(0)
        with self._lock:
            tracks = self._tracks
            start = self._position
            if tracks is None or start >= self._length:
                return
            n = min(frames, self._length - start)
            for i, data in enumerate(tracks):
                old, new = self._applied_gains[i], self._gains[i]
                if old == new == 0.0:
                    continue
                gain = np.linspace(old, new, n, dtype=np.float32)[:, None] if old != new else new
                outdata[:n] += data[start:start + n] * gain  # mono (n,1) broadcasts to both channels
            self._applied_gains = list(self._gains)
            self._position = start + n
        np.clip(outdata, -1.0, 1.0, out=outdata)

    # --- position UI ---

    def _schedule_poll(self) -> None:
        if self._poll_id is None:
            self._poll_id = self.after(_POLL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_id = None
        if not self.winfo_exists():
            return
        self._refresh_position()
        if self._playing:
            with self._lock:
                finished = self._tracks is None or self._position >= self._length
            if finished:
                self.stop()
            else:
                self._schedule_poll()

    def _refresh_position(self) -> None:
        if self._tracks is None or not self._sample_rate:
            return
        with self._lock:
            pos_s = self._position / self._sample_rate
        total_s = self._length / self._sample_rate
        if not self._seeking:
            self._pos_var.set(pos_s)
        self._time_var.set(f"{_format_seconds(pos_s)} / {_format_seconds(total_s)}")

    def _on_seek_start(self, _event: object) -> None:
        self._seeking = True

    def _on_seek_end(self, _event: object) -> None:
        self._seeking = False
        if self._tracks is None:
            return
        frame = int(self._pos_var.get() * self._sample_rate)
        with self._lock:
            self._position = min(max(frame, 0), self._length)
        self._refresh_position()

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        for widget in (self._play_button, self._stop_button, self._scale):
            widget.state(state)
