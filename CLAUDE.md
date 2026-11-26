# Stoinkulator

Automated trading system: algos consume 1-minute bars and open/close trades
through Interactive Brokers (TWS, `ibapi`). Backend is Python and stays Python.
The goal is unattended operation with a dashboard showing live, paper and
account performance. Roadmap and task breakdown live in GitHub Issues.

## Safety rules (non-negotiable)

- Never run `MainStoinker/MainStuff/this is how we do it dum dum.py` or
  `Stonks data collect.bat`. Never connect to TWS / IB Gateway except through
  `python -m MainStoinker fetch-bars` or `probe-data` with `--paper --no-prompt`,
  which only work against the paper account (port 7497, account check). Data
  only. No other way of connecting (no script, no `-c` code, no other module,
  no test). Agents may also run the live bar watcher,
  `python -m MainStoinker.ib.watch_bars`, with `--paper --no-prompt --minutes N`
  (N from 1 to 420: a bounded run, data only), under the same rules (main
  session only, clean `origin/revival` checkout, one broker session at a time,
  exit code 5 means not a paper account: stop, tell the owner); any other way of
  running it is owner-run only. Agents may also run the unattended paper session,
  `python -m MainStoinker run`, with `--paper --no-prompt --minutes N` (N from 1 to
  420: a bounded run) and `--algos`, `--bars` and `--db` each given explicitly once,
  under the same rules (main session only, clean unmodified `origin/revival`
  checkout, one broker session at a time, paper only, data from TWS and simulated
  fills only: it places no orders; exit code 5 means not a paper account: stop,
  tell the owner). A first write migrates the runs database, so the database path
  is the owner's: agents use only the path the owner named for the session. Any
  other way of running it is owner-run only. Everything else works only against
  offline data and the test harness.
- The live account, the live ports and `STOINK_ALLOW_LIVE_PORT` are the owner's
  alone: an agent never uses them, sets them or asks for them.
- One broker session at a time, started by the main session, never by parallel
  subagents. Exit code 5 (`fetch-bars`) or 4 (`probe-data`) means TWS is not
  signed into a paper account: stop, do not retry, tell the owner.
- A paper session is run only by the main session, from the main checkout at an
  unmodified `origin/revival` commit (clean `git status` for tracked files, `HEAD`
  equal to or contained in `origin/revival`, and no untracked `.py` files in the
  repo root or under `MainStoinker/`), never from a task branch or worktree
  and never with uncommitted changes: code that connects must have been reviewed
  and merged before it talks to TWS. The hook cannot check this; the rule does.
- Everything rests on the paper-id rule (`D[UFI][A-Z]?[0-9]+`): it was confirmed
  against the owner's paper account in an attended `--paper` session on 2026-11-23,
  and it still rests on "no live Interactive Brokers account id begins with D".
- Order placement on the paper account is NOT allowed yet: it is a separate
  owner decision. Any `--orders` option of `run` (stage 3) is the owner's alone;
  the hook allowance for `run` refuses it in every spelling.
- Tests never connect.
- Never change the defaults of `LiveTrading`, `LiveData`, `TWS_HOST` or
  `TWS_PORT` in `MainStoinker/MainStuff/Globals.py`. `tests/test_safe_defaults.py`
  enforces the trading flag, host and port; do not weaken it.
- Any change that touches order placement (`TradeTools/trade.py`,
  `DataCollection/apiApi.py` order methods, `Util/IBKRHelper.py`, or their
  successors) must be flagged `needs-human-review` in the PR. A reviewer
  agent's approval is not sufficient for these.
