# Stoinkulator

Automated trading system: algos consume 1-minute bars and open/close trades
through Interactive Brokers (TWS, `ibapi`). Backend is Python and stays Python.
The goal is unattended operation with a dashboard showing live, paper and
account performance. Roadmap and task breakdown live in GitHub Issues.

## Safety rules (non-negotiable)

- Never connect to TWS / IB Gateway, and never run
  `MainStoinker/MainStuff/this is how we do it dum dum.py` or
  `Stonks data collect.bat`. Agents work only against offline data and the
  test harness.
- Never change the defaults of `LiveTrading`, `LiveData`, `TWS_HOST` or
  `TWS_PORT` in `MainStoinker/MainStuff/Globals.py`. `tests/test_safe_defaults.py`
  enforces the trading flag, host and port; do not weaken it.
- Any change that touches order placement (`TradeTools/trade.py`,
  `DataCollection/apiApi.py` order methods, `Util/IBKRHelper.py`, or their
  successors) must be flagged `needs-human-review` in the PR. A reviewer
  agent's approval is not sufficient for these.
- Never commit credentials, account numbers or machine addresses.
- Three mechanical guards back these rules. `IBapi.connect` refuses any port
  other than the paper ports 7497 / 4002 unless `STOINK_ALLOW_LIVE_PORT=1`,
  which only a human sets, on the trading machine. Any broker connection
  during a pytest run raises (`tests/conftest.py`). A `PreToolUse` hook
  (`.claude/settings.json`, `.claude/hooks/block_live_launch.py`) blocks shell
  commands that would run the entry script or the launcher.
- Never set that variable, and never weaken, disable, bypass or work around
  any of the guards or their tests. Changing them is only allowed as an
  approved task with `needs-human-review`.

## Commands

Use the project venv (`.venv`, Python 3.14, Windows). It is uv-managed and has no pip module, so install with `uv pip`.

```
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
uv pip install --python .venv\Scripts\python.exe -e .
.venv\Scripts\python.exe -m pytest                  # fast suite, parallel (pytest-xdist `-n auto`, `--dist loadfile`): ~100-160 s on 16 cores, ~230 s on 4
.venv\Scripts\python.exe -m pytest -n 0             # fast suite in one process (debugging, a clean traceback): ~9-12 min
.venv\Scripts\python.exe -m pytest -m slow          # slow suite, also parallel: full Elliot state digests, legacy-path parity, the four slow golden cases; ~2-2.5 min (serial `-n 0`: ~15 min)
.venv\Scripts\ruff.exe check .                      # legacy defect set
.venv\Scripts\ruff.exe check tests conftest.py MainStoinker/engine MainStoinker/api MainStoinker/ib MainStoinker/__main__.py --select E,F,W,I,B   # new-code set
.venv\Scripts\python.exe -m MainStoinker backtest --algos ALGOS.json --data OfflineData --out OUTDIR   # offline backtest -> OUTDIR/trades.json
.venv\Scripts\python.exe -m MainStoinker runs --db DB [--json]              # list the runs stored by `backtest --db` (read-only)
.venv\Scripts\python.exe -m MainStoinker report RUN --db DB [--json]        # per-algo and total statistics of one stored run
.venv\Scripts\python.exe -m MainStoinker serve --db DB [--port 8750] [--allow-remote]   # read-only API + dashboard on 127.0.0.1 (needs requirements-web.txt)
```

## Layout

- `MainStoinker/MainStuff/` entry script, `Globals.py` (frozen `Config` plus
  mutable runtime state), `main_utils.py` (config parsing, backtest bar loop).
- `MainStoinker/DataCollection/apiApi.py` IBKR wrapper, module-level `ibapi`
  instance; also loads offline bars.
- `MainStoinker/TradeTools/` `Ticker` (bars + registered algos), `Trade`
  (orders, stop, take-profit).
- `MainStoinker/Algos/<Name>/<Name>_Algo.py` each exposes `class Algo(ParentAlgo)`;
  loaded by name from `MainStuff/Algo_config.json`.
