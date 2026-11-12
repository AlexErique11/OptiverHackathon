# Improving `ljf_bot.py`

Implementation plan based on repository commit `f7687bf`, reviewed on 12 November 2026.
This document specifies proposed work. The improvements below have not been implemented or shown profitable.

## 1. Objective and recommended design

Improve net PnL by capturing ordinary price moves between rare jumps, making better passive quotes, and using available risk capacity where fills have demonstrated value.

Build one bot with three trading modes and a shared execution layer:

| Conditions | Trading mode | Source of ideas |
| --- | --- | --- |
| Quiet, liquid market with an economical spread | Two-sided inventory-aware market making | Current maker, #5 and `market_maker_e2e.py` |
| Confirmed gradual movement | Trend-aware quotes toward a bounded directional inventory target | #6 |
| Sudden leader move with executable follower lag | Jump-following trade with priority on the affected instrument | #13 / current LJF |
| Stale data, unresolved orders, uncertain signal or insufficient edge | Withdraw new risk and manage existing inventory | #6 execution controls and #8 regime/lifecycle ideas |

Use #8's regime and exit-latch concepts to select behavior. Use #6's account reconciliation and risk architecture as a reference for execution. Each instrument has one active trading mode; different instruments can operate in different modes simultaneously.

The missing trend mode is a research hypothesis. A strategy can recognize a trend after it has happened and still lose on the next trade. Keep it only if later observations show an improvement over the maker-plus-jump baseline.

## 2. Why the current bot behaves this way

The current maker and jump component cover two narrow opportunity types:

| Current code/default | Consequence |
| --- | --- |
| Maker is optional through `--mm` | Stocks outside that selection receive no quiet-market quotes |
| `--mm-size 2`, `--mm-cap 10`, `--mm-soft 5` | Small inventory and small trading clips |
| Adding size is `max(1, int(mm_size * (1 - abs(position)/mm_cap)))` | With defaults, holding just one lot reduces the adding quote from two lots to one |
| One maker stock visited every `--mm-every 2` cycles | Quote management per stock can be much slower than the overall loop |
| `--jump-ticks 5`, `--window 1`, `--min-remaining 3` | Gradual moves generally do not trigger the jump strategy |
| `--cooldown 15` | A leader cannot trigger continuously; the cooldown starts even if follower entry is rejected |
| Correlation learning still feeds the jump trigger | Learning a link does not create continuous trading between jumps |
| News and public trades are recorded | They currently do not generate a directional forecast for entries |

A stock moving roughly one tick per second for twenty seconds can move substantially without meeting the five-tick, one-second jump threshold. The maker may build inventory against that move. This is the main gap the proposed trend mode addresses.

Increasing order size changes exposure per fill. It does not create counterparties, a predictive signal, or more jump events. Small typical gains also do not establish low risk: held inventory can lose several spreads during a move.

## 3. First fix shared execution and accounting

Complete these changes before raising size or adding another source of orders.

### Cancellation barriers and independent capacity

`mm_cancel()` currently ignores the result of `delete_orders()`, while `maybe_enter()` calculates capacity from position alone. A fake-client probe demonstrated a valid starting position of +90 with 10 outstanding buys, followed by a failed cancellation and another 10-lot buy request. Possible long exposure becomes +110. This was an offline counterexample, not an observed live breach.

For each instrument, maintain a coherent account state containing confirmed position, live orders, actual fills, and unresolved insert/cancel reservations. Reserve an order before submission. Keep uncertain quantities reserved until their outcomes are reconciled; never release them merely because a timeout elapsed.

Use the following hard checks, with a configured account cap no greater than 100:

```text
q = confirmed position
B = all possible outstanding/unresolved buy fills
S = all possible outstanding/unresolved sell fills

additional_buy_room  = max(0, cap - q - B)
additional_sell_room = max(0, cap + q - S)
```

Track each quantity once as it moves from unresolved request to live order or confirmed fill. Existing retained target orders are not additional proposed volume. Reserve new proposals on their corresponding side independently. Never offset buys against sells or positions across instruments.

When changing modes or replacing orders:

1. Request cancellation of conflicting or risk-increasing orders.
2. Wait for a fresh account view that resolves cancellation and any intervening fills.
3. Recompute current inventory, target, price, size and capacity.
4. Submit only if the opportunity still exists and the account state is certain.

If a jump opportunity expires during reconciliation, skip it. Do not compensate by using stale prices or ignoring reservations. Risk exits must also reserve pending reductions and use refreshed held quantity so an exit cannot unintentionally reverse a position.

