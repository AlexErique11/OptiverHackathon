import unittest
from dataclasses import replace

from optiver_bot.models import Snapshot, DesiredOrder
from importlib import import_module

_strategy = import_module('optiver_bot.strategies.market_maker#6')
Config, PositionSnapshot, decide = _strategy.Config, _strategy.PositionSnapshot, _strategy.decide
from optiver_bot.live_position import Controller, News
from optiver_bot.live_position_execution import Account, Resting, Budget, Executor
from optiver_bot.live_position_replay import Markouts, local_output


def config(**kw):
    values = dict(round_trip_cost_ticks=0.1, gross_limit=10000,
                  position_cash_risk=10, loss_limit=50)
    values.update(kw)
    return Config(**values)


def book(t=0, pos=0, **kw):
    return Snapshot('A', round(100+t*0.1, 1), round(100.1+t*0.1, 1),
                    0.1, position=pos, **kw)


def warm(cfg=None, negative=False):
    engine = Controller(('A',), cfg or config(), 1200)
    for t in range(33):
        result = engine.update(t, {'A': book(-t if negative else t)}, equity=0, account_ok=True, feed_ok=True)
    return engine, result[0]


class SignalTests(unittest.TestCase):
    def test_normal_network_jitter_does_not_crash_analysis(self):
        e = Controller(('A',), config(), 1200)
        for n in range(40):
            now = n + (0.2 if n % 2 else 0)
            result = e.update(now, {'A': book(n)}, equity=0,
                              account_ok=True, feed_ok=True)
        self.assertTrue(result[0].features.ready)

    def test_duplicate_or_backward_time_still_fails(self):
        e = Controller(('A',), config(), 1200)
        e.update(1, {'A': book()}, equity=0, account_ok=True)
        for now in (1, 0):
            with self.assertRaises(ValueError):
                e.update(now, {'A': book()}, equity=0, account_ok=True)

    def test_long_and_short_after_warmup(self):
        for negative, sign in ((False, 1), (True, -1)):
            _, d = warm(negative=negative)
            self.assertEqual(d.reason, 'entry')
            self.assertEqual(d.target, sign*3)
            self.assertEqual(d.orders[0].side, 'ask' if negative else 'bid')

    def test_unknown_costs_never_open(self):
        _, d = warm(Config())
        self.assertEqual(d.target, 0)
        self.assertFalse(d.orders)

    def test_stale_and_gap_reset_history(self):
        e, _ = warm()
        d = e.update(35, {'A': book(35)}, equity=0, account_ok=True)[0]
        self.assertFalse(d.features.ready)
        d = e.update(36, {'A': book(36, book_age_seconds=5)}, equity=0, account_ok=True)[0]
        self.assertFalse(d.orders)

    def test_news_dedup_does_not_extend_pause(self):
        e, _ = warm()
        event = News('x', 33, ('A',), -1)
        d = e.update(33, {'A': book(33)}, equity=0, account_ok=True, news=(event,))[0]
        self.assertEqual(d.target, 0)
        e.update(34, {'A': book(34)}, equity=0, account_ok=True, news=(event,))
        self.assertEqual(e.news_until['A'], 53)

    def test_future_news_cannot_affect_present(self):
        e, _ = warm()
        e.update(33, {'A': book(33)}, equity=0, account_ok=True, news=(News('future', 40),))
        self.assertEqual(e.news_until['A'], 0)

    def test_loss_limit_latches(self):
        e, _ = warm()
        e.update(33, {'A': book(33)}, equity=-51, account_ok=True)
        d = e.update(34, {'A': book(34)}, equity=1, account_ok=True)[0]
        self.assertTrue(e.loss_halted)
        self.assertEqual(d.target, 0)

    def test_inherited_position_reduces_during_warmup(self):
        e = Controller(('A',), config(), 1200)
        d = e.update(0, {'A': book(pos=2)}, equity=0, account_ok=True)[0]
        self.assertEqual(d.target, 0)
        self.assertEqual(d.orders[0].side, 'ask')

    def test_stop_and_max_hold_exit(self):
        e, _ = warm()
        e.update(33, {'A': book(33, pos=1)}, equity=0, account_ok=True)
        d = e.update(34, {'A': book(10, pos=1)}, equity=0, account_ok=True)[0]
        self.assertEqual(d.orders[0].order_type, 'ioc')
        self.assertEqual(d.target, 0)

    def test_verified_entry_holds_then_expires(self):
        e, _ = warm()
        d = e.update(33, {'A': book(33, pos=1)}, equity=0, account_ok=True, feed_ok=True,
                     entry_prices={'A': 103.3})[0]
        self.assertEqual(d.reason, 'hold')
        for t in range(34, 94):
            d = e.update(t, {'A': book(t, pos=1)}, equity=0, account_ok=True, feed_ok=True)[0]
        self.assertEqual(d.target, 0)
        self.assertEqual(d.orders[0].order_type, 'ioc')

    def test_missing_cost_basis_is_reduction_only(self):
        e, _ = warm()
        d = e.update(33, {'A': book(33, pos=1)}, equity=0, account_ok=True)[0]
        self.assertEqual(d.target, 0)

    def test_deadline_forces_exit(self):
        e = Controller(('A',), config(), 100)
        d = e.update(50, {'A': book(pos=-2)}, equity=0, account_ok=True)[0]
        self.assertEqual(d.target, 0)
        self.assertEqual(d.orders[0].side, 'bid')
        self.assertEqual(d.orders[0].order_type, 'ioc')

    def test_global_allocation_one_stock(self):
        e = Controller(('B', 'A'), config(), 1200)
        for t in range(33):
            b = book(t)
            ds = e.update(t, {'A': b, 'B': replace(b, instrument_id='B')}, equity=0, account_ok=True, feed_ok=True)
        self.assertEqual(sum(bool(d.target) for d in ds), 1)
        self.assertEqual(ds[0].instrument, 'A')

    def test_bad_config(self):
        for kw in ({'cap': 101}, {'clip': 0}, {'warmup': 10}, {'breadth': float('nan')},
                   {'round_trip_cost_ticks': -1}):
            with self.assertRaises(ValueError):
                config(**kw)

    def test_raw_hard_fill_envelopes_all_positions(self):
        c = config(cap=100, target=3)
        for q in range(-100, 101):
            for buys, sells in ((0, 0), (1, 1), (10, 10)):
                s = PositionSnapshot('A', 100, 100.1, .1, q, buys, sells,
                                     target_position=3 if q <= 0 else -3, account_ok=True)
                for o in decide(s, c):
                    existing = buys if o.side == 'bid' else sells
                    total = existing + max(0, o.volume-existing)
                    self.assertLessEqual(q+total if o.side == 'bid' else -q+total, 100)


