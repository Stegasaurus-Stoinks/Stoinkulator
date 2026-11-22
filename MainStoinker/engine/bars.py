"""Per-symbol bar history for the engine.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .feed import COLUMNS, Bar

_INITIAL_CAPACITY = 1024
_GROWTH_FACTOR = 2

# numpy datetime64 units from coarse to fine. When the legacy concat append met
# a bar whose date had a different unit from the history, the result took the
# finer of the two; the store reproduces that.
_DATE_UNITS: tuple[str, ...] = ("s", "ms", "us", "ns")

_FLOAT_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
_INT_COLUMNS: tuple[str, ...] = ("time", "volume")


def _date_unit(dtype: object) -> str | None:
    """The unit of a plain numpy datetime64 dtype, else None (tz-aware, object, ...)."""
    if isinstance(dtype, np.dtype) and dtype.kind == "M":
        unit = np.datetime_data(dtype)[0]
        if unit in _DATE_UNITS:
            return unit
    return None


def _is_plain_seed(warmup: pd.DataFrame) -> bool:
    """True if `warmup` is shaped like a feed warm-up, so typed buffers reproduce it.

    That is: columns exactly `COLUMNS` in order with the default column index
    type, a `RangeIndex(0, n)`, no attrs, and dtypes datetime64 / int64 /
    float64 as the feed produces them.
    """
    if list(warmup.columns) != list(COLUMNS):
        return False
    if warmup.columns.dtype != pd.Index(list(COLUMNS)).dtype:
        return False
    if type(warmup.index) is not pd.RangeIndex or warmup.index.start != 0 or warmup.index.step != 1:
        return False
    if warmup.attrs:
        return False
    dtypes = warmup.dtypes
    if _date_unit(dtypes["date"]) is None:
        return False
    return all(dtypes[c] == np.dtype("int64") for c in _INT_COLUMNS) and all(
        dtypes[c] == np.dtype("float64") for c in _FLOAT_COLUMNS
    ) and dtypes["volume"] == np.dtype("int64")


class BarStore:
    """Growing bar history of one symbol, with amortised O(1) `append`.

    Seeded from the feed's warm-up frame; `append` adds one closed bar and
    `view` returns the full history for algos. `view()` is frame-identical to
    the legacy `Ticker.data` (values, dtypes, column order, `RangeIndex`), and
    `iloc[-1]` yields the same element types as the legacy frame.

    Storage: one typed numpy buffer per column (`date` datetime64[us] as the
    feed produces it, `time`/`volume` int64, prices float64) with spare
    capacity at the end. When full, every buffer is reallocated at twice the
    size and the used rows copied across, so appends are amortised O(1)
    instead of copying the whole history each bar.

    Aliasing: rows below the current length are never rewritten, and a buffer
    that is replaced is not touched again. A returned frame wraps read-only
    slices `buffer[:n]` of the buffers that were current when it was built, so
    later appends (which only write at index >= n) and reallocations cannot
    change it. Writing through `.values` raises, as it does for the legacy
    frame. The frame is built once per `append` (lazily on the first `view()`
    after it) and every `view()` call returns a new shallow copy of that
    frame, so pandas copy-on-write gives each caller private data as soon as
    it writes: setting a column or cell, adding a column or row, dropping or
    renaming never reaches the store or other views.

    A warm-up that is not feed-shaped (other columns, dtypes or index, or
    empty with object dtypes) cannot be held in typed buffers without changing
    its dtypes, so such a store keeps the legacy concat-append behaviour
    exactly (including its O(n) append). The feed never produces one.

    Bars are expected to follow the `Bar` field types: a naive `date`
    Timestamp, int `time` and `volume`, float prices.
    """

    def __init__(self, warmup: pd.DataFrame, *, _initial_capacity: int = _INITIAL_CAPACITY) -> None:
        # Counts buffer reallocations (initial allocation excluded); for tests.
        self._growths = 0
        self._legacy: pd.DataFrame | None = None
        self._frame: pd.DataFrame | None = None
        self._n = 0
        self._buffers: dict[str, np.ndarray] = {}
        if not _is_plain_seed(warmup):
            self._legacy = warmup.copy()
            return
        n = len(warmup)
        capacity = max(_initial_capacity, n, 1)
        self._n = n
        for name in COLUMNS:
            source = warmup[name].to_numpy()
            buffer = np.empty(capacity, dtype=source.dtype)
            buffer[:n] = source
            self._buffers[name] = buffer

    def _grow(self) -> None:
        capacity = max(len(self._buffers["close"]) * _GROWTH_FACTOR, 1)
        for name, old in self._buffers.items():
            new = np.empty(capacity, dtype=old.dtype)
            new[: self._n] = old[: self._n]
            self._buffers[name] = new
        self._growths += 1

    def _widen_dates(self, unit: str) -> None:
        """Move the date buffer to a finer datetime unit (the legacy concat did)."""
        old = self._buffers["date"]
        new = np.empty(len(old), dtype=f"datetime64[{unit}]")
        new[: self._n] = old[: self._n]
        self._buffers["date"] = new

    def append(self, bar: Bar) -> None:
        """Add one closed bar to the end of the history."""
        if self._legacy is not None:
            record = pd.DataFrame.from_records([bar.as_record()])
            self._legacy = pd.concat([self._legacy, record], ignore_index=True)
            return
        if bar.date.tzinfo is not None:
            raise ValueError(f"Bar.date must be timezone-naive, got {bar.date!r}")
        bar_unit = bar.date.unit
        buffer_unit = _date_unit(self._buffers["date"].dtype)
        assert buffer_unit is not None
        if _DATE_UNITS.index(bar_unit) > _DATE_UNITS.index(buffer_unit):
            self._widen_dates(bar_unit)
            buffer_unit = bar_unit
        if self._n == len(self._buffers["close"]):
            self._grow()
        i = self._n
        buffers = self._buffers
        buffers["date"][i] = bar.date.as_unit(buffer_unit).to_datetime64()
        buffers["time"][i] = bar.time
        buffers["open"][i] = bar.open
        buffers["high"][i] = bar.high
        buffers["low"][i] = bar.low
        buffers["close"][i] = bar.close
        buffers["volume"][i] = bar.volume
        self._n = i + 1
        self._frame = None

    def _build_frame(self) -> pd.DataFrame:
        n = self._n
        columns: dict[str, np.ndarray] = {}
        for name in COLUMNS:
            part = self._buffers[name][:n]
            part.flags.writeable = False
            columns[name] = part
        return pd.DataFrame(columns, copy=False)

    def view(self) -> pd.DataFrame:
        """The full history, oldest first. Writing to it does not change the store."""
        if self._legacy is not None:
            return self._legacy.copy(deep=False)
        if self._frame is None:
            self._frame = self._build_frame()
        return self._frame.copy(deep=False)

    def __len__(self) -> int:
        return len(self._legacy) if self._legacy is not None else self._n
