"""Linal was called Signal-L5 up to version 1.0.12. On the first start after
the rename, its messages, settings and logs move to the new folders and its
old services are stopped (they hold signal-cli's account lock)."""
from __future__ import annotations
import os
import subprocess

OLD, NEW = "signal-l5", "linal"
OLD_SERVICES = ("signal-l5-receiver.service", "signal-l5-daemon.service")


def move_data(environ=None) -> list[str]:
    """Move ~/.local/share/signal-l5 and ~/.local/state/signal-l5 to linal,
    once. Returns the folders moved. Safe when the app and the receiver both
    start at once: a rename either happens or finds it already done."""
    environ = environ if environ is not None else os.environ
    moved = []
    for var, default in (("XDG_DATA_HOME", "~/.local/share"), ("XDG_STATE_HOME", "~/.local/state")):
        base = environ.get(var) or os.path.expanduser(default)
        old, new = os.path.join(base, OLD), os.path.join(base, NEW)
        if os.path.isdir(old) and not os.path.exists(new):
            try:
                os.rename(old, new)
                moved.append(new)
            except OSError:
                pass        # the other process got there first
    return moved


def stop_old_services(run=None) -> None:
    run = run or subprocess.run
    try:
        run(["systemctl", "--user", "stop", *OLD_SERVICES], capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001 — no systemd user manager: nothing to stop
        pass
