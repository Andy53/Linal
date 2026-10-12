"""Local diagnostic logging for support.

Everything stays on the phone: each component (app, receiver) writes a
rotating log under $XDG_STATE_HOME/linal/logs (private: dir 0700, files
0600). Nothing is uploaded. When a user reports a problem, "Save logs for
support" (main menu) bundles these files plus the signal-cli daemon's recent
journal into one text file they can choose to share.

Privacy rules (enforced by the formatter, so they also cover exceptions):
  * message text is never logged (code must not pass it to the logger);
  * phone numbers are cut to their last 4 digits, Signal UUIDs to 8
    characters, captcha links and other long tokens are removed.
"""
from __future__ import annotations
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
from typing import Optional

from . import __version__

MAX_BYTES = 1_000_000      # per file
BACKUPS = 4                # -> at most ~5 MB per component

_PHONE = re.compile(r"\+\d{7,15}")
_UUID = re.compile(r"\b([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_CAPTCHA = re.compile(r"signalcaptcha://\S+")
_TOKEN = re.compile(r"(?<![\w/.-])[A-Za-z0-9_\-]{40,}(?![\w/.-])")   # long opaque tokens


def redact(text: str) -> str:
    text = _CAPTCHA.sub("signalcaptcha://[redacted]", text)
    text = _PHONE.sub(lambda m: "+…" + m.group(0)[-4:], text)
    text = _UUID.sub(lambda m: m.group(1) + "-…", text)
    text = _TOKEN.sub(lambda m: f"[redacted:{len(m.group(0))}]", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def log_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "linal", "logs")


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler whose files are created 0600."""
    def _open(self):
        old = os.umask(0o077)
        try:
            return super()._open()
        finally:
            os.umask(old)


_configured: Optional[str] = None


def setup(component: str, detailed: bool = False, directory: Optional[str] = None) -> logging.Logger:
    """Configure logging for this process (idempotent). `detailed` turns on
    DEBUG (still redacted). Also captures uncaught exceptions in any thread."""
    global _configured
    root = logging.getLogger("linal")
    if _configured:
        set_detailed(detailed)
        return root
    d = directory or log_dir()
    _private_dir(d)
    fmt = RedactingFormatter(
        f"%(asctime)s.%(msecs)03d %(levelname)-7s {component}[%(process)d] "
        "%(threadName)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = _PrivateRotatingHandler(os.path.join(d, f"{component}.log"),
                                 maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    se = logging.StreamHandler(sys.stderr)      # also the journal, warnings and up
    se.setLevel(logging.WARNING)
    se.setFormatter(fmt)
    root.addHandler(se)
    root.propagate = False
    _configured = component
    set_detailed(detailed)

    def excepthook(exc_type, exc, tb):
        root.critical("uncaught exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = excepthook

    def thread_excepthook(args):
        root.critical("uncaught exception in thread %s",
                      getattr(args.thread, "name", "?"),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_excepthook

    root.info("=== %s %s starting (python %s, %s) ===", component, __version__,
              platform.python_version(), platform.platform())
    return root


def set_detailed(on: bool) -> None:
    logging.getLogger("linal").setLevel(logging.DEBUG if on else logging.INFO)


def get(name: str) -> logging.Logger:
    """Logger for a module: logs.get(__name__)."""
    return logging.getLogger(name if name.startswith("linal") else "linal." + name)


def _journal(units: list[str], since: str) -> str:
    import subprocess
    try:
        out = subprocess.run(["journalctl", "--user", "--no-pager", "-o", "short-iso",
                              "--since", since, *[a for u in units for a in ("-u", u)]],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        out = f"(journal unavailable: {e})\n"
    return out


def export_bundle(dest_dir: str, extra_info: Optional[dict] = None,
                  directory: Optional[str] = None, journal_since: str = "-3d") -> str:
    """Write one text file with everything support needs and return its path.
    Contents are redacted the same way as the logs."""
    os.makedirs(dest_dir, exist_ok=True)
    d = directory or log_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(dest_dir, f"linal-logs-{stamp}.txt")
    parts = [f"Linal support log, saved {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
             f"version: {__version__}", f"platform: {platform.platform()}",
             f"python: {platform.python_version()}"]
    for k, v in (extra_info or {}).items():
        parts.append(f"{k}: {v}")
    parts.append("")
    for comp in ("app", "receiver"):
        files = sorted((f for f in os.listdir(d) if f.startswith(comp + ".log")) if os.path.isdir(d) else [],
                       key=lambda f: int(f.rsplit(".", 1)[-1]) if f[-1].isdigit() else 0, reverse=True)
        for f in files:                     # oldest rotation first
            parts.append(f"===== {f} =====")
            try:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read())
            except OSError as e:
                parts.append(f"(unreadable: {e})")
    parts.append("===== signal-cli daemon journal (stderr: warnings/errors) =====")
    parts.append(_journal(["linal-daemon.service", "linal-receiver.service"], journal_since))
    old = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as out:
            out.write(redact("\n".join(parts)))
    finally:
        os.umask(old)
    logging.getLogger("linal").info("support log saved (%d bytes)", os.path.getsize(path))
    return path
