#!/usr/bin/env bash
# Turn a directory of takeloom .debs into a signed apt repository:
#
#   APT_SIGNING_KEY="$(armored private key)" packaging/apt/build_repo.sh SITE_DIR
#
# expecting SITE_DIR/apt/pool/main/t/takeloom/*.deb, and writing
# SITE_DIR/apt/dists/stable/... plus the public key (takeloom.gpg/.asc)
# and an index page. One suite, "stable", component "main", amd64 + arm64.
# Needs apt-utils (apt-ftparchive) and gpg. See docs/releasing.md.
set -euo pipefail

SITE=$(realpath "$1")
REPO="$SITE/apt"
SUITE=stable
ARCHES="amd64 arm64"

export GNUPGHOME=$(mktemp -d)
trap 'rm -rf "$GNUPGHOME"' EXIT
echo "$APT_SIGNING_KEY" | gpg --batch --quiet --import
KEY=$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')

cd "$REPO"
for arch in $ARCHES; do
  dir="dists/$SUITE/main/binary-$arch"
  mkdir -p "$dir"
  apt-ftparchive --arch "$arch" packages pool > "$dir/Packages"
  gzip -9 --keep --force "$dir/Packages"
done
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=takeloom \
  -o APT::FTPArchive::Release::Label=takeloom \
  -o APT::FTPArchive::Release::Suite=$SUITE \
  -o APT::FTPArchive::Release::Codename=$SUITE \
  -o "APT::FTPArchive::Release::Architectures=$ARCHES" \
  -o APT::FTPArchive::Release::Components=main \
  -o "APT::FTPArchive::Release::Description=takeloom releases" \
  release "dists/$SUITE" > "$GNUPGHOME/Release"
mv "$GNUPGHOME/Release" "dists/$SUITE/Release"
gpg --batch --yes --local-user "$KEY" --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"

# The public key, from the signing key itself so they can never disagree.
gpg --batch --export "$KEY" > takeloom.gpg
gpg --batch --armor --export "$KEY" > takeloom.asc

base="https://priestc.github.io/takeloom/apt"
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
<pre>curl -fsSL $base/takeloom.gpg | sudo tee /usr/share/keyrings/takeloom.gpg &gt; /dev/null
echo "deb [signed-by=/usr/share/keyrings/takeloom.gpg] $base stable main" | sudo tee /etc/apt/sources.list.d/takeloom.list
sudo apt update
sudo apt install takeloom</pre>
<p>amd64 and arm64. Signing key fingerprint: <code>$KEY</code></p>
</body></html>
HTML
echo "Signed $REPO with $KEY"
