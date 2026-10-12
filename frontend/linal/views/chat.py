"""Chat view (right pane): message bubbles + composer, Android-style.

Bubbles show sender (groups), body, quoted reply, images/audio/files, time,
delivery ticks and reactions. Long-press (or right-click) a bubble for
react / reply / copy / delete-for-everyone. The composer supports attachments
and replies, and reports typing so the peer sees "typing…".
"""
from __future__ import annotations
import os
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Gio, GObject, GLib  # noqa: E402

QUICK_REACTIONS = ["❤️", "👍", "👎", "😂", "😮", "😢"]
_TICKS = {"sent": "✓", "delivered": "✓✓", "read": "✓✓"}


def fmt_duration(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def day_label(ts_ms: int, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    lt, ln = time.localtime(ts_ms / 1000), time.localtime(now)
    if (lt.tm_year, lt.tm_yday) == (ln.tm_year, ln.tm_yday):
        return "Today"
    y = time.localtime(now - 86400)
    if (lt.tm_year, lt.tm_yday) == (y.tm_year, y.tm_yday):
        return "Yesterday"
    return time.strftime("%a %-d %b" if lt.tm_year == ln.tm_year else "%-d %b %Y", lt)


def call_text(c) -> str:
    if c.missed:
        return "Missed voice call"
    if c.state != "ENDED":
        return "Voice call in progress"
    if not c.answered:
        return "Outgoing call · no answer" if c.outgoing else "Declined voice call"
    return ("Outgoing" if c.outgoing else "Incoming") + f" voice call · {fmt_duration(c.duration)}"


class ChatView(Gtk.Box):
    __gsignals__ = {
        "send": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),        # conv_key, text
        "react": (GObject.SignalFlags.RUN_FIRST, None, (str, object, str, bool)),  # key, msg, emoji, remove
        "delete": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),   # key, msg
        "typing": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),     # key, started
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.conv_key = None
        self.is_group = False
        self.reply_to = None             # Message being replied to
        self.pending_attachments: list[str] = []
        self.resolve_path = lambda att: att.path   # set by the window (backend lookup)
        self.name_for = lambda cid: cid            # set by the window
        self._last_day = None
        self._typing_sent = False
        self._typing_timer = 0

        self._scroller = Gtk.ScrolledWindow(vexpand=True)
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                             margin_top=12, margin_bottom=12, margin_start=8, margin_end=8)
        self._scroller.set_child(self._list)
        self.append(self._scroller)

        self.typing_label = Gtk.Label(xalign=0, visible=False, margin_start=14)
        self.typing_label.add_css_class("dim-label")
        self.typing_label.add_css_class("caption")
        self.append(self.typing_label)

        # reply preview bar
        self._reply_bar = Gtk.Box(spacing=6, visible=False, margin_start=8, margin_end=8)
        self._reply_bar.add_css_class("reply-bar")
        self._reply_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.set_reply(None))
        self._reply_bar.append(self._reply_label)
        self._reply_bar.append(close)
        self.append(self._reply_bar)

        # pending attachment chips
        self._att_bar = Gtk.FlowBox(visible=False, selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=8, margin_end=8)
        self.append(self._att_bar)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        attach = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Attach")
        attach.add_css_class("flat")
        attach.connect("clicked", self._on_attach)
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Message")
        self.entry.connect("activate", self._on_send)
        self.entry.connect("changed", self._on_changed)
        send_btn = Gtk.Button(icon_name="document-send-symbolic")
        send_btn.add_css_class("suggested-action")
        send_btn.add_css_class("circular")
        send_btn.connect("clicked", self._on_send)
        bar.append(attach)
        bar.append(self.entry)
        bar.append(send_btn)
        self.append(bar)

    # -- loading -------------------------------------------------------------
    def clear(self):
        child = self._list.get_first_child()
        while child:
            self._list.remove(child)
            child = self._list.get_first_child()
        self._last_day = None

    def show_conversation(self, conv, messages=None, calls=None):
        changed = conv.key != self.conv_key
        self.conv_key = conv.key
        self.is_group = conv.is_group
        if changed:
            self.set_reply(None)
            self.pending_attachments.clear()
            self._refresh_attachments()
            self.typing_label.set_visible(False)
        self.clear()
        items = [("m", m.timestamp, m) for m in (messages if messages is not None else conv.messages)]
        items += [("c", c.timestamp, c) for c in (calls or [])]
        for kind, _ts, obj in sorted(items, key=lambda x: x[1]):
            if kind == "m":
                self.add_message(obj, scroll=False)
            else:
                self.add_call(obj)
        GLib.idle_add(self._scroll_bottom)

    def _day_separator(self, ts):
        day = day_label(ts)
        if day != self._last_day:
            self._last_day = day
            lbl = Gtk.Label(label=day, margin_top=8, margin_bottom=4)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption-heading")
            self._list.append(lbl)

    def add_call(self, c):
        self._day_separator(c.timestamp)
        lbl = Gtk.Label(label=("📞 " if not c.missed else "☎️ ") + call_text(c) + "  ·  "
                        + time.strftime("%H:%M", time.localtime(c.timestamp / 1000)),
                        margin_top=4, margin_bottom=4)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        if c.missed:
            lbl.add_css_class("error")
        self._list.append(lbl)

    def add_message(self, m, scroll=True):
        self._day_separator(m.timestamp or int(time.time() * 1000))
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.set_halign(Gtk.Align.END if m.outgoing else Gtk.Align.START)
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("bubble")
        bubble.add_css_class("outgoing" if m.outgoing else "incoming")
        bubble.set_size_request(-1, -1)

        if self.is_group and not m.outgoing:
            who = Gtk.Label(xalign=0, label=m.author or self.name_for(m.author_id))
            who.add_css_class("caption-heading")
            who.add_css_class("accent")
            bubble.append(who)

        if m.quote and not m.deleted:
            qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            qbox.add_css_class("quote")
            qa = Gtk.Label(xalign=0, label=self.name_for(m.quote.author_id) or "Quote")
            qa.add_css_class("caption-heading")
            qt = Gtk.Label(xalign=0, label=m.quote.text or "Attachment", wrap=True,
                           max_width_chars=32, ellipsize=3, lines=2)
            qt.add_css_class("caption")
            qbox.append(qa)
            qbox.append(qt)
            bubble.append(qbox)

        if m.deleted:
            gone = Gtk.Label(xalign=0, label="🚫 This message was deleted")
            gone.add_css_class("dim-label")
            bubble.append(gone)
        else:
            for att in m.attachments:
                bubble.append(self._attachment_widget(att))
            if m.body:
                body = Gtk.Label(label=m.body, wrap=True, xalign=0, selectable=True,
                                 max_width_chars=34)
                body.set_wrap_mode(2)   # Pango.WrapMode.WORD_CHAR
                bubble.append(body)

        meta = Gtk.Box(spacing=4, halign=Gtk.Align.END)
        stamp = time.strftime("%H:%M", time.localtime((m.timestamp or time.time() * 1000) / 1000))
        meta_txt = ("edited · " if m.edited else "") + stamp
        ml = Gtk.Label(label=meta_txt)
        ml.add_css_class("caption")
        ml.add_css_class("dim-label")
        meta.append(ml)
        if m.pending:
            ml.set_label("Sending…")
        elif m.outgoing and m.status in _TICKS:
            tick = Gtk.Label(label=_TICKS[m.status])
            tick.add_css_class("caption")
            tick.add_css_class("accent" if m.status == "read" else "dim-label")
            meta.append(tick)
        bubble.append(meta)
        row.append(bubble)

        if m.reactions:
            rx = Gtk.Box(spacing=2, halign=Gtk.Align.END if m.outgoing else Gtk.Align.START)
            rx.add_css_class("reactions")
            for emoji, who in m.reactions.items():
                chip = Gtk.Label(label=emoji + (f" {len(who)}" if len(who) > 1 else ""))
                chip.set_tooltip_text(", ".join(self.name_for(w) if w else "You" for w in who))
                rx.append(chip)
            row.append(rx)

        if not m.deleted and not m.pending:
            self._attach_menu(bubble, m)
        self._list.append(row)
        if scroll:
            GLib.idle_add(self._scroll_bottom)

    def _attachment_widget(self, att):
        path = self.resolve_path(att)
        exists = bool(path) and os.path.exists(path)
        if att.is_image and exists:
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_can_shrink(True)
            pic.set_size_request(220, 160)
            pic.add_css_class("attachment-image")
            click = Gtk.GestureClick()
            click.connect("released", lambda *_: self._open_file(path))
            pic.add_controller(click)
            return pic
        if att.is_audio and exists:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            if att.voice_note:
                box.append(Gtk.Label(xalign=0, label="🎤 Voice message"))
            controls = Gtk.MediaControls(media_stream=Gtk.MediaFile.new_for_filename(path))
            controls.set_size_request(220, -1)
            box.append(controls)
            return box
        btn = Gtk.Button()
        btn.add_css_class("flat")
        inner = Gtk.Box(spacing=6)
        inner.append(Gtk.Image(icon_name="text-x-generic-symbolic" if exists
                               else "folder-download-symbolic"))
        size = f" · {att.size // 1024} KB" if att.size else ""
        inner.append(Gtk.Label(label=(att.filename or att.content_type or "Attachment") + size,
                               ellipsize=3, max_width_chars=24))
        btn.set_child(inner)
        btn.set_sensitive(exists)
        btn.connect("clicked", lambda *_: self._open_file(path))
        return btn

    def _open_file(self, path):
        if path and os.path.exists(path):
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)

    # -- per-message menu ----------------------------------------------------
    def _attach_menu(self, widget, m):
        def show(*_):
            self._show_menu(widget, m)
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", show)
        widget.add_controller(lp)
        rc = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        rc.connect("pressed", show)
        widget.add_controller(rc)

    def _show_menu(self, widget, m):
        pop = Gtk.Popover()
        pop.set_parent(widget)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4,
                      margin_bottom=4, margin_start=4, margin_end=4)
        mine = next((e for e, who in m.reactions.items() if "" in who), None)
        emojis = Gtk.Box(spacing=2)
        for e in QUICK_REACTIONS:
            b = Gtk.Button(label=e)
            b.add_css_class("flat")
            if e == mine:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, e=e: (pop.popdown(),
                                                  self.emit("react", self.conv_key, m, e, e == mine)))
            emojis.append(b)
        box.append(emojis)

        def item(label, cb):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (pop.popdown(), cb()))
            box.append(b)
        item("Reply", lambda: self.set_reply(m))
        if m.body:
            item("Copy text", lambda: widget.get_clipboard().set(m.body))
        if m.outgoing:
            item("Delete for everyone", lambda: self.emit("delete", self.conv_key, m))
        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # -- composer ------------------------------------------------------------
    def set_reply(self, m):
        self.reply_to = m
        if m is None:
            self._reply_bar.set_visible(False)
            return
        who = "You" if m.outgoing else (m.author or self.name_for(m.author_id))
        self._reply_label.set_markup(f"<b>Replying to {GLib.markup_escape_text(who)}</b>  "
                                     f"{GLib.markup_escape_text(m.preview[:80])}")
        self._reply_bar.set_visible(True)
        self.entry.grab_focus()

    def _on_attach(self, *_):
        chooser = Gtk.FileChooserNative(title="Attach file", transient_for=self.get_root(),
                                        action=Gtk.FileChooserAction.OPEN, select_multiple=True)

        def done(dlg, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                files = dlg.get_files()
                for i in range(files.get_n_items()):
                    p = files.get_item(i).get_path()
                    if p:
                        self.pending_attachments.append(p)
                self._refresh_attachments()
            dlg.destroy()
        chooser.connect("response", done)
        chooser.show()
        self._chooser = chooser   # keep alive

    def _refresh_attachments(self):
        child = self._att_bar.get_first_child()
        while child:
            self._att_bar.remove(child)
            child = self._att_bar.get_first_child()
        for p in self.pending_attachments:
            chip = Gtk.Box(spacing=4)
            chip.add_css_class("card")
            chip.append(Gtk.Label(label="📎 " + os.path.basename(p), ellipsize=3,
                                  max_width_chars=18, margin_start=6))
            x = Gtk.Button(icon_name="window-close-symbolic")
            x.add_css_class("flat")
            x.connect("clicked", lambda _b, p=p: (self.pending_attachments.remove(p),
                                                  self._refresh_attachments()))
            chip.append(x)
            self._att_bar.append(chip)
        self._att_bar.set_visible(bool(self.pending_attachments))

    def take_draft(self):
        """(text, attachments, reply_to) for the pending send; clears the composer."""
        text = self.entry.get_text().strip()
        draft = (text, list(self.pending_attachments), self.reply_to)
        self.entry.set_text("")
        self.pending_attachments.clear()
        self._refresh_attachments()
        self.set_reply(None)
        return draft

    def _on_changed(self, *_):
        if not self.conv_key:
            return
        if self.entry.get_text() and not self._typing_sent:
            self._typing_sent = True
            self.emit("typing", self.conv_key, True)
        if self._typing_timer:
            GLib.source_remove(self._typing_timer)
        self._typing_timer = GLib.timeout_add_seconds(4, self._typing_idle)

    def _typing_idle(self):
        self._typing_timer = 0
        if self._typing_sent and self.conv_key:
            self._typing_sent = False
            self.emit("typing", self.conv_key, False)
        return False

    def set_typing(self, who: str, started: bool):
        self.typing_label.set_label(f"{who} is typing…" if self.is_group else "typing…")
        self.typing_label.set_visible(started)

    def _scroll_bottom(self):
        adj = self._scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _on_send(self, *_):
        text = self.entry.get_text().strip()
        if (text or self.pending_attachments) and self.conv_key:
            if self._typing_timer:
                GLib.source_remove(self._typing_timer)
                self._typing_timer = 0
            self._typing_sent = False
            self.emit("send", self.conv_key, text)
