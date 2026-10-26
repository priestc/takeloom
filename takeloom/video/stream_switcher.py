"""StreamSwitcher: the live stream, switchable between cameras.

One ffmpeg process owns the RTMP stream: its video input is MJPEG frames
written to its stdin, its audio the live mix from LiveAudioFeeder's FIFO
(see streaming.py). Every camera recording the session tees stream-quality
MJPEG frames to push_frame() (video/capture.py's VideoRecorder and
phone_camera/recorder.py's PhoneVideoRecorder, `on_stream_frame`); only the
program camera's are written through, so set_program() switches the stream
between cameras instantly, with no restart. The video-ui picks the program
camera (Backend.set_program_camera), and each switch is logged in the
session log, so the streamed video can be rebuilt afterwards from the
cameras' own recordings.

ffmpeg stamps each frame on arrival (-use_wallclock_as_timestamps) and
resamples to a constant frame rate, scaling every camera to the same 16:9
frame (letterboxed), so cameras with different resolutions or frame rates
switch cleanly. If the program camera stalls, the last frame repeats, so
the stream never starves.
"""

from __future__ import annotations

import io
import queue
import subprocess
import threading
from pathlib import Path

from ..ffmpeg_bin import FFMPEG
from ..streaming import StreamTarget

# Width used when the stream preset says "source resolution" (width 0): a
# switcher needs one fixed output size whatever each camera's is.
_SOURCE_RESOLUTION_WIDTH = 1920
# A program camera silent this long gets its last frame repeated.
_REPEAT_AFTER_S = 0.2


def _black_jpeg(width: int, height: int) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (width, height)).save(out, "JPEG")
    return out.getvalue()


class StreamSwitcher:
    def __init__(self, target: StreamTarget, program: str, framerate: int = 30,
                 log_path: Path | None = None) -> None:
        self.target = target
        self.log_path = log_path  # ffmpeg's errors go here (the session's stream.log)
        self.framerate = framerate
        self.width = target.width if target.width > 0 else _SOURCE_RESOLUTION_WIDTH
        self.height = round(self.width * 9 / 16 / 2) * 2
        self._program = program
        self._latest: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=self.framerate)
        self._proc: subprocess.Popen | None = None
        self._writer: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def stream_width(self) -> int:
        """What the cameras should scale their stream frames to."""
        return self.width

    def start(self) -> bool:
        if FFMPEG is None:
            return False
        w, h, t = self.width, self.height, self.target
        bitrate = f"{t.bitrate_kbps}k"
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "1024",
            "-f", "mjpeg", "-i", "pipe:0",
            "-thread_queue_size", "1024",
            "-f", "f32le", "-ar", str(t.sample_rate), "-ac", str(t.channels), "-i", str(t.audio_fifo),
            "-filter_complex",
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.framerate},format=yuv420p[v]",
            "-map", "[v]", "-map", "1:a",
            # Same encoder settings as before the switcher (see the comment in
            # video/capture.py's history: no -tune zerolatency).
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{t.bitrate_kbps * 2}k",
            "-g", str(self.framerate * 2),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-f", "flv", t.rtmp_url,
        ]
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        finally:
            if self.log_path:
                log.close()  # the child has its own copy
        self._writer = threading.Thread(target=self._write_frames, args=(self._proc,), daemon=True)
        self._writer.start()
        return True

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def program(self) -> str:
        return self._program

    def set_program(self, device: str) -> None:
        with self._lock:
            self._program = device
            latest = self._latest.get(device)
        if latest is not None:
            self._enqueue(latest)  # switch on the next frame, not the camera's next one

    def push_frame(self, device: str, jpeg: bytes) -> None:
        """A stream-quality frame from one camera (any of them)."""
        with self._lock:
            self._latest[device] = jpeg
            is_program = device == self._program
        if is_program:
            self._enqueue(jpeg)

    def _enqueue(self, jpeg: bytes) -> None:
        try:
            self._queue.put_nowait(jpeg)
        except queue.Full:  # ffmpeg's behind: drop the oldest, keep the newest
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(jpeg)
            except queue.Full:
                pass

    def _write_frames(self, proc: subprocess.Popen) -> None:
        last: bytes | None = None
        black = _black_jpeg(self.width, self.height)
        try:
            while not self._stopping.is_set():
                try:
                    frame = self._queue.get(timeout=_REPEAT_AFTER_S)
                except queue.Empty:
                    frame = last or black  # program camera silent: hold the picture
                last = frame if frame is not black else last
                proc.stdin.write(frame)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def stop(self, timeout: float = 10.0) -> None:
        """End the stream: EOF on the video input (the audio FIFO is closed
        by LiveAudioFeeder.stop(), called first), so ffmpeg finishes up."""
        self._stopping.set()
        proc, self._proc = self._proc, None
        if self._writer is not None:
            self._writer.join(timeout=2.0)
            self._writer = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
