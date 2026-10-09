"""Offline KIS contract tests: no credentials, broker networking, or real orders."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from personal.kis_broker import BrokerError, KISBroker, KST, OrderRejected, OrderUncertain

NOW = datetime(2026, 10, 12, 10, 0, 30, tzinfo=KST)
CONFIG = {"environment": "paper", "app_key": "offline-app-key", "app_secret": "offline-app-secret",
          "account_no": "12345678", "product_code": "01"}


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status, self.headers = status, headers or {"tr_cont": "D"}
        self.closed = False

    def read(self, size):
        return self.payload[:size]

    def close(self):
        self.closed = True


def fill_row(**updates):
    result = {"pdno": "005930", "odno": "0000123456", "ord_gno_brno": "06010", "ord_dt": "20261012",
              "sll_buy_dvsn_cd": "02", "ord_qty": "3", "tot_ccld_qty": "0", "rmn_qty": "3",
              "cnc_cfrm_qty": "0", "rjct_qty": "0", "cncl_yn": "N", "avg_prvs": "0", "tot_ccld_amt": "0"}
    result.update(updates)
    return result


class Transport:
    def __init__(self):
        self.requests, self.lock, self.order_response = [], threading.Lock(), None
        self.calendar = "Y"
        self.history_rows = [{"stck_bsop_date": "20261009", "stck_clpr": "69000"},
                             {"stck_bsop_date": "20261012", "stck_clpr": "70000"}]
        self.minute_rows = [{"stck_bsop_date": "20261012", "stck_cntg_hour": "100000", "stck_prpr": "70000", "cntg_vol": "1"}]
        self.trade_rows = [{"stck_cntg_hour": "100025", "stck_prpr": "70010"}]
        self.quote_updates = {}
        self.balance_pages = None
        self.balance_rows = [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "2", "pchs_avg_pric": "69000.5", "prpr": "70000"}]
        self.summary = {"ord_psbl_cash": "9000000", "tot_evlu_amt": "10000000"}
        self.daily_rows = []
        self.daily_headers = {"tr_cont": "D"}
        self.power = {"00": {"ord_psbl_cash": "9000000", "nrcvb_buy_amt": "8000000", "nrcvb_buy_qty": "100"},
                      "01": {"ord_psbl_cash": "9000000", "nrcvb_buy_amt": "7000000", "nrcvb_buy_qty": "90"}}

    def __call__(self, request, timeout):
        self.assert_timeout = timeout
        parsed = urlparse(request.full_url)
        params = json.loads(request.data) if request.data is not None else {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        with self.lock:
            self.requests.append((request, params))
        path = parsed.path
        if path == "/oauth2/tokenP":
            return Response({"access_token": "offline-token", "expires_in": 86400})
        if path.endswith("chk-holiday"):
            if self.calendar == "unsupported":
                return Response({"rt_cd": "1", "msg1": "secret should never be exposed"})
            return Response({"rt_cd": "0", "output": [{"bass_dt": "20261012", "opnd_yn": self.calendar}]})
        if path.endswith("inquire-price"):
            return Response({"rt_cd": "0", "output": {"stck_shrn_iscd": "005930", "temp_stop_yn": "N", "stck_prpr": "70000",
                                                       "prdy_vrss": "1000", "prdy_vrss_sign": "2", "acml_vol": "10000", **self.quote_updates}})
        if path.endswith("inquire-daily-itemchartprice"):
            return Response({"rt_cd": "0", "output2": self.history_rows})
        if path.endswith("inquire-time-itemchartprice"):
            return Response({"rt_cd": "0", "output2": self.minute_rows})
        if path.endswith("inquire-ccnl"):
            return Response({"rt_cd": "0", "output": self.trade_rows})
        if path.endswith("inquire-psbl-order"):
            return Response({"rt_cd": "0", "output": self.power[params["ORD_DVSN"]]})
        if path.endswith("inquire-balance"):
            if self.balance_pages:
                return self.balance_pages.pop(0)
            return Response({"rt_cd": "0", "output1": self.balance_rows, "output2": [self.summary]})
        if path.endswith("inquire-daily-ccld"):
            return Response({"rt_cd": "0", "output1": self.daily_rows, "output2": {}}, headers=self.daily_headers)
        if path.endswith("order-cash"):
            if isinstance(self.order_response, Exception):
                raise self.order_response
            if self.order_response is not None:
                return self.order_response
            return Response({"rt_cd": "0", "output": {"ODNO": "0000123456", "KRX_FWDG_ORD_ORGNO": "06010", "ORD_TMD": "100030"}})
        raise AssertionError("Unexpected offline route")


class KISBrokerTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.broker = KISBroker(CONFIG, opener=self.transport, clock=lambda: NOW, sleeper=lambda seconds: None)

    def accepted(self):
        quote = self.broker.quote("005930")
        return self.broker.submit("005930", "buy", 3, quote["price"])

    def order_requests(self):
        return [(r, p) for r, p in self.transport.requests if r.full_url.endswith("order-cash")]

    def test_constructor_validation_and_no_network(self):
        self.assertEqual(self.transport.requests, [])
        for field, value in (("environment", "real"), ("account_no", 12345678), ("account_no", "123456789"),
                             ("product_code", "1"), ("app_key", "key\nleak"), ("app_secret", "")):
            with self.subTest(field=field, value=value), self.assertRaises(BrokerError):
                KISBroker({**CONFIG, field: value}, opener=self.transport)

    def test_environment_hosts_and_account_isolation(self):
        self.broker.account()
        self.assertTrue(all(urlparse(req.full_url).netloc == "openapivts.koreainvestment.com:29443" for req, _ in self.transport.requests))
        live = KISBroker({**CONFIG, "environment": "live"}, opener=Transport(), clock=lambda: NOW, sleeper=lambda seconds: None)
        self.assertNotEqual(live.account_identity, self.broker.account_identity)
        result = live.account()
        self.assertEqual(result["environment"], "live")
        self.assertEqual(live.host, "https://openapi.koreainvestment.com:9443")

    def test_token_cache_single_issuance_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.broker.buying_power("005930"), range(8)))
        self.assertEqual(sum(req.full_url.endswith("/oauth2/tokenP") for req, _ in self.transport.requests), 1)

    def test_account_cash_equity_and_unknown_rejected(self):
        result = self.broker.account()
        self.assertEqual((result["cash"], result["equity"]), (9000000, 10000000))
        self.assertEqual(result["positions"][0]["average_cost"], 69000.5)
        for bad in (None, "", "NaN", "-1", "1e999"):
            with self.subTest(bad=bad):
                self.transport.summary["ord_psbl_cash"] = bad
                with self.assertRaises(BrokerError):
                    self.broker.account()

    def test_balance_pagination_all_pages_or_error(self):
        first = {"rt_cd": "0", "output1": self.transport.balance_rows, "output2": [self.transport.summary],
                 "ctx_area_fk100": "cursor", "ctx_area_nk100": "next"}
        second = {"rt_cd": "0", "output1": [{"pdno": "000660", "hldg_qty": "1", "pchs_avg_pric": "100000", "prpr": "110000"}],
                  "output2": [self.transport.summary]}
        self.transport.balance_pages = [Response(first, headers={"tr_cont": "M"}), Response(second)]
        self.assertEqual(len(self.broker.account()["positions"]), 2)
        self.transport.balance_pages = [Response({**first, "ctx_area_fk100": "", "ctx_area_nk100": ""}, headers={"tr_cont": "M"})]
        with self.assertRaises(BrokerError):
            self.broker.account()

    def test_buying_power_uses_no_credit_minimum_and_exact_symbol(self):
        result = self.broker.buying_power("005930", 70000)
        self.assertEqual((result["cash"], result["quantity"]), (7000000, 90))
        calls = [params for req, params in self.transport.requests if "inquire-psbl-order" in req.full_url]
        self.assertEqual([row["ORD_DVSN"] for row in calls], ["00", "01"])
        self.assertTrue(all(row["PDNO"] == "005930" and row["CMA_EVLU_AMT_ICLD_YN"] == "N" for row in calls))
        del self.transport.power["01"]["nrcvb_buy_qty"]
        with self.assertRaises(BrokerError):
            self.broker.buying_power("005930")

    def test_quote_uses_real_trade_time_and_market_status(self):
        result = self.broker.quote("005930")
        self.assertEqual(result["market_status"], "OPEN")
        self.assertEqual(result["timestamp_basis"], "last_trade")
        self.assertEqual(result["price"], 70010)
        self.assertTrue(result["as_of"].endswith("10:00:25+09:00"))
        self.assertNotEqual(result["as_of"], result["retrieved_at"])

    def test_paper_unknown_calendar_needs_positive_actual_date_and_trade(self):
        self.transport.calendar = "unsupported"
        self.assertEqual(self.broker.quote("005930")["market_status"], "OPEN")
        self.assertEqual(self.accepted()["status"], "accepted")
        self.transport.history_rows = [{"stck_bsop_date": "20261009", "stck_clpr": "69000"}]
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        with self.assertRaises(OrderRejected):
            self.accepted()

    def test_unknown_calendar_session_proof_expires_before_submit(self):
        self.transport.calendar = "unsupported"
        state = {"now": NOW}
        self.broker._clock = lambda: state["now"]
        self.assertEqual(self.broker.quote("005930")["market_status"], "OPEN")
        state["now"] = NOW + timedelta(seconds=91)
        with self.assertRaises(OrderRejected):
            self.broker.submit("005930", "buy", 3, 70010)
        self.assertEqual(self.order_requests(), [])

    def test_positive_calendar_also_requires_unexpired_trade_proof(self):
        state = {"now": NOW}
        self.broker._clock = lambda: state["now"]
        self.assertEqual(self.broker.quote("005930")["market_status"], "OPEN")
        self.assertTrue(self.broker._calendar_open)
        state["now"] = NOW + timedelta(seconds=91)
        with self.assertRaises(OrderRejected):
            self.broker.submit("005930", "buy", 3, 70010)
        self.assertEqual(self.order_requests(), [])

    def test_trade_proof_is_bound_to_symbol_and_limit_price(self):
        self.broker.quote("005930")
        with self.assertRaises(OrderRejected):
            self.broker.submit("000660", "buy", 3, 70010)
        with self.assertRaises(OrderRejected):
            self.broker.submit("005930", "buy", 3, 70000)
        self.assertEqual(self.order_requests(), [])

    def test_final_wire_time_gate_after_rate_wait(self):
        state = {"now": NOW.replace(hour=15, minute=19, second=59), "advance": False}
        self.broker._clock = lambda: state["now"]
        self.transport.trade_rows[0]["stck_cntg_hour"] = "151958"
        self.transport.minute_rows[0]["stck_cntg_hour"] = "151900"
        quote = self.broker.quote("005930")
        def pause(seconds):
            if state["advance"]:
                state["now"] = state["now"].replace(minute=20, second=0)
        self.broker._sleep = pause
        state["advance"] = True
        with self.assertRaises(OrderRejected):
            self.broker.submit("005930", "buy", 3, quote["price"])
        self.assertEqual(self.order_requests(), [])

    def test_explicit_holiday_and_weekend_never_open(self):
        self.transport.calendar = "N"
        self.assertEqual(self.broker.quote("005930")["market_status"], "CLOSED")
        with self.assertRaises(OrderRejected):
            self.accepted()
        weekend_transport = Transport()
        weekend_transport.history_rows = [{"stck_bsop_date": "20261009", "stck_clpr": "69000"}]
        weekend = KISBroker(CONFIG, opener=weekend_transport, clock=lambda: NOW - timedelta(days=1), sleeper=lambda seconds: None)
        self.assertEqual(weekend.quote("005930")["market_status"], "CLOSED")
        self.assertEqual(self.order_requests(), [])

    def test_quote_stale_trade_and_invalid_numeric_fail_closed(self):
        self.transport.trade_rows[0]["stck_cntg_hour"] = "095800"
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")
        for price in ("0", "NaN", "1e999", True, None):
            self.transport.quote_updates["stck_prpr"] = price
            with self.subTest(price=price), self.assertRaises(BrokerError):
                self.broker.quote("005930")

    def test_history_error_has_no_synthetic_fallback(self):
        self.transport.history_rows = []
        with self.assertRaises(BrokerError):
            self.broker.history("005930")
        with self.assertRaises(BrokerError):
            self.broker.quote("005930")

    def test_limit_submit_is_acceptance_not_fill(self):
        result = self.accepted()
        self.assertEqual(result["status"], "accepted")
        self.assertNotIn("filled_quantity", result)
        req, params = self.order_requests()[0]
        self.assertEqual(req.get_header("Tr_id"), "VTTC0012U")
        self.assertEqual((params["ORD_DVSN"], params["ORD_UNPR"], params["ORD_QTY"]), ("00", "70010", "3"))
        self.assertEqual(result["account_identity"], self.broker.account_identity)

    def test_order_validation_blocks_boolean_float_and_price(self):
        for quantity in (True, 1.0, 0, -1, "1"):
            with self.subTest(quantity=quantity), self.assertRaises(OrderRejected):
                self.broker.submit("005930", "buy", quantity, 70000)
        for price in (True, 1.0, 0, -1):
            with self.subTest(price=price), self.assertRaises(OrderRejected):
                self.broker.submit("005930", "buy", 1, price)
        self.assertEqual(self.order_requests(), [])

    def test_network_uncertainty_never_retries_or_leaks(self):
        self.transport.order_response = TimeoutError("offline-app-secret offline-token 12345678")
        with self.assertRaises(OrderUncertain) as caught:
            self.accepted()
        self.assertEqual(len(self.order_requests()), 1)
        for value in ("offline-app-secret", "offline-token", "12345678"):
            self.assertNotIn(value, str(caught.exception))

    def test_http_error_with_rt_code_is_uncertain(self):
        self.transport.order_response = Response({"rt_cd": "1", "msg1": "ambiguous gateway failure"}, status=503)
        with self.assertRaises(OrderUncertain):
            self.accepted()
        self.assertEqual(len(self.order_requests()), 1)

    def test_definite_rejection_and_incomplete_success(self):
        self.transport.order_response = Response({"rt_cd": "1", "msg1": "offline-app-secret"})
        with self.assertRaises(OrderRejected) as caught:
            self.accepted()
        self.assertNotIn("offline-app-secret", str(caught.exception))
        self.transport.order_response = Response({"rt_cd": "0", "output": {"ODNO": "123456"}})
        with self.assertRaises(OrderUncertain):
            self.accepted()

    def test_fills_pending_partial_full_canceled_and_rejected(self):
        order = self.accepted()
        cases = [({}, "pending", False, 0),
                 ({"tot_ccld_qty": "1", "rmn_qty": "2", "avg_prvs": "70000", "tot_ccld_amt": "70000"}, "partial", False, 1),
                 ({"tot_ccld_qty": "3", "rmn_qty": "0", "avg_prvs": "70000", "tot_ccld_amt": "210000"}, "filled", True, 3),
                 ({"tot_ccld_qty": "1", "rmn_qty": "0", "cnc_cfrm_qty": "2", "cncl_yn": "Y", "avg_prvs": "70000", "tot_ccld_amt": "70000"}, "canceled", True, 1),
                 ({"rmn_qty": "0", "rjct_qty": "3"}, "rejected", True, 0)]
        for changes, status, terminal, quantity in cases:
            self.transport.daily_rows = [fill_row(**changes)]
            result = self.broker.fills(order)
            self.assertEqual((result["status"], result["terminal"], result["filled_quantity"]), (status, terminal, quantity))

    def test_fills_missing_unknown_and_foreign_or_mismatch_rejected(self):
        order = self.accepted()
        self.assertIsNone(self.broker.fills(order)["filled_quantity"])
        for changes in ({"pdno": "000660"}, {"sll_buy_dvsn_cd": "01"}, {"ord_qty": "4", "rmn_qty": "4"},
                        {"cano": "87654321"}, {"ord_gno_brno": "99999"}, {"rmn_qty": "2"}):
            self.transport.daily_rows = [fill_row(**changes)]
            with self.subTest(changes=changes), self.assertRaises(BrokerError):
                self.broker.fills(order)
        with self.assertRaises(BrokerError):
            self.broker.fills({**order, "account_identity": "another-account"})
        with self.assertRaises(BrokerError):
            self.broker.fills({key: value for key, value in order.items() if key != "account_identity"})

    def test_open_orders_and_incomplete_fill_pagination(self):
        self.transport.daily_rows = [fill_row()]
        self.assertEqual(len(self.broker.account()["open_orders"]), 1)
        order = self.accepted()
        self.transport.daily_headers = {"tr_cont": "M"}
        with self.assertRaises(BrokerError):
            self.broker.fills(order)
        self.transport.daily_headers = {"unrelated": "header"}
        with self.assertRaises(BrokerError):
            self.broker.account()


if __name__ == "__main__":
    unittest.main()
