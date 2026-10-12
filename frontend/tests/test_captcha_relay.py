"""The local hand-off page for a captcha solved on another device."""
import json, os, sys, threading, unittest, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linal.captcha_relay import CaptchaRelay, lan_address

LINK = "signalcaptcha://signal-hcaptcha.5fad97ac.registration.P1_abc.DEF-ghi_JKL"


def _req(url, data=None):
    r = urllib.request.Request(url, data=data.encode() if data is not None else None,
                               method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.event = threading.Event()
        self.relay = CaptchaRelay(lambda l: (self.got.append(l), self.event.set()),
                                  address="127.0.0.1", port=0)
        self.url = self.relay.start()

    def tearDown(self):
        self.relay.stop()

    def test_serves_page_only_on_secret_path(self):
        status, body = _req(self.url)
        self.assertEqual(status, 200)
        self.assertIn("signalcaptchas.org", body)
        base = self.url.rsplit("/", 1)[0]
        self.assertEqual(_req(base + "/")[0], 404)
        self.assertEqual(_req(base + "/wrong", LINK)[0], 404)
        self.assertEqual(self.got, [])

    def test_accepts_one_valid_link_then_closes(self):
        status, body = _req(self.url, "  " + LINK + "\n")
        self.assertEqual(status, 200, body)
        self.assertTrue(self.event.wait(5))
        self.assertEqual(self.got, [LINK])
        # a second submission (or a reload) is refused; the server goes away
        try:
            self.assertIn(_req(self.url, LINK)[0], (410,))
        except OSError:
            pass
        self.assertEqual(self.got, [LINK])

    def test_rejects_non_captcha_text(self):
        status, body = _req(self.url, "https://example.com/")
        self.assertEqual(status, 400)
        self.assertIn("signalcaptcha://", json.loads(body)["message"])
        self.assertEqual(self.got, [])

    def test_too_large_is_refused(self):
        self.assertEqual(_req(self.url, "signalcaptcha://" + "a" * 20000)[0], 413)
        self.assertEqual(self.got, [])


class LanAddressTest(unittest.TestCase):
    IP = json.dumps([
        {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "addr_info": [{"local": "127.0.0.1"}]},
        {"ifname": "usb0", "flags": ["UP"], "addr_info": [{"local": "10.42.0.109"}]},
        {"ifname": "wwan0", "flags": ["UP"], "addr_info": [{"local": "100.64.1.2"}]},
        {"ifname": "wlan0", "flags": ["BROADCAST", "UP"], "addr_info": [{"local": "10.0.0.78"}]},
    ])

    def test_picks_wifi_never_mobile_or_usb(self):
        self.assertEqual(lan_address(self.IP), "10.0.0.78")

    def test_none_without_wifi(self):
        no_wifi = json.dumps([json.loads(self.IP)[2]])
        self.assertIsNone(lan_address(no_wifi))

    def test_public_wifi_address_not_used(self):
        pub = json.dumps([{"ifname": "wlan0", "flags": ["UP"],
                           "addr_info": [{"local": "8.8.8.8"}]}])
        self.assertIsNone(lan_address(pub))


if __name__ == "__main__":
    unittest.main()