class FakeAdapter:
    def __init__(self, a):
        self.account = a
        self.calls = []
        self.fail = False

    def read(self, i):
        self.account = replace(self.account, revision=self.account.revision+1)
        return self.account

    def cancel(self, i, oid):
        self.calls.append(('cancel', oid))
        if self.fail:
            raise RuntimeError('unknown')

    def insert(self, o, token):
        self.calls.append(('insert', o, token))
        if self.fail:
            raise RuntimeError('unknown')


class ExecutionTests(unittest.TestCase):
    def setup_gate(self, position=0, orders=()):
        a = Account(0, book(pos=position, outstanding_buy=sum(r.order.volume for r in orders
                    if r.order.side == 'bid')), orders, certain=True, entry_allowed=True)
        fake = FakeAdapter(a)
        return fake, Executor(fake, config(), Budget(100, 1))

    def test_cancel_fill_race_rechecks_position(self):
        old = Resting('old', DesiredOrder('A', 'bid', 99.9, 1))
        fake, gate = self.setup_gate(2, (old,))
        desired = [DesiredOrder('A', 'bid', 100, 1)]
        self.assertEqual(gate.reconcile('A', desired, 3, 0), 'cancel_requested')
        self.assertEqual(gate.reconcile('A', desired, 3, 0), 'cancel_pending')
        fake.account = replace(fake.account, book=book(pos=3), orders=(), resolved=frozenset({'old'}))
        self.assertEqual(gate.reconcile('A', desired, 3, 0), 'no_order')
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_insert_is_reserved(self):
        fake, gate = self.setup_gate()
        fake.fail = True
        d = [DesiredOrder('A', 'bid', 100, 1)]
        self.assertEqual(gate.reconcile('A', d, 3, 0), 'insert_unknown')
        self.assertEqual(gate.reconcile('A', d, 3, 0), 'insert_pending')
        self.assertEqual(len(fake.calls), 1)

    def test_retained_order_never_duplicates(self):
        d = DesiredOrder('A', 'bid', 100, 1)
        fake, gate = self.setup_gate(0, (Resting('old', d),))
        for _ in range(3):
            self.assertEqual(gate.reconcile('A', [d], 3, 0), 'retained')
        self.assertFalse(fake.calls)

    def test_new_fill_does_not_reverse_exit(self):
        fake, gate = self.setup_gate(0)
        d = [DesiredOrder('A', 'ask', 100, 1, 'ioc')]
        self.assertEqual(gate.reconcile('A', d, 0, 0), 'no_order')
        self.assertFalse(fake.calls)

    def test_budget_reserves_for_risk(self):
        b = Budget(3, 1, reserve=1)
        self.assertTrue(b.take(0))
        self.assertTrue(b.take(0))
        self.assertFalse(b.take(0))
        self.assertTrue(b.take(0, urgent=True))
        self.assertFalse(b.take(0, urgent=True))
        self.assertTrue(b.take(1))

    def test_portfolio_permission_required(self):
        fake, gate = self.setup_gate()
        fake.account = replace(fake.account, entry_allowed=False)
        self.assertEqual(gate.reconcile('A', [DesiredOrder('A', 'bid', 100, 1)], 3, 0),
                         'no_order')

    def test_revoked_permission_cancels_retained_entry(self):
        d = DesiredOrder('A', 'bid', 100, 1)
        fake, gate = self.setup_gate(0, (Resting('old', d),))
        fake.account = replace(fake.account, entry_allowed=False)
        self.assertEqual(gate.reconcile('A', [d], 3, 0), 'cancel_requested')

    def test_refreshed_book_does_not_turn_entry_into_taker(self):
        fake, gate = self.setup_gate()
        fake.account = replace(fake.account, book=Snapshot('A', 99.9, 100, .1))
        self.assertEqual(gate.reconcile('A', [DesiredOrder('A', 'bid', 100, 1)], 3, 0),
                         'price_changed')


class DiagnosticsTests(unittest.TestCase):
    def test_markouts_are_causal_signed_and_deduplicated(self):
        m = Markouts()
        fill = dict(id='f', instrument='A', side='ask', price=100, volume=1)
        m.fill(fill, 10)
        m.fill(fill, 10)
        self.assertEqual(m.observe(10, {'A': 99}), [])
        self.assertEqual(m.observe(11, {'A': 99})[0]['signed_markout'], 1)
        self.assertEqual(m.observe(15, {'A': 101})[0]['signed_markout'], -1)
        self.assertEqual(m.observe(25, {'A': 100})[0]['signed_markout'], 0)
        self.assertFalse(m.pending)

    def test_replay_outputs_cannot_escape_ignored_directories(self):
        with self.assertRaises(ValueError):
            local_output('docs/should-not-exist.jsonl')


if __name__ == '__main__':
    unittest.main()
