"""Time-window rules shared by every API route that takes `start` and `end`.

Pure helpers: no FastAPI, no I/O. A window is a pair of naive date-times
("data time", as stored; there is no timezone anywhere in the API). Routes call
`parse_window`, turn a `WindowError` into a 422 whose detail is its message, and
write market times into responses with `format_time`.
"""

from __future__ import annotations

import re
from datetime import datetime, time

MAX_WINDOW_DAYS = 7  # calendar days; seven calendar days hold at most five trading days

# Date, `T` or a space, hours and minutes, optional seconds, optional 1-6 digit fraction.
_NAIVE_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?")


class WindowError(ValueError):
    """A bad window. The message is one plain sentence that is safe to show a client.

    It names the offending parameter at most and never echoes the value given.
    """


def _bound(name: str, text: str) -> datetime:
    if _NAIVE_DATETIME.fullmatch(text) is not None:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    raise WindowError(f"{name} must be a date and time (YYYY-MM-DDTHH:MM:SS) with no timezone.")


def parse_window(start: str | None, end: str | None, *,
                 default_day: datetime | None) -> tuple[datetime, datetime]:
    """The window `[start, end]` as naive datetimes, both ends inclusive.

    With neither bound the window is the calendar day of `default_day`
    (00:00:00 to 23:59:59.999999); `WindowError` if that is None too. Giving
    only one bound is an error. Each bound must be a naive date-time with a time
    part, `YYYY-MM-DDTHH:MM[:SS[.ffffff]]` or the same with a space for the `T`:
    an offset, a trailing `Z`, a date alone, an empty string or anything else is
    an error. `end` may not precede `start`, and `end.date() - start.date()` may
    be at most `MAX_WINDOW_DAYS - 1` days (independent of data and weekdays).
    """
    if start is None and end is None:
        if default_day is None:
            raise WindowError("start and end are required.")
        day = default_day.date()
        return datetime.combine(day, time.min), datetime.combine(day, time.max)
    if start is None or end is None:
        raise WindowError("start and end must be given together.")
    lo, hi = _bound("start", start), _bound("end", end)
    if hi < lo:
        raise WindowError("end must not be before start.")
    if (hi.date() - lo.date()).days > MAX_WINDOW_DAYS - 1:
        raise WindowError(f"The window must span at most {MAX_WINDOW_DAYS} calendar days.")
    return lo, hi


def format_time(value: datetime) -> str:
    """A market time as `YYYY-MM-DD HH:MM:SS` (microseconds only when present)."""
    return str(value)
