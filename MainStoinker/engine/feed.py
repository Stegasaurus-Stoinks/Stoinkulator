"""Bar feed interface and a CSV-backed feed for backtests.

`BarFeed` is the pull-based interface of Phase A: the engine asks for the
symbols, the warm-up history of each, and then iterates the feed to get one
batch of bars per timestamp. A push-based live feed (bars arriving from the
broker on their own schedule) is a later phase and will need a different
shape; do not stretch this protocol to cover it.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

COLUMNS: tuple[str, ...] = ("date", "time", "open", "high", "low", "close", "volume")
"""Bar columns, in the order the legacy `Ticker.data` frame uses."""


_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""Same pattern as the CLI's ticker check; keeps a symbol inside the data directory."""


def offline_data_path(data_dir: Path, symbol: str) -> Path:
    """Path of the offline bar file of `symbol` in `data_dir`.

    Raises `ValueError` if `symbol` is not a plain ticker (so it cannot contain
    path separators or `..`-only names).
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"invalid symbol {ascii(symbol)}")
    return Path(data_dir) / f"_{symbol}_offlinedata_"


def read_offline_bars(path: Path) -> pd.DataFrame:
    """Read one offline bar file: columns `COLUMNS`, RangeIndex, `date` as datetime64.

    Raises `FileNotFoundError` for a missing file and `ValueError` for bad
    columns, unparseable data or an empty file.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"offline data file not found: {path}")
    try:
        data = pd.read_csv(path, usecols=list(COLUMNS))
        data["date"] = pd.to_datetime(data["date"])
    except (ValueError, pd.errors.ParserError) as exc:
        raise ValueError(f"cannot load offline data file {path}: {exc}") from exc
    if data.empty:
        raise ValueError(f"offline data file {path} has no rows")
    return data[list(COLUMNS)]


