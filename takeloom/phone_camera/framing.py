"""A Takeloom Camera phone's framing: how far it's zoomed and where it's
panned to, set from `takeloom video-ui` and remembered per camera
(KnownCamera.framing), so a phone frames the same shot every time it
connects.

    zoom   like the phone's Camera app: 1 is the main lens, 0.5 the
           ultra-wide, 5 a 5x telephoto. The phone reports its range and
           lenses in its status ("zoom_caps").
    x, y   the middle of the picture, as fractions (0..1) of the widest
           view the phone has (its zoom_min).

The phone does as much as it can optically: it zooms with its lenses (and
the camera's own zoom) as far as it can while keeping the requested region
in view, and digitally crops the rest — so a centered shot is all optical,
and panning costs resolution the further it is from the middle. The crop
applies to the live feed and the recording alike. See protocol.py's
"framing" message.
"""

from __future__ import annotations

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
        """Within what the phone reported it can do (caps: its status's
        "zoom_caps"; None: not known yet, so only the basic limits)."""
        zoom_min = float((caps or {}).get("min") or 0.5)
        zoom_max = float((caps or {}).get("max") or 15.0)
        zoom = min(max(self.zoom, zoom_min), zoom_max)
        if caps is not None and not caps.get("pan", False):
            return Framing(zoom, 0.5, 0.5)
        # The visible region (zoom_min / zoom of the widest view) stays inside it.
        half = zoom_min / zoom / 2 if caps is not None else 0.0
        return Framing(zoom, min(max(self.x, half), 1 - half), min(max(self.y, half), 1 - half))
