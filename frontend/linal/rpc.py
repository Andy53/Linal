"""Minimal JSON-RPC client for signal-cli's daemon (--socket mode, Unix socket).

signal-cli speaks newline-delimited JSON-RPC over the socket: we send request
objects and the daemon replies with responses (matched by id) and pushes
`receive` *notifications* (no id) as messages arrive. A background reader thread
demultiplexes the two.
"""
from __future__ import annotations
import json
import os
import socket
import threading
import itertools
import time
from typing import Any, Callable, Optional

from . import logs
log = logs.get(__name__)


def default_socket_path() -> str:
    """Where linal-daemon.service puts the JSON-RPC socket (%t/linal)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "linal", "socket")


class RpcError(Exception):
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("message", str(payload)))


class SignalRpc:
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 timeout: float = 30.0, socket_path: Optional[str] = None):
        """Connects to the daemon's Unix socket (default_socket_path()) unless a
        TCP port is given explicitly (tests, development).

        The daemon must not listen on TCP: any local process, and any web page
        able to POST to 127.0.0.1, could then drive the account (signal-cli's
        reader skips non-JSON lines such as HTTP headers). The socket lives in
        $XDG_RUNTIME_DIR, which only this user can enter."""
        self.host, self.port, self.timeout = host or "127.0.0.1", port, timeout
        self.socket_path = None if port is not None else (socket_path or default_socket_path())
        self._sock: Optional[socket.socket] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._events = threading.Event()  # set when a response for any id arrives
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self.on_notification: Optional[Callable[[str, dict], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    # -- connection ----------------------------------------------------------
    def clone(self) -> "SignalRpc":
        """A new, unconnected client for the same daemon address."""
        return SignalRpc(self.host if self.port is not None else None, self.port,
                         self.timeout, self.socket_path)

    def connect(self) -> None:
        if self.socket_path is not None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.socket_path)
            except OSError:
                s.close()
                raise
            self._sock = s
        else:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(None)
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._running

    # -- request/response ----------------------------------------------------
    def call(self, method: str, params: Optional[dict] = None, timeout: float = 60.0) -> Any:
        if not self._sock:
            raise RpcError({"message": "not connected"})
        rid = next(self._ids)
        started = time.monotonic()
        req = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            req["params"] = params
        done = threading.Event()
        with self._lock:
            self._pending[rid] = {"event": done, "result": None, "error": None}
        with self._send_lock:   # calls come from GTK and worker threads; keep lines whole
            self._sock.sendall((json.dumps(req) + "\n").encode())
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            log.warning("rpc %s timed out after %.0fs", method, timeout)
            raise RpcError({"message": f"timeout waiting for {method}"})
        with self._lock:
            slot = self._pending.pop(rid)
        took = time.monotonic() - started
        if slot["error"] is not None:
            # never log params: they can hold message text
            log.warning("rpc %s failed after %.1fs: %s", method, took,
                        (slot["error"] or {}).get("message"))
            raise RpcError(slot["error"])
        log.debug("rpc %s ok (%.2fs)", method, took)
        return slot["result"]

    # -- reader loop ---------------------------------------------------------
    def _read_loop(self) -> None:
        buf = b""
        while self._running and self._sock:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    self._dispatch(line)
        self._running = False
        log.info("daemon connection closed")
        if self.on_disconnect:
            self.on_disconnect()

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            with self._lock:
                slot = self._pending.get(rid)
                if slot:
                    slot["result"] = msg.get("result")
                    slot["error"] = msg.get("error")
                    slot["event"].set()
        elif msg.get("method"):  # a notification pushed by the daemon
            if self.on_notification:
                self.on_notification(msg["method"], msg.get("params") or {})
