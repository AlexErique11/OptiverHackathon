"""Replay recorded JSONL observations; never infers fills or opens a connection.

python -m optiver_bot.live_position_replay --input data/capture.jsonl \
    --config config/challenge.local.json --deadline 1200 --output results/position.jsonl
Config is a JSON object of strategy Config fields, not the starter example schema.
"""
import argparse
import json
from collections import deque
from dataclasses import asdict
from pathlib import Path

from optiver_bot.live_position import Controller, News
from importlib import import_module

_strategy = import_module('optiver_bot.strategies.market_maker#6')
Config, DepthSnapshot = _strategy.Config, _strategy.DepthSnapshot


class Markouts:
    def __init__(self):
        self.pending = deque()
        self.ids = set()

    def fill(self, fill, received_at):
        from math import isfinite
        if fill['id'] in self.ids:
            return
        if (fill['side'] not in ('bid', 'ask') or type(fill['volume']) is not int
                or fill['volume'] <= 0 or not isfinite(fill['price']) or fill['price'] <= 0):
            raise ValueError('Invalid fill')
        self.ids.add(fill['id'])
        for horizon in (1, 5, 15):
            self.pending.append((received_at, horizon, dict(fill)))

    def observe(self, now, mids):
        result, retained = [], deque()
        for start, horizon, fill in self.pending:
            if now-start >= horizon and fill['instrument'] in mids:
                mark = (mids[fill['instrument']]-fill['price']) * (1 if fill['side'] == 'bid' else -1)
                result.append(dict(fill_id=fill['id'], horizon=horizon, elapsed=now-start,
                                   signed_markout=mark, volume=fill['volume']))
            else:
                retained.append((start, horizon, fill))
        self.pending = retained
        return result


def local_output(name):
    path = Path(name).resolve()
    root = Path.cwd().resolve()
    if not any(path.is_relative_to(root/d) for d in ('results', 'logs')):
        raise ValueError('Output must be under ignored results/ or logs/')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True)
    p.add_argument('--config', required=True)
    p.add_argument('--deadline', type=float, required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    cfg = Config(**json.loads(Path(a.config).read_text(encoding='utf-8')))
    output = local_output(a.output)
    if output in (Path(a.input).resolve(), Path(a.config).resolve()):
        raise ValueError('Never overwrite input/configuration')
    engine, marks = None, Markouts()
    # Exclusive output avoids accidentally destroying previous evaluations.
    with Path(a.input).open(encoding='utf-8') as source, output.open('x', encoding='utf-8') as target:
        for line in source:
            row = json.loads(line)
            books = {i: DepthSnapshot(instrument_id=i, **b) for i, b in row['books'].items()}
            if engine is None:
                engine = Controller(tuple(books), cfg, a.deadline)
            now = row['time']
            decisions = engine.update(now, books, equity=row['equity'],
                                      account_ok=row.get('account_ok', False),
                                      entry_prices=row.get('entry_prices'),
                                      feed_ok=row.get('feed_ok', False),
                                      news=tuple(News(**n) for n in row.get('news', ())))
            for fill in row.get('fills', ()):
                marks.fill(fill, now)
            mids = {i: (b.bid+b.ask)/2 for i, b in books.items()
                    if 0 <= b.book_age_seconds <= cfg.max_age and 0 < b.bid < b.ask}
            target.write(json.dumps(dict(time=now, decisions=[asdict(d) for d in decisions],
                                         markouts=marks.observe(now, mids)))+'\n')
    print(f'Wrote causal signal diagnostics to {output}; no simulated fills or profitability inference.')


if __name__ == '__main__':
    main()
