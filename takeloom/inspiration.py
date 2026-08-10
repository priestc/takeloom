"""Inspiration track queries and downloads against the radioserver library."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .config import StudioConfig
from .project import TrackEntry
from .utils import format_duration

ProgressCallback = Callable[[float | None, str], None]


class InspirationError(Exception):
    """Raised when inspiration tracks can't be queried or downloaded."""


def _fmt_length(seconds: float) -> str:
    """"3:00" rather than format_duration()'s zero-padded "03:00" — reads
    more naturally in a derived filter label."""
    minutes, _, secs = format_duration(seconds).partition(":")
    return f"{int(minutes)}:{secs}"


def derive_filter_label(filter_criteria: dict) -> str:
    """Auto-derive a human-readable label for an inspiration filter slot
    from its criteria — e.g. {"genre": "Doo Wop", "year_max": 1965} ->
    "Doo Wop before 1965", {"artist": "Bob Dylan", "year_min": 2003,
    "year_max": 2006} -> "Bob Dylan 2003-2006" — so the "Add inspiration
    filter" dialog doesn't need a name typed in by hand."""
    artist = (filter_criteria.get("artist") or "").strip()
    genre = (filter_criteria.get("genre") or "").strip()
    year_min = filter_criteria.get("year_min")
    year_max = filter_criteria.get("year_max")
    length_min = filter_criteria.get("duration_min")
    length_max = filter_criteria.get("duration_max")

    subject = " - ".join(p for p in (artist, genre) if p)

    if year_min is not None and year_max is not None:
        year_part = f"from {year_min}" if year_min == year_max else f"{year_min}-{year_max}"
    elif year_min is not None:
        year_part = f"after {year_min}"
    elif year_max is not None:
        year_part = f"before {year_max}"
    else:
        year_part = ""

    if length_min is not None and length_max is not None:
        if length_min == length_max:
            length_part = f"around {_fmt_length(length_min)}"
        else:
            length_part = f"{_fmt_length(length_min)}-{_fmt_length(length_max)}"
    elif length_min is not None:
        length_part = f"over {_fmt_length(length_min)}"
    elif length_max is not None:
        length_part = f"under {_fmt_length(length_max)}"
    else:
        length_part = ""

    qualifiers = " ".join(p for p in (year_part, length_part) if p)

    if subject and qualifiers:
        return f"{subject} {qualifiers}"
    if subject:
        return subject
    if qualifiers:
        return f"Tracks {qualifiers}"
    return "Inspiration Filter"


