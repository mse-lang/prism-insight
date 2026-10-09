import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from personal.autotrade import AutoTrader, KST, SecretFile, signal, tradable_quote, validate_config
from personal.engine import DeskStore

AT = datetime(2026, 10, 8, 10, 0, tzinfo=KST)


def bars(last=1200):
    days, day = [], AT.date() - timedelta(days=1)
    while len(days) < 21:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [{"date": day.isoformat(), "close": last if index == 20 else 1000}
            for index, day in enumerate(reversed(days))]


class Market:
    provider = "demo"
    def __init__(self):
        self.price, self.history = 1200, bars()
    def quote(self, code, **kwargs):
        return {"symbol": code, "name": code, "source": self.provider, "status": "ok",
                "price": self.price, "as_of": AT.isoformat(), "market_status": "DEMO",
                "history": copy.deepcopy(self.history)}
    def quotes(self, codes):
        return {code: self.quote(code) for code in codes}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market = Market()
        self.store = DeskStore(Path(self.tmp.name) / "test.sqlite", "demo")
        self.auto = AutoTrader(self.store, self.market, broker_factory=lambda _: None, clock=lambda: AT)
        self.auto.update_config({"symbols": ["005930"], "order_budget": 100000, "max_daily_buy": 100000, "max_daily_orders": 1})
    def tearDown(self):
        self.auto.shutdown()
        self.tmp.cleanup()
    def run_cycle(self):
        self.auto.run_lock.acquire()
        self.auto.running.set()
        self.auto._cycle(execute=True)
    def test_preview_never_orders_and_daily_breakout_order_only_once(self):
        state = self.auto.check()
        self.assertEqual(state["preview"][0]["action"], "buy")
        self.assertEqual(len(self.store.snapshot(self.market.quotes(self.store.required_symbols()))["orders"]), 0)
        self.run_cycle()
        self.run_cycle()
        snapshot = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
        self.assertEqual(len(snapshot["orders"]), 1)
        self.assertLessEqual(snapshot["orders"][0]["total"], 100000)
        self.assertEqual(self.auto._owned()["005930"]["quantity"], snapshot["positions"][0]["quantity"])
    def test_stop_loss_continues_after_buy_cap_and_chart_failure(self):
        self.run_cycle()
        self.market.price = 1000
        self.market.history = []
        self.run_cycle()
        snapshot = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
        self.assertEqual([order["side"] for order in snapshot["orders"]], ["sell", "buy"])
        self.assertFalse(snapshot["positions"])
        self.assertFalse(self.auto._owned())
    def test_manual_holdings_are_never_adopted(self):
        self.store.order({"symbol": "005930", "side": "buy", "quantity": 10, "idempotency_key": "manual-0001"}, self.market.quotes(["005930"]))
        self.market.price = 500
        self.market.history = bars(900)
        self.run_cycle()
        self.assertFalse(self.auto._owned())
        self.assertEqual(len(self.store.snapshot(self.market.quotes(self.store.required_symbols()))["orders"]), 1)
    def test_restart_stopped_and_persistent_signal_and_baseline(self):
        self.run_cycle()
        self.auto.stop()
        replacement = AutoTrader(self.store, self.market, broker_factory=lambda _: None, clock=lambda: AT)
        try:
            self.assertFalse(replacement.running.is_set())
            self.assertEqual(len(replacement.state()["orders"]), 1)
            with self.store.connection() as db:
                self.assertIsNotNone(self.store.meta(db, "auto_baseline:" + replacement._namespace() + ":2026-10-08"))
        finally:
            replacement.shutdown()
    def test_second_server_cannot_run_or_reconcile_same_account(self):
        self.auto.run_lock.acquire()
        replacement = AutoTrader(self.store, self.market, broker_factory=lambda _: None, clock=lambda: AT)
        try:
            with self.assertRaises(ValueError):
                replacement.start()
            with self.assertRaises(ValueError):
                replacement.check()
            with self.assertRaises(ValueError):
                replacement.update_config({"order_budget": 20000})
        finally:
            replacement.shutdown()
    def test_manual_reduction_blocks_automation(self):
        self.run_cycle()
        self.store.order({"symbol": "005930", "side": "sell", "quantity": 1, "idempotency_key": "manual-sell-001"}, self.market.quotes(["005930"]))
        self.run_cycle()
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(self.auto.state()["status"], "blocked")
    def test_server_worker_runs_without_browser_and_stop_blocks_later_cycles(self):
        self.auto.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.auto.state()["orders"]:
            time.sleep(0.02)
        self.assertEqual(len(self.auto.state()["orders"]), 1)
        self.auto.stop()
        self.assertFalse(self.auto.running.is_set())
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.auto.state()["orders"]), 1)
    def test_today_candle_cannot_create_breakout_or_invalid_history_signal(self):
        history = bars(1000)
        history.append({"date": "2026-10-08", "close": 99999})
        self.assertFalse(signal(history, AT)["breakout"])
        with self.assertRaises(ValueError):
            signal(history + [history[0]], AT)
        self.market.price = 900
        self.run_cycle()
        self.assertFalse(self.auto.state()["orders"])
    def test_quote_closed_stale_or_wrong_source_rejected(self):
        quote = {**self.market.quote("005930"), "source": "kis", "market_status": "OPEN"}
        tradable_quote(quote, "kis", AT)
        for changes in ({"market_status": "CLOSED"}, {"source": "naver"}, {"as_of": (AT - timedelta(seconds=91)).isoformat()}, {"price": True}):
            with self.assertRaises(ValueError):
                tradable_quote({**quote, **changes}, "kis", AT)
    def test_invalid_limits_and_secrets_roundtrip(self):
        for payload in ({"symbols": ["bad"]}, {"max_positions": True}, {"interval_seconds": 2}, {"stop_loss_pct": float("nan")}, {"mode": "live"}, {"order_budget": 100001, "max_daily_buy": 100000}):
            with self.assertRaises(ValueError):
                validate_config(payload)
        secrets = SecretFile(Path(self.tmp.name) / "secret.local")
        payload = {"app_key": "fake-test-key", "app_secret": "fake-test-secret"}
        secrets.save(payload)
        self.assertEqual(secrets.load(), payload)
        if __import__("os").name == "nt":
            self.assertNotIn(b"fake-test-secret", secrets.path.read_bytes())
        secrets.delete()


if __name__ == "__main__":
    unittest.main()
