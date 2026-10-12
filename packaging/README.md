# Packaging Linal

Linal ships as one Debian package, `linal` (arm64). Everything it needs is
inside the package; nothing is downloaded when it installs.

## What the package contains

| Path | What |
|---|---|
| `/usr/share/linal/linal/` | the app (Python, GTK 4 / libadwaita) |
| `/usr/bin/linal`, `/usr/bin/linal-receiver` | the app and the background receiver |
| `/usr/share/linal/signal-cli/` | signal-cli 0.14.8 |
| `/usr/lib/linal/jre/` | Java 25 runtime (Eclipse Temurin; Debian 12 has no Java 25) |
| `/usr/lib/linal/lib/libsignal_jni.so` | libsignal for aarch64 (signal-cli bundles x86_64 only) |
| `/usr/lib/linal/bin/signal-call-tunnel` | voice-call media engine (RingRTC), see `third_party/README.md` |
| `/usr/lib/systemd/user/linal-daemon.service` | signal-cli daemon, enabled for all users |
| `/usr/lib/systemd/user/linal-receiver.service` | stores messages and notifies while the app is closed |

## Building

On an arm64 Debian 12 / PureOS Crimson system (the Librem 5 itself works) with
`debhelper` and `devscripts` installed:

    packaging/fetch-vendor.sh        # signal-cli, libsignal, JRE; checks vendor/SHA256SUMS
    scripts/run-tests.sh             # optional; the build does not run them
    dpkg-buildpackage -b -us -uc     # from the repo root
    lintian ../linal_*_arm64.deb

`prebuilt/arm64/signal-call-tunnel` must exist (build it per
`third_party/README.md`).

## Serving it to GNOME Software / the PureOS store

`make-repo.sh` builds a signed apt repository with the AppStream metadata
and icons that GNOME Software reads:

    packaging/make-repo.sh ../linal_1.1.0_arm64.deb /srv/linal-repo

Point apt at it on the phone (the key is written into the repository):

    sudo install -Dm644 /srv/linal-repo/linal-archive-keyring.gpg /etc/apt/keyrings/linal-archive-keyring.gpg
    sudo tee /etc/apt/sources.list.d/linal.sources <<EOF
    Types: deb
    URIs: file:/srv/linal-repo
    Suites: stable
    Components: main
    Architectures: arm64
    Signed-By: /etc/apt/keyrings/linal-archive-keyring.gpg
    EOF
    sudo apt update && sudo apt install linal

Linal then shows in GNOME Software and installs or updates from there.

## Upgrading from Signal-L5

`linal` replaces the `signal-l5` package. On its first start Linal moves
Signal-L5's messages, settings and logs to its own folders and stops the old
services. The Signal account (signal-cli's data in `~/.local/share/signal-cli`)
carries over unchanged.
