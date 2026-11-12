# Offline comparison and independent risk verification

Run from the repository root with Python 3.10+ and Git. No third-party Python packages, exchange client, credentials or network connection are used. Do not run these tools inside a live bot process.

```bash
python -m unittest discover -s tests -v
python -m evaluation.audit
python -m evaluation.compare generate --seed 1729 --frames 600
python -m evaluation.compare run
```

On Optiver cloud, substitute `/opt/venv/bin/python`. On Windows, `scripts/verify_evaluation.ps1 -Python <path-to-python.exe>` runs the same checks, both risk controls, the offline demo and the comparison. The intentional negative-control audit must fail; it does not make the main test suite fail.

Outputs: `results/evaluation/comparison.md`, `comparison.json`, `traces.json`, `risk-audit.json` and `original-risk-audit.json`. Inputs go in `data/`, logs in `logs/`. The tools refuse to write artifacts outside these Git-ignored directories. No generated data, logs, performance reports or challenge brief are committed. Do not force-add them.

## What is compared

The frozen baseline is the supplied inventory maker with cap 100, order size 5, half-spread 2 ticks, skew 2 ticks and maximum book age 2 seconds. The alternatives available locally are parameter families of that same strategy: symmetric quotes (skew 0; half-spread 1/2/3) and stronger inventory skew (skew 4/8; half-spread 1/2/3). These are research controls, not claims of a newly discovered strategy or submitted team candidates.

Every candidate receives identical market frames, starting cash/inventory, instrument universe, fees, fill model, latency, request budget and event ordering within a scenario. Strategies receive only the current snapshot, not future frames. They must be deterministic, stateless implementations of `decide(snapshot, config)`; the current contract has no history/depth signals. The offline execution state persists across frames and reconciles identical targets without duplicate inserts. Strategy modules must not connect to the exchange or have import side effects.

The split is fixed chronologically at 60% training / 20% validation / 20% sealed holdout. All partitions start flat with no orders. Training records are diagnostics; no fitting is required by these stateless candidates. Select one parameter configuration per alternative family using **validation liquidation estimate**, then smaller validation drawdown, then grid order. The baseline stays fixed. Only after selection is frozen are the selected variants and baseline evaluated on the holdout. Do not tune against holdout results, repeatedly reuse the same holdout to select variants, or compare a candidate's validation result with another's test result. New research needs new future data.

Each configuration is independently audited on 3,216 inventory/outstanding-order snapshots *before* the replay's safety gate, so the gate cannot hide unsafe raw proposals. Independent risk tests also inspect every position -100 through +100, long/short boundaries, partial fills, fill permutations, outstanding orders on both sides, pending inserts, rejected cancellation requests, and cancellation/fill races. Configurations above 100 are rejected. Short selling from flat is explicitly tested and allowed. The simulation fails immediately if actual inventory or either reserved fill envelope breaches the configured cap.

## Add the team's actual candidates

Provide a complete checkout with `optiver_bot/` via `EVALUATION_STRATEGY_ROOT` **before starting Python**. The default is the frozen hard-limit reference. Audit the canonical checkout with:

```bash
python -m evaluation.audit --strategy-root /path/to/checkout --output results/evaluation/integration-risk.json
```

For comparison, set the environment variable to the same path. Pass `--suite evaluation.local.json` with a local JSON object containing `baseline`, `families`, and `settings`. `baseline` has `name`, `module` (import path), and `config`; each family has `name` and `candidates`, each with `module` and `config`. Config uses the shared `StrategyConfig`. `python -c "import json; from evaluation.compare import default_suite; print(json.dumps(default_suite(), indent=2))"` shows the complete schema. Keep suite files in ignored locations. Custom configuration classes, history-dependent strategies, instrument relationships and portfolio hedges need an explicit adapter before comparison. Do not invent missing instrument equivalences.

The manifest records input SHA-256, complete suite/settings, source SHA-256 values and Python version. Keep custom modules within the selected strategy root so they are hashed. Pin external dependencies separately if a candidate introduces them. The frozen source manifest records the supplied scaffolding's provenance; see `PROVENANCE.md`.

## Replay input contract

