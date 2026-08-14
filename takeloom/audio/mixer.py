"""Mix backing track + preferred takes for playback output."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .filters import CompressorSettings, apply_compressor
from .formats import read_audio


@dataclass
class MixSource:
    """A single audio source in the mix."""
    name: str
    data: np.ndarray  # float32, shape (N, 2) stereo
    volume: float = 1.0
    active: bool = True
    original_data: np.ndarray | None = None  # full array before trim


class Mixer:
    """Pre-loads and mixes backing track + completed takes.

    All sources are pre-loaded as float32 numpy arrays.
    read() returns summed audio at the current playback position.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.sources: list[MixSource] = []
        self._position: int = 0  # current frame position
        self._playing: bool = False

    def add_source(
        self, name: str, path: Path, volume: float = 1.0, trim_frames: int = 0,
        compressor_settings: CompressorSettings | None = None,
    ) -> None:
        """Load an audio file and add it as a mix source.

        trim_frames: number of frames to skip from the start (for latency compensation).

        compressor_settings, if given, is applied once here (offline,
        whole-file — see filters.apply_compressor) rather than live per
        block — the source file on disk (e.g. a completed take) always
        stays raw; this is what makes an "other instrument's take" heard
        with that take's own instrument-label compressor settings, same
        as backend.py's play_take, without touching the file itself.
        Never used for "backing"/"metronome" sources (see backend.py's
        callers) — only an actual take has an instrument label to look
        settings up by."""
        data, sr = read_audio(path, self.sample_rate)
        # Ensure stereo
        if data.ndim == 1:
            data = np.column_stack([data, data])
        elif data.shape[1] == 1:
            data = np.column_stack([data[:, 0], data[:, 0]])
        if compressor_settings is not None:
            data = apply_compressor(data, sr, compressor_settings)
        original = data
        if trim_frames > 0 and trim_frames < len(data):
            data = data[trim_frames:]
        self.sources.append(MixSource(name=name, data=data, volume=volume, original_data=original))

    def clear(self) -> None:
        """Remove all sources."""
        self.sources.clear()
        self._position = 0

    @property
    def duration_frames(self) -> int:
        """Duration of the longest source in frames."""
        if not self.sources:
            return 0
        return max(len(s.data) for s in self.sources if s.active)

    @property
    def duration_seconds(self) -> float:
        return self.duration_frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def position(self) -> int:
        return self._position

    @property
    def position_seconds(self) -> float:
        return self._position / self.sample_rate if self.sample_rate else 0.0

    def seek(self, frame: int) -> None:
        self._position = max(0, frame)

    def reset(self) -> None:
        """Reset playback to beginning."""
        self._position = 0

    def set_playing(self, playing: bool) -> None:
        self._playing = playing

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_finished(self) -> bool:
        """True if playback position is past all sources."""
        return self._position >= self.duration_frames and self.duration_frames > 0

    def read(self, frames: int) -> np.ndarray:
        """Read mixed audio for the next `frames` frames, advancing
        playback position by `frames`.

        Returns stereo float32 array of shape (frames, 2).
        If not playing, returns silence (and does not advance).
        """
        if not self._playing:
            return np.zeros((frames, 2), dtype=np.float32)
        output = self._sum_sources(self._position, frames, exclude=None)
        self._position += frames
        return output

    def read_excluding(self, position: int, frames: int, exclude: set[str]) -> np.ndarray:
        """Same summing logic as read(), for the exact [position,
        position+frames) window a same-sized read() call elsewhere just
        advanced past (or is about to) — but skipping any source named in
        `exclude`, and without touching playback position/state itself.
        Callers are responsible for passing a position that actually lines
        up with their own read() call; this never advances anything on its
        own, so calling both isn't a double-advance the way calling read()
        twice per block would be.

        Used to build a second, parallel mix — e.g. AudioEngine's live
        stream feed, which needs everything read() does except the backing
        track — alongside (not instead of) the normal read() call that
        monitoring/recording still uses unchanged."""
        if not self._playing:
            return np.zeros((frames, 2), dtype=np.float32)
        return self._sum_sources(position, frames, exclude)

    def _sum_sources(self, position: int, frames: int, exclude: set[str] | None) -> np.ndarray:
        output = np.zeros((frames, 2), dtype=np.float32)
        for source in self.sources:
            if not source.active or (exclude and source.name in exclude):
                continue
            src_len = len(source.data)
            start = position
            end = start + frames
            if start >= src_len:
                continue
            actual_end = min(end, src_len)
            n = actual_end - start
            output[:n] += source.data[start:actual_end] * source.volume
        # Clip to prevent clipping distortion
        np.clip(output, -1.0, 1.0, out=output)
        return output

    def set_volume(self, name: str, volume: float) -> None:
        for source in self.sources:
            if source.name == name:
                source.volume = volume
                break

    def set_trim(self, name: str, trim_frames: int) -> None:
        """Re-slice a source from its original_data with a new trim offset."""
        for source in self.sources:
            if source.name == name and source.original_data is not None:
                if trim_frames > 0 and trim_frames < len(source.original_data):
                    source.data = source.original_data[trim_frames:]
                else:
                    source.data = source.original_data
                break
