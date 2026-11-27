# The ib package: live data, commands and guards

Reference for `MainStoinker/ib/` as merged on `revival`. It describes what the code does now. The
design and the stage plan are in `docs/specs/live-side.md`; the owner's step-by-step session is in
`docs/owner-session-checklist.md`; the short rules for agents are in `CLAUDE.md`. Everything here
is data only: nothing in this package can place an order.

## Architecture

`MainStoinker/ib/` is the only new-code package that imports `ibapi`. Three legacy files still
import it (a frozen allowlist in `tests/test_ib_import_boundary.py`, which can only shrink). The
package imports from the rest of `MainStoinker` only `ib`, `engine` and `Util.broker_guard`, and no
network module (`socket`, `ssl`, `asyncio`, `http`): every connection goes through
`ibapi.connection.Connection`, which the pytest block patches. The engine and the API never import
`ibapi` or `MainStoinker.ib`. `python -m MainStoinker` imports the package lazily, so the offline
commands (`backtest`, `runs`, `report`, `serve`) never load it.

The layers, bottom to top:

- `client.py` `DataOnlyClient`, an `EClient` plus `EWrapper`. An outgoing message is refused unless
  its id is in `ALLOWED_MESSAGE_IDS` (ten ids: market data and its cancel, contract data,
  historical data and its cancel, current time, market data type, start API, head timestamp and its
  cancel). Orders, order cancels, FA, auto-open-orders, display groups and log-level messages are
  outside the list, and so is any id ibapi adds later. The check runs twice: on the method
  (`sendMsg`, `sendMsgProtoBuf`) and on the raw frame at the connection wrapper. Callbacks only turn
  their arguments into events (`events.py`) and queue them.
