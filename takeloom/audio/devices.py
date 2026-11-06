"""Audio device name resolution."""

from __future__ import annotations


def resolve_device(sd: object, device_name: str, kind: str) -> int | None:
    """Resolve a device name to its index. Returns None if not found."""
    if not device_name:
        return None
    devices = sd.query_devices()  # type: ignore[union-attr]
    for i, d in enumerate(devices):
        if d["name"] == device_name:
            return i
    # Partial match fallback
    for i, d in enumerate(devices):
        if device_name.lower() in d["name"].lower():
            return i
    return None


def match_device_rate_settings(sd: object) -> object | None:
    """extra_settings for any realtime stream on the studio's audio
    interface: on macOS, tell PortAudio to switch the device's own nominal
    sample rate to the stream's (config.sample_rate) instead of leaving
    the device wherever it happens to be and resampling.

    Without this, opening a 48 kHz stream on an interface that's sitting
    at 44.1 kHz (whatever the last app to touch it — a browser, Focusrite
    Control, an inspiration playback — left it at) puts a CoreAudio
    sample-rate converter on both the input and output side of the
    realtime callback. On a duplex stream that showed up as a periodic
    crackle every few seconds in a MIDI piano's live monitor, with no
    dropout ever reported. Returns None off macOS (no equivalent needed)."""
    import sys
    if sys.platform != "darwin":
        return None
    return sd.CoreAudioSettings(change_device_parameters=True)  # type: ignore[union-attr]
