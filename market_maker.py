#!/usr/bin/env python3
"""
Baseline inventory-skewing stock market maker for Optibook (single file).

Contents
  1. Strategy      decide(snapshot, config) -> [DesiredOrder]   (pure, no I/O)
  2. Adapter       ExchangeAdapter: the ONLY place that touches the optibook API
  3. Runner        run_iteration / run_bot / cleanup            (dry run by default)
  4. Flatten       flatten(...)                                 (dry run by default)
  5. CLI           run | flatten | test
  6. Tests         FakeExchange + unittest cases (no optibook import)

Usage (from the folder containing this file)
  /opt/venv/bin/python market_maker.py test
  /opt/venv/bin/python market_maker.py run --instruments <ONE_STOCK>            # dry run, STILL CONNECTS
  /opt/venv/bin/python market_maker.py run --live --cap 10 --size 2 --instruments <ONE_STOCK>
  /opt/venv/bin/python market_maker.py flatten --live                           # stop the runner first

Units: half-spread and skew are in TICKS.
  half_spread_ticks   distance from the reservation price to each quote, in ticks
  skew_ticks_per_lot  how far the reservation price shifts per lot of position, in ticks

Everything marked VERIFY is an assumption about the optibook API. If the API
differs, fix it inside ExchangeAdapter / connect_real_exchange only.
"""
from __future__ import annotations

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

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
HARD_LIMIT = 100                # exchange position limit (per instrument)
LIMIT_GUARD = 90                # runner shuts down at |position| >= this
MAX_CONSECUTIVE_INSERT_FAILS = 10
MIN_SLEEP_S = 0.2               # floor on loop sleep, enforced in code
STATUS_EVERY_S = 5.0
FLATTEN_ORDER_GAP_S = 0.25
EPS = 1e-9

EXIT_OK = 0
EXIT_STARTUP = 1                # bad args, not connected, disconnected, no instruments
EXIT_LIMIT_GUARD = 2
EXIT_INSERT_FAILS = 3
EXIT_EXCEPTION = 4
EXIT_NOT_FLAT = 5               # flatten finished with a non-zero position

BID = "bid"                     # VERIFY side literals
ASK = "ask"
ORDER_LIMIT = "limit"           # VERIFY order type literals
ORDER_IOC = "ioc"

Level = Tuple[float, int]       # (price, volume)


