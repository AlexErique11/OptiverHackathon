# Strategy 10: confirmed momentum with persistent position targets

This candidate builds towards a chosen positive or negative inventory in each
instrument. It holds a supported target instead of continuously buying and selling
just to change position. Defaults allow +100 or -100 per stock; the operator can
choose a smaller cap/target. This is a directional strategy, not spread capture.

The supplied screenshots show some competitors at exact per-instrument limits.
Those positions do not identify their signal, entry time, executable entry price,
or risk controls. This implementation does not copy or read competitors' signals.
The repeated AMD screenshot is one observation, not independent evidence.

## Hypothesis and signal

The hypothesis is that a sustained price move sometimes continues. It is an
assumption to evaluate, not an established advantage in this simulation.
There is no fundamental fair-value estimate. Midpoint is a descriptive price
reference; thresholds are not predicted future returns.

History is maintained separately per stock outside the pure `decide` function.
After at least 20 seconds and 8 observations, enter long if midpoint increased by
at least 1 tick over the last 5 seconds and 3 ticks over 20 seconds, and the last
3 nonzero price changes were positive. Short uses the symmetric negative test.
Require gaps no larger than 3 seconds. A 5-tick single-step jump resets history
and pauses entries for 5 seconds; it is not chased immediately.

Entries require a fresh, two-sided, tick-aligned book, spread at most 2 ticks,
and explicit round-trip costs. The slow movement threshold must exceed the cost
input plus 0.5 ticks. This is a rough affordability filter, not an expected-profit
calculation. The last traded price, graph colour, company names, sector links,
and competitors' inventories do not enter the signal.

## Position and order behavior

- Target is +100 or -100 by default. Build with passive clips of at most 5 shares.
  Join a one-tick spread; improve by one tick when the spread is two ticks wide.
- At the target, place no more exposure-increasing orders. Reconcile existing
  orders rather than inserting a fresh copy on every loop.
- Preserve the target through a neutral signal for up to 10 seconds after its
  last support. Opposite confirmation starts an exit immediately.
- Close before reversing. A new opposite target waits for observed flat inventory
  and a 2-second reentry delay. Pending exit orders still need reconciliation.
- Also exit after 60 seconds holding, a 5-tick adverse move, a feed gap/shock,
  or the session wind-down. The stop uses midpoint at first observed inventory,
  **not verified fill VWAP**. Inherited inventory starts in reduction mode.
- Stop entries 30 seconds before the configured deadline; request flat inventory
  15 seconds before it. Exits are passive and can remain unfilled. Stopping the
  process cancels orders through the shared runner; it does not guarantee flatness.

Every stock has independent buy and sell reservations. Opposite-side orders and
other stocks never offset a capacity check. Final submission reads position,
orders, then position again; a changed position, opposing pending orders,
exhausted capacity or a reached target rejects the submission. The shared runner
still owns the one exchange connection. Its legacy early stop at 90 is temporarily
changed to 101 only inside this entry point, so legal +/-100 targets can be held;
fresh per-order hard limits remain 100. The original guard is restored on exit.

`Snapshot` extends the existing shared input with `target_position` and
`reducing`. `decide(snapshot, config)` returns shared `DesiredOrder` objects.
The controller and CLI stay in this strategy file; shared models and the peer
live-position runner are unchanged.

## Run and verify

Offline; no exchange connection:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#10.py' demo
/opt/venv/bin/python -m unittest discover -s tests -p test_target_position_10.py -v
/opt/venv/bin/python -m optiver_bot.demo
```

The `run` subcommand defaults to dry-run orders but opens the one real data
connection. Stop the previous bot first. Supply confirmed values rather than
copying example limits:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#10.py' run \
  --instruments AMD --cap 100 --target 100 --size 5 \
  --cost-ticks "$ROUND_TRIP_COST_TICKS" \
  --max-requests "$MAX_REQUESTS_PER_SECOND" \
  --max-order-size "$MAX_ORDER_SIZE" \
  --session-seconds "$SECONDS_REMAINING"
```

Only the team operator adds `--live` after verifying integration. Ctrl+C or
SIGTERM stops the shared runner. JSONL target transitions and the runner CSV go
to ignored `logs/`. The request limiter conservatively counts all client method
calls over a rolling one-second window, including reads and connection checks.

The teammate update removed `market_maker #1.py`; #5's existing runtime loader
now falls back to the compatible `market_maker#4.py`, including its news-pause
configuration. It also removes a partially imported module if loading fails.
No peer strategy files were replaced and the paused #9 merge is untouched.

## Evidence and evaluation

`scripts/diagnose_target10.py` processes recorder CSVs or derived timestamp/book
JSON. It removes exact duplicate observations, retains repeated prices at distinct
times, and rejects conflicting books for the same stock/timestamp.

```bash
/opt/venv/bin/python -m scripts.diagnose_target10 \
  --csv data/recording.csv --tick 0.1 --cost-ticks 0 > results/target10.json
```

Tick 0.1 and zero fees in this diagnostic are explicit assumptions, not newly
verified event rules. No passive fills, queue positions, latency or slippage are
simulated. On the earlier 265.609-second recording: 4,184 observations, no exact
duplicates, 331 entry-quote evaluations (258 in the first 180 seconds, 73 later).
Positions were held flat for signal coverage. These are neither actual orders
nor a profit backtest. Parameters were fixed before this check; there was no
parameter search. The later period is a chronological diagnostic holdout, not
independent validation given that earlier strategy development saw this recording.

Keep parameters fixed for a new unseen recording. Compare against the baseline
with identical inputs/costs, using actual fills or explicit conservative queue and
latency assumptions. Report net PnL, drawdown, directional exposure, time at limit,
fills, rejects and requests. A profitable short in a falling sample alone does
not prove signal skill. The available quote-only data cannot rank this candidate
above the baseline or explain the leading teams' performance.

## Integration requirements and failure modes

Actual fees, request/order rules beyond the confirmed per-stock +/-100 limit,
and portfolio/notional limits still require the challenge reference and execution
owner's confirmation. The reused adapter measures book age since receipt, not a
verified source timestamp, and sequential reads are not an atomic account snapshot.
The owner should verify source freshness, in-flight request handling, actual-fill
logging and all portfolio rules before live use. No live bot was started here.

Fast reversals can make this signal buy late and sell late. Flat markets abstain;
the strategy will not force a position simply because competitors hold one.
Passive entries may never fill and passive exits may fail during a crash. At
100 shares, a 1-unit adverse move loses approximately 100 units in that stock;
correlated positions can multiply the loss. Price stops do not cap gap losses.
Monitoring many stocks at a low request budget can exceed the history gap gate,
causing abstention. A smaller universe/cap is configurable, not a latency guarantee.

Current full-suite integration blocker on the incoming team commit: the existing
`tests/test_strategy_2.py` still imports the now-deleted `market_maker #2.py`.
That unrelated test/strategy pair is left for its owner to reconcile.
