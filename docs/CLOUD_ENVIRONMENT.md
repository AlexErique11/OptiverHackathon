# Optiver cloud environment

Verified inside the provided team environment on 12 November 2026:

| Item | Value |
| --- | --- |
| Workspace root | `/home/workspace` |
| Our Git clone | `/home/workspace/your_optiver_workspace/OptiverHackathon` |
| Python executable | `/opt/venv/bin/python` |
| Observed Python version | 3.13.16 |
| Client library | `optibook`, already installed |
| Connection configuration | Supplied automatically to `Exchange()` |
| Provided examples | `/home/workspace/your_optiver_workspace/optibook_guides/code_snippets.ipynb` |
| Provided instructions/reference | `/home/workspace/AGENTS.md`, `/home/workspace/api_reference.md` |

The team-specific URL and injected secrets are intentionally not stored in this repository.

## Open and verify

Keep the supplied workspace files intact. Our repository is a separate subfolder; do not initialize or publish the whole `/home/workspace` directory.

Open `OptiverHackathon.cloud.code-workspace` inside the project to use the provided Python interpreter. VS Code tasks use `/opt/venv/bin/python` on Linux and the local `.venv` interpreter on Windows. If the editor has remembered a previous interpreter, run **Python: Select Interpreter** and choose `/opt/venv/bin/python` explicitly.

From a cloud terminal:

```bash
cd /home/workspace/your_optiver_workspace/OptiverHackathon
git status --short --branch
/opt/venv/bin/python scripts/check_cloud.py
/opt/venv/bin/python -m unittest discover -s tests -v
/opt/venv/bin/python -m optiver_bot.demo
```

These checks do not connect to the exchange or submit orders. Do not create a replacement cloud virtual environment or install a similarly named client package: use the supplied runtime, libraries and automatic credentials.

## Git collaboration

The clone tracks `origin/main`. Local Git settings use fast-forward-only pulls and simple pushes. Pull reviewed changes when the team operator is ready to update the running code:

```bash
git pull --ff-only origin main
```

This shared cloud directory has one current branch for everyone. Use separate clones/worktrees for simultaneous branch work, or develop independently and merge into `main` before updating the operator's clone. Do not switch its branch underneath a teammate.

Cloning and fetching the public repository require no GitHub credentials. Push authentication is separate; this setup does not copy desktop credentials or create cloud tokens. Use your normal authorized GitHub flow if pushing from the cloud is needed. Never put a token into a remote URL or commit credentials.

## Runtime rules

- Short selling is allowed. Hard net position is **-100 to +100 inclusive per instrument**, including possible outstanding/proposed fills on each side independently.
- There is **one connection per team**. A new connection kicks the old client; check `is_connected()` in the eventual runner. Do not connect just to inspect the installation.
- Construct `Exchange()` without arguments to use injected configuration. Do not hardcode or print it.
- The supplied guide says to run bots as `.py` scripts. Use notebooks for exploration and examples, rather than continuous bots.
- Rate throttling applies across the team and instruments. The guide suggests a sleep of at least 0.2 seconds per loop; that is pacing guidance, not a verified numerical requests-per-second allowance.
- Discover tradable instruments and their tick/price constraints at runtime. Inspect responses for rejection; orders can fail or fill partially.
- The installed materials mention historical-data APIs, conditional on the event exposing them. Inspect the local API reference before depending on availability.

## Keep supplied material separate

The supplied `PLEASE_KEEP_CODE_PRIVATE.txt` asks participants not to publish solutions and says private repositories are acceptable. The GitHub repository was public when inspected. The generic setup is configured here; do not push event-provided files, proprietary libraries, credentials, datasets or challenge solutions to a public remote. Settle repository visibility with the team before sharing challenge solutions.

The Optiver root instruction file is preserved. Our project `AGENTS.md` and `CLAUDE.md` remain identical and add our team-specific rules.
