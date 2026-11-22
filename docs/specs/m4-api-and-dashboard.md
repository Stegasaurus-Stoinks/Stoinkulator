# Milestone 4 spec: API and dashboard

Status: approved by the owner on 2026-11-22. See "Decisions" at the end.

## Goal

Milestone 2 stores every run in SQLite and can print it on the command line.
Milestone 4 puts that in a browser: a small read-only web API over the
database and a dashboard that shows runs, account and per-algo performance,
trades and charts.

It is built on stored backtest runs, because that is what exists today. Paper
and live runs will go into the same database with the same tables, so the
dashboard shows them without redesign once the live side exists.

Done means:

1. `python -m MainStoinker serve --db SPEC` starts a local web server that
   never writes to the database and never touches a broker.
2. The API returns runs, statistics, trades, orders, equity curves and price
   bars as JSON, with the same numbers as `report --json`.
3. The dashboard has four views: run list, run overview, algo detail with a
   price chart, the algo's own indicators and trade markers, and a comparison
   of two runs.
4. Each run stores the indicator series its algos drew, so the chart shows why
   a trade was taken.
5. The hardcoded front-end address in `Globals.py` is gone (issue 20).

## What stays as it is

- The engine's trading behaviour, the brokers, the ledger and every golden
  file. The store gains two tables (schema version 2, see "Indicator
  series"); the existing tables do not change.
- The live program, `apiApi.py`, `trade.py`, and the old socket.io client
  (`Util/SocketIO_Client.py`). The old client is not extended; it is removed
  with the legacy runtime in milestone 1 Phase B.
- The algos.

## Design

### Package layout

- `MainStoinker/api/`: the web API. It imports `MainStoinker.engine` (store,
  stats, report) and nothing legacy. A test enforces this, like the engine's
  import boundary. The engine does not import the API.
- `MainStoinker/api/static/`: the dashboard files, served by the API.
- New dependencies: `fastapi`, `uvicorn`, and `httpx` for tests only. They are
  needed only by `serve`; `backtest`, `runs` and `report` must still work
  without them installed.

### The `serve` command

```
python -m MainStoinker serve --db SPEC [--host 127.0.0.1] [--port 8750]
```

- `--db` is required unless the environment variable `STOINK_DB` is set.
  `backtest`, `runs` and `report` accept `STOINK_DB` the same way, so the
  database location is set once per machine and never committed.
- The database must exist; `serve` does not create one. It is opened
  read-only (`mode=ro`), per request, so a backtest can write while the
  dashboard is open.
- Exit code 2 for a usage error, a missing database or a missing dependency,
  with one line on stderr.

### Safety and privacy

- **Local only.** The default host is `127.0.0.1`. Any other host is refused
  unless `--allow-remote` is given, because there is no login in this
  milestone.
- **Read-only.** Only `GET` routes exist. No route starts a run, changes
  config or reaches a broker. `MainStoinker/api/` must not import `ibapi`,
  `apiApi` or `trade`; the import-boundary test covers it.
- **No machine details.** The API never returns the database path, the
  absolute data directory or the user name. The data directory is shown by its
  last folder name only.
- **No cross-origin access.** No CORS headers; the dashboard is served from
  the same origin.

### API

All routes are under `/api`. Money, prices and quantities are strings with
four decimals, exactly as `report --json` prints them; times are ISO 8601 in
UTC. The statistics come from `engine/stats.py`, so a number has one
definition everywhere.

| Route | Returns |
|---|---|
| `GET /api/health` | API version, schema version, number of runs, runs that never finished |
| `GET /api/runs` | Run summaries, newest first (what `runs --json` prints) |
| `GET /api/runs/{id}` | Run header and statistics per algo and in total (what `report --json` prints) |
| `GET /api/runs/{id}/trades?algo=` | Round trips, in exit order |
| `GET /api/runs/{id}/orders?algo=` | Order events and fills |
| `GET /api/runs/{id}/equity?algo=&max_points=` | Equity series of one algo, or of the account when `algo` is left out |
| `GET /api/runs/{id}/bars?symbol=&start=&end=` | One-minute bars for the price chart |
| `GET /api/runs/{id}/series?algo=&start=&end=` | The algo's indicator series for the price chart |
| `GET /api/compare?runs=1,2` | Statistics of the listed runs side by side, matched by algo `idname` |

- An unknown run, algo or symbol is a 404 with a one-line JSON message. A bad
  parameter is a 422. A database error is a 503 with one line, never a
  traceback.
- `max_points` thins a long equity series by keeping the last point of each
  bucket plus every peak and trough, so the drawdown still reads correctly.
- A run that never finished is reported with status `incomplete`, as `runs`
  does.

### Bars

Bars are not in the database. The bars route reads the CSV files of the run's
stored data directory through the engine's existing bar loader. If the
directory no longer exists, or the file changed since the run, the route says
so and the dashboard shows the trades without candles. The response is limited
to a time window (default: the session of the selected trade or day) so a long
history is never sent in one piece.

### Indicator series

Every algo already keeps its indicators: `AlgoData` is a frame with one row
per bar, `FrontEndDataStruct` names its columns and `FrontEndDataType` says how
each is drawn (`line`, `segment`, `marker-up`, `marker-down`, `baseline`). For
example EMACrossing has two moving-average lines, the opening-range algos have
upper and lower bound, target and stop segments, and Elliot has swing-low and
swing-high markers. What is missing is that nothing keeps this after the run.

- **Capture.** When a run with `--db` ends, the command reads each algo's
  `AlgoData` and stores every non-empty value. The algos are not changed and
  an algo without `AlgoData` simply stores nothing. A new algo gets its
  indicators on the chart by filling the same three attributes.
- **Schema version 2.** `algo_series` (run, algo, name, draw type, order) and
  `algo_series_points` (run, algo, name, bar time, value). Values are stored
  as floating point: they are indicator levels for drawing, not money. The
  migration from version 1 only adds the tables. Runs stored before it have no
  series, and the chart says so.
- **Read.** `RunData` gains the series; the series route returns them for a
  time window, like bars.
- **Draw.** Lines as line series, segments as stepped lines that break where
  the value is empty, markers as arrows on the candles.

Two things the chart must be honest about:

- **Swing markers are drawn with hindsight.** A swing low is only confirmed
  some bars after it happens, and the algo marks it on the bar where it
  happened. The stored frame is the final one, so a marker shows where the
  swing was, not when the algo knew. The chart legend says this.
- **Stop and target lines come from the order events for realistic-fill
  runs.** The algo's own `StopPrice` and `tp` columns are read from the legacy
  `Trade`, which goes stale when the broker closes a position. Entry and exit
  markers always come from the ledger. The `Trade` column is stored but not
  drawn.

Live and paper runs will need the series written as bars arrive; that is part
of incremental writes (issue 70) in Phase B.

### Dashboard

Plain HTML, CSS and JavaScript modules served as static files: no Node, no
build step, no framework. Charts use TradingView Lightweight Charts, copied
into the repo at a pinned version (Apache 2.0; it requires an attribution
link, which goes in the page footer). The old front end's series types (line,
segment, markers, baseline) suggest it used the same library.

