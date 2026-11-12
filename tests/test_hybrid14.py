from collections import deque
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

m=runpy.run_path(str(Path(__file__).resolve().parents[1]/'optiver_bot/strategies/market_maker#14.py'))


class HybridTests(unittest.TestCase):
    def engine(self,priors=True):
        adapter=SimpleNamespace(own={},received={})
        raw=SimpleNamespace(targets={},reduce_only={})
        return m['HybridController'](adapter,raw,1000.,use_priors=priors)

    def test_jump_follows_only_a_not_yet_moved_product(self):
        c=m['Config'](cost_ticks=0);e=self.engine()
        def signal(i,t,mid):return e.direction(i,deque([(t,mid)]),t,c,.1)
        signal('CRWV',0,100.);signal('MU',0,100.)
        signal('CRWV',.5,100.);signal('MU',.5,101.)
        self.assertEqual(signal('CRWV',.6,100.),1)
        self.assertEqual(signal('CRWV',.7,100.5),0)  # caught up: do not chase
        self.assertEqual(signal('CRWV',11,100.),0)  # expired/stale

    def test_negative_jump_can_short_from_flat(self):
        c=m['Config'](cost_ticks=0);e=self.engine()
        e.direction('CRWV',deque([(0.,100.)]),0.,c,.1)
        e.direction('MU',deque([(0.,100.)]),0.,c,.1)
        e.direction('CRWV',deque([(.5,100.)]),.5,c,.1)
        e.direction('MU',deque([(.5,99.)]),.5,c,.1)
        self.assertEqual(e.direction('CRWV',deque([(.6,100.)]),.6,c,.1),-1)

    def test_no_priors_no_unconfirmed_links(self):
        e=self.engine(False);self.assertEqual(e.links,{})
        c=m['Config'](cost_ticks=0)
        for t in (0.,.5):
            e.direction('CRWV',deque([(t,100.)]),t,c,.1)
            e.direction('MU',deque([(t,100.+2*t)]),t,c,.1)
        self.assertEqual(e.jump_support,{})

    def test_hybrid_uses_shared_pure_order_contract_and_hard_bounds(self):
        c=m['Config'](cost_ticks=0)
        for pos,target in ((100,100),(-100,-100)):
            self.assertEqual(m['decide'](m['Snapshot']('X',100.,100.1,.1,pos,target_position=target),c),[])
        order=m['decide'](m['Snapshot']('X',100.,100.1,.1,target_position=-100),c)[0]
        self.assertEqual((order.instrument_id,order.side,order.volume),('X','ask',5))

    def test_cli_has_concrete_defaults_and_default_stays_dry(self):
        factory=Mock()
        with patch.object(m['target'],'main',return_value=0) as run:
            self.assertEqual(m['main'](['run'],exchange_factory=factory),0)
        args=run.call_args.args[0]
        self.assertNotIn('--live',args)
        self.assertEqual(args[args.index('--session-seconds')+1],'600')
        factory.assert_not_called()


if __name__=='__main__':unittest.main()
