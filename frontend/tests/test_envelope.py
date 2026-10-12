import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.envelope import (parse_incoming, parse_events, MessageEvent, ReactionEvent,
                                ReceiptEvent, TypingEvent, DeleteEvent, EditEvent, ReadSyncEvent)


class EnvelopeTest(unittest.TestCase):
    def test_incoming_direct(self):
        key, title, msg = parse_incoming({"envelope": {
            "timestamp": 10, "sourceNumber": "+1888", "sourceName": "Bob",
            "dataMessage": {"message": "hello"}}})
        self.assertEqual(key, "+1888")
        self.assertEqual(title, "Bob")
        self.assertEqual((msg.body, msg.outgoing, msg.author), ("hello", False, "Bob"))
        self.assertEqual(msg.author_id, "+1888")

    def test_incoming_group(self):
        key, title, msg = parse_incoming({"envelope": {
            "timestamp": 11, "sourceName": "Bob",
            "dataMessage": {"message": "hi all", "groupInfo": {"groupId": "GRP=="}}}})
        self.assertEqual(key, "GRP==")
        self.assertFalse(msg.outgoing)

    def test_group_name_used_as_title(self):
        [ev] = parse_events({"envelope": {
            "timestamp": 11, "sourceNumber": "+1888", "sourceName": "Bob",
            "dataMessage": {"message": "hi", "groupInfo": {"groupId": "GRP==", "groupName": "Family"}}}})
        self.assertEqual((ev.conv_key, ev.title, ev.is_group), ("GRP==", "Family", True))
        self.assertEqual(ev.message.author, "Bob")

    def test_sync_sent_is_outgoing(self):
        key, title, msg = parse_incoming({"envelope": {
            "syncMessage": {"sentMessage": {
                "timestamp": 12, "destinationNumber": "+1999",
                "destinationName": "Carol", "message": "from my phone"}}}})
        self.assertEqual(key, "+1999")
        self.assertTrue(msg.outgoing)
        self.assertEqual(msg.body, "from my phone")
        self.assertEqual(msg.status, "sent")

    def test_attachments_and_quote(self):
        [ev] = parse_events({"envelope": {
            "timestamp": 20, "sourceNumber": "+1888", "sourceName": "Bob",
            "dataMessage": {"message": None, "attachments": [
                {"id": "abc.jpg", "contentType": "image/jpeg", "filename": "cat.jpg", "size": 2048}],
                "quote": {"id": 15, "authorNumber": "+1777", "text": "original"}}}})
        m = ev.message
        self.assertEqual(m.body, "")
        self.assertEqual(m.attachments[0].id, "abc.jpg")
        self.assertTrue(m.attachments[0].is_image)
        self.assertEqual(m.preview, "📷 Photo")
        self.assertEqual((m.quote.timestamp, m.quote.author_id, m.quote.text), (15, "+1777", "original"))

    def test_voice_note_preview(self):
        [ev] = parse_events({"envelope": {"timestamp": 1, "sourceNumber": "+1", "dataMessage": {
            "attachments": [{"id": "v", "contentType": "audio/aac", "isVoiceNote": True}]}}})
        self.assertEqual(ev.message.preview, "🎤 Voice message")

    def test_reaction(self):
        [ev] = parse_events({"envelope": {
            "timestamp": 30, "sourceNumber": "+1888", "dataMessage": {"reaction": {
                "emoji": "👍", "targetAuthorNumber": "+1555", "targetSentTimestamp": 10,
                "isRemove": False}}}})
        self.assertIsInstance(ev, ReactionEvent)
        self.assertEqual((ev.conv_key, ev.emoji, ev.target_author, ev.target_ts, ev.reactor, ev.remove),
                         ("+1888", "👍", "+1555", 10, "+1888", False))

    def test_own_reaction_from_linked_device(self):
        [ev] = parse_events({"envelope": {"syncMessage": {"sentMessage": {
            "timestamp": 31, "destinationNumber": "+1888",
            "reaction": {"emoji": "❤️", "targetAuthorNumber": "+1888", "targetSentTimestamp": 9}}}}})
        self.assertIsInstance(ev, ReactionEvent)
        self.assertEqual(ev.reactor, "")   # "" = this account

    def test_receipts(self):
        [d] = parse_events({"envelope": {"sourceNumber": "+1888", "receiptMessage": {
            "isDelivery": True, "timestamps": [1, 2]}}})
        self.assertEqual((d.sender, d.timestamps, d.kind), ("+1888", [1, 2], "delivered"))
        [r] = parse_events({"envelope": {"sourceNumber": "+1888", "receiptMessage": {
            "isRead": True, "timestamps": [1]}}})
        self.assertIsInstance(r, ReceiptEvent)
        self.assertEqual(r.kind, "read")

    def test_typing(self):
        [t] = parse_events({"envelope": {"sourceNumber": "+1888", "sourceName": "Bob",
                                         "typingMessage": {"action": "STARTED"}}})
        self.assertIsInstance(t, TypingEvent)
        self.assertEqual((t.conv_key, t.who, t.started), ("+1888", "Bob", True))
        [g] = parse_events({"envelope": {"sourceNumber": "+1888", "typingMessage": {
            "action": "STOPPED", "groupId": "G="}}})
        self.assertEqual((g.conv_key, g.started), ("G=", False))

    def test_remote_delete(self):
        [ev] = parse_events({"envelope": {"sourceNumber": "+1888", "dataMessage": {
            "remoteDelete": {"timestamp": 10}}}})
        self.assertIsInstance(ev, DeleteEvent)
        self.assertEqual((ev.conv_key, ev.target_ts), ("+1888", 10))

    def test_edit(self):
        [ev] = parse_events({"envelope": {"sourceNumber": "+1888", "editMessage": {
            "targetSentTimestamp": 10, "dataMessage": {"message": "fixed typo"}}}})
        self.assertIsInstance(ev, EditEvent)
        self.assertEqual((ev.conv_key, ev.target_ts, ev.body), ("+1888", 10, "fixed typo"))

    def test_read_sync(self):
        [ev] = parse_events({"envelope": {"syncMessage": {"readMessages": [
            {"senderNumber": "+1888", "timestamp": 10}]}}})
        self.assertIsInstance(ev, ReadSyncEvent)
        self.assertEqual((ev.sender, ev.timestamp), ("+1888", 10))

    def test_receipt_and_typing_ignored(self):
        self.assertIsNone(parse_incoming({"envelope": {"receiptMessage": {}}}))
        self.assertIsNone(parse_incoming({"envelope": {"typingMessage": {}}}))
        self.assertIsNone(parse_incoming({"envelope": {}}))
        self.assertIsNone(parse_incoming({}))
        self.assertEqual(parse_events({"envelope": {"receiptMessage": {}}}), [])

    def test_profile_key_update_is_not_a_message(self):
        self.assertEqual(parse_events({"envelope": {"sourceNumber": "+1", "dataMessage": {
            "isProfileKeyUpdate": True}}}), [])


if __name__ == "__main__":
    unittest.main()
