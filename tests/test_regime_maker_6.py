"""Causal regime/exit tests, not a performance backtest."""
from dataclasses import replace
from pathlib import Path
import runpy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

m = runpy.run_path(str(Path(__file__).resolve().parents[1]
                       / 'optiver_bot/strategies/market_maker#6.py'))
Config, Snapshot, Point = (m[k] for k in ('Config', 'Snapshot', 'Point'))
decide, classify = m['decide'], m['classify']
flat = m['flat']
rt = flat.load_runtime()


def snapshot(mode='range', **kwargs):
    slope = {'range': 0, 'down': 0.05, 'up': -0.05}[mode]
    return replace(Snapshot('AAA', 100.0, 100.1, 0.1,
                            history=tuple(Point(round(100.05+slope*i, 4), i)
                                          for i in range(1,21))), **kwargs)


class DecisionTests(unittest.TestCase):
    def setUp(self): self.cfg = Config(cost_ticks=0)

    def test_range_quotes_both_and_trends_only_confirmed_side(self):
        for mode, sides in (('range',['bid','ask']), ('down',['ask']), ('up',['bid'])):
            snap = snapshot(mode)
            self.assertEqual(classify(snap, self.cfg)[0], mode)
            self.assertEqual([o.side for o in decide(snap, self.cfg)], sides)

    def test_needs_distinct_changes_not_duplicate_flat_polls(self):
        # A single four-tick move plus 19 repeated prices is not confirmation.
        snap = snapshot(history=tuple(Point(100.45 if i == 20 else 100.05, i)
                                      for i in range(1,21)))
        self.assertEqual(classify(snap, self.cfg)[0], 'uncertain')
        self.assertEqual(decide(snap, self.cfg), [])

    def test_warmup_duplicate_future_and_gapped_history(self):
        for history in ((), (Point(100.1,-1),),
                        tuple(Point(100.05, 20) for _ in range(20)),
                        tuple(Point(100.05,i) for i in (1,2,3,4,5,6,20))):
            self.assertEqual(decide(snapshot(history=history), self.cfg), [])

    def test_shock_freezes_entries_but_inventory_can_reduce(self):
        snap = snapshot(cooldown_remaining=4)
        self.assertEqual(decide(snap, self.cfg), [])
        held = replace(snap, position=1, entry_reference=100.05, held_seconds=1)
        self.assertEqual([(o.side,o.volume) for o in decide(held,self.cfg)], [('ask',1)])

    def test_invalid_market_cannot_generate_exits(self):
        held = snapshot(position=2, entry_reference=100.05, held_seconds=30)
        for changes in ({'bid': float('nan')}, {'ask': 99}, {'bid':100.01},
                        {'tick_size':0}, {'book_age_seconds':-1}, {'book_age_seconds':1}):
            self.assertEqual(decide(replace(held,**changes),self.cfg), [])

    def test_timeout_stop_unknown_state_and_directional_signal_loss(self):
        for held in (snapshot(position=2, held_seconds=21, entry_reference=100.05),
                     snapshot(position=2, held_seconds=1, entry_reference=100.5),
                     snapshot(position=-2),
                     snapshot(position=-2, held_seconds=1, entry_reference=100.05,entry_mode='down')):
            orders = decide(held,self.cfg)
            self.assertEqual(len(orders),1)
            self.assertEqual(orders[0].side,'ask' if held.position>0 else 'bid')
            self.assertLessEqual(orders[0].volume,abs(held.position))

    def test_existing_exit_is_a_target_not_an_additive_instruction(self):
        held = snapshot(position=2, outstanding_sell=2, held_seconds=21,entry_reference=100.05)
        self.assertEqual([(o.side,o.volume) for o in decide(held,self.cfg)], [('ask',2)])

    def test_costs_and_reentry_cooldown(self):
        self.assertEqual(decide(snapshot(),Config()), [])
        self.assertEqual(decide(snapshot('down',reentry_remaining=1),self.cfg), [])

    def test_all_positions_and_independent_pending_capacity(self):
        for cap in (10,100):
            cfg=replace(self.cfg,cap=cap)
            for pos in range(-100,101):
                for buys,sells in ((0,0),(2,7),(99,99)):
                    for mode in ('range','down','up'):
                        snap=snapshot(mode,position=pos,outstanding_buy=buys,outstanding_sell=sells,
                                      entry_reference=100.05,held_seconds=1)
                        for o in decide(snap,cfg):
                            if o.side=='bid': self.assertLessEqual(pos+buys+o.volume,cap)
                            else: self.assertGreaterEqual(pos-sells-o.volume,-cap)

    def test_reject_bad_config(self):
        for changes in ({'cap':101},{'size':0},{'lookback':0},{'max_gap':21},
                        {'range_ticks':3},{'confirmations':True},{'stop_ticks':float('nan')}):
            with self.assertRaises(ValueError): replace(self.cfg,**changes)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now=0.
        self.fake=rt.make_fake(bids=[(100,50)],asks=[(100.1,50)])
        self.reducing=m['ReducingRaw'](self.fake)
        self.cfg=Config(cost_ticks=0)
        self.adapter=flat.guarded_adapter(self.reducing,self.cfg,2,clock=lambda:self.now)
        self.controller=m['Controller'](self.adapter,self.reducing,clock=lambda:self.now)

    def observe(self,mid=100.05,pos=0):
        self.adapter.received['AAA']=self.now
        bid=round(mid-0.05,1);ask=round(mid+0.05,1)
        snap=rt.Snapshot('AAA',[(bid,50)],[(ask,50)],pos,0.1)
        return self.controller(snap,self.cfg)

    def warm(self):
        for i in range(21):
            self.now=float(i);out=self.observe()
        return out

    def test_causal_warmup_and_stock_history_isolation(self):
        self.assertEqual(self.observe(),[])
        self.assertEqual(len(self.warm()),2)
        self.assertEqual(len(self.controller.state['AAA']['history']),21)

    def test_shock_resets_reference_and_old_crash_cannot_become_a_trend(self):
        self.warm()
        self.now=21.;self.assertEqual(self.observe(99.05),[])
        self.assertEqual(len(self.controller.state['AAA']['history']),1)
        for i in range(22,41):
            self.now=float(i);self.assertEqual(self.observe(99.05),[])
        self.now=41.;self.assertEqual(len(self.observe(99.05)),2)
        self.assertEqual(self.controller.state['AAA']['last_mode'],'range')

    def test_timeout_stays_latched_until_confirmed_flat(self):
        self.warm();self.now=21.;self.observe(pos=1)
        opened=self.controller.state['AAA']['opened']
        self.now=22.;self.observe(pos=2)
        self.assertEqual(self.controller.state['AAA']['opened'],opened)
        for i in range(23,43):self.now=float(i);self.observe(pos=2)
        self.assertTrue(self.controller.state['AAA']['exit'])
        self.assertTrue(self.reducing.reduce_only['AAA'])
        self.now=43.;self.assertEqual(self.observe(pos=0),[])
        self.assertFalse(self.reducing.reduce_only['AAA'])
        self.now=45.;self.assertEqual(len(self.observe(pos=0)),2)

    def test_exit_guard_rejects_stale_exit_at_flat_and_pending_overclose(self):
        self.reducing.reduce_only['AAA']=True
        with self.assertRaises(ValueError):
            self.reducing.insert_order('AAA',side='ask',price=100.1,volume=1)
        self.fake.positions['AAA']=1
        self.fake.insert_order('AAA',side='ask',price=100.1,volume=1)
        before=len(self.fake.inserts())
        with self.assertRaises(ValueError):
            self.reducing.insert_order('AAA',side='ask',price=100.1,volume=1)
        self.assertEqual(len(self.fake.inserts()),before)

    def test_cancel_fill_race_must_not_turn_exit_into_new_short(self):
        self.reducing.reduce_only['AAA']=True
        self.fake.get_positions=Mock(side_effect=[{'AAA':1},{'AAA':0}])
        with self.assertRaises(ValueError):
            self.reducing.insert_order('AAA',side='ask',price=100.1,volume=1)
        self.assertEqual(self.fake.inserts(),[])

    def test_demo_is_offline(self):
        factory=Mock(side_effect=AssertionError('must not connect'))
        with rt.quiet(): self.assertEqual(m['main'](['demo'],exchange_factory=factory),0)
        factory.assert_not_called()

    def test_driver_calls_one_factory_and_defaults_to_dry_orders(self):
        factory=Mock(return_value=self.fake)
        with tempfile.TemporaryDirectory() as folder, rt.quiet(), patch.object(rt,'run_bot',return_value=0) as run:
            code=m['main'](['run','--cost-ticks','0','--max-requests','100',
                            '--max-order-size','10','--log-dir',folder],exchange_factory=factory)
        self.assertEqual(code,0)
        factory.assert_called_once_with()
        self.assertTrue(run.call_args.kwargs['dry_run'])
        self.assertEqual(self.fake.calls,[])

    def test_bad_limits_stop_before_connecting(self):
        factory=Mock(side_effect=AssertionError('must not connect'))
        with rt.quiet(),self.assertRaises(SystemExit):
            m['main'](['run','--cap','101','--cost-ticks','0','--max-requests','100',
                       '--max-order-size','10'],exchange_factory=factory)
        factory.assert_not_called()


if __name__=='__main__':unittest.main()