- `connection.py` `IbConnection`: open with a bounded handshake (ibapi's `connect` has no timeout),
  one fresh client per attempt, readiness only on `nextValidId`, retry on `OSError`-class failures
  only, clean close, state events (1100 lost, 1101 restored with data lost, 1102 restored). Its
  `request` takes only data requests. Optional `require_paper=True` (below).
- `market_data.py` `IbMarketData`: resolve a stock, request and cancel 1-minute history, set the
  market data type, request and cancel quotes, ask the server time. Every wait ends with one
  outcome: end, error, timeout, closed, lost (1100 in force at the deadline) or data_lost (1101).
- `bars.py` one IB bar to an engine `Bar`: epoch seconds (`formatDate=2`) to naive
  `America/New_York` time, fixed in code, never the machine's zone. Bars stamp the START of their
  minute. Bad prices, volumes or times are refused (`BadBar`), never repaired.
- `history.py` pure planning: which days to fetch, the pacer (10 s between history requests, at
  most 60 per 10 minutes, fewer than 6 per contract per 2 s, no identical request within 15 s), the
  resume manifest, the gap report. The market calendar (`engine/market_calendar.py`) decides the
  days: weekends and holidays are not planned (and not requested), a half day (13:00 close) is
  expected to hold 210 bars (`complete` with them). A day is still planned only once 16:00 New York
  of that day has passed, also a half day (a request ends at 16:00, never in the future). Where the calendar and the
  data disagree the data is kept and the gap report says so (see "Gap files").
- `fetch.py` the driver behind `fetch-bars`; `probe.py` the steps of `probe-data`; `watch_bars.py`
  the live bar watcher; `cli.py` the argument parsing, path checks, console gate, paper mode,
  scrubbed printer and the shared session runner of `fetch-bars` and `probe-data`; `run_cli.py` the
  `run` command and `live_run.py` its session driver (see "`run`" below).
- `recorder.py` the session recorder (below); `contracts.py` plain US stock contracts (STK, SMART,
  USD); `events.py` the event types.

Client ids: 21 for `fetch-bars` and `probe-data`, 22 for the watcher, 20 for `run`. 0 and 123 are always refused
(0 would take over orders placed by hand in TWS; 123 is the legacy code's id).

## The commands

The first three are for a data session with TWS or IB Gateway running. The owner runs them in a real
terminal: the command prints what it will do and the owner types the port number to confirm
(console gate). Agents run only the `--paper --no-prompt` form (see "Paper mode").

Common options of `fetch-bars` and `probe-data` (the watcher has its own list): `--port`
(required, except with `--paper`, which means 7497), `--host` (default `127.0.0.1`), `--client-id`,
`--paper`, `--no-prompt` (only with `--paper`), `--record FILE` or `--no-record`,
`--connect-timeout` (default 30 s, at most 300).

### `fetch-bars`

    python -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA --start 2024-08-16 --end 2024-08-26 --port 7497

Downloads 1-minute regular-hours bars (09:30 to 16:00 New York, 390 bars a full day, stamped 09:30
to 15:59), one request ("1 D", `useRTH=1`) per symbol per trading day, into one CSV file per symbol
per month. Options: `--symbols` (comma or space separated, at most 20), `--start` and `--end`
(`YYYY-MM-DD`), `--out DIR` (default `%USERPROFILE%\Stoinkulator\bars`). There is no settle option: IB's history
of a day is final from 06:00 New York on the next calendar day (`history.DAY_FINAL_AT`). A day
fetched earlier is stored as usual but is provisional; a later run whose server time is past that
moment fetches it again and replaces its rows (reporting how many minutes changed; a refetch with
fewer bars keeps the stored day, a failed refetch keeps it and tries again). A day with no bars
counts as `no_data` (a day IB has nothing for, such as one the calendar does not know is closed) only
once that moment has passed; sooner it is `failed` ("no data yet") and asked again. Holidays and
weekends are never asked for. The summary shows a provisional count and the days, and the gap files carry
a `provisional` status for such days.

Order of events: arguments, paths, dates, port guard, folder layout, the console gate; connect;
IB's server time (the run's clock: the machine's clock is never used for the plan); the closed
trading days to fetch (days not closed yet are listed, never fetched) with an estimate of about 10 s
per symbol-day; wait for the historical data farm; per symbol, resolve the contract, load the
manifest, fetch each pending day through one pacer; gap report; summary.

Outcomes of one day: bars of the day are `ok` (fewer than 390 is listed as short, not a failure);
an empty complete answer for a closed day is `no_data`; a timeout, a bad bar or any other IB error
is `failed` with IB's text and is asked for again next run. A pacing violation (IB error 162 with
the pacing text) is waited out and retried once; a second one stops the run. Connection loss stops
the run. A damaged month file stops that symbol only and is never touched. Everything finished is on
disk and in the manifest, so running the same command again resumes; a day that is already `ok` is
skipped ("0 requests" when everything is done).

The summary prints, per symbol, counts of ok, no data, failed, already done and not closed, the
first fetched day (first and last bar time, bar count, first-bar volume, day total volume) so the
owner can check it against a chart, the volume note, and the gap counts.

### `probe-data`

    python -m MainStoinker probe-data --symbols AAPL,MSFT --port 7497

Finds out what market data the account gets. Options: `--symbols` (comma separated, at most 5,
default AAPL), `--quote-seconds` (1 to 120, default 20), `--no-history`. Steps, each ending OK or
FAILED: 1 connect; 2 data farms; 3 server time (UTC and New York, and the difference to this
machine's clock); 4 contract details; 5 per symbol, quotes with market data type 1 (live) and, if
no price tick comes within 8 s, type 3 (delayed), giving the verdict LIVE data, DELAYED data
(15 min) or NO data (frozen variants exist); 6 one day of 1-minute bars of the latest closed
session with the day's total and median per-minute volume (a first bar under 100,000 shares prints
a hint that the lots setting is ticked); 7 a summary to paste back, with the IB codes seen. Delayed
data is a success; NO data fails the step (exit 1) and prints advice about subscriptions and code
10197 (another session using the data).

### `python -m MainStoinker.ib.watch_bars`

    python -m MainStoinker.ib.watch_bars --paper --symbols AAPL,MSFT,TSLA --minutes 30

Watches IB's live-updating 1-minute bars and saves the closed ones. Options: `--symbols` (at most 5,
default AAPL), `--minutes` (1 to 420, default 30, required with `--no-prompt`),
`--backfill-minutes` (1 to 120, default 10), `--out DIR` (default `%USERPROFILE%\Stoinkulator\live`),
`--record FILE` / `--no-record` (a long watch writes a large recording and may slow reading: use
`--no-record` for a long one), `--quiet` (no forming-bar line), `--port`, `--host`, `--client-id`,
`--paper`, `--no-prompt`, `--connect-timeout`. It is a module, not a `MainStoinker` subcommand.

