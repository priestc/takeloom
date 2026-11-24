"""Server-rendered HTML for the vault's browser UI: the session list (with
any pending pairing requests on top), one session's page (video player
with per-song jump links, its takes, its raw audio), and the paired-
clients page. Plain HTML + a few lines of inline JS — no build step, no
dependencies, same as the rest of this package."""

from __future__ import annotations

import datetime
import json
import re
import time
from html import escape
from pathlib import Path
from urllib.parse import quote

from .auth import PAIRING_TTL_SECONDS, PairedClient, PairingRequest
from .store import Vault
from .takes import camera_refs

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
button.danger { color: #c44f6d; border-color: #c44f6d; background: none; }
button.danger:hover { background: #c44f6d; color: #fff; }
button.cam.on { background: #2a6db0; color: #fff; border-color: #2a6db0; }
video { width: 100%; max-height: 70vh; background: #000; border-radius: 6px; }
audio { width: 100%; max-width: 520px; }
.songs button { font-size: .85em; padding: .1em .6em; }
form { display: inline; }
.badge { display: inline-block; color: #fff; font-size: .75em; font-weight: 600; padding: .05em .45em; border-radius: 4px; margin-right: .25em; }
.wt { width: 100%; min-width: 10em; box-sizing: border-box; }
.wt.wt-saved { border-color: #2e9d57; }
.wt.wt-failed { border-color: #d33; }
.ct { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.6em; align-items: start; }
@media (max-width: 800px) { .ct { grid-template-columns: 1fr; } }
#mixer { position: sticky; top: .5em; border: 1px solid var(--line); border-radius: 8px; padding: .8em 1em; }
#mixer h2 { margin-top: 0; }
h1 button { font-size: .55em; vertical-align: middle; }
.transport { display: flex; gap: .6em; align-items: center; margin-bottom: .8em; }
.transport input[type=range] { flex: 1; }
.strips { display: flex; gap: 1em; overflow-x: auto; padding-bottom: .4em; }
.strip { display: flex; flex-direction: column; align-items: center; gap: .3em; min-width: 5.5em; font-size: .85em; }
.strip.empty { justify-content: flex-end; }
.strip label { align-self: flex-start; }
.fader { writing-mode: vertical-lr; direction: rtl; height: 170px; width: 1.6em; }
.pct { font-variant-numeric: tabular-nums; }
.buttons { display: flex; gap: .5em; align-items: center; flex-wrap: wrap; margin-top: .8em; }
.trim { margin-top: 1em; border-top: 1px solid var(--line); padding-top: .6em; }
.trim summary { cursor: pointer; font-weight: 600; }
.trimrow { display: flex; gap: .6em; align-items: center; flex-wrap: wrap; }
.trimrow input { width: 5.5em; }
.filters { display: flex; gap: 1em; align-items: center; flex-wrap: wrap; margin-bottom: .6em; }
tr.song { cursor: pointer; } tr.song:hover { background: #8881; } tr.song.on b { color: var(--accent); }
tr.take td { border-bottom: none; padding: .15em .6em .15em 2.2em; font-size: .85em; }
.tri { display: inline-block; width: 1.3em; color: var(--muted); }
"""


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · Takeloom Vault</title><style>{_CSS}</style></head><body>"
        "<header><a class='brand' href='/'>Takeloom Vault</a><a href='/'>Sessions</a>"
        "<a href='/completed'>Completed takes</a><a href='/cameras'>Cameras</a><a href='/clients'>Paired devices</a></header>"
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


_GENERIC_PHONE_NAMES = {"", "iphone", "ipad", "ipod touch"}


def camera_model(phone: dict, data: dict) -> str:
    """The phone's model ("iPhone 16 Pro Max"): logged since takeloom
    started recording it; for an older session, from a program-camera
    event naming it ("Takeloom Camera: iPhone 13 mini"), else its own name
    if that says more than "iPhone". "" for a local camera / unknown."""
    if phone.get("model"):
        return phone["model"]
    device = f"takeloom-camera:{phone['device_id']}" if phone.get("device_id") else ""
    for e in data.get("events") or []:
        if device and e.get("event_type") == "program_camera" and e.get("camera") == device:
            label = (e.get("details") or "").split(";")[0].removeprefix("label=").strip()
            if label.startswith("Takeloom Camera: "):
                return label[len("Takeloom Camera: "):]
    name = (phone.get("client_name") or "").strip()
    if name.lower() in _GENERIC_PHONE_NAMES:
        return ""
    # "Chris's Galaxy S20 FE 5G" -> "Galaxy S20 FE 5G" (as takeloom's
    # phone_camera/models.py strip_owner).
    m = re.fullmatch(r"\S.*?['’]s (.+)", name)
    return m.group(1) if m else name


def camera_title(label: str, phone: dict, data: dict) -> str:
    """How a camera is named on these pages: always with the phone's model
    ("Iphone 16 · iPhone 16 Pro Max"; just the model when there's no name
    or the name already says it)."""
    model = camera_model(phone, data)
    label = (label or "").strip() or model or "Camera"
    if model and model.lower() not in label.lower():
        return f"{label} · {model}"
    return label


def session_videos(directory: Path, data: dict) -> list[tuple[str, str, str]]:
    """(title, file, camera label) of each finished camera video in a
    session: one per camera ("extra_cameras" lists every camera since
    cameras became a set), or session_video.mp4 for a session from before
    that. The label is what take refs name the camera by."""
    videos = []
    if (directory / "session_video.mp4").exists():
        phone = data.get("phone_camera") or {}
        label = phone.get("label") or data.get("camera_label") or ""
        videos.append((camera_title(label, phone, data), "session_video.mp4", label))
    for cam in data.get("extra_cameras") or []:
        file = cam.get("final_file") or ""
        if file and "/" not in file and (directory / file).exists():
            label = cam.get("label") or ""
            videos.append((camera_title(label, cam.get("phone_camera") or {}, data), file, label or file))
    return videos


_CAMERA_STATE_STYLE = {
    "full": ("#2e9d4f", "Full quality"),
    "lining": ("#2a6db0", "Lining up"),
    "stream": ("#e0a800", "Stream quality only"),
    "encoding": ("#2a6db0", "Encoding"),
    "missing": ("#c44f6d", "No video"),
}

_probe_cache: dict[tuple[str, int], str] = {}


def _video_resolution(path: Path) -> str:
    """"1920×1080" (cached per file version), or "" if it can't be read."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return ""
    if key not in _probe_cache:
        import subprocess
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        _probe_cache[key] = out.replace("x", "×") if out else ""
    return _probe_cache[key]


def camera_statuses(directory: Path, data: dict) -> list[dict]:
    """Where each camera's video for this session stands, for showing on
    the session pages: {"title", "state", "detail", "resolution"}, state
    one of "full" (a phone's own recording, or a local camera's capture),
    "stream" (a phone's video made from its live preview feed, because its
    full-quality recording never reached takeloom — see takeloom's
    phone_camera/sync.py), "encoding" (left for this vault to encode and
    not done yet) or "missing"."""
    jobs_pending = set()
    if (directory / "render_jobs.json").is_file():
        try:
            jobs = json.loads((directory / "render_jobs.json").read_text()).get("jobs") or []
            jobs_pending = {j.get("output") for j in jobs if not j.get("done")}
        except (OSError, ValueError):
            pass

    def status(title: str, file: str, sync: dict, is_phone: bool, upload_file: str = "") -> dict:
        exists = bool(file) and "/" not in file and (directory / file).is_file()
        source = (sync or {}).get("source")
        if not exists:
            if file in jobs_pending:
                state, detail = "encoding", "Left for this vault to encode; not done yet."
            else:
                state, detail = "missing", "There's no video file for this camera."
        elif is_phone and source == "live feed":
            state = "stream"
            detail = ("Made from the phone's live preview stream: its full-quality recording never reached "
                      "takeloom. If it's still on the phone, open Takeloom Camera there and it'll upload, "
                      "then replace this.")
            part = directory / (upload_file + ".part") if upload_file else None
            if part is not None and part.is_file():
                detail += f" (A cut-off upload of {_format_size(part.stat().st_size)} is also here.)"
        else:
            state = "full"
            if not is_phone:
                detail = "Recorded by takeloom from a camera attached to the studio machine."
            elif (sync or {}).get("late"):
                detail = "The phone's own recording (it arrived after the session was processed)."
            else:
                detail = "The phone's own recording."
            if is_phone and sync.get("method") == "clock":
                detail += " Lined up by clock sync only (no audio match), so sync may be off by a frame or two."
        return {"title": title, "state": state, "detail": detail,
                "resolution": _video_resolution(directory / file) if exists else ""}

    found = []
    phone = data.get("phone_camera") or {}
    if phone or (directory / "session_video.mp4").exists():
        label = phone.get("label") or data.get("camera_label") or ""
        found.append(status(camera_title(label, phone, data), "session_video.mp4", data.get("video_sync") or {},
                            bool(phone)))
    for cam in data.get("extra_cameras") or []:
        cam_phone = cam.get("phone_camera") or {}
        found.append(status(camera_title(cam.get("label") or "", cam_phone, data), cam.get("final_file") or "",
                            cam.get("video_sync") or {}, bool(cam_phone), cam.get("upload_file") or ""))
    return found


def _camera_status_table(statuses: list[dict]) -> str:
    rows = []
    for st in statuses:
        color, word = _CAMERA_STATE_STYLE[st["state"]]
        rows.append(
            f"<tr><td>{escape(st['title'])}</td>"
            f"<td><span class='pill' style='border-color:{color};color:{color}'>{word}</span></td>"
            f"<td class='muted small'>{escape(st['resolution'])}</td>"
            f"<td class='small'>{escape(st['detail'])}</td></tr>"
        )
    return "<h2>Cameras</h2><table>" + "".join(rows) + "</table>"


def _video_column(statuses: list[dict], has_video: bool) -> str:
    """The sessions list's Video cell: ▶, plus what's not right yet."""
    states = [st["state"] for st in statuses]
    marks = []
    if "stream" in states:
        n = states.count("stream")
        marks.append(f"<span class='pill' style='border-color:#e0a800;color:#e0a800' "
                     f"title='{n} camera(s) only have the live preview stream'>stream quality</span>")
    if "encoding" in states:
        marks.append("<span class='pill' style='border-color:#2a6db0;color:#2a6db0'>encoding</span>")
    if "missing" in states and has_video:
        marks.append("<span class='pill' style='border-color:#c44f6d;color:#c44f6d'>camera missing</span>")
    return ("▶ " if has_video else "") + "".join(marks)


def sessions_page(vault: Vault, logs: dict[str, dict], pending: list[PairingRequest]) -> str:
    rows = []
    for name, data in sorted(
        logs.items(), key=lambda kv: (kv[1].get("events") or [{}])[0].get("wall_time", ""), reverse=True,
    ):
        events = data.get("events") or []
        directory = vault.session_dir(name)
        has_video = bool(session_videos(directory, data))
        statuses = camera_statuses(directory, data)
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
                video=_video_column(statuses, has_video),
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


_EDIT_COLORS = ["#2a6db0", "#d0702a", "#2e9d4f", "#9b4fc4", "#c4a72a", "#c44f6d"]


def _stage_player(rel: str, videos: list[tuple[str, str, str]], shifts: list[float],
                  footer_entries: list[list] | None = None, mics: list[str] | None = None) -> str:
    """For an older session (muxed camera videos; a sources session has
    _sources_player): every camera's video stacked and played together, in
    step, showing one at a time: the one the editor's timeline says
    (Preview edit: the final video as it'll render, cutting as it plays), or
    the one picked with its button. The sound plays from the first camera's
    video (they all carry the session's same two tracks), which the others
    follow, as the render does. Its own controls, since a video's built-in
    ones can't sit over the others. Others drifting more than 0.2 s are
    pulled back into step.
    `shifts`: each video's sync shift (edit.time_shift), so session time T
    is T + shift in that file. `footer_entries` (footer.schedule; None:
    the videos carry their own): the footer, drawn over the picture with
    the camera showing, as the render will draw it. `mics` (URLs of the
    phones' mic tracks, on the session timeline): an Audio switch between
    Processed (the session's, from the audio interface) and Atmospheric
    (those played together, the videos muted). Exposes STAGE for the
    editor."""
    import json as _json
    buttons = "".join(
        f"<button class='cam{' on' if i == 0 else ''}' onclick='STAGE.manual({i})'>{escape(title)}</button>"
        for i, (title, _file, _label) in enumerate(videos)
    )
    angles = "".join(
        f"<video src='{_media_url(rel + '/' + file)}' preload='auto' playsinline muted "
        f"style='position:absolute;inset:0;width:100%;height:100%;object-fit:contain;opacity:{1 if i == 0 else 0}'></video>"
        for i, (_title, file, _label) in enumerate(videos)
    )
    titles = [title for title, _file, _label in videos]
    # The footer: a translucent strip 1/24 of the picture's height, like
    # the one edit.py draws (footer.py), sized from the stage as it resizes.
    footer_div = ("<div id='st-footer' style='position:absolute;left:0;right:0;bottom:0;background:rgba(0,0,0,.43);"
                  "color:rgba(255,255,255,.92);white-space:nowrap;overflow:hidden;display:flex;align-items:center;"
                  "pointer-events:none;font-family:\"DejaVu Sans\",Helvetica,Arial,sans-serif'></div>"
                  if footer_entries is not None else "")
    return (
        f"<p>{buttons if len(videos) > 1 else ''}</p>"
        f"<div id='stage' style='position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:6px;overflow:hidden'>{angles}{footer_div}</div>"
        "<div style='display:flex;gap:.6em;align-items:center;margin:.4em 0'>"
        "<button id='st-play' onclick='STAGE.toggle()' style='min-width:3em'>▶</button>"
        "<input id='st-seek' type='range' min='0' max='1000' value='0' style='flex:1'>"
        "<span id='st-time' class='small muted'>0:00</span>"
        + ("<label class='small'><input id='st-follow' type='checkbox' checked> Preview edit</label>" if len(videos) > 1 else
           "<input id='st-follow' type='checkbox' checked hidden>")
        + "</div>"
        + (("<p class='small'>Audio: <label><input type='radio' name='st-audio' value='processed' checked "
            "onchange='STAGE.setAudio(this.value)'> Processed (audio interface)</label> "
            "<label><input type='radio' name='st-audio' value='atmospheric' onchange='STAGE.setAudio(this.value)'> "
            f"Atmospheric ({len(mics)} phone mic{'s' if len(mics) != 1 else ''})</label></p>"
            "<div id='stage-mics' hidden>"
            + "".join(f"<audio src='{url}' preload='auto'></audio>" for url in mics) + "</div>")
           if mics else "")
        + f"<script>var STAGE=(function(SH,FOOT,TITLES){{"
        "var V=[].slice.call(document.querySelectorAll('#stage video')),master=0,shown=0,manual=0,camAt=null,playing=false,drag=false;"
        "var A=[].slice.call(document.querySelectorAll('#stage-mics audio')),mode='processed';"
        "var seekEl=document.getElementById('st-seek'),follow=document.getElementById('st-follow');"
        "function fmt(t){t=Math.max(0,Math.round(t));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');}"
        "function now(){return V[master].currentTime-SH[master];}"
        "function dur(){return Math.max(0,(V[master].duration||0)-SH[master]);}"
        "function show(i){if(i===shown)return;V[shown].style.opacity=0;V[i].style.opacity=1;shown=i;"
        "document.querySelectorAll('button.cam').forEach(function(b,j){b.classList.toggle('on',j===i);});}"
        "function setMaster(i){master=i;V.forEach(function(v,j){v.muted=mode==='atmospheric'||j!==i;});}"
        "function setAudio(m){mode=m;setMaster(master);if(m!=='atmospheric')A.forEach(function(a){a.pause();});else if(playing)align();"
        "document.querySelectorAll('input[name=st-audio]').forEach(function(r){r.checked=r.value===m;});}"
        "function align(){var T=now();V.forEach(function(v,j){if(j===master)return;var w=T+SH[j];"
        "if(Math.abs(v.currentTime-w)>0.2)v.currentTime=Math.max(0,w);if(playing&&v.paused)v.play().catch(function(){});});"
        "if(mode==='atmospheric')A.forEach(function(a){if(Math.abs(a.currentTime-T)>0.25)a.currentTime=Math.max(0,T);"
        "if(playing&&a.paused)a.play().catch(function(){});});}"
        "function seek(T){V.forEach(function(v,j){v.currentTime=Math.max(0,T+SH[j]);});A.forEach(function(a){a.currentTime=Math.max(0,T);});}"
        "function play(){playing=true;document.getElementById('st-play').textContent='❚❚';"
        "V[master].play().catch(function(){});align();}"
        "function pause(){playing=false;document.getElementById('st-play').textContent='▶';V.forEach(function(v){v.pause();});A.forEach(function(a){a.pause();});}"
        "var footEl=document.getElementById('st-footer'),footText='';"
        "function fit(){if(!footEl)return;var h=document.getElementById('stage').clientHeight,b=Math.max(14,h/24);"
        "footEl.style.height=b+'px';footEl.style.fontSize=Math.max(9,b*0.6)+'px';footEl.style.paddingLeft=(14*h/1080)+'px';}"
        "window.addEventListener('resize',fit);fit();"
        "function footer(T){if(!footEl||!FOOT)return;var t=FOOT[0][1];for(var i=0;i<FOOT.length&&FOOT[i][0]<=T;i++)t=FOOT[i][1];"
        "t=t+'   \u2022   '+TITLES[shown];if(t!==footText){footText=t;footEl.textContent=t;}}"
        "function frame(){var T=now();show(follow.checked&&camAt?camAt(T):manual);footer(T);"
        "if(!drag&&dur())seekEl.value=Math.round(1000*T/dur());"
        "document.getElementById('st-time').textContent=fmt(T)+' / '+fmt(dur());if(playing)align();requestAnimationFrame(frame);}"
        "seekEl.addEventListener('input',function(){drag=true;seek(seekEl.value/1000*dur());});"
        "seekEl.addEventListener('change',function(){drag=false;});"
        "V.forEach(function(v){v.addEventListener('ended',function(){if(v===V[master])pause();});});"
        "setMaster(0);requestAnimationFrame(frame);"
        "return{duration:dur,seek:seek,play:play,pause:pause,toggle:function(){playing?pause():play();},"
        "setMaster:setMaster,setCamAt:function(f){camAt=f;},setAudio:setAudio,audio:function(){return mode;},"
        "manual:function(i){follow.checked=false;manual=i;show(i);},"
        "following:function(){return follow.checked;},"
        "ready:function(cb){var m=V[0];if(m.readyState>=1)cb();else m.addEventListener('loadedmetadata',function f(){m.removeEventListener('loadedmetadata',f);cb();});},"
        "element:function(){return document.getElementById('stage');}};"
        f"}})({_json.dumps(shifts)},{_json.dumps(footer_entries)},{_json.dumps(titles)});</script>"
    )


def _sources_player(rel: str, cameras, mix_url: str) -> str:
    """The player for a session whose cameras are sources (cameras.py): the
    session's own sound (its mix reference) is the clock, and every
    camera's own file follows it, placed by its alignment — file time
    (T - zero) / (1 + drift) for session time T — played at 1 / (1 + drift),
    nudged up to 2% faster or slower toward where it should be (a muted
    video: no one hears it), and re-seeked if it's ever 0.15 s off (a stall).
    Checked in headless Chrome with a camera at 40 times real drift: held
    within about a frame. Shows one camera at a time: the one the
    editor's timeline says (Preview edit), or the one picked with its
    button. Atmospheric plays the phones' own recordings (their own mics)
    instead of the mix. Same STAGE interface as _stage_player, for the editor."""
    import json as _json
    buttons = "".join(
        f"<button class='cam{' on' if i == 0 else ''}' onclick='STAGE.manual({i})'>{escape(cam.title)}</button>"
        for i, cam in enumerate(cameras)
    )
    angles = "".join(
        f"<video src='{_media_url(rel + '/' + cam.path.name)}' preload='auto' playsinline muted "
        f"style='position:absolute;inset:0;width:100%;height:100%;object-fit:contain;opacity:{1 if i == 0 else 0}'></video>"
        for i, cam in enumerate(cameras)
    )
    place = [[cam.file_zero_s, cam.drift, cam.has_mic] for cam in cameras]
    mics = sum(1 for cam in cameras if cam.has_mic)
    return (
        f"<p>{buttons if len(cameras) > 1 else ''}</p>"
        f"<div id='stage' style='position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:6px;overflow:hidden'>{angles}</div>"
        f"<audio id='st-clock' src='{mix_url}' preload='auto'></audio>"
        "<div style='display:flex;gap:.6em;align-items:center;margin:.4em 0'>"
        "<button id='st-play' onclick='STAGE.toggle()' style='min-width:3em'>▶</button>"
        "<input id='st-seek' type='range' min='0' max='1000' value='0' style='flex:1'>"
        "<span id='st-time' class='small muted'>0:00</span>"
        + ("<label class='small'><input id='st-follow' type='checkbox' checked> Preview edit</label>" if len(cameras) > 1 else
           "<input id='st-follow' type='checkbox' checked hidden>")
        + "</div>"
        + (("<p class='small'>Audio: <label><input type='radio' name='st-audio' value='processed' checked "
            "onchange='STAGE.setAudio(this.value)'> Processed (audio interface)</label> "
            "<label><input type='radio' name='st-audio' value='atmospheric' onchange='STAGE.setAudio(this.value)'> "
            f"Atmospheric ({mics} phone mic{'s' if mics != 1 else ''})</label></p>") if mics else "")
        + f"<script>var STAGE=(function(P){{"
        "var V=[].slice.call(document.querySelectorAll('#stage video')),C=document.getElementById('st-clock');"
        "var shown=0,manual=0,camAt=null,playing=false,drag=false,mode='processed';"
        "var seekEl=document.getElementById('st-seek'),follow=document.getElementById('st-follow');"
        "function fmt(t){t=Math.max(0,Math.round(t));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');}"
        "function want(i,T){return (T-P[i][0])/(1+P[i][1]);}"
        "function now(){return C.currentTime;}function dur(){return C.duration||0;}"
        "function show(i){if(i===shown)return;V[shown].style.opacity=0;V[i].style.opacity=1;shown=i;"
        "document.querySelectorAll('button.cam').forEach(function(b,j){b.classList.toggle('on',j===i);});}"
        "function setAudio(m){mode=m;C.muted=m==='atmospheric';V.forEach(function(v,i){v.muted=!(m==='atmospheric'&&P[i][2]);});"
        "document.querySelectorAll('input[name=st-audio]').forEach(function(r){r.checked=r.value===m;});}"
        "function align(){var T=now();V.forEach(function(v,i){var w=want(i,T);"
        "if(w<0||(v.duration&&w>v.duration)){if(!v.paused)v.pause();return;}"
        "var err=w-v.currentTime;if(Math.abs(err)>0.15){v.currentTime=w;err=0;}"
        "v.playbackRate=(1/(1+P[i][1]))*(1+Math.max(-0.02,Math.min(0.02,err*0.5)));"
        "if(playing&&v.paused)v.play().catch(function(){});});}"
        "function seek(T){C.currentTime=Math.max(0,T);V.forEach(function(v,i){v.currentTime=Math.max(0,want(i,T));});}"
        "function play(){playing=true;document.getElementById('st-play').textContent='❚❚';C.play().catch(function(){});align();}"
        "function pause(){playing=false;document.getElementById('st-play').textContent='▶';C.pause();V.forEach(function(v){v.pause();});}"
        "function frame(){var T=now();show(follow.checked&&camAt?camAt(T):manual);"
        "if(!drag&&dur())seekEl.value=Math.round(1000*T/dur());"
        "document.getElementById('st-time').textContent=fmt(T)+' / '+fmt(dur());if(playing)align();requestAnimationFrame(frame);}"
        "seekEl.addEventListener('input',function(){drag=true;seek(seekEl.value/1000*dur());});"
        "seekEl.addEventListener('change',function(){drag=false;});"
        "C.addEventListener('ended',pause);setAudio('processed');requestAnimationFrame(frame);"
        "return{duration:dur,seek:seek,play:play,pause:pause,toggle:function(){playing?pause():play();},"
        "setCamAt:function(f){camAt=f;},setAudio:setAudio,audio:function(){return mode;},"
        "manual:function(i){follow.checked=false;manual=i;show(i);},following:function(){return follow.checked;},"
        "ready:function(cb){if(C.readyState>=1)cb();else C.addEventListener('loadedmetadata',function f(){C.removeEventListener('loadedmetadata',f);cb();});},"
        "element:function(){return document.getElementById('stage');}};"
        f"}})({_json.dumps(place)});</script>"
    )


def _sources_statuses(directory: Path, data: dict) -> list[dict]:
    """Each camera's current state, for a sources session: straight from its
    sources and the files here, nothing to act on."""
    out = []
    for entry in data.get("extra_cameras") or []:
        title = camera_title(entry.get("label") or "", entry.get("phone_camera") or {}, data)
        sources = entry.get("sources") or []
        phone = next((s for s in sources if s.get("kind") == "phone"), None)
        best = next((s for s in sources if s.get("state") == "aligned" and (directory / str(s.get("file"))).is_file()),
                    None)
        resolution = _video_resolution(directory / best["file"]) if best else ""
        if best is not None and best.get("kind") == "phone":
            a = best.get("align") or {}
            if a.get("method") == "audio":
                detail = (f"The phone's own recording, lined up by its sound ({a.get('windows_matched')} matches, "
                          f"{a.get('residual_ms')} ms apart; clock drift {a.get('drift_ppm')} ppm).")
            else:
                detail = "The phone's own recording, lined up by clock sync only (no audio match): may be a frame or two off."
            state = "full"
        elif phone is not None and phone.get("state") == "arrived":
            state, detail = "lining", "The phone's recording is here; lining it up."
        elif best is not None and best.get("kind") == "local":
            state, detail = "full", "Recorded by a camera attached to the studio machine."
        else:
            part = directory / (str((phone or {}).get("file") or "") + ".part")
            uploading = ""
            if phone is not None and part.is_file():
                try:
                    size = int((directory / (phone["file"] + ".size")).read_text())
                    uploading = f" Its recording is uploading: {100 * part.stat().st_size / size:.0f}% here."
                except (OSError, ValueError, ZeroDivisionError):
                    uploading = " Its recording is uploading."
            failed = f" Lining its recording up failed: {phone.get('error')}." if phone and phone.get("state") == "failed" else ""
            if best is not None:
                state, detail = "stream", "The live feed (stream quality): the phone's own recording isn't here." + uploading + failed
            else:
                state, detail = "missing", "No video." + uploading + failed
        out.append({"title": title, "state": state, "detail": detail, "resolution": resolution})
    return out


def _edit_editor(name: str, rel: str, videos: list[tuple[str, str, str]], editor, publisher=None,
                 data: dict | None = None) -> str:
    """The timeline editor (edit.py renders its plan): each camera's share
    of the time on screen (none is special), automatic cuts from those
    (edit.auto_cuts, on the server), a clickable strip to change any
    stretch, and Render."""
    import json as _json
    plan = None
    try:
        plan = _json.loads((Path(editor.vault.session_dir(name)) / "edit.json").read_text())
    except (OSError, ValueError):
        pass
    status = editor.status(name)
    titles = [title for title, _file, _label in videos]
    urls = [_media_url(f"{rel}/{file}") for _title, file, _label in videos]
    final = (f"<p id='ed-final'>Final video: <a href='{_media_url(rel + '/edit.mp4')}'>edit.mp4</a></p>"
             if status.get("state") == "done" else "<p id='ed-final'></p>")
    # Publishing the edit to YouTube (youtube.py): its title what it was
    # last published as, or its songs' working titles, or from takeloom's
    # title template.
    from . import youtube
    from .completed import working_titles
    creds = youtube.credentials()
    published = publisher.status(name) if publisher is not None else {"state": "none"}
    pub = {
        "connected": creds is not None,
        "title": published.get("title") or youtube.default_title(
            data or {}, (creds or {}).get("title_template") or "{date} * {musician} * {project}",
            working_titles(editor.vault.root)),
        "description": youtube.render_template((creds or {}).get("description_template") or "", data or {}),
        "privacy": published.get("asked_privacy") or (creds or {}).get("visibility") or "unlisted",
        "status": published,
    }
    config = _json.dumps({
        "session": name, "titles": titles, "urls": urls, "colors": _EDIT_COLORS, "plan": plan,
        "status": status, "available": editor.available, "final": _media_url(rel + "/edit.mp4"), "pub": pub,
    }).replace("</", "<\\/")
    return (
        "<h2>Edit</h2>"
        "<p class='muted small'>One final video from all the cameras, cut automatically, with the session's "
        "own sound unbroken underneath. Give each camera a share of the time on screen (equal to start with; "
        "0 leaves it out). Click a stretch of the timeline to switch it to the next camera (the player shows "
        "it), then Render.</p>"
        "<table id='ed-shares' class='songs'></table>"
        "<p>Change camera every <input id='ed-every' type='number' min='2' value='20' style='width:4em'> s "
        "<button onclick='edFill()'>Fill timeline</button> <span id='ed-fill' class='muted small'></span></p>"
        "<div id='ed-bar' style='display:flex;height:34px;border-radius:6px;overflow:hidden;"
        "border:1px solid var(--line);cursor:pointer'></div>"
        "<p><button class='ok' id='ed-render' onclick='edRender()'>Render final video</button> "
        "<span id='ed-status' class='muted'></span></p>"
        + final +
        "<p>YouTube title <input id='yt-title' style='width:28em' maxlength='100'> "
        "<select id='yt-privacy'><option value='public'>Public</option><option value='unlisted'>Unlisted</option>"
        "<option value='private'>Private</option></select> "
        "<button id='yt-publish' onclick='ytPublish()'>Publish to YouTube</button> "
        "<span id='yt-status' class='muted'></span></p>"
        f"<script>var ED={config};</script>"
        "<script>"
        "var edCuts=[],edShares=[];"
        "function edDur(){return STAGE.duration();}"
        "function edCamAt(T){var c=edCuts,lo=0,hi=c.length-1;if(!c.length)return 0;"
        "while(lo<hi){var m=(lo+hi+1)>>1;if(c[m][0]<=T)lo=m;else hi=m-1;}return c[lo][1];}"
        "function edFmt(t){t=Math.round(t);return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');}"
        "function edSwatch(c){return \"<span class='pill' style='background:\"+ED.colors[c%ED.colors.length]+\";color:#fff'>\"+"
        "ED.titles[c].replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span>';}"
        "function edList(){var t=document.getElementById('ed-shares');t.innerHTML='<tr><th>Camera</th><th>Share of the time</th></tr>';"
        "edShares.forEach(function(w,c){var r=t.insertRow();r.insertCell().innerHTML=edSwatch(c);"
        "var x=document.createElement('input');x.type='number';x.min='0';x.max='10';x.step='1';x.value=w;x.style.width='4em';"
        "x.onchange=function(){edShares[c]=Math.max(0,+x.value||0);edFill();};r.insertCell().appendChild(x);});}"
        "function edFill(){var q='shares='+edShares.join(',')+'&every='+document.getElementById('ed-every').value;"
        "document.getElementById('ed-fill').textContent='…';"
        "fetch('/sessions/'+encodeURIComponent(ED.session)+'/edit-fill?'+q).then(function(r){return r.json();})"
        ".then(function(j){document.getElementById('ed-fill').textContent=j.error||'';if(j.cuts){edCuts=j.cuts;edDraw();}});}"
        "function edDraw(){var b=document.getElementById('ed-bar'),d=edDur();b.innerHTML='';if(!d)return;"
        "edCuts.forEach(function(c,i){var e=i+1<edCuts.length?edCuts[i+1][0]:d,s=document.createElement('div');"
        "s.style.flex=Math.max(e-c[0],0)+' 0 0';s.style.background=ED.colors[c[1]%ED.colors.length];"
        "s.style.borderRight='1px solid rgba(255,255,255,.6)';"
        "s.title=ED.titles[c[1]]+'  '+edFmt(c[0])+'–'+edFmt(e);"
        "s.onclick=function(){c[1]=(c[1]+1)%ED.titles.length;edDraw();edShow(c[0],c[1]);};b.appendChild(s);});}"
        "function edShow(t,cam){STAGE.seek(t);if(!STAGE.following())STAGE.manual(cam);}"
        "function edState(s){var el=document.getElementById('ed-status'),btn=document.getElementById('ed-render');"
        "btn.disabled=!ED.available||s.state=='queued'||s.state=='rendering';"
        "el.textContent=!ED.available?'This vault can\\'t render (it needs ffmpeg with libx264).':"
        "s.state=='queued'?'Waiting to render…':s.state=='rendering'?'Rendering… '+Math.round(100*(s.progress||0))+'%':"
        "s.state=='failed'?'Render failed: '+(s.error||''):s.state=='done'?'Rendered.':'';"
        "if(s.state=='done'){document.getElementById('ed-final').innerHTML='Final video: <a href=\"'+ED.final+'?'+Date.now()+'\">edit.mp4</a>';ytState({state:'none'});}"
        "if(s.state=='queued'||s.state=='rendering')setTimeout(edPoll,2000);}"
        "function edPoll(){fetch('/sessions/'+encodeURIComponent(ED.session)+'/edit-status').then(function(r){return r.json();}).then(edState);}"
        "function edRender(){fetch('/sessions/'+encodeURIComponent(ED.session)+'/edit',{method:'POST',"
        "headers:{'Content-Type':'application/json'},body:JSON.stringify({shares:edShares,cuts:edCuts,audio:STAGE.audio()})})"
        ".then(function(r){return r.json();}).then(edState);}"
        "function ytState(s){var el=document.getElementById('yt-status'),b=document.getElementById('yt-publish'),P=ED.pub;"
        "var rendered=document.getElementById('ed-final').textContent.length>0;"
        "b.disabled=!P.connected||!rendered||s.state=='uploading'||s.state=='preparing';"
        "if(!P.connected)el.textContent='Connect YouTube in takeloom first (video-ui › Settings › Streaming).';"
        "else if(!rendered)el.textContent='Render the final video first.';"
        "else if(s.state=='preparing'){el.textContent='Adding the footer… '+Math.round(100*(s.progress||0))+'%';setTimeout(ytPoll,3000);}"
        "else if(s.state=='uploading'){el.textContent='Uploading… '+Math.round(100*(s.progress||0))+'%';setTimeout(ytPoll,3000);}"
        "else if(s.state=='failed')el.textContent='Publishing failed: '+(s.error||'');"
        "else if(s.state=='done'){el.innerHTML='Published: <a target=_blank href=\"'+s.url+'\">'+s.url+'</a> ('+s.privacy+"
        "(s.asked_privacy&&s.asked_privacy!=s.privacy?'; YouTube made it '+s.privacy+', not '+s.asked_privacy+"
        "': unverified API projects can only upload private videos':'')+')';b.textContent='Publish again';}"
        "else el.textContent='';}"
        "function ytPoll(){fetch('/sessions/'+encodeURIComponent(ED.session)+'/publish-status').then(function(r){return r.json();}).then(ytState);}"
        "function ytPublish(){var t=document.getElementById('yt-title').value;"
        "fetch('/sessions/'+encodeURIComponent(ED.session)+'/publish',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({title:t,description:ED.pub.description,privacy:document.getElementById('yt-privacy').value})})"
        ".then(function(r){return r.json();}).then(ytState);}"
        "(function(){STAGE.setCamAt(edCamAt);if(ED.plan&&ED.plan.audio)STAGE.setAudio(ED.plan.audio);"
        "document.getElementById('yt-title').value=ED.pub.title;document.getElementById('yt-privacy').value=ED.pub.privacy;"
        "ytState(ED.pub.status);"
        "edShares=(ED.plan&&ED.plan.shares&&ED.plan.shares.length==ED.titles.length)?ED.plan.shares.slice():"
        "ED.titles.map(function(){return 1;});"
        "edList();function init(){if(ED.plan&&ED.plan.cuts){edCuts=ED.plan.cuts;edDraw();}else edFill();}"
        "STAGE.ready(init);"
        "edState(ED.status);})();"
        "</script>"
    )


def session_page(vault: Vault, name: str, data: dict, editor=None, publisher=None) -> str:
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

    from .cameras import is_sources, session_audio, session_cameras
    sources = is_sources(data)
    cameras = session_cameras(directory, data) if sources else []
    videos = ([(c.title, c.path.name, c.label) for c in cameras] if sources else session_videos(directory, data))
    has_video = bool(videos)
    statuses = _sources_statuses(directory, data) if sources else camera_statuses(directory, data)
    # An older session's advice; a sources session's table just says how
    # each camera stands.
    stream_only = [] if sources else [st["title"] for st in statuses if st["state"] == "stream"]
    if stream_only:
        parts.append(
            "<p class='banner'><b>Not full quality yet:</b> " + escape(", ".join(stream_only)) +
            (" only has" if len(stream_only) == 1 else " only have") +
            " the phone's live preview stream — its full-quality recording never reached takeloom. "
            "Open Takeloom Camera on that phone; if the recording's still there it'll upload and replace this.</p>"
        )
    if has_video and sources:
        audio = session_audio(directory, data)
        if audio["mix"] is not None:
            parts.append(_sources_player(rel, cameras, _media_url(f"{rel}/{audio['mix'].name}")))
            if editor is not None and len(cameras) > 1:
                parts.append(_edit_editor(name, rel, videos, editor, publisher, data))
        else:
            parts.append(f"<video id='v' controls preload='metadata' src='{_media_url(rel + '/' + videos[0][1])}'></video>")
    elif has_video:
        if editor is not None and len(videos) > 1:
            # Every camera playing together, cutting between them as the
            # editor's timeline says: a preview of the final video. No
            # footer over it: that's only on YouTube (footer.py).
            from .edit import camera_files
            shifts = [shift for _title, _path, shift in camera_files(directory, data)]
            from .edit import mic_files
            mics = [_media_url(f"{rel}/{path.name}") for _title, path in mic_files(directory, data)]
            parts.append(_stage_player(rel, videos, shifts, None, mics))
            parts.append(_edit_editor(name, rel, videos, editor, publisher, data))
        else:
            parts.append(f"<video id='v' controls preload='metadata' src='{_media_url(rel + '/' + videos[0][1])}'></video>")
    elif (directory / "render_jobs.json").exists():
        parts.append(
            "<p class='banner'>This session's videos are being encoded on this machine "
            "(takeloom left them for the vault). Refresh in a few minutes.</p>"
        )
    elif (directory / "session_video_raw.mp4").exists():
        parts.append(
            "<p class='banner'>This session's video was never processed into a playable file "
            "(only <code>session_video_raw.mp4</code> exists — the capture likely wasn't finalized).</p>"
        )

    if statuses:
        parts.append(_camera_status_table(statuses))

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
            # Cut from a camera's session video on first click (see
            # takes.py), so the first open of a long take takes a little
            # while. A link per camera; the default one (the camera on the
            # program feed when the take started) first.
            video_url = f"/takes/{quote(filename, safe='')}/video"
            refs = camera_refs(take.get("video") or {})
            titles = {label: title for title, _file, label in videos}
            if len(refs) > 1:
                default = (take.get("video") or {}).get("default")
                refs.sort(key=lambda r: r.get("camera") != default)
                links.append("video: " + " ".join(
                    f"<a href='{video_url}?camera={quote(r.get('camera', ''), safe='')}'>"
                    f"{escape(titles.get(r.get('camera', ''), r.get('camera', '')))}</a>"
                    for r in refs
                ))
            elif refs or (vault.root / "completed_takes" / f"{stem}.mp4").exists():
                links.append(f"<a href='{video_url}'>video</a>")
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
    take_count = len({t.get("filename") for entries in takes_by_index.values() for t in entries if t.get("filename")})
    warning = f"Permanently delete this session ({_format_size(vault.session_size(name))})"
    if take_count:
        warning += (f" and the {take_count} take{'s' if take_count != 1 else ''} it recorded? "
                    "Those songs will be left with no take from it.")
    else:
        warning += "?"
    warning += " This can't be undone."
    parts.append(
        "<h2>Delete</h2>"
        f"<form method='post' action='/sessions/{quote(name, safe='')}/delete' "
        f"onsubmit='return confirm({escape(json.dumps(warning))})'>"
        "<button class='danger'>Delete session…</button></form>"
        f" <span class='muted small'>Removes it from the vault"
        + (f", with its {take_count} take{'s' if take_count != 1 else ''}" if take_count else "")
        + "; takeloom drops it from its setlists on its next sync.</span>"
    )
    parts.append(
        "<script>function seek(t){if(window.STAGE){STAGE.seek(t);STAGE.play();"
        "STAGE.element().scrollIntoView({behavior:'smooth'});return;}"
        "var v=document.getElementById('v');v.currentTime=t;v.play();"
        "v.scrollIntoView({behavior:'smooth'});}"
        "function cam(b,u){var v=document.getElementById('v'),t=v.currentTime,p=!v.paused;"
        "document.querySelectorAll('button.cam').forEach(function(x){x.classList.remove('on')});"
        "b.classList.add('on');v.src=u;v.addEventListener('loadedmetadata',function f(){"
        "v.removeEventListener('loadedmetadata',f);v.currentTime=t;if(p)v.play();});}</script>"
    )
    return page(name, "".join(parts))


_COMPLETED_JS = Path(__file__).with_name("completed_page.js")


def completed_page(pending: list[PairingRequest]) -> str:
    """Every completed take across the vault, by song, with a mixer — the
    page's data comes from GET /completed/data (completed.py), its
    workings are completed_page.js."""
    body = (
        _pairing_banner(pending)
        + "<h1>Completed takes <span id='ct-count' class='muted small'></span> "
          "<button id='ct-reload'>⟳ Reload</button> <span id='ct-loaded' class='muted small'></span></h1>"
        "<p class='muted small'>Every completed take across the whole vault, grouped by song. "
        "Click a song to load its takes into the mixer. <span id='ct-pending'></span></p>"
        "<div class='ct'><div id='mixer'></div><div>"
        "<div class='filters'><label>Filter by title <input id='ct-title' type='search'></label>"
        "<label>Project <select id='ct-project'><option value=''>All projects</option></select></label></div>"
        "<table><thead><tr><th>Name</th><th>Working title</th><th>Backing source</th><th>Instruments</th></tr></thead>"
        "<tbody id='ct-songs'></tbody></table></div></div>"
        f"<script>{_COMPLETED_JS.read_text()}</script>"
    )
    return page("Completed takes", body)


def cameras_page(pending: list[PairingRequest]) -> str:
    """The Takeloom Camera phones connected here (camera_hub.py), with
    their previews, and the recording nodes that control them."""
    body = (
        _pairing_banner(pending)
        + "<h1>Cameras</h1>"
        "<p class='muted small'>Takeloom Camera phones connect to this vault. A phone that hasn't paired with it "
        "yet asks to above; in the app, ⚙ › Scan for server finds it. Recording nodes (takeloom on the studio "
        "machines) start and stop the phones' recordings through here.</p>"
        "<div id='cams' style='display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1em'></div>"
        "<h2>Recording nodes</h2><p id='nodes' class='muted'></p>"
        "<script>(function(){"
        "function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;');}"
        "function load(){fetch('/cameras/data').then(function(r){return r.json();}).then(function(j){"
        "var rec={};j.recording.forEach(function(r){rec[r.device_id]=r.session;});"
        "var el=document.getElementById('cams');"
        "if(!j.cameras.length){el.innerHTML=\"<p class='muted'>No phones connected.</p>\";}"
        "else{var seen={};j.cameras.forEach(function(c){seen[c.device_id]=1;"
        "var id='cam-'+c.device_id.replace(/[^\\w-]/g,'_'),box=document.getElementById(id);"
        "if(!box){box=document.createElement('div');box.id=id;"
        "box.innerHTML=\"<img style='width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:6px'><p class='small'></p>\";}"
        "el.appendChild(box);var s=c.status||{};"
        "box.querySelector('img').src='/cameras/'+encodeURIComponent(c.device_id)+'/preview.jpg?'+Date.now();"
        "box.querySelector('p').innerHTML='<b>'+esc(c.client_name)+'</b> <span class=muted>'+esc(c.ip)+' · app '+esc(c.app_version)+'</span><br>'+"
        "(s.width?esc(s.width+'×'+s.height+' '+(s.fps||'')+' fps'):'')+"
        "(s.battery!=null?' · battery '+Math.round(100*s.battery)+'%'+(s.charging?' (charging)':''):'')+"
        "(s.live_width?' · live '+esc(s.live_width+'×'+s.live_height)+' '+((s.live_bitrate||0)/1e6).toFixed(1)+' Mbps':'')+"
        "(rec[c.device_id]?'<br><b>● recording</b> '+esc(rec[c.device_id]):'');});"
        "Array.prototype.slice.call(el.children).forEach(function(b){if(b.id&&b.id.indexOf('cam-')==0&&"
        "!j.cameras.some(function(c){return 'cam-'+c.device_id.replace(/[^\\w-]/g,'_')==b.id;}))el.removeChild(b);});"
        "var empty=el.querySelector('p.muted:not(.small)');if(empty&&j.cameras.length)empty.remove();}"
        "document.getElementById('nodes').textContent=(j.nodes.length?j.nodes.map(function(n){return n.label+' ('+n.ip+')';}).join(', '):'None connected.')+"
        "(j.streams&&j.streams.length?'  ·  Streaming live: '+j.streams.map(function(s){return s.session+' (showing '+s.program+')';}).join(', '):'');"
        "}).catch(function(){}).then(function(){setTimeout(load,1500);});}"
        "load();})();</script>"
    )
    return page("Cameras", body)


def clients_page(clients: list[PairedClient], pending: list[PairingRequest]) -> str:
    # A device paired through another one (auth.py's delegate: a phone or
    # laptop paired with takeloom's studio server) is listed under it;
    # revoking that one revokes it too.
    labels = {c.id: c.label for c in clients}

    def row(c: PairedClient) -> str:
        via = f" <span class='muted'>via {escape(labels.get(c.parent, '?'))}</span>" if c.parent else ""
        return (
            "<tr><td>{label}{via}</td><td>{ip}</td><td>{paired}</td><td>{seen}</td>"
            "<td><form method='post' action='/clients/{id}/revoke'><button>Revoke</button></form></td></tr>"
        ).format(
            label=escape(c.label), via=via, ip=escape(c.last_ip), paired=escape(c.paired_at.replace("T", " ")),
            seen=escape(c.last_seen_at.replace("T", " ")), id=quote(c.id),
        )

    rows = "".join(row(c) + "".join(row(d) for d in clients if d.parent == c.id)
                   for c in clients if not c.parent)
    body = _pairing_banner(pending) + "<h1>Paired devices</h1>" + (
        "<table><tr><th>Device</th><th>Last IP</th><th>Paired</th><th>Last seen</th><th></th></tr>" + rows + "</table>"
        "<p class='muted'>Devices paired with takeloom's studio server reach this vault through it; "
        "revoking the studio server revokes them too.</p>"
        if clients else "<p class='muted'>Nothing is paired yet. Use “Pair” next to the vault server "
        "address in takeloom's Studio Setup tab.</p>"
    )
    return page("Paired devices", body)


def message_page(title: str, message: str) -> str:
    return page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href='/'>Back</a></p>")
