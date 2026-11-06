"""`takeloom camera-android`: build the Android Takeloom Camera from this
checkout's camera_app/android and install it on the phone connected over adb
(Gradle's installDebug, an in-place update that keeps the app's data).

Neither Java nor adb has to be on PATH: Gradle gets Android Studio's bundled
JDK when JAVA_HOME isn't set, and adb comes from the Android SDK
(ANDROID_HOME, or the SDK path in camera_app/android/local.properties, or
Android Studio's default location).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

PACKAGE = "com.priestc.takeloomcamera"

JDK_CANDIDATES = [
    Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
    Path.home() / "Applications/Android Studio.app/Contents/jbr/Contents/Home",
    Path("/opt/android-studio/jbr"),
    Path.home() / "android-studio/jbr",
]


class AndroidInstallError(Exception):
    pass


def _java_home() -> str | None:
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    for candidate in JDK_CANDIDATES:
        if (candidate / "bin" / "java").exists():
            return str(candidate)
    return None


def _sdk_dir(android_dir: Path) -> Path | None:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            return Path(os.environ[var])
    props = android_dir / "local.properties"
    if props.exists():
        m = re.search(r"^sdk\.dir=(.+)$", props.read_text(), re.MULTILINE)
        if m:
            return Path(m.group(1).strip().replace("\\:", ":"))
    for default in (Path.home() / "Library/Android/sdk", Path.home() / "Android/Sdk"):
        if default.exists():
            return default
    return None


def _adb(android_dir: Path) -> str:
    sdk = _sdk_dir(android_dir)
    if sdk and (sdk / "platform-tools" / "adb").exists():
        return str(sdk / "platform-tools" / "adb")
    found = shutil.which("adb")
    if found:
        return found
    raise AndroidInstallError("adb not found: install the Android SDK platform-tools (Android Studio's SDK Manager).")


def _devices(adb: str) -> list[tuple[str, str]]:
    out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    rows = [line.split("\t") for line in out.splitlines()[1:] if "\t" in line]
    return [(serial, state) for serial, state in rows]


def installed_version(adb: str, serial: str) -> str | None:
    out = subprocess.run([adb, "-s", serial, "shell", "dumpsys", "package", PACKAGE],
                         capture_output=True, text=True).stdout
    m = re.search(r"versionName=(\S+)", out)
    return m.group(1) if m else None


def install(root: Path, log=print) -> None:
    android_dir = root / "camera_app" / "android"
    adb = _adb(android_dir)

    devices = _devices(adb)
    ready = [serial for serial, state in devices if state == "device"]
    if not ready:
        if any(state == "unauthorized" for _, state in devices):
            raise AndroidInstallError("The phone hasn't authorized this computer: unlock it and accept "
                                      "the \"Allow USB debugging?\" prompt, then try again.")
        raise AndroidInstallError("No Android phone connected over adb. Plug it in with USB debugging "
                                  "enabled (check with `adb devices`).")
    if len(ready) > 1:
        raise AndroidInstallError(f"More than one phone connected ({', '.join(ready)}); "
                                  "unplug all but one.")
    serial = ready[0]

    version = (root / "camera_app" / "VERSION").read_text().strip()
    before = installed_version(adb, serial)
    log(f"Phone {serial}: {'Takeloom Camera ' + before if before else 'Takeloom Camera not installed'}; "
        f"installing {version} from {android_dir}")

    env = dict(os.environ, ANDROID_SERIAL=serial)
    java_home = _java_home()
    if java_home:
        env["JAVA_HOME"] = java_home
    elif shutil.which("java") is None:
        raise AndroidInstallError("No Java found: install Android Studio (its bundled JDK is used) "
                                  "or set JAVA_HOME.")

    result = subprocess.run(["./gradlew", "installDebug"], cwd=android_dir, env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-30:])
        hint = ""
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in tail:
            hint = (f"\nThe installed app was signed with a different key. Uninstall it first "
                    f"(`adb uninstall {PACKAGE}`, which deletes the app's data on the phone) "
                    "and run this again.")
        raise AndroidInstallError(f"Gradle installDebug failed:\n{tail}{hint}")

    after = installed_version(adb, serial)
    log(f"Installed Takeloom Camera {after or '?'} on {serial}.")
