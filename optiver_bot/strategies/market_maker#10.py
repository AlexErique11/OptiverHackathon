#!/usr/bin/env python3
"""Per-stock target-position candidate, not a copy of a winning team's signal.

Default command is offline demo. Reuses #5 pacing/risk and the shared runner.
Targets may reach +/-100; entries are small PASSIVE clips, not atomic reversals.
"""
import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time

if __name__ == '__main__' and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from optiver_bot.models import DesiredOrder, Snapshot as SharedSnapshot


def load_flat():
    name='_optiver_target10_flat_runtime'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name('market_maker#5.py'))
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module
        spec.loader.exec_module(module)
    return sys.modules[name]


flat=load_flat()


@dataclass(frozen=True)
class Config(flat.Config):
    cap: int=100
    size: int=5
    target: int=100
    lookback: float=20.
    fast_window: float=5.
    min_fast_ticks: float=1.
    min_slow_ticks: float=3.
    confirmations: int=3
    min_samples: int=8
    max_gap: float=3.
    max_spread_ticks: float=2.
    signal_grace: float=10.
    max_hold: float=60.
    stop_ticks: float=5.
    reentry_delay: float=2.

    def __post_init__(self):
        super().__post_init__()
        if type(self.target) is not int or not 1<=self.target<=self.cap:
            raise ValueError('Require integer 1 <= target <= cap <= 100')
        for name in ('lookback','fast_window','min_fast_ticks','min_slow_ticks',
                     'max_gap','max_spread_ticks','signal_grace','max_hold','stop_ticks','reentry_delay'):
            if not flat.finite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError(f'{name} must be positive and finite')
        if not self.fast_window<self.lookback or self.max_gap>=self.fast_window:
            raise ValueError('Invalid history windows/gaps')
        if any(type(v) is not int or v<2 for v in (self.confirmations,self.min_samples)):
            raise ValueError('Confirmation/sample counts must be integers >=2')


@dataclass(frozen=True)
class Snapshot(SharedSnapshot):
    target_position: int=0
    reducing: bool=False


def decide(s,c):
    """Desired TOTAL remaining quote; execution rechecks target after fills."""
    buy_room,sell_room=flat.rooms(s.position,s.outstanding_buy,s.outstanding_sell,c.cap)
    if (not isinstance(s,Snapshot) or type(s.target_position) is not int
            or abs(s.target_position)>c.cap):
        raise ValueError('Invalid target snapshot')
    if (not s.instrument_id or not all(flat.finite(v) for v in
            (s.bid,s.ask,s.tick_size,s.book_age_seconds)) or s.bid<=0 or s.ask<=s.bid
            or s.tick_size<=0 or not 0<=s.book_age_seconds<=c.max_age):return []
    bid,ask,tick=(Decimal(str(v)) for v in (s.bid,s.ask,s.tick_size))
    if bid%tick or ask%tick:return []
    if s.position+s.outstanding_buy>100 or s.position-s.outstanding_sell< -100:return []
    delta=s.target_position-s.position
    if not delta:return []
    reducing=s.position*delta<0
    if s.reducing and not reducing:return []
    # Always close before reversing. No buy/sell reservations offset each other.
    distance=min(abs(delta),abs(s.position)) if reducing else abs(delta)
    side='bid' if delta>0 else 'ask'
    if not reducing:
        if c.cost_ticks is None or (ask-bid)/tick>Decimal(str(c.max_spread_ticks)):return []
        # Trend thresholds are movement proxies, not forecasts. This only checks
        # whether the spread is affordable relative to the configured slow gate.
        if Decimal(str(c.min_slow_ticks))<=Decimal(str(c.cost_ticks))+Decimal(str(c.min_net_ticks)):return []
    reserved=s.outstanding_buy if side=='bid' else s.outstanding_sell
    room=buy_room if side=='bid' else sell_room
    volume=min(c.size,distance)
    # Matching resting quantity is a target, not an additional insertion. The
    # final adapter never grants this credit to NEW orders before reconciliation.
    volume=min(volume,room+min(reserved,volume))
    if volume<=0:return []
    price=bid if side=='bid' else ask
    if not reducing and ask-bid>=2*tick:
        price=min(bid+tick,ask-tick) if side=='bid' else max(ask-tick,bid+tick)
    return [DesiredOrder(s.instrument_id,side,float(price),volume)]


def signal_direction(history,now,c,tick):
    """Causal independent per-stock momentum; no ticker/colour/leader input."""
    if len(history)<c.min_samples:return 0
    if now-history[0][0]<c.lookback or now-history[0][0]>c.lookback+c.max_gap:return 0
    if any(not 0<history[i+1][0]-history[i][0]<=c.max_gap for i in range(len(history)-1)):return 0
    oldest=history[0][1];current=history[-1][1]
    fast_points=[p for t,p in history if t<=now-c.fast_window]
    if not fast_points:return 0
    fast=(current-fast_points[-1])/tick;slow=(current-oldest)/tick
    changes=[b[1]-a[1] for a,b in zip(history,list(history)[1:]) if abs(b[1]-a[1])>1e-9]
    if len(changes)<c.confirmations:return 0
    for direction in (-1,1):
        if (direction*fast>=c.min_fast_ticks-1e-9 and direction*slow>=c.min_slow_ticks-1e-9
                and all(direction*x>0 for x in changes[-c.confirmations:])):return direction
    return 0


