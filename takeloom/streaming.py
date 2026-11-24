"""Live streaming a session: the settings the node keeps for it.

The stream itself is made on the vault (vault_server/takeloom_vault/
live_stream.py), from the Takeloom Camera phones' live feeds that already
go there; a recording node sends it only its live mix and what to show
(phone_camera/vault_stream.py). This is what's left here: where YouTube's
RTMP ingest is, and the quality presets the Streaming settings offer.
"""

from __future__ import annotations

YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"


def youtube_rtmp_url(stream_key: str) -> str:
    return f"{YOUTUBE_RTMP_BASE}/{stream_key.strip()}"


# (label, video width in pixels — 0 means "don't scale, use the source
# resolution", target video bitrate in kbps). Bitrates follow YouTube's own
# recommended live-streaming ranges (https://support.google.com/youtube
# "Recommended upload encoding settings") for each resolution at up to 30fps.
# The UI's Streaming tab presents these labels directly; StudioConfig just
# stores the resolved width/bitrate, not the label, so re-ordering or
# retuning this list later never invalidates a saved config.
STREAM_QUALITY_PRESETS: list[tuple[str, int, int]] = [
    ("720p (3000 kbps)", 1280, 3000),
    ("1080p (4500 kbps)", 1920, 4500),
    ("1080p, high bitrate (6000 kbps)", 1920, 6000),
    ("Source resolution (8000 kbps)", 0, 8000),
]
DEFAULT_STREAM_WIDTH = STREAM_QUALITY_PRESETS[0][1]
DEFAULT_STREAM_BITRATE_KBPS = STREAM_QUALITY_PRESETS[0][2]
