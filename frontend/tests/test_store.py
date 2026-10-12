import os, sys, sqlite3, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.store import Store, account_path, open_account_store
from linal.models import Attachment, Message, Quote
from linal.envelope import parse_events


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def test_add_and_get(self):
        self.s.add_message("+1", Message(timestamp=100, body="hi", outgoing=False, author="Al"), title="Al")
        self.s.add_message("+1", Message(timestamp=200, body="yo", outgoing=True), title="Al")
        msgs = self.s.get_messages("+1")
        self.assertEqual([m.body for m in msgs], ["hi", "yo"])  # ordered by ts
        self.assertEqual([m.outgoing for m in msgs], [False, True])

    def test_idempotent(self):
        m = Message(timestamp=100, body="dup", outgoing=False)
        self.assertTrue(self.s.add_message("+1", m))      # first insert
        self.assertFalse(self.s.add_message("+1", m))     # duplicate ignored
        self.assertEqual(len(self.s.get_messages("+1")), 1)

    def test_conversations_sorted_by_recency(self):
        self.s.add_message("+A", Message(timestamp=100, body="old", outgoing=False), title="A")
        self.s.add_message("+B", Message(timestamp=300, body="new", outgoing=False), title="B")
        convs = self.s.get_conversations()
        self.assertEqual([c.key for c in convs], ["+B", "+A"])  # most recent first
        self.assertEqual(convs[0].last_preview, "new")

    def test_latest_ts(self):
        self.assertEqual(self.s.latest_ts(), 0)
        self.s.add_message("+1", Message(timestamp=555, body="x", outgoing=False))
        self.assertEqual(self.s.latest_ts(), 555)

    def test_group_flag(self):
        self.s.add_message("g1", Message(timestamp=1, body="hey", outgoing=False), title="Team", is_group=True)
        convs = self.s.get_conversations()
        self.assertTrue(convs[0].is_group)
        self.assertEqual(convs[0].title, "Team")

    def test_rich_fields_round_trip(self):
        m = Message(timestamp=5, body="look", outgoing=False, author="Bo", author_id="+1",
                    attachments=[Attachment(id="a1", content_type="image/png", filename="x.png")],
                    quote=Quote(timestamp=3, author_id="+2", text="earlier"))
        self.s.add_message("+1", m)
        [got] = self.s.get_messages("+1")
        self.assertEqual(got.attachments[0].id, "a1")
        self.assertEqual(got.quote.text, "earlier")
        self.assertEqual(got.author_id, "+1")

    def test_reactions_one_per_reactor(self):
        self.s.add_message("+1", Message(timestamp=10, body="hi", outgoing=True))
        self.assertTrue(self.s.set_reaction("+1", 10, "", "👍", "+1"))
        self.s.set_reaction("+1", 10, "", "❤️", "+1")          # replaces their 👍
        self.s.set_reaction("+1", 10, "", "❤️", "")            # and we react too
        [m] = self.s.get_messages("+1")
        self.assertEqual(m.reactions, {"❤️": ["+1", ""]})
        self.s.set_reaction("+1", 10, "", "❤️", "+1", remove=True)
        [m] = self.s.get_messages("+1")
        self.assertEqual(m.reactions, {"❤️": [""]})
        self.assertFalse(self.s.set_reaction("+1", 999, "", "👍", "+1"))   # unknown target

    def test_receipt_status_only_advances(self):
        self.s.add_message("+1", Message(timestamp=10, body="hi", outgoing=True, status="sent"))
        self.s.set_status([10], "read")
        self.s.set_status([10], "delivered")                    # late delivery receipt
        self.assertEqual(self.s.get_messages("+1")[0].status, "read")

    def test_unread_and_mark_read(self):
        self.s.add_message("+1", Message(timestamp=10, body="a", outgoing=False, author_id="+1"))
        self.s.add_message("+1", Message(timestamp=20, body="b", outgoing=False, author_id="+1"))
        self.assertEqual(self.s.get_conversations()[0].unread, 2)
        newly = self.s.mark_read("+1")
        self.assertEqual(sorted(newly), [("+1", 10), ("+1", 20)])
        self.assertEqual(self.s.unread_count("+1"), 0)
        self.assertEqual(self.s.mark_read("+1"), [])            # nothing new to receipt
        # replying marks everything before it read
        self.s.add_message("+1", Message(timestamp=30, body="c", outgoing=False, author_id="+1"))
        self.s.add_message("+1", Message(timestamp=40, body="me", outgoing=True))
        self.assertEqual(self.s.unread_count("+1"), 0)

    def test_remote_delete_and_edit(self):
        self.s.add_message("+1", Message(timestamp=10, body="secret", outgoing=False))
        self.assertTrue(self.s.apply_edit("+1", 10, "fixed"))
        m = self.s.get_messages("+1")[0]
        self.assertEqual((m.body, m.edited), ("fixed", True))
        self.assertTrue(self.s.mark_deleted("+1", 10))
        m = self.s.get_messages("+1")[0]
        self.assertTrue(m.deleted)
        self.assertEqual(m.body, "")
        self.assertEqual(m.preview, "This message was deleted")
        self.assertFalse(self.s.apply_edit("+1", 10, "again"))  # can't edit a deleted message

    def test_apply_events_from_envelopes(self):
        env = lambda **e: {"envelope": e}
        self.s.apply_event(parse_events(env(timestamp=10, sourceNumber="+1", sourceName="Bob",
                                            dataMessage={"message": "hi"}))[0])
        self.assertEqual(self.s.display_name("+1"), "Bob")     # learned profile name
        for ev in parse_events(env(sourceNumber="+1", dataMessage={"reaction": {
                "emoji": "😂", "targetAuthorNumber": "+1", "targetSentTimestamp": 10}})):
            self.s.apply_event(ev)
        self.assertEqual(self.s.get_messages("+1")[0].reactions, {"😂": ["+1"]})
        # the same envelope applied twice (GUI + receiver) changes nothing
        ev = parse_events(env(timestamp=10, sourceNumber="+1", dataMessage={"message": "hi"}))[0]
        self.assertIsNone(self.s.apply_event(ev))
        self.assertEqual(len(self.s.get_messages("+1")), 1)

    def test_read_sync_finds_group(self):
        self.s.add_message("G=", Message(timestamp=10, body="x", outgoing=False, author_id="+1"),
                           title="Team", is_group=True)
        self.s.mark_read_by_message("+1", 10)
        self.assertEqual(self.s.unread_count("G="), 0)

    def test_contact_name_beats_profile_name(self):
        self.s.add_message("+1", Message(timestamp=1, body="x", outgoing=False))
        self.s.set_profile_name("+1", "Robert (profile)")
        self.assertEqual(self.s.get_conversations()[0].title, "Robert (profile)")
        self.s.set_contact_name("+1", "Bob")
        self.assertEqual(self.s.get_conversations()[0].title, "Bob")

    def test_calls(self):
        self.s.record_call("7", "+1", outgoing=False, state="RINGING_INCOMING", now_ms=1000)
        self.s.record_call("7", "+1", outgoing=False, state="CONNECTED", now_ms=2000)
        c = self.s.record_call("7", "+1", outgoing=False, state="ENDED", now_ms=62000)
        self.assertEqual((c.answered, c.duration, c.missed), (True, 60, False))
        self.s.record_call("8", "+1", outgoing=False, state="RINGING_INCOMING", now_ms=70000)
        m = self.s.record_call("8", "+1", outgoing=False, state="ENDED", now_ms=80000)
        self.assertTrue(m.missed)
        self.assertEqual([x.call_id for x in self.s.get_calls("+1")], ["7", "8"])

    def test_revision_changes_on_writes(self):
        r0 = self.s.revision()
        self.s.add_message("+1", Message(timestamp=10, body="hi", outgoing=True))
        r1 = self.s.revision()
        self.assertGreater(r1, r0)
        self.s.set_status([10], "delivered")
        self.assertGreater(self.s.revision(), r1)

    def test_focus_expires(self):
        self.s.set_focus("+1")
        self.assertEqual(self.s.focused_conversation(), "+1")
        self.assertIsNone(self.s.focused_conversation(max_age=-1))

    def test_meta(self):
        self.assertEqual(self.s.get_meta("prefs", {"a": 1}), {"a": 1})
        self.s.set_meta("prefs", {"readReceipts": False})
        self.assertEqual(self.s.get_meta("prefs"), {"readReceipts": False})