Views:

1. **Runs.** Table of runs: start, duration, status, kind, fill model, algos,
   trades, net, code version. Filter by kind and status. Tick two to compare.
2. **Run overview.** Account equity curve with the drawdown under it, the
   run's settings (capital, costs, fill model, bar range), and the per-algo
   table from `report`. Notes for synthetic closes, ignored events and
   cancelled entries.
3. **Algo detail.** Equity curve of one algo, its trade table (sortable, with
   exit reason), and a candle chart with the algo's indicators, stop and
   target lines, and entry and exit markers. Selecting a trade moves the chart
   to it. Each indicator can be switched off in the legend.
4. **Compare.** Two runs side by side: statistics per algo and both equity
   curves on one chart. This is the legacy-versus-realistic view now and the
   paper-versus-live view later.

A header strip shows the health route: database reachable, schema version,
and any incomplete runs. Process health (heartbeat, broker connection) is
milestone 3.

The page loads data when opened and has a refresh button. Live updates are not
part of this phase: runs are written once, at the end (issue 70 covers
incremental writes).

### Front-end address (issue 20)

`Globals.Config.FrontEndPort` becomes a value read from the environment
variable `STOINK_FRONTEND_ADDR` with `localhost:3000` as the default.
`LiveTrading`, `LiveData`, `TWS_HOST` and `TWS_PORT` are not touched.

### Database location

The store keeps refusing OneDrive folders. Its hint changes from
`%LOCALAPPDATA%\Stoinkulator\runs.db` to `%USERPROFILE%\Stoinkulator\runs.db`:
programs installed from the Microsoft Store (the Claude app is one) get a
private copy of `AppData`, so a database created there by one program is
invisible to another. A plain folder in the user profile has no such problem.

## Testing

- API tests use FastAPI's test client against a database built in a temp
  directory by the existing harness. They pin that each route returns the same
  numbers as `report --json` for the paper golden cases.
