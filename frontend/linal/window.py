"""Main window: chat list + chat (Adw.Leaflet), with a full-screen call page.

Live updates come from the store's revision counter (the receiver and this
process both write to it); daemon pushes add typing indicators and call
events on top.
"""
from __future__ import annotations
import os
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib  # noqa: E402

from .views.conversations import ConversationList
from .views.chat import ChatView
from .views.call import CallPage
from .registration_flow import RegistrationController
from .models import Conversation
from . import logs
log = logs.get(__name__)


def _bg(fn, done=None, error=None):
    """Run fn() off the GTK thread; deliver result/exception back on it."""
    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if error:
                GLib.idle_add(error, e)
            else:
                log.exception("background call failed: %s", e)
            return
        if done:
            GLib.idle_add(done, res)
    threading.Thread(target=work, daemon=True).start()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend, open_first_conv=False):
        super().__init__(application=app, title="Linal",
                         default_width=400, default_height=720)
        self.backend = backend
        self._convs: dict[str, Conversation] = {}
        self._rev = -1
        self._typing_hide = 0
        self._pending: dict[str, list] = {}   # conv key -> messages still uploading
        self.call = None            # dict(id, peer, state, input, output) while a call is up
        from .callaudio import CallAudio
        self.audio = CallAudio()

        self.leaflet = Adw.Leaflet(can_navigate_back=True)
        self.leaflet.set_can_unfold(False)  # phone: always single-pane, navigable

        # left pane: header + conversation list
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_hb = Adw.HeaderBar()
        left_hb.set_title_widget(Gtk.Label(label="Signal"))
        new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New chat")
        new_btn.connect("clicked", lambda *_: self._new_chat())
        left_hb.pack_start(new_btn)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("New group", "win.new-group")
        menu.append("Settings", "win.settings")
        menu.append("Accounts / status", "win.status")
        menu.append("Save logs for support", "win.export-logs")
        menu.append("Register device…", "win.register")
        menu_btn.set_menu_model(menu)
        left_hb.pack_end(menu_btn)
        left.append(left_hb)
        self.conv_list = ConversationList()
        self.conv_list.connect("selected", self._on_selected)
        # swap between the chat list and a 'register' welcome page
        self.left_stack = Gtk.Stack(vexpand=True)
        self.left_stack.add_named(self.conv_list, "chats")
        self.left_stack.add_named(self._build_welcome(), "welcome")
        self._starting = Adw.StatusPage(title="Starting Signal…",
                                        description="Starting the Signal service. This takes "
                                                    "a few seconds the first time.",
                                        icon_name="io.github.andy53.Linal")
        self._starting.set_child(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                                             width_request=32, height_request=32))
        self.left_stack.add_named(self._starting, "starting")
        left.append(self.left_stack)
        left.set_size_request(280, -1)

        # right pane: header + chat
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.right_hb = Adw.HeaderBar()
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", visible=False)
        self.back_btn.connect("clicked", lambda *_: self._go_back())
        self.right_hb.pack_start(self.back_btn)
        self.right_title = Gtk.Label(label="Select a chat", ellipsize=3)
        self.right_hb.set_title_widget(self.right_title)
        conv_menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        cmenu = Gio.Menu()
        cmenu.append("Edit name", "win.edit-name")
        cmenu.append("Block", "win.block")
        conv_menu_btn.set_menu_model(cmenu)
        self.right_hb.pack_end(conv_menu_btn)
        self.call_btn = Gtk.Button(icon_name="call-start-symbolic", tooltip_text="Voice call")
        self.call_btn.connect("clicked", lambda *_: self._start_call())
        self.right_hb.pack_end(self.call_btn)
        right.append(self.right_hb)
        self.chat = ChatView()
        self.chat.resolve_path = self.backend.attachment_path
        self.chat.name_for = self.backend.display_name
        self.chat.connect("send", self._on_send)
        self.chat.connect("react", self._on_react)
        self.chat.connect("delete", self._on_delete)
        self.chat.connect("typing", lambda _w, key, started:
                          _bg(lambda: self.backend.typing(key, stop=not started)))
        right.append(self.chat)

        self._right = right
        self.leaflet.append(left)
        self.leaflet.append(right)
        self.leaflet.connect("notify::folded", self._on_fold)
        self.leaflet.connect("notify::visible-child", lambda *_: self._focus_changed())
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.leaflet)

        # root: normal UI, or the full-screen call page
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.add_named(self.toasts, "main")
        self.call_page = CallPage()
        self.call_page.connect("answer", lambda *_: self._answer_call())
        self.call_page.connect("decline", lambda *_: self._end_call(reject=True))
        self.call_page.connect("hangup", lambda *_: self._end_call())
        self.call_page.connect("mute", lambda _w, on: self.audio.set_muted(on))
        self.call_page.connect("speaker", lambda _w, on: self.audio.set_speaker(on))
        self.root_stack.add_named(self.call_page, "call")
        self.set_content(self.root_stack)

        backend.on_typing = lambda key, who, started: GLib.idle_add(self._typing, key, who, started)
        backend.on_call_event = lambda ev: GLib.idle_add(self._on_call_event, ev)
        self.connect("notify::is-active", lambda *_: self._focus_changed())

        self._install_actions(app)
        self._on_fold()
        self.reload()
        self._last_ts = self._safe_latest_ts()
        GLib.timeout_add_seconds(2, self._poll)
        if open_first_conv:
            self.open_first()

    def _build_welcome(self):
        page = Adw.StatusPage(title="Linal",
                              description="Register this phone number as a Signal device to start.",
                              icon_name="io.github.andy53.Linal")
        btn = Gtk.Button(label="Register device",
                         halign=Gtk.Align.CENTER,
                         css_classes=["suggested-action", "pill"])
        btn.connect("clicked", lambda *_: self._open_register())
        page.set_child(btn)
        return page

    def add_toast(self, toast):
        self.toasts.add_toast(toast)

    def _toast(self, text):
        self.add_toast(Adw.Toast(title=text))

    def _open_register(self):
        self._reg_controller = RegistrationController(self, self.backend, on_done=self._after_register)
        self._reg_controller.start()

    def handle_captcha(self, url):
        ctrl = getattr(self, '_reg_controller', None)
        if ctrl is not None:
            ctrl.handle_token(url)

    def _after_register(self):
        # called from the register flow on success; refresh the UI
        GLib.idle_add(self.reload)

    def _install_actions(self, app):
        for name, cb in (("register", lambda *_: self._open_register()),
                         ("status", self._show_status),
                         ("settings", lambda *_: self._open_settings()),
                         ("new-group", lambda *_: self._new_group()),
                         ("edit-name", lambda *_: self._edit_name()),
                         ("block", lambda *_: self._block()),
                         ("export-logs", lambda *_: self.export_logs())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

    # -- list ----------------------------------------------------------------
    def service_failed(self):
        self._starting.set_title("Signal service didn't start")
        self._starting.set_description("Check “journalctl --user -u linal-daemon”, "
                                       "or log out and back in.")
        self._starting.set_child(None)
        return False

    def reload(self):
        if not getattr(self.backend, "connected", True):
            self.left_stack.set_visible_child_name("starting")
            self.right_title.set_label("")
            return
        registered = True
        try:
            registered = self.backend.is_registered()
        except Exception:
            registered = True
        if not registered:
            self.left_stack.set_visible_child_name("welcome")
            self.right_title.set_label("Not registered")
            self._convs = {}
            return
        self.left_stack.set_visible_child_name("chats")
        convs = self.backend.list_conversations()
        self._convs = {c.key: c for c in convs}
        self.conv_list.load(convs)

    def open_first(self):
        convs = self.backend.list_conversations()
        if convs:
            self._on_selected(None, convs[0].key)
            GLib.idle_add(lambda: self.leaflet.set_visible_child(self._right))

    def open_conversation(self, key: str, title: str = ""):
        if key not in self._convs:
            start = getattr(self.backend, "start_conversation", None)
            conv = start(key, title) if start else Conversation(key=key, title=title or key)
            self.reload()
            self._convs.setdefault(key, conv)
        self._on_selected(None, key)

    def _on_selected(self, _w, key):
        conv = self._convs.get(key)
        if not conv:
            return
        self.right_title.set_label(conv.title)
        self.call_btn.set_visible(not conv.is_group)
        self._show_chat(conv)
        if self.leaflet.get_folded():
            self.leaflet.navigate(Adw.NavigationDirection.FORWARD)
        self._focus_changed()

    def _show_chat(self, conv):
        self.chat.show_conversation(conv, self.backend.get_messages(conv.key)
                                    + self._pending.get(conv.key, []),
                                    self.backend.get_calls(conv.key))

    def _go_back(self):
        self.leaflet.navigate(Adw.NavigationDirection.BACK)

    def _chat_visible(self) -> bool:
        return (self.chat.conv_key is not None and self.is_active()
                and (not self.leaflet.get_folded() or self.leaflet.get_visible_child() is self._right))

    def _focus_changed(self):
        """Tell the receiver which chat is on screen (no notifications for it)
        and mark it read."""
        key = self.chat.conv_key if self._chat_visible() else None
        self.backend.set_focus(key)
        if key and self._convs.get(key) and self._convs[key].unread:
            _bg(lambda: self.backend.mark_read(key))

    # -- sending -------------------------------------------------------------
    def _on_send(self, _w, key, _text):
        text, attachments, reply_to = self.chat.take_draft()
        # show it straight away; uploads over mobile data can take a while
        import time as _time
        from .models import Attachment, Message
        pending = Message(
            timestamp=int(_time.time() * 1000), body=text, outgoing=True, pending=True,
            attachments=[Attachment(path=p, filename=os.path.basename(p)) for p in attachments])
        self._pending.setdefault(key, []).append(pending)
        self.chat.add_message(pending)

        def settle():
            self._pending.get(key, []).remove(pending)
            self._refresh()

        def done(_msg):
            settle()

        def failed(e):
            log.error("send to %s failed: %s", key, e)
            self._toast(f"Send failed: {e}")
            settle()                              # drop the "Sending…" bubble
            if text and not self.chat.entry.get_text() and self.chat.conv_key == key:
                self.chat.entry.set_text(text)    # don't lose what was typed
        _bg(lambda: self.backend.send(key, text, attachments=attachments or None, quote=reply_to),
            done, failed)

    def _on_react(self, _w, key, msg, emoji, remove):
        _bg(lambda: self.backend.react(key, msg, emoji, remove=remove),
            lambda _r: self._refresh(), lambda e: self._toast(f"Reaction failed: {e}"))

    def _on_delete(self, _w, key, msg):
        _bg(lambda: self.backend.delete_for_everyone(key, msg),
            lambda _r: self._refresh(), lambda e: self._toast(f"Delete failed: {e}"))

    def _on_fold(self, *_):
        folded = self.leaflet.get_folded()
        self.back_btn.set_visible(folded)

    def _safe_latest_ts(self):
        try:
            return self.backend.revision()
        except Exception:
            return 0

    def _poll(self):
        rev = self._safe_latest_ts()
        if rev != self._last_ts:
            self._last_ts = rev
            self._refresh()
        if self._chat_visible():
            self.backend.set_focus(self.chat.conv_key)   # heartbeat for the receiver
        return True  # keep polling

    def _refresh(self):
        cur = self.chat.conv_key
        self.reload()
        if cur and cur in self._convs:
            self.right_title.set_label(self._convs[cur].title)
            self._show_chat(self._convs[cur])
            if self._chat_visible() and self._convs[cur].unread:
                _bg(lambda: self.backend.mark_read(cur))

    def incoming(self, conv_key, msg):
        """Called (on main loop) when the backend pushes a received message."""
        if self.chat.conv_key == conv_key:
            self.chat.set_typing("", False)
        self._refresh()

    def _typing(self, key, who, started):
        if key != self.chat.conv_key:
            return
        self.chat.set_typing(who, started)
        if self._typing_hide:
            GLib.source_remove(self._typing_hide)
            self._typing_hide = 0
        if started:   # peers re-send STARTED while typing; hide if they go quiet
            self._typing_hide = GLib.timeout_add_seconds(15, self._typing_timeout)

    def _typing_timeout(self):
        self._typing_hide = 0
        self.chat.set_typing("", False)
        return False

    # -- dialogs -------------------------------------------------------------
    def _new_chat(self):
        from .views.newchat import NewChatDialog
        dlg = NewChatDialog(self, self.backend)
        dlg.connect("chosen", lambda _d, key, title: self.open_conversation(key, title))
        dlg.present()

    def _new_group(self):
        from .views.newchat import NewGroupDialog
        dlg = NewGroupDialog(self, self.backend)
        dlg.connect("created", lambda _d, gid, name: self.open_conversation(gid, name))
        dlg.present()

    def _open_settings(self):
        from .views.settings import SettingsWindow
        SettingsWindow(self, self.backend).present()

    def _edit_name(self):
        key = self.chat.conv_key
        conv = self._convs.get(key) if key else None
        if not conv or conv.is_group:
            return
        dlg = Adw.MessageDialog(transient_for=self, modal=True, heading="Contact name",
                                body=f"Save a name for {key}")
        entry = Gtk.Entry(text=conv.title if conv.title != key else "")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "save" and name:
                _bg(lambda: self.backend.save_contact(key, name), lambda _r: self._refresh(),
                    lambda e: self._toast(f"Couldn't save: {e}"))
        dlg.connect("response", resp)
        dlg.present()

    def _block(self):
        key = self.chat.conv_key
        if not key:
            return
        dlg = Adw.MessageDialog(transient_for=self, modal=True, heading="Block?",
                                body="Blocked people can't message or call you.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("block", "Block")
        dlg.set_response_appearance("block", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "block" and _bg(
            lambda: self.backend.block(key), lambda _r: self._toast("Blocked"),
            lambda e: self._toast(f"Block failed: {e}")))
        dlg.present()

    def export_logs(self, parent=None):
        """Bundle the local logs into one file the user can choose to share."""
        from . import logs
        dl = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) \
            or os.path.expanduser("~/Downloads")
        info = {"account": getattr(self.backend, "account", "") or "(none)"}

        def done(path):
            dlg = Adw.MessageDialog(
                transient_for=parent or self, modal=True, heading="Logs saved",
                body=f"Saved to {path}\n\nThe file stays on this phone. Phone numbers are "
                     "shortened and message text is never included. Share it only if you "
                     "choose to, e.g. with support.")
            dlg.add_response("ok", "OK")
            dlg.add_response("open", "Show file")
            dlg.connect("response", lambda _d, r: r == "open" and Gio.AppInfo.launch_default_for_uri(
                Gio.File.new_for_path(os.path.dirname(path)).get_uri(), None))
            dlg.present()
        _bg(lambda: logs.export_bundle(dl, info), done,
            lambda e: self._toast(f"Couldn't save logs: {e}"))

    def maybe_ask_profile_name(self):
        """People see "Unknown" until the account has a profile name. Ask once
        it's connected and registered, if none was set through this app;
        "Not now" asks again a week later."""
        import time as _time
        from .views.settings import load_prefs, save_prefs
        store = getattr(self.backend, "store", None)
        if store is None or not self.backend.is_registered():
            return False
        prefs = load_prefs(store)
        if prefs.get("profileName") or _time.time() - prefs.get("nameAskedAt", 0) < 7 * 86400:
            return False
        dlg = Adw.MessageDialog(transient_for=self, modal=True, heading="Add your name",
                                body="People you message see you as “Unknown” until you set "
                                     "a profile name. It's shared end-to-end encrypted with "
                                     "people you talk to.")
        entry = Gtk.Entry(placeholder_text="Your name")
        dlg.set_extra_child(entry)
        dlg.add_response("later", "Not now")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        entry.connect("activate", lambda *_: dlg.response("save"))

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "save" and name:
                prefs["profileName"] = name
                save_prefs(store, prefs)
                _bg(lambda: self.backend.set_profile_name(name),
                    lambda _r: self._toast("Profile name saved"),
                    lambda e: self._toast(f"Couldn't save name: {e}"))
            else:
                prefs["nameAskedAt"] = _time.time()
                save_prefs(store, prefs)
        dlg.connect("response", resp)
        dlg.present()
        return False

    def _show_status(self, *_):
        try:
            accts = self.backend.list_accounts()
            body = "\n".join(a.number for a in accts) or "No accounts registered yet."
        except Exception as e:  # noqa: BLE001
            body = f"Backend error: {e}"
        dlg = Adw.MessageDialog(transient_for=self, modal=True,
                                heading="Accounts / status", body=body)
        dlg.add_response("ok", "OK")
        dlg.present()

    # -- links from notifications -------------------------------------------
    def handle_uri(self, uri: str):
        from .notify import parse_uri
        parsed = parse_uri(uri)
        if not parsed:
            return
        kind, value, token = parsed
        if kind == "conv":
            self.open_conversation(value)
        elif kind == "call":
            # Only the receiver's one-time token (from our own notification's
            # Answer button) may answer; any other app or web page can open
            # linal: links, and a caller knows their call id.
            answer = bool(token) and self.backend.consume_answer_token(value, token)
            self._resume_call(value, answer)

    def _resume_call(self, call_id: str, answer: bool):
        """Launched for an incoming call: look it up, show it, and answer only
        when the link carried a valid one-time token."""
        def found(calls):
            for c in calls or []:
                if str(c.get("callId")) == call_id:
                    self._on_call_event(c)
                    if answer and c.get("state") == "RINGING_INCOMING":
                        self._answer_call()
                    return False
            return False
        _bg(lambda: self.backend.rpc.call("listCalls", {"account": self.backend.account}),
            found)

    # -- calls ---------------------------------------------------------------
    def _start_call(self):
        key = self.chat.conv_key
        conv = self._convs.get(key) if key else None
        if not conv or conv.is_group or self.call:
            return
        self.call = {"id": None, "peer": key, "state": "RINGING_OUTGOING", "outgoing": True}
        self.call_page.setup(conv.title)
        self.call_page.set_state("CONNECTING")
        self.root_stack.set_visible_child_name("call")

        def started(info):
            info = dict(info or {})
            info.setdefault("number", key)
            info["isOutgoing"] = True
            self._on_call_event(info)
            return False

        def failed(e):
            log.error("starting a call failed: %s", e)
            self._toast(f"Call failed: {e}")
            self._call_finished()
            return False
        _bg(lambda: self.backend.start_call(key), started, failed)

    def _on_call_event(self, ev):
        if not ev or ev.get("callId") is None:
            return False
        cid = str(ev["callId"])
        state = ev.get("state") or ""
        log.info("call %s: %s (%s)%s", cid[-6:], state,
                 "outgoing" if ev.get("isOutgoing") else "incoming",
                 f" reason={ev.get('reason')}" if ev.get("reason") else "")
        peer = ev.get("number") or ev.get("uuid") or (self.call or {}).get("peer", "")
        if self.call and self.call.get("id") not in (None, cid):
            if state == "RINGING_INCOMING":   # busy with another call
                _bg(lambda: self.backend.reject_call(ev["callId"]))
            return False
        if self.call is None:
            if state == "ENDED":
                return False
            self.call = {"id": cid, "peer": peer, "outgoing": bool(ev.get("isOutgoing"))}
            self.call_page.setup(self.backend.display_name(peer) or peer)
            self.present()
        self.call["id"] = cid
        self.call["state"] = state
        for k in ("inputDeviceName", "outputDeviceName"):
            if ev.get(k):
                self.call[k] = ev[k]
        self.call_page.set_state(state)
        self.root_stack.set_visible_child_name("call")
        if state == "CONNECTED":
            self.audio.start(self.call.get("inputDeviceName", ""),
                             self.call.get("outputDeviceName", ""))
        elif state == "ENDED":
            self.audio.stop()
            GLib.timeout_add(1500, self._call_finished)
        return False

    def _answer_call(self):
        if not self.call or not self.call.get("id"):
            return

        def answered(info):
            if info:
                info = dict(info)
                info.setdefault("number", self.call["peer"] if self.call else "")
                self._on_call_event(info)
            return False
        self.call_page.set_state("CONNECTING")
        _bg(lambda: self.backend.accept_call(int(self.call["id"])), answered,
            lambda e: self._toast(f"Couldn't answer: {e}"))

    def _end_call(self, reject=False):
        if not self.call:
            self._call_finished()
            return
        cid = self.call.get("id")
        self.audio.stop()
        if cid:
            fn = self.backend.reject_call if reject else self.backend.hangup_call
            _bg(lambda: fn(int(cid)), None, lambda e: log.error("ending the call failed: %s", e))
        self.call_page.set_state("ENDED")
        GLib.timeout_add(1200, self._call_finished)

    def _call_finished(self):
        self.audio.stop()
        self.call = None
        self.root_stack.set_visible_child_name("main")
        self._refresh()
        return False
