"""Per-symbol bar history for the engine.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import logging
import math
import numbers
from datetime import datetime

import numpy as np
import pandas as pd

from .feed import COLUMNS, Bar

_INITIAL_CAPACITY = 1024
_GROWTH_FACTOR = 2

_DATE_UNITS: tuple[str, ...] = ("s", "ms", "us", "ns")
_FLOAT_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
_INT_COLUMNS: tuple[str, ...] = ("time", "volume")
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _seed_problem(warmup: pd.DataFrame) -> str | None:
    """Why `warmup` cannot be held in typed buffers without changing it, or None.

    The fast path needs a feed-shaped frame: columns exactly `COLUMNS` in order
    with the default column index type, an unnamed `RangeIndex(0, n)`, no
    attrs, duplicate labels allowed, and dtypes datetime64 (s/ms/us/ns) for
    `date`, int64 for `time`/`volume`, float64 for the prices.
    """
    if list(warmup.columns) != list(COLUMNS):
        return f"columns are {list(warmup.columns)}, expected {list(COLUMNS)}"
    if warmup.columns.dtype != pd.Index(list(COLUMNS)).dtype or warmup.columns.name is not None:
        return f"column index is {warmup.columns.dtype} named {warmup.columns.name!r}"
    index = warmup.index
    if type(index) is not pd.RangeIndex or index.start != 0 or index.step != 1 or index.name is not None:
        return f"index is not an unnamed RangeIndex(0, n): {type(index).__name__} named {index.name!r}"
    if warmup.attrs:
        return "frame has attrs"
    if not warmup.flags.allows_duplicate_labels:
        return "frame disallows duplicate labels"
    for name in COLUMNS:
        dtype = warmup.dtypes[name]
        if name == "date":
            ok = (
                isinstance(dtype, np.dtype) and dtype.kind == "M"
                and np.datetime_data(dtype)[0] in _DATE_UNITS
            )
        else:
            ok = dtype == np.dtype("int64" if name in _INT_COLUMNS else "float64")
        if not ok:
            return f"column {name!r} has dtype {dtype}"
    return None


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"Bar.{name} must be an int, got {value!r} ({type(value).__name__})")
    result = int(value)
    if not _INT64_MIN <= result <= _INT64_MAX:
        raise ValueError(f"Bar.{name} does not fit in int64: {value!r}")
    return result


def _check_price(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"Bar.{name} must be a real number, got {value!r} ({type(value).__name__})")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"Bar.{name} is too large for a float: {value!r}") from None
    if not math.isfinite(result):
        raise ValueError(f"Bar.{name} must be finite, got {value!r}")
    return result


def _check_date(value: object, unit: str) -> np.datetime64:
    """`value` as a datetime64 in `unit`, or raise. Never rounds."""
    if not isinstance(value, datetime | np.datetime64):
        raise TypeError(f"Bar.date must be a Timestamp, datetime or datetime64, got {value!r}")
    stamp = pd.Timestamp(value)
    if stamp is pd.NaT:
        raise ValueError("Bar.date must not be NaT")
    if stamp.tzinfo is not None:
        raise ValueError(f"Bar.date must be timezone-naive, got {value!r}")
    try:
        return stamp.as_unit(unit, round_ok=False).to_datetime64()
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"Bar.date {value!r} cannot be stored exactly in the history's {unit!r} resolution: {exc}"
        ) from None


class BarStore:
    """Growing bar history of one symbol, with amortised O(1) `append`.

    Seeded from the feed's warm-up frame; `append` adds one closed bar and
    `view` returns the full history for algos. `view()` is frame-identical to
    the legacy `Ticker.data` (values, dtypes, column order, `RangeIndex`), and
    `iloc[-1]` yields the same element types as the legacy frame.

    Contract: **a frame returned by `view()` is read-only input.** Callers must
    not modify it; one that needs to change values takes its own copy
    (`df.copy()`), which is freely writable. The frame wraps read-only slices
    of the store's buffers, so an in-place write raises (`ValueError`) rather
    than corrupting the store. Within the bar in which the view was taken a
    pandas write may happen to succeed on a private copy (copy-on-write while
    the store still holds its cached frame), but after the store's next
    `append` in-place writes raise. Replacing a whole column or adding a
    column or row never touches the store. Nothing here is a promise that a
    write succeeds.

    Storage: one typed numpy buffer per column with spare capacity at the
    end. The `date` buffer keeps the warm-up's datetime unit (us for the
    feed), `time`/`volume` are int64, prices float64. When full, every buffer
    is reallocated at twice the size and the used rows copied. Rows below the
    current length are never rewritten and a replaced buffer is never touched
    again, so a returned frame (read-only slices `buffer[:n]` of the buffers
    that were current when it was built) never changes, whatever is appended
    later. The frame is built once per `append`, lazily on the first `view()`
    after it; each `view()` returns a new shallow copy of it.

    `append` validates and converts the whole bar before touching any buffer,
    and rejects (raising `TypeError`/`ValueError`, leaving the store as it
    was): a `date` that is not a naive Timestamp/datetime/datetime64, is NaT,
    or cannot be stored exactly in the buffer's unit; a `time` or `volume`
    that is not an integer (bool and floats included); a price that is not a
    finite real number.

    A warm-up that is not feed-shaped (see `_seed_problem`) cannot be held in
    typed buffers without changing it, so such a store logs a warning naming
    the reason and keeps the legacy concat-append behaviour exactly, with
    none of the checks above and with O(n) appends. The feed never produces
    one; a CSV with a blank volume cell (float64 `volume`) does.
    """

    def __init__(self, warmup: pd.DataFrame, *, _initial_capacity: int = _INITIAL_CAPACITY) -> None:
        # Counts buffer reallocations (initial allocation excluded); for tests.
        self._growths = 0
        self._legacy: pd.DataFrame | None = None
        self._frame: pd.DataFrame | None = None
        self._n = 0
        self._buffers: dict[str, np.ndarray] = {}
        problem = _seed_problem(warmup)
        if problem is not None:
            logging.getLogger(__name__).warning(
                "BarStore: warm-up is not feed-shaped (%s); using the slow concat-append path", problem
            )
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

    def append(self, bar: Bar) -> None:
        """Add one closed bar to the end of the history (all-or-nothing)."""
        if self._legacy is not None:
            record = pd.DataFrame.from_records([bar.as_record()])
            self._legacy = pd.concat([self._legacy, record], ignore_index=True)
            return
        buffers = self._buffers
        unit = np.datetime_data(buffers["date"].dtype)[0]
        row = (
            _check_date(bar.date, unit),
            _check_int("time", bar.time),
            _check_price("open", bar.open),
            _check_price("high", bar.high),
            _check_price("low", bar.low),
            _check_price("close", bar.close),
            _check_int("volume", bar.volume),
        )
        if self._n == len(buffers["close"]):
            self._grow()
        i = self._n
        for name, value in zip(COLUMNS, row, strict=True):
            buffers[name][i] = value
        self._n = i + 1
        self._frame = None

    def _build_frame(self) -> pd.DataFrame:
        columns: dict[str, np.ndarray] = {}
        for name in COLUMNS:
            part = self._buffers[name][: self._n]
            part.flags.writeable = False
            columns[name] = part
        return pd.DataFrame(columns, copy=False)

    def view(self) -> pd.DataFrame:
        """The full history, oldest first. Read-only input: copy it before modifying.

        See the class docstring for the contract.
        """
        if self._legacy is not None:
            return self._legacy.copy(deep=False)
        if self._frame is None:
            self._frame = self._build_frame()
        return self._frame.copy(deep=False)

    def __len__(self) -> int:
        return len(self._legacy) if self._legacy is not None else self._n
