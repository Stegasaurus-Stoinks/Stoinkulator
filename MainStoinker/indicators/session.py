"""Session indicators from 1-minute bars: volume by time slot and the daily range.

Feed 1-minute bars (time = bar START, naive New York time) oldest first. Bars before
09:30 are ignored. A session is one calendar date.

`SlotVolume` builds `bar_minutes` buckets aligned to 09:30 (the same alignment and
completeness rule as `trend.TrendIndicator` and `fvg.resample_session`: a bucket is
complete when all its minutes arrived, or when a later bar of the same session shows
that it ended; a session's last bucket that never completes is dropped). For each
completed bucket of the CURRENT session it knows the bucket's volume and the mean
volume of the same slot (same start time) over the previous `sessions` sessions that
HAVE that slot (a half day has no afternoon slots, so those sessions are simply not in
the afternoon history). A session's buckets join the history only when a later session
starts, so nothing of the current session leaks into its own comparison. A slot is
available once `sessions` earlier sessions have it; before that every query says None.

`DailyRange` knows the session open (the open of the 09:30 bar; None when the session's
first bar is later) and the average daily range (ADR): the mean of high - low over the
previous `sessions` sessions (a session joins when the next one starts; ready only when
`sessions` of them exist).

Both take plain numbers. Feed integers (cents, shares) and the sums are exact; the
`at_least` / `distance_at_least` queries compare by cross-multiplication,
`value * n >= multiple * total`, with the multiple taken as the decimal it is written
as, so no division and no float rounding decides a trade. Work per bar is constant;
memory is bounded by `sessions`. See docs/indicators.md.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import date
from decimal import Decimal
from typing import Any

SESSION_OPEN_MINUTE = 9 * 60 + 30  # 09:30


def _sessions_ok(sessions: Any) -> None:
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
        raise ValueError("sessions must be a whole number >= 1")


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


class SlotVolume:
    """Incremental relative volume by time slot: call `update` once per closed 1-minute bar."""

    def __init__(self, bar_minutes: int, sessions: int = 20) -> None:
        if isinstance(bar_minutes, bool) or not isinstance(bar_minutes, int) or bar_minutes < 1:
            raise ValueError("bar_minutes must be a whole number >= 1")
        _sessions_ok(sessions)
        self.bar_minutes = bar_minutes
        self.sessions = sessions
        self._last_time: Any = None
        self._day: date | None = None
        # The bucket being built: its index in the session, its start (minute of day), count, volume.
        self._index = -1
        self._start = 0
        self._count = 0
        self._volume: int | float = 0
        self._today: dict[int, int | float] = {}  # completed buckets of this session by start minute
        self._history: dict[int, deque[int | float]] = {}  # earlier sessions, per slot
        self._sums: dict[int, int | float] = {}

    def update(self, time: Any, volume: int | float) -> None:
        """Take one closed 1-minute bar's volume. ValueError for a bad volume or time going backwards."""
        _finite("volume", volume)
        if volume < 0:
            raise ValueError(f"volume {volume!r} is negative")
        if self._last_time is not None and time <= self._last_time:
            raise ValueError(f"time {time!r} is not after the previous bar {self._last_time!r}")
        self._last_time = time
        offset = time.hour * 60 + time.minute - SESSION_OPEN_MINUTE
        if offset < 0:
            return
        day = time.date()
        index = offset // self.bar_minutes
        if day != self._day:
            self._join_today()  # the session before is complete now
            self._day = day
            self._count = 0  # the unfinished last bucket of the old session is dropped
        elif index != self._index and self._count:
            self._complete()  # a later bucket began: the last one is final
        if self._count == 0:
            self._index = index
            self._start = SESSION_OPEN_MINUTE + index * self.bar_minutes
            self._volume = 0
        self._count += 1
        self._volume += volume
        if self._count == self.bar_minutes:
            self._complete()

    def _complete(self) -> None:
        self._today[self._start] = self._volume
        self._count = 0

    def _join_today(self) -> None:
        for start, volume in self._today.items():
            history = self._history.get(start)
            if history is None:
                history = self._history[start] = deque(maxlen=self.sessions)
            history.append(volume)
            self._sums[start] = sum(history)
        self._today = {}

    @staticmethod
    def _slot(start: Any) -> int:
        """The slot (minute of day) of a start time given as a datetime/Timestamp or a minute of day."""
        if isinstance(start, bool):
            raise ValueError(f"start {start!r} is not a time")
        if isinstance(start, int):
            return start
        try:
            return int(start.hour * 60 + start.minute)
        except AttributeError:
            raise ValueError(f"start {start!r} is not a time") from None

    def volume(self, start: Any) -> int | float | None:
        """The volume of the completed bucket of this session that starts at `start`, else None."""
        return self._today.get(self._slot(start))

    def history_count(self, start: Any) -> int:
        """How many earlier sessions (at most `sessions`) the slot has."""
        history = self._history.get(self._slot(start))
        return 0 if history is None else len(history)

    def mean(self, start: Any) -> float | None:
        """Mean volume of the slot over the previous `sessions` sessions that have it.

        None until that many sessions have the slot.
        """
        slot = self._slot(start)
        history = self._history.get(slot)
        if history is None or len(history) < self.sessions:
            return None
        return self._sums[slot] / self.sessions

    def relative_volume(self, start: Any) -> float | None:
        """Volume of the completed bucket / the slot mean; None if either is unavailable or the mean is 0."""
        volume = self.volume(start)
        mean = self.mean(start)
        if volume is None or mean is None or mean == 0:
            return None
        return volume / mean

    def at_least(self, start: Any, multiple: float) -> bool | None:
        """Is the bucket's volume >= `multiple` x the slot mean? None if bucket or mean is unavailable.

        Exact: `volume * sessions >= multiple * sum of the slot's history`.
        """
        factor = _multiple(multiple)  # a bad multiple is an error even when nothing is known yet
        slot = self._slot(start)
        volume = self._today.get(slot)
        history = self._history.get(slot)
        if volume is None or history is None or len(history) < self.sessions:
            return None
        return Decimal(str(volume)) * self.sessions >= factor * Decimal(str(self._sums[slot]))


