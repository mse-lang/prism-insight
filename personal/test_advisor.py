import copy
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from personal.advisor import Advisor
from personal.autotrade import AutoTrader
from personal.engine import DeskStore
from personal.test_autotrade import AT, Market, bars


class PublicMarket(Market):
    provider = "naver"
    def quote(self, code, **kwargs):
        return {**super().quote(code, **kwargs), "market_status": "OPEN"}


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market = PublicMarket()
        self.store = DeskStore(Path(self.tmp.name) / "desk.sqlite", "naver")
        self.auto = AutoTrader(self.store, self.market, broker_factory=lambda _: None, toss_broker_factory=lambda _: None, clock=lambda: AT)
        self.auto.update_config({"symbols": ["005930"], "order_budget": 100000, "max_daily_buy": 100000, "max_daily_orders": 1})
        self.advisor = Advisor(self.auto, clock=lambda: AT, interval=.05)
    def tearDown(self):
        self.advisor.shutdown()
        self.auto.shutdown()
        self.tmp.cleanup()
    def ledger(self):
        with self.store.connection() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                    for table in ("meta", "positions", "orders", "auto_positions", "auto_intents", "auto_events")}
    def wait_ready(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.advisor.state()
            if state["status"] in ("ready", "error"):
                return state
            time.sleep(.01)
        self.fail("Proposal calculation did not complete")
    def test_calculation_explains_all_strategies_without_trading_or_baseline_writes(self):
        before = self.ledger()
        with patch.object(self.auto, "start", side_effect=AssertionError("No start")), patch.object(self.auto, "_reconcile", side_effect=AssertionError("No fills")), patch.object(self.store, "order", side_effect=AssertionError("No order")):
            result = self.advisor.calculate()
        self.assertEqual(self.ledger(), before)
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(result["source"], "naver")
        item = result["items"][0]
        self.assertEqual(len(item["strategies"]), 4)
        self.assertEqual(len(item["stages"]), 4)
        self.assertEqual(item["signal_date"], "2026-10-07")
        self.assertTrue(any("기준선" in reason for reason in item["risk"]["gate_reasons"]))
        self.assertEqual(item["stages"][-1]["status"], "blocked")
    def test_today_forming_candle_cannot_create_entry_and_missing_history_stays_missing(self):
        self.market.history = bars(1000) + [{"date": AT.date().isoformat(), "close": 99999}]
        result = self.advisor.calculate()
        self.assertNotEqual(result["items"][0]["action"], "buy")
        self.assertFalse(result["items"][0]["strategies"][0]["current_entry"])
        self.market.history = []
        result = self.advisor.calculate()
        self.assertEqual(result["items"][0]["action"], "blocked")
        self.assertEqual(result["items"][0]["risk"]["quantity"], 0)
        self.assertTrue(all(row.get("error") for row in result["items"][0]["strategies"]))
    def test_valid_retained_buy_has_existing_baseline_and_explains_quantity(self):
        with self.store.connection(write=True) as db:
            self.store.set_meta(db, "auto_baseline:" + self.auto._namespace() + ":" + AT.date().isoformat(), 10000000)
        before = self.ledger()
        item = self.advisor.calculate()["items"][0]
        self.assertEqual(item["action"], "buy")
        self.assertEqual(item["risk"]["quantity"], 82)
        self.assertLessEqual(item["risk"]["budget"], 100000)
        self.assertFalse(item["risk"]["gate_reasons"])
        self.assertEqual(item["stages"][-1]["status"], "ok")
        self.assertEqual(self.ledger(), before)
        self.assertFalse(self.auto.running.is_set())
    def test_stale_wrong_source_and_unavailable_quotes_do_not_propose_buy(self):
        valid = self.market.quote("005930", history=True)
        for changes in ({"as_of": (AT - timedelta(seconds=91)).isoformat()}, {"source": "demo"}, {"status": "unavailable"}):
            with patch.object(self.market, "quote", return_value={**copy.deepcopy(valid), **changes}):
                item = self.advisor.calculate()["items"][0]
            self.assertEqual(item["action"], "blocked", changes)
            self.assertEqual(item["risk"].get("quantity", 0), 0, changes)
            self.assertFalse(self.auto.running.is_set())
    def test_pending_and_daily_limits_appear_in_final_risk_stage(self):
        account = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
        intent = self.auto._reserve(self.auto.config(), account, "005930", "buy", 82, 1200, "2026-10-07", AT)
        self.auto._status(intent["id"], "uncertain")
        before = self.ledger()
        item = self.advisor.calculate()["items"][0]
        reasons = " ".join(item["risk"]["gate_reasons"])
        self.assertIn("불확실", reasons)
        self.assertIn("주문 수", reasons)
        self.assertEqual(item["stages"][-1]["status"], "blocked")
        self.assertEqual(self.ledger(), before)
    def test_automatic_owned_stop_exit_is_not_blocked_by_buy_caps(self):
        self.store.order({"symbol": "005930", "side": "buy", "quantity": 10, "idempotency_key": "advisor-owned-fixture"}, self.market.quotes(["005930"]))
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO auto_positions VALUES (?,?,?,?)", (self.auto._namespace(), "005930", 10, 12000))
        account = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
        intent = self.auto._reserve(self.auto.config(), account, "005930", "buy", 82, 1200, "2026-10-07", AT)
        self.auto._status(intent["id"], "filled")
        self.market.price, self.market.history = 900, []
        before = self.ledger()
        item = self.advisor.calculate()["items"][0]
        self.assertEqual(item["action"], "sell")
        self.assertEqual(item["risk"]["quantity"], 10)
        reasons = " ".join(item["risk"]["gate_reasons"])
        self.assertNotIn("일일 매수 주문 수", reasons)
        self.assertNotIn("1주를 살 수", reasons)
        self.assertEqual(self.ledger(), before)
    def test_manual_holdings_are_not_adopted_or_sold_by_advisor(self):
        self.store.order({"symbol": "005930", "side": "buy", "quantity": 10, "idempotency_key": "advisor-manual-fixture"}, self.market.quotes(["005930"]))
        self.market.price = 900
        before = self.ledger()
        item = self.advisor.calculate()["items"][0]
        self.assertEqual(item["action"], "hold")
        self.assertEqual(item["risk"]["quantity"], 0)
        self.assertFalse(self.auto._owned())
        self.assertEqual(self.ledger(), before)
    def test_total_broker_position_cap_blocks_new_entry_without_any_order(self):
        self.auto.update_config({"mode": "toss-live"})
        account = {"status": "ok", "cash": 1000000, "equity": 1000000, "positions": [],
                   "total_position_count": self.auto.config()["max_positions"], "equity_basis": "krw-trading-capital"}
        with self.store.connection(write=True) as db:
            self.store.set_meta(db, "auto_baseline:" + self.auto._namespace() + ":" + AT.date().isoformat(), 1000000)
        before = self.ledger()
        with patch.object(self.auto, "dashboard_snapshot", return_value=account):
            item = self.advisor.calculate()["items"][0]
        self.assertEqual(item["action"], "blocked")
        self.assertEqual(item["risk"]["quantity"], 0)
        self.assertTrue(any("종목 수" in reason for reason in item["risk"]["gate_reasons"]))
        self.assertEqual(self.ledger(), before)
    def test_unavailable_broker_account_never_uses_paper_equity_for_proposals(self):
        self.auto.update_config({"mode": "toss-live"})
        before = self.ledger()
        with patch.object(self.auto, "dashboard_snapshot", return_value={"status": "error", "positions": []}), patch.object(self.store, "snapshot", side_effect=AssertionError("No paper fallback")):
            item = self.advisor.calculate()["items"][0]
        self.assertEqual(item["action"], "blocked")
        self.assertEqual(item["risk"]["quantity"], 0)
        self.assertEqual(self.ledger(), before)
    def test_autonomous_suggestions_persist_and_restart_stopped(self):
        with patch.object(self.store, "order", side_effect=AssertionError("No order")), patch.object(self.auto, "start", side_effect=AssertionError("No automation")):
            self.advisor.configure({"enabled": True})
            state = self.wait_ready()
            self.assertEqual(state["status"], "ready")
            self.advisor.configure({"enabled": False})
            self.advisor.shutdown()
        restored = Advisor(self.auto, clock=lambda: AT)
        try:
            state = restored.state()
            self.assertFalse(state["enabled"])
            self.assertFalse(state["running"])
            self.assertIsNone(state["next_run"])
            self.assertEqual(state["status"], "stale")
            self.assertTrue(state["items"])
            self.assertFalse(self.auto.running.is_set())
        finally:
            restored.shutdown()
    def test_invalid_configuration_and_calculation_inputs_cannot_enable_trading(self):
        for payload in ({"enabled": "true"}, {"enabled": 1}, {"enabled": True, "trade": True}):
            with self.assertRaises(ValueError):
                self.advisor.configure(payload)
        with self.assertRaises(ValueError):
            self.advisor.request({"order": True})
        self.assertFalse(self.auto.running.is_set())


if __name__ == "__main__":
    unittest.main()
