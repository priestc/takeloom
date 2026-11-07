"""How long a Takeloom Camera phone's battery will last, learned per phone.

Every phone reports its battery level (0..1) and whether it's charging in
the status it sends every few seconds. While a phone runs off its battery,
the server measures how fast the level falls and remembers that rate per
camera (KnownCamera.battery_drain_per_hour), smoothed over every run, so
the estimate is there from the moment a phone connects next time.

A rate is only measured over a stretch of at least MIN_WINDOW_S with a drop
of at least MIN_DROP: iPhones report the level in 5% steps, so anything
shorter is mostly rounding. Charging (or the level going up) starts a new
stretch. One rate covers the phone running the app, whether it's
recording or not: the camera and encoders run either way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MIN_WINDOW_S = 15 * 60
MIN_DROP = 0.05
# Weight of a new measurement against what's been learned so far.
SMOOTHING = 0.3


@dataclass
class _Stretch:
    start_t: float
    start_level: float


class BatteryTracker:
    """One per connected phone: feed it every status, and it hands back a
    newly measured drain rate (fraction per hour) when a stretch is long
    enough. Not thread-safe; the backend keeps one per phone under a lock."""

    def __init__(self) -> None:
        self._stretch: _Stretch | None = None

    def update(self, level: float | None, charging: bool, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        if level is None or charging:
            self._stretch = None
            return None
        s = self._stretch
        if s is None or level > s.start_level:
            self._stretch = _Stretch(now, level)
            return None
        drop = s.start_level - level
        elapsed = now - s.start_t
        if elapsed < MIN_WINDOW_S or drop < MIN_DROP:
            return None
        self._stretch = _Stretch(now, level)
        return drop / (elapsed / 3600)


def learned(previous: float, measured: float) -> float:
    """Fold a new measurement into the remembered rate (0: none yet)."""
    if previous <= 0:
        return measured
    return previous * (1 - SMOOTHING) + measured * SMOOTHING


def summary(level: float | None, charging: bool, drain_per_hour: float) -> dict:
    """What video-ui and the phone show: {"level": 0..1|None, "charging",
    "minutes_left": n|None (None: charging, or no rate learned yet),
    "full_hours": h|None (a full charge's runtime at the learned rate)}."""
    known = drain_per_hour > 0
    return {
        "level": level,
        "charging": charging,
        "minutes_left": round(level / drain_per_hour * 60) if known and level is not None and not charging else None,
        "full_hours": round(1 / drain_per_hour, 1) if known else None,
    }


def describe(info: dict) -> str:
    """"64% · 3h 10m left", "64% · charging", "64%" (nothing learned yet),
    "" (no level)."""
    level = info.get("level")
    if level is None:
        return ""
    text = f"{round(level * 100)}%"
    if info.get("charging"):
        return text + " · charging"
    minutes = info.get("minutes_left")
    if minutes is not None:
        hours, mins = divmod(int(minutes), 60)
        text += f" · {hours}h {mins:02d}m left" if hours else f" · {mins}m left"
    return text
