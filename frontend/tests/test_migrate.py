import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal import migrate


class MigrateTest(unittest.TestCase):
    """Signal-L5's data and logs move to Linal's folders once."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"XDG_DATA_HOME": os.path.join(self.tmp.name, "share"),
                    "XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        for base in self.env.values():
            os.makedirs(os.path.join(base, "signal-l5"))
            open(os.path.join(base, "signal-l5", "f"), "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_moves_data_and_logs(self):
        self.assertEqual(len(migrate.move_data(self.env)), 2)
        for base in self.env.values():
            self.assertTrue(os.path.exists(os.path.join(base, "linal", "f")))
            self.assertFalse(os.path.exists(os.path.join(base, "signal-l5")))

    def test_only_once(self):
        migrate.move_data(self.env)
        self.assertEqual(migrate.move_data(self.env), [])

    def test_never_overwrites_linal_data(self):
        os.makedirs(os.path.join(self.env["XDG_DATA_HOME"], "linal"))
        migrate.move_data(self.env)
        self.assertTrue(os.path.exists(os.path.join(self.env["XDG_DATA_HOME"], "signal-l5", "f")))

    def test_stops_old_services(self):
        calls = []
        migrate.stop_old_services(lambda cmd, **kw: calls.append(cmd))
        self.assertEqual(calls, [["systemctl", "--user", "stop", "signal-l5-receiver.service",
                                  "signal-l5-daemon.service"]])


if __name__ == "__main__":
    unittest.main()
