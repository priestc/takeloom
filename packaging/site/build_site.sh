#!/usr/bin/env bash
# Build the package repositories published on GitHub Pages
# (https://priestc.github.io/takeloom/) from one release's packages:
#
#   APT_SIGNING_KEY="$(armored private key)" packaging/site/build_site.sh SITE_DIR DEBS_DIR FLATPAKS_DIR
#
# - SITE_DIR/apt: a signed apt repository (suite "stable", component
#   "main", amd64 + arm64) of the .debs in DEBS_DIR;
# - SITE_DIR/flatpak: a signed Flatpak (OSTree) repository of the bundles
#   in FLATPAKS_DIR, plus takeloom.flatpakrepo / .flatpakref to add it;
# - SITE_DIR/index.html: how to install from either.
#
# Both are signed with the same key. Needs apt-utils, flatpak and ostree.
# See docs/releasing.md.
set -euo pipefail

SITE=$(realpath -m "$1")
DEBS=$(realpath "$2")
FLATPAKS=$(realpath "$3")
BASE="https://priestc.github.io/takeloom"
APP=io.github.priestc.Takeloom

export GNUPGHOME=$(mktemp -d)
trap 'rm -rf "$GNUPGHOME"' EXIT
echo "$APT_SIGNING_KEY" | gpg --batch --quiet --import
KEY=$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')
mkdir -p "$SITE"

# --- apt ---
APT="$SITE/apt"
mkdir -p "$APT/pool/main/t/takeloom"
cp "$DEBS"/*.deb "$APT/pool/main/t/takeloom/"
(
  cd "$APT"
  for arch in amd64 arm64; do
    dir="dists/stable/main/binary-$arch"
    mkdir -p "$dir"
    apt-ftparchive --arch "$arch" packages pool > "$dir/Packages"
    gzip -9 --keep --force "$dir/Packages"
  done
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=takeloom \
    -o APT::FTPArchive::Release::Label=takeloom \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o "APT::FTPArchive::Release::Architectures=amd64 arm64" \
    -o APT::FTPArchive::Release::Components=main \
    -o "APT::FTPArchive::Release::Description=takeloom releases" \
    release dists/stable > "$GNUPGHOME/Release"
  mv "$GNUPGHOME/Release" dists/stable/Release
  gpg --batch --yes --local-user "$KEY" --clearsign -o dists/stable/InRelease dists/stable/Release
  gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o dists/stable/Release.gpg dists/stable/Release
  # The public key, from the signing key itself so they can never disagree.
  gpg --batch --export "$KEY" > takeloom.gpg
  gpg --batch --armor --export "$KEY" > takeloom.asc
)

# --- Flatpak ---
FP="$SITE/flatpak"
mkdir -p "$FP"
ostree init --mode=archive-z2 --repo="$FP/repo"
for bundle in "$FLATPAKS"/*.flatpak; do
  flatpak build-import-bundle --gpg-sign="$KEY" --gpg-homedir="$GNUPGHOME" "$FP/repo" "$bundle"
done
# No --generate-static-deltas: they'd roughly double the repository's size,
# and GitHub Pages sites are capped at 1 GB (the .debs share it).
flatpak build-update-repo --gpg-sign="$KEY" --gpg-homedir="$GNUPGHOME" \
  --title="takeloom" --default-branch=stable --prune "$FP/repo"
du -sh "$SITE"/*
gpgkey=$(gpg --batch --export "$KEY" | base64 -w0)
cat > "$FP/takeloom.flatpakrepo" <<REPO
[Flatpak Repo]
Title=takeloom
Url=$BASE/flatpak/repo/
Homepage=https://github.com/priestc/takeloom
Comment=takeloom releases
GPGKey=$gpgkey
REPO
cat > "$FP/$APP.flatpakref" <<REF
[Flatpak Ref]
Name=$APP
Branch=stable
Title=Takeloom
Url=$BASE/flatpak/repo/
SuggestRemoteName=takeloom
Homepage=https://github.com/priestc/takeloom
IsRuntime=false
GPGKey=$gpgkey
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
REF

# --- index ---
cat > "$SITE/index.html" <<HTML
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>takeloom packages</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}
pre{background:#f4f4f4;padding:.75rem 1rem;overflow-x:auto}</style></head>
<body>
<h1>takeloom</h1>
<p>Music recording session manager — <a href="https://github.com/priestc/takeloom">github.com/priestc/takeloom</a>.</p>
<h2>Debian / Ubuntu (apt)</h2>
<pre>curl -fsSL $BASE/apt/takeloom.gpg | sudo tee /usr/share/keyrings/takeloom.gpg &gt; /dev/null
echo "deb [signed-by=/usr/share/keyrings/takeloom.gpg] $BASE/apt stable main" | sudo tee /etc/apt/sources.list.d/takeloom.list
sudo apt update
sudo apt install takeloom</pre>
<h2>Any Linux (Flatpak)</h2>
<pre>flatpak install --user $BASE/flatpak/$APP.flatpakref</pre>
<p>Or open <a href="flatpak/$APP.flatpakref">$APP.flatpakref</a> in your software center.
Updates come with <code>flatpak update</code>.</p>
<p>Both are amd64/x86_64 and arm64/aarch64. Signing key fingerprint: <code>$KEY</code></p>
</body></html>
HTML
echo "Built $SITE, signed with $KEY"
