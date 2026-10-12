"""Parse signal-cli 'receive' notification envelopes into typed events.

One envelope can carry a new message, a reaction, a receipt, a typing
indicator, a remote delete, an edit, or a sync transcript of something this
account did on another device. parse_events() maps each to an event the store
and UI understand. Pure/testable — no GTK or I/O.

Field names follow signal-cli's JSON output (JsonMessageEnvelope & friends).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, Union
from .models import Attachment, Message, Quote


@dataclass
class MessageEvent:
    conv_key: str
    title: str          # best display title for the conversation
    is_group: bool
    message: Message


@dataclass
class ReactionEvent:
    conv_key: str
    target_author: str  # number/uuid of the reacted-to message's author
    target_ts: int
    emoji: str
    reactor: str        # number/uuid of whoever reacted ("" = this account)
    remove: bool


@dataclass
class ReceiptEvent:
    sender: str         # who sent the receipt (the recipient of our message)
    timestamps: list[int]
    kind: str           # "delivered" | "read"


@dataclass
class TypingEvent:
    conv_key: str
    who: str
    started: bool


@dataclass
class DeleteEvent:
    conv_key: str
    target_ts: int


@dataclass
class EditEvent:
    conv_key: str
    target_ts: int
    body: str


@dataclass
class ReadSyncEvent:
    """We read a message on another (linked) device. Only the sender and the
    message timestamp are given (no group id), so the store looks it up."""
    sender: str
    timestamp: int


Event = Union[MessageEvent, ReactionEvent, ReceiptEvent, TypingEvent,
              DeleteEvent, EditEvent, ReadSyncEvent]


def _attachments(dm: dict) -> list[Attachment]:
    return [Attachment(id=a.get("id") or "", content_type=a.get("contentType") or "",
                       filename=a.get("filename") or "", size=a.get("size") or 0,
                       voice_note=bool(a.get("isVoiceNote")))
            for a in (dm.get("attachments") or [])]


def _quote(dm: dict) -> Optional[Quote]:
    q = dm.get("quote")
    if not q:
        return None
    return Quote(timestamp=q.get("id") or 0,
                 author_id=q.get("authorNumber") or q.get("authorUuid") or q.get("author") or "",
                 text=q.get("text") or "")


def _group(dm: dict) -> Tuple[Optional[str], Optional[str]]:
    gi = dm.get("groupInfo") or {}
    return gi.get("groupId"), gi.get("groupName")


def _data_events(dm: dict, conv_key: str, title: str, is_group: bool,
                 outgoing: bool, author: str, author_id: str, ts: int) -> list[Event]:
    """Events from one dataMessage (incoming, or a sync transcript of our own)."""
    events: list[Event] = []
    if dm.get("reaction"):
        r = dm["reaction"]
        events.append(ReactionEvent(
            conv_key=conv_key,
            target_author=r.get("targetAuthorNumber") or r.get("targetAuthorUuid") or r.get("targetAuthor") or "",
            target_ts=r.get("targetSentTimestamp") or 0,
            emoji=r.get("emoji") or "", reactor="" if outgoing else author_id,
            remove=bool(r.get("isRemove"))))
        return events
    if dm.get("remoteDelete"):
        events.append(DeleteEvent(conv_key=conv_key,
                                  target_ts=dm["remoteDelete"].get("timestamp") or 0))
        return events
    atts = _attachments(dm)
    body = dm.get("message")
    if body is None and not atts:
        return events   # profile-key updates, expiration timer changes, etc.
    msg = Message(timestamp=dm.get("timestamp") or ts, body=body or "", outgoing=outgoing,
                  author="" if outgoing else author, author_id="" if outgoing else author_id,
                  attachments=atts, quote=_quote(dm), status="sent" if outgoing else "")
    events.append(MessageEvent(conv_key=conv_key, title=title, is_group=is_group, message=msg))
    return events


def parse_events(params: dict) -> list[Event]:
    env = (params or {}).get("envelope") or {}
    source_id = env.get("sourceNumber") or env.get("sourceUuid") or env.get("source") or ""
    source_name = env.get("sourceName") or source_id
    ts = env.get("timestamp") or 0
    events: list[Event] = []

    # 1) incoming message / reaction / delete
    dm = env.get("dataMessage")
    if dm:
        gid, gname = _group(dm)
        if gid:
            key, title, is_group = gid, gname or "", True
        else:
            key, title, is_group = source_id or "unknown", source_name or "", False
        events += _data_events(dm, key, title, is_group, False, source_name, source_id, ts)

    # 2) an incoming edit of an earlier message
    em = env.get("editMessage")
    if em and em.get("dataMessage"):
        edm = em["dataMessage"]
        gid, _ = _group(edm)
        events.append(EditEvent(conv_key=gid or source_id, target_ts=em.get("targetSentTimestamp") or 0,
                                body=edm.get("message") or ""))

    # 3) receipts for messages we sent
    rm = env.get("receiptMessage")
    if rm and (rm.get("isDelivery") or rm.get("isRead") or rm.get("isViewed")):
        events.append(ReceiptEvent(sender=source_id, timestamps=list(rm.get("timestamps") or []),
                                   kind="delivered" if rm.get("isDelivery") else "read"))

    # 4) typing indicators
    tm = env.get("typingMessage")
    if tm and tm.get("action"):
        events.append(TypingEvent(conv_key=tm.get("groupId") or source_id, who=source_name,
                                  started=tm["action"] == "STARTED"))

    # 5) sync messages: things this account did on another device
    sm = env.get("syncMessage") or {}
    sent = sm.get("sentMessage")
    if sent:
        gid, gname = _group(sent)
        if gid:
            key, title, is_group = gid, gname or "", True
        else:
            key = sent.get("destinationNumber") or sent.get("destinationUuid") or sent.get("destination") or "unknown"
            title, is_group = sent.get("destinationName") or "", False
        events += _data_events(sent, key, title, is_group, True, "", "", sent.get("timestamp") or ts)
    for rd in sm.get("readMessages") or []:
        who = rd.get("senderNumber") or rd.get("senderUuid") or rd.get("sender") or ""
        if who:
            events.append(ReadSyncEvent(sender=who, timestamp=rd.get("timestamp") or 0))

    return events


def parse_incoming(params: dict) -> Optional[Tuple[str, str, Message]]:
    """Back-compat: the first new message in the envelope as (key, title, msg)."""
    for ev in parse_events(params):
        if isinstance(ev, MessageEvent):
            return ev.conv_key, ev.title or ev.conv_key, ev.message
    return None
