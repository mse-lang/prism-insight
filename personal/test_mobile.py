import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from personal.mobile import MobileAccess
from personal.server import Desk, make_server

ORIGIN = "https://desk.example.ts.net"


class MobileAccessTests(unittest.TestCase):
    def setUp(self):
        self.stamp = 100.0
        self.access = MobileAccess(ORIGIN, clock=lambda: self.stamp)
    def login(self):
        return self.access.login({"code": self.access.pair()["code"]})
    def test_origin_requires_https_without_credentials_or_path(self):
        for origin in ("http://desk.example.ts.net", "https://user:pass@desk.example.ts.net", "https://desk.example.ts.net/path", "https://desk.example.ts.net/?token=secret"):
            with self.assertRaises(ValueError):
                MobileAccess(origin)
        with self.assertRaises(ValueError):
            MobileAccess().pair()
    def test_pair_is_hashed_expires_after_five_minutes_and_is_single_use(self):
        pair = self.access.pair()
        self.assertEqual(len(pair["code"]), 8)
        self.assertNotEqual(self.access.code[0], pair["code"])
        self.assertEqual(pair["expires_in"], 300)
        token = self.access.login({"code": pair["code"]})
        self.assertTrue(self.access.authenticated("prism_device=" + token))
        with self.assertRaises(ValueError):
            self.access.login({"code": pair["code"]})
        pair = self.access.pair()
        self.stamp += 300
        with self.assertRaises(ValueError):
            self.access.login({"code": pair["code"]})
    def test_eight_invalid_attempts_invalidate_code(self):
        pair = self.access.pair()
        wrong = "00000000" if pair["code"] != "00000000" else "11111111"
        for _ in range(8):
            with self.assertRaises(ValueError):
                self.access.login({"code": wrong})
        with self.assertRaises(ValueError):
            self.access.login({"code": pair["code"]})
        self.assertEqual(self.access.state()["paired_devices"], 0)
    def test_cookie_session_expiry_logout_and_revoke(self):
        token = self.login()
        cookie = self.access.cookie(token)
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age=43200"):
            self.assertIn(flag, cookie)
        self.assertNotIn(token, self.access.sessions)
        self.assertTrue(self.access.authenticated(cookie))
        self.stamp += 12 * 3600
        self.assertFalse(self.access.authenticated(cookie))
        first, second = self.login(), self.login()
        self.access.logout("prism_device=" + first)
        self.assertFalse(self.access.authenticated("prism_device=" + first))
        self.assertTrue(self.access.authenticated("prism_device=" + second))
        self.access.revoke()
        self.assertFalse(self.access.authenticated("prism_device=" + second))
        self.assertIn("Max-Age=0", self.access.cookie("", clear=True))


class MobileHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desk = Desk(Path(self.tmp.name) / "desk.sqlite", "demo")
        self.desk.mobile = MobileAccess(ORIGIN)
        self.server = make_server(self.desk, 0, mobile=True)
        self.desktop = make_server(self.desk, 0)
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.server, self.desktop)]
        for thread in self.threads:
            thread.start()
        self.cookie = None
    def tearDown(self):
        for server in (self.server, self.desktop):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)
        self.desk.advisor.shutdown()
        self.desk.jobs.shutdown()
        self.desk.autotrade.shutdown()
        self.tmp.cleanup()
    def request(self, path, method="GET", data=None, *, desktop=False, authenticated=True, headers=None):
        server = self.desktop if desktop else self.server
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", "X-CSRF-Token": self.desk.csrf}
        if not desktop:
            request_headers.update(Host="desk.example.ts.net", Origin=ORIGIN)
        if authenticated and self.cookie:
            request_headers["Cookie"] = self.cookie
        request_headers.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        status, raw = response.status, response.read()
        values = dict(response.getheaders())
        connection.close()
        payload = json.loads(raw) if "json" in values.get("Content-Type", "") else raw
        return status, payload, values
    def pair_and_login(self):
        status, pair, _ = self.request("/api/mobile/pair", "POST", {}, desktop=True)
        self.assertEqual(status, 200)
        status, payload, headers = self.request("/api/mobile/login", "POST", {"code": pair["code"]}, authenticated=False)
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        return pair
    def test_mobile_listener_is_loopback_and_requires_pairing_for_every_api(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path in ("/api/state", "/api/health", "/api/autotrade", "/api/advisor", "/api/reports", "/api/export.csv"):
            self.assertEqual(self.request(path)[0], 401, path)
        self.assertEqual(self.request("/api/advisor/config", "POST", {"enabled": True})[0], 401)
        self.assertFalse(self.desk.advisor.enabled)
    def test_login_requires_exact_origin_host_and_single_use_code(self):
        pair = self.desk.mobile.pair()
        for headers in ({"Origin": "https://evil.example"}, {"Origin": ""}, {"Host": "evil.example"}):
            self.assertEqual(self.request("/api/mobile/login", "POST", {"code": pair["code"]}, headers=headers)[0], 403)
        _, _, headers = self.request("/api/mobile/login", "POST", {"code": pair["code"]})
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/mobile/login", "POST", {"code": pair["code"]})[0], 400)
    def test_authenticated_mobile_has_csrf_origin_host_and_desktop_boundaries(self):
        self.pair_and_login()
        self.assertEqual(self.request("/api/state")[0], 200)
        for path in ("/api/mobile/pair", "/api/mobile/revoke", "/api/autotrade/connect", "/api/autotrade/toss/accounts", "/api/autotrade/disconnect"):
            self.assertEqual(self.request(path, "POST", {})[0], 403, path)
        for headers in ({"X-CSRF-Token": ""}, {"Origin": "https://evil.example"}, {"Origin": ""}, {"Host": "evil.example"}):
            self.assertEqual(self.request("/api/advisor/config", "POST", {"enabled": False}, headers=headers)[0], 403)
        self.assertEqual(self.request("/api/state", desktop=True, headers={"Host": "desk.example.ts.net"})[0], 403)
        self.assertFalse(self.desk.autotrade.running.is_set())
    def test_desktop_revoke_and_mobile_logout_reject_old_sessions(self):
        self.pair_and_login()
        status, _, headers = self.request("/api/mobile/logout", "POST", {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/state")[0], 401)
        self.pair_and_login()
        self.assertEqual(self.request("/api/mobile/revoke", "POST", {}, desktop=True)[0], 200)
        self.assertEqual(self.request("/api/state")[0], 401)


if __name__ == "__main__":
    unittest.main()
