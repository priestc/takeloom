"""Server-rendered HTML for the vault's browser UI: the session list (with
any pending pairing requests on top), one session's page (video player
with per-song jump links, its takes, its raw audio), and the paired-
clients page. Plain HTML + a few lines of inline JS — no build step, no
dependencies, same as the rest of this package."""

from __future__ import annotations

import datetime
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
button.cam.on { background: #2a6db0; color: #fff; border-color: #2a6db0; }
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


def sessions_page(vault: Vault, logs: dict[str, dict], pending: list[PairingRequest]) -> str:
    rows = []
    for name, data in sorted(
        logs.items(), key=lambda kv: (kv[1].get("events") or [{}])[0].get("wall_time", ""), reverse=True,
    ):
        events = data.get("events") or []
        directory = vault.session_dir(name)
        has_video = bool(session_videos(directory, data))
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


_EDIT_COLORS = ["#2a6db0", "#d0702a", "#2e9d4f", "#9b4fc4", "#c4a72a", "#c44f6d"]


def _stage_player(rel: str, videos: list[tuple[str, str, str]], shifts: list[float],
                  footer_entries: list[list] | None = None) -> str:
    """Every camera's video stacked and played together, in step, showing
    one at a time: the one the editor's timeline says (Preview edit: the
    final video as it'll render, cutting as it plays), or the one picked
    with its button. Only the priority-1 camera's sound plays, as in the
    render. Its own controls, since a video's built-in ones can't sit over
    the others. Others drifting more than 0.2 s are pulled back into step.
    `shifts`: each video's sync shift (edit.time_shift), so session time T
    is T + shift in that file. `footer_entries` (footer.schedule; None:
    the videos carry their own): the footer, drawn over the picture with
    the camera showing, as the render will draw it. Exposes STAGE for the
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
        f"<script>var STAGE=(function(SH,FOOT,TITLES){{"
        "var V=[].slice.call(document.querySelectorAll('#stage video')),master=0,shown=0,manual=0,camAt=null,playing=false,drag=false;"
        "var seekEl=document.getElementById('st-seek'),follow=document.getElementById('st-follow');"
        "function fmt(t){t=Math.max(0,Math.round(t));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');}"
        "function now(){return V[master].currentTime-SH[master];}"
        "function dur(){return Math.max(0,(V[master].duration||0)-SH[master]);}"
        "function show(i){if(i===shown)return;V[shown].style.opacity=0;V[i].style.opacity=1;shown=i;"
        "document.querySelectorAll('button.cam').forEach(function(b,j){b.classList.toggle('on',j===i);});}"
        "function setMaster(i){master=i;V.forEach(function(v,j){v.muted=j!==i;});}"
        "function align(){var T=now();V.forEach(function(v,j){if(j===master)return;var w=T+SH[j];"
        "if(Math.abs(v.currentTime-w)>0.2)v.currentTime=Math.max(0,w);if(playing&&v.paused)v.play().catch(function(){});});}"
        "function seek(T){V.forEach(function(v,j){v.currentTime=Math.max(0,T+SH[j]);});}"
        "function play(){playing=true;document.getElementById('st-play').textContent='❚❚';"
        "V[master].play().catch(function(){});align();}"
        "function pause(){playing=false;document.getElementById('st-play').textContent='▶';V.forEach(function(v){v.pause();});}"
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
        "setMaster:setMaster,setCamAt:function(f){camAt=f;},"
        "manual:function(i){follow.checked=false;manual=i;show(i);},"
        "following:function(){return follow.checked;},"
        "ready:function(cb){var m=V[0];if(m.readyState>=1)cb();else m.addEventListener('loadedmetadata',function f(){m.removeEventListener('loadedmetadata',f);cb();});},"
        "element:function(){return document.getElementById('stage');}};"
        f"}})({_json.dumps(shifts)},{_json.dumps(footer_entries)},{_json.dumps(titles)});</script>"
    )


def _edit_editor(name: str, rel: str, videos: list[tuple[str, str, str]], editor) -> str:
    """The timeline editor (edit.py renders its plan): a camera priority
    (the first is the main camera and the sound; the lower, the less it's
    cut to), automatic cuts from it (edit.auto_cuts, on the server), a
    clickable strip to change any stretch, and Render."""
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
    config = _json.dumps({
        "session": name, "titles": titles, "urls": urls, "colors": _EDIT_COLORS, "plan": plan,
        "status": status, "available": editor.available, "final": _media_url(rel + "/edit.mp4"),
    }).replace("</", "<\\/")
    return (
        "<h2>Edit</h2>"
        "<p class='muted small'>One final video from all the cameras, cut automatically. Camera priority: "
        "the first is the main camera, on screen most of the time and the source of the sound (unbroken); "
        "the others are cut in now and then, the lower the priority the less often. Click a stretch of the "
        "timeline to switch it to the next camera (the player shows it), then Render.</p>"
        "<table id='ed-priority' class='songs'></table>"
        "<p>Cut to the others every <input id='ed-every' type='number' min='5' value='30' style='width:4em'> s "
        "for <input id='ed-len' type='number' min='1' value='5' style='width:3em'> s "
        "<button onclick='edFill()'>Fill timeline</button> <span id='ed-fill' class='muted small'></span></p>"
        "<div id='ed-bar' style='display:flex;height:34px;border-radius:6px;overflow:hidden;"
        "border:1px solid var(--line);cursor:pointer'></div>"
        "<p><button class='ok' id='ed-render' onclick='edRender()'>Render final video</button> "
        "<span id='ed-status' class='muted'></span></p>"
        + final +
        f"<script>var ED={config};</script>"
        "<script>"
        "var edCuts=[],edPri=[];"
        "function edDur(){return STAGE.duration();}"
        "function edCamAt(T){var c=edCuts,lo=0,hi=c.length-1;if(!c.length)return edPri[0]||0;"
        "while(lo<hi){var m=(lo+hi+1)>>1;if(c[m][0]<=T)lo=m;else hi=m-1;}return c[lo][1];}"
        "function edFmt(t){t=Math.round(t);return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');}"
        "function edSwatch(c){return \"<span class='pill' style='background:\"+ED.colors[c%ED.colors.length]+\";color:#fff'>\"+"
        "ED.titles[c].replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span>';}"
        "function edList(){var t=document.getElementById('ed-priority');t.innerHTML='<tr><th>Priority</th><th>Camera</th><th></th></tr>';"
        "edPri.forEach(function(c,i){var r=t.insertRow();r.insertCell().textContent=(i+1)+(i==0?' (main, sound)':'');"
        "r.insertCell().innerHTML=edSwatch(c);var m=r.insertCell();"
        "[['▲',-1],['▼',1]].forEach(function(b){var j=i+b[1];if(j<0||j>=edPri.length)return;var x=document.createElement('button');"
        "x.textContent=b[0];x.onclick=function(){var s=edPri[i];edPri[i]=edPri[j];edPri[j]=s;edList();edFill();};m.appendChild(x);});});"
        "STAGE.setMaster(edPri[0]);}"
        "function edFill(){var q='priority='+edPri.join(',')+'&every='+document.getElementById('ed-every').value+"
        "'&len='+document.getElementById('ed-len').value;document.getElementById('ed-fill').textContent='…';"
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
        "if(s.state=='done')document.getElementById('ed-final').innerHTML='Final video: <a href=\"'+ED.final+'?'+Date.now()+'\">edit.mp4</a>';"
        "if(s.state=='queued'||s.state=='rendering')setTimeout(edPoll,2000);}"
        "function edPoll(){fetch('/sessions/'+encodeURIComponent(ED.session)+'/edit-status').then(function(r){return r.json();}).then(edState);}"
        "function edRender(){fetch('/sessions/'+encodeURIComponent(ED.session)+'/edit',{method:'POST',"
        "headers:{'Content-Type':'application/json'},body:JSON.stringify({priority:edPri,cuts:edCuts})})"
        ".then(function(r){return r.json();}).then(edState);}"
        "(function(){STAGE.setCamAt(edCamAt);"
        "var p=ED.plan&&(ED.plan.priority||(ED.plan.primary!=null?[ED.plan.primary]:null));"
        "edPri=ED.titles.map(function(_,i){return i;});"
        "if(p){edPri=p.concat(edPri.filter(function(i){return p.indexOf(i)<0;}));}"
        "edList();function init(){if(ED.plan&&ED.plan.cuts){edCuts=ED.plan.cuts;edDraw();}else edFill();}"
        "STAGE.ready(init);"
        "edState(ED.status);})();"
        "</script>"
    )


def session_page(vault: Vault, name: str, data: dict, editor=None) -> str:
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

    videos = session_videos(directory, data)
    has_video = bool(videos)
    if has_video:
        from . import footer
        if editor is not None and (len(videos) > 1 or footer.wanted(data)):
            # Every camera playing together, cutting between them as the
            # editor's timeline says, with the footer over it: a preview of
            # the final video.
            from .edit import camera_files
            shifts = [shift for _title, _path, shift in camera_files(directory, data)]
            parts.append(_stage_player(rel, videos, shifts,
                                       footer.schedule(data) if footer.wanted(data) else None))
            if len(videos) > 1:
                parts.append(_edit_editor(name, rel, videos, editor))
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
