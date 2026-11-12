"""CLI: generate synthetic input, select on validation, then evaluate holdout."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import platform
import random
import subprocess

from evaluation.replay import Replay, Settings, validate_frames
from evaluation.runtime import ROOT
from evaluation.audit import audit_candidate

REPO = Path(__file__).resolve().parents[1]
MODULE = "optiver_bot.strategies.inventory_maker"


def ignored_output(path):
    path = Path(path).resolve()
    # Enforce local ignored directories even before the first Git commit.
    if not any(path.is_relative_to((REPO / name).resolve()) and path != (REPO / name).resolve()
               for name in ("data", "logs", "results")):
        raise ValueError("Output must be below this checkout's data/, logs/ or results/")
    check = subprocess.run(["git", "check-ignore", "--no-index", "-q", str(path)], cwd=REPO)
    if check.returncode != 0:
        raise ValueError("Output is not Git-ignored")
    return path


def write_json(path, payload):
    path = ignored_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def generate(seed, count):
    if count < 30:
        raise ValueError("At least 30 synthetic frames required")
    rng = random.Random(seed)
    prices = {"SIM_A": 100.0, "SIM_B": 80.0}
    frames = []
    for step in range(count):
        books = {}
        for index, instrument in enumerate(prices):
            # Fabricated regime change and trade flow; cannot establish an edge.
            drift = 0 if step < count * .6 else (.03 if step < count * .8 else -.05)
            prices[instrument] = round(max(1, prices[instrument] + drift + rng.choice([-.2, -.1, 0, .1, .2])), 1)
            mid = prices[instrument]
            trades = []
            for _ in range(rng.randrange(3)):
                aggressor = rng.choice(["buy", "sell"])
                offset = rng.choice([0.0, .1, .2, .3, .4])
                trades.append({"aggressor": aggressor, "price": round(mid + (offset if aggressor == "buy" else -offset), 1),
                               "volume": rng.randrange(1, 16)})
            books[instrument] = {"bid": round(mid - .1, 1), "ask": round(mid + .1, 1),
                                 "tick_size": .1, "bid_volume": rng.randrange(1, 12),
                                 "ask_volume": rng.randrange(1, 12), "trades": trades}
        frames.append({"timestamp": step, "books": books})
    return {"provenance": "synthetic", "seed": seed, "frames": frames}


def default_suite():
    config = {"position_limit": 100, "order_size": 5, "half_spread_ticks": 2.0,
              "inventory_skew_ticks": 2.0, "max_book_age_seconds": 2.0}
    baseline = {"name": "baseline", "module": MODULE, "config": config}
    families = [{"name": "symmetric", "candidates": [
        {"module": MODULE, "config": {**config, "inventory_skew_ticks": 0, "half_spread_ticks": h}}
        for h in (1, 2, 3)]},
        {"name": "stronger_inventory_skew", "candidates": [
            {"module": MODULE, "config": {**config, "inventory_skew_ticks": skew, "half_spread_ticks": h}}
            for skew in (4, 8) for h in (1, 2, 3)]}]
    return {"baseline": baseline, "families": families, "settings": asdict(Settings())}


def split_chronologically(frames):
    # Fixed before any candidate evaluation. No random folds or overlapping rows.
    n = len(frames)
    a, b = int(n * .6), int(n * .8)
    parts = {"train": frames[:a], "validation": frames[a:b], "holdout": frames[b:]}
    if any(len(part) < 2 for part in parts.values()):
        raise ValueError("Each chronological partition needs at least two frames")
    return parts


def evaluate(candidate, settings, frames):
    replay = Replay(candidate, settings)
    metrics = replay.run(frames)
    return metrics, replay.trace


def compare(dataset, suite):
    frames = validate_frames(dataset["frames"])
    parts = split_chronologically(frames)
    settings = Settings(**suite["settings"])
    candidate_audits = [{"name": suite["baseline"]["name"],
                         "audit": audit_candidate(suite["baseline"])}]
    for family in suite["families"]:
        for index, candidate in enumerate(family["candidates"]):
            candidate_audits.append({"name": family["name"], "index": index,
                                     "audit": audit_candidate(candidate)})
    selected = [suite["baseline"]]
    development = []
    names = {suite["baseline"]["name"]}
    for family in suite["families"]:
        if family["name"] in names or not family["candidates"]:
            raise ValueError("Candidate families need unique names and nonempty grids")
        names.add(family["name"])
        records = []
        for index, candidate in enumerate(family["candidates"]):
            # Each segment starts flat with no orders; no state crosses splits.
            train, _ = evaluate(candidate, settings, parts["train"])
            validation, _ = evaluate(candidate, settings, parts["validation"])
            record = {"index": index, "candidate": candidate, "train": train, "validation": validation}
            records.append(record)
        best = max(records, key=lambda r: (r["validation"]["liquidation_estimate_after_costs"],
                                          -r["validation"]["max_drawdown"], -r["index"]))
        selected.append({**best["candidate"], "name": family["name"]})
        development.append({"family": family["name"], "selected_index": best["index"], "records": records})
    # Selection is finished before holdout access. Report every selected candidate
    # under identical base/stress settings; no holdout-driven parameter changes.
    scenarios = {"base": settings,
                 "no_passive_fills": replace(settings, passive_model="none"),
                 "optimistic_touch": replace(settings, passive_model="touch", queue_fraction=0),
                 "latency_cost_stress": replace(settings, latency_steps=max(2, settings.latency_steps * 2),
                    cancel_latency_steps=max(2, settings.cancel_latency_steps * 2),
                    fee_per_unit=settings.fee_per_unit * 2, slippage_ticks=settings.slippage_ticks + 1)}
    holdout, traces = {}, {}
    for scenario, scenario_settings in scenarios.items():
        holdout[scenario] = {}
        for candidate in selected:
            metrics, trace = evaluate(candidate, scenario_settings, parts["holdout"])
            holdout[scenario][candidate["name"]] = metrics
            traces[f"{scenario}/{candidate['name']}"] = trace
    return {"provenance": dataset.get("provenance", "unspecified"), "selected": selected,
            "candidate_risk_audits": candidate_audits,
            "partitions": {name: {"count": len(part), "start": part[0]["timestamp"],
                                  "end": part[-1]["timestamp"]} for name, part in parts.items()},
            "development": development, "holdout": holdout,
            "recommendation": "Insufficient evidence to promote a candidate. Synthetic/queue-modeled fills and unverified event costs cannot establish competition performance."
                if dataset.get("provenance") == "synthetic" else
                "Offline screening only. Validate event costs, execution assumptions, independent risk audit and observed fill/markout behavior with the integration owner before promotion."}, traces


def markdown(report):
    lines = ["# Offline comparison", "", report["recommendation"], "",
             f"Input provenance: {report['provenance']}. PnL is midpoint marked after modeled costs.", "",
             "| Scenario | Candidate | PnL | Close estimate | Drawdown | Execution edge | Directional | Fills / units | Rejected / risk refused | Writes / reads |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for scenario, candidates in report["holdout"].items():
        for name, m in candidates.items():
            c = m["counts"]
            lines.append(f"| {scenario} | {name} | {m['pnl_after_costs']:.4f} | {m['liquidation_estimate_after_costs']:.4f} | {m['max_drawdown']:.4f} | {m['spread_capture']:.4f} | {m['directional_pnl']:.4f} | {c['fills']} / {c['filled_units']} | {c['rejected_requests']} / {c['risk_refusals']} | {c['request_attempts']} / {c['modeled_read_requests']} |")
    lines += ["", "| Base candidate | Peak inventory by instrument | Terminal inventory | Peak reserved position | Peak gross notional |",
              "|---|---|---|---|---:|"]
    for name, m in report["holdout"]["base"].items():
        lines.append(f"| {name} | {m['peak_abs_inventory']} | {m['terminal_inventory']} | {m['peak_reserved_abs_position']} | {m['peak_gross_exposure']:.4f} |")
    lines += ["", "Detailed inventory by instrument, reserved envelopes, fees, counts, selected parameters and assumptions are in comparison.json; event traces are in traces.json.", "",
              "Assumptions: cancel-pending orders can fill before acknowledgment; queue ahead uses displayed same-side depth times queue_fraction; strict trade-through is the base passive model. Tape replay has no market impact or proven queue priority. Latency is in frame steps (minimum one frame); slippage is an adverse accounting penalty. Close estimate assumes unlimited depth at final BBO plus costs and is not an executed liquidation. No passive fills is the zero-maker-fill sensitivity, not a PnL lower bound. Different strategies have different inventories, so PnL need not vary monotonically with fill conservatism.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--output", default="data/evaluation/synthetic.json")
    gen.add_argument("--seed", type=int, default=1729)
    gen.add_argument("--frames", type=int, default=600)
    run = sub.add_parser("run")
    run.add_argument("--data", default="data/evaluation/synthetic.json")
    run.add_argument("--suite", help="Local JSON suite; omit for documented frozen reference variants")
    run.add_argument("--output", default="results/evaluation")
    args = parser.parse_args()
    if args.command == "generate":
        write_json(args.output, generate(args.seed, args.frames))
        print(f"Generated synthetic input: {args.output}")
        return
    data_path = Path(args.data)
    dataset = json.loads(data_path.read_text(encoding="utf-8"))
    suite = json.loads(Path(args.suite).read_text(encoding="utf-8")) if args.suite else default_suite()
    report, traces = compare(dataset, suite)
    source_paths = sorted(set(ROOT.rglob("*.py")) | set((REPO / "evaluation").glob("*.py")))
    report["manifest"] = {"python": platform.python_version(), "strategy_root": str(ROOT),
                          "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                          "suite": suite, "sources_sha256": {
                              str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p):
                              hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
    out = Path(args.output)
    write_json(out / "comparison.json", report)
    write_json(out / "traces.json", traces)
    summary = ignored_output(out / "comparison.md")
    summary.write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"Artifacts: {out.resolve()}")


if __name__ == "__main__":
    main()
