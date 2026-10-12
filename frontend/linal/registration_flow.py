"""Registration via a real browser + the signalcaptcha:// scheme handler.

number -> captcha in the default browser (GNOME Web on PureOS) -> the page
redirects to
signalcaptcha://TOKEN, which the OS routes back to this app (or the user pastes
the link) -> register (in the background) -> SMS code, auto-read from the
phone's messages -> optional registration-lock PIN -> profile name.

`reregister` is always set, so this also moves a number that is already
registered (on this device or another phone) here, like installing Signal on a
new phone.
"""
from __future__ import annotations
import subprocess
import sys
import threading
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib  # noqa: E402

from .registration import (CAPTCHA_URL, is_captcha_url, captcha_token,
                           parse_sms_code, normalize_number, device_number, needs_pin,
                           friendly_error)
from .sms import latest_sms_bodies
from . import logs
log = logs.get(__name__)

def open_captcha_page() -> None:
    """Open the captcha in the default browser (GNOME Web on PureOS). It hands
    the resulting signalcaptcha:// link straight back to us via the scheme
    handler, without the confirmation prompt Firefox shows."""
    Gio.AppInfo.launch_default_for_uri(CAPTCHA_URL, None)


def webkit_available() -> bool:
    """Is WebKitGTK 6.0 (for the in-app captcha window) installed? Checked via
    the typelib list, without loading WebKit into this process."""
    try:
        return "6.0" in gi.Repository.get_default().enumerate_versions("WebKit")
    except Exception:  # noqa: BLE001
        return False


