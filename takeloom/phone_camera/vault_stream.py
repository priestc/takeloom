"""VaultStream: a session's live stream, made by the vault.

When the phones are the vault's (vault_hub.py), so is the stream: the
phones' live feeds are already there, and the vault is wired to the
router, where this recording node is usually on Wi-Fi. This stands in for
video/stream_switcher.py's StreamSwitcher in the backend — the same
set_program / footer / stop — but all this machine sends is its live mix
(int16 PCM, about 1.5 Mbps for 48 kHz stereo) and what to show, over a
connection of its own to the vault's camera hub (role "stream"; the
vault's live_stream.py has the messages and does the rest: switching,
footer, encoding, RTMP).

The YouTube broadcast's title is still set from here (backend.py's
_create_youtube_broadcast): that's only a few API calls.

Only the vault's phones can be shown: a webcam on this machine has no way
there, so with one as the program camera the picture holds on the last
phone.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import numpy as np

from .protocol import FRAME_JSON, decode_json, encode_frame, encode_json, read_frame

FRAME_AUDIO = 5  # the vault's live_stream.py


class VaultStream:
    def __init__(self, hub, vault_url: str, token: str, client_name: str, session_id: str, targets: list[str],
                 sample_rate: int, channels: int, width: int, bitrate_kbps: int, program: str,
                 labels: dict[str, str], footer: tuple[list[str], str | None],
                 on_status: Callable[[str, bool], None]) -> None:
        parts = urlsplit(vault_url)
        self.host, self.port = parts.hostname or "", parts.port or 51824
        self.hub = hub  # vault_hub.VaultCameraHub: its clock offset to the vault's
        self._hello = {
            "kind": "hello", "role": "stream", "token": token, "client_name": client_name, "session_id": session_id,
            "targets": targets, "sample_rate": sample_rate, "channels": channels, "width": width,
            "bitrate_kbps": bitrate_kbps, "program": program, "labels": labels,
            "footer": footer[0], "song": footer[1],
        }
        self.channels = channels
        self._program = program
        self._on_status = on_status
        self._sock: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._audio: queue.Queue = queue.Queue(maxsize=100)
        self._stopping = threading.Event()
        self._running = False

    @property
    def stream_width(self) -> int:
        return 0  # nothing here sends the stream frames

    @property
    def program(self) -> str:
        return self._program

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(encode_json(self._hello))
            sock.settimeout(30)
            while True:  # its status may come first
                frame_type, payload = read_frame(sock)
                ack = decode_json(payload) if frame_type == FRAME_JSON else {}
                if ack.get("kind") == "stream_ack":
                    break
                if ack.get("kind") == "stream_status":
                    self._on_status(str(ack.get("status") or ""), bool(ack.get("active")))
        except (OSError, ConnectionError, ValueError) as e:
            self._on_status(f"Couldn't start the stream on the vault at {self.host}:{self.port}: {e}", False)
            return False
        if not ack.get("ok"):
            self._on_status(f"The vault at {self.host}:{self.port} couldn't start the stream: "
                            f"{ack.get('error') or 'no reason given'}", False)
            sock.close()
            return False
        sock.settimeout(None)
        self._sock, self._running = sock, True
        threading.Thread(target=self._send_audio, name="vault-stream-audio", daemon=True).start()
        threading.Thread(target=self._read, name="vault-stream-status", daemon=True).start()
        return True

    def _send(self, data: bytes) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._write_lock:
                sock.sendall(data)
            return True
        except OSError:
            return False

    def _read(self) -> None:
        sock = self._sock
        try:
            while True:
                frame_type, payload = read_frame(sock)
                if frame_type == FRAME_JSON:
                    msg = decode_json(payload)
                    if msg.get("kind") == "stream_status":
                        self._on_status(str(msg.get("status") or ""), bool(msg.get("active")))
                        if not msg.get("active"):
                            self._running = False
        except Exception:  # noqa: BLE001 — the connection's gone
            pass
        if not self._stopping.is_set():
            self._running = False
            self._on_status(f"Lost the connection to the vault's stream at {self.host}:{self.port}.", False)

    # --- what the backend calls ---

    def push_audio(self, block: np.ndarray) -> None:
        """The live mix, from the audio callback: never blocks."""
        try:
            self._audio.put_nowait((time.monotonic_ns(), np.array(block, dtype=np.float32, copy=True)))
        except queue.Full:
            pass  # the link's behind: drop rather than stall the audio

    def _send_audio(self) -> None:
        while not self._stopping.is_set():
            try:
                captured, block = self._audio.get(timeout=0.5)
            except queue.Empty:
                continue
            if block.ndim == 1:
                block = block.reshape(-1, 1)
            if block.shape[1] != self.channels:
                block = (np.repeat(block[:, :1], self.channels, axis=1) if block.shape[1] < self.channels
                         else block[:, :self.channels])
            pcm = (np.clip(block, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            at = self.hub.to_vault_ns(captured) or 0
            if not self._send(encode_frame(FRAME_AUDIO, struct.pack(">q", at) + pcm)):
                return

    def push_frame(self, device: str, jpeg: bytes) -> None:
        pass  # a camera here can't be shown (see the module docstring)

    def set_program(self, device: str) -> None:
        self._program = device
        self._send(encode_json({"kind": "program", "camera": device}))

    def set_footer(self, text: str) -> None:
        pass  # the vault needs the parts (set_footer_parts), to put in the song's working title

    def set_footer_parts(self, parts: list[str], song: str | None) -> None:
        self._send(encode_json({"kind": "footer", "parts": parts, "song": song}))

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._send(encode_json({"kind": "stop"}))
        sock, self._sock = self._sock, None
        self._running = False
        if sock is not None:
            time.sleep(0.2)
            try:
                sock.close()
            except OSError:
                pass
