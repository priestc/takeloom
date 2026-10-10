"""Server-rendered HTML for the vault's browser UI: the session list (with
any pending pairing requests on top), one session's page (video player
with per-song jump links, its takes, its raw audio), and the paired-
clients page. Plain HTML + a few lines of inline JS — no build step, no
dependencies, same as the rest of this package."""

from __future__ import annotations

import datetime
import time
from html import escape
from pathlib import Path
from urllib.parse import quote

from .auth import PAIRING_TTL_SECONDS, PairedClient, PairingRequest
from .store import Vault

_CSS = """
:root { color-scheme: light dark; --muted: #888; --line: #8884; --accent: #4a7dff; }
body { font: 15px/1.45 -apple-system, system-ui, sans-serif; margin: 0 auto; max-width: 1100px; padding: 1.2em; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
h1 { font-size: 1.4em; margin: .2em 0 .8em; } h2 { font-size: 1.1em; margin: 1.6em 0 .5em; }
header { display: flex; gap: 1.2em; align-items: baseline; border-bottom: 1px solid var(--line); margin-bottom: 1em; }
header .brand { font-weight: 600; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .45em .6em; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-weight: 600; font-size: .85em; color: var(--muted); }
.muted { color: var(--muted); } .small { font-size: .85em; }
.pill { display: inline-block; font-size: .75em; padding: .05em .55em; border-radius: 1em; border: 1px solid var(--line); margin-right: .25em; }
.banner { border: 2px solid #e0a800; border-radius: 8px; padding: .6em 1em; margin-bottom: 1.2em; }
.code { font: 600 1.3em ui-monospace, monospace; letter-spacing: .12em; }
button { font: inherit; padding: .25em .9em; border-radius: 6px; border: 1px solid var(--line); cursor: pointer; }
button.ok { background: #2e9d4f; color: #fff; border-color: #2e9d4f; }
video { width: 100%; max-height: 70vh; background: #000; border-radius: 6px; }
audio { width: 100%; max-width: 520px; }
.songs button { font-size: .85em; padding: .1em .6em; }
form { display: inline; }
"""


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · Takeloom Vault</title><style>{_CSS}</style></head><body>"
        "<header><a class='brand' href='/'>Takeloom Vault</a><a href='/'>Sessions</a>"
        "<a href='/clients'>Paired devices</a></header>"
        f"{body}</body></html>"
    )


