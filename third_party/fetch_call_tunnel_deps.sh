#!/bin/bash
# Gather everything needed to build signal-call-tunnel OFFLINE on the Librem 5
# (aarch64), so the phone's build doesn't spend mobile data. Runs on the laptop.
#
# Produces third_party/call-tunnel-bundle/ containing:
#   rust-<ver>-aarch64-unknown-linux-gnu.tar.xz   standalone Rust installer (checksummed)
#   webrtc-<ver>-linux-arm64-release.tar.bz2      RingRTC's prebuilt WebRTC (checksummed by ringrtc)
#   src.tar.gz                                    tunnel source + ringrtc + vendored crates
set -euo pipefail

TP="$(cd "$(dirname "$0")" && pwd)"
SRC="$TP/hara-seihun_signal-call-tunnel"
OUT="$TP/call-tunnel-bundle"
RUSTVER="$(cat "$SRC/third-party/ringrtc/rust-toolchain")"
WEBRTCVER="$(sed -n 's/^webrtc\.version=//p' "$SRC/third-party/ringrtc/config/version.properties")"
mkdir -p "$OUT"

fetch() {  # url dest
    python3 - "$1" "$2" <<'PY'
import sys, urllib.request, shutil
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2] + ".part", "wb") as f:
    shutil.copyfileobj(r, f, 1 << 20)
import os; os.replace(sys.argv[2] + ".part", sys.argv[2])
PY
}

# 1. host toolchain (laptop, x86_64), only to run `cargo vendor`
if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
    echo "== installing rustup (user-local, laptop)"
    fetch https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init /tmp/rustup-init.$$
    fetch https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256 /tmp/rustup-init.$$.sha256
    want=$(awk '{print $1}' /tmp/rustup-init.$$.sha256)
    echo "$want  /tmp/rustup-init.$$" | sha256sum -c -
    chmod +x /tmp/rustup-init.$$
    /tmp/rustup-init.$$ -y --profile minimal --default-toolchain "$RUSTVER" --no-modify-path
    rm -f /tmp/rustup-init.$$ /tmp/rustup-init.$$.sha256
fi
export PATH="$HOME/.cargo/bin:$PATH"

# 2. aarch64 standalone Rust for the phone
RT="rust-$RUSTVER-aarch64-unknown-linux-gnu.tar.xz"
if [ ! -f "$OUT/$RT" ]; then
    echo "== downloading $RT"
    fetch "https://static.rust-lang.org/dist/$RT" "$OUT/$RT"
    fetch "https://static.rust-lang.org/dist/$RT.sha256" "$OUT/$RT.sha256"
fi
(cd "$OUT" && echo "$(awk '{print $1}' "$RT.sha256")  $RT" | sha256sum -c -)

# 3. prebuilt WebRTC for linux-arm64 (fetch-artifact.py re-verifies the checksum)
WA="webrtc-$WEBRTCVER-linux-arm64-release.tar.bz2"
if [ ! -f "$OUT/$WA" ]; then
    echo "== downloading $WA"
    fetch "https://build-artifacts.signal.org/libraries/$WA" "$OUT/$WA"
fi
want=$(python3 -c "import json;print(json.load(open('$SRC/third-party/ringrtc/config/webrtc_artifact_checksums.json'))['linux-arm64'])")
echo "$want  $OUT/$WA" | sha256sum -c -

# 4. vendor every crate (incl. git deps) so the phone builds with --offline
echo "== cargo vendor"
cd "$SRC/signal-call-tunnel"
mkdir -p .cargo
cargo vendor --locked --versioned-dirs "$SRC/vendor" > .cargo/config.toml
sed -i "s#$SRC/vendor#../vendor#" .cargo/config.toml

# 5. apply the RingRTC patches here (needs git), so the phone can run plain
#    `cargo build --offline` without the bundle carrying .git directories
echo "== applying ringrtc patches"
RR="$SRC/third-party/ringrtc"
for entry in "ringrtc-disable-vpio.patch|RINGRTC_NO_VOICE_PROCESSING" \
             "ringrtc-custom-audio-backend.patch|CustomAudioDevice"; do
    p="${entry%%|*}"; marker="${entry##*|}"
    if grep -q "$marker" "$RR/src/rust/src/webrtc/audio_device_module.rs"; then
        echo "already applied: $p"
    else
        git -C "$RR" apply "$SRC/signal-call-tunnel/patches/$p"
    fi
done

echo "== packing source"
tar -C "$TP" --exclude='.git' -czf "$OUT/src.tar.gz" "$(basename "$SRC")"
ls -lh "$OUT"
echo "BUNDLE READY"
