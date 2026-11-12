#!/usr/bin/env python3
"""Leader-jump follower (LJF) bot. Self-contained; stdlib + optibook only.

When a LEADER stock's mid jumps >= --jump-ticks within --window seconds, send ONE IOC
in its FOLLOWER in the same direction, then close that quantity by IOC after --hold
seconds, at target, or at the stop. The bot never leaves resting orders.

    DRY (connects, reads market data, sends nothing):  python ljf_bot.py run
    LIVE:                                               python ljf_bot.py run --live --size 20
    Offline replay of an old run log (no connection):   python ljf_bot.py replay logs/run_X.csv

ONE connection per team: stop every other bot first. Evidence is 4 MU->CRWV and
2 NVDA->SNDK events from 2026-11-12; replay fills are optimistic and prove nothing
about real depth. Pre-existing inventory is left untouched (the bot only closes what
it opened).
"""
import argparse
import csv
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HARD_LIMIT = 100
DEFAULT_PAIRS = "MU:CRWV:1.0,NVDA:SNDK:0.4"      # leader:follower:follower_ticks_per_leader_tick


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def top(book_side):
    """[(price, volume)] best-first from optibook PriceVolume list."""
    return [(float(l.price), int(l.volume)) for l in (book_side or []) if int(l.volume) > 0]


