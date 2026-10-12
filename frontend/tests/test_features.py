"""Phone book parsing, notification links, call audio routing, receiver
notification rules and new-chat search — the pieces around the core."""
import os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.phonebook import parse_vcard, load_contacts
from linal.notify import conversation_uri, call_uri, parse_uri
from linal.callaudio import CallAudio, tunnel_endpoints
from linal.store import Store
from linal import receiver

VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nTEL;TYPE=CELL:\r\n"
         "TEL;X-EVOLUTION-E164=3035550101,;TYPE=CELL:(303) 555-0101\r\n"
         "TEL;TYPE=WORK:+44 20 7946 0958\r\nFN:Jane Q\\, Public\r\nN:Public;Jane;;;\r\n"
         "NOTE:\r\nEND:VCARD")


class PhonebookTest(unittest.TestCase):
    def test_parse_vcard(self):
        c = parse_vcard(VCARD)
        self.assertEqual(c.name, "Jane Q, Public")
        self.assertEqual(c.numbers, ["+13035550101", "+442079460958"])

    def test_no_number_no_contact(self):
        self.assertIsNone(parse_vcard("BEGIN:VCARD\r\nFN:Nobody\r\nTEL:\r\nEND:VCARD"))

    def test_folded_lines(self):
        c = parse_vcard("BEGIN:VCARD\r\nFN:Very Long\r\n  Name\r\nTEL:3035550199\r\nEND:VCARD")
        self.assertEqual(c.name, "Very Long Name")

    def test_load_from_eds_layout(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "system"))
            os.makedirs(os.path.join(d, "trash"))
            for book in ("system", "trash"):
                c = sqlite3.connect(os.path.join(d, book, "contacts.db"))
                c.execute("CREATE TABLE folder_id(uid TEXT, vcard TEXT)")
                c.execute("INSERT INTO folder_id VALUES('1', ?)", (VCARD,))
                c.execute("INSERT INTO folder_id VALUES('2', 'BEGIN:VCARD\r\nFN:Abe\r\nTEL:3035550100\r\nEND:VCARD')")
                c.commit()
                c.close()
            got = load_contacts(d)
            self.assertEqual([x.name for x in got], ["Abe", "Jane Q, Public"])   # trash skipped, sorted
        self.assertEqual(load_contacts("/nonexistent"), [])


class NotifyUriTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(parse_uri(conversation_uri("+13035550101")), ("conv", "+13035550101", None))
        gid = "ab/cd+ef=="
        self.assertEqual(parse_uri(conversation_uri(gid)), ("conv", gid, None))
        self.assertEqual(parse_uri(call_uri("77", "tok_-en")), ("call", "77", "tok_-en"))
        self.assertEqual(parse_uri(call_uri("77")), ("call", "77", None))
        # the old bare "?answer" form carries no token, so it can't answer
        self.assertEqual(parse_uri("linal:call/77?answer"), ("call", "77", None))
        # GIO's rewrite (linal:///..., trailing slash) keeps the token intact
        self.assertEqual(parse_uri("linal:///call/77?answer=tok/"), ("call", "77", "tok"))

    def test_rejects_garbage(self):
        for bad in ("", "signalcaptcha://x", "linal:", "linal:nope/1", "linal:conv/"):
            self.assertIsNone(parse_uri(bad))


class FakeRun:
    def __init__(self):
        self.calls = []
        self._mod = 100

    def __call__(self, args, **kw):
        self.calls.append(args)

        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        r = R()
        if args[:2] == ["pactl", "get-default-source"]:
            r.stdout = "mic0\n"
        elif args[:2] == ["pactl", "get-default-sink"]:
            r.stdout = "spk0\n"
        elif args[:3] == ["pactl", "load-module", "module-loopback"]:
            self._mod += 1
            r.stdout = f"{self._mod}\n"
        return r