### A common halt and lifecycle

The current `halted` flag blocks jump entries but does not block maker entries. An offline probe with `halted=True` still submitted two maker limit orders.

Create a shared latched entry halt checked by every mode. On activation, withdraw exposure-increasing quotes and continue only safe reconciliation and reductions. Use actual account/fill data for loss controls; the current jump touch-price PnL estimate is insufficient.

Manage all inventory owned or explicitly adopted by the combined bot. Normal shutdown currently cancels maker orders but does not necessarily flatten maker inventory. Define a wind-down policy using the actual session deadline and end-inventory rules. Report any remaining position explicitly; cancellation is not liquidation.

### One request scheduler

The current maker request counter covers only part of the process. Centralize raw API access so books, fills, positions, orders, feeds, inserts, cancellations and exits share the confirmed request allowance. Reserve capacity for urgent cancellation, reconciliation and exits.

Prioritize unresolved risk and exits, then valid jump opportunities, then maker updates, then nonurgent diagnostics. Include actual API calls made inside higher-level adapter operations. Reduce the selected universe or update frequency if the budget cannot support fresh operation.

### Actual-fill accounting

Poll and deduplicate own fills centrally. Track instrument, order ID, side, quantity, price, mode at submission and timestamps. Build entry VWAP and realized PnL from actual fills, and reconcile the ledger to exchange positions/cash. Unknown cost basis must remain explicitly unknown.

Replace jump `base_pos` ownership assumptions with the combined instrument lifecycle. Maker fills racing a jump handoff must never be misidentified as jump fills. Use a total instrument target that accounts for existing inventory; do not assume every event starts flat. If a target requires reversing direction, confirm reduction through zero before building the opposite position.

## 4. Add the missing gradual-trend signal

Reuse #6's causal feature ideas, feeding them through the same snapshot and controller as the other modes:

- Fast and slow returns measured over elapsed time, initially testing the existing 5-second and 20-second windows.
- Local volatility and movement relative to noise.
- Basket return and the proportion of fresh instruments moving in the same direction.
- Persistent confirmation across distinct observations.
- Source freshness, spread, liquidity and explicit cost checks.

These windows are starting research settings, not event rules or calibrated optimal values. Reset or suspend inference after gaps and shocks. A large old jump must not remain a new trend-entry signal after its opportunity has passed.

Start with transparent rules: fast and slow direction agree, the basket supports the direction, sufficient instruments have valid observations, and movement is large enough relative to noise. Log every failed gate. Do not loosen warm-up, freshness or cost checks merely because no orders appear.

First use the signal to adjust the existing maker's inventory preference. Trial a separate directional target only after measuring that change. This avoids introducing both a new prediction rule and a large exposure change in the same experiment.

For example:

```text
quiet:        preferred inventory = 0
confirmed up: preferred inventory = small positive target
confirmed down: preferred inventory = small negative target
uncertain:    no new directional exposure
```

Targets are bounded desired inventory, not a new order each loop. Define entry expiry, maximum holding time, signal-reversal exits, adverse-move exits and a reentry delay. Keep exit mode latched until the required position change is confirmed. Partial fills and repeated polling must not reset the holding timer.

## 5. Make the passive quotes respond to the signal

The maker currently adjusts for inventory but has no forecast of the next ordinary move. Introduce a bounded preferred inventory `q_star` and shift quote preference according to the difference between actual and preferred inventory:

```text
quote_center = midpoint - inventory_skew * (position - q_star)
```

Here `inventory_skew` has price-per-lot units. Round bid downward and ask upward to the actual tick grid. Clamp passive bids below the current ask and passive asks above the current bid. Final execution must revalidate prices against fresh books.

In an uptrend, a positive target makes buying more attractive and selling less eager. A downtrend produces the symmetric behavior. Once inventory reaches its target or budget, stop adding in that direction. In quiet conditions, the target returns toward zero and two-sided spread capture resumes.

Treat a separate forecast-price shift as an alternative experiment; do not count the same trend twice through both a large price shift and a large inventory target.

Additional maker changes:

- Require enough expected spread capture to cover costs and an adverse-selection allowance.
- Keep matching quotes and useful partial orders to preserve queue position.
- Withdraw unwanted orders when the cleaned book is missing, stale or crossed; an empty decision must remove old quotes.
- Track actual quote age, time at the best price, and how often quotes become stale before management revisits them.
- Improve inside a wider spread only when the resulting estimated edge remains positive. A one-tick spread cannot be improved passively on that tick grid.
- Give instruments different sizes only when observed fill quality supports the difference.

