"""Headless offline-backtest harness.

Two runners, both with no TWS connection, no front end and no wall-clock delay:

- `run_offline_backtest` (and the `run_mixed_` form) drives
  `MainStoinker.engine.Engine` over a `CsvBarFeed` reading ./OfflineData in the
  cwd, with a `SimBroker` and `NullSink`. This is the path behind the golden
  backtests.
- `run_legacy_offline_backtest` (and the `run_mixed_legacy_` form) drives the
  legacy runtime (Ticker -> ibapi.startData offline -> `replay_bars`). It is
  kept only as a parity guard while the live entry path still uses that
  runtime, and is removed in Phase B.

The legacy runtime keeps its state in module globals and a frozen Config
instance, which this harness resets by hand. Do not copy this pattern into
production code.
"""
from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from MainStoinker.engine.report import trade_records  # noqa: F401  (re-export)

REPO_ROOT = Path(__file__).resolve().parent.parent
OFFLINE_DATA = REPO_ROOT / "OfflineData"

# Mode flags every test run must have. LiveTrading / LiveData stay False so
# no code path can reach the broker.
SAFE_CONFIG = {
    "offline": True,
    "collectofflinedata": False,
    "LiveData": False,
    "LiveTrading": False,
    "FrontEndDisplay": False,
    "TimeDelayPerPoint": 0.0,
}


def apply_safe_config() -> None:
    from MainStoinker.MainStuff.Globals import config

    for key, value in SAFE_CONFIG.items():
        # Config is a frozen dataclass shared by reference across modules.
        object.__setattr__(config, key, value)


def reset_runtime_state() -> None:
    from MainStoinker.MainStuff import Globals

    Globals.tickers = {}
    Globals.algos = []
    Globals.updating = 1
    Globals.tickerIndex = 0
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("algo:"):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


def stage_offline_data(workdir: Path, symbols: list[str], max_rows: int | None = None) -> None:
    """Copy offline bar files into workdir, optionally truncated to max_rows."""
    target = workdir / "OfflineData"
    target.mkdir(exist_ok=True)
    for symbol in symbols:
        name = f"_{symbol}_offlinedata_"
        if max_rows is None:
            shutil.copy(OFFLINE_DATA / name, target / name)
        else:
            pd.read_csv(OFFLINE_DATA / name, index_col=0).head(max_rows).to_csv(target / name)


def run_offline_backtest(
    algo_id: str, algo_configs: list[dict], warmup_days: int = 2, *, broker=None, ledger=None
) -> list:
    """Run algo_id over offline data for each config, through the Engine.

    Returns the algo objects in config order. The current working directory
    must contain OfflineData/ (see stage_offline_data). `broker` defaults to a
    fresh SimBroker per call (the engine refuses to run without one). `ledger`
    is described on `run_mixed_offline_backtest`.
    """
    return run_mixed_offline_backtest(
        [(algo_id, cfg) for cfg in algo_configs], warmup_days, broker=broker, ledger=ledger
    )


