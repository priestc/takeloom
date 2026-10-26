"""Development mode: following GitHub's `main` instead of releases.

With development mode on, `takeloom ui` checks on launch whether `main` has
moved past the installed commit and, if so, reinstalls from it via pipx and
restarts the process so the new code takes effect immediately. With it off,
nothing is checked at launch; takeloom updates however it was installed.

Only a pipx install can do this (install_kind() "git" or "pipx-release"):
a Homebrew/apt/App Store install is the package manager's to update, and
the Mac Mini's editable install always runs the working copy as-is, so
neither offers the setting (development_mode_available()). Turning the
setting on in a release install switches it to `main` on the next launch;
turning it off in a `main` install switches it back to the latest PyPI
release. StudioConfig.development_mode is None until set, which counts as
on for a `main` install, so existing ones (the laptop) keep updating.

Best-effort throughout: any failure (no network, pipx missing, GitHub
unreachable, etc.) is logged and swallowed rather than blocking startup.
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


def _direct_url() -> dict:
    try:
        raw = metadata.distribution("takeloom").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    return json.loads(raw) if raw else {}


def install_kind() -> str:
    """How this copy of takeloom was installed:

    "editable"      an editable install of a working copy (the Mac Mini);
    "git"           pipx from GitHub's main — development mode's install;
    "pipx-release"  pipx from PyPI (a release);
    "packaged"      anything else (Homebrew, apt, the App Store, plain pip).
    """
    info = _direct_url()
    if info.get("dir_info", {}).get("editable"):
        return "editable"
    if info.get("vcs_info"):
        return "git"
    if (Path(sys.prefix) / "pipx_metadata.json").exists():  # pipx writes this into every venv it manages
        return "pipx-release"
    return "packaged"


def development_mode_available() -> bool:
    return install_kind() in ("git", "pipx-release")


def development_mode_enabled(config) -> bool:
    if not development_mode_available():
        return False
    if config.development_mode is None:
        return install_kind() == "git"
    return config.development_mode


def _installed_commit() -> str | None:
    """The commit a "git" install was built from, else None."""
    return (_direct_url().get("vcs_info") or {}).get("commit_id")


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


def check_and_restart(config, log=print) -> None:
    """Act on development mode at launch (see the module docstring):
    reinstall and re-exec this process in place if this install should be
    following something newer or different, else return normally — also
    when the check or the install fails for any reason."""
    kind = install_kind()
    if kind not in ("git", "pipx-release"):
        return
    enabled = development_mode_enabled(config)
    if kind == "pipx-release":
        if not enabled:
            return
        log("Development mode is on — switching from the release to GitHub's main...")
        _reinstall(f"git+{_REPO_URL}", log)
        return
    if not enabled:
        log("Development mode is off — switching from GitHub's main to the latest release...")
        _reinstall("takeloom", log)
        return

    installed = _installed_commit()
    latest = _latest_commit()
    if installed is None or latest is None:
        return
    if latest == installed:
        log(f"takeloom {metadata.version('takeloom')} ({installed[:8]}) — already on the latest version.")
        return
    log(f"Update available ({installed[:8]} -> {latest[:8]}) — installing...")
    _reinstall(f"git+{_REPO_URL}", log)


def _reinstall(spec: str, log) -> None:
    """pipx-install `spec` over this install and restart into it. Returns
    only if that failed (the current version keeps running)."""
    try:
        result = _run_with_progress_window([_pipx(), "install", "--force", spec])
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"Update check: install failed ({e}), continuing with current version.")
        return
    if result.returncode != 0:
        log(f"Update check: install failed, continuing with current version.\n{result.stderr}")
        return
    log("Updated — restarting...")
    os.execv(sys.executable, [sys.executable, "-m", "takeloom"] + sys.argv[1:])
