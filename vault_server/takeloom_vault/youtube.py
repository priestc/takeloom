"""Publishing a session's edit (edit.mp4) to YouTube, from the vault.

The video is here, so it's uploaded from here rather than copied back to
the studio machine first. The YouTube account is takeloom's: when it's
connected (video-ui › Settings › Streaming), takeloom sends the vault its
OAuth client and refresh token (PUT /api/youtube, paired clients only),
kept in CREDENTIALS_FILE readable only by this user. Its scope
(youtube, full access) covers uploads.

An upload is YouTube's resumable protocol over urllib (stdlib only, like
the rest of this server): start a session with the video's details, then
send the file in chunks, resuming from where YouTube says it got to if a
chunk fails. One at a time, in the background; the result goes into the
session's folder as youtube.json, which the session page links to.

YouTube makes every upload from an unverified API project (one created
after July 2020 that hasn't passed its audit) private, whatever privacy
was asked for; the privacy YouTube actually gave it is recorded and shown.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from .store import Vault

CREDENTIALS_FILE = Path.home() / ".config" / "takeloom-vault" / "youtube.json"
RESULT_FILE = "youtube.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 16 * 1024 * 1024  # a multiple of 256 KiB, as YouTube requires
PRIVACY = ("public", "unlisted", "private")
MUSIC_CATEGORY = "10"


class YouTubeError(Exception):
    pass


# --- credentials (from takeloom) ---

def save_credentials(data: dict) -> None:
    """What takeloom sends: client_id, client_secret, refresh_token (""
    to disconnect), and its title/description templates and visibility."""
    keys = ("client_id", "client_secret", "refresh_token", "title_template", "description_template", "visibility")
    clean = {k: str(data.get(k) or "") for k in keys}
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not clean["refresh_token"]:
        CREDENTIALS_FILE.unlink(missing_ok=True)
        return
    tmp = CREDENTIALS_FILE.with_name(CREDENTIALS_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(clean, f)
    os.replace(tmp, CREDENTIALS_FILE)


def credentials() -> dict | None:
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("refresh_token") else None


# --- titles ---

def render_template(template: str, data: dict) -> str:
    """takeloom's youtube_api.render_stream_template, for a session: its
    {placeholders} filled from the session log (the date it was recorded)."""
    events = data.get("events") or []
    started = next((e.get("wall_time", "") for e in events if e.get("event_type") == "session_start"), "")
    try:
        when = datetime.strptime(started, "%Y-%m-%d %H:%M:%S").strftime("%B %d, %Y")
    except ValueError:
        when = started
    values = {
        "{date}": when, "{studio}": data.get("studio_name", ""), "{studio-location}": data.get("studio_location", ""),
        "{musician}": data.get("musician", ""), "{project}": data.get("project", ""),
        "{instrument name}": data.get("instrument", ""),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template.strip()


# --- the upload ---

def _access_token(creds: dict) -> str:
    body = urllib.parse.urlencode({
        "client_id": creds["client_id"], "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(TOKEN_URL, data=body, timeout=20) as resp:
            token = json.loads(resp.read()).get("access_token")
    except urllib.error.HTTPError as e:
        raise YouTubeError(f"Google refused the account's sign-in at {TOKEN_URL}: "
                           f"{e.read().decode(errors='replace')[:300]} (reconnect YouTube in takeloom)") from e
    except urllib.error.URLError as e:
        raise YouTubeError(f"Couldn't reach Google at {TOKEN_URL}: {e.reason}") from e
    if not token:
        raise YouTubeError(f"Google didn't return an access token from {TOKEN_URL}.")
    return token


def upload(path: Path, title: str, description: str, privacy: str, creds: dict,
           progress: Callable[[float], None]) -> dict:
    """Upload `path`; returns YouTube's video resource ({"id", "status", ...})."""
    token = _access_token(creds)
    size = path.stat().st_size
    meta = {
        "snippet": {"title": title[:100], "description": description[:5000], "categoryId": MUSIC_CATEGORY},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    start = urllib.request.Request(
        f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status", data=json.dumps(meta).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
    )
    try:
        with urllib.request.urlopen(start, timeout=30) as resp:
            session_url = resp.headers["Location"]
    except urllib.error.HTTPError as e:
        raise YouTubeError(f"YouTube refused the upload at {UPLOAD_URL}: {e.read().decode(errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise YouTubeError(f"Couldn't reach YouTube at {UPLOAD_URL}: {e.reason}") from e

    offset, failures = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            put = urllib.request.Request(session_url, data=chunk, method="PUT", headers={
                "Authorization": f"Bearer {token}", "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}",
            })
            try:
                with urllib.request.urlopen(put, timeout=300) as resp:
                    progress(1.0)
                    return json.loads(resp.read())  # 200/201: done
            except urllib.error.HTTPError as e:
                if e.code == 308:  # more wanted: YouTube says how much it has
                    got = e.headers.get("Range")
                    offset = int(got.rsplit("-", 1)[1]) + 1 if got else 0
                    failures = 0
                    progress(offset / size)
                    continue
                if e.code < 500 and e.code != 429:
                    raise YouTubeError(f"YouTube rejected the upload at {UPLOAD_URL}: "
                                       f"{e.read().decode(errors='replace')[:400]}") from e
                error = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError) as e:
                error = str(getattr(e, "reason", e))
            failures += 1
            if failures > 8:
                raise YouTubeError(f"The upload to YouTube kept failing ({error}).")
            time.sleep(min(60, 2 ** failures))
            offset = _ask_offset(session_url, token, size, offset)


def _ask_offset(session_url: str, token: str, size: int, fallback: int) -> int:
    """After a failed chunk: how much YouTube actually has."""
    probe = urllib.request.Request(session_url, data=b"", method="PUT", headers={
        "Authorization": f"Bearer {token}", "Content-Length": "0", "Content-Range": f"bytes */{size}"})
    try:
        urllib.request.urlopen(probe, timeout=30).close()
    except urllib.error.HTTPError as e:
        if e.code == 308:
            got = e.headers.get("Range")
            return int(got.rsplit("-", 1)[1]) + 1 if got else 0
    except (urllib.error.URLError, OSError):
        pass
    return fallback


class Publisher:
    """One upload at a time, in the background; status per session."""

    def __init__(self, vault: Vault, log: Callable[[str], None]) -> None:
        self.vault = vault
        self.log = log
        self._lock = threading.Lock()
        self._status: dict[str, dict] = {}

    def status(self, name: str) -> dict:
        with self._lock:
            status = dict(self._status.get(name) or {})
        if status:
            return status
        try:
            return {"state": "done", **json.loads((self.vault.session_dir(name) / RESULT_FILE).read_text())}
        except (OSError, ValueError):
            return {"state": "none"}

    def publish(self, name: str, title: str, description: str, privacy: str) -> dict:
        creds = credentials()
        if creds is None:
            raise ValueError("no YouTube account: connect one in takeloom (video-ui › Settings › Streaming)")
        video = self.vault.session_dir(name) / "edit.mp4"
        if not video.is_file():
            raise ValueError("render the final video first")
        title = title.strip()
        if not title:
            raise ValueError("give it a title")
        if privacy not in PRIVACY:
            raise ValueError("pick public, unlisted or private")
        with self._lock:
            if self._status.get(name, {}).get("state") == "uploading":
                return dict(self._status[name])
            self._status[name] = {"state": "uploading", "progress": 0.0}
        threading.Thread(target=self._run, args=(name, video, title, description, privacy, creds),
                         name="youtube-upload", daemon=True).start()
        return self.status(name)

    def _run(self, name: str, video: Path, title: str, description: str, privacy: str, creds: dict) -> None:
        def progress(fraction: float) -> None:
            with self._lock:
                self._status[name] = {"state": "uploading", "progress": round(fraction, 3)}
        self.log(f"Publishing {name}'s edit to YouTube ({privacy}): {title!r}")
        try:
            result = upload(video, title, description, privacy, creds, progress)
        except Exception as e:  # noqa: BLE001
            self.log(f"Publishing {name} to YouTube failed: {e}")
            with self._lock:
                self._status[name] = {"state": "failed", "error": str(e)}
            return
        record = {
            "video_id": result.get("id", ""), "url": f"https://youtu.be/{result.get('id', '')}", "title": title,
            "privacy": (result.get("status") or {}).get("privacyStatus", privacy), "asked_privacy": privacy,
            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self.vault.session_dir(name) / RESULT_FILE
        path.write_text(json.dumps(record, indent=2))
        self.log(f"Published {name} to YouTube: {record['url']} ({record['privacy']})")
        with self._lock:
            self._status.pop(name, None)
