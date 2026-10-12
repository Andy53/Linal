"""Plain data models shared by the mock and real backends."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    number: str
    registered: bool = False


@dataclass
class Contact:
    # Signal identifies recipients by UUID and/or E.164 number.
    uuid: Optional[str] = None
    number: Optional[str] = None
    name: str = ""
    on_signal: Optional[bool] = None   # None = not checked yet

    @property
    def display(self) -> str:
        return self.name or self.number or (self.uuid or "unknown")


@dataclass
class Attachment:
    id: str = ""                # signal-cli attachment id (file name in its store)
    content_type: str = ""
    filename: str = ""
    size: int = 0
    path: str = ""              # local file, when known (outgoing, or resolved incoming)
    voice_note: bool = False

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def is_audio(self) -> bool:
        return self.content_type.startswith("audio/")

    def to_dict(self) -> dict:
        return {"id": self.id, "contentType": self.content_type, "filename": self.filename,
                "size": self.size, "path": self.path, "isVoiceNote": self.voice_note}

    @classmethod
    def from_dict(cls, d: dict) -> "Attachment":
        return cls(id=d.get("id") or "", content_type=d.get("contentType") or "",
                   filename=d.get("filename") or "", size=d.get("size") or 0,
                   path=d.get("path") or "", voice_note=bool(d.get("isVoiceNote")))


@dataclass
class Quote:
    timestamp: int              # sent-timestamp of the quoted message
    author_id: str = ""         # number/uuid of the quoted message's author
    text: str = ""

    def to_dict(self) -> dict:
        return {"id": self.timestamp, "author": self.author_id, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Quote":
        return cls(timestamp=d.get("id") or 0, author_id=d.get("author") or "",
                   text=d.get("text") or "")


# outgoing delivery states, in the order they can only advance
STATUS_ORDER = ["", "sent", "delivered", "read"]


@dataclass
class Message:
    timestamp: int          # signal-cli uses ms-since-epoch as the message id
    body: str
    outgoing: bool
    author: str = ""        # display name of sender (incoming)
    pending: bool = False   # sent locally, not yet confirmed
    author_id: str = ""     # sender number/uuid (needed to react to / quote it)
    attachments: list[Attachment] = field(default_factory=list)
    quote: Optional[Quote] = None
    reactions: dict[str, list[str]] = field(default_factory=dict)  # emoji -> reactor ids
    status: str = ""        # outgoing: "", sent, delivered, read
    deleted: bool = False   # remote-deleted ("This message was deleted")
    edited: bool = False

    @property
    def preview(self) -> str:
        if self.deleted:
            return "This message was deleted"
        if self.body:
            return self.body
        if self.attachments:
            a = self.attachments[0]
            if a.voice_note:
                return "🎤 Voice message"
            if a.is_image:
                return "📷 Photo"
            return "📎 " + (a.filename or "Attachment")
        return ""


@dataclass
class Conversation:
    key: str                # recipient number/uuid or group id
    title: str
    is_group: bool = False
    messages: list[Message] = field(default_factory=list)
    unread: int = 0

    @property
    def last_preview(self) -> str:
        return self.messages[-1].preview if self.messages else ""

    @property
    def last_ts(self) -> int:
        return self.messages[-1].timestamp if self.messages else 0


@dataclass
class CallRecord:
    call_id: str
    conv_key: str
    timestamp: int          # ms; when the call started ringing
    outgoing: bool
    state: str = ""         # last CallInfo state seen (e.g. ENDED)
    answered: bool = False  # reached CONNECTED at some point
    duration: int = 0       # seconds connected

    @property
    def missed(self) -> bool:
        return not self.outgoing and not self.answered and self.state == "ENDED"
