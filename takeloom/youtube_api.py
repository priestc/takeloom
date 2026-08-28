"""YouTube Data API v3 access: OAuth ("Connect YouTube Account" on the
Streaming tab) plus the handful of liveBroadcast/liveStream calls needed to
give each session's live stream a real title.

RTMP (the stream key mechanism in streaming.py/video/capture.py) carries no
metadata channel at all — there's no way to set a title through it. YouTube
manages a broadcast's title as a separate `liveBroadcast` resource that gets
*bound* to the persistent `liveStream` resource behind a stream key. So
giving each session a title means: create a fresh liveBroadcast with that
title, and bind it to the liveStream matching the configured stream key,
before the RTMP push starts. With contentDetails.enableAutoStart/enableAutoStop
set, YouTube then flips it live/complete on its own as the bound stream's
RTMP data starts/stops — no polling or manual transition() needed to go
live, though _end_session still calls transition("complete") explicitly so
the broadcast doesn't sit in "reconnecting" for YouTube's own stream-health
timeout after ffmpeg has already stopped.

Uses stdlib urllib only (no new dependency), the same as inspiration.py's
HTTP calls elsewhere in this codebase.
"""

from __future__ import annotations

import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

from .net import terse_source_note

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
# Full read/write access — needed to create and bind liveBroadcasts. There's
# no narrower official scope that still covers broadcast management.
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"

# YouTube truncates/rejects titles/descriptions beyond these regardless;
# enforced here so the error (if any) is ours to word, not a raw API
# rejection.
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000


class YouTubeAPIError(Exception):
    """Raised by any OAuth or Data API call in this module on failure.
    Message is safe to show to the user."""


# --- OAuth: "Connect YouTube Account" (loopback redirect flow) ---


class _RedirectCatcherHandler(http.server.BaseHTTPRequestHandler):
    """Handles exactly the one GET Google's consent screen redirects back
    to, pulling `code`/`error`/`state` off the query string onto the
    server instance for run_oauth_flow() to read once handle_request()
    returns."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        self.server.oauth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.oauth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]
        self.server.oauth_state = params.get("state", [None])[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = (
            "Connected. You can close this tab and return to Takeloom."
            if self.server.oauth_code  # type: ignore[attr-defined]
            else f"Authorization failed: {self.server.oauth_error or 'no code returned'}."  # type: ignore[attr-defined]
        )
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

    def log_message(self, format: str, *args: object) -> None:
        pass  # this is a one-shot redirect catcher, not a real web server — keep the console quiet


def run_oauth_flow(client_id: str, client_secret: str, timeout: float = 300.0) -> str:
    """Runs Google's OAuth "installed app" flow (loopback IP redirect —
    the current supported approach for a Desktop-type OAuth client;
    Google's old copy-paste "oob" flow is deprecated) for YOUTUBE_SCOPE,
    blocking until the user finishes or denies authorization in their
    browser. Returns a refresh_token to store in config — every future API
    call refreshes a fresh access token from it rather than needing the
    user signed in again.

    Must run on the same machine as the browser it opens: the redirect
    lands on a local server started here on an ephemeral loopback port,
    reachable only from that machine's own browser. Called directly from
    the UI layer for exactly that reason — unlike everything else on the
    Streaming tab, this is never proxied through Backend/RemoteBackend.
    """
    if not client_id or not client_secret:
        raise YouTubeAPIError("Enter both the OAuth Client ID and Client Secret first.")

    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectCatcherHandler)
    server.oauth_code = None  # type: ignore[attr-defined]
    server.oauth_error = None  # type: ignore[attr-defined]
    server.oauth_state = None  # type: ignore[attr-defined]
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces Google to hand back a refresh_token even on a repeat authorization
        "state": state,
    }
    webbrowser.open(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")

    deadline = time.monotonic() + timeout
    try:
        server.timeout = 1.0  # handle_request()'s own per-call socket timeout, so the deadline below is checked
        while (
            server.oauth_code is None  # type: ignore[attr-defined]
            and server.oauth_error is None  # type: ignore[attr-defined]
            and time.monotonic() < deadline
        ):
            server.handle_request()
    finally:
        server.server_close()

    if server.oauth_error:  # type: ignore[attr-defined]
        raise YouTubeAPIError(f"Google denied authorization: {server.oauth_error}")  # type: ignore[attr-defined]
    if server.oauth_code is None:  # type: ignore[attr-defined]
        raise YouTubeAPIError("Timed out waiting for authorization in the browser.")
    if server.oauth_state != state:  # type: ignore[attr-defined]
        raise YouTubeAPIError("Authorization response didn't match this request — please try again.")

    return _exchange_code(client_id, client_secret, redirect_uri, server.oauth_code)  # type: ignore[attr-defined]


def _post_form(url: str, fields: dict, timeout: float = 15.0) -> dict:
    payload = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise YouTubeAPIError(f"Google rejected the request to {url}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise YouTubeAPIError(f"Could not reach Google at {url}: {e}{terse_source_note(e)}") from e


def _exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> str:
    data = _post_form(GOOGLE_TOKEN_URL, {
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    })
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise YouTubeAPIError(
            "Google didn't return a refresh token. If Takeloom was already authorized before, "
            "revoke its access at myaccount.google.com/permissions and try connecting again."
        )
    return refresh_token


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a stored refresh_token for a short-lived access token. Called
    fresh for every session rather than caching — this endpoint is cheap and
    it avoids tracking access-token expiry as separate state."""
    data = _post_form(GOOGLE_TOKEN_URL, {
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    })
    access_token = data.get("access_token")
    if not access_token:
        raise YouTubeAPIError("Google didn't return an access token when refreshing.")
    return access_token


