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
`no_data`, which resume does not ask for again. Half days come out of the gap
report as `short`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Sequence
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
DEFAULT_BAR_SIZE = "1 min"
DEFAULT_WHAT_TO_SHOW = "TRADES"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_FAILED = "failed"
DAY_STATUSES = (STATUS_OK, STATUS_NO_DATA, STATUS_FAILED)

_UTC = timezone.utc


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")
    return moment


def session_close_utc(day: date) -> datetime:
    """16:00 New York of `day` as an aware UTC datetime (DST handled by the zone database)."""
    return datetime.combine(day, SESSION_CLOSE, tzinfo=_zone(DATA_ZONE)).astimezone(_UTC)


def plan_days(start: date, end: date, server_now: datetime) -> list[date]:
    """Weekdays from `start` to `end` inclusive, in order.

    `server_now` is an aware datetime (the injected clock). If the regular
    session of `end` (16:00 New York) has not closed by `server_now` the plan is
    refused with `ValueError`. `start` after `end` is a `ValueError` too.
    Holidays are not removed (see the module docstring).
    """
    _aware(server_now, "server_now")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if session_close_utc(end) > server_now:
        raise ValueError(f"the regular session of {end} has not closed yet at {server_now.isoformat()}")
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


@dataclass(frozen=True, slots=True)
class DayRequest:
    """Arguments of one `reqHistoricalData` call for one trading day."""

    day: date
    end_date_time: str  # "YYYYMMDD-HH:MM:SS", UTC
    duration: str
    bar_size: str
    what_to_show: str
    use_rth: int

    def key(self) -> str:
        """Identity for the identical-request rule."""
        return "|".join((self.end_date_time, self.duration, self.bar_size,
                         self.what_to_show, str(self.use_rth)))


def day_request(
    day: date, *, bar_size: str = DEFAULT_BAR_SIZE, what_to_show: str = DEFAULT_WHAT_TO_SHOW,
) -> DayRequest:
    """The request for the regular session of `day`: ends 16:00 New York (as UTC), "1 D", RTH only."""
    end = session_close_utc(day)
    return DayRequest(day, end.strftime("%Y%m%d-%H:%M:%S"), "1 D", bar_size, what_to_show, 1)


def filter_day(
    bars: pd.DataFrame, day: date, server_now: datetime, *,
    data_zone: str = DATA_ZONE, bar_minutes: int = 1,
) -> pd.DataFrame:
    """Rows of `bars` (a `date` column of naive data-zone times, the bar start) for `day` only.

    On a holiday IB answers "1 D" with the previous session; those rows are
    dropped. A bar whose minute had not ended at `server_now` is dropped as well
    (it would still change). Returns a copy with a fresh RangeIndex.
    """
    _aware(server_now, "server_now")
    now_local = server_now.astimezone(_zone(data_zone)).replace(tzinfo=None)
    dates = pd.to_datetime(bars["date"])
    keep = (dates.dt.date == day) & (dates + pd.Timedelta(minutes=bar_minutes) <= pd.Timestamp(now_local))
    return bars.loc[keep].reset_index(drop=True)


# --- pacing -----------------------------------------------------------------


class Pacer:
    """Decides how long to wait before the next history request. Never sleeps.

    Defaults are IB's documented limits: at least 10 s between history
    requests, at most 60 per 10 minutes, fewer than 6 per contract per 2 s and
    no identical request within 15 s. Window limits keep `margin` seconds extra
    so that a window never holds more than its limit even when closed at both
    ends. `reserve` returns the wait and books the request at `clock() + wait`;
    the caller waits that long (its own sleep) before sending. Same calls and
    clock readings give the same waits.
    """

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
        if min(max_in_window, max_per_contract) < 1:
            raise ValueError("limits must be at least 1")
        self._clock = clock
        self.min_gap = min_gap
        self.window = window
        self.max_in_window = max_in_window
        self.contract_window = contract_window
        self.max_per_contract = max_per_contract
        self.identical_gap = identical_gap
        self.margin = margin
        self._all: deque[float] = deque(maxlen=max_in_window)
        self._per_contract: dict[Hashable, deque[float]] = {}
        self._identical: dict[tuple[Hashable, Hashable], float] = {}

    def earliest(self, contract: Hashable, request: Hashable = None) -> float:
        """Earliest time (on the injected clock) the request may be sent; books nothing."""
        t = self._clock()
        if self._all:
            t = max(t, self._all[-1] + self.min_gap)
        if len(self._all) >= self.max_in_window:
            t = max(t, self._all[0] + self.window + self.margin)
        sent = self._per_contract.get(contract)
        if sent is not None and len(sent) >= self.max_per_contract:
            t = max(t, sent[0] + self.contract_window + self.margin)
        last_same = self._identical.get((contract, request))
        if last_same is not None:
            t = max(t, last_same + self.identical_gap + self.margin)
        return t

    def reserve(self, contract: Hashable, request: Hashable = None) -> float:
        """Seconds to wait from now before sending; the request is booked at that time."""
        when = self.earliest(contract, request)
        self._all.append(when)
        self._per_contract.setdefault(
            contract, deque(maxlen=self.max_per_contract)).append(when)
        self._identical[(contract, request)] = when
        return when - self._clock()


