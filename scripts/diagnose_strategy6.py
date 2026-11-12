"""Read-only readiness diagnostics from strategy #6 JSONL logs. No connection."""
import argparse
import json
from pathlib import Path

from optiver_bot.live_position import Controller, News
from optiver_bot.live_position_runner import strategy


def diagnose(path):
    engine = None
    latest = None
    fills = 0
    with Path(path).open(encoding='utf-8') as source:
        for line in source:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                break  # a still-running writer may have an incomplete last line
            if row.get('kind') == 'settings':
                cfg = strategy.Config(**row['config'])
                engine = Controller(row['instruments'], cfg, row['duration'])
            elif row.get('kind') == 'frame' and engine is not None:
                books = {i: strategy.DepthSnapshot(instrument_id=i, **b) for i, b in row['books'].items()}
                engine.update(row['time'], books, equity=row['equity'],
                    account_ok=row.get('account_ok', False), feed_ok=row.get('feed_ok', False),
                    news=tuple(News(**e) for e in row.get('news', ())),
                    entry_prices=row.get('entry_prices'))
                fills += len(row.get('fills', ()))
                latest = row
    if latest is None:
        raise ValueError('No strategy #6 market frames found')
    return dict(log=str(path), seconds=round(latest['time'], 2),
                account_ok=latest.get('account_ok'), feed_ok=latest.get('feed_ok'),
                fills_recorded=fills, stocks=engine.readiness)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--log', help='default: most recently modified strategy #6 run log')
    args = p.parse_args()
    path = Path(args.log) if args.log else None
    if path is None:
        for candidate in sorted(Path('logs/strategy6').glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True):
            with candidate.open(encoding='utf-8') as f:
                first = f.readline()
            if first and json.loads(first).get('kind') == 'settings':
                path = candidate
                break
    if path is None:
        p.error('No run logs under logs/strategy6; supply --log')
    print(json.dumps(diagnose(path), indent=2))


if __name__ == '__main__':
    main()