def _media_url(relative: str) -> str:
    return "/media/" + quote(relative)


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_date(wall_time: str) -> str:
    try:
        dt = datetime.datetime.strptime(wall_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return wall_time
    return dt.strftime("%a %b %-d, %Y · %-I:%M %p")


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _track_names(data: dict) -> list[str]:
    names: list[str] = []
    for e in data.get("events", []):
        name = e.get("track_name")
        if name and name not in names:
            names.append(name)
    return names


def _take_count(data: dict) -> int | None:
    if "takes" not in data:
        return None
    return sum(len(v) for v in (data.get("takes") or {}).values())


def _pairing_banner(pending: list[PairingRequest]) -> str:
    if not pending:
        return ""
    rows = []
    for req in pending:
        remaining = max(0, int(PAIRING_TTL_SECONDS - (time.time() - req.created)))
        rows.append(
            "<p><b>{name}</b> <span class='muted'>({ip})</span> wants to pair with this vault. "
            "Check that it shows code <span class='code'>{code}</span>, then "
            "<form method='post' action='/pairing/{id}/approve'><button class='ok'>Approve</button></form> "
            "<form method='post' action='/pairing/{id}/deny'><button>Deny</button></form> "
            "<span class='muted small'>expires in {mins}:{secs:02d}</span></p>".format(
                name=escape(req.client_name), ip=escape(req.ip), code=escape(req.code), id=quote(req.id),
                mins=remaining // 60, secs=remaining % 60,
            )
        )
    # Pending requests appear without the page being reloaded by hand.
    return "<div class='banner'>" + "".join(rows) + "</div>"


def sessions_page(vault: Vault, logs: dict[str, dict], pending: list[PairingRequest]) -> str:
    rows = []
    for name, data in sorted(
        logs.items(), key=lambda kv: (kv[1].get("events") or [{}])[0].get("wall_time", ""), reverse=True,
    ):
        events = data.get("events") or []
        directory = vault.session_dir(name)
        has_video = (directory / "session_video.mp4").exists()
        takes = _take_count(data)
        tracks = _track_names(data)
        rows.append(
            "<tr><td><a href='/sessions/{q}'>{date}</a></td><td>{project}</td><td>{instrument}</td>"
            "<td>{duration}</td><td>{songs}</td><td>{takes}</td><td>{video}</td><td class='muted small'>{size}</td></tr>".format(
                q=quote(name),
                date=escape(_format_date(events[0]["wall_time"]) if events else name),
                project=escape(data.get("project", "")),
                instrument=escape(data.get("instrument", "")),
                duration=_format_duration(events[-1]["timestamp"]) if events else "",
                songs=len(tracks),
                takes="<span class='muted'>pending</span>" if takes is None else takes,
                video="▶" if has_video else "",
                size=_format_size(vault.session_size(name)),
            )
        )
    table = (
        "<table><tr><th>Date</th><th>Project</th><th>Instrument</th><th>Length</th><th>Songs</th>"
        "<th>Takes</th><th>Video</th><th>Size</th></tr>" + "".join(rows) + "</table>"
        if rows else "<p class='muted'>No sessions in the vault yet.</p>"
    )
    refresh = "<meta http-equiv='refresh' content='5'>" if pending else ""
    return page("Sessions", refresh + _pairing_banner(pending) + f"<h1>Sessions <span class='muted small'>{len(rows)}</span></h1>" + table)


def session_page(vault: Vault, name: str, data: dict) -> str:
    directory = vault.session_dir(name)
    rel = f"sessions/{name}"
    events = data.get("events") or []
    sample_rate = data.get("sample_rate") or 48000
    mix_start = data.get("mix_start_frame") or 0

    meta = " · ".join(escape(x) for x in (
        data.get("project", ""), data.get("instrument", ""), data.get("musician", ""),
        ", ".join(filter(None, (data.get("studio_name", ""), data.get("studio_location", "")))),
    ) if x)
    parts = [
        f"<h1>{escape(_format_date(events[0]['wall_time']) if events else name)}</h1>",
        f"<p class='muted'>{meta}<br><span class='small'>{escape(name)}</span></p>",
    ]

    has_video = (directory / "session_video.mp4").exists()
    if has_video:
        parts.append(f"<video id='v' controls preload='metadata' src='{_media_url(rel + '/session_video.mp4')}'></video>")
    elif (directory / "session_video_raw.mp4").exists():
        parts.append(
            "<p class='banner'>This session's video was never processed into a playable file "
            "(only <code>session_video_raw.mp4</code> exists — the capture likely wasn't finalized).</p>"
        )

    # One row per play-through of a song: where its recording started, in
    # the session video's own timeline. session_video.mp4 is muxed against
    # session_mix.flac, which begins at mix_start_frame of session.flac —
    # so an event's frame minus that is its offset into the video.
    takes_by_index = data.get("takes") or {}
    song_rows = []
    for e in events:
        if e.get("event_type") != "record_start":
            continue
        frame = e.get("frame")
        offset = max(0.0, (frame - mix_start) / sample_rate) if frame is not None else e.get("timestamp", 0.0)
        jump = f"<button onclick='seek({offset:.2f})'>▶ {_format_duration(offset)}</button>" if has_video else _format_duration(offset)
        take_links = []
        for take in takes_by_index.get(str(e.get("track_index")), []):
            filename = take.get("filename", "")
            stem = Path(filename).stem
            links = []
            if (vault.root / "completed_takes" / filename).exists():
                links.append(f"<a href='{_media_url('completed_takes/' + filename)}'>audio</a>")
            if take.get("has_video") and (vault.root / "completed_takes" / f"{stem}.mp4").exists():
                links.append(f"<a href='{_media_url('completed_takes/' + stem + '.mp4')}'>video</a>")
            take_links.append(
                f"<span class='pill'>{escape(take.get('instrument', ''))} take {take.get('take_number', '')}</span>"
                + " ".join(links)
            )
        song_rows.append(
            f"<tr><td>{jump}</td><td>{escape(e.get('track_name', ''))}</td><td>{'<br>'.join(take_links)}</td></tr>"
        )
    if song_rows:
        parts.append(
            "<h2>Songs</h2><table class='songs'><tr><th>Starts</th><th>Song</th><th>Takes filed</th></tr>"
            + "".join(song_rows) + "</table>"
        )

    files = []
    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        url = _media_url(f"{rel}/{f.name}")
        player = f"<br><audio controls preload='none' src='{url}'></audio>" if f.suffix == ".flac" else ""
        files.append(
            f"<tr><td><a href='{url}' download>{escape(f.name)}</a>{player}</td>"
            f"<td class='muted small'>{_format_size(f.stat().st_size)}</td></tr>"
        )
    parts.append("<h2>Files</h2><table>" + "".join(files) + "</table>")
    parts.append(
        "<script>function seek(t){var v=document.getElementById('v');v.currentTime=t;v.play();"
        "v.scrollIntoView({behavior:'smooth'});}</script>"
    )
    return page(name, "".join(parts))


def clients_page(clients: list[PairedClient], pending: list[PairingRequest]) -> str:
    rows = "".join(
        "<tr><td>{label}</td><td>{ip}</td><td>{paired}</td><td>{seen}</td>"
        "<td><form method='post' action='/clients/{id}/revoke'><button>Revoke</button></form></td></tr>".format(
            label=escape(c.label), ip=escape(c.last_ip), paired=escape(c.paired_at.replace("T", " ")),
            seen=escape(c.last_seen_at.replace("T", " ")), id=quote(c.id),
        )
        for c in clients
    )
    body = _pairing_banner(pending) + "<h1>Paired devices</h1>" + (
        "<table><tr><th>Device</th><th>Last IP</th><th>Paired</th><th>Last seen</th><th></th></tr>" + rows + "</table>"
        if clients else "<p class='muted'>Nothing is paired yet. Use “Pair” next to the vault server "
        "address in takeloom's Studio Setup tab.</p>"
    )
    return page("Paired devices", body)


def message_page(title: str, message: str) -> str:
    return page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href='/'>Back</a></p>")
