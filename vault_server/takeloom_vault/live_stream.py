"""The live stream, from the vault.

It used to come from the recording node (takeloom/video/stream_switcher.py
on the Mac Mini), over the node's Wi-Fi: every phone's live feed in, the
stream out. Now the phones' live feeds already come here (camera_hub.py),
and the vault is wired to the router, so the stream goes out from here and
the node only sends its live mix up: one small audio stream.

A node streaming a session opens a connection of its own to the camera
hub (role "stream", takeloom/phone_camera/vault_stream.py its other end):

    node -> vault  {"kind": "hello", "role": "stream", "token", "client_name",
                    "session_id", "targets": [rtmp url, ...], "sample_rate",
                    "channels", "width", "bitrate_kbps", "program": camera_device,
                    "labels": {camera_device: name}, "footer": [part, ...], "song"}
    vault -> node  {"kind": "stream_ack", "ok", "error"?}
                   {"kind": "stream_status", "status", "active"}
    node -> vault  FRAME_AUDIO: 8-byte BE capture time (ns, on the vault's
                   clock) + interleaved little-endian int16 PCM
                   {"kind": "program", "camera": camera_device}
                   {"kind": "footer", "parts": [...], "song": name|null}
                   {"kind": "stop"}

One ffmpeg makes the stream, as the node's switcher did: its video input
is MJPEG frames on stdin, its audio the node's mix through a FIFO. Every
phone's live H.264 is decoded here to stream-size MJPEG (one ffmpeg each);
only the program camera's frames are written through, so switching is
instant. A camera that isn't a phone here (a webcam on the node) can't be
shown: the picture holds on the last phone. ffmpeg stamps each frame and
each audio block as it arrives; the audio is delayed by however much later
the program camera's frames arrive than its sound (both measured when the
stream starts), so they line up.

The footer is the live half of footer.py's: the node's text, with the
song's working title (completed.py) in place of its name, and the program
camera's name.
"""

from __future__ import annotations

import os
import queue
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

FRAME_AUDIO = 5
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc")
_FOOTER_FILE = "footer.txt"
SEPARATOR = "   •   "  # footer.py's
_REPEAT_AFTER_S = 0.2
_SOURCE_RESOLUTION_WIDTH = 1920
DEVICE_PREFIX = "takeloom-camera:"


def read_jpeg_frames(stream, on_frame: Callable[[bytes], None]) -> None:
    """takeloom/video/mjpeg.py's: split ffmpeg's MJPEG output into JPEGs."""
    buf = b""
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start == -1:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end == -1:
                    if start > 0:
                        buf = buf[start:]
                    break
                frame, buf = buf[start:end + 2], buf[end + 2:]
                try:
                    on_frame(frame)
                except Exception:  # noqa: BLE001
                    pass
    except (OSError, ValueError):
        pass


def _black_jpeg(width: int, height: int) -> bytes:
    result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={width}x{height}",
                             "-frames:v", "1", "-f", "mjpeg", "-"], capture_output=True)
    return result.stdout


