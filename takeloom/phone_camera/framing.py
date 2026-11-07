"""A Takeloom Camera phone's framing: how far it's zoomed and where it's
panned to, set from `takeloom video-ui` and remembered per camera
(KnownCamera.framing), so a phone frames the same shot every time it
connects.

    zoom   like the phone's Camera app: 1 is the main lens, 0.5 the
           ultra-wide, 5 a 5x telephoto. The phone reports its range and
           lenses in its status ("zoom_caps").
    x, y   the middle of the picture, as fractions (0..1) of the widest
           view the phone has (its zoom_min).

Zoom is optical only: the server snaps every framing to one of the
phone's real lenses (its status's zoom_caps "lenses") and keeps it
centered, so the phones never digitally zoom or crop, and the picture is
always the lens's full resolution. Framing beyond that is done by aiming
the phone in its mount. (The phones can still crop to pan, and older apps
digitally zoom; the server just never asks them to.) See protocol.py's
"framing" message.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_ZOOM = 1.0


@dataclass(frozen=True)
class Framing:
    zoom: float = DEFAULT_ZOOM
    x: float = 0.5
    y: float = 0.5

    @classmethod
    def from_dict(cls, data: dict | None) -> "Framing":
        data = data or {}
        try:
            return cls(float(data.get("zoom", DEFAULT_ZOOM)), float(data.get("x", 0.5)), float(data.get("y", 0.5)))
        except (TypeError, ValueError):
            return cls()

    def to_dict(self) -> dict:
        return {"zoom": self.zoom, "x": self.x, "y": self.y}

    def message(self) -> dict:
        return {"kind": "framing", **self.to_dict()}

    def clamped(self, caps: dict | None) -> "Framing":
        """Optical only: the phone's nearest real lens (caps: its status's
        "zoom_caps"; None: not known yet, so the zoom's left as it is),
        centered."""
        return Framing(nearest_lens(self.zoom, lenses(caps)) if caps is not None else self.zoom, 0.5, 0.5)


def lenses(caps: dict | None) -> list[float]:
    """The phone's optical zoom steps, smallest first (1x if it says none)."""
    try:
        found = sorted({float(z) for z in (caps or {}).get("lenses") or [] if float(z) > 0})
    except (TypeError, ValueError):
        found = []
    return found or [1.0]


def nearest_lens(zoom: float, steps: list[float]) -> float:
    """Closest by ratio, not difference: 2x is nearer 1x than 5x is to 3x."""
    zoom = max(float(zoom), 1e-6)
    return min(steps, key=lambda lens: abs(math.log(zoom / lens)))
