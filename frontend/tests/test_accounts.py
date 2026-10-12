import json, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.accounts import local_accounts, logged_out, signal_cli_dir
from linal.backend import RealBackend
from linal.models import Message
from linal.store import Store, account_path, default_path, open_account_store


class FakeRpc:
    def __init__(self, loaded=(), fail=()):
        self.loaded, self.fail, self.calls = list(loaded), set(fail), []
        self.on_notification = self.on_disconnect = None
    def connect(self): pass
    def close(self): pass
    def clone(self): return self
    def call(self, method, params=None, timeout=60.0):
        self.calls.append((method, params))
        if method in self.fail:
            raise OSError("Failed to get response for request")
        if method == "listAccounts":
            return [{"number": n} for n in self.loaded]
        return {}
    def deleted(self):
        return [p["account"] for m, p in self.calls if m == "deleteLocalAccountData"]


def write_cli(d, accounts):
    """signal-cli's files: accounts.json plus one file per account, and a
    received attachment in its shared folder."""
    data = os.path.join(d, "data")
    os.makedirs(data, exist_ok=True)
    os.makedirs(os.path.join(d, "attachments"), exist_ok=True)
    open(os.path.join(d, "attachments", "photo.jpg"), "w").close()
    entries = []
    for i, (number, aci, registered) in enumerate(accounts):
        entries.append({"path": str(i), "number": number, "uuid": aci})
        with open(os.path.join(data, str(i)), "w") as f:
            json.dump({"number": number, "registered": registered}, f)
    with open(os.path.join(data, "accounts.json"), "w") as f:
        json.dump({"accounts": entries, "version": 2}, f)


class AccountFilesTest(unittest.TestCase):
    def test_config_from_daemon_command_line(self):
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "42"))
            with open(os.path.join(proc, "42", "cmdline"), "wb") as f:
                f.write(b"java\0-jar\0x\0--config\0/srv/cli\0daemon\0")
            run = lambda *a, **k: mock.Mock(stdout="42\n")  # noqa: E731
            self.assertEqual(signal_cli_dir(run, proc), "/srv/cli")

    def test_default_config(self):
        run = lambda *a, **k: mock.Mock(stdout="0\n")  # noqa: E731
        self.assertEqual(signal_cli_dir(run, "/nonexistent", {"XDG_DATA_HOME": "/d"}), "/d/signal-cli")

    def test_logged_out_needs_aci_unregistered_and_not_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            write_cli(d, [("+1111", "aci-1", False),     # Signal refused it: logged out
                          ("+2222", "aci-2", True),      # offline: still registered
                          ("+3333", None, False),        # mid-registration
                          ("+4444", "aci-4", False)])    # unregistered but loaded
            accts = local_accounts(d)
            self.assertEqual(logged_out(accts, {"+4444"}), ["+1111"])

    def test_no_signal_cli_files(self):
        self.assertEqual(local_accounts("/nonexistent"), [])


class BackendCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.cli = os.path.join(self.tmp.name, "cli")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def backend(self, rpc):
        b = RealBackend()
        b.rpc, b.cli_dir = rpc, self.cli
        return b

    def history(self, number, body="hi"):
        open_account_store(number).add_message(
            "+1777", Message(timestamp=1, body=body, outgoing=False))

    def test_new_account_deletes_all_previous_accounts(self):
        self.history("+1111")
        write_cli(self.cli, [("+1111", "a", True), ("+2222", "b", True), ("+3333", "c", False)])
        rpc = FakeRpc(loaded=["+1111", "+2222"])
        b = self.backend(rpc)
        b.start()                                   # +1111 signed in last
        self.assertEqual(b.account, "+1111")
        self.assertEqual(rpc.deleted(), ["+2222", "+3333"])
        b.adopt_account("+2222")                    # a new account logs in
        self.assertIn("+1111", rpc.deleted())
        self.assertEqual(os.listdir(os.path.join(self.cli, "attachments")), [])
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "linal", "accounts")),
                         [os.path.basename(os.path.dirname(b.store.path))])

    def test_logged_out_by_signal_deletes_everything(self):
        self.history("+1111")
        write_cli(self.cli, [("+1111", "a", False)])
        rpc = FakeRpc(loaded=[])
        self.backend(rpc).start()
        self.assertEqual(rpc.deleted(), ["+1111"])
        self.assertEqual(os.listdir(os.path.join(self.cli, "attachments")), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "linal", "accounts")))
        self.assertIsNone(Store(default_path()).get_meta("owner"))

    def test_offline_deletes_nothing(self):
        self.history("+1111")
        write_cli(self.cli, [("+1111", "a", True)])   # not loaded, but still registered
        rpc = FakeRpc(loaded=[])
        self.backend(rpc).start()
        self.assertEqual(rpc.deleted(), [])
        self.assertEqual(os.listdir(os.path.join(self.cli, "attachments")), ["photo.jpg"])
        self.assertTrue(os.path.exists(account_path("+1111")))

    def test_history_of_accounts_gone_from_signal_cli_is_deleted(self):
        self.history("+1111")
        rpc = FakeRpc(loaded=[])                    # signal-cli has no accounts at all
        self.backend(rpc).start()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "linal", "accounts")))

    def test_logout(self):
        self.history("+1111")
        write_cli(self.cli, [("+1111", "a", True)])
        rpc = FakeRpc(loaded=["+1111"])
        b = self.backend(rpc)
        b.start()
        b.logout()
        self.assertIn(("unregister", {"account": "+1111"}), rpc.calls)
        self.assertEqual(rpc.deleted(), ["+1111"])
        self.assertIsNone(b.account)
        self.assertEqual(os.listdir(os.path.join(self.cli, "attachments")), [])
        self.assertEqual(b.store.path, default_path())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "linal", "accounts")))

    def test_logout_offline_deletes_nothing(self):
        self.history("+1111")
        write_cli(self.cli, [("+1111", "a", True)])
        rpc = FakeRpc(loaded=["+1111"], fail={"unregister"})
        b = self.backend(rpc)
        b.start()
        with self.assertRaises(OSError):
            b.logout()
        self.assertEqual(rpc.deleted(), [])
        self.assertEqual(b.account, "+1111")
        self.assertTrue(os.path.exists(account_path("+1111")))


if __name__ == "__main__":
    unittest.main()
