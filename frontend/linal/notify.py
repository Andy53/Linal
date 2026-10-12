"""Desktop notifications + ringing for the headless receiver.

Uses only Gio/GLib over D-Bus (no GTK), so it works inside the systemd user
service: org.freedesktop.Notifications for banners (Phosh shows these on the
lock screen too) and feedbackd (org.sigxcpu.Feedback) for the ringtone and
vibration of an incoming call. Tapping a notification opens the app at that
conversation via a linal: URI handled by the app's do_open.
"""
from __future__ import annotations
import threading
from typing import Callable, Optional
from . import logs
log = logs.get(__name__)

APP_ID = "io.github.andy53.Linal"
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
          "org.freedesktop.Notifications")
FEEDBACK = ("org.sigxcpu.Feedback", "/org/sigxcpu/Feedback", "org.sigxcpu.Feedback")


def conversation_uri(conv_key: str) -> str:
    from urllib.parse import quote
    return "linal:conv/" + quote(conv_key, safe="")


def call_uri(call_id: str, answer_token: Optional[str] = None) -> str:
    """Link that shows a call; with the receiver's one-time token it answers it.
    Any app or web page can open linal: links, and a caller knows their own
    call id, so answering must never be possible without the token."""
    from urllib.parse import quote
    uri = f"linal:call/{call_id}"
    return uri + ("?answer=" + quote(answer_token, safe="") if answer_token else "")


def parse_uri(uri: str) -> Optional[tuple[str, str, Optional[str]]]:
    """linal:conv/<key> or linal:call/<id>[?answer=<token>]
    -> (kind, value, answer token or None)."""
    from urllib.parse import unquote
    if not uri or not uri.startswith("linal:"):
        return None
    rest = uri[len("linal:"):].lstrip("/")
    kind, _, value = rest.partition("/")
    value, _, query = value.partition("?")
    token = None
    if query.startswith("answer="):
        token = unquote(query[len("answer="):].rstrip("/")) or None
    value = value.rstrip("/")      # GIO may append a slash
    if kind not in ("conv", "call") or not value:
        return None
    return kind, unquote(value), token


class Notifier:
    def __init__(self, on_reject: Optional[Callable[[str], None]] = None):
        self.on_reject = on_reject
        self._bus = None
        self._by_id: dict[int, tuple[str, str]] = {}   # notification id -> (kind, value)
        self._ring: dict[str, int] = {}                 # call id -> feedback id
        self._call_note: dict[str, int] = {}            # call id -> notification id
        self._conv_note: dict[str, int] = {}            # conv key -> notification id (replace, don't stack)
        self._answer_tokens: dict[str, str] = {}        # call id -> one-time answer token
        try:
            import gi
            from gi.repository import Gio, GLib
            self._Gio, self._GLib = Gio, GLib
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None,
                                       Gio.DBusSignalFlags.NONE, self._on_action, None)
            # a main loop so D-Bus signals (notification taps) are delivered
            threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log.error("no session bus, notifications disabled: %s", e)

    # -- low level -----------------------------------------------------------
    def _call(self, iface, method, args, reply="(u)"):
        if not self._bus:
            return None
        GLib = self._GLib
        try:
            res = self._bus.call_sync(iface[0], iface[1], iface[2], method, args,
                                      GLib.VariantType.new(reply) if reply else None,
                                      self._Gio.DBusCallFlags.NONE, 5000, None)
            return res.unpack()[0] if reply else None
        except Exception as e:  # noqa: BLE001
            log.warning("%s failed: %s", method, e)
            return None

    def _notify(self, summary, body, actions=(), replaces=0, urgency=1, category="",
                timeout=-1) -> int:
        V = self._GLib.Variant if self._bus else None
        if not self._bus:
            return 0
        hints = {"desktop-entry": V("s", APP_ID), "urgency": V("y", urgency)}
        # Phosh renders body markup: escape it so a sender can't style or add
        # links to what's shown
        body = self._GLib.markup_escape_text(body or "")
        if category:
            hints["category"] = V("s", category)
        nid = self._call(NOTIFY, "Notify", V("(susssasa{sv}i)", (
            "Linal", replaces, APP_ID, summary, body, list(actions), hints, timeout)))
        return nid or 0

    def _close(self, nid: int):
        if nid and self._bus:
            self._call(NOTIFY, "CloseNotification", self._GLib.Variant("(u)", (nid,)), reply=None)

    def _launch(self, uri: str):
        try:
            info = self._Gio.DesktopAppInfo.new(APP_ID + ".desktop")
            if info:
                info.launch_uris([uri], None)
        except Exception as e:  # noqa: BLE001
            log.error("launching the app failed: %s", e)

    def _on_action(self, _conn, _sender, _path, _iface, _signal, params, _data):
        nid, action = params.unpack()
        target = self._by_id.get(nid)
        if not target:
            return
        kind, value = target
        if kind == "conv":
            self._launch(conversation_uri(value))
        elif kind == "call":
            if action == "decline":
                self.stop_ringing(value)
                if self.on_reject:
                    self.on_reject(value)
            elif action == "answer":
                self._launch(call_uri(value, self._answer_tokens.pop(value, None)))
            else:
                self._launch(call_uri(value))

    # -- public --------------------------------------------------------------
    def message(self, conv_key: str, title: str, msg, unread: int = 1) -> None:
        body = msg.preview
        if unread > 1:
            body = f"{body}\n({unread} unread)"
        nid = self._notify(title, body, actions=["default", "Open"],
                           replaces=self._conv_note.get(conv_key, 0), category="im.received")
        if nid:
            self._conv_note[conv_key] = nid
            self._by_id[nid] = ("conv", conv_key)

    def knows_call(self, call_id: str) -> bool:
        """Already ringing/notified for this call."""
        return call_id in self._call_note or call_id in self._ring or call_id in self._answer_tokens

    def incoming_call(self, call_id: str, peer: str, name: str,
                      answer_token: Optional[str] = None) -> None:
        log.info("incoming call %s: ringing", call_id[-6:])
        if answer_token:
            self._answer_tokens[call_id] = answer_token
        nid = self._notify(name or peer, "Incoming Signal voice call",
                           actions=["answer", "Answer", "decline", "Decline", "default", "Open"],
                           urgency=2, category="call.incoming", timeout=0)
        if nid:
            self._call_note[call_id] = nid
            self._by_id[nid] = ("call", call_id)
        if self._bus and call_id not in self._ring:
            V = self._GLib.Variant
            fid = self._call(FEEDBACK, "TriggerFeedback",
                             V("(ssa{sv}i)", (APP_ID, "phone-incoming-call", {}, 60)))
            if fid:
                self._ring[call_id] = fid
        # like Android's full-screen call UI: bring the app up on the call page
        self._launch(call_uri(call_id))

    def stop_ringing(self, call_id: str) -> None:
        self._answer_tokens.pop(call_id, None)
        fid = self._ring.pop(call_id, None)
        if fid and self._bus:
            self._call(FEEDBACK, "EndFeedback", self._GLib.Variant("(u)", (fid,)), reply=None)
        self._close(self._call_note.pop(call_id, 0))

    def missed_call(self, peer: str, name: str) -> None:
        nid = self._notify(name or peer, "Missed Signal voice call", actions=["default", "Open"],
                           category="call.unanswered")
        if nid:
            self._by_id[nid] = ("conv", peer)
