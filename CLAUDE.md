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

## Commands

Use the project venv (`.venv`, Python 3.14, Windows).

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pytest                  # fast suite, ~20 s
.venv\Scripts\python.exe -m pytest -m slow          # Elliot golden, ~3 min
.venv\Scripts\ruff.exe check .                      # legacy defect set
.venv\Scripts\ruff.exe check tests --select E,F,W,I,B   # new-code set
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
- `OfflineData/` committed 1-minute bars (AAPL, MSFT, TSLA) used by tests.
- `tests/` pytest suite; `tests/harness.py` runs offline backtests headless.

## Testing conventions

- Golden backtests (`tests/test_golden_backtests.py`) pin the exact trades an
  algo makes on the offline bars. A golden diff means trading behaviour
  changed. Regenerate with `pytest --update-golden` only when the change is
  intended, and explain the diff in the PR.
- Known defects are recorded as `xfail(strict=True)` tests. Fixing one means
  removing its marker in the same change.
- The runtime writes `loggy.log`, `tradey.txt` and `algo.csv` to the current
  directory. Tests run in a temp directory (autouse fixture); keep it that way.
- New behaviour needs a test that fails without the change.

## Code conventions

- Legacy modules use globals and import-time singletons. Do not add new ones.
  New code takes its dependencies as arguments.
- New and rewritten modules must pass `ruff --select E,F,W,I,B` and carry type
  hints on public functions.
- Keep changes scoped to the task. Do not reformat or rename legacy files you
  are not otherwise changing.

## Workflow

Work flows planner → task-divider → implementer → tester → reviewer → human
merge. Agent roles are defined in `.claude/agents/`. One task is one GitHub
issue, one branch (`task/<issue>-<slug>`, cut from `dev`) and one PR into `dev`.
Humans merge; agents never merge or push to `dev` or `master`.
