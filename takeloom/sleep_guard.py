"""Keeps the *UI's own* display awake while a recording (or video check) is
in progress, so a long take doesn't get cut short by the screen locking or
the system suspending mid-recording.

Driven entirely by the Tk UI's `AppState.recording_active` — see its
setter. Deliberately not wired up anywhere headless (`takeloom server`,
the CLI's `start-session`/`inspiration` commands): a machine with nobody
watching its screen has no reason to hold its own screensaver/sleep off
just because a recording happens to be running on it — only a UI actually
being looked at does. That includes a Remote client just watching a
session recording on another machine, not only a local one — see
AppState.recording_active's docstring and record.py's
_update_recording_active, which both apply regardless of whether
app_state.backend is local or remote.

On Linux under GNOME (Ubuntu's desktop, X11 or Wayland), screen blanking,
locking and suspend are held off by gnome-session-inhibit (--inhibit idle
--inhibit suspend), GNOME's own tool: it asks the session manager to
inhibit for as long as it runs, holding the D-Bus connection that the
inhibit lives on — which is what a one-shot `gdbus call` to the same
interfaces can't do (the inhibit dies with the call), and likely why the
earlier D-Bus attempts below seemed to be ignored. On the Framework
laptop (Ubuntu, GNOME on Wayland, xdotool not installed) it was the only
thing actually keeping the screen on during a session.

Elsewhere on Linux (no gnome-session-inhibit), screen-blanking is held off
by simulating a harmless keypress
(Shift, via xdotool) every _KEYPRESS_INTERVAL seconds — see
_linux_keypress_loop. This replaced two earlier attempts (xdg-screensaver's
suspend/resume, then org.freedesktop.ScreenSaver's Inhibit/UnInhibit
D-Bus calls): both target the desktop's own idle timer through a protocol
the desktop has to actually implement and answer, and in practice neither
got answered on a real Ubuntu laptop this was tested against — still
blanked with either "held". Simulated real input sidesteps the whole
protocol question: as far as the desktop's idle timer is concerned, it's
indistinguishable from an actual person pressing a key, so there's no
protocol for a given desktop/session type to fail to implement.
systemd-inhibit (see _spawn_inhibitor) still separately holds off actual
system suspend/lid-close, which is a logind-level concern the keypress
loop doesn't touch.
"""

from __future__ import annotations

import atexit
import ctypes
import subprocess
import sys
import threading

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_KEYPRESS_INTERVAL = 10.0  # seconds

_processes: list[subprocess.Popen] = []
_keypress_thread: threading.Thread | None = None
_keypress_stop: threading.Event | None = None


def _gnome_inhibit_available() -> bool:
    import os
    import shutil
    return bool(shutil.which("gnome-session-inhibit")) and bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR"))


def _spawn_inhibitors() -> list[subprocess.Popen]:
    if sys.platform == "darwin":
        return [subprocess.Popen(["caffeinate", "-d", "-i"])]
    if not sys.platform.startswith("linux"):
        return []
    started = []
    if _gnome_inhibit_available():
        started.append(subprocess.Popen(
            ["gnome-session-inhibit", "--app-id=takeloom", "--reason=Recording in progress",
             "--inhibit=idle", "--inhibit=suspend", "--inhibit-only"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    started.append(subprocess.Popen(
        [
            "systemd-inhibit",
            "--what=idle:sleep:handle-lid-switch",
            "--who=takeloom",
            "--why=Recording in progress",
            "sleep", "infinity",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
    return started


def _linux_keypress_loop(stop_event: threading.Event) -> None:
    """Press Shift (via xdotool) every _KEYPRESS_INTERVAL seconds until
    stop_event is set — real synthetic input, indistinguishable to the
    desktop's idle timer from an actual keypress, so it resets whatever
    screen-blank countdown is running regardless of desktop environment or
    X11-vs-Wayland session type (see module docstring). A bare modifier
    key has no effect if it lands in a focused text field or terminal,
    unlike a printable key. Exits for good the first time xdotool itself
    is missing or errors, rather than retrying it every interval forever."""
    while not stop_event.wait(_KEYPRESS_INTERVAL):
        try:
            subprocess.run(
                ["xdotool", "key", "--clearmodifiers", "shift"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return


def _set_linux_keypress_loop_active(active: bool) -> None:
    global _keypress_thread, _keypress_stop
    if active:
        if _keypress_thread is not None and _keypress_thread.is_alive():
            return
        _keypress_stop = threading.Event()
        _keypress_thread = threading.Thread(
            target=_linux_keypress_loop, args=(_keypress_stop,), daemon=True,
        )
        _keypress_thread.start()
    else:
        if _keypress_stop is not None:
            _keypress_stop.set()
        _keypress_thread = None
        _keypress_stop = None


def set_active(active: bool) -> None:
    """Prevent (True) or re-allow (False) display/system sleep. Idempotent."""
    global _processes

    if sys.platform.startswith("win"):
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED if active else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        return

    if sys.platform.startswith("linux") and not _gnome_inhibit_available():
        _set_linux_keypress_loop_active(active)

    if active:
        if _processes and all(p.poll() is None for p in _processes):
            return  # already running
        for p in _processes:
            p.terminate()
        try:
            _processes = _spawn_inhibitors()
        except OSError:
            _processes = []
    else:
        for p in _processes:
            p.terminate()
        _processes = []
    if sys.platform.startswith("linux") and not active:
        _set_linux_keypress_loop_active(False)


@atexit.register
def _cleanup() -> None:
    set_active(False)