def bars_between(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Rows of `frame` whose `date` is in `[start, end]` (inclusive, naive datetimes).

    Returns a copy with a fresh RangeIndex; empty if nothing matches.
    """
    dates = frame["date"]
    return frame.loc[(dates >= start) & (dates <= end)].reset_index(drop=True)


def file_fingerprint(path: Path) -> tuple[str, int]:
    """`(sha256 hex digest, size in bytes)` of the file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def month_file_path(data_dir: Path, symbol: str, year: int, month: int) -> Path:
    """Path of the month file `<data_dir>/<SYMBOL>/<SYMBOL>_<YYYY>-<MM>.csv`.

    Raises `ValueError` for a symbol that is not a plain ticker or a month
    outside 1..12 (so the path always stays inside `data_dir`).
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"invalid symbol {ascii(symbol)}")
    if not (isinstance(year, int) and isinstance(month, int) and 1 <= month <= 12
            and 1 <= year <= 9999):
        raise ValueError(f"invalid month {year!r}-{month!r}")
    return Path(data_dir) / symbol / f"{symbol}_{year:04d}-{month:02d}.csv"


def _month_name(symbol: str) -> re.Pattern[str]:
    return re.compile(re.escape(symbol) + r"_(\d{4})-(0[1-9]|1[0-2])\.csv")


def symbol_bar_files(data_dir: Path, symbol: str) -> list[Path]:
    """The bar file(s) of `symbol`: the legacy single file, else its month files, sorted.

    Other names in the symbol folder (manifest.json, *.tmp, ...) are ignored.
    Both layouts present is a `ValueError`; neither is a `FileNotFoundError`
    naming both places.
    """
    legacy = offline_data_path(data_dir, symbol)
    folder = Path(data_dir) / symbol
    pattern = _month_name(symbol)
    months = (
        sorted(p for p in folder.iterdir() if pattern.fullmatch(p.name) and p.is_file())
        if folder.is_dir()
        else []
    )
    if legacy.is_file():
        if months:
            raise ValueError(
                f"symbol {symbol} has both the single file {legacy} and month files in "
                f"{folder}; keep only one"
            )
        return [legacy]
    if not months:
        raise FileNotFoundError(
            f"offline data not found for {symbol}: neither {legacy} nor month files "
            f"{folder / (symbol + '_YYYY-MM.csv')}"
        )
    return months


def read_symbol_bars(
    data_dir: Path,
    symbol: str,
    reader: Callable[[Path], pd.DataFrame] = read_offline_bars,
) -> pd.DataFrame:
    """All bars of `symbol` as one frame (columns `COLUMNS`, RangeIndex).

    A legacy file is read exactly as `read_offline_bars` does. Month files are
    concatenated; every row must lie in its file's month and the dates must be
    strictly increasing across the whole result, else `ValueError` naming the
    file. Errors are those of `symbol_bar_files` and `read_offline_bars`.
    `reader` reads one file (default `read_offline_bars`; a seam for callers).
    """
    files = symbol_bar_files(data_dir, symbol)
    if files[0] == offline_data_path(data_dir, symbol):
        return reader(files[0])
    pattern = _month_name(symbol)
    frames = []
    previous: tuple[Path, pd.Timestamp] | None = None
    for path in files:
        frame = reader(path)
        match = pattern.fullmatch(path.name)
        assert match is not None  # symbol_bar_files only returns matching names
        year, month = int(match.group(1)), int(match.group(2))
        dates = frame["date"]
        if getattr(dates.dt, "tz", None) is not None:
            raise ValueError(f"month file {path} has time-zone aware dates")
        outside = (dates.dt.year != year) | (dates.dt.month != month)
        if outside.any():
            raise ValueError(
                f"month file {path} has a row outside {year:04d}-{month:02d}: "
                f"{dates[outside].iloc[0]}"
            )
        if not dates.is_monotonic_increasing or dates.duplicated().any():
            raise ValueError(f"month file {path} has dates that are not strictly increasing")
        if previous is not None and dates.iloc[0] <= previous[1]:
            raise ValueError(
                f"month file {path} overlaps {previous[0].name}: starts at {dates.iloc[0]}, "
                f"which is not after {previous[1]}"
            )
        previous = (path, dates.iloc[-1])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)[list(COLUMNS)]


def symbol_fingerprint(
    data_dir: Path,
    symbol: str,
    hasher: Callable[[Path], tuple[str, int]] = file_fingerprint,
) -> tuple[str, int]:
    """`(sha256 hex digest, size in bytes)` of the data of `symbol`.

    For a legacy single file this is `file_fingerprint` of that file, so runs
    stored before month files existed still verify. For month files it is the
    sha256 over one `"<name> NUL <file sha> NUL <size>\\n"` line per file, in
    name order, with the total size of the files. `hasher` computes the
    `(sha, size)` of one file (default `file_fingerprint`; a seam for callers).
    """
    files = symbol_bar_files(data_dir, symbol)
    if files[0] == offline_data_path(data_dir, symbol):
        return hasher(files[0])
    digest = hashlib.sha256()
    total = 0
    for path in files:
        sha, size = hasher(path)
        digest.update(f"{path.name}\0{sha}\0{size}\n".encode("ascii"))
        total += size
    return digest.hexdigest(), total


def write_month_file(path: Path, frame: pd.DataFrame) -> None:
    """Write `frame` (columns `COLUMNS`) as a month file, atomically.

    Format: header `date,time,open,high,low,close,volume`, no index column,
    `date` as `YYYY-MM-DD HH:MM:SS`, LF line endings. The data goes to a temp
    file in the same folder and is then moved over `path` with `os.replace`, so
    a reader never sees a half-written file. The folder is created if needed.
    A frame with other columns or no rows is a `ValueError`; so is a row outside
    the month named by `path` when `path` is a month file name.
    """
    path = Path(path)
    if list(frame.columns) != list(COLUMNS):
        raise ValueError(f"month file frame needs columns {list(COLUMNS)}, got {list(frame.columns)}")
    if frame.empty:
        raise ValueError("refusing to write an empty month file")
    dates = pd.to_datetime(frame["date"])
    match = re.fullmatch(r".+_(\d{4})-(0[1-9]|1[0-2])\.csv", path.name)
    if match and ((dates.dt.year != int(match.group(1))) | (dates.dt.month != int(match.group(2)))).any():
        raise ValueError(f"frame has rows outside the month of {path.name}")
    out = frame.copy()
    out["date"] = dates.dt.strftime("%Y-%m-%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as temp:
            out.to_csv(temp, index=False, lineterminator="\n")
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


@dataclass(frozen=True, slots=True)
class Bar:
    """One closed 1-minute bar.

    The field types are chosen so that appending bars to a frame reproduces
    what the legacy `Ticker.append` builds from a row of the offline data:
    `date` is a `pd.Timestamp` (datetime64), `time` integer epoch seconds
    (int64), prices are floats (float64) and `volume` is an int (int64, as in
    the offline files). Use `as_record()` to get the values in column order.
    """

    date: pd.Timestamp
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int

    def as_record(self) -> dict[str, object]:
        """The bar as a mapping in `COLUMNS` order, ready for `DataFrame.from_records`."""
        return {name: getattr(self, name) for name in COLUMNS}


@runtime_checkable
class BarFeed(Protocol):
    """Source of bars for a backtest (pull-based)."""

    def symbols(self) -> list[str]:
        """The symbols in this feed, in a stable order (batches use the same order)."""
        ...

    def warmup(self, symbol: str) -> pd.DataFrame:
        """Warm-up bars for `symbol`: columns `COLUMNS`, RangeIndex from 0.

        The caller owns the returned frame and may mutate it.
        """
        ...

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        """Yield `(timestamp, bars)` per timestamp after warm-up, in time order.

        `bars` maps each symbol to its bar, keys in `symbols()` order.
        """
        ...


class CsvBarFeed:
    """Feed backed by `<data_dir>/_<SYMBOL>_offlinedata_` files or month files.

    Parsing matches `IBapi.load_offline_data`. Files are read eagerly in the
    constructor, so every problem (missing file, bad columns, misaligned
    symbols) surfaces there with a message naming the symbol or file, never
    midway through a run. Paths are resolved from `data_dir` only, never the
    current directory.

    Symbols are de-duplicated, keeping the caller's order. The warm-up of a
    symbol is the rows whose date is before `first_date + warmup_days days`
    (`first_date` = that symbol's first row), as in the legacy loader. After
    warm-up all symbols must have identical timestamps, else `ValueError`.
    The feed may be iterated repeatedly and yields the same batches each time.
    """

    def __init__(self, data_dir: Path, symbols: Sequence[str], warmup_days: int = 2) -> None:
        self._symbols: list[str] = list(dict.fromkeys(symbols))
        if not self._symbols:
            raise ValueError("CsvBarFeed needs at least one symbol")
        self._warmup: dict[str, pd.DataFrame] = {}
        replay: dict[str, pd.DataFrame] = {}
        for symbol in self._symbols:
            data = self._read(Path(data_dir), symbol)
            cutoff = data["date"].iloc[0] + timedelta(days=warmup_days)
            is_warmup = data["date"] < cutoff
            self._warmup[symbol] = data.loc[is_warmup].reset_index(drop=True)
            replay[symbol] = data.loc[~is_warmup].reset_index(drop=True)
        self._check_aligned(replay)
        self._batches: list[tuple[pd.Timestamp, dict[str, Bar]]] = self._build_batches(replay)

    @staticmethod
    def _read(data_dir: Path, symbol: str) -> pd.DataFrame:
        return read_symbol_bars(data_dir, symbol)

    def _check_aligned(self, replay: Mapping[str, pd.DataFrame]) -> None:
        ref_symbol = self._symbols[0]
        ref = replay[ref_symbol]["date"]
        for symbol in self._symbols[1:]:
            dates = replay[symbol]["date"]
            common = min(len(ref), len(dates))
            diff = (ref.iloc[:common].to_numpy() != dates.iloc[:common].to_numpy()).nonzero()[0]
            if len(diff):
                row = int(diff[0])
                raise ValueError(
                    f"symbol {symbol} is not aligned with {ref_symbol}: replay row {row} "
                    f"is {dates.iloc[row]} but {ref_symbol} has {ref.iloc[row]}"
                )
            if len(ref) != len(dates):
                longer, shorter = (ref, dates) if len(ref) > len(dates) else (dates, ref)
                raise ValueError(
                    f"symbol {symbol} is not aligned with {ref_symbol}: {symbol} has "
                    f"{len(dates)} replay bars, {ref_symbol} has {len(ref)} "
                    f"(first unmatched timestamp {longer.iloc[len(shorter)]})"
                )

    def _build_batches(
        self, replay: Mapping[str, pd.DataFrame]
    ) -> list[tuple[pd.Timestamp, dict[str, Bar]]]:
        per_symbol: dict[str, list[Bar]] = {}
        for symbol in self._symbols:
            frame = replay[symbol]
            per_symbol[symbol] = [
                Bar(*row) for row in zip(*(frame[name].tolist() for name in COLUMNS), strict=True)
            ]
        batches = []
        for i, first in enumerate(per_symbol[self._symbols[0]]):
            batches.append((first.date, {s: per_symbol[s][i] for s in self._symbols}))
        return batches

    def symbols(self) -> list[str]:
        """Symbols in caller order, de-duplicated (a fresh list each call)."""
        return list(self._symbols)

    def warmup(self, symbol: str) -> pd.DataFrame:
        """A copy of the warm-up frame for `symbol`; RangeIndex from 0."""
        try:
            return self._warmup[symbol].copy()
        except KeyError:
            raise KeyError(f"unknown symbol {symbol!r}; feed has {self._symbols}") from None

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        for timestamp, bars in self._batches:
            yield timestamp, dict(bars)
