#!/bin/sh
# Download the pinned third-party components bundled into the package
# (Debian packages may not download anything at install time). Verified
# against vendor/SHA256SUMS; the Temurin sum matches Adoptium's published one.
set -eu
cd "$(dirname "$0")/../vendor"
fetch() { [ -f "$1" ] || curl -fL --retry 3 -o "$1.part" "$2" && { [ -f "$1" ] || mv "$1.part" "$1"; }; }
fetch signal-cli-0.14.8.tar.gz \
  https://github.com/AsamK/signal-cli/releases/download/v0.14.8/signal-cli-0.14.8.tar.gz
fetch libsignal_jni.so-v0.102.1-aarch64-unknown-linux-gnu.tar.gz \
  https://github.com/exquo/signal-libs-build/releases/download/libsignal_v0.102.1/libsignal_jni.so-v0.102.1-aarch64-unknown-linux-gnu.tar.gz
fetch OpenJDK25U-jre_aarch64_linux_hotspot_25.0.4.1_1.tar.gz \
  https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1/OpenJDK25U-jre_aarch64_linux_hotspot_25.0.4.1_1.tar.gz
sha256sum -c SHA256SUMS
