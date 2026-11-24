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
  resume manifest, the gap report. There is no holiday calendar: a holiday is requested like a
  weekday and recorded as `no_data`.
- `fetch.py` the driver behind `fetch-bars`; `probe.py` the steps of `probe-data`; `watch_bars.py`
  the live bar watcher; `cli.py` the argument parsing, path checks, console gate, paper mode,
  scrubbed printer and the shared session runner of `fetch-bars` and `probe-data`.
- `recorder.py` the session recorder (below); `contracts.py` plain US stock contracts (STK, SMART,
  USD); `events.py` the event types.

Client ids: 21 for `fetch-bars` and `probe-data`, 22 for the watcher. 0 and 123 are always refused
(0 would take over orders placed by hand in TWS; 123 is the legacy code's id).

## The commands

All three are for a data session with TWS or IB Gateway running. The owner runs them in a real
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
counts as a holiday (`no_data`) only once that moment has passed; sooner it is `failed` ("no data
yet") and asked again. The summary shows a provisional count and the days, and the gap files carry
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
closed. Run `fetch-bars` for the same day after 19:00 New York to compare (and see "Known limits").

### Exit codes

| code | `fetch-bars` | `probe-data` | `watch_bars` |
|------|--------------|--------------|--------------|
| 0 | complete: every day ok, no data (holiday) or already done | every step done (delayed data is a success) | watched to the end, every symbol streaming |
| 1 | finished, but a day failed or a symbol was not fetched | connected, but a step failed (including NO data) | finished, but a symbol stopped, had no stream at the end or delivered no bars |
| 2 | refused: gate, port guard, arguments, path, folder layout, `--paper` conditions | refused (same list) | refused (same list) |
| 3 | could not connect | could not connect | could not connect |
| 4 | stopped early: connection lost, pacing violation twice or a fatal fault; rerun resumes | not a paper account (`--paper` only) | stopped by a lost or closed connection, or an unexpected error |
| 5 | not a paper account (`--paper` only) | not used | not a paper account (`--paper` only) |
| 130 | Ctrl-C | Ctrl-C | Ctrl-C |

4 means "stopped, run again" for `fetch-bars` and "not a paper account" for `probe-data` (issue 177
item 2 asks for a code of its own). In every case a not-paper result is never retried and no
account id is printed.

## Paper mode and the layers between an agent and a live account

`--paper` (all three commands) makes the command refuse, before any connection object is built
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
  final), short (a half day), partial, no_data or not_fetched. Files are written atomically and merged day by day; the manifest is set aside, never
  deleted, if it cannot be used.
- `%USERPROFILE%\Stoinkulator\live` (watcher `--out`): `SYMBOL/SYMBOL_live_YYYY-MM-DD.csv`, kept out
  of the bars folder so fetched history and live observation never mix.
- `%USERPROFILE%\Stoinkulator\recordings`: one new file per run, `fetch-bars-DATE-NNN.jsonl`,
  `probe-data-DATE-NNN.jsonl` or `watch-bars-DATE-NNN.jsonl`; an existing file is never overwritten.

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
  bars become final is not known, 06:00 is a margin. Still open: a watcher run that receives
  nothing for a symbol says "streaming" with exit 0 (a "no data received" outcome with its own
  code is in an unmerged PR and is not in the code described above); a pre-open start is
  unverified; the banner says "Only the owner runs it" in unattended runs.
- Issue 177: the exit code 4 clash of `probe-data`; console masking of the exact ids TWS reports;
  data-farm breaks timed from when the notice is read; the lag bound is a running minimum; holidays
  are unknown to the watcher; whether the last update of a minute equals the final bar is still to
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
- A day with fewer than 390 bars is kept and listed, never failed; there is no holiday calendar.
- Python cannot stop code that replaces the client's guard by writing private attributes or talks
  to a socket of its own: callers are reviewed for that, and the import boundary test forbids
  socket modules in the package.
- Whether the rest of the real behaviour matches the code is checked only in the owner's sessions;
  nothing here is verified against IB beyond what those sessions showed.
