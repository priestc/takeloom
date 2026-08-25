"""Camera device discovery, via ffmpeg's device-listing support."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from takeloom.ffmpeg_bin import CAMERA_FFMPEG


def ffmpeg_available() -> bool:
    return CAMERA_FFMPEG is not None


def list_cameras() -> list[tuple[str, str]]:
    """Return available cameras as (device_id, display_name) pairs.

    device_id is the platform-specific identifier to pass to ffmpeg's -i
    at capture time (an avfoundation index, a /dev/video path, or a
    dshow device name).
    """
    if sys.platform == "darwin":
        return _list_cameras_avfoundation()
    if sys.platform.startswith("linux"):
        return _list_cameras_v4l2()
    if sys.platform.startswith("win"):
        return _list_cameras_dshow()
    return []


def _list_cameras_avfoundation() -> list[tuple[str, str]]:
    if not ffmpeg_available():
        return []
    result = subprocess.run(
        [CAMERA_FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    devices = []
    in_video_section = False
    for line in result.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices" in line:
            in_video_section = False
            continue
        if in_video_section:
            m = re.search(r"\[(\d+)\]\s+(.+)", line)
            if m:
                devices.append((m.group(1), m.group(2).strip()))
    return devices


def _list_cameras_v4l2() -> list[tuple[str, str]]:
    return [(str(dev), dev.name) for dev in sorted(Path("/dev").glob("video*"))]


def _list_cameras_dshow() -> list[tuple[str, str]]:
    if not ffmpeg_available():
        return []
    result = subprocess.run(
        [CAMERA_FFMPEG, "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
        capture_output=True, text=True,
    )
    devices = []
    in_video_section = False
    for line in result.stderr.splitlines():
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            in_video_section = False
            continue
        if in_video_section:
            m = re.search(r'"([^"]+)"', line)
            if m:
                devices.append((m.group(1), m.group(1)))
    return devices
