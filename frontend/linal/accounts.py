"""One Signal account at a time: clearing out previous accounts.

When a different account signs in, or the signed-in account is logged out,
everything belonging to previous accounts goes: signal-cli's keys and data for
them (deleteLocalAccountData) and the app's history and sent attachments
(store.open_account_store / store.forget_all_history).

"Logged out" means Signal itself refused the account's credentials, e.g. after
the number was registered on another device. signal-cli then marks the
account unregistered in its own files; a network failure leaves it marked
registered, so being offline never deletes anything.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from typing import Optional

from . import logs

log = logs.get(__name__)

DAEMON_SERVICE = "linal-daemon.service"
# signal-cli folders shared by all its accounts (received attachments etc.)
SHARED_DIRS = ("attachments", "avatars", "stickers")


def signal_cli_dir(run=None, proc: str = "/proc", environ=None) -> str:
    """signal-cli's data folder: --config from the running daemon's command
    line, else signal-cli's default ($XDG_DATA_HOME/signal-cli)."""
    run = run or subprocess.run
    environ = environ if environ is not None else os.environ
    try:
        pid = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", DAEMON_SERVICE],
                  capture_output=True, text=True, timeout=10).stdout.strip()
        if pid.isdigit() and pid != "0":
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0")]
            for i, a in enumerate(args):
                if a == "--config" and i + 1 < len(args):
                    return args[i + 1]
                if a.startswith("--config="):
                    return a.split("=", 1)[1]
    except Exception:  # noqa: BLE001 — fall back to the default location
        pass
    base = environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "signal-cli")


def local_accounts(config_dir: str) -> list[dict]:
    """Every account signal-cli has files for, loaded by the daemon or not:
    [{"number", "has_aci", "registered"}]. "registered" is None when the
    account's own file can't be read."""
    data = os.path.join(config_dir, "data")
    try:
        with open(os.path.join(data, "accounts.json")) as f:
            entries = json.load(f).get("accounts", [])
    except (OSError, ValueError):
        return []
    out = []
    for e in entries:
        number = e.get("number")
        if not number:
            continue
        registered = None
        try:
            with open(os.path.join(data, str(e.get("path", "")))) as f:
                registered = bool(json.load(f).get("registered"))
        except (OSError, ValueError):
            pass
        out.append({"number": number, "has_aci": bool(e.get("uuid")), "registered": registered})
    return out


def logged_out(accounts: list[dict], loaded: set) -> list[str]:
    """Accounts Signal has signed out: once working (they have an ACI),
    marked unregistered by signal-cli, and not loaded by the daemon. A number
    in the middle of registering has no ACI yet and is left alone."""
    return [a["number"] for a in accounts
            if a["has_aci"] and a["registered"] is False and a["number"] not in loaded]


def delete_local_data(rpc, numbers: list[str]) -> list[str]:
    """Delete signal-cli's keys and data for these accounts. Returns the
    numbers that were deleted."""
    done = []
    for n in numbers:
        try:
            rpc.call("deleteLocalAccountData", {"account": n, "ignoreRegistered": True})
            done.append(n)
        except Exception as e:  # noqa: BLE001 — try the rest; retried next start
            log.warning("couldn't delete local data of %s: %s", n, e)
    if done:
        log.info("deleted local data of %d previous account(s)", len(done))
    return done


def clear_shared_files(config_dir: str) -> None:
    """Delete signal-cli's received attachments, avatars and sticker packs.
    They aren't kept per account, so this follows deleting an account."""
    for name in SHARED_DIRS:
        d = os.path.join(config_dir, name)
        if os.path.isdir(d):
            for entry in os.listdir(d):
                p = os.path.join(d, entry)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
