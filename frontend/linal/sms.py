"""Read recent SMS bodies from the phone's Chatty history DB (best-effort).

Used to auto-detect the Signal verification code during registration.
"""
from __future__ import annotations
import os
import sqlite3
import time
from typing import Optional

CHATTY_DB = os.path.expanduser("~/.purple/chatty/db/chatty-history.db")


def latest_sms_bodies(limit: int = 5, within_seconds: int = 600,
                      since: Optional[int] = None, db: str = CHATTY_DB) -> list[str]:
    """Newest message bodies first. `since` (unix seconds) restricts to messages
    received after that moment, so a code from an earlier attempt isn't reused."""
    if not os.path.exists(db):
        return []
    after = since if since is not None else int(time.time()) - within_seconds
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.execute(
            "SELECT body FROM messages WHERE time>? ORDER BY time DESC LIMIT ?",
            (after, limit))
        rows = [r[0] for r in cur.fetchall() if r[0]]
        con.close()
        return rows
    except Exception:
        return []
