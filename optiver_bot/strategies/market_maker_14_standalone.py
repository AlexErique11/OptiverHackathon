#!/usr/bin/env python3
"""Standalone #14: persistent momentum targets and provisional jump links.

Copy THIS FILE alone into the Optiver simulator. No other project files needed.
Only Python's standard library and the simulator's installed optibook client.
No arguments / demo: offline. run: market-data dry run. run --live: orders.
Defaults: targets +/-100 per stock, clips of 5, session 600 seconds.
Zero costs and 25 API calls/second are configurable assumptions, not event rules.
Ctrl+C cancels resting orders; held positions may remain. Inspect final inventory.
"""
from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from typing import Literal
import json
import argparse

import contextlib

import csv

import io

import math

import signal

import sys

import tempfile

import time

import traceback

import unittest

from collections import defaultdict

from dataclasses import dataclass

from datetime import datetime, timezone

from decimal import Decimal

from pathlib import Path

from types import SimpleNamespace

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_r_HARD_LIMIT = 100

_r_LIMIT_GUARD = 101

_r_MAX_CONSECUTIVE_INSERT_FAILS = 10

_r_MIN_SLEEP_S = 0.2

_r_STATUS_EVERY_S = 5.0

_r_FLATTEN_ORDER_GAP_S = 0.25

_r_EPS = 1e-09

_r_EXIT_OK = 0

_r_EXIT_STARTUP = 1

_r_EXIT_LIMIT_GUARD = 2

_r_EXIT_INSERT_FAILS = 3

_r_EXIT_EXCEPTION = 4

_r_EXIT_NOT_FLAT = 5

_r_BID = 'bid'

_r_ASK = 'ask'

_r_ORDER_LIMIT = 'limit'

_r_ORDER_IOC = 'ioc'

_r_Level = Tuple[float, int]

def _r_tick_decimals(tick: float) -> int:
    exp = Decimal(str(tick)).normalize().as_tuple().exponent
    return max(0, -int(exp))

def _r_to_ticks(price: float, tick: float) -> int:
    return int(round(price / tick))

def _r_from_ticks(n: int, tick: float) -> float:
    return round(n * tick, _r_tick_decimals(tick))

def _r_on_grid(price: float, tick: float) -> bool:
    x = price / tick
    return abs(x - round(x)) <= _r_EPS

@dataclass(frozen=True)
class _r_DesiredOrder:
    side: str
    price: float
    volume: int

@dataclass(frozen=True)
class _r_OwnOrder:
    order_id: Any
    side: str
    price: float
    volume: int

@dataclass(frozen=True)
class _r_Config:
    cap: int = 60
    size: int = 5
    half_spread_ticks: float = 2.0
    skew_ticks_per_lot: float = 0.05
    wall_min: int = 500
    news_pause_s: float = 20.0

@dataclass(frozen=True)
class _r_InstrumentInfo:
    instrument_id: str
    tick_size: float
    is_stock: Optional[bool] = None
    tradable: bool = True
    price_change_limit: Optional[float] = None
    price_limit: Optional[float] = None

@dataclass(frozen=True)
class _r_Snapshot:
    """What decide() sees. Book levels are CLEANED (own resting volume removed)."""
    instrument_id: str
    bids: Sequence[_r_Level]
    asks: Sequence[_r_Level]
    position: int
    tick_size: float
    price_min: Optional[float] = None
    price_max: Optional[float] = None

@dataclass
class _r_Result:
    success: bool
    order_id: Any = None
    error: str = ''

def _r_decide(snapshot: _r_Snapshot, config: _r_Config) -> List[_r_DesiredOrder]:
    """At most one bid and one ask. Returns [] when it is not safe to quote."""
    tick = snapshot.tick_size
    if tick <= 0:
        return []
    bid_ticks = [_r_to_ticks(p, tick) for p, v in snapshot.bids if v > 0]
    ask_ticks = [_r_to_ticks(p, tick) for p, v in snapshot.asks if v > 0]
    if not bid_ticks or not ask_ticks:
        return []
    best_bid_t, best_ask_t = (max(bid_ticks), min(ask_ticks))
    if best_bid_t >= best_ask_t:
        return []
    mid_t = (best_bid_t + best_ask_t) / 2.0
    reservation_t = mid_t - config.skew_ticks_per_lot * snapshot.position
    bid_t = math.floor(reservation_t - config.half_spread_ticks + _r_EPS)
    ask_t = math.ceil(reservation_t + config.half_spread_ticks - _r_EPS)
    bid_t = min(bid_t, best_ask_t - 1)
    ask_t = max(ask_t, best_bid_t + 1)
    if bid_t >= ask_t:
        return []
    bid_vol = min(config.size, config.cap - snapshot.position)
    ask_vol = min(config.size, config.cap + snapshot.position)
    orders: List[_r_DesiredOrder] = []
    for side, t, vol in ((_r_BID, bid_t, bid_vol), (_r_ASK, ask_t, ask_vol)):
        if vol <= 0:
            continue
        price = _r_from_ticks(t, tick)
        if price <= 0:
            continue
        if snapshot.price_min is not None and price < snapshot.price_min - _r_EPS:
            continue
        if snapshot.price_max is not None and price > snapshot.price_max + _r_EPS:
            continue
        orders.append(_r_DesiredOrder(side, price, int(vol)))
    return orders

_r_MAX_WALL_GAP_TICKS = 12

def _r_decide_touch(snapshot: _r_Snapshot, config: _r_Config) -> List[_r_DesiredOrder]:
    """Quote tight around the WALL fair value instead of the visible mid.

    The exchange's own market maker rests very large orders (thousands of lots)
    symmetrically around its fair value. Fair = midpoint of the highest wall bid
    and the lowest wall ask. We quote the tightest prices that still have
    half_spread_ticks of edge against that fair (0.5 = tightest), shifted by
    inventory. No walls visible -> no quotes."""
    tick = snapshot.tick_size
    if tick <= 0:
        return []
    bids = [(_r_to_ticks(p, tick), v) for p, v in snapshot.bids if v > 0]
    asks = [(_r_to_ticks(p, tick), v) for p, v in snapshot.asks if v > 0]
    if not bids or not asks:
        return []
    best_bid_t = max((t for t, _ in bids))
    best_ask_t = min((t for t, _ in asks))
    if best_bid_t >= best_ask_t:
        return []
    wall_bids = [t for t, v in bids if v >= config.wall_min]
    wall_asks = [t for t, v in asks if v >= config.wall_min]
    if not wall_bids or not wall_asks:
        return []
    wall_bid_t, wall_ask_t = (max(wall_bids), min(wall_asks))
    if wall_bid_t >= wall_ask_t or wall_ask_t - wall_bid_t > _r_MAX_WALL_GAP_TICKS:
        return []
    fair_t = (wall_bid_t + wall_ask_t) / 2.0
    reservation_t = fair_t - config.skew_ticks_per_lot * snapshot.position
    bid_t = math.floor(reservation_t - config.half_spread_ticks + _r_EPS)
    ask_t = math.ceil(reservation_t + config.half_spread_ticks - _r_EPS)
    bid_t = min(bid_t, best_ask_t - 1)
    ask_t = max(ask_t, best_bid_t + 1)
    if bid_t >= ask_t:
        return []
    bid_vol = min(config.size, config.cap - snapshot.position)
    ask_vol = min(config.size, config.cap + snapshot.position)
    orders: List[_r_DesiredOrder] = []
    for side, t, vol in ((_r_BID, bid_t, bid_vol), (_r_ASK, ask_t, ask_vol)):
        if vol <= 0:
            continue
        price = _r_from_ticks(t, tick)
        if price <= 0:
            continue
        if snapshot.price_min is not None and price < snapshot.price_min - _r_EPS:
            continue
        if snapshot.price_max is not None and price > snapshot.price_max + _r_EPS:
            continue
        orders.append(_r_DesiredOrder(side, price, int(vol)))
    return orders

