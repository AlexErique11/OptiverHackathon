# Strategy #5: rule-of-thumb inventory-aware market making

File: optiver_bot/strategies/market_maker#5.py. Prior strategies #1-#4 and shared
models are unchanged. Uses #1's runner through dynamic import, avoiding a copied
runner and allowing a normal .py entry point in the supplied cloud environment.

## Why this candidate

For the user's second, roughly static simulation, use ordinary passive spread
capture instead of waiting for a trend. It is inspired by the standard inventory
control idea, not a calibrated solution of the Avellaneda-Stoikov model:
https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf.
Prices staying still does not imply positive profit: there must be incoming
trades at our quotes, and fees/queue position/adverse selection still matter.

## Exact rule

At flat inventory, join the CLEANED best bid and ask with three units each.
No warm-up/history required. Do not cross or improve a one-tick spread. Require
observed spread in ticks >= explicit estimated round-trip cost + 0.5 ticks.
Costs default to None (no NEW exposure). A zero-cost assumption must be explicit.
The midpoint is a descriptive reference, not asserted true fair value.

Default internal cap is 20. Long inventory tapers the bid size and widens only its
price; the ask stays at touch. Shorts work symmetrically and selling from zero is
allowed. At 80% of cap, suppress the exposure-increasing side. In formulas, the
size is min(independent capacity, max(1, floor(size*(1-exposure/soft_cap)))) below
the soft cap; zero at/above it. Widen by floor(2*exposure/soft_cap) ticks. Exposure
for tapering uses held position. Outstanding orders are separately reserved in
capacity; using them again for tapering would create quote-size feedback/churn.

When costs are unknown or spread too narrow, only position-reducing quotes are
permitted, with volume constrained not to reverse through zero. Matching quotes
are retained by #1's reconciler to preserve queue position; partial fills need
not be topped up. Identical market data does not justify endless cancel/replace.

The Controller consumes cleaned books from #1 and keeps only last midpoint and
cooldown state. A move >=5 ticks pauses new exposure for five seconds. During the
pause, reducing quotes remain but cannot reverse through zero. Exits are PASSIVE,
not guaranteed immediate flattening. A gap can still cause losses. Ctrl+C cancels
orders using #1 cleanup, but does NOT liquidate held inventory. The operator can
use #1's flatten command after stopping the runner; do not run both together.

## Risk/execution integration

decide(snapshot, config) is deterministic with no connection or state mutation.
Its local Snapshot includes instrument, top prices, tick, position, pending buy
and sell volumes and age. The Controller maps #1's depth Snapshot to this shape.
Every proposed side independently reserves outstanding orders under +/-100 and
the lower cap. Opposite-side orders and other instruments never offset exposure.

The driver's adapter wraps the existing runtime's adapter and shares ONE client.
Before every insert (including after a cancellation), read positions, outstanding
orders, and positions again. If the two position reads differ, skip the insert.
Recalculate independent side capacities; reread the book and reject crossing
quotes. This addresses cancellation/fill races without assuming cancellation
success frees risk. These snapshots are not an exchange-atomic transaction;
exclusive ownership of the trading username remains essential. No second bot.

Core age validation and the Controller track local retrieval time. The current
#1 adapter exposes no verified exchange update timestamp; recently fetched data
is not guaranteed fresh at the market source. The cloud API fields remain marked
VERIFY in #1. Confirm them against the supplied reference before live use.

RequestBudget conservatively counts EVERY raw API method call after connection
(reads, checks, inserts and cancellations) in a rolling one-second window. No
numeric event budget is invented. --max-requests and --max-order-size must be
set from verified rules. This may be slower than a write-only event allowance;
it does not claim millisecond execution. Price/order bands remain enforced by
#1's clamp. All new orders are passive limits. No automatic reconnect.

## Run

No arguments, or demo, is OFFLINE and opens no connection:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#5.py' demo
```

Market-data dry run requires the same username's sole connection, even though it
does not mutate exchange orders. Stop the other bot first. Replace placeholders
with verified event values and actual estimated costs:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#5.py' run \
  --instruments AMD --cap 20 --size 3 \
  --cost-ticks COST --max-requests REQUEST_BUDGET --max-order-size ORDER_LIMIT
```

Only the team operator adds --live to submit orders. Config validation occurs
before creating the client. Driver calls the connection factory exactly once,
then reuses #1's run_bot, reconciliation, logs and cleanup. Keep #1 alongside #5.
No live bot was started while implementing/testing. No actual event costs or
request limits have been verified, and no profitability result is claimed.

## Verification and evaluation

Run python -m unittest discover -s tests -v; python -m optiver_bot.demo.
Tests cover both touches without history, short selling, inventory taper, stale
and invalid books, independent hard/lower caps, costs, same-quote retention,
partial fills, cancellation/fill races, refreshed crossing checks, shock cooldown
and non-reversal, conservative request pacing and dry-run non-mutation. Synthetic
FakeExchange fills are mechanics tests, not evidence of a trading edge.

For selection, compare #1/#5 with identical real chronological input, fees,
latency, queue and fill assumptions. Measure net PnL, drawdown, actual fills,
inventory, request counts and rejects. If no one trades against resting orders,
remaining flat is expected; changing orders just to show activity is not an edge.
Keep logs/results in ignored directories.