def run_mixed_offline_backtest(
    specs: Sequence[tuple[str, dict]],
    warmup_days: int = 2,
    *,
    broker=None,
    sink=None,
    wrap: Callable[[object], None] | None = None,
    ledger=None,
) -> list:
    """Engine run over (algo_id, cfg) pairs; algos come back in `specs` order.

    `sink` defaults to NullSink. `wrap`, if given, is called on each algo after
    it is built and before the engine takes it (to spy on `update`, say).

    `ledger`, if given (an empty `Ledger`), is filled by the run: an account
    named after the algo (capital 100000, notional) is opened and the default
    SimBroker reports to it, and the engine marks and finishes the ledger.
    Single algo only, and not together with `broker`: both raise `ValueError`.
    """
    if ledger is not None:
        if len(specs) != 1:
            raise ValueError("ledger= supports a single algo only")
        if broker is not None:
            raise ValueError("ledger= cannot be combined with broker=")
    from MainStoinker.engine.algos import load_algo
    from MainStoinker.engine.broker import SimBroker
    from MainStoinker.engine.engine import Engine, EngineConfig
    from MainStoinker.engine.feed import CsvBarFeed
    from MainStoinker.engine.sink import NullSink

    apply_safe_config()
    reset_runtime_state()

    algos = [load_algo(algo_id, cfg) for algo_id, cfg in specs]
    if wrap is not None:
        for algo in algos:
            wrap(algo)
    # First-appearance order, matching the legacy Ticker index order.
    symbols = list(dict.fromkeys(cfg["ticker"] for _, cfg in specs))
    feed = CsvBarFeed(Path.cwd() / "OfflineData", symbols, warmup_days)
    if ledger is not None:
        (only,) = algos
        broker = SimBroker(account=ledger.open_account(only.name, only.ticker, 100_000))
    engine = Engine(
        EngineConfig(),
        feed,
        broker if broker is not None else SimBroker(),
        sink if sink is not None else NullSink(),
        algos,
        ledger=ledger,
    )
    engine.run_backtest()
    return algos


@dataclass
class PaperRun:
    """What `run_paper_backtest` returns.

    `violations` lists every per-bar disagreement found between the algo's own
    view (`inTrade`) and the broker's position state, and `max_live` is the most
    live positions (pending or open) the algo ever had at once. `bars_checked`
    is the number of `update` calls checked (one per post-warm-up bar).
    """

    ledger: Any
    broker: Any
    algo: Any
    capital: Decimal
    violations: list[str] = field(default_factory=list)
    bars_checked: int = 0
    max_live: int = 0


def view_problem(
    in_trade: bool, current_state: str | None, unsettled_states: list[str]
) -> tuple[str | None, int]:
    """The per-bar consistency rules of `run_paper_backtest`, as a pure function.

    Returns `(problem or None, live_count)`. Rules: (1) `in_trade` is True
    exactly when the broker state of the algo's current trade is `pending` or
    `open`; (2) at most one position is live, where only `pending` and `open`
    count as live. An `exiting` position alongside a new `pending` one on the
    same bar is legitimate under PaperBroker's rules, so `exiting` is not live.
    """
    live = sum(1 for s in unsettled_states if s in ("pending", "open"))
    flat_broker = current_state not in ("pending", "open")
    if (not in_trade) != flat_broker or live > 1:
        return f"inTrade={in_trade} state={current_state} live={live}", live
    return None, live


def run_paper_backtest(
    algo_id: str,
    cfg: dict,
    costs=None,
    capital: float | int | str | Decimal = 10_000,
    warmup_days: int = 2,
) -> PaperRun:
    """Engine run of one algo through a `PaperBroker` and a `Ledger`.

    One account (named after the algo), one `PaperBroker` behind a
    `MappingRouter`, and the broker's cost model as `end_commission`. The
    current working directory must contain OfflineData/. After every
    `algo.update` the algo's `inTrade` is compared with the broker's state for
    its current trade (see `PaperRun`); the engine is not modified for that,
    `update` is wrapped on the algo instance.
    """
    from MainStoinker.engine.algos import load_algo
    from MainStoinker.engine.engine import Engine, EngineConfig
    from MainStoinker.engine.feed import CsvBarFeed
    from MainStoinker.engine.ledger import Ledger
    from MainStoinker.engine.paper_broker import PaperBroker
    from MainStoinker.engine.router import MappingRouter
    from MainStoinker.engine.sink import NullSink

    apply_safe_config()
    reset_runtime_state()

    algo = load_algo(algo_id, cfg)
    ledger = Ledger()
    broker = PaperBroker(ledger.open_account(algo.name, algo.ticker, capital), costs)
    run = PaperRun(ledger, broker, algo, Decimal(str(capital)))
    unsettled: list = []  # refs of trades not yet closed or cancelled

    def check() -> None:
        run.bars_checked += 1
        trades = algo.trades
        unsettled.extend(t.brokerRef for t in trades[len(run_seen) :])
        run_seen[:] = trades
        states = {ref: broker.position_state(ref) for ref in unsettled}
        unsettled[:] = [r for r, s in states.items() if s not in ("closed", "cancelled")]
        current = broker.position_state(trades[-1].brokerRef) if trades else None
        problem, live = view_problem(algo.inTrade, current, list(states.values()))
        run.max_live = max(run.max_live, live)
        if problem:
            run.violations.append(f"bar {run.bars_checked}: {problem}")

    run_seen: list = []
    original_update = algo.update

    def update(frame):
        result = original_update(frame)
        check()
        return result

    algo.update = update

    feed = CsvBarFeed(Path.cwd() / "OfflineData", [algo.ticker], warmup_days)
    brokers = {algo.name: broker}
    engine = Engine(
        EngineConfig(),
        feed,
        MappingRouter(brokers),
        NullSink(),
        [algo],
        ledger=ledger,
        end_commission=lambda idname, quantity: brokers[idname].costs.commission(quantity),
    )
    engine.run_backtest()
    return run


