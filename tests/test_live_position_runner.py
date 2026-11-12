import contextlib
from dataclasses import asdict
from decimal import Decimal
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from optiver_bot.live_position_runner import (
    ApiError, Limiter, LiveAdapter, age_seconds, cloud_preflight, main, strategy, error_location, json_default,
)
from optiver_bot.models import DesiredOrder


class Fake:
    def __init__(self):
        self.now = 0.
        self.pos = {'A': 0}
        self.live = {}
        self.trades = []
        self.calls = []
        self.fill = False
        self.n = 0
        self.jitter = False
        self.pnl_calls = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def connect(self):
        self.calls.append(('connect',))

    def disconnect(self):
        self.calls.append(('disconnect',))

    def is_connected(self):
        return True

    def get_instruments(self):
        return {'A': NS(tick_size=.1, instrument_type='STOCK')}

    def get_positions(self):
        return dict(self.pos)

    def get_pnl(self):
        self.pnl_calls += 1
        if self.jitter and self.pnl_calls % 2:
            self.now += .2
        return 0.

    def get_outstanding_orders(self, i):
        return dict(self.live)

    def poll_new_trades(self, i):
        trades, self.trades = self.trades, []
        return trades

    def poll_new_social_media_feeds(self):
        return []

    def get_last_price_book(self, i):
        bid = round(100+self.now*.1, 1)
        return NS(bids=[NS(price=bid, volume=100)], asks=[NS(price=round(bid+.1, 1), volume=100)],
                  timestamp=datetime.fromtimestamp(1000000+self.now, timezone.utc))

    def insert_order(self, i, *, price, volume, side, order_type):
        self.n += 1
        self.calls.append(('insert', side, price, volume, order_type))
        if self.fill or order_type == 'ioc':
            self.pos[i] += volume*(1 if side == 'bid' else -1)
            self.trades.append(NS(price=price, volume=volume, side=side))
        else:
            self.live[self.n] = NS(price=price, volume=volume, side=side)
        return NS(success=True, order_id=self.n)

    def delete_order(self, i, order_id):
        self.calls.append(('cancel', order_id))
        self.live.pop(order_id, None)
        return NS(success=True)


def config(**kw):
    return strategy.Config(round_trip_cost_ticks=.1, gross_limit=10000,
                           position_cash_risk=10, loss_limit=50, **kw)


def adapter(fake, cfg=None):
    return LiveAdapter(fake, cfg or config(), ('A',),
        Limiter(1000, clock=fake.clock, sleep=fake.sleep), 1,
        fake.clock, lambda: 1000000+fake.now)


class AdapterTests(unittest.TestCase):
    def test_decimal_tick_is_normalized_before_signals_and_logging(self):
        f = Fake()
        f.get_instruments = lambda: {'A': NS(tick_size=Decimal('0.1'), instrument_type='STOCK')}
        a = adapter(f)
        account = a.read('A')
        self.assertIs(type(account.book.tick_size), float)
        self.assertTrue(strategy.valid_book(account.book, config()))

    def test_telemetry_handles_decimal_and_sdk_boolean_scalars(self):
        class SDKBoolean:
            def item(self):
                return True
        encoded = json.dumps({'tick': Decimal('0.1'), 'exit': SDKBoolean()}, default=json_default)
        self.assertEqual(json.loads(encoded), {'tick': .1, 'exit': True})
        with self.assertRaises(TypeError):
            json.dumps({'unknown': object()}, default=json_default)

    def test_book_timestamps_and_actual_fills(self):
        f = Fake()
        a = adapter(f)
        f.fill = True
        a.read('A')
        a.insert(DesiredOrder('A', 'bid', 100, 1), 't')
        account = a.read('A')
        self.assertTrue(account.certain)
        self.assertEqual(account.book.position, 1)
        self.assertEqual(a.cost_basis['A'], 100)
        self.assertIn('t', account.resolved)
        self.assertEqual(a.envelope('A'), (1, 1))

    def test_cancellation_keeps_conservative_fill_envelope(self):
        f = Fake()
        a = adapter(f)
        a.read('A')
        a.insert(DesiredOrder('A', 'bid', 100, 1), 't')
        account = a.read('A')
        a.cancel('A', account.orders[0].order_id)
        account = a.read('A')
        self.assertFalse(account.orders)
        self.assertEqual(a.envelope('A'), (0, 1))

    def test_repeated_cancels_cannot_bypass_internal_or_hard_limit(self):
        f = Fake()
        a = adapter(f)
        for n in range(10):
            a.read('A')
            a.insert(DesiredOrder('A', 'bid', 100, 1), str(n))
            account = a.read('A')
            a.cancel('A', account.orders[0].order_id)
            a.read('A')
        with self.assertRaises(ApiError):
            a.insert(DesiredOrder('A', 'bid', 100, 1), 'excess')
        self.assertEqual(len([c for c in f.calls if c[0] == 'insert']), 10)

    def test_stale_book_never_inserts(self):
        f = Fake()
        a = adapter(f)
        a.wall = lambda: 1000005
        with self.assertRaises(ApiError):
            a.insert(DesiredOrder('A', 'bid', 100, 1), 'x')
        self.assertFalse(f.calls)

    def test_unknown_response_keeps_backstop_reservation(self):
        f = Fake()
        a = adapter(f)
        def unknown(*args, **kwargs):
            raise TimeoutError()
        f.insert_order = unknown
        with self.assertRaises(TimeoutError):
            a.insert(DesiredOrder('A', 'bid', 100, 1), 'x')
        self.assertEqual(a.envelope('A'), (0, 1))
        self.assertNotIn('x', a.resolved)

    def test_existing_orders_and_unselected_inventory_refuse_takeover(self):
        for mode in ('orders', 'inventory'):
            f = Fake()
            if mode == 'orders':
                f.live[1] = NS(side='bid', price=100, volume=1)
            else:
                f.pos['OTHER'] = 1
            with self.assertRaises(ApiError):
                adapter(f)

    def test_selected_inventory_refuses_live_startup_without_orders(self):
        f = Fake()
        f.pos['A'] = 3
        with self.assertRaises(ApiError):
            adapter(f)
        self.assertFalse(any(c[0] in ('insert', 'cancel') for c in f.calls))

    def test_shadow_can_observe_inherited_inventory_without_mutating_it(self):
        f = Fake()
        f.pos['A'] = 3
        a = LiveAdapter(f, config(), ('A',), Limiter(1000, clock=f.clock, sleep=f.sleep),
                        1, f.clock, lambda: 1000000+f.now, require_flat=False)
        self.assertEqual(a.initial['A'], 3)
        self.assertEqual(f.pos['A'], 3)
        self.assertFalse(f.calls)

    def test_limiter_counts_actual_calls(self):
        f = Fake()
        limit = Limiter(2, clock=f.clock, sleep=f.sleep)
        for _ in range(3):
            limit.call(f.get_pnl)
        self.assertGreaterEqual(f.now, 1.)

    def test_bad_timestamps_fail_closed(self):
        for ts in (None, 1000001):
            with self.assertRaises(ApiError):
                age_seconds(ts, 1000000)

    def test_reference_preflight_reads_without_exposing_contents(self):
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d:
            root = Path(d)
            with self.assertRaises(ValueError):
                cloud_preflight(root)
            for name in ('AGENTS.md', 'api_reference.md'):
                (root/name).write_text('private reference text')
            result = cloud_preflight(root)
            self.assertEqual(len(result['AGENTS.md']), 64)
            self.assertNotIn('private', str(result))