class DeadlineRaw:
    """Deadline checked after the request limiter has finished waiting."""
    def __init__(self,raw,deadline,clock=time.monotonic):
        self.raw,self.deadline,self.clock=raw,deadline,clock
    def __getattr__(self,name):return getattr(self.raw,name)
    def insert_order(self,iid,**kwargs):
        if self.clock()>=self.deadline:raise ValueError('Session deadline reached')
        return self.raw.insert_order(iid,**kwargs)


class TargetRaw:
    """Final target-distance barrier on the same budgeted client."""
    def __init__(self,raw,deadline=float('inf'),clock=time.monotonic):
        self.raw,self.targets,self.reduce_only=raw,{},{}
        self.deadline,self.clock=deadline,clock
    def __getattr__(self,name):return getattr(self.raw,name)
    def insert_order(self,iid,**kwargs):
        if self.clock()>=self.deadline:raise ValueError('Session deadline reached')
        if iid not in self.targets:raise ValueError('No approved current target')
        p1=self.raw.get_positions().get(iid,0)
        orders=self.raw.get_outstanding_orders(iid) or {}
        p2=self.raw.get_positions().get(iid,0)
        if p1!=p2:raise ValueError('Position changed during final target check')
        delta=self.targets[iid]-p2
        side,volume=kwargs['side'],kwargs['volume']
        if not delta or side!=('bid' if delta>0 else 'ask'):raise ValueError('Target already reached/changed')
        rt=flat.load_runtime()
        same=sum(o.volume for o in orders.values() if rt._norm_side(o.side)==side)
        if any(rt._norm_side(o.side)!=side for o in orders.values()):
            raise ValueError('Confirm cancellation of opposing orders first')
        allowance=abs(delta)
        if p2*delta<0:allowance=min(allowance,abs(p2))
        if self.reduce_only.get(iid,False):
            if p2*delta>=0:raise ValueError('Exit cannot open new exposure')
            allowance=min(allowance,abs(p2))
        if same+volume>allowance:raise ValueError('Pending/new orders exceed remaining target')
        return self.raw.insert_order(iid,**kwargs)


@contextmanager
def target_runtime_guard(runtime):
    """Permit legal +/-100 targets in this one runner, restore even on failure.

    The shared runner's early stop at 90 is inappropriate for target=100
    mode. Fresh per-insert cap/target checks remain mandatory and unchanged.
    """
    previous=runtime.LIMIT_GUARD
    runtime.LIMIT_GUARD=101
    try:yield
    finally:runtime.LIMIT_GUARD=previous


class Controller:
    def __init__(self,adapter,target_raw,deadline,clock=time.monotonic,logger=None):
        self.adapter,self.target_raw,self.deadline,self.clock,self.logger=adapter,target_raw,deadline,clock,logger
        self.state={}

    def __call__(self,snap,c):
        bids=[p for p,v in snap.bids if v>0];asks=[p for p,v in snap.asks if v>0]
        if not bids or not asks:return []
        bid,ask=max(bids),min(asks)
        if not all(flat.finite(v) for v in (bid,ask,snap.tick_size)) or bid<=0 or ask<=bid or snap.tick_size<=0:return []
        now,mid=self.clock(),(bid+ask)/2;iid=snap.instrument_id
        s=self.state.setdefault(iid,dict(history=deque(),target=0,position=0,opened=None,
                                        reference=None,last_support=None,pause=0.,exit=False,flat_at=None,report=None))
        history=s['history'];reset=False
        if history:
            gap=now-history[-1][0]
            shock=abs(mid-history[-1][1])>=c.shock_ticks*snap.tick_size-1e-9
            if gap<=0 or gap>c.max_gap or shock:
                history.clear();reset=True
                if shock:s['pause']=now+c.cooldown
        while len(history)>1 and history[1][0]<=now-c.lookback:history.popleft()
        history.append((now,mid))
        direction=signal_direction(history,now,c,snap.tick_size)
        if not snap.position:
            if s['position']:
                s['flat_at']=now;s['target']=0
            s['opened'],s['reference'],s['exit']=None,None,False
        elif not s['position'] or snap.position*s['position']<0:
            s['opened'],s['reference']=now,mid
            # Startup/reversal without our previous target is not a valid entry.
            if not s['target'] or s['target']*snap.position<0:s['exit']=True
        if direction and direction*s['target']>0:s['last_support']=now
        if snap.position:
            adverse=((s['reference']-mid) if snap.position>0 else (mid-s['reference']))/snap.tick_size
            if (reset or now<s['pause'] or now>=self.deadline-15
                    or now-s['opened']>=c.max_hold or adverse>=c.stop_ticks-1e-9
                    or direction*snap.position<0
                    or s['last_support'] is None or now-s['last_support']>=c.signal_grace):s['exit']=True
            if s['exit']:s['target']=0
        else:
            eligible=(not reset and now>=s['pause'] and now<self.deadline-30
                      and (s['flat_at'] is None or now-s['flat_at']>=c.reentry_delay))
            if eligible and direction:
                s['target']=direction*c.target;s['last_support']=now
            else:s['target']=0
        pending=self.adapter.own.get(iid,[])
        own=Snapshot(iid,bid,ask,snap.tick_size,snap.position,
                      sum(o.volume for o in pending if o.side=='bid'),
                      sum(o.volume for o in pending if o.side=='ask'),
                      max(0,now-self.adapter.received.get(iid,float('-inf'))),s['target'],s['exit'])
        self.target_raw.targets[iid]=s['target'];self.target_raw.reduce_only[iid]=s['exit']
        orders=decide(own,c)
        report=(s['target'],snap.position,direction,s['exit'])
        if self.logger and report!=s['report']:
            self.logger(dict(event='target_state',time=now,instrument=iid,target=s['target'],
                             position=snap.position,signal=direction,exit=s['exit'],
                             quotes=[o.__dict__ for o in orders]))
        s['position'],s['report']=snap.position,report
        return orders


