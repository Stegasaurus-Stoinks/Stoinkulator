"""Higher-timeframe trend from 1-minute bars: an EMA of completed N-minute closes.

Feed 1-minute bars (time = bar START, naive New York time) to `TrendIndicator.update`,
oldest first. It builds `bar_minutes` buckets aligned to 09:30 of each session, the
same alignment and the same completeness rule as `fvg.resample_session`: a bucket is
complete when all its minutes arrived, or when a later bar of the same session shows
that it ended (a missing minute is allowed); a session's last bucket that never
completes is dropped. Bars before 09:30 are ignored.

Only COMPLETED buckets feed the EMA, so the state after a bar never depends on a bar
after it (no look-ahead): `direction` at minute t is what a process that has seen the
bars up to t knows.

Rule: `up` when the last completed close is above the EMA of the completed closes AND
the EMA is higher than one bucket earlier; `down` is the mirror; otherwise `none`.
Until `ema_bars` buckets have completed (`ready` is False) the direction is `none`.
With `ema_bars` 1 the EMA equals the last close, so the direction is always `none`.
For `ema_bars` >= 2 the two conditions are one: both hold exactly when the last
completed close is above the EMA as it was before that close (`down`: below).

The EMA uses `alpha = 2 / (ema_bars + 1)` and is seeded with the first completed
close. Work per bar is constant. See docs/indicators.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

SESSION_OPEN_MINUTE = 9 * 60 + 30  # 09:30


@dataclass(frozen=True)
class TrendConfig:
    """Rules of the indicator. See docs/indicators.md."""

    bar_minutes: int = 30
    ema_bars: int = 20
    cross_session: bool = True  # False: the EMA starts again each session

    def __post_init__(self) -> None:
        bars = self.bar_minutes
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < 2:
            raise ValueError("bar_minutes must be a whole number >= 2")
        if isinstance(self.ema_bars, bool) or not isinstance(self.ema_bars, int) or self.ema_bars < 1:
            raise ValueError("ema_bars must be a whole number >= 1")
        if not isinstance(self.cross_session, bool):
            raise ValueError("cross_session must be true or false")


class TrendIndicator:
    """Incremental trend: call `update` once per closed 1-minute bar, oldest first."""

    def __init__(self, config: TrendConfig | None = None) -> None:
        self.config = config or TrendConfig()
        self._alpha = 2.0 / (self.config.ema_bars + 1)
        self._last_time: Any = None
        self._day: date | None = None
        # The bucket being built: its index in the session, its start, its count and last close.
        self._index = -1
        self._label: Any = None
        self._count = 0
        self._bucket_close = 0.0
        self.direction = "none"
        self.ready = False
        self.ema: float | None = None
        self.last_close: float | None = None
        self.bars_seen = 0  # completed buckets in the EMA (since the last reset)
        self.known_at: Any = None
        self._prev_ema: float | None = None

    def update(self, time: Any, open: float, high: float, low: float, close: float) -> None:  # noqa: A002
        """Take one closed 1-minute bar. Raises ValueError for NaN, inf or time going backwards."""
        for name, value in (("open", open), ("high", high), ("low", low), ("close", close)):
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} {value!r} is not finite")
        if self._last_time is not None and time <= self._last_time:
            raise ValueError(f"time {time!r} is not after the previous bar {self._last_time!r}")
        self._last_time = time
        offset = time.hour * 60 + time.minute - SESSION_OPEN_MINUTE
        if offset < 0:
            return
        day = time.date()
        index = offset // self.config.bar_minutes
        if day != self._day:
            # A new session: the last bucket of the old one is dropped (it never completed).
            self._day = day
            self._count = 0
            if not self.config.cross_session:
                self._reset_ema()
        elif index != self._index and self._count:
            self._complete()  # a later bucket began: the last one is final
        if self._count == 0:
            self._index = index
            self._label = time - timedelta(minutes=offset % self.config.bar_minutes)
        self._count += 1
        self._bucket_close = float(close)
        if self._count == self.config.bar_minutes:
            self._complete()

    def _reset_ema(self) -> None:
        self.ema = self._prev_ema = self.last_close = None
        self.bars_seen = 0
        self.ready = False
        self.direction = "none"

    def _complete(self) -> None:
        close = self._bucket_close
        self._prev_ema = self.ema
        self.ema = close if self.ema is None else self.ema + self._alpha * (close - self.ema)
        self.last_close = close
        self.bars_seen += 1
        self.known_at = self._label + timedelta(minutes=self.config.bar_minutes)
        self._count = 0
        self.ready = self.bars_seen >= self.config.ema_bars
        self.direction = "none"
        if self.ready and self._prev_ema is not None:
            if close > self.ema and self.ema > self._prev_ema:
                self.direction = "up"
            elif close < self.ema and self.ema < self._prev_ema:
                self.direction = "down"


def trend_series(frame: pd.DataFrame, config: TrendConfig | None = None) -> pd.DataFrame:
    """The trend after each 1-minute bar of `frame` (columns `date` or the index, open..close).

    One row per input row: `date`, `direction`, `ready`, `ema`, `last_close`,
    `bars_seen`, `known_at` as known at that bar's close. For research; the input is
    not modified.
    """
    indicator = TrendIndicator(config)
    times = pd.DatetimeIndex(frame["date"] if "date" in frame.columns else frame.index)
    rows = []
    for when, o, h, lo, c in zip(
        times, frame["open"].tolist(), frame["high"].tolist(), frame["low"].tolist(),
        frame["close"].tolist(), strict=True,
    ):
        indicator.update(when, o, h, lo, c)
        rows.append((when, indicator.direction, indicator.ready,
                     math.nan if indicator.ema is None else indicator.ema,
                     math.nan if indicator.last_close is None else indicator.last_close,
                     indicator.bars_seen,
                     pd.NaT if indicator.known_at is None else indicator.known_at))
    return pd.DataFrame(
        rows, columns=["date", "direction", "ready", "ema", "last_close", "bars_seen", "known_at"],
    )


__all__ = ["TrendConfig", "TrendIndicator", "trend_series"]
