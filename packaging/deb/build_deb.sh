#!/usr/bin/env bash
# Build takeloom's .deb from a built wheel:
#
#   packaging/deb/build_deb.sh dist/takeloom-X.Y.Z-py3-none-any.whl OUTPUT_DIR
#
# Runs on Debian/Ubuntu with sudo (GitHub's ubuntu runners), for the
# machine's own architecture (amd64 or arm64).
#
# Everything lives in /opt/takeloom: a standalone CPython build (from
# python-build-standalone, Tk included) with takeloom and its Python
# dependencies installed into it. So one .deb works on every Debian/Ubuntu
# release, whatever python3 each one ships. It's built at /opt/takeloom on
# the build machine too, so the absolute paths pip writes are already right.
# System libraries come from apt (Depends below), including ffmpeg: the
# launcher sets TAKELOOM_SYSTEM_FFMPEG=1 (see takeloom/ffmpeg_bin.py).
set -euo pipefail

WHEEL=$(realpath "$1")
OUT=$(realpath -m "$2")
HERE=$(cd "$(dirname "$0")" && pwd)

# Pinned standalone Python; bump deliberately, with its checksums from the
# release's SHA256SUMS.
PBS_RELEASE=20261003
PBS_PYTHON=3.12.15
declare -A PBS_SHA256=(
  [x86_64]=731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91
  [aarch64]=6541297dd1798dec8b98c3ad7492808a5b9d1c126801ceb2011e7754cd20d1ce
)

case "$(dpkg --print-architecture)" in
  amd64) DEB_ARCH=amd64; PBS_ARCH=x86_64 ;;
  arm64) DEB_ARCH=arm64; PBS_ARCH=aarch64 ;;
  *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac

# takeloom-0.5.2.dev3-py3-none-any.whl -> 0.5.2.dev3 -> Debian 0.5.2~dev3-1
# ("~" sorts before the release it leads up to, like PEP 440's .devN).
VERSION=$(basename "$WHEEL" | sed -E 's/^takeloom-([^-]+)-.*/\1/')
DEB_VERSION="$(echo "$VERSION" | sed -E 's/\.(dev|a|b|rc)/~\1/')-1"

PREFIX=/opt/takeloom
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "==> Standalone Python $PBS_PYTHON ($PBS_ARCH)"
tarball="cpython-$PBS_PYTHON+$PBS_RELEASE-$PBS_ARCH-unknown-linux-gnu-install_only_stripped.tar.gz"
curl -fsSL -o "$WORK/$tarball" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE/$tarball"
echo "${PBS_SHA256[$PBS_ARCH]}  $WORK/$tarball" | sha256sum -c -
sudo rm -rf "$PREFIX"
sudo mkdir -p "$PREFIX"
sudo chown "$(id -u):$(id -g)" "$PREFIX"
tar -xzf "$WORK/$tarball" -C "$PREFIX"  # -> $PREFIX/python
PY="$PREFIX/python/bin/python3"

echo "==> takeloom $VERSION and its dependencies"
"$PY" -m pip install --quiet --no-cache-dir --disable-pip-version-check "$WHEEL"
# Camera capture only, never OpenCV's own windows: the headless build
# leaves out Qt and its X libraries (tens of MB).
cv_version=$("$PY" -c "from importlib.metadata import version; print(version('opencv-python'))")
"$PY" -m pip uninstall --quiet --yes opencv-python
"$PY" -m pip install --quiet --no-cache-dir --disable-pip-version-check "opencv-python-headless==$cv_version"
# static-ffmpeg wrongly requires twine (a PyPI upload tool); nothing imports it.
"$PY" -m pip uninstall --quiet --yes twine readme-renderer nh3
find "$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "==> Package tree"
ROOT="$WORK/root"
mkdir -p "$ROOT/opt" "$ROOT/usr/bin" "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/512x512/apps" "$ROOT/usr/share/doc/takeloom" "$ROOT/DEBIAN"
cp -a "$PREFIX" "$ROOT/opt/"
cat > "$ROOT/usr/bin/takeloom" <<'SH'
#!/bin/sh
# takeloom's launcher (from the takeloom .deb): the bundled Python in
# /opt/takeloom, using Debian's ffmpeg instead of downloading its own.
export TAKELOOM_SYSTEM_FFMPEG=1
exec /opt/takeloom/python/bin/takeloom "$@"
SH
chmod 755 "$ROOT/usr/bin/takeloom"
icon="$PREFIX/python/lib/python3.12/site-packages/takeloom/data/icon.png"
cp "$icon" "$ROOT/usr/share/icons/hicolor/512x512/apps/takeloom.png"
cat > "$ROOT/usr/share/applications/takeloom.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Takeloom
Comment=Music recording session manager
Exec=/usr/bin/takeloom ui
Icon=takeloom
Terminal=false
Categories=AudioVideo;Audio;Recorder;
StartupWMClass=Takeloom
DESKTOP
cp "$HERE/copyright" "$ROOT/usr/share/doc/takeloom/copyright"

installed_kb=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: takeloom
Version: $DEB_VERSION
Architecture: $DEB_ARCH
Maintainer: Chris Priest <cp368202@ohiou.edu>
Installed-Size: $installed_kb
Depends: ffmpeg, libportaudio2, libsndfile1, libfluidsynth3, libhidapi-libusb0, libusb-1.0-0, libx11-6, libxft2, libxss1
Section: sound
Priority: optional
Homepage: https://github.com/priestc/takeloom
Description: Music recording session manager
 Records practice and performance sessions against a setlist of backing
 tracks, with live monitoring, per-take video from webcams or phones
 (Takeloom Camera), Stream Deck control, and a remote-control UI.
CONTROL

mkdir -p "$OUT"
deb="$OUT/takeloom_${DEB_VERSION}_${DEB_ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$deb"
ls -lh "$deb"
