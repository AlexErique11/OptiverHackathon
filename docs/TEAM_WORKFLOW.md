# Four-person workflow

Suggested roles: two strategy developers, one execution/risk owner and one evaluation/integration owner. Everyone shares the same input/output contract.

1. Pull the latest integration branch before editing.
2. Create a personal branch such as `strategy/alex-inventory`.
3. Copy `optiver_bot/strategies/template.py` to a unique filename. Implement `decide(snapshot, config)` with no exchange access.
4. Include a short description of fair-value assumptions, expected opportunity and failure modes. Use only permitted event information.
5. Test using common data and chronological holdout periods. Record costs, PnL, drawdown, exposure, fills and request counts in ignored `results/`.
6. Compare with the working baseline. Replay of displayed books cannot establish passive fills without queue/latency assumptions.
7. The integration owner reviews and merges a tested change. One person operates the one trading connection.

Desired orders describe target quotes. A future runner must reconcile them with existing orders, avoid duplicate inserts, handle cancellation/fill races, and enforce all risk/request limits. An empty result means remove old desired quotes, including when the book is stale.

Public repository sharing should be settled before pushing challenge solutions. Do not commit credentials, event-provided libraries, notebooks, datasets or participant details.
