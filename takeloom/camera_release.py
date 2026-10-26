"""`takeloom make-release --camera`: release the Takeloom Camera apps.

The camera apps (camera_app/ios, camera_app/android) have their own version
number, camera_app/VERSION, which only moves when the apps change: a camera
release refuses to run if nothing under camera_app/ changed since the last
one. Unlike a takeloom release (a GitHub Actions workflow that builds and
publishes packages), this runs in the working copy, since the apps are
built locally with Xcode and Android Studio:

1. bump camera_app/VERSION;
2. write it into camera_app/ios/project.yml, the XcodeGen spec the Xcode
   project is generated from (MARKETING_VERSION/CURRENT_PROJECT_VERSION),
   and regenerate the project if xcodegen is installed. Android's build
   reads VERSION itself;
3. commit, tag camera-vX.Y.Z, push both;
4. make a GitHub release for the tag, never marked "latest" (that's
   takeloom's own).

Camera tags never affect takeloom's version, which only counts v* tags.
takeloom ships camera_app/VERSION (see phone_camera/app_version.py), so a
phone running an older release than that gets flagged in the Record tab.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .phone_camera.app_version import parse_version

TAG_PREFIX = "camera-v"


class CameraReleaseError(Exception):
    pass


def checkout_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists() or not (root / "camera_app" / "VERSION").exists():
        raise CameraReleaseError(
            "Camera releases run from a git checkout of takeloom (an editable install), "
            "since they commit and tag camera_app/VERSION."
        )
    return root


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise CameraReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def bumped(version: str, bump: str) -> str:
    parts = parse_version(version)
    if parts is None:
        raise CameraReleaseError(f"camera_app/VERSION isn't a version number: {version!r}")
    major, minor, patch = parts
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[bump]


def version_code(version: str) -> int:
    """The ever-increasing integer build number both stores want: 1.2.3 ->
    10203. Same formula as camera_app/android/app/build.gradle.kts."""
    major, minor, patch = parse_version(version)
    return major * 10000 + minor * 100 + patch


def write_version(root: Path, version: str) -> list[Path]:
    """camera_app/VERSION plus the iOS project spec. Returns the (tracked)
    files written."""
    version_file = root / "camera_app" / "VERSION"
    version_file.write_text(version + "\n")
    spec = root / "camera_app" / "ios" / "project.yml"
    text = spec.read_text()
    text, n_marketing = re.subn(r'MARKETING_VERSION: "[^"]*"', f'MARKETING_VERSION: "{version}"', text)
    text, n_build = re.subn(r'CURRENT_PROJECT_VERSION: "[^"]*"',
                            f'CURRENT_PROJECT_VERSION: "{version_code(version)}"', text)
    if not n_marketing or not n_build:
        raise CameraReleaseError(f"Couldn't find the version settings in {spec}.")
    spec.write_text(text)
    # The generated Xcode project is gitignored: refresh the local one.
    if shutil.which("xcodegen"):
        subprocess.run(["xcodegen", "generate", "--quiet"], cwd=spec.parent, check=False)
    return [version_file, spec]


def make_camera_release(bump: str, log=print, confirm=lambda question: True, root: Path | None = None) -> str:
    """Release the camera apps (see the module docstring). Returns the new
    version. `root`: the checkout to release from (default: this one)."""
    root = root or checkout_root()
    if _git(root, "status", "--porcelain", "--", "camera_app"):
        raise CameraReleaseError("camera_app/ has uncommitted changes: commit them first.")
    # Just main and the camera tags: a stale daily-* tag would make a plain
    # --tags fetch fail ("would clobber existing tag").
    _git(root, "fetch", "--quiet", "origin", "main", f"+refs/tags/{TAG_PREFIX}*:refs/tags/{TAG_PREFIX}*")
    if _git(root, "rev-list", "--count", "HEAD..origin/main") != "0":
        raise CameraReleaseError("This checkout is behind GitHub's main: pull first.")

    previous_tag = _git(root, "describe", "--tags", "--abbrev=0", "--match", f"{TAG_PREFIX}[0-9]*",
                        check=False) or None
    if previous_tag:
        changed = _git(root, "diff", "--name-only", previous_tag, "HEAD", "--", "camera_app")
        if not changed:
            raise CameraReleaseError(
                f"Nothing in camera_app/ changed since {previous_tag}, so there's nothing to release."
            )

    current = (root / "camera_app" / "VERSION").read_text().strip()
    version = bumped(current, bump)
    tag = f"{TAG_PREFIX}{version}"
    log(f"Takeloom Camera {current} -> {version}"
        + (f" (changes since {previous_tag})" if previous_tag else " (first camera release)"))
    if not confirm(f"Commit, tag {tag} and push?"):
        raise CameraReleaseError("Cancelled.")

    files = write_version(root, version)
    _git(root, "add", *[str(f) for f in files])
    _git(root, "commit", "--quiet", "-m", f"Takeloom Camera {version}")
    _git(root, "tag", "-a", tag, "-m", f"Takeloom Camera {version}")
    _git(root, "push", "--quiet", "origin", "HEAD:main")
    _git(root, "push", "--quiet", "origin", tag)
    log(f"Tagged and pushed {tag}.")

    if shutil.which("gh"):
        notes_range = f"{previous_tag}..{tag}" if previous_tag else tag
        commits = _git(root, "log", "--format=- %s (%h)", notes_range, "--", "camera_app")
        notes = f"### Changes{f' since {previous_tag}' if previous_tag else ''}\n{commits}\n"
        result = subprocess.run(
            ["gh", "release", "create", tag, "--title", f"Takeloom Camera {version}",
             "--notes", notes, "--latest=false", "-R", "priestc/takeloom"],
            capture_output=True, text=True, cwd=root,
        )
        if result.returncode == 0:
            log(f"GitHub release: {result.stdout.strip()}")
        else:
            log(f"(Couldn't create the GitHub release: {result.stderr.strip()})")
    log("Build and install the apps from Xcode / Android Studio to ship it. takeloom's next "
        "release carries the new version, so older phones get flagged.")
    return version
