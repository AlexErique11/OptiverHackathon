**Strategy #6: live position candidate**

Canonical strategy file: `optiver_bot/strategies/market_maker#6.py`. Controller,
execution and replay entry points keep their existing names. Numbered strategy
modules are loaded with importlib because '#' cannot appear in an import statement.

The deployable runner is `optiver_bot/live_position_runner.py`. Existing strategies
are unchanged. Imports, demo and replay never connect. `run` opens ONE connection
for shadow inference; only `run --live` submits/cancels orders. This candidate has
been tested against a fake client, not deployed or shown profitable on Optibook.

Deployment in the supplied cloud terminal, from the repository root:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' --help
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' demo
```

To close existing selected inventory BEFORE a clean new run, stop the active bot,
then run (request cap is a local setting; use the verified allowance if known):

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' flatten --live \
  --instruments 'CRWV,MU' --max-requests 60
```

Flattening executes one-lot IOC reductions and waits for confirmation of that
exact lot before another. It never requests new exposure. An unfilled/uncertain
order can stop it without achieving flat; it does not repeatedly submit against
a possibly stale position. Only `FLAT CONFIRMED:` indicates successful completion.
Closing may incur spreads/slippage and realize losses; account PnL does NOT reset.
Then use `run --live` WITHOUT `--manage-existing`; the new run's baseline is its
starting PnL and the live guard requires all selected positions to be zero.

Read `/home/workspace/AGENTS.md`, `/home/workspace/api_reference.md` and the unlocked
challenge rules first. Confirm method/response fields against the supplied client.
The startup preflight reads/hashes the two supplied reference files without printing
their contents; it checks their presence, NOT that event semantics were reviewed.
Stop the previous team bot before connecting even in shadow mode. Existing orders
at startup or inventory outside the selected universe refuse takeover. LIVE mode
also refuses startup with ANY selected position. It must not silently unwind
inventory from previous strategies during warm-up. Shadow mode may observe selected
inventory without trading it. Startup prints starting account PnL and positions;
status prints both account PnL and change from that run's baseline. The loss-limit
setting is a trigger relative to startup PnL, NOT a guaranteed maximum realized loss.
An operator can explicitly choose `run --live --manage-existing` to start from
current selected positions. This enables reduction of inherited inventory, not
adoption of its unknown entry cost or a reset of account PnL. Existing shorts may
be bought back immediately; their price risk and unwind costs still affect the
run-relative PnL. New entries wait for portfolio slots and risk capacity.

Set these shell variables to confirmed event settings and chosen cash-risk budgets
(no example value here is an event rule). `DURATION_SECONDS` is the time REMAINING,
not an extra twenty minutes when the session is already underway. Request limits
include every raw API call across all instruments, and REQUEST_WINDOW is in seconds.

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#6.py' run \
  --instruments "${STOCK_IDS:?set comma-separated confirmed stock IDs}" \
  --duration-seconds "${DURATION_SECONDS:?set remaining session seconds}" \
  --cost-ticks "${COST_TICKS:?set conservative round-trip costs in ticks}" \
  --max-requests "${MAX_REQUESTS:?set verified team-wide request budget}" \
  --request-window "${REQUEST_WINDOW:?set budget window seconds}" \
  --max-order-size "${MAX_ORDER_SIZE:?set verified maximum order quantity}" \
  --gross-limit "${GROSS_LIMIT:?set portfolio gross-notional budget}" \
  --cash-risk "${CASH_RISK:?set per-position cash-risk allowance}" \
  --loss-limit "${LOSS_LIMIT:?set session loss allowance}"
```

That command uses real data but sends NO orders. For live trading, stop shadow mode
and rerun the same command with `--live`. There is no automatic switch to live.
Optional sizing flags: `--cap 10 --target 3 --clip 1` (defaults), and `--max-age 1`.
Never set cap above 100. Costs must include the intended conservative spread,
fees and slippage budget; use a worst-case scalar across selected stocks or choose
a smaller universe if their costs differ materially. Keep logs under logs/ or
results/. Python -m optiver_bot.live_position_runner accepts the same arguments.

The full sampling sweep currently uses roughly six raw reads per instrument plus
connection/PnL/news reads, and extra reads around actual order changes. All are
rate-limited. If this exceeds the verified budget, frames slow down; gaps over
2.5 seconds reset warm-up and stale stocks cannot support new entries. Select a
smaller confirmed stock universe instead of exceeding the request allowance or
discarding risk checks. Irregular valid observations use actual elapsed time,
time-based return windows and volatility normalized per square-root second.
Normal sub-second variation in frame completion time is accepted; it is not a
reason to raise ValueError. The main loop always sleeps at least 0.2 seconds.
Failures identify exception type and source location without printing client
exception text, credentials or locals.

The independent lifetime reservation backstop counts every submitted quantity
until confirmed opposite fills replenish capacity. Cancellation alone does NOT
release that backstop capacity, even after the normal order ledger reconciles.
Repeated unfilled replacements can therefore exhaust the small internal cap and
stop new entries. This deliberate conservative limitation is logged via envelopes;
do not restart merely to bypass it while any order outcome is uncertain. A rejected
or changed-book insertion may also conservatively consume backstop room. Unknown
insert outcomes remain blocked; never retry them blindly.

The runner logs normalized market frames, actual fills and cost bases, decisions,
execution outcomes, risk envelopes and markouts to ignored logs/strategy6/. Logs
can be passed to live_position_replay (the settings header is skipped). JSON output
never includes credentials or raw client exceptions. Ctrl+C/SIGTERM, failure and
normal exit attempt to cancel orders and disconnect. Cancelling/disconnecting does
NOT guarantee liquidation; wind-down works reductions before the deadline and
remaining inventory must be inspected. This runner never starts a second connector.

The deterministic entry hypothesis is persistent same-direction 5-second and
20-second stock returns, matching basket direction, breadth at least 0.625,
standardized slow-move strength at least 1.5, no strongly opposing depth
imbalance, and three confirming observations. Past movement must exceed twice
explicit estimated round-trip costs; this heuristic is NOT a forecast of future
edge. Prices must be fresh, valid and within the configured spread threshold.

Default targets are three lots, one-lot clips, internal cap ten. The controller
uses up to 121 one-second observations per stock. Warm-up needs thirty valid
return intervals, at least ten changed intervals and 75% basket coverage. A gap
over 2.5 seconds or stale data resets affected history;
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

The cloud adapter is `live_position_runner.LiveAdapter`. It preserves timestamps,
tracks incremental own fills and actual fill cost bases, reconciles fresh position/
order reads and uses the independent lifetime reservation backstop above. It
rejects unsupported schema instead of guessing success. Unknown feed metadata
is not interpreted as sentiment: posts trigger scoped or basket caution only.
Do not attach #1/#4 unchanged or run their connectors alongside this runner. The
supplied cloud reference and unlocked challenge brief remain unavailable on this
Windows machine; actual environment/schema compatibility must be checked in cloud
before live operation. Fake-client tests cannot certify server ordering/atomicity.

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
