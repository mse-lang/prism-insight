import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from personal.server import Desk, make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desk = Desk(Path(self.tmp.name) / "test.sqlite", "demo")
        self.server = make_server(self.desk, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.desk.jobs.shutdown()
        self.desk.autotrade.shutdown()
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(data).encode() if data is not None else None
        request_headers = {"Content-Type": "application/json", "X-CSRF-Token": self.desk.csrf}
        request_headers.update(headers or {})
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        content_type = response.getheader("Content-Type")
        connection.close()
        return status, json.loads(payload) if "json" in content_type else payload

    def test_state_and_frontend_static_boundary(self):
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["mode"], "paper")
        self.assertEqual(state["equity"], 10000000)
        self.assertFalse(state["positions"])
        self.assertFalse(state["upstream"]["enabled"])
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/static/app.css")[0], 200)
        self.assertEqual(self.request("/static/app.js")[0], 200)
        self.assertEqual(self.request("/../../mcp_agent.secrets.yaml")[0], 404)
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.example"})[0], 403)

    def test_order_report_journal_and_csv_end_to_end(self):
        data = {"symbol": "005930", "side": "buy", "quantity": 2,
                "idempotency_key": "test-order-001", "note": "=malicious_formula"}
        status, result = self.request("/api/orders", "POST", data)
        self.assertEqual(status, 200, result)
        status, duplicate = self.request("/api/orders", "POST", data)
        self.assertEqual(result["order"]["id"], duplicate["order"]["id"])
        state = self.request("/api/state")[1]
        self.assertEqual(state["positions"][0]["quantity"], 2)
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(self.request("/api/analyze", "POST", {"symbol": "005930"})[0], 200)
        self.assertEqual(self.request("/api/journal", "POST", {"symbol": "005930", "text": "관찰 기록"})[0], 200)
        self.assertEqual(len(self.request("/api/reports")[1]["reports"]), 1)
        csv = self.request("/api/export.csv")[1].decode("utf-8-sig")
        self.assertIn("'=malicious_formula", csv)
        self.assertEqual(self.request("/api/orders", "POST", {**data, "quantity": 3})[0], 400)

    def test_mutation_security_and_invalid_payload(self):
        payload = {"symbol": "005930", "action": "add"}
        self.assertEqual(self.request("/api/watchlist", "POST", payload,
                                      {"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request("/api/watchlist", "POST", payload,
                                      {"Origin": "https://external.example"})[0], 403)
        self.assertEqual(self.request("/api/watchlist", "POST", payload,
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/settings", "POST", {"fee_bps": float('nan')})[0], 400)
        self.assertEqual(self.request("/api/orders", "POST", {"symbol": "../../"})[0], 400)
        self.assertEqual(self.request("/api/prism-analysis", "POST", {"symbol": "005930"})[0], 400)

    def test_automation_http_boundaries_and_read_only_check(self):
        state = self.request("/api/autotrade")[1]
        self.assertFalse(state["running"])
        self.assertFalse(state["connection"]["configured"])
        self.assertEqual(self.request("/api/autotrade/config", "POST", {"interval_seconds": 1})[0], 400)
        self.assertEqual(self.request("/api/autotrade/config", "POST", {"mode": "kis-live", "symbols": ["005930"]})[0], 200)
        self.assertEqual(self.request("/api/autotrade/start", "POST", {})[0], 400)
        self.assertEqual(self.request("/api/autotrade/start", "POST", {"confirm_live": True})[0], 400)
        self.assertEqual(self.request("/api/autotrade/connect", "POST", {"app_key": "invalid"})[0], 400)
        self.assertEqual(self.request("/api/autotrade/start", "POST", {}, {"Origin": "https://external.example"})[0], 403)
        self.assertEqual(self.request("/api/autotrade/config", "POST", {"mode": "paper"})[0], 200)
        self.assertEqual(self.request("/api/autotrade/check", "POST", {})[0], 200)
        self.assertFalse(self.request("/api/state")[1]["orders"])


if __name__ == "__main__":
    unittest.main()
