# Live-side spec: market data, paper trading and broker orders

Status: draft, waiting for the owner's answers under "Decisions needed".

This covers what earlier specs called milestone 1 Phase B and milestone 2
Phase B. It replaces the Phase B section of `m1-core-seams.md`, which was
written before the ledger, the realistic broker, the store and the dashboard
existed.

## Goal

Run the same engine and the same algos on live Interactive Brokers data, first
with simulated fills and then with real orders on the IB paper account, with
every run stored and visible in the dashboard while it runs.

Done means:

1. Historical bars can be downloaded from IB into files the backtester reads.
2. `python -m MainStoinker run` starts the engine on live one-minute bars.
   Algos in `paper` mode get simulated fills from the existing realistic
   broker.
3. Algos in `live` mode send real orders through a new IB broker, and every
   fill reported by IB is recorded in the ledger with its real price, time and
   commission (issue 6).
4. A running session is written to the database as it goes, and the dashboard
   shows it, including open positions.
5. The live-order hazards of issue 15 do not exist in the new order code.
6. The legacy runtime is deleted.

Real money stays out of scope. The connection keeps refusing every port except
the IB paper ports unless a human sets `STOINK_ALLOW_LIVE_PORT` on the trading
machine, and nothing in this work changes that rule. Trading the real account
is a decision for after milestone 3 (risk limits, kill switch, reconnect).

## What agents can and cannot do

Agents never connect to TWS or IB Gateway. So:

- All new code is built and tested against a fake IB client inside the test
  suite, and against recordings of real sessions (see "Testing").
- Each stage ends with a short session that only the owner can run, on the
  trading machine, following a written checklist. The stage is not done until
  that session passes.
- Every pull request that can place, change or cancel an order is labelled
  `order-path` and `needs-human-review` and waits for the owner. Nothing in
  this spec auto-merges once it touches orders.

## Design

New code goes in `MainStoinker/ib/`. It is the only package allowed to import
`ibapi`. The engine package still imports neither `ibapi` nor `MainStoinker/ib`;
the two meet only in the `run` command, which builds the parts and hands them
to the engine.

### One connection, two faces

`IbConnection` owns the socket, the reader thread and the IB callbacks. It
carries the port guard (moved here from `apiApi.py`, with its tests). The rest
of the program never sees it directly; it sees one of two narrow objects:

- `IbMarketData`: historical bars and live bar updates. It has no method that
  can place, change or cancel an order. The `fetch-bars` command and `paper`
  mode use only this.
- `IbOrders`: order placement, changes, cancels, order status, executions and
  commissions. It is constructed only when at least one algo is in `live`
  mode and the command line carries `--allow-orders`.

A test proves that a run with no `live` algo cannot reach an order call: the
order methods are not on the object it holds.

Connection problems are no longer silent (issue 19): a failed connect is
logged with IB's code and message, and the command gives up after a bounded
number of attempts with a non-zero exit code.

### Threading

IB delivers callbacks on its own thread. Today each ticker has its own thread
and algos run on them. The new design has one rule: IB callbacks only put
events on a queue, and one engine thread takes them off and does all the work
(algos, brokers, ledger, store). No algo, broker or ledger code runs on two
threads, so none of it needs locks.

### Live bars

`IbBarFeed` asks IB for the warm-up history and keeps the request open, as the
legacy code does. IB then sends updates to the bar in progress. A bar is
closed when the first update for the next minute arrives; only closed bars
reach the algos. Updates to the bar in progress go to the event sink, for a
future live chart, and to the paper broker for nothing: simulated fills use
closed bars only, exactly as in a backtest.

Bar times stay in the machine's local time without a zone, as in the offline
files, so stored runs and offline data stay comparable. The run records the
machine's time zone name so the dashboard can label it.

A missing minute (no trades in that minute, or a gap after a reconnect) is
recorded as a gap. Algos are not fed invented bars.

### `Engine.run_live`