_r_STRATEGIES: Dict[str, Callable[[_r_Snapshot, _r_Config], List[_r_DesiredOrder]]] = {'touch': _r_decide_touch, 'mid': _r_decide}

def _r__levels(raw_levels: Any) -> List[_r_Level]:
    out: List[_r_Level] = []
    for lv in raw_levels or []:
        if isinstance(lv, (tuple, list)):
            price, vol = (lv[0], lv[1])
        else:
            price, vol = (lv.price, lv.volume)
        out.append((float(price), int(vol)))
    return out

def _r__norm_side(side: Any) -> str:
    s = str(getattr(side, 'value', side)).lower()
    if s in ('bid', 'buy', 'b'):
        return _r_BID
    if s in ('ask', 'sell', 's', 'offer'):
        return _r_ASK
    return s

def _r__result(resp: Any) -> _r_Result:
    if resp is None:
        return _r_Result(False, None, 'no response')
    if isinstance(resp, bool):
        return _r_Result(resp, None, '' if resp else 'returned False')
    if hasattr(resp, 'success'):
        return _r_Result(bool(resp.success), getattr(resp, 'order_id', None), str(getattr(resp, 'error_reason', '') or ''))
    if isinstance(resp, int):
        return _r_Result(True, resp)
    return _r_Result(False, None, f'unrecognised response type {type(resp).__name__}')

def _r__exc(e: BaseException) -> str:
    return f'{type(e).__name__}: {e}'

class _r_ExchangeAdapter:
    """Normalises a raw optibook Exchange (or the fake) to what the runner needs."""

    def __init__(self, raw: Any):
        self.raw = raw

    def is_connected(self) -> bool:
        try:
            return bool(self.raw.is_connected())
        except Exception:
            return False

    def instruments(self) -> Dict[str, _r_InstrumentInfo]:
        raw = self.raw.get_instruments()
        items = raw.items() if isinstance(raw, dict) else [(i.instrument_id, i) for i in raw]
        out: Dict[str, _r_InstrumentInfo] = {}
        for iid, inst in items:
            kind = getattr(inst, 'instrument_type', None)
            if kind is None:
                is_stock = None
            else:
                name = str(getattr(kind, 'name', None) or kind)
                is_stock = 'STOCK' in name.upper()
            tradable = not bool(getattr(inst, 'paused', False))
            out[iid] = _r_InstrumentInfo(instrument_id=iid, tick_size=float(getattr(inst, 'tick_size', 0) or 0), is_stock=is_stock, tradable=tradable, price_change_limit=_r__opt_float(getattr(inst, 'price_change_limit', None)), price_limit=_r__opt_float(getattr(inst, 'price_limit', None)))
        return out

    def book(self, iid: str) -> Tuple[List[_r_Level], List[_r_Level]]:
        pb = self.raw.get_last_price_book(iid)
        if pb is None:
            return ([], [])
        return (_r__levels(getattr(pb, 'bids', [])), _r__levels(getattr(pb, 'asks', [])))

    def own_orders(self, iid: str) -> List[_r_OwnOrder]:
        raw = self.raw.get_outstanding_orders(iid)
        out: List[_r_OwnOrder] = []
        for oid, st in (raw or {}).items():
            out.append(_r_OwnOrder(oid, _r__norm_side(st.side), float(st.price), int(st.volume)))
        return out

    def positions(self) -> Dict[str, int]:
        raw = self.raw.get_positions()
        return {k: int(v) for k, v in (raw or {}).items()}

    def poll_feed(self) -> List[Tuple[str, str, str]]:
        """New social media posts since the last call: (exchange timestamp, post, meta_data).
        Never raises; an exchange without a feed just returns []."""
        try:
            raw = self.raw.poll_new_social_media_feeds()
        except Exception:
            return []
        out: List[Tuple[str, str, str]] = []
        for f in raw or []:
            out.append((str(getattr(f, 'timestamp', '') or ''), str(getattr(f, 'post', '') or ''), str(getattr(f, 'meta_data', '') or '')))
        return out

    def pnl(self) -> Optional[float]:
        try:
            return float(self.raw.get_pnl())
        except Exception:
            return None

    def insert(self, iid: str, side: str, price: float, volume: int, order_type: str=_r_ORDER_LIMIT) -> _r_Result:
        try:
            resp = self.raw.insert_order(iid, price=price, volume=volume, side=side, order_type=order_type)
        except Exception as e:
            return _r_Result(False, None, _r__exc(e))
        return _r__result(resp)

    def cancel(self, iid: str, order_id: Any) -> _r_Result:
        """None is NOT treated as success: the order stays 'unconfirmed' and the
        runner re-reads outstanding orders next loop instead of inserting blindly."""
        try:
            resp = self.raw.delete_order(iid, order_id=order_id)
        except Exception as e:
            return _r_Result(False, None, _r__exc(e))
        return _r__result(resp)

    def delete_all(self, iid: str) -> _r_Result:
        try:
            resp = self.raw.delete_orders(iid)
        except Exception as e:
            return _r_Result(False, None, _r__exc(e))
        if hasattr(resp, 'success'):
            return _r_Result(bool(resp.success), None, str(getattr(resp, 'error_reason', '') or ''))
        return _r_Result(True)

