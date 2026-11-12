# Strategy #6: inventory-light regime market maker

Module: optiver_bot/strategies/market_maker#6.py. Dependencies: #5's risk/pacing
wrapper and #1's runner, in the same folder. Strategies #1-#5 are unchanged.
No args or demo is offline. run creates ONE connection; orders default dry-run.

## Frozen rule-of-thumb defaults

Per instrument, retain 20 seconds of midpoint observations. Require >=8 samples
with no gap >3 seconds. Count distinct price changes, not unchanged polls. If net
change is within 2 ticks, classify range and quote both cleaned touches. If net
change is >=3 ticks and the last 3 nonzero changes agree, classify up/down, provided
current midpoint is on the same side of its last-five-second average. Up quotes
only bids, down only asks, with #5's inventory taper/widening. Otherwise abstain
from new exposure. This is an uncalibrated heuristic, not estimated fair value.

Default size=2 and internal cap=10; hard per-instrument cap remains 100. Reserve
pending buys and sells independently. Short selling from zero is permitted.
New exposure needs explicit estimated round-trip costs and sufficient spread,
using #5's cost buffer. There is no hidden zero-cost live assumption.

Any consecutive observed move >=5 ticks resets the entire price history and
blocks new exposure for at least 5 seconds. Then require a complete new 20-second
window before entering. Thus an old crash cannot by itself remain a short signal.
A sample gap/clock anomaly also clears history; held inventory requests reduction.

Request reducing quotes when a directional signal disappears/reverses, inventory
opposes the trend, the holding timer reaches 20 seconds, or price moves 3 ticks
against its reference. Exit mode stays latched until a confirmed flat position,
then wait 2 seconds before new entries. Partial fills/additions do not reset the
holding timer. Range positions may stay open until risk/time criteria trigger.
Restart inventory with unknown lifecycle is reduced before new exposure.

IMPORTANT: the reference is the midpoint when a new position is first OBSERVED,
not actual fill VWAP. It is a risk trigger, not a realized-PnL stop. The existing
runner lacks verified fill polling. Improve this reference once the execution
owner supplies actual fill records; never label the current estimate fill price.

## Integration and execution

decide(snapshot, config) is pure. Controller owns per-stock causal histories,
position lifecycle, cooldowns, exit latching and change-only structured logs.
It consumes #1's cleaned books, excluding own orders, and #5's outstanding-order
cache. Histories are never fitted using future rows. Runtime tick sizes come
from instrument metadata, not the diagnostic's assumed tick.

The driver uses #1 run_bot and #5 fresh risk checks and rolling request budget.
ReducingRaw adds a final pre-submit exit check on the SAME raw client: stable
position reads, correct reducing side, no opposing pending orders, and pending
exit volume + proposed volume <= held quantity. A fill/cancel race must not turn
an exit into a new position. Requests remain budgeted through #5's BudgetedRaw.
No exchange calls occur in decide; no other bot or connection was started.

All quotes/exits are PASSIVE LIMITS. Exit thresholds trigger requests, not a
guaranteed fill, maximum loss or deadline to flat. If no takers arrive, positions
can remain open. Ctrl+C cancels resting orders through #1 cleanup; it does not
liquidate inventory. Stop the runner before invoking #1's separate flatten tool.
Do not run #6 in parallel with #1/#5 or any other bot under the same username.

#5 counts all raw API calls after connection against the operator's supplied
budget, including reads. No event request/order limits or costs are invented.
Snapshots are not exchange-atomic and require exclusive execution ownership.
Freshly retrieved data may still be stale at source: #1 does not expose a verified
exchange update timestamp. Local age checks cannot establish source freshness.
Actual API fields and event limits must still be checked against the cloud brief.

## Commands (repository root in the provided cloud)

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' demo
```

Market-data dry-run below still uses the team's sole connection. Replace COST,
REQUESTS and MAX_ORDER with verified values before running:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' run \
  --instruments AMD,AVGO,CRWV,MU,NBIS,NVDA,SNDK,VRT --cap 10 --size 2 \
  --cost-ticks COST --max-requests REQUESTS --max-order-size MAX_ORDER
```

Only the team operator adds --live. No live execution was performed during this
implementation. Config validation occurs before the one connection-factory call.
Regime/position observation logs go to logs/regime6_*.jsonl; #1 quote/state CSVs
go to logs/run6_*.csv. These are observations, not a complete actual-fill ledger.

## Validation and diagnostic replay

57 repository tests pass, including 18 new #6 tests for range/up/down modes,
confirmation, warm-up, gaps, invalid data, independent bounds, stops/timeouts,
latched exits, partial-fill lifecycle, shock reset, cancellation/fill races,
non-reversing exits and one-factory dry-run defaults. Offline demos also pass.

The supplied 4,184-row, 265.609-second recording was processed chronologically
with no parameter changes after the scan. First 180 seconds and later timestamps
are reported separately. The scan assumes tick=0.1, zero fees, flat positions,
no pending orders and positive placeholder book quantities solely to examine
regime/quote outputs. It does NOT simulate fills, inventory or PnL. Therefore
the later segment is a signal diagnostic, not validated out-of-sample profit.

To reproduce on the original CSV without a connection:

```bash
python -m scripts.diagnose_regime6 --csv data/run_20261112T174306Z.csv
python -m unittest discover -s tests -v
python -m optiver_bot.demo
```

Data and outputs remain under ignored data/, logs/ and results/. Compare #1/#5/#6
on the same true chronological market/trade data with measured fees, latency,
queue/fill assumptions, drawdown, actual fills, rejects and request counts.
The recorded other bot's PnL must not be attributed to #6. Profitability remains
unproven and the observed short sample is insufficient for robust optimization.