def start_captcha_window(on_link) -> subprocess.Popen | None:
    """Show Signal's captcha in our own window (captcha_helper, a separate
    process) and call on_link(url) on the GLib main loop with the
    signalcaptcha:// link. The helper catches the link from the page's
    navigation itself, so it works whether the page redirects on its own or the
    user taps "Open Signal" -- a browser hand-off fails when the page redirects
    (GNOME Web then shows the link as a broken page). Returns None if the window
    can't be shown; the caller falls back to the browser."""
    import os
    if not webkit_available():
        return None
    env = dict(os.environ)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen([sys.executable, "-m", "linal.captcha_helper"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, env=env)
    except OSError as e:
        log.warning("captcha window failed to start: %s", e)
        return None

    def read():
        for line in proc.stdout:
            link = line.strip()
            if is_captcha_url(link):
                GLib.idle_add(on_link, link)
                break
        proc.wait()
        log.info("captcha window closed (exit %s)", proc.returncode)
    threading.Thread(target=read, daemon=True).start()
    log.info("captcha window opened")
    return proc


def restore_account_session() -> None:
    """Registering an already-loaded number makes signal-cli close that
    account's session ("Account is already in use, attempting to close
    existing manager"), and it stays closed if registration then fails or is
    abandoned: the account silently stops receiving messages and calls.
    Restarting the daemon reloads it; the app and the receiver reconnect on
    their own. The pending verification session is kept by signal-cli."""
    subprocess.run(["systemctl", "--user", "restart", "linal-daemon.service"],
                   capture_output=True, timeout=60)


def _qr_widget(text: str, size: int = 220):
    """A QR code for `text`, drawn directly (python3-qrcode for the matrix).
    Returns None when python3-qrcode isn't installed; the address is shown
    as text either way."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    def draw(_area, cr, w, h):
        n = len(matrix)
        cell = min(w, h) / n
        x0, y0 = (w - cell * n) / 2, (h - cell * n) / 2
        cr.set_source_rgb(1, 1, 1)               # always dark-on-light for scanners
        cr.rectangle(x0, y0, cell * n, cell * n)
        cr.fill()
        cr.set_source_rgb(0, 0, 0)
        for r, row in enumerate(matrix):
            for c, on in enumerate(row):
                if on:
                    cr.rectangle(x0 + c * cell, y0 + r * cell, cell + 0.5, cell + 0.5)
        cr.fill()

    area = Gtk.DrawingArea(content_width=size, content_height=size,
                           halign=Gtk.Align.CENTER)
    area.set_draw_func(draw)
    return area


def _run_bg(fn, done, error):
    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(error, e)
            return
        GLib.idle_add(done, res)
    threading.Thread(target=work, daemon=True).start()


class RegistrationController:
    def __init__(self, host, backend, on_done=None):
        self.host = host
        self.backend = backend
        self.on_done = on_done
        self.number = ""
        self._sms_stop = threading.Event()
        self._awaiting = False
        self._started_at = 0.0
        self._captcha_dlg = None
        self._busy_dlg = None

    # -- step 1: number --------------------------------------------------------
    def start(self):
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True,
                                heading="Register device",
                                body="Enter the phone number for this Signal account. "
                                     "If it's already on Signal on another phone, that "
                                     "phone will be signed out.")
        entry = Gtk.Entry(placeholder_text="+1 303 555 1234", input_purpose=Gtk.InputPurpose.PHONE)
        prefill = device_number()
        if prefill:
            entry.set_text(prefill)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("continue", "Continue")
        dlg.set_response_appearance("continue", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("continue")
        entry.connect("activate", lambda *_: dlg.response("continue"))
        dlg.connect("response", lambda _d, r: self._continue(entry.get_text())
                    if r == "continue" else None)
        dlg.present()

    # -- step 2: captcha -------------------------------------------------------
    def _continue(self, raw):
        self.number = normalize_number(raw)
        if len(self.number) < 8:
            self._error("Enter a valid phone number.")
            return
        self._ask_captcha()

    def _ask_captcha(self, note: str = ""):
        """Let the user pick where to solve the captcha. Solving it on another
        device is recommended: Signal usually refuses captchas solved in the
        Librem 5's own (slow) browser."""
        dlg = Adw.MessageDialog(
            transient_for=self.host, modal=True, heading="Solve a captcha",
            body=(note + "\n\n" if note else "") +
                 "Signal asks for a captcha to check you're a person. Captchas solved "
                 "on the Librem 5 itself are often refused, so solving it on a computer "
                 "or another phone on the same Wi-Fi works best.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("here", "On this phone")
        dlg.add_response("other", "On another device")
        dlg.set_response_appearance("other", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("other")
        dlg.set_close_response("cancel")

        def resp(_d, r):
            if r == "other":
                self._ask_captcha_other()
            elif r == "here":
                self._ask_captcha_here()
        dlg.connect("response", resp)
        dlg.present()

    def _ask_captcha_other(self):
        from .captcha_relay import CaptchaRelay
        self._awaiting = True
        relay = CaptchaRelay(lambda link: GLib.idle_add(self.handle_token, link))
        try:
            url = relay.start()
        except OSError as e:
            log.warning("captcha relay unavailable: %s", e)
            self._awaiting = False
            self._ask_captcha("Connect the phone to Wi-Fi first: the other device "
                              "reaches it over the local network.")
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        qr = _qr_widget(url)
        if qr is not None:
            box.append(qr)
        label = Gtk.Label(label=url, selectable=True, wrap=True)
        label.add_css_class("title-4")
        box.append(label)
        dlg = Adw.MessageDialog(
            transient_for=self.host, modal=True, heading="Solve it on another device",
            body="On a computer or phone connected to the same Wi-Fi, scan this code "
                 "or type the address into its browser, then follow the steps on that "
                 "page. Registration continues here by itself.")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("here", "Use this phone instead")
        dlg.set_close_response("cancel")

        def resp(_d, r):
            relay.stop()
            self._captcha_dlg = None
            if r == "here":
                self._ask_captcha_here()
            else:
                self._awaiting = False
        dlg.connect("response", resp)
        self._captcha_dlg = dlg
        dlg.present()

    def _ask_captcha_here(self, note: str = ""):
        self._awaiting = True
        window = start_captcha_window(self.handle_token)
        if window is None:
            open_captcha_page()
            where = ("A captcha opened in your browser. Solve it, then tap “Open Signal” "
                     "(allow it to open Linal if asked) — registration continues "
                     "automatically.")
        else:
            where = ("A captcha window opened. Solve it — registration continues "
                     "automatically once it's done.")
        dlg = Adw.MessageDialog(
            transient_for=self.host, modal=True, heading="Solve the captcha",
            body=(note + "\n\n" if note else "") + where +
                 "\n\nIf nothing happens, copy the “Open Signal” link and paste it here:")
        paste = Gtk.Entry(placeholder_text="signalcaptcha://…")
        dlg.set_extra_child(paste)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reopen", "Reopen captcha")
        dlg.add_response("use", "Use pasted link")
        dlg.set_response_appearance("use", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_close_response("cancel")

        def resp(_d, r):
            self._captcha_dlg = None
            if window is not None and window.poll() is None:
                window.terminate()             # done with it (cancel/reopen/paste)
            if r == "use":
                link = paste.get_text().strip()
                if not is_captcha_url(link):
                    self._ask_captcha_here("That isn't a signalcaptcha:// link.")
                else:
                    self.handle_token(link)
            elif r == "reopen":
                self._ask_captcha_here()
            else:
                self._awaiting = False
        dlg.connect("response", resp)
        self._captcha_dlg = dlg
        dlg.present()

    def handle_token(self, url):
        """Called with the signalcaptcha:// link (scheme handler or pasted)."""
        if not self._awaiting or not is_captcha_url(url):
            return
        self._awaiting = False
        if self._captcha_dlg is not None:      # the browser handed it over
            dlg, self._captcha_dlg = self._captcha_dlg, None
            dlg.close()
        try:
            token = captcha_token(url)
        except ValueError as e:
            self._ask_captcha(str(e))
            return
        import hashlib   # fingerprint only, never the token itself
        log.info("registering %s: captcha %d chars, sha256 %s", self.number, len(token),
                 hashlib.sha256(token.encode()).hexdigest()[:12])
        self._busy("Requesting a verification code…")
        self._started_at = time.time()
        _run_bg(lambda: self.backend.register(self.number, token, reregister=True),
                lambda _r: (self._unbusy(), self._show_verify()),
                self._register_failed)

    def _register_failed(self, e):
        self._unbusy()
        raw = str(e)
        log.error("register failed: %s", raw)
        self._restore()                 # get the account receiving again meanwhile
        msg = friendly_error(raw)
        # never reopen the browser on our own: that loops if Signal keeps
        # refusing. Show what Signal said; the user decides to retry.
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True,
                                heading="Registration didn't go through",
                                body=f"{msg}\n\nSignal said: {raw}" if msg != raw else raw)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("retry", "Try again")
        dlg.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: self._ask_captcha() if r == "retry" else None)
        dlg.present()

    # -- step 3: verify ----------------------------------------------------------
    def _show_verify(self):
        self._sms_stop.clear()
        self._vdlg = Adw.MessageDialog(transient_for=self.host, modal=True,
                                       heading="Enter verification code",
                                       body=f"Signal sent a code to {self.number}. "
                                            "It will be filled in automatically when the SMS "
                                            "arrives.")
        self._code = Gtk.Entry(placeholder_text="123456", input_purpose=Gtk.InputPurpose.DIGITS)
        self._vdlg.set_extra_child(self._code)
        self._vdlg.add_response("cancel", "Cancel")
        self._vdlg.add_response("verify", "Verify")
        self._vdlg.set_response_appearance("verify", Adw.ResponseAppearance.SUGGESTED)
        self._code.connect("activate", lambda *_: self._vdlg.response("verify"))
        handled = []

        def resp(dlg, r):
            # response() from code (Enter, SMS auto-fill) doesn't close the
            # dialog, and close() emits a second "close" response: act on the
            # first one only, so a code is never sent twice and a successful
            # verify is never followed by an abandon
            if handled:
                return
            handled.append(r)
            dlg.close()
            if r == "verify":
                self._verify(self._code.get_text().strip())
            else:
                self._abandon()
        self._vdlg.connect("response", resp)
        self._vdlg.present()
        self._watch_sms()

    def _watch_sms(self):
        since = int(self._started_at) - 5     # only codes sent for this attempt
        def worker():
            for _ in range(100):              # ~5 minutes
                if self._sms_stop.is_set():
                    return
                for body in latest_sms_bodies(limit=5, since=since):
                    code = parse_sms_code(body)
                    if code:
                        GLib.idle_add(self._autofill, code)
                        return
                self._sms_stop.wait(3)
        threading.Thread(target=worker, daemon=True).start()

    def _autofill(self, code):
        if self._sms_stop.is_set():
            return False
        self._code.set_text(code)
        self._vdlg.response("verify")       # closes the dialog and verifies once
        return False

    def _verify(self, code, pin=None):
        self._sms_stop.set()
        code = code.replace("-", "").replace(" ", "")
        if not code:
            self._show_verify()
            return
        self._busy("Verifying…")
        _run_bg(lambda: self.backend.verify(self.number, code, pin=pin) if pin
                else self.backend.verify(self.number, code),
                lambda _r: self._verified(),
                lambda e: self._verify_failed(e, code, pin))

    def _verify_failed(self, e, code, pin):
        self._unbusy()
        if needs_pin(str(e)) and not pin:
            self._ask_pin(code)
        else:
            log.error("verify failed: %s", e)
            self._restore()
            self._error(f"Verify failed: {friendly_error(str(e))}")

    def _ask_pin(self, code):
        """The number has a registration lock: Signal wants the account's PIN."""
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True, heading="Enter your Signal PIN",
                                body="This number has a registration lock. Enter the PIN you set "
                                     "in Signal on your previous phone.")
        entry = Gtk.PasswordEntry(show_peek_icon=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("verify", "Verify")
        dlg.set_response_appearance("verify", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: self._verify(code, entry.get_text().strip())
                    if r == "verify" else self._abandon())
        dlg.present()

    # -- step 4: start using the account, then the profile name ------------------
    def _verified(self):
        self._busy("Setting up…")
        _run_bg(self._adopt, lambda _r: (self._unbusy(), self._ask_name()),
                lambda e: (self._unbusy(), self._ask_name()))

    def _adopt(self):
        """Make the GUI and the background receiver use the new account: the
        daemon only pushes messages/call events for accounts a connection
        subscribed to, so both reconnect."""
        adopt = getattr(self.backend, "adopt_account", None)
        if adopt:
            adopt(self.number)
        subprocess.run(["systemctl", "--user", "restart", "linal-receiver.service"],
                       capture_output=True, timeout=20)

    def _ask_name(self):
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True, heading="Your profile",
                                body="Registered! Add your name — it's shown to people you "
                                     "message (and end-to-end encrypted).")
        entry = Gtk.Entry(placeholder_text="Your name")
        dlg.set_extra_child(entry)
        dlg.add_response("skip", "Skip")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        entry.connect("activate", lambda *_: dlg.response("save"))

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "save" and name:
                _run_bg(lambda: self.backend.set_profile_name(name),
                        lambda _r: self._toast("Profile saved"),
                        lambda e: self._toast(f"Couldn't save name: {e}"))
            self._finish()
        dlg.connect("response", resp)
        dlg.present()

    def _finish(self):
        self._toast("Signal is ready")
        if self.on_done:
            self.on_done()

    # -- helpers ---------------------------------------------------------------
    def _abandon(self):
        """Verification cancelled: stop watching SMS and bring the existing
        account back (signal-cli closed it when registration started)."""
        self._sms_stop.set()
        self._restore()

    def _restore(self):
        def work():
            try:
                restore_account_session()
            except Exception as e:  # noqa: BLE001
                log.error("couldn't restart the daemon: %s", e)
        threading.Thread(target=work, daemon=True).start()

    def _busy(self, text):
        self._unbusy()
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True, heading=text)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, width_request=32,
                              height_request=32)
        dlg.set_extra_child(spinner)
        self._busy_dlg = dlg
        dlg.present()

    def _unbusy(self):
        if self._busy_dlg is not None:
            dlg, self._busy_dlg = self._busy_dlg, None
            dlg.close()

    def _toast(self, text):
        if hasattr(self.host, "add_toast"):
            self.host.add_toast(Adw.Toast(title=text))

    def _error(self, text):
        dlg = Adw.MessageDialog(transient_for=self.host, modal=True,
                                heading="Registration", body=text)
        dlg.add_response("ok", "OK")
        dlg.present()
