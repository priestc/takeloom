"""The live stream's footer text: what's showing right now.

The footer (musician, instrument, when, song, camera) is only ever on
YouTube: drawn into the live stream (stream_switcher.py) and into a
session's edit when it's published (vault_server/takeloom_vault/edit.py).
This is the live half: the same text as the vault's footer.py schedule
gives for the moment just past the latest event — keep the two in step.
The camera's name is added by the switcher, which knows which is on.
"""

from __future__ import annotations

SEPARATOR = "   •   "


def join(*parts: str) -> str:
    return SEPARATOR.join(p for p in parts if p)


def footer_text(musician: str, instrument: str, events, session_start: str = "") -> str:
    """`events`: the session's events so far (backend.py's _SessionEvent,
    or session-log dicts). Before the first song: musician / instrument /
    when the session started; then the latest song's, with the instrument
    and wall-clock time it came up."""
    def get(e, key):
        return e.get(key) if isinstance(e, dict) else getattr(e, key, None)

    start = next((get(e, "wall_time") or "" for e in events if get(e, "event_type") == "session_start"),
                 session_start)
    text = join(musician, instrument, start)
    current = None
    for e in events:
        name = get(e, "track_name")
        if not name or get(e, "frame") is None or name == current:
            continue
        current = name
        text = join(musician, get(e, "instrument") or instrument, get(e, "wall_time") or "", name)
    return text
