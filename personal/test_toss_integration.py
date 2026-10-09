"""Toss/controller boundary checks with fake credentials and no HTTP access."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from personal.autotrade import AutoTrader, KST
from personal.engine import DeskStore

AT = datetime(2026, 10, 8, 10, 0, tzinfo=KST)
TOSS = {"provider": "toss", "environment": "live", "client_id": "offline-toss-client",
        "client_secret": "offline-toss-secret", "account_seq": 1}
KIS = {"provider": "kis", "environment": "live", "account_no": "12345678", "product_code": "01",
       "app_key": "offline-kis-key", "app_secret": "offline-kis-secret"}


def bars():
    days, day = [], AT.date() - timedelta(days=1)
    while len(days) < 21:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [{"date": day.isoformat(), "close": 1200 if i == 20 else 1000}
            for i, day in enumerate(reversed(days))]


class NoFallbackMarket:
    provider = "naver"
    def __init__(self):
        self.calls = 0
    def quote(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("A broker account must never use the public quote fallback")
    def quotes(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("A broker account must never use public portfolio quotes")


class IdleThread:
    def is_alive(self):
        return True
    def join(self, timeout=None):
        pass


class FakeBroker:
    def __init__(self, config, provider):
        self.config, self.provider = dict(config), provider
        self.environment = config["environment"]
        identity = str(config.get("account_seq")) if provider == "toss" else config["account_no"] + ":" + config["product_code"]
        self.account_identity = provider + ":" + self.environment + ":" + identity
        self.account_masked = "가짜 계좌 **01"
        self.quantity = 0
        self.extra_position_count = 0
        self.submissions = []
        self.quote_error = False
        self.quote_source = provider
        self.quote_time = AT
        self.submit_error = False
        self.before_submit = None
        self.fill = {"status": "pending", "filled_quantity": 0, "average_price": None}
    def list_accounts(self):
        return [{"account_seq": 1, "account_masked": "가짜 계좌 **01", "account_type": "BROKERAGE"}]
    def account(self):
        return {"cash": 1000000, "equity": 1000000, "positions":
                [{"symbol": "005930", "quantity": self.quantity, "price": 1200}] if self.quantity else [],
                "total_position_count": int(self.quantity > 0) + self.extra_position_count,
                "excluded_positions_count": self.extra_position_count,
                "equity_basis": "krw-trading-capital" if self.provider == "toss" else "account-equity",
                "open_orders": [], "as_of": AT.isoformat(), "account_identity": self.account_identity,
                "environment": self.environment}
    def quote(self, code):
        if self.quote_error:
            raise ValueError("offline broker price unavailable")
        return {"symbol": code, "source": self.quote_source, "status": "ok", "price": 1200,
                "as_of": self.quote_time.isoformat(), "market_status": "OPEN"}
    def history(self, code):
        return bars()
    def buying_power(self, code, limit_price):
        return {"quantity": 1000, "cash": 1000000}
    def submit(self, code, side, quantity, limit_price=None, *, client_order_id=None):
        if self.before_submit:
            self.before_submit(client_order_id)
        self.submissions.append({"symbol": code, "side": side, "quantity": quantity,
                                 "limit_price": limit_price, "client_order_id": client_order_id})
        if self.submit_error:
            raise RuntimeError("offline HTTP order response lost")
        return {"broker_order_id": "opaque_toss_order_identity_12345678901234567890", "order_date": "20261008",
                "organization_id": "fake", "account_identity": self.account_identity, "environment": self.environment,
                "client_order_id": client_order_id, "limit_price": limit_price, "symbol": code,
                "side": side, "quantity": quantity}
    def fills(self, receipt):
        if receipt.get("account_identity") != self.account_identity or receipt.get("environment") != self.environment:
            raise ValueError("offline wrong-account order receipt")
        return copy.deepcopy(self.fill)


class TossIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home_patch = patch("personal.autotrade.Path.home", return_value=Path(self.tmp.name) / "fake-home")
        self.home_patch.start()
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden in integration tests"))
        self.http_patch.start()
        self.created = []
        self.market = NoFallbackMarket()
        self.store = DeskStore(Path(self.tmp.name) / "desk.sqlite", "demo")
        self.auto = self.controller(self.store)
        self.auto.connect(dict(TOSS))
        self.auto.update_config({"mode": "toss-live", "symbols": ["005930"], "order_budget": 100000,
                                 "max_daily_buy": 1000000, "max_daily_orders": 10})
        self.auto.thread = IdleThread()
        self.broker = self.auto.broker
    def tearDown(self):
        self.auto.shutdown()
        self.http_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()
    def factory(self, config, provider):
        broker = FakeBroker(config, provider)
        self.created.append(broker)
        return broker
    def controller(self, store):
        return AutoTrader(store, self.market, broker_factory=lambda config: self.factory(config, "kis"),
                          toss_broker_factory=lambda config: self.factory(config, "toss"), clock=lambda: AT)
    def confirmation(self, controller=None):
        controller = controller or self.auto
        return {"confirm_live": True, "confirm_external_orders": True, "review_token": controller.state()["review_token"]}
    def intent(self, *, status="pending"):
        intent = self.auto._reserve(self.auto.config(), self.broker.account(), "005930", "buy", 10, 1200, "2026-10-07", AT)
        self.auto._status(intent["id"], status)
        return intent
    def insert_owned(self):
        self.broker.quantity = 10
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO auto_positions VALUES (?,?,?,?)", (self.auto._namespace(), "005930", 10, 12000))
    def test_toss_live_requires_fresh_explicit_limit_review(self):
        for payload in ({}, {"confirm_live": True}, {**self.confirmation(), "confirm_live": 1}):
            with self.assertRaises(ValueError):
                self.auto.start(payload)
        reviewed = self.confirmation()
        self.auto.update_config({"order_budget": 110000})
        with self.assertRaises(ValueError):
            self.auto.start(reviewed)
        self.auto.start(self.confirmation())
        self.assertTrue(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
    def test_toss_external_app_order_limit_requires_exact_confirmation(self):
        for value in (None, False, 1, "true"):
            with self.assertRaises(ValueError):
                self.auto.start({**self.confirmation(), "confirm_external_orders": value})
        self.assertFalse(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
    def test_account_identity_discovered_during_start_requires_new_review(self):
        self.broker.account_identity = "toss:unverified"
        reviewed = self.confirmation()
        original_account = self.broker.account
        def verify_identity():
            self.broker.account_identity = "toss:verified-live:1"
            return original_account()
        with patch.object(self.broker, "account", side_effect=verify_identity):
            with self.assertRaises(ValueError):
                self.auto.start(reviewed)
        self.assertFalse(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
        self.assertIsNone(self.auto.broker_run_lock.handle)
        self.auto.start(self.confirmation())
        self.assertTrue(self.auto.running.is_set())
    def test_provider_environment_and_mode_cannot_cross_mix(self):
        for payload in ({**TOSS, "environment": "paper"}, {**TOSS, "provider": "unknown"}):
            with self.assertRaises(ValueError):
                self.auto.connect(payload)
        self.auto.update_config({"mode": "kis-live"})
        with self.assertRaises(ValueError):
            self.auto.start(self.confirmation())
        self.assertFalse(self.auto.running.is_set())
        self.assertFalse(self.broker.submissions)
    def test_namespace_is_stable_and_never_collides_with_legacy_kis(self):
        expected = hashlib.sha256(("toss:live:" + self.broker.account_identity).encode()).hexdigest()[:32]
        self.assertEqual(self.auto._namespace(), expected)
        replacement = self.controller(self.store)
        try:
            self.assertEqual(replacement._namespace(), expected)
            self.assertEqual(replacement.state()["connection"]["provider"], "toss")
        finally:
            replacement.shutdown()
        self.auto.update_config({"mode": "paper"})
        self.auto.connect(dict(KIS))
        self.auto.update_config({"mode": "kis-live"})
        legacy = hashlib.sha256(b"live:12345678:01").hexdigest()[:32]
        self.assertEqual(self.auto._namespace(), legacy)
        self.assertNotEqual(legacy, expected)
    def test_restart_keeps_provider_and_secrets_but_is_stopped_and_not_ready(self):
        replacement = self.controller(self.store)
        try:
            state = replacement.state()
            self.assertFalse(state["running"])
            self.assertFalse(state["connection"]["ready"])
            self.assertEqual(state["connection"]["provider"], "toss")
            encoded = json.dumps(state, ensure_ascii=False)
            for secret in (TOSS["client_id"], TOSS["client_secret"], KIS["app_secret"]):
                self.assertNotIn(secret, encoded)
            self.assertTrue(replacement.toss_secrets.path.exists())
            self.assertFalse(self.broker.submissions)
        finally:
            replacement.shutdown()
    def test_pending_order_prevents_provider_or_account_replacement(self):
        self.intent(status="uncertain")
        for action in (lambda: self.auto.connect(dict(KIS)), lambda: self.auto.connect({**TOSS, "account_seq": 2}),
                       self.auto.disconnect, lambda: self.auto.update_config({"mode": "kis-live"})):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.auto.state()["connection"]["provider"], "toss")
    def test_owned_toss_shares_prevent_replacement_disconnect_and_mode_switch(self):
        self.insert_owned()
        for action in (lambda: self.auto.connect(dict(KIS)), lambda: self.auto.connect({**TOSS, "account_seq": 2}),
                       self.auto.disconnect, lambda: self.auto.update_config({"mode": "paper"})):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.auto._owned()["005930"]["quantity"], 10)
    def test_preview_uses_only_toss_quotes_and_never_orders(self):
        state = self.auto.check()
        self.assertEqual(state["preview"][0]["action"], "buy")
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.market.calls, 0)
        self.assertEqual(state["account"]["equity_basis"], "krw-trading-capital")
    def test_account_discovery_masks_accounts_and_does_not_replace_active_provider(self):
        broker = self.auto.broker
        result = self.auto.toss_accounts({"client_id": TOSS["client_id"], "client_secret": TOSS["client_secret"]})
        self.assertEqual(result["accounts"][0]["account_seq"], 1)
        self.assertEqual(result["accounts"][0]["account_masked"], "가짜 계좌 **01")
        self.assertIs(self.auto.broker, broker)
        self.assertEqual(self.auto.state()["connection"]["provider"], "toss")
        self.assertNotIn(TOSS["client_secret"], json.dumps(result))
        self.assertFalse(broker.submissions)
    def test_same_toss_client_cannot_issue_another_token_while_running_other_account(self):
        other_store = DeskStore(Path(self.tmp.name) / "other-data" / "desk.sqlite", "demo")
        other = self.controller(other_store)
        try:
            other.connect({**TOSS, "account_seq": 2})
            other.update_config(self.auto.config())
            other.thread = IdleThread()
            self.assertNotEqual(other._namespace(), self.auto._namespace())
            self.auto.start(self.confirmation())
            created = len(self.created)
            for action in (lambda: other.toss_accounts({"client_id": TOSS["client_id"], "client_secret": TOSS["client_secret"]}),
                           lambda: other.connect({**TOSS, "account_seq": 3}), lambda: other.start(self.confirmation(other))):
                with self.assertRaises(ValueError):
                    action()
            self.assertEqual(len(self.created), created)
            self.assertFalse(other.running.is_set())
            self.assertFalse(self.broker.submissions)
            self.assertFalse(other.broker.submissions)
        finally:
            other.shutdown()
    def test_missing_stale_or_wrong_provider_quotes_never_fall_back(self):
        self.auto.running.set()
        for changes in ({"quote_error": True}, {"quote_source": "naver"}, {"quote_time": AT - timedelta(seconds=91)}):
            for key, value in changes.items():
                setattr(self.broker, key, value)
            self.auto._cycle(execute=True)
            self.assertFalse(self.broker.submissions)
            self.assertEqual(self.market.calls, 0)
            self.broker.quote_error, self.broker.quote_source, self.broker.quote_time = False, "toss", AT
    def test_intent_client_order_id_exists_before_post_and_survives_lost_response(self):
        def inspect(client_order_id):
            with self.store.connection() as db:
                rows = db.execute("SELECT * FROM auto_intents").fetchall()
            self.assertEqual(len(rows), 1)
            row = dict(rows[0])
            metadata = json.loads(row["broker"])
            self.assertEqual(row["status"], "submitting")
            self.assertEqual(client_order_id, row["id"])
            self.assertEqual(metadata["client_order_id"], client_order_id)
            self.assertEqual(metadata["limit_price"], 1200)
        self.broker.before_submit, self.broker.submit_error = inspect, True
        self.auto.running.set()
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.auto.running.is_set())
        self.assertEqual(self.auto.state()["orders"][0]["status"], "uncertain")
        with self.store.connection() as db:
            row = dict(db.execute("SELECT * FROM auto_intents").fetchone())
        self.assertEqual(json.loads(row["broker"])["client_order_id"], row["id"])
        self.auto.check()
        with self.assertRaises(ValueError):
            self.auto.start(self.confirmation())
        self.assertEqual(len(self.broker.submissions), 1)
    def test_accepted_toss_order_waits_for_exact_fills_and_account(self):
        self.auto.running.set()
        self.auto._cycle(execute=True)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.auto.state()["orders"][0]["status"], "pending")
        self.assertFalse(self.auto._owned())
        quantity = self.broker.submissions[0]["quantity"]
        self.broker.quantity = quantity
        self.broker.fill = {"status": "filled", "filled_quantity": quantity, "average_price": 1200}
        self.auto.check()
        self.assertEqual(self.auto._owned()["005930"]["quantity"], quantity)
        self.assertEqual(len(self.broker.submissions), 1)
    def test_total_account_holdings_cap_includes_non_korean_positions(self):
        self.broker.extra_position_count = self.auto.config()["max_positions"]
        self.auto.running.set()
        self.auto._cycle(execute=True)
        self.assertFalse(self.broker.submissions)
        self.assertFalse(self.auto._owned())


    def test_toss_rejected_partial_fill_retains_ownership_and_daily_budget(self):
        intent = self.intent()
        self.broker.quantity = 4
        self.auto._settle(intent, {"status": "rejected", "filled_quantity": 4, "average_price": 1200})
        self.assertEqual(self.auto._owned()["005930"]["quantity"], 4)
        with self.store.connection() as db:
            row = dict(db.execute("SELECT * FROM auto_intents WHERE id=?", (intent["id"],)).fetchone())
            daily = self.auto._daily(db, self.auto._namespace(), self.broker.account(), AT)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["filled_quantity"], 4)
        self.assertEqual(daily["buys"], row["planned_amount"])
        self.assertGreater(daily["buys"], 0)

    def test_restart_restores_verified_namespace_and_resolves_pending(self):
        intent = self.intent(status="uncertain")
        saved_identity = self.auto.toss_secrets.load()["verified_account_identity"]
        namespace = self.auto._namespace()
        def restore_factory(config):
            broker = self.factory(config, "toss")
            broker.account_identity = config["verified_account_identity"]
            return broker
        with patch.object(FakeBroker, "account", side_effect=AssertionError("Restart must not authenticate or query")):
            replacement = AutoTrader(self.store, self.market,
                                     broker_factory=lambda config: self.factory(config, "kis"),
                                     toss_broker_factory=restore_factory, clock=lambda: AT)
        try:
            self.assertFalse(replacement.running.is_set())
            self.assertFalse(replacement.state()["connection"]["ready"])
            self.assertEqual(replacement.broker.account_identity, saved_identity)
            self.assertEqual(replacement._namespace(), namespace)
            replacement.broker.quantity = 10
            replacement.broker.fill = {"status": "filled", "filled_quantity": 10, "average_price": 1200}
            replacement.resolve({"intent_id": intent["id"],
                                 "broker_order_id": "opaque_toss_order_identity_12345678901234567890",
                                 "order_date": "20261008"})
            self.assertEqual(replacement._owned()["005930"]["quantity"], 10)
            self.assertEqual(replacement.state()["orders"][0]["status"], "filled")
            self.assertFalse(replacement.broker.submissions)
            self.assertFalse(self.broker.submissions)
        finally:
            replacement.shutdown()

    def test_empty_account_can_connect_read_only_but_cannot_start(self):
        empty_broker = FakeBroker(dict(TOSS), "toss")
        empty_account = {**empty_broker.account(), "cash": 0, "equity": 0, "positions": [],
                         "total_position_count": 0, "excluded_positions_count": 0, "equity_basis": "krw-trading-capital"}
        with patch.object(self.auto, "toss_broker_factory", return_value=empty_broker), \
             patch.object(empty_broker, "account", return_value=empty_account):
            state = self.auto.connect(dict(TOSS))
            self.assertTrue(state["connection"]["ready"])
            self.assertEqual(state["connection"]["provider"], "toss")
            with self.assertRaises(ValueError):
                self.auto.start(self.confirmation())
            self.assertFalse(self.auto.running.is_set())
            self.assertFalse(empty_broker.submissions)

    def test_int64_account_sequence_survives_browser_wire_format(self):
        seq = 2**63 - 1
        with patch.object(self.broker, "list_accounts", return_value=[
            {"account_seq": seq, "account_masked": "*****0001", "account_type": "BROKERAGE"}]):
            result = self.auto.toss_accounts({"client_id": TOSS["client_id"], "client_secret": TOSS["client_secret"]})
        self.assertEqual(result["accounts"][0]["account_seq"], str(seq))
        self.auto.connect({**TOSS, "account_seq": str(seq)})
        self.assertEqual(self.auto.connection_config["account_seq"], seq)
        for bad in ("0", "-1", "01", str(2**63), "1.0", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.auto.connect({**TOSS, "account_seq": bad})


if __name__ == "__main__":
    unittest.main()
