import unittest
from dataclasses import replace
from pathlib import Path
import runpy
import tempfile
from unittest.mock import Mock

m = runpy.run_path(str(Path(__file__).resolve().parents[1]
                       / 'optiver_bot/strategies/market_maker#5.py'))
Config, Snapshot, decide = (m[k] for k in ('Config', 'Snapshot', 'decide'))
rt = m['load_runtime']()


class FlatMakerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(cost_ticks=0)
        self.snap = Snapshot('AAA', 100.0, 100.1, 0.1)

    def test_flat_market_quotes_both_touches_without_history(self):
        self.assertEqual([(o.side, o.price, o.volume) for o in decide(self.snap, self.cfg)],
                         [('bid', 100.0, 3), ('ask', 100.1, 3)])
        self.assertEqual(decide(self.snap, self.cfg), decide(self.snap, self.cfg))

    def test_short_selling_and_inventory_taper(self):
        self.assertIn('ask', [o.side for o in decide(self.snap, self.cfg)])
        orders = decide(replace(self.snap, position=10), self.cfg)
        self.assertEqual([(o.side, o.price, o.volume) for o in orders],
                         [('bid', 99.9, 1), ('ask', 100.1, 3)])
        self.assertEqual([o.side for o in decide(replace(self.snap, position=16), self.cfg)], ['ask'])
        self.assertEqual([o.side for o in decide(replace(self.snap, position=-16), self.cfg)], ['bid'])

    def test_cost_gate_and_reducing_orders_do_not_reverse(self):
        for cfg in (Config(), replace(self.cfg, cost_ticks=1)):
            self.assertEqual(decide(self.snap, cfg), [])
            orders = decide(replace(self.snap, position=1), cfg)
            self.assertEqual([(o.side, o.volume) for o in orders], [('ask', 1)])
            self.assertEqual(decide(replace(self.snap, position=1, outstanding_sell=1), cfg), [])

    def test_stale_bad_and_crossed_books(self):
        for changes in ({'bid': float('nan')}, {'ask': 100}, {'tick_size': 0},
                        {'bid': 100.01}, {'book_age_seconds': 1}, {'book_age_seconds': -1}):
            self.assertEqual(decide(replace(self.snap, **changes), self.cfg), [])

    def test_independent_caps_for_every_position_and_pending_orders(self):
        for cap in (20, 100):
            for pos in range(-100, 101):
                for buys, sells in ((0, 0), (3, 7), (100, 100)):
                    snap = replace(self.snap, position=pos,
                                   outstanding_buy=buys, outstanding_sell=sells)
                    for o in decide(snap, replace(self.cfg, cap=cap)):
                        if o.side == 'bid':
                            self.assertLessEqual(pos+buys+o.volume, cap)
                            self.assertLess(o.price, snap.ask)
                        else:
                            self.assertGreaterEqual(pos-sells-o.volume, -cap)
                            self.assertGreater(o.price, snap.bid)

    def test_bad_config_and_inventory_fail_loudly(self):
        for changes in ({'cap': 101}, {'cap': True}, {'size': 0},
                        {'cost_ticks': -1}, {'soft_fraction': 2}):
            with self.assertRaises(ValueError):
                replace(self.cfg, **changes)
        with self.assertRaises(ValueError):
            decide(replace(self.snap, position=-101), self.cfg)


class FlatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(cost_ticks=0)
        self.fake = rt.make_fake(bids=[(100.0, 50)], asks=[(100.1, 50)])
        self.adapter = m['guarded_adapter'](self.fake, self.cfg, 3)
        self.controller = m['Controller'](self.adapter)
        self.instruments, _ = rt.select_instruments(self.adapter.instruments(), None)
        self.executor = rt.LiveExecutor(self.adapter)  # FakeExchange only
        self.state = rt.RunState()

    def step(self, dry=False):
        with rt.quiet():
            return rt.run_iteration(self.adapter, self.executor, self.instruments,
                                    self.cfg, self.state, self.controller, dry_run=dry)

    def test_matching_quotes_keep_queue_position_no_duplicate_or_replace(self):
        self.assertIsNone(self.step())
        self.assertEqual(len(self.fake.inserts()), 2)
        for _ in range(3): self.assertIsNone(self.step())
        self.assertEqual(len(self.fake.inserts()), 2)
        self.assertEqual(len(self.fake.deletes()), 0)

    def test_partial_fill_and_inventory_reduction(self):
        self.step()
        bid_id = next(oid for oid, o in self.fake.orders['AAA'].items() if o.side == 'bid')
        self.fake.orders['AAA'][bid_id].volume = 2
        self.fake.positions['AAA'] = 1
        self.step()
        self.assertIn(bid_id, self.fake.orders['AAA'])
        self.assertEqual(len(self.fake.inserts()), 2)

    def test_fresh_capacity_blocks_pending_buys_despite_opposite_orders(self):
        self.fake.positions['AAA'] = 19
        self.fake.insert_order('AAA', price=99.9, volume=1, side='bid')
        self.fake.insert_order('AAA', price=100.2, volume=10, side='ask')
        before = len(self.fake.inserts())
        result = self.adapter.insert('AAA', 'bid', 100.0, 1)
        self.assertFalse(result.success)
        self.assertEqual(len(self.fake.inserts()), before)

    def test_cancel_fill_race_rechecks_position_and_remaining_orders(self):
        # A cancellation raced with a fill and the position is already +20.
        self.fake.positions['AAA'] = 20
        self.assertFalse(self.adapter.insert('AAA', 'bid', 100, 1).success)
        # A fill during the risk read also causes an abstention, not a retry loop.
        self.adapter.positions = Mock(side_effect=[{'AAA': 0}, {'AAA': 3}])
        self.assertFalse(self.adapter.insert('AAA', 'bid', 100, 1).success)

    def test_refreshed_book_prevents_crossing_and_order_size_is_enforced(self):
        self.assertFalse(self.adapter.insert('AAA', 'bid', 100.1, 1).success)
        self.assertFalse(self.adapter.insert('AAA', 'ask', 100.1, 4).success)
        self.assertEqual(self.fake.inserts(), [])

    def test_shock_withdraws_exposure_and_reduction_cannot_reverse(self):
        now = [0.0]
        self.adapter.received['AAA'] = 0
        controller = m['Controller'](self.adapter, clock=lambda: now[0])
        snap = rt.Snapshot('AAA', [(100.0, 10)], [(100.1, 10)], 0, 0.1)
        self.assertEqual(len(controller(snap, self.cfg)), 2)
        now[0] = 0.1
        self.adapter.received['AAA'] = 0.1
        moved = rt.Snapshot('AAA', [(99.0, 10)], [(99.1, 10)], 1, 0.1)
        orders = controller(moved, self.cfg)
        self.assertEqual([(o.side, o.volume) for o in orders], [('ask', 1)])
        flat = rt.Snapshot('AAA', [(99.0, 10)], [(99.1, 10)], 0, 0.1)
        self.assertEqual(controller(flat, self.cfg), [])
        now[0] = 5.2
        self.adapter.received['AAA'] = 5.2
        self.assertEqual(len(controller(flat, self.cfg)), 2)

    def test_dry_runner_uses_no_exchange_order_mutations(self):
        with tempfile.TemporaryDirectory() as folder, rt.quiet():
            code = rt.run_bot(self.adapter, self.instruments, self.cfg, dry_run=True,
                              sleep_s=0.5, log_path=Path(folder)/'dry.csv',
                              decide_fn=self.controller, max_iterations=2, sleep_fn=lambda _: None)
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.calls, [])

    def test_offline_demo_never_calls_a_factory(self):
        factory = Mock(side_effect=AssertionError('must not connect'))
        with rt.quiet():
            self.assertEqual(m['main'](['demo'], exchange_factory=factory), 0)
        factory.assert_not_called()


class BudgetTests(unittest.TestCase):
    def test_rolling_window_counts_reads_and_writes(self):
        now = [0.0]
        def sleep(delta): now[0] += delta
        budget = m['RequestBudget'](2, clock=lambda: now[0], sleep=sleep)
        raw = Mock()
        wrapped = m['BudgetedRaw'](raw, budget)
        wrapped.get_positions()
        wrapped.insert_order('AAA')
        wrapped.get_positions()
        self.assertGreaterEqual(now[0], 1.0)
        self.assertEqual(raw.get_positions.call_count, 2)
        self.assertEqual(raw.insert_order.call_count, 1)


if __name__ == '__main__':
    unittest.main()
