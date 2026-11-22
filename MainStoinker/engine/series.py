"""Read an algo's indicator series after a run, for the store.

Every algo keeps `AlgoData` (one row per bar), `FrontEndDataStruct` (column
names) and `FrontEndDataType` (how each column is drawn). `extract_series`
turns them into `SeriesInfo` / `SeriesPoint` records. Pure over pandas; it
reads the algo and never changes it.

Rows are matched to bars by position: `bar_times` is the full history the algo
saw (warm-up bars, then every replayed bar) and `len(AlgoData)` must equal its
length. The algo's own `time` column is not used (it is unreliable).

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from datetime import datetime

import pandas as pd

from .store import SeriesInfo, SeriesPoint


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _naive_datetime(value: object) -> datetime:
    """A naive `datetime` for a bar time (a naive `Timestamp` becomes a `datetime`)."""
    if isinstance(value, pd.Timestamp):
        if value is pd.NaT or value.tzinfo is not None:
            raise ValueError(f"bar time {value!r} must be a naive timestamp")
        return value.to_pydatetime()
    if isinstance(value, datetime) and value.tzinfo is None and value == value:
        return value
    raise ValueError(f"bar time {value!r} must be a naive datetime")


def extract_series(
    algo: object, bar_times: Sequence[object]
) -> tuple[list[SeriesInfo], list[SeriesPoint]]:
    """The indicator series of `algo` as store records.

    `algo.name` is the account (the algo's idname). Returns `([], [])` when
    `FrontEndDataStruct`, `FrontEndDataType` or `AlgoData` is missing or
    malformed (not two equal-length lists, not a DataFrame). When the frame
    length differs from `len(bar_times)` the algo is skipped with a one-line
    warning on stderr; so is an algo when `bar_times` repeats a time (the
    warning names the first repeated one). An algo with an empty frame never
    ran and is skipped silently. Otherwise each declared column becomes one series in
    declaration order (a name listed twice keeps the first; a name that is not
    a column, or a draw type that is not a non-empty string, is left out). Values
    go through `pd.to_numeric(errors="coerce")`; empty and non-finite values
    are dropped, so a series with no value left is still declared. Any draw
    type text is kept.
    """
    names = getattr(algo, "FrontEndDataStruct", None)
    types = getattr(algo, "FrontEndDataType", None)
    frame = getattr(algo, "AlgoData", None)
    if (not isinstance(names, list) or not isinstance(types, list)
            or len(names) != len(types) or not isinstance(frame, pd.DataFrame)):
        return [], []
    account = getattr(algo, "name", None)
    if not isinstance(account, str) or not account:
        return [], []
    if len(frame) == 0:
        return [], []  # the algo never ran (replay shorter than the warm-up): nothing to say
    if len(frame) != len(bar_times):
        _warn(f"warning: {account}: indicator series not stored "
              f"({len(frame)} algo rows for {len(bar_times)} bars)")
        return [], []
    try:
        times = [_naive_datetime(t) for t in bar_times]
    except ValueError as exc:
        _warn(f"warning: {account}: indicator series not stored ({exc})")
        return [], []
    seen_times: set[datetime] = set()
    for when in times:
        if when in seen_times:  # the engine accepts repeated bar times; the store does not
            _warn(f"warning: {account}: indicator series not stored "
                  f"(bar time {when} appears more than once in the data)")
            return [], []
        seen_times.add(when)
    infos: list[SeriesInfo] = []
    points: list[SeriesPoint] = []
    seen: set[str] = set()
    for position, (name, draw_type) in enumerate(zip(names, types, strict=True)):
        if (not isinstance(name, str) or not name or name in seen
                or not isinstance(draw_type, str) or not draw_type):
            continue
        if name not in frame.columns:
            continue
        column = frame[name]
        if isinstance(column, pd.DataFrame):  # duplicate column labels
            continue
        seen.add(name)
        infos.append(SeriesInfo(account, name, draw_type, position))
        if pd.api.types.is_bool_dtype(column):
            continue  # truth values are not indicator levels
        values = pd.to_numeric(column, errors="coerce").to_numpy(dtype="float64", na_value=math.nan)
        for when, value in zip(times, values.tolist(), strict=True):
            if math.isfinite(value):
                points.append(SeriesPoint(account, name, when, value))
    return infos, points
