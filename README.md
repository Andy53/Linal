# Linal

Signal for the Librem 5.

Linal is a native GTK app that makes the Librem 5 a full Signal phone. The
Librem 5 is the account's main device, as a phone running Signal's Android
or iOS app would be, so you don't need another phone. Under the hood it uses
[signal-cli](https://github.com/AsamK/signal-cli) for everything that talks
to Signal.

> **Unofficial.** Linal is not made, reviewed or supported by Signal
> Messenger LLC. Signal doesn't support third-party clients.

## What it does

- One-to-one and group chats, with photos and files
- Replies, reactions, delete for everyone, read receipts and typing indicators
- One-to-one voice calls, with earpiece, speaker and mute
- Start chats from the phone's contacts
- Notifications and incoming calls while the app is closed
- Registration lock PIN

## What it doesn't do (yet)

- Video calls and group calls
- Recording voice messages
- Moving message history over from another phone. Chats start empty.
- More than one account on the phone.

## Requirements

- A Librem 5 running PureOS Crimson
- A phone number that can receive SMS (can be the L5 if it has a phone number)
- A laptop or other device to complete the captcha.

## Installing

Download `linal_1.1.0_arm64.deb` from the
[releases page](https://github.com/Andy53/Linal/releases) and install it
on the phone:

    sudo apt install ./linal_1.1.0_arm64.deb

Everything Linal needs is in the package, including signal-cli and its Java
runtime. Then open **Linal** from the app grid.

## Setting up

1. Open Linal and tap **Register device…**.
2. Check the phone number and tap **Continue**.
3. Signal asks you to solve a captcha. Linal shows a QR code and a 
   web address **On another device** open one.
4. On a computer or phone on the same Wi-Fi, open that address and follow
   the link to Signal's captcha. Solve it. When the page offers to open
   Signal, copy that link instead and paste it into the first page. Linal
   receives it and carries on.
5. Signal sends a code by SMS. If the SMS arrives on the Librem 5, Linal fills
   it in by itself. Otherwise type it in.
6. If the number has a Signal PIN, enter it. Then choose your profile name.

**On this phone** also exists however at the moment the captcha always fails
when completed on the L5. As such the **On another device** option is the 
preferred method.

If the number is already registered with Signal on another phone,
registering it on the Librem 5 signs that phone out of Signal.

## One account at a time

Linal keeps one Signal account on the phone. When you register a different
number, or log out, everything of the previous account is **deleted from the
phone**: its keys, messages, contacts, call history and attachments. This
can't be undone.

You're logged out when:

- you choose **Settings → Account → Log out**, which also tells Signal to stop
  sending this phone messages; or
- Signal signs the phone out, for example because the number was registered
  on another phone.

Losing the network never deletes anything.

## Privacy

- Your Signal keys and messages stay on the phone. Linal sends nothing
  anywhere except to Signal.
- The app talks to signal-cli through a socket that only your user can open.
  It never listens on the network, apart from the one-time page on your Wi-Fi
  during setup.
- Logs shorten phone numbers and never contain message text. They stay on
  the phone.

| Where | What |
|---|---|
| `~/.local/share/signal-cli/` | Signal keys and account data |
| `~/.local/share/linal/` | messages and settings |
| `~/.local/state/linal/logs/` | logs |

## Battery and network

Linal stays connected to Signal so messages arrive while the app is closed,
like any messenger. This uses some battery. It connects over IPv4, because
some mobile networks silently drop large IPv6 packets, which made new
accounts hang.

## Troubleshooting

- **Signal service didn't start:** `systemctl --user restart linal-daemon`,
  then reopen Linal. Details: `journalctl --user -u linal-daemon`.
- **No notifications while the app is closed:** check
  `systemctl --user status linal-receiver`.
- **Reporting a problem:** **Settings → Save logs for support** writes one file
  to Downloads. Read it before you share it.

## Building from source

    scripts/run-tests.sh            # tests; no extra dependencies
    frontend/bin/linal --mock       # the app with demo data (needs GTK 4 and libadwaita)

Building the package is described in [packaging/README.md](packaging/README.md).

## Other Linux Devices
The app is designed for a phone and tested on the Librem 5 however it should work on 
most Debian based distros and other Linux phones. We currently only test builds on the
Librem 5 so other environments may have issues.

## Licence

Linal is free software under the [GNU GPL, version 3 or later](LICENSE). The
package bundles signal-cli (GPL-3.0), libsignal and signal-call-tunnel /
RingRTC (AGPL-3.0) and Eclipse Temurin (GPL-2.0 with Classpath exception);
see [debian/copyright](debian/copyright).
