"""Reading ffmpeg's MJPEG output (`-f mjpeg`) one JPEG at a time."""

from __future__ import annotations

from typing import BinaryIO, Callable


def read_jpeg_frames(stream: BinaryIO, on_frame: Callable[[bytes], None]) -> None:
    """Split a raw MJPEG byte stream into individual JPEGs (each delimited
    by its own SOI/EOI markers) and hand each to on_frame, until the stream
    ends. Exceptions from on_frame are swallowed: one bad frame never stops
    the feed."""
    buf = b""
    try:
        while True:
            chunk = stream.read(65536)
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
                except Exception:
                    pass
    except (OSError, ValueError):
        pass
