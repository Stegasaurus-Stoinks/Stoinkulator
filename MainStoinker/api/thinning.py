"""Thinning of a long equity series for charting: pure, no I/O.

The points are the stored step points of one series in time order; any object
with an `equity` attribute (a `Decimal`) will do, and the same objects come
back. `thin` keeps the shape of the curve and, exactly, its largest drawdown.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

MIN_POINTS = 10
MAX_POINTS = 100_000


def _worst_drawdown(points: Sequence[Any], capital: Decimal) -> tuple[int | None, int | None]:
    """Indices of the peak and the trough of the largest drawdown.

    The peak is None when it is the starting `capital` (no point carries it);
    both are None when the series never falls below its peak.
    """
    peak_value = capital
    peak_index: int | None = None
    best = Decimal(0)
    found: tuple[int | None, int | None] = (None, None)
    for i, point in enumerate(points):
        value = point.equity
        if value > peak_value:
            peak_value, peak_index = value, i
        elif peak_value - value > best:
            best = peak_value - value
            found = (peak_index, i)
    return found


def thin(points: Sequence[Any], max_points: int, capital: Decimal | None = None) -> list[Any]:
    """At most `max_points` of `points`, in time order, keeping the largest drawdown exactly.

    A series of `max_points` or fewer is returned unchanged. Otherwise the
    points are split by index into `(max_points - 3) // 3` buckets and each
    bucket keeps its last, its minimum-equity and its maximum-equity point;
    the first point and the peak and trough of the largest drawdown are kept
    on top (three extra at most, which the bucket count leaves room for), so
    the result never exceeds `max_points`, always starts with the first point
    and ends with the last, and `stats.max_drawdown` of it equals the full
    series' (given the same `capital`, the first peak; it defaults to the
    first point's equity).
    """
    total = len(points)
    if total <= max_points:
        return list(points)
    if max_points < MIN_POINTS:
        raise ValueError(f"max_points must be at least {MIN_POINTS}")
    start = capital if capital is not None else points[0].equity
    keep = {0, total - 1}
    peak, trough = _worst_drawdown(points, start)
    keep.update(i for i in (peak, trough) if i is not None)
    buckets = (max_points - 3) // 3
    for b in range(buckets):
        lo, hi = b * total // buckets, (b + 1) * total // buckets
        indices = range(lo, hi)
        keep.add(hi - 1)
        keep.add(min(indices, key=lambda i: points[i].equity))
        keep.add(max(indices, key=lambda i: points[i].equity))
    return [points[i] for i in sorted(keep)]