def run_legacy_offline_backtest(
    algo_id: str, algo_configs: list[dict], warmup_days: int = 2, *, broker=None
) -> list:
    """Run algo_id over offline data for each config on the LEGACY runtime path.

    Exists only as a parity guard for the legacy runtime, which the live entry
    path still uses; it is removed in Phase B. Returns the algo objects.

    The current working directory must contain OfflineData/ (see
    stage_offline_data); the runtime also writes its log and CSV output there.

    If `broker` is given it is set as `algo.broker` on every algo before the
    replay, so their trades report to it. Without it the legacy path runs.
    """
    return run_mixed_legacy_offline_backtest(
        [(algo_id, cfg) for cfg in algo_configs], warmup_days, broker=broker
    )


def run_mixed_legacy_offline_backtest(
    specs: Sequence[tuple[str, dict]],
    warmup_days: int = 2,
    *,
    broker=None,
    wrap: Callable[[object], None] | None = None,
) -> list:
    """Legacy path over (algo_id, cfg) pairs. `wrap` is applied after the broker
    is set and before the algo is registered on its Ticker."""
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.MainStuff import Globals
    from MainStoinker.MainStuff import main_utils as utils
    from MainStoinker.TradeTools.ticker import Ticker

    apply_safe_config()
    reset_runtime_state()

    by_symbol: dict[str, Ticker] = {}
    algos = []
    for algo_id, cfg in specs:
        ticker = by_symbol.get(cfg["ticker"])
        if ticker is None:
            ticker = Ticker(cfg["ticker"], len(by_symbol))
            by_symbol[cfg["ticker"]] = ticker
        algo = utils.algo_starter(algo_id, cfg)
        if broker is not None:
            algo.broker = broker
        if wrap is not None:
            wrap(algo)
        ticker.register_algo(algo)
        algos.append(algo)

    Globals.tickers = {t.index: t for t in by_symbol.values()}
    Globals.algos = algos

    ibapi.startData(Globals.tickers, algos, warmup_days, {0: threading.Event()}, 1)
    replay_bars(ibapi.simulatedDatadict)
    return algos


def replay_bars(simulated: dict) -> None:
    """The bar loop of main_utils.backtesting_data_blast, minus sleeps,
    front-end pushes and the end-of-run CSV dump."""
    from MainStoinker.MainStuff import Globals

    start = Globals.tickers[0].data.shape[0]
    total = simulated[Globals.tickers[0].index].shape[0] - start
    for k in range(total):
        for ticker in Globals.tickers.values():
            ticker.append(simulated[ticker.index].iloc[start + k])
        for ticker in Globals.tickers.values():
            ticker.update_algos()
