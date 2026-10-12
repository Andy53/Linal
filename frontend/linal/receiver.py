"""Always-on receiver: stays connected to the signal-cli daemon, persists every
event (messages, reactions, receipts, edits, deletes, calls) into the Store so
nothing is missed while the GUI is closed, and raises desktop notifications.
Incoming calls ring (feedbackd) and bring up the app. Runs as a
`systemd --user` service.
"""
from __future__ import annotations
import time
from .rpc import SignalRpc
from .store import Store, open_account_store
from .envelope import MessageEvent, parse_events
from .backend import call_event_from, record_call_event
from . import notify
from . import logs
log = logs.get(__name__)


def handle_receive(store: Store, params: dict, notifier=None) -> list[MessageEvent]:
    """Apply one 'receive' notification. Returns new messages worth notifying."""
    fresh: list[MessageEvent] = []
    focus = store.focused_conversation()
    for ev in parse_events(params):
        new = store.apply_event(ev)
        if new is not None and not new.message.outgoing and new.conv_key != focus:
            fresh.append(new)
    if notifier:
        for ev in fresh:
            notifier.message(ev.conv_key, _title(store, ev), ev.message,
                             store.unread_count(ev.conv_key))
    return fresh


def handle_call_event(store: Store, ev: dict, notifier=None) -> None:
    record_call_event(store, ev)
    if not notifier or not ev:
        return
    peer = ev.get("number") or ev.get("uuid") or ""
    state = ev.get("state") or ""
    if state == "RINGING_INCOMING":
        cid = str(ev.get("callId"))
        # signal-cli sends RINGING_INCOMING twice (again once the audio devices
        # exist). Ring/notify once, or a second token would replace the one in
        # the notification and its Answer button would stop working.
        if getattr(notifier, "knows_call", lambda _c: False)(cid):
            return
        notifier.incoming_call(cid, peer, store.display_name(peer),
                               answer_token=store.issue_answer_token(cid))
    elif state in ("CONNECTED", "ENDED", "CONNECTING"):
        notifier.stop_ringing(str(ev.get("callId")))
        if state == "ENDED" and not ev.get("isOutgoing"):
            calls = [c for c in store.get_calls(peer) if c.call_id == str(ev.get("callId"))]
            if calls and calls[0].missed:
                notifier.missed_call(peer, store.display_name(peer))


def _title(store: Store, ev: MessageEvent) -> str:
    if ev.is_group:
        group = next((c.title for c in store.get_conversations() if c.key == ev.conv_key), "Group")
        return f"{ev.message.author or store.display_name(ev.message.author_id)} · {group}"
    return store.display_name(ev.conv_key)


def run(socket_path=None):
    from . import migrate
    migrate.move_data()             # first start after the Signal-L5 -> Linal rename
    store = Store()
    prefs = store.get_meta("prefs", {}) or {}
    logs.setup("receiver", detailed=bool(prefs.get("detailedLogs")))
    current = {"rpc": None, "account": None, "store": store}

    def reject(call_id: str):
        # "Decline" tapped on the incoming-call notification
        rpc, acct = current["rpc"], current["account"]
        if rpc and acct:
            try:
                rpc.call("rejectCall", {"account": acct, "callId": int(call_id)})
            except Exception as e:  # noqa: BLE001
                log.error("reject call failed: %s", e)

    notifier = notify.Notifier(on_reject=reject)
    while True:
        rpc = SignalRpc(socket_path=socket_path)
        current["rpc"] = rpc
        done = {"flag": False}

        def on_note(method, params):
            try:
                if method == "receive":
                    handle_receive(current["store"], params, notifier)
                elif method == "callEvent":
                    handle_call_event(current["store"], call_event_from(params), notifier)
            except Exception as e:  # noqa: BLE001 — never let one bad event kill the receiver
                log.exception("failed to handle %s: %s", method, e)
        rpc.on_notification = on_note
        rpc.on_disconnect = lambda: done.__setitem__("flag", True)
        try:
            rpc.connect()
            log.info("connected to the daemon")
            accts = rpc.call("listAccounts") or []
            current["account"] = accts[0].get("number") if accts else None
            if current["account"]:
                current["store"] = open_account_store(current["account"])
            for a in accts:
                try:
                    rpc.call("subscribeCallEvents", {"account": a.get("number")})
                except Exception:  # noqa: BLE001
                    pass
            while not done["flag"]:
                time.sleep(1)
        except Exception as e:  # noqa: BLE001
            log.warning("daemon connection error: %s", e)
        finally:
            rpc.close()
        log.info("disconnected from the daemon; retrying in 5s")
        time.sleep(5)


if __name__ == "__main__":
    run()
