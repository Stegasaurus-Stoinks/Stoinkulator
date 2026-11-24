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

import numpy as np
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


class SeriesTracker:
    """The indicator series of one algo, handed out step by step during a run.

    The union of every `declare()` result is `extract_series(algo, history)[0]`
    and the union of every `new_points(history)` result is
    `extract_series(algo, history)[1]` for the final `history` (as sets of
    records; within one call points are ordered by series, then time, as
    `extract_series` does). The rules are those of `extract_series`: rows are
    matched to bars by position, only finite values are points, boolean
    columns are declared but give no points, and an algo is skipped (with a
    one-line warning on stderr) when the frame length differs from the bar
    count, when a bar time is not a naive datetime, or when a bar time repeats.
    An algo whose frame is still empty is skipped silently.

    Each call to `new_points` looks at every row of the algo's frame, not only
    the new ones: an algo may fill an earlier row later (Elliot confirms a
    local extremum `order` bars after it), and that point must still arrive. A
    point (series, row) is returned once, the first time its value is finite;
    a later change of that value is not reported.

    Rows can also be provisional: Elliot and FunctionTest mark a local extremum
    of `order` bars either side, and the newest rows are marked as extrema that
    later vanish. So a row is only looked at once `settle` bars have come after
    it (`settle` defaults to the algo's integer `order` attribute, else 0, so
    an algo without one is passed through at once). `new_points(..., final=True)`,
    given on the last call of a run, releases the rest; without it the last
    `settle` rows never arrive and the union falls short of `extract_series`.
    The cost is that a trade marker or stop on those newest rows reaches a live
    reader `settle` bars late. A length mismatch is warned
    once per tracker; later calls that match again resume normally. A bad or
    repeated bar time is permanent (it stays in the history) and warned once.

    `bar_times` is the full history so far and must only grow: the part seen
    before is not checked again. The tracker reads the algo and never changes
    it. The cost of a call is linear in the history length.
    """

    def __init__(self, algo: object, settle: int | None = None) -> None:
        self._algo = algo
        if settle is None:
            order = getattr(algo, "order", 0)
            settle = order if isinstance(order, int) and not isinstance(order, bool) else 0
        self._settle = max(0, settle)
        self._declared: set[str] = set()
        self._times: list[datetime] = []
        self._seen_times: set[datetime] = set()
        self._emitted: dict[str, np.ndarray] = {}
        self._blocked = False  # a bad or repeated bar time: skip for good
        self._warned_length = False

    def _account(self) -> str | None:
        account = getattr(self._algo, "name", None)
        return account if isinstance(account, str) and account else None

    def _columns(self) -> list[tuple[int, str, str]] | None:
        """(position, name, draw type) of each usable series, or None when malformed."""
        names = getattr(self._algo, "FrontEndDataStruct", None)
        types = getattr(self._algo, "FrontEndDataType", None)
        frame = getattr(self._algo, "AlgoData", None)
        if (not isinstance(names, list) or not isinstance(types, list)
                or len(names) != len(types) or not isinstance(frame, pd.DataFrame)
                or self._account() is None or len(frame) == 0):
            return None
        usable: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for position, (name, draw_type) in enumerate(zip(names, types, strict=True)):
            if (not isinstance(name, str) or not name or name in seen
                    or not isinstance(draw_type, str) or not draw_type
                    or name not in frame.columns
                    or isinstance(frame[name], pd.DataFrame)):
                continue
            seen.add(name)
            usable.append((position, name, draw_type))
        return usable

    def declare(self) -> list[SeriesInfo]:
        """The series not declared before (all of them on the first call with a frame).

        Returns `[]` while the algo's structure is malformed or its frame is
        empty; a series is declared as soon as its column exists.
        """
        columns = self._columns()
        account = self._account()
        if columns is None or account is None:
            return []
        fresh = [SeriesInfo(account, name, draw_type, position)
                 for position, name, draw_type in columns if name not in self._declared]
        self._declared.update(info.name for info in fresh)
        return fresh

    def _extend_times(self, bar_times: Sequence[object], account: str) -> bool:
        """Validate the bar times added since the last call; False if blocked."""
        if self._blocked:
            return False
        for value in bar_times[len(self._times):]:
            try:
                when = _naive_datetime(value)
            except ValueError as exc:
                _warn(f"warning: {account}: indicator series not stored ({exc})")
                self._blocked = True
                return False
            if when in self._seen_times:
                _warn(f"warning: {account}: indicator series not stored "
                      f"(bar time {when} appears more than once in the data)")
                self._blocked = True
                return False
            self._seen_times.add(when)
            self._times.append(when)
        return True

    def new_points(self, bar_times: Sequence[object], *, final: bool = False) -> list[SeriesPoint]:
        """The points of `algo` not returned by an earlier call, for the history `bar_times`.

        Rows within `settle` bars of the end are held back unless `final` is
        true (pass it on the last call of a run).
        """
        columns = self._columns()
        account = self._account()
        if columns is None or account is None:
            return []
        frame: pd.DataFrame = getattr(self._algo, "AlgoData")  # noqa: B009
        if len(frame) != len(bar_times):
            if not self._warned_length:
                self._warned_length = True
                _warn(f"warning: {account}: indicator series not stored "
                      f"({len(frame)} algo rows for {len(bar_times)} bars)")
            return []
        if not self._extend_times(bar_times, account):
            return []
        size = len(frame)
        limit = size if final else max(0, size - self._settle)
        points: list[SeriesPoint] = []
        for _position, name, _draw_type in columns:
            column = frame[name]
            if pd.api.types.is_bool_dtype(column):
                continue  # truth values are not indicator levels
            values = pd.to_numeric(column, errors="coerce").to_numpy(
                dtype="float64", na_value=math.nan)
            emitted = self._emitted.get(name)
            if emitted is None or len(emitted) < size:
                grown = np.zeros(size, dtype=bool)
                if emitted is not None:
                    grown[: len(emitted)] = emitted
                emitted = self._emitted[name] = grown
            fresh = np.isfinite(values) & ~emitted[:size]
            fresh[limit:] = False  # the newest rows may still change: wait for them
            emitted[:size] |= fresh
            for row in np.flatnonzero(fresh).tolist():
                points.append(SeriesPoint(account, name, self._times[row], float(values[row])))
        return points
