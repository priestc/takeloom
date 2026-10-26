"""Shared "is this configured device actually connected?" check, run at
startup by both the headless server (`takeloom server`) and the Tk UI so a
missing mic, camera, or Stream Deck is obvious immediately instead of only
surfacing later as a BackendError when a take starts. Devices left
unconfigured are skipped entirely — nothing to warn about there."""

from __future__ import annotations

from .backend import Backend


def _device_present(devices: list[dict], name: str) -> bool:
    if any(d.get("name") == name for d in devices):
        return True
    # Same partial-match fallback as audio.devices.resolve_device, so a
    # renamed-with-suffix device (e.g. macOS appending "(2)") doesn't false-
    # positive as missing.
    return any(name.lower() in (d.get("name") or "").lower() for d in devices)


def check_configured_devices(backend: Backend, include_streamdeck: bool = True) -> list[str]:
    """Return a warning message for every device configured in Recording
    Devices settings that isn't actually connected right now. `backend` is
    queried for its own audio/camera hardware (works for either LocalBackend
    or a connected RemoteBackend); the Stream Deck, when included, is always
    probed locally — it's expected to be physically attached to whichever
    machine is running this process, same as RecordingDeckDriver."""
    config = backend.get_config()
    warnings: list[str] = []

    if config.output_device or config.input_labels:
        try:
            audio_devices = backend.list_audio_devices()
        except Exception:
            audio_devices = []
        if not audio_devices:
            warnings.append("Audio: could not query audio devices.")
        else:
            if config.output_device and not _device_present(audio_devices, config.output_device):
                warnings.append(f"Audio: configured output device '{config.output_device}' not connected.")
            checked: set[str] = set()
            for il in config.input_labels:
                if il.device in checked:
                    continue
                checked.add(il.device)
                if not _device_present(audio_devices, il.device):
                    warnings.append(f"Audio: configured input device '{il.device}' (for '{il.label}') not connected.")

    if config.main_camera:
        try:
            inventory = backend.camera_inventory()
        except Exception:
            inventory = []
        main = next((c for c in inventory if c["key"] == config.main_camera), None)
        if main is not None and not main["connected"] and not main["hidden"]:
            warnings.append(f"Video: main camera '{main['label']}' not connected.")

    if include_streamdeck and config.streamdeck_id:
        from .streamdeck_controller import list_streamdecks
        decks = list_streamdecks()
        if not any(serial == config.streamdeck_id for serial, _label in decks):
            warnings.append(
                f"StreamDeck: configured device '{config.streamdeck_label or config.streamdeck_id}' not connected."
            )

    return warnings
