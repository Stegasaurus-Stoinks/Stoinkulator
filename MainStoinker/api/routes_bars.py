"""`GET /api/runs/{run_id}/bars`: the 1-minute bars of one run's symbol, for the price chart.

The bars of a backtest are read from the run's recorded data directory, never
stored in the database. The bars of a live run (kind in `LIVE_KINDS`) are the
ones its engine consumed, read from the database (`run_bars`) with the same
window rules, limits and JSON shape; a live run with no bars yet has an empty
series. The symbol must be one of the run's own symbols, so nothing outside
the run's data files can be named. The window rules are `MainStoinker.api.windows`'
(default: the calendar day of the run's last bar); times are naive "data time",
formatted by `format_time` (`YYYY-MM-DD HH:MM:SS`); prices are JSON numbers
(floats) because the chart library needs numbers and bars are market data, not
ledger money. The directory path is never returned, in any field or error.
Reading the file is blocking work, so it runs inside the `with_store` callback, on the worker thread.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.api.windows import WindowError, format_time, parse_window
from MainStoinker.engine.feed import (
    bars_between,
    read_offline_bars,
    read_symbol_bars,
    symbol_fingerprint,
)
from MainStoinker.engine.run_documents import LIVE_KINDS
from MainStoinker.engine.store import DataMeta, Store

router = APIRouter()

MAX_BARS = 10_000

_PRICES = ["open", "high", "low", "close"]
_MAX_VOLUME = 2**53  # larger whole numbers are not exact in a JSON number


def _empty(symbol: str, reason: str, available: bool = False) -> dict[str, Any]:
    return {"symbol": symbol, "available": available, "reason": reason, "bars": []}


def _read(directory: Path, symbol: str) -> pd.DataFrame | None:
    """The bars of `symbol` (single file or month files), or None when absent or unreadable."""
    try:
        frame = read_symbol_bars(directory, symbol, read_offline_bars)
    except (OSError, ValueError):
        return None
    if not pd.api.types.is_datetime64_any_dtype(frame["date"]) \
            or isinstance(frame["date"].dtype, pd.DatetimeTZDtype):
        return None  # time-zone aware (or non-date) times: not data this API can serve
    return frame


def _servable(frame: pd.DataFrame) -> pd.DataFrame:
    """Only the rows whose prices and volume can be written as plain JSON numbers.

    A row is dropped when a price is not finite, or the volume is not a finite
    whole number in [0, 2**53).
    """
    numbers = frame[[*_PRICES, "volume"]].apply(pd.to_numeric, errors="coerce").astype(float)
    volume = numbers["volume"]
    keep = np.isfinite(numbers[_PRICES]).all(axis=1) & np.isfinite(volume) \
        & (volume >= 0) & (volume < _MAX_VOLUME) & (volume == np.floor(volume))
    return frame[keep.to_numpy()]


class Loaded(NamedTuple):
    """`load_window`'s answer: `frame` is None when the bars cannot be served (`reason` says why).

    `first` and `last` are the window asked for (as parsed, before any widening); None only for a live run
    that has no bar yet and was given no window.
    """

    frame: pd.DataFrame | None
    reason: str | None
    first: datetime | None
    last: datetime | None


def _no_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", *_PRICES, "volume"])


def _from_day_start(first: datetime, widen_days: int | None) -> datetime:
    """`first`, or midnight `widen_days` days before its day (an indicator's warm-up), if asked."""
    if widen_days is None:
        return first
    return datetime.combine(first.date() - timedelta(days=widen_days), time.min)


def _stored_bars(store: Store, run_id: int, symbol: str, data: DataMeta,
                 start: str | None, end: str | None, widen_days: int | None) -> Loaded:
    """A live run's bars come from the database (`run_bars`), under the same window rules.

    With no window the default day is the day of the run's last bar, else of the
    latest stored bar; a run with no bars yet has an empty series, not an error.
    """
    default_day = data.last_bar
    if default_day is None:
        default_day = max((b.time for b in store.load_last_bars(run_id)), default=None)
    try:
        if start is None and end is None and default_day is None:
            first = last = None
        else:
            first, last = parse_window(start, end, default_day=default_day)
    except WindowError as error:
        raise HTTPException(422, str(error)) from None
    if first is None or last is None:
        return Loaded(_no_bars(), None, None, None)
    asked = first
    first = _from_day_start(first, widen_days)

    def servable(limit: int | None) -> tuple[pd.DataFrame, int]:
        stored = store.load_bars(run_id, symbol, first, last, limit=limit)
        frame = pd.DataFrame(
            [(b.time, b.open, b.high, b.low, b.close, b.volume) for b in stored],
            columns=["date", *_PRICES, "volume"])
        frame["date"] = pd.to_datetime(frame["date"])
        return _servable(frame).reset_index(drop=True), len(stored)

    # One row more than the limit is enough to know the window is too wide; if rows that
    # cannot be served were among them, the whole window is read to count the others.
    window, read = servable(MAX_BARS + 1)
    if read > MAX_BARS and len(window) <= MAX_BARS:
        window, _ = servable(None)
    return Loaded(window, None, asked, last)


def load_window(store: Store, run_id: int, symbol: str, start: str | None, end: str | None,
                *, widen_days: int | None = None) -> Loaded:
    """The servable bars of a run's symbol in the window (see `Loaded`).

    The run, symbol and window rules, errors (404 before 422) and sources are the bars route's. `frame` is
    None when the bars cannot be served (`reason` then says why: "missing" or "changed"); otherwise it holds
    the rows in the window (it may be empty) and `reason` is "unverified" or None. With `widen_days` the bars
    are read from midnight that many days before the window's first day (up to the window's end), which an
    indicator needs to warm up; those rows are outside the window asked for. The caller applies the
    `MAX_BARS` limit (`check_width`) to the rows of the window asked for.
    """
    run = next((r for r in store.list_runs() if r.run_id == run_id), None)
    if run is None:
        raise NotFound("no such run")
    data: DataMeta = run.data
    if symbol not in data.symbols:
        raise NotFound("no such symbol in this run")
    if run.kind in LIVE_KINDS:
        return _stored_bars(store, run_id, symbol, data, start, end, widen_days)
    try:
        first, last = parse_window(start, end, default_day=data.last_bar)
    except WindowError as error:
        raise HTTPException(422, str(error)) from None
    asked, first = first, _from_day_start(first, widen_days)
    if not data.directory:
        return Loaded(None, "missing", asked, last)
    directory = Path(data.directory)
    reason: str | None = "unverified"
    recorded = next(((sha, size) for sym, sha, size in data.files if sym == symbol), None)
    if recorded is not None:
        try:
            current = symbol_fingerprint(directory, symbol)
        except (OSError, ValueError):  # absent, both layouts present, or not a plain ticker
            return Loaded(None, "missing", asked, last)
        if current != recorded:
            return Loaded(None, "changed", asked, last)
        reason = None
    frame = _read(directory, symbol)
    if frame is None:
        return Loaded(None, "missing", asked, last)
    try:
        window = bars_between(_servable(frame), first, last)
    except TypeError:  # dates that cannot be compared with a naive window
        return Loaded(None, "missing", asked, last)
    return Loaded(window, reason, asked, last)


def check_width(window: pd.DataFrame) -> None:
    if len(window) > MAX_BARS:
        raise HTTPException(422, f"window too wide: more than {MAX_BARS} bars")


def _bars(store: Store, run_id: int, symbol: str,
          start: str | None, end: str | None) -> dict[str, Any]:
    window, reason, _first, _last = load_window(store, run_id, symbol, start, end)
    if window is None:
        return _empty(symbol, reason or "missing")
    return _served(symbol, window, reason)


def _served(symbol: str, window: pd.DataFrame, reason: str | None) -> dict[str, Any]:
    """The response of a window of bars (already servable); shared by files and the store."""
    check_width(window)
    bars = [
        {"time": format_time(stamp.to_pydatetime()), "open": o, "high": h, "low": low, "close": c,
         "volume": v}
        for stamp, o, h, low, c, v in zip(
            window["date"], window["open"].astype(float), window["high"].astype(float),
            window["low"].astype(float), window["close"].astype(float),
            window["volume"].astype(float).astype("int64"), strict=True)
    ]
    return {"symbol": symbol, "available": True, "reason": reason, "bars": bars}


@router.get("/api/runs/{run_id}/bars")
async def run_bars(request: Request, run_id: int, symbol: str,
                   start: str | None = None, end: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _bars(store, run_id, symbol, start, end))
