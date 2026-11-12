"""Deterministic quote comparison, NOT a backtest or profit estimate.

Run from repository root: python -m scripts.compare_strategies
Uses user-supplied snapshots, assumed tick 0.1, zero-cost scenario, flat inventory,
five-lot orders and cap 60 for every candidate. #2's previous observation is
synthetically repeated; actual persistence has NOT been observed.
"""
import json
from pathlib import Path
import runpy

from optiver_bot.models import Snapshot, StrategyConfig
from optiver_bot.strategies.inventory_maker import decide as starter_decide


def main():
    root = Path(__file__).resolve().parents[1]
    first = runpy.run_path(str(root / 'optiver_bot/strategies/market_maker #1.py'))
    second = runpy.run_path(str(root / 'optiver_bot/strategies/market_maker #2.py'))
    fixture = (
        ('AMD', 161.2, 161.5, 3010, 10), ('AVGO', 207, 207.2, 12, 7),
        ('CRWV', 136.5, 136.8, 26, 11), ('MU', 71.7, 72, 2990, 3000),
        ('NBIS', 93, 93.2, 1010, 6), ('NVDA', 139.6, 139.8, 24, 5),
        ('SNDK', 59.6, 59.8, 2, 61),
    )
    config = second['PressureConfig'](position_limit=60, order_size=5,
                                      round_trip_cost_ticks=0)
    rows = []
    for name, bid, ask, bq, aq in fixture:
        one = first['Snapshot'](name, [(bid, bq)], [(ask, aq)], 0, 0.1)
        two = second['DepthSnapshot'](
            name, bid, ask, 0.1, bid_volume=bq, ask_volume=aq,
            previous=second['DepthObservation'](bid, ask, bq, aq, 0.1))
        def describe(orders):
            return [dict(side=o.side, price=o.price, volume=o.volume) for o in orders]
        rows.append(dict(
            instrument=name, imbalance=round((bq-aq)/(bq+aq), 4),
            starter=describe(starter_decide(Snapshot(name, bid, ask, 0.1),
                                           StrategyConfig(position_limit=60, order_size=5))),
            strategy_1=describe(first['decide'](one, first['Config'](cap=60, size=5))),
            strategy_2=describe(second['decide'](two, config))))
    print(json.dumps({'kind': 'quote comparison only; no fills or PnL',
                      'assumptions': 'tick=0.1; zero costs; repeated prior snapshot; cap=60; size=5',
                      'results': rows}, indent=2))


if __name__ == '__main__':
    main()
