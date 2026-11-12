#!/usr/bin/env python3
"""Inventory-light regime maker. Default command is an OFFLINE demo.

Reuses #5 risk/pacing and #1 execution, with exactly one connection when run.
No fits or profits are inferred from the supplied quote-only recording.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time


def load_flat():
    name = '_optiver_regime6_flat_runtime'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('market_maker#5.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


flat = load_flat()


@dataclass(frozen=True)
class Config(flat.Config):
    cap: int = 10
    size: int = 2
    lookback: float = 20.0
    min_samples: int = 8
    max_gap: float = 3.0
    trend_ticks: float = 3.0
    range_ticks: float = 2.0
    confirmations: int = 3
    stop_ticks: float = 3.0
    max_hold: float = 20.0
    reentry_delay: float = 2.0

    def __post_init__(self):
        super().__post_init__()
        for name in ('lookback', 'max_gap', 'trend_ticks', 'range_ticks',
                     'stop_ticks', 'max_hold', 'reentry_delay'):
            if not flat.finite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive and finite')
        if self.range_ticks >= self.trend_ticks or self.max_gap >= self.lookback:
            raise ValueError('invalid range/trend thresholds or sample gap')
        if any(type(v) is not int or v < 2 for v in (self.min_samples, self.confirmations)):
            raise ValueError('sample/confirmation counts must be integers >= 2')


@dataclass(frozen=True)
class Point:
    mid: float
    age: float


@dataclass(frozen=True)
class Snapshot(flat.Snapshot):
    history: tuple[Point, ...] = ()
    # A conservative observed market reference, NOT an actual entry-fill VWAP.
    entry_reference: float | None = None
    held_seconds: float | None = None
    cooldown_remaining: float = 0.0
    reentry_remaining: float = 0.0
    exit_latched: bool = False
    entry_mode: str | None = None


def classify(snapshot, cfg):
    """Return mode and explanation, using only past/current observations."""
    if (not isinstance(snapshot, Snapshot)
            or not all(flat.finite(v) for v in (snapshot.bid, snapshot.ask, snapshot.tick_size,
                                                snapshot.book_age_seconds))
            or snapshot.bid <= 0 or snapshot.ask <= snapshot.bid or snapshot.tick_size <= 0
            or not 0 <= snapshot.book_age_seconds <= cfg.max_age):
        return 'invalid', 'bad/stale book'
    if snapshot.cooldown_remaining > 0:
        return 'shock', 'shock cooldown'
    tick = Decimal(str(snapshot.tick_size))
    if Decimal(str(snapshot.bid)) % tick or Decimal(str(snapshot.ask)) % tick:
        return 'invalid', 'off-grid book'
    mid = (Decimal(str(snapshot.bid)) + Decimal(str(snapshot.ask)))/2
    points = [(Decimal(0), mid)]
    for p in snapshot.history:
        if not isinstance(p, Point) or not flat.finite(p.mid) or p.mid <= 0 or not flat.finite(p.age) or p.age <= 0:
            return 'invalid', 'invalid/future history'
        points.append((Decimal(str(p.age)), Decimal(str(p.mid))))
    points.sort(reverse=True)  # oldest -> newest
    if len(points) < cfg.min_samples or points[0][0] < Decimal(str(cfg.lookback)):
        return 'warmup', 'insufficient lookback'
    if points[0][0] > Decimal(str(cfg.lookback + cfg.max_gap)):
        return 'invalid', 'untrimmed/old history'
    gaps = [points[i][0]-points[i+1][0] for i in range(len(points)-1)]
    if any(gap <= 0 or gap > Decimal(str(cfg.max_gap)) for gap in gaps):
        return 'invalid', 'duplicate/gapped observations'
    moves = [(points[i+1][1]-points[i][1])/tick for i in range(len(points)-1)]
    if any(abs(move) >= Decimal(str(cfg.shock_ticks)) for move in moves):
        return 'shock', 'large move in history; reset reference'
    net = (points[-1][1]-points[0][1])/tick
    nonzero = [move for move in moves if move != 0]
    # Confirmation is by distinct price changes, not repeated polling of a flat price.
    recent = nonzero[-cfg.confirmations:]
    fast = [price for age, price in points if age <= Decimal(str(cfg.lookback/4))]
    fast_mean = sum(fast)/len(fast)
    if len(recent) == cfg.confirmations:
        if net <= -Decimal(str(cfg.trend_ticks)) and all(x < 0 for x in recent) and mid <= fast_mean:
            return 'down', 'confirmed downtrend'
        if net >= Decimal(str(cfg.trend_ticks)) and all(x > 0 for x in recent) and mid >= fast_mean:
            return 'up', 'confirmed uptrend'
    if abs(net) <= Decimal(str(cfg.range_ticks)):
        return 'range', 'small net drift'
    return 'uncertain', 'drift lacks confirmation'


def wants_exit(snapshot, cfg, mode):
    if not snapshot.position:
        return False
    if snapshot.exit_latched or mode in ('invalid', 'warmup', 'shock', 'uncertain'):
        return True
    if (not flat.finite(snapshot.entry_reference) or snapshot.entry_reference <= 0
            or not flat.finite(snapshot.held_seconds) or snapshot.held_seconds < 0):
        return True  # restart or unknown fill lifecycle: do not expand inventory
    if snapshot.held_seconds >= cfg.max_hold:
        return True
    if snapshot.entry_mode in ('up', 'down') and mode != snapshot.entry_mode:
        return True
    mid = (snapshot.bid + snapshot.ask)/2
    adverse = ((snapshot.entry_reference-mid) if snapshot.position > 0
               else (mid-snapshot.entry_reference))/snapshot.tick_size
    if adverse >= cfg.stop_ticks-1e-9:
        return True
    return (snapshot.position > 0 and mode == 'down') or (snapshot.position < 0 and mode == 'up')


def decide(snapshot, cfg):
    """Pure desired passive quotes; runner owns state and all order mutations."""
    buy_room, sell_room = flat.rooms(snapshot.position, snapshot.outstanding_buy,
                                      snapshot.outstanding_sell, cfg.cap)
    if (not isinstance(snapshot, Snapshot) or not isinstance(snapshot.instrument_id, str)
            or not snapshot.instrument_id.strip()
            or not all(flat.finite(v) for v in (snapshot.bid, snapshot.ask, snapshot.tick_size,
                                               snapshot.book_age_seconds))
            or snapshot.bid <= 0 or snapshot.ask <= snapshot.bid or snapshot.tick_size <= 0
            or not 0 <= snapshot.book_age_seconds <= cfg.max_age):
        return []
    tick = Decimal(str(snapshot.tick_size))
    if Decimal(str(snapshot.bid)) % tick or Decimal(str(snapshot.ask)) % tick:
        return []
    mode, _ = classify(snapshot, cfg)
    if (snapshot.position+snapshot.outstanding_buy > 100
            or snapshot.position-snapshot.outstanding_sell < -100):
        return []
    if wants_exit(snapshot, cfg, mode):
        side = 'ask' if snapshot.position > 0 else 'bid'
        room = sell_room if side == 'ask' else buy_room
        volume = min(cfg.size, abs(snapshot.position), room)
        price = snapshot.ask if side == 'ask' else snapshot.bid
        return [flat.DesiredOrder(side, price, volume)] if volume else []
    if mode not in ('range', 'down', 'up') or snapshot.reentry_remaining > 0:
        return []
    orders = flat.decide(snapshot, cfg)
    if mode == 'down':
        orders = [o for o in orders if o.side == 'ask']
    elif mode == 'up':
        orders = [o for o in orders if o.side == 'bid']
    return orders


class ReducingRaw:
    """Final exit guard on the SAME client, beneath #5's pre-insert checks."""
    def __init__(self, raw):
        self.raw, self.reduce_only = raw, {}

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def insert_order(self, iid, **kwargs):
        if self.reduce_only.get(iid, False):
            p1 = self.raw.get_positions().get(iid, 0)
            pending = self.raw.get_outstanding_orders(iid) or {}
            p2 = self.raw.get_positions().get(iid, 0)
            side, volume = kwargs['side'], kwargs['volume']
            expected = 'ask' if p2 > 0 else 'bid'
            rt = flat.load_runtime()
            if not p2 or p1 != p2 or side != expected:
                raise ValueError('exit state changed; retry from fresh positions')
            same, opposite = 0, 0
            for order in pending.values():
                if rt._norm_side(order.side) == side:
                    same += order.volume
                else:
                    opposite += order.volume
            if opposite or same+volume > abs(p2):
                raise ValueError('cancel opposing orders / reserve pending exits before reducing')
        return self.raw.insert_order(iid, **kwargs)


