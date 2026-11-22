# Milestone 1 spec: core seams

Status: Phase A approved by the owner on 2026-11-21. Phase B is deferred until
the owner can run a paper-trading session. See "Decisions" at the end.

## Goal

Make the engine runnable and testable without globals, a broker connection or
a front end, so that backtests are fast and repeatable and later milestones
(per-algo paper/live modes, persistence, autonomy, dashboard) have something
solid to build on.

Done means:

1. An offline backtest runs through a new `Engine` with every dependency
   passed in. Engine modules read and write no module-level state. (Algo
   imports still construct the legacy `ibapi` and front-end objects, without
   connecting, until Phase B removes them.)
2. All six golden backtests produce byte-identical files through the new
   engine. Algo files do not change.
3. A backtest of the committed data runs with no sleeps and no socket, from a
   command line, and writes its trades to a file.
4. The live path uses the same `Engine`, `Trade` and algo code as the
   backtest path, differing only in which broker and data feed are plugged in.

## Where the coupling is today

Measured on `revival`:

| Module | Depends on |
|---|---|
| Algos (`*_Algo.py`) | Nothing global. They use only `ParentAlgo` and the `Trade` they are handed. |
| `ParentAlgo` | Constructs `Trade` directly; `main_utils.create_logger` (file handler in the current directory). |
| `Trade` | Module-level `ibapi` (17 uses: order ids, `placeOrder`, stop, take-profit, open orders) and `config.LiveTrading`. |
| `Ticker` | Module-level `ibapi` (`readOrders`, `firstdataofminute`), the front-end client, `config.LiveData`. |
| `apiApi.IBapi` | Everything: broker connection, order helpers, historical and live bars, offline CSV loading, front-end pushes, `Globals.tickers`. |
| `main_utils` | `Globals`, the front-end client, the backtest bar loop with a sleep per bar. |

The algos are already clean. The work is in `Trade`, `Ticker`, `IBapi` and the
run loop.

What algos call on a trade, which is the surface that must keep working:
`set_stopPrice`, `set_stopLossType`, `set_stopDelta`, `check_stoploss`,
`check_tp`, `create_tp`, `close_position`, `get_stats`, and the attributes
`stopPrice`, `tp`, `openTime`.

## Design

New code lives in a new subpackage, `MainStoinker/engine/`. Legacy modules are
changed as little as possible and deleted only once nothing uses them.

### Interfaces

Small `typing.Protocol` classes, each with a real and a test implementation.

- **`Broker`**: what `Trade` needs. Open a position (market or limit entry
  together with its initial stop), modify the stop, place or modify a
  take-profit, close the position, and report whether the stop is still
  working. Order ids are
  the broker's business and do not appear in `Trade`.
  - `SimBroker`: fills exactly as the current non-live code does (at the price
    the caller passes, immediately). It records every call, so tests can
    assert on the sequence. A better fill model is milestone 2.
  - `IbkrBroker`: the existing order code from `Trade` and `IBapi`, moved, not
    rewritten.
- **`BarFeed`**: yields warm-up bars, then one batch per timestamp holding
  the bar for every symbol. The Phase A protocol is for backtests; a
  push-based live feed is designed in Phase B.
  - `CsvBarFeed`: reads the offline files. Deterministic.
  - `IbkrBarFeed`: wraps the existing historical and live callbacks.
- **`EventSink`**: receives "bar closed", "intrabar update" and "algo data"
  events.
  - `NullSink` for tests and plain backtests; `SocketIoSink` wrapping the
    existing front-end client; a recording sink for tests.

### Engine

`Engine(config, feed, broker, sink, algos)` owns the tickers and the run loop.

- `run_backtest()` is a plain synchronous loop: for each timestamp, append the
  bar for every symbol, then update every algo. No sleep, no threads.
- `run_live()` is driven by feed callbacks and keeps today's threading model
  for now. Changing it is milestone 3.
- The engine gives each algo its broker when it registers the algo, so the
  algo constructor signature `Algo(algoConfigData)` does not change.
  `ParentAlgo.open_trade` passes that broker to `Trade`.
- Loggers are created by the engine and handed to algos; no file handler is
  opened in the current directory unless the config says where.

### Config

One explicit, immutable `Config` object passed to the engine. `Globals.py`
stays as-is for the legacy entry script until that script is switched over,
then its mutable state is removed.

### Performance

