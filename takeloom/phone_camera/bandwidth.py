"""How the Takeloom Camera phones share the Wi-Fi.

Each phone records its own full-quality file and uploads it after the
session, so the only video crossing the Wi-Fi live is each phone's *live
feed*: what the stream, the previews, and the fallback recording (see
recorder.py) are made from. Its quality is the server's call: a total
budget (StudioConfig.camera_wifi_budget_mbps) split equally between every
phone streaming, so adding phones lowers everyone's quality instead of
flooding the network. No camera gets more than another: the cameras are a
set, and whichever one the video-ui or a live stream happens to be showing
is just a view of it (the stream is 1280 wide, so an equal share is plenty
for it), not a better camera — its fallback recording shouldn't come out
sharper than the others' because it was picked at the time. Re-planned
whenever a phone connects or leaves or reports a change; each phone gets a
{"kind": "live_quality"} message (protocol.py).

A phone that can't adapt (an app from before this, or an Android phone
whose camera can't feed a second encoder) streams its whole recording
bitrate regardless, so that comes off the budget first.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BUDGET_MBPS = 20.0
# Past this, more bits don't show: the stream is 1280 wide (StreamSwitcher),
# and the video-ui's full-resolution view (full_preview.py) is 1080p.
MAX_BPS = 8_000_000
# Below this the picture falls apart, budget or not.
MIN_BPS = 400_000

# A non-adaptive phone's live feed is its recording encode: the camera
# apps' bitrate for each quality (VideoQuality in Settings.swift/.kt), by
# frame height.
_RECORDING_BPS = {720: 6_000_000, 1080: 12_000_000, 2160: 35_000_000}


@dataclass(frozen=True)
class LiveQuality:
    max_width: int
    bitrate: int

    def message(self) -> dict:
        return {"kind": "live_quality", "max_width": self.max_width, "bitrate": self.bitrate}


@dataclass(frozen=True)
class Phone:
    camera_device: str
    adaptive: bool
    status: dict

    @property
    def fixed_bps(self) -> int:
        """What a non-adaptive phone streams anyway."""
        height = int(self.status.get("height") or 1080)
        return _RECORDING_BPS.get(height, 12_000_000)


def width_for(bitrate: int) -> int:
    if bitrate >= 5_000_000:
        return 1920
    if bitrate >= 2_000_000:
        return 1280
    if bitrate >= 900_000:
        return 960
    return 640


def plan(phones: list[Phone], budget_mbps: float) -> dict[str, LiveQuality]:
    """camera_device -> LiveQuality for each adaptive phone: an equal share
    of what the phones that can't adapt leave of the budget."""
    adaptive = [p for p in phones if p.adaptive]
    if not adaptive:
        return {}
    left = max(0.0, budget_mbps * 1e6 - sum(p.fixed_bps for p in phones if not p.adaptive))
    bps = int(max(MIN_BPS, min(MAX_BPS, left / len(adaptive))))
    return {p.camera_device: LiveQuality(width_for(bps), bps) for p in adaptive}


def describe(phones: list[Phone], qualities: dict[str, LiveQuality], labels: dict[str, str],
             budget_mbps: float) -> str:
    """One line for the server log."""
    parts = []
    total = 0
    for p in phones:
        name = labels.get(p.camera_device, p.camera_device)
        q = qualities.get(p.camera_device)
        if q is None:
            total += p.fixed_bps
            parts.append(f"{name}: {p.fixed_bps / 1e6:.0f} Mbps, can't adapt")
        else:
            total += q.bitrate
            parts.append(f"{name}: {q.bitrate / 1e6:.1f} Mbps, {q.max_width} wide")
    over = ""
    if total > budget_mbps * 1e6 * 1.01:
        over = ("  — over budget: update Takeloom Camera on the phones that can't adapt"
                if any(p.camera_device not in qualities for p in phones)
                else "  — over budget: already the lowest quality, too many phones")
    return (f"Live camera feeds: {total / 1e6:.1f} of {budget_mbps:g} Mbps Wi-Fi budget, "
            f"{len(phones)} phone{'s' if len(phones) != 1 else ''}. " + "; ".join(parts) + over)