`--data data/your_capture.json` expects `{ "provenance": "recorded", "frames": [...] }`. Set provenance truthfully, retain original captures unchanged, and document collection/synchronization upstream. Each frame has numeric, strictly increasing `timestamp` in seconds and a complete `books` mapping with the same instruments in every frame. Each book has positive `bid`, `ask`, `tick_size`, nonnegative integer `bid_volume` / `ask_volume`, optional nonnegative `age_seconds`, and optional `trades`.

Each trade has positive `price`, positive integer `volume` and `aggressor: "buy" | "sell"`. Trades are incremental, not cumulative: supply each trade once, assigned to the next observation at which it was knowable. Orders decided at a frame become eligible on a later frame. Use synchronized frame marks; asynchronous books require explicit upstream age handling. Snapshot-only data may omit trades, in which case no passive fills are inferred. Never infer fills merely because quotes touch a displayed book. Only top-of-book crossing volume is executable; deeper liquidity is not invented.

## Assumptions and limits

- **Passive fills:** base uses strict trade-through at the limit price, not a touch. The public tape is exogenous, may contain transactions unrelated to our queue, and cannot prove fills. One trade's volume is consumed once across our orders. A `none` model removes passive fills; an optimistic `touch` scenario also assumes zero queue ahead. These are sensitivities, not statistical confidence bounds.
- **Queue:** initial queue ahead equals same-side displayed best-level depth times `queue_fraction` (base 1). Away from the best, unknown queue is approximated by best-level depth. Retained orders keep queue; replacement loses priority. Queue cancellations, hidden liquidity, actual price/time priority and market impact are unknown. The conservative label does not imply a guaranteed bound on PnL.
- **Latency:** insert and cancel delays are fixed numbers of observation steps. Inserts have at least one-event delay even when configured as zero, so they cannot fill on information already observed. A resting order can fill before a same-event cancel acknowledgment. These delays are not measured milliseconds; irregular samples change wall-clock latency.
- **Execution:** marketable limits execute against capped displayed opposite best volume each event at BBO. IOC remainders expire. A resting quote crossed by the next BBO is counted as modeled taker volume; real exchange maker/taker classification may differ. Books replenish exogenously each frame. Insert acceptance and cancellation success are simulated; no actual exchange response or session safety is certified.
- **Costs:** default fee 0.01 per filled unit, zero slippage, write budget 20 per frame are illustrative assumptions. Replace them with confirmed event rules. Adverse slippage in ticks is an accounting penalty even for passive fills, not exchange execution beyond a limit. Stress doubles fees and delays (at least two steps) and adds one tick of slippage. Fee tiers, maker rebates, rate windows, cancel fees and cross-currency multipliers are not modeled.
- **Requests:** insert/cancel attempts, acceptance and budget rejections are separate from preflight risk refusals and modeled read counts. Reads are `1 + 2 * instruments` per decision frame (positions, books, outstanding orders); they are not charged to the illustrative write budget. Fill polling/real API calls/reconnects and event-specific request accounting are absent. No decision requests are modeled on the terminal frame.
- **PnL:** `pnl_after_costs = cash + inventory * midpoint`, starting at zero. Exact accounting decomposition is execution edge relative to contemporaneous midpoint + price change on carried inventory - explicit fees. Slippage is already in execution edge. Positive execution edge alone is not proven paired spread capture or realized round-trip profit; adverse selection appears in subsequent directional PnL. Maximum drawdown uses frame-end marked equity and a zero initial high-water mark, so intra-frame drawdown can be larger.
- **Exposure and close:** report per-instrument minimum/maximum/peak/terminal inventory, reserved worst-case position, peak/terminal gross notional, and event traces. Notional uses unit multiplier 1 and a common accounting currency. Derivatives/multicurrency require metadata adapters. Final midpoint PnL retains inventory and outstanding reservations; the additional liquidation estimate assumes unlimited final BBO depth plus fees/slippage, is not executed, and is not used to erase risk. Event scoring/last-trade marks may differ.

No recorded challenge data, exact objective, costs, request windows or terminal scoring treatment were available here. Synthetic inputs are seeded tests with fabricated flow/regimes; they validate reproducibility and expose assumption sensitivity. They cannot establish a tradable advantage. Promotion requires independent audit on the canonical integrated checkout and evidence from actual observations reviewed with the execution owner.
