**Strategy #6: live position candidate**

Canonical strategy file: `optiver_bot/strategies/market_maker#6.py`. Controller,
execution and replay entry points keep their existing names. Numbered strategy
modules are loaded with importlib because '#' cannot appear in an import statement.

Implementation branch: `codex/live-position-strategy`. Existing strategies are
unchanged. No exchange connection is opened by any new module, including imports,
demo and replay. This candidate has not been deployed or shown profitable.

The deterministic entry hypothesis is persistent same-direction 5-second and
20-second stock returns, matching basket direction, breadth at least 0.625,
standardized slow-move strength at least 1.5, no strongly opposing depth
imbalance, and three confirming observations. Past movement must exceed twice
explicit estimated round-trip costs; this heuristic is NOT a forecast of future
edge. Prices must be fresh, valid and within the configured spread threshold.

Default targets are three lots, one-lot clips, internal cap ten. The controller
uses up to 121 one-second observations per stock. Warm-up needs thirty valid
return intervals, at least ten changed intervals and 75% basket coverage. A gap
over the one-second sampling tolerance or stale data resets affected history;
missing information never becomes a zero return. All thresholds are unvalidated
test settings. Feed/market faults disable new entries. Costs, gross notional,
position cash-risk and loss limit must be configured before any entry.

Held positions exit on stop, opposing news, persistent thesis failure, sixty
seconds maximum holding time, session wind-down or a latched session loss limit.
Reversals go through confirmed flat. No pyramiding or averaging down. The deadline
is explicit: entries stop ninety seconds before it and wind-down begins sixty
seconds before it. Stops and IOC exits do not guarantee a price or a fill.
Inherited inventory is reduction-only even during warm-up. Supply verified
weighted fill prices through `entry_prices` on first observed entry. Missing
cost basis forces reduction-only handling; no midpoint is assumed to be a fill.
The owner must maintain actual-fill accounting and reconciliation.

Run offline:

```powershell
python -m unittest discover -s tests -v
python -m optiver_bot.demo
python -m optiver_bot.live_position --demo
```

The new demo generates artificial rising prices and prints desired orders. It
does not simulate fills, queue priority or profit. On this Windows checkout use
the installed interpreter under AppData/Local/Python/pythoncore-3.14-64 if the
WindowsApps python alias is inaccessible. Tests with temporary files may require
TEMP/TMP set to a directory under ignored results/. In cloud use /opt/venv/bin/python.

Public integration interfaces:

- Strategy #6's `Config`, `DepthSnapshot`, `Features`, `PositionSnapshot`
  and `decide(snapshot, config)` implement pure rules and target orders.
- `live_position.Controller.update(time, books, equity=..., account_ok=...,
  news=..., feed_ok=...)` maintains bounded market/lifecycle state. Feed one
  approximately one-second analysis frame. Use a new controller after session
  reset; faster account/order reconciliation belongs to the owner execution loop.
- `News(event_id, received_at, instruments, sentiment)` is normalized received data.
  Use verified metadata/parser output;
  no generic NLP classifier has been implemented. A scoped/unknown post applies
  a twenty-second scoped/basket pause. Duplicates do not refresh it; future or
  already expired events cannot affect current decisions. Opposing sentiment
  exits existing exposure. Neutral/unknown news still invokes caution.
- `live_position_execution.Executor` takes an injected adapter and common request
  `Budget`. It retains matching orders, cancels mismatches, and requires a new
  reconciled revision before replacing. It rechecks current position versus the
  target, independent side capacities, tick prices and passive-entry constraints.
  Uncertain insert/cancel outcomes remain reserved; they cannot release capacity.

The adapter contract is deliberately strict. `read(instrument)` returns a fresh
`Account` containing coherent position, outstanding orders and reservations.
`resolved` contains insert client tokens/cancelled order IDs whose outcomes have
been definitively reconciled. An accepted insert may be resolved only once its
remaining live quantity and actual fills are represented in the account view.
Separate position/order API reads are not automatically coherent. An adapter must
implement reconciliation barriers, include unresolved requests, and leave
`certain=False` when it cannot establish the view. No timeout releases risk.

`entry_allowed` defaults to false. The owner may set it true only after verifying
global worst-case portfolio budgets, deadline, product/order size/price-band rules,
and all exchange constraints on that same account revision. Per-stock checks here
do not replace the global owner check. All underlying reads, polling and mutation
requests must share the real API limiter; the Budget charge at the executor
boundary covers one logical adapter operation only. If one logical read makes
multiple actual API requests, the adapter must charge the additional calls too.
Reserve request capacity for risk management. Token identities must be unique
within the adapter session; reconstruct outstanding/reservations before restart.

No actual cloud adapter has been provided: the unlocked challenge brief and
/home/workspace/api_reference.md are unavailable locally. Read those and
/home/workspace/AGENTS.md before client integration. Do not plug this into #1/#4's
runner unchanged: it discards timestamps, uses old positions after cancellation
and does not implement this coherent revision/reservation contract. No second
connector should be introduced. Use the existing single owner and centrally poll
poll_new_social_media_feeds(); verify message fields and consumption semantics.

For recorded observations, JSONL rows contain `time`, `equity`, `account_ok`,
`feed_ok`, `books` keyed by fixed instrument ID, optional `entry_prices` (verified
weighted fill prices keyed by instrument), optional `news` normalized by
actual receipt, and optional observed `fills`. Book fields are shared Snapshot
fields without instrument_id, optionally bid_volume/ask_volume. Each fill has
id, instrument, side (bid/ask), price and volume. Times are monotonic session
seconds. Feed the already reconciled views, not a raw asynchronous UI export.

```text
python -m optiver_bot.live_position_replay --input data/capture.jsonl --config config/challenge.local.json --deadline 1200 --output results/position.jsonl
```

Config is a JSON object of this candidate's Config fields (not the starter config
schema). Replay emits decisions and 1/5/15-second signed midpoint markouts from
observed fills, records actual elapsed horizons, never invents fills and writes
only under ignored logs/ or results/. A successful replay validates mechanics,
not live profitability. Compare a frozen candidate on later data with the same
execution assumptions; return forecasts, markouts and realized after-cost PnL
must be assessed separately. Outstanding final marks may remain unobserved.
