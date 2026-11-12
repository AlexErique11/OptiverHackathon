# Strategy comparison at repository commit 4404c54

Reviewed inventory_maker.py and market_maker #1.py, and added market_maker #2.py
under optiver_bot/strategies. No changes to the other strategies or shared runner.

| Aspect | Starter inventory_maker | Strategy #1 | Strategy #2 (Razvan) |
| --- | --- | --- | --- |
| Opportunity | Two-sided spread capture | Two-sided spread capture | Persistent queue-pressure signal |
| Price reference | Midpoint minus inventory skew | Midpoint minus inventory skew | Quantity-weighted midpoint proxy |
| Orders | Usually bid and ask | Usually bid and ask | At most one side, only with confirmed pressure |
| Inventory control | Skew prices and cap volumes | Skew prices and runner clamps | Signal-dependent inventory target and independent pending capacity |
| Depth/history required | No | Depth for cleaned book; no history for signal | Current and previous cleaned best-level sizes and prices |
| Execution included | No | Adapter, runner, reconciliation, flatten CLI | No; intentionally shares the team's runner |
| Default cost gate | None | None in decide | Explicit costs required; None abstains |
| Proven profitable here | No | No | No |

## Reproducible quote comparison

Run `python -m scripts.compare_strategies` from repo root. Inputs are the seven
user-supplied stock rows, flat inventory, tick assumed 0.1, cap 60, size 5, no
outstanding orders. #2 explicitly assumes zero costs and a synthetically repeated
observation 100 ms earlier. These are comparison assumptions, not verified rules.
No order was submitted. No fills, costs actually incurred, PnL, drawdown, latency,
rejections or request counts have been observed. This is NOT a profitability test.

| Instrument | Starter and #1 buy / sell | #2 conditional quote |
| --- | --- | --- |
| AMD | 161.1 / 161.6 | Buy 161.3 |
| AVGO | 206.9 / 207.3 | None |
| CRWV | 136.4 / 136.9 | None |
| MU | 71.6 / 72.1 | None |
| NBIS | 92.9 / 93.3 | Buy 93.1 |
| NVDA | 139.5 / 139.9 | Buy 139.7 |
| SNDK | 59.5 / 59.9 | Sell 59.7 |

Quotes closer to the market can fill more readily, but may earn less spread and
receive worse informed flow. #2 has directional risk and needs an inventory
timeout/unwind policy. #1's inventory skew can help shed positions, but its more
distant default quotes may rarely fill. Neither conclusion establishes net profit.

## Recommendation

Strategy #1 is the more complete implementation to integrate/test immediately:
it already has an execution path and flattening tool. It still contains API
assumptions marked VERIFY and is not demonstrated live-safe or profitable by
offline tests. #2 is the more distinct research candidate for these imbalanced
sample books. Do not claim it is the better trading strategy without chronological
out-of-sample prediction and execution results. Starter and #1 agree at zero
inventory here; their inventory skew parameterizations differ away from zero.

Before #2 can run, the integration owner must map #1's snapshot/config/order types,
provide previous observations and reliable freshness, reserve pending orders
independently, configure actual costs/limits, and define how to exit positions.
The module does not start a second exchange connection or switch the live bot.

Validation: repository unittest discovery passes 24 tests (13 existing, 11 for #2).
The standalone #1 suite passes 36 tests. Its first run hit sandbox temporary-folder
cleanup permissions; rerunning with TEMP/TMP inside this checkout passed, without
changing #1's code. Offline demo and deterministic comparison also run.

No PnL recommendation is supported yet. Evaluate candidates using the same
chronological holdout, exchange limits, fees, latency and conservative queue/fill
assumptions. Report net PnL, drawdown, exposure, fills, post-fill markouts, rejects,
request counts and unwind costs; separate spread capture from directional gains.
