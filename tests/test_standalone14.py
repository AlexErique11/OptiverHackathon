from collections import deque
from dataclasses import replace
from pathlib import Path
import contextlib
import io
import runpy
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'optiver_bot/strategies/market_maker_14_standalone.py'
m = runpy.run_path(str(PATH))


class Exchange:
    def __init__(self, position=0):
        self.pos = position
        self.orders = {}
        self.calls = []
        self.sequence = 0
    def is_connected(self): return True
    def get_instruments(self):
        return {'AAA': NS(tick_size=.1, instrument_type='STOCK')}
    def get_positions(self): return {'AAA': self.pos}
    def get_pnl(self): return 0.
    def poll_new_social_media_feeds(self): return []
    def get_last_price_book(self, iid):
        return NS(bids=[NS(price=100., volume=50)], asks=[NS(price=100.1, volume=50)])
    def get_outstanding_orders(self, iid): return dict(self.orders)
    def insert_order(self, iid, **kwargs):
        self.sequence += 1
        self.calls.append(('insert', kwargs))
        self.orders[self.sequence] = NS(**kwargs)
        return NS(success=True, order_id=self.sequence)
    def delete_order(self, iid, order_id):
        self.calls.append(('cancel', order_id))
        self.orders.pop(order_id, None)
        return NS(success=True)
    def delete_orders(self, iid):
        self.calls.append(('cancel_all', iid))
        self.orders.clear()
        return NS(success=True)


class StandaloneTests(unittest.TestCase):
    def test_file_alone_runs_without_project_or_optibook(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'bot.py'
            shutil.copyfile(PATH, target)
            result = subprocess.run([sys.executable, '-I', str(target), 'demo'], cwd=d,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('position=100 target=100 []', result.stdout)

    def test_hard_bounds_and_close_before_reverse(self):
        c = m['Config'](cost_ticks=0)
        for p in range(-100, 101):
            for target in (-100, 0, 100):
                s = m['Snapshot']('AAA', 100., 100.1, .1, p, target_position=target)
                for o in m['decide'](s, c):
                    self.assertLessEqual(p + (o.volume if o.side == 'bid' else 0), 100)
                    self.assertGreaterEqual(p - (o.volume if o.side == 'ask' else 0), -100)
                    if p * (target - p) < 0:
                        self.assertLessEqual(o.volume, abs(p))

    def test_runtime_dry_and_live_startup_use_one_client(self):
        for live in (False, True):
            ex = Exchange(position=2)
            real_runner = m['_runtime'].run_bot
            def short_run(*a, **kw):
                kw.update(max_iterations=2, sleep_fn=lambda _: None)
                return real_runner(*a, **kw)
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(m['_runtime'], 'run_bot', side_effect=short_run), \
                    patch.object(m['_f_RequestBudget'], 'acquire', return_value=None):
                args = ['run', '--instruments', 'AAA', '--log-dir', d]
                if live: args.append('--live')
                count = []
                def factory(): count.append(1); return ex
                result = m['main'](args, exchange_factory=factory)
                self.assertEqual(result, 0)
                self.assertEqual(count, [1])
            if live:
                inserts = [x for x in ex.calls if x[0] == 'insert']
                self.assertEqual(len(inserts), 1, ex.calls)
                self.assertEqual(inserts[0][1]['volume'], 2)
                self.assertEqual(inserts[0][1]['side'], 'ask')
            else:
                self.assertEqual(ex.calls, [])

    def test_final_guard_rejects_cancel_fill_race_and_overshoot(self):
        ex = Exchange(99)
        guard = m['TargetRaw'](ex)
        guard.targets['AAA'] = 100
        with self.assertRaises(ValueError):
            guard.insert_order('AAA', side='bid', price=100., volume=5)
        guard.targets['AAA'] = 0
        guard.reduce_only['AAA'] = True
        ex.pos = 0
        with self.assertRaises(ValueError):
            guard.insert_order('AAA', side='ask', price=100.1, volume=1)
        self.assertEqual(ex.calls, [])

    def test_jump_signal_and_expiry(self):
        e = m['HybridController'](NS(own={}, received={}), NS(targets={}, reduce_only={}), 1000.)
        c = m['Config'](cost_ticks=0)
        def signal(i, t, p): return e.direction(i, deque([(t, p)]), t, c, .1)
        signal('CRWV', 0, 100.); signal('MU', 0, 100.)
        signal('CRWV', .5, 100.); signal('MU', .5, 101.)
        self.assertEqual(signal('CRWV', .6, 100.), 1)
        self.assertEqual(signal('CRWV', 11, 100.), 0)

    def test_controller_runs_through_warmup_and_builds_integer_target(self):
        now = [0.]
        adapter = NS(own={}, received={'AAA': 0.})
        raw = NS(targets={}, reduce_only={})
        e = m['HybridController'](adapter, raw, 1000., clock=lambda: now[0], use_priors=False)
        c = m['Config'](cost_ticks=0)
        for t in range(23):
            now[0] = float(t); adapter.received['AAA'] = float(t)
            p = round(100. + .1*t, 1)
            orders = e(NS(instrument_id='AAA', bids=[(p, 50)], asks=[(round(p+.1, 1), 50)],
                          tick_size=.1, position=0), c)
        self.assertEqual(raw.targets['AAA'], 100)
        self.assertIs(type(raw.targets['AAA']), int)
        self.assertEqual(orders[0].volume, 5)


if __name__ == '__main__': unittest.main()