def main(argv=None,exchange_factory=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command')
    sub.add_parser('demo');run=sub.add_parser('run',help='one connection; orders dry-run unless --live')
    run.add_argument('--live',action='store_true');run.add_argument('--cap',type=int,default=100)
    run.add_argument('--target',type=int,default=100);run.add_argument('--size',type=int,default=5)
    run.add_argument('--cost-ticks',type=float,required=True);run.add_argument('--max-requests',type=int,required=True)
    run.add_argument('--max-order-size',type=int,required=True);run.add_argument('--session-seconds',type=float,required=True)
    run.add_argument('--instruments',default='AMD');run.add_argument('--sleep',type=float,default=.2)
    run.add_argument('--log-dir',default='logs');args=p.parse_args(argv)
    if args.command!='run':
        c=Config(cost_ticks=0)
        for pos,target in ((0,100),(95,100),(100,100),(100,-100),(0,-100),(-100,-100)):
            s=Snapshot('SYNTHETIC',100.,100.1,.1,pos,target_position=target)
            print('OFFLINE',f'position={pos} target={target}',decide(s,c))
        return 0
    try:
        c=Config(cap=args.cap,target=args.target,size=args.size,cost_ticks=args.cost_ticks)
        budget=flat.RequestBudget(args.max_requests)
        if not 1<=args.size<=args.max_order_size or not flat.finite(args.session_seconds) or args.session_seconds<=30:
            raise ValueError('Invalid order bound/session duration')
        if not flat.finite(args.sleep) or args.sleep<0:raise ValueError('Invalid sleep')
    except ValueError as exc:p.error(str(exc))
    rt=flat.load_runtime();start=time.monotonic();deadline=start+args.session_seconds
    raw=(exchange_factory or rt.connect_real_exchange)()
    target_raw=TargetRaw(flat.BudgetedRaw(DeadlineRaw(raw,deadline),budget),deadline)
    adapter=flat.guarded_adapter(target_raw,c,args.max_order_size)
    if not adapter.is_connected():return rt.EXIT_STARTUP
    instruments,error=rt.select_instruments(adapter.instruments(),rt._parse_ids(args.instruments))
    if error or not instruments:print(error or 'No tradable stocks');return rt.EXIT_STARTUP
    signal.signal(signal.SIGTERM,rt._raise_keyboard_interrupt)
    directory=Path(args.log_dir);directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    print('LIVE target orders' if args.live else 'DRY target orders; one real connection')
    print('Target',c.target,'per stock; stop other bots. No guaranteed profit or fill.')
    with (directory/f'target10_{stamp}.jsonl').open('w') as events:
        def logger(event):events.write(json.dumps(event)+'\n');events.flush()
        controller=Controller(adapter,target_raw,deadline,logger=logger)
        # Session deadline first drives targets to zero; final loop limit stops
        # requests even if a passive reduction has not filled. Inspect inventory.
        def limited_sleep(seconds):
            if time.monotonic()>=deadline:raise KeyboardInterrupt
            time.sleep(min(seconds,max(0,deadline-time.monotonic())))
        with target_runtime_guard(rt):
            return rt.run_bot(adapter,instruments,c,dry_run=not args.live,
                              sleep_s=max(rt.MIN_SLEEP_S,args.sleep),log_path=directory/f'run10_{stamp}.csv',
                              decide_fn=controller,sleep_fn=limited_sleep)


if __name__=='__main__':raise SystemExit(main())
