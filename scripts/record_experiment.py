#!/usr/bin/env python3
"""Experiment tracker for the Optibook strategy loop (stdlib only, Python 3.10+).

  python scripts/record_experiment.py new --strategy NAME --tag TAG \
      [--parent ID] [--source sim|practice|live] [--strategy-file PATH] --hypothesis "..."
  python scripts/record_experiment.py finish ID --metrics metrics.json \
      --verdict keep|reject|inconclusive --note "what we learned / next change"
  python scripts/record_experiment.py show [--top N]

Everything is written under results/ (Git-ignored). See results/fable_pack/03_RESULTS_PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
EXPERIMENTS = RESULTS / "experiments"
LEADERBOARD = RESULTS / "leaderboard.csv"
INDEX = RESULTS / "EXPERIMENTS.md"

REQUIRED_METRICS = ["net_pnl", "max_drawdown", "fills", "max_abs_position", "duration_s"]
COLUMNS = ["id", "created_utc", "parent", "strategy", "tag", "source", "verdict",
           "net_pnl", "max_drawdown", "fills", "max_abs_position", "duration_s",
           "pnl_per_hour", "requests", "rejects", "git", "strategy_sha256", "note"]
SOURCES = ("sim", "practice", "live")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""


def cmd_new(a: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    eid = f"{now:%Y%m%d-%H%M%S}_{a.strategy}_{a.tag}".replace(" ", "-")
    d = EXPERIMENTS / eid
    d.mkdir(parents=True, exist_ok=False)
    sf = Path(a.strategy_file) if a.strategy_file else None
    config = {
        "id": eid, "created_utc": now.isoformat(), "parent": a.parent, "strategy": a.strategy,
        "tag": a.tag, "source": a.source, "hypothesis": a.hypothesis,
        "git": _git("rev-parse", "--short", "HEAD"), "git_dirty": bool(_git("status", "--porcelain")),
        "strategy_file": str(sf) if sf else "", "strategy_sha256": _sha(ROOT / sf) if sf else "",
        "data_window": {"start_utc": "", "end_utc": "", "split": "dev|validation|holdout"},
        "params": {}, "assumptions": {"fill_model": "", "latency_ms": None, "fees": None},
    }
    (d / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (d / "notes.md").write_text(
        f"# {eid}\n\n**Hypothesis:** {a.hypothesis}\n\n**Change vs parent:** (exactly ONE thing)\n\n"
        "## Result\n\n## Verdict\n\n## Next change\n")
    print(eid)
    print(f"Fill in {d / 'config.json'} (params, data_window, assumptions), run it, then `finish`.",
          file=sys.stderr)
    return 0


def cmd_finish(a: argparse.Namespace) -> int:
    d = EXPERIMENTS / a.id
    if not d.is_dir():
        print(f"unknown experiment {a.id}", file=sys.stderr)
        return 2
    metrics = json.loads(Path(a.metrics).read_text())
    missing = [k for k in REQUIRED_METRICS if k not in metrics]
    if missing:
        print(f"metrics missing required keys: {missing}", file=sys.stderr)
        return 2
    if abs(metrics["max_abs_position"]) > 100:
        print("WARNING: max_abs_position > 100 violates the hard limit; verdict forced to reject",
              file=sys.stderr)
        a.verdict = "reject"
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    cfg = json.loads((d / "config.json").read_text())
    hours = metrics["duration_s"] / 3600 if metrics["duration_s"] else 0
    row = {
        "id": a.id, "created_utc": cfg["created_utc"], "parent": cfg["parent"] or "",
        "strategy": cfg["strategy"], "tag": cfg["tag"], "source": cfg["source"],
        "verdict": a.verdict, "net_pnl": metrics["net_pnl"], "max_drawdown": metrics["max_drawdown"],
        "fills": metrics["fills"], "max_abs_position": metrics["max_abs_position"],
        "duration_s": metrics["duration_s"],
        "pnl_per_hour": round(metrics["net_pnl"] / hours, 2) if hours else "",
        "requests": metrics.get("requests", ""), "rejects": metrics.get("rejects", ""),
        "git": cfg["git"], "strategy_sha256": cfg["strategy_sha256"], "note": a.note,
    }
    new_file = not LEADERBOARD.exists()
    with LEADERBOARD.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    line = (f"- `{a.id}` [{cfg['source']}] **{a.verdict}** net_pnl={metrics['net_pnl']} "
            f"dd={metrics['max_drawdown']} fills={metrics['fills']} - {a.note}\n")
    old = INDEX.read_text() if INDEX.exists() else "# Experiment index (newest first)\n\n"
    head, _, rest = old.partition("\n\n")
    INDEX.write_text(head + "\n\n" + line + rest)
    print("recorded", a.id)
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    if not LEADERBOARD.exists():
        print("no experiments yet")
        return 0
    rows = list(csv.DictReader(LEADERBOARD.open()))
    rows.sort(key=lambda r: float(r["net_pnl"] or 0), reverse=True)
    for r in rows[: a.top]:
        print(f"{r['net_pnl']:>10} dd={r['max_drawdown']:>8} fills={r['fills']:>5} "
              f"[{r['source']}/{r['verdict']}] {r['id']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--strategy", required=True)
    n.add_argument("--tag", required=True)
    n.add_argument("--parent", default="")
    n.add_argument("--source", choices=SOURCES, default="sim")
    n.add_argument("--strategy-file", default="")
    n.add_argument("--hypothesis", required=True)
    n.set_defaults(fn=cmd_new)
    f = sub.add_parser("finish")
    f.add_argument("id")
    f.add_argument("--metrics", required=True)
    f.add_argument("--verdict", choices=["keep", "reject", "inconclusive"], required=True)
    f.add_argument("--note", default="")
    f.set_defaults(fn=cmd_finish)
    s = sub.add_parser("show")
    s.add_argument("--top", type=int, default=10)
    s.set_defaults(fn=cmd_show)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