class DailyRange:
    """Incremental session open and average daily range: call `update` once per closed 1-minute bar."""

    def __init__(self, sessions: int = 20) -> None:
        _sessions_ok(sessions)
        self.sessions = sessions
        self._last_time: Any = None
        self._day: date | None = None
        self._high: int | float = 0
        self._low: int | float = 0
        self._open: int | float | None = None
        self._ranges: deque[int | float] = deque(maxlen=sessions)
        self._sum: int | float = 0

    def update(self, time: Any, open: float, high: float, low: float, close: float) -> None:  # noqa: A002
        """Take one closed 1-minute bar. ValueError for NaN, inf, high < low or time going backwards."""
        for name, value in (("open", open), ("high", high), ("low", low), ("close", close)):
            _finite(name, value)
        if high < low:
            raise ValueError(f"high {high!r} is below low {low!r}")
        if self._last_time is not None and time <= self._last_time:
            raise ValueError(f"time {time!r} is not after the previous bar {self._last_time!r}")
        self._last_time = time
        offset = time.hour * 60 + time.minute - SESSION_OPEN_MINUTE
        if offset < 0:
            return
        day = time.date()
        if day != self._day:
            if self._day is not None:  # the session before is complete now
                self._ranges.append(self._high - self._low)
                self._sum = sum(self._ranges)
            self._day = day
            self._high, self._low = high, low
            self._open = open if offset == 0 else None
        else:
            self._high = max(self._high, high)
            self._low = min(self._low, low)

    @property
    def session_open(self) -> int | float | None:
        """The open of the current session's 09:30 bar; None before any bar or when that bar is missing."""
        return self._open

    @property
    def sessions_seen(self) -> int:
        """Completed sessions in the window (at most `sessions`)."""
        return len(self._ranges)

    @property
    def ready(self) -> bool:
        return len(self._ranges) >= self.sessions

    @property
    def range_sum(self) -> int | float | None:
        """The sum of the window's session ranges (exact for integer input); None until ready."""
        return self._sum if self.ready else None

    @property
    def adr(self) -> float | None:
        """Mean (high - low) of the previous `sessions` completed sessions; None until ready."""
        return self._sum / self.sessions if self.ready else None

    def distance_at_least(self, distance: float, multiple: float) -> bool | None:
        """Is the signed `distance` >= `multiple` x ADR? None until ready.

        Exact: `distance * sessions >= multiple * range_sum`. The caller signs the distance
        for the side it asks about (price - session open for a long, the negative for a short).
        """
        _finite("distance", distance)
        factor = _multiple(multiple)
        if not self.ready:
            return None
        return Decimal(str(distance)) * self.sessions >= factor * Decimal(str(self._sum))


__all__ = ["DailyRange", "SlotVolume"]
