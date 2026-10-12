"""Route a Signal call's audio to the phone's mic and earpiece/speaker.

signal-call-tunnel (spawned by signal-cli per call) creates virtual PulseAudio
devices, named in the call event (ringrtc's Linux virtual_audio layout):

    mic  --loopback-->  sink_for_<inputDeviceName>   (WebRTC records its monitor)
    <outputDeviceName>.monitor  --loopback-->  earpiece/speaker

callaudiod (org.mobian_project.CallAudio, used by GNOME Calls on the Librem 5)
switches the codec into call mode (earpiece + handset mic) and handles the
speaker and mute toggles. Everything degrades gracefully: if callaudiod is
missing we still bridge to the current default devices.
"""
from __future__ import annotations
import subprocess
import time
from typing import Optional
from . import logs
log = logs.get(__name__)

CALLAUDIO = ("org.mobian_project.CallAudio", "/org/mobian_project/CallAudio",
             "org.mobian_project.CallAudio")
LATENCY_MS = 40


def loopback_args(source: str, sink: str) -> list[str]:
    return ["pactl", "load-module", "module-loopback", f"source={source}", f"sink={sink}",
            f"latency_msec={LATENCY_MS}", "source_dont_move=true", "sink_dont_move=false"]


def tunnel_endpoints(input_device: str, output_device: str) -> tuple[str, str]:
    """(sink we write the mic into, source we play to the speaker)."""
    return f"sink_for_{input_device}", f"{output_device}.monitor"


class CallAudio:
    def __init__(self, run=subprocess.run):
        self._run = run
        self._modules: list[str] = []
        self.speaker = False
        self.muted = False
        self.active = False

    # -- helpers -------------------------------------------------------------
    def _pactl(self, *args) -> str:
        r = self._run(["pactl", *args], capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()

    def _default(self, kind: str) -> str:
        out = self._pactl(f"get-default-{kind}")
        if out:
            return out
        for line in self._pactl("info").splitlines():   # older pactl
            if line.lower().startswith(f"default {kind}:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _callaudiod(self, method: str, sig: str, value) -> bool:
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(CALLAUDIO[0], CALLAUDIO[1], CALLAUDIO[2], method,
                          GLib.Variant(f"({sig})", (value,)), None,
                          Gio.DBusCallFlags.NONE, 3000, None)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("callaudiod %s unavailable: %s", method, e)
            return False

    # -- lifecycle -----------------------------------------------------------
    def start(self, input_device: str, output_device: str) -> bool:
        if self.active or not input_device or not output_device:
            return self.active
        if self._callaudiod("SelectMode", "u", 1):   # 1 = call mode
            time.sleep(0.4)                         # let the UCM profile switch settle
        mic, spk = self._default("source"), self._default("sink")
        into_tunnel, from_tunnel = tunnel_endpoints(input_device, output_device)
        for args in (loopback_args(mic, into_tunnel), loopback_args(from_tunnel, spk)):
            r = self._run(args, capture_output=True, text=True, timeout=5)
            mod = (r.stdout or "").strip()
            if r.returncode == 0 and mod:
                self._modules.append(mod)
            else:
                log.error("audio loopback failed: %s: %s", ' '.join(args[2:]), (r.stderr or '').strip())
        self.active = bool(self._modules)
        log.info("call audio %s (mic=%s speaker=%s, %d loopbacks)",
                 "bridged" if self.active else "NOT bridged", mic, spk, len(self._modules))
        return self.active

    def stop(self) -> None:
        for mod in self._modules:
            self._run(["pactl", "unload-module", mod], capture_output=True, timeout=5)
        if self._modules:
            log.info("call audio stopped (%d loopbacks removed)", len(self._modules))
        self._modules.clear()
        if self.active:
            self._callaudiod("SelectMode", "u", 0)  # back to normal (media) mode
        self.active = False
        self.speaker = False
        self.muted = False

    def set_speaker(self, on: bool) -> None:
        self.speaker = on
        self._callaudiod("EnableSpeaker", "b", on)

    def set_muted(self, on: bool) -> None:
        self.muted = on
        if not self._callaudiod("MuteMic", "b", on):
            src = self._default("source")
            if src:
                self._pactl("set-source-mute", src, "1" if on else "0")
