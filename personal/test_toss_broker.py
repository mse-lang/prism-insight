"""Offline Toss contract tests. Every HTTP call uses the injected fake opener."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from personal.kis_broker import BrokerError, OrderRejected, OrderUncertain, KST
from personal.toss_broker import TossBroker, _TOKENS


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, 10, 0, tzinfo=KST)

    def __call__(self):
        return self.now


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.headers = {}
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.closed = False

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        self.closed = True


def holding(symbol="005930", country="KR", currency="KRW", quantity="2", price="70000", amount="140000"):
    return {"symbol": symbol, "name": "삼성전자", "marketCountry": country,
            "currency": currency, "quantity": quantity, "lastPrice": price,
            "averagePurchasePrice": "65000" if country == "KR" else "180",
            "marketValue": {"amount": amount, "purchaseAmount": "130000", "amountAfterCost": amount},
            "profitLoss": {}, "dailyProfitLoss": {}, "cost": {}}


def candle(day, close="69000"):
    return {"timestamp": day + "T00:00:00+09:00", "openPrice": close,
            "highPrice": close, "lowPrice": close, "closePrice": close,
            "volume": "1000", "currency": "KRW"}


class FakeHTTP:
    def __init__(self, clock):
        self.clock = clock
        self.calls, self.overrides = [], {}
        self.account_rows = [{"accountSeq": 7, "accountNo": "12345678901", "accountType": "BROKERAGE"}]
        self.holdings = {"items": [holding()], "marketValue": {"amount": {"krw": "140000", "usd": None}},
                         "totalPurchaseAmount": {"krw": "130000", "usd": None}, "profitLoss": {}, "dailyProfitLoss": {}}
        self.cash = "1000000"
        self.commissions = [{"marketCountry": "KR", "commissionRate": "0.001",
                             "startDate": "2026-01-01", "endDate": None}]
        self.sellable = "2"
        self.stock = {"symbol": "005930", "name": "삼성전자", "currency": "KRW",
                      "market": "KOSPI", "status": "ACTIVE",
                      "koreanMarketDetail": {"krxTradingSuspended": False, "nxtSupported": True}}
        self.price_stamp = self.clock.now - timedelta(seconds=2)
        self.trade_stamp = self.clock.now - timedelta(seconds=2)
        self.trade_price, self.trades_present = "70000", True
        self.calendar_open, self.krx_open = True, True
        self.orders = {"orders": [], "nextCursor": None, "hasNext": False}
        self.conditional_pages = {None: {"conditionalOrders": [], "nextCursor": None, "hasNext": False}}
        self.chart_pages = {None: {"candles": [candle("2026-10-08", "70000"), candle("2026-10-07")], "nextBefore": None}}
        self.detail = {"orderId": "opaque_ORDER-123", "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
                       "timeInForce": "DAY", "status": "FILLED", "price": "70000", "quantity": "2", "currency": "KRW",
                       "orderedAt": self.clock.now.isoformat(),
                       "execution": {"filledQuantity": "2", "averageFilledPrice": "70000", "filledAmount": "140000",
                                     "commission": "140", "tax": None, "filledAt": self.clock.now.isoformat(), "settlementDate": None}}

    def result(self, endpoint, result, *, method="GET", status=200):
        self.overrides[(method, "/api/v1/" + endpoint)] = (status, {"result": result})

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        if parsed.scheme != "https" or parsed.netloc != "openapi.tossinvest.com":
            raise AssertionError("unexpected host")
        if timeout != 10:
            raise AssertionError("unexpected timeout")
        self.calls.append(request)
        key = (request.get_method(), parsed.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        if key in self.overrides:
            override = self.overrides[key]
            if isinstance(override, Exception):
                raise override
            status, payload = override(request, query) if callable(override) else override
            return Response(copy.deepcopy(payload), status)
        path = parsed.path
        if path == "/oauth2/token":
            return Response({"access_token": "FAKE_ACCESS_TOKEN", "token_type": "Bearer", "expires_in": 86400})
        if path == "/api/v1/accounts":
            value = self.account_rows
        elif path == "/api/v1/holdings":
            value = self.holdings
        elif path == "/api/v1/buying-power":
            if query != {"currency": "KRW"}:
                raise AssertionError("cash query must be KRW")
            value = {"currency": "KRW", "cashBuyingPower": self.cash}
        elif path == "/api/v1/commissions":
            value = self.commissions
        elif path == "/api/v1/sellable-quantity":
            if query != {"symbol": "005930"}:
                raise AssertionError("sellable quantity must be symbol scoped")
            value = {"sellableQuantity": self.sellable}
        elif path == "/api/v1/stocks":
            value = [self.stock]
        elif path == "/api/v1/prices":
            value = [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW",
                      "timestamp": self.price_stamp.isoformat() if self.price_stamp else None}]
        elif path == "/api/v1/trades":
            value = [{"price": self.trade_price, "volume": "3", "currency": "KRW",
                      "timestamp": self.trade_stamp.isoformat()}] if self.trades_present else []
        elif path == "/api/v1/market-calendar/KR":
            day = self.clock.now.date().isoformat()
            regular = {"startTime": day + "T09:00:00+09:00", "endTime": day + "T15:30:00+09:00",
                       "singlePriceAuctionStartTime": day + "T15:20:00+09:00" if self.krx_open else None}
            value = {"today": {"date": day, "integrated": {"regularMarket": regular} if self.calendar_open else None},
                     "previousBusinessDay": {}, "nextBusinessDay": {}}
        elif path == "/api/v1/candles":
            if query.get("symbol") != "005930" or query.get("interval") != "1d" or query.get("adjusted") != "true":
                raise AssertionError("unexpected candle scope")
            value = self.chart_pages[query.get("before")]
        elif path == "/api/v1/conditional-orders":
            if query.get("status") != "OPEN":
                raise AssertionError("conditional query must be OPEN")
            value = self.conditional_pages[query.get("cursor")]
        elif path == "/api/v1/orders" and request.get_method() == "POST":
            body = json.loads(request.data)
            value = {"orderId": "opaque_ORDER-123", "clientOrderId": body["clientOrderId"]}
        elif path == "/api/v1/orders":
            if query != {"status": "OPEN"}:
                raise AssertionError("pending query must be OPEN")
            value = self.orders
        elif path == "/api/v1/orders/opaque_ORDER-123":
            value = self.detail
        else:
            raise AssertionError("unexpected endpoint: " + path)
        return Response({"result": copy.deepcopy(value)})

    def count(self, path, method=None):
        return sum(urlsplit(r.full_url).path == path and (method is None or r.get_method() == method) for r in self.calls)


class TossBrokerTests(unittest.TestCase):
    def setUp(self):
        _TOKENS.clear()
        self.clock = Clock()
        self.http = FakeHTTP(self.clock)
        self.config = {"provider": "toss", "environment": "live", "client_id": "FAKE_CLIENT",
                       "client_secret": "FAKE_SECRET", "account_seq": 7}
        self.broker = TossBroker(self.config, opener=self.http, clock=self.clock, sleeper=lambda _: None)

    def receipt(self):
        return {"broker_order_id": "opaque_ORDER-123", "symbol": "005930", "side": "buy", "quantity": 2,
                "limit_price": 70000, "order_date": "20261008", "client_order_id": "durable-intent-123",
                "account_identity": hashlib.sha256(b"toss:live:12345678901").hexdigest(), "environment": "live",
                "attempt_at": self.clock.now.isoformat()}

    def submit(self, side="buy", quantity=2):
        return self.broker.submit("005930", side, quantity, limit_price=70000, client_order_id="durable-intent-123")

    def test_constructor_has_no_io_and_live_only(self):
        self.assertEqual(self.http.calls, [])
        with self.assertRaises(BrokerError):
            TossBroker({**self.config, "environment": "paper"}, opener=self.http)
        with self.assertRaises(BrokerError):
            TossBroker({**self.config, "account_seq": True}, opener=self.http)
        with self.assertRaises(BrokerError):
            TossBroker({**self.config, "host": "https://example.invalid"}, opener=self.http)
        self.assertEqual(self.http.calls, [])

    def test_discovery_oauth_form_and_mask(self):
        temporary = TossBroker({k: v for k, v in self.config.items() if k != "account_seq"}, opener=self.http, clock=self.clock, sleeper=lambda _: None)
        accounts = temporary.list_accounts()
        self.assertEqual(accounts, [{"account_seq": 7, "account_masked": "*******8901", "account_type": "BROKERAGE"}])
        auth = self.http.calls[0]
        self.assertEqual(auth.get_method(), "POST")
        self.assertEqual(auth.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(auth.data.decode()), {"grant_type": ["client_credentials"], "client_id": ["FAKE_CLIENT"], "client_secret": ["FAKE_SECRET"]})
        self.assertNotIn("12345678901", json.dumps(accounts))
        with self.assertRaises(BrokerError):
            temporary.account()

    def test_token_shared_for_same_client_without_reissue(self):
        self.broker.list_accounts()
        other = TossBroker(self.config, opener=self.http, clock=self.clock, sleeper=lambda _: None)
        other.list_accounts()
        self.assertEqual(self.http.count("/oauth2/token"), 1)

    def test_identity_persists_without_io_and_is_verified(self):
        account = self.broker.account()
        expected = hashlib.sha256(b"toss:live:12345678901").hexdigest()
        self.assertEqual(account["account_identity"], expected)
        before = len(self.http.calls)
        restarted = TossBroker({**self.config, "verified_account_identity": expected, "account_masked": "*******8901"}, opener=self.http, clock=self.clock, sleeper=lambda _: None)
        self.assertEqual(restarted.account_identity, expected)
        self.assertEqual(len(self.http.calls), before)
        self.assertEqual(restarted.account()["account_identity"], expected)
        private = [r for r in self.http.calls if urlsplit(r.full_url).path == "/api/v1/holdings"]
        self.assertEqual(private[-1].get_header("X-tossinvest-account"), "7")
        self.http.account_rows[0]["accountNo"] = "99999999999"
        with self.assertRaises(BrokerError):
            restarted.account()

    def test_unowned_and_duplicate_account_rejected(self):
        self.http.account_rows[0]["accountSeq"] = 8
        with self.assertRaises(BrokerError):
            self.broker.account()
        self.assertEqual(self.http.count("/api/v1/holdings"), 0)
        self.http.account_rows.append(copy.deepcopy(self.http.account_rows[0]))
        with self.assertRaises(BrokerError):
            self.broker.list_accounts()

    def test_account_cash_and_domestic_valuation(self):
        account = self.broker.account()
        self.assertEqual(account["cash"], 1000000)
        self.assertEqual(account["equity"], 1140000)
        self.assertEqual(account["positions"][0]["quantity"], 2)
        self.assertEqual(account["total_position_count"], 1)
        self.assertEqual(account["equity_basis"], "krw-trading-capital")
        self.assertEqual(account["order_visibility"], "api-supported-types")

    def test_empty_unfunded_account_is_readable(self):
        self.http.holdings["items"] = []
        self.http.holdings["marketValue"]["amount"]["krw"] = "0"
        self.http.cash = "0"
        account = self.broker.account()
        self.assertEqual((account["cash"], account["equity"], account["total_position_count"]), (0, 0, 0))

    def test_mixed_us_and_alphanumeric_kr_preserve_count_and_kr_basis(self):
        self.http.holdings["items"].extend([holding("AAPL", "US", "USD", ".5", "200", "100"), holding("A12345", quantity="1", price="20000", amount="20000")])
        self.http.holdings["marketValue"]["amount"] = {"krw": "160000", "usd": "100"}
        account = self.broker.account()
        self.assertEqual(account["equity"], 1160000)
        self.assertEqual(account["total_position_count"], 3)
        self.assertEqual(account["excluded_positions_count"], 2)
        self.assertEqual([p["symbol"] for p in account["positions"]], ["005930"])

    def test_missing_cash_never_defaults_to_zero(self):
        self.http.result("buying-power", {"currency": "KRW"})
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_unknown_or_mismatched_holdings_totals_rejected(self):
        for value in (None, "NaN", "139999", "140001"):
            with self.subTest(value=value):
                self.http.holdings["marketValue"]["amount"]["krw"] = value
                with self.assertRaises(BrokerError):
                    self.broker.account()
        self.http.holdings["marketValue"]["amount"]["krw"] = "140000"
        self.http.holdings["items"].append(holding("AAPL", "US", "USD", ".5", "200", "100"))
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_quote_requires_recent_trade_and_positive_krx_calendar(self):
        quote = self.broker.quote("005930")
        self.assertEqual(quote["market_status"], "OPEN")
        self.assertEqual(quote["source"], "toss")
        self.assertEqual(quote["price"], 70000)
        self.assertEqual(quote["as_of"], self.http.trade_stamp.isoformat())
        self.assertEqual(quote["timestamp_basis"], "last_trade")
        self.assertEqual(quote["venue_scope"], "krx-nxt-integrated")

    def test_holiday_and_nxt_only_calendar_cannot_open(self):
        self.http.calendar_open = False
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        self.http.calendar_open, self.http.krx_open = True, False
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        with self.assertRaises(OrderRejected):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_stale_or_missing_recent_trade_cannot_open(self):
        self.http.trade_stamp = self.clock.now - timedelta(seconds=91)
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        self.http.trades_present = False
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        with self.assertRaises(OrderRejected):
            self.submit()

    def test_future_or_missing_source_timestamp_rejected(self):
        self.http.price_stamp = self.clock.now + timedelta(seconds=6)
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")
        self.http.price_stamp = None
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")
        self.http.price_stamp = self.clock.now
        self.http.trade_stamp = self.clock.now + timedelta(seconds=6)
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")

    def test_krx_halt_and_unknown_halt_state_fail_closed(self):
        self.http.stock["koreanMarketDetail"]["krxTradingSuspended"] = True
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        self.http.stock["koreanMarketDetail"]["krxTradingSuspended"] = None
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")

    def test_failed_quote_invalidates_previous_proof(self):
        self.broker.quote("005930")
        self.http.stock["status"] = "DELISTED"
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")
        with self.assertRaises(OrderRejected):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_order_proof_binds_symbol_and_price(self):
        self.broker.quote("005930")
        with self.assertRaises(OrderRejected):
            self.broker.submit("000660", "buy", 1, limit_price=70000, client_order_id="durable-intent-2")
        with self.assertRaises(OrderRejected):
            self.broker.submit("005930", "buy", 1, limit_price=70100, client_order_id="durable-intent-2")
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_proof_expiry_and_final_1520_boundary(self):
        self.broker.quote("005930")
        self.clock.now += timedelta(seconds=91)
        with self.assertRaises(OrderRejected):
            self.submit()
        self.clock.now = datetime(2026, 10, 8, 15, 19, 59, tzinfo=KST)
        self.http.price_stamp = self.http.trade_stamp = self.clock.now
        self.assertEqual(self.broker.quote("005930")["market_status"], "OPEN")
        self.clock.now += timedelta(seconds=1)
        with self.assertRaises(OrderRejected):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_auth_delay_rechecks_freshness_before_order_wire(self):
        self.broker.quote("005930")
        _TOKENS.clear()
        def delayed_token(request, query):
            self.clock.now += timedelta(seconds=91)
            return 200, {"access_token": "NEW_FAKE_TOKEN", "token_type": "Bearer", "expires_in": 86400}
        self.http.overrides[("POST", "/oauth2/token")] = delayed_token
        with self.assertRaises(OrderRejected):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_rate_wait_delay_rechecks_freshness_before_order_wire(self):
        self.broker.quote("005930")
        delayed = []
        def delay_once(seconds):
            if not delayed:
                delayed.append(True)
                self.clock.now += timedelta(seconds=91)
        self.broker._sleep = delay_once
        with self.assertRaises(OrderRejected):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_buying_power_is_cash_derived_with_fee_reserve(self):
        power = self.broker.buying_power("005930", limit_price=70000)
        self.assertEqual(power["cash"], 1000000)
        self.assertEqual(power["quantity"], 14)
        self.assertEqual(power["quantity_basis"], "cash-derived")
        with self.assertRaises(BrokerError):
            self.broker.buying_power("005930")
        self.http.commissions = []
        with self.assertRaises(BrokerError):
            self.broker.buying_power("005930", limit_price=70000)

    def test_buy_cannot_use_margin_or_unknown_cash(self):
        self.broker.quote("005930")
        self.http.result("buying-power", {"currency": "KRW", "cashBuyingPower": "0", "marginBuyingPower": "900000000"})
        with self.assertRaises(OrderRejected):
            self.submit()
        self.http.result("buying-power", {"currency": "KRW", "marginBuyingPower": "900000000"})
        with self.assertRaises(BrokerError):
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_sell_requires_verified_sellable_quantity(self):
        self.broker.quote("005930")
        self.http.sellable = "1"
        with self.assertRaises(OrderRejected):
            self.submit("sell")
        self.http.sellable = None
        with self.assertRaises(BrokerError):
            self.submit("sell")
        self.http.sellable = "2"
        receipt = self.submit("sell")
        self.assertEqual(receipt["side"], "sell")
        body = json.loads([r for r in self.http.calls if r.get_method() == "POST" and urlsplit(r.full_url).path == "/api/v1/orders"][-1].data)
        self.assertEqual(body["side"], "SELL")
        self.assertEqual(self.http.count("/api/v1/sellable-quantity"), 3)

    def test_submit_limit_cash_receipt_is_acceptance_not_fill(self):
        self.broker.quote("005930")
        receipt = self.submit()
        self.assertEqual(receipt["broker_order_id"], "opaque_ORDER-123")
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["side"], "buy")
        self.assertEqual(receipt["order_date"], "20261008")
        self.assertEqual(receipt["attempt_at"], self.clock.now.isoformat())
        body = json.loads([r for r in self.http.calls if r.get_method() == "POST" and urlsplit(r.full_url).path == "/api/v1/orders"][-1].data)
        self.assertEqual(body, {"symbol": "005930", "side": "BUY", "quantity": "2", "price": "70000",
                                "orderType": "LIMIT", "timeInForce": "DAY", "clientOrderId": "durable-intent-123", "confirmHighValueOrder": False})
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 1)

    def test_high_value_and_invalid_quantity_never_send_orders(self):
        with self.assertRaises(OrderRejected) as caught:
            self.submit(quantity=1500)
        self.assertIn("1억원", str(caught.exception))
        for quantity in (True, 2.0, 0, -1):
            with self.subTest(quantity=quantity), self.assertRaises(BrokerError):
                self.submit(quantity=quantity)
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 0)

    def test_uncertain_timeout_never_retries_or_discloses_raw_error(self):
        self.broker.quote("005930")
        self.http.overrides[("POST", "/api/v1/orders")] = TimeoutError("FAKE_SECRET FAKE_ACCESS_TOKEN RAW_SERVER_ERROR")
        with self.assertRaises(OrderUncertain) as caught:
            self.submit()
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 1)
        for secret in ("FAKE_SECRET", "FAKE_ACCESS_TOKEN", "RAW_SERVER_ERROR"):
            self.assertNotIn(secret, str(caught.exception))

    def test_gateway_error_is_uncertain_even_with_rejection_code(self):
        self.broker.quote("005930")
        self.http.overrides[("POST", "/api/v1/orders")] = (500, {"error": {"code": "invalid-request", "message": "FAKE_SECRET"}})
        with self.assertRaises(OrderUncertain) as caught:
            self.submit()
        self.assertNotIn("FAKE_SECRET", str(caught.exception))
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 1)

    def test_explicit_documented_400_rejection_is_sanitized(self):
        self.broker.quote("005930")
        self.http.overrides[("POST", "/api/v1/orders")] = (400, {"error": {"code": "invalid-request", "message": "FAKE_SECRET"}})
        with self.assertRaises(OrderRejected) as caught:
            self.submit()
        self.assertNotIn("FAKE_SECRET", str(caught.exception))
        self.assertEqual(self.http.count("/api/v1/orders", "POST"), 1)

    def test_incomplete_acceptance_receipt_stays_uncertain(self):
        self.broker.quote("005930")
        for result in ({"orderId": "opaque_ORDER-123"}, {"orderId": "opaque_ORDER-123", "clientOrderId": "wrong-key"},
                       {"orderId": "unsafe/path", "clientOrderId": "durable-intent-123"}):
            with self.subTest(result=result):
                before = self.http.count("/api/v1/orders", "POST")
                self.http.result("orders", result, method="POST")
                with self.assertRaises(OrderUncertain):
                    self.submit()
                self.assertEqual(self.http.count("/api/v1/orders", "POST"), before + 1)

    def test_exact_opaque_order_detail_and_unknown_fees_preserved(self):
        fill = self.broker.fills(self.receipt())
        self.assertEqual(fill["status"], "filled")
        self.assertTrue(fill["terminal"])
        self.assertEqual(fill["filled_quantity"], 2)
        self.assertEqual(fill["average_price"], 70000)
        self.assertIsNone(fill["tax"])
        request = [r for r in self.http.calls if urlsplit(r.full_url).path.endswith("opaque_ORDER-123")][-1]
        self.assertEqual(request.get_header("X-tossinvest-account"), "7")

    def test_detail_identity_and_economics_must_match(self):
        original = copy.deepcopy(self.http.detail)
        changes = [("orderId", "different-id"), ("symbol", "000660"), ("side", "SELL"),
                   ("orderType", "MARKET"), ("timeInForce", "OPG"), ("currency", "USD"),
                   ("quantity", "3"), ("price", "70100"), ("clientOrderId", "different-intent")]
        for field, value in changes:
            with self.subTest(field=field):
                self.http.detail = {**copy.deepcopy(original), field: value}
                with self.assertRaises(BrokerError):
                    self.broker.fills(self.receipt())
        self.http.detail = original
        wrong = {**self.receipt(), "account_identity": "0" * 64}
        before = self.http.count("/api/v1/orders/opaque_ORDER-123")
        with self.assertRaises(BrokerError):
            self.broker.fills(wrong)
        self.assertEqual(self.http.count("/api/v1/orders/opaque_ORDER-123"), before)

    def test_earlier_matching_manual_order_and_future_order_are_rejected(self):
        self.http.detail["orderedAt"] = (self.clock.now - timedelta(seconds=6)).isoformat()
        with self.assertRaises(BrokerError):
            self.broker.fills(self.receipt())
        self.http.detail["orderedAt"] = (self.clock.now + timedelta(seconds=6)).isoformat()
        with self.assertRaises(BrokerError):
            self.broker.fills(self.receipt())
        self.http.detail["orderedAt"] = self.clock.now.isoformat()
        receipt = self.receipt()
        receipt["created_at"] = receipt.pop("attempt_at")
        self.assertEqual(self.broker.fills(receipt)["filled_quantity"], 2)

    def test_partial_pending_canceled_and_rejected_preserve_fill(self):
        self.http.detail["execution"].update({"filledQuantity": "1", "averageFilledPrice": "70000", "filledAmount": "70000"})
        for broker_status, normalized, terminal in (("PARTIAL_FILLED", "partial", False),
                                                     ("CANCELED", "canceled", True),
                                                     ("REJECTED", "rejected", True)):
            with self.subTest(broker_status=broker_status):
                self.http.detail["status"] = broker_status
                fill = self.broker.fills(self.receipt())
                self.assertEqual(fill["status"], normalized)
                self.assertEqual(fill["terminal"], terminal)
                self.assertEqual(fill["filled_quantity"], 1)
                self.assertEqual(fill["remaining_quantity"], 1)
                self.assertEqual(fill["broker_status"], broker_status)

    def test_unfilled_pending_requires_explicit_zero_quantity(self):
        self.http.detail["status"] = "PENDING"
        self.http.detail["execution"].update({"filledQuantity": "0", "averageFilledPrice": None,
                                               "filledAmount": None, "filledAt": None, "commission": None})
        fill = self.broker.fills(self.receipt())
        self.assertEqual(fill["status"], "pending")
        self.assertEqual(fill["filled_quantity"], 0)
        self.assertIsNone(fill["average_price"])
        self.http.detail["execution"].pop("filledQuantity")
        with self.assertRaises(BrokerError):
            self.broker.fills(self.receipt())

    def test_fill_arithmetic_limit_and_unsupported_state_fail_closed(self):
        original = copy.deepcopy(self.http.detail)
        for field, value in (("filledQuantity", "3"), ("averageFilledPrice", None), ("filledAmount", "1"),
                             ("averageFilledPrice", "70001"), ("filledAt", None)):
            with self.subTest(field=field, value=value):
                self.http.detail = copy.deepcopy(original)
                self.http.detail["execution"][field] = value
                with self.assertRaises(BrokerError):
                    self.broker.fills(self.receipt())
        for status in ("REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED", "NEW_UNKNOWN_STATUS"):
            with self.subTest(status=status):
                self.http.detail = {**copy.deepcopy(original), "status": status}
                with self.assertRaises(BrokerError):
                    self.broker.fills(self.receipt())

    def test_pending_and_active_conditional_orders_block_account(self):
        pending = copy.deepcopy(self.http.detail)
        pending["status"] = "PENDING"
        pending["execution"].update({"filledQuantity": "0", "averageFilledPrice": None, "filledAmount": None, "filledAt": None})
        self.http.orders["orders"] = [pending]
        self.http.conditional_pages[None]["conditionalOrders"] = [{"conditionalOrderId": "conditional-1", "type": "OTO",
            "status": "WATCHING", "symbol": "005930", "market": "KR", "quantity": "2", "orderType": "LIMIT",
            "first": {"type": "STOP", "status": "WATCHING", "orderSide": "SELL"}, "createdAt": self.clock.now.isoformat()}]
        account = self.broker.account()
        self.assertEqual(len(account["open_orders"]), 2)
        self.assertEqual(account["open_orders"][1]["kind"], "conditional")
        self.assertEqual(account["order_visibility"], "api-supported-types")

    def test_all_conditional_pages_and_duplicate_cursor_fail(self):
        def conditional(identity):
            return {"conditionalOrderId": identity, "type": "SINGLE", "status": "WATCHING", "symbol": "005930",
                    "market": "KR", "quantity": "1", "orderType": "LIMIT", "createdAt": self.clock.now.isoformat(),
                    "first": {"type": "STOP", "status": "WATCHING", "orderSide": "SELL"}}
        self.http.conditional_pages = {None: {"conditionalOrders": [conditional("c-1")], "hasNext": True, "nextCursor": "page-2"},
                                       "page-2": {"conditionalOrders": [conditional("c-2")], "hasNext": False, "nextCursor": None}}
        self.assertEqual(len(self.broker.account()["open_orders"]), 2)
        self.http.conditional_pages["page-2"]["conditionalOrders"] = [conditional("c-1")]
        with self.assertRaises(BrokerError):
            self.broker.account()
        self.http.conditional_pages["page-2"] = {"conditionalOrders": [], "hasNext": True, "nextCursor": "page-2"}
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_open_order_list_must_be_complete_and_unique(self):
        self.http.orders = {"orders": [], "hasNext": True, "nextCursor": "unexpected-page"}
        with self.assertRaises(BrokerError):
            self.broker.account()
        duplicate = {**copy.deepcopy(self.http.detail), "status": "PENDING"}
        self.http.orders = {"orders": [duplicate, copy.deepcopy(duplicate)], "hasNext": False, "nextCursor": None}
        with self.assertRaises(BrokerError):
            self.broker.account()
        self.http.orders = {"orders": [], "nextCursor": None}
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_history_inclusive_boundary_is_deduplicated(self):
        boundary = "2026-10-07T00:00:00+09:00"
        self.http.chart_pages = {None: {"candles": [candle("2026-10-08"), candle("2026-10-07")], "nextBefore": boundary},
                                 boundary: {"candles": [candle("2026-10-07"), candle("2026-10-06")], "nextBefore": None}}
        history = self.broker.history("005930")
        self.assertEqual([row["date"] for row in history], ["2026-10-06", "2026-10-07", "2026-10-08"])
        self.assertEqual(self.http.count("/api/v1/candles"), 2)

    def test_history_conflicting_boundary_and_repeated_cursor_fail(self):
        boundary = "2026-10-07T00:00:00+09:00"
        self.http.chart_pages = {None: {"candles": [candle("2026-10-07")], "nextBefore": boundary},
                                 boundary: {"candles": [candle("2026-10-07", "68000")], "nextBefore": None}}
        with self.assertRaises(BrokerError):
            self.broker.history("005930")
        self.http.chart_pages[boundary] = {"candles": [candle("2026-10-06")], "nextBefore": boundary}
        with self.assertRaises(BrokerError):
            self.broker.history("005930")

    def test_history_future_unknown_price_and_currency_fail(self):
        for row in (candle("2026-10-09"), {**candle("2026-10-07"), "closePrice": None},
                    {**candle("2026-10-07"), "currency": "USD"}):
            with self.subTest(row=row):
                self.http.chart_pages = {None: {"candles": [row], "nextBefore": None}}
                with self.assertRaises(BrokerError):
                    self.broker.history("005930")


if __name__ == "__main__":
    unittest.main()
