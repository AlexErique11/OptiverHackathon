"""Offline chronological signal coverage; no fills or counterfactual PnL.

Use --csv for the recorder's utc_time/instrument/clean_best_bid/clean_best_ask
columns, or --json for derived [epoch_seconds, instrument, bid, ask] rows.
Exact duplicate observations are removed. Repeated prices at NEW times remain.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
import runpy
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--csv');source.add_argument('--json')
    parser.add_argument('--tick',type=float,required=True)
    parser.add_argument('--cost-ticks',type=float,required=True)
    args=parser.parse_args()
    if args.tick<=0:parser.error('tick must be positive')
    if args.csv:
        with open(args.csv,newline='') as f:
            rows=[(datetime.fromisoformat(r['utc_time'].replace('Z','+00:00')).timestamp(),
                   r['instrument'],float(r['clean_best_bid']),float(r['clean_best_ask']))
                  for r in csv.DictReader(f)]
    else:rows=json.loads(Path(args.json).read_text())
    raw_count=len(rows)
    rows=sorted(set(map(tuple,rows)))
    if not rows:parser.error('no observations')
    if len({(t,i) for t,i,b,a in rows})!=len(rows):
        parser.error('Conflicting books at the same instrument/timestamp')
    root=Path(__file__).resolve().parents[1]
    module=runpy.run_path(str(root/'optiver_bot/strategies/market_maker#10.py'))
    cfg=module['Config'](cost_ticks=args.cost_ticks)
    now=[0.];adapter=SimpleNamespace(own={},received={})
    target=SimpleNamespace(targets={},reduce_only={})
    engine=module['Controller'](adapter,target,rows[-1][0]-rows[0][0]+3600,clock=lambda:now[0])
    counts=defaultdict(Counter)
    for t,i,b,a in rows:
        now[0]=t-rows[0][0];adapter.received[i]=now[0]
        orders=engine(SimpleNamespace(instrument_id=i,bids=[(b,1)],asks=[(a,1)],
                                     tick_size=args.tick,position=0),cfg)
        split='first_180_seconds' if now[0]<180 else 'later_holdout'
        side=orders[0].side if orders else 'abstain'
        counts[(i,split)][side]+=1
    print(json.dumps(dict(kind='signal coverage only; NOT a profit backtest',
        assumptions=dict(positions='flat',pending_orders=0,fills='not simulated',
                         tick=args.tick,cost_ticks=args.cost_ticks),
        rows=len(rows),duplicates=raw_count-len(rows),seconds=rows[-1][0]-rows[0][0],
        results=[dict(instrument=i,period=p,counts=dict(c)) for (i,p),c in sorted(counts.items())]),indent=2))


if __name__=='__main__':main()
