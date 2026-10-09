"""Runs `takeloom server` as a macOS launchd LaunchAgent — the `takeloom
service ...` commands (see __main__.py) — so the Mac Mini's server starts
at login and comes back on its own after a crash, instead of living in a
Terminal window.

A LaunchAgent rather than a LaunchDaemon on purpose: it runs inside the
logged-in user's GUI session, which is what the audio interface, the
Stream Deck (HID), macOS's per-binary camera/microphone permissions and
the pairing-approval dialog (server_command's request_authorization) all
need. A boot-time daemon has none of those.

launchd doesn't read the shell profile, so the PATH of whoever ran
`takeloom service install` is baked into the plist — that's how system
ffmpeg, rsync/ssh etc. stay findable (re-run install if that ever needs
changing). Output goes to LOG_PATH; server log lines carry their own
timestamps.

macOS only — the laptop never hosts a server (see CLAUDE.md)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.takeloom.server"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "takeloom-server.log"

# How long launchd waits after SIGTERM before SIGKILL. server_command's
# shutdown ends any active session and waits for its post-processing/sync
# (backend.join_session_processing), which can take minutes — the launchd
# default of 20 s would cut that off mid-write.
EXIT_TIMEOUT_SECONDS = 600


class ServiceError(Exception):
    pass


def _check_platform() -> None:
    if sys.platform != "darwin":
        raise ServiceError("takeloom service is macOS-only (it manages a launchd LaunchAgent).")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _program_arguments() -> list[str]:
    # The pipx shim on PATH (~/.local/bin/takeloom) rather than the venv's
    # resolved path, so a pipx reinstall doesn't strand the plist.
    exe = shutil.which("takeloom")
    if exe:
        return [exe, "server", "--disable-color"]
    return [sys.executable, "-m", "takeloom", "server", "--disable-color"]


def build_plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
        "ProcessType": "Interactive",  # audio work — don't get App Nap'd/throttled
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONUNBUFFERED": "1",  # log lines reach LOG_PATH as they happen
        },
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }


def is_installed() -> bool:
    return PLIST_PATH.exists()


def is_loaded() -> bool:
    return _launchctl("print", _target()).returncode == 0


def running_pid() -> int | None:
    result = _launchctl("print", _target())
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def install() -> None:
    """Write (or rewrite) the plist and (re)load it — starts the server now."""
    _check_platform()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if is_loaded():
        stop()
    PLIST_PATH.write_bytes(plistlib.dumps(build_plist()))
    start()


def uninstall() -> None:
    _check_platform()
    if is_loaded():
        stop()
    PLIST_PATH.unlink(missing_ok=True)


def start() -> None:
    _check_platform()
    if not is_installed():
        raise ServiceError("Not installed — run `takeloom service install` first.")
    if is_loaded():
        return
    result = _launchctl("bootstrap", _domain(), str(PLIST_PATH))
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")


def stop() -> None:
    """Unload the agent (with KeepAlive set, merely killing the process
    would just get it restarted). Blocks until the server has exited —
    up to EXIT_TIMEOUT_SECONDS if it's finishing a session."""
    _check_platform()
    if not is_loaded():
        return
    result = _launchctl("bootout", _target())
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootout failed: {(result.stderr or result.stdout).strip()}")


def restart() -> None:
    """Graceful restart — what picks up new code on the Mac Mini's
    editable install. stop() then start() rather than `launchctl
    kickstart -k`, so the old server gets the same SIGTERM +
    EXIT_TIMEOUT_SECONDS grace to finish a session."""
    stop()
    start()
