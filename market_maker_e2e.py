#!/usr/bin/env python3
"""End-to-end Optibook market maker with full debug logging. ONE self-contained file.

  python market_maker_e2e.py test                      offline unit tests (never connects)
  python market_maker_e2e.py run [--live] [options]    DRY RUN by default (still connects, sends no orders)
  python market_maker_e2e.py summary [--log-dir logs]  paste-able report from the newest run's CSVs
  v2 tuning (from the first live log, NOT proven): fewer/smaller positions, add-side stops at --soft-lots,
  positions of >= soft-lots older than --hold-s are crossed out with IOC, per-stock PnL from the exchange's
  atomic positions+cash, default request budget 8/s.

STRATEGY (rule of thumb, NOT a calibrated model, no profit claim)
  Quote passively at the OTHERS' touch (our own resting volume is removed from the book first).
  Inventory: the side that adds exposure shrinks and moves away as |pos|/cap grows; the reducing side
  stays at the touch.  Spread gate: below --min-spread ticks only the reducing side is quoted.
GUARDS
  shock   mid jumps >= --shock-ticks between loops -> cancel that stock's orders, pause it, IOC-flatten any position
  news    any social-media post -> cancel EVERYTHING, pause all quoting --news-pause seconds
  stop    mid is --stop-ticks against our average entry (from real fill prices) -> cancel + IOC-flatten
  limits  cap <= 100 per stock, position+resting counted per side, cancel before re-quote, fresh
          positions/orders read every loop, request-rate limiter (--max-requests), 10 insert failures or
          |pos| >= --guard stops the bot; resting orders are deleted on exit.
DEBUG OUTPUT (logs/e2e_<stamp>_*.csv)
  ticks   one row per stock per loop: book, position, state/reason, wanted quotes, actions, loop ms
  events  every insert / cancel / IOC / delete-all / shock / news with result and error text
  fills   each of OUR trades with real price; entry average; mid at the fill
  pnl     cash, exchange pnl, mark-to-mid pnl, requests, throttling
  news    social-media posts
Everything marked VERIFY is an assumption about the optibook API shapes (see api_reference.md).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import signal
import sys
import tempfile
import time
import unittest
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

HARD_LIMIT = 100                 # per-instrument hard limit (net position)
BID, ASK = "bid", "ask"
MIN_SLEEP_S = 0.2
MAX_INSERT_FAILS = 10
EPS = 1e-9


# --------------------------------------------------------------------------- helpers
def decimals(tick: float) -> int:
    return max(0, -int(Decimal(str(tick)).normalize().as_tuple().exponent))


def to_ticks(price: float, tick: float) -> int:
    return int(round(price / tick))


def from_ticks(n: int, tick: float) -> float:
    return round(n * tick, decimals(tick))


def norm_side(side: Any) -> str:
    s = str(getattr(side, "value", side)).lower()
    return BID if s in ("bid", "buy", "b") else ASK if s in ("ask", "sell", "s", "offer") else s


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def epoch(x: Any) -> float:
    if isinstance(x, datetime):
        return x.timestamp()
    try:
        return float(x)
    except (TypeError, ValueError):
        return time.time()


@dataclass(frozen=True)
class Config:
    cap: int = 10                    # internal per-stock cap (<= 100)
    size: int = 2                    # base quote size, lots
    max_order_size: int = 5          # largest single order we will send
    soft_lots: int = 5               # |pos| at/above this: no more exposure-adding quotes
    hold_s: float = 45.0             # |pos| >= soft_lots held this long -> cross out with IOC (0 = off)
    edge_ticks: int = 0              # quote this many ticks further from the touch
    widen_ticks: float = 2.0         # extra ticks on the exposure-adding side at |pos| = cap
    min_spread_ticks: int = 1        # below this only the reducing side is quoted
    requote_ticks: int = 1           # keep a resting quote up to this many ticks less aggressive
    shock_ticks: float = 4.0
    shock_pause_s: float = 15.0
    news_pause_s: float = 30.0
    stop_ticks: float = 6.0          # 0 disables the stop
    guard: int = 90                  # stop the bot if |position| reaches this
    pnl_every_s: float = 10.0

    def __post_init__(self) -> None:
        if type(self.cap) is not int or not 1 <= self.cap <= HARD_LIMIT:
            raise ValueError("cap must be an integer 1..100")
        if type(self.size) is not int or self.size < 1:
            raise ValueError("size must be a positive integer")
        if type(self.max_order_size) is not int or not 1 <= self.size <= self.max_order_size:
            raise ValueError("size must be within max_order_size")
        if self.edge_ticks < 0 or self.widen_ticks < 0 or self.requote_ticks < 0 or self.min_spread_ticks < 1:
            raise ValueError("edge/widen/requote must be >= 0 and min_spread >= 1")
        if not 1 <= self.guard <= HARD_LIMIT:
            raise ValueError("guard must be 1..100")
        if type(self.soft_lots) is not int or not 1 <= self.soft_lots <= self.cap or self.hold_s < 0:
            raise ValueError("soft_lots must be an integer 1..cap and hold_s >= 0")


class RateLimiter:
    """Sliding 1s window over ALL exchange calls (reads and writes). The real limit is UNKNOWN."""

    def __init__(self, max_rps: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if type(max_rps) is not int or max_rps <= 0:
            raise ValueError("max_requests must be a positive integer")
        self.max_rps, self.clock, self.sleep = max_rps, clock, sleep
        self.stamps: List[float] = []
        self.total = 0
        self.throttled = 0

    def wait(self) -> None:
        while True:
            now = self.clock()
            self.stamps = [t for t in self.stamps if now - t < 1.0]
            if len(self.stamps) < self.max_rps:
                break
            self.throttled += 1
            self.sleep(max(0.005, 1.0 - (now - self.stamps[0])))
        self.stamps.append(self.clock())
        self.total += 1


# --------------------------------------------------------------------------- exchange gate
class Gate:
    """The ONLY code that touches the optibook client. Paced. In dry-run, writes go to a local virtual book."""

    def __init__(self, raw: Any, limiter: RateLimiter, live: bool):
        self.raw, self.limiter, self.live = raw, limiter, live
        self.virtual: Dict[str, List[SimpleNamespace]] = defaultdict(list)
        self.next_id = 1

    def connected(self) -> bool:
        try:
            return bool(self.raw.is_connected())
        except Exception:
            return False

    def ticks(self) -> Dict[str, float]:
        self.limiter.wait()
        return {iid: float(i.tick_size) for iid, i in self.raw.get_instruments().items()}       # VERIFY

    def book(self, iid: str) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
        self.limiter.wait()
        pb = self.raw.get_last_price_book(iid)
        if pb is None:
            return [], []
        f = lambda ls: [(float(l.price), int(l.volume)) for l in (ls or []) if int(l.volume) > 0]
        return f(pb.bids), f(pb.asks)

    def own_orders(self, iid: str) -> List[SimpleNamespace]:
        if not self.live:
            return list(self.virtual[iid])
        self.limiter.wait()
        raw = self.raw.get_outstanding_orders(iid) or {}
        items = raw.values() if isinstance(raw, dict) else raw
        return [SimpleNamespace(order_id=o.order_id, side=norm_side(o.side), price=float(o.price),
                                volume=int(o.volume)) for o in items]

    def pos_cash(self) -> Tuple[Dict[str, int], Dict[str, float]]:
        """ONE call: per-stock position and cash are consistent with each other (needed for honest PnL)."""
        self.limiter.wait()
        try:
            raw = self.raw.get_positions_and_cash() or {}                                         # VERIFY shape
            return ({k: int(v["volume"]) for k, v in raw.items()},
                    {k: float(v["cash"]) for k, v in raw.items()})
        except (AttributeError, KeyError, TypeError):
            self.limiter.wait()
            return {k: int(v) for k, v in (self.raw.get_positions() or {}).items()}, {}

    def exch_pnl(self) -> Optional[float]:
        self.limiter.wait()
        try:
            return float(self.raw.get_pnl())
        except Exception:
            return None

    def own_trades(self, iid: str) -> List[Any]:
        if not self.live:
            return []
        self.limiter.wait()
        try:
            return list(self.raw.poll_new_trades(iid) or [])
        except Exception:
            return []

    def feed(self) -> List[Tuple[str, str, str]]:
        self.limiter.wait()
        try:
            raw = self.raw.poll_new_social_media_feeds() or []
        except Exception:
            return []
        return [(str(getattr(f, "timestamp", "") or ""), str(getattr(f, "post", "") or ""),
                 str(getattr(f, "meta_data", "") or "")) for f in raw]

    def insert(self, iid: str, side: str, price: float, volume: int, ioc: bool = False) -> Tuple[bool, Any, str]:
        if not self.live:
            if ioc:
                return True, None, "dry"
            oid = f"dry{self.next_id}"
            self.next_id += 1
            self.virtual[iid].append(SimpleNamespace(order_id=oid, side=side, price=price, volume=volume))
            return True, oid, "dry"
        self.limiter.wait()
        try:
            r = self.raw.insert_order(iid, price=price, volume=volume, side=side,
                                      order_type="ioc" if ioc else "limit")                       # VERIFY
        except Exception as e:
            return False, None, f"{type(e).__name__}: {e}"
        ok = bool(getattr(r, "success", False))
        return ok, getattr(r, "order_id", None), str(getattr(r, "error_reason", "") or "")

    def cancel(self, iid: str, oid: Any) -> Tuple[bool, str]:
        if not self.live:
            self.virtual[iid] = [o for o in self.virtual[iid] if o.order_id != oid]
            return True, "dry"
        self.limiter.wait()
        try:
            r = self.raw.delete_order(iid, order_id=oid)                                          # VERIFY
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if r is None:
            return False, "no response"                       # unconfirmed: never treat as cancelled
        return bool(getattr(r, "success", r is True)), str(getattr(r, "error_reason", "") or "")

    def delete_all(self, iid: str) -> Tuple[bool, str]:
        if not self.live:
            self.virtual[iid] = []
            return True, "dry"
        self.limiter.wait()
        try:
            r = self.raw.delete_orders(iid)                                                       # VERIFY
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return bool(getattr(r, "success", True)), str(getattr(r, "error_reason", "") or "")


# --------------------------------------------------------------------------- pure strategy
def clean_book(levels: List[Tuple[float, int]], own: List[SimpleNamespace], side: str,
               tick: float) -> List[Tuple[float, int]]:
    """Remove our own resting volume so we never treat our own quote as the market."""
    mine: Dict[int, int] = defaultdict(int)
    for o in own:
        if o.side == side:
            mine[to_ticks(o.price, tick)] += o.volume
    out = []
    for p, v in levels:
        v -= mine.get(to_ticks(p, tick), 0)
        if v > 0:
            out.append((p, v))
    return out


def target_quotes(bid_t: int, ask_t: int, pos: int, cfg: Config) -> Dict[str, Optional[Tuple[int, int]]]:
    """Desired (price_in_ticks, volume) per side from the CLEANED touch. Pure."""
    frac = min(1.0, abs(pos) / cfg.cap)
    spread = ask_t - bid_t
    out: Dict[str, Optional[Tuple[int, int]]] = {BID: None, ASK: None}
    for side in (BID, ASK):
        adding = (side == BID and pos >= 0) or (side == ASK and pos <= 0)
        if adding and (spread < cfg.min_spread_ticks or abs(pos) >= cfg.soft_lots):
            continue
        if adding:
            vol = max(1, int(cfg.size * (1 - frac))) if frac < 1 else 0
            off = cfg.edge_ticks + int(math.floor(cfg.widen_ticks * frac + EPS))
        else:
            vol, off = cfg.size, cfg.edge_ticks
        room = cfg.cap - pos if side == BID else cfg.cap + pos
        vol = min(vol, room, cfg.max_order_size)
        px = bid_t - off if side == BID else ask_t + off
        if vol > 0 and px > 0:
            out[side] = (px, vol)
    return out


# --------------------------------------------------------------------------- bot
@dataclass
class Stock:
    tick: float
    prev_mid: Optional[float] = None
    shock_until: float = 0.0
    last_pos: Optional[int] = None
    pos_t: int = 0
    avg: Optional[float] = None
    since_poll: int = 0
    mid: Optional[float] = None
    held_since: Optional[float] = None
    pos_prev: int = 0
    base: Optional[float] = None      # cash + pos*mid at the first loop (PnL baseline)


class Bot:
    def __init__(self, gate: Gate, ticks: Dict[str, float], cfg: Config, outdir: Path, stamp: str,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.g, self.cfg, self.clock, self.sleep = gate, cfg, clock, sleep
        self.st = {iid: Stock(t) for iid, t in ticks.items()}
        self.news_until = 0.0
        self.loop = 0
        self.insert_fails = 0
        self.base_pnl: Optional[float] = None
        self.last_pnl_t = -1e9
        self.counts: Counter = Counter()
        outdir.mkdir(parents=True, exist_ok=True)
        self.files = {}
        self.w = {}
        for name, header in (
            ("ticks", "utc,t_epoch,loop,instrument,bid,ask,mid,spread_ticks,pos,out_bid,out_ask,state,want_bid,want_ask,acts,loop_ms"),
            ("events", "utc,instrument,action,side,price,volume,order_id,ok,error,why"),
            ("fills", "utc,instrument,side,price,volume,order_id,trade_id,ex_ts_epoch,mid_loop,pos_after,avg_entry"),
            ("pnl", "utc,t_epoch,exch_pnl,mid_pnl,gross_pos,positions,requests,throttled,by_stock"),
            ("news", "utc,exchange_ts,post,meta"),
        ):
            f = open(outdir / f"e2e_{stamp}_{name}.csv", "w", newline="")
            self.files[name] = f
            self.w[name] = csv.writer(f)
            self.w[name].writerow(header.split(","))

    # ---- logging
    def ev(self, iid, action, side="", price="", volume="", oid="", ok="", err="", why="") -> None:
        self.counts[action] += 1
        self.w["events"].writerow([utc(), iid, action, side, price, volume, oid, ok, err, why])

    def flush(self) -> None:
        for f in self.files.values():
            f.flush()

    def close(self) -> None:
        self.flush()
        for f in self.files.values():
            f.close()

    # ---- order actions (all return success flags; every call is logged)
    def do_insert(self, iid, side, px_t, vol, why, ioc=False) -> bool:
        tick = self.st[iid].tick
        price = from_ticks(px_t, tick)
        ok, oid, err = self.g.insert(iid, side, price, vol, ioc)
        self.ev(iid, "IOC" if ioc else "INSERT", side, price, vol, oid or "", ok, err, why)
        self.insert_fails = 0 if ok else self.insert_fails + 1
        return ok

    def do_cancel(self, iid, o, why) -> bool:
        ok, err = self.g.cancel(iid, o.order_id)
        self.ev(iid, "CANCEL", o.side, o.price, o.volume, o.order_id, ok, err, why)
        return ok

    def do_delete_all(self, iid, why) -> bool:
        ok, err = self.g.delete_all(iid)
        self.ev(iid, "DELETE_ALL", ok=ok, err=err, why=why)
        return ok

    def flatten(self, iid, pos, bid, ask, why) -> bool:
        """Cancel everything on the stock, then IOC across the spread. Never overshoots through zero."""
        st = self.st[iid]
        if not self.do_delete_all(iid, why):
            return False                                          # cannot confirm: do not send more
        side = ASK if pos > 0 else BID
        px_t = to_ticks(bid if pos > 0 else ask, st.tick)
        vol = min(abs(pos), self.cfg.max_order_size)
        return self.do_insert(iid, side, px_t, vol, why, ioc=True)

    # ---- fills
    def read_fills(self, iid, pos, mid) -> None:
        st = self.st[iid]
        for tr in self.g.own_trades(iid):
            side, price, vol = norm_side(tr.side), float(tr.price), int(tr.volume)
            q = vol if side == BID else -vol
            p0 = st.pos_t
            if p0 == 0 or (p0 > 0) == (q > 0):
                st.avg = (abs(p0) * (st.avg if st.avg is not None else price) + vol * price) / (abs(p0) + vol)
            elif p0 + q == 0:
                st.avg = None
            elif (p0 + q > 0) != (p0 > 0):
                st.avg = price
            st.pos_t = p0 + q
            self.counts["FILL"] += 1
            self.w["fills"].writerow([utc(), iid, side, price, vol, getattr(tr, "order_id", ""),
                                      getattr(tr, "trade_id", ""), epoch(getattr(tr, "timestamp", None)),
                                      "" if mid is None else mid, st.pos_t,
                                      "" if st.avg is None else round(st.avg, 6)])

    # ---- one stock, one loop
    def handle(self, iid: str, pos: int, now: float, cash: Optional[float] = None) -> str:
        cfg, st = self.cfg, self.st[iid]
        tick = st.tick
        t0 = time.perf_counter()
        bids, asks = self.g.book(iid)
        own = self.g.own_orders(iid)
        out_bid = sum(o.volume for o in own if o.side == BID)
        out_ask = sum(o.volume for o in own if o.side == ASK)
        cb, ca = clean_book(bids, own, BID, tick), clean_book(asks, own, ASK, tick)
        acts = 0
        state, want_b, want_a = "quote", "", ""
        mid = None
        bid = ask = None
        if not cb or not ca:
            state = "no_book"
        else:
            bid, ask = cb[0][0], ca[0][0]
            if bid >= ask:
                state = "crossed"
            else:
                mid = (bid + ask) / 2
        st.mid = mid if mid is not None else st.mid

        if st.last_pos is None or pos != st.last_pos or st.since_poll >= 5:
            self.read_fills(iid, pos, mid)
            st.since_poll = 0
        else:
            st.since_poll += 1
        if pos != st.pos_t:                                      # resync with the exchange's truth
            st.pos_t = pos
        if pos == 0:
            st.avg = None
            st.held_since = None
        else:
            if st.avg is None and mid is not None:
                st.avg = mid                                     # unknown entry: assume now
            if st.held_since is None or (pos > 0) != (st.pos_prev > 0):
                st.held_since = now
        st.pos_prev = pos
        if cash is not None and mid is not None and st.base is None:
            st.base = cash + pos * mid
        st.last_pos = pos

        if mid is None:
            return self.row(iid, bid, ask, mid, pos, out_bid, out_ask, state, "", "", acts, t0)

        # ---- shock / stop guards
        shocked = st.prev_mid is not None and abs(mid - st.prev_mid) >= cfg.shock_ticks * tick - EPS
        st.prev_mid = mid
        if shocked:
            st.shock_until = now + cfg.shock_pause_s
            self.ev(iid, "SHOCK", price=mid, why=f"mid jump >= {cfg.shock_ticks} ticks")
            if pos:
                acts += 1
                self.flatten(iid, pos, bid, ask, "shock")
            else:
                self.do_delete_all(iid, "shock")
            return self.row(iid, bid, ask, mid, pos, out_bid, out_ask, "shock", "", "", acts, t0)
        if cfg.stop_ticks > 0 and pos and st.avg is not None:
            adverse = ((st.avg - mid) if pos > 0 else (mid - st.avg)) / tick
            if adverse >= cfg.stop_ticks - EPS:
                st.shock_until = now + cfg.shock_pause_s
                acts += 1
                self.flatten(iid, pos, bid, ask, f"stop adverse={adverse:.1f}t")
                return self.row(iid, bid, ask, mid, pos, out_bid, out_ask, "stop", "", "", acts, t0)

        if (cfg.hold_s > 0 and pos and abs(pos) >= cfg.soft_lots and st.held_since is not None
                and now - st.held_since >= cfg.hold_s and now >= self.news_until):
            acts += 1
            self.flatten(iid, pos, bid, ask, f"age_exit held={now - st.held_since:.0f}s")
            return self.row(iid, bid, ask, mid, pos, out_bid, out_ask, "age_exit", "", "", acts, t0)
        if now < self.news_until:
            state = "news_pause"
            if own:
                self.do_delete_all(iid, "news_pause")
                acts += 1
        elif now < st.shock_until:
            state = "shock_pause"
            if own:
                self.do_delete_all(iid, "shock_pause")
                acts += 1
        else:
            bt, at = to_ticks(bid, tick), to_ticks(ask, tick)
            want = target_quotes(bt, at, pos, cfg)
            if at - bt < cfg.min_spread_ticks:
                state = "quote_reduce_only"
            if want[BID]:
                want_b = f"{from_ticks(want[BID][0], tick)}x{want[BID][1]}"
            if want[ASK]:
                want_a = f"{from_ticks(want[ASK][0], tick)}x{want[ASK][1]}"
            acts += self.reconcile(iid, own, want, tick)
        return self.row(iid, bid, ask, mid, pos, out_bid, out_ask, state, want_b, want_a, acts, t0)

    def reconcile(self, iid, own, want, tick) -> int:
        acts = 0
        for side in (BID, ASK):
            mine = [o for o in own if o.side == side]
            w = want[side]
            keep = None
            if w is not None:
                sign = 1 if side == BID else -1
                for o in mine:
                    if 0 <= sign * (w[0] - to_ticks(o.price, tick)) <= self.cfg.requote_ticks and o.volume <= w[1]:
                        keep = o
                        break
            failed = False
            for o in mine:
                if o is keep:
                    continue
                acts += 1
                if not self.do_cancel(iid, o, "requote" if w else "no_target"):
                    failed = True                                 # may have filled: re-read before inserting
            if w is not None and keep is None and not failed:
                acts += 1
                self.do_insert(iid, side, w[0], w[1], "quote")
        return acts

    def row(self, iid, bid, ask, mid, pos, ob, oa, state, wb, wa, acts, t0) -> str:
        st = self.st[iid]
        spread = "" if bid is None or ask is None else to_ticks(ask, st.tick) - to_ticks(bid, st.tick)
        self.counts["state_" + state] += 1
        self.w["ticks"].writerow([utc(), round(time.time(), 3), self.loop, iid,
                                  "" if bid is None else bid, "" if ask is None else ask,
                                  "" if mid is None else round(mid, 6), spread, pos, ob, oa, state, wb, wa,
                                  acts, round((time.perf_counter() - t0) * 1000, 1)])
        return state

    # ---- one loop
    def step(self) -> Optional[str]:
        if not self.g.connected():
            return "DISCONNECTED"
        now = self.clock()
        self.loop += 1
        pos, cash = self.g.pos_cash()
        posts = self.g.feed()
        if posts:
            self.news_until = now + self.cfg.news_pause_s
            for ts, post, meta in posts:
                self.w["news"].writerow([utc(), ts, post, meta])
                self.ev("*", "NEWS", why=post[:80])
            for iid in self.st:
                self.do_delete_all(iid, "news")
        for iid in self.st:
            self.handle(iid, pos.get(iid, 0), now, cash.get(iid) if cash else None)
        if now - self.last_pnl_t >= self.cfg.pnl_every_s:
            self.last_pnl_t = now
            self.log_pnl(pos, cash)
        self.flush()
        worst = max((abs(pos.get(i, 0)) for i in self.st), default=0)
        if worst >= self.cfg.guard:
            return f"LIMIT_GUARD |pos|={worst}"
        if self.insert_fails >= MAX_INSERT_FAILS:
            return f"{self.insert_fails} consecutive failed inserts"
        return None

    def log_pnl(self, pos, cash) -> None:
        """mid_pnl = sum over stocks of (cash + pos*mid - baseline), all from ONE atomic positions+cash read."""
        exch = self.g.exch_pnl()
        by, total = {}, 0.0
        for iid, st in self.st.items():
            if st.mid is not None and st.base is not None and iid in cash:
                by[iid] = round(cash[iid] + pos.get(iid, 0) * st.mid - st.base, 2)
                total += by[iid]
        self.w["pnl"].writerow([utc(), round(time.time(), 3), "" if exch is None else exch,
                                round(total, 2) if by else "", sum(abs(v) for v in pos.values()),
                                json.dumps({k: v for k, v in pos.items() if v}).replace(",", ";"),
                                self.g.limiter.total, self.g.limiter.throttled,
                                json.dumps(by).replace(",", ";")])

    def run(self, max_loops: Optional[int] = None) -> str:
        reason = "done"
        try:
            while True:
                stop = self.step()
                if stop:
                    reason = stop
                    break
                if max_loops is not None and self.loop >= max_loops:
                    break
                self.sleep(max(MIN_SLEEP_S, 0))
        except KeyboardInterrupt:
            reason = "interrupted"
        except Exception as e:
            import traceback
            traceback.print_exc()
            reason = f"exception {type(e).__name__}: {e}"
        finally:
            if self.g.connected():
                for iid in self.st:
                    try:
                        self.do_delete_all(iid, "shutdown")
                    except Exception:
                        pass
            else:
                self.ev("*", "DISCONNECTED", why="connection lost; exchange pulls resting orders itself, positions remain")
            self.close()
        return reason


# --------------------------------------------------------------------------- summary
def _rows(path: str) -> List[Dict[str, str]]:
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []


def summarize(logdir: str, stamp: Optional[str] = None, out=print) -> int:
    import bisect
    if stamp is None:
        c = glob.glob(os.path.join(logdir, "e2e_*_ticks.csv"))
        if not c:
            out(f"no e2e_*_ticks.csv in {logdir}")
            return 1
        stamp = os.path.basename(max(c, key=os.path.getmtime))[4:-10]
    p = lambda n: os.path.join(logdir, f"e2e_{stamp}_{n}.csv")
    ticks, ev, fills, pnl, news = (_rows(p(n)) for n in ("ticks", "events", "fills", "pnl", "news"))
    if not ticks:
        out("empty ticks file")
        return 1
    flt = lambda x: float(x) if x not in ("", None) else None
    t0, t1 = float(ticks[0]["t_epoch"]), float(ticks[-1]["t_epoch"])
    mins = max((t1 - t0) / 60, 1e-9)
    loop_t: Dict[int, float] = {}
    for r in ticks:
        loop_t.setdefault(int(r["loop"]), float(r["t_epoch"]))
    ts_ = [loop_t[k] for k in sorted(loop_t)]
    gaps = [b - a for a, b in zip(ts_, ts_[1:])]
    out(f"RUN {stamp}  {mins:.1f} min  loops={len(loop_t)}  loop time avg/max "
        f"{(sum(gaps) / len(gaps) if gaps else 0):.2f}/{max(gaps, default=0):.2f}s  news posts={len(news)}")
    by_last: Dict[str, float] = {}
    if pnl:
        mp = [flt(r["mid_pnl"]) for r in pnl if flt(r["mid_pnl"]) is not None]
        ep = [flt(r["exch_pnl"]) for r in pnl if flt(r["exch_pnl"]) is not None]
        if mp:
            out(f"PNL mid-marked    first/min/max/last {mp[0]:.2f}/{min(mp):.2f}/{max(mp):.2f}/{mp[-1]:.2f}")
        if ep:
            out(f"PNL exchange      first/min/max/last {ep[0]:.2f}/{min(ep):.2f}/{max(ep):.2f}/{ep[-1]:.2f}  (last-trade marked)")
        reqs = [(float(r["t_epoch"]), int(r["requests"])) for r in pnl]
        rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(reqs, reqs[1:]) if b[0] > a[0]]
        out(f"requests total={reqs[-1][1]} avg/s={reqs[-1][1] / max(t1 - t0, 1):.1f} peak/s={max(rates, default=0):.1f} "
            f"throttle waits={pnl[-1]['throttled']} ({int(pnl[-1]['throttled']) / max(1, len(loop_t)):.1f} per loop)")
        try:
            by_last = json.loads(pnl[-1]["by_stock"].replace(";", ","))
        except (KeyError, ValueError):
            pass
    out("states: " + ", ".join(f"{k}={v}" for k, v in Counter(r["state"] for r in ticks).most_common()))
    out("events: " + ", ".join(f"{k}={v}" for k, v in Counter(r["action"] for r in ev).most_common()))
    for (a, e), n in Counter((r["action"], r["error"][:70]) for r in ev if r["ok"] == "False" and r["error"]).most_common(5):
        out(f"  ERROR x{n} {a}: {e}")
    if any(r["action"] == "DISCONNECTED" for r in ev):
        out("  !! DISCONNECTED during the run (check the terminal for 'Max requests per second' or 'kicked')")

    series: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    tick_of: Dict[str, float] = {}
    for iid in {r["instrument"] for r in ticks}:
        tick_of[iid] = _tick_of([r for r in ticks if r["instrument"] == iid])
    for r in ticks:
        if r["mid"] != "":
            series[r["instrument"]].append((float(r["t_epoch"]), float(r["mid"])))
    times = {k: [t for t, _ in v] for k, v in series.items()}

    def mid_at(iid, t, after=False):
        v, ts = series.get(iid, []), times.get(iid, [])
        k = bisect.bisect_left(ts, t) if after else bisect.bisect_right(ts, t) - 1
        return v[k][1] if 0 <= k < len(v) else None

    ioc_ids = {e["order_id"] for e in ev if e["action"] == "IOC" and e["ok"] == "True" and e["order_id"]}
    passive = [f for f in fills if f["order_id"] not in ioc_ids]
    H = (5, 15, 30, 60)

    def agg(fs):
        r = {"lots": 0, "edge": 0.0}
        for h in H:
            r[h] = [0.0, 0]
        for f in fs:
            iid, sg, v, px = f["instrument"], (1 if f["side"] == BID else -1), int(f["volume"]), float(f["price"])
            t, tk = float(f["ex_ts_epoch"]), tick_of.get(f["instrument"], 0.1)
            m0 = mid_at(iid, t)
            if m0 is None:
                continue
            r["lots"] += v
            r["edge"] += sg * (m0 - px) / tk * v
            for h in H:
                m = mid_at(iid, t + h, after=True)
                if m is not None and t + h <= t1:
                    r[h][0] += sg * (m - px) / tk * v
                    r[h][1] += v
        return r

    def fmt(r):
        av = lambda a, n: "  n/a" if not n else f"{a / n:+5.2f}"
        return (f"lots {r['lots']:>4}  edge {av(r['edge'], r['lots'])}  after 5s {av(*r[5])}  15s {av(*r[15])}  "
                f"30s {av(*r[30])}  60s {av(*r[60])}")

    out("")
    out("PASSIVE fills, ticks per lot vs our fill price (+ = good; 'edge' = vs mid at fill; after N s = vs mid N s later)")
    out("  all      " + fmt(agg(passive)))
    out("")
    out("stock   spr1% quote% pos[min,max,last]  pnl(mid)   passive fills")
    for iid in sorted(tick_of):
        R = [r for r in ticks if r["instrument"] == iid]
        pos = [int(r["pos"]) for r in R]
        sp1 = 100 * sum(1 for r in R if r["spread_ticks"] == "1") / len(R)
        qt = 100 * sum(1 for r in R if r["want_bid"] or r["want_ask"]) / len(R)
        pl = by_last.get(iid)
        out(f"{iid:<7}{sp1:>5.0f} {qt:>6.0f}  [{min(pos):>4},{max(pos):>4},{pos[-1]:>4}]  {'   n/a' if pl is None else f'{pl:>8.2f}'}   "
            + fmt(agg([f for f in passive if f["instrument"] == iid])))

    out("")
    out("by inventory before the fill (ADD = fill increased |position|, RED = reduced it)")
    grp: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for f in passive:
        sg, v, pa = (1 if f["side"] == BID else -1), int(f["volume"]), int(f["pos_after"])
        pb = pa - sg * v
        a = abs(pb)
        grp[("ADD" if abs(pa) > abs(pb) else "RED") + " |pos| " + ("0" if a == 0 else "1-5" if a <= 5 else "6-10" if a <= 10 else "11+")].append(f)
    for k in sorted(grp):
        out(f"  {k:<14}" + fmt(agg(grp[k])))

    exits = [e for e in ev if e["action"] == "IOC"]
    if exits:
        out("")
        out("IOC exits (shock / stop / age): slippage vs mid 1s before, and exit price vs mid 30s later (+ = exit beat holding)")
        for e in exits[:14]:
            t = datetime.fromisoformat(e["utc"].replace("Z", "+00:00")).timestamp()
            m0, m30, px, tk = mid_at(e["instrument"], t - 1.0), mid_at(e["instrument"], t + 30, True), float(e["price"]), tick_of.get(e["instrument"], 0.1)
            slip = "n/a" if m0 is None else f"{((m0 - px) if e['side'] == ASK else (px - m0)) / tk:+.1f}t"
            hold = "n/a" if m30 is None or t + 30 > t1 else f"{((px - m30) if e['side'] == ASK else (m30 - px)) / tk:+.1f}t"
            out(f"  {e['utc'][11:19]} {e['instrument']:<5}{e['side']} {e['volume']:>2} @ {px:<8g} {e['why'][:22]:<22} slip {slip:<7} vs hold30 {hold}")
        if len(exits) > 14:
            out(f"  ... {len(exits) - 14} more")
    out("")
    out("Read: few fills / minutes => noise (a stock needs ~100+ lots to say anything). PnL is NOT proof of an edge.")
    return 0


def _tick_of(rows: List[Dict[str, str]]) -> float:
    ps = sorted({round(float(r[k]), 6) for r in rows for k in ("bid", "ask") if r[k] != ""})
    steps = [y - x for x, y in zip(ps, ps[1:]) if y - x > 1e-9]
    return min(steps) if steps else 0.1


# --------------------------------------------------------------------------- CLI
def connect() -> Any:
    from optibook.synchronous_client import Exchange                                              # VERIFY
    ex = Exchange()
    ex.connect()
    return ex


def cmd_run(a: argparse.Namespace, factory: Callable[[], Any]) -> int:
    try:
        cfg = Config(cap=a.cap, size=a.size, max_order_size=a.max_order_size or max(a.size, 5), edge_ticks=a.edge,
                     widen_ticks=a.widen, min_spread_ticks=a.min_spread, requote_ticks=a.requote,
                     shock_ticks=a.shock_ticks, shock_pause_s=a.shock_pause, news_pause_s=a.news_pause,
                     stop_ticks=a.stop_ticks, guard=a.guard, soft_lots=min(a.soft_lots, a.cap), hold_s=a.hold_s)
        limiter = RateLimiter(a.max_requests)
    except ValueError as e:
        print("ERROR:", e)
        return 1
    raw = factory()
    gate = Gate(raw, limiter, a.live)
    if not gate.connected():
        print("NOT CONNECTED. No retries, no orders.")
        return 1
    ticks = gate.ticks()
    if a.instruments:
        want = [x.strip() for x in a.instruments.split(",") if x.strip()]
        missing = [x for x in want if x not in ticks]
        if missing:
            print("ERROR: unknown instruments", missing)
            return 1
        ticks = {k: ticks[k] for k in want}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print("LIVE ORDERS" if a.live else "DRY RUN (real market data, no orders sent)")
    print(f"stocks={list(ticks)} cap={cfg.cap} size={cfg.size} max_order={cfg.max_order_size} soft={cfg.soft_lots} "
          f"hold_s={cfg.hold_s:g} max_requests={a.max_requests}/s")
    est = (2 + 2 * len(ticks) + len(ticks) // 2 + 3) / a.max_requests
    print(f"estimated loop time ~{est:.1f}s (calls/loop ~{2 + 2 * len(ticks) + len(ticks) // 2 + 3}). "
          + ("WARNING: >2s means quotes sit stale and get picked off; trade fewer stocks." if est > 2 else "ok"))
    print(f"logs: {a.log_dir}/e2e_{stamp}_*.csv   summary: python {os.path.basename(__file__)} summary --log-dir {a.log_dir}")
    print("One connection per team: this kicked any other session. Ctrl-C stops and deletes resting orders.")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    bot = Bot(gate, ticks, cfg, Path(a.log_dir), stamp)
    reason = bot.run()
    print("stopped:", reason)
    summarize(a.log_dir, stamp)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true")
    r.add_argument("--instruments", default=None)
    r.add_argument("--cap", type=int, default=10)
    r.add_argument("--size", type=int, default=2)
    r.add_argument("--max-order-size", type=int, default=None, help="default = max(size, 5); also the IOC exit chunk")
    r.add_argument("--max-requests", type=int, default=8, help="calls/second budget (reads+writes); real limit UNKNOWN")
    r.add_argument("--soft-lots", type=int, default=5, help="|pos| at/above this: no exposure-adding quotes")
    r.add_argument("--hold-s", type=float, default=45.0, help="cross out |pos|>=soft held this long (0 = off)")
    r.add_argument("--edge", type=int, default=0)
    r.add_argument("--widen", type=float, default=2.0)
    r.add_argument("--min-spread", type=int, default=1)
    r.add_argument("--requote", type=int, default=1)
    r.add_argument("--shock-ticks", type=float, default=4.0)
    r.add_argument("--shock-pause", type=float, default=15.0)
    r.add_argument("--news-pause", type=float, default=30.0)
    r.add_argument("--stop-ticks", type=float, default=6.0, help="0 = off")
    r.add_argument("--guard", type=int, default=90)
    r.add_argument("--log-dir", default="logs")
    s = sub.add_parser("summary")
    s.add_argument("--log-dir", default="logs")
    s.add_argument("--stamp", default=None)
    sub.add_parser("test")
    return p


def main(argv: Optional[List[str]] = None, factory: Callable[[], Any] = connect) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "test":
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    if a.cmd == "summary":
        return summarize(a.log_dir, a.stamp)
    return cmd_run(a, factory)


# --------------------------------------------------------------------------- tests
class FakeExchange:
    def __init__(self, ids=("AAA",), tick=0.1):
        self.connected = True
        self.instr = {i: SimpleNamespace(tick_size=tick) for i in ids}
        self.books = {i: ([SimpleNamespace(price=100.0, volume=50)], [SimpleNamespace(price=100.1, volume=50)]) for i in ids}
        self.orders: Dict[str, Dict[int, SimpleNamespace]] = {i: {} for i in ids}
        self.pos = {i: 0 for i in ids}
        self.trades: Dict[str, List[SimpleNamespace]] = {i: [] for i in ids}
        self.feedq: List[SimpleNamespace] = []
        self.calls: List[Tuple] = []
        self.nid = 1
        self.cash = 0.0
        self.cash_by: Dict[str, float] = {}
        self.fail_insert = False

    def set_book(self, iid, bid, ask, bv=50, av=50):
        self.books[iid] = ([SimpleNamespace(price=bid, volume=bv)], [SimpleNamespace(price=ask, volume=av)])

    def is_connected(self): return self.connected
    def get_instruments(self): return self.instr
    def get_positions(self): return dict(self.pos)
    def get_cash(self): return self.cash

    def get_positions_and_cash(self):
        return {i: {"volume": v, "cash": self.cash_by.get(i, 0.0)} for i, v in self.pos.items()}
    def get_pnl(self): return self.cash

    def get_last_price_book(self, iid):
        b, a = self.books[iid]
        # show our resting orders in the book like the real exchange does
        bb = list(b) + [SimpleNamespace(price=o.price, volume=o.volume) for o in self.orders[iid].values() if o.side == BID]
        aa = list(a) + [SimpleNamespace(price=o.price, volume=o.volume) for o in self.orders[iid].values() if o.side == ASK]
        return SimpleNamespace(bids=sorted(bb, key=lambda l: -l.price), asks=sorted(aa, key=lambda l: l.price))

    def get_outstanding_orders(self, iid): return dict(self.orders[iid])

    def insert_order(self, iid, price, volume, side, order_type="limit"):
        self.calls.append(("insert", iid, side, round(price, 4), volume, order_type))
        if self.fail_insert:
            return SimpleNamespace(success=False, order_id=None, error_reason="rejected")
        oid = self.nid
        self.nid += 1
        if order_type == "limit":
            self.orders[iid][oid] = SimpleNamespace(order_id=oid, instrument_id=iid, price=price, volume=volume, side=side)
        return SimpleNamespace(success=True, order_id=oid, error_reason=None)

    def delete_order(self, iid, order_id):
        self.calls.append(("cancel", iid, order_id))
        ok = self.orders[iid].pop(order_id, None) is not None
        return SimpleNamespace(success=ok, error_reason=None if ok else "unknown order")

    def delete_orders(self, iid):
        self.calls.append(("delete_all", iid))
        self.orders[iid].clear()
        return SimpleNamespace(success=True, error_reason=None)

    def poll_new_trades(self, iid):
        out, self.trades[iid] = self.trades[iid], []
        return out

    def poll_new_social_media_feeds(self):
        out, self.feedq = self.feedq, []
        return out

    def fill(self, iid, side, price, vol, oid=None):
        self.pos[iid] += vol if side == BID else -vol
        self.trades[iid].append(SimpleNamespace(timestamp=time.time(), order_id=oid or 0, trade_id=len(self.calls),
                                                instrument_id=iid, price=price, volume=vol, side=side))


class T(unittest.TestCase):
    def make(self, cfg=None, live=True, **kw):
        self.fake = FakeExchange(**kw)
        self.now = [1000.0]
        self.dir = tempfile.mkdtemp()
        gate = Gate(self.fake, RateLimiter(1000, clock=lambda: self.now[0], sleep=lambda s: None), live)
        cfg = cfg or Config(cap=10, size=3, max_order_size=5)
        self.bot = Bot(gate, gate.ticks(), cfg, Path(self.dir), "t", clock=lambda: self.now[0], sleep=lambda s: None)
        return self.bot

    def inserts(self): return [c for c in self.fake.calls if c[0] == "insert"]

    def test_quotes_at_touch_both_sides(self):
        self.make().step()
        self.assertEqual(sorted((c[2], c[3], c[4]) for c in self.inserts()), [(ASK, 100.1, 3), (BID, 100.0, 3)])

    def test_no_churn_when_market_static(self):
        b = self.make()
        for _ in range(5):
            b.step()
        self.assertEqual(len(self.inserts()), 2)
        self.assertEqual([c for c in self.fake.calls if c[0] == "cancel"], [])

    def test_own_quote_not_mistaken_for_market(self):
        b = self.make()
        b.step()
        self.fake.set_book("AAA", 99.9, 100.1)               # others' touch dropped; our 100.0 bid is alone
        b.step()                                             # cleaned bid = 99.9 -> our 100.0 is more aggressive: replace
        self.assertEqual(sorted(c[0] for c in self.fake.calls if c[0] == "cancel"), ["cancel"])
        self.assertIn((BID, 99.9), [(c[2], c[3]) for c in self.inserts()])

    def test_cap_blocks_exposure_adding_side(self):
        b = self.make()
        self.fake.pos["AAA"] = 10
        b.step()
        self.assertEqual([c for c in self.inserts() if c[2] == BID], [])

    def test_exposure_side_shrinks_and_widens(self):
        q = target_quotes(1000, 1001, 5, Config(cap=10, size=4, max_order_size=4, widen_ticks=2.0, soft_lots=8))
        self.assertEqual(q[BID], (999, 2))                  # half cap: size 4*0.5=2, widen 1 tick
        self.assertEqual(q[ASK], (1001, 4))

    def test_spread_gate_reduce_only(self):
        q = target_quotes(1000, 1001, 3, Config(cap=10, size=3, max_order_size=3, min_spread_ticks=2))
        self.assertIsNone(q[BID])
        self.assertIsNotNone(q[ASK])

    def test_news_cancels_everything_and_pauses(self):
        b = self.make(ids=("AAA", "BBB"))
        b.step()
        self.fake.feedq.append(SimpleNamespace(timestamp="t", post="NVDA beats", meta_data=None))
        b.step()
        self.assertEqual(sum(len(v) for v in self.fake.orders.values()), 0)
        n = len(self.inserts())
        self.now[0] += 10
        b.step()
        self.assertEqual(len(self.inserts()), n)             # still paused
        self.now[0] += 25
        b.step()
        self.assertGreater(len(self.inserts()), n)

    def test_shock_flattens_with_ioc(self):
        b = self.make()
        b.step()
        self.fake.pos["AAA"] = 6
        self.fake.set_book("AAA", 99.0, 99.1)                # -10 tick jump
        b.step()
        iocs = [c for c in self.inserts() if c[5] == "ioc"]
        self.assertEqual(iocs, [("insert", "AAA", ASK, 99.0, 5, "ioc")])
        self.assertEqual(self.fake.orders["AAA"], {})

    def test_stop_uses_real_fill_price(self):
        b = self.make(Config(cap=10, size=3, max_order_size=5, stop_ticks=3, shock_ticks=50))
        b.step()
        self.fake.fill("AAA", BID, 100.0, 3)
        b.step()
        self.fake.set_book("AAA", 99.6, 99.7)                # mid 99.65 vs entry 100.0 => 3.5 ticks adverse
        b.step()
        self.assertTrue([c for c in self.inserts() if c[5] == "ioc"])

    def test_dry_run_sends_nothing(self):
        b = self.make(live=False)
        b.step()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(len(b.g.virtual["AAA"]), 2)

    def test_cleanup_deletes_orders(self):
        b = self.make()
        self.assertEqual(b.run(max_loops=2), "done")
        self.assertEqual(self.fake.orders["AAA"], {})

    def test_disconnect_stops(self):
        b = self.make()
        self.fake.connected = False
        self.assertEqual(b.run(max_loops=5), "DISCONNECTED")

    def test_insert_failures_stop_bot(self):
        b = self.make(Config(cap=10, size=3, max_order_size=5))
        self.fake.fail_insert = True
        self.assertIn("failed inserts", b.run(max_loops=20))

    def test_limit_guard(self):
        b = self.make()
        self.fake.pos["AAA"] = 95
        self.assertIn("LIMIT_GUARD", b.run(max_loops=3))

    def test_config_rejects_cap_over_100_and_big_size(self):
        with self.assertRaises(ValueError):
            Config(cap=101)
        with self.assertRaises(ValueError):
            Config(size=5, max_order_size=3)

    def test_rate_limiter_spaces_calls(self):
        t = [0.0]
        rl = RateLimiter(5, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
        for _ in range(23):
            rl.wait()
        self.assertGreater(rl.throttled, 0)
        self.assertGreaterEqual(t[0], 3.0)

    def test_soft_lots_stops_adding_side(self):
        q = target_quotes(1000, 1001, 5, Config(cap=10, size=2, soft_lots=5))
        self.assertIsNone(q[BID])
        self.assertEqual(q[ASK], (1001, 2))
        q = target_quotes(1000, 1001, 4, Config(cap=10, size=2, soft_lots=5))
        self.assertIsNotNone(q[BID])

    def test_age_exit_crosses_spread_after_hold(self):
        b = self.make(Config(cap=10, size=2, max_order_size=5, soft_lots=5, hold_s=30, shock_ticks=50, stop_ticks=0))
        self.fake.pos["AAA"] = 6
        b.step()
        self.assertEqual([c for c in self.inserts() if c[5] == "ioc"], [])
        self.now[0] += 31
        b.step()
        self.assertEqual([c for c in self.inserts() if c[5] == "ioc"], [("insert", "AAA", ASK, 100.0, 5, "ioc")])

    def test_pnl_by_stock_from_atomic_read(self):
        b = self.make()
        b.step()
        self.fake.pos["AAA"] = 2
        self.fake.cash_by["AAA"] = -200.0                         # bought 2 at 100
        self.fake.set_book("AAA", 100.2, 100.3)                   # mid 100.25 -> +0.5
        self.now[0] += 11
        b.step()
        b.close()
        rows = _rows(os.path.join(self.dir, "e2e_t_pnl.csv"))
        self.assertAlmostEqual(float(rows[-1]["mid_pnl"]), 0.5, places=2)

    def test_disconnect_logged_without_cleanup_calls(self):
        b = self.make()
        b.step()
        n = len(self.fake.calls)
        self.fake.connected = False
        b.run(max_loops=3)
        self.assertEqual(len([c for c in self.fake.calls[n:] if c[0] == "delete_all"]), 0)
        b2 = _rows(os.path.join(self.dir, "e2e_t_events.csv"))
        self.assertIn("DISCONNECTED", [r["action"] for r in b2])

    def test_csvs_and_summary(self):
        b = self.make()
        b.step()
        self.fake.fill("AAA", BID, 100.0, 2)
        b.step()
        b.run(max_loops=3)
        lines = []
        self.assertEqual(summarize(self.dir, "t", out=lines.append), 0)
        text = "\n".join(lines)
        self.assertIn("AAA", text)
        self.assertIn("PASSIVE", text)
        self.assertTrue(os.path.getsize(os.path.join(self.dir, "e2e_t_fills.csv")) > 60)


if __name__ == "__main__":
    raise SystemExit(main())
