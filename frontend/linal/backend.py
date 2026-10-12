"""Backend abstraction: one interface, two implementations (real + mock).

RealBackend drives signal-cli over JSON-RPC and persists history to a local
Store (shared with the always-on receiver). The GUI reads conversations/messages
from the Store and polls revision() for live updates.
"""
from __future__ import annotations
import os
import re
from typing import Callable, Optional
from .models import Account, Attachment, Contact, Conversation, Message, Quote
from .rpc import SignalRpc, RpcError
from .store import (Store, account_path, forget_all_history, last_account,
                    open_account_store)
from .accounts import (clear_shared_files, delete_local_data, local_accounts, logged_out,
                       signal_cli_dir)
from .envelope import MessageEvent, TypingEvent, parse_events
from . import logs
log = logs.get(__name__)

# signal-cli keeps received attachment files here, named by attachment id
ATTACHMENT_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                              "signal-cli", "attachments")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_group_id(key: str) -> bool:
    """Group ids are base64; direct chats are an E.164 number or a uuid."""
    return bool(key) and not key.startswith("+") and not _UUID_RE.match(key)


class Backend:
    on_message: Optional[Callable[[str, Message], None]] = None
    on_connection_change: Optional[Callable[[bool], None]] = None
    on_typing: Optional[Callable[[str, str, bool], None]] = None      # conv, who, started
    on_call_event: Optional[Callable[[dict], None]] = None            # raw JsonCallEvent

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def list_accounts(self) -> list[Account]: return []
    def is_registered(self) -> bool: return True
    def list_conversations(self) -> list[Conversation]: return []
    def get_messages(self, conv_key: str) -> list[Message]: return []
    def get_calls(self, conv_key: str) -> list: return []
    def latest_ts(self) -> int: return 0
    def revision(self) -> int: return self.latest_ts()
    def send(self, conv_key: str, text: str, attachments: Optional[list[str]] = None,
             quote: Optional[Message] = None) -> Message: raise NotImplementedError
    def register(self, number: str, captcha: str, voice: bool = False) -> None: ...
    def verify(self, number: str, code: str) -> None: ...
    def display_name(self, cid: str) -> str: return cid
    def mark_read(self, conv_key: str) -> None: ...
    def set_focus(self, conv_key: Optional[str]) -> None: ...
    def attachment_path(self, att: Attachment) -> str: return att.path
    def typing(self, conv_key: str, stop: bool = False) -> None: ...
    def list_contacts(self) -> list[Conversation]: return []
    def check_numbers(self, numbers: list[str]) -> dict[str, bool]: return {n: True for n in numbers}
    def start_conversation(self, cid: str, title: str = "") -> Conversation:
        return Conversation(key=cid, title=title or cid, is_group=is_group_id(cid))
    def react(self, conv_key: str, msg: Message, emoji: str, remove: bool = False) -> None: ...
    def delete_for_everyone(self, conv_key: str, msg: Message) -> None: ...
    def save_contact(self, cid: str, name: str) -> None: ...
    def create_group(self, name: str, members: list[str]) -> str: raise NotImplementedError
    def block(self, conv_key: str) -> None: ...
    def set_profile_name(self, name: str) -> None: ...
    def update_settings(self, **kw) -> None: ...
    def set_pin(self, pin: str) -> None: ...
    def remove_pin(self) -> None: ...
    def start_call(self, recipient: str) -> dict: raise NotImplementedError("calls need signal-cli")
    def accept_call(self, call_id) -> dict: return {}
    def hangup_call(self, call_id) -> None: ...
    def reject_call(self, call_id) -> None: ...
    def consume_answer_token(self, call_id: str, token: Optional[str]) -> bool: return False
    def logout(self) -> None: ...