def revoke(refresh_token: str) -> None:
    """Best-effort: tell Google to invalidate a refresh token (Streaming
    tab's "Disconnect"). Never raises — the token is dropped from config
    either way, so a failed revoke call just leaves it valid-but-unused on
    Google's side instead of blocking the local disconnect."""
    try:
        urllib.request.urlopen(
            urllib.request.Request(GOOGLE_REVOKE_URL, data=urllib.parse.urlencode({"token": refresh_token}).encode(), method="POST"),
            timeout=10,
        ).close()
    except (urllib.error.URLError, OSError):
        pass


# --- YouTube Data API: liveStreams / liveBroadcasts ---


def _api_request(access_token: str, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"{YOUTUBE_API_BASE}/{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise YouTubeAPIError(
            f"YouTube API error ({e.code}) from {method} {url}: {e.read().decode(errors='replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise YouTubeAPIError(
            f"Could not reach the YouTube API at {method} {url}: {e}{terse_source_note(e)}"
        ) from e


def find_stream_id(access_token: str, stream_key: str) -> str:
    """Find the liveStream resource id whose ingestion stream key matches
    stream_key (the pasted key on the Streaming tab) — the persistent
    resource a fresh, titled broadcast gets bound to each session (see
    create_and_bind_broadcast). Checks every page of the account's streams;
    raises if none match."""
    page_token = None
    for _ in range(10):  # 10 pages * 50 = 500 streams — far more than any real account has
        params = {"part": "cdn,id", "mine": "true", "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        data = _api_request(access_token, "GET", "liveStreams", params=params)
        for item in data.get("items", []):
            if item.get("cdn", {}).get("ingestionInfo", {}).get("streamName") == stream_key:
                return item["id"]
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    raise YouTubeAPIError(
        "Could not find a YouTube stream matching this stream key on the connected account — "
        "make sure it's a current key from YouTube Studio's Stream settings."
    )


ATTRIBUTION_FOOTER = "\n\nMade with Takeloom\nhttps://github.com/priestc/takeloom"


def create_and_bind_broadcast(
    access_token: str, stream_id: str, title: str, description: str, privacy_status: str,
) -> str:
    """Create a titled+described liveBroadcast and bind it to `stream_id`,
    returning the new broadcast's id. This is what actually gets the title
    onto YouTube. enableAutoStart/enableAutoStop (plus disabling the
    monitor-stream health-check step) let YouTube flip the broadcast live/
    complete on its own as the bound stream's RTMP data starts/stops, with
    no manual transition() call needed to go live.

    ATTRIBUTION_FOOTER is appended to every description here — at the
    actual API call, not baked into the user's own editable description
    template — so it's always present regardless of what that template
    says, and the user's own text is truncated first (reserving room for
    it) rather than the footer risking getting pushed out by a long
    description."""
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    full_description = description[:MAX_DESCRIPTION_LENGTH - len(ATTRIBUTION_FOOTER)] + ATTRIBUTION_FOOTER
    broadcast = _api_request(
        access_token, "POST", "liveBroadcasts", params={"part": "id,snippet,status,contentDetails"},
        body={
            "snippet": {
                "title": title[:MAX_TITLE_LENGTH], "description": full_description,
                "scheduledStartTime": now_iso,
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
            "contentDetails": {
                "enableAutoStart": True, "enableAutoStop": True,
                "monitorStream": {"enableMonitorStream": False},
            },
        },
    )
    broadcast_id = broadcast.get("id")
    if not broadcast_id:
        raise YouTubeAPIError("YouTube didn't return a broadcast id after creating it.")
    _api_request(
        access_token, "POST", "liveBroadcasts/bind",
        params={"id": broadcast_id, "streamId": stream_id, "part": "id,contentDetails"},
    )
    return broadcast_id


def transition_broadcast(access_token: str, broadcast_id: str, status: str) -> None:
    """Move a broadcast to `status` (_end_session uses "complete") right
    away, rather than leaving it for YouTube's own stream-health timeout
    to notice the RTMP connection dropped."""
    _api_request(
        access_token, "POST", "liveBroadcasts/transition",
        params={"broadcastStatus": status, "id": broadcast_id, "part": "id,status"},
    )


def render_stream_template(
    template: str, *, studio: str, studio_location: str, musician: str, project: str, instrument: str,
    when: datetime | None = None,
) -> str:
    """Fill in a title/description template's {placeholders} with this
    session's details — used for both StudioConfig.youtube_title_template
    and youtube_description_template, since both are just a template
    string plus this same set of values.

    Plain substring replacement rather than str.format(): a couple of
    these placeholder names (`{studio-location}`, `{instrument name}`)
    aren't valid str.format field names — a hyphen/space isn't legal in a
    Python identifier, which is what a bare field name has to be — so
    str.format would raise on the very defaults this is built to support.
    A blank value (e.g. no musician set) just disappears, leaving whatever
    punctuation/spacing the template had around it — deliberately literal,
    since the whole point of a user-edited template is that they control
    the exact wording, not that it gets smoothed over automatically."""
    when = when or datetime.now()
    values = {
        "{date}": when.strftime("%B %d, %Y"),
        "{studio}": studio,
        "{studio-location}": studio_location,
        "{musician}": musician,
        "{project}": project,
        "{instrument name}": instrument,
    }
    result = template
    for placeholder, value in values.items():
        result = result.replace(placeholder, value)
    return result