class _PhoneDecoder:
    """One phone's live H.264, decoded to stream-size MJPEG frames."""

    def __init__(self, width: int, on_frame: Callable[[bytes], None]) -> None:
        self._proc = subprocess.Popen(
            # Decoding starts on the first frames, not after a probe's worth.
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "32768", "-analyzeduration", "0", "-thread_type", "slice",
             "-f", "h264", "-i", "pipe:0", "-vf", f"scale={width}:-2", "-f", "mjpeg", "-q:v", "3", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._queue: queue.Queue = queue.Queue(maxsize=120)
        self._started = False
        threading.Thread(target=read_jpeg_frames, args=(self._proc.stdout, on_frame), daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()

    def push(self, pts_ns: int, keyframe: bool, data: bytes) -> None:
        if not self._started:
            if not keyframe:
                return
            self._started = True
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self._started = False  # behind: start again at the next keyframe

    def _write(self) -> None:
        try:
            while True:
                data = self._queue.get()
                if data is None:
                    return
                self._proc.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class _AudioFifo:
    """The node's mix into ffmpeg's audio FIFO, never blocking the reader
    (takeloom/streaming.py's LiveAudioFeeder, without the audio callback)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path)
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            pass

    def _run(self) -> None:
        fd = None
        deadline = time.monotonic() + 15
        while fd is None and not self._stop.is_set() and time.monotonic() < deadline:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                self._stop.wait(0.05)
        if fd is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    view = memoryview(self._queue.get(timeout=0.5))
                except queue.Empty:
                    continue
                while view and not self._stop.is_set():
                    _, writable, _ = select.select([], [fd], [], 0.5)
                    if not writable:
                        continue
                    try:
                        view = view[os.write(fd, view):]
                    except BlockingIOError:
                        continue
                    except OSError:
                        return
        finally:
            os.close(fd)

    def stop(self) -> None:
        self._stop.set()


class LiveStream:
    def __init__(self, hub, vault, hello: dict, log: Callable[[str], None],
                 status: Callable[[str, bool], None]) -> None:
        self.hub = hub
        self.vault = vault
        self.session_id = str(hello.get("session_id") or "")
        self.targets = [str(t) for t in hello.get("targets") or [] if str(t).startswith(("rtmp://", "rtmps://"))]
        self.sample_rate = int(hello.get("sample_rate") or 48000)
        self.channels = int(hello.get("channels") or 2)
        width = int(hello.get("width") or 0)
        self.width = width if width > 0 else _SOURCE_RESOLUTION_WIDTH
        self.height = round(self.width * 9 / 16 / 2) * 2
        self.bitrate = int(hello.get("bitrate_kbps") or 3000)
        self.framerate = 30
        self.labels = {str(k): str(v) for k, v in (hello.get("labels") or {}).items()}
        self._program = str(hello.get("program") or "")
        self._footer_parts = [str(p) for p in hello.get("footer") or []]
        self._song = hello.get("song")
        self._log = log
        self._status = status
        self._lock = threading.Lock()
        self._latest: dict[str, bytes] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=self.framerate)
        self._decoders: dict[str, _PhoneDecoder] = {}
        self._stopping = threading.Event()
        self._dir = Path(tempfile.mkdtemp(prefix="takeloom-vault-stream-"))
        self._audio: _AudioFifo | None = None
        self._proc: subprocess.Popen | None = None
        self.audio_latency_ns: int | None = None

    # --- the program camera and the footer ---

    def _phone(self, device: str) -> str:
        return device[len(DEVICE_PREFIX):] if device.startswith(DEVICE_PREFIX) else ""

    def set_program(self, device: str) -> None:
        with self._lock:
            self._program = device
            latest = self._latest.get(self._phone(device))
        if latest is not None:
            self._enqueue(latest)
        self._write_footer()

    def set_footer(self, parts: list[str], song: str | None) -> None:
        with self._lock:
            self._footer_parts, self._song = [str(p) for p in parts], song
        self._write_footer()

    def _footer_text(self) -> str:
        from .completed import working_titles
        with self._lock:
            parts, song, program = list(self._footer_parts), self._song, self._program
        title = working_titles(self.vault.root).get(song) if song else None
        if title:
            parts = [title if p == song else p for p in parts]
        return SEPARATOR.join(p for p in [*parts, self.labels.get(program, "")] if p)

    def _write_footer(self) -> None:
        tmp = self._dir / (_FOOTER_FILE + ".tmp")
        try:
            tmp.write_text(self._footer_text() or " ")
            os.replace(tmp, self._dir / _FOOTER_FILE)  # whole, never in place: ffmpeg rereads it every frame
        except OSError:
            pass

    # --- video ---

    def _on_phone_video(self, device_id: str, pts_ns: int, keyframe: bool, data: bytes) -> None:
        with self._lock:
            decoder = self._decoders.get(device_id)
            if decoder is None and not self._stopping.is_set():
                decoder = self._decoders[device_id] = _PhoneDecoder(
                    self.width, lambda jpeg, d=device_id: self._on_jpeg(d, jpeg))
        decoder.push(pts_ns, keyframe, data)

    def _on_jpeg(self, device_id: str, jpeg: bytes) -> None:
        with self._lock:
            self._latest[device_id] = jpeg
            is_program = self._phone(self._program) == device_id
        if is_program:
            self._enqueue(jpeg)

    def _enqueue(self, jpeg: bytes) -> None:
        try:
            self._queue.put_nowait(jpeg)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(jpeg)
            except queue.Full:
                pass

    def _write_frames(self, proc: subprocess.Popen) -> None:
        black = _black_jpeg(self.width, self.height)
        last = None
        try:
            while not self._stopping.is_set():
                try:
                    frame = self._queue.get(timeout=_REPEAT_AFTER_S)
                    last = frame
                except queue.Empty:
                    frame = last or black  # program camera silent: hold the picture
                proc.stdin.write(frame)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    # --- audio ---

    def push_audio(self, payload: bytes) -> None:
        if len(payload) < 8 or self._audio is None:
            return
        (captured,) = struct.unpack_from(">q", payload)
        if captured:
            latency = time.monotonic_ns() - captured
            prev = self.audio_latency_ns
            self.audio_latency_ns = latency if prev is None else (prev * 15 + latency) // 16
        self._audio.push(payload[8:])

    # --- the stream ---

    def start(self) -> bool:
        if not self.targets:
            self._status("No stream target (RTMP URL) was given.", False)
            return False
        self._write_footer()
        from .edit import can_draw_text
        font = next((f for f in _FONTS if Path(f).exists()), None) if can_draw_text() else None
        if font is None:
            self._log("The stream has no footer: this ffmpeg has no drawtext, or no font was found")
        bar = max(28, self.height // 24)
        size = max(13, bar - 12)
        footer = (f",drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black@0.43:t=fill,"
                  f"drawtext=fontfile='{font}':textfile={_FOOTER_FILE}:reload=1:expansion=none:"
                  f"fontsize={size}:fontcolor=white@0.92:x=14:y=h-{bar}+({bar}-th)/2") if font else ""
        # How much later the program camera's frames get here than the
        # node's sound: the audio waits that long (see the docstring).
        cam = next((c for c in self.hub.cameras() if c["device_id"] == self._phone(self._program)), None)
        video_latency = (cam or {}).get("live_latency_ns") or 0
        delay_s = max(0.0, min(2.0, (video_latency - (self.audio_latency_ns or 0)) / 1e9))
        self._audio = _AudioFifo(self._dir / "audio.fifo")
        w, h, b = self.width, self.height, self.bitrate
        out = (["-f", "flv", self.targets[0]] if len(self.targets) == 1 else
               ["-flags", "+global_header", "-f", "tee",
                "|".join(f"[f=flv:onfail=ignore]{t}" for t in self.targets)])
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "1024", "-f", "mjpeg", "-i", "pipe:0",
            "-thread_queue_size", "1024", "-itsoffset", f"{delay_s:.3f}",
            "-f", "s16le", "-ar", str(self.sample_rate), "-ac", str(self.channels), "-i", str(self._audio.path),
            "-filter_complex",
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={self.framerate}{footer},format=yuv420p[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{b}k", "-maxrate", f"{b}k", "-bufsize", f"{b * 2}k",
            "-g", str(self.framerate * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "44100", *out,
        ]
        log_path = None
        try:
            directory = self.vault.session_dir(self.session_id)
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "stream.log"
        except Exception:  # noqa: BLE001 — no session folder: no log file
            pass
        err = open(log_path, "ab") if log_path else subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                          cwd=self._dir)
        except OSError as e:
            self._status(f"The vault couldn't start ffmpeg for the stream: {e}", False)
            return False
        finally:
            if log_path:
                err.close()
        self.hub.add_video_tap(self._on_phone_video)
        for c in self.hub.cameras():
            self.hub.send(c["device_id"], {"kind": "request_keyframe"})
        threading.Thread(target=self._write_frames, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        where = ", ".join(t.split("/")[2] if t.count("/") >= 2 else t for t in self.targets)
        self._log(f"Streaming {self.session_id} live to {where} ({w}x{h}, {b} kbps, audio delayed {delay_s * 1000:.0f} ms)")
        self._status(f"The vault is streaming live ({w}x{h}).", True)
        return True

    def _watch(self) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.wait()
        if not self._stopping.is_set():
            self._log(f"The stream of {self.session_id} ended by itself (ffmpeg {proc.returncode}; see its stream.log)")
            self._status(f"The vault's stream stopped (ffmpeg exited {proc.returncode}; see the session's stream.log).",
                         False)

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.hub.remove_video_tap(self._on_phone_video)
        if self._audio is not None:
            self._audio.stop()  # EOF on the audio first: ffmpeg may be blocked reading it
        proc = self._proc
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        with self._lock:
            decoders = list(self._decoders.values())
            self._decoders.clear()
        for d in decoders:
            d.stop()
        shutil.rmtree(self._dir, ignore_errors=True)
        self._log(f"Stream of {self.session_id} ended")
