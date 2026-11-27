"""Today's regular session for the connecting code: the market calendar, and IB's own hours.

Pure and offline (no `ibapi`, no connection, no clock). Two jobs:

* `closed_reason(day)`: why a day has no regular session (a weekend, a named holiday, a one-off
  closure), for the refusal text of `run`, the watcher and `fetch-bars`;
* `ib_hours_for(liquid_hours, time_zone, day)`: what the contract details' `liquidHours` field says
  about one day, parsed defensively. The field is already part of every `reqContractDetails` answer
  the session asks for (`StockInfo.liquid_hours`); nothing new is requested.

IB's format (documented by IB, seen in `tests/recordings`): days separated by `;`, several
sessions of one day by `,`; a session is `YYYYMMDD:HHMM-YYYYMMDD:HHMM`, a closed day is
`YYYYMMDD:CLOSED`. The times are in the contract's time zone. Anything that does not fit is
"unknown" (the calendar is followed and one line says that the hours could not be compared), never a
guess: a regular session that does not start at 09:30, a zone other than New York's, a day the
text does not mention, a session that crosses midnight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from MainStoinker.engine import market_calendar as calendar

# IB's names for the New York time zone as the contract details report it
NEW_YORK_ZONES = frozenset({"US/EASTERN", "AMERICA/NEW_YORK", "EST5EDT", "EST", "EDT"})
_SESSION = re.compile(r"^(\d{8}):(\d{4})-(\d{8}):(\d{4})$")

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class IbHours:
    """What IB says about one day: `kind` is `open` (with `close_minute`, minutes since midnight),
    `closed`, or `unknown` (`why` says what could not be used)."""

    kind: str
    close_minute: int | None = None
    why: str = ""


def closed_reason(day: date, extra: calendar.Overrides | None = None) -> str:
    """Why `day` has no regular session ("a weekend", "a market holiday (Thanksgiving)", ...).

    Only meaningful for a day that is not a trading day by the calendar."""
    if day.weekday() >= 5:
        return f"a weekend ({day:%A})"
    if extra is not None and day in extra:
        return "a day the caller's calendar table lists as closed"
    name = calendar.holidays(day.year).get(day)
    if name is not None and day not in calendar.OVERRIDES:
        return f"a market holiday ({name})"
    return "a one-off closure listed in the market calendar"


def not_a_trading_day_text(day: date, extra: calendar.Overrides | None = None) -> str:
    return f"{day.isoformat()} is not a trading day: the market is closed on {closed_reason(day, extra)}"


def _minutes(hhmm: str) -> int | None:
    hours, minutes = int(hhmm[:2]), int(hhmm[2:])
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def ib_hours_for(liquid_hours: str, time_zone: str, day: date) -> IbHours:
    """The regular hours of `day` as `liquid_hours` (the contract details field) states them."""
    if not isinstance(liquid_hours, str) or not liquid_hours.strip():
        return IbHours(UNKNOWN, why="the contract details carry no liquid hours")
    if str(time_zone).strip().upper() not in NEW_YORK_ZONES:
        return IbHours(UNKNOWN, why=f"the hours are in time zone {time_zone!r}, not New York's")
    key = day.strftime("%Y%m%d")
    closed = False
    sessions: list[tuple[int, int]] = []
    for item in liquid_hours.split(";"):
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            if part == f"{key}:CLOSED":
                closed = True
                continue
            found = _SESSION.match(part)
            if found is None:
                continue  # another day's CLOSED, or text this parser does not know
            first_day, first, last_day, last = found.groups()
            if first_day != key and last_day != key:
                continue
            if first_day != key or last_day != key:
                return IbHours(UNKNOWN, why="a session that crosses midnight")
            start, end = _minutes(first), _minutes(last)
            if start is None or end is None or end <= start:
                return IbHours(UNKNOWN, why=f"unreadable session {part!r}")
            sessions.append((start, end))
    if closed and sessions:
        return IbHours(UNKNOWN, why="the text says both closed and open for the day")
    if closed:
        return IbHours(CLOSED)
    if not sessions:
        return IbHours(UNKNOWN, why="the text does not mention the day")
    start = min(s for s, _ in sessions)
    end = max(e for _, e in sessions)
    if start != calendar.OPEN_MINUTE:
        return IbHours(UNKNOWN, why=f"the session starts at {start // 60:02d}:{start % 60:02d}, "
                                    "not at 09:30")
    if not calendar.OPEN_MINUTE < end <= calendar.FULL_CLOSE_MINUTE:
        return IbHours(UNKNOWN, why=f"the session ends at {end // 60:02d}:{end % 60:02d}, outside "
                                    "09:31 to 16:00")
    return IbHours(OPEN, close_minute=end)
