"""Adw.Application entry point."""
from __future__ import annotations
import os
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio  # noqa: E402
from .window import MainWindow
from . import logs
log = logs.get(__name__)


class LinalApp(Adw.Application):
    def __init__(self, backend, open_first=False):
        super().__init__(application_id="io.github.andy53.Linal",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.backend = backend
        self.open_first = open_first
        self.win = None

    def do_activate(self):
        if not self.win:
            from . import style
            style.install()
            # connect before building the window: its first reload() asks the
            # daemon whether we're registered, which needs a live connection
            connected = True
            try:
                self.backend.start()
            except Exception as e:  # noqa: BLE001
                log.warning("daemon not reachable at start (%s); starting services", e)
                connected = False
            self.win = MainWindow(self, self.backend, open_first_conv=self.open_first)
            # marshal backend pushes onto the GTK main loop
            self.backend.on_message = lambda key, msg: GLib.idle_add(self.win.incoming, key, msg)
            import threading
            self._reconnecting = False
            # the daemon can restart (package upgrade, crash): reconnect then
            self.backend.on_connection_change = \
                lambda up: None if up else GLib.idle_add(self._reconnect)
            if connected:
                self._after_connect()
            else:
                # first run after install: the user services are enabled for the
                # next login but not running yet, so start them and wait
                threading.Thread(target=self._start_services, daemon=True).start()
        self.win.present()

    def _after_connect(self):
        GLib.timeout_add_seconds(2, self.win.maybe_ask_profile_name)
        sync = getattr(self.backend, "sync_names", None)
        if sync:   # contact/profile names and group titles, off the UI thread
            import threading
            threading.Thread(target=lambda: _quiet(sync), daemon=True).start()

    def _reconnect(self):
        if self._reconnecting:
            return False
        self._reconnecting = True
        self.win.reload()                   # shows "Starting Signal…"
        import threading
        threading.Thread(target=self._start_services, daemon=True).start()
        return False

    def _start_services(self):
        import subprocess
        import time
        self._reconnecting = True
        sysd = ["systemctl", "--user"]
        from . import migrate
        migrate.stop_old_services()     # Signal-L5's daemon would hold the account lock
        # pick up unit files replaced by a package upgrade
        subprocess.run([*sysd, "daemon-reload"], capture_output=True, timeout=30)
        subprocess.run([*sysd, "start", *SERVICES], capture_output=True, timeout=30)
        deadline = time.time() + 120       # the JVM takes a while on the L5
        while time.time() < deadline:
            try:
                self.backend.start()
            except Exception:  # noqa: BLE001
                time.sleep(2)
                continue
            log.info("connected to the daemon")
            GLib.idle_add(self._connected_late)
            return
        log.error("daemon still unreachable after 120s")
        self._reconnecting = False
        GLib.idle_add(self.win.service_failed)

    def _connected_late(self):
        self._reconnecting = False
        self.win.reload()
        self._after_connect()
        return False

    def do_open(self, files, n_files, hint):
        self.do_activate()
        for f in files:
            uri = f.get_uri()
            if not uri or not self.win:
                continue
            import hashlib   # log a fingerprint, never the captcha token itself
            log.info("opened %s: link (%d chars, sha256 %s)", uri.split(':', 1)[0], len(uri),
                     hashlib.sha256(uri.encode()).hexdigest()[:12])
            if uri.startswith('signalcaptcha://'):
                self.win.handle_captcha(uri)
            elif uri.startswith('linal:'):   # from a notification
                self.win.handle_uri(uri)
        self.win.present()


SERVICES = ("linal-daemon.service", "linal-receiver.service")


def _setup_logging() -> None:
    detailed = False
    try:
        from .store import Store
        detailed = bool((Store().get_meta("prefs", {}) or {}).get("detailedLogs"))
    except Exception:  # noqa: BLE001 — logging must never stop the app starting
        pass
    logs.setup("app", detailed=detailed)


def _ensure_session_bus_env(environ=None) -> None:
    """When started by the systemd user manager (the receiver bringing the app
    up for a call, or a service) the environment can lack
    DBUS_SESSION_BUS_ADDRESS. GIO copes, but Firefox launched from here then
    can't find its running instance over D-Bus and reports "Firefox is already
    running, but is not responding". Fill in the standard per-user bus."""
    import os
    env = os.environ if environ is None else environ
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    bus = os.path.join(runtime, "bus")
    if os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"


def _quiet(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed: %s", getattr(fn, '__name__', 'task'), e)


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    _ensure_session_bus_env()
    from . import migrate
    migrate.move_data()             # first start after the Signal-L5 -> Linal rename
    _setup_logging()
    # Wayland app_id comes from the program name; match the .desktop file so
    # Phosh shows our icon/name for the window and its notifications
    GLib.set_prgname("io.github.andy53.Linal")
    use_mock = "--mock" in argv
    open_first = "--open-first" in argv
    if use_mock:
        from .mock import MockBackend
        backend = MockBackend()
    else:
        from .backend import RealBackend
        backend = RealBackend()
    app = LinalApp(backend, open_first=open_first)
    # Adw.Application.run parses args; strip our custom flag.
    clean = [a for a in argv if a not in ("--mock", "--open-first")]
    return app.run(clean)


if __name__ == "__main__":
    raise SystemExit(main())
