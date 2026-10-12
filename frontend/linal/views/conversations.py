"""Conversation list (left pane of the leaflet): avatar, name, last message,
time and unread badge — like the Android chat list."""
from __future__ import annotations
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject  # noqa: E402


def short_time(ts_ms: int, now: float | None = None) -> str:
    """'14:05' today, 'Mon' this week, else '3 Oct'."""
    if not ts_ms:
        return ""
    now = now if now is not None else time.time()
    t = ts_ms / 1000
    lt, ln = time.localtime(t), time.localtime(now)
    if lt.tm_year == ln.tm_year and lt.tm_yday == ln.tm_yday:
        return time.strftime("%H:%M", lt)
    if now - t < 6 * 86400:
        return time.strftime("%a", lt)
    return time.strftime("%-d %b", lt)


class ConversationList(Gtk.Box):
    __gsignals__ = {
        "selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._stack = Gtk.Stack(vexpand=True)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-activated", self._on_row)
        scroller.set_child(self.listbox)
        self._stack.add_named(scroller, "list")
        self._stack.add_named(Adw.StatusPage(
            icon_name="mail-unread-symbolic", title="No chats yet",
            description="Tap + to message someone on Signal."), "empty")
        self.append(self._stack)

    def load(self, conversations):
        selected = self.listbox.get_selected_row()
        sel_key = getattr(selected, "conv_key", None)
        child = self.listbox.get_first_child()
        while child:
            self.listbox.remove(child)
            child = self.listbox.get_first_child()
        self._rows.clear()
        for conv in conversations:
            row = self._make_row(conv)
            self.listbox.append(row)
            if conv.key == sel_key:
                self.listbox.select_row(row)
        self._stack.set_visible_child_name("list" if conversations else "empty")

    def _make_row(self, conv):
        row = Gtk.ListBoxRow()
        row.conv_key = conv.key
        outer = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        avatar = Adw.Avatar(size=44, text=conv.title, show_initials=not conv.is_group)
        if conv.is_group:
            avatar.set_icon_name("system-users-symbolic")
        outer.append(avatar)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                      valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=6)
        title = Gtk.Label(xalign=0, label=conv.title, hexpand=True, ellipsize=3)
        title.add_css_class("heading")
        when = Gtk.Label(label=short_time(conv.last_ts))
        when.add_css_class("dim-label")
        when.add_css_class("caption")
        top.append(title)
        top.append(when)

        bottom = Gtk.Box(spacing=6)
        preview = Gtk.Label(xalign=0, label=conv.last_preview.replace("\n", " "),
                            ellipsize=3, hexpand=True)
        preview.add_css_class("dim-label" if not conv.unread else "caption-heading")
        preview.add_css_class("caption")
        bottom.append(preview)
        if conv.unread:
            badge = Gtk.Label(label=str(conv.unread) if conv.unread < 100 else "99+")
            badge.add_css_class("unread-badge")
            bottom.append(badge)
        box.append(top)
        box.append(bottom)
        outer.append(box)
        row.set_child(outer)
        self._rows[conv.key] = row
        return row

    def _on_row(self, _listbox, row):
        if row is not None:
            self.emit("selected", row.conv_key)