How it works: one keep-up-to-date history request per symbol (empty end time): IB sends the
backfill, the end marker, then updates carrying the CURRENT state of the bar still forming. A bar is
closed only when an update with a later minute arrives (or, with no later update, 90 s after its
minute ended on the stream's clock); only closed bars are written, to
`DIR/SYMBOL/SYMBOL_live_YYYY-MM-DD.csv` (same columns and formats as the month files; a second run
the same day appends only later bars). A row is a finished bar with final values: when that cannot
be known (seen only in the backfill snapshot, spanned a connection loss or a data farm break, last
update too long before the minute ended) no row is written and one line says why. A later revision
of a written minute is reported, never rewritten. A stream whose bars lag IB's clock by over 120 s
is labelled DELAYED. Each printed bar shows how long after its minute it first appeared and was
closed. A symbol with no bar of today's session and no live update for 120 s of the regular session
(09:30 to 16:00 New York, weekday, outage time excluded) after its backfill ended is `no data
received`: the watcher says so and ends with exit 6 (check the account's market data
subscription). Run `fetch-bars` for the same day after 06:00 New York next day to compare (and see
"Known limits").

### `run`

    python -m MainStoinker run --algos ALGOS.json --bars BARS --db live-runs.db --paper --no-prompt --minutes 415

Runs the algos of an algos file on live 1-minute bars from the paper account. The bars come from
the same watcher as `watch_bars`; each closed bar goes into the engine, and the algos trade on the
simulated paper broker (realistic fills, an entry fills at the next bar's open). No order is sent
to TWS: the command builds a data-only client and has no option for orders. The run is stored in
the database as kind `paper-live` while it runs, so `runs`, `report` and `serve` can read it.
`run` is a `MainStoinker` subcommand and is paper only: without `--paper` it is refused (exit 2,
nothing started). `--paper` makes it refuse any port but 7497, any host but `127.0.0.1` and the
live opt-in variable in the environment, and the connection is never ready unless every account
id TWS reports looks like a paper id (as for the other commands, see "Paper mode").

Options (the exact text is in `--help`):

| option | default | meaning |
|--------|---------|---------|
| `--algos JSON` | required | algo config file (the `Algo_config.json` shape). Every algo needs `"mode": "paper"`. Its symbols are the symbols of the session, at most 5 |
| `--bars DIR` | required | the folder of the month files (`fetch-bars` output) for the warm-up; not in the repo or OneDrive |
| `--db SPEC` | the `STOINK_DB` environment variable | the run database: a file path or `sqlite:///path`; created if missing; not in the repo or OneDrive. With neither `--db` nor `STOINK_DB` the command is refused. `:memory:` is refused |
| `--out DIR` | `%USERPROFILE%\Stoinkulator\live` | folder for the live files (`SYMBOL/SYMBOL_live_YYYY-MM-DD.csv`, as the watcher writes them). The watcher must not write it at the same time |
| `--warmup-days N` | 2 | trading days of warm-up from the month files, at least 1 |
| `--fills realistic\|legacy` | `realistic` | only `realistic` is accepted; `legacy` is refused |
| `--capital AMOUNT` | 10000 | starting capital per algo (as `backtest`) |
| `--commission-per-share AMOUNT` | 0.005 | commission per share (as `backtest`) |
| `--commission-min AMOUNT` | 1.00 | minimum commission per order (as `backtest`) |
| `--slippage-per-share AMOUNT` | 0 | slippage per share on market and stop fills (as `backtest`) |
| `--port N` | 7497 with `--paper` | only 7497 is accepted with `--paper` |
| `--host` | `127.0.0.1` | only `127.0.0.1` is accepted with `--paper` |
| `--client-id N` | 20 | 0 and 123 are refused |
| `--paper` | off | required: without it the command is refused. Also makes it refuse the live opt-in variable in the environment and run the account check |
| `--no-prompt` | off | only with `--paper`: skip the console gate. Without it you must type the port number |
| `--minutes N` | required | how long the session may last, 1 to 420. 415 covers 09:25 to 16:20; on a half day (13:00 close) 235 covers 09:25 to 13:20 (the session ends on its own at the close plus 20 minutes) |
| `--stop-file PATH` | none | when this file appears the run is ended cleanly (`completed`, `stop_file`). It must not exist at the start. Not in the repo or OneDrive |
| `--record FILE` / `--no-record` | a new `run-<date>-<n>.jsonl` in `%USERPROFILE%\Stoinkulator\recordings` | the session recording is on by default; an existing file is never overwritten. The two options exclude each other |
| `--connect-timeout S` | 30 | seconds per connection attempt, above 0 and at most 300 |

Where it writes:

- the run database (`--db`): the run, its bars, orders, fills, round trips, equity, counts and a
  heartbeat, written step by step while it runs. The database must be on a local disk: a file that
  cannot be switched to WAL journal mode is refused. The first write with this code migrates an
  older database to the current schema. A database a newer program wrote is refused;
- the live files (`--out`), one file per symbol and day, written by the watcher;
- the recording (`--record`);
- `%USERPROFILE%\Stoinkulator\run-out`: the algos' working folder;
- the stop file, which the owner creates; the command only looks for it (about once a second).

Folders the command makes and leaves empty after a refusal are removed again. A path inside the
repository or a OneDrive folder is refused for every one of the above.

The warm-up. Before the engine starts, the algos get the last `--warmup-days` trading days before
today from the month files in `--bars`, plus today's bars since 09:30 from the first backfill that
IB sends. No algo is updated on these bars and nothing trades on them: a start after 09:30 does
not catch up (decision D1 of issue 187). The warm-up needs month files for every symbol with at
least one day before today, else the command is refused. A last warm-up day that is not the
previous trading day (market calendar: weekends and holidays skipped) is printed as a warning. The engine starts when every symbol's first backfill is
over; bars that arrive earlier wait. Run `fetch-bars` for the warm-up days after 06:00 New York on
the day of the session: a day fetched earlier is provisional, and a later refetch changes the
file, which makes `report RUN --verify` say UNVERIFIABLE.

Gaps. The watcher drops every minute it cannot vouch for (seen only in the backfill, spanning a
connection loss or a data farm break, a stalled stream, a re-request gap). A bar that follows a gap
(the symbol's previous bar is not exactly one minute earlier, or its first bar of a day is not
09:30) opens no new position, and a pending entry that would fill on such a bar is cancelled and
counted instead of filled; exits and stops still work (`engine/gaprule.py`). The counts show in
`report RUN` ("minutes fed", "minutes dropped by the watcher" with a reason each, "late, not fed",
"bars that follow a gap", "entries refused", "pending entries cancelled") and on the dashboard in
the "Live counts" panel and the notes of the run's overview.

Faults and end reasons. Every end stores a status and an `end_reason`. The status is `completed`
or `failed`; while it runs it is `running`.

| end reason | status | exit | meaning |
|------------|--------|------|---------|
| `session_end` | completed | 0 | every running symbol is decided through the last session minute (15:59), or New York server time passed 16:21 (16:00 plus the watcher's 20 minute grace plus 60 s) |
| `minutes_elapsed` | completed | 0 | `--minutes` ran out |
| `stop_file` | completed | 0 | the stop file appeared |
| `symbol_stopped` | failed | 1 | a symbol stopped (IB error, unusable live file): a run with a symbol missing is ended |
| `error` | failed | 4 | an unexpected error, a store error, or an engine step that raised. |
| `connection_closed` | failed | 4 | the connection closed or stayed lost |
| `not_paper` | failed | 5 | TWS is not signed into a paper account. At the start nothing was requested and nothing is stored; later the run is ended |
| `no_data` | failed | 6 | a symbol showed no bar: no lag could be measured for 900 s of the session, or the watcher ended with no data received for a symbol |
| `delayed_data` | failed | 7 | a symbol's stream is labelled delayed, or silent while its bars are behind IB's clock. The first sessions need real-time data |
| `data_fault` | failed | 7 | the aligner refused a bar; a bar came late after its symbol was decided; a late bar that no re-requested backfill explains; or a symbol was without any bar or decision for 600 s of the session |
| `step_too_slow` | failed | 7 | one engine step took more than 60 s. A step over 20 s is only stored as a warning and printed |
| `interrupted` | failed | 130 | Ctrl-C |
| `abandoned` | failed | none | not a result of `run` itself: the next `run` closes a run that was left marked running with a stale last write (see below) |

Open positions are never closed at the end: the engine cancels pending entries and fills exits
that were already requested, and the positions that are still open are printed and listed by
`report` and the dashboard.

Exit codes of `run` (`--help` shows them): see the table under "Exit codes".

The output. The first line is `paper mode: port 7497, account check on, no prompt` (with
`--no-prompt`), then `Unattended paper run: data only, paper account only.` and a description of
the run (host, port, client id, symbols, the longest run time, algos file, database, live files,
recording). Then the watcher's lines, one per closed bar, and lines that start with `live:` for the
engine (the run was begun, a late or refused bar, a slow step, a fault). At the end: the watcher's
summary, then `live: run N <status> (<reason>): ...` with the bars fed, counts and the longest step;
`live: no position left open` or one `OPEN POSITION (left open)` line per position; the check of the
stored run (`live: verify: VERIFIED`, `MISMATCH` or `UNVERIFIABLE`, with `ALARM` lines for the
last two; it does not change the exit code; it is skipped when no bar was fed). The last line is

    Live session: <status>, end reason <reason> (exit N)

It says what the database holds. Two other forms mean the end was not stored as intended:
`the run could not be closed: still marked running in the database (...)` (the database stayed busy
or refused the end; the run turns stale after 3 minutes and the next `run` closes it as
`abandoned`), and `stored as <status>, end reason <reason> (the session meant ...; the run's record
has errors)`. A refusal prints `Refused: ...` and exits 2 before anything is connected. If no run
was begun, the line before it says `no run was stored`.

Ctrl-C. Ctrl-C while the session runs ends the watch; the end of the session then cannot be
interrupted. The engine ends, the run is stored as `failed` with `interrupted`, the check runs, and
the command exits with 130. Ctrl-C during the end only counts: the run is ended first, then the
exit is 130.

Another run is running. At the start the command reads the database. If a `paper-live` run is
marked `running` and was written within the last 3 minutes (six heartbeats of 30 s), `run` is
refused (exit 2) and names the run id: wait until it ends or turns stale. A run marked `running`
with no write for more than 3 minutes is stale: the process probably died. `run` closes each such
run as `failed` with `abandoned`, says so, and its open positions stay listed as open. A restart
after a crash is always a new run; an earlier run is never resumed. `runs` and `report` show a stale
run as `stale`, the dashboard as a `stale` badge.

The database is busy. The dashboard and `report` only read and never block the writer. If another
writer holds the database (a second `run`, a `backtest --db` on the same file), one store call waits
up to 2 s, is repeated 5 times with 0.6 s between, and then fails. A failed step stores nothing of
that step and ends the run as `error` (exit 4). If the end of the run cannot be stored for the same
reason, the last line says the run is still marked running.

### Half days and holidays (the market calendar, issue 285)

The watcher, `run` and `fetch-bars` follow `engine/market_calendar.py` (rule-based: NYSE holidays,
13:00 closes on the day after Thanksgiving, 24 December and 3 July when 3 and 4 July are weekdays,
one-off closures listed in `OVERRIDES`).

- A weekend or a holiday. `run` and the watcher refuse (exit 2, before anything is built or
  connected, no folder made) with `Refused: <date> is not a trading day: the market is closed on a
  market holiday (Thanksgiving)`; the date is this computer's clock in New York. After connecting,
  IB's server date is checked the same way (a clock that disagrees about the date) and IB's reported
  hours (below): a closed day ends the session with exit 2 before any history request.
  `fetch-bars` does not plan such days (no request, nothing in the manifest) and refuses a range
  with no trading day at all.
- A half day. Everything derived from 16:00 follows 13:00: the regular session of the watcher
  (silent-symbol, delayed-data and no-data windows: nothing is judged after 13:00, and "market
  closed: no new bars expected" is said then), the last-bars hold (the last two bars of the session
  are held until the close plus 20 minutes, 13:20, as 16:20 on a full day), the end of a `run` session
  (the last bar 12:59 decided, or 13:20 plus the 60 s margin) and the aligner's held clock. A half day
  ends as `completed` (`session_end`, exit 0), not `failed`. Open positions follow the existing rule
  (left open and listed) unless the algo closed them: `FVGZone` goes flat at 12:55 on a half day
  (`docs/algos-fvgzone.md`). The start line says so (`Note: ... is a half day`), and `run` names the
  `--minutes` that cover it: 235 from 09:25 to 13:20 (415 on a full day). A larger `--minutes` is
  fine: the session ends on its own.
- IB's hours as a cross-check. The contract details every symbol's lookup returns already carry
  `liquidHours`; no new request and no new message id is involved. At the start, after the lookups
  and before the first history request, today's close by the calendar is compared with IB's
  regular hours for the day: one line `Trading hours: ...` says they agree, or that they could not be
  read and the calendar is followed (no field, another time zone, a session that does not start at
  09:30, a day the text does not mention, ...). The rule, in plain words:
  - IB's close equals the calendar's: nothing changes.
  - IB's close differs, and IB's close is exactly 13:00 or 16:00 (the only closes the exchange
    uses): a `WARNING` line, and IB's close is used for today in every derived time. Example: the
    calendar says half day, IB says 16:00: the session is judged as a full day; if the stream then
    stops at 13:00, the silent-symbol check of `run` ends the run as a data fault (`data_fault`, exit
    7) by design, and the watcher reports `no data` / a stopped stream the way it would on a full
    day. The other way round (calendar full, IB says 13:00): the session ends at 13:00 and 13:20 as
    on any half day.
  - IB's close differs and is any other value (12:00, 15:00, a mis-parse that still looks valid): a
    `WARNING` naming both values, and the calendar is followed. IB's value is not used.
  - IB's hours cannot be read: one line, the calendar is followed.
  - IB says closed for a symbol on a day the calendar calls open: nothing is requested and the
    session ends with exit 2 (refused).
  Symbols that differ among themselves: the earliest close, or closed, wins, then the rule above.
- Gap reports. A half day is expected to hold 210 bars and is `complete` with them. A day the
  calendar calls full that has exactly the bars 09:30 to 12:59 stays `short`; bars after 12:59 on a
  calendar half day are kept (judged against 390) and bars on a calendar holiday are kept. Each such
  disagreement gets a `calendar:` line in `gaps_summary.txt` and a line in the `fetch-bars` summary;
  `gaps.csv` keeps its columns.
- Not covered: a closure or early close the calendar does not list (announced at short notice). Add
  it to `OVERRIDES` in a reviewed change. For a live session IB's hours (above) catch it on the day
  if IB already reports it; otherwise the owner does not start the session.

### Exit codes

| code | `fetch-bars` | `probe-data` | `watch_bars` | `run` |
|------|--------------|--------------|--------------|-------|
| 0 | complete: every day ok, no data or already done | every step done (delayed data is a success) | watched to the end, every symbol streaming | the session ended as planned (`session_end` at 16:20, or 13:20 on a half day, `minutes_elapsed` or `stop_file`); stored as `completed` |
| 1 | finished, but a day failed or a symbol was not fetched | connected, but a step failed (including NO data) | finished, but a symbol stopped, had no stream at the end or delivered no bars | a symbol stopped; stored as `failed` |
| 2 | refused: gate, port guard, arguments, path, folder layout, `--paper` conditions | refused (same list) | refused (same list), and a weekend or a market holiday | refused, nothing built or connected: usage, gate, port guard, `--paper` conditions, a mode other than `paper`, legacy fills, an unusable path or database, another `paper-live` run still running, a weekend or a market holiday (also after connecting: IB says the day is closed) |
| 3 | could not connect | could not connect | could not connect | could not connect |
| 4 | stopped early: connection lost, pacing violation twice or a fatal fault; rerun resumes | not a paper account (`--paper` only) | stopped by a lost or closed connection, or an unexpected error | stopped by a lost or closed connection or by an error; stored as `failed`. Also: the run ended as planned but its end could not be stored |
| 5 | not a paper account (`--paper` only) | not used | not a paper account (`--paper` only) | not a paper account (at the start nothing requested, nothing stored; later the run was ended) |
| 6 | not used | not used | finished, every symbol streaming or closed, but at least one symbol received no data: no bar of today's session and no live update for 120 s of the regular session after its backfill ended | no data received for a symbol; stored as `failed`, `no_data` |
| 7 | not used | not used | not used | the session stopped the run on a data fault: delayed data, refused or late bars, a silent symbol, a step that took more than 60 s; stored as `failed` |
| 130 | Ctrl-C | Ctrl-C | Ctrl-C | Ctrl-C (the run was ended in the store first) |

4 means "stopped, run again" for `fetch-bars` and "not a paper account" for `probe-data` (issue 177
item 2 asks for a code of its own). In every case a not-paper result is never retried and no
account id is printed.

## Paper mode and the layers between an agent and a live account

`--paper` (all four commands) makes the command refuse, before any connection object is built
(exit 2), unless the port is exactly 7497, the host is exactly `127.0.0.1` and the live opt-in
variable is not in the environment at all, with any value. Then the connection is built with
`require_paper=True`: it is never READY, and nothing is requested, until `nextValidId` and
`managedAccounts` have both arrived and EVERY account id listed matches `D[UFI][A-Z]?[0-9]+`
(case-sensitive, whole match; an empty list fails). A failing list at any time closes the
connection. The command re-checks `paper_verified` right after `open()` and before the first
request. A session that is not a verified paper session ends with the not-paper code and no id is
printed or logged. A connection merely closed or lost after a good check is an ordinary stop
(result line "paper account verified at start"), not a not-paper result (PR 172); a failing list
that the client never delivers before the peer closes cannot be told from a plain close.

`--paper --no-prompt` skips the console gate (no terminal, nothing read from stdin); every other
check stays. `--no-prompt` without `--paper` is a usage error. With `--no-prompt` the watcher also
requires `--minutes`, so an unattended run is bounded.

The paper-id rule was confirmed against the owner's paper account in an attended session on
2026-11-23 (three-letter form: `DU`, one letter, digits). It still rests on "no live Interactive
Brokers account id begins with D". The port is only a convention: TWS signed into a live account
with its API port set to 7497, and a live id that matched the rule, would pass every layer.

Every layer between an agent and a live account:

1. The `PreToolUse` hook (`.claude/hooks/block_live_launch.py`) blocks the entry script, the
   launcher, every `python -m MainStoinker` subcommand except `backtest`, `runs`, `report`,
   `serve`, and everything under `MainStoinker.ib`, with one exception: one whole plain command,
   `.venv\Scripts\python.exe -P -m MainStoinker fetch-bars|probe-data <args>` or `-m
   MainStoinker.ib.watch_bars <args>`, with `--paper` and `--no-prompt`, `--port` only 7497,
   `--host` only `127.0.0.1`, no `--help`, no shell syntax, the live opt-in name nowhere (and
   `--minutes` once for the watcher). `--help` of these commands is blocked for agents. The hook
   is a tripwire against accidents, not a security boundary, and cannot check which code is
   checked out: the rule in `CLAUDE.md` (main session, clean `origin/revival`) does.
2. The `--paper` gate in `cli.py` (above).
3. `IbConnection(require_paper=True)` (above).
4. `check_broker_port` (`Util/broker_guard.py`, also used by `IBapi.connect` and the data-only
   client): only the paper ports 7497 and 4002 unless the owner has set the live opt-in in their
   own terminal. Only an exact `1` counts.
5. The data-only client (allowlist of ten message ids, checked twice): even a wrongly verified live
   session could only make data requests. The owner also runs TWS with Read-Only API on, so TWS
   refuses API orders itself.
6. The pytest block: any broker connection during a pytest run raises (`tests/conftest.py`), and a
   pytest session marks its environment so `IbConnection.open()` refuses a real client in a child
   process too.
7. Path and gate rules: output and recording folders inside the repo or a OneDrive folder, or that
   are files, are refused; a connecting command run without `--no-prompt` needs a real terminal and
   the typed port.
8. TWS itself: port 7497 exists only while TWS is configured for it.

The live account, the live ports and the opt-in variable are the owner's alone.

## Where data lands

Defaults come from `USERPROFILE`, else `HOME`, in the environment given to `main` (never the
machine's idea of "home"); a command with neither set refuses to start.

- `%USERPROFILE%\Stoinkulator\bars` (`fetch-bars --out`): `SYMBOL/SYMBOL_YYYY-MM.csv`, the layout
  `engine.feed` reads, plus in each symbol folder `manifest.json` (per day: ok, no_data or failed
  with the IB text, the contract id), `gaps.csv` (`symbol,day,status,expected,present,missing,
  ranges`) and `gaps_summary.txt`. A gap status is complete, provisional (all 390 minutes, fetched before the day is
  final), short (a day the calendar calls full that has exactly the bars 09:30 to 12:59: an early close it did not know), partial, no_data or not_fetched. A half day by the calendar is complete with 210 bars (390 expected on a full day). Where the data and the calendar disagree (bars after 12:59 on a calendar half day, a short day on a calendar full day, bars on a calendar holiday) the row is kept as the bars say, `gaps_summary.txt` carries a `calendar:` line for the day and a count, and `fetch-bars` prints it. Files are written atomically and merged day by day; the manifest is set aside, never
  deleted, if it cannot be used.
- `%USERPROFILE%\Stoinkulator\live` (watcher `--out`): `SYMBOL/SYMBOL_live_YYYY-MM-DD.csv`, kept out
  of the bars folder so fetched history and live observation never mix.
- `%USERPROFILE%\Stoinkulator\recordings`: one new file per run, `fetch-bars-DATE-NNN.jsonl`,
  `probe-data-DATE-NNN.jsonl`, `watch-bars-DATE-NNN.jsonl` or `run-DATE-NNN.jsonl`; an existing
  file is never overwritten.
- `run` also writes the run database (`--db`, or `STOINK_DB`; the path
  `%USERPROFILE%\Stoinkulator\live-runs.db` is the one unattended sessions use, there is no default) and
  `%USERPROFILE%\Stoinkulator\run-out` (the algos' working folder); its live files go to the same
  `live` folder as the watcher's.

These folders are the owner's. Agents write there only through the commands' own defaults, in a
session the owner asked for. Tests never do. The command test files (`test_ib_cli.py`,
`test_ib_cli_paper.py`, `test_ib_probe.py`, `test_ib_watch_bars.py`) enforce it with
`tests/home_guard.py` (shown by `test_home_guard.py`): for the length of a test it wraps the calls
that write or change the file system, raises at once and records when a path inside the real
`Stoinkulator` folder is touched, so the test fails at teardown even if the code swallowed the
error. It is not applied to other test files, and it watches only its own process.

Volumes are stored exactly as IB sends them: shares only if the TWS API setting for US stock sizes
in lots is unticked. The data time zone is fixed in code.

## The recorder

`SessionRecorder` is passed to `IbConnection` as an observer. It writes JSON Lines: one header, then
one line per request and per raw callback IB made, flushed per line, so a crash leaves a readable
file. It is on by default in the commands; `--no-record` turns it off.

Redaction happens at write time and fails closed: if anything account-shaped remains, the arguments
are replaced by a marker. In plain words, it removes or replaces:

- the user name, machine name, home folder and cloud-sync folder name (the command passes them),
  and the first and last name taken from the home folder;
- every account id TWS reported (`managedAccounts`) wherever it appears, as `ACCOUNT-n`, and
  anything of the shape one to three letters followed by five or more digits, even before the list
  is known (it over-redacts on purpose; narrow exceptions exist for option symbols, trading-hours
  dates and the time in the header);
- paths, e-mail addresses (except IB's `SYMBOL@EXCHANGE` tokens) and IP addresses;
- anything passed under an account-like name;
- the times IB itself puts in an `error` and in the server-time answer (set to 0), so a recording
  does not say when the session ran. Bar times are kept; for a request that ends "now" they say
  roughly when the session ran.

What it does not cover: encodings (base64, percent, hex), ids split over events, look-alike
letters, and private data of no recognisable shape. `scan_recording` is a separate checker with
its own patterns; each command runs it on the finished file and prints the findings (line number,
reason, first two characters and length, never the text). A clean scan is not proof.

A recording is committed only after the owner has read the whole file once (issue rule, owner
ruling 2026-11-22; header, every `error` line and every server-time line by eye, a search for
account digits with any separator, and each one- or two-letter word of the owner's names, which are
not removed). Replay (`tests/ib_replay.py`) must then report no problems. When in doubt, do not
commit. `tests/test_ib_recorder.py::test_no_account_pattern_in_committed_files` runs the repo's own
account-shape check over the committed recordings and recorder files, and `scan_recording` over
`tests/recordings/`. Only the synthetic recording is committed today.

## Testing

IB code is tested against `tests/fake_ib.py`, a scripted stand-in for `EClient` that follows
ibapi 10.37's behaviour (handshake order, refused connections, a hung handshake released by
`disconnect()`, peer close, client id in use, thread of each callback), has no order-capable method
and never opens a socket. `tests/ib_replay.py` turns a recording into fake responders: a recorded
request is paired with the callbacks that carry its request id, replayed with the new id. Tests
never connect; none runs a connecting command in a subprocess (a subprocess test may only use
`--help` or an argument error that exits before any client is built). `tests/no_build.py` makes a
"usage error, nothing built" test fail fast rather than hang.

## Known limits and open follow-ups

- Issue 174: same-evening history is not final (the opening-minute volume of one day grew from
  about 127,000 to about 1,030,000 shares between 22:19 and the next morning). `fetch-bars` now
  treats a day as provisional until 06:00 New York next day (PR 176, above); the exact moment IB's
  bars become final is not known, 06:00 is a margin. A watcher symbol that receives nothing is
  `no data received` with exit 6 (PR 178). Still open: a pre-open start is unverified (whether a
  subscription opened before 09:30 rolls into the session; `run` neither waits for 09:30 nor
  refuses an earlier start); the `--help` text of `run` still says "Only the owner runs it for
  now".
- Issue 177: the exit code 4 clash of `probe-data`; console masking of the exact ids TWS reports;
  data-farm breaks timed from when the notice is read; the lag bound is a running minimum; (holidays
  and half days: issue 285, below); whether the last update of a minute equals the final bar is still to
  be checked against `fetch-bars`; the hook matches the interpreter by path only and accepts
  `--minutes` 0 and 421 to 999, which the watcher then refuses. Volumes differ by a few percent
  between the live and paper feeds for the same bar (owner: not a concern for current strategies).
- Issue 156: recorder follow-ups, among them warning when a private word is a common word, which
  blanks farm texts everywhere (replay still works by code).
- Issue 151: `IbMarketData` follow-ups: the pre-request drain has no bound, `poll_event` and
  `wait_history` can disagree after a 1101 followed by an error, `resolve_stock` raises
  `NotConnected` on a closed connection, a late server-time answer can be old.
- Issue 140: monthly bar file follow-ups: the fingerprint is taken after the read, so a fetch that
  rewrites the current month while a backtest runs can mismatch; the writer accepts unsorted dates
  the reader refuses; no fsync before the atomic replace; lower-case tickers behave differently
  from upper-case on Windows.
- Issue 161: two small follow-ups to the hook's legacy-script rule.
- A day with fewer bars than the calendar expects is kept and listed, never failed. The calendar is
  rule-based (`engine/market_calendar.py`); a closure it does not list (an announced one-off) is
  not planned for, IB answers it with no data, and `fetch-bars` records `no_data`; add it there.
- Python cannot stop code that replaces the client's guard by writing private attributes or talks
  to a socket of its own: callers are reviewed for that, and the import boundary test forbids
  socket modules in the package.
- Whether the rest of the real behaviour matches the code is checked only in the owner's sessions;
  nothing here is verified against IB beyond what those sessions showed.
