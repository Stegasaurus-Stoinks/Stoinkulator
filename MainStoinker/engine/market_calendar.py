"""The US stock market calendar: holidays and early closes, for any year.

Pure and offline: standard library only, no network, no third-party calendar.
All times are naive New York wall-clock minutes since midnight, like the bar
data (`OPEN_MINUTE` 09:30 = 570, `FULL_CLOSE_MINUTE` 16:00 = 960, `HALF_CLOSE_MINUTE`
13:00 = 780).

Rules (the NYSE's published holiday pattern, nyse.com "Holidays & Trading Hours";
the rules are written down here from that pattern, not copied from a library):

Full closures
- New Year's Day, 1 January. On a Sunday it is observed on Monday 2 January. On a
  Saturday it is NOT observed on the Friday before (the exchange's rule, e.g. 2022-01-01
  left Friday 2021-12-31 open).
- Martin Luther King Jr. Day: third Monday of January.
- Washington's Birthday: third Monday of February.
- Good Friday: two days before Easter Sunday (Gregorian Easter, anonymous algorithm).
- Memorial Day: last Monday of May.
- Juneteenth, 19 June, from 2022 on.
- Independence Day, 4 July.
- Labor Day: first Monday of September.
- Thanksgiving: fourth Thursday of November.
- Christmas Day, 25 December.
Juneteenth, Independence Day and Christmas Day: a Saturday holiday is observed on
the Friday before, a Sunday holiday on the Monday after.

Early closes at 13:00
- The day after Thanksgiving.
- Christmas Eve, 24 December, when it is a trading day (a weekday that is not the
  observed Christmas Day).
- 3 July, when 3 July and 4 July are both weekdays (so 3 July is a Monday to Thursday;
  on a Friday 3 July it is the observed Independence Day, a full closure).

Overrides: one-off closures and exceptions are in `OVERRIDES` (date -> close minute, or
`None` for a full closure). Add a line there, in a reviewed change, for a day such as a
national day of mourning. A caller may also pass its own table, `extra`, to any function
(`load_overrides(path)` reads a JSON file `{"2025-01-09": null, "2026-12-24": "13:00"}`:
`null` closes the day, a "HH:MM" string sets the close). `extra` wins over `OVERRIDES`.

Not covered: sessions shortened for other reasons than the pattern above (the
exchange may announce one; add it as an override), and years before about 2005: the
rules above are today's, applied to every year (Juneteenth only from 2022), and
one-off closures are listed only from 2004 (the closure of 2001-09-11 to 14, for
example, is not).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

OPEN_MINUTE = 9 * 60 + 30
FULL_CLOSE_MINUTE = 16 * 60
HALF_CLOSE_MINUTE = 13 * 60

# One-off changes to the rules above. None = closed all day; an int = close minute.
# 2025-01-09: national day of mourning (President Carter). 2018-12-05: national day of
# mourning (President G. H. W. Bush). 2012-10-29 and 2012-10-30: Hurricane Sandy.
# 2007-01-02: national day of mourning (President Ford). 2004-06-11: national day of
# mourning (President Reagan).
OVERRIDES: dict[date, int | None] = {
    date(2004, 6, 11): None,
    date(2007, 1, 2): None,
    date(2012, 10, 29): None,
    date(2012, 10, 30): None,
    date(2018, 12, 5): None,
    date(2025, 1, 9): None,
}

Overrides = Mapping[date, int | None]


@dataclass(frozen=True)
class Session:
    """A trading day's regular session (naive New York minutes since midnight)."""

    day: date
    open_minute: int
    close_minute: int

    @property
    def is_half_day(self) -> bool:
        return self.close_minute < FULL_CLOSE_MINUTE


