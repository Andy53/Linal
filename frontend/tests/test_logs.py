"""Local support logging: private files, redaction, export bundle."""
import logging, os, stat, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal import logs


class RedactTest(unittest.TestCase):
    def test_redacts_identifiers(self):
        line = ("call from +13035550101 uuid 374f7df2-32aa-44ba-b2f6-5bf4f7393eff "
                "captcha signalcaptcha://signal-hcaptcha.5fad.registration.P1_eyJhbGciOiJIUzI1NiIs "
                "token Ab_cdefghijklmnopqrstuvwxyz0123456789ABCDEFGH done")
        out = logs.redact(line)
        self.assertIn("+…0101", out)
        self.assertNotIn("3035550101", out)
        self.assertIn("374f7df2-…", out)
        self.assertNotIn("5bf4f7393eff", out)
        self.assertIn("signalcaptcha://[redacted]", out)
        self.assertNotIn("P1_eyJ", out)
        self.assertIn("[redacted:45]", out)
        self.assertTrue(out.endswith("done"))

    def test_keeps_ordinary_text(self):
        for s in ("rpc send failed after 1.2s: [429] Rate Limited",
                  "/usr/lib/linal/bin/signal-call-tunnel", "call 001401: CONNECTED"):
            self.assertEqual(logs.redact(s), s)


class FileLoggingTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.d.name, "logs")
        self._saved = (logs._configured, sys.excepthook)
        root = logging.getLogger("linal")
        self._handlers = list(root.handlers)
        logs._configured = None

    def tearDown(self):
        root = logging.getLogger("linal")
        for h in list(root.handlers):
            if h not in self._handlers:
                root.removeHandler(h)
                h.close()
        logs._configured, sys.excepthook = self._saved
        self.d.cleanup()

    def test_private_files_redacted_with_tracebacks(self):
        logs.setup("app", directory=self.dir)
        log = logs.get("linal.test")
        log.info("sent to %s", "+13035550101")
        try:
            raise RuntimeError("boom for +13035550199")
        except RuntimeError:
            log.exception("failed")
        for h in logging.getLogger("linal").handlers:
            h.flush()
        path = os.path.join(self.dir, "app.log")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = open(path).read()
        from linal import __version__
        self.assertIn(f"app {__version__} starting", text)
        self.assertIn("sent to +…0101", text)
        self.assertIn("RuntimeError: boom for +…0199", text)    # traceback kept, redacted
        self.assertNotIn("3035550101", text)

    def test_detailed_toggles_debug(self):
        logs.setup("app", directory=self.dir)
        root = logging.getLogger("linal")
        self.assertEqual(root.level, logging.INFO)
        logs.set_detailed(True)
        self.assertEqual(root.level, logging.DEBUG)
        logs.set_detailed(False)

    def test_export_bundle(self):
        logs.setup("app", directory=self.dir)
        logs.get("x").warning("problem with +13035550101")
        open(os.path.join(self.dir, "receiver.log"), "w").write("receiver line\n")
        for h in logging.getLogger("linal").handlers:
            h.flush()
        out = os.path.join(self.d.name, "Downloads")
        path = logs.export_bundle(out, {"account": "+13035550142"}, directory=self.dir,
                                  journal_since="-1min")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = open(path).read()
        for part in ("Linal support log", "===== app.log =====", "===== receiver.log =====",
                     "receiver line", "problem with +…0101", "account: +…0142", "daemon journal"):
            self.assertIn(part, text)
        self.assertNotIn("3035550142", text)


class DuplicateRingTest(unittest.TestCase):
    def test_second_ringing_event_keeps_first_token(self):
        from linal.store import Store
        from linal import receiver

        class N:
            def __init__(self): self.rings, self.tokens = [], {}
            def knows_call(self, cid): return cid in self.tokens
            def incoming_call(self, cid, peer, name, answer_token=None):
                self.rings.append(cid); self.tokens[cid] = answer_token
            def stop_ringing(self, cid): pass
            def missed_call(self, peer, name): pass
        s, n = Store(":memory:"), N()
        ev = {"callId": 42, "state": "RINGING_INCOMING", "number": "+1888"}
        receiver.handle_call_event(s, ev, n)
        receiver.handle_call_event(s, dict(ev, inputDeviceName="signal_input_42"), n)
        self.assertEqual(n.rings, ["42"])                          # rang once
        self.assertTrue(s.consume_answer_token("42", n.tokens["42"]))  # its token still valid


if __name__ == "__main__":
    unittest.main()