class CallAudioTest(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(tunnel_endpoints("signal_input_7", "signal_output_7"),
                         ("sink_for_signal_input_7", "signal_output_7.monitor"))

    def test_bridges_and_unloads(self):
        run = FakeRun()
        a = CallAudio(run=run)
        a._callaudiod = lambda *args: False          # no callaudiod in tests
        self.assertTrue(a.start("signal_input_7", "signal_output_7"))
        loops = [c for c in run.calls if c[:3] == ["pactl", "load-module", "module-loopback"]]
        self.assertIn("source=mic0", loops[0])
        self.assertIn("sink=sink_for_signal_input_7", loops[0])
        self.assertIn("source=signal_output_7.monitor", loops[1])
        self.assertIn("sink=spk0", loops[1])
        self.assertTrue(a.start("x", "y"))           # idempotent while active
        self.assertEqual(len([c for c in run.calls if "module-loopback" in c]), 2)
        a.stop()
        self.assertEqual([c for c in run.calls if c[:2] == ["pactl", "unload-module"]],
                         [["pactl", "unload-module", "101"], ["pactl", "unload-module", "102"]])
        self.assertFalse(a.active)

    def test_needs_device_names(self):
        a = CallAudio(run=FakeRun())
        a._callaudiod = lambda *args: False
        self.assertFalse(a.start("", "signal_output_7"))


class FakeNotifier:
    def __init__(self):
        self.messages, self.rings, self.stopped, self.missed = [], [], [], []

    def message(self, key, title, msg, unread=1):
        self.messages.append((key, title, msg.body, unread))

    def incoming_call(self, cid, peer, name, answer_token=None):
        self.rings.append((cid, peer, name))
        self.tokens = getattr(self, "tokens", {})
        self.tokens[cid] = answer_token

    def stop_ringing(self, cid):
        self.stopped.append(cid)

    def missed_call(self, peer, name):
        self.missed.append(peer)


class ReceiverTest(unittest.TestCase):
    def env(self, ts, body, src="+1888", name="Bob"):
        return {"envelope": {"timestamp": ts, "sourceNumber": src, "sourceName": name,
                             "dataMessage": {"message": body}}}

    def test_notifies_new_incoming_once(self):
        s, n = Store(":memory:"), FakeNotifier()
        receiver.handle_receive(s, self.env(10, "hi"), n)
        receiver.handle_receive(s, self.env(10, "hi"), n)      # duplicate delivery
        self.assertEqual(n.messages, [("+1888", "Bob", "hi", 1)])
        receiver.handle_receive(s, self.env(20, "again"), n)
        self.assertEqual(n.messages[-1][3], 2)                 # unread count shown

    def test_no_notification_for_open_chat(self):
        s, n = Store(":memory:"), FakeNotifier()
        s.set_focus("+1888")
        receiver.handle_receive(s, self.env(10, "hi"), n)
        self.assertEqual(n.messages, [])
        self.assertEqual(len(s.get_messages("+1888")), 1)       # still stored

    def test_call_ring_and_missed(self):
        s, n = Store(":memory:"), FakeNotifier()
        receiver.handle_call_event(s, {"callId": 9, "state": "RINGING_INCOMING", "number": "+1888"}, n)
        self.assertEqual(n.rings, [("9", "+1888", "+1888")])
        receiver.handle_call_event(s, {"callId": 9, "state": "ENDED", "number": "+1888"}, n)
        self.assertEqual(n.stopped, ["9"])
        self.assertEqual(n.missed, ["+1888"])

    def test_ring_issues_one_time_answer_token(self):
        s, n = Store(":memory:"), FakeNotifier()
        receiver.handle_call_event(s, {"callId": 9, "state": "RINGING_INCOMING", "number": "+1888"}, n)
        tok = n.tokens["9"]
        self.assertTrue(tok and len(tok) >= 24)
        self.assertFalse(s.consume_answer_token("9", "guess"))     # wrong token...
        self.assertFalse(s.consume_answer_token("9", tok))         # ...also burned it

    def test_answered_call_not_missed(self):
        s, n = Store(":memory:"), FakeNotifier()
        for st in ("RINGING_INCOMING", "CONNECTED", "ENDED"):
            receiver.handle_call_event(s, {"callId": 3, "state": st, "number": "+1888"}, n)
        self.assertEqual(n.missed, [])


class AnswerTokenTest(unittest.TestCase):
    """A linal:call link can only answer with the receiver's token."""
    def test_single_use(self):
        s = Store(":memory:")
        tok = s.issue_answer_token("5")
        self.assertTrue(s.consume_answer_token("5", tok))
        self.assertFalse(s.consume_answer_token("5", tok))          # replay

    def test_rejects_missing_wrong_other_call_and_expired(self):
        s = Store(":memory:")
        self.assertFalse(s.consume_answer_token("5", None))         # never issued
        tok = s.issue_answer_token("5")
        self.assertFalse(s.consume_answer_token("6", tok))         # other call
        self.assertFalse(s.consume_answer_token("5", None))
        tok = s.issue_answer_token("7")
        s.set_meta("answer-token:7", {"t": tok, "at": 0})          # issued long ago
        self.assertFalse(s.consume_answer_token("7", tok))


class DaemonUnitTest(unittest.TestCase):
    """The daemon must not listen on TCP or log message text."""
    def test_unit_file(self):
        unit = open(os.path.join(os.path.dirname(__file__), "..", "..", "backend",
                                 "linal-daemon.service")).read()
        exec_line = [l for l in unit.splitlines() if l.startswith("ExecStart=")][0]
        self.assertIn("--socket %t/linal/socket", exec_line)
        self.assertNotIn("--tcp", exec_line)
        self.assertNotIn("--http", exec_line)
        self.assertIn("StandardOutput=null", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)


class ReceiverWireFormatTest(unittest.TestCase):
    def test_incoming_call_rings_from_wrapped_event(self):
        from linal.backend import call_event_from
        s, n = Store(":memory:"), FakeNotifier()
        wire = {"subscription": 3, "result": {"callId": 99, "state": "RINGING_INCOMING",
                                              "number": "+1888", "isOutgoing": False}}
        receiver.handle_call_event(s, call_event_from(wire), n)
        self.assertEqual(n.rings, [("99", "+1888", "+1888")])


def _pixbuf_available():
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_pixbuf_available(), "GdkPixbuf not installed")
class PrepareAttachmentTest(unittest.TestCase):
    def test_large_photo_is_shrunk_small_files_untouched(self):
        from gi.repository import GdkPixbuf
        from linal import backend as bmod
        with tempfile.TemporaryDirectory() as d:
            old = (bmod.SENT_DIR, bmod.RESIZE_OVER_BYTES)
            bmod.SENT_DIR, bmod.RESIZE_OVER_BYTES = os.path.join(d, "sent"), 1000
            try:
                big = os.path.join(d, "IMG_1.png")
                pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 4000, 3000)
                pb.fill(0x3366ccff)
                pb.savev(big, "png", [], [])
                out = bmod.prepare_attachment(big)
                self.assertTrue(out.endswith("IMG_1.jpg"))
                small = GdkPixbuf.Pixbuf.new_from_file(out)
                self.assertEqual((small.get_width(), small.get_height()), (1600, 1200))
                # the kept copy is moved (not duplicated) out of the temp dir
                kept = bmod._keep_copy(out, 123)
                self.assertEqual(os.path.basename(kept), "123-IMG_1.jpg")
                self.assertEqual(sorted(os.listdir(bmod.SENT_DIR)), ["123-IMG_1.jpg"])
                txt = os.path.join(d, "notes.txt")
                open(txt, "w").write("x" * 5000)
                self.assertEqual(bmod.prepare_attachment(txt), txt)   # not an image
            finally:
                bmod.SENT_DIR, bmod.RESIZE_OVER_BYTES = old


