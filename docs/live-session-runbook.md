# Runbook: the first unattended paper session with `run`

For the owner. This is the session of issue 201 (stage 2: the algos run on live 1-minute bars from
the paper account, with simulated fills; no order is ever sent). Every option and exit code of
`run` is in `docs/live-data.md`; the older checklist for the data commands is
`docs/owner-session-checklist.md`. All commands are PowerShell, typed from the repository root.

The session is done (issue 187, D12) when all four hold: it ends `completed`; `report RUN --verify`
says VERIFIED; the dashboard showed the run while it ran; the next-morning comparison is reported.

## The evening before

1. TWS is installed and you can log in with the PAPER login. Port 7497.
2. In TWS, Global Configuration, API, Settings (TWS's own menu names, not checked by this project):
   Read-Only API ON, and the setting that sends US stock sizes in lots UNTICKED. Ticked, the
   volumes are lots and cannot be converted back.
3. The paper account gets real-time market data (the live account's data shared with it, as in the
   session of 2026-11-24). No other session of the same user may use the data (a second TWS, a phone
   app, the web portal), or IB answers with code 10197.
4. TWS's auto-restart time is outside 09:25 to 16:25 New York. A restart closes the connection
   and ends the run.
5. The computer stays awake and on power until after 16:25 New York: no sleep, no update restart.
6. Back up the live runs database. The first write by this code (`run`, or `backtest --db`) migrates
   an older database to the current schema, and a database written by newer code is refused by
   older code, so there is no way back except the copy. Close `serve` first, then:

       New-Item -ItemType Directory -Force "$env:USERPROFILE\Stoinkulator\runs-backup" | Out-Null
       Copy-Item "$env:USERPROFILE\Stoinkulator\live-runs.db*" "$env:USERPROFILE\Stoinkulator\runs-backup"

   `%USERPROFILE%\Stoinkulator\live-runs.db` is the path unattended sessions use (the code has no default
   path: you give `--db`, or set `STOINK_DB`). If you keep the database elsewhere, copy that file
   and the files beside it that start with the same name. If there is no database yet, skip this.
   Unattended sessions write to `live-runs.db`, a separate file, and never to `runs.db`: `runs.db` holds
   backtest runs and no session opens it for writing (a first write migrates a schema-2 database one way).
7. The algos file: `"mode": "paper"` on every algo (a live run refuses any other mode), at most 5
   symbols in all. Issue 187, D10: FunctionTest and EMACrossing for the first sessions. Keep the file
   outside the repository if you like; the placeholder below is `ALGOS_FILE`.

## Before the start (morning, New York time)

1. The warm-up days. `run` works out how many it needs (the largest `warmup_sessions()` of the
   algos in the algos file, at least 2 trading sessions, counted by the market calendar), checks
   the month files in `--bars`, and fetches what is missing or still provisional itself, after the
   paper check and before it subscribes to live bars (issue 287): you do not have to run
   `fetch-bars` first. It needs time: at least 10 s per missing symbol-day at IB's pacing limits,
   all of it before 09:30 New York, and that time comes off the `--minutes` window (which counts
   from the start of the command, not from 09:30). A full session to 16:20 therefore needs the
   history ALREADY on disk: fetch it beforehand (the evening before after 16:00 for all but the
   last day, or in the morning after 06:00 New York) and start `run` at about 09:25, so that it
   only tops up what is missing. Starting early to fetch costs the end of the day: `--minutes 415`
   from 09:00 ends the run at 15:55 and even 420 ends it at 16:00, before the last bars at 16:20
   (the console prints a WARNING with the time the run will stop). A day fetched before 06:00 next
   day is provisional and is fetched again by the first `run` after 06:00 of the session day;
   yesterday, present or missing, is never taken as final before 06:00 (a start before then is
   refused). To fetch by hand (use the symbols of the algos file, here
   AAPL and MSFT, and a start a few trading days back):

       .venv\Scripts\python.exe -P -m MainStoinker fetch-bars --symbols AAPL,MSFT --start YYYY-MM-DD --end YYYY-MM-DD --paper --no-prompt

   `--end` is the last trading day before today. `fetch-bars` ends with `Result: complete (exit
   0)`. Console lines to expect from `run` before the open: `Warm-up: N trading session(s) of
   history (...)`, then either `History: nothing missing: ...` (one line, nothing is requested) or
   `History: incomplete: ...` and, after the connection, `History: estimate: ...`, the fetch's lines
   and `History: complete: ...`. If it says `History: STOPPED: ...` (exit 7) nothing was traded and
   no run was stored: read the reason (a day failed, the fetch was not done at 09:30), run
   `fetch-bars` for the days it names, then start again. A start after 09:30 with history missing,
   or `--no-fetch` with a gap, is refused (exit 2) and the line names the `fetch-bars` command.
   Owner-only options: `--warmup-days N` overrides the length (below the algos' need it is refused),
   `--no-fetch` refuses a gap instead of fetching, `--allow-cold` starts anyway with the history it
   has (said, and stored in the run's warnings).
2. Check out a clean `origin/revival`: `git status` shows nothing for tracked files, no untracked
   `.py` files in the repository root or under `MainStoinker/`, and `HEAD` is a commit of
   `origin/revival`.
3. TWS is running, logged in on the PAPER login, with no dialog waiting.
4. Probe the data:

       .venv\Scripts\python.exe -P -m MainStoinker probe-data --symbols AAPL --paper --no-prompt

   It must end `Result: all steps completed` with the verdict `LIVE data` for AAPL.
   `DELAYED data` is a success for the probe but not for `run`: a delayed stream stops the run
   (`delayed_data`, exit 7). Exit 4 from `probe-data` means TWS is not on a paper account: stop,
   do not retry.
5. Nothing else runs `watch_bars` on the same `live` folder, and no other `run` is running.
6. Make sure there is no stop file: `Test-Path "$env:USERPROFILE\Stoinkulator\stop-run"` must
   say `False`. A stop file that exists at the start refuses the command.

## Start (about 09:25 New York)

    .venv\Scripts\python.exe -P -m MainStoinker run --algos ALGOS_FILE --bars "$env:USERPROFILE\Stoinkulator\bars" --db "$env:USERPROFILE\Stoinkulator\live-runs.db" --stop-file "$env:USERPROFILE\Stoinkulator\stop-run" --paper --no-prompt --minutes 415

`--paper` means port 7497 on 127.0.0.1, the live opt-in absent, and an account check. 415 minutes
cover 09:25 to 16:20; the session ends on its own once the last bar is decided (about 16:20, at the
latest 16:21). On a half day (13:00 close: the day after Thanksgiving, 24 December, sometimes 3 July)
the market calendar moves every one of those times by three hours: start at about 09:25 with
`--minutes 235` (09:25 to 13:20; a larger number is fine, the session ends on its own at about
13:20) and expect `completed`, `session_end`, exit 0. On a weekend or a market holiday `run` refuses
to start (exit 2, `Refused: ... is not a trading day`). The `--minutes` deadline runs from the start of the
watch: 415 (235 on a half day) leaves no room if the command is started at or before about 09:25:10, and the
session then ends as `minutes_elapsed` just before the last two held bars (fed at the close + 20 minutes) are fed;
the safe values are 420 on a full day and 240 on a half day (420 is the cap). Without `--no-prompt` the command prints what it will do and asks you to type
`7497`.

A start before 09:30 depends on a subscription opened before the open rolling into the session
(issue 187, D8). The code neither waits for 09:30 nor refuses an earlier start. Read the result of
the pre-open watcher run of 2026-11-25 first; if it did not roll, start at 09:30 instead and use
`--minutes 410`.

## What healthy output looks like

- First line `paper mode: port 7497, account check on, no prompt`, then `Unattended paper run:
  data only, paper account only.` and the description of the run: host, port, client id 20, the
  symbols, the algos file, the database, the live files, the recording file. It says that no order
  is ever sent.
- A line `live: run N begun (paper-live, realistic fills); warm-up 2 day(s) ... stepping from the
  next closed bar`. Run N is the id to use below. Note it down.
- Then one watcher line per closed bar and symbol, about 4 to 5 s after its minute, for example
  `09:31  AAPL  ...  volume ...  (updates 12, closed by a later update)`.
- Possible but not alarming: a minute dropped because it cannot be vouched for (the watcher says
  why; it is counted), a line about a step slower than 20 s, a bar that came late from a
  re-requested backfill (`counted late`).
- Alarming: a line that starts `live: FAULT`, `no data received`, `live: ERROR`, a bar `not given on
  (overlap)`, or a step above 60 s. A fault ends the run (see the failure table).
- Do not read anything into a quiet console between bars: the forming-bar line is switched off.

## Watch it on the dashboard

In a second terminal, from the repository root (needs `requirements-web.txt`):

    .venv\Scripts\python.exe -m MainStoinker serve --db "$env:USERPROFILE\Stoinkulator\live-runs.db"

Open `http://127.0.0.1:8750`. Reading never blocks the run. Run `serve` on the same database file;
it only reads. What to look for:

- The header strip shows `live: running` while a live run is being written. `live: stale` means
  the last write is more than 3 minutes old (the run's process probably died).
- The runs list shows a badge `running` on the run (`stale`, `completed` or `failed` later; the
  end reason is the tooltip). The list refreshes every 30 s.
- The run's overview shows the live status (liveness, last write), the open positions (algo, symbol,
  side, quantity, entry price and time, stop, target, and last close and unrealised result when
  known), the live counts per symbol (minutes fed, late, dropped with the reason) and the gap rule
  notes. It refreshes every 30 s. The algo view refreshes every 60 s. A note `refresh failed` means
  the last refresh failed.
- The run writes after each stepped minute, so the dashboard is up to a minute plus the refresh
  interval behind the console. Indicator markers of an algo arrive some bars late (a known delay).

## Stop it cleanly

- At the end of the session, or after `--minutes`, nothing to do.
- To stop earlier: create the stop file. The command looks for it about once a second, ends the
  run as `completed` with `stop_file`, and exits 0:

      New-Item -ItemType File "$env:USERPROFILE\Stoinkulator\stop-run"

  Delete the file afterwards (`Remove-Item "$env:USERPROFILE\Stoinkulator\stop-run"`), or the next
  start is refused.
- Ctrl-C also works: the run is ended in the database first, stored `failed` with `interrupted`,
  and the command exits 130. Prefer the stop file.
- Open positions are not closed. The end lists them (`OPEN POSITION (left open)`), and so do
  `report` and the dashboard. The positions were simulated: nothing is open at the broker.

## After the session

1. Read the last line. `Live session: completed, end reason session_end (exit 0)` is the wanted one
   (`minutes_elapsed` and `stop_file` are also `completed`). Anything else: see the failure table.
2. List the runs and print the report (replace RUN with the run id):

       .venv\Scripts\python.exe -m MainStoinker runs --db "$env:USERPROFILE\Stoinkulator\live-runs.db"
       .venv\Scripts\python.exe -m MainStoinker report RUN --db "$env:USERPROFILE\Stoinkulator\live-runs.db"

   The report gives the statistics per algo and in total, the liveness and end reason, the live
   counts (minutes fed, dropped by the watcher with the reason, late and not fed), the gap rule
   (bars that follow a gap, entries refused, pending entries cancelled) and the open positions.
3. Verify the run exactly. This replays the stored bars through the engine and compares:

       .venv\Scripts\python.exe -m MainStoinker report RUN --verify --bars "$env:USERPROFILE\Stoinkulator\bars" --db "$env:USERPROFILE\Stoinkulator\live-runs.db"

   The session already printed the same check at its end (`live: verify: ...`). Results:

   | line | exit | meaning |
   |------|------|---------|
   | `verify: VERIFIED` | 0 | the stored fills, round trips, open positions, equity, gap counts and bar counts equal the replay. This is the D12 pass |
   | `verify: MISMATCH` | 1 | at least one difference, the first few listed per record. An alarm about the ENGINE: open an issue and keep the database |
   | `verify: UNVERIFIABLE` | 5 | the replay could not be built or its input changed: a warm-up month file is missing or changed (for example a provisional day was refetched), or the run's record is incomplete. An alarm about the RECORD, not the engine |

   Exit 2 is a usage error (no bars folder, unknown run or database); exit 4 means the run is not a
   live run with stored bars.
4. After 06:00 New York next day, compare with final bars. Fetch the day (same symbols, same bars
   folder), backtest the same day with the same algos file, and compare the two runs in the
   dashboard (the compare view takes 2 to 5 run ids). It is not expected to be exact (whether the last live
   update of a minute carries the final values is not established, and volumes differ by a few
   percent between feeds):

       .venv\Scripts\python.exe -P -m MainStoinker fetch-bars --symbols AAPL,MSFT --start YYYY-MM-DD --end YYYY-MM-DD --paper --no-prompt
       .venv\Scripts\python.exe -m MainStoinker backtest --algos ALGOS_FILE --data "$env:USERPROFILE\Stoinkulator\bars" --out "$env:USERPROFILE\Stoinkulator\compare-YYYY-MM-DD" --replay-from YYYY-MM-DD --fills realistic --db "$env:USERPROFILE\Stoinkulator\live-runs.db" --quiet

   (`--start YYYY-MM-DD --end YYYY-MM-DD` instead of `--replay-from` also loads the history the algos
   declare they need before that day, `warmup_sessions()`, and refuses when the bars do not reach back
   far enough; it needs bars from earlier days, so fetch them too.)

   YYYY-MM-DD is the day of the session in both commands. `fetch-bars` needs TWS on the paper
   login again. The fetched day stays provisional until 06:00, so do it after that time.
5. Findings: one new issue per finding, with the run id, the end reason and the lines around it.
   Before sending any file or console text, search it for your account number.

## If it did not end `completed`

The last line, `Live session: ... (exit N)`, says what the database holds. In every case the open
positions are simulated only. A restart after any failure is a NEW run; an earlier run is never
resumed.

| exit | meaning | what to do |
|------|---------|------------|
| 1 | a symbol stopped (`symbol_stopped`) | read the watcher lines above the fault line (IB error, unknown symbol, unusable live file). Fix that, start a new run |
| 2 | refused, nothing was connected | the line `Refused: ...` says which check. Another `paper-live` run marked running and written within 3 minutes: wait, or check `runs`. A stop file that exists: delete it. Missing history that cannot be fetched before 09:30 (`--no-fetch`, a start after the open, too little time, a day not final before 06:00), or an algo config that cannot be built: the line says which, and names the `fetch-bars` command |
| 3 | could not connect | TWS running and logged in on the paper login, API enabled, port 7497, 127.0.0.1 trusted, no dialog waiting in TWS, client id 20 not used by another program. Nothing was stored |
| 4 | stopped by a lost or closed connection, or by an error (`connection_closed`, `error`); also a connection lost while the history was fetched (no run stored) | check TWS (a restart, a lost internet connection, a login dialog). Read the last line: if it says `could not be closed`, see the last row. Start a new run if the session is still open (it only warms up on today's earlier bars; no catch-up trading) |
| 5 | TWS is not on a paper account (`not_paper`) | STOP. Do not retry. Log TWS into the paper account yourself, and look at what happened before you try again |
| 6 | no data received (`no_data`) | check the market data subscription of the paper account (shared with the live account's data), 10197 (another session uses the data), and whether a closure the market calendar does not list was announced (weekends and the listed holidays are refused at the start, never run) |
| 7 | the run was stopped on a data fault (`delayed_data`, `data_fault`, `step_too_slow`), or, before any bar and with no run stored, the history was not complete by the open | read the line `live: FAULT (...)`. Delayed data: market data again. A data fault or slow step is a finding: open an issue with the console text. `History: STOPPED: ...`: see "Before the start", item 1: fetch the days it names, start again earlier |
| 130 | Ctrl-C | the run is stored `failed`, `interrupted` |
| any | the last line says `still marked running in the database` | the database stayed busy or refused the end of the run. Wait 3 minutes: it shows as `stale`, and the next `run` closes it as `failed`, `abandoned`. Do not edit the database by hand. `report RUN --verify` still works on it |

## What must never be done

- Never use a live port (any port but 7497 on the paper login), the live account, or `STOINK_ALLOW_LIVE_PORT`. They are
  the owner's own opt-in for data sessions only. `run` refuses everything but `--paper`.
- Never turn Read-Only API off in TWS. It stays ON for all of stage 2.
- Never add an option to place orders. `run` has none; orders are not part of stage 2 and are a
  separate owner decision.
- Never run the entry script or the launcher (`this is how we do it dum dum.py`, `Stonks data
  collect.bat`).
- Never run two broker sessions at once, and never run `watch_bars` and `run` into the same
  `live` folder at the same time.
- Never commit anything under `tests/recordings/` before you have read the whole file.
