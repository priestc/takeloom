"""Hardware watcher for the always-running `takeloom server`.

The studio's recording hardware — the audio interface, webcam, and Stream
Deck — all hang off one switched USB hub that's only powered on for a
session. The server itself stays up permanently, so turning the hub on and
off is the whole "start/stop the studio" gesture. This polls for that
hardware every couple of seconds and brings the recording environment up
or down to match:

- Audio interface appears (every audio device the config refers to is in
  CoreAudio's live device list — see audio/coreaudio_devices.py for why
  PortAudio can't answer this): re-initialize PortAudio and open ambient
  monitoring (which also sets the Scarlett's direct monitor) — see
  LocalBackend.set_audio_hardware_present. Waits for two consecutive
  sightings first, since a just-powered interface shows up in CoreAudio a
  moment before it's actually ready to stream.
- Audio interface disappears: end any still-open session (kept and
  post-processed as usual), cancel an in-progress scan, close monitoring.
- Stream Deck appears: connect it and paint the idle Start buttons.
- Stream Deck disappears: release its handle (RecordingDeckDriver.
  release_deck), ready to reconnect cleanly next time.
- Webcam appears: restart the camera preview, in case a Remote client was
  already watching it when the camera was still off.

Every transition is logged once; nothing is logged on a tick where nothing
changed, so an idle server's console stays quiet for days.
"""

from __future__ import annotations

import threading
from typing import Callable

from .backend import LocalBackend
from .device_check import _device_present
from .recording_driver import RecordingDeckDriver

POLL_INTERVAL_SECONDS = 2.0
# Consecutive polls the audio interface must be seen on before it's used.
_AUDIO_SETTLE_POLLS = 2
# Polls to keep retrying ambient monitoring after the interface appeared,
# if the first attempt didn't open (device still settling).
_MONITOR_RETRY_POLLS = 5


class RigWatcher:
    def __init__(
        self,
        backend: LocalBackend,
        driver: RecordingDeckDriver,
        log: Callable[..., None],
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._backend = backend
        self._driver = driver
        self._log = log
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._audio_present: bool | None = None  # None until the first poll decides
        self._audio_seen_polls = 0
        self._monitor_retries_left = 0
        self._camera_present = False
        self._deck_error_logged: str | None = None

    def start(self) -> None:
        self._poll()  # first poll inline, so startup state is logged before "listening…"
        self._thread = threading.Thread(target=self._run, daemon=True, name="rig-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            self._poll()

    def _poll(self) -> None:
        try:
            config = self._backend.get_config()
        except Exception as e:
            self._log(f"Device watcher: could not read config: {e}", err=True)
            return
        for step in (self._poll_audio, self._poll_streamdeck, self._poll_camera):
            try:
                step(config)
            except Exception as e:  # never let one bad poll kill the watcher thread
                self._log(f"Device watcher error ({step.__name__}): {e}", err=True)

    # --- audio interface ---

    @staticmethod
    def _required_audio_devices(config) -> list[str]:
        names = [config.output_device] + [il.device for il in config.input_labels]
        return list(dict.fromkeys(n for n in names if n))

    def _poll_audio(self, config) -> None:
        from .audio.coreaudio_devices import list_device_names

        required = self._required_audio_devices(config)
        live = list_device_names()
        if live is None:
            # Can't watch on this platform — behave like the old one-shot
            # startup: assume present, try once.
            present = True
        else:
            present = all(_device_present([{"name": n} for n in live], name) for name in required)

        if not present:
            self._audio_seen_polls = 0
            if self._audio_present is not False:
                was_on = self._audio_present is True
                self._audio_present = False
                self._monitor_retries_left = 0
                if was_on:
                    self._log("Audio interface turned off — closing monitoring.")
                    if self._backend.is_session_active():
                        self._log("Ending the open session...")
                else:
                    self._log(f"Waiting for audio interface ({', '.join(required) or 'none configured'})...")
                self._backend.set_audio_hardware_present(False)
                self._driver.streamdeck.notify("Waiting for audio interface…", revert_after=30.0)
            return

        if self._audio_present is True:
            if self._monitor_retries_left > 0:
                self._monitor_retries_left -= 1
                if self._backend.start_monitoring():
                    self._monitor_retries_left = 0
                    self._log_monitoring(config)
            return

        self._audio_seen_polls += 1
        if live is not None and self._audio_seen_polls < _AUDIO_SETTLE_POLLS:
            return
        self._audio_present = True
        self._log(f"Audio interface connected ({', '.join(required) or 'default devices'}).")
        if self._backend.set_audio_hardware_present(True):
            self._log_monitoring(config)
        else:
            self._monitor_retries_left = _MONITOR_RETRY_POLLS
        self._driver.streamdeck.notify("Ready", revert_after=2.0)

    def _log_monitoring(self, config) -> None:
        self._log(f"Live-monitoring {self._backend.monitoring_description()}.")

    # --- Stream Deck ---

    def _poll_streamdeck(self, config) -> None:
        from .streamdeck_controller import any_streamdeck_attached

        deck = self._driver.streamdeck
        if deck.connected:
            if not deck.still_attached():
                self._driver.release_deck()
                self._log("StreamDeck turned off.")
            return
        if not config.streamdeck_id or not any_streamdeck_attached():
            return
        if self._driver.connect():
            self._deck_error_logged = None
            self._log("StreamDeck connected.")
            if not self._audio_present:
                deck.notify("Waiting for audio interface…", revert_after=30.0)
        elif deck.last_error and deck.last_error != self._deck_error_logged:
            # Logged once per distinct failure, not every poll.
            self._deck_error_logged = deck.last_error
            self._log(f"StreamDeck: found a device but could not connect — {deck.last_error}", err=True)

    # --- webcam ---

    def _poll_camera(self, config) -> None:
        if not config.camera_device:
            return
        if not self._audio_present:
            # Only look while the rig is on — listing cameras shells out to
            # ffmpeg, not something to do every 2s forever on an idle server.
            # Camera and interface share the hub's power switch.
            self._camera_present = False
            return
        if self._camera_present:
            return
        if any(device_id == config.camera_device for device_id, _label in self._backend.list_cameras()):
            self._camera_present = True
            self._log(f"Camera connected ({config.camera_label or config.camera_device}).")
            self._backend.refresh_camera_preview()