def _post_track_query(config: StudioConfig, filters: list[dict]) -> list[dict]:
    if not config.inspiration_server or not config.inspiration_api_key:
        raise InspirationError(
            "inspiration_server and inspiration_api_key must be set (takeloom setup-studio)."
        )

    server = config.inspiration_server.rstrip("/")
    payload = json.dumps({"filters": filters}).encode()
    req = urllib.request.Request(
        f"{server}/library/api/tracks/",
        data=payload,
        headers={"Authorization": f"Bearer {config.inspiration_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        # Includes a plain socket timeout — urllib wraps that as a
        # URLError(reason=socket.timeout(...)) rather than raising it bare.
        # A stuck/unreachable inspiration server used to be able to hang
        # this call forever (no timeout was set at all); over Remote that
        # blocked the whole connection's request queue behind it — see
        # remote/server.py's per-request threading, added for the same
        # reason.
        raise InspirationError(f"Error contacting server: {e}") from e
    return data.get("tracks", [])


def search_tracks_by_filter(config: StudioConfig, filter_criteria: dict) -> list[dict]:
    """Query the inspiration server for every track matching one arbitrary
    filter dict (e.g. {"artist": "Miles Davis"} or {"genre": "Rock"}).
    Backs both a setlist "inspiration filter" slot's random draw each
    session (see backend.py's _resolve_filter_slot) and the "Show
    tracks..." preview of what a slot currently matches."""
    if not filter_criteria:
        raise InspirationError("This filter slot has no filter criteria set.")
    return _post_track_query(config, [filter_criteria])


def average_duration(tracks: list[dict]) -> float:
    """Mean duration (seconds) across `tracks` (inspiration-server track
    dicts, as from search_tracks_by_filter) — a filter slot has no
    single fixed song of its own, so this stands in as its
    duration_seconds for setlist display and the total-runtime sum.
    0.0 if there's nothing to average (an unmatched filter, or tracks
    missing duration data)."""
    durations = [t["duration"] for t in tracks if t.get("duration")]
    if not durations:
        return 0.0
    return sum(durations) / len(durations)


def _get_suggestions(config: StudioConfig, kind: str, params: dict) -> list:
    """GET one of the inspiration server's autocomplete endpoints (see
    docs/inspiration-server-autocomplete-api.md). Autocomplete fires on
    every keystroke and isn't a user-triggered action the way search/
    download are, so failures here are swallowed and return [] rather
    than raising InspirationError — a slow/unreachable/unconfigured
    server should just mean no suggestions, not an error popup while
    someone is mid-word."""
    if not config.inspiration_server or not config.inspiration_api_key or not params.get("q"):
        return []
    server = config.inspiration_server.rstrip("/")
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{server}/library/api/autocomplete/{kind}/?{query}",
        headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, ValueError, OSError):
        return []
    return data.get("suggestions", [])


def search_artist_suggestions(config: StudioConfig, partial: str, limit: int = 10) -> list[str]:
    """Autocomplete suggestions for an inspiration filter's Artist field."""
    return _get_suggestions(config, "artists", {"q": partial.strip(), "limit": limit})


def build_inspiration_track_entry(track_info: dict) -> TrackEntry:
    """Construct a TrackEntry from an inspiration track record, without
    adding it to any project's setlist — used for a session-only,
    throwaway resolution (a setlist "filter slot"'s random draw — see
    backend.py's _resolve_filter_slot) that should never persist as a
    setlist entry itself."""
    track_id = track_info["id"]
    artist = track_info.get("artist", "Unknown")
    title = track_info.get("title", "Unknown")
    year = track_info.get("year", "")
    fmt = track_info.get("format", "flac") or "flac"
    duration = float(track_info.get("duration") or 0)
    year_str = f" ({year})" if year else ""
    name = f"{artist} - {title}{year_str}"
    return TrackEntry(
        name=name,
        backing_track=f"inspiration_{track_id}.{fmt}",
        duration_seconds=duration,
        inspiration_track_id=track_id,
    )


_DOWNLOAD_CHUNK_SIZE = 65536


def download_inspiration_track(
    track: TrackEntry, backing_path: Path, config: StudioConfig, on_progress: ProgressCallback | None = None,
) -> None:
    """Download an inspiration track's audio to backing_path. Inspiration
    files are full-quality (often FLAC) and can take a while, so this
    streams in chunks and reports live progress the same way
    youtube.download_youtube_video does, rather than blocking silently
    on a single resp.read()."""
    server = config.inspiration_server.rstrip("/")
    url = f"{server}/library/api/tracks/{track.inspiration_track_id}/download/"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            read = 0
            with open(backing_path, "wb") as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    read += len(chunk)
                    if on_progress:
                        if total:
                            on_progress(read / total * 100, f"Downloading {track.name}... ({read // 1024} / {total // 1024} KB)")
                        else:
                            on_progress(None, f"Downloading {track.name}... ({read // 1024} KB)")
            if total is not None and read != total:
                # The server said how big the file was but the connection
                # dropped (or otherwise stopped) before all of it arrived —
                # resp.read() just returns b"" at that point rather than
                # raising, so without this check a truncated download would
                # silently look like a completed one.
                raise InspirationError(f"Download incomplete: got {read} of {total} bytes.")
    except urllib.error.URLError as e:
        # A partial file left behind here would look "already downloaded"
        # to the next caller's exists() check, permanently leaving a
        # truncated/corrupt backing track in place.
        backing_path.unlink(missing_ok=True)
        raise InspirationError(f"Download failed: {e}") from e
    except Exception:
        backing_path.unlink(missing_ok=True)
        raise
