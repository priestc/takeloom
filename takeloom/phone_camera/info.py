"""What's known about a Takeloom Camera phone, and where its recording of
a session lands. The phones connect to the vault (vault_server's
camera_hub.py); a recording node sees them through phone_camera/
vault_hub.py, as PhoneCameraInfo. (The node's own phone server, hub.py,
went once the vault had one.)"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# Where a phone recording lands for a session from before cameras were a set
# (its one camera; sync.py turns it into session_video_raw.mp4). Every camera
# of a newer session lands at its own path instead —
# see upload_paths().
UPLOAD_FILE_NAME = "phone_camera.mp4"
UPLOAD_META_NAME = "phone_camera_upload.json"


def upload_paths(session_dir: Path, file_name: str = UPLOAD_FILE_NAME) -> tuple[Path, Path, Path]:
    """(final file, in-progress .part, metadata JSON) for an upload saved
    as session_dir/file_name. Flat in session_dir, never a subdirectory:
    the vault only uploads a session's top-level files (vault_client.py)."""
    dest = session_dir / file_name
    if file_name == UPLOAD_FILE_NAME:
        return dest, session_dir / "phone_camera_upload.part", session_dir / UPLOAD_META_NAME
    return dest, dest.with_name(dest.name + ".part"), dest.with_name(dest.stem + ".upload.json")


@dataclass
class PhoneCameraInfo:
    device_id: str
    client_name: str
    platform: str
    ip: str
    app_version: str = ""
    device_model: str = ""  # see models.py
    status: dict = field(default_factory=dict)
    # vault_ns = phone_ns + clock_offset_ns (the phones sync with the
    # vault's clock); None until the phone's first clock_sync report.
    clock_offset_ns: int | None = None
    clock_rtt_ns: int | None = None
    # Smoothed (vault receive time - capture time) of its live frames: how
    # stale its live feed is when it reaches the vault.
    live_latency_ns: int | None = None
