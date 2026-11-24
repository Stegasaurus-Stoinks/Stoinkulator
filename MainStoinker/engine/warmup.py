"""Warm-up history for a live session, read from the month files before today.

`live_warmup` reads only the data folder (never the current directory) and
imports nothing outside `MainStoinker.engine`. Data time is naive
`America/New_York` wall-clock time, so `today` is the New York date.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from MainStoinker.engine.feed import (
    COLUMNS,
    read_symbol_bars,
    symbol_bar_files,
    symbol_fingerprint,
)


def previous_weekday(today: date) -> date:
    """The weekday before `today` (Monday gives the Friday before)."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def live_warmup(
    bars_dir: Path,
    symbols: Sequence[str],
    today: date,
    trading_days: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any], list[str]]:
    """Warm-up frames for `symbols`: the last `trading_days` days before `today`.

    Returns `(frames, description, warnings)`.

    `frames` maps each symbol to a frame shaped like `CsvBarFeed.warmup`:
    columns `COLUMNS`, RangeIndex, `date` as datetime64, rows in file order,
    only rows dated strictly before `today` and only the last `trading_days`
    distinct dates that have rows.

    `description` is `{"today": "YYYY-MM-DD", "trading_days": N, "symbols":
    {symbol: {"files": [names], "sha256": ..., "size": ..., "days": [dates],
    "rows": n, "first_bar": ts, "last_bar": ts}}}` with timestamps as
    `YYYY-MM-DD HH:MM:SS` strings; `sha256`/`size` are `symbol_fingerprint`.

    `warnings` lists, per symbol, a last warm-up day that is not the previous
    weekday and fewer than `trading_days` days of history.

    Raises `FileNotFoundError` for a symbol without bar files, and
    `ValueError` for a symbol with no rows before `today`, `trading_days < 1`,
    no symbols, or any error `read_symbol_bars` raises.
    """
    if trading_days < 1:
        raise ValueError(f"trading_days must be at least 1, got {trading_days}")
    names = list(dict.fromkeys(symbols))
    if not names:
        raise ValueError("live_warmup needs at least one symbol")
    cutoff = pd.Timestamp(today)
    expected = previous_weekday(today)
    frames: dict[str, pd.DataFrame] = {}
    described: dict[str, Any] = {}
    warnings: list[str] = []
    for symbol in names:
        files = symbol_bar_files(bars_dir, symbol)
        data = read_symbol_bars(bars_dir, symbol)
        before = data.loc[data["date"] < cutoff]
        if before.empty:
            raise ValueError(f"symbol {symbol}: no bars before {today.isoformat()}")
        days = sorted(before["date"].dt.normalize().unique())
        keep = days[-trading_days:]
        frame = before.loc[before["date"].dt.normalize().isin(keep)].reset_index(drop=True)
        frames[symbol] = frame[list(COLUMNS)]
        last_day = pd.Timestamp(keep[-1]).date()
        if last_day != expected:
            warnings.append(
                f"symbol {symbol}: last warm-up day is {last_day.isoformat()}, "
                f"expected the previous weekday {expected.isoformat()}"
            )
        if len(keep) < trading_days:
            warnings.append(
                f"symbol {symbol}: only {len(keep)} trading days of warm-up, wanted {trading_days}"
            )
        sha, size = symbol_fingerprint(bars_dir, symbol)
        described[symbol] = {
            "files": [path.name for path in files],
            "sha256": sha,
            "size": size,
            "days": [pd.Timestamp(day).date().isoformat() for day in keep],
            "rows": len(frame),
            "first_bar": frame["date"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
            "last_bar": frame["date"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        }
    description: dict[str, Any] = {
        "today": today.isoformat(),
        "trading_days": trading_days,
        "symbols": described,
    }
    return frames, description, warnings
