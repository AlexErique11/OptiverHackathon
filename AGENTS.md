# Project instructions

- Read README.md, docs/TEAM_WORKFLOW.md and the actual challenge brief before changing trading logic.
- Implement strategies through `decide(snapshot, config) -> list[DesiredOrder]`.
- Keep strategy functions deterministic and independent of exchange connections and credentials.
- One execution process owns all orders. Do not introduce a second connector in a strategy.
- HARD POSITION LIMIT: every instrument must stay between -100 (short) and +100 (long), inclusive. Count current position plus all possible outstanding and proposed fills on each side independently. Never offset buy orders against sell orders or positions across instruments. Use a lower internal cap if desired; never configure a cap above 100. Cancel risk-increasing orders before replacing them and confirm fresh positions/order state before sending more.
- Confirm instrument relationships, limits, costs and request budgets from the challenge. Do not treat example numbers as event rules.
- Check current positions plus all possible outstanding fills. Do not assume opposite orders offset or that a cancellation succeeded.
- Do not publish credentials, participant information, provided challenge materials or data. Keep output in ignored directories.
- Work on a separate branch and strategy file. Preserve the working baseline.
- Run `python -m unittest discover -s tests -v` and the offline demo after changing risk or strategy behavior.
- State assumptions and evaluation limitations; synthetic or replay fills are not proof of real performance.
- Keep AGENTS.md and CLAUDE.md identical. Whenever either file changes, apply the same change to the other file in the same commit.