The engine gains a second loop beside `run_backtest`: it takes bar events
from the queue instead of iterating a file, and otherwise does the same
things in the same order (broker hooks, algos, ledger mark). A shared inner
step keeps the two from drifting apart; the golden backtests pin
`run_backtest`.

Fill events from IB arrive between bars. The engine applies them to the
ledger as they come, in order.

### Paper mode on live bars

`PaperBroker` is used unchanged. Entry at the next bar's open, stops and
targets inside the bar, commission and slippage: the same rules as a
realistic backtest, now fed by live bars. End of session cancels pending
entries as in a backtest. A position still open when the program stops is
left open in the stored run and marked as such; it is not closed by a
synthetic fill, because nothing was closed.

### `IbBroker` for `live` mode

A new implementation of the existing `Broker` protocol. It is written fresh
against the protocol and the issue 15 list, not moved from `trade.py` (see
decision 1).

- **Entry and protection as one bracket.** The entry order is the parent; the
  stop is its child (`parentId` set). IB then activates the stop only when the
  entry fills, and cancels it if the entry is cancelled or rejected. A
  take-profit, when the algo asks for one, is a second child in the same
  one-cancels-other group as the stop.
- **Time in force.** Stop and take-profit are good-till-cancelled, so a
  position held overnight keeps its protection. A market entry that has not
  filled within a configured number of seconds is cancelled.
- **Prices** are rounded to the instrument's tick before they are sent.
- **Order ids** come from a counter seeded by IB at connect time and never
  reused; there is no per-order round trip that can time out and leave a
  stale id.
- **Every request is checked.** A rejected order or modification comes back as
  an event; the broker object reports it to the engine, which logs it, records
  it in the ledger's ignored-event list with the reason, and stops opening new
  positions for that algo until a human looks. The local stop price is updated
  only when IB confirms the change.
- **Fills come from IB.** Execution reports give price, quantity and time;
  commission reports give the cost. Both go to the ledger as real fills. The
  legacy `Trade` object's own idea of the close is not used for reporting, as
  in a realistic backtest.
- **`stop_is_working`** answers from order status events already received,
  not from polling all open orders every minute.

### Limits that apply to `live` mode in this milestone

Full risk management is milestone 3. Two hard limits ship now because orders
ship now: a maximum quantity per order and a maximum position value per algo,
both from the run's configuration, both refused before the order is built,
both with no default (a `live` algo without them is a configuration error).

### Starting and stopping

- **Start.** Before any algo runs, the program reads the account's open
  positions and open orders. If there is anything on a symbol an algo is about
  to trade that this program's stored runs do not explain, that algo is not
  started and the reason is printed. Nothing is cancelled or closed
  automatically.
- **Stop** (Ctrl+C or a stop file): no new entries; pending unfilled entries
  are cancelled; open positions stay open with their stop and target working
  at IB; the run is marked stopped, with its open positions listed.
- **Crash.** The run stays marked running. The next start reports it and
  applies the start rule above.

### Storing a run as it happens

The store writes during the run instead of once at the end (issue 70): order
events and fills as they occur, trades when they close, an equity point per
bar, indicator series per bar. A run gains the kinds `paper-live` and `live`
beside `backtest`, and a last-write time, so a reader can tell a run that is
alive from one that died.

Live bars are also written to disk in the offline file format, so every
session becomes replayable test data.

### Dashboard

Small additions, on the existing pages:

- The runs list shows running sessions first, with a "running" or "stale"
  badge from the last-write time, and refreshes itself while one is running.
- The run overview shows open positions (symbol, side, quantity, entry,
  current stop and target, unrealised result).
- The algo chart reloads the current day's bars and indicators on a timer
  while the run is live.

Push updates and a move to React are not part of this spec.

### `fetch-bars`

`python -m MainStoinker fetch-bars --symbols AAPL MSFT --start 2024-01-01
--end 2024-06-30 --out DIR` downloads one-minute bars through `IbMarketData`
(issue 46). It respects IB's limits on request size and pace, resumes after an
interruption, reports gaps, and writes one file per symbol per month. It
cannot place orders. `CsvBarFeed` learns to read a folder of monthly files and
to accept bars that are missing for one symbol at a timestamp, with a rule the
task split will propose (decision 6).

