"""Shared, persistent app-level state.

Holds this machine's Backend (there's no remote mode: every UI drives its
own machine's hardware; the vault's web UI is how a recording node is
controlled from elsewhere) plus a tiny pub/sub for app-wide changes.

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
        self.local_backend = LocalBackend()
        self.backend: Backend = self.local_backend  # the same one: tabs use either name
        self._recording_active: bool = False
        self._listeners: list[Listener] = []

    def start_local_monitoring(self) -> None:
        """Best-effort live monitoring of this machine's own hardware (see
        LocalBackend.start_monitoring()). Off the main thread so a slow/
        misbehaving audio device can't delay the window."""
        threading.Thread(target=self.local_backend.start_monitoring, daemon=True).start()
        # This machine records, so it's a recording node: the Takeloom
        # Camera phones on the vault are its cameras too (the laptop
        # recording drums). Nothing if there's no remote vault with them.
        threading.Thread(target=self.local_backend.connect_vault_cameras, daemon=True).start()

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

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                pass
