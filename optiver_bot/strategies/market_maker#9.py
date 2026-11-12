#!/usr/bin/env python3
"""Jump-follower bot v2: watches EVERY stock, learns leader->follower links online.

Principle: when stock A jumps and stock B is linked to A, B catches up within seconds.
Buy/sell B by IOC before it has caught up; close after --hold s, at target, or at stop.

Links come from three sources:
  prior   pairs seen on 2026-11-12 (used only if those instruments exist; --no-priors disables)
  corr    learned live: 5 s returns of two stocks correlate >= --learn-corr
  cojump  learned live: after A jumped, B moved in the following 10 s (needs 2 sightings)

    DRY  (connects, sends nothing):  python ljf_bot.py run
    LIVE:                            python ljf_bot.py run --live --size 20
    Offline replay of a run log:     python ljf_bot.py replay logs/run_X.csv

ONE connection per team: stop every other bot first. The bot holds no resting orders and
only closes what it opened. Replay fills are optimistic; they prove logic, not profit.
"""
import argparse
import csv
import math
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HARD_LIMIT = 100
PRIORS = {("MU", "CRWV"): 0.57, ("NVDA", "SNDK"): 1.0, ("AVGO", "NBIS"): 0.7}   # follower return per leader return


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def top(book_side):
    return [(float(l.price), int(l.volume)) for l in (book_side or []) if int(l.volume) > 0]


def clip(x, lo, hi):
    return max(lo, min(hi, x))


