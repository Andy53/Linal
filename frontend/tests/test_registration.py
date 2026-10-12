import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.registration import (is_captcha_url, captcha_token, parse_sms_code,
                                     normalize_number, device_number)


class RegistrationTest(unittest.TestCase):
    def test_is_captcha_url(self):
        self.assertTrue(is_captcha_url("signalcaptcha://signal-hcaptcha.abc.registration.XYZ"))
        self.assertFalse(is_captcha_url("https://signalcaptchas.org/..."))
        self.assertFalse(is_captcha_url(""))

    def test_captcha_token_passthrough(self):
        u = "signalcaptcha://signal-hcaptcha.abc.registration.TOKEN123"
        self.assertEqual(captcha_token(u), u)
        with self.assertRaises(ValueError):
            captcha_token("https://x")
        with self.assertRaises(ValueError):
            captcha_token("signalcaptcha://")

    def test_captcha_token_strips_gio_trailing_slash(self):
        """GIO hands the browser's link to the app with a trailing '/'; sent
        on to Signal that made every in-app captcha fail with HTTP 403."""
        tok = ("signalcaptcha://signal-hcaptcha.5fad97ac-7d06-4e44-b18a-b950b20148ff"
               ".registration.P1_eyJhbGciOi.haJwZACj-AbC_dEf.Y-BkJFitqPLFN9JOIBPRDoY")
        self.assertEqual(captcha_token(tok + "/"), tok)
        self.assertEqual(captcha_token("  " + tok + "\n"), tok)
        try:
            from gi.repository import Gio
        except Exception:
            return
        # the real round trip the app sees: browser -> GIO -> Gio.File -> do_open
        self.assertEqual(captcha_token(Gio.File.new_for_uri(tok).get_uri()), tok)

    def test_service_environment(self):
        from linal.registration import service_environment, _CAPTCHA_URLS
        self.assertEqual(service_environment({}), "live")
        self.assertEqual(service_environment({"LINAL_SERVICE_ENV": "Staging "}), "staging")
        self.assertEqual(service_environment({"LINAL_SERVICE_ENV": "prod"}), "live")
        self.assertIn("/staging/", _CAPTCHA_URLS["staging"])
        self.assertNotIn("staging", _CAPTCHA_URLS["live"])

    def test_friendly_errors(self):
        from linal.registration import friendly_error
        self.assertIn("another device", friendly_error("[403] Authorization failed!"))
        self.assertIn("fresh one", friendly_error("Invalid captcha given."))
        self.assertIn("Too many", friendly_error("StatusCode: 429 rate limit exceeded"))
        self.assertIn("code wasn't right", friendly_error("Invalid verification code"))
        self.assertEqual(friendly_error("something odd"), "something odd")

    def test_sms_since_ignores_older_codes(self):
        import sqlite3, tempfile, time
        from linal.sms import latest_sms_bodies
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "chatty-history.db")
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, time INTEGER, body TEXT)")
            now = int(time.time())
            c.execute("INSERT INTO messages(time, body) VALUES(?, ?)", (now - 120, "SIGNAL code: 111111."))
            c.execute("INSERT INTO messages(time, body) VALUES(?, ?)", (now, "SIGNAL code: 222222."))
            c.commit(); c.close()
            self.assertEqual(latest_sms_bodies(since=now - 30, db=db), ["SIGNAL code: 222222."])
            self.assertEqual(len(latest_sms_bodies(db=db)), 2)
            self.assertEqual(latest_sms_bodies(db=os.path.join(d, "missing.db")), [])

    def test_parse_sms_code(self):
        self.assertEqual(parse_sms_code("SIGNAL code: 282337. Do not share this code."), "282337")
        self.assertEqual(parse_sms_code("Your Signal code is 123-456"), "123456")
        self.assertIsNone(parse_sms_code("Welcome to Ultra Mobile! Your balance is 500"))
        self.assertIsNone(parse_sms_code(""))

    def test_normalize_number(self):
        self.assertEqual(normalize_number("303 555 0142"), "+13035550142")
        self.assertEqual(normalize_number("(303) 555-0142"), "+13035550142")
        self.assertEqual(normalize_number("+13035550142"), "+13035550142")
        self.assertEqual(normalize_number("13035550142"), "+13035550142")


    def test_device_number_graceful(self):
        # no mmcli (or no SIM) -> returns "" without raising
        self.assertIsInstance(device_number(), str)

if __name__ == "__main__":
    unittest.main()
