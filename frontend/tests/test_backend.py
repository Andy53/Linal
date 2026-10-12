import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.backend import RealBackend
from linal.store import Store
from linal.models import Message


class FakeRpc:
    """Stand-in for SignalRpc: records calls, returns scripted results."""
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []
        self.on_notification = None
        self.on_disconnect = None
    def connect(self): pass
    def close(self): pass
    def clone(self): return type(self)()
    def call(self, method, params=None, timeout=60.0):
        self.calls.append((method, params))
        return self.results.get(method, {})


class RealBackendTest(unittest.TestCase):
    def _backend(self, results=None, account="+1555"):
        b = RealBackend(account=account, store=Store(":memory:"))
        b.rpc = FakeRpc(results)
        return b

    def test_list_accounts_maps_models(self):
        b = self._backend({"listAccounts": [{"number": "+1555"}, {"number": "+1666"}]})
        accts = b.list_accounts()
        self.assertEqual([a.number for a in accts], ["+1555", "+1666"])
        self.assertTrue(all(a.registered for a in accts))

    def test_version(self):
        b = self._backend({"version": {"version": "0.14.8"}})
        self.assertEqual(b.version(), "0.14.8")

    def test_send_builds_params_and_returns_message(self):
        b = self._backend({"send": {"timestamp": 999}})
        msg = b.send("+17770001111", "hello")
        self.assertIsInstance(msg, Message)
        self.assertTrue(msg.outgoing)
        self.assertEqual(msg.body, "hello")
        self.assertEqual(msg.timestamp, 999)
        method, params = b.rpc.calls[-1]
        self.assertEqual(method, "send")
        self.assertEqual(params["recipient"], ["+17770001111"])
        self.assertEqual(params["message"], "hello")
        self.assertEqual(params["account"], "+1555")

    def test_register_and_verify_params(self):
        b = self._backend()
        b.register("+1999", "signalcaptcha://TOKEN", voice=True)
        self.assertEqual(b.rpc.calls[-1],
                         ("register", {"account": "+1999", "captcha": "signalcaptcha://TOKEN", "voice": True}))
        b.verify("+1999", "123-456")
        self.assertEqual(b.rpc.calls[-1],
                         ("verify", {"account": "+1999", "verificationCode": "123-456"}))

    def test_incoming_datamessage_delivered(self):
        b = self._backend()
        received = []
        b.on_message = lambda key, msg: received.append((key, msg))
        b._on_notification("receive", {"envelope": {
            "timestamp": 111, "sourceNumber": "+1888", "sourceName": "Bob",
            "dataMessage": {"message": "yo"}}})
        self.assertEqual(len(received), 1)
        key, msg = received[0]
        self.assertEqual(key, "+1888")
        self.assertEqual(msg.body, "yo")
        self.assertFalse(msg.outgoing)
        self.assertEqual(msg.author, "Bob")

    def test_non_datamessage_ignored(self):
        """Typing indicators / receipts (no dataMessage body) must not create messages."""
        b = self._backend()
        received = []
        b.on_message = lambda key, msg: received.append((key, msg))
        b._on_notification("receive", {"envelope": {"timestamp": 1, "receiptMessage": {}}})
        b._on_notification("receive", {"envelope": {"timestamp": 2, "typingMessage": {}}})
        b._on_notification("somethingElse", {"envelope": {}})
        self.assertEqual(received, [])


    def test_list_contacts_from_contacts_and_groups(self):
        b = self._backend({
            "listGroups": [{"id": "g1=", "name": "Family"}],
            "listContacts": [
                {"number": "+1222", "name": "Alice"},
                {"uuid": "uuid-2", "profile": {"givenName": "Bob"}},
            ],
        })
        convs = b.list_contacts()
        titles = [(c.title, c.is_group, c.key) for c in convs]
        self.assertIn(("Family", True, "g1="), titles)
        self.assertIn(("Alice", False, "+1222"), titles)
        self.assertIn(("Bob", False, "uuid-2"), titles)

    def test_start_autoselects_account(self):
        b = self._backend({"listAccounts": [{"number": "+1999"}]}, account=None)
        b.start()
        self.assertEqual(b.account, "+1999")

    def test_is_registered(self):
        self.assertFalse(self._backend({"listAccounts": []}).is_registered())
        self.assertTrue(self._backend({"listAccounts": [{"number": "+1"}]}).is_registered())

    def test_start_subscribes_to_call_events(self):
        b = self._backend({"listAccounts": [{"number": "+1999"}]}, account=None)
        b.start()
        self.assertIn(("subscribeCallEvents", {"account": "+1999"}), b.rpc.calls)

    def test_send_to_group_uses_group_id(self):
        b = self._backend({"send": {"timestamp": 5}})
        b.send("Zm9vYmFyYmF6PT0=", "hi team")
        _m, params = b.rpc.calls[-1]
        self.assertEqual(params["groupId"], "Zm9vYmFyYmF6PT0=")
        self.assertNotIn("recipient", params)
        self.assertTrue(b.store.get_conversations()[0].is_group)

    def test_send_with_attachment_and_quote(self):
        b = self._backend({"send": {"timestamp": 50}})
        orig = Message(timestamp=40, body="question?", outgoing=False, author_id="+1222")
        msg = b.send("+1222", "answer", attachments=["/tmp/pic.jpg"], quote=orig)
        _m, p = b.rpc.calls[-1]
        self.assertEqual(p["attachments"], ["/tmp/pic.jpg"])
        self.assertEqual((p["quoteTimestamp"], p["quoteAuthor"], p["quoteMessage"]),
                         (40, "+1222", "question?"))
        self.assertEqual(msg.attachments[0].content_type, "image/jpeg")
        stored = b.store.get_messages("+1222")[0]
        self.assertEqual((stored.quote.timestamp, stored.status), (40, "sent"))

    def test_own_number_is_note_to_self(self):
        b = self._backend({"send": {"timestamp": 9}})
        b.send("+1555", "memo")
        self.assertEqual(b.list_conversations()[0].title, "Note to Self")

    def test_sent_attachment_is_kept(self):
        import tempfile
        from linal import backend as bmod
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "photo.png")
            open(src, "wb").write(b"png-bytes")
            old, bmod.SENT_DIR = bmod.SENT_DIR, os.path.join(d, "sent")
            try:
                b = self._backend({"send": {"timestamp": 77}})
                b.send("+1222", "", attachments=[src])
                os.remove(src)                      # original goes away...
                att = b.store.get_messages("+1222")[0].attachments[0]
                self.assertTrue(os.path.exists(att.path))   # ...our copy remains
                self.assertEqual((att.filename, att.size), ("photo.png", 9))
            finally:
                bmod.SENT_DIR = old

    def test_quoting_own_message_uses_our_number(self):
        b = self._backend({"send": {"timestamp": 50}})
        b.send("+1222", "re", quote=Message(timestamp=40, body="mine", outgoing=True))
        self.assertEqual(b.rpc.calls[-1][1]["quoteAuthor"], "+1555")

    def test_react_params_and_local_echo(self):
        b = self._backend()
        b.store.add_message("+1222", Message(timestamp=40, body="hi", outgoing=False, author_id="+1222"))
        msg = b.store.get_messages("+1222")[0]
        b.react("+1222", msg, "👍")
        m, p = b.rpc.calls[-1]
        self.assertEqual(m, "sendReaction")
        self.assertEqual((p["emoji"], p["targetAuthor"], p["targetTimestamp"], p["recipient"]),
                         ("👍", "+1222", 40, ["+1222"]))
        self.assertNotIn("remove", p)
        self.assertEqual(b.store.get_messages("+1222")[0].reactions, {"👍": [""]})

    def test_delete_for_everyone(self):
        b = self._backend()
        b.store.add_message("+1222", Message(timestamp=40, body="oops", outgoing=True))
        b.delete_for_everyone("+1222", b.store.get_messages("+1222")[0])
        self.assertEqual(b.rpc.calls[-1], ("remoteDelete", {"account": "+1555", "targetTimestamp": 40,
                                                            "recipient": ["+1222"]}))
        self.assertTrue(b.store.get_messages("+1222")[0].deleted)

    def test_mark_read_sends_receipts_per_sender(self):
        b = self._backend()
        g = "R1JPVVA9PQ=="
        b.store.add_message(g, Message(timestamp=1, body="a", outgoing=False, author_id="+1"), is_group=True)
        b.store.add_message(g, Message(timestamp=2, body="b", outgoing=False, author_id="+2"), is_group=True)
        b.store.add_message(g, Message(timestamp=3, body="c", outgoing=False, author_id="+1"), is_group=True)
        b.mark_read(g)
        receipts = sorted((p["recipient"], p["targetTimestamp"]) for m, p in b.rpc.calls if m == "sendReceipt")
        self.assertEqual(receipts, [("+1", [1, 3]), ("+2", [2])])
        self.assertEqual(b.store.unread_count(g), 0)

    def test_check_numbers(self):
        b = self._backend({"getUserStatus": [
            {"recipient": "+1222", "number": "+1222", "isRegistered": True},
            {"recipient": "+1333", "number": "+1333", "isRegistered": False}]})
        self.assertEqual(b.check_numbers(["+1222", "+1333"]), {"+1222": True, "+1333": False})
        self.assertEqual(b.rpc.calls[-1][1]["recipient"], ["+1222", "+1333"])

    def test_save_contact(self):
        b = self._backend()
        b.save_contact("+1222", "Alice")
        self.assertEqual(b.rpc.calls[-1], ("updateContact", {"account": "+1555", "recipient": "+1222",
                                                             "name": "Alice"}))
        self.assertEqual(b.store.display_name("+1222"), "Alice")

    def test_create_group(self):
        b = self._backend({"updateGroup": {"groupId": "TkVXPT0="}})
        gid = b.create_group("Hikers", ["+1222", "+1333"])
        self.assertEqual(gid, "TkVXPT0=")
        self.assertEqual(b.rpc.calls[-1][1], {"account": "+1555", "name": "Hikers",
                                             "member": ["+1222", "+1333"]})
        self.assertEqual(b.store.get_conversations()[0].title, "Hikers")

    def test_sync_names(self):
        b = self._backend({"listGroups": [{"id": "Rz0=", "name": "Family"}],
                           "listContacts": [{"number": "+1222", "profile": {"givenName": "Alice"}}]})
        b.sync_names()
        self.assertEqual(b.store.display_name("+1222"), "Alice")
        self.assertEqual(b.store.get_conversations()[0].title, "Family")

    def test_typing_callback_and_no_store_write(self):
        b = self._backend()
        seen = []
        b.on_typing = lambda key, who, started: seen.append((key, who, started))
        r0 = b.store.revision()
        b._on_notification("receive", {"envelope": {"sourceNumber": "+1888", "sourceName": "Bob",
                                                    "typingMessage": {"action": "STARTED"}}})
        self.assertEqual(seen, [("+1888", "Bob", True)])
        self.assertEqual(b.store.revision(), r0)

    def test_call_lifecycle(self):
        b = self._backend({"startCall": {"callId": 77, "state": "RINGING_OUTGOING",
                                         "inputDeviceName": "signal_input_77",
                                         "outputDeviceName": "signal_output_77"}})
        info = b.start_call("+1222")
        self.assertEqual(info["callId"], 77)
        self.assertEqual(b.rpc.calls[-1], ("startCall", {"account": "+1555", "recipient": "+1222"}))
        events = []
        b.on_call_event = events.append
        b._on_notification("callEvent", {"callId": 77, "state": "ENDED", "number": "+1222",
                                         "isOutgoing": True})
        self.assertEqual(events[0]["state"], "ENDED")
        [c] = b.store.get_calls("+1222")
        self.assertEqual((c.call_id, c.outgoing, c.state), ("77", True, "ENDED"))
        b.accept_call(5)
        self.assertEqual(b.rpc.calls[-1], ("acceptCall", {"account": "+1555", "callId": 5}))
        b.hangup_call(5)
        self.assertEqual(b.rpc.calls[-1][0], "hangupCall")

    def test_call_event_real_wire_format(self):
        """Exactly what signal-cli 0.14.8 pushes (captured on the phone): the
        event is wrapped in {"subscription", "result"}. Missing this left calls
        silent (audio never bridged) and incoming calls invisible."""
        from linal.backend import call_event_from
        wire = {"subscription": 13, "result": {
            "callId": 2250974135398001401, "state": "CONNECTED", "number": "+1222",
            "uuid": "374f7df2-32aa-44ba-b2f6-5bf4f7393eff", "isOutgoing": False,
            "inputDeviceName": "signal_input_2250974135398001401",
            "outputDeviceName": "signal_output_2250974135398001401"}}
        self.assertEqual(call_event_from(wire)["state"], "CONNECTED")
        self.assertEqual(call_event_from({"callEvent": {"callId": 1}})["callId"], 1)
        b = self._backend()
        seen = []
        b.on_call_event = seen.append
        b._on_notification("callEvent", wire)
        self.assertEqual(seen[0]["inputDeviceName"], "signal_input_2250974135398001401")
        [c] = b.store.get_calls("+1222")
        self.assertEqual((c.call_id, c.state), ("2250974135398001401", "CONNECTED"))

    def test_send_with_attachment_waits_for_upload(self):
        b = self._backend({"send": {"timestamp": 5}})
        timeouts = []
        orig = b.rpc.call
        b.rpc.call = lambda m, p=None, timeout=60.0: (timeouts.append(timeout), orig(m, p))[1]
        b.send("+1222", "hi")
        b.send("+1222", "pic", attachments=["/nonexistent/x.bin"])
        self.assertEqual(timeouts, [60, 600])

    def test_register_reregister_flag(self):
        b = self._backend()
        b.register("+1999", "signalcaptcha://T", reregister=True)
        self.assertEqual(b.rpc.calls[-1][1]["reregister"], True)

    def test_adopt_account_uses_fresh_connection(self):
        b = self._backend(account=None)
        old_rpc = b.rpc
        b.adopt_account("+1777")
        self.assertIsNot(b.rpc, old_rpc)
        self.assertEqual(b.account, "+1777")
        self.assertIn(("subscribeCallEvents", {"account": "+1777"}), b.rpc.calls)
        self.assertIsNotNone(b.rpc.on_notification)

    def test_is_group_id(self):
        from linal.backend import is_group_id
        self.assertFalse(is_group_id("+13035551234"))
        self.assertFalse(is_group_id("0d7f8c2e-1b2a-4c3d-8e9f-0a1b2c3d4e5f"))
        self.assertTrue(is_group_id("Zm9vYmFyYmF6PT0="))
        self.assertFalse(is_group_id(""))

class AccountHistoryTest(unittest.TestCase):
    """The app switches to the account's own history file, including after a
    new number is registered (adopt_account)."""
    def test_start_and_adopt_switch_history_file(self):
        import tempfile
        from unittest import mock
        from linal.store import account_path
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_DATA_HOME": d}):
            b = RealBackend()
            b.rpc = FakeRpc({"listAccounts": [{"number": "+1555"}]})
            b.start()
            self.assertEqual(b.store.path, account_path("+1555"))
            b.store.add_message("+1777", Message(timestamp=1, body="mine", outgoing=False))
            b.adopt_account("+1666")
            self.assertEqual(b.store.path, account_path("+1666"))
            self.assertEqual(b.store.get_conversations(), [])


if __name__ == "__main__":
    unittest.main()
