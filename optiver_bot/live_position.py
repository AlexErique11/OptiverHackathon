"""Bounded live analysis/controller. No credentials, network calls or connections.

    python -m optiver_bot.live_position --demo

The active execution owner feeds cleaned books and centrally polled social events
to Controller.update. Returned desired orders need an independently reconciled
execution gate; see live_position_execution.py. Returns are causal and resets
discard history. All times supplied here are monotonic session seconds.
"""
import argparse
from collections import deque
from dataclasses import dataclass, replace, fields
from math import ceil, log, sqrt
from statistics import pstdev

from optiver_bot.models import Snapshot
from importlib import import_module

# Numbered filenames contain '#', so load the canonical module by name.
_strategy = import_module('optiver_bot.strategies.market_maker#6')
Config, Features, PositionSnapshot = _strategy.Config, _strategy.Features, _strategy.PositionSnapshot
decide, direction, valid_book = _strategy.decide, _strategy.direction, _strategy.valid_book


@dataclass(frozen=True)
class News:
    event_id: str
    received_at: float
    instruments: tuple[str, ...] = ()  # empty means unknown/basket scope
    sentiment: int = 0  # verified metadata/parser output only; never inferred here


@dataclass
class Life:
    previous_position: int = 0
    entered: float | None = None
    entry: float = 0.0
    stop: float = 0.0
    target: int = 0
    streak: int = 0
    weak: int = 0
    cooldown_until: float = 0.0
    attempt: float | None = None
    reducing: bool = False


@dataclass(frozen=True)
class Decision:
    instrument: str
    target: int
    reason: str
    features: Features
    snapshot: PositionSnapshot
    orders: tuple


