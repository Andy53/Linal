"""Local SQLite persistence for conversations, messages, contacts and calls.

The always-on receiver and the GUI both apply the same daemon events here, so
every write is idempotent: messages are keyed by (conv, ts, direction),
reactions are a set per reactor, receipts only ever advance a status, and the
unread count is derived from a per-conversation read position rather than
incremented. A revision counter lets the GUI notice any change cheaply.
Each account has its own history file, accounts/<id>/history.db, and signing
in a different account deletes the others' (see open_account_store).
~/.local/share/linal/history.db holds app-wide settings, and is where
history lived before 1.0.12.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from typing import Optional
from .models import Attachment, CallRecord, Conversation, Message, Quote, STATUS_ORDER
from .envelope import (DeleteEvent, EditEvent, MessageEvent, ReactionEvent,
                       ReadSyncEvent, ReceiptEvent)


def default_path() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "linal")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "history.db")


def account_path(number: str, base: Optional[str] = None) -> str:
    """History file for one account (the number is hashed, not spelled out)."""
    base = base or os.path.dirname(default_path())
    d = os.path.join(base, "accounts", hashlib.sha256(number.encode()).hexdigest()[:16])
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, "history.db")


def open_account_store(number: str, legacy_path: Optional[str] = None) -> "Store":
    """The Store for `number`, the account signed in now.

    A different account signing in deletes every other account's history:
    their history files and the copies of sent attachments. The same account
    signing in again keeps its own. History from before 1.0.12 lived in the
    app-wide file: it moves to its account when that is the one signed in
    (the account recorded there, or the first one opened for a file with none
    recorded, as after an upgrade), and is deleted otherwise. The app and the
    receiver can both get here at start-up, so this runs under a file lock."""
    legacy = legacy_path or default_path()
    base = os.path.dirname(legacy)
    path = account_path(number, base)
    with open(legacy + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = Store(legacy)
        try:
            last = settings.get_meta("owner")
            if last is not None and last != number:
                shutil.rmtree(os.path.join(base, "sent"), ignore_errors=True)
            if last in (None, number) and not os.path.exists(path) and settings.has_history():
                tmp = path + ".tmp"
                dst = sqlite3.connect(tmp)
                with settings._lock:
                    settings._conn.backup(dst)
                dst.close()
                os.replace(tmp, path)
            settings.clear_history()
            if last != number:
                settings.set_meta("owner", number)
        finally:
            settings.close()
        accounts = os.path.join(base, "accounts")
        for d in os.listdir(accounts):
            if os.path.join(accounts, d) != os.path.dirname(path):
                shutil.rmtree(os.path.join(accounts, d), ignore_errors=True)
    return Store(path)


def last_account(legacy_path: Optional[str] = None) -> Optional[str]:
    """The account that last signed in on this phone (None if none has)."""
    s = Store(legacy_path or default_path())
    try:
        return s.get_meta("owner")
    finally:
        s.close()


def forget_all_history(legacy_path: Optional[str] = None) -> None:
    """Logged out: delete every account's history and sent attachments.
    App-wide settings stay."""
    legacy = legacy_path or default_path()
    base = os.path.dirname(legacy)
    with open(legacy + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shutil.rmtree(os.path.join(base, "accounts"), ignore_errors=True)
        shutil.rmtree(os.path.join(base, "sent"), ignore_errors=True)
        s = Store(legacy)
        try:
            s.clear_history()
            with s._lock, s._conn:
                s._conn.execute("DELETE FROM meta WHERE k='owner'")
        finally:
            s.close()


# columns added after the first release; created on open if missing
_MESSAGE_COLUMNS = {
    "author_id": "TEXT DEFAULT ''",
    "attachments": "TEXT DEFAULT '[]'",
    "quote": "TEXT DEFAULT ''",
    "reactions": "TEXT DEFAULT '{}'",
    "status": "TEXT DEFAULT ''",
    "deleted": "INTEGER DEFAULT 0",
    "edited": "INTEGER DEFAULT 0",
}
_CONVERSATION_COLUMNS = {
    "read_upto": "INTEGER DEFAULT 0",
}
_MSG_SELECT = "ts,body,outgoing,author,author_id,attachments,quote,reactions,status,deleted,edited"


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations(
                    key TEXT PRIMARY KEY,
                    title TEXT,
                    is_group INTEGER DEFAULT 0,
                    last_time INTEGER DEFAULT 0
                )""")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS messages(
                    conv_key TEXT,
                    ts INTEGER,
                    body TEXT,
                    outgoing INTEGER,
                    author TEXT,
                    PRIMARY KEY (conv_key, ts, outgoing)
                )""")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS contacts(
                    id TEXT PRIMARY KEY,      -- number or uuid
                    name TEXT DEFAULT '',     -- name the user saved
                    profile_name TEXT DEFAULT ''
                )""")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS calls(
                    call_id TEXT PRIMARY KEY,
                    conv_key TEXT,
                    ts INTEGER,
                    outgoing INTEGER,
                    state TEXT DEFAULT '',
                    connected_at INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 0
                )""")
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            self._add_missing_columns("messages", _MESSAGE_COLUMNS)
            self._add_missing_columns("conversations", _CONVERSATION_COLUMNS)

    def _add_missing_columns(self, table: str, cols: dict):
        have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self):
        self._conn.close()

    _HISTORY_TABLES = ("messages", "conversations", "contacts", "calls")

    def has_history(self) -> bool:
        with self._lock:
            return any(self._conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                       for t in self._HISTORY_TABLES)

    def clear_history(self) -> None:
        """Delete all chats, contacts and calls (settings are kept), and
        rewrite the file so the deleted rows don't linger in free pages."""
        if not self.has_history():
            return
        with self._lock:
            with self._conn:
                for t in self._HISTORY_TABLES:
                    self._conn.execute(f"DELETE FROM {t}")
                self._conn.execute("DELETE FROM meta WHERE k='focus' OR k LIKE 'answer-token:%'")
                self._bump()
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- revision counter (cheap change detection for the GUI) ---------------
    def _bump(self):
        self._conn.execute("""
            INSERT INTO meta(k,v) VALUES('rev','1')
            ON CONFLICT(k) DO UPDATE SET v=CAST(meta.v AS INTEGER)+1""")

    def revision(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT v FROM meta WHERE k='rev'").fetchone()
        return int(r[0]) if r else 0

    # -- small JSON key/value settings --------------------------------------
    def get_meta(self, k: str, default=None):
        with self._lock:
            r = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k: str, value) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(value)))

    # -- one-time tokens that let a notification's "Answer" answer a call ----
    ANSWER_TOKEN_TTL = 120.0   # seconds; a call rings for about a minute

    def issue_answer_token(self, call_id: str) -> str:
        """Random single-use token for the incoming-call notification's Answer
        link (the receiver issues it, the app consumes it)."""
        import secrets
        token = secrets.token_urlsafe(24)
        self.set_meta(f"answer-token:{call_id}", {"t": token, "at": time.time()})
        return token

    def consume_answer_token(self, call_id: str, token: Optional[str]) -> bool:
        """True only for the token issued for this call, unexpired and unused."""
        import hmac
        key = f"answer-token:{call_id}"
        rec = self.get_meta(key)
        if rec is not None:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM meta WHERE k=?", (key,))   # single use
        if not token or not rec or not isinstance(rec.get("t"), str):
            return False
        if time.time() - float(rec.get("at", 0)) > self.ANSWER_TOKEN_TTL:
            return False
        return hmac.compare_digest(rec["t"].encode(), token.encode())

    # -- GUI focus (lets the receiver skip notifying for the open chat) ------
    def set_focus(self, conv_key: Optional[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('focus',?)",
                               (json.dumps({"key": conv_key, "at": time.time()}),))

    def focused_conversation(self, max_age: float = 6.0) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT v FROM meta WHERE k='focus'").fetchone()
        if not r:
            return None
        d = json.loads(r[0])
        return d.get("key") if time.time() - d.get("at", 0) <= max_age else None

    # -- conversations -------------------------------------------------------
    def upsert_conversation(self, key: str, title: str, is_group: bool = False,
                            last_time: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO conversations(key,title,is_group,last_time)
                VALUES(?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE conversations.title END,
                    is_group=MAX(conversations.is_group, excluded.is_group),
                    last_time=MAX(conversations.last_time, excluded.last_time)
            """, (key, title or "", 1 if is_group else 0, last_time))
            self._bump()

    def mark_read(self, conv_key: str, upto_ts: Optional[int] = None) -> list[tuple[str, int]]:
        """Advance the read position. Returns (author_id, ts) of incoming messages
        that just became read, so the caller can send read receipts."""
        with self._lock, self._conn:
            r = self._conn.execute("SELECT read_upto FROM conversations WHERE key=?",
                                   (conv_key,)).fetchone()
            if r is None:
                return []
            prev = r[0] or 0
            if upto_ts is None:
                m = self._conn.execute("SELECT MAX(ts) FROM messages WHERE conv_key=?",
                                       (conv_key,)).fetchone()
                upto_ts = m[0] or 0
            if upto_ts <= prev:
                return []
            rows = self._conn.execute("""
                SELECT author_id, ts FROM messages
                WHERE conv_key=? AND outgoing=0 AND ts>? AND ts<=?""",
                (conv_key, prev, upto_ts)).fetchall()
            self._conn.execute("UPDATE conversations SET read_upto=? WHERE key=?",
                               (upto_ts, conv_key))
            self._bump()
        return [(a or "", t) for a, t in rows]

    def unread_count(self, conv_key: str) -> int:
        with self._lock:
            r = self._conn.execute("""
                SELECT COUNT(*) FROM messages m JOIN conversations c ON c.key=m.conv_key
                WHERE m.conv_key=? AND m.outgoing=0 AND m.deleted=0 AND m.ts>c.read_upto""",
                (conv_key,)).fetchone()
        return r[0] or 0

    def total_unread(self) -> int:
        with self._lock:
            r = self._conn.execute("""
                SELECT COUNT(*) FROM messages m JOIN conversations c ON c.key=m.conv_key
                WHERE m.outgoing=0 AND m.deleted=0 AND m.ts>c.read_upto""").fetchone()
        return r[0] or 0

    # -- contacts ------------------------------------------------------------
    def set_contact_name(self, cid: str, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO contacts(id,name) VALUES(?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name""", (cid, name or ""))
            self._bump()

    def set_profile_name(self, cid: str, name: str) -> None:
        if not cid or not name:
            return
        with self._lock, self._conn:
            cur = self._conn.execute("""
                INSERT INTO contacts(id,profile_name) VALUES(?,?)
                ON CONFLICT(id) DO UPDATE SET profile_name=excluded.profile_name
                WHERE contacts.profile_name<>excluded.profile_name""", (cid, name))
            if cur.rowcount:
                self._bump()

    def display_name(self, cid: str) -> str:
        if not cid:
            return ""
        with self._lock:
            r = self._conn.execute("SELECT name, profile_name FROM contacts WHERE id=?",
                                   (cid,)).fetchone()
        if r:
            return r[0] or r[1] or cid
        return cid

    def contacts(self) -> list[tuple[str, str]]:
        """(id, best name) for everyone we have a name for."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, CASE WHEN name<>'' THEN name ELSE profile_name END FROM contacts").fetchall()
        return [(i, n or i) for i, n in rows]

    # -- messages ------------------------------------------------------------
    def add_message(self, conv_key: str, msg: Message, title: Optional[str] = None,
                    is_group: bool = False) -> bool:
        """Idempotent insert. Returns True if a new row was added."""
        with self._lock, self._conn:
            cur = self._conn.execute("""
                INSERT OR IGNORE INTO messages
                    (conv_key,ts,body,outgoing,author,author_id,attachments,quote,reactions,status)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (conv_key, msg.timestamp, msg.body, 1 if msg.outgoing else 0, msg.author,
                 msg.author_id, json.dumps([a.to_dict() for a in msg.attachments]),
                 json.dumps(msg.quote.to_dict()) if msg.quote else "",
                 json.dumps(msg.reactions), msg.status))
            added = cur.rowcount > 0
            self._conn.execute("""
                INSERT INTO conversations(key,title,is_group,last_time) VALUES(?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    last_time=MAX(conversations.last_time, excluded.last_time),
                    is_group=MAX(conversations.is_group, excluded.is_group),
                    title=CASE WHEN excluded.title<>excluded.key AND excluded.title<>''
                               THEN excluded.title ELSE conversations.title END
            """, (conv_key, title or conv_key, 1 if is_group else 0, msg.timestamp))
            if msg.outgoing:
                # our own message: everything before it has been seen
                self._conn.execute(
                    "UPDATE conversations SET read_upto=MAX(read_upto, ?) WHERE key=?",
                    (msg.timestamp, conv_key))
            if added:
                self._bump()
        return added

    def _row_to_message(self, row) -> Message:
        ts, body, outgoing, author, author_id, atts, quote, reacts, status, deleted, edited = row
        return Message(timestamp=ts, body=body or "", outgoing=bool(outgoing), author=author or "",
                       author_id=author_id or "",
                       attachments=[Attachment.from_dict(a) for a in json.loads(atts or "[]")],
                       quote=Quote.from_dict(json.loads(quote)) if quote else None,
                       reactions=json.loads(reacts or "{}"), status=status or "",
                       deleted=bool(deleted), edited=bool(edited))

    def _find(self, conv_key: str, ts: int, author: str = "") -> Optional[tuple]:
        """Locate a message by its sent-timestamp; author disambiguates when two
        people sent at the same millisecond. Returns (ts, outgoing)."""
        rows = self._conn.execute(
            "SELECT ts, outgoing, author_id FROM messages WHERE conv_key=? AND ts=?",
            (conv_key, ts)).fetchall()
        if not rows:
            return None
        if len(rows) > 1 and author:
            for t, o, a in rows:
                if a == author:
                    return t, o
            for t, o, a in rows:
                if o:
                    return t, o
        return rows[0][0], rows[0][1]

    def set_reaction(self, conv_key: str, target_ts: int, target_author: str,
                     emoji: str, reactor: str, remove: bool = False) -> bool:
        """One reaction per reactor, as in Signal: a new emoji replaces theirs."""
        with self._lock, self._conn:
            hit = self._find(conv_key, target_ts, target_author)
            if not hit:
                return False
            ts, outgoing = hit
            r = self._conn.execute(
                "SELECT reactions FROM messages WHERE conv_key=? AND ts=? AND outgoing=?",
                (conv_key, ts, outgoing)).fetchone()
            reactions: dict[str, list[str]] = json.loads(r[0] or "{}")
            for e in list(reactions):
                reactions[e] = [x for x in reactions[e] if x != reactor]
                if not reactions[e]:
                    del reactions[e]
            if not remove and emoji:
                reactions.setdefault(emoji, []).append(reactor)
            self._conn.execute(
                "UPDATE messages SET reactions=? WHERE conv_key=? AND ts=? AND outgoing=?",
                (json.dumps(reactions), conv_key, ts, outgoing))
            self._bump()
        return True

    def set_status(self, timestamps: list[int], status: str) -> int:
        """Advance outgoing messages' delivery status (never moves backwards)."""
        rank = STATUS_ORDER.index(status)
        lower = STATUS_ORDER[:rank]
        if not timestamps or not lower:
            return 0
        with self._lock, self._conn:
            qs = ",".join("?" * len(timestamps))
            ls = ",".join("?" * len(lower))
            cur = self._conn.execute(
                f"UPDATE messages SET status=? WHERE outgoing=1 AND ts IN ({qs}) AND status IN ({ls})",
                (status, *timestamps, *lower))
            if cur.rowcount:
                self._bump()
            return cur.rowcount

    def mark_deleted(self, conv_key: str, target_ts: int, outgoing: Optional[bool] = None) -> bool:
        with self._lock, self._conn:
            q = "UPDATE messages SET deleted=1, body='', attachments='[]' WHERE conv_key=? AND ts=?"
            args: list = [conv_key, target_ts]
            if outgoing is not None:
                q += " AND outgoing=?"
                args.append(1 if outgoing else 0)
            cur = self._conn.execute(q, args)
            if cur.rowcount:
                self._bump()
            return cur.rowcount > 0

    def apply_edit(self, conv_key: str, target_ts: int, body: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE messages SET body=?, edited=1 WHERE conv_key=? AND ts=? AND deleted=0",
                (body, conv_key, target_ts))
            if cur.rowcount:
                self._bump()
            return cur.rowcount > 0

    def mark_read_by_message(self, sender: str, ts: int) -> None:
        """A read-sync names only the sender and timestamp; find its chat."""
        with self._lock:
            r = self._conn.execute(
                "SELECT conv_key FROM messages WHERE outgoing=0 AND author_id=? AND ts=?",
                (sender, ts)).fetchone()
        if r:
            self.mark_read(r[0], ts)

    def apply_event(self, ev) -> Optional[MessageEvent]:
        """Persist one parsed envelope event. Returns the MessageEvent if it was a
        message not seen before (for notifications), else None."""
        if isinstance(ev, MessageEvent):
            m = ev.message
            if m.author_id and m.author and m.author != m.author_id:
                self.set_profile_name(m.author_id, m.author)
            if not ev.is_group and ev.title and ev.title != ev.conv_key:
                self.set_profile_name(ev.conv_key, ev.title)
            added = self.add_message(ev.conv_key, m, title=ev.title if ev.is_group else None,
                                     is_group=ev.is_group)
            return ev if added else None
        if isinstance(ev, ReactionEvent):
            self.set_reaction(ev.conv_key, ev.target_ts, ev.target_author, ev.emoji,
                              ev.reactor, ev.remove)
        elif isinstance(ev, ReceiptEvent):
            self.set_status(ev.timestamps, ev.kind)
        elif isinstance(ev, DeleteEvent):
            self.mark_deleted(ev.conv_key, ev.target_ts)
        elif isinstance(ev, EditEvent):
            self.apply_edit(ev.conv_key, ev.target_ts, ev.body)
        elif isinstance(ev, ReadSyncEvent):
            self.mark_read_by_message(ev.sender, ev.timestamp)
        return None

    # -- calls ---------------------------------------------------------------
    def record_call(self, call_id: str, conv_key: str, outgoing: bool, state: str,
                    now_ms: Optional[int] = None) -> CallRecord:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO calls(call_id,conv_key,ts,outgoing,state) VALUES(?,?,?,?,?)
                ON CONFLICT(call_id) DO NOTHING""",
                (call_id, conv_key, now_ms, 1 if outgoing else 0, state))
            if state == "CONNECTED":
                self._conn.execute(
                    "UPDATE calls SET connected_at=? WHERE call_id=? AND connected_at=0",
                    (now_ms, call_id))
            if state == "ENDED":
                self._conn.execute("""
                    UPDATE calls SET duration=CASE WHEN connected_at>0
                        THEN (?-connected_at)/1000 ELSE 0 END
                    WHERE call_id=? AND state<>'ENDED'""", (now_ms, call_id))
            self._conn.execute("UPDATE calls SET state=? WHERE call_id=?", (state, call_id))
            self._conn.execute("""
                INSERT INTO conversations(key,title,last_time) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET last_time=MAX(conversations.last_time, excluded.last_time)
            """, (conv_key, conv_key, now_ms))
            self._bump()
        return next(c for c in self.get_calls(conv_key) if c.call_id == call_id)

    def get_calls(self, conv_key: Optional[str] = None) -> list[CallRecord]:
        q = "SELECT call_id,conv_key,ts,outgoing,state,connected_at,duration FROM calls"
        args: tuple = ()
        if conv_key is not None:
            q += " WHERE conv_key=?"
            args = (conv_key,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY ts", args).fetchall()
        return [CallRecord(call_id=c, conv_key=k, timestamp=t, outgoing=bool(o), state=s,
                           answered=bool(ca), duration=d or 0)
                for c, k, t, o, s, ca, d in rows]

    # -- reads ---------------------------------------------------------------
    def get_conversations(self) -> list[Conversation]:
        """Conversations by recency, each with only its latest message (enough
        for the list; the chat view loads the full history)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key,title,is_group FROM conversations ORDER BY last_time DESC").fetchall()
        out = []
        for key, title, is_group in rows:
            if is_group:
                name = title if title and title != key else "Group"
            else:
                name = self.display_name(key)
                if name == key and title and title != key:
                    name = title
            c = Conversation(key=key, title=name, is_group=bool(is_group))
            c.messages = self.get_messages(key, limit=1)
            c.unread = self.unread_count(key)
            out.append(c)
        return out

    def get_messages(self, conv_key: str, limit: Optional[int] = None) -> list[Message]:
        with self._lock:
            if limit:
                rows = self._conn.execute(
                    f"SELECT {_MSG_SELECT} FROM messages WHERE conv_key=? ORDER BY ts DESC LIMIT ?",
                    (conv_key, limit)).fetchall()[::-1]
            else:
                rows = self._conn.execute(
                    f"SELECT {_MSG_SELECT} FROM messages WHERE conv_key=? ORDER BY ts",
                    (conv_key,)).fetchall()
        return [self._row_to_message(r) for r in rows]

    def latest_ts(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT MAX(ts) FROM messages").fetchone()
        return r[0] or 0