def _r__opt_float(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None

def _r_connect_real_exchange() -> Any:
    """Creates the real exchange connection. Only ever called by the CLI."""
    from optibook.synchronous_client import Exchange
    exchange = Exchange()
    exchange.connect()
    return exchange

def _r_price_band(info: _r_InstrumentInfo, reference: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """VERIFY semantics against api_reference.md. Assumed here:
       price_change_limit = max absolute deviation from the reference price,
       price_limit        = absolute maximum price.
    A band that is too tight only costs missed trades, never a rejected order."""
    lo: Optional[float] = None
    hi: Optional[float] = None
    if info.price_change_limit and reference is not None:
        lo = reference - info.price_change_limit
        hi = reference + info.price_change_limit
    if info.price_limit:
        hi = info.price_limit if hi is None else min(hi, info.price_limit)
    return (lo, hi)

def _r_select_instruments(all_info: Dict[str, _r_InstrumentInfo], wanted: Optional[Sequence[str]]) -> Tuple[Dict[str, _r_InstrumentInfo], Optional[str]]:
    if wanted:
        missing = [w for w in wanted if w not in all_info]
        if missing:
            return ({}, f"unknown instrument id(s): {', '.join(missing)}")
        chosen = {w: all_info[w] for w in wanted}
        bad = [w for w, i in chosen.items() if i.is_stock is False]
        if bad:
            return ({}, f"not a stock: {', '.join(bad)}")
    else:
        chosen = {k: v for k, v in all_info.items() if v.is_stock is True}
    chosen = {k: v for k, v in chosen.items() if v.tradable and v.tick_size > 0}
    return (chosen, None)

def _r_clean_book(levels: Sequence[_r_Level], own: Sequence[_r_OwnOrder], side: str, tick: float) -> List[_r_Level]:
    """Subtract our own resting volume per price level; drop empty levels.
    Returns bids sorted best-first (desc) or asks best-first (asc)."""
    agg: Dict[int, int] = defaultdict(int)
    for p, v in levels:
        agg[_r_to_ticks(p, tick)] += int(v)
    for o in own:
        if o.side == side:
            agg[_r_to_ticks(o.price, tick)] -= int(o.volume)
    keys = sorted((k for k, v in agg.items() if v > 0), reverse=side == _r_BID)
    return [(_r_from_ticks(k, tick), agg[k]) for k in keys]

def _r_clamp_orders(orders: Sequence[_r_DesiredOrder], position: int, cap: int, tick: float, best_bid: Optional[float], best_ask: Optional[float], price_min: Optional[float]=None, price_max: Optional[float]=None, label: str='') -> List[_r_DesiredOrder]:
    """Always applied, independent of the strategy. Buy and sell room are separate."""
    rooms = {_r_BID: cap - position, _r_ASK: cap + position}
    out: List[_r_DesiredOrder] = []

    def drop(o: _r_DesiredOrder, why: str) -> None:
        print(f'CLAMP DROP {label} {o.side} {o.volume}@{o.price}: {why}')
    for o in orders:
        if o.side not in rooms:
            drop(o, 'unknown side')
            continue
        vol = min(int(o.volume), rooms[o.side])
        if vol <= 0:
            continue
        if o.price <= 0 or not _r_on_grid(o.price, tick):
            drop(o, 'price off tick grid')
        elif price_min is not None and o.price < price_min - _r_EPS:
            drop(o, 'below price band')
        elif price_max is not None and o.price > price_max + _r_EPS:
            drop(o, 'above price band')
        elif o.side == _r_BID and best_ask is not None and (_r_to_ticks(o.price, tick) >= _r_to_ticks(best_ask, tick)):
            drop(o, 'would cross best ask')
        elif o.side == _r_ASK and best_bid is not None and (_r_to_ticks(o.price, tick) <= _r_to_ticks(best_bid, tick)):
            drop(o, 'would cross best bid')
        else:
            out.append(_r_DesiredOrder(o.side, o.price, vol))
            rooms[o.side] -= vol
    return out

@dataclass
class _r_RunState:
    iteration: int = 0
    consecutive_insert_failures: int = 0
    inserts: int = 0
    cancels: int = 0
    failures: int = 0
    last_status_t: Optional[float] = None
    positions: Optional[Dict[str, int]] = None
    pnl: Optional[float] = None
    pause_until: float = 0.0
    posts_seen: int = 0
    feed_writer: Any = None

class _r_LiveExecutor:

    def __init__(self, adapter: _r_ExchangeAdapter):
        self.adapter = adapter

    def insert(self, iid: str, side: str, price: float, volume: int) -> _r_Result:
        return self.adapter.insert(iid, side, price, volume, _r_ORDER_LIMIT)

    def cancel(self, iid: str, order: _r_OwnOrder) -> _r_Result:
        return self.adapter.cancel(iid, order.order_id)

class _r_DryExecutor:
    """Pretends. Keeps a simulated set of resting orders so repeated identical
    quotes are not re-printed every loop. Never touches the exchange."""

    def __init__(self) -> None:
        self.sim: Dict[str, Dict[int, _r_OwnOrder]] = defaultdict(dict)
        self._next = 1

    def orders_for(self, iid: str) -> List[_r_OwnOrder]:
        return list(self.sim[iid].values())

    def insert(self, iid: str, side: str, price: float, volume: int) -> _r_Result:
        oid = self._next
        self._next += 1
        self.sim[iid][oid] = _r_OwnOrder(oid, side, price, volume)
        print(f'[DRY RUN] would INSERT {iid} {side} {volume}@{price}')
        return _r_Result(True, oid)

    def cancel(self, iid: str, order: _r_OwnOrder) -> _r_Result:
        self.sim[iid].pop(order.order_id, None)
        print(f'[DRY RUN] would CANCEL {iid} {order.side} {order.volume}@{order.price}')
        return _r_Result(True)

def _r__reconcile_side(executor: Any, iid: str, side: str, existing: List[_r_OwnOrder], desired: List[_r_DesiredOrder], room: int, tick: float, state: _r_RunState) -> None:
    remaining = list(existing)
    kept: List[_r_OwnOrder] = []
    to_insert: List[_r_DesiredOrder] = []
    for d in desired:
        match = next((o for o in remaining if _r_to_ticks(o.price, tick) == _r_to_ticks(d.price, tick) and o.volume <= d.volume), None)
        if match is not None:
            remaining.remove(match)
            kept.append(match)
        else:
            to_insert.append(d)
    cancel_failed = False
    kept_volume = sum((o.volume for o in kept))
    for o in remaining:
        res = executor.cancel(iid, o)
        if res.success:
            state.cancels += 1
        else:
            state.failures += 1
            cancel_failed = True
            kept_volume += o.volume
            print(f'CANCEL FAILED {iid} {side} {o.volume}@{o.price}: {res.error}')
    if cancel_failed:
        return
    for d in to_insert:
        if kept_volume + d.volume > room:
            print(f'SKIP INSERT {iid} {side} {d.volume}@{d.price}: would exceed room {room}')
            continue
        res = executor.insert(iid, side, d.price, d.volume)
        if res.success:
            state.inserts += 1
            state.consecutive_insert_failures = 0
            kept_volume += d.volume
        else:
            state.failures += 1
            state.consecutive_insert_failures += 1
            print(f'INSERT FAILED {iid} {side} {d.volume}@{d.price}: {res.error}')

def _r_reconcile_instrument(executor: Any, iid: str, resting: List[_r_OwnOrder], desired: List[_r_DesiredOrder], position: int, cap: int, tick: float, state: _r_RunState) -> None:
    rooms = {_r_BID: cap - position, _r_ASK: cap + position}
    for side in (_r_BID, _r_ASK):
        _r__reconcile_side(executor, iid, side, [o for o in resting if o.side == side], [d for d in desired if d.side == side], rooms[side], tick, state)

_r_CSV_HEADER = ['utc_time', 'instrument', 'position', 'clean_best_bid', 'clean_best_ask', 'our_bid', 'our_ask', 'pnl']

def _r__utc_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

def _r_run_iteration(adapter: _r_ExchangeAdapter, executor: Any, instruments: Dict[str, _r_InstrumentInfo], cfg: _r_Config, state: _r_RunState, decide_fn: Callable[[_r_Snapshot, _r_Config], List[_r_DesiredOrder]]=_r_decide, csv_writer: Any=None, dry_run: bool=False) -> Optional[int]:
    """One loop. Returns None to continue, or an exit code to stop."""
    if not adapter.is_connected():
        print('DISCONNECTED: exchange connection lost. Shutting down (never reconnecting).')
        return _r_EXIT_STARTUP
    posts = adapter.poll_feed()
    if posts:
        if not dry_run and cfg.news_pause_s > 0:
            for iid in instruments:
                adapter.delete_all(iid)
        state.posts_seen += len(posts)
        state.pause_until = time.monotonic() + cfg.news_pause_s
        for ts, post, meta in posts:
            print(f'NEWS [{ts}] {post}' + (f' | meta={meta}' if meta else ''))
            if state.feed_writer is not None:
                state.feed_writer.writerow([_r__utc_iso(), ts, post, meta])
        if cfg.news_pause_s > 0:
            print(f'NEWS GUARD: no quotes for {cfg.news_pause_s:.0f}s')
    paused = time.monotonic() < state.pause_until
    positions = adapter.positions()
    pnl = adapter.pnl()
    state.positions = {iid: int(positions.get(iid, 0)) for iid in instruments}
    state.pnl = pnl
    for iid, info in instruments.items():
        tick = info.tick_size
        pos = state.positions[iid]
        raw_bids, raw_asks = adapter.book(iid)
        real_own = adapter.own_orders(iid)
        bids = _r_clean_book(raw_bids, real_own, _r_BID, tick)
        asks = _r_clean_book(raw_asks, real_own, _r_ASK, tick)
        reference = None
        if raw_bids and raw_asks:
            reference = (max((p for p, _ in raw_bids)) + min((p for p, _ in raw_asks))) / 2.0
        price_min, price_max = _r_price_band(info, reference)
        desired: List[_r_DesiredOrder] = []
        if bids and asks and (not paused):
            snap = _r_Snapshot(iid, bids, asks, pos, tick, price_min, price_max)
            desired = list(decide_fn(snap, cfg))
        clamped = _r_clamp_orders(desired, pos, cfg.cap, tick, bids[0][0] if bids else None, asks[0][0] if asks else None, price_min, price_max, label=iid)
        resting = executor.orders_for(iid) if dry_run else real_own
        _r_reconcile_instrument(executor, iid, resting, clamped, pos, cfg.cap, tick, state)
        if csv_writer is not None:
            our_bid = max((d.price for d in clamped if d.side == _r_BID), default='')
            our_ask = min((d.price for d in clamped if d.side == _r_ASK), default='')
            csv_writer.writerow([_r__utc_iso(), iid, pos, bids[0][0] if bids else '', asks[0][0] if asks else '', our_bid, our_ask, '' if pnl is None else pnl])
    worst = [iid for iid, p in state.positions.items() if abs(p) >= _r_LIMIT_GUARD]
    if worst:
        print(f"LIMIT GUARD: |position| >= {_r_LIMIT_GUARD} in {', '.join(worst)}. Shutting down.")
        return _r_EXIT_LIMIT_GUARD
    if state.consecutive_insert_failures >= _r_MAX_CONSECUTIVE_INSERT_FAILS:
        print(f'{state.consecutive_insert_failures} consecutive failed inserts (rate limit or bad prices?). Shutting down instead of hammering the exchange.')
        return _r_EXIT_INSERT_FAILS
    return None

def _r_print_status(state: _r_RunState) -> None:
    pos = ' '.join((f'{k}={v}' for k, v in (state.positions or {}).items()))
    pnl = 'n/a' if state.pnl is None else f'{state.pnl:.2f}'
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] PnL={pnl} | pos {pos} | inserts={state.inserts} cancels={state.cancels} failures={state.failures}")
    state.inserts = state.cancels = state.failures = 0

def _r_cleanup(adapter: _r_ExchangeAdapter, instrument_ids: Sequence[str], live: bool) -> None:
    if live:
        for iid in instrument_ids:
            try:
                res = adapter.delete_all(iid)
                if not res.success:
                    print(f'CLEANUP: delete-all failed for {iid}: {res.error}')
            except Exception as e:
                print(f'CLEANUP: delete-all raised for {iid}: {_r__exc(e)}')
    try:
        if adapter.is_connected():
            positions = adapter.positions()
            shown = {k: v for k, v in positions.items() if k in set(instrument_ids)}
            pnl = adapter.pnl()
            print(f"FINAL positions: {shown}  PnL: {('n/a' if pnl is None else f'{pnl:.2f}')}")
    except Exception as e:
        print(f'CLEANUP: could not read final state: {_r__exc(e)}')
    if not live:
        print('Dry run: nothing was inserted or cancelled.')

def _r_run_bot(adapter: _r_ExchangeAdapter, instruments: Dict[str, _r_InstrumentInfo], cfg: _r_Config, dry_run: bool, sleep_s: float, log_path: Path, decide_fn: Callable[[_r_Snapshot, _r_Config], List[_r_DesiredOrder]]=_r_decide, max_iterations: Optional[int]=None, sleep_fn: Callable[[float], None]=time.sleep) -> int:
    executor: Any = _r_DryExecutor() if dry_run else _r_LiveExecutor(adapter)
    state = _r_RunState()
    exit_code = _r_EXIT_OK
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path = log_path.with_name('feed_' + log_path.name)
        with open(log_path, 'w', newline='') as fh, open(feed_path, 'w', newline='') as feed_fh:
            writer = csv.writer(fh)
            writer.writerow(_r_CSV_HEADER)
            state.feed_writer = csv.writer(feed_fh)
            state.feed_writer.writerow(['utc_received', 'exchange_timestamp', 'post', 'meta_data'])
            state.last_status_t = time.monotonic()
            while True:
                code = _r_run_iteration(adapter, executor, instruments, cfg, state, decide_fn, writer, dry_run)
                fh.flush()
                feed_fh.flush()
                if code is not None:
                    exit_code = code
                    break
                state.iteration += 1
                if time.monotonic() - state.last_status_t >= _r_STATUS_EVERY_S:
                    _r_print_status(state)
                    state.last_status_t = time.monotonic()
                if max_iterations is not None and state.iteration >= max_iterations:
                    break
                sleep_fn(max(_r_MIN_SLEEP_S, sleep_s))
    except KeyboardInterrupt:
        print('Interrupted (Ctrl+C / SIGTERM): shutting down.')
    except Exception:
        traceback.print_exc()
        exit_code = _r_EXIT_EXCEPTION
    finally:
        _r_cleanup(adapter, list(instruments), live=not dry_run)
    return exit_code

def _r__raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt

def _r__parse_ids(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    return [x.strip() for x in s.split(',') if x.strip()]

_runtime = SimpleNamespace(HARD_LIMIT=_r_HARD_LIMIT, LIMIT_GUARD=_r_LIMIT_GUARD, MAX_CONSECUTIVE_INSERT_FAILS=_r_MAX_CONSECUTIVE_INSERT_FAILS, MIN_SLEEP_S=_r_MIN_SLEEP_S, STATUS_EVERY_S=_r_STATUS_EVERY_S, FLATTEN_ORDER_GAP_S=_r_FLATTEN_ORDER_GAP_S, EPS=_r_EPS, EXIT_OK=_r_EXIT_OK, EXIT_STARTUP=_r_EXIT_STARTUP, EXIT_LIMIT_GUARD=_r_EXIT_LIMIT_GUARD, EXIT_INSERT_FAILS=_r_EXIT_INSERT_FAILS, EXIT_EXCEPTION=_r_EXIT_EXCEPTION, EXIT_NOT_FLAT=_r_EXIT_NOT_FLAT, BID=_r_BID, ASK=_r_ASK, ORDER_LIMIT=_r_ORDER_LIMIT, ORDER_IOC=_r_ORDER_IOC, Level=_r_Level, tick_decimals=_r_tick_decimals, to_ticks=_r_to_ticks, from_ticks=_r_from_ticks, on_grid=_r_on_grid, DesiredOrder=_r_DesiredOrder, OwnOrder=_r_OwnOrder, Config=_r_Config, InstrumentInfo=_r_InstrumentInfo, Snapshot=_r_Snapshot, Result=_r_Result, decide=_r_decide, MAX_WALL_GAP_TICKS=_r_MAX_WALL_GAP_TICKS, decide_touch=_r_decide_touch, STRATEGIES=_r_STRATEGIES, _levels=_r__levels, _norm_side=_r__norm_side, _result=_r__result, _exc=_r__exc, ExchangeAdapter=_r_ExchangeAdapter, _opt_float=_r__opt_float, connect_real_exchange=_r_connect_real_exchange, price_band=_r_price_band, select_instruments=_r_select_instruments, clean_book=_r_clean_book, clamp_orders=_r_clamp_orders, RunState=_r_RunState, LiveExecutor=_r_LiveExecutor, DryExecutor=_r_DryExecutor, _reconcile_side=_r__reconcile_side, reconcile_instrument=_r_reconcile_instrument, CSV_HEADER=_r_CSV_HEADER, _utc_iso=_r__utc_iso, run_iteration=_r_run_iteration, print_status=_r_print_status, cleanup=_r_cleanup, run_bot=_r_run_bot, _raise_keyboard_interrupt=_r__raise_keyboard_interrupt, _parse_ids=_r__parse_ids)

def _f_load_runtime():
    return _runtime

@dataclass(frozen=True)
class SharedSnapshot:
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
    instrument_id: str
    side: Literal['bid', 'ask']
    price: float
    volume: int
    order_type: Literal['limit', 'ioc'] = 'limit'

@dataclass(frozen=True)
class _f_Config:
    cap: int = 20
    size: int = 3
    cost_ticks: float | None = None
    min_net_ticks: float = 0.5
    soft_fraction: float = 0.8
    inventory_widen_ticks: float = 2.0
    max_age: float = 0.5
    shock_ticks: float = 5.0
    cooldown: float = 5.0

    @property
    def news_pause_s(self):
        """Compatibility with the renamed shared #4 runner's news guard."""
        return self.cooldown

    def __post_init__(self):
        if type(self.cap) is not int or not 1 <= self.cap <= 100:
            raise ValueError('cap must be an integer between 1 and 100')
        if type(self.size) is not int or self.size < 1:
            raise ValueError('size must be a positive integer')
        for name in ('min_net_ticks', 'soft_fraction', 'max_age', 'shock_ticks', 'cooldown'):
            value = getattr(self, name)
            if not _f_finite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if self.soft_fraction > 1 or not _f_finite(self.inventory_widen_ticks) or self.inventory_widen_ticks < 0:
            raise ValueError('invalid inventory controls')
        if self.cost_ticks is not None and (not _f_finite(self.cost_ticks) or self.cost_ticks < 0):
            raise ValueError('cost_ticks must be an explicit nonnegative round-trip estimate')

def _f_finite(value):
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and math.isfinite(value)

def _f_rooms(position, buys, sells, cap):
    if any((type(v) is not int for v in (position, buys, sells, cap))):
        raise ValueError('positions, pending volumes and cap must be integers')
    if not 1 <= cap <= 100 or abs(position) > 100 or min(buys, sells) < 0:
        raise ValueError('invalid position/cap/pending volume')
    return (max(0, cap - position - buys), max(0, cap + position - sells))

class _f_RequestBudget:
    """Conservative rolling one-second limit on ALL raw API method calls."""

    def __init__(self, limit, clock=time.monotonic, sleep=time.sleep):
        if type(limit) is not int or limit <= 0:
            raise ValueError('request budget must be a positive integer from event rules')
        self.limit, self.clock, self.sleep = (limit, clock, sleep)
        self.events = deque()

    def acquire(self):
        while True:
            now = self.clock()
            while self.events and now - self.events[0] >= 1:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now)
                return
            self.sleep(min(0.1, max(0.001, 1 - (now - self.events[0]))))

class _f_BudgetedRaw:

    def __init__(self, raw, budget):
        self.raw, self.budget = (raw, budget)

    def __getattr__(self, name):
        value = getattr(self.raw, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            self.budget.acquire()
            return value(*args, **kwargs)
        return call

def _f_guarded_adapter(raw, config, max_order_size, clock=time.monotonic):
    """Wrap ONE already-connected raw client; never create another connection."""
    runtime = _f_load_runtime()

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
                p1 = self.positions().get(iid, 0)
                pending = self.own_orders(iid)
                p2 = self.positions().get(iid, 0)
                if p1 != p2:
                    return runtime.Result(False, None, 'position changed during risk snapshot')
                buys = sum((o.volume for o in pending if o.side == 'bid'))
                sells = sum((o.volume for o in pending if o.side == 'ask'))
                buy_room, sell_room = _f_rooms(p2, buys, sells, config.cap)
                if p2 + buys > 100 or p2 - sells < -100:
                    return runtime.Result(False, None, 'existing orders violate hard bounds')
                if volume > (buy_room if side == 'bid' else sell_room):
                    return runtime.Result(False, None, 'fresh independent capacity exhausted')
                bids, asks = self.book(iid)
                if not _f_finite(price) or price <= 0 or (not bids) or (not asks):
                    return runtime.Result(False, None, 'invalid price or empty fresh book')
                if side == 'bid' and price >= min((p for p, v in asks if v > 0)) or (side == 'ask' and price <= max((p for p, v in bids if v > 0))):
                    return runtime.Result(False, None, 'quote would cross refreshed book')
                return super().insert(iid, side, price, volume, order_type)
            except Exception as exc:
                return runtime.Result(False, None, f'pre-insert check failed: {type(exc).__name__}')
    return Adapter()

@dataclass(frozen=True)
class Config(_f_Config):
    cap: int = 100
    size: int = 5
    target: int = 100
    lookback: float = 20.0
    fast_window: float = 5.0
    min_fast_ticks: float = 1.0
    min_slow_ticks: float = 3.0
    confirmations: int = 3
    min_samples: int = 8
    max_gap: float = 3.0
    max_spread_ticks: float = 2.0
    signal_grace: float = 10.0
    max_hold: float = 60.0
    stop_ticks: float = 5.0
    reentry_delay: float = 2.0

    def __post_init__(self):
        super().__post_init__()
        if type(self.target) is not int or not 1 <= self.target <= self.cap:
            raise ValueError('Require integer 1 <= target <= cap <= 100')
        for name in ('lookback', 'fast_window', 'min_fast_ticks', 'min_slow_ticks', 'max_gap', 'max_spread_ticks', 'signal_grace', 'max_hold', 'stop_ticks', 'reentry_delay'):
            if not _f_finite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive and finite')
        if not self.fast_window < self.lookback or self.max_gap >= self.fast_window:
            raise ValueError('Invalid history windows/gaps')
        if any((type(v) is not int or v < 2 for v in (self.confirmations, self.min_samples))):
            raise ValueError('Confirmation/sample counts must be integers >=2')

@dataclass(frozen=True)
class Snapshot(SharedSnapshot):
    target_position: int = 0
    reducing: bool = False

def decide(s, c):
    """Desired TOTAL remaining quote; execution rechecks target after fills."""
    buy_room, sell_room = _f_rooms(s.position, s.outstanding_buy, s.outstanding_sell, c.cap)
    if not isinstance(s, Snapshot) or type(s.target_position) is not int or abs(s.target_position) > c.cap:
        raise ValueError(f'Invalid target snapshot: type={type(s).__name__}, target={getattr(s, "target_position", None)!r}, cap={c.cap!r}')
    if not s.instrument_id or not all((_f_finite(v) for v in (s.bid, s.ask, s.tick_size, s.book_age_seconds))) or s.bid <= 0 or (s.ask <= s.bid) or (s.tick_size <= 0) or (not 0 <= s.book_age_seconds <= c.max_age):
        return []
    bid, ask, tick = (Decimal(str(v)) for v in (s.bid, s.ask, s.tick_size))
    if bid % tick or ask % tick:
        return []
    if s.position + s.outstanding_buy > 100 or s.position - s.outstanding_sell < -100:
        return []
    delta = s.target_position - s.position
    if not delta:
        return []
    reducing = s.position * delta < 0
    if s.reducing and (not reducing):
        return []
    distance = min(abs(delta), abs(s.position)) if reducing else abs(delta)
    side = 'bid' if delta > 0 else 'ask'
    if not reducing:
        if c.cost_ticks is None or (ask - bid) / tick > Decimal(str(c.max_spread_ticks)):
            return []
        if Decimal(str(c.min_slow_ticks)) <= Decimal(str(c.cost_ticks)) + Decimal(str(c.min_net_ticks)):
            return []
    reserved = s.outstanding_buy if side == 'bid' else s.outstanding_sell
    room = buy_room if side == 'bid' else sell_room
    volume = min(c.size, distance)
    volume = min(volume, room + min(reserved, volume))
    if volume <= 0:
        return []
    price = bid if side == 'bid' else ask
    if not reducing and ask - bid >= 2 * tick:
        price = min(bid + tick, ask - tick) if side == 'bid' else max(ask - tick, bid + tick)
    return [DesiredOrder(s.instrument_id, side, float(price), volume)]

def signal_direction(history, now, c, tick):
    """Causal independent per-stock momentum; no ticker/colour/leader input."""
    if len(history) < c.min_samples:
        return 0
    if now - history[0][0] < c.lookback or now - history[0][0] > c.lookback + c.max_gap:
        return 0
    if any((not 0 < history[i + 1][0] - history[i][0] <= c.max_gap for i in range(len(history) - 1))):
        return 0
    oldest = history[0][1]
    current = history[-1][1]
    fast_points = [p for t, p in history if t <= now - c.fast_window]
    if not fast_points:
        return 0
    fast = (current - fast_points[-1]) / tick
    slow = (current - oldest) / tick
    changes = [b[1] - a[1] for a, b in zip(history, list(history)[1:]) if abs(b[1] - a[1]) > 1e-09]
    if len(changes) < c.confirmations:
        return 0
    for direction in (-1, 1):
        if direction * fast >= c.min_fast_ticks - 1e-09 and direction * slow >= c.min_slow_ticks - 1e-09 and all((direction * x > 0 for x in changes[-c.confirmations:])):
            return direction
    return 0

class DeadlineRaw:
    """Deadline checked after the request limiter has finished waiting."""

    def __init__(self, raw, deadline, clock=time.monotonic):
        self.raw, self.deadline, self.clock = (raw, deadline, clock)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def insert_order(self, iid, **kwargs):
        if self.clock() >= self.deadline:
            raise ValueError('Session deadline reached')
        return self.raw.insert_order(iid, **kwargs)

class TargetRaw:
    """Final target-distance barrier on the same budgeted client."""

    def __init__(self, raw, deadline=float('inf'), clock=time.monotonic):
        self.raw, self.targets, self.reduce_only = (raw, {}, {})
        self.deadline, self.clock = (deadline, clock)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def insert_order(self, iid, **kwargs):
        if self.clock() >= self.deadline:
            raise ValueError('Session deadline reached')
        if iid not in self.targets:
            raise ValueError('No approved current target')
        p1 = self.raw.get_positions().get(iid, 0)
        orders = self.raw.get_outstanding_orders(iid) or {}
        p2 = self.raw.get_positions().get(iid, 0)
        if p1 != p2:
            raise ValueError('Position changed during final target check')
        delta = self.targets[iid] - p2
        side, volume = (kwargs['side'], kwargs['volume'])
        if not delta or side != ('bid' if delta > 0 else 'ask'):
            raise ValueError('Target already reached/changed')
        rt = _f_load_runtime()
        same = sum((o.volume for o in orders.values() if rt._norm_side(o.side) == side))
        if any((rt._norm_side(o.side) != side for o in orders.values())):
            raise ValueError('Confirm cancellation of opposing orders first')
        allowance = abs(delta)
        if p2 * delta < 0:
            allowance = min(allowance, abs(p2))
        if self.reduce_only.get(iid, False):
            if p2 * delta >= 0:
                raise ValueError('Exit cannot open new exposure')
            allowance = min(allowance, abs(p2))
        if same + volume > allowance:
            raise ValueError('Pending/new orders exceed remaining target')
        return self.raw.insert_order(iid, **kwargs)

@contextmanager
def target_runtime_guard(runtime):
    """Permit legal +/-100 targets in this one runner, restore even on failure.

    The shared runner's early stop at 90 is inappropriate for target=100
    mode. Fresh per-insert cap/target checks remain mandatory and unchanged.
    """
    previous = runtime.LIMIT_GUARD
    runtime.LIMIT_GUARD = 101
    try:
        yield
    finally:
        runtime.LIMIT_GUARD = previous

class Controller:

    def __init__(self, adapter, target_raw, deadline, clock=time.monotonic, logger=None):
        self.adapter, self.target_raw, self.deadline, self.clock, self.logger = (adapter, target_raw, deadline, clock, logger)
        self.state = {}

    def direction(self, iid, history, now, c, tick):
        return signal_direction(history, now, c, tick)

    def __call__(self, snap, c):
        bids = [p for p, v in snap.bids if v > 0]
        asks = [p for p, v in snap.asks if v > 0]
        if not bids or not asks:
            return []
        bid, ask = (max(bids), min(asks))
        if not all((_f_finite(v) for v in (bid, ask, snap.tick_size))) or bid <= 0 or ask <= bid or (snap.tick_size <= 0):
            return []
        now, mid = (self.clock(), (bid + ask) / 2)
        iid = snap.instrument_id
        s = self.state.setdefault(iid, dict(history=deque(), target=0, position=0, opened=None, reference=None, last_support=None, pause=0.0, exit=False, flat_at=None, report=None))
        history = s['history']
        reset = False
        if history:
            gap = now - history[-1][0]
            shock = abs(mid - history[-1][1]) >= c.shock_ticks * snap.tick_size - 1e-09
            if gap <= 0 or gap > c.max_gap or shock:
                history.clear()
                reset = True
                if shock:
                    s['pause'] = now + c.cooldown
        while len(history) > 1 and history[1][0] <= now - c.lookback:
            history.popleft()
        history.append((now, mid))
        direction = self.direction(iid, history, now, c, snap.tick_size)
        if not snap.position:
            if s['position']:
                s['flat_at'] = now
                s['target'] = 0
            s['opened'], s['reference'], s['exit'] = (None, None, False)
        elif not s['position'] or snap.position * s['position'] < 0:
            s['opened'], s['reference'] = (now, mid)
            if not s['target'] or s['target'] * snap.position < 0:
                s['exit'] = True
        if direction and direction * s['target'] > 0:
            s['last_support'] = now
        if snap.position:
            adverse = (s['reference'] - mid if snap.position > 0 else mid - s['reference']) / snap.tick_size
            if reset or now < s['pause'] or now >= self.deadline - 15 or (now - s['opened'] >= c.max_hold) or (adverse >= c.stop_ticks - 1e-09) or (direction * snap.position < 0) or (s['last_support'] is None) or (now - s['last_support'] >= c.signal_grace):
                s['exit'] = True
            if s['exit']:
                s['target'] = 0
        else:
            eligible = not reset and now >= s['pause'] and (now < self.deadline - 30) and (s['flat_at'] is None or now - s['flat_at'] >= c.reentry_delay)
            if eligible and direction:
                s['target'] = direction * c.target
                s['last_support'] = now
            else:
                s['target'] = 0
        pending = self.adapter.own.get(iid, [])
        own = Snapshot(iid, bid, ask, snap.tick_size, snap.position, sum((o.volume for o in pending if o.side == 'bid')), sum((o.volume for o in pending if o.side == 'ask')), max(0, now - self.adapter.received.get(iid, float('-inf'))), s['target'], s['exit'])
        self.target_raw.targets[iid] = s['target']
        self.target_raw.reduce_only[iid] = s['exit']
        orders = decide(own, c)
        report = (s['target'], snap.position, direction, s['exit'])
        if self.logger and report != s['report']:
            self.logger(dict(event='target_state', time=now, instrument=iid, target=s['target'], position=snap.position, signal=direction, exit=s['exit'], quotes=[o.__dict__ for o in orders]))
        s['position'], s['report'] = (snap.position, report)
        return orders

def target_main(argv=None, exchange_factory=None, controller_factory=Controller):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command')
    sub.add_parser('demo')
    run = sub.add_parser('run', help='one connection; orders dry-run unless --live')
    run.add_argument('--live', action='store_true')
    run.add_argument('--cap', type=int, default=100)
    run.add_argument('--target', type=int, default=100)
    run.add_argument('--size', type=int, default=5)
    run.add_argument('--cost-ticks', type=float, required=True)
    run.add_argument('--max-requests', type=int, required=True)
    run.add_argument('--max-order-size', type=int, required=True)
    run.add_argument('--session-seconds', type=float, required=True)
    run.add_argument('--instruments', default='AMD')
    run.add_argument('--sleep', type=float, default=0.2)
    run.add_argument('--log-dir', default='logs')
    args = p.parse_args(argv)
    if args.command != 'run':
        c = Config(cost_ticks=0)
        for pos, target in ((0, 100), (95, 100), (100, 100), (100, -100), (0, -100), (-100, -100)):
            s = Snapshot('SYNTHETIC', 100.0, 100.1, 0.1, pos, target_position=target)
            print('OFFLINE', f'position={pos} target={target}', decide(s, c))
        return 0
    try:
        c = Config(cap=args.cap, target=args.target, size=args.size, cost_ticks=args.cost_ticks)
        budget = _f_RequestBudget(args.max_requests)
        if not 1 <= args.size <= args.max_order_size or not _f_finite(args.session_seconds) or args.session_seconds <= 30:
            raise ValueError('Invalid order bound/session duration')
        if not _f_finite(args.sleep) or args.sleep < 0:
            raise ValueError('Invalid sleep')
    except ValueError as exc:
        p.error(str(exc))
    rt = _f_load_runtime()
    start = time.monotonic()
    deadline = start + args.session_seconds
    raw = (exchange_factory or rt.connect_real_exchange)()
    target_raw = TargetRaw(_f_BudgetedRaw(DeadlineRaw(raw, deadline), budget), deadline)
    adapter = _f_guarded_adapter(target_raw, c, args.max_order_size)
    if not adapter.is_connected():
        return rt.EXIT_STARTUP
    instruments, error = rt.select_instruments(adapter.instruments(), rt._parse_ids(args.instruments))
    if error or not instruments:
        print(error or 'No tradable stocks')
        return rt.EXIT_STARTUP
    signal.signal(signal.SIGTERM, rt._raise_keyboard_interrupt)
    directory = Path(args.log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    print('LIVE target orders' if args.live else 'DRY target orders; one real connection')
    print('Target', c.target, 'per stock; stop other bots. No guaranteed profit or fill.')
    with (directory / f'standalone14_{stamp}.jsonl').open('w') as events:

        def logger(event):
            events.write(json.dumps(event) + '\n')
            events.flush()
        controller = controller_factory(adapter, target_raw, deadline, logger=logger)

        def limited_sleep(seconds):
            if time.monotonic() >= deadline:
                raise KeyboardInterrupt
            time.sleep(min(seconds, max(0, deadline - time.monotonic())))
        with target_runtime_guard(rt):
            return rt.run_bot(adapter, instruments, c, dry_run=not args.live, sleep_s=max(rt.MIN_SLEEP_S, args.sleep), log_path=directory / f'run14_{stamp}.csv', decide_fn=controller, sleep_fn=limited_sleep)

PRIORS = {('MU', 'CRWV'): .57, ('NVDA', 'SNDK'): 1., ('AVGO', 'NBIS'): .7}

def clip(x, lo, hi):
    return max(lo, min(hi, x))

class HybridController(Controller):
    """Bounded causal co-jump learning, with independent per-stock targets."""

    def __init__(self, *args, use_priors=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.short = {}
        self.links = {k: dict(beta=v, n=0, src='prior') for k, v in PRIORS.items()} if use_priors else {}
        self.last_trigger = {}
        self.events = deque(maxlen=256)
        self.jump_support = {}
        self.latest = {}

    def direction(self, iid, history, now, c, tick):
        momentum = super().direction(iid, history, now, c, tick)
        mid = history[-1][1]
        old = self.latest.get(iid)
        if old and (now - old[0] <= 0 or now - old[0] > c.max_gap):
            self.short.pop(iid, None)
            self.jump_support.pop(iid, None)
        self.latest[iid] = (now, mid, tick)
        h = self.short.setdefault(iid, deque())
        h.append((now, mid))
        while len(h) > 1 and h[1][0] <= now - 1.0:
            h.popleft()
        move = (mid - h[0][1]) / tick if len(h) > 1 and now - h[0][0] <= 1.0 + c.max_gap else 0.0
        while self.events and now >= self.events[0]['t']:
            event = self.events.popleft()
            for follower, (t0, p0, ftick) in event['at'].items():
                latest = self.latest.get(follower)
                if not latest or now - latest[0] > c.max_gap or latest[0] < event['t'] - c.max_gap:
                    continue
                change = latest[1] - p0
                if abs(change) / ftick < 4:
                    continue
                beta = clip(change / p0 / event['ret'], -1.5, 1.5)
                if abs(beta) < 0.15:
                    continue
                key = (event['leader'], follower)
                link = self.links.get(key)
                if link is None:
                    link = self.links[key] = dict(beta=beta, n=0, src='cojump')
                elif link['beta'] * beta > 0:
                    link['beta'] = (link['beta'] + beta) / 2
                else:
                    link.update(beta=beta, n=-1, src='cojump')
                link['n'] += 1
        if abs(move) >= 5.0 - 1e-09 and now - self.last_trigger.get(iid, -1000000000.0) >= 15.0:
            self.last_trigger[iid] = now
            ret = (mid - h[0][1]) / h[0][1]
            fresh = {i: v for i, v in self.latest.items() if i != iid and now - v[0] <= c.max_gap}
            self.events.append(dict(t=now + 10.0, leader=iid, ret=ret, at=fresh))
            for (leader, follower), link in self.links.items():
                if leader != iid or (link['src'] != 'prior' and link['n'] < 2):
                    continue
                if follower not in fresh:
                    continue
                _, fmid, ftick = fresh[follower]
                fh = self.short.get(follower)
                if not fh:
                    continue
                expected = link['beta'] * ret * fmid / ftick
                done = (fmid - fh[0][1]) / ftick
                remaining = expected - done
                hurdle = max(3.0, (c.cost_ticks or 0) + c.max_spread_ticks + c.min_net_ticks)
                if remaining * expected > 0 and abs(remaining) >= hurdle:
                    self.jump_support[follower] = (now + 10.0, 1 if expected > 0 else -1, fmid + remaining * ftick)
        support = self.jump_support.get(iid)
        hurdle = max(3.0, (c.cost_ticks or 0) + c.max_spread_ticks + c.min_net_ticks)
        follower_direction = support[1] if support and now < support[0] and (support[1] * (support[2] - mid) / tick >= hurdle) else 0
        if momentum and follower_direction and (momentum != follower_direction):
            return 0
        return follower_direction or momentum

def main(argv=None, exchange_factory=None):
    args = list(sys.argv[1:] if argv is None else argv)
    use_priors = '--no-priors' not in args
    if not use_priors:
        args.remove('--no-priors')
    if args and args[0] == 'run':
        defaults = {'--cost-ticks': '0', '--max-requests': '25', '--max-order-size': '5', '--session-seconds': '600', '--instruments': 'AMD,AVGO,CRWV,MU,NBIS,NVDA,SNDK,VRT'}
        for flag, value in defaults.items():
            if not any((a == flag or a.startswith(flag + '=') for a in args)):
                args.extend([flag, value])
        print('STANDALONE HYBRID #14: confirmed momentum + provisional/learned jump links; zero-fee default, local 25-call/s ceiling. Verify event settings.')

    def factory(*a, **kw):
        return HybridController(*a, use_priors=use_priors, **kw)
    return target_main(args, exchange_factory=exchange_factory, controller_factory=factory)

if __name__ == '__main__':
    raise SystemExit(main())
