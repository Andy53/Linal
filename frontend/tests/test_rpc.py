import os, sys, time, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.rpc import SignalRpc, RpcError
from tests.fake_server import FakeSignalCli


class RpcTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeSignalCli()
        self.rpc = SignalRpc(port=self.srv.port, timeout=5)
        self.rpc.connect()
        self.assertTrue(self.srv.wait_connected(), "server never saw the client")

    def tearDown(self):
        self.rpc.close()
        self.srv.shutdown()

    def test_request_response_by_id(self):
        self.srv.responders["version"] = lambda p: {"version": "0.14.8"}
        self.assertEqual(self.rpc.call("version"), {"version": "0.14.8"})

    def test_params_are_forwarded(self):
        seen = {}
        self.srv.responders["send"] = lambda p: seen.update(p) or {"timestamp": 42}
        self.rpc.call("send", {"recipient": ["+1555"], "message": "hi"})
        self.assertEqual(seen["recipient"], ["+1555"])
        self.assertEqual(seen["message"], "hi")

    def test_concurrent_calls_matched_to_right_ids(self):
        # responder echoes which method so we can verify no cross-talk
        self.srv.responders["a"] = lambda p: {"who": "a"}
        self.srv.responders["b"] = lambda p: {"who": "b"}
        results = {}
        def do(name):
            results[name] = self.rpc.call(name)
        t1 = threading.Thread(target=do, args=("a",))
        t2 = threading.Thread(target=do, args=("b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results["a"], {"who": "a"})
        self.assertEqual(results["b"], {"who": "b"})

    def test_error_response_raises(self):
        def boom(p):
            raise ValueError("nope")
        self.srv.responders["bad"] = boom
        with self.assertRaises(RpcError) as ctx:
            self.rpc.call("bad")
        self.assertIn("nope", str(ctx.exception))

    def test_timeout(self):
        self.srv.silent.add("hang")
        with self.assertRaises(RpcError) as ctx:
            self.rpc.call("hang", timeout=0.5)
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_notification_dispatch(self):
        got = []
        self.rpc.on_notification = lambda m, p: got.append((m, p))
        self.srv.push_notification("receive", {"envelope": {"timestamp": 1}})
        for _ in range(50):
            if got:
                break
            time.sleep(0.02)
        self.assertEqual(got[0][0], "receive")
        self.assertEqual(got[0][1]["envelope"]["timestamp"], 1)

    def test_split_frame_reassembled(self):
        # A response delivered across two TCP writes must still parse.
        self.srv.silent.add("slow")  # we answer manually, split
        def answer_split():
            req = self.srv.requests.get(timeout=3)
            payload = ('{"jsonrpc":"2.0","id":%s,"result":{"ok":true}}\n' % req["id"]).encode()
            self.srv.send_raw(payload[:10])
            time.sleep(0.05)
            self.srv.send_raw(payload[10:])
        threading.Thread(target=answer_split, daemon=True).start()
        self.assertEqual(self.rpc.call("slow"), {"ok": True})

    def test_two_messages_one_chunk(self):
        # Two JSON objects arriving in a single packet both dispatch.
        self.srv.silent.add("x")
        notes = []
        self.rpc.on_notification = lambda m, p: notes.append(m)
        def answer():
            req = self.srv.requests.get(timeout=3)
            blob = ('{"jsonrpc":"2.0","method":"receive","params":{}}\n'
                    '{"jsonrpc":"2.0","id":%s,"result":{"ok":1}}\n' % req["id"]).encode()
            self.srv.send_raw(blob)
        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(self.rpc.call("x"), {"ok": 1})
        self.assertIn("receive", notes)

    def test_disconnect_callback(self):
        fired = threading.Event()
        self.rpc.on_disconnect = fired.set
        self.srv.close_client()
        self.assertTrue(fired.wait(2.0), "on_disconnect was not called")
        self.assertFalse(self.rpc.connected)


class UnixSocketTest(unittest.TestCase):
    """Default transport: a Unix socket, reachable only by this user."""
    def test_default_path_and_round_trip(self):
        import json, os, socket, tempfile, threading
        from linal.rpc import SignalRpc, default_socket_path
        self.assertTrue(default_socket_path().endswith("/linal/socket"))
        self.assertIsNone(SignalRpc(port=7583).socket_path)       # TCP only if asked
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "socket")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)

            def serve():
                c, _ = srv.accept()
                req = json.loads(c.makefile().readline())
                c.sendall((json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                       "result": {"version": "x"}}) + "\n").encode())
            threading.Thread(target=serve, daemon=True).start()
            rpc = SignalRpc(socket_path=path, timeout=5)
            rpc.connect()
            self.assertEqual(rpc.call("version", timeout=5), {"version": "x"})
            clone = rpc.clone()
            self.assertEqual((clone.socket_path, clone.port), (path, None))
            rpc.close()
            srv.close()


if __name__ == "__main__":
    unittest.main()