class Controller:
    """Per-stock causal histories/lifecycle; no extra connection or model fitting."""
    def __init__(self, adapter, reducing_raw, clock=time.monotonic, logger=None):
        self.adapter, self.reducing_raw = adapter, reducing_raw
        self.clock, self.logger, self.state = clock, logger, {}

    def __call__(self, snap, cfg):
        bids, asks = [(p,v) for p,v in snap.bids if v > 0], [(p,v) for p,v in snap.asks if v > 0]
        if not bids or not asks:
            return []
        bid, ask = max(p for p,v in bids), min(p for p,v in asks)
        now, mid = self.clock(), (bid+ask)/2
        if not all(flat.finite(v) for v in (bid, ask, snap.tick_size)) or bid <= 0 or ask <= bid or snap.tick_size <= 0:
            return []
        iid = snap.instrument_id
        s = self.state.setdefault(iid, dict(history=deque(), pause=0., pos=0, opened=None,
                                            reference=None, flat_at=None, exit=False, report=None,
                                            entry_mode=None, last_mode=None))
        history = s['history']
        if history:
            delta_t = now-history[-1][0]
            shock = abs(mid-history[-1][1]) >= cfg.shock_ticks*snap.tick_size-1e-9
            if delta_t <= 0 or delta_t > cfg.max_gap or shock:
                history.clear()
                if shock: s['pause'] = now+cfg.cooldown
                if snap.position: s['exit'] = True
        # Keep one observation at or just older than the lookback boundary.
        while len(history) > 1 and history[1][0] <= now-cfg.lookback:
            history.popleft()
        if not snap.position:
            if s['pos']:
                s['flat_at'] = now
            s['opened'], s['reference'], s['exit'] = None, None, False
            s['entry_mode'] = None
        elif not s['pos'] or snap.position*s['pos'] < 0:
            s['opened'], s['reference'] = now, mid
            s['entry_mode'] = s['last_mode']
            # Existing inventory at startup has an unknown entry: reduce it first.
            s['exit'] = s['pos'] == 0 and not history
        pending = self.adapter.own.get(iid, [])
        snapshot = Snapshot(iid, bid, ask, snap.tick_size, snap.position,
                            sum(o.volume for o in pending if o.side == 'bid'),
                            sum(o.volume for o in pending if o.side == 'ask'),
                            max(0, now-self.adapter.received.get(iid, float('-inf'))),
                            tuple(Point(price, now-t) for t, price in history),
                            s['reference'], None if s['opened'] is None else now-s['opened'],
                            max(0, s['pause']-now),
                            0 if s['flat_at'] is None else max(0, cfg.reentry_delay-(now-s['flat_at'])),
                            s['exit'], s['entry_mode'])
        mode, reason = classify(snapshot, cfg)
        if wants_exit(snapshot, cfg, mode):
            s['exit'] = True
            snapshot = Snapshot(**{**snapshot.__dict__, 'exit_latched': True})
        self.reducing_raw.reduce_only[iid] = s['exit']
        orders = decide(snapshot, cfg)
        report = (mode, snap.position, s['exit'])
        if self.logger and report != s['report']:
            self.logger(dict(event='regime_or_position_observation', instrument=iid, mode=mode,
                             reason=reason, position=snap.position, exit=s['exit'],
                             monotonic_time=now, bid=bid, ask=ask,
                             quotes=[o.__dict__ for o in orders]))
        s['report'], s['pos'], s['last_mode'] = report, snap.position, mode
        history.append((now, mid))
        return orders


