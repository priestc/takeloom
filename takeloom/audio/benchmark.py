"""Microbenchmark rig for the audio-modifier chain (the compressor for
now, more later — see filters.py's module docstring).

AudioEngine's real-time callback (engine.py) has exactly buffer_size/
sample_rate seconds to do all of its own work each block before the
audio hardware needs the next one — run over that budget often enough
and it's an audible dropout, not just a slow UI. This measures how
expensive that work actually is on the machine that would really run it,
so a slow modifier (or too many of them) shows up as a number someone
can look at, rather than as glitching audio discovered mid-session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..config import INSTRUMENT_LABELS, StudioConfig
from .filters import Compressor


@dataclass
class ModifierBenchmarkResult:
    """See run_audio_modifier_benchmark(). `total_ms` is the "entirety of
    all audio modifiers settings" figure: every configured label's
    compressor run once, back to back, against one block — an aggregate
    stress figure, not what a single real callback actually does (that
    only ever runs *one* label's compressor per block — see engine.py's
    _callback and backend.py's per-session AudioEngine construction)."""
    labels_measured: int
    block_frames: int
    sample_rate: int
    trials: int
    total_ms: float
    budget_ms: float  # one real-time block's actual deadline: block_frames/sample_rate, in ms
    per_label_ms: dict[str, float] = field(default_factory=dict)

    @property
    def within_budget(self) -> bool:
        return self.total_ms < self.budget_ms


def run_audio_modifier_benchmark(
    config: StudioConfig, block_frames: int | None = None, trials: int = 50,
) -> ModifierBenchmarkResult:
    """Time running *every* instrument label's current compressor
    settings (config.compressor_settings — see StudioConfig.
    compressor_for_label) against one synthetic audio block each,
    `trials` times apiece, and report each label's median-ish (mean,
    here) per-block cost plus their sum — "the entirety of all audio
    modifiers settings" combined.

    block_frames defaults to config.buffer_size, the real block size
    AudioEngine's callback actually gets each time, so total_ms is
    directly comparable to budget_ms.

    Uses fixed-seed random noise, not silence: Compressor.process's
    per-sample envelope math (filters.py) takes a different path once a
    sample actually crosses the threshold than it does on silence (which
    never does), and a benchmark meant to catch worst-case cost should
    exercise that path, not the cheaper one. Fixed seed keeps repeated
    runs comparable — "unit test style": deterministic input, not
    influenced by whatever happens to be plugged in or playing right now.
    """
    sample_rate = config.sample_rate
    frames = block_frames or config.buffer_size
    rng = np.random.default_rng(0)
    block = (rng.random((frames, 1)).astype(np.float32) - 0.5) * 2.0

    per_label_ms: dict[str, float] = {}
    for label in INSTRUMENT_LABELS:
        settings = config.compressor_for_label(label)
        compressor = Compressor(sample_rate, settings)
        compressor.process(block)  # warm-up call — excluded from the timed trials below

        elapsed_total = 0.0
        for _ in range(trials):
            start = time.perf_counter()
            compressor.process(block)
            elapsed_total += time.perf_counter() - start
        per_label_ms[label] = (elapsed_total / trials) * 1000.0

    return ModifierBenchmarkResult(
        labels_measured=len(INSTRUMENT_LABELS),
        block_frames=frames,
        sample_rate=sample_rate,
        trials=trials,
        total_ms=sum(per_label_ms.values()),
        budget_ms=(frames / sample_rate) * 1000.0,
        per_label_ms=per_label_ms,
    )
