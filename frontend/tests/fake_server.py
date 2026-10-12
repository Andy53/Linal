"""A tiny fake signal-cli JSON-RPC daemon for tests.

Listens on an ephemeral TCP port, accepts one client, reads newline-delimited
JSON-RPC requests, and lets the test control responses, pushed notifications,
and raw byte framing.
"""
from __future__ import annotations
import json
import socket
import threading
import queue
from typing import Callable, Optional


class FakeSignalCli:
    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.conn: Optional[socket.socket] = None
        self.requests: "queue.Queue[dict]" = queue.Queue()
        # method -> callable(req_params) -> result  (raise to produce an error)
        self.responders: dict[str, Callable[[dict], object]] = {}
        # methods listed here get NO response (to exercise client timeouts)
        self.silent: set[str] = set()
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()
        self._connected = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def wait_connected(self, timeout=5.0) -> bool:
        return self._connected.wait(timeout)

    def _accept(self):
        self.conn, _ = self._srv.accept()
        self._connected.set()
        buf = b""
        while True:
            try:
                chunk = self.conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self._handle(json.loads(line))

    def _handle(self, req: dict):
        self.requests.put(req)
        method = req.get("method")
        if method in self.silent:
            return
        rid = req.get("id")
        resp = {"jsonrpc": "2.0", "id": rid}
        try:
            if method in self.responders:
                resp["result"] = self.responders[method](req.get("params") or {})
            else:
                resp["result"] = {}
        except Exception as e:  # noqa: BLE001
            resp["error"] = {"code": -1, "message": str(e)}
        self.send_raw((json.dumps(resp) + "\n").encode())

    # -- test controls -------------------------------------------------------
    def send_raw(self, data: bytes):
        assert self.conn is not None, "no client connected yet"
        self.conn.sendall(data)

    def push_notification(self, method: str, params: dict):
        self.send_raw((json.dumps({"jsonrpc": "2.0", "method": method,
                                   "params": params}) + "\n").encode())

    def close_client(self):
        if self.conn:
            try:
                # shutdown() (unlike close()) interrupts a recv() blocked in the
                # accept thread and sends a FIN, so the client sees EOF promptly.
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.conn.close()
            self.conn = None

    def shutdown(self):
        self.close_client()
        try:
            self._srv.close()
        except OSError:
            pass
