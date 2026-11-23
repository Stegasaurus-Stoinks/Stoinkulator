"""Turn one IB historical bar into an engine `Bar`.

Time. Request history with `formatDate=2`: IB then sends the bar time as epoch
seconds in `HistoricalBar.date` (a string of digits). IB stamps a 1-minute bar
with the START of its minute. The engine bar gets `time` = that epoch and
`date` = the epoch converted to `zone` (default `DATA_TIME_ZONE`,
`America/New_York`, fixed in code) and made naive. The machine's time zone is
never consulted: the conversion uses `ZoneInfo` on a UTC instant only, never
`datetime.fromtimestamp` without a zone or `astimezone()` without an argument.

Session rule (`BadBar` otherwise). A bar is accepted only if its New York
wall-clock start time is within IB's extended session, 04:00 up to but not
including 20:00 (the check is in New York whatever `zone` is), and if its naive
time in the target `zone` is unambiguous, that is, not in an hour a clock change
repeats. The repeated autumn hour (01:00 to 02:00 New York on the change date)
maps two different epochs to one naive time; it is outside the session window,
so it is refused. Regular-hours fetching (`useRTH=1`) never produces such a
bar. For another `zone` the second check refuses the bars that land in that
zone's repeated hour.

Not epoch seconds, so refused (`BadBar`): the `formatDate=1` string
(`20240816  09:30:00`, with spaces and colons), a daily bar's `yyyymmdd`, any
non-digit text, an epoch outside 2000-01-01 .. 2100-01-01 (a milliseconds value
or a garbage number) and an epoch that is not a whole minute.

What IB can legitimately send, and what happens to it:

* Volume 0: accepted (a bar can exist without a reported trade volume). A
  volume above `MAX_VOLUME` (2**63 - 1, the int64 of the engine and the files)
  is refused.
* Volume as `Decimal` (ibapi 10.37): converted to `int`; a fraction (`Decimal`
  of 12.5) is refused, not rounded.
* Volume -1 and prices of -1 are IB's "no data" sentinels: refused. Prices must
  be finite, above 0 and at most `MAX_PRICE`. A caller that fetches a day treats a refused bar as a
  failed day, never as data.
* A bar whose high is below its open or close, or whose low is above them, is
  inconsistent and refused rather than passed on.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from MainStoinker.engine.feed import Bar

DATA_TIME_ZONE = "America/New_York"
"""Data times are naive wall-clock times of this zone (owner decision, issue 132)."""

MIN_EPOCH = 946_684_800  # 2000-01-01 00:00:00 UTC
MAX_EPOCH = 4_102_444_800  # 2100-01-01 00:00:00 UTC
MAX_VOLUME = 2**63 - 1
"""The largest volume an engine frame (int64) holds."""
MAX_PRICE = 10_000_000.0
"""A price above this is garbage, not data."""
SESSION_START_MINUTE = 4 * 60
SESSION_END_MINUTE = 20 * 60
"""IB's extended session in New York minutes of the day: [04:00, 20:00)."""

_EPOCH = re.compile(r"[0-9]{1,12}")
_DAILY = re.compile(r"[0-9]{8}")


class BadBar(ValueError):
    """An IB bar that cannot be turned into an engine bar (the message says why)."""


class IbBarLike(Protocol):
    """The fields of `events.HistoricalBar` (or ibapi's `BarData`) this module reads."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal | int


def _zone(zone: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown time zone {zone!r}") from exc


def epoch_from_date(text: str) -> int:
    """The epoch seconds in IB's `date` string; `BadBar` if it is not (see the module docstring)."""
    if not isinstance(text, str):
        raise BadBar(f"bar date must be a string of epoch seconds, got {text!r}")
    if _DAILY.fullmatch(text):
        raise BadBar(
            f"bar date {text!r} looks like a daily yyyymmdd date, not epoch seconds; "
            "request 1-minute bars with formatDate=2"
        )
    if not _EPOCH.fullmatch(text):
        raise BadBar(
            f"bar date {text!r} is not epoch seconds (a formatDate=1 string?); "
            "request bars with formatDate=2"
        )
    epoch = int(text)
    if not MIN_EPOCH <= epoch <= MAX_EPOCH:
        raise BadBar(f"bar epoch {epoch} is outside 2000-01-01 to 2100-01-01 (milliseconds?)")
    if epoch % 60:
        raise BadBar(f"bar epoch {epoch} is not a whole minute; IB stamps minute bars at their start")
    return epoch


