import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from personal.engine import DeskStore


def quote(code='005930', price=1000):
    return {code: {'symbol': code, 'name': code, 'price': price, 'source': 'demo', 'status': 'ok'}}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'desk.sqlite'
        self.store = DeskStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def order(self, side, quantity, price=1000, key=None, code='005930'):
        return self.store.order({'symbol': code, 'side': side, 'quantity': quantity,
                                 'idempotency_key': key or f'{side}-{quantity}-{price}'}, quote(code, price))

    def test_weighted_cost_partial_sell_and_full_sell_reconcile(self):
        self.order('buy', 100)
        self.order('buy', 100, 1200)
        self.assertEqual(self.store.snapshot(quote(price=1200))['positions'][0]['cost_basis'], 220033)
        sale = self.order('sell', 50, 1300)
        self.assertEqual(sale['fee'], 10)
        self.assertEqual(sale['realized_pnl'], 9982)
        state = self.store.snapshot(quote(price=1300))
        self.assertEqual(state['positions'][0]['cost_basis'], 165025)
        self.assertEqual(state['equity'], state['initial_cash'] + state['realized_pnl'] + state['unrealized_pnl'])
        self.order('sell', 150, 1300)
        state = self.store.snapshot({})
        self.assertEqual(state['positions'], [])
        self.assertEqual(state['realized_pnl'], 39927)
        self.assertEqual(state['cash'], 10039927)

    def test_concurrent_duplicate_executes_once_and_reused_payload_rejected(self):
        payload = {'symbol': '005930', 'side': 'buy', 'quantity': 1, 'idempotency_key': 'parallel-001'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.store.order(payload, quote()), range(8)))
        self.assertEqual(len({row['id'] for row in rows}), 1)
        state = self.store.snapshot(quote())
        self.assertEqual(len(state['orders']), 1)
        self.assertEqual(state['cash'], 9998999)
        self.assertEqual(self.store.order(payload, {})['id'], rows[0]['id'])
        self.assertEqual(self.store.order({**payload, 'note': '재시도 메모'}, {})['note'], '')
        with self.assertRaises(ValueError):
            self.store.order({**payload, 'quantity': 2}, quote())

    def test_position_cap_accumulates_and_cash_includes_fees(self):
        self.store.update_settings({'max_position_pct': 1, 'fee_bps': 0})
        self.order('buy', 100)
        with self.assertRaises(ValueError):
            self.order('buy', 1, key='over-limit')
        self.assertEqual(len(self.store.snapshot(quote())['orders']), 1)
        with self.assertRaises(ValueError):
            self.order('sell', 101)
        with self.assertRaises(ValueError):
            self.order('buy', 100000)
        fresh = DeskStore(Path(self.tmp.name) / 'fees.sqlite')
        fresh.update_settings({'max_position_pct': 100})
        with self.assertRaises(ValueError):
            fresh.order({'symbol': '005930', 'side': 'buy', 'quantity': 1, 'idempotency_key': 'fee-budget'}, quote(price=10000000))

    def test_position_limit_uses_equity_after_buy_fee(self):
        with self.assertRaises(ValueError):
            self.order('buy', 3000)
        self.order('buy', 2999)
        state = self.store.snapshot(quote())
        self.assertLessEqual(state['positions'][0]['weight_pct'], 30)

    def test_snapshot_remains_consistent_if_order_commits_between_reads(self):
        peer = DeskStore(self.path)
        read_meta = self.store.meta
        committed = False
        def interleave(db, key):
            nonlocal committed
            value = read_meta(db, key)
            if key == 'cash' and not committed:
                committed = True
                peer.order({'symbol': '005930', 'side': 'buy', 'quantity': 1,
                            'idempotency_key': 'during-snapshot'}, quote())
            return value
        with patch.object(self.store, 'meta', side_effect=interleave):
            state = self.store.snapshot(quote())
        self.assertEqual(state['equity'], 10000000)
        self.assertEqual(state['positions'], [])
        self.assertEqual(state['orders'], [])
        self.assertEqual(self.store.snapshot(quote())['positions'][0]['quantity'], 1)

    def test_missing_holding_price_has_unknown_equity_and_blocks_buy(self):
        self.order('buy', 10)
        state = self.store.snapshot({})
        self.assertIsNone(state['equity'])
        self.assertIsNone(state['positions'][0]['weight_pct'])
        with self.assertRaises(ValueError):
            self.order('buy', 1, code='000660', key='missing-held')
        self.order('sell', 1, key='valid-sell')

    def test_price_and_quantity_validation_and_client_price_ignored(self):
        base = {'symbol': '005930', 'side': 'buy', 'quantity': 1, 'idempotency_key': 'input-001', 'price': 1}
        for value in (True, 1.0, 0, -1, '2'):
            with self.assertRaises(ValueError):
                self.store.order({**base, 'quantity': value}, quote())
        for price in (None, False, 0, -1, float('nan'), 1.0):
            with self.assertRaises(ValueError):
                self.store.order(base, quote(price=price))
        wrong = quote()
        wrong['005930']['source'] = 'naver'
        with self.assertRaises(ValueError):
            self.store.order(base, wrong)
        self.assertEqual(self.store.order(base, quote())['price'], 1000)

    def test_restart_preserves_account_watchlist_and_journal(self):
        self.order('buy', 1)
        self.store.change_watchlist('035720', 'remove')
        self.store.add_journal({'symbol': '005930', 'text': '가설을 기록합니다.'})
        self.store.update_settings({'display_name': 'MY DESK'})
        reopened = DeskStore(self.path)
        self.assertEqual(reopened.snapshot(quote())['cash'], 9998999)
        self.assertNotIn('035720', reopened.watchlist())
        self.assertEqual(reopened.settings()['display_name'], 'MY DESK')
        self.assertEqual(len(reopened.snapshot(quote())['journal']), 1)
        with self.assertRaises(ValueError):
            DeskStore(self.path, provider='naver')

    def test_nonfinite_and_boolean_settings_rejected(self):
        for field, value in [('fee_bps', float('inf')), ('fee_bps', True), ('max_position_pct', float('nan')), ('max_position_pct', 0)]:
            with self.assertRaises(ValueError):
                self.store.update_settings({field: value})
        self.assertEqual(self.store.settings()['fee_bps'], 1.5)


if __name__ == '__main__':
    unittest.main()
