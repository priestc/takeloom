"""How the Takeloom Camera phones share the Wi-Fi.

Each phone records its own full-quality file and uploads it after the
session, so the only video crossing the Wi-Fi live is each phone's *live
feed*: what the stream, the previews, and the fallback recording (see
recorder.py) are made from. Its quality is the server's call: a total
budget (StudioConfig.camera_wifi_budget_mbps) split between every phone
streaming, with the program camera (the one on the stream) getting three
shares to everyone else's one, so adding phones lowers the others' quality
instead of flooding the network. Re-planned whenever a phone connects or
leaves, reports a change, or the program camera switches; each phone
gets a {"kind": "live_quality"} message (protocol.py).

A phone that can't adapt (an app from before this, or an Android phone
whose camera can't feed a second encoder) streams its whole recording
bitrate regardless, so that comes off the budget first.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BUDGET_MBPS = 20.0
PROGRAM_SHARES = 3
# Past these, more bits don't show: the stream is 1280 wide (StreamSwitcher)
# and a thumbnail is a thumbnail. The program view's full-resolution frames
# (full_preview.py) are decoded from the program camera's feed: 1080p.
PROGRAM_MAX_BPS = 8_000_000
OTHER_MAX_BPS = 3_000_000
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


def plan(phones: list[Phone], program: str, budget_mbps: float) -> dict[str, LiveQuality]:
    """camera_device -> LiveQuality for each adaptive phone."""
    adaptive = [p for p in phones if p.adaptive]
    if not adaptive:
        return {}
    left = max(0.0, budget_mbps * 1e6 - sum(p.fixed_bps for p in phones if not p.adaptive))
    has_program = any(p.camera_device == program for p in adaptive)
    share = left / (len(adaptive) + (PROGRAM_SHARES - 1 if has_program else 0))
    out = {}
    for p in adaptive:
        if p.camera_device == program:
            bps = min(PROGRAM_MAX_BPS, share * PROGRAM_SHARES)
        else:
            bps = min(OTHER_MAX_BPS, share)
        bps = int(max(MIN_BPS, bps))
        out[p.camera_device] = LiveQuality(width_for(bps), bps)
    return out


def describe(phones: list[Phone], qualities: dict[str, LiveQuality], labels: dict[str, str],
             program: str, budget_mbps: float) -> str:
    """One line for the server log."""
    parts = []
    total = 0
    for p in phones:
        name = labels.get(p.camera_device, p.camera_device) + (" (program)" if p.camera_device == program else "")
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