class RealBackend(Backend):
    def __init__(self, account: Optional[str] = None, host: Optional[str] = None,
                 port: Optional[int] = None, store: Optional[Store] = None,
                 socket_path: Optional[str] = None):
        self.account = account
        # Unix socket by default; host/port (TCP) only when given explicitly
        self.rpc = SignalRpc(host, port, socket_path=socket_path)
        # the account's own history once the account is known (app-wide
        # settings file until then); a store passed in (tests) is kept
        self._own_store = store is None
        self.store = store if store is not None else Store()
        self.cli_dir: Optional[str] = None      # signal-cli's folder; found from the daemon
        self.rpc.on_notification = self._on_notification
        self.rpc.on_disconnect = lambda: self._conn(False)

    def _conn(self, up: bool):
        log.info("daemon %s", "connected" if up else "disconnected")
        if self.on_connection_change:
            self.on_connection_change(up)

    def start(self) -> None:
        self.rpc.connect()
        loaded = [a.number for a in self.list_accounts()]
        if not self.account and loaded:
            last = last_account() if self._own_store else None
            self.account = last if last in loaded else loaded[0]
        if self._own_store:
            self._clear_previous_accounts(loaded)
        if self.account:
            self._use_account_store(self.account)
            try:
                self.rpc.call("subscribeCallEvents", {"account": self.account})
            except RpcError:
                pass    # older daemon without call support
        self._conn(True)

    def stop(self) -> None:
        self.rpc.close()

    @property
    def connected(self) -> bool:
        return self.rpc.connected

    def adopt_account(self, number: str) -> None:
        """Switch to a just-registered account. Uses a fresh connection (the
        daemon pushes messages/call events for the accounts a connection
        subscribed to) rather than reusing the old one, whose reader thread is
        still winding down."""
        old = self.rpc
        new = old.clone()
        new.on_notification = self._on_notification
        new.on_disconnect = lambda: self._conn(False)
        old.on_disconnect = None
        new.connect()
        self.rpc = new
        self.account = number
        self._use_account_store(number)
        old.close()
        if self._own_store:
            self._clear_previous_accounts([number])
        try:
            new.call("subscribeCallEvents", {"account": number})
        except RpcError:
            pass
        self._conn(True)

    def _clear_previous_accounts(self, loaded: list[str]) -> None:
        """Only one account is kept on the phone. With an account signed in,
        every other account's signal-cli data goes (its history went when the
        store switched). With none signed in, accounts Signal has logged out
        go, with all history; so does history left from accounts that no
        longer exist here. Never deletes anything because of a network error."""
        try:
            cli = self.cli_dir or signal_cli_dir()
            accounts = local_accounts(cli)
            if self.account:
                if delete_local_data(self.rpc, [a["number"] for a in accounts
                                                if a["number"] != self.account]):
                    clear_shared_files(cli)
                return
            out = logged_out(accounts, set(loaded))
            if out:
                log.info("logged out by Signal: deleting the account's data from this phone")
                delete_local_data(self.rpc, out)
            if out or (not accounts and last_account() is not None):
                clear_shared_files(cli)
                forget_all_history()
        except Exception as e:  # noqa: BLE001 — cleanup must never stop the app
            log.warning("couldn't clear previous accounts: %s", e)

    def logout(self) -> None:
        """Log this phone out: Signal stops delivering to it, then everything
        of the account (and any other) is deleted here. Raises, deleting
        nothing, if Signal can't be reached."""
        if self.account:
            self.rpc.call("unregister", {"account": self.account})
            log.info("logged out; deleting the account's data from this phone")
        cli = self.cli_dir or signal_cli_dir()
        delete_local_data(self.rpc, [a["number"] for a in local_accounts(cli)])
        clear_shared_files(cli)
        self.account = None
        if self._own_store:
            forget_all_history()
            self.store = Store()

    def _use_account_store(self, number: str) -> None:
        if self._own_store and self.store.path != account_path(number):
            self.store = open_account_store(number)

    def _p(self, **params) -> dict:
        """Params for an account-scoped call, dropping unset values."""
        d = {"account": self.account}
        d.update({k: v for k, v in params.items() if v is not None})
        return d

    def _target(self, conv_key: str) -> dict:
        return {"groupId": conv_key} if is_group_id(conv_key) else {"recipient": [conv_key]}

    # -- account -------------------------------------------------------------
    def list_accounts(self) -> list[Account]:
        res = self.rpc.call("listAccounts") or []
        return [Account(number=a.get("number", ""), registered=True) for a in res]

    def is_registered(self) -> bool:
        try:
            return len(self.list_accounts()) > 0
        except Exception:
            return False

    def version(self) -> str:
        return (self.rpc.call("version") or {}).get("version", "?")

    def register(self, number: str, captcha: str, voice: bool = False,
                 reregister: bool = False) -> None:
        params = {"account": number, "captcha": captcha, "voice": voice}
        if reregister:   # take over a number that's registered (here or on another phone)
            params["reregister"] = True
        self.rpc.call("register", params)

    def verify(self, number: str, code: str, pin: Optional[str] = None) -> None:
        params = {"account": number, "verificationCode": code}
        if pin:
            params["pin"] = pin
        self.rpc.call("verify", params)

    def set_profile_name(self, name: str) -> None:
        self.rpc.call("updateProfile", self._p(givenName=name))

    def update_settings(self, read_receipts: Optional[bool] = None,
                        typing_indicators: Optional[bool] = None,
                        link_previews: Optional[bool] = None) -> None:
        self.rpc.call("updateConfiguration", self._p(readReceipts=read_receipts,
                                                     typingIndicators=typing_indicators,
                                                     linkPreviews=link_previews))

    def set_pin(self, pin: str) -> None:
        self.rpc.call("setPin", self._p(pin=pin))

    def remove_pin(self) -> None:
        self.rpc.call("removePin", self._p())

    # -- history (from the persistent store, filled by the receiver) ---------
    def list_conversations(self) -> list[Conversation]:
        convs = self.store.get_conversations()
        for c in convs:
            if self.account and c.key == self.account:
                c.title = "Note to Self"     # messages to our own number, as on Android
        return convs

    def get_messages(self, conv_key: str) -> list[Message]:
        return self.store.get_messages(conv_key)

    def get_calls(self, conv_key: str) -> list:
        return self.store.get_calls(conv_key)

    def latest_ts(self) -> int:
        return self.store.latest_ts()

    def revision(self) -> int:
        return self.store.revision()

    def display_name(self, cid: str) -> str:
        return self.store.display_name(cid)

    def set_focus(self, conv_key: Optional[str]) -> None:
        self.store.set_focus(conv_key)

    def attachment_path(self, att: Attachment) -> str:
        if att.path and os.path.exists(att.path):
            return att.path
        return os.path.join(ATTACHMENT_DIR, att.id) if att.id else ""

    # -- contacts & groups ---------------------------------------------------
    def list_contacts(self) -> list[Conversation]:
        """Signal contacts and groups this account knows (for the new-chat picker)."""
        convs: list[Conversation] = []
        try:
            groups = self.rpc.call("listGroups", {"account": self.account}) or []
        except RpcError:
            groups = []
        for g in groups:
            convs.append(Conversation(key=g.get("id", ""),
                                      title=g.get("name") or "Group", is_group=True))
        try:
            contacts = self.rpc.call("listContacts", {"account": self.account}) or []
        except RpcError:
            contacts = []
        for c in contacts:
            num = c.get("number") or c.get("uuid") or ""
            prof = c.get("profile") or {}
            name = (c.get("name") or c.get("profileName") or prof.get("givenName") or num)
            if num:
                convs.append(Conversation(key=num, title=name))
        return convs

    def sync_names(self) -> None:
        """Pull profile/contact names and group titles from signal-cli into the store."""
        for c in self.list_contacts():
            if c.is_group:
                self.store.upsert_conversation(c.key, c.title, is_group=True)
            elif c.title and c.title != c.key:
                self.store.set_profile_name(c.key, c.title)

    def check_numbers(self, numbers: list[str]) -> dict[str, bool]:
        """Which of these numbers have a Signal account (getUserStatus)."""
        res = self.rpc.call("getUserStatus", self._p(recipient=list(numbers))) or []
        out: dict[str, bool] = {}
        for r in res:
            key = r.get("recipient") or r.get("number") or ""
            out[key] = bool(r.get("isRegistered"))
        return out

    def save_contact(self, cid: str, name: str) -> None:
        """Name a contact in the Signal account (synced to linked devices)."""
        self.rpc.call("updateContact", self._p(recipient=cid, name=name))
        self.store.set_contact_name(cid, name)

    def start_conversation(self, cid: str, title: str = "") -> Conversation:
        self.store.upsert_conversation(cid, title or "", is_group=is_group_id(cid))
        if title and not is_group_id(cid):
            self.store.set_profile_name(cid, title)
        return Conversation(key=cid, title=title or self.display_name(cid),
                            is_group=is_group_id(cid))

    def create_group(self, name: str, members: list[str]) -> str:
        res = self.rpc.call("updateGroup", self._p(name=name, member=list(members))) or {}
        gid = res.get("groupId") or ""
        if gid:
            self.store.upsert_conversation(gid, name, is_group=True)
        return gid

    def block(self, conv_key: str) -> None:
        if is_group_id(conv_key):
            self.rpc.call("block", self._p(groupId=[conv_key]))
        else:
            self.rpc.call("block", self._p(recipient=[conv_key]))

    # -- messaging -----------------------------------------------------------
    def send(self, conv_key: str, text: str, attachments: Optional[list[str]] = None,
             quote: Optional[Message] = None) -> Message:
        params = {"account": self.account, "message": text}
        params.update(self._target(conv_key))
        uploads: list[str] = []
        if attachments:
            # shrink photos like Android's "standard" quality: a camera JPEG
            # can take minutes to upload over mobile data otherwise
            uploads = [prepare_attachment(p) for p in attachments]
            params["attachments"] = uploads
        q = None
        if quote is not None:
            q = Quote(timestamp=quote.timestamp,
                      author_id=self.account if quote.outgoing else quote.author_id,
                      text=quote.preview)
            params.update(quoteTimestamp=q.timestamp, quoteAuthor=q.author_id,
                          quoteMessage=quote.body)
        # signal-cli uploads attachments before it answers; don't give up early
        # (a timeout here dropped the message locally although it was sent)
        res = self.rpc.call("send", params, timeout=600 if attachments else 60)
        ts = (res or {}).get("timestamp", 0)
        log.info("sent to %s: ts=%s, %d attachment(s)%s", conv_key, ts, len(uploads),
                 ", reply" if quote is not None else "")
        msg = Message(timestamp=ts, body=text, outgoing=True, status="sent", quote=q,
                      attachments=[Attachment(path=_keep_copy(up, ts), filename=os.path.basename(up),
                                              content_type=_guess_type(up), size=_size(up))
                                   for up in uploads])
        self.store.add_message(conv_key, msg, is_group=is_group_id(conv_key))  # reflect our own send
        return msg

    def react(self, conv_key: str, msg: Message, emoji: str, remove: bool = False) -> None:
        author = self.account if msg.outgoing else msg.author_id
        params = self._p(emoji=emoji, targetAuthor=author, targetTimestamp=msg.timestamp,
                         remove=remove or None)
        params.update(self._target(conv_key))
        self.rpc.call("sendReaction", params)
        self.store.set_reaction(conv_key, msg.timestamp, author or "", emoji, "", remove)

    def delete_for_everyone(self, conv_key: str, msg: Message) -> None:
        params = self._p(targetTimestamp=msg.timestamp)
        params.update(self._target(conv_key))
        self.rpc.call("remoteDelete", params)
        self.store.mark_deleted(conv_key, msg.timestamp, outgoing=True)

    def mark_read(self, conv_key: str) -> None:
        """Mark the chat read locally and send read receipts to each sender."""
        newly = self.store.mark_read(conv_key)
        by_author: dict[str, list[int]] = {}
        for author, ts in newly:
            if author:
                by_author.setdefault(author, []).append(ts)
        for author, stamps in by_author.items():
            try:
                self.rpc.call("sendReceipt", self._p(recipient=author, targetTimestamp=stamps,
                                                     type="read"))
            except RpcError:
                pass

    def typing(self, conv_key: str, stop: bool = False) -> None:
        params = self._p(stop=True if stop else None)
        params.update(self._target(conv_key))
        try:
            self.rpc.call("sendTyping", params, timeout=10)
        except RpcError:
            pass

    # -- calls (1:1 voice, via signal-cli + signal-call-tunnel) --------------
    def start_call(self, recipient: str) -> dict:
        info = self.rpc.call("startCall", self._p(recipient=recipient)) or {}
        if info.get("callId") is not None:
            self.store.record_call(str(info["callId"]), recipient, True,
                                   info.get("state") or "RINGING_OUTGOING")
        return info

    def accept_call(self, call_id) -> dict:
        return self.rpc.call("acceptCall", self._p(callId=call_id)) or {}

    def hangup_call(self, call_id) -> None:
        self.rpc.call("hangupCall", self._p(callId=call_id))

    def reject_call(self, call_id) -> None:
        self.rpc.call("rejectCall", self._p(callId=call_id))

    def consume_answer_token(self, call_id: str, token: Optional[str]) -> bool:
        return self.store.consume_answer_token(call_id, token)

    # -- daemon pushes -------------------------------------------------------
    def _on_notification(self, method: str, params: dict) -> None:
        if method == "callEvent":
            ev = call_event_from(params)
            record_call_event(self.store, ev)
            if self.on_call_event:
                self.on_call_event(ev)
            return
        if method != "receive":
            return
        for ev in parse_events(params):
            if isinstance(ev, TypingEvent):
                if self.on_typing:
                    self.on_typing(ev.conv_key, ev.who, ev.started)
                continue
            # the receiver applies the same event; store writes are idempotent
            self.store.apply_event(ev)
            if isinstance(ev, MessageEvent) and self.on_message:
                self.on_message(ev.conv_key, ev.message)


