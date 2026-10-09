"""Controller through the real Toss adapter with a completely fake HTTP transport."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from personal.autotrade import AutoTrader
from personal.engine import DeskStore
from personal.test_toss_broker import Clock, FakeHTTP, candle, holding
from personal.test_toss_integration import IdleThread, NoFallbackMarket
from personal.toss_broker import TossBroker, _TOKENS


class TossExecutionTests(unittest.TestCase):
    def test_durable_intent_crosses_adapter_and_reconciles_then_restarts_stopped(self):
        _TOKENS.clear()
        clock, market = Clock(), NoFallbackMarket()
        http = FakeHTTP(clock)
        http.holdings = {"items": [], "marketValue": {"amount": {"krw": "0", "usd": None}}}
        http.trade_price = "74000"
        days, day = [], clock.now.date() - timedelta(days=1)
        while len(days) < 21:
            if day.weekday() < 5:
                days.append(day.isoformat())
            day -= timedelta(days=1)
        http.chart_pages = {None: {"candles": [candle(day, "72000" if i == 20 else "70000")
                                               for i, day in enumerate(reversed(days))], "nextBefore": None}}
        config = {"provider": "toss", "environment": "live", "client_id": "FAKE_CLIENT",
                  "client_secret": "FAKE_SECRET", "account_seq": "7"}
        with tempfile.TemporaryDirectory() as folder, \
             patch("personal.autotrade.Path.home", return_value=Path(folder) / "fake-home"), \
             patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("external HTTP forbidden")):
            store = DeskStore(Path(folder) / "desk.sqlite", "demo")
            factory = lambda saved: TossBroker(saved, opener=http, clock=clock, sleeper=lambda _: None)
            auto = AutoTrader(store, market, toss_broker_factory=factory, clock=clock)
            restarted = None
            try:
                auto.connect(config)
                auto.update_config({"mode": "toss-live", "symbols": ["005930"], "order_budget": 100000,
                                    "max_daily_buy": 300000, "max_daily_orders": 3})
                namespace = auto._namespace()
                auto.thread = IdleThread()
                auto.start({"confirm_live": True, "confirm_external_orders": True,
                            "review_token": auto.state()["review_token"]})
                auto._cycle(execute=True)
                self.assertEqual(http.count("/api/v1/orders", "POST"), 1)
                self.assertEqual(auto.state()["orders"][0]["status"], "pending")
                self.assertFalse(auto._owned())
                with store.connection() as db:
                    intent = dict(db.execute("SELECT * FROM auto_intents").fetchone())
                self.assertEqual(intent["side"], "buy")
                self.assertEqual(intent["quantity"], 1)
                http.detail.update({"quantity": "1", "price": "74000"})
                http.detail["execution"].update({"filledQuantity": "1", "averageFilledPrice": "74000",
                                                 "filledAmount": "74000"})
                http.holdings = {"items": [holding(quantity="1", price="74000", amount="74000")],
                                 "marketValue": {"amount": {"krw": "74000", "usd": None}}}
                http.cash = "926000"
                auto.check()
                self.assertEqual(auto._owned()["005930"]["quantity"], 1)
                self.assertEqual(auto.state()["orders"][0]["status"], "filled")
                self.assertEqual(market.calls, 0)
                auto.shutdown()
                restarted = AutoTrader(store, market, toss_broker_factory=factory, clock=clock)
                self.assertEqual(restarted._namespace(), namespace)
                self.assertFalse(restarted.running.is_set())
                self.assertEqual(restarted._owned()["005930"]["quantity"], 1)
                self.assertEqual(http.count("/api/v1/orders", "POST"), 1)
            finally:
                auto.shutdown()
                if restarted:
                    restarted.shutdown()


if __name__ == "__main__":
    unittest.main()
