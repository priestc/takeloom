"""Inspiration track queries and downloads against the radioserver library."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .config import StudioConfig
from .net import terse_source_note
from .project import Project, TrackEntry
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
    """Auto-derive a human-readable label for a song set slot
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


def _post_track_query(config: StudioConfig, filters: list[dict], all_matches: bool = False) -> list[dict]:
    """POST /library/api/tracks/. By default radioserver answers with a
    radio-style random sample of at most 100 matches; `all_matches` asks
    for every match instead (radioserver 41eda00+ — an older server just
    ignores it and samples as before)."""
    if not config.inspiration_server or not config.inspiration_api_key:
        raise InspirationError(
            "inspiration_server and inspiration_api_key must be set (takeloom setup-studio)."
        )

    server = config.inspiration_server.rstrip("/")
    url = f"{server}/library/api/tracks/"
    body: dict = {"filters": filters}
    if all_matches:
        body["all"] = True
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {config.inspiration_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        # A broad filter (e.g. just {"artist": "Bob Dylan"} against a large
        # library) can measurably take the inspiration server 30+ seconds
        # to answer — 15s used to cut that off mid-query with a confusing
        # raw TimeoutError (see the except clause below) instead of ever
        # getting a real answer.
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        # A timeout connecting is wrapped by urllib as URLError(reason=
        # TimeoutError(...)), but a timeout (or dropped connection) while
        # waiting on the response itself — after the connection succeeded
        # — comes through as a bare OSError/TimeoutError instead (urllib
        # only wraps failures from sending the request, not from
        # h.getresponse()), so both must be caught here or a slow/stalled
        # server crashes the calling thread instead of surfacing a clean
        # error. A stuck/unreachable inspiration server used to be able to
        # hang this call forever (no timeout was set at all); over Remote
        # that blocked the whole connection's request queue behind it —
        # see remote/server.py's per-request threading, added for the same
        # reason. Always name the URL — a bare "url failed" reason (e.g. a
        # DNS miss) is close to useless without knowing what was being hit
        # — and flag that the reason itself is as terse as it gets.
        raise InspirationError(f"Error contacting server at {url}: {e}{terse_source_note(e)}") from e
    return data.get("tracks", [])


def search_tracks_by_filter(config: StudioConfig, filter_criteria: dict, all_matches: bool = False) -> list[dict]:
    """Query the inspiration server for every track matching one arbitrary
    filter dict (e.g. {"artist": "Miles Davis"} or {"genre": "Rock"}).
    Backs the song set builder's "Add from filter" tab (see
    ui/song_set_dialog.py)."""
    if not filter_criteria:
        raise InspirationError("Enter at least one filter field.")
    return _post_track_query(config, [filter_criteria], all_matches=all_matches)


def search_inspiration_tracks(config: StudioConfig, artist: str = "", title: str = "") -> list[dict]:
    """Query radioserver directly by artist and/or title — backs the Add
    to Setlist dialog's "Inspiration" tab (add one exact track), as
    opposed to search_tracks_by_filter's broader browsing."""
    filters = {k: v for k, v in {"artist": artist.strip(), "title": title.strip()}.items() if v}
    if not filters:
        raise InspirationError("Enter an artist and/or title to search.")
    # all_matches: an exact lookup must see every artist/title match, not a
    # random 100 of them (which could leave out the very song asked for).
    tracks = _post_track_query(config, [filters], all_matches=True)
    if not tracks:
        raise InspirationError(f"No match found for {_describe(artist, title)}.")
    return tracks


def _describe(artist: str, title: str) -> str:
    if artist and title:
        return f'"{artist} - {title}"'
    return f'"{artist or title}"'


def select_best_match(tracks: list[dict], artist: str, title: str) -> dict:
    """Pick the track that actually matches what was searched for, out of
    whatever /library/api/tracks/'s filter search returned. That endpoint
    is built for broad library-browsing filters (see search_tracks_by_
    filter) rather than a precise "find this one song" lookup, so it can
    return loosely-related tracks alongside — or instead of — an exact
    hit (e.g. matching just the artist and ignoring an unmatched title).
    Requiring an exact, case-insensitive match on whichever of
    artist/title was actually given — and raising rather than guessing
    when there isn't one — is what stops a search like "Bob Dylan" /
    "Are You Ready" from silently adding some other Bob Dylan track
    instead."""
    artist_norm = artist.strip().lower()
    title_norm = title.strip().lower()

    def is_exact(t: dict) -> bool:
        if artist_norm and t.get("artist", "").strip().lower() != artist_norm:
            return False
        if title_norm and t.get("title", "").strip().lower() != title_norm:
            return False
        return True

    exact = [t for t in tracks if is_exact(t)]
    if exact:
        return exact[0]
    raise InspirationError(
        f"No exact match for {_describe(artist, title)} — the server returned "
        f"{len(tracks)} similar track(s) instead. Try adjusting the artist/title."
    )


def average_duration(tracks: list[dict]) -> float:
    """Mean duration (seconds) across `tracks` (inspiration-server track
    dicts, as from search_tracks_by_filter) — a song set slot has no
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


def search_title_suggestions(config: StudioConfig, partial: str, artist: str = "", limit: int = 10) -> list[dict]:
    """Autocomplete suggestions for the Add to Setlist dialog's Inspiration
    tab Title field, optionally narrowed to a specific artist. Each result
    is a track dict (id/artist/title/year/format/duration) — see
    docs/inspiration-server-autocomplete-api.md — so selecting one can add
    that exact track directly, with no secondary by-name search needed.
    Tolerates an older server still returning bare title strings
    (normalized here to a dict with no "id"), which just means the caller
    falls back to the by-name search path for that selection."""
    params = {"q": partial.strip(), "limit": limit}
    if artist.strip():
        params["artist"] = artist.strip()
    raw = _get_suggestions(config, "titles", params)
    return [item if isinstance(item, dict) else {"title": item} for item in raw]


def build_inspiration_track_entry(track_info: dict) -> TrackEntry:
    """Construct a TrackEntry from an inspiration track record, without
    adding it to any project's setlist — used for a session-only,
    throwaway resolution (a setlist "song set slot"'s random draw — see
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


def find_or_add_inspiration_track(project: Project, track_info: dict) -> TrackEntry:
    """Return the setlist entry for an inspiration track, creating it if
    absent — so adding the same track twice (from the Add to Setlist
    dialog, or across sessions) reuses the one existing entry rather than
    duplicating it."""
    track_id = track_info["id"]
    for entry in project.setlist.tracks:
        if entry.inspiration_track_id == track_id:
            return entry
    entry = build_inspiration_track_entry(track_info)
    project.setlist.add_track(entry)
    return entry


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
                raise InspirationError(f"Download incomplete: got {read} of {total} bytes from {url}.")
    except urllib.error.URLError as e:
        # A partial file left behind here would look "already downloaded"
        # to the next caller's exists() check, permanently leaving a
        # truncated/corrupt backing track in place.
        backing_path.unlink(missing_ok=True)
        raise InspirationError(f"Download failed from {url}: {e}{terse_source_note(e)}") from e
    except Exception:
        backing_path.unlink(missing_ok=True)
        raise
