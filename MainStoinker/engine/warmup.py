"""Warm-up history for a live session, read from the month files before today.

`live_warmup` reads only the data folder (never the current directory) and
imports nothing outside `MainStoinker.engine`. Data time is naive
`America/New_York` wall-clock time, so `today` is the New York date.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from MainStoinker.engine.feed import (
    COLUMNS,
    offline_data_path,
    read_offline_bars,
    read_symbol_bars,
    symbol_bar_files,
    symbol_fingerprint,
)
from MainStoinker.engine.market_calendar import previous_trading_day

_MONTH_FILE = re.compile(r".+_([0-9]{4})-([0-9]{2})\.csv")
_STAMP = "%Y-%m-%d %H:%M:%S"


def warmup_digest(frame: pd.DataFrame) -> str:
    """sha256 hex of `frame` serialised as the month-file writer does.

    The columns are first cast to fixed dtypes (`date` datetime64, `time` and
    `volume` int64, `open`/`high`/`low`/`close` float64), so only values
    matter (`100` and `100.0` hash alike). Then `to_csv(index=False,
    lineterminator="\\n", date_format="%Y-%m-%d %H:%M:%S")` encoded as UTF-8
    (header line included); floats use pandas' shortest round-trip repr.
    A `time` or `volume` value with a fractional part raises `ValueError`
    rather than being truncated.
    """
    for column in ("time", "volume"):
        values = frame[column]
        if (values.astype("float64") % 1 != 0).any():
            raise ValueError(f"warm-up column {column} has a value with a fractional part")
    fixed = frame[list(COLUMNS)].astype(
        {
            "date": "datetime64[ns]",
            "time": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "int64",
        }
    )
    text = fixed.to_csv(index=False, lineterminator="\n", date_format=_STAMP)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _month_of(path: Path) -> tuple[int, int]:
    match = _MONTH_FILE.fullmatch(path.name)
    assert match is not None
    return int(match.group(1)), int(match.group(2))


def _read_month(path: Path, symbol: str) -> pd.DataFrame:
    """One month file via `read_offline_bars`, with the checks `read_symbol_bars` makes."""
    frame = read_offline_bars(path)
    year, month = _month_of(path)
    dates = frame["date"]
    if getattr(dates.dt, "tz", None) is not None:
        raise ValueError(f"month file {path} has time-zone aware dates")
    outside = (dates.dt.year != year) | (dates.dt.month != month)
    if outside.any():
        raise ValueError(
            f"month file {path} has a row outside {year:04d}-{month:02d}: {dates[outside].iloc[0]}"
        )
    _check_increasing(symbol, frame, path)
    return frame


def _check_increasing(symbol: str, data: pd.DataFrame, source: Path | None = None) -> None:
    dates = data["date"]
    bad = (dates <= dates.shift()).to_numpy().nonzero()[0]
    # The first row compares with NaT, which is never `<=`, so it cannot be flagged.
    if len(bad):
        row = int(bad[0])
        where = f" in {source}" if source is not None else ""
        raise ValueError(
            f"symbol {symbol}: dates are not strictly increasing{where}: data row {row + 1} "
            f"({dates.iloc[row]}) is not after the row above ({dates.iloc[row - 1]})"
        )


def _read_needed(
    bars_dir: Path, symbol: str, today: date, trading_days: int
) -> tuple[pd.DataFrame, list[Path]]:
    """Rows before `today` of `symbol` from as few files as possible, and the files read.

    Month layout: the files up to today's month are opened newest first until
    `trading_days` distinct days before `today` are found; later months and
    older months are never opened. Legacy single file: read whole.
    """
    files = symbol_bar_files(bars_dir, symbol)
    cutoff = pd.Timestamp(today)
    if files[0] == offline_data_path(bars_dir, symbol):
        data = read_symbol_bars(bars_dir, symbol)
        _check_increasing(symbol, data)
        return data.loc[data["date"] < cutoff].reset_index(drop=True), files
    candidates = [p for p in files if _month_of(p) <= (today.year, today.month)]
    if not candidates:
        raise ValueError(f"symbol {symbol}: no bars before {today.isoformat()}")
    parts: list[pd.DataFrame] = []
    read: list[Path] = []
    days: set[pd.Timestamp] = set()
    for path in reversed(candidates):
        frame = _read_month(path, symbol)
        read.append(path)
        parts.insert(0, frame)
        days.update(frame.loc[frame["date"] < cutoff, "date"].dt.normalize().unique())
        if len(days) >= trading_days:
            break
    data = pd.concat(parts, ignore_index=True)[list(COLUMNS)]
    return data.loc[data["date"] < cutoff].reset_index(drop=True), sorted(read)


def live_warmup(
    bars_dir: Path,
    symbols: Sequence[str],
    today: date,
    trading_days: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any], list[str]]:
    """Warm-up frames for `symbols`: the last `trading_days` days before `today`.

    Returns `(frames, description, warnings)`.

    `frames` maps each symbol to a frame shaped like `CsvBarFeed.warmup`:
    columns `COLUMNS`, RangeIndex, `date` as datetime64, rows strictly
    increasing in time, only rows dated strictly before `today` and only the
    last `trading_days` distinct dates that have rows.

    `description` is `{"today": "YYYY-MM-DD", "trading_days": N, "symbols":
    {symbol: {...}}}` with, per symbol:
    - `files`: names of the files that hold the kept days (the single legacy
      file, or the month files of the kept days);
    - `days`, `rows`, `first_bar`, `last_bar` (`YYYY-MM-DD HH:MM:SS`) of the frame;
    - `warmup_sha256`: `warmup_digest` of the frame, which depends only on rows
      before `today`; this is the value a later parity check compares;
    - `data_snapshot_sha256` and `data_snapshot_size`: `symbol_fingerprint` of
      all files of the symbol at warm-up time (changes when today's rows or a
      later month are written; informational, not for parity).

    Month files are opened newest first from `today`'s month back until enough
    days are found; later months are never opened (so a broken later file
    cannot fail the warm-up).

    `warnings` lists, per symbol, a last warm-up day that is not the previous
    trading day of the market calendar (weekends and holidays skipped, so the day
    after a holiday expects the day before it) and fewer than `trading_days`
    days of history.

    Raises `TypeError` if `today` is a `datetime`; `FileNotFoundError` for a
    symbol without bar files; `ValueError` for a symbol with no rows before
    `today`, dates not strictly increasing, `trading_days < 1`, no symbols, or
    any error `read_symbol_bars` raises.
    """
    if isinstance(today, datetime) or not isinstance(today, date):
        raise TypeError(f"today must be a datetime.date, not {type(today).__name__}")
    if trading_days < 1:
        raise ValueError(f"trading_days must be at least 1, got {trading_days}")
    names = list(dict.fromkeys(symbols))
    if not names:
        raise ValueError("live_warmup needs at least one symbol")
    expected = previous_trading_day(today)
    frames: dict[str, pd.DataFrame] = {}
    described: dict[str, Any] = {}
    warnings: list[str] = []
    for symbol in names:
        before, read_files = _read_needed(bars_dir, symbol, today, trading_days)
        if before.empty:
            raise ValueError(f"symbol {symbol}: no bars before {today.isoformat()}")
        day_of = before["date"].dt.normalize()
        keep = sorted(day_of.unique())[-trading_days:]
        frame = before.loc[day_of.isin(keep)].reset_index(drop=True)[list(COLUMNS)]
        frames[symbol] = frame
        last_day = pd.Timestamp(keep[-1]).date()
        if last_day != expected:
            warnings.append(
                f"symbol {symbol}: last warm-up day is {last_day.isoformat()}, expected the "
                f"previous trading day {expected.isoformat()}"
            )
        if len(keep) < trading_days:
            warnings.append(
                f"symbol {symbol}: only {len(keep)} trading days of warm-up, wanted {trading_days}"
            )
        if len(read_files) == 1 and not _MONTH_FILE.fullmatch(read_files[0].name):
            kept_files = read_files
        else:
            kept = {(pd.Timestamp(d).year, pd.Timestamp(d).month) for d in keep}
            kept_files = [p for p in read_files if _month_of(p) in kept]
        sha, size = symbol_fingerprint(bars_dir, symbol)
        described[symbol] = {
            "files": [path.name for path in kept_files],
            "days": [pd.Timestamp(day).date().isoformat() for day in keep],
            "rows": len(frame),
            "first_bar": frame["date"].iloc[0].strftime(_STAMP),
            "last_bar": frame["date"].iloc[-1].strftime(_STAMP),
            "warmup_sha256": warmup_digest(frame),
            "data_snapshot_sha256": sha,
            "data_snapshot_size": size,
        }
    description: dict[str, Any] = {
        "today": today.isoformat(),
        "trading_days": trading_days,
        "symbols": described,
    }
    return frames, description, warnings
