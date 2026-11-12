#!/usr/bin/env python3
"""Rule-of-thumb inventory-aware maker for a quiet market.

No arguments = offline demo. `run` uses the SAME runner/API adapter as #1,
with one connection, request pacing and fresh pre-insert risk checks.
This is not a calibrated Avellaneda-Stoikov implementation or a profit claim.
"""
import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import importlib.util
import math
from pathlib import Path
import signal
import sys
import time


@dataclass(frozen=True)
class Config:
    cap: int = 20
    size: int = 3
    cost_ticks: float | None = None
    min_net_ticks: float = 0.5
    soft_fraction: float = 0.8
    inventory_widen_ticks: float = 2.0
    max_age: float = 0.5
    shock_ticks: float = 5.0
    cooldown: float = 5.0

    def __post_init__(self):
        if type(self.cap) is not int or not 1 <= self.cap <= 100:
            raise ValueError('cap must be an integer between 1 and 100')
        if type(self.size) is not int or self.size < 1:
            raise ValueError('size must be a positive integer')
        for name in ('min_net_ticks', 'soft_fraction', 'max_age', 'shock_ticks', 'cooldown'):
            value = getattr(self, name)
            if not finite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if self.soft_fraction > 1 or not finite(self.inventory_widen_ticks) or self.inventory_widen_ticks < 0:
            raise ValueError('invalid inventory controls')
        if self.cost_ticks is not None and (not finite(self.cost_ticks) or self.cost_ticks < 0):
            raise ValueError('cost_ticks must be an explicit nonnegative round-trip estimate')


@dataclass(frozen=True)
class Snapshot:
    instrument_id: str
    bid: float
    ask: float
    tick_size: float
    position: int = 0
    outstanding_buy: int = 0
    outstanding_sell: int = 0
    book_age_seconds: float = 0.0


@dataclass(frozen=True)
class DesiredOrder:
    side: str
    price: float
    volume: int


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def rooms(position, buys, sells, cap):
    if any(type(v) is not int for v in (position, buys, sells, cap)):
        raise ValueError('positions, pending volumes and cap must be integers')
    if not 1 <= cap <= 100 or abs(position) > 100 or min(buys, sells) < 0:
        raise ValueError('invalid position/cap/pending volume')
    return max(0, cap-position-buys), max(0, cap+position-sells)


def decide(snapshot, config):
    """Pure target quotes. No history warm-up, exchange calls or mutations."""
    buy_room, sell_room = rooms(snapshot.position, snapshot.outstanding_buy,
                                 snapshot.outstanding_sell, config.cap)
    if (snapshot.position + snapshot.outstanding_buy > 100
            or snapshot.position - snapshot.outstanding_sell < -100):
        return []
    if not snapshot.instrument_id or not all(finite(v) for v in
            (snapshot.bid, snapshot.ask, snapshot.tick_size, snapshot.book_age_seconds)):
        return []
    if (snapshot.bid <= 0 or snapshot.ask <= snapshot.bid or snapshot.tick_size <= 0
            or not 0 <= snapshot.book_age_seconds <= config.max_age):
        return []
    bid, ask, tick = (Decimal(str(v)) for v in
                      (snapshot.bid, snapshot.ask, snapshot.tick_size))
    if bid % tick or ask % tick:
        return []
    spread = (ask-bid)/tick
    # Missing/uneconomic costs block new exposure, but not reducing quotes.
    economical = (config.cost_ticks is not None
                  and spread >= Decimal(str(config.cost_ticks)) + Decimal(str(config.min_net_ticks)))
    soft = Decimal(config.cap) * Decimal(str(config.soft_fraction))
    orders = []
    for side, room, exposure, touch in (
        ('bid', buy_room, max(0, snapshot.position), bid),
        ('ask', sell_room, max(0, -snapshot.position), ask),
    ):
        reducing = (side == 'ask' and snapshot.position > 0) or (side == 'bid' and snapshot.position < 0)
        if not economical and not reducing:
            continue
        fraction = max(Decimal(0), 1 - Decimal(exposure)/soft)
        volume = min(room, max(1, int(Decimal(config.size)*fraction))) if fraction > 0 else 0
        if not economical and reducing:
            # Never reverse through zero merely to get out of inventory.
            pending = snapshot.outstanding_sell if side == 'ask' else snapshot.outstanding_buy
            volume = min(volume, max(0, abs(snapshot.position)-pending))
        # Widen only the risk-increasing quote. The reducing side stays at touch.
        widen = int((Decimal(str(config.inventory_widen_ticks))*Decimal(exposure)/soft)
                    .to_integral_value(rounding=ROUND_FLOOR))
        price = touch - widen*tick if side == 'bid' else touch + widen*tick
        if volume > 0 and price > 0:
            orders.append(DesiredOrder(side, float(price), volume))
    return orders