class SessionBusEnvTest(unittest.TestCase):
    def test_fills_missing_bus_address(self):
        from linal.app import _ensure_session_bus_env
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "bus"), "w").close()
            env = {"XDG_RUNTIME_DIR": d}
            _ensure_session_bus_env(env)
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={d}/bus")
            keep = {"XDG_RUNTIME_DIR": d, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
            _ensure_session_bus_env(keep)
            self.assertEqual(keep["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/x")
        none = {"XDG_RUNTIME_DIR": "/nonexistent"}
        _ensure_session_bus_env(none)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", none)


def _gtk_importable():
    try:
        import gi
        gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_gtk_importable(), "GTK4/libadwaita not installed")
class NewChatSearchTest(unittest.TestCase):
    def test_captcha_opens_default_browser(self):
        from linal import registration_flow as rf
        opened = []
        orig = rf.Gio.AppInfo.launch_default_for_uri
        rf.Gio.AppInfo.launch_default_for_uri = lambda uri, ctx: opened.append(uri)
        try:
            rf.open_captcha_page()
        finally:
            rf.Gio.AppInfo.launch_default_for_uri = orig
        self.assertEqual(opened, [rf.CAPTCHA_URL])

    def test_failed_registration_restores_account(self):
        """signal-cli closes the loaded account when a registration starts;
        a failure must restart the daemon or the account stops receiving."""
        from linal import registration_flow as rf
        calls = []
        orig = rf.subprocess.run
        rf.subprocess.run = lambda args, **kw: calls.append(args)
        try:
            rf.restore_account_session()
        finally:
            rf.subprocess.run = orig
        self.assertEqual(calls, [["systemctl", "--user", "restart", "linal-daemon.service"]])

    def test_merge_and_filter(self):
        from linal.views.newchat import gather_entries, filter_entries, looks_like_number
        from linal.phonebook import PhoneContact
        from linal.models import Conversation
        rows = gather_entries(
            [PhoneContact("Jane", ["+13035550101"]), PhoneContact("Abe", ["+13035550100"])],
            [Conversation(key="R3JwPQ==", title="Family", is_group=True),
             Conversation(key="+13035550101", title="jane-profile"),
             Conversation(key="+13035550199", title="Zed")])
        self.assertEqual([r["title"] for r in rows], ["Family", "Jane", "Abe", "Zed"])  # no dup Jane
        self.assertEqual([r["title"] for r in filter_entries(rows, "fam")], ["Family"])
        self.assertEqual([r["title"] for r in filter_entries(rows, "555-0100")], ["Abe"])
        self.assertTrue(looks_like_number("(303) 555-0101"))
        self.assertFalse(looks_like_number("Jane 5"))


if __name__ == "__main__":
    unittest.main()
