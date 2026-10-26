"""Wire format between the takeloom server and the Takeloom Camera phone app
(camera_app/ios, camera_app/android). See docs/takeloom-camera.md for the
full picture; this module is the server's half of it.

One persistent TCP connection per phone on CAMERA_SERVER_PORT, carrying
length-prefixed binary frames — not the remote protocol's newline JSON,
since most of the traffic here is compressed video:

    [1 byte type][4 bytes big-endian payload length][payload]

    FRAME_JSON     UTF-8 JSON object with a "kind" (control messages below)
    FRAME_VIDEO    8-byte BE int64 capture timestamp (ns, phone clock) +
                   1 flag byte (bit 0 = keyframe) + one H.264 access unit,
                   Annex-B, SPS/PPS repeated ahead of every keyframe
    FRAME_PREVIEW  one small JPEG (~320px wide, a few per second) for the
                   Record tab's live preview
    FRAME_UPLOAD   a chunk of the finished recording's file bytes (upload
                   connections only)

Control messages (FRAME_JSON):

    phone -> server  {"kind": "hello", "role": "camera"|"upload", "token": "...",
                      "client_name": "...", "device_id": "...", "platform": "ios"|"android",
                      "app_version": "...",
                      "device_model": "..."}  iOS: the hardware identifier ("iPhone14,4");
                                              Android: a readable model name. See models.py.
    server -> phone  {"kind": "hello_pending"}      while a human approves pairing
                     {"kind": "hello_ack", "ok": true, "hostname": "...", "token"?: "..."}
                     {"kind": "hello_ack", "ok": false, "error": "..."}

    phone -> server  {"kind": "clock_ping", "id": n, "t0": phone_ns}
    server -> phone  {"kind": "clock_pong", "id": n, "t0": phone_ns, "ts": server_ns}
    phone -> server  {"kind": "clock_sync", "offset_ns": n, "rtt_ns": n}
                     (server_ns = phone_ns + offset_ns, from the lowest-RTT
                     recent ping — NTP's estimate, computed phone-side)
    phone -> server  {"kind": "status", "width", "height", "fps", "camera": "back"|"front",
                      "battery": 0..1|null, "charging": bool, "free_bytes": n,
                      "recording_session": "..."|null, "pending_uploads": n}

    server -> phone  {"kind": "start_recording", "session_id": "..."}
    phone -> server  {"kind": "recording_started", "session_id": "...", "video_start_pts_ns": n}
    server -> phone  {"kind": "stop_recording", "session_id": "..."}
    phone -> server  {"kind": "recording_stopped", "session_id": "...", "file_size": n,
                      "video_start_pts_ns": n, "duration_s": f}
    phone -> server  {"kind": "recording_failed", "session_id": "...", "error": "..."}
    server -> phone  {"kind": "request_keyframe"}

Uploads happen on a second connection (hello with role "upload"), so a
multi-GB transfer never sits in front of the live feed or clock pings:

    phone -> server  {"kind": "upload_begin", "session_id": "...", "size": n,
                      "metadata": {"video_start_pts_ns", "clock_offset_ns",
                                   "clock_rtt_ns", "width", "height", "fps", ...}}
    server -> phone  {"kind": "upload_resume", "offset": n}   bytes already held
                     {"kind": "upload_rejected", "error": "..."} (no such session, ...)
    phone -> server  FRAME_UPLOAD chunks from that offset, then {"kind": "upload_end"}
    server -> phone  {"kind": "upload_done", "ok": true} | {"kind": "upload_done", "ok": false, "error": "..."}

Same LAN-only pairing model as the remote protocol (takeloom/remote/
protocol.py): tokens live in StudioConfig.remote_authorized_clients.
"""

from __future__ import annotations

import json
import socket
import struct

# REMOTE_SERVER_PORT + 1 — fixed for the same reason that one is (see
# takeloom/remote/protocol.py): an app and a server that can only ever
# agree on a port in advance can never drift apart.
CAMERA_SERVER_PORT = 51824

FRAME_JSON = 1
FRAME_VIDEO = 2
FRAME_PREVIEW = 3
FRAME_UPLOAD = 4

# Generous enough for a 4K keyframe at a high bitrate; anything bigger is a
# corrupt or hostile stream, not a frame.
MAX_FRAME_BYTES = 32 * 1024 * 1024

# camera_device values for a phone are this prefix + the app's device_id,
# so the existing config field / Recording Devices picker carry it as-is.
DEVICE_PREFIX = "takeloom-camera:"

_HEADER = struct.Struct(">BI")
_VIDEO_HEADER = struct.Struct(">qB")


class ProtocolError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1024 * 1024))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    frame_type, length = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES}-byte limit")
    return frame_type, _recv_exact(sock, length)


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    return _HEADER.pack(frame_type, len(payload)) + payload


def encode_json(msg: dict) -> bytes:
    return encode_frame(FRAME_JSON, json.dumps(msg).encode("utf-8"))


def decode_json(payload: bytes) -> dict:
    try:
        msg = json.loads(payload.decode("utf-8"))
    except ValueError as e:
        raise ProtocolError(f"bad JSON frame: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("JSON frame is not an object")
    return msg


def decode_video(payload: bytes) -> tuple[int, bool, bytes]:
    """(capture pts in phone-clock ns, is_keyframe, Annex-B access unit)."""
    if len(payload) < _VIDEO_HEADER.size:
        raise ProtocolError("truncated video frame")
    pts_ns, flags = _VIDEO_HEADER.unpack_from(payload)
    return pts_ns, bool(flags & 1), payload[_VIDEO_HEADER.size:]


def is_phone_device(camera_device: str) -> bool:
    return camera_device.startswith(DEVICE_PREFIX)


def phone_device_id(camera_device: str) -> str:
    return camera_device[len(DEVICE_PREFIX):]