def load_runtime():
    name = '_optiver_strategy5_shared_runtime'
    if name not in sys.modules:
        path = Path(__file__).with_name('market_maker #1.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class RequestBudget:
    """Conservative rolling one-second limit on ALL raw API method calls."""
    def __init__(self, limit, clock=time.monotonic, sleep=time.sleep):
        if type(limit) is not int or limit <= 0:
            raise ValueError('request budget must be a positive integer from event rules')
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.events = deque()

    def acquire(self):
        while True:
            now = self.clock()
            while self.events and now-self.events[0] >= 1:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now)
                return
            self.sleep(min(0.1, max(0.001, 1-(now-self.events[0]))))


class BudgetedRaw:
    def __init__(self, raw, budget):
        self.raw, self.budget = raw, budget

    def __getattr__(self, name):
        value = getattr(self.raw, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            self.budget.acquire()
            return value(*args, **kwargs)
        return call


def guarded_adapter(raw, config, max_order_size, clock=time.monotonic):
    """Wrap ONE already-connected raw client; never create another connection."""
    runtime = load_runtime()
    class Adapter(runtime.ExchangeAdapter):
        def __init__(self):
            super().__init__(raw)
            self.own = {}
            self.received = {}

        def book(self, iid):
            result = super().book(iid)
            self.received[iid] = clock()
            return result

        def own_orders(self, iid):
            result = super().own_orders(iid)
            self.own[iid] = result
            return result

        def insert(self, iid, side, price, volume, order_type='limit'):
            try:
                if side not in ('bid', 'ask') or order_type != 'limit':
                    return runtime.Result(False, None, 'only passive bids/asks supported')
                if type(volume) is not int or not 0 < volume <= max_order_size:
                    return runtime.Result(False, None, 'invalid/excessive order volume')
                # Re-read after every cancellation. If fills changed the position
                # between these reads, retry NEXT loop with a fresh snapshot.
                p1 = self.positions().get(iid, 0)
                pending = self.own_orders(iid)
                p2 = self.positions().get(iid, 0)
                if p1 != p2:
                    return runtime.Result(False, None, 'position changed during risk snapshot')
                buys = sum(o.volume for o in pending if o.side == 'bid')
                sells = sum(o.volume for o in pending if o.side == 'ask')
                buy_room, sell_room = rooms(p2, buys, sells, config.cap)
                if p2+buys > 100 or p2-sells < -100:
                    return runtime.Result(False, None, 'existing orders violate hard bounds')
                if volume > (buy_room if side == 'bid' else sell_room):
                    return runtime.Result(False, None, 'fresh independent capacity exhausted')
                bids, asks = self.book(iid)
                if not finite(price) or price <= 0 or not bids or not asks:
                    return runtime.Result(False, None, 'invalid price or empty fresh book')
                if (side == 'bid' and price >= min(p for p, v in asks if v > 0)
                        or side == 'ask' and price <= max(p for p, v in bids if v > 0)):
                    return runtime.Result(False, None, 'quote would cross refreshed book')
                return super().insert(iid, side, price, volume, order_type)
            except Exception as exc:
                return runtime.Result(False, None, f'pre-insert check failed: {type(exc).__name__}')
    return Adapter()


class Controller:
    """State outside decide; consumes the existing runner's cleaned books."""
    def __init__(self, adapter, clock=time.monotonic):
        self.adapter, self.clock = adapter, clock
        self.last_mid, self.pause_until = {}, {}

    def __call__(self, snap, cfg):
        if not snap.bids or not snap.asks:
            return []
        bid = max(p for p, v in snap.bids if v > 0)
        ask = min(p for p, v in snap.asks if v > 0)
        now, mid = self.clock(), (bid+ask)/2
        if snap.instrument_id in self.last_mid:
            if abs(mid-self.last_mid[snap.instrument_id]) >= cfg.shock_ticks*snap.tick_size - 1e-9:
                self.pause_until[snap.instrument_id] = now+cfg.cooldown
        self.last_mid[snap.instrument_id] = mid
        pending = self.adapter.own.get(snap.instrument_id, [])
        own = Snapshot(snap.instrument_id, bid, ask, snap.tick_size, snap.position,
                       sum(o.volume for o in pending if o.side == 'bid'),
                       sum(o.volume for o in pending if o.side == 'ask'),
                       max(0, now-self.adapter.received.get(snap.instrument_id, float('-inf'))))
        orders = decide(own, cfg)
        if now < self.pause_until.get(snap.instrument_id, 0):
            reducing = []
            for order in orders:
                if ((order.side == 'ask' and snap.position > 0)
                        or (order.side == 'bid' and snap.position < 0)):
                    pending_volume = own.outstanding_sell if order.side == 'ask' else own.outstanding_buy
                    volume = min(order.volume, max(0, abs(snap.position)-pending_volume))
                    if volume:
                        reducing.append(DesiredOrder(order.side, order.price, volume))
            orders = reducing
        return orders


def main(argv=None, exchange_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('demo', help='offline; no connection and no fills simulated')
    run = sub.add_parser('run', help='one connection, dry-run orders unless --live')
    run.add_argument('--live', action='store_true')
    run.add_argument('--cap', type=int, default=20)
    run.add_argument('--size', type=int, default=3)
    run.add_argument('--cost-ticks', type=float, required=True, help='explicit round-trip cost assumption')
    run.add_argument('--max-requests', type=int, required=True, help='verified all-team per-second budget')
    run.add_argument('--max-order-size', type=int, required=True, help='verified event order-size bound')
    run.add_argument('--instruments', default=None)
    run.add_argument('--sleep', type=float, default=0.5)
    run.add_argument('--log-dir', default='logs')
    args = parser.parse_args(argv)
    if args.command != 'run':
        cfg = Config(cost_ticks=0)  # explicit zero-cost OFFLINE illustration
        print('OFFLINE: flat book, no connection or fills. Zero costs assumed.')
        for pos in (0, 10, -10, 20, -20):
            print(pos, decide(Snapshot('DEMO', 100.0, 100.1, 0.1, pos), cfg))
        return 0
    try:
        cfg = Config(cap=args.cap, size=args.size, cost_ticks=args.cost_ticks)
        budget = RequestBudget(args.max_requests)
        if type(args.max_order_size) is not int or not 1 <= args.size <= args.max_order_size:
            raise ValueError('size must be within verified maximum order size')
        if not finite(args.sleep) or args.sleep < 0:
            raise ValueError('sleep must be finite and nonnegative')
    except ValueError as exc:
        parser.error(str(exc))
    rt = load_runtime()
    # Exactly ONE call to the factory, using the same supplied Optibook runtime.
    raw = (exchange_factory or rt.connect_real_exchange)()
    adapter = guarded_adapter(BudgetedRaw(raw, budget), cfg, args.max_order_size)
    if not adapter.is_connected():
        print('Disconnected; no retries or orders.')
        return rt.EXIT_STARTUP
    instruments, error = rt.select_instruments(adapter.instruments(), rt._parse_ids(args.instruments))
    if error or not instruments:
        print(error or 'No tradable stocks')
        return rt.EXIT_STARTUP
    print('LIVE orders' if args.live else 'DRY orders; real market-data connection')
    print('Do not run alongside another bot using the same username.')
    signal.signal(signal.SIGTERM, rt._raise_keyboard_interrupt)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return rt.run_bot(adapter, instruments, cfg, dry_run=not args.live,
                      sleep_s=max(rt.MIN_SLEEP_S, args.sleep),
                      log_path=Path(args.log_dir)/f'flat5_{stamp}.csv',
                      decide_fn=Controller(adapter))


if __name__ == '__main__':
    raise SystemExit(main())
