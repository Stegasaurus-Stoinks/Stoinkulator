"""Headless offline-backtest harness.

Drives the *existing* runtime path (Globals -> ibapi.startData offline ->
main_utils.backtesting_data_blast) with no TWS connection, no front end and
no wall-clock delay, so tests characterise what the system does today.

The runtime keeps its state in module globals and a frozen Config instance.
Until milestone 1 replaces those with injectable objects, this harness resets
them by hand. Do not copy this pattern into production code.
"""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

import pandas as pd

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


def run_offline_backtest(algo_id: str, algo_configs: list[dict], warmup_days: int = 2) -> list:
    """Run algo_id over offline data for each config. Returns the algo objects.

    The current working directory must contain OfflineData/ (see
    stage_offline_data); the runtime also writes its log and CSV output there.
    """
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.MainStuff import Globals
    from MainStoinker.MainStuff import main_utils as utils
    from MainStoinker.TradeTools.ticker import Ticker

    apply_safe_config()
    reset_runtime_state()

    by_symbol: dict[str, Ticker] = {}
    algos = []
    for cfg in algo_configs:
        ticker = by_symbol.get(cfg["ticker"])
        if ticker is None:
            ticker = Ticker(cfg["ticker"], len(by_symbol))
            by_symbol[cfg["ticker"]] = ticker
        algo = utils.algo_starter(algo_id, cfg)
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


def trade_records(algo) -> list[dict]:
    """Plain, comparable record of each trade an algo made."""
    records = []
    for trade in algo.trades:
        records.append(
            {
                "id": trade.tradeID,
                "symbol": trade.symbol,
                "volume": trade.volume,
                "direction": int(trade.direction),
                "openTime": str(trade.openTime),
                "openPrice": round(float(trade.openPrice), 4),
                "closeTime": str(getattr(trade, "closeTime", None)),
                "closePrice": (
                    round(float(trade.closePrice), 4) if hasattr(trade, "closePrice") else None
                ),
                "status": getattr(trade, "status", None),
            }
        )
    return records