def main(argv=None, exchange_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('demo')
    run = sub.add_parser('run', help='one real connection; orders dry-run unless --live')
    run.add_argument('--live', action='store_true')
    run.add_argument('--cap', type=int, default=10)
    run.add_argument('--size', type=int, default=2)
    run.add_argument('--cost-ticks', type=float, required=True)
    run.add_argument('--max-requests', type=int, required=True)
    run.add_argument('--max-order-size', type=int, required=True)
    run.add_argument('--instruments', default=None)
    run.add_argument('--sleep', type=float, default=0.5)
    run.add_argument('--log-dir', default='logs')
    args = parser.parse_args(argv)
    if args.command != 'run':
        cfg = Config(cost_ticks=0)
        for label, slope in [('range', 0), ('down', 0.05), ('up', -0.05)]:
            snapshot = Snapshot('SYNTHETIC', 100, 100.1, 0.1,
                                history=tuple(Point(round(100.05+slope*i, 4), i) for i in range(1,21)))
            print('OFFLINE', label, classify(snapshot, cfg), decide(snapshot, cfg))
        return 0
    try:
        cfg = Config(cap=args.cap, size=args.size, cost_ticks=args.cost_ticks)
        budget = flat.RequestBudget(args.max_requests)
        if not 1 <= args.size <= args.max_order_size or not flat.finite(args.sleep) or args.sleep < 0:
            raise ValueError('invalid event size/sleep setting')
    except ValueError as exc:
        parser.error(str(exc))
    rt = flat.load_runtime()
    client = (exchange_factory or rt.connect_real_exchange)()
    reducing = ReducingRaw(flat.BudgetedRaw(client, budget))
    adapter = flat.guarded_adapter(reducing, cfg, args.max_order_size)
    if not adapter.is_connected(): return rt.EXIT_STARTUP
    instruments, error = rt.select_instruments(adapter.instruments(), rt._parse_ids(args.instruments))
    if error or not instruments:
        print(error or 'No tradable stocks')
        return rt.EXIT_STARTUP
    signal.signal(signal.SIGTERM, rt._raise_keyboard_interrupt)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = Path(args.log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    print('LIVE orders' if args.live else 'DRY orders; real connection. Do not run another bot.')
    with (directory/f'regime6_{stamp}.jsonl').open('w') as events:
        def log(event):
            events.write(json.dumps(event)+'\n'); events.flush()
        return rt.run_bot(adapter, instruments, cfg, dry_run=not args.live,
                          sleep_s=max(rt.MIN_SLEEP_S,args.sleep),
                          log_path=directory/f'run6_{stamp}.csv',
                          decide_fn=Controller(adapter,reducing,logger=log))


if __name__ == '__main__':
    raise SystemExit(main())