- Tests for: read-only open (a write through the API's connection fails), the
  host refusal, 404 and 503 paths, no absolute path or user name in any
  response, the import boundary, and `backtest` working with the web
  dependencies absent.
- The JavaScript is kept thin (fetch, format, draw) so the logic that can be
  wrong lives in Python. One test checks that every static file the pages
  reference is served. Each dashboard task is also checked by hand in a
  browser against the two stored runs, with a screenshot in the pull request.

## Corrections from the task split (2026-11-22)

The task divider checked this spec against the code. Where the text above
differs, these apply:

- **Times.** Bar, trade, order, equity and series times are stored as the
  data's wall-clock time with no zone recorded. The API returns them as
  stored, without a zone, and the dashboard labels them "data time". Only a
  run's start and end are UTC.
- **Readers never write.** `runs`, `report` and `serve` open the database
  strictly read-only: no migration, no journal-mode switch. Only `backtest`
  upgrades a database to schema 2, and older code then refuses that file.
- **Series capture.** `AlgoData` covers every bar including warm-up, but its
  `time` column is not reliable. Rows are matched to bars by position, and an
  algo whose frame does not match the bar count is skipped with a warning.
  Any draw type is stored; unknown ones are drawn as a line.
- **Stop and target lines.** Legacy-fill runs have no stop or target orders,
  so they draw the algo's own `StopPrice` and `tp` columns. Realistic runs
  draw the order events and hide those columns by default.
- **Data fingerprints.** Each stored backtest records a sha256 and size per
  data file, so the bars route can say whether a file changed since the run.
  Runs stored earlier are served as "unverified".
- **Bars and series windows.** Default: the trading day of the run's last
  bar. At most 5 trading days or 10,000 bars per request.
- **Equity.** The stored series holds a point only where equity changed; the
  API returns it as a step series. Thinning keeps, per bucket, the last,
  lowest and highest point, plus the first point and the peak and trough of
  the largest drawdown.
- **Errors.** A 503 carries a fixed message, because store errors contain
  the database path.
- **Where `serve` lives.** In `MainStoinker/api/`, reached from
  `MainStoinker/__main__.py`, so the engine never imports the API. The web
  packages are in `requirements-web.txt`, which `requirements-dev.txt`
  includes.
- **Compare.** The route accepts 2 to 5 runs; the view shows 2.
- **Chart library.** Lightweight Charts 5.x, one file from the npm registry,
  committed unchanged with its licence and checksum. Its logo stays on the
  chart.

## Phases

**Phase A, stored runs (this spec).** Tasks D1 to D17, GitHub issues 77 to
93:

- D1. Read-only store access and `STOINK_DB`.
- D2. Store schema version 2: indicator series tables and data fingerprints.
- D3. Capture of indicator series and fingerprints at the end of a stored
  backtest.
- D4. Shared documents for `runs --json` and `report --json`.
- D5. Public bar-file reader, window filter and fingerprint.
- D6. `serve` command, web dependencies, health route, safety refusals.
- D7. Runs and run-detail routes.
- D8. Trades, orders and equity routes, with thinning.
- D9. Bars route.
- D10. Series route.
- D11. Compare route.
- D12. Dashboard shell, chart library and runs view.
- D13. Run overview.
- D14. Algo detail with the candle chart and trade markers.
- D15. Indicator overlays, stop and target lines, legend.
- D16. Compare view.
- D17. Front-end address from the environment (issue 20).

Order: D1, D5, D17 first; then D2, D4, D6; then D3 and the routes D7 to D11
with D12; then D13; then D14 and D16; then D15.

**Phase B, with the live side.** Live updates (server-sent events), paper and
live runs side by side, open positions, the account view from the broker, and
a login if the dashboard is ever reached from another machine.

## Decisions

1. **Dashboard technology: plain static pages for now (owner, 2026-11-22).**
   No build step. A move to React is revisited once the API is built; the API
   does not change either way. If it happens, agents for the React tasks and
   builds are set up first.
2. **Chart library: TradingView Lightweight Charts (owner, 2026-11-22).**
   Copied into the repo, with the attribution link it requires.
3. **Access: this machine only, no login (owner, 2026-11-22).**
4. **Indicator overlays: in Phase A (owner, 2026-11-22).** Without them
   trades appear on the chart with no visible reason. See "Indicator series".
5. **Database location: `%USERPROFILE%\Stoinkulator\runs.db` (owner,
   2026-11-22),** set once through `STOINK_DB`.
6. **Task split D1 to D17: approved (owner, 2026-11-22),** with the
   corrections above and the chart library download.
7. **Dashboard pull requests merge on green CI (owner, 2026-11-22)** once
   screenshots against the stored runs are in the pull request.
8. **D17 does not wait for the owner (owner, 2026-11-22).** The planner
   checks that only the front-end address changes in `Globals.py`.
