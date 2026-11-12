from dataclasses import replace
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace

m=runpy.run_path(str(Path(__file__).resolve().parents[1]/'optiver_bot/strategies/market_maker#10.py'))
Config,Snapshot,decide=(m[k] for k in ('Config','Snapshot','decide'))
flat=m['flat'];rt=flat.load_runtime()


class TargetRulesTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config(cost_ticks=0)
        self.snap=Snapshot('AAA',100.,100.1,.1,target_position=100)

    def test_build_hold_and_close_before_reverse(self):
        for pos,target,side,volume in ((0,100,'bid',5),(97,100,'bid',3),
                                      (100,100,None,0),(100,-100,'ask',5),
                                      (0,-100,'ask',5),(-100,-100,None,0)):
            orders=decide(replace(self.snap,position=pos,target_position=target),self.cfg)
            self.assertEqual([(o.side,o.volume) for o in orders],[] if side is None else [(side,volume)])

    def test_matching_pending_target_and_partial_fills(self):
        before=replace(self.snap,position=95,outstanding_buy=5)
        after=replace(self.snap,position=97,outstanding_buy=3)
        self.assertEqual(decide(before,self.cfg)[0].volume,5)
        self.assertEqual(decide(after,self.cfg)[0].volume,3)
        self.assertEqual(decide(replace(self.snap,position=100),self.cfg),[])

    def test_two_tick_spread_improves_only_entry_side(self):
        wide=replace(self.snap,ask=100.2)
        self.assertEqual(decide(wide,self.cfg)[0].price,100.1)
        short=replace(wide,target_position=-100)
        self.assertEqual(decide(short,self.cfg)[0].price,100.1)

    def test_costs_wide_stale_and_bad_books(self):
        self.assertEqual(decide(self.snap,Config()),[])
        self.assertEqual(decide(replace(self.snap,ask=100.4),self.cfg),[])
        for changes in ({'book_age_seconds':2},{'bid':float('nan')},{'ask':99.},
                        {'tick_size':0},{'bid':100.01}):
            self.assertEqual(decide(replace(self.snap,**changes),self.cfg),[])

    def test_held_inventory_can_reduce_without_known_costs(self):
        held=replace(self.snap,position=1,target_position=0)
        self.assertEqual([(o.side,o.volume) for o in decide(held,Config())],[('ask',1)])
        self.assertEqual(decide(replace(self.snap,reducing=True),self.cfg),[])

    def test_all_hard_limit_positions_and_pending_reservations(self):
        for pos in range(-100,101):
            for buys,sells in ((0,0),(2,7),(100,100)):
                for target in (-100,0,100):
                    snap=replace(self.snap,position=pos,outstanding_buy=buys,
                                 outstanding_sell=sells,target_position=target)
                    for o in decide(snap,self.cfg):
                        # Outputs are total target quantities. Matching pending
                        # volume is retained, not counted as another insertion.
                        if o.side=='bid':self.assertLessEqual(pos+max(buys,o.volume),100)
                        else:self.assertGreaterEqual(pos-max(sells,o.volume),-100)
                        if pos*(target-pos)<0:self.assertLessEqual(o.volume,abs(pos))

    def test_invalid_target_config_and_snapshot(self):
        for changes in ({'target':101},{'cap':101},{'target':0},{'size':0},
                        {'confirmations':True},{'max_gap':6}):
            with self.assertRaises(ValueError):replace(self.cfg,**changes)
        with self.assertRaises(ValueError):decide(replace(self.snap,target_position=101),self.cfg)

    def test_confirmed_fast_and_slow_signal_not_plot_colour(self):
        history=[(i,100.+.1*i) for i in range(21)]
        self.assertEqual(m['signal_direction'](history,20,self.cfg,.1),1)
        history=[(i,100.-.1*i) for i in range(21)]
        self.assertEqual(m['signal_direction'](history,20,self.cfg,.1),-1)
        history=[(i,100.) for i in range(21)]
        self.assertEqual(m['signal_direction'](history,20,self.cfg,.1),0)