- `MainStoinker/engine/` new engine package: `Broker` protocol, `SimBroker`; imports nothing legacy
  (enforced by `tests/test_engine_import_boundary.py`).
- `MainStoinker/api/` read-only web API (FastAPI, GET only) and static dashboard, started by `serve`;
  may import `MainStoinker.engine` but not `engine.algos`/`cli`/`modes` or anything legacy
  (enforced by `tests/test_api_boundary.py`); the engine never imports it.
- `OfflineData/` committed 1-minute bars (AAPL, MSFT, TSLA) used by tests. Data time is naive
  `America/New_York` wall-clock time (session 09:30 to 16:00); algos' hard-coded times use it too.
- `tests/` pytest suite; `tests/harness.py` runs offline backtests headless.

## Testing conventions

- Golden backtests (`tests/test_golden_backtests.py`) pin the exact trades an
  algo makes on the offline bars. A golden diff means trading behaviour
  changed. Regenerate with `pytest --update-golden` only when the change is
  intended, and explain the diff in the PR.
- Golden backtests run through the engine (`harness.run_offline_backtest`:
  `Engine` + `CsvBarFeed` + `SimBroker`). The legacy runtime path
  (`harness.run_legacy_offline_backtest`) is compared against the same golden
  files read-only in the slow suite (`tests/test_legacy_path_parity.py`) and
  goes away in Phase B. `test_golden_backtests.py` writes `tests/golden/` (four
  of its eight cases are slow: add `-m "slow or not slow"` to regenerate all) and
  `test_paper_goldens.py` writes `tests/golden_paper/`; nothing else writes goldens.
- Realistic-fill goldens live in `tests/golden_paper/`. They pin the ledger's
  round trips, a summary and an equity hash for the same eight cases through
  `PaperBroker`. Only `tests/test_paper_goldens.py` writes them, with
  `--update-golden` (add `-m "slow or not slow"` to regenerate all eight). It
  also checks after every bar that each algo and the broker agree on whether a
  trade is open.
- Known defects are recorded as `xfail(strict=True)` tests. Fixing one means
  removing its marker in the same change.
- The runtime writes `loggy.log`, `tradey.txt` and `algo.csv` to the current
  directory. Tests run in a temp directory (autouse fixture); keep it that way.
- New behaviour needs a test that fails without the change.
- The suite runs in parallel (pytest-xdist). Nothing may use a fixed path or a
  fixed port or depend on what else is running on the machine (the dashboard
  listens on 8750: use port 0 or stub the check). Expensive runs shared by
  several tests are built once per session, across all workers, by
  `tests/run_support.py` (`full_store_run` for tests that compare with goldens,
  `small_store_run` for tests that only need a stored run, `full_run`); they are
  read-only, so copy before changing. Use a short slice of the data
  (`rows=`) unless the data is the point of the test, and mark a test
  `slow` only if the fast suite covers the same behaviour another way.

## Code conventions

- Legacy modules use globals and import-time singletons. Do not add new ones.
  New code takes its dependencies as arguments.
- New and rewritten modules must pass `ruff --select E,F,W,I,B` and carry type
  hints on public functions.
- Algos must treat the price frame passed to `update` as read-only; copy it
  (`df.copy()`) before modifying it.
- Keep changes scoped to the task. Do not reformat or rename legacy files you
  are not otherwise changing.

## Workflow

Work flows planner → task-divider → implementer → tester → reviewer → human
merge. Agent roles are defined in `.claude/agents/`. One task is one GitHub
issue, one branch (`task/<issue>-<slug>`, cut from `revival`) and one PR into
`revival`. Humans merge; agents never merge or push to `revival`.

`revival` is the integration branch for all of this work. `dev` and `master`
are frozen: never push to them, merge into them, rebase onto newer versions of
them, or open a PR against them. Only the repo owner decides when `revival`
goes back into `dev`.
