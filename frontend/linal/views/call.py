"""Full-screen voice call page (overlays the chat UI, like Android's call screen).

Driven by signal-cli call events. States: RINGING_INCOMING (answer/decline),
RINGING_OUTGOING / CONNECTING ("Calling…"), CONNECTED (timer, mute, speaker),
RECONNECTING, ENDED.
"""
from __future__ import annotations
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject  # noqa: E402

STATUS_TEXT = {
    "RINGING_INCOMING": "Incoming Signal voice call",
    "RINGING_OUTGOING": "Ringing…",
    "CONNECTING": "Connecting…",
    "CONNECTED": "",
    "RECONNECTING": "Reconnecting…",
    "ENDED": "Call ended",
}


def _round_button(icon, css, tooltip):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, halign=Gtk.Align.CENTER)
    b.add_css_class("circular")
    b.add_css_class("call-button")
    if css:
        b.add_css_class(css)
    return b


class CallPage(Gtk.Box):
    __gsignals__ = {
        "answer": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "decline": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "hangup": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "mute": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "speaker": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                         valign=Gtk.Align.FILL, vexpand=True)
        self.add_css_class("call-page")
        self.state = ""
        self._connected_at = 0.0
        self._timer = 0

        self.append(Gtk.Box(vexpand=True))
        self.avatar = Adw.Avatar(size=128, show_initials=True, halign=Gtk.Align.CENTER)
        self.append(self.avatar)
        self.name = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.name.add_css_class("title-1")
        self.append(self.name)
        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        self.append(self.status)
        self.append(Gtk.Box(vexpand=True))

        # in-call toggles
        self.toggles = Gtk.Box(spacing=36, halign=Gtk.Align.CENTER)
        self.mute_btn = Gtk.ToggleButton(icon_name="microphone-sensitivity-muted-symbolic",
                                         tooltip_text="Mute")
        self.mute_btn.add_css_class("circular")
        self.mute_btn.add_css_class("call-button")
        self.mute_btn.connect("toggled", lambda b: self.emit("mute", b.get_active()))
        self.speaker_btn = Gtk.ToggleButton(icon_name="audio-speakers-symbolic",
                                            tooltip_text="Speaker")
        self.speaker_btn.add_css_class("circular")
        self.speaker_btn.add_css_class("call-button")
        self.speaker_btn.connect("toggled", lambda b: self.emit("speaker", b.get_active()))
        self.toggles.append(self.mute_btn)
        self.toggles.append(self.speaker_btn)
        self.append(self.toggles)

        # answer / decline / hang up
        self.actions = Gtk.Box(spacing=64, halign=Gtk.Align.CENTER, margin_bottom=48, margin_top=18)
        self.decline_btn = _round_button("call-stop-symbolic", "destructive-action", "Decline")
        self.decline_btn.connect("clicked", lambda *_: self.emit("decline"))
        self.answer_btn = _round_button("call-start-symbolic", "success", "Answer")
        self.answer_btn.connect("clicked", lambda *_: self.emit("answer"))
        self.hangup_btn = _round_button("call-stop-symbolic", "destructive-action", "Hang up")
        self.hangup_btn.connect("clicked", lambda *_: self.emit("hangup"))
        for b in (self.decline_btn, self.answer_btn, self.hangup_btn):
            self.actions.append(b)
        self.append(self.actions)

    def setup(self, name: str):
        self.name.set_label(name)
        self.avatar.set_text(name)
        self.mute_btn.set_active(False)
        self.speaker_btn.set_active(False)
        self._connected_at = 0.0

    def set_state(self, state: str):
        self.state = state
        incoming = state == "RINGING_INCOMING"
        self.answer_btn.set_visible(incoming)
        self.decline_btn.set_visible(incoming)
        self.hangup_btn.set_visible(not incoming and state != "ENDED")
        self.toggles.set_visible(state in ("CONNECTED", "RECONNECTING", "CONNECTING",
                                           "RINGING_OUTGOING"))
        if state == "CONNECTED" and not self._connected_at:
            self._connected_at = time.time()
        if state == "CONNECTED" and not self._timer:
            self._timer = GLib.timeout_add_seconds(1, self._on_timer)
        self._tick()

    def _on_timer(self):
        self._tick()
        if self.state == "ENDED":
            self._timer = 0
            return False
        return True

    def _tick(self):
        if self.state == "CONNECTED" and self._connected_at:
            s = int(time.time() - self._connected_at)
            self.status.set_label(f"{s // 60}:{s % 60:02d}")
        else:
            self.status.set_label(STATUS_TEXT.get(self.state, self.state.title()))