class TargetExecutionTests(unittest.TestCase):
    def setUp(self):
        self.now=0.;self.cfg=Config(cost_ticks=0)
        self.fake=rt.make_fake(bids=[(100.,50)],asks=[(100.1,50)])
        self.target_raw=m['TargetRaw'](self.fake,clock=lambda:self.now)
        self.target_raw.targets['AAA']=100
        self.adapter=flat.guarded_adapter(self.target_raw,self.cfg,5,clock=lambda:self.now)

    def test_final_target_guard_stops_overshoot_and_stale_direction(self):
        self.fake.positions['AAA']=99
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='bid',price=100.,volume=5)
        self.fake.positions['AAA']=100
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='bid',price=100.,volume=1)
        self.assertEqual(self.fake.inserts(),[])

    def test_final_target_guard_reserves_pending_entries(self):
        self.fake.positions['AAA']=95
        self.fake.insert_order('AAA',side='bid',price=100.,volume=5)
        before=len(self.fake.inserts())
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='bid',price=100.,volume=1)
        self.assertEqual(len(self.fake.inserts()),before)

    def test_fill_during_target_recheck_and_session_deadline(self):
        self.fake.get_positions=Mock(side_effect=[{'AAA':95},{'AAA':100}])
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='bid',price=100.,volume=5)
        self.target_raw.deadline=0.
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='bid',price=100.,volume=1)

    def test_guard_permits_legal_100_and_restores_shared_runtime(self):
        previous=rt.LIMIT_GUARD
        self.fake.positions['AAA']=100
        instruments,_=rt.select_instruments(self.adapter.instruments(),None)
        with m['target_runtime_guard'](rt),rt.quiet():
            self.assertIsNone(rt.run_iteration(self.adapter,rt.LiveExecutor(self.adapter),instruments,
                                               self.cfg,rt.RunState(),lambda s,c:[]))
        self.assertEqual(rt.LIMIT_GUARD,previous)
        with self.assertRaises(RuntimeError):
            with m['target_runtime_guard'](rt):raise RuntimeError('test')
        self.assertEqual(rt.LIMIT_GUARD,previous)

    def test_matching_target_retained_without_duplicate(self):
        self.fake.positions['AAA']=95
        self.fake.insert_order('AAA',side='bid',price=100.,volume=5)
        instruments,_=rt.select_instruments(self.adapter.instruments(),None)
        def desired(s,c):return decide(Snapshot('AAA',100.,100.1,.1,95,5,0,0,100),c)
        state=rt.RunState()
        with m['target_runtime_guard'](rt),rt.quiet():
            rt.run_iteration(self.adapter,rt.LiveExecutor(self.adapter),instruments,self.cfg,state,desired)
        self.assertEqual(len(self.fake.inserts()),1)
        self.assertEqual(self.fake.deletes(),[])

    def test_reversal_closes_before_opposite_exposure(self):
        self.target_raw.targets['AAA']=-100
        self.fake.positions['AAA']=2
        with self.assertRaises(ValueError):self.target_raw.insert_order('AAA',side='ask',price=100.1,volume=5)
        self.assertTrue(self.adapter.insert('AAA','ask',100.1,2).success)

    def test_cancel_fill_race_never_opens_short_in_reduce_mode(self):
        self.target_raw.targets['AAA']=0
        self.target_raw.reduce_only['AAA']=True
        self.fake.positions['AAA']=0  # cancellation raced a complete exit fill
        with self.assertRaises(ValueError):
            self.target_raw.insert_order('AAA',side='ask',price=100.1,volume=5)
        self.assertEqual(self.fake.inserts(),[])

    def test_pending_opposite_side_blocks_new_order_even_if_net_is_small(self):
        self.fake.insert_order('AAA',side='ask',price=100.1,volume=5)
        with self.assertRaises(ValueError):
            self.target_raw.insert_order('AAA',side='bid',price=100.,volume=5)
        self.assertEqual(len(self.fake.inserts()),1)

    def test_deadline_checked_after_pacing_wait(self):
        inner=m['DeadlineRaw'](self.fake,1.,clock=lambda:self.now)
        budget=Mock()
        def wait():self.now=2.
        budget.acquire.side_effect=wait
        paced=flat.BudgetedRaw(inner,budget)
        with self.assertRaises(ValueError):
            paced.insert_order('AAA',side='bid',price=100.,volume=5)
        self.assertEqual(self.fake.inserts(),[])

    def test_renamed_runtime_news_guard_cancels_and_pauses(self):
        self.fake.insert_order('AAA',side='bid',price=100.,volume=5)
        self.fake.feed.append(SimpleNamespace(timestamp='t',post='synthetic news',meta_data=None))
        instruments,_=rt.select_instruments(self.adapter.instruments(),None)
        state=rt.RunState()
        with rt.quiet():
            result=rt.run_iteration(self.adapter,rt.LiveExecutor(self.adapter),instruments,
                                    self.cfg,state,lambda s,c:[])
        self.assertIsNone(result)
        self.assertEqual(state.posts_seen,1)
        self.assertGreater(state.pause_until,0)
        self.assertEqual(self.fake.orders['AAA'],{})

    def test_offline_demo_and_one_factory_default_dry(self):
        factory=Mock(return_value=self.fake)
        with rt.quiet():self.assertEqual(m['main'](['demo'],exchange_factory=factory),0)
        factory.assert_not_called()
        with tempfile.TemporaryDirectory() as folder,rt.quiet(),patch.object(rt,'run_bot',return_value=0) as run:
            self.assertEqual(m['main'](['run','--cost-ticks','0','--max-requests','100',
                                       '--max-order-size','10','--session-seconds','120',
                                       '--instruments','AAA',
                                       '--log-dir',folder],exchange_factory=factory),0)
        factory.assert_called_once_with();self.assertTrue(run.call_args.kwargs['dry_run'])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now=0.;self.cfg=Config(cost_ticks=0)
        self.adapter=SimpleNamespace(own={},received={})
        self.targets=SimpleNamespace(targets={},reduce_only={})
        self.controller=m['Controller'](self.adapter,self.targets,200.,clock=lambda:self.now)

    def step(self,t,bid,position=0,iid='AAA'):
        self.now=float(t);self.adapter.received[iid]=self.now
        return self.controller(SimpleNamespace(instrument_id=iid,bids=[(round(bid,1),50)],
            asks=[(round(bid+.1,1),50)],position=position,tick_size=.1),self.cfg)

    def warm(self,d=1):
        for t in range(21):orders=self.step(t,100.+d*.1*t)
        return orders

    def test_build_then_hold_target_without_continuous_trading(self):
        self.assertEqual(self.warm()[0].side,'bid')
        self.assertEqual(self.targets.targets['AAA'],100)
        self.assertEqual(self.step(21,102.1,95)[0].volume,5)
        self.assertEqual(self.step(22,102.2,100),[])
        self.assertEqual(self.targets.targets['AAA'],100)

    def test_short_from_zero_is_enabled(self):
        self.assertEqual(self.warm(-1)[0].side,'ask')
        self.assertEqual(self.targets.targets['AAA'],-100)

    def test_inherited_inventory_only_reduces(self):
        orders=self.step(0,100.,100)
        self.assertEqual(self.targets.targets['AAA'],0)
        self.assertEqual([(o.side,o.volume) for o in orders],[('ask',5)])

    def test_opposite_signal_exits_then_waits_for_flat(self):
        self.warm();self.step(21,102.1,100)
        with patch.dict(m['Controller'].__call__.__globals__,signal_direction=lambda *args:-1):
            self.assertEqual(self.step(22,102.,100)[0].side,'ask')
            self.assertEqual(self.targets.targets['AAA'],0)
            self.assertEqual(self.step(23,101.9,5)[0].volume,5)
            self.assertEqual(self.step(24,101.8,0),[])
            self.assertEqual(self.step(25,101.7,0),[])
            self.assertEqual(self.step(26,101.6,0)[0].side,'ask')
            self.assertEqual(self.targets.targets['AAA'],-100)

    def test_neutral_signal_grace_prevents_immediate_churn(self):
        self.warm();self.step(21,102.1,100)
        with patch.dict(m['Controller'].__call__.__globals__,signal_direction=lambda *args:0):
            for t in range(22,31):
                self.assertEqual(self.step(t,102.1,100),[])
                self.assertEqual(self.targets.targets['AAA'],100)
            self.assertEqual(self.step(31,102.1,100)[0].side,'ask')
            self.assertEqual(self.targets.targets['AAA'],0)

    def test_shock_and_feed_gap_reset_and_latch_exit(self):
        for t,bid in ((22,101.5),(25,102.3)):
            self.setUp();self.warm();self.step(21,102.1,100)
            self.assertEqual(self.step(t,bid,100)[0].side,'ask')
            self.assertEqual(self.targets.targets['AAA'],0)
            self.assertEqual(len(self.controller.state['AAA']['history']),1)

    def test_session_cutoff_and_winddown(self):
        self.controller.deadline=50.
        self.assertEqual(self.warm(),[])  # 30-second entry cutoff at t=20
        self.setUp();self.warm();self.step(21,102.1,100)
        self.controller.deadline=37.
        self.assertEqual(self.step(22,102.2,100)[0].side,'ask')
        self.assertEqual(self.targets.targets['AAA'],0)

    def test_stocks_have_independent_targets(self):
        self.warm()
        for t in range(21):self.step(t,100.-.1*t,iid='BBB')
        self.assertEqual(self.targets.targets,{'AAA':100,'BBB':-100})


if __name__=='__main__':unittest.main()
