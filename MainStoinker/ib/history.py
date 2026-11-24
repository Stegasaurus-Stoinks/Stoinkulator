"""Fetch planning for historical bars: day requests, pacing, resume manifest, gap report.

Pure: no `ibapi`, no connection, no real clock and no sleeping (the clock is
injected, waits are returned as numbers). Talking to IB is a later task.

Data times are naive wall-clock times of `DATA_ZONE` (`America/New_York`), fixed
in code and never the machine's zone. The regular session is 09:30 to 16:00 in
that zone, so a full day has `SESSION_MINUTES` (390) one-minute bars, stamped
09:30 to 15:59 (a bar is stamped with the start of its minute).

There is deliberately no holiday calendar. A holiday is planned like any
weekday; IB answers it with the previous session (or nothing), `filter_day`
keeps only bars of the requested date, and an empty day is recorded as
`no_data`. Half days come out of the gap report as `short` (complete from 09:30
to 12:59 and nothing after); any other missing tail is `partial`.

Contract for the driver (the later task that talks to IB)
---------------------------------------------------------
1. One reservation at a time: `Pacer.reserve(con_id, pacing_request_key(con_id,
   request))` -> wait `reservation.wait` seconds (the driver's own sleep) ->
   send -> `Pacer.sent(reservation)`; call `Pacer.cancel(reservation)` in a
   `finally` if the send did not happen. A second `reserve` before `sent` or
   `cancel` raises `ReservationOutstanding`. `sent` before the wait has elapsed
   records the send and raises `SentTooEarly`; the driver must stop on it.
2. The clock is monotonic (`time.monotonic` in the driver, never wall time).
3. One thread: the pacer is not thread-safe.
4. The contract key is the `int` conId, never an `ibapi` `Contract` (those
   compare by identity). The request key is `pacing_request_key`.
5. One pacer per TWS session, shared by every history request (retries too).
6. Write the month file first, then `Manifest.record(day, "ok", ...)` and
   `save_manifest`. After a crash the day is simply fetched again.
7. Convert bar times to naive `America/New_York` from epoch seconds
   (`formatDate=2`) before `filter_day`. Bars are stamped at the START of the
   minute. `filter_day` raises on bars of the requested day outside 09:30 to
   15:59: a time-zone mistake must not look like a short day.
8. Record every outcome in the `Manifest` and `save_manifest` it. IB error 162
   means BOTH "pacing violation" and "HMDS query returned no data", told apart
   only by the text. A pacing violation is `failed` (and the run should back
   off). `no_data` is only for a complete answer with zero bars on the day and
   needs `complete_answer=True`; it is never asked for again. Timeouts,
   disconnects and anything else that may succeed later are `failed`.
9. To force a refetch of recorded days use `Manifest.mark_pending` /
   `Manifest.mark_gaps_pending`.
10. `plan_range` lists the days to fetch and the days skipped because their
    session has not closed yet; report both.
11. Gap files are written per symbol folder: `write_gap_files(symbol_folder(...),
    rows, symbol)`.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from MainStoinker.engine.feed import (
    REPLACE_RETRY_PAUSE,
    REPLACE_RETRY_SECONDS,
    month_file_path,
)

DATA_ZONE = "America/New_York"
SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)
SESSION_MINUTES = 390
EARLY_CLOSE_MINUTES = 210  # an early close at 13:00 leaves bars 09:30 to 12:59
DEFAULT_BAR_SIZE = "1 min"
DEFAULT_WHAT_TO_SHOW = "TRADES"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_FAILED = "failed"
DAY_STATUSES = (STATUS_OK, STATUS_NO_DATA, STATUS_FAILED)

_UTC = timezone.utc
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _aware(moment: datetime, what: str) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError(f"{what} must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")
    return moment


def check_symbol(symbol: str) -> str:
    """`symbol` if it is safe as a folder name on every platform, else `ValueError`.

    On top of `engine.feed`'s plain-ticker rule this refuses Windows reserved
    device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`..`COM9`, `LPT1`..`LPT9`, any
    case, also with an extension such as `NUL.A`) and a trailing dot or space.
    """
    month_file_path(Path("."), symbol, 2000, 1)  # the shared plain-ticker rule
    if symbol != symbol.rstrip(". "):
        raise ValueError(f"invalid symbol {ascii(symbol)}: trailing dot or space")
    if symbol.split(".")[0].rstrip().upper() in _WINDOWS_RESERVED:
        raise ValueError(f"invalid symbol {ascii(symbol)}: reserved Windows device name")
    return symbol


def session_close_utc(day: date) -> datetime:
    """16:00 New York of `day` as an aware UTC datetime (DST handled by the zone database)."""
    return datetime.combine(day, SESSION_CLOSE, tzinfo=_zone(DATA_ZONE)).astimezone(_UTC)


@dataclass(frozen=True, slots=True)
class DayPlan:
    """Result of `plan_range`: the days to fetch and the trading days skipped as not yet closed."""

    days: list[date]
    not_closed: list[date]


def plan_range(start: date, end: date, server_now: datetime) -> DayPlan:
    """Every closed trading day (weekday) from `start` to `end`, plus the ones not closed yet.

    `server_now` is an aware datetime (the injected clock). A weekday is closed
    once its regular session (16:00 New York) is over at `server_now`. Weekdays
    whose session has not closed go into `not_closed` (in order) so the driver
    can report them; they are never planned. The plan is refused with
    `ValueError` only when the range holds trading days but none of them has
    closed. A range of only weekend days plans nothing. `start` after `end` is a
    `ValueError`. Holidays are not removed (see the module docstring).
    """
    _aware(server_now, "server_now")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    days: list[date] = []
    not_closed: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            (days if session_close_utc(current) <= server_now else not_closed).append(current)
        current += timedelta(days=1)
    if not_closed and not days:
        raise ValueError(
            f"no trading day from {start} to {end} has closed yet at {server_now.isoformat()}: "
            f"session of {not_closed[0]} not closed")
    return DayPlan(days, not_closed)


def plan_days(start: date, end: date, server_now: datetime) -> list[date]:
    """The closed trading days of `plan_range` (the skipped, not closed ones are dropped)."""
    return plan_range(start, end, server_now).days


def format_end_date_time(moment: datetime) -> str:
    """The `endDateTime` string for `reqHistoricalData`, from an aware datetime.

    IB documents two forms: `yyyymmdd-HH:mm:ss` in UTC and `yyyymmdd HH:mm:ss
    <zone>` (a time zone name after a space). This uses the first, in UTC. The
    older `ibapi` docstring only lists the second form, so the first owner
    session of the driver task must confirm that TWS accepts this one; if not,
    change it here, the only place that builds the string.
    """
    return _aware(moment, "moment").astimezone(_UTC).strftime("%Y%m%d-%H:%M:%S")


@dataclass(frozen=True, slots=True)
class DayRequest:
    """Arguments of one `reqHistoricalData` call for one trading day."""

    day: date
    end_date_time: str  # see `format_end_date_time`
    duration: str
    bar_size: str
    what_to_show: str
    use_rth: int

    def key(self) -> str:
        """Identity for the identical-request rule."""
        return "|".join((self.end_date_time, self.duration, self.bar_size,
                         self.what_to_show, str(self.use_rth)))


def pacing_request_key(con_id: int, request: DayRequest) -> str:
    """The pacer's request key for `request` on contract `con_id` (the conId is part of it)."""
    if isinstance(con_id, bool) or not isinstance(con_id, int):
        raise TypeError(f"con_id must be an int, got {con_id!r}")
    return f"{con_id}|{request.key()}"


def day_request(
    day: date, *, bar_size: str = DEFAULT_BAR_SIZE, what_to_show: str = DEFAULT_WHAT_TO_SHOW,
) -> DayRequest:
    """The request for the regular session of `day`: ends 16:00 New York, "1 D", RTH only."""
    return DayRequest(day, format_end_date_time(session_close_utc(day)), "1 D", bar_size,
                      what_to_show, 1)


def _bar_times(bars: pd.DataFrame, what: str) -> pd.Series:
    """The `date` column as naive whole-minute datetimes; `ValueError` for anything else."""
    if "date" not in bars.columns:
        raise ValueError(f"{what}: bars have no date column")
    dates = pd.to_datetime(bars["date"])
    if getattr(dates.dt, "tz", None) is not None:
        raise ValueError(f"{what}: the date column is timezone-aware, expected naive data-zone times")
    if dates.isna().any():
        raise ValueError(f"{what}: the date column holds missing timestamps")
    if len(dates) and (dates != dates.dt.floor("min")).any():
        raise ValueError(f"{what}: timestamps are not on whole minutes")
    return dates


def filter_day(
    bars: pd.DataFrame, day: date, server_now: datetime, *,
    data_zone: str = DATA_ZONE, bar_minutes: int = 1,
) -> pd.DataFrame:
    """Rows of `bars` (a `date` column of naive data-zone times, the bar start) for `day` only.

    On a holiday IB answers "1 D" with the previous session; bars of other
    dates are dropped quietly. A bar whose minute had not ended at `server_now`
    is dropped as well (it would still change). A bar of `day` itself outside
    09:30 to 15:59 New York raises `ValueError`: a `useRTH=1` answer never has
    one, so such times mean the stamps are in the wrong zone. Returns a copy
    with a fresh RangeIndex. Nothing else is repaired silently: a tz-aware
    `date` column, timestamps not on whole minutes, duplicate timestamps or
    unsorted rows raise `ValueError`.
    """
    _aware(server_now, "server_now")
    dates = _bar_times(bars, "filter_day")
    if dates.duplicated().any():
        raise ValueError("filter_day: duplicate timestamps in bars")
    if not dates.is_monotonic_increasing:
        raise ValueError("filter_day: bars are not sorted by time")
    now_local = server_now.astimezone(_zone(data_zone)).replace(tzinfo=None)
    minutes = expected_minutes(day, data_zone)
    in_session = (dates >= pd.Timestamp(minutes[0])) & (dates <= pd.Timestamp(minutes[-1]))
    new_york = (dates.dt.tz_localize(data_zone, ambiguous="NaT", nonexistent="NaT")
                .dt.tz_convert(DATA_ZONE))
    on_day = new_york.dt.date == day
    stray = on_day & ~in_session
    if stray.any():
        first = dates[stray].iloc[0]
        raise ValueError(
            f"filter_day: {int(stray.sum())} bar(s) of {day} outside 09:30 to 15:59 New York "
            f"(first at {first}); the times do not look like regular-hours New York times "
            f"(a useRTH=1 answer never contains such bars), check the time zone conversion")
    keep = in_session & (dates + pd.Timedelta(minutes=bar_minutes) <= pd.Timestamp(now_local))
    return bars.loc[keep].reset_index(drop=True)


# --- pacing -----------------------------------------------------------------


def _number(value: float, name: str, *, minimum: float, inclusive: bool) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < minimum or (value == minimum and not inclusive)):
        sign = ">=" if inclusive else ">"
        raise ValueError(f"{name} must be a finite number {sign} {minimum}, got {value!r}")


def _limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer of at least 1, got {value!r}")


class ReservationOutstanding(RuntimeError):
    """`Pacer.reserve` was called while an earlier reservation is neither sent nor cancelled."""


class SentTooEarly(RuntimeError):
    """`Pacer.sent` was called before the planned time; the early send HAS been recorded."""

    def __init__(self, early_by: float) -> None:
        super().__init__(f"request sent {early_by:.3f} s before the end of its wait")
        self.early_by = early_by


@dataclass(frozen=True, slots=True, eq=False)
class Reservation:
    """Token returned by `Pacer.reserve`; hand it back to `Pacer.sent` or `Pacer.cancel`."""

    contract: int
    request: str
    planned: float  # clock time at which the send is allowed
    wait: float  # seconds from the reserve call until then


def _check_keys(contract: object, request: object) -> None:
    if isinstance(contract, bool) or not isinstance(contract, int):
        raise TypeError(f"the contract key must be an int conId, got {contract!r}")
    if not isinstance(request, str):
        raise TypeError(f"the request key must be a str, got {request!r}")


class Pacer:
    """Decides how long to wait before the next history request. Never sleeps.

    Defaults are IB's documented limits: at least 10 s between history
    requests, at most 60 per 10 minutes, fewer than 6 per contract per 2 s and
    no identical request within 15 s. Window limits keep `margin` seconds extra
    so that a window never holds more than its limit even when closed at both
    ends (`margin=0` drops that guarantee).

    Use: at most ONE reservation at a time. `reserve(con_id, request_key)`
    returns a `Reservation` whose `wait` is the seconds to wait
    (`ReservationOutstanding` if another is still open); the caller waits (its
    own sleep), sends, and calls `sent(reservation)`, which records the ACTUAL
    send time from the clock, or `cancel(reservation)` if nothing was sent (put
    it in a `finally`). Every plan is computed from actual send times only, so a
    late send pushes the following requests back. A send before the wait
    elapsed is recorded and then raises `SentTooEarly`; the next `reserve`
    already accounts for it. The contract key must be the `int` conId and the
    request key a `str` (`TypeError` otherwise): `ibapi` `Contract` objects
    compare by identity and would defeat the per-contract and identical rules.
    The clock must be monotonic (`time.monotonic`). The class is not
    thread-safe: use one thread. History older than the longest window is
    dropped, so memory stays bounded.
    """

    EARLY_TOLERANCE = 1e-6  # seconds of float noise allowed before a send counts as early

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        min_gap: float = 10.0,
        window: float = 600.0,
        max_in_window: int = 60,
        contract_window: float = 2.0,
        max_per_contract: int = 5,
        identical_gap: float = 15.0,
        margin: float = 1.0,
    ) -> None:
        _number(min_gap, "min_gap", minimum=0.0, inclusive=True)
        _number(window, "window", minimum=0.0, inclusive=False)
        _number(contract_window, "contract_window", minimum=0.0, inclusive=False)
        _number(identical_gap, "identical_gap", minimum=0.0, inclusive=True)
        _number(margin, "margin", minimum=0.0, inclusive=True)
        _limit(max_in_window, "max_in_window")
        _limit(max_per_contract, "max_per_contract")
        self._clock = clock
        self.min_gap = min_gap
        self.window = window
        self.max_in_window = max_in_window
        self.contract_window = contract_window
        self.max_per_contract = max_per_contract
        self.identical_gap = identical_gap
        self.margin = margin
        self._all: list[float] = []  # actual send times, sorted
        self._per_contract: dict[int, list[float]] = {}
        self._identical: dict[tuple[int, str], float] = {}
        self._outstanding: Reservation | None = None

    def earliest(self, contract: int, request: str) -> float:
        """Earliest time (on the injected clock) the request may be sent; books nothing."""
        _check_keys(contract, request)
        t = self._clock()
        if self._all:
            t = max(t, self._all[-1] + self.min_gap)
        if len(self._all) >= self.max_in_window:
            t = max(t, self._all[-self.max_in_window] + self.window + self.margin)
        mine = self._per_contract.get(contract, [])
        if len(mine) >= self.max_per_contract:
            t = max(t, mine[-self.max_per_contract] + self.contract_window + self.margin)
        last = self._identical.get((contract, request))
        if last is not None:
            t = max(t, last + self.identical_gap + self.margin)
        return t

    def reserve(self, contract: int, request: str) -> Reservation:
        """Plan one request: `.wait` seconds from now; nothing is booked until `sent`."""
        _check_keys(contract, request)
        if self._outstanding is not None:
            raise ReservationOutstanding(
                "a reservation is still outstanding: call sent() or cancel() first")
        when = self.earliest(contract, request)
        self._outstanding = Reservation(contract, request, when, when - self._clock())
        return self._outstanding

    def _settle(self, reservation: Reservation) -> Reservation:
        if reservation is not self._outstanding:
            raise ValueError("unknown reservation (already sent or cancelled?)")
        self._outstanding = None
        return reservation

    def _prune(self, now: float) -> None:
        """Forget sends older than the window that could still matter (the clock is monotonic)."""
        horizon = now - (max(self.min_gap, self.window) + self.margin)
        del self._all[:bisect.bisect_left(self._all, horizon)]
        horizon = now - (self.contract_window + self.margin)
        for key in [k for k, v in self._per_contract.items() if v[-1] < horizon]:
            del self._per_contract[key]
        horizon = now - (self.identical_gap + self.margin)
        for key in [k for k, v in self._identical.items() if v < horizon]:
            del self._identical[key]

    def sent(self, reservation: Reservation) -> float:
        """Record that the request was just sent: the send time is the clock now. Returns it.

        An early send (before `reservation.planned`) is recorded too, then
        `SentTooEarly` is raised.
        """
        known = self._settle(reservation)
        when = self._clock()
        bisect.insort(self._all, when)
        del self._all[:-self.max_in_window]
        mine = self._per_contract.setdefault(known.contract, [])
        bisect.insort(mine, when)
        del mine[:-self.max_per_contract]
        key = (known.contract, known.request)
        self._identical[key] = max(self._identical.get(key, when), when)
        self._prune(when)
        early = known.planned - when
        if early > self.EARLY_TOLERANCE:
            raise SentTooEarly(early)
        return when

    def cancel(self, reservation: Reservation) -> None:
        """Release a reservation that was not sent; it leaves no trace."""
        self._settle(reservation)


# --- manifest ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayRecord:
    status: str
    bars: int = 0
    error_code: int | None = None
    error_text: str = ""
    fetched_utc: str = ""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_day_fields(
    status: object, bars: object, error_code: object, error_text: object, fetched_utc: object,
) -> None:
    """The one rule set for a day record, used by `Manifest.record` and by the loader.

    `TypeError` for a wrong type, `ValueError` for a wrong value: status one of
    `DAY_STATUSES`; bars an int of at least 0 (at least 1 for `ok`, exactly 0
    for `no_data`); error_code `None` or an int; error_text and fetched_utc str.
    """
    if not isinstance(status, str):
        raise TypeError(f"status must be a str, got {status!r}")
    if status not in DAY_STATUSES:
        raise ValueError(f"unknown day status {status!r}")
    if not _is_int(bars):
        raise TypeError(f"bars must be an int, got {bars!r}")
    assert isinstance(bars, int)
    if bars < 0:
        raise ValueError("bars must not be negative")
    if status == STATUS_OK and bars < 1:
        raise ValueError("an ok day needs at least one bar")
    if status == STATUS_NO_DATA and bars != 0:
        raise ValueError("a no_data day has no bars")
    if error_code is not None and not _is_int(error_code):
        raise TypeError(f"error_code must be an int or None, got {error_code!r}")
    if not isinstance(error_text, str):
        raise TypeError(f"error_text must be a str, got {error_text!r}")
    if not isinstance(fetched_utc, str):
        raise TypeError(f"fetched_utc must be a str, got {fetched_utc!r}")


@dataclass(slots=True)
class Manifest:
    """What was fetched for one symbol: identity of the fetch plus one record per day."""

    symbol: str
    con_id: int
    use_rth: int = 1
    what_to_show: str = DEFAULT_WHAT_TO_SHOW
    bar_size: str = DEFAULT_BAR_SIZE
    data_zone: str = DATA_ZONE
    days: dict[str, DayRecord] = field(default_factory=dict)

    def record(
        self, day: date, status: str, *, bars: int = 0, error_code: int | None = None,
        error_text: str = "", fetched_utc: datetime, complete_answer: bool = False,
    ) -> None:
        """Set the record of `day`; whatever is accepted here also loads back unchanged.

        Rules are `validate_day_fields` (`TypeError` / `ValueError`); `fetched_utc`
        must be aware. `no_data` is evidence-based: it needs `complete_answer=True`
        (IB gave a complete answer with no bars for the day), else `ValueError`.
        Record transient failures, and a pacing violation (error 162 with that
        text), as `failed`, never `no_data`.
        """
        if status == STATUS_NO_DATA and complete_answer is not True:
            raise ValueError("no_data needs complete_answer=True: only a complete answer "
                             "with no bars is no_data; anything else is failed")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError(f"day must be a date, got {day!r}")
        stamp = _aware(fetched_utc, "fetched_utc").astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        validate_day_fields(status, bars, error_code, error_text, stamp)
        self.days[day.isoformat()] = DayRecord(status, bars, error_code, error_text, stamp)

    def pending(self, days: Iterable[date]) -> list[date]:
        """The days still to request, in the given order: unknown and failed ones."""
        done = (STATUS_OK, STATUS_NO_DATA)
        return [d for d in days
                if (rec := self.days.get(d.isoformat())) is None or rec.status not in done]

    def mark_pending(self, days: Iterable[date]) -> list[date]:
        """Forget the records of `days` so `pending` asks for them again (a forced refetch).

        Returns the days that had a record. Days without one are left alone.
        """
        removed = []
        for d in days:
            if self.days.pop(d.isoformat(), None) is not None:
                removed.append(d)
        return removed

    def mark_gaps_pending(self, rows: Iterable[GapRow]) -> list[date]:
        """`mark_pending` for every day of a gap report that is `no_data`, `short` or `partial`."""
        return self.mark_pending(r.day for r in rows if r.status != GAP_COMPLETE)

    def to_json(self) -> dict[str, object]:
        return {
            "version": MANIFEST_VERSION,
            "symbol": self.symbol,
            "conId": self.con_id,
            "data_zone": self.data_zone,
            "use_rth": self.use_rth,
            "what_to_show": self.what_to_show,
            "bar_size": self.bar_size,
            "days": {k: {"status": r.status, "bars": r.bars, "error_code": r.error_code,
                         "error_text": r.error_text, "fetched_utc": r.fetched_utc}
                     for k, r in sorted(self.days.items())},
        }


def manifest_path(data_dir: Path, symbol: str) -> Path:
    """`<data_dir>/<SYMBOL>/manifest.json`; `ValueError` for a symbol that is not a plain ticker."""
    return symbol_folder(data_dir, symbol) / MANIFEST_NAME


def symbol_folder(data_dir: Path, symbol: str) -> Path:
    """`<data_dir>/<SYMBOL>`, where the manifest, the bars and the gap files of one symbol live."""
    check_symbol(symbol)
    return month_file_path(data_dir, symbol, 2000, 1).parent


def _parse_manifest(text: str, expected: Manifest) -> Manifest:
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("version") != MANIFEST_VERSION:
        raise ValueError("not a manifest of this version")
    identity = {"symbol": expected.symbol, "conId": expected.con_id,
                "data_zone": expected.data_zone, "use_rth": expected.use_rth,
                "what_to_show": expected.what_to_show, "bar_size": expected.bar_size}
    for key, want in identity.items():
        if raw.get(key) != want:
            raise ValueError(f"manifest {key} is {raw.get(key)!r}, expected {want!r}")
    result = Manifest(expected.symbol, expected.con_id, expected.use_rth, expected.what_to_show,
                      expected.bar_size, expected.data_zone)
    days = raw.get("days")
    if not isinstance(days, dict):
        raise ValueError("manifest has no days")
    for key, rec in days.items():
        day_key = date.fromisoformat(key).isoformat()  # also reads "20240311"; stored normalised
        if day_key in result.days:
            raise ValueError(f"day {day_key} appears twice")
        if not isinstance(rec, dict):
            raise ValueError(f"bad record for {key}")
        status = rec.get("status")
        bars, code = rec.get("bars", 0), rec.get("error_code")
        text_, stamp = rec.get("error_text", ""), rec.get("fetched_utc", "")
        try:
            validate_day_fields(status, bars, code, text_, stamp)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad record for {key}: {exc}") from exc
        result.days[day_key] = DayRecord(status, bars, code, text_, stamp)
    return result


def _set_aside(path: Path) -> bool:
    """Move `path` to the first free `<name>.unusable`, `.unusable.1`, `.unusable.2`, ...

    Never overwrites or deletes an earlier set-aside file. False if it could not be moved.
    """
    for n in range(10_000):
        target = path.with_name(path.name + (".unusable" if n == 0 else f".unusable.{n}"))
        if target.exists():
            continue
        try:
            os.rename(path, target)  # refuses an existing target on Windows
        except FileExistsError:
            continue
        except OSError:
            return False
        return True
    return False


def load_manifest(path: Path, expected: Manifest) -> tuple[Manifest, str | None]:
    """The manifest stored at `path`, or a fresh one built from `expected`'s identity.

    A missing file gives a fresh manifest and `None`. A truncated, unparseable,
    foreign or mismatching file (other symbol, conId, zone, bar size, ...) is
    never trusted and never deleted: it is moved to the first free
    `<path>.unusable`, `<path>.unusable.1`, ... (earlier ones are kept), the
    result is fresh, and the second item says why.
    """
    path = Path(path)
    fresh = Manifest(expected.symbol, expected.con_id, expected.use_rth, expected.what_to_show,
                     expected.bar_size, expected.data_zone)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh, None
    except (OSError, UnicodeDecodeError) as exc:
        reason = f"cannot read {path.name}: {exc}"
    else:
        try:
            return _parse_manifest(text, expected), None
        except (ValueError, TypeError) as exc:  # JSONDecodeError is a ValueError
            reason = f"unusable {path.name}: {exc}"
    if not _set_aside(path):
        reason += " (it could not be set aside)"
    return fresh, reason


def _write_text_atomic(
    path: Path, text: str, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> None:
    """Write `text` to `path` through a temp file in the same folder and `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as temp:
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        deadline = clock() + REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if clock() >= deadline:
                    raise
                sleep(REPLACE_RETRY_PAUSE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def save_manifest(
    path: Path, manifest: Manifest, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> None:
    """Write the manifest atomically: a reader sees the old or the new file, never half."""
    _write_text_atomic(path, json.dumps(manifest.to_json(), indent=2) + "\n", sleep=sleep, clock=clock)


# --- gap report -------------------------------------------------------------

GAP_COMPLETE = "complete"
GAP_SHORT = "short"
GAP_PARTIAL = "partial"
GAP_NO_DATA = "no_data"
GAP_NOT_FETCHED = "not_fetched"  # a day this run never asked IB for (the run stopped first)
GAPS_CSV = "gaps.csv"
GAPS_SUMMARY = "gaps_summary.txt"


@dataclass(frozen=True, slots=True)
class GapRow:
    day: date
    status: str
    expected: int
    present: int
    missing: tuple[tuple[datetime, datetime], ...]  # inclusive minute ranges, naive data zone

    @property
    def missing_count(self) -> int:
        return self.expected - self.present


def expected_minutes(day: date, data_zone: str = DATA_ZONE) -> list[datetime]:
    """The 390 minute stamps 09:30 to 15:59 New York of `day`, as naive `data_zone` times."""
    ny = _zone(DATA_ZONE)
    zone = _zone(data_zone)
    start = datetime.combine(day, SESSION_OPEN, tzinfo=ny).astimezone(_UTC)
    return [(start + timedelta(minutes=i)).astimezone(zone).replace(tzinfo=None)
            for i in range(SESSION_MINUTES)]


def _ranges(missing: Sequence[datetime]) -> tuple[tuple[datetime, datetime], ...]:
    out: list[tuple[datetime, datetime]] = []
    step = timedelta(minutes=1)
    for stamp in missing:
        if out and stamp - out[-1][1] == step:
            out[-1] = (out[-1][0], stamp)
        else:
            out.append((stamp, stamp))
    return tuple(out)


def gap_report(
    days: Iterable[date], bars: pd.DataFrame, *, data_zone: str = DATA_ZONE,
    not_requested: Collection[date] = (),
) -> list[GapRow]:
    """One `GapRow` per day, in the order given, comparing `bars` (naive data-zone `date`) to the session.

    Status: `no_data` (no bar in the session minutes, so also a day that was
    never fetched, unless it is in `not_requested`), `not_fetched` (no bar, and the
    day is in `not_requested`: the run stopped before it was asked for; a day with
    bars keeps its bar-based status), `complete` (all 390 minutes), `short` (bars complete from
    09:30 to 12:59, an early close at 13:00, and nothing after) or `partial`
    (any other gap: a hole, a missing tail such as only 15:59, a one-bar day).
    Bars outside 09:30 to 15:59 are ignored; duplicates count once and order
    does not matter. A tz-aware `date` column or timestamps not on whole minutes
    raise `ValueError` (they would silently match nothing).
    """
    dates = _bar_times(bars, "gap_report")
    present = set(dates.dt.to_pydatetime()) if len(dates) else set()
    rows = []
    for day in days:
        minutes = expected_minutes(day, data_zone)
        missing = [m for m in minutes if m not in present]
        have = len(minutes) - len(missing)
        ranges = _ranges(missing)
        if have == 0:
            status = GAP_NOT_FETCHED if day in not_requested else GAP_NO_DATA
        elif not missing:
            status = GAP_COMPLETE
        elif have == EARLY_CLOSE_MINUTES and missing == minutes[EARLY_CLOSE_MINUTES:]:
            status = GAP_SHORT
        else:
            status = GAP_PARTIAL
        rows.append(GapRow(day, status, len(minutes), have, ranges))
    return rows


def _range_text(span: tuple[datetime, datetime]) -> str:
    return f"{span[0]:%H:%M}-{span[1]:%H:%M}"


def gaps_csv_text(rows: Sequence[GapRow], symbol: str) -> str:
    """CSV text, header `symbol,day,status,expected,present,missing,ranges` (`;` joins ranges)."""
    check_symbol(symbol)
    lines = ["symbol,day,status,expected,present,missing,ranges"]
    for r in rows:
        ranges = ";".join(_range_text(s) for s in r.missing)
        lines.append(f"{symbol},{r.day.isoformat()},{r.status},{r.expected},{r.present},"
                     f"{r.missing_count},{ranges}")
    return "\n".join(lines) + "\n"


def gaps_summary_text(rows: Sequence[GapRow], symbol: str = "") -> str:
    """Plain-text summary: counts per status, total missing minutes, and every day that is not complete."""
    counts = {s: sum(1 for r in rows if r.status == s)
              for s in (GAP_COMPLETE, GAP_SHORT, GAP_PARTIAL, GAP_NO_DATA)}
    head = f"Gap report{' for ' + symbol if symbol else ''}: {len(rows)} days"
    not_fetched = sum(1 for r in rows if r.status == GAP_NOT_FETCHED)
    lines = [head] + [f"  {s}: {n}" for s, n in counts.items()]
    if not_fetched:  # only after a stopped run: a run that asked for every day has no such line
        lines.append(f"  {GAP_NOT_FETCHED}: {not_fetched}")
    lines.append(f"  missing minutes in days with data: "
                 f"{sum(r.missing_count for r in rows if r.status not in (GAP_NO_DATA, GAP_NOT_FETCHED))}")
    for r in rows:
        if r.status == GAP_COMPLETE:
            continue
        detail = ("" if r.status in (GAP_NO_DATA, GAP_NOT_FETCHED)
                  else " " + ", ".join(_range_text(s) for s in r.missing))
        lines.append(f"{r.day.isoformat()} {r.status}{detail}")
    return "\n".join(lines) + "\n"


def write_gap_files(
    symbol_folder_path: Path, rows: Sequence[GapRow], symbol: str, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> tuple[Path, Path]:
    """Write `gaps.csv` and `gaps_summary.txt` atomically into the symbol's own folder; returns both paths.

    `symbol_folder_path` is that symbol's folder (see `symbol_folder`), given
    explicitly. The driver writes one pair per symbol folder, so two symbols
    never share a file; the csv also carries a leading `symbol` column.
    """
    folder = Path(symbol_folder_path)
    csv_path, text_path = folder / GAPS_CSV, folder / GAPS_SUMMARY
    _write_text_atomic(csv_path, gaps_csv_text(rows, symbol), sleep=sleep, clock=clock)
    _write_text_atomic(text_path, gaps_summary_text(rows, symbol), sleep=sleep, clock=clock)
    return csv_path, text_path
