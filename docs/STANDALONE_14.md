# Standalone strategy 14

`optiver_bot/strategies/market_maker_14_standalone.py` includes the momentum
controller, jump-link signal, models, adapter, limiter, risk checks, reconciliation
and runner. It imports no project modules and reads no other strategy files.
Its only external package is the simulator's preinstalled `optibook` client,
imported only when connecting. All other imports are Python standard library.

From the repository root in the Optiver simulator, stop the previous bot and run:

```bash
/opt/venv/bin/python optiver_bot/strategies/market_maker_14_standalone.py run --live
```

If copying the file alone, the path can be replaced with its actual location.
`demo` works offline without Optibook. `run` without `--live` reads market data
and sends no orders, including during cleanup. Only one team client may run.

This retains #14's strategy: confirmed 5/20-second momentum plus provisional
or learned jump links; persistent per-stock targets; clips of five; close before
reversing; history reset after shocks; expiry, stop and session exits. Defaults
are targets of +/-100, all eight configured stocks, and a 600-second session.
Defaults of zero round-trip costs and 25 calls per second remain unverified
assumptions, configurable with `--cost-ticks` and `--max-requests`.

The standalone runtime permits positions at the hard +/-100 boundary and retains
independent outstanding-buy/outstanding-sell reservations, fresh pre-insert
position/order checks, passive-price checks and a final target-distance barrier.
Cancels are followed by fresh state reads. Ctrl+C cancels resting quotes but does
not guarantee closing held inventory; inspect the final position report.

Validation includes running an isolated copy without the repository, causal
signal/controller checks, all legal starting positions, cancellation/fill races,
and complete dry/live startup against an in-memory exchange. Tests do not
establish exchange compatibility or profitability; live orders were not started.
