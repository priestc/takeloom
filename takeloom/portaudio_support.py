"""Diagnosing (and, on Debian/Ubuntu, fixing) a missing PortAudio.

`sounddevice` needs the PortAudio C library. Its macOS/Windows wheels bundle
one, but on Linux it loads the system's, so a pip/pipx install there can't
provide it — the .deb gets it via `Depends: libportaudio2`, but a pipx
install has no way to. Without it `import sounddevice` raises OSError
("PortAudio library not found") and every device list comes back empty.

Everything here is about *this* machine: over a Remote connection the audio
devices (and any missing PortAudio) are the server's, not the UI's.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def sounddevice_error() -> str | None:
    """Why `import sounddevice` fails here, or None if it works."""
    try:
        import sounddevice  # noqa: F401
    except Exception as e:  # OSError for a missing PortAudio, ImportError if not installed
        return str(e) or type(e).__name__
    return None


def portaudio_missing() -> bool:
    error = sounddevice_error()
    return error is not None and "portaudio" in error.lower()


def install_command() -> str | None:
    """The command a user should run to install PortAudio here, if known."""
    if sys.platform == "darwin":
        return "brew install portaudio"
    if sys.platform.startswith("linux"):
        if shutil.which("apt-get"):
            return "sudo apt install libportaudio2"
        if shutil.which("dnf"):
            return "sudo dnf install portaudio"
        if shutil.which("pacman"):
            return "sudo pacman -S portaudio"
        if shutil.which("zypper"):
            return "sudo zypper install libportaudio2"
    return None


def describe_problem() -> str:
    """User-facing explanation of why audio devices can't be listed here."""
    error = sounddevice_error()
    if error is None:
        return "No audio devices found."
    if "portaudio" in error.lower():
        command = install_command()
        message = "Could not query audio devices: the PortAudio library isn't installed."
        return f"{message}\nInstall it with:  {command}" if command else message
    return f"Could not query audio devices (sounddevice unavailable: {error})."


def can_auto_install() -> bool:
    """Whether install_portaudio() can work: Debian/Ubuntu with a graphical
    sudo prompt (pkexec) available."""
    return (
        sys.platform.startswith("linux")
        and shutil.which("apt-get") is not None
        and shutil.which("pkexec") is not None
    )


def install_portaudio() -> tuple[bool, str]:
    """Install libportaudio2 via apt, authorized through pkexec's password
    prompt. Blocks until done — call it off the Tk thread. Returns
    (ok, message)."""
    try:
        proc = subprocess.run(
            ["pkexec", "apt-get", "install", "-y", "libportaudio2"],
            capture_output=True, text=True,
        )
    except OSError as e:
        return False, f"Could not run pkexec: {e}"
    if proc.returncode in (126, 127):  # pkexec: dismissed / not authorized
        return False, "Installation was cancelled."
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return False, "apt-get failed:\n" + "\n".join(detail[-8:])
    if portaudio_missing():
        return False, "libportaudio2 installed, but PortAudio still can't be loaded. Try restarting Takeloom."
    return True, "PortAudio installed."
