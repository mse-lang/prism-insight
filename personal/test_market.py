import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from personal.market import KST, Market, demo_quote, parse_naver_history, parse_naver_quote, technical_report


def payload():
    return {"datas": [{"itemCode": "005930", "stockName": "삼성전자", "closePriceRaw": "72000",
                       "compareToPreviousClosePriceRaw": "-1000", "accumulatedTradingVolumeRaw": "12345",
                       "localTradedAt": datetime.now(KST).isoformat(),
                       "tradeStopType": {"name": "TRADING"}, "marketStatus": "CLOSE"}]}


class MarketTests(unittest.TestCase):
    def test_public_quote_negative_difference_and_source_time(self):
        quote = parse_naver_quote(payload(), "005930")
        self.assertEqual(quote["previous_close"], 73000)
        self.assertEqual(quote["source"], "naver")
        self.assertEqual(quote["name"], "삼성전자")
        self.assertLess(quote["change_pct"], 0)
        self.assertTrue(quote["as_of"].endswith("+09:00"))

    def test_bad_quote_shapes_do_not_become_zero_prices(self):
        for field, value in [("closePriceRaw", ""), ("closePriceRaw", "NaN"),
                             ("closePriceRaw", 0), ("localTradedAt", None),
                             ("localTradedAt", "2020-01-01T10:00:00+09:00"),
                             ("tradeStopType", {"name": "HALTED"})]:
            with self.subTest(field=field, value=value):
                data = payload()
                data["datas"][0][field] = value
                with self.assertRaises(ValueError):
                    parse_naver_quote(data, "005930")
        for rows in ([], payload()["datas"] * 2, [{"itemCode": "000660"}]):
            with self.assertRaises(ValueError):
                parse_naver_quote({"datas": rows}, "005930")

    def test_daily_bars_sorted_and_duplicates_rejected(self):
        raw = "[['날짜','시가','고가','저가','종가'],['20261008',1,2,1,72000],['20261007',1,2,1,73000]]"
        history = parse_naver_history(raw)
        self.assertEqual(history[0], {"date": "2026-10-07", "close": 73000})
        with self.assertRaises(ValueError):
            parse_naver_history("[['date'],['20261008',1,2,1,72000],['20261008',1,2,1,73000]]")
        with self.assertRaises((ValueError, SyntaxError)):
            parse_naver_history("__import__('os').system('false')")
        with self.assertRaises(ValueError):
            parse_naver_history("[['date'],['20261008',1,2,1,'1e999']]")
        with self.assertRaises(ValueError):
            parse_naver_history("[['date'],['20261008',1,2,1,'1e-999']]")

    def test_open_market_stale_quote_rejected_closed_snapshot_allowed(self):
        data = payload()
        data['datas'][0]['localTradedAt'] = (datetime.now(KST) - timedelta(days=2)).isoformat()
        data['datas'][0]['marketStatus'] = 'OPEN'
        with self.assertRaises(ValueError):
            parse_naver_quote(data, '005930')
        data['datas'][0]['marketStatus'] = 'CLOSE'
        self.assertEqual(parse_naver_quote(data, '005930')['status'], 'ok')

    def test_public_failure_never_falls_back_to_synthetic(self):
        with patch("personal.market.read_url", side_effect=OSError("offline")):
            quote = Market("naver").quotes(["005930"])["005930"]
        self.assertEqual(quote["status"], "unavailable")
        self.assertIsNone(quote["price"])
        self.assertEqual(quote["source"], "naver")

    def test_demo_is_explicit_and_repeatable(self):
        self.assertEqual(demo_quote("005930"), demo_quote("005930"))
        quote = demo_quote("005930")
        self.assertEqual(quote["source"], "demo")
        report = technical_report(quote)
        self.assertEqual(report["kind"], "technical")
        self.assertIn("합성", report["body"])
        self.assertIn("AI 분석이나", report["body"])
        self.assertGreaterEqual(report["metrics"]["rsi14"], 0)
        self.assertLessEqual(report["metrics"]["rsi14"], 100)
        quote["history"] = []
        with self.assertRaises(ValueError):
            technical_report(quote)


if __name__ == "__main__":
    unittest.main()
