# Frozen source provenance

The local repository had no commits and no canonical root Python package when evaluation started. It contained separate scaffolding and update directories. To make this branch runnable from a clean checkout, evaluation owns **unmodified offline reference copies**, not shared execution code.

- `reference/optiver_bot/models.py`, `risk.py`, and `demo.py` are copied byte-for-byte from `position_limit_update/optiver_bot/`.
- `reference/optiver_bot/__init__.py` and `strategies/*.py` are copied byte-for-byte from `project_setup/optiver_bot/`.
- `reference_original/optiver_bot/` copies `project_setup` models, risk, package marker and strategies. This is an intentionally unsafe negative control for the independent audit, not a usable trading configuration.
- `reference_manifest.json` records SHA-256 of the frozen reference sources (CRLF normalized to LF for portability) and their original local paths. These are authored scaffolding, not exchange-provided libraries/data. The provided challenge brief, credentials and participant information are not included.

Read during preparation: `project_setup/AGENTS.md`, its README and team workflow; the position-limit update's AGENTS/CLAUDE/README/workflow; cloud-update AGENTS/CLAUDE/README; root challenge preparation brief and strategy playbook. `project_setup/CLAUDE.md` was absent. No root AGENTS/CLAUDE/README existed. No exchange connection was opened.

The challenge document available locally is a preparation brief with remaining event-rule unknowns, not an unlocked final challenge specification. Only the supplied +/-100 hard position rule is treated as confirmed. The originals remain untouched. A canonical checkout can replace the frozen reference at process startup via `EVALUATION_STRATEGY_ROOT`.
