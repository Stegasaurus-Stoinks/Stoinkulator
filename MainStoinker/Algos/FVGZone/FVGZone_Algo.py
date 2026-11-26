"""FVGZone: fair value gap zones from `MainStoinker.indicators.fvg`, plus a trend filter.

Reference for how an algo consumes the detector. The rules, parameters and limits
are in `docs/algos-fvgzone.md`; this docstring only says where each lives.

The algo receives 1-minute bars. It builds `zone_minutes` bars itself, one
minute at a time (no frame copies, no per-bar work that grows with history),
and feeds every COMPLETED higher-timeframe bar to an `FvgDetector`. A zone is
traded when a later 1-minute bar retests it and closes back out of it, with
the session VWAP and an EMA of the 1-minute closes agreeing with the side.

Prices are kept as integer cents (ticks of 0.01) wherever a comparison decides
something, so the long and the short side are exact mirrors of each other.

Everything the algo keeps per bar is O(1) amortised; `AlgoData` (the indicator
series the engine reads after a run) is built lazily from plain lists.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd

from MainStoinker.Algos.ParentAlgo import ParentAlgo
from MainStoinker.engine.gaprule import RefusedEntry
from MainStoinker.indicators.fvg import Direction, FvgConfig, FvgDetector

SESSION_OPEN_MINUTE = 9 * 60 + 30  # 09:30
SESSION_MINUTES = 390  # 09:30 to 16:00

# Keys the engine and the CLI put in a config object besides the algo's own parameters.
GENERAL_KEYS = frozenset({"idname", "ticker", "mode"})

DEFAULTS: dict[str, Any] = {
    "zone_minutes": 5,
    "trend_filter": True,
    "trend_ema": 50,
    "RRRatio": 2,
    "stop_buffer": 0.02,
    "min_risk": 0.10,
    "max_risk_atr": 3,
    "risk_dollars": 50,
    "max_shares": 500,
    "first_entry": "09:45",
    "last_entry": "15:30",
    "flat_time": "15:55",
    "min_size_atr": 0.25,
}

# Series for the dashboard: (column, draw type). 'line' is drawn smooth, 'segment' stepped.
SERIES: tuple[tuple[str, str], ...] = (
    ("VWAP", "line"),
    ("EMA", "line"),
    ("ZoneAboveTop", "segment"),
    ("ZoneAboveBottom", "segment"),
    ("ZoneBelowTop", "segment"),
    ("ZoneBelowBottom", "segment"),
    ("StopPrice", "segment"),
    ("tp", "segment"),
)

_NAN = math.nan
_TICK = 100


class ConfigError(ValueError):
    """An invalid or unknown algo parameter (raised at construction)."""


def _fail(who: str, message: str) -> ConfigError:
    return ConfigError(f"FVGZone {who}: {message}")


def _number(who: str, cfg: dict, key: str) -> float:
    value = cfg.get(key, DEFAULTS[key])
    if isinstance(value, bool):
        raise _fail(who, f"{key} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _fail(who, f"{key} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise _fail(who, f"{key} must be finite, got {value!r}")
    return number


def _integer(who: str, cfg: dict, key: str, low: int, high: int | None = None) -> int:
    number = _number(who, cfg, key)
    if number != int(number):
        raise _fail(who, f"{key} must be a whole number, got {cfg.get(key)!r}")
    result = int(number)
    if result < low or (high is not None and result > high):
        span = f">= {low}" if high is None else f"from {low} to {high}"
        raise _fail(who, f"{key} must be {span}, got {result}")
    return result


def _cents(who: str, cfg: dict, key: str, low: float = 0.0) -> int:
    """A money parameter as whole cents (it must be a whole number of cents)."""
    number = _number(who, cfg, key)
    scaled = number * _TICK
    if abs(scaled - round(scaled)) > 1e-6:
        raise _fail(who, f"{key} must be a whole number of cents, got {cfg.get(key)!r}")
    if number < low:
        raise _fail(who, f"{key} must be >= {low}, got {cfg.get(key)!r}")
    return round(scaled)


def _clock(who: str, cfg: dict, key: str) -> int:
    """'HH:MM' as minutes since midnight."""
    value = cfg.get(key, DEFAULTS[key])
    try:
        text = str(value)
        hours, minutes = text.split(":")
        if len(hours) != 2 or len(minutes) != 2:
            raise ValueError
        h, m = int(hours), int(minutes)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except (TypeError, ValueError):
        raise _fail(who, f"{key} must be a time 'HH:MM', got {value!r}") from None
    return h * 60 + m


def _cent_ticks(price: float) -> int:
    """A price in whole cents; raises for a price that is not a cent price."""
    scaled = float(price) * _TICK
    if not math.isfinite(scaled) or abs(scaled - round(scaled)) > 1e-6:
        raise ValueError(f"FVGZone: price {price!r} is not a cent price")
    return round(scaled)


class _Bucket:
    """The higher-timeframe bar being built (all prices in cents)."""

    __slots__ = ("index", "label", "open", "high", "low", "close", "count")

    def __init__(self, index: int, label: datetime, o: int, h: int, lo: int, c: int) -> None:
        self.index, self.label = index, label
        self.open, self.high, self.low, self.close = o, h, lo, c
        self.count = 1


class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        cfg = dict(algoConfigData)
        who = repr(cfg.get("idname", "?"))
        unknown = sorted(set(cfg) - GENERAL_KEYS - set(DEFAULTS))
        if unknown:
            raise _fail(who, f"unknown parameter(s) {unknown}; known: {sorted(DEFAULTS)}")
        self.zone_minutes = _integer(who, cfg, "zone_minutes", 1, SESSION_MINUTES)
        trend_filter = cfg.get("trend_filter", DEFAULTS["trend_filter"])
        if not isinstance(trend_filter, bool):
            raise _fail(who, f"trend_filter must be true or false, got {trend_filter!r}")
        self.trend_filter = trend_filter
        self.trend_ema = _integer(who, cfg, "trend_ema", 1)
        self.RRRatio = _number(who, cfg, "RRRatio")
        if self.RRRatio <= 0:
            raise _fail(who, f"RRRatio must be > 0, got {cfg.get('RRRatio')!r}")
        self.stop_buffer_ticks = _cents(who, cfg, "stop_buffer")
        self.min_risk_ticks = _cents(who, cfg, "min_risk")
        self.max_risk_atr = _number(who, cfg, "max_risk_atr")
        if self.max_risk_atr <= 0:
            raise _fail(who, f"max_risk_atr must be > 0, got {cfg.get('max_risk_atr')!r}")
        self.risk_dollars = _number(who, cfg, "risk_dollars")
        if self.risk_dollars <= 0:
            raise _fail(who, f"risk_dollars must be > 0, got {cfg.get('risk_dollars')!r}")
        self.max_shares = _integer(who, cfg, "max_shares", 1)
        self.first_entry = _clock(who, cfg, "first_entry")
        self.last_entry = _clock(who, cfg, "last_entry")
        self.flat_time = _clock(who, cfg, "flat_time")
        if not SESSION_OPEN_MINUTE <= self.first_entry <= self.last_entry < self.flat_time:
            raise _fail(who, "times must satisfy 09:30 <= first_entry <= last_entry < flat_time")
        min_size_atr = cfg.get("min_size_atr", DEFAULTS["min_size_atr"])
        if min_size_atr is not None:
            min_size_atr = _number(who, cfg, "min_size_atr")
            if min_size_atr < 0:
                raise _fail(who, f"min_size_atr must be >= 0 or null, got {cfg.get('min_size_atr')!r}")
        try:
            self._fvg_config = FvgConfig(bar_minutes=self.zone_minutes, min_size_atr=min_size_atr)
        except ValueError as exc:
            raise _fail(who, str(exc)) from None

        super().__init__(algoConfigData)

        # Dashboard series (see `SERIES`); the frame is built from these lists on demand.
        self.FrontEndDataStruct = [name for name, _ in SERIES]
        self.FrontEndDataType = [kind for _, kind in SERIES]
        self.DataColumns = ["time", *self.FrontEndDataStruct]
        self._columns: dict[str, list] = {name: [] for name in self.DataColumns}
        self._frame: pd.DataFrame | None = None

        self._det = FvgDetector(self._fvg_config)
        self._atr_window = self._fvg_config.atr_bars
        self._atr_min = self._fvg_config.atr_min_bars
        self._ema_alpha = 2.0 / (self.trend_ema + 1)
        self._seen = 0  # bars of the price frame already processed
        self._day: Any = None
        self._ema: float | None = None
        self._ema_count = 0
        self._tr: deque[int] = deque(maxlen=self._atr_window)
        self._tr_sum = 0
        self._prev_htf_close: int | None = None
        self._reset_session_state()
        self.currentZone: Any = None
        self.skipped: list[tuple[datetime, str]] = []  # (bar time, why) of skipped setups

    # ----- the indicator frame the engine reads -----

    @property
    def AlgoData(self) -> pd.DataFrame | int:  # noqa: N802 (name fixed by the engine's contract)
        n = len(self._columns["time"]) if hasattr(self, "_columns") else 0
        if n == 0:
            return pd.DataFrame(columns=getattr(self, "DataColumns", ["time"]))
        if self._frame is None or len(self._frame) != n:
            self._frame = pd.DataFrame(self._columns)
        return self._frame

    @AlgoData.setter
    def AlgoData(self, value: Any) -> None:  # noqa: N802
        if isinstance(value, int):  # ParentAlgo.__init__ starts it at 0
            return
        raise AttributeError("FVGZone builds AlgoData from its own series; it cannot be assigned")

    def _reset_session_state(self) -> None:
        self._vwap_num = 0  # sum of volume * (high + low + close), in cents
        self._vwap_den = 0  # three times the sum of volume
        self._bucket: _Bucket | None = None
        self._traded: set[int] = set()
        self._dead: set[int] = set()  # zones a 1-minute bar traded through to the far edge

    # ----- the engine's entry point -----

    def update(self, StockData: pd.DataFrame) -> None:
        """Process the newest bar of `StockData` (and any earlier bar not yet seen).

        The frame is read, never modified. Bars before the last unseen one (the
        warm-up history on the first call) only feed the indicators: no order.
        """
        total = len(StockData)
        if total < self._seen:
            raise ValueError("FVGZone: the price frame is shorter than before")
        if total == self._seen:
            return
        if total - self._seen > 1:
            self._backfill(StockData, self._seen, total - 1)
        row = StockData.iloc[-1]
        self.curStockData = row
        when = row["date"].to_pydatetime()
        self._bar(when, float(row["open"]), float(row["high"]), float(row["low"]),
                  float(row["close"]), int(row["volume"]), int(row["time"]), live=True)
        self._seen = total

    def _backfill(self, frame: pd.DataFrame, start: int, stop: int) -> None:
        part = frame.iloc[start:stop]
        times = pd.DatetimeIndex(part["date"]).to_pydatetime()
        for when, o, h, lo, c, v, t in zip(
            times, part["open"].tolist(), part["high"].tolist(), part["low"].tolist(),
            part["close"].tolist(), part["volume"].tolist(), part["time"].tolist(), strict=True,
        ):
            self._bar(when, o, h, lo, c, int(v), int(t), live=False)
        self._seen = stop

    # ----- one bar -----

    def _bar(self, when: datetime, o: float, h: float, lo: float, c: float, volume: int,
             stamp: int, *, live: bool) -> None:
        minute_of_day = when.hour * 60 + when.minute
        offset = minute_of_day - SESSION_OPEN_MINUTE
        if offset < 0:  # before the regular session: not used for anything
            self._append(stamp, {})
            return
        o_t, h_t, l_t, c_t = (_cent_ticks(x) for x in (o, h, lo, c))
        day = when.date()
        exited = False
        if day != self._day:
            if live and self.inTrade:
                # A position left over from an earlier session (a half day, or bars that
                # stopped before flat_time): the algo cannot know the close in advance.
                # check_stoploss first: the broker may already have stopped it at this bar's
                # open, and a close on a position it no longer holds would be an ignored event.
                self.logger.info("***position left from an earlier session***")
                if self.trade.check_stoploss(self.curStockData) != 0:
                    self.close_trade()
                else:
                    self.inTrade = False
                exited = True
            self._reset_session_state()
            self._day = day

        # EMA of the 1-minute closes (all sessions in a row), seeded with the first close.
        self._ema = float(c_t) if self._ema is None else self._ema + self._ema_alpha * (c_t - self._ema)
        self._ema_count += 1
        # Session VWAP over typical prices, in exact integer arithmetic.
        self._vwap_num += volume * (h_t + l_t + c_t)
        self._vwap_den += 3 * volume

        self._add_to_bucket(when, offset, o_t, h_t, l_t, c_t)

        zones = []
        for z in self._det.live_zones():
            if z.known_at > when or z.formed_time.date() != day or z.id in self._dead:
                continue
            # A 1-minute bar that trades to or through the FAR edge ends the zone for the
            # algo at once (this bar included), whatever the zone-timeframe detector says
            # later: it learns of a fill only when the zone bar completes.
            if (l_t <= z.bottom_ticks) if z.direction is Direction.UP else (h_t >= z.top_ticks):
                self._dead.add(z.id)
                continue
            zones.append(z)
        values: dict[str, float] = {}

        if live:
            if self.inTrade and not exited:
                exited = self._manage_trade(minute_of_day)
            if not self.inTrade and not exited and self.first_entry <= minute_of_day <= self.last_entry:
                self._look_for_entry(when, zones, h_t, l_t, c_t)
            if self.inTrade and not isinstance(getattr(self.trade, "brokerRef", None), RefusedEntry):
                values["StopPrice"] = float(self.trade.stopPrice)
                values["tp"] = float(self.trade.tp)

        if self._vwap_den > 0:
            values["VWAP"] = self._vwap_num / self._vwap_den / _TICK
        if self._ema_count >= self.trend_ema and self._ema is not None:
            values["EMA"] = self._ema / _TICK
        above, below = self._nearest_zones(zones, c_t)
        if above is not None:
            values["ZoneAboveTop"], values["ZoneAboveBottom"] = above.top, above.bottom
        if below is not None:
            values["ZoneBelowTop"], values["ZoneBelowBottom"] = below.top, below.bottom
        self._append(stamp, values)

    def _append(self, stamp: int, values: dict[str, float]) -> None:
        self._columns["time"].append(stamp)
        for name, _ in SERIES:
            self._columns[name].append(values.get(name, _NAN))

    # ----- higher-timeframe bars and the detector -----

    def _add_to_bucket(self, when: datetime, offset: int, o: int, h: int, lo: int, c: int) -> None:
        m = self.zone_minutes
        index = offset // m
        bucket = self._bucket
        if bucket is not None and bucket.index != index:
            self._feed(bucket)  # a later bucket began: this one is final (a missing minute is allowed)
            bucket = self._bucket = None
        if bucket is None:
            label = when - timedelta(minutes=offset % m)
            bucket = self._bucket = _Bucket(index, label, o, h, lo, c)
        else:
            bucket.high = max(bucket.high, h)
            bucket.low = min(bucket.low, lo)
            bucket.close = c
            bucket.count += 1
        if bucket.count == m:  # every minute present: complete now, no need to wait
            self._feed(bucket)
            self._bucket = None

    def _feed(self, b: _Bucket) -> None:
        self._det.update(b.label, b.open / _TICK, b.high / _TICK, b.low / _TICK, b.close / _TICK)
        prev = self._prev_htf_close
        tr = b.high - b.low if prev is None else max(b.high - b.low, abs(b.high - prev), abs(b.low - prev))
        if len(self._tr) == self._atr_window:
            self._tr_sum -= self._tr[0]
        self._tr.append(tr)
        self._tr_sum += tr
        self._prev_htf_close = b.close

    # ----- trade management -----

    def _manage_trade(self, minute_of_day: int) -> bool:
        """Stop, target and the flat time for an open trade. True when the trade ended."""
        row = self.curStockData
        # check_stoploss first: it reports a position the broker no longer holds (stopped,
        # target filled, entry refused or cancelled) before any manual check can act on it.
        if self.trade.check_stoploss(row) == 0:
            self.inTrade = False
            return True
        if self.trade.check_tp(row) == 1:
            self.inTrade = False
            return True
        if minute_of_day >= self.flat_time:
            self.logger.info("***flat time close position***")
            self.close_trade()
            return True
        return False

    def _look_for_entry(self, when: datetime, zones: list, h_t: int, l_t: int, c_t: int) -> None:
        long_ok = self._trend_ok(c_t, long=True)
        short_ok = self._trend_ok(c_t, long=False)
        for z in reversed(zones):  # newest first
            if z.id in self._traded:
                continue
            if z.direction is Direction.UP:
                if not (long_ok and l_t <= z.top_ticks and c_t > z.top_ticks):
                    continue
                stop_t = z.bottom_ticks - self.stop_buffer_ticks
                risk_t = c_t - stop_t
            else:
                if not (short_ok and h_t >= z.bottom_ticks and c_t < z.bottom_ticks):
                    continue
                stop_t = z.top_ticks + self.stop_buffer_ticks
                risk_t = stop_t - c_t
            self._enter(when, z, z.direction is Direction.UP, c_t, stop_t, risk_t)
            return  # the newest qualifying zone wins, even if its setup is then skipped

    def _trend_ok(self, c_t: int, *, long: bool) -> bool:
        if not self.trend_filter:
            return True
        if self._vwap_den <= 0 or self._ema is None or self._ema_count < self.trend_ema:
            return False
        above_vwap = c_t * self._vwap_den > self._vwap_num
        below_vwap = c_t * self._vwap_den < self._vwap_num
        if long:
            return above_vwap and c_t > self._ema
        return below_vwap and c_t < self._ema

    def _enter(self, when: datetime, zone: Any, long: bool, c_t: int, stop_t: int, risk_t: int) -> None:
        if risk_t < self.min_risk_ticks:
            self.skipped.append((when, "risk below min_risk"))
            return
        n = len(self._tr)
        if n < self._atr_min or risk_t * n > self.max_risk_atr * self._tr_sum:
            self.skipped.append((when, "risk above max_risk_atr * ATR (or no ATR yet)"))
            return
        distance = int((Decimal(str(self.RRRatio)) * risk_t).to_integral_value(rounding=ROUND_HALF_UP))
        target_t = c_t + distance if long else c_t - distance
        shares = int((Decimal(str(self.risk_dollars)) * _TICK) // risk_t)
        shares = max(1, min(self.max_shares, shares))
        self.logger.debug("FVGZone entry %s zone %s: stop %s target %s shares %s",
                          "long" if long else "short", zone.id, stop_t, target_t, shares)
        self._traded.add(zone.id)
        self.currentZone = zone
        self.trade = self.open_trade(shares, 1 if long else 0)
        self.trade.set_stopLossType("Fixed")
        self.trade.set_stopPrice(stop_t / _TICK)
        self.trade.create_tp(target_t / _TICK, shares)

    # ----- series -----

    @staticmethod
    def _nearest_zones(zones: list, c_t: int) -> tuple[Any, Any]:
        """The usable live zone nearest above and nearest below the price (ties: newest)."""
        above = below = None
        best_above = best_below = None
        for z in reversed(zones):
            if z.bottom_ticks > c_t:
                dist = z.bottom_ticks - c_t
                if best_above is None or dist < best_above:
                    above, best_above = z, dist
            elif z.top_ticks < c_t:
                dist = c_t - z.top_ticks
                if best_below is None or dist < best_below:
                    below, best_below = z, dist
        return above, below

    def update_frontend(self) -> dict:
        """The newest indicator values, built from the last row (no frame is made)."""
        data = []
        for name, kind in SERIES:
            value = self._columns[name][-1]
            if not math.isnan(value):
                data.append({"name": name, "data": value, "type": kind})
        return {"idname": self.name, "time": int(self.curStockData["time"]), "data": data}
