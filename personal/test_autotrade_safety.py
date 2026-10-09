"""Offline broker-boundary regressions. These tests never contact KIS."""
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path

from personal.autotrade import AutoTrader, KST
from personal.engine import DeskStore


AT = datetime(2026, 10, 8, 10, 0, tzinfo=KST)
CONFIG = {"environment": "live", "account_no": "12345678", "product_code": "01",
          "app_key": "offline-fake-key", "app_secret": "offline-fake-secret"}


def live_confirmation(auto):
    return {"confirm_live": True, "review_token": auto.state()["review_token"]}


def history():
    days, day = [], AT.date() - timedelta(days=1)
    while len(days) < 21:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [{"date": day.isoformat(), "close": 1200 if i == 20 else 1000}
            for i, day in enumerate(reversed(days))]


class OfflineMarket:
    provider = "demo"
    def quote(self, code, **kwargs):
        return {"symbol": code, "source": "demo", "status": "ok", "price": 1200,
                "as_of": AT.isoformat(), "market_status": "DEMO", "history": history()}
    def quotes(self, codes):
        return {code: self.quote(code) for code in codes}


class OfflineBroker:
    account_identity = "offline-account-identity"
    environment = "live"
    def __init__(self, config):
        self.config = dict(config)
        self.quantity = 0
        self.submissions = []
        self.account_error = False
        self.submit_error = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.block_submission = False
        self.fill = {"status": "pending", "filled_quantity": 0, "average_price": None}
    def account(self):
        if self.account_error:
            raise ValueError("offline account outage")
        return {"cash": 1000000, "equity": 1000000, "as_of": AT.isoformat(),
                "positions": [{"symbol": "005930", "quantity": self.quantity, "price": 1200}] if self.quantity else [],
                "open_orders": []}
    def quote(self, code):
        return {"symbol": code, "source": "kis", "status": "ok", "price": 1200,
                "as_of": AT.isoformat(), "market_status": "OPEN"}
    def history(self, code):
        return history()
    def buying_power(self, code, limit_price):
        return {"cash": 1000000, "quantity": 1000}
    def submit(self, code, side, quantity, limit_price):
        self.submissions.append((code, side, quantity, limit_price))
        self.entered.set()
        if self.block_submission and not self.release.wait(3):
            raise RuntimeError("offline synchronization timeout")
        if self.submit_error:
            raise RuntimeError("offline lost response")
        return {"broker_order_id": "12345", "organization_id": "67890", "order_date": "20261008",
                "account_identity": self.account_identity, "environment": self.environment}
    def fills(self, receipt):
        if receipt.get("account_identity") != self.account_identity or receipt.get("environment") != self.environment:
            raise ValueError("offline receipt belongs to another account")
        return copy.deepcopy(self.fill)


class IdleThread:
    def is_alive(self):
        return True
    def join(self, timeout=None):
        pass


class AutoTradeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home_patch = patch("personal.autotrade.Path.home", return_value=Path(self.tmp.name) / "fake-home")
        self.home_patch.start()
        self.market = OfflineMarket()
        self.store = DeskStore(Path(self.tmp.name) / "desk.sqlite", "demo")
        self.broker = OfflineBroker(CONFIG)
        self.auto = AutoTrader(self.store, self.market, broker_factory=lambda _: self.broker, clock=lambda: AT)
        self.auto.connection_config = dict(CONFIG)
        self.auto.broker = self.broker
        self.auto.update_config({"mode": "kis-live", "symbols": ["005930"], "order_budget": 100000,
                                 "max_daily_buy": 1000000, "max_daily_orders": 10})
        self.auto.thread = IdleThread()
    def tearDown(self):
        self.broker.release.set()
        self.auto.shutdown()
        self.home_patch.stop()
        self.tmp.cleanup()
    def intent(self, quantity=10, status="pending"):
        record = self.auto._reserve(self.auto.config(), self.broker.account(), "005930", "buy", quantity, 1200, "2026-10-07", AT)
        with self.store.connection() as db:
            metadata = json.loads(db.execute("SELECT broker FROM auto_intents WHERE id=?", (record["id"],)).fetchone()[0])
        metadata.update({"broker_order_id": "12345", "organization_id": "67890", "order_date": "20261008",
                         "account_identity": self.broker.account_identity, "environment": "live",
                         "symbol": "005930", "side": "buy", "quantity": quantity})
        self.auto._status(record["id"], status, metadata)
        return record
    def row(self, record):
        with self.store.connection() as db:
            return dict(db.execute("SELECT * FROM auto_intents WHERE id=?", (record["id"],)).fetchone())
    def test_terminal_fill_requires_account_query_before_ownership(self):
        record = self.intent()
        self.broker.account_error = True
        with self.assertRaises(ValueError):
            self.auto._settle(record, {"status": "filled", "filled_quantity": 10, "average_price": 1200})
        self.assertFalse(self.auto._owned())
        self.assertEqual(self.row(record)["status"], "pending")
    def test_terminal_fill_requires_exact_account_quantity(self):
        record = self.intent()
        self.broker.quantity = 11
        with self.assertRaises(ValueError):
            self.auto._settle(record, {"status": "filled", "filled_quantity": 10, "average_price": 1200})
        self.assertFalse(self.auto._owned())
        self.assertEqual(self.row(record)["status"], "pending")
    def test_canceled_partial_fill_adopts_only_verified_fills_once(self):
        record = self.intent()
        self.auto._settle(record, {"status": "partial", "filled_quantity": 4, "average_price": 1199})
        self.assertFalse(self.auto._owned())
        self.broker.quantity = 4
        terminal = {"status": "canceled", "filled_quantity": 4, "average_price": 1199}
        self.auto._settle(record, terminal)
        self.auto._settle(record, terminal)
        self.assertEqual(self.auto._owned()["005930"]["quantity"], 4)
        self.assertEqual(self.auto._owned()["005930"]["cost_basis"], 4796)
    def test_decreasing_cumulative_fill_and_wrong_limit_price_block(self):
        record = self.intent()
        self.auto._settle(record, {"status": "partial", "filled_quantity": 4, "average_price": 1200})
        for fill in ({"status": "partial", "filled_quantity": 3, "average_price": 1200},
                     {"status": "filled", "filled_quantity": 10, "average_price": 1201}):
            with self.assertRaises(ValueError):
                self.auto._settle(record, fill)
        self.assertFalse(self.auto._owned())
        self.assertEqual(self.row(record)["filled_quantity"], 4)
    def test_accepted_order_waits_for_fills_and_never_adopts_receipt(self):
        self.auto.running.set()
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.auto._owned())
        self.assertEqual(self.auto.state()["orders"][0]["status"], "pending")
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.auto._owned())
    def test_unknown_response_is_persistent_and_never_retried(self):
        self.broker.submit_error = True
        self.auto.running.set()
        self.auto._cycle(execute=True)
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(self.auto.state()["orders"][0]["status"], "uncertain")
        with self.assertRaises(ValueError):
            self.auto.start(live_confirmation(self.auto))
        self.auto._cycle(execute=False)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.auto._owned())
    def test_unknown_resolution_uses_verified_account_identity(self):
        record = self.auto._reserve(self.auto.config(), self.broker.account(), "005930", "buy", 10, 1200, "2026-10-07", AT)
        self.auto._status(record["id"], "uncertain")
        self.broker.quantity = 10
        self.broker.fill = {"status": "filled", "filled_quantity": 10, "average_price": 1200}
        self.auto.resolve({"intent_id": record["id"], "broker_order_id": "12345",
                           "organization_id": "67890", "order_date": "20261008"})
        self.assertEqual(self.auto._owned()["005930"]["quantity"], 10)
        self.assertEqual(self.row(record)["status"], "filled")
    def test_restart_preserves_unknown_order_and_stays_stopped(self):
        record = self.intent(status="uncertain")
        replacement = AutoTrader(self.store, self.market, broker_factory=lambda _: self.broker, clock=lambda: AT)
        try:
            self.assertFalse(replacement.running.is_set())
            self.assertEqual(replacement.state()["orders"][0]["status"], "uncertain")
            with self.assertRaises(ValueError):
                replacement.start(live_confirmation(replacement))
            self.assertFalse(self.broker.submissions)
            self.assertEqual(self.row(record)["status"], "uncertain")
        finally:
            replacement.shutdown()
    def test_same_broker_account_cannot_run_from_different_data_directories(self):
        other_store = DeskStore(Path(self.tmp.name) / "other-data" / "desk.sqlite", "demo")
        replacement = AutoTrader(other_store, self.market, broker_factory=lambda _: self.broker, clock=lambda: AT)
        replacement.connection_config = dict(CONFIG)
        replacement.broker = self.broker
        replacement.thread = IdleThread()
        with patch("personal.autotrade.Path.home", return_value=Path(self.tmp.name) / "fake-home"):
            try:
                replacement.update_config(self.auto.config())
                self.auto.start(live_confirmation(self.auto))
                self.assertNotEqual(self.auto.run_lock.path, replacement.run_lock.path)
                with self.assertRaises(ValueError):
                    replacement.start(live_confirmation(replacement))
                self.assertFalse(replacement.running.is_set())
                self.assertFalse(self.broker.submissions)
            finally:
                self.auto.stop()
                replacement.shutdown()
    def test_live_start_needs_exact_explicit_confirmation(self):
        for value in (None, False, 1, "true"):
            with self.assertRaises(ValueError):
                self.auto.start({**live_confirmation(self.auto), "confirm_live": value})
        self.assertFalse(self.broker.submissions)
        with self.assertRaises(ValueError):
            self.auto.start({"confirm_live": True})
        self.auto.start(live_confirmation(self.auto))
        self.assertTrue(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
    def test_live_review_must_match_current_limits_and_position_percentage(self):
        reviewed = live_confirmation(self.auto)
        self.auto.update_config({"order_budget": 110000})
        with self.assertRaises(ValueError):
            self.auto.start(reviewed)
        reviewed = live_confirmation(self.auto)
        previous = self.store.settings()["max_position_pct"]
        self.store.update_settings({"max_position_pct": 99 if previous == 100 else previous + 1})
        with self.assertRaises(ValueError):
            self.auto.start(reviewed)
        self.assertFalse(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
    def test_stop_wins_when_start_completes_before_waiting_stop_lock(self):
        stopping, returned = threading.Event(), threading.Event()
        original_clear = self.auto.running.clear
        def mark_clear():
            original_clear()
            stopping.set()
        self.auto.running.clear = mark_clear
        def stop():
            self.auto.stop()
            returned.set()
        with self.auto.lock:
            worker = threading.Thread(target=stop)
            worker.start()
            self.assertTrue(stopping.wait(2))
            self.auto.start(live_confirmation(self.auto))
            self.assertTrue(self.auto.running.is_set())
        worker.join(3)
        self.assertTrue(returned.is_set())
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(self.auto.state()["status"], "stopped")
    def test_stop_waits_for_inflight_submission_and_preserves_pending(self):
        self.broker.block_submission = True
        self.auto.running.set()
        def cycle():
            with self.auto.lock:
                self.auto._cycle(execute=True)
        worker = threading.Thread(target=cycle)
        worker.start()
        self.assertTrue(self.broker.entered.wait(2))
        returned = threading.Event()
        stopper = threading.Thread(target=lambda: (self.auto.stop(), returned.set()))
        stopper.start()
        self.assertFalse(returned.wait(0.05))
        self.broker.release.set()
        worker.join(3)
        stopper.join(3)
        self.assertTrue(returned.is_set())
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(self.auto.state()["orders"][0]["status"], "pending")
        self.assertEqual(len(self.broker.submissions), 1)
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.broker.submissions), 1)


if __name__ == "__main__":
    unittest.main()
