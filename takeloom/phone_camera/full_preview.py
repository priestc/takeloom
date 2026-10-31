"""PhoneFullPreview: full-resolution preview frames from a Takeloom Camera
phone, for video-ui's program view (see backend.py's PreviewSubscription).

The phone's own preview JPEGs are thumbnails (~320px), but its live H.264
feed is full quality and always streaming, so the server decodes that:
FULL_PREVIEW_FPS JPEGs out of an ffmpeg reading the feed on stdin. Only
while somebody wants this phone at full resolution (backend.py's
_on_full_preview_wanted), since decoding a 1080p/4K feed isn't free.

The feed is watched through a hub video tap, alongside (never instead of) a
session's PhoneVideoRecorder. Frames queue for a writer thread, which drops
them until the next keyframe once ffmpeg falls behind, so a stalled decoder
never holds up the hub's reader (and with it the recording).
"""

from __future__ import annotations

import queue
import subprocess
import threading
from typing import Callable

from ..ffmpeg_bin import FFMPEG
from ..video.capture import FULL_PREVIEW_SCALE
from ..video.mjpeg import read_jpeg_frames
from .hub import PhoneCameraHub

# About 2 s of 30 fps video.
_MAX_QUEUED_FRAMES = 60


class PhoneFullPreview:
    def __init__(self, hub: PhoneCameraHub, device_id: str, on_frame: Callable[[bytes], None]) -> None:
        self.hub = hub
        self.device_id = device_id
        self._on_frame = on_frame
        self._queue: queue.Queue = queue.Queue()
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        if FFMPEG is None:
            return False
        # Raw H.264 carries no timestamps: stamped on arrival, as in
        # recorder.py, so the fps filter thins it by real time.
        self._proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-use_wallclock_as_timestamps", "1", "-probesize", "1M", "-analyzeduration", "500000",
             "-f", "h264", "-i", "pipe:0",
             "-vf", FULL_PREVIEW_SCALE, "-f", "mjpeg", "-q:v", "3", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._write_frames, args=(self._proc,), daemon=True).start()
        threading.Thread(target=read_jpeg_frames, args=(self._proc.stdout, self._on_frame), daemon=True).start()
        self.hub.add_video_tap(self.device_id, self._on_video)
        self.hub.send(self.device_id, {"kind": "request_keyframe"})  # the decoder can start at once
        return True

    def _on_video(self, pts_ns: int, keyframe: bool, data: bytes) -> None:
        if self._queue.qsize() < _MAX_QUEUED_FRAMES or keyframe:
            self._queue.put((keyframe, data))

    def _write_frames(self, proc: subprocess.Popen) -> None:
        started = dropping = False
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                keyframe, data = item
                if self._queue.qsize() >= _MAX_QUEUED_FRAMES:
                    dropping = True
                if dropping or not started:
                    if not keyframe:
                        continue
                    started, dropping = True, False
                proc.stdin.write(data)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def stop(self) -> None:
        self.hub.remove_video_tap(self.device_id, self._on_video)
        self._queue.put(None)
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