def easter(year: int) -> date:
    """Gregorian Easter Sunday (the anonymous Gregorian algorithm, Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day_minus = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day_minus + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1-based) `weekday` (Monday 0) of the month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Saturday holiday on the Friday before, Sunday holiday on the Monday after."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> dict[date, str]:
    """The full closures of `year` by the rules (no overrides): date -> name."""
    found: dict[date, str] = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year's Day is not observed on the Friday
        found[_observed(new_year)] = "New Year's Day"
    found[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    found[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    found[easter(year) - timedelta(days=2)] = "Good Friday"
    found[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        found[_observed(date(year, 6, 19))] = "Juneteenth"
    found[_observed(date(year, 7, 4))] = "Independence Day"
    found[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    found[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    found[_observed(date(year, 12, 25))] = "Christmas Day"
    return found


def _rule_close(day: date) -> int | None:
    """Close minute by the rules, or None on a weekend or a holiday."""
    if day.weekday() >= 5 or day in holidays(day.year):
        return None
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return HALF_CLOSE_MINUTE
    if day.month == 12 and day.day == 24:
        return HALF_CLOSE_MINUTE
    if day.month == 7 and day.day == 3 and date(day.year, 7, 4).weekday() < 5:
        return HALF_CLOSE_MINUTE
    return FULL_CLOSE_MINUTE


def _as_date(day: date) -> date:
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError(f"day must be a datetime.date, not {type(day).__name__}")
    return day


def session(day: date, extra: Overrides | None = None) -> Session | None:
    """The regular session of `day`, or None on a weekend or a full holiday."""
    day = _as_date(day)
    if extra is not None and day in extra:
        close = extra[day]
    elif day in OVERRIDES:
        close = OVERRIDES[day]
    else:
        close = _rule_close(day)
    return None if close is None else Session(day, OPEN_MINUTE, close)


def is_trading_day(day: date, extra: Overrides | None = None) -> bool:
    return session(day, extra) is not None


def is_half_day(day: date, extra: Overrides | None = None) -> bool:
    """True on a trading day that closes before 16:00 (False on a closed day)."""
    found = session(day, extra)
    return found is not None and found.is_half_day


def close_minute(day: date, extra: Overrides | None = None) -> int:
    """Close of `day` in minutes since midnight (960 or 780); ValueError if it is not a trading day."""
    found = session(day, extra)
    if found is None:
        raise ValueError(f"{day.isoformat()} is not a trading day")
    return found.close_minute


def previous_trading_day(day: date, extra: Overrides | None = None) -> date:
    """The trading day before `day` (`day` itself need not be one)."""
    current = _as_date(day) - timedelta(days=1)
    while not is_trading_day(current, extra):
        current -= timedelta(days=1)
    return current


def next_trading_day(day: date, extra: Overrides | None = None) -> date:
    """The trading day after `day` (`day` itself need not be one)."""
    current = _as_date(day) + timedelta(days=1)
    while not is_trading_day(current, extra):
        current += timedelta(days=1)
    return current


def trading_days(start: date, end: date, extra: Overrides | None = None) -> Iterator[date]:
    """Every trading day from `start` to `end`, both included, in order."""
    current = _as_date(start)
    last = _as_date(end)
    while current <= last:
        if is_trading_day(current, extra):
            yield current
        current += timedelta(days=1)


def load_overrides(path: Path) -> dict[date, int | None]:
    """Read a JSON file of overrides: `{"YYYY-MM-DD": null | "HH:MM"}`.

    Raises ValueError for anything else (a bad date, a close outside 09:31 to 16:00).
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object of date -> null or 'HH:MM'")
    table: dict[date, int | None] = {}
    for key, value in raw.items():
        try:
            day = date.fromisoformat(key)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: {key!r} is not a date YYYY-MM-DD") from None
        if value is None:
            table[day] = None
            continue
        try:
            hours, minutes = str(value).split(":")
            minute = int(hours) * 60 + int(minutes)
            if len(hours) != 2 or len(minutes) != 2:
                raise ValueError
        except ValueError:
            raise ValueError(f"{path}: {key}: close must be null or 'HH:MM', got {value!r}") from None
        if not OPEN_MINUTE < minute <= FULL_CLOSE_MINUTE:
            raise ValueError(f"{path}: {key}: close {value!r} is outside 09:31 to 16:00")
        table[day] = minute
    return table
