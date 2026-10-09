"""Offline decision fixtures and regression of the original daily signal."""
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from personal.strategies import KST, catalog, evaluate


AT = datetime(2026, 10, 9, 10, 0, tzinfo=KST)


def history(values, at=AT):
    dates, day = [], at.date() - timedelta(days=1)
    while len(dates) < len(values):
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day -= timedelta(days=1)
    return [{"date": day, "close": value} for day, value in zip(reversed(dates), values)]


PULLBACK = list(range(100, 151)) + [148, 146, 144, 146]
RECOVERY = list(range(100, 151)) + [148, 146, 138, 146]
CONSENSUS = list(range(100, 151)) + [148, 146, 138, 151]


class StrategyTests(unittest.TestCase):
    def test_catalog_names_requirements_and_copy(self):
        items = catalog()
        self.assertEqual([item["id"] for item in items], ["breakout20", "trend_pullback", "ma_recovery", "consensus"])
        self.assertEqual([item["min_bars"] for item in items], [21, 55, 55, 55])
        self.assertEqual(items[2]["name"], "평균선 회복")
        items[0]["name"] = "changed"
        self.assertEqual(catalog()[0]["name"], "20일 종가 돌파")

    def test_original_breakout_golden_fields_preserved(self):
        bars = history(list(range(100, 121)))
        result = evaluate(bars, AT, current_price=120)
        expected = {"signal_date": "2026-10-08", "close": 120.0, "breakout_level": 119.0,
                    "ma20": 110.5, "breakout": True, "exit": False, "bars": bars}
        self.assertEqual({key: result[key] for key in expected}, expected)
        self.assertTrue(result["entry"])
        self.assertTrue(result["current_entry"])
        self.assertEqual(result["entry_threshold"], 119.0)

    def test_original_exit_and_historical_entry_are_separate(self):
        result = evaluate(history(list(range(120, 99, -1))), AT, current_price=999)
        self.assertEqual(result["ma20"], 109.5)
        self.assertTrue(result["exit"])
        self.assertFalse(result["breakout"])
        self.assertFalse(result["entry"])
        self.assertFalse(result["current_entry"])

    def test_last_completed_close_must_break_previous_twenty(self):
        result = evaluate(history(list(range(100, 120)) + [119]), AT, current_price=200)
        self.assertFalse(result["breakout"])
        self.assertFalse(result["entry"])
        self.assertFalse(result["current_entry"])
        equal_current = evaluate(history(list(range(100, 121))), AT, current_price=119)
        self.assertTrue(equal_current["entry"])
        self.assertFalse(equal_current["current_entry"])

    def test_missing_current_price_never_passes_live_entry(self):
        result = evaluate(history(list(range(100, 121))), AT)
        self.assertTrue(result["entry"])
        self.assertFalse(result["current_entry"])
        self.assertTrue(result["risks"])
        self.assertIsNone(result["checks"][-1]["value"]["current_price"])

    def test_today_bar_is_excluded_without_signal_leak(self):
        bars = history(list(range(100, 121)))
        original = evaluate(bars, AT, current_price=120)
        with_today = evaluate(bars + [{"date": AT.date().isoformat(), "close": 1000000}], AT, current_price=120)
        for key in ("signal_date", "close", "breakout_level", "ma20", "breakout", "exit", "bars", "entry", "current_entry"):
            self.assertEqual(with_today[key], original[key])
        self.assertEqual(with_today["checks"][1]["value"]["excluded"], 1)

    def test_minimum_completed_bars_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "21"):
            evaluate(history(list(range(100, 120))), AT)
        for strategy in ("trend_pullback", "ma_recovery", "consensus"):
            with self.subTest(strategy=strategy), self.assertRaisesRegex(ValueError, "55"):
                evaluate(history(CONSENSUS[1:]), AT, strategy, current_price=151)
        self.assertEqual(evaluate(history(CONSENSUS), AT, "consensus", current_price=151)["available_bars"], 55)

    def test_pullback_confirms_rising_trend_support_and_rebound(self):
        result = evaluate(history(PULLBACK), AT, "trend_pullback", current_price=146)
        self.assertTrue(result["entry"])
        self.assertTrue(result["current_entry"])
        self.assertFalse(result["breakout"])
        self.assertFalse(result["exit"])
        self.assertEqual(result["metrics"], {"ma20": 143.2, "ma50": 128.98, "ma50_5_sessions_ago": 124.5})
        self.assertEqual(len(result["bars"]), 21)
        self.assertFalse(evaluate(history(PULLBACK), AT, "ma_recovery", current_price=146)["entry"])

    def test_pullback_counterexamples_not_two_falls_or_not_rebound(self):
        for tail in ([148, 149, 144, 146], [148, 146, 144, 144], [158, 156, 154, 156]):
            with self.subTest(tail=tail):
                result = evaluate(history(list(range(100, 151)) + tail), AT, "trend_pullback", current_price=tail[-1])
                self.assertFalse(result["entry"])

    def test_recovery_is_a_mean_cross_not_position_reentry(self):
        result = evaluate(history(RECOVERY), AT, "ma_recovery", current_price=146)
        self.assertTrue(result["entry"])
        self.assertTrue(result["current_entry"])
        self.assertEqual(result["name"], "평균선 회복")
        self.assertFalse(evaluate(history(RECOVERY), AT, "trend_pullback", current_price=146)["entry"])
        collapsed = evaluate(history(RECOVERY), AT, "ma_recovery", current_price=140)
        self.assertTrue(collapsed["entry"])
        self.assertFalse(collapsed["current_entry"])

    def test_mean_cross_in_downtrend_does_not_authorize_entry(self):
        values = list(range(200, 149, -1)) + [148, 146, 155, 165]
        result = evaluate(history(values), AT, "ma_recovery", current_price=165)
        self.assertFalse(result["entry"])
        self.assertLess(result["metrics"]["ma20"], result["metrics"]["ma50"])
        entry_checks = [check for check in result["checks"] if check["phase"] == "entry"]
        self.assertTrue(all(check["passed"] for check in entry_checks))

    def test_flat_or_nonrising_long_mean_does_not_pass_trend(self):
        flat = evaluate(history([100] * 55), AT, "ma_recovery", current_price=101)
        self.assertFalse(flat["entry"])
        self.assertFalse(flat["breakout"])
        values = [90] * 35 + [110] * 15 + [90, 80, 70, 100, 110]
        result = evaluate(history(values), AT, "ma_recovery", current_price=110)
        self.assertGreater(result["metrics"]["ma20"], result["metrics"]["ma50"])
        self.assertEqual(result["metrics"]["ma50"], result["metrics"]["ma50_5_sessions_ago"])
        self.assertFalse(result["entry"])

    def test_consensus_uses_completed_close_and_requires_two_votes_now(self):
        one = evaluate(history(RECOVERY), AT, "consensus", current_price=151)
        self.assertEqual(sum(vote["entry"] for vote in one["votes"]), 1)
        self.assertFalse(one["entry"])
        result = evaluate(history(CONSENSUS), AT, "consensus", current_price=151)
        self.assertTrue(result["breakout"])
        self.assertTrue(result["entry"])
        self.assertTrue(result["current_entry"])
        self.assertEqual(sum(vote["entry"] for vote in result["votes"]), 2)
        self.assertEqual(result["entry_threshold"], 150.0)
        collapsed = evaluate(history(CONSENSUS), AT, "consensus", current_price=150)
        self.assertTrue(collapsed["entry"])
        self.assertFalse(collapsed["current_entry"])
        self.assertEqual(sum(vote["current_entry"] for vote in collapsed["votes"]), 1)

    def test_new_entry_does_not_overwrite_original_breakout_field(self):
        pullback = evaluate(history(PULLBACK), AT, "trend_pullback", current_price=146)
        self.assertTrue(pullback["entry"])
        self.assertFalse(pullback["breakout"])

    def test_future_unsorted_duplicate_and_weekend_dates_are_rejected(self):
        bars = history(CONSENSUS)
        cases = [list(reversed(bars)), bars + [dict(bars[-1])],
                 bars + [{"date": "2026-10-12", "close": 152}],
                 bars[:-1] + [{"date": "2026-10-03", "close": 152}]]
        for invalid in cases:
            with self.subTest(invalid=invalid[-1]), self.assertRaises(ValueError):
                evaluate(invalid, AT, "consensus", synthetic=True, current_price=151)

    def test_stale_data_only_is_exempted_for_synthetic(self):
        old_at = AT - timedelta(days=8)
        bars = history(CONSENSUS, old_at)
        with self.assertRaises(ValueError):
            evaluate(bars, AT, "consensus", current_price=151)
        self.assertTrue(evaluate(bars, AT, "consensus", synthetic=True, current_price=151)["entry"])
        with self.assertRaises(ValueError):
            evaluate(bars, AT, "consensus", synthetic="true", current_price=151)

    def test_bad_prices_and_naive_time_cannot_be_used(self):
        for invalid in (None, True, "NaN", "Infinity", 0, -1, 10**16):
            with self.subTest(invalid=invalid):
                bars = history(CONSENSUS)
                bars[-1]["close"] = invalid
                with self.assertRaises(ValueError):
                    evaluate(bars, AT, "consensus", current_price=151)
        for current in (True, "NaN", 0, -1):
            with self.subTest(current=current), self.assertRaises(ValueError):
                evaluate(history(CONSENSUS), AT, "consensus", current_price=current)
        with self.assertRaises(ValueError):
            evaluate(history(CONSENSUS), AT.replace(tzinfo=None), "consensus")

    def test_scaling_decimal_prices_preserves_decisions_and_json_is_safe(self):
        for strategy in ("breakout20", "trend_pullback", "ma_recovery", "consensus"):
            original = evaluate(history(CONSENSUS), AT, strategy, current_price=151)
            factor = Decimal("1000.25")
            scaled = evaluate(history([Decimal(value) * factor for value in CONSENSUS]), AT, strategy,
                              current_price=Decimal(151) * factor)
            self.assertEqual((original["entry"], original["current_entry"], original["breakout"], original["exit"]),
                             (scaled["entry"], scaled["current_entry"], scaled["breakout"], scaled["exit"]))
            json.dumps(scaled, allow_nan=False)
            self.assertIn("완료된 종가", scaled["explanation"])

    def test_unknown_strategy_and_date_input(self):
        with self.assertRaises(ValueError):
            evaluate(history(CONSENSUS), AT, "unknown", current_price=151)
        self.assertEqual(evaluate(history(CONSENSUS), AT.date(), "consensus", current_price=151)["signal_date"], "2026-10-08")


if __name__ == "__main__":
    unittest.main()
