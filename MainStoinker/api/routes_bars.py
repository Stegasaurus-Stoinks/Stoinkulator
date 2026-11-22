"""`GET /api/runs/{run_id}/bars`: the 1-minute bars of one run's symbol, for the price chart.

The bars are read from the run's recorded data directory, never stored in the
database. The symbol must be one of the run's own symbols, so nothing outside
the run's data files can be named. The window rules are `MainStoinker.api.windows`'
(default: the calendar day of the run's last bar); times are naive "data time",
formatted by `format_time` (`YYYY-MM-DD HH:MM:SS`); prices are JSON numbers
(floats) because the chart library needs numbers and bars are market data, not
ledger money. The directory path is never returned, in any field or error.
Reading the file is blocking work, so it runs inside the `with_store` callback, on the worker thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.api.windows import WindowError, format_time, parse_window
from MainStoinker.engine.feed import (
    bars_between,
    file_fingerprint,
    offline_data_path,
    read_offline_bars,
)
from MainStoinker.engine.store import DataMeta, Store

router = APIRouter()

MAX_BARS = 10_000

_PRICES = ["open", "high", "low", "close"]


def _empty(symbol: str, reason: str, available: bool = False) -> dict[str, Any]:
    return {"symbol": symbol, "available": available, "reason": reason, "bars": []}


def _read(path: Path) -> pd.DataFrame | None:
    """The bar file, or None when it is absent or unreadable."""
    try:
        return read_offline_bars(path)
    except (OSError, ValueError):
        return None


def _bars(store: Store, run_id: int, symbol: str,
          start: str | None, end: str | None) -> dict[str, Any]:
    run = next((r for r in store.list_runs() if r.run_id == run_id), None)
    if run is None:
        raise NotFound("no such run")
    data: DataMeta = run.data
    if symbol not in data.symbols:
        raise NotFound("no such symbol in this run")
    try:
        lo, hi = parse_window(start, end, default_day=data.last_bar)
    except WindowError as error:
        raise HTTPException(422, str(error)) from None
    if not data.directory:
        return _empty(symbol, "missing")
    path = offline_data_path(Path(data.directory), symbol)
    reason: str | None = "unverified"
    recorded = next(((sha, size) for sym, sha, size in data.files if sym == symbol), None)
    if recorded is not None:
        try:
            current = file_fingerprint(path)
        except OSError:
            return _empty(symbol, "missing")
        if current != recorded:
            return _empty(symbol, "changed")
        reason = None
    frame = _read(path)
    if frame is None:
        return _empty(symbol, "missing")
    frame = frame.dropna(subset=[*_PRICES, "volume"])
    window = bars_between(frame, lo, hi)
    if len(window) > MAX_BARS:
        raise HTTPException(422, f"window too wide: more than {MAX_BARS} bars")
    bars = [
        {"time": format_time(stamp.to_pydatetime()), "open": o, "high": h, "low": lo, "close": c, "volume": v}
        for stamp, o, h, lo, c, v in zip(
            window["date"], window["open"].astype(float), window["high"].astype(float),
            window["low"].astype(float), window["close"].astype(float),
            window["volume"].astype(int), strict=True)
    ]
    return {"symbol": symbol, "available": True, "reason": reason, "bars": bars}


@router.get("/api/runs/{run_id}/bars")
async def run_bars(request: Request, run_id: int, symbol: str,
                   start: str | None = None, end: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _bars(store, run_id, symbol, start, end))
