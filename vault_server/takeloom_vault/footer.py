"""The footer: the strip along the bottom of a session's video naming the
musician, the instrument, the song, when it was played, and the camera.

It used to be burned into every camera's video while takeloom processed
the session. Now it belongs to the web video mixer: the session page draws
it over the player (web.py), changing with the song and with each cut, and
"Render final video" draws the same thing into edit.mp4 (edit.py). Only for
sessions whose camera videos are clean — the session log's
"video_footer": "none" (takeloom marks them); an older session's videos
already carry their footer, so it isn't added twice.

Times are on the session's timeline (the mix's: session time 0 is the
mix's first sample), like the editor's cuts.
"""

from __future__ import annotations

SEPARATOR = "   •   "


def wanted(data: dict) -> bool:
    """Whether this session's videos are clean, so the mixer adds the footer."""
    return data.get("video_footer") == "none"


def _text(*parts: str) -> str:
    return SEPARATOR.join(p for p in parts if p)


def schedule(data: dict) -> list[list]:
    """[[start_s, text], ...] in order: before the first song just
    musician / instrument / date, then for each song as it's loaded its
    name too (and the instrument and wall-clock time it came up). The
    camera's name is added where it's shown (with_camera), since it changes
    with every cut."""
    sample_rate = data.get("sample_rate") or 48000
    mix_start = data.get("mix_start_frame") or 0
    musician = data.get("musician", "")
    events = data.get("events") or []
    session_start = next((e.get("wall_time", "") for e in events if e.get("event_type") == "session_start"), "")
    entries = [[0.0, _text(musician, data.get("instrument", ""), session_start)]]
    current = None
    for e in events:
        name, frame = e.get("track_name"), e.get("frame")
        if not name or frame is None or name == current:
            continue
        current = name
        start = max(0.0, (frame - mix_start) / sample_rate)
        text = _text(musician, e.get("instrument") or data.get("instrument", ""), e.get("wall_time", ""), name)
        if start <= entries[-1][0]:
            entries[-1] = [entries[-1][0], text]  # replaces the opening (or an instantly superseded) one
        else:
            entries.append([round(start, 3), text])
    return entries


def with_camera(text: str, camera: str) -> str:
    return _text(text, camera)
