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
   passed in. No module-level state is read or written on that path.
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

- **`Broker`**: what `Trade` needs. Open an entry (market or limit), place a
  stop, modify a stop, place or modify a take-profit, close a position, and
  report whether a given stop order is still open or has filled. Order ids are
  the broker's business and do not appear in `Trade`.
  - `SimBroker`: fills exactly as the current non-live code does (at the price
    the caller passes, immediately). It records every call, so tests can
    assert on the sequence. A better fill model is milestone 2.
  - `IbkrBroker`: the existing order code from `Trade` and `IBapi`, moved, not
    rewritten.
- **`BarFeed`**: yields warm-up bars, then bars one at a time per symbol.
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

`Ticker.append` rebuilds the whole DataFrame for every bar, so a run costs
time proportional to the square of the number of bars. The feed will hold the
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

**Phase A: backtest side.** No change to what is sent to the broker.

1. `Broker` protocol and `SimBroker`. `Trade` takes a broker argument; when
   none is given it behaves exactly as today. Goldens identical.
2. `BarFeed` protocol and `CsvBarFeed`.
3. `EventSink` protocol, `NullSink`, `SocketIoSink`. `Ticker` publishes
   through a sink and no longer imports the front-end client or `ibapi`.
4. `Engine.run_backtest()`. The test harness switches to it. The legacy
   backtest loop in `main_utils` is deleted.
5. Linear-time bar storage. Command line entry point.
6. Elliot made incremental (issue 11). Issues 8, 12, 14 and 16 fit here too.

**Phase B: live side.** Order-path changes; every pull request needs owner
review.

7. Characterization first: with a fake `ibapi` object, tests that record the
   exact sequence and fields of `placeOrder` calls for a long and a short
   trade through entry, stop change, take-profit and close, on today's code.
   This is the safety net for step 8 and covers issue 15 item 8.
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
