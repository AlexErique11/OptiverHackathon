# OptiverHackathon

Shared Python workspace for a four-person Optibook team. Strategies propose orders; one shared runner owns risk checks and exchange access.

## Hard position limit: 100 long or short per instrument

Every instrument must stay within **-100 to +100 inclusive**. This is a confirmed event rule, not a tunable strategy parameter. Both the strategy configuration and capacity helper reject caps above 100; a lower internal cap is allowed.

Before adding orders, reserve all possible outstanding fills independently:

- `buy_capacity = max(0, cap - current_position - outstanding_buy_volume)`
- `sell_capacity = max(0, cap + current_position - outstanding_sell_volume)`

Include proposed volume in the corresponding capacity check. Opposite orders and positions in other instruments do not offset risk. At +100, stop new buys; at -100, stop new sells. The eventual runner must also cancel unsafe existing orders, handle cancellation/fill races and recheck fresh state before submission.

## Start locally (Windows)

A virtual environment has been created in `.venv` on this computer. No third-party dependencies are needed for the starter code.

```powershell
.\.venv\Scripts\python.exe -m optiver_bot.demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On another computer, create the environment with `python -m venv .venv` first. Python 3.10+ is supported. Open this folder in VS Code and select `.venv` as the interpreter. VS Code tasks are provided for the demo and tests.

## Project layout

```text
optiver_bot/
  models.py                      # Shared inputs and desired-order format
  risk.py                        # Tick rounding and outstanding-order capacity
  strategies/
    inventory_maker.py           # First illustrative strategy
    template.py                  # Copy this when adding a strategy
  demo.py                        # Offline example; sends no orders
config/challenge.example.json    # Fill from the actual challenge brief
docs/TEAM_WORKFLOW.md            # Branches, roles and comparisons
docs/CHALLENGE_CHECKLIST.md       # Unknowns to resolve before exchange use
tests/                          # Risk and strategy checks
data/, logs/, results/          # Local-only outputs (ignored by Git)
```

## Strategy contract

Every strategy exposes `decide(snapshot, config) -> list[DesiredOrder]`. Strategies must not connect to Optibook, send/cancel orders, read secrets or mutate shared state. A desired order describes the target quote; it is not an instruction to insert another identical order every iteration.

`snapshot` includes the book, tick size, current position, outstanding buy/sell volumes and book age. This starter quotes one instrument per call. An eventual execution layer must recheck fresh state, enforce all challenge/portfolio/request limits and reconcile desired orders with existing orders. The capacity helper conservatively reserves existing orders; it does not assume they cancel or fill together.

## Actual Optibook use

Start in the provided cloud environment with `your_optiver_workspace/optibook_guides/code_snippets.ipynb`. The documented API package is `optibook.synchronous_client`; use the supplied notebook for configuration. Do not install an unrelated similarly named package from PyPI.

There is no live-order runner in this starter. The demo uses artificial prices and the confirmed +/-100 position cap. Fill in the remaining challenge rules before writing the exchange adapter; only one process should own the trading username. The documented API allows one connection per trading username.

- [Setup](https://2026-bona-fide.optibook.net/setup-instructions)
- [Client methods](https://2026-bona-fide.optibook.net/docs/synchronous_client)
- [Data types](https://2026-bona-fide.optibook.net/docs/common_types)
- [Order responses](https://2026-bona-fide.optibook.net/docs/exchange_responses)
- [Exercises](https://2026-bona-fide.optibook.net/intro-exercises)

The GitHub repository was public when this starter was created. Keep credentials, participant information, provided challenge files and trading data out of commits. Confirm event sharing rules before publishing solutions.
