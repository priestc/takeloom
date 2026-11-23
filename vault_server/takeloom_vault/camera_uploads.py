"""Takeloom Camera recordings uploaded straight here by the phones.

When this vault encodes a session's videos (render.py), the phones' own
full-quality recordings don't go through the studio machine at all: its
takeloom server, asked by a phone where to upload, points it here instead
(takeloom/phone_camera/hub.py's upload_redirect), with a token it got for
that phone (auth.py's delegate). The studio then lines each recording up
with the session's audio from a small extract of its sound (audio_for),
and leaves render jobs that move it into the session and make its video
here (render.py's staged inputs, "normalize" and "mic" jobs).

    <vault>/camera_uploads/<session>/<file>        once complete
                                     <file>.part   while arriving
                                     <file>.size   the size it'll be

Uploads are resumable, in chunks: PUT with ?offset=<bytes already sent>
appends one chunk, and is refused (409, with what's here) if that isn't
the end of what's here, so a phone that dropped mid-chunk just asks again
and carries on. The file takes its real name only once all `size` bytes
are in.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO

from .store import Vault, VaultPathError

DIR = "camera_uploads"
_FILE = re.compile(r"\w[\w .+()'-]*\.mp4")
CHUNK = 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
# The same decode takeloom's phone_camera/sync.py does of a local upload
# (_decode_mono with -copyts and pad_from_zero), at its RATE.
ALIGN_RATE = 8000


class UploadConflict(Exception):
    def __init__(self, received: int) -> None:
        super().__init__(f"this vault has {received} bytes of it")
        self.received = received


class CameraUploads:
    """`in_session`: uploads go straight into the session's own folder
    (sessions/<session>/<file>, its .part and .size beside it) rather than
    camera_uploads/ — for sessions whose cameras are sources (align.py),
    which nothing has to move them out of later."""

    def __init__(self, vault: Vault, in_session: bool = False) -> None:
        self.vault = vault
        self.in_session = in_session
        self._lock = threading.Lock()
        self._busy: set[Path] = set()

    def path(self, session: str, file: str) -> Path:
        if not _FILE.fullmatch(file) or ".." in file or len(file) > 200:
            raise VaultPathError(f"Not a camera recording's file name: {file!r}")
        directory = self.vault.session_dir(session)  # the same check a session name gets
        if self.in_session:
            return directory / file
        return self.vault.resolve(f"{DIR}/{session}/{file}")

    def status(self, session: str, file: str) -> dict:
        dest = self.path(session, file)
        if dest.is_file() or (self.vault.session_dir(session) / file).is_file():
            size = dest.stat().st_size if dest.is_file() else (self.vault.session_dir(session) / file).stat().st_size
            return {"received": size, "size": size, "done": True}
        part = dest.with_name(dest.name + ".part")
        size_file = dest.with_name(dest.name + ".size")
        try:
            size = int(size_file.read_text())
        except (OSError, ValueError):
            size = None
        return {"received": part.stat().st_size if part.exists() else 0, "size": size, "done": False}

    def append(self, session: str, file: str, offset: int, size: int, body: BinaryIO, length: int) -> dict:
        """Write one chunk (`length` bytes of `body`) at `offset` of a
        `size`-byte recording. UploadConflict if `offset` isn't where this
        upload is up to; ValueError for a chunk that doesn't fit."""
        dest = self.path(session, file)
        if not 0 < length <= MAX_CHUNK:
            raise ValueError(f"A chunk must be 1 byte to {MAX_CHUNK} bytes, not {length}")
        if size <= 0 or offset < 0 or offset + length > size:
            raise ValueError(f"A {length}-byte chunk at {offset} doesn't fit a {size}-byte file")
        with self._lock:
            if dest in self._busy:
                raise UploadConflict(self.status(session, file)["received"])
            self._busy.add(dest)
        try:
            if self.status(session, file)["done"]:
                return self.status(session, file)
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            size_file = dest.with_name(dest.name + ".size")
            received = part.stat().st_size if part.exists() else 0
            try:
                recorded = int(size_file.read_text())
            except (OSError, ValueError):
                recorded = None
            if recorded != size:
                # A new upload (or the same one from the start): what's here
                # doesn't belong to it.
                part.unlink(missing_ok=True)
                size_file.write_text(str(size))
                received = 0
            if offset != received:
                raise UploadConflict(received)
            remaining = length
            with open(part, "ab") as out:
                try:
                    while remaining > 0:
                        chunk = body.read(min(CHUNK, remaining))
                        if not chunk:
                            raise ValueError(f"The chunk ended after {length - remaining} of {length} bytes")
                        out.write(chunk)
                        remaining -= len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                except BaseException:
                    # Keep only whole chunks: the phone resends this one.
                    out.truncate(received)
                    raise
            if received + length == size:
                os.chmod(part, 0o644)
                os.replace(part, dest)
                size_file.unlink(missing_ok=True)
            return self.status(session, file)
        finally:
            with self._lock:
                self._busy.discard(dest)

    def complete(self, session: str, file: str) -> Path:
        dest = self.path(session, file)
        if not dest.is_file():
            raise FileNotFoundError(f"{DIR}/{session}/{file} hasn't finished uploading")
        return dest

    def first_video_pts(self, session: str, file: str) -> float:
        """The first video frame's time on the file's own raw timeline —
        takeloom's phone_camera/sync.py's _probe_first_video_pts."""
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-read_intervals", "%+#30", "-of", "csv=p=0", str(self.complete(session, file))],
            capture_output=True, text=True,
        )
        times = [float(x) for x in result.stdout.split() if x and x != "N/A"]
        if not times:
            raise ValueError(f"no video frames found in {file}")
        return min(times)

    def audio_for(self, session: str, file: str) -> Path:
        """Its sound, mono at ALIGN_RATE, as raw little-endian float32
        (sample n = file time n/ALIGN_RATE, silence before the first): what
        takeloom lines the recording up with. A temp file — the caller
        deletes it."""
        src = self.complete(session, file)
        fd, name = tempfile.mkstemp(prefix=".align-", suffix=".f32", dir=src.parent)
        os.close(fd)
        result = subprocess.run(
            [*(["nice", "-n", "10"] if shutil.which("nice") else []), "ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(src), "-map", "0:a:0",
             "-vn", "-ac", "1", "-ar", str(ALIGN_RATE), "-af", "aresample=async=1:first_pts=0",
             "-f", "f32le", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            Path(name).unlink(missing_ok=True)
            raise ValueError(f"ffmpeg couldn't decode {file}: {result.stderr.strip()[-300:]}")
        return Path(name)