class RunnerTests(unittest.TestCase):
    def test_readiness_diagnostic_reads_older_logs_and_partial_trailing_line(self):
        from scripts.diagnose_strategy6 import diagnose
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d:
            path = Path(d)/'recording.jsonl'
            header = dict(kind='settings', config=asdict(config()), instruments=['A'], duration=1200)
            rows = [dict(kind='frame', time=t, equity=0, account_ok=True, feed_ok=True,
                         books={'A': dict(bid=100, ask=100.1, tick_size=.1)}) for t in range(40)]
            path.write_text('\n'.join(json.dumps(r) for r in [header]+rows)+'\n{"kind":')
            report = diagnose(path)
        self.assertEqual(report['seconds'], 39)
        self.assertEqual(report['stocks']['A']['price_changes'], 0)
        self.assertIn('price_changes', report['stocks']['A']['blockers'])

    def test_flatten_large_clips_never_reverse_and_cap_at_depth(self):
        for start, expected_side in ((43, 'ask'), (-43, 'bid')):
            f = Fake()
            f.pos['A'] = start
            with tempfile.TemporaryDirectory(dir='results/test-temp') as d, contextlib.redirect_stdout(io.StringIO()):
                code = main(['flatten', '--live', '--instruments', 'A', '--max-requests', '1000',
                             '--clip', '20', '--log-dir', d], exchange_factory=lambda: f,
                            clock=f.clock, sleep=f.sleep, wall=lambda: 1000000+f.now,
                            preflight=lambda: {}, max_frames=20)
            self.assertEqual(code, 0)
            self.assertEqual(f.pos['A'], 0)
            orders = [c for c in f.calls if c[0] == 'insert']
            self.assertEqual([c[3] for c in orders], [20, 20, 3])
            self.assertTrue(all(c[1] == expected_side for c in orders))

    def test_partial_ioc_does_not_trigger_blind_large_retry(self):
        f = Fake()
        f.pos['A'] = -43
        original = f.insert_order
        def partial(i, **kw):
            kw['volume'] = min(5, kw['volume'])
            return original(i, **kw)
        f.insert_order = partial
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d, contextlib.redirect_stdout(io.StringIO()):
            code = main(['flatten', '--live', '--instruments', 'A', '--max-requests', '1000',
                         '--clip', '20', '--log-dir', d], exchange_factory=lambda: f,
                        clock=f.clock, sleep=f.sleep, wall=lambda: 1000000+f.now,
                        preflight=lambda: {}, max_frames=20)
        self.assertEqual(code, 1)
        self.assertEqual(f.pos['A'], -38)
        self.assertEqual(len([c for c in f.calls if c[0] == 'insert']), 1)

    def test_flatten_live_confirms_each_lot_and_ends_flat(self):
        f = Fake()
        f.pos['A'] = -3
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d, contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['flatten', '--live', '--instruments', 'A', '--max-requests', '1000', '--log-dir', d],
                        exchange_factory=lambda: f, clock=f.clock, sleep=f.sleep,
                        wall=lambda: 1000000+f.now, preflight=lambda: {}, max_frames=20)
        self.assertEqual(code, 0)
        self.assertEqual(f.pos['A'], 0)
        orders = [c for c in f.calls if c[0] == 'insert']
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(c[1] == 'bid' and c[3] == 1 and c[4] == 'ioc' for c in orders))
        self.assertIn('FLAT CONFIRMED:', out.getvalue())

    def test_flatten_preview_never_changes_position(self):
        f = Fake()
        f.pos['A'] = 3
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d, contextlib.redirect_stdout(io.StringIO()):
            code = main(['flatten', '--instruments', 'A', '--max-requests', '1000', '--log-dir', d],
                        exchange_factory=lambda: f, clock=f.clock, sleep=f.sleep,
                        wall=lambda: 1000000+f.now, preflight=lambda: {}, max_frames=20)
        self.assertEqual(code, 0)
        self.assertEqual(f.pos['A'], 3)
        self.assertFalse(any(c[0] == 'insert' for c in f.calls))

    def test_flatten_no_fill_does_not_repeat_or_report_flat(self):
        f = Fake()
        f.pos['A'] = -3
        def unfilled(*a, **kw):
            f.calls.append(('unfilled',))
            return NS(success=True, order_id=123)
        f.insert_order = unfilled
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d, contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['flatten', '--live', '--instruments', 'A', '--max-requests', '1000', '--log-dir', d],
                        exchange_factory=lambda: f, clock=f.clock, sleep=f.sleep,
                        wall=lambda: 1000000+f.now, preflight=lambda: {}, max_frames=20)
        self.assertEqual(code, 1)
        self.assertEqual(f.calls.count(('unfilled',)), 1)
        self.assertNotIn('FLAT CONFIRMED:', out.getvalue())

    def run_fake(self, live, jitter=False, inherited=False):
        f = Fake()
        f.fill = True
        if inherited:
            f.pos['A'] = -3
        f.jitter = jitter
        with tempfile.TemporaryDirectory(dir='results/test-temp') as d:
            args = ['run', '--instruments', 'A', '--duration-seconds', '300',
                    '--cost-ticks', '.1', '--max-requests', '1000', '--max-order-size', '1',
                    '--gross-limit', '10000', '--cash-risk', '10', '--loss-limit', '50',
                    '--log-dir', d]
            if live:
                args.append('--live')
            if inherited:
                args.append('--manage-existing')
            factory_calls = []
            def factory():
                factory_calls.append(1)
                return f
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(args, exchange_factory=factory, clock=f.clock, sleep=f.sleep,
                            wall=lambda: 1000000+f.now, preflight=lambda: {}, max_frames=40)
            rows = [json.loads(line) for p in Path(d).glob('*.jsonl') for line in p.read_text().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(f.calls.count(('connect',)), 1)
        self.assertEqual(f.calls.count(('disconnect',)), 1)
        return f, rows

    def test_shadow_has_no_order_mutations(self):
        f, rows = self.run_fake(False)
        self.assertFalse(any(c[0] in ('insert', 'cancel') for c in f.calls))
        self.assertTrue(any(d['target'] for r in rows if r.get('kind') == 'frame' for d in r['decisions']))

    def test_shadow_survives_variable_api_latency(self):
        f, rows = self.run_fake(False, jitter=True)
        self.assertEqual(sum(r.get('kind') == 'frame' for r in rows), 40)
        self.assertFalse(any(c[0] in ('insert', 'cancel') for c in f.calls))

    def test_explicit_manage_existing_reduces_inherited_short(self):
        f, rows = self.run_fake(True, inherited=True)
        orders = [c for c in f.calls if c[0] == 'insert']
        self.assertTrue(orders)
        self.assertEqual(orders[0][1], 'bid')
        self.assertEqual(rows[0]['starting_positions']['A'], -3)

    def test_diagnostic_identifies_location_without_private_message(self):
        try:
            raise ValueError('injected-password-must-not-be-printed')
        except ValueError as exc:
            diagnostic = error_location(exc)
        self.assertIn('ValueError at test_live_position_runner.py:', diagnostic)
        self.assertNotIn('injected-password', diagnostic)

    def test_live_opens_target_and_tracks_actual_fill_basis(self):
        f, rows = self.run_fake(True)
        inserts = [c for c in f.calls if c[0] == 'insert']
        self.assertTrue(inserts)
        self.assertLessEqual(max(abs(r['books']['A']['position']) for r in rows if r.get('kind') == 'frame'), 10)
        self.assertTrue(any(r.get('entry_prices') for r in rows))

    def test_invalid_startup_never_constructs_exchange(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['run'], exchange_factory=lambda: self.fail('connected'))


if __name__ == '__main__':
    unittest.main()
