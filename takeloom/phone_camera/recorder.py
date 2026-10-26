"""PhoneVideoRecorder: VideoRecorder's stand-in when the session camera is a
Takeloom Camera phone.

The phone records its own full-quality file for the session and uploads
it afterwards (see sync.py for how that becomes session_video_raw.mp4).
Meanwhile this records the *live* feed the phone streams over Wi-Fi, for
two reasons:

- the fallback session video, in case that upload never arrives (phone
  died, app killed, ...) — gaps where Wi-Fi dropped frames and all; and
- live streaming: stream-quality MJPEG frames for video/stream_switcher.py,
  the same as VideoRecorder tees off a local camera (`on_stream_frame`).

The phone's encoded H.264 is passed straight through (no decode) to the
local file, written via a named pipe so ffmpeg reads it like any other
input. Raw H.264 carries no timestamps, so ffmpeg stamps each frame on
arrival (-use_wallclock_as_timestamps); good enough for a fallback and a
stream, and why the uploaded file is preferred whenever it shows up.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..ffmpeg_bin import FFMPEG
from ..video.mjpeg import read_jpeg_frames
from .hub import PhoneCameraHub

# About 10 s of 30 fps video. Past that the writer is hopelessly behind
# (ffmpeg stalled): drop until the next keyframe rather than grow forever.
_MAX_QUEUED_FRAMES = 300


def build_phone_capture_cmd(
    video_fifo: Path, output_path: Path, stream_frames_fd: int | None = None, stream_width: int = 1280,
) -> list[str]:
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "1024",
        "-probesize", "1M", "-analyzeduration", "500000",
        "-f", "h264", "-i", str(video_fifo),
    ]
    # Same fragmented-MP4 reasoning as video/capture.py's _record_output_args
    # (playable however ffmpeg dies), but a stream copy — the phone already
    # encoded it.
    cmd += [
        "-map", "0:v", "-c:v", "copy",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(output_path),
    ]
    if stream_frames_fd is not None:
        cmd += ["-map", "0:v", "-vf", f"scale={stream_width}:-2", "-f", "mjpeg", "-q:v", "3",
                f"pipe:{stream_frames_fd}"]
    return cmd


class PhoneVideoRecorder:
    """Same start()/stop()/is_running surface as video.capture.VideoRecorder,
    so _ActiveSession can hold either."""

    def __init__(
        self,
        hub: PhoneCameraHub,
        device_id: str,
        session_id: str,
        output_path: Path,
        on_stream_frame: Callable[[bytes], None] | None = None,
        stream_width: int = 1280,
    ) -> None:
        self.hub = hub
        self.device_id = device_id
        self.session_id = session_id
        self.output_path = output_path
        self._on_stream_frame = on_stream_frame
        self.stream_width = stream_width
        # Per output file: several phones can be recording into one session.
        self._fifo = output_path.with_name(output_path.stem + ".h264.fifo")
        self._queue: queue.Queue = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._writer: threading.Thread | None = None
        self._stopping = threading.Event()
        # Set by the writer on the first frame it actually hands to ffmpeg
        # — where the live file's timeline starts, in phone-clock ns, plus
        # the clock offset in force then (sync.py's fallback alignment).
        self.first_live_pts_ns: int | None = None
        self.first_live_clock_offset_ns: int | None = None

    def start(self) -> bool:
        info = self.hub.camera(self.device_id)
        if info is None or FFMPEG is None:
            return False
        self._fifo.unlink(missing_ok=True)
        os.mkfifo(self._fifo)
        stream_read_fd = stream_write_fd = None
        if self._on_stream_frame is not None:
            stream_read_fd, stream_write_fd = os.pipe()
        cmd = build_phone_capture_cmd(self._fifo, self.output_path, stream_write_fd, self.stream_width)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(stream_write_fd,) if stream_write_fd is not None else (),
        )
        if stream_write_fd is not None:
            os.close(stream_write_fd)
            threading.Thread(
                target=read_jpeg_frames, args=(os.fdopen(stream_read_fd, "rb"), self._on_stream_frame),
                daemon=True,
            ).start()
        self._writer = threading.Thread(target=self._write_frames, daemon=True)
        self._writer.start()
        self.hub.set_video_sink(self.device_id, self._on_frame)
        self.hub.send(self.device_id, {"kind": "request_keyframe"})
        self.hub.start_recording(self.device_id, self.session_id)
        return True

    def _on_frame(self, pts_ns: int, keyframe: bool, data: bytes) -> None:
        if self._queue.qsize() < _MAX_QUEUED_FRAMES or keyframe:
            self._queue.put((pts_ns, keyframe, data))

    def _write_frames(self) -> None:
        # Opening a FIFO for writing blocks until ffmpeg opens it to read.
        try:
            out = open(self._fifo, "wb", buffering=0)
        except OSError:
            return
        started = False
        dropping = False
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._stopping.is_set():
                        return
                    continue
                if item is None:
                    return
                pts_ns, keyframe, data = item
                if self._queue.qsize() >= _MAX_QUEUED_FRAMES:
                    dropping = True
                if dropping or not started:
                    if not keyframe:
                        continue
                    dropping = False
                if not started:
                    started = True
                    info = self.hub.camera(self.device_id)
                    self.first_live_pts_ns = pts_ns
                    self.first_live_clock_offset_ns = info.clock_offset_ns if info else None
                out.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                out.close()  # EOF: ffmpeg finishes the file
            except OSError:
                pass

    def stop(self, timeout: float = 10.0) -> bool:
        self.hub.set_video_sink(self.device_id, None)
        self.hub.stop_recording(self.device_id, self.session_id)
        self._stopping.set()
        self._queue.put(None)
        clean = True
        proc, self._proc = self._proc, None
        if self._writer is not None:
            self._writer.join(timeout=2.0)
            if self._writer.is_alive():
                # Still blocked opening the FIFO: ffmpeg never started
                # reading. Open the read end ourselves to release it.
                try:
                    fd = os.open(self._fifo, os.O_RDONLY | os.O_NONBLOCK)
                    os.close(fd)
                except OSError:
                    pass
                self._writer.join(timeout=2.0)
            self._writer = None
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                clean = False
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.wait(timeout=5.0)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        self._fifo.unlink(missing_ok=True)
        return clean

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def session_log_info(self, mix_start_server_ns: int) -> dict:
        """What sync.py needs later, saved into session_log.json's
        "phone_camera" (see backend.py's _save_session_log)."""
        info = self.hub.camera(self.device_id)
        return {
            "device_id": self.device_id,
            "client_name": info.client_name if info else "",
            "session_id": self.session_id,
            "mix_start_server_ns": mix_start_server_ns,
            "phone_video_start_pts_ns": self.hub.recording_start_pts(self.session_id, self.device_id),
            "first_live_pts_ns": self.first_live_pts_ns,
            "first_live_clock_offset_ns": self.first_live_clock_offset_ns,
            "clock_offset_ns": info.clock_offset_ns if info else None,
            "clock_rtt_ns": info.clock_rtt_ns if info else None,
            "logged_at_ns": time.monotonic_ns(),
        }
