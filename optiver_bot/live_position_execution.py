"""One-owner execution boundary with an injected, verified account adapter.

No Exchange construction. The adapter MUST return fresh coherent account views,
including unresolved insert/cancel reservations; never attach #1/#4 unchanged.
The strict cancel barrier requires a subsequent independently refreshed view.
"""
from dataclasses import dataclass
from collections import deque
from math import isfinite
from typing import Protocol

from optiver_bot.models import DesiredOrder, Snapshot
from importlib import import_module

_strategy = import_module('optiver_bot.strategies.market_maker#6')
Config, valid_book = _strategy.Config, _strategy.valid_book


@dataclass(frozen=True)
class Resting:
    order_id: str
    order: DesiredOrder


@dataclass(frozen=True)
class Account:
    revision: int
    book: Snapshot
    orders: tuple[Resting, ...]
    resolved: frozenset[str] = frozenset()  # definitively resolved; accepted inserts represented in orders
    certain: bool = False
    entry_allowed: bool = False  # owner verified global budgets/deadline on THIS revision


class Adapter(Protocol):
    def read(self, instrument: str) -> Account: ...
    def cancel(self, instrument: str, order_id: str) -> None: ...
    def insert(self, order: DesiredOrder, client_token: str) -> None: ...


class Budget:
    """Conservative common request window; all owner API calls must share it."""
    def __init__(self, requests: int, window: float, reserve: int = 2):
        if type(requests) is not int or requests <= 0 or not 0 <= reserve < requests:
            raise ValueError('Explicit request budget required')
        if not isfinite(window) or window <= 0:
            raise ValueError('Invalid request window')
        self.limit, self.window, self.reserve = requests, window, reserve
        self.calls = deque()
        self.last = float('-inf')

    def take(self, now, *, urgent=False):
        if not isfinite(now) or now < self.last:
            raise ValueError('Monotonic budget time required')
        self.last = now
        while self.calls and self.calls[0] <= now-self.window:
            self.calls.popleft()
        if len(self.calls) >= self.limit-(0 if urgent else self.reserve):
            return False
        self.calls.append(now)
        return True


class Executor:
    """One pending mutation at a time, with a fresh-read barrier before the next.

    Caller recomputes targets after every refreshed frame. target_position is
    checked AGAIN here so fills since decide() cannot cause a reversal/overshoot.
    Unknown insert outcomes block further inserts until explicit reconciliation.
    """
    def __init__(self, adapter: Adapter, cfg: Config, budget: Budget):
        self.adapter, self.cfg, self.budget = adapter, cfg, budget
        self.revision = {}
        self.pending = {}  # token -> desired order
        self.cancel_pending = {}  # (instrument, order_id) -> old order
        self.sequence = 0

    def reconcile(self, instrument, desired, target_position, now):
        if type(target_position) is not int or abs(target_position) > self.cfg.cap:
            raise ValueError('Invalid target')
        if len(desired) > 1 or any(o.instrument_id != instrument for o in desired):
            raise ValueError('At most one target order per instrument')
        if not self.budget.take(now, urgent=True):
            return 'budget'
        a = self.adapter.read(instrument)
        s, c = a.book, self.cfg
        if s.instrument_id != instrument or not a.certain or a.revision <= self.revision.get(instrument, -1):
            return 'uncertain'
        self.revision[instrument] = a.revision
        if (type(s.position) is not int or abs(s.position) > 100
                or any(type(x) is not int or x < 0 for x in (s.outstanding_buy, s.outstanding_sell))):
            return 'unsafe'
        live_ids = {r.order_id for r in a.orders}
        # A cancellation acknowledgement alone never releases its reservation.
        for key in list(self.cancel_pending):
            if key[0] == instrument and key[1] not in live_ids and key[1] in a.resolved:
                del self.cancel_pending[key]
        for token in list(self.pending):
            if self.pending[token].instrument_id == instrument and token in a.resolved:
                del self.pending[token]
        if any(o.instrument_id == instrument for o in self.pending.values()):
            return 'insert_pending'
        if any(i == instrument for i, _ in self.cancel_pending):
            return 'cancel_pending'
        buys = sum(r.order.volume for r in a.orders if r.order.side == 'bid')
        sells = sum(r.order.volume for r in a.orders if r.order.side == 'ask')
        if buys != s.outstanding_buy or sells != s.outstanding_sell:
            return 'uncertain'
        unsafe = (s.position+buys > min(100, max(c.cap, s.position))
                  or s.position-sells < max(-100, min(-c.cap, s.position)))
        d = desired[0] if desired else None
        if d is not None:
            if (d.side not in ('bid', 'ask') or d.order_type not in ('limit', 'ioc')
                    or type(d.volume) is not int or d.volume <= 0 or d.volume > c.clip
                    or not isfinite(d.price) or d.price <= 0
                    or not valid_book(s, c)
                    or abs(d.price/s.tick_size-round(d.price/s.tick_size)) > 1e-6):
                d = None
            else:
                delta = target_position-s.position
                if not delta or (delta > 0) != (d.side == 'bid'):
                    d = None
                elif s.position*delta >= 0 and (not a.entry_allowed or any(x is None for x in (
                        c.round_trip_cost_ticks, c.gross_limit, c.position_cash_risk, c.loss_limit))):
                    d = None
        # Keep matching remaining targets; never top up partial fills needlessly.
        keep = (not unsafe and d is not None and len(a.orders) == 1
                and a.orders[0].order.side == d.side and a.orders[0].order.price == d.price
                and a.orders[0].order.order_type == 'limit' and d.order_type == 'limit'
                and a.orders[0].order.volume <= min(d.volume, abs(target_position-s.position)))
        if keep:
            return 'retained'
        if a.orders:
            r = a.orders[0]
            if not self.budget.take(now, urgent=True):
                return 'budget'
            self.cancel_pending[(instrument, r.order_id)] = r.order
            try:
                self.adapter.cancel(instrument, r.order_id)
            except Exception:
                return 'cancel_unknown'
            return 'cancel_requested'  # never insert in the same call
        if unsafe or d is None:
            return 'no_order'
        delta = target_position-s.position
        reducing = s.position * delta < 0
        if not reducing and (not a.entry_allowed or any(x is None for x in (
                c.round_trip_cost_ticks, c.gross_limit, c.position_cash_risk, c.loss_limit))):
            return 'entries_disabled'
        room = (100-s.position if d.side == 'bid' else 100+s.position)
        internal = c.cap-s.position if d.side == 'bid' else c.cap+s.position
        if reducing:
            internal = max(internal, abs(s.position))
        volume = min(d.volume, abs(delta), room, internal)
        if reducing:
            volume = min(volume, abs(s.position))
        if volume <= 0:
            return 'no_capacity'
        if d.order_type == 'ioc' and not reducing:
            return 'ioc_entry_disabled'
        if d.order_type == 'limit' and ((d.side == 'bid' and d.price >= s.ask)
                                        or (d.side == 'ask' and d.price <= s.bid)):
            return 'price_changed'
        if (d.side == 'bid' and d.price > s.ask) or (d.side == 'ask' and d.price < s.bid):
            return 'price_changed'
        if not self.budget.take(now, urgent=reducing):
            return 'budget'
        self.sequence += 1
        token = f'position-{self.sequence}'
        order = DesiredOrder(instrument, d.side, d.price, volume, d.order_type)
        self.pending[token] = order  # reserve BEFORE request, including exceptions
        try:
            self.adapter.insert(order, token)
        except Exception:
            return 'insert_unknown'
        return 'insert_requested'
