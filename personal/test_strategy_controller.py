import json
import unittest
from datetime import timedelta
from personal import test_autotrade as fixture
from personal.autotrade import validate_config
AT = fixture.AT


def history(values):
    days, day = [], AT.date() - timedelta(days=1)
    while len(days) < len(values):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return [{'date': day, 'close': value} for day, value in zip(reversed(days), values)]


class StrategyControllerTests(unittest.TestCase):
    setUp = fixture.AutomationTests.setUp
    tearDown = fixture.AutomationTests.tearDown
    run_cycle = fixture.AutomationTests.run_cycle
    def test_new_strategy_fills_only_paper_and_uses_original_exit(self):
        self.market.history = history(list(range(100,151)) + [148,146,138,151])
        self.market.price = 151
        self.auto.update_config({'strategy':'ma_recovery'})
        self.auto.check()
        self.assertEqual(self.auto.state()['preview'][0]['action'], 'buy')
        self.assertFalse(self.auto.state()['orders'])
        self.run_cycle()
        first = self.auto.state()['orders']
        self.assertEqual(len(first),1)
        with self.store.connection() as db:
            recorded = db.execute('SELECT policy,decision FROM auto_intents').fetchone()
        self.assertEqual(recorded['policy'], 'personal-ma_recovery-v1')
        self.assertEqual(json.loads(recorded['decision'])['signal']['strategy'], 'ma_recovery')
        self.run_cycle()
        self.assertEqual(len(self.auto.state()['orders']),1)
        self.auto.stop()
        self.auto.update_config({'strategy':'consensus'})
        self.market.price = 100
        self.market.history = []
        self.run_cycle()
        self.assertCountEqual([row['side'] for row in self.auto.state()['orders']], ['sell','buy'])
        self.assertFalse(self.auto._owned())

    def test_strategy_selection_never_promotes_broker_or_resets_limits(self):
        for mode in ('kis-paper','kis-live','toss-live'):
            with self.assertRaises(ValueError):
                validate_config({'mode':mode, 'strategy':'ma_recovery'})
        previous = self.auto.config()
        self.auto.update_config({'strategy':'trend_pullback'})
        current = self.auto.config()
        self.assertEqual({k:v for k,v in current.items() if k != 'strategy'}, {k:v for k,v in previous.items() if k != 'strategy'})
        self.assertFalse(self.auto.state()['running'])
        self.assertFalse(self.auto.state()['orders'])

    def test_legacy_config_gets_default_without_runtime_rewrite(self):
        with self.store.connection(write=True) as db:
            legacy = self.auto.config()
            legacy.pop('strategy')
            self.store.set_meta(db,'auto_config',json.dumps(legacy))
        self.assertEqual(self.auto.config()['strategy'],'breakout20')
        with self.store.connection() as db:
            self.assertNotIn('strategy',json.loads(self.store.meta(db,'auto_config')))


