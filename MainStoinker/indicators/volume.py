"""Relative volume against the previous bars: a bar's volume against the mean of the N bars before it.

Feed COMPLETED bars (any timeframe) oldest first, one `update(time, volume)` each. The bars before a
bar are counted straight through, across session boundaries, the way an ATR over bars is (there is no
reset at the open). A bar is compared with the `bars` bars immediately before it, itself and later
bars never included, so nothing after a bar can change its answer. Until `bars` earlier bars exist
every query says None (not available).

The answer for a bar is fixed when the bar is fed, and kept for the bars of the CURRENT calendar date
only (the memory is bounded by one session's bars; a query for an earlier date's bar says None). Work
per bar is constant. Feed integers (shares) and the sums are exact; `at_least` compares by
cross-multiplication, `volume * bars >= multiple * sum of the previous bars`, with the multiple taken as
the decimal it is written as, so no division and no float rounding decides a trade. See
docs/indicators.md.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import date
from decimal import Decimal
from typing import Any


def _finite(name: str, value: Any) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{name} {value!r} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} {value!r} is not a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} {value!r} is not finite")


def _multiple(value: Any) -> Decimal:
    """A multiple as the decimal it is written as; finite and > 0."""
    _finite("multiple", value)
    if float(value) <= 0:
        raise ValueError(f"multiple must be > 0, got {value!r}")
    return Decimal(str(value))


class PrevBarsVolume:
    """Incremental relative volume against the previous `bars` bars: one `update` per completed bar."""

    def __init__(self, bars: int = 20) -> None:
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < 1:
            raise ValueError("bars must be a whole number >= 1")
        self.bars = bars
        self._last_time: Any = None
        self._day: date | None = None
        self._window: deque[int | float] = deque(maxlen=bars)  # the last `bars` volumes, across days
        self._sum: int | float = 0
        # This date's bars by start minute of day: (volume, sum of the `bars` bars before it or None).
        self._today: dict[int, tuple[int | float, int | float | None]] = {}

    def update(self, time: Any, volume: int | float) -> None:
        """Take one completed bar. ValueError for a bad volume or a time not after the previous bar."""
        _finite("volume", volume)
        if volume < 0:
            raise ValueError(f"volume {volume!r} is negative")
        if self._last_time is not None and time <= self._last_time:
            raise ValueError(f"time {time!r} is not after the previous bar {self._last_time!r}")
        self._last_time = time
        day = time.date()
        if day != self._day:
            self._day = day
            self._today = {}
        before = self._sum if len(self._window) == self.bars else None
        self._today[time.hour * 60 + time.minute] = (volume, before)
        if len(self._window) == self.bars:
            self._sum -= self._window[0]
        self._window.append(volume)
        self._sum += volume

    def _found(self, start: Any) -> tuple[int | float, int | float | None] | None:
        """This date's bar that starts at `start` (a datetime/Timestamp, or a minute of day); else None.

        A datetime of another date than the one being fed is not a bar of this date: None.
        """
        if isinstance(start, bool):
            raise ValueError(f"start {start!r} is not a time")
        if isinstance(start, int):
            return self._today.get(start)
        try:
            minute = int(start.hour * 60 + start.minute)
            day = start.date()
        except AttributeError:
            raise ValueError(f"start {start!r} is not a time") from None
        return self._today.get(minute) if day == self._day else None

    def volume(self, start: Any) -> int | float | None:
        """The volume of this date's bar that starts at `start`, else None."""
        found = self._found(start)
        return None if found is None else found[0]

    def mean(self, start: Any) -> float | None:
        """Mean volume of the `bars` bars before the bar at `start`; None until that many exist."""
        found = self._found(start)
        if found is None or found[1] is None:
            return None
        return found[1] / self.bars

    def relative_volume(self, start: Any) -> float | None:
        """Volume of the bar / the mean of the bars before it; None if unavailable or the mean is 0."""
        volume, mean = self.volume(start), self.mean(start)
        if volume is None or mean is None or mean == 0:
            return None
        return volume / mean

    def at_least(self, start: Any, multiple: float) -> bool | None:
        """Is the bar's volume >= `multiple` x the mean of the bars before it? None if unavailable.

        Exact: `volume * bars >= multiple * sum of the previous bars`.
        """
        factor = _multiple(multiple)  # a bad multiple is an error even when nothing is known yet
        found = self._found(start)
        if found is None or found[1] is None:
            return None
        return Decimal(str(found[0])) * self.bars >= factor * Decimal(str(found[1]))


__all__ = ["PrevBarsVolume"]
