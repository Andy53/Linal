"""Best-effort GUI construction smoke test. Skips cleanly when no display /
GTK is available so the suite stays green in headless CI or over SSH."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gtk_available():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    try:
        import gi
        gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
        return Gtk.init_check() if False else True  # import success is enough
    except Exception:
        return False


@unittest.skipUnless(_gtk_available(), "no display / GTK4 unavailable")
class GuiSmokeTest(unittest.TestCase):
    def test_window_builds_with_mock(self):
        import gi
        gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
        from gi.repository import Adw
        from linal.mock import MockBackend
        from linal.window import MainWindow
        Adw.init()
        app = Adw.Application(application_id="io.github.andy53.Linal.test")
        app.register(None)   # emit startup so adding a window is valid
        win = MainWindow(app, MockBackend())
        # conversations loaded into the list model
        self.assertEqual(len(win._convs), 3)
        win.open_first()
        self.assertIsNotNone(win.chat.conv_key)


    def test_unregistered_shows_welcome(self):
        import gi
        gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
        from gi.repository import Adw
        from linal.mock import MockBackend
        from linal.window import MainWindow
        class Unreg(MockBackend):
            def is_registered(self):
                return False
        Adw.init()
        app = Adw.Application(application_id="io.github.andy53.Linal.test2")
        app.register(None)
        win = MainWindow(app, Unreg())
        self.assertEqual(win.left_stack.get_visible_child_name(), "welcome")

if __name__ == "__main__":
    unittest.main()