- Never commit credentials, account numbers or machine addresses.
- Mechanical guards back these rules. `IBapi.connect` refuses any port
  other than the paper ports 7497 / 4002 unless `STOINK_ALLOW_LIVE_PORT=1`,
  which only a human sets, on the trading machine. Any broker connection
  during a pytest run raises (`tests/conftest.py`). `--paper` makes the command
  itself refuse any port but 7497, any host but `127.0.0.1`, and the live
  opt-in variable in the environment; the connection is built with
  `require_paper=True`, so it is never ready, and nothing is requested, unless
  every account id TWS reports looks like a paper id (`D[UFI][A-Z]?[0-9]+`,
  confirmed against the owner's paper account on 2026-11-23). A `PreToolUse` hook
  (`.claude/settings.json`, `.claude/hooks/block_live_launch.py`) blocks shell
  commands that would run the entry script or the launcher, and every
  `python -m MainStoinker` subcommand except `backtest`, `runs`, `report`,
  `serve` and ONE whole-command shape: `.venv\Scripts\python.exe -P -m
  MainStoinker fetch-bars|probe-data <args>` (or a drive-absolute path ending in
  `.venv\Scripts\python.exe`) with `--paper` and `--no-prompt`, `--port` only 7497,
  `--host` only `127.0.0.1`, no `--help`, and no shell syntax, wrapper, `cd` or
  variable assignment; the live opt-in name may not appear at all. The same
  shape with the module `.venv\Scripts\python.exe -P -m MainStoinker.ib.watch_bars <args>`
  is allowed too, and needs `--minutes` once with 1 to 3 digits besides
  (`--no-prompt` makes the watcher itself require it, 1 to 420). The same shape
  with the subcommand `run` (`.venv\Scripts\python.exe -P -m MainStoinker run <args>`)
  is allowed as well, and needs `--minutes` once with 1 to 3 digits, and `--algos`,
  `--bars` and `--db` each once with an explicit value (no reliance on `STOINK_DB`);
  `--fills` only `realistic`; any token that contains `--order` (also `--orders`,
  abbreviations and `=` forms) and `--help` are refused, and so is any stray word, as
  `run` has no positional argument. The hook does
  not check which code is checked out or which interpreter file it is. It is a
  tripwire against accidents, not a security boundary.
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
.venv\Scripts\python.exe -P -m MainStoinker fetch-bars --symbols "AAPL,MSFT" --start 2024-08-16 --end 2024-08-26 --paper --no-prompt   # DATA only, PAPER account only (TWS on 7497): bars to CSV; one session at a time, main session only
.venv\Scripts\python.exe -P -m MainStoinker probe-data --symbols "AAPL" --paper --no-prompt   # DATA only, PAPER account only: what market data the account gets
.venv\Scripts\python.exe -P -m MainStoinker.ib.watch_bars --symbols "AAPL,MSFT,TSLA" --minutes 30 --paper --no-prompt   # DATA only, PAPER account only: watch live 1-minute bars for N minutes (1-420); one session at a time, main session only
.venv\Scripts\python.exe -P -m MainStoinker run --algos ALGOS.json --bars BARS --db RUNS.db --minutes 415 --paper --no-prompt   # PAPER account only, NO orders: algos on live 1-minute bars with simulated fills, stored as a `paper-live` run; N 1-420 (415 = 09:25 to 16:20); one session at a time, main session only; DB path from the owner
```

Owner forms of the same four (the owner's terminal, prompted: you type the port;
`--paper` means 7497, a live port needs the owner's own opt-in, see
`docs/owner-session-checklist.md`): `fetch-bars --symbols A,B --start D --end D --port P`,
`probe-data --symbols A,B --port P`, `watch_bars --symbols A,B --minutes N --port P`,
`run --algos F --bars D --db F --minutes N --port P` (with `--paper` instead of `--port`
for 7497; every algo needs `"mode": "paper"`, `--fills` is `realistic`, `--stop-file F` ends
the run cleanly). Options and outcomes: `docs/live-data.md`; `run --help` for `run`.

Exit codes (`--help` shows them; the not-paper code is 5 for `fetch-bars`, the
watcher and `run` but 4 for `probe-data`, where 4 is otherwise unused: issue 177):

| code | `fetch-bars` | `probe-data` | `watch_bars` | `run` |
|------|--------------|--------------|--------------|-------|
| 0 | every day ok, no data or done | every step done (delayed data counts) | watched to the end, every symbol streaming | the session ended as planned (end of session at 16:20, `--minutes` elapsed or the stop file): stored `completed` |
| 1 | a day failed or a symbol not fetched | a step failed (incl. NO data) | a symbol stopped or delivered no bars | a symbol stopped: stored `failed` |
| 2 | refused: gate, port guard, arguments, path, `--paper` conditions | same | same | same, and a mode other than paper, legacy fills, an unusable database, another `paper-live` run still running: nothing built or connected |
| 3 | could not connect | same | same | same |
| 4 | stopped early (connection lost, pacing twice, fault): rerun resumes | not a paper account | connection lost or closed, or a fault | lost or closed connection, or an error: stored `failed` |
| 5 | not a paper account | (unused) | not a paper account | not a paper account |
| 6 | (unused) | (unused) | no data received for a symbol in the session: check the account's market data subscription | no data received for a symbol: stored `failed`, `no_data` |
| 7 | (unused) | (unused) | (unused) | the session stopped the run on a data fault (delayed data, refused or late bars, a silent symbol, a step that took too long): stored `failed` |
| 130 | Ctrl-C | same | same | same (the run was ended in the store first) |

Where data lands (the owner's folders, outside the repo and OneDrive; a command
refuses a path inside either): `%USERPROFILE%\Stoinkulator\bars` (`fetch-bars`),
`...\live` (watcher and `run`), `...\recordings` (session recordings, `run` too:
`run-<date>-<n>.jsonl`, on by default), `...\run-out` (the algos' working folder of `run`),
and the run database named by `run --db` (the owner's file; a first write migrates it).
Bars are month files
`DIR/SYMBOL/SYMBOL_YYYY-MM.csv` (naive New York times, regular hours) with
`manifest.json`, `gaps.csv` and `gaps_summary.txt` beside them; live files are
`DIR/SYMBOL/SYMBOL_live_YYYY-MM-DD.csv`. Agents write there only through a
command's own defaults, in a session the owner asked for; tests never (the
command test files enforce it with `tests/home_guard.py`).

Seen in real sessions (2026-11-23/24, owner's TWS):
- Keep-up-to-date bars update about every 5 s; a bar is known closed 4 to 5 s after its minute.
- Same-day history is not final until the next morning (the opening minute grew from 127k to
  1.03M shares), so `fetch-bars` stores a day fetched before 06:00 New York next day as provisional
  and a later run refetches it (`history.DAY_FINAL_AT`).
- Volumes are shares only with the TWS "US stock sizes in lots" API setting unticked.
- The paper account got market data only once the live account's data was shared with it
  (an observation of the owner's sessions of 2026-11-24, not checked in code).

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
  (enforced by `tests/test_engine_import_boundary.py`). Live modules: `live_feed.py` aligns the
  symbols' closed bars into one batch per minute; `live_store.py` the incremental writer of a live
  run; `live_counts.py` live run kinds, heartbeat and count keys; `warmup.py` warm-up frames from
  the month files.
- `MainStoinker/api/` read-only web API (FastAPI, GET only) and static dashboard, started by `serve`;
  may import `MainStoinker.engine` but not `engine.algos`/`cli`/`modes` or anything legacy,
  and the pure `MainStoinker.indicators` (the FVG zones of the run page; enforced by
  `tests/test_api_boundary.py`); the engine never imports it.
- `MainStoinker/ib/` the only new-code package that imports `ibapi` (apart from three frozen legacy
  files; the list only shrinks); imports only `ib`, `engine` and `Util.broker_guard`, no socket
  modules; the engine and the API never import it (enforced by `tests/test_ib_import_boundary.py`).
  The client is data-only: an allowlist of ten outgoing message ids, so it can never send an order.
  `client.py` that client; `connection.py` open, readiness, retry, paper check, close; `market_data.py`
  history, quotes, server time, contract lookup; `bars.py` IB bar to engine `Bar`; `history.py`
  day planning, pacing, manifest, gap report; `fetch.py` the fetch driver; `recorder.py` session
  recording with account/path redaction; `cli.py` `fetch-bars` and `probe-data` (gate, paper mode,
  paths); `probe.py` the probe steps; `watch_bars.py` the live bar watcher; `live_run.py` the
  live paper session driver (watcher, aligner, engine and run writer; no orders); `run_cli.py` the
  `run` command (paper only, refusals, gate); `contracts.py`, `events.py`.
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
  of its thirteen cases are slow: add `-m "slow or not slow"` to regenerate all) and
  `test_paper_goldens.py` writes `tests/golden_paper/`; nothing else writes goldens.
- Realistic-fill goldens live in `tests/golden_paper/`. They pin the ledger's
  round trips, a summary and an equity hash for the same thirteen cases through
  `PaperBroker`. Only `tests/test_paper_goldens.py` writes them, with
  `--update-golden` (add `-m "slow or not slow"` to regenerate all thirteen). It
  also checks after every bar that each algo and the broker agree on whether a
  trade is open.
- Known defects are recorded as `xfail(strict=True)` tests. Fixing one means
  removing its marker in the same change.
- The runtime writes `loggy.log`, `tradey.txt` and `algo.csv` to the current
  directory. Tests run in a temp directory (autouse fixture); keep it that way.
- New behaviour needs a test that fails without the change.
- IB code is tested only against `tests/fake_ib.py` (`FakeIbClient`, a scripted stand-in for
  `EClient` with no socket and no order method) and, through `tests/ib_replay.py`, against
  recordings under `tests/recordings/`. A pytest run marks its environment so a child process
  cannot open a real client either; no test runs a connecting command in a subprocess.
- `tests/home_guard.py` fails a test that writes into the real `%USERPROFILE%\Stoinkulator`; only
  `test_ib_cli.py`, `test_ib_cli_paper.py`, `test_ib_probe.py` and `test_ib_watch_bars.py` use it
  (plus `test_home_guard.py`), and those tests point `USERPROFILE`/`HOME` at `tmp_path`. Nothing real is committed under
  `tests/recordings/` before the owner has read it (`docs/live-data.md`, "The recorder").
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