class StoreMigrationTest(unittest.TestCase):
    def test_upgrades_first_release_schema(self):
        """A history.db written by the first release (5 message columns) opens,
        keeps its rows, and gains the new columns."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "history.db")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE conversations(key TEXT PRIMARY KEY, title TEXT, is_group INTEGER DEFAULT 0, last_time INTEGER DEFAULT 0)")
            c.execute("CREATE TABLE messages(conv_key TEXT, ts INTEGER, body TEXT, outgoing INTEGER, author TEXT, PRIMARY KEY (conv_key, ts, outgoing))")
            c.execute("INSERT INTO conversations VALUES('+1','Al',0,5)")
            c.execute("INSERT INTO messages VALUES('+1',5,'old msg',0,'Al')")
            c.commit()
            c.close()
            s = Store(p)
            [m] = s.get_messages("+1")
            self.assertEqual((m.body, m.attachments, m.reactions, m.status), ("old msg", [], {}, ""))
            s.set_reaction("+1", 5, "", "👍", "")
            self.assertEqual(s.get_messages("+1")[0].reactions, {"👍": [""]})
            s.close()


class AccountStoreTest(unittest.TestCase):
    """Each account has its own history; signing in a different account
    deletes everyone else's. Pre-1.0.12 history moves to its own account."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.legacy = os.path.join(self.tmp.name, "history.db")
        self.sent = os.path.join(self.tmp.name, "sent")
        os.makedirs(self.sent)
        open(os.path.join(self.sent, "1-photo.jpg"), "w").close()
        old = Store(self.legacy)
        old.add_message("+1555", Message(timestamp=100, body="old chat", outgoing=False), title="Al")
        old.set_meta("prefs", {"profileName": "Me"})
        old.close()

    def tearDown(self):
        self.tmp.cleanup()

    def bodies(self, s):
        return sorted(m.body for c in s.get_conversations() for m in s.get_messages(c.key))

    def legacy_bodies(self):
        return self.bodies(Store(self.legacy))

    def test_upgrade_moves_old_history_to_its_account(self):
        a = open_account_store("+1111", self.legacy)
        self.assertEqual(a.path, account_path("+1111", self.tmp.name))
        self.assertEqual(self.bodies(a), ["old chat"])
        self.assertEqual(a.get_meta("prefs"), {"profileName": "Me"})
        self.assertEqual(self.legacy_bodies(), [])               # no copy left behind
        self.assertEqual(Store(self.legacy).get_meta("prefs"), {"profileName": "Me"})
        self.assertTrue(os.listdir(self.sent))                    # same account: kept

    def test_same_account_again_keeps_history(self):
        open_account_store("+1111", self.legacy).add_message(
            "+1777", Message(timestamp=200, body="new", outgoing=False))
        self.assertEqual(self.bodies(open_account_store("+1111", self.legacy)), ["new", "old chat"])
        self.assertTrue(os.listdir(self.sent))

    def test_new_account_deletes_all_other_history(self):
        a = open_account_store("+1111", self.legacy)
        a.close()
        b = open_account_store("+2222", self.legacy)
        self.assertEqual(self.bodies(b), [])
        self.assertFalse(os.path.exists(os.path.dirname(a.path)))
        self.assertFalse(os.path.exists(self.sent))
        self.assertEqual(self.legacy_bodies(), [])

    def test_old_history_of_another_account_is_deleted(self):
        Store(self.legacy).set_meta("owner", "+1111")
        self.assertEqual(self.bodies(open_account_store("+2222", self.legacy)), [])
        self.assertEqual(self.legacy_bodies(), [])
        self.assertFalse(os.path.exists(self.sent))
        self.assertEqual(Store(self.legacy).get_meta("owner"), "+2222")

    def test_deleted_rows_are_not_left_in_the_file(self):
        Store(self.legacy).set_meta("owner", "+1111")
        open_account_store("+2222", self.legacy)
        with open(self.legacy, "rb") as f:
            self.assertNotIn(b"old chat", f.read())

    def test_no_old_history(self):
        os.remove(self.legacy)
        self.assertEqual(self.bodies(open_account_store("+1111", self.legacy)), [])

if __name__ == "__main__":
    unittest.main()
