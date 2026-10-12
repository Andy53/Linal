"""Settings: profile, privacy toggles and the Signal PIN (registration lock).

signal-cli has no getter for these account settings, so the toggles remember
the last value chosen here (store meta, default on — Signal's defaults).
"""
from __future__ import annotations
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

PREF_DEFAULTS = {"readReceipts": True, "typingIndicators": True, "linkPreviews": True,
                 "profileName": "", "detailedLogs": False}


def load_prefs(store) -> dict:
    prefs = dict(PREF_DEFAULTS)
    prefs.update(store.get_meta("prefs", {}) or {})
    return prefs


def save_prefs(store, prefs: dict) -> None:
    store.set_meta("prefs", prefs)
    # also in the app-wide settings, read at start-up before the account is
    # known (detailed logs)
    from ..store import Store, default_path
    if store.path != default_path():
        Store().set_meta("prefs", prefs)


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, parent, backend):
        super().__init__(transient_for=parent, modal=True, title="Settings")
        self.backend = backend
        store = getattr(backend, "store", None)
        self.prefs = load_prefs(store) if store else dict(PREF_DEFAULTS)
        page = Adw.PreferencesPage()

        prof = Adw.PreferencesGroup(title="Profile",
                                    description=f"Signed in as {getattr(backend, 'account', '') or ''}")
        self.name_row = Adw.EntryRow(title="Your name (shown to people you message)",
                                     show_apply_button=True, text=self.prefs.get("profileName", ""))
        self.name_row.connect("apply", self._apply_name)
        prof.add(self.name_row)
        page.add(prof)

        priv = Adw.PreferencesGroup(title="Privacy")
        for key, title, sub in (
                ("readReceipts", "Read receipts", "Let people see when you've read their messages"),
                ("typingIndicators", "Typing indicators", "Show and see when someone is typing"),
                ("linkPreviews", "Generate link previews", "")):
            row = Adw.ActionRow(title=title, subtitle=sub)
            sw = Gtk.Switch(active=bool(self.prefs.get(key, True)), valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s, _p, key=key: self._toggle(key, s.get_active()))
            row.add_suffix(sw)
            row.set_activatable_widget(sw)
            priv.add(row)
        page.add(priv)

        pin = Adw.PreferencesGroup(
            title="Signal PIN",
            description="A PIN protects your account: re-registering this number on another "
                        "device will require it (registration lock).")
        self.pin_row = Adw.PasswordEntryRow(title="New PIN (4+ digits)", show_apply_button=True)
        self.pin_row.connect("apply", self._apply_pin)
        pin.add(self.pin_row)
        rm = Adw.ActionRow(title="Remove PIN", activatable=True)
        rm.connect("activated", self._remove_pin)
        pin.add(rm)
        page.add(pin)
        sup = Adw.PreferencesGroup(
            title="Support",
            description="Logs stay on this phone and are never sent anywhere. Phone "
                        "numbers are shortened and message text is never logged.")
        det = Adw.ActionRow(title="Detailed logging",
                            subtitle="Turn on while tracking down a problem")
        sw = Gtk.Switch(active=bool(self.prefs.get("detailedLogs")), valign=Gtk.Align.CENTER)
        sw.connect("notify::active", lambda s, _p: self._set_detailed(s.get_active()))
        det.add_suffix(sw)
        det.set_activatable_widget(sw)
        sup.add(det)
        exp = Adw.ActionRow(title="Save logs for support", activatable=True,
                            subtitle="Writes one file to Downloads")
        exp.connect("activated", lambda *_: parent.export_logs(self) if hasattr(parent, "export_logs") else None)
        sup.add(exp)
        page.add(sup)

        acct = Adw.PreferencesGroup(
            title="Account",
            description="Logging out deletes everything of this account from the phone.")
        out = Adw.ActionRow(title="Log out", activatable=True,
                            subtitle="Stop using Signal on this phone")
        out.add_css_class("error")
        out.connect("activated", self._confirm_logout)
        acct.add(out)
        page.add(acct)
        self.add(page)

    def _bg(self, fn, ok_text):
        def work():
            try:
                fn()
                GLib.idle_add(self.add_toast, Adw.Toast(title=ok_text))
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.add_toast, Adw.Toast(title=f"Failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _save(self):
        store = getattr(self.backend, "store", None)
        if store:
            save_prefs(store, self.prefs)

    def _apply_name(self, row):
        name = row.get_text().strip()
        if not name:
            return
        self.prefs["profileName"] = name
        self._save()
        self._bg(lambda: self.backend.set_profile_name(name), "Profile name updated")

    def _set_detailed(self, on):
        from .. import logs
        self.prefs["detailedLogs"] = on
        self._save()
        logs.set_detailed(on)
        import subprocess   # the receiver reads the setting when it starts
        subprocess.run(["systemctl", "--user", "restart", "linal-receiver.service"],
                       capture_output=True, timeout=30)
        logs.get(__name__).info("detailed logging %s (app; receiver from its next start)",
                                "on" if on else "off")

    def _toggle(self, key, value):
        self.prefs[key] = value
        self._save()
        kw = {"readReceipts": "read_receipts", "typingIndicators": "typing_indicators",
              "linkPreviews": "link_previews"}[key]
        self._bg(lambda: self.backend.update_settings(**{kw: value}), "Saved")

    def _apply_pin(self, row):
        pin = row.get_text().strip()
        if len(pin) < 4:
            self.add_toast(Adw.Toast(title="PIN must be at least 4 characters"))
            return
        row.set_text("")
        self._bg(lambda: self.backend.set_pin(pin), "PIN set — remember it!")

    def _confirm_logout(self, *_):
        number = getattr(self.backend, "account", "") or "this account"
        dlg = Adw.MessageDialog(
            transient_for=self, modal=True, heading="Log out?",
            body=f"Signal stops delivering messages for {number} to this phone, and all "
                 "its messages, contacts, call history and attachments are deleted from "
                 "the phone. To use Signal here again, register a number.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("logout", "Log out and delete")
        dlg.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: self._logout() if r == "logout" else None)
        dlg.present()

    def _logout(self):
        parent = self.get_transient_for()

        def work():
            try:
                self.backend.logout()
            except Exception as e:  # noqa: BLE001 — nothing was deleted
                GLib.idle_add(self.add_toast, Adw.Toast(
                    title=f"Couldn't log out (nothing was deleted): {e}"))
                return
            import subprocess   # the receiver drops the account when it restarts
            subprocess.run(["systemctl", "--user", "restart", "linal-receiver.service"],
                           capture_output=True, timeout=30)

            def done():
                self.close()
                if parent is not None and hasattr(parent, "reload"):
                    parent.reload()
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _remove_pin(self, *_):
        self._bg(self.backend.remove_pin, "PIN removed")
