# Milestone 2 spec: results you can trust and keep

Status: approved by the owner on 2026-11-21, except the choice of database
(decision 1), which is still open. See "Decisions" at the end.

## Goal

After milestone 1 the engine can run a backtest, but its output is a list of
trade records taken from the legacy `Trade` objects. Fills are optimistic
(entry at the signal bar's close, stops filled at the stop price), there is no
commission, profit is not computed correctly, nothing is stored, and every algo
runs the same way. Milestone 2 makes the numbers trustworthy and durable, which
is what the dashboard (milestone 4) will display.

Done means:

1. A run produces an account of what happened that does not depend on the
   legacy `Trade` bookkeeping: every order, every fill, every round-trip trade
   with correct profit, and an equity curve per algo and for the whole account.
2. A realistic simulated broker is available alongside the current one: no
   look-ahead on entries, stops and targets triggered inside the bar,
   commission and slippage.
3. Every run is stored in a database and can be listed, inspected and compared
   later, from the command line.
4. Each algo in the config has a mode, and the engine routes it to the right
   broker. Only modes that cannot reach a real broker are accepted until the
   live side exists.
5. The known profit and status defects in the legacy `Trade` (issues 3, 4, 5,
   7) are fixed.

## What stays as it is

- The live program and `Ticker`, `apiApi.py`, `Globals.py`, the entry script.
- `SimBroker` and every existing golden file. The realistic broker is a new
  class with its own goldens.
- The algos. No strategy change in this milestone.

## Design

All new code is in `MainStoinker/engine/`, with dependencies passed in.

### Ledger

`engine/ledger.py`: pure accounting, no pandas, no I/O. It consumes order and
fill events and maintains, per algo and for the account:

- positions (quantity, average price), cash, realised and unrealised profit;
- round-trip trades: side, quantity, entry and exit fills, gross profit,
  commission, net profit, duration, how it was closed (target, stop, algo
  exit, end of data);
- an equity point per bar: cash plus position value at the bar's close.

Profit is `(exit - entry) x quantity` for longs and `(entry - exit) x quantity`
for shorts, minus commission. Every algo starts from a configured amount of
capital so equity curves are comparable. Money is held as integer cents or
`Decimal`, not float.

### PaperBroker

`engine/paper_broker.py` implements the `Broker` protocol and owns the
simulated orders. The engine tells it about each bar before the algos see it
(the protocol gains an optional `on_bar(symbol, bar)`).

- **Entries.** A market entry requested while processing bar *t* fills at the
  open of bar *t+1*. This removes the look-ahead of filling at the close that
  produced the signal, and matches what a live market order would get.
- **Stops and targets.** They rest at the broker and are checked against each
  bar's high and low. A stop fills at the stop price, or at the bar's open if
  the bar gapped through it. If a bar's range covers both stop and target, the
  stop is assumed to have filled first.
- **Costs.** Commission per share with a minimum per order, and slippage per
  share on market fills. Both configurable.
- **Closing.** The broker is the authority on whether a position is open. A
  close request for a position the broker already closed is ignored and
  counted, since the legacy `Trade` also runs its own checks on the bar close.
- Every order and fill goes to the ledger.

`Trade` already asks the broker whether its stop is still working and stands
down when it is not, so no change to `trade.py` is needed for this.

The existing `SimBroker` remains the "legacy fills" option. It also reports
its fills to the ledger, so both brokers produce the same kind of record.

### Store

`engine/store.py`: a `Store` protocol with `SqliteStore` and `NullStore`.
SQLite through Python's standard library: one file, no server, no new
dependency, readable by the dashboard while a run is writing (WAL mode).

Tables: `runs` (id, start and end time, kind, config, code version), `algos`
(run, idname, algo id, ticker, mode, parameters, starting capital), `orders`,
`fills`, `trades`, `equity` (run, algo, bar time, equity, cash, position
value). Bars are not copied into the database. The schema carries a version
number and a migration step from the first release.

### Modes

Each config entry may carry `"mode": "backtest" | "paper" | "live"` (default
`backtest`). The engine takes a small router that maps an algo to its broker.

- `backtest` and `paper` both use a simulated broker. They differ in the data
  they run on: stored bars now, live bars once the live data feed exists.
- `live` is refused with a clear message until the live broker exists
  (milestone 1 Phase B). The config format, router and database all carry the
  mode already, so nothing has to be redesigned when it arrives.

### Command line

- `backtest` gains `--db <file>`, `--fills legacy|realistic`, `--capital`,
  commission and slippage options. `trades.json` is still written.
- `runs` lists stored runs. `report <run>` prints, per algo and for the
  account: number of trades, win rate, net profit, average win and loss,
  largest drawdown, and final equity.

### Legacy `Trade` fixes (order-path file, owner review)

Issues 3, 4, 5 and 7: profit sign for shorts and share count, `to_json` in
simulated mode, status on an open simulated trade, `printStats` and
`update_tp` calling things that do not exist. These touch `trade.py` and
`ParentAlgo.py`. The 42 live-order tests from milestone 1 must pass unedited,
and the trade goldens change only where a recorded value was wrong.

## Phases

**Phase A, no broker connection needed (this spec).**

1. Ledger, with tests that pin the arithmetic.
2. Brokers report to the ledger; `SimBroker` runs produce a ledger whose
   trades match the existing goldens.
3. `PaperBroker` with the fill model, its own goldens for every algo.
4. Store and schema.
5. Modes in the config, router, engine integration.
6. Command line: `--db`, `runs`, `report`.
7. Legacy `Trade` fixes.

**Phase B, needs the live side.** Recording real broker fills in the ledger
(issue 6), the live-order hazards (issue 15), `live` mode, paper trading on
live data.

## Suggested order of milestones from here

The live side needs the owner's paper-trading session, and everything the
dashboard shows comes from the store. So:

1. Milestone 2 Phase A (this).
2. Milestone 4, API and dashboard, on stored backtest runs.
3. Milestone 1 Phase B and milestone 2 Phase B, the live side, when the owner
   can run the paper session. The dashboard then shows paper and live runs
   with no further design work.
4. Milestone 3, unattended operation.

## Decisions

1. **Database: open.** SQLite file is the working assumption; the owner has
   asked what it is for and has not yet chosen. Only the store task depends on
   this.
2. **Order of milestones: approved (2026-11-21).** Dashboard before the live
   side, as listed above.
3. **Fill model defaults: approved (2026-11-21).** Entry at the next bar's
   open; the stop is assumed to fill first when a bar covers both stop and
   target.
4. **Costs and capital: approved (2026-11-21).** Commission 0.005 per share
   with a 1.00 minimum per order, no slippage, 10,000 starting capital per
   algo. All configurable; the commission figures are still to be checked
   against the account's actual IBKR pricing plan.
