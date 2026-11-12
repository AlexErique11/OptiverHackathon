# Strategy #2 (Razvan): persistent queue pressure

Module: optiver_bot/strategies/market_maker #2.py. Experimental; no verified
profitability or challenge-specific calibration. Supersedes spread_gate as our
preferred candidate for evaluation; the earlier spread_gate module remains in
the separate strategy/alternative development checkout, not this main-based commit.

## One hypothesis

Persistent top-of-book quantity imbalance predicts the next short-horizon price
direction well enough to improve passive order selection. Research motivation:
Gould and Bonart, Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit
Order Book, https://arxiv.org/abs/1512.03492. Their Nasdaq results motivate the
signal; they do not validate this implementation or this simulated market.
The weighted-mid formula below is a heuristic, not their fitted logistic model.

For bid quantity B and ask quantity A, I=(B-A)/(B+A). Both the current and previous
observation must have same-sign |I| >= 0.6. Require 50-500 ms between observations,
current age <= 250 ms, at least 10 combined displayed units, and spread <= 3 ticks.
Do not buy into a falling midpoint or sell into a rising midpoint between samples.
These defaults are hypotheses, not event rules or optimized parameters.

Weighted-mid proxy M=(ask*B + bid*A)/(B+A) estimates a reservation reference.
Buy price = floor_to_tick(min(bid+tick, ask-tick, M-edge)); symmetric sell price
= ceil_to_tick(max(ask-tick, bid+tick, M+edge)). Edge = explicit estimated round-trip
cost per unit + minimum edge buffer, in price units. Default buffer is 0.25 ticks.
Require buy >= existing bid, sell <= existing ask; otherwise abstain. Quotes never
cross. A bounded weighted-mid cannot justify crossing the ask/bid with positive
costs: this implementation intentionally does not submit aggressive IOC orders.

At most one order is returned, in the signal direction. Target magnitude is
floor(position_limit * max_target_fraction * min(|I_now|,|I_previous|)); default
max_target_fraction=0.5. Cap the order at order_size, independently calculated
hard capacity, and distance to target including pending orders on that side.
Other-side pending orders cannot free capacity. Short selling from zero is allowed.
Stale/invalid/unconfirmed signals yield []; runner must withdraw previous quotes.
Existing invalid inventory raises ValueError. A neutral signal does not liquidate.

## The supplied stock table

Assuming the outer columns are best-level bid/ask quantities, quantity imbalances
are approximately AMD +0.993, AVGO +0.263, CRWV +0.405, MU -0.002,
NBIS +0.988, NVDA +0.655, SNDK -0.937. These are snapshot descriptions, not
predictions. If repeated and all gates pass, the candidate considers bids for
AMD/NBIS/NVDA and an ask for SNDK. AVGO/CRWV/MU abstain. Missing VRT data abstains.
Tick=0.1 in the fixture is an explicit assumption; obtain actual ticks at runtime.
Neither ticker identity nor sector similarity establishes a tradable relationship.

## Proposed integration schema (shared models unchanged)

Current repo review (commit 35a41a1): the newer standalone market_maker.py has a
different Snapshot (bids/asks sequences), Config (cap/size), and DesiredOrder
(no instrument_id). Its run_iteration accepts decide_fn, but does not currently
pass previous observations, book ages or aggregate outstanding quantities into
that callback. This module is NOT directly interchangeable with that callback.
The integration owner must map those fields and order types explicitly, and
supply historical observations/freshness and independent outstanding quantities.
The standalone runner already removes own volume via clean_book; preserve that
behavior. No edits to market_maker.py were made by the alternative strategy owner.

The existing Snapshot cannot carry depth. This module proposes optional
DepthSnapshot(Snapshot) with bid_volume, ask_volume, and previous:DepthObservation.
DepthObservation contains bid, ask, bid_volume, ask_volume, book_age_seconds.
Both ages are evaluated at the SAME decision time; previous age must be larger.
Samples must be distinct observations, per instrument, and never future data.
Previous depth is paired with previous prices, not current prices. The runner
should use trusted market timestamps when available, and must not call retrieval
age a guarantee that displayed liquidity remains current. Use a monotonic clock
for local observation age. If freshness cannot be established, abstain.

The integration owner must approve/populate the subtype. No shared-model change
has been silently made. Regular Snapshot input safely returns [], never fabricated
sizes. PressureConfig is a StrategyConfig subtype, so runner config loading must
explicitly construct it. Inherited half_spread_ticks/inventory_skew_ticks are unused.
No exchange connection, state mutation, signal cache or credentials in the strategy.

IMPORTANT: Use external queue quantities, excluding this team's own remaining
orders at matching best prices, for BOTH observations. Aggregate outstanding_buy
and outstanding_sell fields are insufficient to subtract by price: the runner
must use its detailed outstanding-order records. If no positive external queue
remains on a side, abstain rather than invent the next best price. Use consistent
snapshots; cancellation requests do not remove orders until confirmed.

```python
import runpy
strategy = runpy.run_path('optiver_bot/strategies/market_maker #2.py')
DepthSnapshot = strategy['DepthSnapshot']
DepthObservation = strategy['DepthObservation']
PressureConfig = strategy['PressureConfig']
decide = strategy['decide']
# Synthetic illustration, not verified live costs or event sizes:
previous = DepthObservation(161.2, 161.5, 3010, 10, 0.10)
snapshot = DepthSnapshot('AMD', 161.2, 161.5, 0.1,
                         bid_volume=3010, ask_volume=10, previous=previous)
config = PressureConfig(order_size=5, round_trip_cost_ticks=0.0)
orders = decide(snapshot, config)
```

Cost defaults to None, disabling orders. Operator must supply actual conservative
round-trip costs including expected exit costs; zero above is solely a synthetic
test assumption. The edge uses a proxy, not a guaranteed exit price. A safety
buffer cannot cap losses. Do not alter live selection without operator review.

Runner owns matching desired quotes to outstanding orders, fresh independent
per-instrument limits, actual exchange order/message limits, rejects, partial fills,
cancel/fill races and timeout/unwind behavior. No active bot or second exchange
connection was started. Selection and collection of the depth fields remain
integration requirements. Rate guidance is not a verified request allowance.

## Evaluation and failure modes

Spoofed/cancelled queues, stale data, own-order contamination and informed flow
can reverse the apparent signal. Two samples reduce transient observations but
do not prove genuine demand or protect against spoofing. Large queues also mean
slow passive fills: signal accuracy does not imply profitable execution. Stocks
with different liquidity require separate calibration. No news or cross-product
relationships are inferred. The runner needs an explicit timeout/end-of-session
unwind policy because held inventory persists after the signal vanishes.

First test imbalance versus subsequent midpoint changes using chronological data.
Tune threshold/holding horizon/cost buffers only on early data; freeze and evaluate
on later holdout data by instrument. Compare baseline, spread_gate and this
candidate under identical costs, latency, queue assumptions and risk caps.
Include all observations, not just fills; measure prediction calibration, signal
coverage, fill rates, post-fill markouts, PnL net of costs, drawdown, inventory,
request counts, rejects and unwind costs. Separate spread capture from directional
mark-to-market gains. Test inverted pressure and no-signal controls. Stress queue
disappearance and latency. Book touching does not establish a passive fill.
No PnL or predictive performance has been measured yet. Prefer the baseline if
holdout evidence does not support switching. Keep data/logs/results Git-ignored.

Validation: python -m unittest discover -s tests -v; python -m optiver_bot.demo.
Tests include the provided stock values as synthetic repeated observations, not
actual observed persistence. They verify logic and limits, not market performance.
