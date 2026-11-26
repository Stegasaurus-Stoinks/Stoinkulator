"""Fair value gap (FVG) detector, usable beside any other indicator.

A bullish gap is three consecutive bars where the low of candle 3 is above the
high of candle 1; the zone is [high1, low3]. A bearish gap is the mirror:
the high of candle 3 is below the low of candle 1; the zone is [high3, low1].
The zone is known at the close of candle 3.

Prices are compared as integer ticks of 0.01 (the float input rounded to the
nearest cent), so a gap of exactly 0.05 is exactly 0.05. The ATR and body
means use integer sums in a fixed-size window, which makes them exact too.
Fill fractions and ATR multiples are floats.

Work per bar depends on the number of live zones and the window size, never on
the number of bars seen. See docs/indicators.md.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pandas as pd

TICK = 100  # ticks per price unit (0.01)


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class State(StrEnum):
    OPEN = "open"
    TOUCHED = "touched"
    FILLED = "filled"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


_CLOSED_STATES = frozenset({State.FILLED, State.INVALIDATED, State.EXPIRED})
_INVALIDATE_ON = ("close_beyond_far_edge", "wick_beyond_far_edge")


@dataclass(frozen=True)
class FvgConfig:
    """All rules of the detector. See docs/indicators.md for each."""

    min_size: float = 0.05
    min_size_inclusive: bool = False
    min_size_atr: float | None = 0.25
    atr_bars: int = 20
    atr_min_bars: int = 5
    atr_cross_session: bool = True
    require_same_direction: bool = False
    min_displacement_body: float | None = None
    bar_minutes: int = 1
    expire_after_bars: int | None = None
    expire_at_session_end: bool = True
    invalidate_on: str = "close_beyond_far_edge"
    max_closed: int = 1000

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("min_size must be >= 0")
        if self.atr_bars < 1 or not 1 <= self.atr_min_bars <= self.atr_bars:
            raise ValueError("need 1 <= atr_min_bars <= atr_bars")
        if self.bar_minutes < 1:
            raise ValueError("bar_minutes must be >= 1")
        if self.invalidate_on not in _INVALIDATE_ON:
            raise ValueError(f"invalidate_on must be one of {_INVALIDATE_ON}")
        if self.expire_after_bars is not None and self.expire_after_bars < 1:
            raise ValueError("expire_after_bars must be >= 1 or None")
        if self.min_size_atr is not None and self.min_size_atr < 0:
            raise ValueError("min_size_atr must be >= 0 or None")
        if self.min_displacement_body is not None and self.min_displacement_body < 0:
            raise ValueError("min_displacement_body must be >= 0 or None")
        if self.max_closed < 0:
            raise ValueError("max_closed must be >= 0")


@dataclass
class Fvg:
    """One zone. Events and queries hand out copies: mutating one changes nothing."""

    id: int
    direction: Direction
    top: float
    bottom: float
    midpoint: float
    size: float
    size_atr: float | None
    formed_time: Any
    candle_times: tuple[Any, Any, Any]
    state: State = State.OPEN
    touches: int = 0
    deepest_fill: float = 0.0
    age_bars: int = 0
    last_change_time: Any = None
    top_ticks: int = 0
    bottom_ticks: int = 0

    @property
    def is_live(self) -> bool:
        return self.state not in _CLOSED_STATES


@dataclass(frozen=True)
class FvgEvent:
    kind: str  # formed, touched, filled, invalidated, expired
    zone: Fvg  # a copy as of this event
    time: Any


def _ticks(price: float) -> int:
    return round(float(price) * TICK)


class FvgDetector:
    """Incremental detector: call `update` once per closed bar, oldest first."""

    def __init__(self, config: FvgConfig | None = None) -> None:
        self.config = config or FvgConfig()
        cfg = self.config
        self._min_size_ticks = Decimal(str(cfg.min_size)) * TICK  # exact: 0.05 -> 5
        self._bars: deque[tuple[Any, int, int, int, int]] = deque(maxlen=3)  # t, o, h, l, c
        # State of the rolling windows after each of the last three bars:
        # (count, sum of true range, sum of body).
        self._snaps: deque[tuple[int, int, int]] = deque(maxlen=3)
        self._tr: deque[int] = deque(maxlen=cfg.atr_bars)
        self._body: deque[int] = deque(maxlen=cfg.atr_bars)
        self._tr_sum = 0
        self._body_sum = 0
        self._last_time: Any = None
        self._last_close_ticks: int | None = None
        self._session: Any = None
        self._live: list[Fvg] = []
        self._closed: deque[Fvg] = deque(maxlen=cfg.max_closed)
        self._next_id = 1
        self.bars_seen = 0
        self.last_visits = 0  # live zones visited by the last update
        self.total_visits = 0

    # ----- feeding -----

    def update(
        self,
        time: Any,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float | None = None,
    ) -> list[FvgEvent]:
        """Process one closed bar and return the events it caused, in order."""
        if self._last_time is not None and not time > self._last_time:
            raise ValueError(f"bar at {time} is not after the previous bar at {self._last_time}")
        o, h, lo, c = _ticks(open), _ticks(high), _ticks(low), _ticks(close)
        cfg = self.config
        events: list[FvgEvent] = []
        session = time.date()
        new_session = self._session is not None and session != self._session

        if new_session and cfg.expire_at_session_end:
            for z in self._live:
                self._close(z, State.EXPIRED, time, events)
            self._live = []
        if new_session and not cfg.atr_cross_session:
            self._tr.clear()
            self._body.clear()
            self._tr_sum = self._body_sum = 0

        # Existing zones first: candle 3 never touches its own zone.
        self.last_visits = len(self._live)
        self.total_visits += self.last_visits
        if self._live:
            still: list[Fvg] = []
            for z in self._live:
                self._advance(z, time, h, lo, c, events)
                if z.is_live:
                    still.append(z)
            self._live = still

        # Rolling windows, then the snapshot after this bar.
        prev_close = self._last_close_ticks
        if prev_close is None or (new_session and not cfg.atr_cross_session):
            tr = h - lo
        else:
            tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        body = abs(c - o)
        if len(self._tr) == self._tr.maxlen:
            self._tr_sum -= self._tr[0]
            self._body_sum -= self._body[0]
        self._tr.append(tr)
        self._body.append(body)
        self._tr_sum += tr
        self._body_sum += body

        self._bars.append((time, o, h, lo, c))
        self._snaps.append((len(self._tr), self._tr_sum, self._body_sum))
        self._last_time = time
        self._last_close_ticks = c
        self._session = session
        self.bars_seen += 1

        zone = self._try_form(time)
        if zone is not None:
            self._live.append(zone)
            events.append(FvgEvent("formed", replace(zone), time))
        return events

    # ----- formation -----

    def _try_form(self, time: Any) -> Fvg | None:
        if len(self._bars) < 3:
            return None
        cfg = self.config
        (t1, o1, h1, l1, c1), (t2, o2, h2, l2, c2), (t3, o3, h3, l3, c3) = self._bars
        step = timedelta(minutes=cfg.bar_minutes)
        if t2 - t1 != step or t3 - t2 != step:
            return None
        if not (t1.date() == t2.date() == t3.date()):
            return None
        if l3 > h1:
            direction = Direction.UP
            size = l3 - h1
            top, bottom = l3, h1
        elif l1 > h3:
            direction = Direction.DOWN
            size = l1 - h3
            top, bottom = l1, h3
        else:
            return None
        if cfg.require_same_direction:
            if direction is Direction.UP and not (o1 < c1 and o2 < c2 and o3 < c3):
                return None
            if direction is Direction.DOWN and not (o1 > c1 and o2 > c2 and o3 > c3):
                return None
        if size < self._min_size_ticks or (size == self._min_size_ticks and not cfg.min_size_inclusive):
            return None
        # Windows as they were after candle 1 (two bars ago).
        n, tr_sum, body_sum = self._snaps[0]
        enough = n >= cfg.atr_min_bars
        if cfg.min_size_atr is not None and (not enough or size * n < cfg.min_size_atr * tr_sum):
            return None
        if cfg.min_displacement_body is not None:
            if not enough or abs(c2 - o2) * n < cfg.min_displacement_body * body_sum:
                return None
        size_atr = size * n / tr_sum if enough and tr_sum > 0 else None
        zone = Fvg(
            id=self._next_id,
            direction=direction,
            top=top / TICK,
            bottom=bottom / TICK,
            midpoint=(top + bottom) / (2 * TICK),
            size=size / TICK,
            size_atr=size_atr,
            formed_time=t3,
            candle_times=(t1, t2, t3),
            last_change_time=t3,
            top_ticks=top,
            bottom_ticks=bottom,
        )
        self._next_id += 1
        return zone

    # ----- lifecycle -----

    def _advance(self, z: Fvg, time: Any, h: int, lo: int, c: int, events: list[FvgEvent]) -> None:
        cfg = self.config
        z.age_bars += 1
        up = z.direction is Direction.UP
        size = z.top_ticks - z.bottom_ticks
        # Mirrored rules. Depth is measured from the near edge.
        if up:
            entered = lo < z.top_ticks
            depth = z.top_ticks - lo
            reached_far = lo <= z.bottom_ticks
            wick_beyond = lo < z.bottom_ticks
            close_beyond = c < z.bottom_ticks
        else:
            entered = h > z.bottom_ticks
            depth = h - z.bottom_ticks
            reached_far = h >= z.top_ticks
            wick_beyond = h > z.top_ticks
            close_beyond = c > z.top_ticks
        if entered:
            z.touches += 1
            z.deepest_fill = max(z.deepest_fill, min(1.0, depth / size))
            if z.state is State.OPEN:
                z.state = State.TOUCHED
                z.last_change_time = time
                events.append(FvgEvent("touched", replace(z), time))
            invalid = close_beyond if cfg.invalidate_on == "close_beyond_far_edge" else wick_beyond
            if invalid:
                self._close(z, State.INVALIDATED, time, events)
                return
            if reached_far:
                self._close(z, State.FILLED, time, events)
                return
        if cfg.expire_after_bars is not None and z.age_bars >= cfg.expire_after_bars:
            self._close(z, State.EXPIRED, time, events)

    def _close(self, z: Fvg, state: State, time: Any, events: list[FvgEvent]) -> None:
        z.state = state
        z.last_change_time = time
        if self._closed.maxlen:
            self._closed.append(z)
        events.append(FvgEvent(state.value, replace(z), time))

    # ----- queries -----

    def live_zones(self, direction: Direction | str | None = None) -> list[Fvg]:
        """Copies of the live zones (open or touched), oldest first."""
        d = None if direction is None else Direction(direction)
        return [replace(z) for z in self._live if d is None or z.direction is d]

    def closed_zones(self) -> list[Fvg]:
        """Copies of the most recent closed zones (at most `max_closed`), oldest first."""
        return [replace(z) for z in self._closed]

    def zone_containing(self, price: float) -> Fvg | None:
        """The newest live zone with bottom <= price <= top, or None."""
        p = _ticks(price)
        for z in reversed(self._live):
            if z.bottom_ticks <= p <= z.top_ticks:
                return replace(z)
        return None

    def nearest_zone(
        self, price: float, direction: Direction | str | None = None, side: str | None = None
    ) -> Fvg | None:
        """Live zone nearest to `price` (0 distance when inside), ties to the newest.

        `side` "above" keeps zones entirely above the price, "below" those
        entirely below, None any (including a zone containing the price).
        """
        best = self._nearest(_ticks(price), direction, side)
        return None if best is None else replace(best[1])

    def distance_to_nearest(
        self, price: float, direction: Direction | str | None = None, side: str | None = None
    ) -> float | None:
        """Distance in price units to the nearest live zone, or None if there is none."""
        best = self._nearest(_ticks(price), direction, side)
        return None if best is None else best[0] / TICK

    def _nearest(
        self, p: int, direction: Direction | str | None, side: str | None
    ) -> tuple[int, Fvg] | None:
        if side not in (None, "above", "below"):
            raise ValueError("side must be None, 'above' or 'below'")
        d = None if direction is None else Direction(direction)
        best: tuple[int, Fvg] | None = None
        for z in reversed(self._live):  # newest first, so ties keep the newest
            if d is not None and z.direction is not d:
                continue
            if side == "above" and not z.bottom_ticks > p:
                continue
            if side == "below" and not z.top_ticks < p:
                continue
            dist = max(z.bottom_ticks - p, p - z.top_ticks, 0)
            if best is None or dist < best[0]:
                best = (dist, z)
        return best

    def snapshot(self) -> dict[str, float]:
        """Flat plain numbers about the live zones at the last close (NaN where absent)."""
        nan = math.nan
        close = nan if self._last_close_ticks is None else self._last_close_ticks / TICK
        out: dict[str, float] = {
            "live": float(len(self._live)),
            "live_up": float(sum(z.direction is Direction.UP for z in self._live)),
            "live_down": float(sum(z.direction is Direction.DOWN for z in self._live)),
            "in_zone": 0.0,
            "dist_above": nan,
            "dist_below": nan,
            "newest_direction": 0.0,
            "newest_top": nan,
            "newest_bottom": nan,
            "newest_age_bars": nan,
        }
        if self._last_close_ticks is None:
            return out
        p = self._last_close_ticks
        out["in_zone"] = float(self.zone_containing(close) is not None)
        above = self._nearest(p, None, "above")
        below = self._nearest(p, None, "below")
        if above is not None:
            out["dist_above"] = above[0] / TICK
        if below is not None:
            out["dist_below"] = below[0] / TICK
        if self._live:
            z = self._live[-1]
            out["newest_direction"] = 1.0 if z.direction is Direction.UP else -1.0
            out["newest_top"] = z.top
            out["newest_bottom"] = z.bottom
            out["newest_age_bars"] = float(z.age_bars)
        return out


_COLUMNS = (
    "id", "direction", "top", "bottom", "midpoint", "size", "size_atr", "formed_time",
    "time_1", "time_2", "time_3", "state", "touches", "deepest_fill", "age_bars", "last_change_time",
)


def _bar_rows(frame: pd.DataFrame) -> Iterable[tuple[Any, ...]]:
    time_col = frame["date"] if "date" in frame.columns else frame.index.to_series()
    times = pd.DatetimeIndex(time_col).to_pydatetime()
    vol = frame["volume"].to_numpy() if "volume" in frame.columns else [None] * len(frame)
    return zip(
        times,
        frame["open"].to_numpy(),
        frame["high"].to_numpy(),
        frame["low"].to_numpy(),
        frame["close"].to_numpy(),
        vol,
        strict=True,
    )


def detect_fvgs(frame: pd.DataFrame, config: FvgConfig | None = None) -> pd.DataFrame:
    """Run the detector over `frame` and return one row per zone with its final state.

    `frame` needs `open`, `high`, `low`, `close` and times in a `date` column
    (or a DatetimeIndex). It is not modified. The zones are exactly those of
    an `FvgDetector` fed the same bars (it is one), including every closed zone
    whatever `max_closed` says.
    """
    det = FvgDetector(config)
    zones: dict[int, Fvg] = {}
    for t, o, h, lo, c, v in _bar_rows(frame):
        for ev in det.update(t, o, h, lo, c, v):
            zones[ev.zone.id] = ev.zone
    for z in det.live_zones():  # live ones are not in the events beyond their last change
        zones[z.id] = z
    rows = [
        (
            z.id, z.direction.value, z.top, z.bottom, z.midpoint, z.size, z.size_atr, z.formed_time,
            *z.candle_times, z.state.value, z.touches, z.deepest_fill, z.age_bars, z.last_change_time,
        )
        for z in zones.values()
    ]
    return pd.DataFrame(rows, columns=list(_COLUMNS))


def resample_session(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1-minute bars to `minutes`-minute bars, aligned to 09:30 of each session.

    Bars before 09:30 are ignored. The last bar of a session is dropped when it
    holds fewer than `minutes` source bars. The result has columns `date`,
    `open`, `high`, `low`, `close` (and `volume` if the input has it). The
    input is not modified.
    """
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    times = pd.DatetimeIndex(frame["date"] if "date" in frame.columns else frame.index)
    day = times.normalize()
    offset = ((times - day) / pd.Timedelta(minutes=1)).astype(int) - (9 * 60 + 30)
    keep = offset >= 0
    work = pd.DataFrame(
        {
            "day": day[keep],
            "bucket": offset[keep] // minutes,
            "open": frame["open"].to_numpy()[keep],
            "high": frame["high"].to_numpy()[keep],
            "low": frame["low"].to_numpy()[keep],
            "close": frame["close"].to_numpy()[keep],
        }
    )
    has_volume = "volume" in frame.columns
    if has_volume:
        work["volume"] = frame["volume"].to_numpy()[keep]
    agg: dict[str, Any] = {"open": "first", "high": "max", "low": "min", "close": "last", "n": "size"}
    work["n"] = 1
    if has_volume:
        agg["volume"] = "sum"
    out = work.groupby(["day", "bucket"], sort=True).agg(agg).reset_index()
    last = out.groupby("day")["bucket"].transform("max") == out["bucket"]
    out = out[~(last & (out["n"] < minutes))].copy()
    out["date"] = out["day"] + pd.to_timedelta(9 * 60 + 30 + out["bucket"] * minutes, unit="min")
    cols = ["date", "open", "high", "low", "close", *(["volume"] if has_volume else [])]
    return out[cols].reset_index(drop=True)


__all__ = [
    "Direction", "Fvg", "FvgConfig", "FvgDetector", "FvgEvent", "State", "detect_fvgs",
    "resample_session",
]
