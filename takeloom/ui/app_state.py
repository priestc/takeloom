"""Shared, persistent app-level state.

Holds the active Backend (local or remote) plus a tiny pub/sub so tabs that
get rebuilt from scratch on every tab switch (Studio Setup, Recording
Devices — see app.py's `rebuild()`) can pick up the current backend on
construction, while the persistent Record/Remote tabs react to a backend
swap in place without being destroyed.

Created once in `app.py:run()` and passed into every tab constructor.
"""

from __future__ import annotations

import threading
from typing import Callable

from .. import sleep_guard
from ..backend import Backend, LocalBackend

Listener = Callable[[], None]


class AppState:
    def __init__(self) -> None:
        # The one true local backend for this machine — always exists, even
        # when `backend` below is swapped to a RemoteBackend.
        self.local_backend = LocalBackend()
        self.backend: Backend = self.local_backend
        self.remote_name: str = ""  # hostname of the connected remote; "" when backend is local
        self._recording_active: bool = False
        self._listeners: list[Listener] = []
        # Local live monitoring is deliberately NOT started here — see
        # start_local_monitoring(). This object is created before app.py
        # knows whether this launch is a remote-control terminal, and a
        # remote terminal (the laptop) must never touch its own audio/MIDI
        # hardware: it only sends commands; the server does all recording
        # and sound.

    def start_local_monitoring(self) -> None:
        """Best-effort live monitoring of this machine's own hardware (see
        LocalBackend.start_monitoring()) — only for when this machine is
        actually being used in local mode: a plain local launch, or an
        explicit Disconnect back to local. Off the main thread so a slow/
        misbehaving audio device can't delay the window."""
        threading.Thread(target=self.local_backend.start_monitoring, daemon=True).start()

    @property
    def recording_active(self) -> bool:
        return self._recording_active

    @recording_active.setter
    def recording_active(self, value: bool) -> None:
        if value == self._recording_active:
            return
        self._recording_active = value
        # Keeps the display from sleeping/locking mid-take; see sleep_guard.
        sleep_guard.set_active(value)

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def set_backend(self, backend: Backend, remote_name: str = "") -> None:
        """Swap the active backend (e.g. Connect/Disconnect in the Remote tab)
        and notify all listeners so persistent tabs can refresh in place."""
        old = self.backend
        self.backend = backend
        self.remote_name = remote_name
        if old is not backend:
            old.close()
        if backend.is_remote():
            # Remote mode only relays commands — release any local audio/
            # MIDI monitoring this machine had open from local mode.
            threading.Thread(target=self.local_backend.stop_monitoring, daemon=True).start()
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                pass