class Bot:
    def __init__(self, ex, args, live, clock=time.monotonic, out_dir=Path("logs")):
        self.ex, self.a, self.live, self.clock = ex, args, live, clock
        inst = ex.get_instruments()
        types = {n: str(getattr(i, "instrument_type", "") or "") for n, i in inst.items()}
        self.names = sorted(n for n, t in types.items()
                            if (t == "" or t.upper().endswith("STOCK")) and float(inst[n].tick_size) > 0)
        if args.instruments:
            self.names = [n for n in args.instruments.split(",") if n in inst]
        if len(self.names) < 2:
            raise SystemExit(f"need >= 2 stocks; exchange lists {types}")
        self.tick = {n: float(inst[n].tick_size) for n in self.names}
        self.plimit = {n: getattr(inst[n], "price_limit", None) for n in self.names}
        self.links = {}                                       # (leader, follower) -> {beta, n, src}
        if not args.no_priors:
            for (a, b), beta in PRIORS.items():
                if a in self.names and b in self.names:
                    self.links[(a, b)] = dict(beta=beta, n=0, src="prior")
        self.hist = {n: deque() for n in self.names}          # (t, mid) over the jump window
        self.book = {n: ([], []) for n in self.names}
        self.last_trigger, self.trades, self.evals = {}, {}, []
        self.samples = {n: [] for n in self.names}            # 5 s returns for correlation learning
        self.last_sample_t, self.last_sample_mid, self.sample_dirty = None, {}, False
        self.losses_in_row, self.realised, self.halted = 0, 0.0, False
        self.settle = 0.05 if clock is time.monotonic else 0.0
        self.feed_error_shown = False
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.files = [open(out_dir / f"ljf_{k}_{stamp}.csv", "w", newline="") for k in ("events", "book", "feed", "ticks")]
        self.ev, self.bk, self.fd, self.tk = (csv.writer(f) for f in self.files)
        self.cols = ["leader", "follower", "leader_move_ticks", "follower_done_ticks", "expected_ticks", "side",
                     "price", "volume", "filled", "pos_before", "pos_after", "f_bid", "f_ask", "note"]
        self.ev.writerow(["utc", "event"] + self.cols)
        self.bk.writerow(["utc", "instrument", "bid1", "bidvol1", "bid2", "bidvol2", "bid3", "bidvol3",
                          "ask1", "askvol1", "ask2", "askvol2", "ask3", "askvol3"])
        self.fd.writerow(["utc_received", "exchange_timestamp", "post", "meta_data"])
        self.tk.writerow(["utc_received", "exchange_timestamp", "instrument", "price", "volume",
                          "aggressor_side", "buyer", "seller", "trade_id"])
        self.tick_error_shown, self.n_posts, self.n_ticks = False, 0, 0
        print(f"exchange instruments: {types}")
        print(f"watching {self.names}; starting links: { {k: v['beta'] for k, v in self.links.items()} }")
        self.log("start", note=f"types={types} links={list(self.links)}", quiet=True)

    # ---------------------------------------------------------------- helpers
    def log(self, event, quiet=False, **kw):
        self.ev.writerow([utc(), event] + [kw.get(c, "") for c in self.cols])
        self.files[0].flush()
        if not quiet:
            print(f"[{utc()[11:23]}] {event} " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def px(self, n, price):
        t = self.tick[n]
        p = round(round(price / t) * t, 6)
        lim = self.plimit[n]
        return p if lim is None else min(max(p, float(lim.min)), float(lim.max))

    def mid(self, n):
        b, a = self.book[n]
        return (b[0][0] + a[0][0]) / 2 if b and a else None

    def read_book(self, n):
        pb = self.ex.get_last_price_book(n)
        bids, asks = (top(getattr(pb, "bids", None)), top(getattr(pb, "asks", None))) if pb else ([], [])
        self.book[n] = (bids, asks)
        now = self.clock()
        if bids and asks:
            self.hist[n].append((now, (bids[0][0] + asks[0][0]) / 2))
        h = self.hist[n]
        while len(h) > 1 and h[1][0] <= now - self.a.window:
            h.popleft()
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
        pos = self.position(n)
        for _ in range(4):                                    # position may update just after the insert returns
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
        except Exception as e:
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
        self.n_posts += len(posts)
        if posts:
            self.files[2].flush()

    def poll_ticks(self):
        """Public trades for every watched stock: price, size, who was the aggressor, who traded."""
        got = False
        for n in self.names:
            try:
                ticks = self.ex.poll_new_trade_ticks(n) or []
            except Exception as e:
                if not self.tick_error_shown:
                    print(f"TRADE TICK ERROR (shown once): {type(e).__name__}: {e}")
                    self.tick_error_shown = True
                return
            for t in ticks:
                got = True
                self.n_ticks += 1
                self.tk.writerow([utc(), getattr(t, "timestamp", ""), n, getattr(t, "price", ""),
                                  getattr(t, "volume", ""), getattr(t, "aggressor_side", ""),
                                  getattr(t, "buyer", ""), getattr(t, "seller", ""), getattr(t, "trade_id", "")])
        if got:
            self.files[3].flush()

    def tradable(self, link):
        return link["src"] in ("prior", "corr") or link["n"] >= 2

    # ---------------------------------------------------------------- core loop
    def step(self):
        a, now = self.a, self.clock()
        for n in self.names:
            self.read_book(n)
        for n in self.names:                                  # 1. jumps -> entries
            mv = self.move_ticks(n)
            if mv is None or abs(mv) < a.jump_ticks or now - self.last_trigger.get(n, -1e9) < a.cooldown:
                continue
            b, k = self.book[n]
            if (k[0][0] - b[0][0]) / self.tick[n] > 4.5:      # one-sided glitch, not a jump
                continue
            self.last_trigger[n] = now
            self.sample_dirty = True
            ret = mv * self.tick[n] / self.hist[n][0][1]
            self.log("jump", leader=n, leader_move_ticks=round(mv, 1), note=f"ret={ret * 100:+.2f}%")
            self.evals.append(dict(t=now + 10, leader=n, ret=ret,
                                   at={m: self.mid(m) for m in self.names if m != n}))
            if not self.halted:
                for (ld, fo), link in list(self.links.items()):
                    if ld == n and self.tradable(link):
                        self.maybe_enter(n, fo, link["beta"], mv, ret)
        for fo in list(self.trades):                          # 2. exits
            self.maybe_exit(fo)
        self.learn(now)                                       # 3. learning
        self.poll_feed()
        self.poll_ticks()
        self.files[1].flush()

    def maybe_enter(self, leader, follower, beta, mv, ret):
        a, now = self.a, self.clock()
        self.read_book(follower)                              # fresh follower touch
        fb, fa = self.book[follower]
        base = dict(leader=leader, follower=follower, leader_move_ticks=round(mv, 1))
        if not fb or not fa:
            return self.log("skip", note="follower book one-sided", **base)
        fmid = (fb[0][0] + fa[0][0]) / 2
        done = self.move_ticks(follower) or 0.0
        expected = beta * ret * fmid / self.tick[follower]    # follower ticks, signed
        rem = expected - done
        base.update(follower_done_ticks=round(done, 1), expected_ticks=round(expected, 1),
                    f_bid=fb[0][0], f_ask=fa[0][0])
        if follower in self.trades:
            return self.log("skip", note="trade already open", **base)
        if rem * expected <= 0 or abs(rem) < a.min_remaining:
            return self.log("skip", note="follower already moved / move too small", **base)
        d = 1 if expected > 0 else -1
        pos = self.position(follower)                         # fresh; no resting orders by design
        cap = min(a.cap, HARD_LIMIT)
        vol = max(0, min(a.size, (cap - pos) if d > 0 else (cap + pos), HARD_LIMIT))
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
            self.trades[follower] = dict(dir=d, base_pos=pos, t=now, entry=touch, tries=0, qty=filled, sim_pos=pos2,
                                         target=pre_mid + 0.85 * expected * self.tick[follower])

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
        self.log("exit", follower=follower, side=side, price=price, volume=open_qty, filled=abs(pos2 - pos),
                 pos_before=pos, pos_after=pos2, f_bid=fb[0][0], f_ask=fa[0][0],
                 note=f"{reason} try{tr['tries']} " + ("ok" if ok else f"FAILED {err}"))
        if (pos2 - tr["base_pos"]) * d <= 0:
            self.close_trade(follower, fb[0][0] if d > 0 else fa[0][0])

    def close_trade(self, follower, exit_px):
        tr = self.trades.pop(follower)
        ticks = (exit_px - tr["entry"]) * tr["dir"] / self.tick[follower]
        pnl = ticks * self.tick[follower] * tr["qty"]
        self.realised += pnl
        self.losses_in_row = 0 if ticks > 0 else self.losses_in_row + 1
        self.log("closed", follower=follower, volume=tr["qty"],
                 note=f"approx {ticks:+.1f} ticks/lot, {pnl:+.1f} (touch estimate); "
                      f"total={self.realised:+.1f} losses_in_row={self.losses_in_row}")
        if self.losses_in_row >= self.a.max_losses or self.realised <= -self.a.dd_stop:
            self.halted = True
            self.log("HALT", note="abandon rule hit: no new entries. Restart is a human decision.")

    # ---------------------------------------------------------------- online learning
    def learn(self, now):
        a = self.a
        # (b) co-jump: what did everyone else do in the 10 s after a jump?
        for e in [e for e in self.evals if now >= e["t"]]:
            self.evals.remove(e)
            for b, m0 in e["at"].items():
                m1 = self.mid(b)
                if m0 is None or m1 is None or abs(m1 - m0) / self.tick[b] < 4:
                    continue
                ratio = clip(((m1 - m0) / m0) / e["ret"], -1.5, 1.5)
                if abs(ratio) < 0.15:
                    continue
                key = (e["leader"], b)
                link = self.links.get(key)
                if link is None:
                    link = self.links[key] = dict(beta=ratio, n=0, src="cojump")
                elif link["beta"] * ratio > 0:
                    link["beta"] = 0.5 * link["beta"] + 0.5 * ratio
                else:
                    link["n"] = -1                            # contradicts: must be re-seen twice
                    link["src"], link["beta"] = "cojump", ratio
                link["n"] += 1
                self.log("link", leader=key[0], follower=b,
                         note=f"cojump beta={link['beta']:+.2f} seen={link['n']} tradable={self.tradable(link)}")
        # (a) correlation of 5 s returns in quiet periods
        if self.last_sample_t is None:
            self.last_sample_t, self.last_sample_mid = now, {n: self.mid(n) for n in self.names}
            return
        if now - self.last_sample_t < 5.0:
            return
        mids = {n: self.mid(n) for n in self.names}
        prev, dirty = self.last_sample_mid, self.sample_dirty
        self.last_sample_t, self.last_sample_mid, self.sample_dirty = now, mids, False
        if dirty or any(mids[n] is None or prev.get(n) is None for n in self.names):
            return
        for n in self.names:
            self.samples[n].append((mids[n] - prev[n]) / prev[n])
        k = len(self.samples[self.names[0]])
        if k < a.learn_min or k % 12:
            return
        for i, x in enumerate(self.names):
            for y in self.names[i + 1:]:
                sx, sy = self.samples[x], self.samples[y]
                mx, my = sum(sx) / k, sum(sy) / k
                vx, vy = sum((v - mx) ** 2 for v in sx), sum((v - my) ** 2 for v in sy)
                if vx <= 0 or vy <= 0:
                    continue
                cov = sum((p - mx) * (q - my) for p, q in zip(sx, sy))
                r = cov / math.sqrt(vx * vy)
                if r < a.learn_corr:
                    continue
                for ld, fo, var in ((x, y, vx), (y, x, vy)):
                    if (ld, fo) not in self.links:
                        beta = clip(1.5 * cov / var, 0.2, 1.5)   # x1.5: quiet-period slope understates jump response
                        self.links[(ld, fo)] = dict(beta=beta, n=0, src="corr")
                        self.log("link", leader=ld, follower=fo, note=f"corr r={r:.2f} beta={beta:.2f} n={k}")

    def shutdown(self):
        self.halted = True
        for _ in range(8):
            if not self.trades:
                break
            for fo in list(self.trades):
                self.read_book(fo)
                self.maybe_exit(fo)
            time.sleep(0.25 if self.settle else 0)
        if self.trades:
            print(f"WARNING: could not close positions opened by this bot in {list(self.trades)}; flatten manually.")
        try:                                                  # safety net: everything the client saw since connect
            hist = self.ex.get_social_media_feeds_history() or []
            for p in hist:
                self.fd.writerow(["HISTORY", getattr(p, "timestamp", ""), getattr(p, "post", ""), getattr(p, "meta_data", "")])
            print(f"feed: {self.n_posts} posts polled live, {len(hist)} in history; public trade ticks logged: {self.n_ticks}")
        except Exception as e:
            print(f"feed history unavailable: {type(e).__name__}: {e}")
        print(f"links at exit: { {k: (round(v['beta'], 2), v['src'], v['n']) for k, v in self.links.items()} }")
        for f in self.files:
            f.close()


# -------------------------------------------------------------------- replay (offline, optimistic fills)
class ReplayExchange:
    """Feeds a run_*.csv log (top of book only). IOC fills fully at the touch if the limit allows."""
    def __init__(self, path):
        from types import SimpleNamespace as NS
        self.NS, self.rows, self.idx, self.pos, self.cash = NS, {}, {}, {}, 0.0
        for r in csv.DictReader(open(path)):
            if r["clean_best_bid"] and r["clean_best_ask"]:
                ts = datetime.strptime(r["utc_time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
                self.rows.setdefault(r["instrument"], []).append((ts, float(r["clean_best_bid"]), float(r["clean_best_ask"])))
        self.t = min(v[0][0] for v in self.rows.values())
        self.t1 = max(v[-1][0] for v in self.rows.values())

    def _at(self, n):
        rows, i = self.rows[n], self.idx.get(n, 0)
        while i + 1 < len(rows) and rows[i + 1][0] <= self.t:
            i += 1
        self.idx[n] = i
        return rows[i] if rows[i][0] <= self.t else None

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

    def get_social_media_feeds_history(self):
        return []

    def poll_new_trade_ticks(self, n):
        return []

    def insert_order(self, n, price, volume, side, order_type):
        cur = self._at(n)
        if side == "bid" and price >= cur[2] - 1e-9:
            self.pos[n] = self.pos.get(n, 0) + volume
            self.cash -= volume * cur[2]
        elif side == "ask" and price <= cur[1] + 1e-9:
            self.pos[n] = self.pos.get(n, 0) - volume
            self.cash += volume * cur[1]
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
        s.add_argument("--instruments", default=None, help="comma-separated ids (default: every stock)")
        s.add_argument("--no-priors", action="store_true", help="start with no links; learn everything live")
        s.add_argument("--size", type=int, default=20, help="lots per entry (<=100)")
        s.add_argument("--cap", type=int, default=100, help="internal |position| cap per follower (<=100)")
        s.add_argument("--sleep", type=float, default=0.2)
        s.add_argument("--jump-ticks", type=float, default=5.0)
        s.add_argument("--window", type=float, default=1.0)
        s.add_argument("--min-remaining", type=float, default=3.0)
        s.add_argument("--entry-frac", type=float, default=0.5)
        s.add_argument("--hold", type=float, default=10.0)
        s.add_argument("--stop-ticks", type=float, default=8.0)
        s.add_argument("--cooldown", type=float, default=15.0)
        s.add_argument("--learn-min", type=int, default=60, help="5 s samples before correlation links (60 = 5 min)")
        s.add_argument("--learn-corr", type=float, default=0.4)
        s.add_argument("--max-losses", type=int, default=3)
        s.add_argument("--dd-stop", type=float, default=100.0, help="halt when realised estimate <= -this")
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
        bot.shutdown()
        return 0
    from optibook.synchronous_client import Exchange
    ex = Exchange()
    ex.connect()
    bot = Bot(ex, args, live=args.live, out_dir=Path(args.log_dir))
    print(("LIVE ORDERS" if args.live else "DRY RUN: NO ORDERS ARE SENT (add --live)") + f" size={args.size} sleep={args.sleep}")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    last_beat = time.monotonic()
    try:
        while True:
            if not ex.is_connected():
                print("DISCONNECTED: stopping (not reconnecting).")
                break
            t = time.monotonic()
            bot.step()
            if t - last_beat >= 30:
                last_beat = t
                print(f"[{utc()[11:19]}] alive: cycle={time.monotonic() - t:.2f}s links={len(bot.links)} "
                      f"open={list(bot.trades)} total={bot.realised:+.1f} halted={bot.halted} "
                      f"posts={bot.n_posts} ticks={bot.n_ticks}")
            time.sleep(max(0.0, max(0.2, args.sleep) - (time.monotonic() - t)))
    except KeyboardInterrupt:
        print("Interrupted: closing positions opened by this bot.")
    finally:
        bot.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