class Bot:
    def __init__(self, ex, args, live, clock=time.monotonic, out_dir=Path("logs")):
        self.ex, self.a, self.live, self.clock = ex, args, live, clock
        self.pairs = {}
        for item in args.pairs.split(","):
            leader, follower, beta = item.split(":")
            self.pairs[leader] = (follower, float(beta))
        self.names = list(self.pairs) + [f for f, _ in self.pairs.values()]
        inst = ex.get_instruments()
        missing = [n for n in self.names if n not in inst]
        if missing:
            raise SystemExit(f"unknown instruments: {missing}; available: {sorted(inst)}")
        self.tick = {n: float(inst[n].tick_size) for n in self.names}
        self.plimit = {n: getattr(inst[n], "price_limit", None) for n in self.names}
        self.hist = {n: deque() for n in self.names}          # (t, mid)
        self.book = {}                                        # n -> (bids, asks)
        self.last_trigger = {}
        self.trades = {}                                      # follower -> dict
        self.losses_in_row = 0
        self.realised = 0.0
        self.halted = False
        self.settle = 0.05 if clock is time.monotonic else 0.0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.f_ev = open(out_dir / f"ljf_events_{stamp}.csv", "w", newline="")
        self.f_bk = open(out_dir / f"ljf_book_{stamp}.csv", "w", newline="")
        self.f_fd = open(out_dir / f"ljf_feed_{stamp}.csv", "w", newline="")
        self.ev, self.bk, self.fd = csv.writer(self.f_ev), csv.writer(self.f_bk), csv.writer(self.f_fd)
        self.ev.writerow(["utc", "event", "leader", "follower", "leader_move_ticks", "follower_done_ticks",
                          "expected_ticks", "side", "price", "volume", "filled", "pos_before", "pos_after",
                          "f_bid", "f_ask", "note"])
        self.bk.writerow(["utc", "instrument", "bid1", "bidvol1", "bid2", "bidvol2", "bid3", "bidvol3",
                          "ask1", "askvol1", "ask2", "askvol2", "ask3", "askvol3"])
        self.fd.writerow(["utc_received", "exchange_timestamp", "post", "meta_data"])
        self.feed_error_shown = False
        types = {n: str(getattr(i, "instrument_type", "?")) for n, i in inst.items()}
        self.log("start", note=f"instruments={types}")
        print(f"instruments on exchange: {types}")

    # ---------------------------------------------------------------- helpers
    def log(self, event, **kw):
        cols = ["leader", "follower", "leader_move_ticks", "follower_done_ticks", "expected_ticks", "side",
                "price", "volume", "filled", "pos_before", "pos_after", "f_bid", "f_ask", "note"]
        self.ev.writerow([utc(), event] + [kw.get(c, "") for c in cols])
        self.f_ev.flush()
        if event != "start":
            print(f"[{utc()[11:23]}] {event} " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def px(self, n, price):
        t = self.tick[n]
        p = round(round(price / t) * t, 6)
        lim = self.plimit[n]
        if lim is not None:
            p = min(max(p, float(lim.min)), float(lim.max))
        return p

    def read_book(self, n):
        pb = self.ex.get_last_price_book(n)
        bids, asks = (top(getattr(pb, "bids", None)), top(getattr(pb, "asks", None))) if pb else ([], [])
        self.book[n] = (bids, asks)
        now = self.clock()
        if bids and asks:
            self.hist[n].append((now, (bids[0][0] + asks[0][0]) / 2))
        h = self.hist[n]
        while len(h) > 1 and h[1][0] <= now - self.a.window:
            h.popleft()                                       # keep one sample at/before now-window
        row = [utc(), n]
        for side in (bids, asks):
            for i in range(3):
                row += list(side[i]) if i < len(side) else ["", ""]
        self.bk.writerow(row)

    def move_ticks(self, n):
        h = self.hist[n]
        if len(h) < 2 or self.clock() - h[-1][0] > 1.0:
            return None
        return (h[-1][1] - h[0][1]) / self.tick[n]

    def position(self, n):
        return int((self.ex.get_positions() or {}).get(n, 0))

    def position_after_order(self, n, before):
        """The position may update a moment after insert_order returns: re-read briefly
        so a filled IOC is never mistaken for an unfilled one."""
        pos = self.position(n)
        for _ in range(4):
            if pos != before or self.settle <= 0:
                break
            time.sleep(self.settle)
            pos = self.position(n)
        return pos

    def ioc(self, n, side, price, volume):
        if not self.live:
            return True, "dry"
        try:
            r = self.ex.insert_order(n, price=price, volume=int(volume), side=side, order_type="ioc")
            return bool(r.success), str(getattr(r, "error_reason", "") or "")
        except Exception as e:                                # never crash the loop on one order
            return False, f"{type(e).__name__}: {e}"

    def poll_feed(self):
        try:
            posts = self.ex.poll_new_social_media_feeds() or []
        except Exception as e:
            if not self.feed_error_shown:
                print(f"FEED ERROR (shown once): {type(e).__name__}: {e}")
                self.feed_error_shown = True
            return
        for p in posts:
            self.fd.writerow([utc(), getattr(p, "timestamp", ""), getattr(p, "post", ""), getattr(p, "meta_data", "")])
            print(f"NEWS {getattr(p, 'timestamp', '')} {getattr(p, 'post', '')!r}")
        if posts:
            self.f_fd.flush()

    # ---------------------------------------------------------------- core
    def step(self):
        for leader in self.pairs:                             # leaders first: speed is the edge
            self.read_book(leader)
        if not self.halted:
            for leader, (follower, beta) in self.pairs.items():
                self.maybe_enter(leader, follower, beta)
        for follower, _ in self.pairs.values():
            self.read_book(follower)
        for follower in list(self.trades):
            self.maybe_exit(follower)
        self.poll_feed()
        self.f_bk.flush()

    def maybe_enter(self, leader, follower, beta):
        a, now = self.a, self.clock()
        mv = self.move_ticks(leader)
        if mv is None or abs(mv) < a.jump_ticks or now - self.last_trigger.get(leader, -1e9) < a.cooldown:
            return
        lb, la = self.book[leader]
        if (la[0][0] - lb[0][0]) / self.tick[leader] > 4.5:   # one-sided glitch, not a jump
            return
        self.last_trigger[leader] = now
        self.read_book(follower)                              # fresh follower touch for pricing
        fb, fa = self.book[follower]
        base = dict(leader=leader, follower=follower, leader_move_ticks=round(mv, 1))
        if not fb or not fa:
            return self.log("skip", note="follower book one-sided", **base)
        done = self.move_ticks(follower) or 0.0
        expected = beta * mv
        rem = expected - done
        base.update(follower_done_ticks=round(done, 1), expected_ticks=round(expected, 1),
                    f_bid=fb[0][0], f_ask=fa[0][0])
        if follower in self.trades:
            return self.log("skip", note="trade already open", **base)
        if rem * mv <= 0 or abs(rem) < a.min_remaining:
            return self.log("skip", note="follower already moved", **base)
        d = 1 if mv > 0 else -1
        pos = self.position(follower)                         # fresh; bot has no resting orders by design
        room = (min(a.cap, HARD_LIMIT) - pos) if d > 0 else (min(a.cap, HARD_LIMIT) + pos)
        vol = max(0, min(a.size, room, HARD_LIMIT))
        if vol <= 0:
            return self.log("skip", note=f"no capacity pos={pos}", **base)
        touch = fa[0][0] if d > 0 else fb[0][0]
        price = self.px(follower, touch + d * a.entry_frac * abs(rem) * self.tick[follower])
        side = "bid" if d > 0 else "ask"
        ok, err = self.ioc(follower, side, price, vol)
        pos2 = self.position_after_order(follower, pos) if self.live else pos + d * vol
        filled = abs(pos2 - pos)
        self.log("entry", side=side, price=price, volume=vol, filled=filled, pos_before=pos, pos_after=pos2,
                 note=("ok" if ok else f"FAILED {err}"), **base)
        if filled:
            pre_mid = self.hist[follower][0][1]
            self.trades[follower] = dict(dir=d, base_pos=pos, t=now, entry=touch, tries=0, qty=filled,
                                         sim_pos=pos2, target=pre_mid + 0.85 * expected * self.tick[follower])

    def maybe_exit(self, follower):
        a, now, tr = self.a, self.clock(), self.trades[follower]
        fb, fa = self.book[follower]
        if not fb or not fa:
            return
        mid, d, tick = (fb[0][0] + fa[0][0]) / 2, tr["dir"], self.tick[follower]
        reason = ("hold" if now - tr["t"] >= a.hold else
                  "target" if (mid - tr["target"]) * d >= 0 else
                  "stop" if (mid - tr["entry"]) * d <= -a.stop_ticks * tick else
                  "halt" if self.halted else None)
        if reason is None:
            return
        pos = self.position(follower) if self.live else tr["sim_pos"]
        open_qty = (pos - tr["base_pos"]) * d                 # only what THIS trade opened
        if open_qty <= 0:
            return self.close_trade(follower, mid)
        k = min(tr["tries"], 5)
        tr["tries"] += 1
        side = "ask" if d > 0 else "bid"
        price = self.px(follower, fb[0][0] - k * tick if d > 0 else fa[0][0] + k * tick)
        ok, err = self.ioc(follower, side, price, min(open_qty, HARD_LIMIT))
        pos2 = self.position_after_order(follower, pos) if self.live else tr["base_pos"]
        tr["sim_pos"] = pos2
        exit_px = fb[0][0] if d > 0 else fa[0][0]
        self.log("exit", follower=follower, side=side, price=price, volume=open_qty, filled=abs(pos2 - pos),
                 pos_before=pos, pos_after=pos2, f_bid=fb[0][0], f_ask=fa[0][0],
                 note=f"{reason} try{tr['tries']} " + ("ok" if ok else f"FAILED {err}"))
        if (pos2 - tr["base_pos"]) * d <= 0:
            self.close_trade(follower, exit_px)

    def close_trade(self, follower, exit_px):
        tr = self.trades.pop(follower)
        ticks = (exit_px - tr["entry"]) * tr["dir"] / self.tick[follower]
        pnl = ticks * self.tick[follower] * tr["qty"]
        self.realised += pnl
        self.losses_in_row = 0 if ticks > 0 else self.losses_in_row + 1
        self.log("closed", follower=follower, volume=tr["qty"],
                 note=f"approx {ticks:+.1f} ticks/lot, {pnl:+.1f} (touch-to-touch estimate); "
                      f"ljf_total={self.realised:+.1f} losses_in_row={self.losses_in_row}")
        if self.losses_in_row >= self.a.max_losses or self.realised <= -self.a.dd_stop:
            self.halted = True
            self.log("HALT", note="abandon rule hit: no new entries. Restart is a human decision.")

    def shutdown(self):
        self.halted = True
        for _ in range(8):                                    # close what we opened; leave the rest alone
            if not self.trades:
                break
            for follower, _b in self.pairs.values():
                self.read_book(follower)
            for follower in list(self.trades):
                self.maybe_exit(follower)
            time.sleep(0.25 if self.live else 0)
        if self.trades:
            print(f"WARNING: could not close LJF positions in {list(self.trades)}; flatten manually.")
        for f in (self.f_ev, self.f_bk, self.f_fd):
            f.close()


# -------------------------------------------------------------------- replay (offline, optimistic fills)
class ReplayExchange:
    """Feeds a run_*.csv log (top of book only). IOC fills fully at the touch if the limit allows."""
    def __init__(self, path):
        from types import SimpleNamespace as NS
        self.NS, self.rows, self.t, self.pos, self.cash = NS, {}, 0.0, {}, 0.0
        for r in csv.DictReader(open(path)):
            if r["clean_best_bid"] and r["clean_best_ask"]:
                ts = datetime.strptime(r["utc_time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
                self.rows.setdefault(r["instrument"], []).append((ts, float(r["clean_best_bid"]), float(r["clean_best_ask"])))
        self.t0 = min(v[0][0] for v in self.rows.values())
        self.t1 = max(v[-1][0] for v in self.rows.values())
        self.t = self.t0

    def _at(self, n):
        cur = None
        for row in self.rows[n]:
            if row[0] > self.t:
                break
            cur = row
        return cur

    def get_instruments(self):
        return {n: self.NS(tick_size=0.1, instrument_type="STOCK", price_limit=None) for n in self.rows}

    def get_last_price_book(self, n):
        cur = self._at(n)
        if cur is None:
            return None
        return self.NS(bids=[self.NS(price=cur[1], volume=1000)], asks=[self.NS(price=cur[2], volume=1000)])

    def get_positions(self):
        return dict(self.pos)

    def poll_new_social_media_feeds(self):
        return []

    def insert_order(self, n, price, volume, side, order_type):
        cur = self._at(n)
        if side == "bid" and price >= cur[2] - 1e-9:
            self.pos[n] = self.pos.get(n, 0) + volume; self.cash -= volume * cur[2]
        elif side == "ask" and price <= cur[1] + 1e-9:
            self.pos[n] = self.pos.get(n, 0) - volume; self.cash += volume * cur[1]
        return self.NS(success=True, order_id=1, error_reason=None)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "replay"):
        s = sub.add_parser(name)
        if name == "run":
            s.add_argument("--live", action="store_true", help="send real orders (default: dry)")
        else:
            s.add_argument("log")
        s.add_argument("--pairs", default=DEFAULT_PAIRS)
        s.add_argument("--size", type=int, default=20, help="lots per LJF entry (<=100)")
        s.add_argument("--cap", type=int, default=100, help="internal |position| cap in the follower (<=100)")
        s.add_argument("--sleep", type=float, default=0.2)
        s.add_argument("--jump-ticks", type=float, default=5.0)
        s.add_argument("--window", type=float, default=1.0)
        s.add_argument("--min-remaining", type=float, default=3.0)
        s.add_argument("--entry-frac", type=float, default=0.5)
        s.add_argument("--hold", type=float, default=10.0)
        s.add_argument("--stop-ticks", type=float, default=8.0)
        s.add_argument("--cooldown", type=float, default=15.0)
        s.add_argument("--max-losses", type=int, default=3)
        s.add_argument("--dd-stop", type=float, default=100.0, help="halt when LJF realised estimate <= -this")
        s.add_argument("--log-dir", default="logs")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.size <= HARD_LIMIT or not 1 <= args.cap <= HARD_LIMIT:
        raise SystemExit("size and cap must be between 1 and 100")
    if args.cmd == "replay":
        ex = ReplayExchange(args.log)
        bot = Bot(ex, args, live=True, clock=lambda: ex.t, out_dir=Path(args.log_dir) / "replay")
        while ex.t <= ex.t1:
            bot.step()
            ex.t += max(0.05, args.sleep)
        mtm = ex.cash + sum(q * (ex._at(n)[1] + ex._at(n)[2]) / 2 for n, q in ex.pos.items() if q)
        print(f"REPLAY (optimistic fills, unlimited depth): cash+mtm = {mtm:+.1f}, end positions = {ex.pos}")
        for f in (bot.f_ev, bot.f_bk, bot.f_fd):
            f.close()
        return 0
    from optibook.synchronous_client import Exchange
    ex = Exchange()
    ex.connect()
    bot = Bot(ex, args, live=args.live, out_dir=Path(args.log_dir))
    print(("LIVE ORDERS" if args.live else "DRY RUN (no orders)") + f" pairs={bot.pairs} size={args.size} sleep={args.sleep}")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            if not ex.is_connected():
                print("DISCONNECTED: stopping (not reconnecting).")
                break
            t = time.monotonic()
            bot.step()
            time.sleep(max(0.0, args.sleep - (time.monotonic() - t)) if args.sleep >= 0.2 else 0.2)
    except KeyboardInterrupt:
        print("Interrupted: closing LJF positions.")
    finally:
        bot.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