`Ticker.append` rebuilds the whole DataFrame for every bar. Measured on the
committed data that is about a third of a fast algo's run time; it only
dominates from roughly two months of 1-minute bars. Most of the remaining cost
is per-bar pandas work inside the algo files, which Phase A does not touch. The feed will hold the
full history once and hand algos a growing view of it. Algos still receive a
DataFrame with the same columns and values. The Elliot rewrite (issue 11) is
a separate task inside this milestone.

### Command line

`python -m MainStoinker backtest --algos <json> --data <dir> --out <dir>`
runs an offline backtest and writes a trades file. It can never connect to a
broker: it only constructs `CsvBarFeed` and `SimBroker`.

## Phases

Each phase leaves `revival` green with identical goldens. The task-divider
splits each into tasks.

**Phase A: backtest side.** Nothing that is sent to the broker changes. The
legacy `Ticker`, `main_utils.backtesting_data_blast`, `apiApi.py`,
`Globals.py` and the entry script are not modified, because the live path uses
them; they are retired in Phase B. Only A3 touches order-path code.

- **A1** Tests that pin today's live order sequence, using a fake `ibapi`
  object (pulled forward from Phase B step 7 as the safety net for A3).
- **A2** `MainStoinker/engine/` package: `Broker` protocol and a record-only
  `SimBroker`; a test that the engine package imports no legacy or broker
  modules.
- **A3** `Trade` takes an optional `broker`. When one is given it is used and
  the live branch cannot be reached; when none is given `Trade` behaves
  exactly as today. Additions only inside `trade.py`.
- **A4** `BarFeed` protocol and `CsvBarFeed` (per-timestamp batches; symbols
  whose timestamps do not line up raise an error); `EventSink` protocol with
  `NullSink` and a recording sink.
- **A5** `Engine.run_backtest()` with its own per-symbol bar store and an algo
  loader.
- **A6** The test harness runs through the engine. The legacy loop is kept
  under another name and compared, read-only, against all six goldens in the
  slow suite.
- **A7** Linear-time bar storage.
- **A8** The engine creates algo loggers, so no log file opens in the current
  directory.
- **A9** Command line: `python -m MainStoinker backtest --algos <json>
  --data <dir> --out <dir>`, writing `trades.json` in the golden record shape.

Then, in this order: issue 11 (Elliot incremental), issue 8 (TrendLine
golden), issue 12, issue 14, issue 16.

Byte-identity of goldens is checked by regenerating them and running
`git diff --exit-code tests/golden`; the test fixture alone compares parsed
JSON.

**Phase B: live side.** Order-path changes; every pull request needs owner
review.

7. Extend the A1 characterization tests to the rest of the live path:
   `Ticker` (order polling, the partial last bar), the data callbacks and the
   entry script's sequence.
8. `IbkrBroker`: move the order code out of `Trade` behind the protocol. The
   recorded sequences from step 7 must not change.
9. `IbkrBarFeed` and `Engine.run_live()`. The entry script becomes a thin
   launcher that builds the engine.
10. Remove the mutable state from `Globals.py` and the module-level `ibapi`
    and front-end instances.

Phase B cannot be fully verified by agents, because agents never connect to a
broker. Before step 9 merges, the owner runs a paper-trading session against
TWS on the paper port and confirms: connection, warm-up bars, live bars, one
FunctionTest trade with stop and take-profit, and a clean shutdown.

## Not in this milestone

- Fill model, slippage, commission (milestone 2).
- Per-algo backtest / paper / live modes; persistence; P&L fixes, issues 3
  to 7 (milestone 2).
- Fixing the live-order hazards in issue 15. Phase B moves that code without
  changing its behaviour, so the hazards move with it and are fixed in
  milestone 2 against the characterization tests.
- Reconnect, scheduling, risk limits (milestone 3). Dashboard (milestone 4).

## Constraints

- Golden files change only where a task says so and explains the diff.
- No new third-party dependencies.
- The three guards from issue 10 stay in force. The port guard moves with the
  connection code and keeps its tests.
- `dev` and `master` stay frozen until the owner merges `revival` into `master`.

## Decisions

Made by the owner on 2026-11-21:

1. **Phase A now, Phase B later.** Phase A (the backtest side) is approved.
   Phase B (the live side) starts only when the owner has time to run the
   paper-trading session it needs.
2. **Package name.** Keep `MainStoinker`; new code goes in
   `MainStoinker/engine/`.
3. **Branches.** `dev` stays untouched. `revival` is expected to merge
   directly into `master` when the work is done, so keeping `revival`
   mergeable with `dev` is not a goal.