def data_time(epoch: int, zone: str = DATA_TIME_ZONE) -> pd.Timestamp:
    """The naive wall-clock time of `zone` at the UTC instant `epoch` (seconds)."""
    instant = datetime.fromtimestamp(epoch, tz=UTC).astimezone(_zone(zone))
    return pd.Timestamp(instant.replace(tzinfo=None))


def _volume(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise BadBar(f"volume must be a Decimal or int, got {value!r}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise BadBar(f"volume {value!r} is not a finite number")
    if value != int(value):
        raise BadBar(f"volume {value!r} is not a whole number")
    if value < 0:
        raise BadBar(f"volume {value!r} is negative (-1 is IB's 'no data' marker)")
    if value > MAX_VOLUME:
        raise BadBar(f"volume {value!r} is above {MAX_VOLUME} (it would not fit an int64)")
    return int(value)


def _price(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BadBar(f"{name} must be a number, got {value!r}")
    price = float(value)
    if not math.isfinite(price):
        raise BadBar(f"{name} {value!r} is not finite")
    if price <= 0:
        raise BadBar(f"{name} {value!r} is not above 0 (-1 is IB's 'no data' marker)")
    if price > MAX_PRICE:
        raise BadBar(f"{name} {value!r} is above {MAX_PRICE:g}")
    return price


def _check_session(epoch: int, zone: str) -> None:
    """`BadBar` unless `epoch` is in the extended session and has an unambiguous naive time."""
    target = _zone(zone)
    instant = datetime.fromtimestamp(epoch, tz=UTC)
    new_york = instant.astimezone(_zone(DATA_TIME_ZONE))
    minute = new_york.hour * 60 + new_york.minute
    if not SESSION_START_MINUTE <= minute < SESSION_END_MINUTE:
        raise BadBar(
            f"bar at {epoch} is {new_york:%Y-%m-%d %H:%M} New York, outside IB's extended "
            "session 04:00 to 20:00 (the repeated autumn hour 01:00 to 02:00 is ambiguous)"
        )
    naive = instant.astimezone(target).replace(tzinfo=None)
    first = naive.replace(tzinfo=target, fold=0).utcoffset()
    if first != naive.replace(tzinfo=target, fold=1).utcoffset():
        raise BadBar(f"bar at {epoch}: the time {naive} is ambiguous in {zone} (repeated hour)")


def bar_from_ib(bar: IbBarLike, zone: str = DATA_TIME_ZONE) -> Bar:
    """The engine `Bar` for one IB historical bar requested with `formatDate=2`.

    Raises `BadBar` (a `ValueError`) for anything the module docstring lists, and
    `ValueError` for an unknown `zone`. A bar whose New York time is ambiguous or
    outside 04:00 to 20:00 is refused (see "Session rule" in the module docstring).
    """
    epoch = epoch_from_date(bar.date)
    _check_session(epoch, zone)
    open_, high, low, close = (
        _price(n, getattr(bar, n)) for n in ("open", "high", "low", "close")
    )
    volume = _volume(bar.volume)
    if high < max(open_, close):
        raise BadBar(f"bar at {epoch}: high {high} is below open {open_} or close {close}")
    if low > min(open_, close):
        raise BadBar(f"bar at {epoch}: low {low} is above open {open_} or close {close}")
    return Bar(
        date=data_time(epoch, zone), time=epoch,
        open=open_, high=high, low=low, close=close, volume=volume,
    )
