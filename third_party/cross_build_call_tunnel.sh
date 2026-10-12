#!/bin/bash
# Cross-compile signal-call-tunnel for the Librem 5 (aarch64, PureOS Crimson /
# glibc 2.36) on an x86_64 laptop with no system C toolchain: zig provides the
# C/C++ compiler and linker (via cargo-zigbuild), rustup the aarch64 std.
# Prereq: fetch_call_tunnel_deps.sh has populated call-tunnel-bundle/ and
# vendored + patched the source tree.
set -euo pipefail
TP="$(cd "$(dirname "$0")" && pwd)"
SRC="$TP/hara-seihun_signal-call-tunnel"
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$HOME/.local/zig-x86_64-linux-0.15.2:$PATH"
# host-side build scripts also need a C compiler/linker
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=zcc-host
export CC_x86_64_unknown_linux_gnu=zcc-host HOST_CC=zcc-host
# ringrtc's build.rs looks here for the (checksummed) prebuilt WebRTC
export OUTPUT_DIR="$TP/webrtc-out-aarch64"
mkdir -p "$OUTPUT_DIR"
cp -n "$TP"/call-tunnel-bundle/webrtc-*-linux-arm64-release.tar.bz2 "$OUTPUT_DIR"/
cd "$SRC/signal-call-tunnel"
cargo +1.91.1 zigbuild --release --offline --target aarch64-unknown-linux-gnu.2.36
ls -la target/aarch64-unknown-linux-gnu/release/signal-call-tunnel
echo CROSS-BUILD-OK
