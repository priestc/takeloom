"""HTTP server: a JSON API for takeloom (token-authenticated) and a
browser UI for people (local-network only, no login).

API — every route except /api/pair* needs `Authorization: Bearer <token>`:

    POST   /api/pair                    {"client_name"} -> {"request_id", "code"}
    GET    /api/pair/<id>               -> {"status": "pending"|"approved"|"denied"|"expired", "token"?}
    GET    /api/ping                    -> {"ok", "hostname", "client"}
    GET    /api/sessions                -> {"sessions": {name: session_log, ...}}
    GET    /api/sessions/<name>/log     -> session_log.json
    PUT    /api/sessions/<name>/log     <- session_log.json (existing session only)
    GET    /api/list/<dir>              -> {"files": [{"name", "size"}]}
    GET    /api/files/<vault path>      -> raw bytes
    PUT    /api/files/<vault path>      <- raw bytes, X-Content-SHA256 header required
    POST   /api/delete                  {"paths": [...]} -> {}

Browser UI:

    GET  /                       session list (+ pending pairing requests)
    GET  /sessions/<name>        one session: video, songs, files
    GET  /clients                paired devices
    GET  /media/<vault path>     file download/stream, with Range support (for <video> seeking)
    POST /pairing/<id>/approve|deny,  POST /clients/<id>/revoke
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import web
from .auth import Auth
from .store import Vault, VaultPathError

# Tailscale hands out addresses from the CGNAT block, which Python doesn't
# count as "private" — but it's every bit as much "our own network".
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")

mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("audio/midi", ".mid")

_MAX_JSON_BODY = 32 * 1024 * 1024
# A request refused before its body was read gets that body read and
# discarded first, up to this size — otherwise closing the socket with
# unread data in it resets the connection, and the client sees "connection
# reset" instead of the actual error message. Bigger bodies (a whole
# session upload) aren't worth reading just to refuse; vault_client.py
# pings before uploading so auth problems surface before that point.
_MAX_DRAIN = 64 * 1024 * 1024


def is_local_network(ip: str) -> bool:
    """Loopback/private/link-local/Tailscale only — the same "never from
    the public internet, even if a port gets forwarded by mistake" rule
    takeloom's own remote server enforces (remote/server.py)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr in _TAILSCALE


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class _HTTPError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class VaultHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, vault: Vault, auth: Auth) -> None:
        self.vault = vault
        self.auth = auth
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: VaultHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "takeloom-vault"

    # --- plumbing ---

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # requests worth noting are logged explicitly below

    @property
    def ip(self) -> str:
        return self.client_address[0]

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, html.encode(), "text/html; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": location})

    def _drain_unread_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if self._body_read or not 0 < length <= _MAX_DRAIN:
            return
        self._body_read = True
        while length > 0:
            chunk = self.rfile.read(min(1024 * 1024, length))
            if not chunk:
                break
            length -= len(chunk)

    def _read_json(self) -> dict:
        self._body_read = True
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_JSON_BODY:
            raise _HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            raise _HTTPError(HTTPStatus.BAD_REQUEST, f"Invalid JSON body: {e}") from e
        if not isinstance(data, dict):
            raise _HTTPError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _require_token(self) -> str:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        client = self.server.auth.authenticate(token, self.ip)
        if client is None:
            raise _HTTPError(HTTPStatus.UNAUTHORIZED, "Missing or unrecognized vault token — pair this device first.")
        return client.label

    def _require_same_origin(self) -> None:
        """Browser form POSTs (approve/deny/revoke) have no token — the web
        UI is open to the LAN — so at least refuse ones submitted from some
        *other* site's page, which a browser always labels via Origin."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise _HTTPError(HTTPStatus.FORBIDDEN, "Cross-origin request refused")

    def _dispatch(self) -> None:
        if not is_local_network(self.ip):
            self._send(HTTPStatus.FORBIDDEN, b"Local network only\n", "text/plain")
            self.close_connection = True
            return
        path = unquote(urlsplit(self.path).path)
        api = path.startswith("/api/")
        self._body_read = False
        try:
            self._route(self.command, path)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            # An error can leave a request body (possibly a whole upload)
            # unread, so this connection can't be reused for another request.
            self.close_connection = True
            self._drain_unread_body()
            if isinstance(e, _HTTPError):
                status, title, message = e.status, e.status.phrase, str(e)
            elif isinstance(e, VaultPathError):
                status, title, message = HTTPStatus.BAD_REQUEST, "Bad path", str(e)
            else:  # keep serving; report what went wrong
                log(f"ERROR {self.command} {path} from {self.ip}: {type(e).__name__}: {e}")
                status, title, message = HTTPStatus.INTERNAL_SERVER_ERROR, "Error", f"{type(e).__name__}: {e}"
            if api:
                self._json({"error": message}, status)
            else:
                self._html(web.message_page(title, message), status)

    do_GET = do_HEAD = do_POST = do_PUT = _dispatch

    # --- routing ---

    def _route(self, method: str, path: str) -> None:
        vault, auth = self.server.vault, self.server.auth

        # Pairing — the only API calls allowed without a token.
        if path == "/api/pair" and method == "POST":
            name = str(self._read_json().get("client_name", ""))
            req = auth.request_pairing(name, self.ip)
            log(f"Pairing requested by {req.client_name} ({self.ip}), code {req.code} — approve it in the web UI")
            return self._json({"request_id": req.id, "code": req.code})
        if (m := re.fullmatch(r"/api/pair/([\w-]+)", path)) and method == "GET":
            return self._json(auth.poll(m.group(1), self.ip))

        if path.startswith("/api/"):
            label = self._require_token()
            return self._route_api(method, path[len("/api"):], label)

        # Browser UI.
        if method == "POST":
            self._require_same_origin()
            if m := re.fullmatch(r"/pairing/([\w-]+)/(approve|deny)", path):
                req = auth.decide(m.group(1), approve=m.group(2) == "approve")
                if req is not None:
                    log(f"Pairing for {req.client_name} ({req.ip}) {req.status} via web UI from {self.ip}")
                return self._redirect("/")
            if m := re.fullmatch(r"/clients/(\w+)/revoke", path):
                if auth.revoke(m.group(1)):
                    log(f"Paired client {m.group(1)} revoked via web UI from {self.ip}")
                return self._redirect("/clients")
            raise _HTTPError(HTTPStatus.NOT_FOUND, "Not found")
        if method not in ("GET", "HEAD"):
            raise _HTTPError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
        if path == "/":
            return self._html(web.sessions_page(vault, vault.all_session_logs(), auth.pending_requests()))
        if path == "/clients":
            return self._html(web.clients_page(list(auth.clients), auth.pending_requests()))
        if m := re.fullmatch(r"/sessions/([^/]+)", path):
            data = vault.read_session_log(m.group(1))
            if data is None:
                raise _HTTPError(HTTPStatus.NOT_FOUND, f"No session named {m.group(1)!r}")
            return self._html(web.session_page(vault, m.group(1), data))
        if path.startswith("/media/"):
            return self._send_file(vault.resolve(path[len("/media/"):]))
        if path == "/favicon.ico":
            return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        raise _HTTPError(HTTPStatus.NOT_FOUND, "Not found")

    def _route_api(self, method: str, path: str, label: str) -> None:
        vault = self.server.vault
        if path == "/ping" and method == "GET":
            return self._json({"ok": True, "hostname": socket.gethostname(), "client": label})
        if path == "/sessions" and method == "GET":
            return self._json({"sessions": vault.all_session_logs()})
        if m := re.fullmatch(r"/sessions/([^/]+)/log", path):
            name = m.group(1)
            if method == "GET":
                data = vault.read_session_log(name)
                if data is None:
                    raise _HTTPError(HTTPStatus.NOT_FOUND, f"No session named {name!r}")
                return self._json(data)
            if method == "PUT":
                try:
                    vault.write_session_log(name, self._read_json())
                except FileNotFoundError:
                    raise _HTTPError(HTTPStatus.NOT_FOUND, f"No session named {name!r}") from None
                log(f"{label} ({self.ip}) updated sessions/{name}/session_log.json")
                return self._json({})
        if (m := re.fullmatch(r"/list/(.+)", path)) and method == "GET":
            return self._json({"files": vault.list_dir(m.group(1))})
        if path.startswith("/files/"):
            relative = path[len("/files/"):]
            if method in ("GET", "HEAD"):
                return self._send_file(vault.resolve(relative))
            if method == "PUT":
                return self._receive_file(relative, label)
        if path == "/delete" and method == "POST":
            paths = self._read_json().get("paths") or []
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise _HTTPError(HTTPStatus.BAD_REQUEST, "'paths' must be a list of strings")
            vault.delete(paths)
            log(f"{label} ({self.ip}) deleted: {', '.join(paths)}")
            return self._json({})
        raise _HTTPError(HTTPStatus.NOT_FOUND, f"No API route {method} /api{path}")

    # --- file transfer ---

    def _receive_file(self, relative: str, label: str) -> None:
        length_header = self.headers.get("Content-Length")
        expected = (self.headers.get("X-Content-SHA256") or "").strip()
        if length_header is None:
            raise _HTTPError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise _HTTPError(HTTPStatus.BAD_REQUEST, "X-Content-SHA256 header (hex SHA-256 of the body) required")
        self.server.vault.resolve(relative)  # refuse a bad path before reading anything
        self._body_read = True
        try:
            result = self.server.vault.receive_file(relative, self.rfile, int(length_header), expected)
        except ValueError as e:
            # The body may be only partly read — this connection's framing
            # is unrecoverable, so don't try to reuse it.
            self.close_connection = True
            raise _HTTPError(HTTPStatus.UNPROCESSABLE_ENTITY, str(e)) from e
        log(f"{label} ({self.ip}) uploaded {relative} ({result['size']:,} bytes)")
        self._json(result)

    def _send_file(self, path) -> None:
        if not path.is_file():
            raise _HTTPError(HTTPStatus.NOT_FOUND, "File not found")
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if m := re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip()):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            elif m.group(2):  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", "text/plain",
                           {"Content-Range": f"bytes */{size}"})
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def serve(vault: Vault, auth: Auth, host: str, port: int) -> None:
    httpd = VaultHTTPServer((host, port), vault, auth)
    log(f"takeloom-vault serving {vault.root} on http://{socket.gethostname()}:{port} (pid {os.getpid()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        sys.stdout.flush()
