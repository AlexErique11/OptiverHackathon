"""Offline independent candidate and shared-code audit; returns nonzero on failure."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from evaluation.risk_oracle import safe, validate_cap
from evaluation.runtime import Snapshot, StrategyConfig, strategy


def audit_candidate(candidate):
    """Inspect raw proposals before the replay's protective gate can hide defects."""
    validate_cap(candidate["config"]["position_limit"])
    config = StrategyConfig(**candidate["config"])
    decide = strategy(candidate["module"])
    checked = 0
    for position in range(-100, 101):
        for b in (0, 1, 5, 100):
            for s in (0, 1, 5, 100):
                buys, sells = min(b, 100-position), min(s, 100+position)
                snapshot = Snapshot("RISK_AUDIT", 99.9, 100.1, .1, position, buys, sells)
                orders = decide(snapshot, config)
                for order in orders:
                    if order.instrument_id != snapshot.instrument_id or type(order.volume) is not int or order.volume <= 0:
                        raise ValueError(f"Invalid raw proposal at {snapshot}: {order}")
                pending = [("bid", buys), ("ask", sells)] + [(o.side, o.volume) for o in orders]
                if not safe(position, pending, 100):
                    raise ValueError(f"Unsafe raw proposal at {snapshot}: {orders}")
                checked += 1
    return {"passed": True, "snapshots_checked": checked}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--strategy-root", help="Complete checkout; defaults to frozen hard-limit reference")
    parser.add_argument("--output", default="results/evaluation/risk-audit.json")
    args = parser.parse_args()
    env = os.environ.copy()
    if args.strategy_root:
        env["EVALUATION_STRATEGY_ROOT"] = str(Path(args.strategy_root).resolve())
    completed = subprocess.run([sys.executable, "-m", "unittest", "tests.test_risk_independent", "-v"],
                               env=env, capture_output=True, text=True)
    from evaluation.compare import write_json
    write_json(args.output, {"strategy_root": env.get("EVALUATION_STRATEGY_ROOT", "frozen hard-limit reference"),
                            "passed": completed.returncode == 0, "exit_code": completed.returncode,
                            "test_output": completed.stdout + completed.stderr})
    print(completed.stdout + completed.stderr)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
