"""Upload the iOS Takeloom Camera to TestFlight.

Run by `takeloom make-release --camera` after it tags a camera release, and
on its own by `takeloom camera-testflight` (e.g. to retry a failed upload).
Builds on this Mac with Xcode, signed with the Apple account Xcode is
logged into (Xcode > Settings > Accounts): `-allowProvisioningUpdates`
lets xcodebuild create the distribution certificate and App Store profile
itself, so there are no keys to manage. Steps:

1. regenerate the Xcode project from camera_app/ios/project.yml;
2. archive a Release build for devices;
3. export it with destination "upload", which sends it to App Store
   Connect. Apple processes it for 10-30 minutes, then it's in TestFlight,
   and the phones' TestFlight app offers the update (or installs it, with
   automatic updates on).

The build number is camera_release.version_code(camera_app/VERSION), so
each camera release can be uploaded once: App Store Connect refuses a
repeat. Needs the app's record in App Store Connect first (bundle ID
com.priestc.takeloomcamera) — see docs/takeloom-camera.md.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TEAM_ID = "6V798W6LMZ"  # also DEVELOPMENT_TEAM in camera_app/ios/project.yml
SCHEME = "TakeloomCamera"


class TestFlightError(Exception):
    pass


def _run(cmd: list[str], cwd: Path, log, what: str) -> None:
    """Run cmd, echoing only xcodebuild's errors and result lines (its full
    output is thousands of lines); the whole log goes in a file on failure."""
    log_file = Path(tempfile.gettempdir()) / f"takeloom-testflight-{what}.log"
    with open(log_file, "w") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            out.write(line)
            stripped = line.strip()
            if stripped.startswith(("error:", "** ")) or " error: " in stripped or "Upload succeeded" in stripped:
                log("  " + stripped)
        proc.wait()
    if proc.returncode != 0:
        raise TestFlightError(f"{what} failed (exit {proc.returncode}). Full log: {log_file}")


def upload(root: Path, log=print) -> None:
    if sys.platform != "darwin" or shutil.which("xcodebuild") is None:
        raise TestFlightError("TestFlight uploads need a Mac with Xcode.")
    if shutil.which("xcodegen") is None:
        raise TestFlightError("Needs XcodeGen: brew install xcodegen")
    ios = root / "camera_app" / "ios"
    version = (root / "camera_app" / "VERSION").read_text().strip()
    log(f"Uploading Takeloom Camera {version} (iOS) to TestFlight…")
    subprocess.run(["xcodegen", "generate", "--quiet"], cwd=ios, check=True)

    with tempfile.TemporaryDirectory(prefix="takeloom-testflight-") as tmp:
        archive = Path(tmp) / f"{SCHEME}.xcarchive"
        log("  Archiving (a few minutes)…")
        _run(["xcodebuild", "archive", "-project", f"{SCHEME}.xcodeproj", "-scheme", SCHEME,
              "-configuration", "Release", "-destination", "generic/platform=iOS",
              "-archivePath", str(archive), "-allowProvisioningUpdates", f"DEVELOPMENT_TEAM={TEAM_ID}"],
             ios, log, "archive")

        options = Path(tmp) / "ExportOptions.plist"
        options.write_bytes(plistlib.dumps({
            "method": "app-store-connect",
            "destination": "upload",
            "teamID": TEAM_ID,
            "signingStyle": "automatic",
            # The build number is ours (CURRENT_PROJECT_VERSION), not Xcode's.
            "manageAppVersionAndBuildNumber": False,
            "uploadSymbols": True,
        }))
        log("  Uploading to App Store Connect…")
        _run(["xcodebuild", "-exportArchive", "-archivePath", str(archive),
              "-exportOptionsPlist", str(options), "-exportPath", str(Path(tmp) / "export"),
              "-allowProvisioningUpdates"],
             ios, log, "upload")
    log(f"Uploaded Takeloom Camera {version}. Apple processes it for 10-30 minutes; then the "
        "TestFlight app on each phone offers the update.")
