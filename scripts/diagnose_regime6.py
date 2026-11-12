"""Causal quote/regime diagnostic. NO counterfactual fills or profit estimates.

python -m scripts.diagnose_regime6 --csv data/recording.csv
Requires the previously recorded clean bid/ask CSV; never connects to Optibook.
Tick=0.1 and zero costs are explicit diagnostic assumptions, not event rules.
Positions are kept flat to examine signals alone, not simulated executions.
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
    source.add_argument('--csv')
    source.add_argument('--json',help='derived [timestamp,instrument,bid,ask] diagnostic records')
    args=parser.parse_args()
    if args.csv:
        with open(args.csv,newline='') as f:
            rows=[(datetime.fromisoformat(r['utc_time'].replace('Z','+00:00')).timestamp(),
                   r['instrument'],float(r['clean_best_bid']),float(r['clean_best_ask']))
                  for r in csv.DictReader(f)]
    else:
        rows=json.loads(Path(args.json).read_text())
    rows.sort(key=lambda r:r[0])
    root=Path(__file__).resolve().parents[1]
    module=runpy.run_path(str(root/'optiver_bot/strategies/market_maker#6.py'))
    config=module['Config'](cost_ticks=0)
    adapter=SimpleNamespace(own={},received={})
    reducing=SimpleNamespace(reduce_only={})
    now=[0.]
    controller=module['Controller'](adapter,reducing,clock=lambda:now[0])
    counts=defaultdict(Counter)
    quote_counts=Counter()
    start=rows[0][0]
    for timestamp,symbol,bid,ask in rows:
        now[0]=timestamp-start
        adapter.received[symbol]=now[0]
        snapshot=SimpleNamespace(instrument_id=symbol,bids=[(bid,1)],asks=[(ask,1)],
                                 position=0,tick_size=0.1)
        orders=controller(snapshot,config)
        mode=controller.state[symbol]['last_mode']
        split='first_180_seconds' if now[0]<180 else 'later_holdout'
        counts[(symbol,split)][mode]+=1
        quote_counts[(symbol,split)]+=len(orders)
    summary=[dict(instrument=symbol,period=split,states=dict(value),
                  proposed_quotes=quote_counts[(symbol,split)])
             for (symbol,split),value in sorted(counts.items())]
    print(json.dumps(dict(kind='causal signal diagnostic, NOT a profit backtest',
                          assumptions='flat positions, no pending orders, tick=0.1, zero costs, no fills',
                          rows=len(rows),seconds=rows[-1][0]-start,results=summary),indent=2))


if __name__=='__main__':main()
