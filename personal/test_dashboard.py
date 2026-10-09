"""Read-only broker dashboard regression: a live choice must not show paper cash."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from personal.autotrade import AutoTrader
from personal.server import Desk, make_server
from personal.test_toss_integration import AT, TOSS, FakeBroker


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = patch("personal.autotrade.Path.home", return_value=Path(self.tmp.name) / "fake-home")
        self.http = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("external HTTP forbidden"))
        self.home.start()
        self.http.start()
        self.desk = Desk(Path(self.tmp.name) / "desk.sqlite", "demo")
        self.auto = self.desk.autotrade
        self.auto.clock = lambda: AT
        self.created = []
        def factory(config):
            broker = FakeBroker(config, "toss")
            broker.reads = 0
            original = broker.account
            def read():
                broker.reads += 1
                return original()
            broker.account = read
            self.created.append(broker)
            return broker
        self.auto.toss_broker_factory = factory
        self.auto.connect(dict(TOSS))
        self.auto.update_config({"mode": "toss-live"})
        self.broker = self.auto.broker
        self.broker.reads = 0

    def tearDown(self):
        self.auto.shutdown()
        self.desk.jobs.shutdown()
        self.http.stop()
        self.home.stop()
        self.tmp.cleanup()

    def ledger(self):
        with self.desk.store.connection() as db:
            return {table: [dict(row) for row in db.execute("SELECT * FROM " + table)]
                    for table in ("auto_intents", "auto_positions", "orders", "positions", "meta")}

    def test_live_dashboard_uses_broker_cash_and_preserves_paper_account(self):
        state = self.desk.state()
        self.assertEqual(state["cash"], 10000000)
        dashboard = state["dashboard_account"]
        self.assertEqual(dashboard["status"], "ok")
        self.assertEqual(dashboard["kind"], "broker")
        self.assertEqual(dashboard["cash"], 1000000)
        self.assertEqual(dashboard["equity_basis"], "krw-trading-capital")
        self.assertIsNone(dashboard["initial_cash"])
        self.assertIsNone(dashboard["realized_pnl"])
        self.assertEqual(self.broker.reads, 1)
        self.assertFalse(self.broker.submissions)

    def test_open_orders_and_uncertain_intents_do_not_block_read_or_mutate_ledger(self):
        original = self.broker.account
        self.broker.account = lambda: {**original(), "open_orders": [{"broker_order_id": "unrelated-pending"}]}
        self.auto._reserve(self.auto.config(), original(), "005930", "buy", 1, 1200, "2026-10-07", AT)
        before = self.ledger()
        with patch.object(self.broker, "submit", side_effect=AssertionError("no order")), \
             patch.object(self.broker, "fills", side_effect=AssertionError("no settlement")), \
             patch.object(self.auto, "_cycle", side_effect=AssertionError("no signals")):
            dashboard = self.auto.dashboard_snapshot(refresh=True)
        self.assertEqual(dashboard["status"], "ok")
        self.assertEqual(dashboard["open_order_count"], 1)
        self.assertEqual(self.ledger(), before)
        self.assertFalse(self.auto.running.is_set())

    def test_failed_lookup_never_exposes_paper_cash_or_raw_error(self):
        with patch.object(self.broker, "account", side_effect=RuntimeError("API_SECRET RAW_ERROR")):
            dashboard = self.desk.state()["dashboard_account"]
        self.assertEqual(dashboard["status"], "unavailable")
        self.assertIsNone(dashboard["cash"])
        self.assertIsNone(dashboard["equity"])
        self.assertEqual(dashboard["positions"], [])
        self.assertNotIn("API_SECRET", json.dumps(dashboard))
        self.assertNotIn("10000000", json.dumps(dashboard))

    def test_cache_coalesces_reads_refreshes_and_never_reuses_after_connection_change(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            values = list(pool.map(lambda _: self.auto.dashboard_snapshot(), range(5)))
        self.assertTrue(all(value["status"] == "ok" for value in values))
        self.assertEqual(self.broker.reads, 1)
        self.auto.dashboard_snapshot(refresh=True)
        self.assertEqual(self.broker.reads, 2)
        self.auto.connect({**TOSS, "account_seq": 2})
        self.auto.dashboard_snapshot()
        self.assertEqual(self.auto.broker.reads, 2)  # connect plus new dashboard read
        self.auto.update_config({"mode": "paper"})
        self.assertIsNone(self.auto.dashboard_snapshot())
        self.auto.update_config({"mode": "toss-live"})
        self.auto.dashboard_snapshot()
        self.assertEqual(self.auto.broker.reads, 3)

    def test_failed_refresh_clears_previous_numbers_instead_of_showing_unlabelled_stale_values(self):
        self.assertEqual(self.auto.dashboard_snapshot()["status"], "ok")
        with patch.object(self.broker, "account", side_effect=RuntimeError("offline")):
            failed = self.auto.dashboard_snapshot(refresh=True)
        self.assertEqual(failed["status"], "unavailable")
        self.assertIsNone(failed["cash"])

    def test_empty_account_and_missing_average_are_not_fabricated(self):
        original = self.broker.account
        self.broker.account = lambda: {**original(), "cash": 0, "equity": 0}
        empty = self.auto.dashboard_snapshot(refresh=True)
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["cash"], 0)
        self.broker.quantity = 2
        self.broker.account = original
        snapshot = self.auto.dashboard_snapshot(refresh=True)
        self.assertEqual(snapshot["positions"][0]["market_value"], 2400)
        self.assertIsNone(snapshot["positions"][0]["average_cost"])
        self.assertIsNone(snapshot["positions"][0]["unrealized_pnl"])

    def test_wrong_identity_or_provider_cannot_display_another_account(self):
        original = self.broker.account
        self.broker.account = lambda: {**original(), "account_identity": "other-account"}
        self.assertEqual(self.auto.dashboard_snapshot(refresh=True)["status"], "unavailable")
        self.auto.update_config({"mode": "kis-live"})
        self.assertEqual(self.auto.dashboard_snapshot(refresh=True)["status"], "unavailable")
        self.auto.disconnect()
        self.assertEqual(self.auto.dashboard_snapshot(refresh=True)["status"], "unavailable")

    def test_paper_mode_never_reads_broker(self):
        self.auto.update_config({"mode": "paper"})
        with patch.object(self.broker, "account", side_effect=AssertionError("paper must not authenticate")):
            self.assertIsNone(self.desk.state()["dashboard_account"])


if __name__ == "__main__":
    unittest.main()
