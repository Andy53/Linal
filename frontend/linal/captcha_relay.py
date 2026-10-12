"""Receive a captcha solved on another device, over the local network.

Captchas solved in the phone's own browser are usually refused by Signal
(HTTP 403): hCaptcha scores the Librem 5's slow browser as a likely bot. So
while registration waits for a captcha, the phone can serve one small page on
its Wi-Fi address. A computer (or another phone) on the same network opens it,
follows its link to Signal's captcha page, and pastes or drops the resulting
"Open Signal" link back into the page, which hands it to the registration.

Kept deliberately small and closed:
  * binds only to a private (RFC 1918) Wi-Fi address, never to mobile data;
  * everything lives under a random, unguessable path; other paths are 404;
  * accepts one valid signalcaptcha:// link, then stops; also stops after a
    timeout or after too many bad submissions;
  * the page loads nothing from elsewhere and the token is never logged.
"""
from __future__ import annotations
import html
import ipaddress
import json
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .registration import CAPTCHA_URL, is_captcha_url
from . import logs
log = logs.get(__name__)

PREFERRED_PORT = 8463
MAX_BODY = 16 * 1024          # real links are ~2.2 KB
MAX_BAD = 20
TIMEOUT_S = 15 * 60


def lan_address(ip_json: Optional[str] = None) -> Optional[str]:
    """The phone's private IPv4 address on Wi-Fi (or Ethernet), if any.
    Mobile data (wwan*), USB networking and loopback are never used."""
    if ip_json is None:
        try:
            ip_json = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True,
                                     text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            return None
    try:
        ifaces = json.loads(ip_json or "[]")
    except ValueError:
        return None
    candidates = []
    for iface in ifaces:
        name = iface.get("ifname", "")
        if not name.startswith(("wl", "en", "eth")) or "UP" not in iface.get("flags", []):
            continue
        for a in iface.get("addr_info", []):
            try:
                addr = ipaddress.IPv4Address(a.get("local", ""))
            except ValueError:
                continue
            if addr.is_private and not addr.is_loopback and not addr.is_link_local:
                candidates.append((0 if name.startswith("wl") else 1, str(addr)))
    return min(candidates)[1] if candidates else None


class CaptchaRelay:
    """Serve the hand-off page until one valid link arrives (then call on_link
    from the server thread) or until stop()/timeout."""

    def __init__(self, on_link: Callable[[str], None], address: Optional[str] = None,
                 port: int = PREFERRED_PORT, timeout_s: float = TIMEOUT_S):
        self.on_link = on_link
        self.address = address
        self.port = port
        self.timeout_s = timeout_s
        self.secret = secrets.token_urlsafe(6)          # 8 chars, ~48 bits
        self._server: Optional[ThreadingHTTPServer] = None
        self._done = threading.Event()
        self._bad = 0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}/{self.secret}"

    def start(self) -> str:
        """Start serving; returns the URL to open on the other device.
        Raises OSError when there is no Wi-Fi address to serve on."""
        if self.address is None:
            self.address = lan_address()
        if not self.address:
            raise OSError("no Wi-Fi network")
        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer((self.address, self.port), handler)
        except OSError:                                  # preferred port busy
            self._server = ThreadingHTTPServer((self.address, 0), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._expire, daemon=True).start()
        log.info("captcha relay listening on %s:%d", self.address, self.port)
        return self.url

    def stop(self) -> None:
        self._done.set()
        srv, self._server = self._server, None
        if srv is not None:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()),
                             daemon=True).start()
            log.info("captcha relay stopped")

    def _expire(self) -> None:
        if not self._done.wait(self.timeout_s):
            log.info("captcha relay timed out")
            self.stop()

    # called by the handler: returns (status, message, link to deliver or None)
    def _submit(self, text: str) -> tuple[int, str, Optional[str]]:
        with self._lock:
            if self._done.is_set():
                return 410, "This page has already been used. Start again on the phone.", None
            link = (text or "").strip()
            if not is_captcha_url(link):
                self._bad += 1
                if self._bad >= MAX_BAD:
                    self.stop()
                return 400, ("That isn't the “Open Signal” link. It starts with "
                             "signalcaptcha:// — copy it again and paste it here."), None
            self._done.set()
        return 200, "Sent. Continue on your phone.", link

    # called by the handler after the reply has gone out
    def _deliver(self, link: str) -> None:
        import hashlib   # fingerprint only, never the token itself
        log.info("captcha link received over the network (%d chars, sha256 %s)",
                 len(link), hashlib.sha256(link.encode()).hexdigest()[:12])
        try:
            self.on_link(link)
        finally:
            self.stop()


