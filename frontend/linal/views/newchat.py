"""New chat / new group pickers.

Sources: the phone's address book (phonebook.py), Signal contacts and groups
the account already knows, and any number typed into the search box. A number
is checked with Signal (getUserStatus) only when the user picks it — no bulk
upload of the address book.
"""
from __future__ import annotations
import re
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject  # noqa: E402

from ..registration import normalize_number
from ..phonebook import load_contacts


def looks_like_number(q: str) -> bool:
    digits = re.sub(r"\D", "", q or "")
    return len(digits) >= 7 and not re.search(r"[A-Za-z]", q or "")


def gather_entries(phone_contacts, signal_entries) -> list[dict]:
    """Merge address-book and Signal entries into picker rows, one per number,
    preferring the address-book name. Groups come first."""
    rows: list[dict] = []
    seen: set[str] = set()
    for c in signal_entries:
        if c.is_group:
            rows.append({"key": c.key, "title": c.title, "subtitle": "Group", "group": True})
            seen.add(c.key)
    for pc in phone_contacts:
        for n in pc.numbers:
            if n not in seen:
                rows.append({"key": n, "title": pc.name, "subtitle": n, "group": False})
                seen.add(n)
    for c in signal_entries:
        if not c.is_group and c.key not in seen:
            rows.append({"key": c.key, "title": c.title, "subtitle": c.key if c.key.startswith("+") else "Signal",
                         "group": False})
            seen.add(c.key)
    return rows


def filter_entries(rows: list[dict], query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return rows
    qd = re.sub(r"\D", "", q)
    out = []
    for r in rows:
        if q in r["title"].lower():
            out.append(r)
        elif qd and qd in re.sub(r"\D", "", r["subtitle"]):
            out.append(r)
    return out


class _Picker(Adw.Window):
    def __init__(self, parent, backend, title, multi=False):
        super().__init__(transient_for=parent, modal=True, title=title,
                         default_width=380, default_height=640)
        self.backend = backend
        self.multi = multi
        self.selected: dict[str, str] = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.header = Adw.HeaderBar()
        box.append(self.header)
        self.toasts = Adw.ToastOverlay()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.search = Gtk.SearchEntry(placeholder_text="Name or phone number",
                                      margin_start=12, margin_end=12, margin_top=6)
        self.search.connect("search-changed", lambda *_: self._refill())
        inner.append(self.search)
        self.top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.append(self.top)
        sc = Gtk.ScrolledWindow(vexpand=True)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.set_margin_start(12)
        self.list.set_margin_end(12)
        self.list.set_margin_bottom(12)
        sc.set_child(self.list)
        inner.append(sc)
        self.toasts.set_child(inner)
        box.append(self.toasts)
        self.set_content(box)
        self._rows: list[dict] = []
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            signal_entries = self.backend.list_contacts()
        except Exception:  # noqa: BLE001
            signal_entries = []
        rows = gather_entries(load_contacts(), signal_entries)
        if self.multi:
            rows = [r for r in rows if not r["group"]]
        GLib.idle_add(self._loaded, rows)

    def _loaded(self, rows):
        self._rows = rows
        self._refill()
        return False

    def _refill(self):
        child = self.list.get_first_child()
        while child:
            self.list.remove(child)
            child = self.list.get_first_child()
        q = self.search.get_text()
        if looks_like_number(q):
            n = normalize_number(q)
            if not any(r["key"] == n for r in self._rows):
                self.list.append(self._row({"key": n, "title": n, "subtitle": "Use this number",
                                            "group": False}))
        for r in filter_entries(self._rows, q)[:200]:
            self.list.append(self._row(r))
        if not self.list.get_first_child():
            self.list.append(Adw.ActionRow(title="No matches",
                                           subtitle="Type a full phone number to message it"))

    def _row(self, r):
        row = Adw.ActionRow(title=GLib.markup_escape_text(r["title"]),
                            subtitle=GLib.markup_escape_text(r["subtitle"]), activatable=True)
        av = Adw.Avatar(size=36, text=r["title"], show_initials=not r["group"])
        if r["group"]:
            av.set_icon_name("system-users-symbolic")
        row.add_prefix(av)
        if self.multi:
            chk = Gtk.CheckButton(active=r["key"] in self.selected, valign=Gtk.Align.CENTER)
            chk.connect("toggled", lambda c, r=r: self._toggle(r, c.get_active()))
            row.add_suffix(chk)
            row.set_activatable_widget(chk)
        else:
            row.connect("activated", lambda *_: self._pick(r))
        return row

    def _toggle(self, r, on):
        if on:
            self.selected[r["key"]] = r["title"]
        else:
            self.selected.pop(r["key"], None)
        self._selection_changed()

    def _selection_changed(self):
        pass

    def _pick(self, r):
        pass

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))


