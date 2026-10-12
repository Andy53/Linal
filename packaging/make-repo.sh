#!/bin/bash
# Build a signed apt repository for linal the way a store/distro archive
# serves it: Packages + AppStream DEP-11 metadata (what GNOME Software / the
# PureOS store reads to list apps) + cached icons, signed with a repo key.
#
#   packaging/make-repo.sh <linal_*.deb> <repo-dir> [<gnupg-home>]
#
# Then point apt at it (see packaging/README.md) and install with PackageKit
# (GNOME Software's backend): pkcon refresh && pkcon install linal
set -euo pipefail
DEB=$(readlink -f "$1"); REPO=$2; GNUPGHOME=${3:-$HOME/.linal-repo-gnupg}
SRC=$(cd "$(dirname "$0")/.." && pwd)
SUITE=stable; COMP=main; ARCH=arm64
KEYNAME="Linal package repository"
export GNUPGHOME

mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"
if ! gpg --batch --list-secret-keys "$KEYNAME" >/dev/null 2>&1; then
    gpg --batch --pinentry-mode loopback --passphrase '' \
        --quick-gen-key "$KEYNAME" ed25519 sign 3y
fi

rm -rf "$REPO"
POOL="$REPO/pool/$COMP/s/linal"; DIST="$REPO/dists/$SUITE"
mkdir -p "$POOL" "$DIST/$COMP/binary-$ARCH" "$DIST/$COMP/dep11"
cp "$DEB" "$POOL/"

# --- apt package index
(cd "$REPO" && apt-ftparchive packages pool > "dists/$SUITE/$COMP/binary-$ARCH/Packages")
gzip -9kn "$DIST/$COMP/binary-$ARCH/Packages"

# --- AppStream DEP-11 metadata + cached icons, from the shipped metainfo
python3 - "$SRC/frontend/data/io.github.andy53.Linal.metainfo.xml" \
         "$SRC/frontend/data/icons/io.github.andy53.Linal.svg" "$DIST/$COMP/dep11" <<'PY'
import json, os, sys, tarfile, time, xml.etree.ElementTree as ET
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
meta, svg, out = sys.argv[1:]
root = ET.parse(meta).getroot()
q = json.dumps                       # JSON scalars are valid YAML
def text(tag): return (root.findtext(tag) or "").strip()
desc = root.find("description")
desc_html = "".join(ET.tostring(e, encoding="unicode", method="xml").strip() for e in desc)
desc_html = " ".join(desc_html.split())
pkg, cid = "linal", text("id")
icon = f"{pkg}_{cid}.png"
rel = root.find("releases/release")
ts = int(time.mktime(time.strptime(rel.get("date"), "%Y-%m-%d")))
y = ["---", "File: DEP-11", "Version: '0.16'", "Origin: linal", "---",
     f"Type: {root.get('type')}", f"ID: {cid}", f"Package: {pkg}",
     "Name:", f"  C: {q(text('name'))}", "Summary:", f"  C: {q(text('summary'))}",
     "Description:", f"  C: {q(desc_html)}",
     f"ProjectLicense: {q(text('project_license'))}",
     "DeveloperName:", f"  C: {q(root.findtext('developer/name') or '')}",
     "Categories:"] + [f"- {c.text}" for c in root.findall("categories/category")] + [
     "Keywords:", "  C:"] + [f"  - {q(k.text)}" for k in root.findall("keywords/keyword")] + [
     "Launchable:", "  desktop-id:", f"  - {root.findtext('launchable')}",
     "Provides:", "  binaries:"] + [f"  - {b.text}" for b in root.findall("provides/binary")] + [
     "Icon:", f"  stock: {cid}", "  cached:"]
for size in (48, 64, 128):
    y += [f"  - name: {icon}", f"    width: {size}", f"    height: {size}"]
y += ["ContentRating:", "  oars-1.1:"] + [
     f"    {a.get('id')}: {a.text}" for a in root.findall("content_rating/content_attribute")]
y += ["Releases:", f"- version: {rel.get('version')}", f"  unix-timestamp: {ts}", "  type: stable"]
open(os.path.join(out, "Components-arm64.yml"), "w").write("\n".join(y) + "\n")
for size in (48, 64, 128):
    png = f"/tmp/{icon}"
    GdkPixbuf.Pixbuf.new_from_file_at_size(svg, size, size).savev(png, "png", [], [])
    with tarfile.open(os.path.join(out, f"icons-{size}x{size}.tar"), "w") as t:
        t.add(png, arcname=icon)
    os.remove(png)
PY
# keep the uncompressed files too: apt only creates the DEP-11 index target
# when Release lists the base name (its MetaKey), as distro archives do
for f in "$DIST/$COMP/dep11"/*; do gzip -9kn "$f"; done

# --- Release (hashes of every index) + signatures
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="Linal" \
    -o APT::FTPArchive::Release::Label="Linal" \
    -o APT::FTPArchive::Release::Suite="$SUITE" \
    -o APT::FTPArchive::Release::Codename="$SUITE" \
    -o APT::FTPArchive::Release::Architectures="$ARCH" \
    -o APT::FTPArchive::Release::Components="$COMP" \
    -o APT::FTPArchive::Release::Description="Linal for the Librem 5" \
    release "$DIST" > "$DIST/Release.tmp"
mv "$DIST/Release.tmp" "$DIST/Release"
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$KEYNAME" --clearsign -o "$DIST/InRelease" "$DIST/Release"
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$KEYNAME" -abs -o "$DIST/Release.gpg" "$DIST/Release"
gpg --batch --export "$KEYNAME" > "$REPO/linal-archive-keyring.gpg"
echo "repository ready: $REPO (key: $REPO/linal-archive-keyring.gpg)"
