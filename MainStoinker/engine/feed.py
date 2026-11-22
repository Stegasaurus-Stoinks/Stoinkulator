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

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
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
    """Feed backed by `<data_dir>/_<SYMBOL>_offlinedata_` files.

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
            data = self._read(offline_data_path(Path(data_dir), symbol))
            cutoff = data["date"].iloc[0] + timedelta(days=warmup_days)
            is_warmup = data["date"] < cutoff
            self._warmup[symbol] = data.loc[is_warmup].reset_index(drop=True)
            replay[symbol] = data.loc[~is_warmup].reset_index(drop=True)
        self._check_aligned(replay)
        self._batches: list[tuple[pd.Timestamp, dict[str, Bar]]] = self._build_batches(replay)

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        return read_offline_bars(path)

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