def call_event_from(params: dict) -> dict:
    """Unwrap a 'callEvent' notification. signal-cli sends
    {"subscription": N, "result": {callId, state, number, ...}}."""
    params = params or {}
    for key in ("result", "callEvent"):
        if isinstance(params.get(key), dict):
            return params[key]
    return params


def record_call_event(store: Store, ev: dict) -> None:
    """Keep the call log in step with a JsonCallEvent (shared with the receiver)."""
    if not ev or ev.get("callId") is None:
        return
    peer = ev.get("number") or ev.get("uuid") or ""
    if peer:
        store.record_call(str(ev["callId"]), peer, bool(ev.get("isOutgoing")),
                          ev.get("state") or "")


SENT_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "linal", "sent")


MAX_IMAGE_SIDE = 1600       # Android "standard" media quality
RESIZE_OVER_BYTES = 800_000


def prepare_attachment(path: str) -> str:
    """Return the file to upload: large JPEG/PNG/WebP photos are scaled to at
    most MAX_IMAGE_SIDE px (honouring EXIF rotation) and re-encoded as JPEG
    q85; anything else, or anything that fails, is sent as-is."""
    ctype = _guess_type(path)
    if ctype not in ("image/jpeg", "image/png", "image/webp"):
        return path
    try:
        if os.path.getsize(path) <= RESIZE_OVER_BYTES:
            return path
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(path)
        pb = pb.apply_embedded_orientation() or pb
        w, h = pb.get_width(), pb.get_height()
        scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
        if scale < 1.0:
            pb = pb.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        if pb.get_has_alpha():   # JPEG has no alpha; flatten onto white
            flat = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                        pb.get_width(), pb.get_height())
            flat.fill(0xFFFFFFFF)
            pb.composite(flat, 0, 0, pb.get_width(), pb.get_height(), 0, 0, 1, 1,
                         GdkPixbuf.InterpType.NEAREST, 255)
            pb = flat
        import tempfile
        os.makedirs(SENT_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(tempfile.mkdtemp(prefix=".upload-", dir=SENT_DIR), base + ".jpg")
        pb.savev(out, "jpeg", ["quality"], ["85"])
        return out
    except Exception as e:  # noqa: BLE001 — never block a send on resizing
        log.warning("couldn't resize %s, sending as-is: %s", os.path.basename(path), e)
        return path


def _keep_copy(path: str, ts: int) -> str:
    """Keep our own copy of a sent attachment, so the chat still shows it if
    the original file is moved or deleted. Falls back to the original path."""
    import shutil
    try:
        os.makedirs(SENT_DIR, exist_ok=True)
        dest = os.path.join(SENT_DIR, f"{ts}-{os.path.basename(path)}")
        if os.path.exists(dest):
            return dest
        tmp_dir = os.path.dirname(path)
        if os.path.basename(tmp_dir).startswith(".upload-") and \
                os.path.dirname(tmp_dir) == SENT_DIR:
            os.replace(path, dest)        # our resized copy: move, don't duplicate
            os.rmdir(tmp_dir)
        else:
            shutil.copy2(path, dest)
        return dest
    except OSError:
        return path


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _guess_type(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