Useful initial sizing experiments are maker clips 2, 3 and 5 while keeping the same internal cap and risk controls. These are offline experiment values, subject to the confirmed event order-size maximum. Log soft-threshold overshoot and pending exposure: `mm_soft` is currently a stop-adding threshold, not a guaranteed position bound.

Test a smoother taper separately if rounding suppresses size too early. Raise the maker cap only when data shows profitable opportunities are actually blocked by inventory capacity. Each change must be evaluated at comparable portfolio risk.

## 6. Improve the jump component without forcing extra trades

Preserve the current jump thresholds in the initial combined candidate so the effect of the new trend logic can be isolated.

Then improve jump selection and execution:

1. Distinguish a detected event, an eligible follower candidate, a submitted order and a confirmed fill. Count each separately.
2. Separate event deduplication from per-pair entry cooldown. Do not repeatedly trade the same jump, but record when a rejected candidate unnecessarily blocks a later opportunity.
3. Estimate leader-to-follower timing from subsequent follower movement. Simultaneous correlation alone does not establish a useful directed lead/lag relationship.
4. Validate or replace the current `1.5` slope multiplier; it is a heuristic, not an established prediction.
5. Age out poorly performing links and log performance by pair and direction.
6. Compute executable entry VWAP for the intended size using current depth. Estimate a conservative exit price, subtract applicable fees and an uncertainty allowance, and require positive remaining edge.
7. Cap IOC prices and quantities by measured edge, depth, verified order limits and shared capacity. Always reconcile partial fills and delayed outcomes before another order.

When spreads are already embedded in executable entry/exit prices, do not subtract them again. Do not infer free hedging or arbitrage from similar stock behavior.

A smaller-jump or longer-window follower signal can be tested later as a separate candidate. Lowering `jump_ticks` and `min_remaining` together would change both event selection and the cost margin, making results harder to interpret.

## 7. Mode selection and implementation structure

Use this decision priority for each instrument:

1. Unresolved account/order state or invalid data: reconcile and suppress new risk.
2. Global halt, required exit or session wind-down: perform bounded reductions when executable.
3. Valid jump: cancel conflicts, confirm fresh state, revalidate edge, then hand control to jump mode.
4. Shock without a valid jump: pause new exposure and reset stale trend history.
5. Confirmed gradual trend: use the bounded trend target and associated quotes.
6. Quiet market with sufficient edge: two-sided maker.
7. Otherwise: no new exposure.

Use hysteresis and a minimum confirmation period to avoid switching every tick. A jump takes priority over a valid trend, but neither takes priority over risk controls. Avoid having #8 and #6 independently add trend positions; #8 supplies routing/lifecycle ideas and #6 supplies the initial trend rule.

Implement on a separate `codex/` branch with a separate strategy/entry file. Preserve `ljf_bot.py` as the comparison baseline. Suggested new filenames, not existing commands:

```text
optiver_bot/strategies/hybrid_maker.py  # decide(snapshot, config) -> list[DesiredOrder]
optiver_bot/hybrid_controller.py       # histories, features, regime and lifecycle
optiver_bot/hybrid_execution.py        # common reconciliation and reservation boundary
ljf_bot_v3.py                         # configuration, one connection, loop and logs
tests/test_hybrid_maker.py
tests/test_hybrid_execution.py
```

Keep `decide` deterministic and independent of network calls, wall-clock reads, credentials and mutable shared state. Supply history/features and elapsed times in the snapshot. Desired orders represent target remaining orders, not duplicate insertion instructions.

Reuse existing components where their contracts fit. Two incompatibilities require deliberate work: #6's executor currently allows only one desired order per instrument and rejects exposure-increasing IOC entries. A two-sided maker and jump-entry mode therefore cannot use it unchanged. Generalize the desired-order handling while preserving independent capacity reservations and fresh-state barriers around every mutation. Do not simply remove the IOC refusal without equivalent entry checks.

## 8. Measurements that explain the result

Write structured logs under ignored `logs/` or `results/` with the exact commit, full nonsecret configuration and instrument metadata.

| Measurement | What it resolves |
| --- | --- |
| Time in each mode and gate rejection counts | Whether inactivity comes from conditions, missing inputs or restrictive settings |
| Candidates, submissions, rejects and actual fills | Whether an opportunity is detected but cannot be executed |
| Source book age, quote age and loop/per-stock revisit latency | Whether execution responds before the opportunity disappears |
| Requests by operation, budget waits and queue retention | Whether processing capacity or unnecessary replacements limit trading |
| Fill VWAP, fees, realized PnL and marked inventory PnL | Whether reported gains reflect actual trades or inventory valuation |
| Signed midpoint movement after fills at 1, 5 and 10 seconds | Whether quotes are repeatedly filled just before adverse moves |
| Drawdown, time in inventory and worst-case reserved exposure | Whether increased PnL comes with unacceptable risk |
| Opportunity lost at size, target or cap limits | Whether larger size/cap could plausibly help |