# --- manifest ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayRecord:
    status: str
    bars: int = 0
    error_code: int | None = None
    error_text: str = ""
    fetched_utc: str = ""


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
        error_text: str = "", fetched_utc: datetime,
    ) -> None:
        """Set the record of `day`. `fetched_utc` must be aware; `ok` needs bars, `no_data` none."""
        if status not in DAY_STATUSES:
            raise ValueError(f"unknown day status {status!r}")
        if status == STATUS_OK and bars < 1:
            raise ValueError("an ok day needs at least one bar")
        if status == STATUS_NO_DATA and bars != 0:
            raise ValueError("a no_data day has no bars")
        stamp = _aware(fetched_utc, "fetched_utc").astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.days[day.isoformat()] = DayRecord(status, bars, error_code, error_text, stamp)

    def pending(self, days: Iterable[date]) -> list[date]:
        """The days still to request, in the given order: unknown and failed ones."""
        done = (STATUS_OK, STATUS_NO_DATA)
        return [d for d in days
                if (rec := self.days.get(d.isoformat())) is None or rec.status not in done]

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
    return month_file_path(data_dir, symbol, 2000, 1).parent / MANIFEST_NAME


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
        date.fromisoformat(key)
        if not isinstance(rec, dict) or rec.get("status") not in DAY_STATUSES:
            raise ValueError(f"bad record for {key}")
        bars, code = rec.get("bars", 0), rec.get("error_code")
        if (isinstance(bars, bool) or not isinstance(bars, int) or bars < 0
                or not (code is None or (isinstance(code, int) and not isinstance(code, bool)))
                or not isinstance(rec.get("error_text", ""), str)
                or not isinstance(rec.get("fetched_utc", ""), str)):
            raise ValueError(f"bad record for {key}")
        result.days[key] = DayRecord(rec["status"], bars, code, rec.get("error_text", ""),
                                     rec.get("fetched_utc", ""))
    return result


def load_manifest(path: Path, expected: Manifest) -> tuple[Manifest, str | None]:
    """The manifest stored at `path`, or a fresh one built from `expected`'s identity.

    A missing file gives a fresh manifest and `None`. A truncated, unparseable,
    foreign or mismatching file (other symbol, conId, zone, bar size, ...) is
    never trusted and never deleted: it is moved to `<path>.unusable`, the
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
    with contextlib.suppress(OSError):
        os.replace(path, path.with_name(path.name + ".unusable"))
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
) -> list[GapRow]:
    """One `GapRow` per day, in the order given, comparing `bars` (naive data-zone `date`) to the session.

    Status: `no_data` (no bar in the session minutes, so also a day that was
    never fetched), `complete` (all 390 minutes), `short` (only the end of the
    session is missing, as on a half day) or `partial` (a gap anywhere else).
    Bars outside 09:30 to 15:59 are ignored.
    """
    present = set(pd.to_datetime(bars["date"]).dt.to_pydatetime()) if len(bars) else set()
    rows = []
    for day in days:
        minutes = expected_minutes(day, data_zone)
        missing = [m for m in minutes if m not in present]
        have = len(minutes) - len(missing)
        ranges = _ranges(missing)
        if have == 0:
            status = GAP_NO_DATA
        elif not missing:
            status = GAP_COMPLETE
        elif len(ranges) == 1 and ranges[0][1] == minutes[-1]:
            status = GAP_SHORT
        else:
            status = GAP_PARTIAL
        rows.append(GapRow(day, status, len(minutes), have, ranges))
    return rows


def _range_text(span: tuple[datetime, datetime]) -> str:
    return f"{span[0]:%H:%M}-{span[1]:%H:%M}"


def gaps_csv_text(rows: Sequence[GapRow]) -> str:
    """CSV with header `day,status,expected,present,missing,ranges` (ranges `HH:MM-HH:MM`, `;` separated)."""
    lines = ["day,status,expected,present,missing,ranges"]
    for r in rows:
        ranges = ";".join(_range_text(s) for s in r.missing)
        lines.append(f"{r.day.isoformat()},{r.status},{r.expected},{r.present},{r.missing_count},{ranges}")
    return "\n".join(lines) + "\n"


def gaps_summary_text(rows: Sequence[GapRow], symbol: str = "") -> str:
    """Plain-text summary: counts per status, total missing minutes, and every day that is not complete."""
    counts = {s: sum(1 for r in rows if r.status == s)
              for s in (GAP_COMPLETE, GAP_SHORT, GAP_PARTIAL, GAP_NO_DATA)}
    head = f"Gap report{' for ' + symbol if symbol else ''}: {len(rows)} days"
    lines = [head] + [f"  {s}: {n}" for s, n in counts.items()]
    lines.append(f"  missing minutes in days with data: "
                 f"{sum(r.missing_count for r in rows if r.status != GAP_NO_DATA)}")
    for r in rows:
        if r.status == GAP_COMPLETE:
            continue
        detail = "" if r.status == GAP_NO_DATA else " " + ", ".join(_range_text(s) for s in r.missing)
        lines.append(f"{r.day.isoformat()} {r.status}{detail}")
    return "\n".join(lines) + "\n"


def write_gap_files(
    folder: Path, rows: Sequence[GapRow], symbol: str = "", *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> tuple[Path, Path]:
    """Write `<folder>/gaps.csv` and `<folder>/gaps_summary.txt` atomically; returns both paths."""
    folder = Path(folder)
    csv_path, text_path = folder / GAPS_CSV, folder / GAPS_SUMMARY
    _write_text_atomic(csv_path, gaps_csv_text(rows), sleep=sleep, clock=clock)
    _write_text_atomic(text_path, gaps_summary_text(rows, symbol), sleep=sleep, clock=clock)
    return csv_path, text_path
