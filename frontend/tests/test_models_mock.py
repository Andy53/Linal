import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.models import Contact, Conversation, Message
from linal.mock import MockBackend


class ModelsTest(unittest.TestCase):
    def test_contact_display_precedence(self):
        self.assertEqual(Contact(name="Al", number="+1", uuid="u").display, "Al")
        self.assertEqual(Contact(number="+1", uuid="u").display, "+1")
        self.assertEqual(Contact(uuid="u").display, "u")
        self.assertEqual(Contact().display, "unknown")

    def test_last_preview(self):
        c = Conversation(key="k", title="t")
        self.assertEqual(c.last_preview, "")
        c.messages.append(Message(timestamp=1, body="first", outgoing=False))
        c.messages.append(Message(timestamp=2, body="latest", outgoing=True))
        self.assertEqual(c.last_preview, "latest")


class MockBackendTest(unittest.TestCase):
    def setUp(self):
        self.b = MockBackend()

    def test_start_sets_connected(self):
        states = []
        self.b.on_connection_change = states.append
        self.b.start()
        self.assertEqual(states, [True])

    def test_lists_three_conversations(self):
        convs = self.b.list_conversations()
        self.assertEqual(len(convs), 3)
        self.assertTrue(any(c.is_group for c in convs))

    def test_send_appends_and_returns_outgoing(self):
        convs = self.b.list_conversations()
        key = convs[0].key
        before = len(convs[0].messages)
        msg = self.b.send(key, "new message")
        self.assertTrue(msg.outgoing)
        self.assertEqual(msg.body, "new message")
        after = [c for c in self.b.list_conversations() if c.key == key][0]
        self.assertEqual(len(after.messages), before + 1)
        self.assertEqual(after.messages[-1].body, "new message")

    def test_accounts(self):
        self.assertEqual(len(self.b.list_accounts()), 1)


if __name__ == "__main__":
    unittest.main()
