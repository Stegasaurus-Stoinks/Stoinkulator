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
from pathlib import Path

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
    algo_id: str, algo_configs: list[dict], warmup_days: int = 2, *, broker=None
) -> list:
    """Run algo_id over offline data for each config, through the Engine.

    Returns the algo objects in config order. The current working directory
    must contain OfflineData/ (see stage_offline_data). `broker` defaults to a
    fresh SimBroker per call (the engine refuses to run without one).
    """
    return run_mixed_offline_backtest(
        [(algo_id, cfg) for cfg in algo_configs], warmup_days, broker=broker
    )


def run_mixed_offline_backtest(
    specs: Sequence[tuple[str, dict]],
    warmup_days: int = 2,
    *,
    broker=None,
    sink=None,
    wrap: Callable[[object], None] | None = None,
) -> list:
    """Engine run over (algo_id, cfg) pairs; algos come back in `specs` order.

    `sink` defaults to NullSink. `wrap`, if given, is called on each algo after
    it is built and before the engine takes it (to spy on `update`, say).
    """
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
    engine = Engine(
        EngineConfig(),
        feed,
        broker if broker is not None else SimBroker(),
        sink if sink is not None else NullSink(),
        algos,
    )
    engine.run_backtest()
    return algos


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