class Controller:
    def __init__(self, instruments, config: Config, deadline: float):
        from math import isfinite
        self.ids = tuple(sorted(instruments))
        if not self.ids or len(set(self.ids)) != len(self.ids):
            raise ValueError('A fixed nonempty unique universe is required')
        if not isfinite(deadline) or deadline <= 0:
            raise ValueError('Explicit session deadline required')
        self.cfg, self.deadline = config, deadline
        self.history = {i: deque(maxlen=121) for i in self.ids}
        self.life = {i: Life() for i in self.ids}
        self.news_until = {i: 0.0 for i in self.ids}
        self.news_sign = {i: 0 for i in self.ids}
        self.seen = {}  # bounded by expiration; one event cannot refresh its pause
        self.last = None
        self.initial_equity = None
        self.loss_halted = False

    def update(self, now, books: dict[str, Snapshot], *, equity: float,
               account_ok: bool, news=(), feed_ok=False, entry_prices=None):
        from math import isfinite
        if not isfinite(now) or not isfinite(equity):
            raise ValueError('Finite time/equity required')
        if self.last is not None and now <= self.last:
            raise ValueError('Time must strictly increase; create new controller on reset')
        if any(i not in self.ids or b.instrument_id != i for i, b in books.items()):
            raise ValueError('Universe/book identity mismatch')
        # Require roughly one-second analysis frames; fast order handling is external.
        if self.last is not None and now - self.last < 0.99:
            raise ValueError('Supply at most one analysis frame per second')
        gap = self.last is not None and now - self.last > 1.01
        self.last = now
        if self.initial_equity is None:
            self.initial_equity = equity
        if self.cfg.loss_limit is not None and self.initial_equity - equity >= self.cfg.loss_limit:
            self.loss_halted = True
        for key in list(self.seen):
            if now - self.seen[key] > 120:
                del self.seen[key]
        for event in news:
            if (not event.event_id or event.sentiment not in (-1, 0, 1)
                    or not isfinite(event.received_at) or event.received_at > now):
                continue
            if event.event_id in self.seen or now - event.received_at >= self.cfg.news_pause:
                continue
            self.seen[event.event_id] = now
            for i in event.instruments or self.ids:
                if i in self.news_until:
                    self.news_until[i] = max(self.news_until[i], event.received_at + self.cfg.news_pause)
                    self.news_sign[i] = event.sentiment
        for i in self.ids:
            b = books.get(i)
            h = self.history[i]
            if gap or b is None or not valid_book(b, self.cfg):
                h.clear()
            if b is not None and valid_book(b, self.cfg):
                h.append((now, (b.bid + b.ask) / 2))
        features = {}
        for i, h in self.history.items():
            c = self.cfg
            if len(h) <= c.warmup:
                features[i] = Features()
                continue
            prices = [p for _, p in h]
            returns = [log(b / a) for a, b in zip(prices[-c.warmup-1:], prices[-c.warmup:])]
            changes = sum(abs(r) > 1e-12 for r in returns)
            features[i] = Features(
                ready=changes >= c.min_changes,
                fast_return=log(prices[-1] / prices[-1-c.fast]),
                slow_return=log(prices[-1] / prices[-1-c.slow]), sigma=pstdev(returns))
        ready = [f for f in features.values() if f.ready]
        covered = len(ready) >= ceil(len(self.ids) * self.cfg.coverage)
        basket = sum(f.slow_return for f in ready) / len(ready) if ready else 0
        up = sum(f.slow_return > 0 for f in ready) / len(ready) if ready else 0
        down = sum(f.slow_return < 0 for f in ready) / len(ready) if ready else 0
        for i, f in features.items():
            b = books.get(i)
            bv, av = getattr(b, 'bid_volume', 0), getattr(b, 'ask_volume', 0)
            imbalance = (bv-av)/(bv+av) if type(bv) is int and type(av) is int and min(bv, av) >= 0 and bv+av > 0 else 0
            features[i] = replace(f, ready=f.ready and covered, basket=basket,
                                  up_breadth=up, down_breadth=down, imbalance=imbalance)
        c = self.cfg
        configured = all(x is not None for x in (c.round_trip_cost_ticks, c.gross_limit,
                                                c.position_cash_risk, c.loss_limit))
        global_exit = self.loss_halted or now >= self.deadline - c.wind_down_seconds
        entries = configured and account_ok and feed_ok and now < self.deadline-c.no_entry_seconds
        # Gross reservation includes either possible side, without cross-stock offsets.
        gross = sum(((b.bid+b.ask)/2) * (abs(b.position) + b.outstanding_buy + b.outstanding_sell)
                    for b in books.values() if valid_book(b, c))
        if len(books) != len(self.ids) or any(not valid_book(b, c) for b in books.values()):
            entries = False
        candidates = []
        targets, reasons, aggressive = {}, {}, {}
        for i in self.ids:
            b, life, f = books.get(i), self.life[i], features[i]
            if b is None:
                continue
            # An inherited position is reduction-only, never adopted as a new thesis.
            if b.position and life.previous_position == 0:
                if life.attempt is None:
                    life.reducing = True
                life.entered = now
                actual_entry = (entry_prices or {}).get(i)
                if actual_entry is not None and (not isfinite(actual_entry) or actual_entry <= 0):
                    raise ValueError('Invalid verified entry price')
                life.entry = actual_entry if actual_entry is not None else (b.ask if b.position > 0 else b.bid)
                if actual_entry is None:
                    life.reducing = True  # do not manage unverified cost basis as a valid thesis
                life.stop = max(c.stop_ticks*b.tick_size,
                                c.stop_sigma*f.sigma*sqrt(c.max_hold)*life.entry,
                                b.ask-b.bid)
            if not b.position and life.previous_position:
                life.cooldown_until = now + c.cooldown
                life.target, life.attempt, life.reducing = 0, None, False
                life.entered = None
            life.previous_position = b.position
            d = direction(f, b, c) if feed_ok and now >= self.news_until[i] else 0
            life.streak = life.streak + d if d and life.streak*d >= 0 else d
            reason, risk_exit = 'warmup' if not f.ready else 'no_signal', False
            target = life.target if b.position else 0
            if b.position:
                sign = 1 if b.position > 0 else -1
                life.weak = life.weak + 1 if d != sign else 0
                stop = sign * ((b.bid if sign > 0 else b.ask)-life.entry) <= -life.stop
                news_exit = now < self.news_until[i] and self.news_sign[i] == -sign
                timed = life.entered is not None and now-life.entered >= c.max_hold
                if global_exit or stop or news_exit or timed or life.weak >= 2 or life.reducing:
                    life.reducing = True
                    target = 0
                    reason = 'wind_down_or_loss' if global_exit else 'exit'
                    risk_exit = global_exit or stop or news_exit or timed
                else:
                    reason = 'hold'
            else:
                expired = life.attempt is not None and now-life.attempt >= c.entry_ttl
                if expired:
                    life.cooldown_until = now+c.cooldown
                    life.attempt, life.target = None, 0
                if (entries and not global_exit and now >= life.cooldown_until
                        and abs(life.streak) >= c.confirmation and d):
                    score = abs(f.slow_return) / max(f.sigma*sqrt(c.slow), b.tick_size/((b.bid+b.ask)/2))
                    candidates.append((-score, i, d))
                elif b.outstanding_buy or b.outstanding_sell:
                    reason = 'cancel_entry'
            targets[i], reasons[i], aggressive[i] = target, reason, risk_exit
        occupied = {i for i, b in books.items() if b.position or b.outstanding_buy or b.outstanding_sell}
        for _, i, d in sorted(candidates):
            if i not in occupied and len(occupied) >= c.max_active:
                continue
            b, life, f = books[i], self.life[i], features[i]
            mid = (b.bid+b.ask)/2
            stop = max(c.stop_ticks*b.tick_size, b.ask-b.bid, c.stop_sigma*f.sigma*sqrt(c.max_hold)*mid)
            size = min(c.target, int(c.position_cash_risk/stop),
                       max(0, int((c.gross_limit-gross)/mid)))
            if life.attempt is not None:
                size = min(size, abs(life.target))
            if size > 0:
                # Whole target reserved in this batch, not just this order's clip.
                gross += size*mid
                occupied.add(i)
                targets[i], reasons[i] = d*size, 'entry'
                life.target = d*size
                if life.attempt is None:
                    life.attempt = now
        output = []
        for i in self.ids:
            if i not in books:
                continue
            base = {f.name: getattr(books[i], f.name) for f in fields(Snapshot)}
            snap = PositionSnapshot(**base, target_position=targets[i],
                                    account_ok=account_ok, risk_exit=aggressive[i])
            output.append(Decision(i, targets[i], reasons[i], features[i], snap, tuple(decide(snap, c))))
        return output


def main():
    p = argparse.ArgumentParser(description='Offline live-position demonstration; no exchange access')
    p.add_argument('--demo', action='store_true', required=True)
    p.parse_args()
    cfg = Config(round_trip_cost_ticks=0.1, gross_limit=10000,
                 position_cash_risk=10, loss_limit=50)
    engine = Controller(('A', 'B'), cfg, deadline=1200)
    for t in range(45):
        books = {i: Snapshot(i, round(100+t*0.1, 1), round(100.1+t*0.1, 1), 0.1)
                 for i in ('A', 'B')}
        decisions = engine.update(t, books, equity=0, account_ok=True, feed_ok=True)
        if t in (0, 30, 32, 44):
            print(t, [(d.instrument, d.reason, d.target, list(d.orders)) for d in decisions])
    print('Synthetic observations only. No fills simulated; no profitability claim.')


if __name__ == '__main__':
    main()
