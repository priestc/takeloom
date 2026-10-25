"""Checks whether a newer commit of takeloom is available on GitHub and, if
so, reinstalls via pipx and restarts the process so the new code takes
effect immediately.

Only meaningful for a non-editable, `pipx install git+https://...` install
(the laptop, per CLAUDE.md's Deployment section) — the Mac Mini's editable
install always reflects the current working copy already, so this is a
no-op there. Best-effort throughout: any failure (no network, pipx missing,
GitHub unreachable, etc.) is logged and swallowed rather than blocking
startup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from importlib import metadata
from pathlib import Path

_REPO_URL = "https://github.com/priestc/takeloom.git"
_GIT_TIMEOUT = 5.0
_INSTALL_TIMEOUT = 120.0


def _installed_commit() -> str | None:
    """The commit this install was built from, or None if that can't be
    determined — including the editable-install case, which has no
    meaningful "installed commit" to compare (see module docstring)."""
    try:
        direct_url = metadata.distribution("takeloom").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not direct_url:
        return None
    info = json.loads(direct_url)
    if info.get("dir_info", {}).get("editable"):
        return None
    vcs_info = info.get("vcs_info") or {}
    return vcs_info.get("commit_id")


def _latest_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", _REPO_URL, "main"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _pipx() -> str:
    """pipx's path. Launched from a desktop launcher (see launcher.py),
    PATH may not include where pipx lives."""
    found = shutil.which("pipx")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "pipx", Path("/opt/homebrew/bin/pipx"),
                      Path("/usr/local/bin/pipx"), Path("/usr/bin/pipx")):
        if candidate.exists():
            return str(candidate)
    return "pipx"


def _run_with_progress_window(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run cmd, showing a small "Updating Takeloom…" window meanwhile when
    there's no terminal to log to — opened from a desktop launcher, a
    minute-long reinstall would otherwise look like the app never opened."""
    if sys.stdout is not None and sys.stdout.isatty():
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT)
    try:
        import tkinter as tk
        window = tk.Tk(className="takeloom")
    except Exception:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT)
    window.title("Takeloom")
    tk.Label(window, text="Updating Takeloom to the latest version…", padx=30, pady=24).pack()
    window.resizable(False, False)
    outcome: dict = {}

    def worker() -> None:
        try:
            outcome["result"] = subprocess.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            outcome["error"] = e

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll() -> None:
        if thread.is_alive():
            window.after(200, poll)
        else:
            window.destroy()

    window.after(200, poll)
    window.mainloop()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def check_and_restart(log=print) -> None:
    """If GitHub's main branch is ahead of the installed commit, reinstall
    and re-exec this process in place. Returns normally (does nothing) if
    already current, not applicable (editable install), or the check/update
    itself fails for any reason."""
    installed = _installed_commit()
    if installed is None:
        return
    latest = _latest_commit()
    if latest is None:
        return
    if latest == installed:
        log(f"takeloom {metadata.version('takeloom')} ({installed[:8]}) — already on the latest version.")
        return

    log(f"Update available ({installed[:8]} -> {latest[:8]}) — installing...")
    try:
        result = _run_with_progress_window([_pipx(), "install", "--force", f"git+{_REPO_URL}"])
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"Update check: install failed ({e}), continuing with current version.")
        return
    if result.returncode != 0:
        log(f"Update check: install failed, continuing with current version.\n{result.stderr}")
        return

    log("Updated — restarting...")
    os.execv(sys.executable, [sys.executable, "-m", "takeloom"] + sys.argv[1:])
