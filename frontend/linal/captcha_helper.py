"""Standalone captcha helper (separate process).

On the Librem 5, a WebKit WebView only composites in an app's *first* window, so
the main app spawns this tiny app: its single window hosts the captcha webview,
and when the page redirects to signalcaptcha://TOKEN we print the URL to stdout
and exit. The parent reads stdout and completes registration.
"""
from __future__ import annotations
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Adw, WebKit, GLib, Gio  # noqa: E402

from .registration import CAPTCHA_URL, is_captcha_url


class CaptchaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.andy53.Linal.Captcha",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._emitted = False

    def do_activate(self):
        win = Adw.ApplicationWindow(application=self, title="Signal captcha",
                                    default_width=400, default_height=720)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hb = Adw.HeaderBar()
        hb.set_title_widget(Gtk.Label(label="Solve the captcha"))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.quit())
        hb.pack_start(cancel)
        box.append(hb)
        self.wv = WebKit.WebView(vexpand=True, hexpand=True)
        self.wv.connect("decide-policy", self._on_decide)
        box.append(self.wv)
        win.set_content(box)
        win.present()
        self.wv.load_uri(CAPTCHA_URL)

    def _on_decide(self, _wv, decision, dtype):
        try:
            uri = decision.get_navigation_action().get_request().get_uri()
        except Exception:
            return False
        if is_captcha_url(uri):
            decision.ignore()
            if not self._emitted:
                self._emitted = True
                sys.stdout.write(uri + "\n")
                sys.stdout.flush()
                GLib.idle_add(self.quit)
            return True
        return False


def main():
    return CaptchaApp().run([])  # ignore argv


if __name__ == "__main__":
    raise SystemExit(main())