def _make_handler(relay: CaptchaRelay):
    page = _PAGE.replace("{CAPTCHA_URL}", html.escape(CAPTCHA_URL, quote=True)) \
                .encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Linal"
        sys_version = ""

        def log_message(self, fmt, *args):       # keep the path (secret) out of logs
            pass

        def _ours(self) -> bool:
            return self.path.split("?", 1)[0] == "/" + relay.secret

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; "
                             "form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ours():
                self._send(404, b"Not found", "text/plain")
            elif relay._done.is_set():
                self._send(410, "This page has already been used. Start again on "
                                "the phone.".encode(), "text/plain; charset=utf-8")
            else:
                self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            if not self._ours():
                self._send(404, b"Not found", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._send(413, b"Too large", "text/plain")
                return
            text = self.rfile.read(n).decode("utf-8", "replace")
            status, msg, link = relay._submit(text)
            self._send(status, json.dumps({"message": msg}).encode(),
                       "application/json")
            if link:                             # reply first, then hand it over
                self.wfile.flush()
                relay._deliver(link)

    return Handler


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Linal captcha</title>
<style>
:root { color-scheme: light dark; --accent: #3a76f0; }
body { font: 17px/1.5 system-ui, sans-serif; max-width: 36rem; margin: 2rem auto;
       padding: 0 1rem; }
h1 { font-size: 1.4rem; }
ol { padding-left: 1.3rem; } li { margin: 0 0 1.2rem; }
a.button, button { display: inline-block; background: var(--accent); color: #fff;
       border: 0; border-radius: .5rem; padding: .6rem 1.1rem; font: inherit;
       text-decoration: none; cursor: pointer; }
textarea { width: 100%; box-sizing: border-box; min-height: 6rem; font: 14px monospace;
       border: 2px dashed #888; border-radius: .5rem; padding: .5rem; }
textarea.over { border-color: var(--accent); }
#status { font-weight: 600; min-height: 1.5em; }
.ok { color: #1a7f37; } .err { color: #c62828; }
</style></head><body>
<h1>Register Signal on your Librem 5</h1>
<ol>
<li><a class="button" href="{CAPTCHA_URL}" target="_blank" rel="noopener noreferrer">Open Signal's captcha</a>
  <br>Solve it in the new tab.</li>
<li>When it shows <b>“Open Signal”</b>, don't click it. Right-click it (long-press on a
  phone) and choose <b>Copy link</b>, or drag it into the box below.</li>
<li><textarea id="link" placeholder="Paste or drop the signalcaptcha:// link here"
  autocomplete="off" spellcheck="false"></textarea>
  <button id="send">Send to phone</button></li>
</ol>
<p id="status" role="status"></p>
<script>
const box = document.getElementById("link"), status = document.getElementById("status"),
      btn = document.getElementById("send");
function say(text, cls) { status.textContent = text; status.className = cls || ""; }
async function send() {
  const link = box.value.trim();
  if (!link.startsWith("signalcaptcha://")) {
    say("That isn't the “Open Signal” link yet. It starts with signalcaptcha://", "err");
    return;
  }
  btn.disabled = true; say("Sending…");
  try {
    const r = await fetch(location.pathname, {method: "POST",
      headers: {"Content-Type": "text/plain"}, body: link});
    const j = await r.json();
    say(j.message, r.ok ? "ok" : "err");
    if (r.ok) { box.disabled = true; return; }
  } catch (e) { say("Couldn't reach the phone. Is it still on the same Wi-Fi?", "err"); }
  btn.disabled = false;
}
btn.addEventListener("click", send);
box.addEventListener("dragover", e => { e.preventDefault(); box.classList.add("over"); });
box.addEventListener("dragleave", () => box.classList.remove("over"));
box.addEventListener("drop", e => {
  e.preventDefault(); box.classList.remove("over");
  const t = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
  box.value = (t || "").split(/\\r?\\n/).find(l => l && !l.startsWith("#")) || "";
  if (box.value.trim().startsWith("signalcaptcha://")) send();
});
box.addEventListener("paste", () => setTimeout(() => {
  if (box.value.trim().startsWith("signalcaptcha://")) send();
}, 0));
</script>
</body></html>
"""