### Retiring the legacy runtime

Once stage 3 has passed its owner session: delete `Ticker`, the legacy loop in
`main_utils.py`, `apiApi.py`, the entry script, the launcher batch file, the
socket.io client and the mutable state in `Globals.py`. The pre-tool hook and
the tests that name the entry script are updated in the same change, which
needs owner review because it edits a guard. `Trade` loses its direct `ibapi`
branch; algos keep using it through the broker protocol.

## Testing

- **Fake IB client.** A test double that accepts the calls the new code makes
  and plays IB's callbacks from a script: history, live updates, order status
  sequences, partial fills, rejections, disconnects. Every order sequence is
  asserted call by call, for long and short.
- **Recorded sessions.** During each owner session the program writes every
  IB callback and every request to a file, with account numbers removed. The
  recordings are committed and replayed in tests, so the fake is checked
  against what IB really sent.
- **The existing guards stay.** Any real connection during a pytest run still
  raises. The port guard keeps its tests.
- **Parity.** A recorded live session replayed through `run_live` must give
  the same trades as the same bars through `run_backtest`.

## Stages

Each stage is split into tasks by the task divider and ends with an owner
session.

| Stage | Delivers | Owner session |
|---|---|---|
| 1. Data only | `IbConnection`, `IbMarketData`, `fetch-bars`, connection errors (issue 19), the fake client and the recorder | Run `fetch-bars` for a few symbols and a few months; any time, market closed is fine. About 15 minutes. |
| 2. Paper on live bars | `IbBarFeed`, `Engine.run_live`, the `run` command, incremental store, dashboard additions | During market hours: start a `paper` run of FunctionTest and EMACrossing, watch it trade in the dashboard for 30 minutes, stop it. |
| 3. Orders on the IB paper account | `IbOrders`, `IbBroker`, limits, start and stop rules, issue 6 and issue 15 | During market hours: one FunctionTest trade with stop and target in `live` mode on the paper port, a stop change, a manual stop of the program, a restart with a position open. |
| 4. Clean-up | Legacy runtime deleted, guards updated | None beyond review. |

Stage 1 gives more offline data early, which every later stage and the algo
work benefit from, and proves the connection with no order code present.

## Not in this spec

- Trading the real account, risk limits beyond the two above, kill switch,
  reconnect during a session, scheduling by market hours, running as a
  service, alerts: milestone 3.
- Algo fixes and tuning (issues 13, 48, 51).
- Dashboard push updates and React.

## Decisions needed

1. **Write the IB order code fresh, or move the legacy code.** The milestone 1
   spec said to move it unchanged and fix the hazards later. Recommended now:
   write `IbBroker` fresh against the broker protocol. The protocol, the
   ledger and the list of eight hazards did not exist then; moving code that
   is known to be wrong, then rewriting it, means two rounds of order-path
   review instead of one. The legacy code stays untouched and usable until
   stage 4.
2. **Stage order.** Recommended: data, then paper on live bars, then orders,
   as in the table.
3. **One engine thread fed by a queue.** Recommended, in place of a thread
   per ticker.
4. **Order shape.** Recommended: bracket orders with the stop as a child of
   the entry, stop and target good-till-cancelled, unfilled market entries
   cancelled after a configured number of seconds (the legacy code used two
   minutes).
5. **What happens to open positions when the program stops.** Recommended:
   leave them open with their stop and target working at IB. The alternative
   is to close everything at market on stop.
6. **Bars missing for one symbol.** Real multi-month data has gaps. Options:
   skip that symbol for that minute (recommended), or refuse the data set.
7. **About your setup**, needed before stage 1: TWS or IB Gateway; whether
   the trading machine is this machine; whether the paper account has market
   data for US stocks; and whether `STOINK_DB` should be set on the trading
   machine so sessions land in the same database as the backtests.