# --------------------------------------------------------------------------- #
# Tick helpers (integer tick arithmetic, no raw float prices)
# --------------------------------------------------------------------------- #
def tick_decimals(tick: float) -> int:
    exp = Decimal(str(tick)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def to_ticks(price: float, tick: float) -> int:
    return int(round(price / tick))


def from_ticks(n: int, tick: float) -> float:
    return round(n * tick, tick_decimals(tick))


def on_grid(price: float, tick: float) -> bool:
    x = price / tick
    return abs(x - round(x)) <= EPS


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DesiredOrder:
    side: str       # 'bid' | 'ask'
    price: float
    volume: int


@dataclass(frozen=True)
class OwnOrder:
    order_id: Any
    side: str
    price: float
    volume: int     # remaining volume


@dataclass(frozen=True)
class Config:
    cap: int = 60                       # internal position cap per instrument
    size: int = 5                       # quote size in lots
    half_spread_ticks: float = 2.0
    skew_ticks_per_lot: float = 0.05


@dataclass(frozen=True)
class InstrumentInfo:
    instrument_id: str
    tick_size: float
    is_stock: Optional[bool] = None     # None = could not be determined
    tradable: bool = True
    price_change_limit: Optional[float] = None
    price_limit: Optional[float] = None


@dataclass(frozen=True)
class Snapshot:
    """What decide() sees. Book levels are CLEANED (own resting volume removed)."""
    instrument_id: str
    bids: Sequence[Level]
    asks: Sequence[Level]
    position: int
    tick_size: float
    price_min: Optional[float] = None
    price_max: Optional[float] = None


@dataclass
class Result:
    success: bool
    order_id: Any = None
    error: str = ""


# --------------------------------------------------------------------------- #
# 1. Strategy (pure)
# --------------------------------------------------------------------------- #
def decide(snapshot: Snapshot, config: Config) -> List[DesiredOrder]:
    """At most one bid and one ask. Returns [] when it is not safe to quote."""
    tick = snapshot.tick_size
    if tick <= 0:
        return []
    bid_ticks = [to_ticks(p, tick) for p, v in snapshot.bids if v > 0]
    ask_ticks = [to_ticks(p, tick) for p, v in snapshot.asks if v > 0]
    if not bid_ticks or not ask_ticks:
        return []                                   # empty / one-sided book
    best_bid_t, best_ask_t = max(bid_ticks), min(ask_ticks)
    if best_bid_t >= best_ask_t:
        return []                                   # locked / crossed book

    mid_t = (best_bid_t + best_ask_t) / 2.0
    # Long -> reservation price moves down (want to sell); short -> up.
    reservation_t = mid_t - config.skew_ticks_per_lot * snapshot.position
    bid_t = math.floor(reservation_t - config.half_spread_ticks + EPS)   # bid rounds down
    ask_t = math.ceil(reservation_t + config.half_spread_ticks - EPS)    # ask rounds up

    # Never cross the (cleaned) book.
    bid_t = min(bid_t, best_ask_t - 1)
    ask_t = max(ask_t, best_bid_t + 1)
    if bid_t >= ask_t:
        return []

    bid_vol = min(config.size, config.cap - snapshot.position)
    ask_vol = min(config.size, config.cap + snapshot.position)

    orders: List[DesiredOrder] = []
    for side, t, vol in ((BID, bid_t, bid_vol), (ASK, ask_t, ask_vol)):
        if vol <= 0:
            continue
        price = from_ticks(t, tick)
        if price <= 0:
            continue
        if snapshot.price_min is not None and price < snapshot.price_min - EPS:
            continue
        if snapshot.price_max is not None and price > snapshot.price_max + EPS:
            continue
        orders.append(DesiredOrder(side, price, int(vol)))
    return orders


# --------------------------------------------------------------------------- #
# 2. Adapter: the only code that knows optibook's API names
# --------------------------------------------------------------------------- #
def _levels(raw_levels: Any) -> List[Level]:
    out: List[Level] = []
    for lv in raw_levels or []:
        if isinstance(lv, (tuple, list)):
            price, vol = lv[0], lv[1]
        else:
            price, vol = lv.price, lv.volume
        out.append((float(price), int(vol)))
    return out


def _norm_side(side: Any) -> str:
    s = str(getattr(side, "value", side)).lower()       # also handles enums
    if s in ("bid", "buy", "b"):
        return BID
    if s in ("ask", "sell", "s", "offer"):
        return ASK
    return s


def _result(resp: Any) -> Result:
    if resp is None:
        return Result(False, None, "no response")
    if isinstance(resp, bool):
        return Result(resp, None, "" if resp else "returned False")
    if hasattr(resp, "success"):
        return Result(bool(resp.success), getattr(resp, "order_id", None),
                      str(getattr(resp, "error_reason", "") or ""))
    if isinstance(resp, int):
        return Result(True, resp)                       # bare order id
    return Result(False, None, f"unrecognised response type {type(resp).__name__}")


def _exc(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


class ExchangeAdapter:
    """Normalises a raw optibook Exchange (or the fake) to what the runner needs."""

    def __init__(self, raw: Any):
        self.raw = raw

    def is_connected(self) -> bool:
        try:
            return bool(self.raw.is_connected())        # VERIFY
        except Exception:
            return False

    def instruments(self) -> Dict[str, InstrumentInfo]:
        raw = self.raw.get_instruments()                # VERIFY: dict id -> instrument
        items = raw.items() if isinstance(raw, dict) else [(i.instrument_id, i) for i in raw]
        out: Dict[str, InstrumentInfo] = {}
        for iid, inst in items:
            kind = getattr(inst, "instrument_type", None)            # VERIFY stock marker
            if kind is None:
                is_stock = None
            else:
                name = str(getattr(kind, "name", None) or kind)
                is_stock = "STOCK" in name.upper()
            tradable = not bool(getattr(inst, "paused", False))      # VERIFY paused flag
            out[iid] = InstrumentInfo(
                instrument_id=iid,
                tick_size=float(getattr(inst, "tick_size", 0) or 0),  # VERIFY
                is_stock=is_stock,
                tradable=tradable,
                price_change_limit=_opt_float(getattr(inst, "price_change_limit", None)),  # VERIFY
                price_limit=_opt_float(getattr(inst, "price_limit", None)),                # VERIFY
            )
        return out

    def book(self, iid: str) -> Tuple[List[Level], List[Level]]:
        pb = self.raw.get_last_price_book(iid)          # VERIFY
        if pb is None:
            return [], []
        return _levels(getattr(pb, "bids", [])), _levels(getattr(pb, "asks", []))

    def own_orders(self, iid: str) -> List[OwnOrder]:
        raw = self.raw.get_outstanding_orders(iid)      # VERIFY: dict order_id -> status
        out: List[OwnOrder] = []
        for oid, st in (raw or {}).items():
            out.append(OwnOrder(oid, _norm_side(st.side), float(st.price), int(st.volume)))
        return out

    def positions(self) -> Dict[str, int]:
        raw = self.raw.get_positions()                  # VERIFY: dict id -> int
        return {k: int(v) for k, v in (raw or {}).items()}

    def pnl(self) -> Optional[float]:
        try:
            return float(self.raw.get_pnl())            # VERIFY
        except Exception:
            return None

    def insert(self, iid: str, side: str, price: float, volume: int,
               order_type: str = ORDER_LIMIT) -> Result:
        try:
            resp = self.raw.insert_order(iid, price=price, volume=volume,
                                         side=side, order_type=order_type)   # VERIFY
        except Exception as e:
            return Result(False, None, _exc(e))
        return _result(resp)

    def cancel(self, iid: str, order_id: Any) -> Result:
        """None is NOT treated as success: the order stays 'unconfirmed' and the
        runner re-reads outstanding orders next loop instead of inserting blindly."""
        try:
            resp = self.raw.delete_order(iid, order_id=order_id)             # VERIFY
        except Exception as e:
            return Result(False, None, _exc(e))
        return _result(resp)

    def delete_all(self, iid: str) -> Result:
        try:
            resp = self.raw.delete_orders(iid)          # VERIFY
        except Exception as e:
            return Result(False, None, _exc(e))
        if hasattr(resp, "success"):
            return Result(bool(resp.success), None, str(getattr(resp, "error_reason", "") or ""))
        return Result(True)


def _opt_float(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def connect_real_exchange() -> Any:
    """Creates the real exchange connection. Only ever called by the CLI."""
    from optibook.synchronous_client import Exchange    # VERIFY import path
    exchange = Exchange()
    exchange.connect()                                  # VERIFY (some versions connect in __init__)
    return exchange


def price_band(info: InstrumentInfo, reference: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
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
    return lo, hi


def select_instruments(all_info: Dict[str, InstrumentInfo],
                       wanted: Optional[Sequence[str]]) -> Tuple[Dict[str, InstrumentInfo], Optional[str]]:
    if wanted:
        missing = [w for w in wanted if w not in all_info]
        if missing:
            return {}, f"unknown instrument id(s): {', '.join(missing)}"
        chosen = {w: all_info[w] for w in wanted}
        bad = [w for w, i in chosen.items() if i.is_stock is False]
        if bad:
            return {}, f"not a stock: {', '.join(bad)}"
    else:
        chosen = {k: v for k, v in all_info.items() if v.is_stock is True}
    chosen = {k: v for k, v in chosen.items() if v.tradable and v.tick_size > 0}
    return chosen, None


# --------------------------------------------------------------------------- #
# 3. Runner
# --------------------------------------------------------------------------- #
def clean_book(levels: Sequence[Level], own: Sequence[OwnOrder], side: str, tick: float) -> List[Level]:
    """Subtract our own resting volume per price level; drop empty levels.
    Returns bids sorted best-first (desc) or asks best-first (asc)."""
    agg: Dict[int, int] = defaultdict(int)
    for p, v in levels:
        agg[to_ticks(p, tick)] += int(v)
    for o in own:
        if o.side == side:
            agg[to_ticks(o.price, tick)] -= int(o.volume)
    keys = sorted((k for k, v in agg.items() if v > 0), reverse=(side == BID))
    return [(from_ticks(k, tick), agg[k]) for k in keys]


def clamp_orders(orders: Sequence[DesiredOrder], position: int, cap: int, tick: float,
                 best_bid: Optional[float], best_ask: Optional[float],
                 price_min: Optional[float] = None, price_max: Optional[float] = None,
                 label: str = "") -> List[DesiredOrder]:
    """Always applied, independent of the strategy. Buy and sell room are separate."""
    rooms = {BID: cap - position, ASK: cap + position}
    out: List[DesiredOrder] = []

    def drop(o: DesiredOrder, why: str) -> None:
        print(f"CLAMP DROP {label} {o.side} {o.volume}@{o.price}: {why}")

    for o in orders:
        if o.side not in rooms:
            drop(o, "unknown side")
            continue
        vol = min(int(o.volume), rooms[o.side])
        if vol <= 0:
            continue                                    # expected at/over the cap
        if o.price <= 0 or not on_grid(o.price, tick):
            drop(o, "price off tick grid")
        elif price_min is not None and o.price < price_min - EPS:
            drop(o, "below price band")
        elif price_max is not None and o.price > price_max + EPS:
            drop(o, "above price band")
        elif o.side == BID and best_ask is not None and to_ticks(o.price, tick) >= to_ticks(best_ask, tick):
            drop(o, "would cross best ask")
        elif o.side == ASK and best_bid is not None and to_ticks(o.price, tick) <= to_ticks(best_bid, tick):
            drop(o, "would cross best bid")
        else:
            out.append(DesiredOrder(o.side, o.price, vol))
            rooms[o.side] -= vol
    return out


@dataclass
class RunState:
    iteration: int = 0
    consecutive_insert_failures: int = 0
    inserts: int = 0            # since last status line
    cancels: int = 0
    failures: int = 0
    last_status_t: Optional[float] = None
    positions: Optional[Dict[str, int]] = None
    pnl: Optional[float] = None


class LiveExecutor:
    def __init__(self, adapter: ExchangeAdapter):
        self.adapter = adapter

    def insert(self, iid: str, side: str, price: float, volume: int) -> Result:
        return self.adapter.insert(iid, side, price, volume, ORDER_LIMIT)

    def cancel(self, iid: str, order: OwnOrder) -> Result:
        return self.adapter.cancel(iid, order.order_id)


class DryExecutor:
    """Pretends. Keeps a simulated set of resting orders so repeated identical
    quotes are not re-printed every loop. Never touches the exchange."""

    def __init__(self) -> None:
        self.sim: Dict[str, Dict[int, OwnOrder]] = defaultdict(dict)
        self._next = 1

    def orders_for(self, iid: str) -> List[OwnOrder]:
        return list(self.sim[iid].values())

    def insert(self, iid: str, side: str, price: float, volume: int) -> Result:
        oid = self._next
        self._next += 1
        self.sim[iid][oid] = OwnOrder(oid, side, price, volume)
        print(f"[DRY RUN] would INSERT {iid} {side} {volume}@{price}")
        return Result(True, oid)

    def cancel(self, iid: str, order: OwnOrder) -> Result:
        self.sim[iid].pop(order.order_id, None)
        print(f"[DRY RUN] would CANCEL {iid} {order.side} {order.volume}@{order.price}")
        return Result(True)


def _reconcile_side(executor: Any, iid: str, side: str, existing: List[OwnOrder],
                    desired: List[DesiredOrder], room: int, tick: float, state: RunState) -> None:
    remaining = list(existing)
    kept: List[OwnOrder] = []
    to_insert: List[DesiredOrder] = []
    for d in desired:
        match = next((o for o in remaining
                      if to_ticks(o.price, tick) == to_ticks(d.price, tick) and o.volume <= d.volume), None)
        if match is not None:
            remaining.remove(match)
            kept.append(match)
        else:
            to_insert.append(d)

    cancel_failed = False
    kept_volume = sum(o.volume for o in kept)
    for o in remaining:                                  # stale / unwanted resting orders
        res = executor.cancel(iid, o)
        if res.success:
            state.cancels += 1
        else:
            state.failures += 1
            cancel_failed = True
            kept_volume += o.volume                      # still resting as far as we know
            print(f"CANCEL FAILED {iid} {side} {o.volume}@{o.price}: {res.error}")

    if cancel_failed:
        return                                           # never insert on top of an unconfirmed cancel

    for d in to_insert:
        if kept_volume + d.volume > room:
            print(f"SKIP INSERT {iid} {side} {d.volume}@{d.price}: would exceed room {room}")
            continue
        res = executor.insert(iid, side, d.price, d.volume)
        if res.success:
            state.inserts += 1
            state.consecutive_insert_failures = 0
            kept_volume += d.volume
        else:
            state.failures += 1
            state.consecutive_insert_failures += 1
            print(f"INSERT FAILED {iid} {side} {d.volume}@{d.price}: {res.error}")


def reconcile_instrument(executor: Any, iid: str, resting: List[OwnOrder], desired: List[DesiredOrder],
                         position: int, cap: int, tick: float, state: RunState) -> None:
    rooms = {BID: cap - position, ASK: cap + position}
    for side in (BID, ASK):
        _reconcile_side(executor, iid, side,
                        [o for o in resting if o.side == side],
                        [d for d in desired if d.side == side],
                        rooms[side], tick, state)


CSV_HEADER = ["utc_time", "instrument", "position", "clean_best_bid", "clean_best_ask",
              "our_bid", "our_ask", "pnl"]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_iteration(adapter: ExchangeAdapter, executor: Any, instruments: Dict[str, InstrumentInfo],
                  cfg: Config, state: RunState, decide_fn: Callable[[Snapshot, Config], List[DesiredOrder]] = decide,
                  csv_writer: Any = None, dry_run: bool = False) -> Optional[int]:
    """One loop. Returns None to continue, or an exit code to stop."""
    if not adapter.is_connected():
        print("DISCONNECTED: exchange connection lost. Shutting down (never reconnecting).")
        return EXIT_STARTUP

    positions = adapter.positions()
    pnl = adapter.pnl()
    state.positions = {iid: int(positions.get(iid, 0)) for iid in instruments}
    state.pnl = pnl

    for iid, info in instruments.items():
        tick = info.tick_size
        pos = state.positions[iid]
        raw_bids, raw_asks = adapter.book(iid)
        real_own = adapter.own_orders(iid)
        bids = clean_book(raw_bids, real_own, BID, tick)
        asks = clean_book(raw_asks, real_own, ASK, tick)

        reference = None
        if raw_bids and raw_asks:
            reference = (max(p for p, _ in raw_bids) + min(p for p, _ in raw_asks)) / 2.0
        price_min, price_max = price_band(info, reference)

        desired: List[DesiredOrder] = []
        if bids and asks:                                # one-sided/empty: never guess
            snap = Snapshot(iid, bids, asks, pos, tick, price_min, price_max)
            desired = list(decide_fn(snap, cfg))
        clamped = clamp_orders(desired, pos, cfg.cap, tick,
                               bids[0][0] if bids else None, asks[0][0] if asks else None,
                               price_min, price_max, label=iid)

        resting = executor.orders_for(iid) if dry_run else real_own
        reconcile_instrument(executor, iid, resting, clamped, pos, cfg.cap, tick, state)

        if csv_writer is not None:
            our_bid = max((d.price for d in clamped if d.side == BID), default="")
            our_ask = min((d.price for d in clamped if d.side == ASK), default="")
            csv_writer.writerow([_utc_iso(), iid, pos,
                                 bids[0][0] if bids else "", asks[0][0] if asks else "",
                                 our_bid, our_ask, "" if pnl is None else pnl])

    worst = [iid for iid, p in state.positions.items() if abs(p) >= LIMIT_GUARD]
    if worst:
        print(f"LIMIT GUARD: |position| >= {LIMIT_GUARD} in {', '.join(worst)}. Shutting down.")
        return EXIT_LIMIT_GUARD
    if state.consecutive_insert_failures >= MAX_CONSECUTIVE_INSERT_FAILS:
        print(f"{state.consecutive_insert_failures} consecutive failed inserts "
              f"(rate limit or bad prices?). Shutting down instead of hammering the exchange.")
        return EXIT_INSERT_FAILS
    return None


def print_status(state: RunState) -> None:
    pos = " ".join(f"{k}={v}" for k, v in (state.positions or {}).items())
    pnl = "n/a" if state.pnl is None else f"{state.pnl:.2f}"
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] PnL={pnl} | pos {pos} | "
          f"inserts={state.inserts} cancels={state.cancels} failures={state.failures}")
    state.inserts = state.cancels = state.failures = 0


def cleanup(adapter: ExchangeAdapter, instrument_ids: Sequence[str], live: bool) -> None:
    if live:
        for iid in instrument_ids:
            try:
                res = adapter.delete_all(iid)
                if not res.success:
                    print(f"CLEANUP: delete-all failed for {iid}: {res.error}")
            except Exception as e:                       # one failure must not skip the others
                print(f"CLEANUP: delete-all raised for {iid}: {_exc(e)}")
    try:
        if adapter.is_connected():
            positions = adapter.positions()
            shown = {k: v for k, v in positions.items() if k in set(instrument_ids)}
            pnl = adapter.pnl()
            print(f"FINAL positions: {shown}  PnL: {'n/a' if pnl is None else f'{pnl:.2f}'}")
    except Exception as e:
        print(f"CLEANUP: could not read final state: {_exc(e)}")
    if not live:
        print("Dry run: nothing was inserted or cancelled.")


def run_bot(adapter: ExchangeAdapter, instruments: Dict[str, InstrumentInfo], cfg: Config,
            dry_run: bool, sleep_s: float, log_path: Path,
            decide_fn: Callable[[Snapshot, Config], List[DesiredOrder]] = decide,
            max_iterations: Optional[int] = None,
            sleep_fn: Callable[[float], None] = time.sleep) -> int:
    executor: Any = DryExecutor() if dry_run else LiveExecutor(adapter)
    state = RunState()
    exit_code = EXIT_OK
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            state.last_status_t = time.monotonic()
            while True:
                code = run_iteration(adapter, executor, instruments, cfg, state, decide_fn, writer, dry_run)
                fh.flush()
                if code is not None:
                    exit_code = code
                    break
                state.iteration += 1
                if time.monotonic() - state.last_status_t >= STATUS_EVERY_S:
                    print_status(state)
                    state.last_status_t = time.monotonic()
                if max_iterations is not None and state.iteration >= max_iterations:
                    break
                sleep_fn(max(MIN_SLEEP_S, sleep_s))
    except KeyboardInterrupt:
        print("Interrupted (Ctrl+C / SIGTERM): shutting down.")
    except Exception:
        traceback.print_exc()
        exit_code = EXIT_EXCEPTION
    finally:
        cleanup(adapter, list(instruments), live=not dry_run)
    return exit_code


# --------------------------------------------------------------------------- #
# 4. Flatten
# --------------------------------------------------------------------------- #
def flatten(adapter: ExchangeAdapter, instrument_ids: Sequence[str], live: bool,
            max_rounds: int = 20, chunk: int = 20,
            sleep_fn: Callable[[float], None] = time.sleep) -> Tuple[Dict[str, int], int]:
    """IOC-flatten. Each order is <= |position| and <= chunk, so it can never flip a position."""
    ids = list(instrument_ids)
    if live:
        for iid in ids:
            res = adapter.delete_all(iid)
            if not res.success:
                print(f"delete-all failed for {iid}: {res.error}")

    for rnd in range(1, max_rounds + 1):
        if not adapter.is_connected():
            print("DISCONNECTED: exiting flatten.")
            return {}, EXIT_STARTUP
        positions = adapter.positions()
        open_pos = {i: int(positions.get(i, 0)) for i in ids if int(positions.get(i, 0)) != 0}
        if not open_pos:
            print("Everything is flat.")
            break
        print(f"Round {rnd}: open positions {open_pos}")
        for iid, pos in open_pos.items():
            bids, asks = adapter.book(iid)
            if pos > 0:
                if not bids:
                    print(f"SKIP {iid}: long {pos} but no bids in the book")
                    continue
                side, price = ASK, max(p for p, _ in bids)      # sell into the best bid
            else:
                if not asks:
                    print(f"SKIP {iid}: short {pos} but no asks in the book")
                    continue
                side, price = BID, min(p for p, _ in asks)      # buy from the best ask
            vol = min(abs(pos), chunk)
            if not live:
                print(f"[DRY RUN] would send IOC {side} {vol}@{price} on {iid}")
                continue
            res = adapter.insert(iid, side, price, vol, ORDER_IOC)
            if not res.success:
                print(f"IOC FAILED {iid} {side} {vol}@{price}: {res.error}")
            sleep_fn(FLATTEN_ORDER_GAP_S)
        if not live:
            break                                               # nothing changes in a dry run

    remaining: Dict[str, int] = {}
    if adapter.is_connected():
        positions = adapter.positions()
        remaining = {i: int(positions.get(i, 0)) for i in ids if int(positions.get(i, 0)) != 0}
    print(f"Remaining positions: {remaining if remaining else 'none (flat)'}")
    if live and remaining:
        return remaining, EXIT_NOT_FLAT
    return remaining, EXIT_OK


# --------------------------------------------------------------------------- #
# 5. CLI
# --------------------------------------------------------------------------- #
def validate_params(size: int, cap: int, half_spread: float, skew: float) -> Optional[str]:
    if size < 1:
        return "--size must be >= 1"
    if cap < 1:
        return "--cap must be >= 1"
    if cap + size > HARD_LIMIT:
        return (f"--cap + --size must be <= {HARD_LIMIT} (got {cap + size}); "
                f"this keeps a fill race inside the exchange limit")
    if half_spread < 0.5:
        return "--half-spread must be >= 0.5 ticks"
    if skew < 0:
        return "--skew must be >= 0"
    return None


def _banner(title: str, live: bool, lines: Sequence[str], extra: Sequence[str] = ()) -> None:
    print("=" * 72)
    print(f"{title}: {'LIVE' if live else 'DRY RUN'}")
    for ln in lines:
        print(f"  {ln}")
    print("This process CONNECTS to the exchange and will disconnect any teammate currently connected.")
    if not live:
        print("No orders will be inserted or cancelled.")
    for ln in extra:
        print(ln)
    print("=" * 72)


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _parse_ids(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _connect(factory: Callable[[], Any]) -> Optional[ExchangeAdapter]:
    try:
        raw = factory()
    except Exception as e:
        print(f"Could not create/connect the exchange: {_exc(e)}")
        return None
    adapter = ExchangeAdapter(raw)
    if not adapter.is_connected():
        print("NOT CONNECTED after connect. Not retrying.")
        return None
    return adapter


def cmd_run(args: argparse.Namespace, factory: Callable[[], Any]) -> int:
    err = validate_params(args.size, args.cap, args.half_spread, args.skew)
    if err:
        print(f"ERROR: {err}")
        return EXIT_STARTUP
    sleep_s = max(MIN_SLEEP_S, args.sleep)
    cfg = Config(cap=args.cap, size=args.size, half_spread_ticks=args.half_spread,
                 skew_ticks_per_lot=args.skew)
    _banner("MARKET MAKER", args.live,
            [f"cap={cfg.cap} size={cfg.size} half_spread={cfg.half_spread_ticks} ticks "
             f"skew={cfg.skew_ticks_per_lot} ticks/lot sleep={sleep_s}s",
             f"instruments={args.instruments or 'all stocks'}"])
    adapter = _connect(factory)
    if adapter is None:
        return EXIT_STARTUP
    instruments, err = select_instruments(adapter.instruments(), _parse_ids(args.instruments))
    if err:
        print(f"ERROR: {err}")
        return EXIT_STARTUP
    if not instruments:
        print("No tradable stock instruments to quote.")
        return EXIT_STARTUP
    print(f"Quoting: {', '.join(instruments)}")
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(args.log_dir) / f"run_{stamp}.csv"
    return run_bot(adapter, instruments, cfg, dry_run=not args.live, sleep_s=sleep_s, log_path=log_path)


def cmd_flatten(args: argparse.Namespace, factory: Callable[[], Any]) -> int:
    if args.max_rounds < 1 or args.chunk < 1:
        print("ERROR: --max-rounds and --chunk must be >= 1")
        return EXIT_STARTUP
    _banner("FLATTEN", args.live, [f"max_rounds={args.max_rounds} chunk={args.chunk}"],
            ["Stop the runner first: this connection will kick it."])
    adapter = _connect(factory)
    if adapter is None:
        return EXIT_STARTUP
    instruments, err = select_instruments(adapter.instruments(), _parse_ids(args.instruments))
    if err:
        print(f"ERROR: {err}")
        return EXIT_STARTUP
    if not instruments:
        print("No tradable stock instruments.")
        return EXIT_STARTUP
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        _, code = flatten(adapter, list(instruments), args.live, args.max_rounds, args.chunk)
    except KeyboardInterrupt:
        print("Interrupted.")
        return EXIT_OK
    except Exception:
        traceback.print_exc()
        return EXIT_EXCEPTION
    return code


def run_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(prog="market_maker.py",
                                description="Baseline Optibook market maker. Dry run unless --live.")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the market maker (default command)")
    r.add_argument("--live", action="store_true", help="send real orders (default: dry run)")
    r.add_argument("--cap", type=int, default=d.cap, help="internal position cap per instrument")
    r.add_argument("--size", type=int, default=d.size, help="quote size in lots")
    r.add_argument("--half-spread", type=float, default=d.half_spread_ticks, help="in ticks")
    r.add_argument("--skew", type=float, default=d.skew_ticks_per_lot, help="ticks per lot of position")
    r.add_argument("--instruments", default=None, help="comma-separated ids (default: all stocks)")
    r.add_argument("--sleep", type=float, default=0.5, help=f"seconds per loop (floor {MIN_SLEEP_S})")
    r.add_argument("--log-dir", default="logs")

    f = sub.add_parser("flatten", help="IOC-flatten all positions (stop the runner first)")
    f.add_argument("--live", action="store_true", help="send real orders (default: dry run)")
    f.add_argument("--instruments", default=None)
    f.add_argument("--max-rounds", type=int, default=20)
    f.add_argument("--chunk", type=int, default=20, help="max lots per IOC order")

    sub.add_parser("test", help="run the unit tests (never connects)")
    return p


def main(argv: Optional[Sequence[str]] = None,
         exchange_factory: Optional[Callable[[], Any]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "run")                       # `market_maker.py --live ...` == `run --live ...`
    args = build_parser().parse_args(argv)
    if args.command == "test":
        return run_tests()
    factory = exchange_factory or connect_real_exchange
    if args.command == "flatten":
        return cmd_flatten(args, factory)
    return cmd_run(args, factory)


# --------------------------------------------------------------------------- #
# 6. Tests (fake exchange mimics the RAW optibook API; no optibook import)
# --------------------------------------------------------------------------- #
class FakeExchange:
    def __init__(self) -> None:
        self.instruments: Dict[str, SimpleNamespace] = {}
        self.others: Dict[str, Tuple[List[Level], List[Level]]] = {}
        self.orders: Dict[str, Dict[int, SimpleNamespace]] = {}
        self.positions: Dict[str, int] = {}
        self.connected = True
        self.connect_calls = 0
        self.calls: List[Tuple] = []          # ('insert'|'delete'|'delete_all', ...)
        self.fail_inserts = 0                 # next N inserts fail
        self.raise_on_book_call: Optional[int] = None
        self.book_calls = 0
        self.pnl = 0.0
        self._ids = iter(range(1, 10 ** 9))

    # -- test setup helpers
    def add_instrument(self, iid: str, tick: float = 0.1, stock: bool = True, **kw: Any) -> None:
        self.instruments[iid] = SimpleNamespace(
            instrument_id=iid, tick_size=tick,
            instrument_type="STOCK" if stock else "FUTURE", **kw)
        self.others[iid] = ([], [])
        self.orders[iid] = {}
        self.positions.setdefault(iid, 0)

    def set_market(self, iid: str, bids: List[Level], asks: List[Level]) -> None:
        self.others[iid] = (list(bids), list(asks))

    def inserts(self, order_type: Optional[str] = None) -> List[Tuple]:
        return [c for c in self.calls if c[0] == "insert" and (order_type is None or c[5] == order_type)]

    def deletes(self) -> List[Tuple]:
        return [c for c in self.calls if c[0] == "delete"]

    # -- raw API
    def connect(self) -> None:
        self.connect_calls += 1

    def is_connected(self) -> bool:
        return self.connected

    def get_instruments(self) -> Dict[str, SimpleNamespace]:
        return dict(self.instruments)

    def get_last_price_book(self, iid: str) -> SimpleNamespace:
        self.book_calls += 1
        if self.raise_on_book_call is not None and self.book_calls >= self.raise_on_book_call:
            raise RuntimeError("boom")
        tick = self.instruments[iid].tick_size

        def merge(levels: List[Level], own_side: str, reverse: bool) -> List[SimpleNamespace]:
            agg: Dict[int, int] = defaultdict(int)
            for p, v in levels:
                agg[to_ticks(p, tick)] += v
            for o in self.orders[iid].values():
                if o.side == own_side:
                    agg[to_ticks(o.price, tick)] += o.volume
            return [SimpleNamespace(price=from_ticks(k, tick), volume=v)
                    for k, v in sorted(agg.items(), reverse=reverse) if v > 0]

        ob, oa = self.others[iid]
        return SimpleNamespace(bids=merge(ob, BID, True), asks=merge(oa, ASK, False))

    def get_outstanding_orders(self, iid: str) -> Dict[int, SimpleNamespace]:
        return {oid: SimpleNamespace(order_id=oid, price=o.price, volume=o.volume, side=o.side)
                for oid, o in self.orders[iid].items()}

    def get_positions(self) -> Dict[str, int]:
        return dict(self.positions)

    def get_pnl(self) -> float:
        return self.pnl

    def insert_order(self, iid: str, price: float = 0.0, volume: int = 0, side: str = BID,
                     order_type: str = ORDER_LIMIT) -> SimpleNamespace:
        self.calls.append(("insert", iid, side, price, volume, order_type))
        if self.fail_inserts > 0:
            self.fail_inserts -= 1
            return SimpleNamespace(success=False, order_id=None, error_reason="forced failure")
        if order_type == ORDER_IOC:
            ob, oa = self.others[iid]
            if side == ASK and ob and price <= max(p for p, _ in ob) + EPS:
                self.positions[iid] -= volume
            elif side == BID and oa and price >= min(p for p, _ in oa) - EPS:
                self.positions[iid] += volume
            return SimpleNamespace(success=True, order_id=None, error_reason="")
        oid = next(self._ids)
        self.orders[iid][oid] = SimpleNamespace(price=price, volume=volume, side=side)
        return SimpleNamespace(success=True, order_id=oid, error_reason="")

    def delete_order(self, iid: str, order_id: int = 0) -> SimpleNamespace:
        self.calls.append(("delete", iid, order_id))
        self.orders[iid].pop(order_id, None)
        return SimpleNamespace(success=True)

    def delete_orders(self, iid: str) -> SimpleNamespace:
        self.calls.append(("delete_all", iid))
        self.orders[iid].clear()
        return SimpleNamespace(success=True)


@contextlib.contextmanager
def quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out


def default_test_cfg() -> Config:
    return Config(cap=60, size=10, half_spread_ticks=2.0, skew_ticks_per_lot=0.05)


def make_fake(ids: Sequence[str] = ("AAA",), tick: float = 0.1,
              bids: Optional[List[Level]] = None, asks: Optional[List[Level]] = None) -> FakeExchange:
    fake = FakeExchange()
    for iid in ids:
        fake.add_instrument(iid, tick)
        fake.set_market(iid, bids if bids is not None else [(100.0, 50)],
                        asks if asks is not None else [(101.0, 50)])
    return fake


class Harness:
    """Drives run_iteration directly (no cleanup between steps)."""

    def __init__(self, fake: FakeExchange, cfg: Optional[Config] = None,
                 decide_fn: Callable = decide, dry_run: bool = False):
        self.fake = fake
        self.adapter = ExchangeAdapter(fake)
        self.instruments, _ = select_instruments(self.adapter.instruments(), None)
        self.cfg = cfg or default_test_cfg()
        self.decide_fn = decide_fn
        self.dry_run = dry_run
        self.executor: Any = DryExecutor() if dry_run else LiveExecutor(self.adapter)
        self.state = RunState()

    def step(self) -> Optional[int]:
        with quiet():
            return run_iteration(self.adapter, self.executor, self.instruments, self.cfg,
                                 self.state, self.decide_fn, None, self.dry_run)


class StrategyTests(unittest.TestCase):
    def snap(self, bids, asks, pos=0, tick=0.1, **kw):
        return Snapshot("AAA", bids, asks, pos, tick, **kw)

    def test_basic_quotes_inside_spread_and_on_grid(self):
        orders = decide(self.snap([(100.0, 5)], [(101.0, 5)]), default_test_cfg())
        by = {o.side: o for o in orders}
        self.assertAlmostEqual(by[BID].price, 100.3)
        self.assertAlmostEqual(by[ASK].price, 100.7)

    def test_empty_and_one_sided_book(self):
        cfg = default_test_cfg()
        self.assertEqual(decide(self.snap([], []), cfg), [])
        self.assertEqual(decide(self.snap([(100.0, 5)], []), cfg), [])
        self.assertEqual(decide(self.snap([], [(101.0, 5)]), cfg), [])

    def test_inventory_skew_direction(self):
        cfg = default_test_cfg()
        flat = {o.side: o.price for o in decide(self.snap([(100.0, 5)], [(101.0, 5)], pos=0), cfg)}
        long_ = {o.side: o.price for o in decide(self.snap([(100.0, 5)], [(101.0, 5)], pos=50), cfg)}
        short = {o.side: o.price for o in decide(self.snap([(100.0, 5)], [(101.0, 5)], pos=-50), cfg)}
        self.assertLess(long_[ASK], flat[ASK])      # long -> sell cheaper
        self.assertGreater(short[BID], flat[BID])   # short -> buy dearer

    def test_never_crosses_book(self):
        cfg = Config(cap=60, size=10, half_spread_ticks=2.0, skew_ticks_per_lot=0.5)
        orders = decide(self.snap([(100.0, 5)], [(101.0, 5)], pos=-50), cfg)
        bid = next(o for o in orders if o.side == BID)
        self.assertLessEqual(bid.price, 100.9 + 1e-9)

    def test_price_band_drops_quotes(self):
        orders = decide(self.snap([(100.0, 5)], [(101.0, 5)], price_max=100.5), default_test_cfg())
        self.assertEqual([o.side for o in orders], [BID])


class RunnerTests(unittest.TestCase):
    # 1
    def test_long_at_or_over_cap_never_bids(self):
        for pos in (60, 100):
            h = Harness(make_fake())
            h.fake.positions["AAA"] = pos
            code = h.step()
            self.assertEqual(code, EXIT_LIMIT_GUARD if pos >= LIMIT_GUARD else None)
            self.assertEqual([c for c in h.fake.inserts() if c[2] == BID], [])
            asks = [c for c in h.fake.inserts() if c[2] == ASK]
            self.assertEqual(len(asks), 1)
            self.assertEqual(asks[0][4], 10)

    # 2
    def test_short_at_or_over_cap_never_asks(self):
        for pos in (-60, -100):
            h = Harness(make_fake())
            h.fake.positions["AAA"] = pos
            code = h.step()
            self.assertEqual(code, EXIT_LIMIT_GUARD if pos <= -LIMIT_GUARD else None)
            self.assertEqual([c for c in h.fake.inserts() if c[2] == ASK], [])
            bids = [c for c in h.fake.inserts() if c[2] == BID]
            self.assertEqual(len(bids), 1)
            self.assertEqual(bids[0][4], 10)

    # 3
    def test_partial_room_volume(self):
        h = Harness(make_fake())
        h.fake.positions["AAA"] = 57            # cap - 3
        h.step()
        vols = {c[2]: c[4] for c in h.fake.inserts()}
        self.assertEqual(vols[BID], 3)
        self.assertEqual(vols[ASK], 10)

    # 4
    def test_runner_clamps_oversized_strategy_output(self):
        def greedy(snap, cfg):
            return [DesiredOrder(BID, 100.3, 500), DesiredOrder(ASK, 100.7, 500)]
        h = Harness(make_fake(), decide_fn=greedy)
        h.step()
        vols = {c[2]: c[4] for c in h.fake.inserts()}
        self.assertEqual(vols, {BID: 60, ASK: 60})

    def test_runner_clamps_multiple_orders_on_one_side_to_room(self):
        def greedy(snap, cfg):
            return [DesiredOrder(BID, 100.3, 40), DesiredOrder(BID, 100.2, 40)]
        h = Harness(make_fake(), decide_fn=greedy)
        h.step()
        self.assertEqual(sum(c[4] for c in h.fake.inserts() if c[2] == BID), 60)

    # 5
    def test_clean_book_unit(self):
        own = [OwnOrder(1, BID, 100.0, 10)]
        self.assertEqual(clean_book([(100.0, 30), (99.9, 20)], own, BID, 0.1), [(100.0, 20), (99.9, 20)])
        own = [OwnOrder(1, BID, 100.0, 30)]
        self.assertEqual(clean_book([(100.0, 30), (99.9, 20)], own, BID, 0.1), [(99.9, 20)])
        self.assertEqual(clean_book([(100.0, 30)], [OwnOrder(1, ASK, 100.0, 30)], BID, 0.1), [(100.0, 30)])

    def test_own_quote_exclusion_in_runner(self):
        seen = []

        def spy(snap, cfg):
            seen.append(snap)
            return []
        fake = make_fake(bids=[(99.9, 20)], asks=[(101.0, 50)])
        fake.insert_order("AAA", price=100.0, volume=5, side=BID)       # our own best bid
        Harness(fake, decide_fn=spy).step()
        self.assertEqual(list(seen[0].bids), [(99.9, 20)])
        # shared level keeps the others' volume
        fake2 = make_fake(bids=[(100.0, 20)], asks=[(101.0, 50)])
        fake2.insert_order("AAA", price=100.0, volume=5, side=BID)
        seen.clear()
        Harness(fake2, decide_fn=spy).step()
        self.assertEqual(list(seen[0].bids), [(100.0, 20)])

    # 6
    def test_no_drift_with_static_market(self):
        h = Harness(make_fake())
        for _ in range(6):
            h.step()
        self.assertEqual(len(h.fake.inserts()), 2)
        self.assertEqual(h.fake.deletes(), [])
        self.assertEqual({(c[2], c[3]) for c in h.fake.inserts()}, {(BID, 100.3), (ASK, 100.7)})

    # 7
    def test_empty_and_one_sided_book_cancel_resting_quotes(self):
        for bids, asks in (([], []), ([(100.0, 50)], []), ([], [(101.0, 50)])):
            h = Harness(make_fake())
            h.step()
            self.assertEqual(len(h.fake.orders["AAA"]), 2)
            h.fake.set_market("AAA", bids, asks)
            self.assertIsNone(h.step())
            self.assertEqual(len(h.fake.orders["AAA"]), 0)
            n = len(h.fake.inserts())
            h.step()
            self.assertEqual(len(h.fake.inserts()), n)                  # still nothing re-inserted

    # 8
    def test_unchanged_quotes_send_nothing(self):
        h = Harness(make_fake())
        h.step()
        n = len(h.fake.calls)
        h.step()
        self.assertEqual(len(h.fake.calls), n)

    def test_partial_fill_keeps_order_without_churn(self):
        h = Harness(make_fake())
        h.step()
        for o in h.fake.orders["AAA"].values():
            o.volume = 4                                                # partially filled
        n = len(h.fake.calls)
        h.step()
        self.assertEqual(len(h.fake.calls), n)

    # 9
    def test_price_change_cancels_and_reinserts_one_side(self):
        h = Harness(make_fake())
        h.step()
        before = len(h.fake.calls)
        h.fake.set_market("AAA", [(100.0, 50)], [(101.1, 50)])         # mid moves half a tick
        h.step()
        new = h.fake.calls[before:]
        self.assertEqual([c[0] for c in new], ["delete", "insert"])
        self.assertEqual(new[1][2], ASK)
        self.assertAlmostEqual(new[1][3], 100.8)

    # 10
    def test_tick_grid_and_no_crossing(self):
        fake = make_fake(tick=0.05)
        h = Harness(fake)
        markets = [([(10.05, 10)], [(10.35, 10)]), ([(10.00, 10)], [(10.60, 10)]),
                   ([(10.05, 10)], [(10.10, 10)]), ([(9.95, 10)], [(10.50, 10)])]
        for bids, asks in markets:
            for pos in (-50, 0, 50):
                fake.positions["AAA"] = pos
                fake.set_market("AAA", bids, asks)
                h.step()
                best_bid = max(p for p, _ in bids)
                best_ask = min(p for p, _ in asks)
                rb = [o.price for o in fake.orders["AAA"].values() if o.side == BID]
                ra = [o.price for o in fake.orders["AAA"].values() if o.side == ASK]
                for p in rb + ra:
                    self.assertTrue(on_grid(p, 0.05), p)
                if rb and ra:
                    self.assertLess(max(rb), min(ra))
                for p in rb:
                    self.assertLess(p, best_ask - 1e-9)
                for p in ra:
                    self.assertGreater(p, best_bid + 1e-9)
        for c in fake.inserts():
            self.assertTrue(on_grid(c[3], 0.05))

    # 11
    def test_insert_failure_is_reported_and_loop_continues(self):
        h = Harness(make_fake())
        h.fake.fail_inserts = 1
        self.assertIsNone(h.step())
        self.assertEqual(h.state.failures, 1)
        self.assertEqual(len(h.fake.orders["AAA"]), 1)                  # only the ask got in
        self.assertIsNone(h.step())
        self.assertEqual(len(h.fake.orders["AAA"]), 2)                  # bid retried
        self.assertEqual(h.state.consecutive_insert_failures, 0)

    def test_ten_consecutive_insert_failures_stop_the_bot(self):
        h = Harness(make_fake())
        h.fake.fail_inserts = 10 ** 6
        codes = [h.step() for _ in range(6)]
        self.assertIn(EXIT_INSERT_FAILS, codes)

    # 12
    def test_disconnected_stops_without_orders_or_reconnect(self):
        fake = make_fake()
        fake.connected = False
        adapter = ExchangeAdapter(fake)
        insts, _ = select_instruments(ExchangeAdapter(make_fake()).instruments(), None)
        with tempfile.TemporaryDirectory() as d, quiet():
            code = run_bot(adapter, insts, default_test_cfg(), False, 0.0, Path(d) / "l.csv",
                           max_iterations=5, sleep_fn=lambda s: None)
        self.assertEqual(code, EXIT_STARTUP)
        self.assertEqual(fake.inserts(), [])
        self.assertEqual(fake.connect_calls, 0)

    # 13
    def test_exception_mid_loop_live_deletes_all_for_every_instrument(self):
        fake = make_fake(ids=("AAA", "BBB"))
        fake.raise_on_book_call = 3                                      # 2nd iteration
        adapter = ExchangeAdapter(fake)
        insts, _ = select_instruments(adapter.instruments(), None)
        with tempfile.TemporaryDirectory() as d, quiet():
            code = run_bot(adapter, insts, default_test_cfg(), False, 0.0, Path(d) / "l.csv",
                           max_iterations=10, sleep_fn=lambda s: None)
        self.assertEqual(code, EXIT_EXCEPTION)
        deleted = {c[1] for c in fake.calls if c[0] == "delete_all"}
        self.assertEqual(deleted, {"AAA", "BBB"})
        self.assertEqual(sum(len(v) for v in fake.orders.values()), 0)

    def test_ctrl_c_leaves_no_orders(self):
        fake = make_fake()
        adapter = ExchangeAdapter(fake)
        insts, _ = select_instruments(adapter.instruments(), None)
        n = {"i": 0}

        def sleeper(s):
            n["i"] += 1
            if n["i"] == 2:
                raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as d, quiet():
            code = run_bot(adapter, insts, default_test_cfg(), False, 0.0, Path(d) / "l.csv",
                           sleep_fn=sleeper)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(fake.orders["AAA"]), 0)

    # 14
    def test_dry_run_sends_nothing_including_cleanup(self):
        fake = make_fake(ids=("AAA", "BBB"))
        adapter = ExchangeAdapter(fake)
        insts, _ = select_instruments(adapter.instruments(), None)
        with tempfile.TemporaryDirectory() as d, quiet() as out:
            code = run_bot(adapter, insts, default_test_cfg(), True, 0.0, Path(d) / "l.csv",
                           max_iterations=4, sleep_fn=lambda s: None)
            text = out.getvalue()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fake.calls, [])
        self.assertIn("would INSERT", text)
        self.assertEqual(text.count("would INSERT"), 4)                  # 2 instruments x 2 sides, once

    # 15
    def test_startup_validation(self):
        self.assertIsNotNone(validate_params(10, 95, 2, 0.05))
        self.assertIsNotNone(validate_params(10, 91, 2, 0.05))
        self.assertIsNone(validate_params(10, 90, 2, 0.05))
        self.assertIsNotNone(validate_params(0, 10, 2, 0.05))
        self.assertIsNotNone(validate_params(1, 0, 2, 0.05))
        with quiet() as out:
            code = main(["run", "--cap", "95", "--size", "10"], exchange_factory=lambda: self.fail("connected"))
        self.assertEqual(code, EXIT_STARTUP)

    def test_cli_unknown_instrument_is_startup_error_and_dry_run_default(self):
        fake = make_fake()
        with quiet():
            code = main(["run", "--instruments", "NOPE"], exchange_factory=lambda: fake)
        self.assertEqual(code, EXIT_STARTUP)
        self.assertEqual(fake.calls, [])

    def test_only_tradable_stocks_selected(self):
        fake = make_fake(ids=("AAA",))
        fake.add_instrument("FUT", 1.0, stock=False)
        fake.add_instrument("PAUSED", 0.1, paused=True)
        sel, err = select_instruments(ExchangeAdapter(fake).instruments(), None)
        self.assertIsNone(err)
        self.assertEqual(set(sel), {"AAA"})

    def test_adapter_accepts_tuple_levels(self):
        fake = make_fake()
        fake.get_last_price_book = lambda iid: SimpleNamespace(bids=[(100.0, 5)], asks=[(101.0, 5)])
        self.assertEqual(ExchangeAdapter(fake).book("AAA"), ([(100.0, 5)], [(101.0, 5)]))


class FlattenTests(unittest.TestCase):
    def run_flatten(self, fake, live=True, **kw):
        adapter = ExchangeAdapter(fake)
        with quiet():
            return flatten(adapter, list(fake.instruments), live, sleep_fn=lambda s: None, **kw)

    # 16
    def test_long_sells_exactly_the_position(self):
        fake = make_fake(bids=[(100.0, 50)], asks=[(101.0, 50)])
        fake.positions["AAA"] = 7
        remaining, code = self.run_flatten(fake)
        iocs = fake.inserts(ORDER_IOC)
        self.assertEqual([(c[2], c[3], c[4]) for c in iocs], [(ASK, 100.0, 7)])
        self.assertEqual((remaining, code), ({}, EXIT_OK))

    def test_short_buys_exactly_the_position(self):
        fake = make_fake(bids=[(100.0, 50)], asks=[(101.0, 50)])
        fake.positions["AAA"] = -7
        remaining, code = self.run_flatten(fake)
        iocs = fake.inserts(ORDER_IOC)
        self.assertEqual([(c[2], c[3], c[4]) for c in iocs], [(BID, 101.0, 7)])
        self.assertEqual(remaining, {})

    def test_flat_sends_nothing(self):
        fake = make_fake()
        remaining, code = self.run_flatten(fake)
        self.assertEqual(fake.inserts(), [])
        self.assertEqual((remaining, code), ({}, EXIT_OK))

    def test_empty_book_side_is_skipped_without_crash(self):
        fake = make_fake(bids=[], asks=[(101.0, 5)])
        fake.positions["AAA"] = 7
        remaining, code = self.run_flatten(fake, max_rounds=2)
        self.assertEqual(fake.inserts(), [])
        self.assertEqual(remaining, {"AAA": 7})
        self.assertEqual(code, EXIT_NOT_FLAT)

    def test_chunking_never_exceeds_position(self):
        fake = make_fake(bids=[(100.0, 500)], asks=[(101.0, 500)])
        fake.positions["AAA"] = 45
        remaining, _ = self.run_flatten(fake, chunk=20)
        self.assertEqual([c[4] for c in fake.inserts(ORDER_IOC)], [20, 20, 5])
        self.assertEqual(remaining, {})

    def test_cancels_own_orders_first_in_live_mode(self):
        fake = make_fake()
        self.run_flatten(fake)
        self.assertEqual(fake.calls[0], ("delete_all", "AAA"))

    def test_dry_run_sends_nothing(self):
        fake = make_fake(bids=[(100.0, 50)], asks=[(101.0, 50)])
        fake.positions["AAA"] = 7
        remaining, code = self.run_flatten(fake, live=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(code, EXIT_OK)

    def test_disconnected_flatten_exits(self):
        fake = make_fake()
        fake.positions["AAA"] = 7
        fake.connected = False
        _, code = self.run_flatten(fake, live=False)
        self.assertEqual(code, EXIT_STARTUP)
        self.assertEqual(fake.connect_calls, 0)


if __name__ == "__main__":
    sys.exit(main())
