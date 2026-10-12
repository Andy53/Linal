"""Canned data so the entire UI runs with no Signal account."""
from __future__ import annotations
import time
from .backend import Backend
from .models import Account, Conversation, Message


def _m(body, outgoing, ago=0, author=""):
    return Message(timestamp=int((time.time() - ago) * 1000), body=body,
                   outgoing=outgoing, author=author)


class MockBackend(Backend):
    def __init__(self):
        self._convs = [
            Conversation(key="+15551230001", title="Alex Rivera", messages=[
                _m("Hey, is the Librem build working?", False, 3600, "Alex Rivera"),
                _m("Getting there — backend's up on the phone.", True, 3500),
                _m("Nice. Native GTK?", False, 3400, "Alex Rivera"),
            ]),
            Conversation(key="+15551230002", title="Mum", messages=[
                _m("Call me when you get a chance ❤️", False, 86400, "Mum"),
            ]),
            Conversation(key="group.devs", title="L5 Tinkerers", is_group=True, messages=[
                _m("Anyone got VoLTE stable on T-Mobile?", False, 7200, "Sam"),
                _m("Data + SMS yes, voice is flaky.", False, 7100, "Jo"),
                _m("Same here.", True, 7000),
            ]),
        ]

    def start(self):
        if self.on_connection_change:
            self.on_connection_change(True)

    def stop(self):
        pass

    def list_accounts(self):
        return [Account(number="+15550000000 (mock)", registered=True)]

    def is_registered(self):
        return True

    def list_conversations(self):
        return list(self._convs)

    def get_messages(self, conv_key):
        for c in self._convs:
            if c.key == conv_key:
                return list(c.messages)
        return []

    def latest_ts(self):
        return max((m.timestamp for c in self._convs for m in c.messages), default=0)

    def send(self, conv_key, text, attachments=None, quote=None):
        msg = Message(timestamp=int(time.time() * 1000), body=text, outgoing=True, status="sent")
        for c in self._convs:
            if c.key == conv_key:
                c.messages.append(msg)
        return msg