class NewChatDialog(_Picker):
    """Pick someone to message. Emits chosen(key, title)."""
    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str, str))}

    def __init__(self, parent, backend):
        super().__init__(parent, backend, "New chat")
        grp = Adw.ActionRow(title="New group", activatable=True, margin_start=12, margin_end=12)
        grp.add_prefix(Gtk.Image(icon_name="system-users-symbolic"))
        grp.add_css_class("card")
        grp.connect("activated", lambda *_: self._new_group(parent))
        self.top.append(grp)

    def _new_group(self, parent):
        self.close()
        dlg = NewGroupDialog(parent, self.backend)
        dlg.connect("created", lambda _d, gid, name: self.emit("chosen", gid, name))
        dlg.present()

    def _pick(self, r):
        if r["group"] or not r["key"].startswith("+"):
            self.emit("chosen", r["key"], r["title"])
            self.close()
            return
        self.toast(f"Checking {r['key']} on Signal…")

        def work():
            try:
                ok = self.backend.check_numbers([r["key"]]).get(r["key"], False)
                err = None
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e)
            GLib.idle_add(self._checked, r, ok, err)
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, r, ok, err):
        if ok:
            self.emit("chosen", r["key"], r["title"] if r["title"] != r["key"] else "")
            self.close()
        elif err:
            self.toast(f"Couldn't check: {err}")
        else:
            self.toast(f"{r['title']} isn't on Signal")
        return False


class NewGroupDialog(_Picker):
    """Name a group and pick members. Emits created(group_id, name)."""
    __gsignals__ = {"created": (GObject.SignalFlags.RUN_FIRST, None, (str, str))}

    def __init__(self, parent, backend):
        super().__init__(parent, backend, "New group", multi=True)
        self.name = Gtk.Entry(placeholder_text="Group name", margin_start=12, margin_end=12)
        self.top.append(self.name)
        self.create = Gtk.Button(label="Create", sensitive=False)
        self.create.add_css_class("suggested-action")
        self.create.connect("clicked", self._create)
        self.header.pack_end(self.create)
        self.name.connect("changed", lambda *_: self._selection_changed())

    def _selection_changed(self):
        self.create.set_sensitive(bool(self.selected) and bool(self.name.get_text().strip()))

    def _create(self, *_):
        name = self.name.get_text().strip()
        members = list(self.selected)
        self.create.set_sensitive(False)
        self.toast("Creating group…")

        def work():
            try:
                status = self.backend.check_numbers([m for m in members if m.startswith("+")])
                missing = [m for m in members if m.startswith("+") and not status.get(m, False)]
                ok = [m for m in members if m not in missing]
                gid = self.backend.create_group(name, ok) if ok else ""
                GLib.idle_add(self._done, gid, name, missing, None)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._done, "", name, [], str(e))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, gid, name, missing, err):
        if gid:
            self.emit("created", gid, name)
            if missing:
                self.get_transient_for().add_toast(
                    Adw.Toast(title=f"Not on Signal, left out: {', '.join(missing)}"))
            self.close()
        else:
            self.toast(err or "None of those people are on Signal")
            self._selection_changed()
        return False
