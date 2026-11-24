# Owner session checklist: market data from TWS

For the owner, in the owner's own terminal. No agent runs any of this against the live account, and
no agent sets or asks for the live opt-in. Reference for every option and outcome:
`docs/live-data.md`. The commands only request data (history, quotes, contract details, the
server time); the client cannot send an order.

## Before a session

TWS (or IB Gateway), logged in. Global Configuration, API, Settings:

- Enable ActiveX and Socket Clients: ON.
- Read-Only API: ON. TWS then refuses every API order, on top of the data-only client.
- Socket port: 7497 for the paper login, 7496 for the live login (TWS defaults; the commands use
  whatever you type, but `--paper` accepts only 7497).
- Trusted IPs include 127.0.0.1, and "Allow connections from localhost only" is ON. The commands
  connect to 127.0.0.1 only.
- Master API client id: empty. The commands use client id 21 (`fetch-bars`, `probe-data`) and 22
  (the watcher). If another program uses one, pass another `--client-id`; 0 and 123 are refused.
- The setting that sends US stock sizes in lots: UNTICKED. Ticked, volumes come in lots and cannot
  be converted back.
- Which login: the paper login for `--paper` runs; the live login for a live-account data session.
  The paper account gets market data only if the live account's market data is shared with it
  (Account Management). No other session of the same user may be using the market data (a second
  TWS, a phone app, the web portal), or IB answers with code 10197.
- Auto-restart time (Global Configuration, Lock and Exit): set it outside the hours you will run. A
  restart closes the connection mid-run: the command stops (see the exit codes) and `fetch-bars`
  resumes when run again.
- The `.venv` is installed (`uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt`
  and `-e .`) and the checkout is the one you intend to run.

Folders: the commands write under `%USERPROFILE%\Stoinkulator` (`bars`, `live`, `recordings`) and
refuse a folder inside the repository or OneDrive. Use `--out` to keep a first session apart from
later data.

## Paper account

Start TWS on the paper login, then, from the repository root:

    .venv\Scripts\python.exe -P -m MainStoinker probe-data --symbols AAPL --paper
    .venv\Scripts\python.exe -P -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA --start 2024-08-01 --end 2024-08-30 --paper
    .venv\Scripts\python.exe -P -m MainStoinker.ib.watch_bars --paper --symbols AAPL,MSFT,TSLA --minutes 30

`--paper` means port 7497, host 127.0.0.1, the live opt-in absent from your environment, and an
account check: nothing is requested unless every account TWS reports looks like a paper account. You
type `7497` when asked. Add `--no-prompt` only for an unattended run (the watcher then also needs
`--minutes`).

## Live account (data only)

Start TWS on the live login with Read-Only API ON. A live port is refused unless you opt in, in your
own terminal, for that terminal only. The refusal message prints the exact PowerShell line to use;
run it yourself in the one window you will run the commands in, and do not put it in a profile, a
script or the system settings. A value with anything after it (a trailing space) is refused. When
the session is over, close that window.

    .venv\Scripts\python.exe -P -m MainStoinker probe-data --symbols AAPL,MSFT,TSLA --port 7496
    .venv\Scripts\python.exe -P -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA --start 2024-08-01 --end 2024-08-30 --port 7496 --out %USERPROFILE%\Stoinkulator\bars-stage1
    .venv\Scripts\python.exe -P -m MainStoinker.ib.watch_bars --symbols AAPL,MSFT,TSLA --minutes 30 --port 7496

Type `7496` when asked. `--paper` is refused on 7496. Run `probe-data` during the regular session
(09:30 to 16:00 New York) to see live data; the watcher is only useful then. `fetch-bars` can run
at any time: about 10 s per symbol-day (the three symbols above for August 2024: 66 requests, about
11 minutes; the printed estimate comes from IB's own clock).

## What good output looks like

- Every command prints what it will do (host, port, client id, files) before asking for the port.
  With `--paper` the first line after the gate is `paper mode: port 7497, account check on`.
- `probe-data`: steps 1 to 7, each OK; per symbol a verdict `LIVE data`, `DELAYED data (15 min)` or
  `NO data`; a first bar with hundreds of thousands to millions of shares for a large stock; the last
  line `Result: all steps completed`. Delayed data is a success (exit 0).
- `fetch-bars`: a `Summary` with, per symbol, `N ok, N no data, 0 failed, ... [done]` (22 `ok`
  days for 2024-08-01 to 2024-08-30; weekends are not requested); the first fetched day
  of each symbol with 390 bars (09:30 to 15:59) or fewer on a half day; a gaps line per symbol that
  names no `partial` day; `Result: complete (exit 0)`. Running the same command again fetches
  nothing and also ends with exit 0.
- Watcher: one line per closed bar, the offsets, then a summary with every symbol `streaming`. A
  symbol that shows no bars at all is not a success even if the exit code is 0 (issue 174).
- Last lines of every command: `Recording: <file>` with the scan findings, `Result: ... (exit N)`.

## Exit codes

| code | meaning | what to do |
|------|---------|------------|
| 0 | done | nothing |
| 1 | finished with problems (`fetch-bars`: a day failed or a symbol was not fetched; `probe-data`: a step failed or a symbol has NO data; watcher: a symbol stopped or delivered no bars) | read the lines above the result; run `fetch-bars` again to retry failed days; for NO data check the market data subscriptions and code 10197 |
| 2 | refused, nothing started (gate, port guard, arguments, path, `--paper` conditions) | read the message; it says which check |
| 3 | could not connect | TWS running and logged in, API enabled, the right port, 127.0.0.1 trusted, no dialog waiting in TWS |
| 4 | `fetch-bars` and watcher: stopped early (connection lost, pacing violation twice, a fault). `probe-data`: not a paper account | `fetch-bars`: what was finished is on disk; run the same command again (after a pacing stop wait at least 10 minutes). `probe-data` with `--paper`: stop and read the next row |
| 5 | `--paper` only, `fetch-bars` and watcher: the account TWS is signed into is not a paper account; nothing was requested | log TWS into the paper account; do not retry in a loop |
| 130 | Ctrl-C | the connection and the recording were closed; run again to resume |

## Recordings

Each run writes a new session recording in `%USERPROFILE%\Stoinkulator\recordings`, with account
numbers, names, paths and addresses removed. Nothing is committed before you have read it:

1. Read the whole file once: the header (`command`, `start_utc`), every `error` line, every
   server-time line.
2. Read the scan findings the command printed; each is a line number and a reason.
3. Search the file for your account digits with any separator between them, and for each word of one
   or two letters in your names (those are not removed automatically).
4. Remember a request that ends "now" puts bar times near the time of the session.
5. If in doubt, do not commit it. Committed recordings go under `tests/recordings/`, where the test
   suite scans them again.

The `manifest.json` files next to the bars hold IB's text verbatim and are treated like a recording:
read before sharing. `docs/live-data.md` has the redaction rules and what they cannot see.

## What to send back

- The console output of each command (it shows local paths: read it first).
- From the `probe-data` run: the summary block (verdict per symbol, IB codes seen).
- For `fetch-bars`: each symbol's `gaps.csv`, `gaps_summary.txt` and `manifest.json`, and the month
  files you want compared with `OfflineData`.
- The recordings, after you have read them, and any watcher live files.
- Anything unexpected in TWS: an order ticket, an order warning or a "read-only" refusal pop-up
  means something tried to order. Stop and report it. TWS's own API log, if enabled, should show
  only history, contract-details, time and market data requests.
- Before sending any file, search it for your account number.

## Afterwards

Close the terminal window used for a live session. Never leave the live opt-in set in a terminal
you use for anything else.
