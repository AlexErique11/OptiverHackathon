"""Deployable single-owner Optibook runner for strategy #6.

No imports connect. `run` connects for shadow inference; only `run --live` sends
orders. Start after stopping the prior bot. Unknown outcomes fail closed. All
network methods share a limiter; no credentials are read or printed here.
"""
import argparse
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module
import json
from math import isfinite
from pathlib import Path
import signal
import time
import traceback
import uuid

from optiver_bot.live_position import Controller, News
from optiver_bot.live_position_execution import Account, Budget, Executor, Resting
from optiver_bot.live_position_replay import Markouts, local_output
from optiver_bot.models import DesiredOrder

strategy = import_module('optiver_bot.strategies.market_maker#6')


class ApiError(RuntimeError):
    pass


class NonFlatAccount(ApiError):
    pass


def error_location(exc):
    """Identify the failing code without exposing client messages or locals."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    frame = frames[-1]
    return f'{type(exc).__name__} at {Path(frame.filename).name}:{frame.lineno} ({frame.name})'


class Limiter:
    def __init__(self, limit, window=1.0, clock=time.monotonic, sleep=time.sleep):
        if type(limit) is not int or limit < 1 or not isfinite(window) or window <= 0:
            raise ValueError('Explicit positive request limit/window required')
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self.events = deque()

    def call(self, fn, *a, **kw):
        while True:
            now = self.clock()
            while self.events and self.events[0] <= now-self.window:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now)
                return fn(*a, **kw)
            self.sleep(min(.1, max(.001, self.events[0]+self.window-now)))


def integer(value):
    if type(value) is not int:
        raise ApiError('Non-integer position/volume from client')
    return value


def success(response):
    # Do not guess that None or a truthy object means an accepted order.
    return getattr(response, 'success', None) is True


def levels(values):
    result = []
    for v in values:
        p, q = (v[0], v[1]) if isinstance(v, (tuple, list)) else (v.price, v.volume)
        q = integer(q)
        if not isfinite(p) or p <= 0 or q < 0:
            raise ApiError('Invalid price level')
        if q:
            result.append((float(p), q))
    return result


def age_seconds(timestamp, wall_now):
    if isinstance(timestamp, datetime):
        # Optibook commonly supplies naive UTC datetimes. Never use local timezone.
        ts = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
        age = wall_now-ts.timestamp()
    elif type(timestamp) in (int, float) and isfinite(timestamp):
        age = wall_now-timestamp
    else:
        raise ApiError('Book timestamp missing/unsupported; freshness cannot be established')
    if age < -.1:
        raise ApiError('Book timestamp is in the future; check clock synchronization')
    return max(0., age)


class LiveAdapter:
    """SDK translation plus conservative per-side lifetime reservation backstop.

    Cancelled quantities are NOT released from the backstop. Confirmed opposite
    fills replenish room. Thus stale client views/late fills cannot create fresh
    capacity by merely hiding an order after cancel. This may conservatively stop
    new trading after repeated unfilled replacements, instead of risking a breach.
    """
    def __init__(self, raw, cfg, instruments, limiter, max_order_size,
                 clock=time.monotonic, wall=time.time, require_flat=True):
        self.raw, self.cfg, self.ids = raw, cfg, tuple(instruments)
        self.limiter, self.max_size = limiter, max_order_size
        self.clock, self.wall = clock, wall
        self.meta = self.call('get_instruments')
        if set(self.ids)-self.meta.keys():
            raise ApiError('Unknown instrument selection')
        for i in self.ids:
            info = self.meta[i]
            tick = getattr(info, 'tick_size', 0)
            if not isfinite(tick) or tick <= 0 or getattr(info, 'paused', False):
                raise ApiError('Invalid tick or paused instrument')
            kind = getattr(info, 'instrument_type', None)
            if kind is not None and 'STOCK' not in str(getattr(kind, 'name', kind)).upper():
                raise ApiError('Only stocks are supported')
        self.revision = 0
        self.resolved = set()
        self.insert_ids = {}
        self.cancel_ids = set()
        self.sent = {i: [0, 0] for i in self.ids}
        self.filled = {i: [0, 0] for i in self.ids}
        self.cost_basis = {}
        self.ledger = {}
        self.fills = []
        self.markouts = Markouts()
        self.fill_seq = 0
        self.session = uuid.uuid4().hex
        # Drain historical own fills before defining the account baseline.
        for i in self.ids:
            self.call('poll_new_trades', i)
        p = self.positions()
        outside = {i: q for i, q in p.items() if q and i not in self.ids}
        if outside:
            raise ApiError('Unselected inventory exists; include those instruments for risk management')
        self.initial = {i: p.get(i, 0) for i in self.ids}
        if require_flat and any(self.initial.values()):
            raise NonFlatAccount('Live startup requires a flat account')
        self.ledger.update(self.initial)
        self.initial_equity = self.equity()
        self.entry_enabled = True
        self.halted = False
        self.deadline = float('inf')
        self.decision_expires = float('inf')
        self.last_frame = {}
        # Existing orders cannot be safely assigned to this process's fill ledger.
        # Refuse takeover rather than silently cancelling another strategy's orders.
        for i in self.ids:
            if self.orders(i):
                raise ApiError('Outstanding orders at startup; reconcile/stop previous owner first')

    def call(self, name, *a, **kw):
        return self.limiter.call(getattr(self.raw, name), *a, **kw)

    def positions(self):
        result = self.call('get_positions')
        return {i: integer(q) for i, q in result.items()}

    def equity(self):
        value = self.call('get_pnl')
        if type(value) not in (int, float) or not isfinite(value):
            raise ApiError('PnL unavailable')
        return value

    def orders(self, i):
        result = []
        for oid, o in self.call('get_outstanding_orders', i).items():
            side, quantity = getattr(o.side, 'value', o.side), integer(o.volume)
            if side not in ('bid', 'ask') or quantity <= 0 or not isfinite(o.price):
                raise ApiError('Unexpected outstanding-order schema')
            result.append(Resting(str(oid), DesiredOrder(i, side, float(o.price), quantity)))
        return tuple(sorted(result, key=lambda r: r.order_id))

    def poll_fills(self, i):
        for f in self.call('poll_new_trades', i):
            side, qty, price = getattr(f.side, 'value', f.side), integer(f.volume), float(f.price)
            if side not in ('bid', 'ask') or qty <= 0 or not isfinite(price) or price <= 0:
                raise ApiError('Unexpected own-trade schema')
            sign = 1 if side == 'bid' else -1
            old = self.ledger[i]
            new = old+sign*qty
            if old == 0 or old*sign > 0:
                previous = self.cost_basis.get(i)
                # An inherited unknown cost basis stays unknown until flat.
                if old == 0 or previous is not None:
                    self.cost_basis[i] = (abs(old)*(previous or 0)+qty*price)/abs(new)
            elif new == 0:
                self.cost_basis.pop(i, None)
            elif old*new < 0:
                self.cost_basis[i] = price
            self.ledger[i] = new
            self.filled[i][0 if sign > 0 else 1] += qty
            self.fill_seq += 1
            fill = dict(id=f'{self.session}-{self.fill_seq}', instrument=i,
                        side=side, price=price, volume=qty, received_at=self.clock())
            self.fills.append(fill)
            self.markouts.fill(fill, self.clock())

    def envelope(self, i):
        buys, sells = self.sent[i]
        buy_fills, sell_fills = self.filled[i]
        return (self.initial[i]-sells+buy_fills,
                self.initial[i]+buys-sell_fills)

    def gross(self, extra=None):
        total = 0.
        for i in self.ids:
            b = self.last_frame.get(i)
            if b is None or not strategy.valid_book(b, self.cfg):
                return float('inf')
            lo, hi = self.envelope(i)
            if extra is not None and extra.instrument_id == i:
                if extra.side == 'bid':
                    hi += extra.volume
                else:
                    lo -= extra.volume
            total += max(abs(lo), abs(hi))*b.ask
        return total

    def read(self, i):
        # Fresh positions around the orders/fills snapshot plus full sent-volume
        # backstop. Any mismatch is a reason to wait, never permission to insert.
        self.poll_fills(i)
        p1 = self.positions()
        orders = self.orders(i)
        self.poll_fills(i)
        p2 = self.positions()
        certain = p1 == p2 and p2.get(i, 0) == self.ledger[i]
        book = self.call('get_last_price_book', i)
        if book is None:
            raise ApiError('Missing book')
        bids, asks = levels(book.bids), levels(book.asks)
        tick = self.meta[i].tick_size
        def clean(ls, side):
            quantities = {p: sum(v for pp, v in ls if pp == p) for p, _ in ls}
            for r in orders:
                if r.order.side == side:
                    quantities[r.order.price] = quantities.get(r.order.price, 0)-r.order.volume
            return sorted(((p, v) for p, v in quantities.items() if v > 0), reverse=side == 'bid')
        bids, asks = clean(bids, 'bid'), clean(asks, 'ask')
        snap = strategy.DepthSnapshot(i, bids[0][0] if bids else 0,
             asks[0][0] if asks else 0, tick, p2.get(i, 0),
             sum(r.order.volume for r in orders if r.order.side == 'bid'),
             sum(r.order.volume for r in orders if r.order.side == 'ask'),
             age_seconds(getattr(book, 'timestamp', None), self.wall()),
             bids[0][1] if bids else 0, asks[0][1] if asks else 0)
        self.last_frame[i] = snap
        live = {r.order_id for r in orders}
        if certain:
            for token, (iid, oid) in list(self.insert_ids.items()):
                if iid == i:
                    # Accepted insert is now accounted by fills/live quantity.
                    self.resolved.add(token)
                    del self.insert_ids[token]
            for iid, oid in list(self.cancel_ids):
                if iid == i and oid not in live:
                    self.resolved.add(oid)
                    self.cancel_ids.remove((iid, oid))
        self.revision += 1
        return Account(self.revision, snap, orders, frozenset(self.resolved), certain,
                       certain and self.entry_enabled and self.gross() < self.cfg.gross_limit)

    def cancel(self, i, oid):
        self.cancel_ids.add((i, oid))
        try:
            response = self.call('delete_order', i, order_id=self.native_id(i, oid))
        except Exception:
            self.halted = True
            raise
        if not success(response):
            self.halted = True
            raise ApiError('Cancellation outcome not confirmed')

    def native_id(self, i, oid):
        # Preserve the API's actual identifier type; do not assume int/string.
        for key in self.call('get_outstanding_orders', i):
            if str(key) == oid:
                return key
        raise ApiError('Order disappeared before cancellation; refresh next frame')

    def insert(self, order, client_token):
        i, c = order.instrument_id, self.cfg
        a = self.read(i)
        q = a.book.position
        reducing = q*(1 if order.side == 'bid' else -1) < 0
        if not a.certain or not strategy.valid_book(a.book, c) or a.orders:
            raise ApiError('Pre-insert reconciliation changed')
        checked_at = self.clock()
        if order.volume > self.max_size or order.volume > c.clip:
            raise ApiError('Order exceeds configured maximum')
        if reducing and order.volume > abs(q):
            raise ApiError('Reduction would reverse')
        lo, hi = self.envelope(i)
        if ((order.side == 'bid' and hi+order.volume > min(100, max(c.cap, self.initial[i])))
                or (order.side == 'ask' and lo-order.volume < max(-100, min(-c.cap, self.initial[i])))):
            raise ApiError('Conservative fill envelope exhausted')
        buys, sells = a.book.outstanding_buy, a.book.outstanding_sell
        if q+buys+(order.volume if order.side == 'bid' else 0) > 100 or q-sells-(order.volume if order.side == 'ask' else 0) < -100:
            raise ApiError('Fresh hard capacity exhausted')
        equity = self.equity()
        if self.initial_equity-equity >= c.loss_limit:
            self.entry_enabled = False
        if not reducing and (not a.entry_allowed or not self.entry_enabled or self.halted
                or self.clock() >= min(self.deadline-c.no_entry_seconds, self.decision_expires)
                or self.gross(order) > c.gross_limit):
            raise ApiError('Portfolio budget/deadline exhausted')
        if order.order_type == 'limit' and ((order.side == 'bid' and order.price >= a.book.ask)
                                           or (order.side == 'ask' and order.price <= a.book.bid)):
            raise ApiError('Book changed; entry would cross')
        if order.order_type == 'ioc' and not reducing:
            raise ApiError('IOC entry disabled')
        # Reserve before sending, including exceptions/unknown acceptance. Never
        # retry an unknown insert. Executor keeps its token unresolved as well.
        self.sent[i][0 if order.side == 'bid' else 1] += order.volume
        def send():
            if a.book.book_age_seconds+self.clock()-checked_at > c.max_age:
                raise ApiError('Book aged while waiting for request budget')
            if not reducing and self.clock() >= min(self.deadline-c.no_entry_seconds, self.decision_expires):
                raise ApiError('Entry decision expired while waiting for request budget')
            return self.raw.insert_order(i, price=order.price, volume=order.volume,
                                         side=order.side, order_type=order.order_type)
        try:
            response = self.limiter.call(send)
        except Exception:
            self.halted = True
            raise
        if not success(response):
            if getattr(response, 'success', None) is False:
                self.resolved.add(client_token)  # rejection definite; backstop remains conservative
            else:
                self.halted = True
            raise ApiError('Insert rejected or unconfirmed')
        oid = getattr(response, 'order_id', None)
        if oid is None:
            self.halted = True
            raise ApiError('Accepted insert without order identifier')
        self.insert_ids[client_token] = (i, str(oid))

    def news(self, now):
        events = self.call('poll_new_social_media_feeds')
        result = []
        for e in events:
            text = str(getattr(e, 'post', ''))
            raw_meta = getattr(e, 'meta_data', None)
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            scope = tuple(i for i in self.ids if i in text.split() or meta.get('instrument_id') == i)
            # No guessed news-to-price semantics: all posts cause caution.
            stable = getattr(e, 'id', None)
            key = str(stable) if stable is not None else sha256(
                (str(getattr(e, 'timestamp', ''))+'\0'+text+'\0'+str(raw_meta)).encode()).hexdigest()
            result.append(News(key, now, scope, 0))
        return result

    def cleanup(self):
        for i in self.ids:
            try:
                for r in self.orders(i):
                    self.cancel(i, r.order_id)
            except Exception:
                pass
        # Never print exception messages: client exceptions may include private config.


def cloud_preflight(root=Path('/home/workspace')):
    hashes = {}
    for filename in ('AGENTS.md', 'api_reference.md'):
        p = root/filename
        if not p.is_file():
            raise ValueError(f'Required supplied cloud reference missing: {p}')
        hashes[filename] = sha256(p.read_bytes()).hexdigest()
    return hashes


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('demo', help='offline demo, no client import/connection')
    r = sub.add_parser('run', help='connect once; shadow unless --live')
    r.add_argument('--live', action='store_true')
    r.add_argument('--instruments', required=True, help='comma-separated confirmed stock IDs')
    r.add_argument('--duration-seconds', type=float, required=True, help='time REMAINING until session end')
    r.add_argument('--cost-ticks', type=float, required=True, help='confirmed conservative round-trip cost estimate')
    r.add_argument('--max-requests', type=int, required=True)
    r.add_argument('--request-window', type=float, default=1.)
    r.add_argument('--max-order-size', type=int, required=True)
    r.add_argument('--gross-limit', type=float, required=True)
    r.add_argument('--cash-risk', type=float, required=True)
    r.add_argument('--loss-limit', type=float, required=True)
    r.add_argument('--cap', type=int, default=10)
    r.add_argument('--target', type=int, default=3)
    r.add_argument('--clip', type=int, default=1)
    r.add_argument('--max-age', type=float, default=1.)
    r.add_argument('--log-dir', default='logs/strategy6')
    return p


def main(argv=None, *, exchange_factory=None, clock=time.monotonic,
         sleep=time.sleep, wall=time.time, preflight=cloud_preflight, max_frames=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == 'demo':
        from optiver_bot.live_position import main as demo
        import sys
        old = sys.argv
        try:
            sys.argv = [old[0], '--demo']
            demo()
        finally:
            sys.argv = old
        return 0
    try:
        cfg = strategy.Config(cap=args.cap, target=args.target, clip=args.clip,
                              round_trip_cost_ticks=args.cost_ticks, gross_limit=args.gross_limit,
                              position_cash_risk=args.cash_risk, loss_limit=args.loss_limit, max_age=args.max_age)
        ids = tuple(x.strip() for x in args.instruments.split(',') if x.strip())
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('Nonempty unique instruments required')
        if (not isfinite(args.duration_seconds) or args.duration_seconds <= cfg.no_entry_seconds
                or type(args.max_order_size) is not int or args.max_order_size < cfg.clip):
            raise ValueError('Invalid remaining duration/order limit')
        limiter = Limiter(args.max_requests, args.request_window, clock, sleep)
        refs = preflight()  # reads references without printing or copying their contents
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        path = local_output(str(Path(args.log_dir)/(stamp+'.jsonl')))
    except (ValueError, OSError) as exc:
        p.error(str(exc))
    print('LIVE ORDERS' if args.live else 'SHADOW: real data, no order mutations')
    print('One team connection: stop the previous bot before running this process.')
    raw = None
    adapter = None
    old_term = None
    start = clock()
    deadline = start+args.duration_seconds
    try:
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        old_term = signal.signal(signal.SIGTERM, interrupt)
        if exchange_factory is None:
            from optibook.synchronous_client import Exchange
            exchange_factory = Exchange
        raw = exchange_factory()  # exactly once; injected configuration is automatic
        limiter.call(raw.connect)
        required = ('get_instruments', 'get_positions', 'get_pnl', 'get_outstanding_orders',
                    'get_last_price_book', 'poll_new_trades', 'poll_new_social_media_feeds',
                    'insert_order', 'delete_order', 'is_connected')
        if any(not callable(getattr(raw, n, None)) for n in required):
            raise ApiError('Supplied client is missing a required method')
        adapter = LiveAdapter(raw, cfg, ids, limiter, args.max_order_size, clock, wall,
                              require_flat=args.live)
        print(f'Starting account PnL: {adapter.initial_equity:.2f}; positions: {adapter.initial}')
        adapter.deadline = deadline
        engine = Controller(ids, cfg, deadline)
        gate = Executor(adapter, cfg, Budget(args.max_requests, args.request_window,
                                             reserve=min(2, args.max_requests-1)))
        frames = 0
        with path.open('x', encoding='utf-8') as log:
            log.write(json.dumps(dict(kind='settings', config=asdict(cfg), references=refs,
                                      live=args.live, instruments=ids, duration=args.duration_seconds,
                                      starting_equity=adapter.initial_equity,
                                      starting_positions=adapter.initial))+'\n')
            while clock() < deadline and (max_frames is None or frames < max_frames):
                begin = clock()
                if not limiter.call(raw.is_connected):
                    raise ApiError('Disconnected; no reconnect attempts')
                views, captured = {}, {}
                for i in ids:
                    views[i] = adapter.read(i)
                    captured[i] = clock()
                now = clock()
                feed_ok = True
                try:
                    news = adapter.news(now)
                except Exception:
                    news, feed_ok = [], False
                now = clock()
                books = {i: strategy.DepthSnapshot(**{
                    **asdict(v.book), 'book_age_seconds': v.book.book_age_seconds+now-captured[i]})
                         for i, v in views.items()}
                equity = adapter.equity()
                now = clock()
                adapter.entry_enabled = (not adapter.halted and feed_ok and now < deadline-cfg.no_entry_seconds
                    and adapter.initial_equity-equity < cfg.loss_limit)
                frame_entry_prices = dict(adapter.cost_basis)
                decisions = engine.update(now, books, equity=equity,
                    account_ok=all(v.certain for v in views.values()), news=news,
                    feed_ok=feed_ok and not adapter.halted, entry_prices=frame_entry_prices)
                outcomes = {}
                adapter.decision_expires = now+cfg.entry_ttl
                if args.live:
                    # Reductions first; each actual insertion re-reads and reserves.
                    for d in sorted(decisions, key=lambda d: d.target != 0):
                        if (not d.orders and not views[d.instrument].orders
                                and not any(o.instrument_id == d.instrument for o in gate.pending.values())
                                and not any(i == d.instrument for i, _ in gate.cancel_pending)):
                            continue
                        outcomes[d.instrument] = gate.reconcile(d.instrument, d.orders,
                                                               d.target, clock())
                pending_fills = [{**f, 'received_at': f['received_at']-start}
                                 for f in adapter.fills if f['received_at'] <= now]
                adapter.fills = [f for f in adapter.fills if f['received_at'] > now]
                row = dict(kind='frame', time=now-start, equity=equity,
                    account_ok=all(v.certain for v in views.values()), feed_ok=feed_ok,
                    books={i: {k: v for k, v in asdict(b).items() if k != 'instrument_id'} for i, b in books.items()},
                    entry_prices=frame_entry_prices, news=[{**asdict(n), 'received_at': n.received_at-start} for n in news],
                    fills=pending_fills, decisions=[asdict(d) for d in decisions], outcomes=outcomes,
                    envelopes={i: adapter.envelope(i) for i in ids},
                    markouts=adapter.markouts.observe(now, {i: (b.bid+b.ask)/2 for i, b in books.items()
                        if strategy.valid_book(b, cfg)}))
                log.write(json.dumps(row)+'\n')
                log.flush()
                if frames % 5 == 0:
                    print(f'{now-start:.0f}s PnL={equity:.2f} change={equity-adapter.initial_equity:+.2f}',
                          [(d.instrument, d.reason, d.target) for d in decisions])
                frames += 1
                sleep(max(.2, 1-(clock()-begin)))
        return 0
    except KeyboardInterrupt:
        print('Interrupted; cancelling outstanding orders. Filled inventory may remain.')
        return 130
    except NonFlatAccount:
        print('LIVE STARTUP REFUSED: existing positions detected. No orders were submitted. '
              'Inspect and manage existing inventory separately; do not restart to bypass this guard.')
        return 1
    except Exception as exc:
        print(f'Runner stopped: {error_location(exc)}. Inspect account state; inventory may remain.')
        return 1
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if adapter is not None and args.live:
            adapter.cleanup()
        if raw is not None and callable(getattr(raw, 'disconnect', None)):
            try:
                limiter.call(raw.disconnect)
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