For a fill, the signed markout is `direction * (future_mid - fill_price)`, with buys positive direction and sells negative. It is a diagnostic, not realized round-trip profit. Use actual elapsed timestamps and flag unavailable horizons or data gaps.

Attribute fills to the submitting mode, but reconcile total PnL to account cash and inventory. Define how remaining inventory is transferred or marked at mode changes so the same gain cannot be credited twice.

## 9. Evaluation sequence and acceptance criteria

Compare these candidates in order:

| Candidate | Change from prior candidate |
| --- | --- |
| A | Current behavior, inspected offline as a reference |
| B | Common risk, cancellation, halt, request and fill-accounting repairs |
| C | B plus trend-aware maker quotes |
| D | C plus bounded directional trend targets |
| E | Best prior candidate with one maker sizing change |
| F | Best prior candidate with one jump-learning/filter change |

Use B as the baseline for any subsequent controlled live evaluation; do not rerun a known unsafe cancellation path to obtain a comparison. All live observations must come through the one authorized execution process. Other candidates can compute shadow decisions from its snapshots without opening another connection or placing competing orders.

Freeze each candidate before evaluation on later chronological data. Compare net PnL, costs, drawdown, reserved exposure and fill quality across quiet, trending and jump periods, at matched risk and request budgets. If evidence is sparse, keep the result inconclusive rather than declaring the added mode successful.

The current replay fully fills eligible IOCs at touch and never fills maker limits. It cannot establish hybrid profitability. Quote-only recordings cannot establish queue position or passive fills. Either collect actual-fill evidence or clearly label a replay's queue, latency, partial-fill, fee and depth assumptions and run sensitivity checks. Keep marked ending PnL separate from an estimated or executed liquidation.

Required execution tests include:

- Failed, delayed and partially filled cancellations before maker replacements and jump handoffs.
- The +90 position / 10 outstanding buys / 10 proposed buys counterexample: the new buy must be blocked.
- Independent sell-side and all-mode aggregate reservations, including near both hard boundaries.
- Unknown IOC outcomes remain reserved; partial exits cannot reverse inventory.
- Global halt suppresses new maker, trend and jump exposure while allowing bounded reductions.
- No duplicate target inserts; retained partial orders preserve priority.
- Stale/crossed/missing books remove unwanted quotes.
- Trend reversal, shock reset, expiry and mode handoff do not double-manage inventory.
- All raw requests share the verified allowance, with room reserved for risk operations.

Required behavior tests include a gradual move below the one-second jump threshold, a quiet range, a jump with a lagging follower, a jump with an already-moved follower, an oscillating high-volatility path, and a data gap. These establish expected decisions and invariants, not profit.

After strategy or risk changes, run:

```text
python -m unittest discover -s tests -v
python -m optiver_bot.demo
python market_maker_e2e.py test
```

At the reviewed commit, earlier checks found 21 e2e tests passing and 53 #6-related tests passing. Full discovery ran 86 tests with 10 errors because removed #1/#2 files are still referenced by #5/#8 and tests. Resolve those integration dependencies or migrate the affected contracts and tests deliberately; do not hide the errors by deleting checks.

## 10. Delivery requirements

Deliver the separate hybrid strategy and runner, focused execution tests, an offline demo, a description of the mode/position lifecycle, and an experiment report that separates verified observations from assumptions. Clearly label any new configuration flags and their defaults; names proposed in this document are not currently available CLI options.

Before exchange integration, read the actual unlocked challenge brief and the cloud `/home/workspace/AGENTS.md` and `/home/workspace/api_reference.md`. The local preparation brief still lacks confirmed fees, request/order limits, scoring and end-inventory treatment. Use `/opt/venv/bin/python` and the supplied `optibook` client; `Exchange()` picks up credentials automatically. Keep supplied files intact, never print or commit credentials, and run continuous bots as `.py` scripts.

Keep reports, logs, data and challenge-specific materials in ignored locations. Keep `AGENTS.md` and `CLAUDE.md` identical if either is edited. The practical completion condition is a reconciled, tested candidate with measured incremental performance; broader market coverage alone is not evidence of higher profit.
