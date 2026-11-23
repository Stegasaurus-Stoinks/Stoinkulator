"""The fetch driver: download 1-minute regular-hours bars day by day, resume, report gaps.

`fetch(...)` asks IB, through an open `IbConnection` and its `IbMarketData`, for one
regular session ("1 D", `useRTH=1`) per trading day and per symbol, writes the bars
into one CSV file per symbol per month (`engine.feed` month layout) and keeps a
manifest per symbol so a later run asks only for what is missing. It sends data
requests only (no order of any kind can leave `IbConnection`), consults no process
setting, prints nothing (progress goes to the `on_progress` callback) and sleeps and
reads time only through the `clock` and `sleep` it is given.

Run, in this order
------------------
1. Before anything is sent: arguments, and the folder layout of every symbol (a legacy
   single file `_SYM_offlinedata_`, or both layouts, is a `FetchSetupError`).
2. One `request_server_time` / `wait_server_time`. IB's clock is `server_now` for
   `plan_range` and `filter_day`; there is no fallback to the machine's clock, a
   missing answer ends the run (`FetchResult.fatal`) before any history request.
3. `plan_range`: the closed trading days (the days not closed yet are reported, never
   fetched) and an up-front `FetchEstimate` (requests and the minimum duration at the
   pacer's settings; an upper bound, since days already done are skipped).
4. `wait_for_data_farm(farm="historical")`; False ends the run.
5. Per symbol, in the order given: `resolve_stock` (the only source of a contract;
   the conId goes into the manifest), `load_manifest` (an unusable or mismatching
   manifest stops THAT symbol, nothing is fetched for it, the file was set aside by
   `load_manifest` and is never deleted), then every pending day, one request each,
   through ONE `Pacer` for the whole run.
6. Gap report (`gap_report`, `write_gap_files`) for every symbol that got as far as
   its contract, also after a stopped run, for what is on disk.

One day (order of writes)
-------------------------
Every bar is converted with `bar_from_ib` (a `BadBar` fails the whole day, nothing of
it is written), then `filter_day`, then the day is merged into that month's frame
(the day's earlier rows are dropped, the new ones added, times strictly increasing),
then `write_month_file` (atomic; skipped when the file would not change), and only
then `Manifest.record(day, "ok", ...)` and `save_manifest`. A crash between the two
leaves bars on disk and the day pending; the next run fetches it again and the merge
gives the same file.

Outcomes of one day
-------------------
==================================================== ===========================================
what IB did                                          what is recorded
==================================================== ===========================================
end marker, bars of the day                          `ok` (also with fewer than 390 bars: listed in
                                                     `SymbolResult.short_days`, never fails the run)
end marker, no bar of the requested day (holiday:    `no_data` (complete answer), but only when the
IB returns the previous session)                     session closed at least `settle_seconds` before
                                                     the server time; sooner: `failed` ("no data
                                                     yet"), so the next run asks again
error 162 with "no data" text (`NO_DATA_PHRASES`),   as the line above
no bars
error 162 with "pacing violation" text               wait `pacing_backoff` seconds (sliced),
(`PACING_VIOLATION_PHRASES`)                         retry the day ONCE; a second pacing
                                                     violation in the run: day `failed`, the
                                                     run STOPS for all symbols
code 1101 (`data_lost`)                              cancel, discard, ask again once through the
                                                     pacer, then `failed`
`lost` (1100 at the deadline) or `closed`            day `failed`, the run STOPS (everything
                                                     finished is on disk, the manifest saved)
timeout, any other IB error (162 with other text,    `failed` with the code and IB's text, never
200, 321, 354, 366, 504, 10089, 10167, ...), a       `no_data`; the next run asks again
`BadBar`, a `filter_day` error
no data (end marker, or 162 "no data") for a day     `failed` (`no_data_but_bars_on_disk`, IB's code and
that already has bars in its month file              text kept; the bars stay), never `no_data`
a month file that `engine.feed` would refuse         first pending day of that month `failed`
(columns, zone-aware dates, a row outside its        (`damaged_file`), THAT SYMBOL stops (state
month, times not strictly increasing), found         `stopped`, file untouched, NO request sent for
BEFORE the first request of a symbol's month         it), the next symbol goes on; a file that
(once per symbol and month); or one that cannot      cannot be merged into later is treated the same
be merged into or read later
==================================================== ===========================================

Verbatim texts
--------------
The texts in progress events, in the result (`ib_errors`, `failures`, `message`) and in
`manifest.json` are verbatim from IB and may contain account identifiers. This module
does not scrub them (the owner needs the real text on the first run); the command layer
must scrub before it displays anything, and the manifest must be treated like a
recording: never commit one without reading it first.

What the owner's first session must confirm (cannot be checked offline)
-----------------------------------------------------------------------
The 162 texts below, that TWS accepts the end-time form, that `formatDate=2` gives
epoch seconds at the START of the minute (first bar 09:30, last 15:59) and the unit of
the volume. The result makes them observable: `SymbolResult.first_day` (first and last
bar time, bar count and the first bar's volume of the first requested day of each
symbol) and `FetchResult.ib_errors` (every IB error text verbatim); both are also
progress events.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from MainStoinker.engine.feed import (
    COLUMNS,
    month_file_path,
    offline_data_path,
    read_offline_bars,
    read_symbol_bars,
    symbol_bar_files,
    write_month_file,
)
from MainStoinker.ib import events
from MainStoinker.ib.bars import BadBar, bar_from_ib
from MainStoinker.ib.connection import ConnState, IbConnection, NotConnected
from MainStoinker.ib.history import (
    DATA_ZONE,
    DEFAULT_BAR_SIZE,
    DEFAULT_WHAT_TO_SHOW,
    GAP_PARTIAL,
    GAP_SHORT,
    STATUS_FAILED,
    STATUS_NO_DATA,
    STATUS_OK,
    GapRow,
    Manifest,
    Pacer,
    SentTooEarly,
    day_request,
    filter_day,
    format_end_date_time,
    gap_report,
    load_manifest,
    manifest_path,
    pacing_request_key,
    plan_range,
    save_manifest,
    session_close_utc,
    symbol_folder,
    write_gap_files,
)
from MainStoinker.ib.market_data import (
    NOTICE_CODES,
    DataLost,
    IbMarketData,
    Outcome,
    ResolveConnectionClosed,
    ResolveConnectionLost,
    ResolveFailed,
    StockInfo,
)

PACING_ERROR_CODE = 162
CODE_CONNECTIVITY_LOST = 1100
CODE_DATA_LOST = 1101

# The texts that tell the two meanings of error 162 apart. ASSUMED from IB's
# documentation and reports, to be confirmed in the first owner session: the real texts
# are in `FetchResult.ib_errors` after that run. Matching is case-insensitive substring.
PACING_VIOLATION_PHRASES: tuple[str, ...] = ("pacing violation",)
NO_DATA_PHRASES: tuple[str, ...] = ("returned no data",)

KIND_PACING = "pacing"
KIND_NO_DATA = "no_data"
KIND_OTHER = "other"

DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_RESOLVE_TIMEOUT = 10.0
DEFAULT_SERVER_TIME_TIMEOUT = 10.0
DEFAULT_FARM_TIMEOUT = 30.0
DEFAULT_PACING_BACKOFF = 600.0
DEFAULT_PAUSE_SLICE = 5.0
DEFAULT_SETTLE_SECONDS = 3 * 3600.0
MAX_RECORDED_ERRORS = 1000


class FetchSetupError(ValueError):
    """The arguments or the folder layout are unusable; raised before anything is requested."""


def classify_162(text: str) -> str:
    """`KIND_PACING`, `KIND_NO_DATA` or `KIND_OTHER` for the text of an IB error 162.

    The phrases are `PACING_VIOLATION_PHRASES` and `NO_DATA_PHRASES` (matched
    case-insensitively; pacing wins if both occur). They are assumptions to be
    confirmed in the first owner session; any other text is `KIND_OTHER`, which the
    driver records as `failed`.
    """
    low = text.lower()
    if any(p in low for p in PACING_VIOLATION_PHRASES):
        return KIND_PACING
    if any(p in low for p in NO_DATA_PHRASES):
        return KIND_NO_DATA
    return KIND_OTHER


# --- result types ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchEstimate:
    """What the run will ask for, worked out before the first history request."""

    symbols: int
    days: int  # closed trading days per symbol in the range
    requests: int  # symbols * days: an upper bound (days already done are skipped)
    min_seconds: float  # at the pacer's settings, from the first to the last request
    not_closed: int  # trading days of the range that are not closed yet (never fetched)

    def describe(self) -> str:
        hours, rest = divmod(int(self.min_seconds), 3600)
        return (f"up to {self.requests} requests ({self.symbols} symbols x {self.days} days), "
                f"at least {hours} h {rest // 60:02d} min at the pacing limits")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One structured progress message (the module prints nothing).

    `kind`: estimate, server_time, farm_ready, resolve, symbol_start, symbol_stopped, symbol_done,
    day, first_day, ib_error, pacing_wait, pacing_backoff, not_closed, gaps, stopped,
    done. `detail` holds plain values; `message` is a readable one-liner.
    """

    kind: str
    message: str
    symbol: str | None = None
    day: date | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DayFailure:
    symbol: str
    day: date
    code: int | None
    text: str
    # reason: error, timeout, pacing, data_lost, bad_bar, invalid_data, lost, closed, disk,
    # no_data_yet (closed less than `settle_seconds` ago), damaged_file (month file refused or
    # not mergeable; the symbol stops), no_data_but_bars_on_disk (IB empty, bars already stored)
    reason: str


@dataclass(frozen=True, slots=True)
class IbErrorRecord:
    """An IB error as it came (text verbatim), with where it arrived."""

    symbol: str | None
    day: date | None
    code: int
    text: str
    req_id: int
    notice: bool  # codes 2100 to 2199


@dataclass(frozen=True, slots=True)
class FirstDay:
    """The first requested day of a symbol, for the owner to check IB's conventions by eye.

    `raw_*` describe every bar IB sent (before `filter_day`), the others the bars of the
    requested day; times are naive New York wall-clock times, None when there is no bar.
    """

    symbol: str
    day: date
    status: str  # ok, no_data or failed
    raw_count: int
    raw_first: datetime | None
    raw_last: datetime | None
    bars: int
    first_bar: datetime | None
    last_bar: datetime | None
    first_volume: int | None


@dataclass(slots=True)
class SymbolResult:
    symbol: str
    con_id: int | None = None
    state: str = "not_started"  # not_started, done, stopped, manifest, resolve_failed
    message: str = ""
    planned: int = 0  # days asked for in this run (not done before)
    ok: int = 0
    no_data: int = 0
    failed: int = 0
    already_done: int = 0
    not_closed: int = 0  # trading days skipped because their session has not closed
    failures: list[DayFailure] = field(default_factory=list)
    first_day: FirstDay | None = None
    gaps: list[GapRow] = field(default_factory=list)
    gap_files: tuple[Path, Path] | None = None
    gap_error: str = ""
    # (day, bars on disk, gap status "short" or "partial") for every day of the range with
    # bars that is not a full session; the days stay `ok` (missing minutes are data)
    short_days: list[tuple[date, int, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        """Planned days that were not attempted (the run stopped first)."""
        return self.planned - self.ok - self.no_data - self.failed


@dataclass(slots=True)
class FetchResult:
    symbols: dict[str, SymbolResult] = field(default_factory=dict)
    estimate: FetchEstimate | None = None
    server_now: datetime | None = None
    not_closed: list[date] = field(default_factory=list)
    connection_lost: bool = False  # 1100 in force at a request, or the connection closed
    pacing_stopped: bool = False  # a second pacing violation ended the run
    stop_reason: str | None = None  # why the run ended early, None when it ran to the end
    fatal: str | None = None  # the run could not start or hit a fault (no server time, no farm, disk)
    ib_errors: list[IbErrorRecord] = field(default_factory=list)
    ib_errors_dropped: int = 0
    days_with_gaps: int = 0  # total of `SymbolResult.short_days`; does not affect `complete`

    @property
    def complete(self) -> bool:
        """True when nothing stopped the run and no symbol has a failed day or stopped."""
        return (self.stop_reason is None and self.fatal is None and all(
            r.state == "done" and r.failed == 0 for r in self.symbols.values()))


Progress = Callable[[ProgressEvent], None]


def estimate_duration(
    requests: int, *, min_gap: float = 10.0, window: float = 600.0, max_in_window: int = 60,
    contract_window: float = 2.0, max_per_contract: int = 5, identical_gap: float = 15.0,
    margin: float = 1.0,
) -> float:
    """Seconds from the first to the last of `requests` history requests at the pacing limits.

    Worked out by running a `Pacer` with those settings on a virtual clock (one
    contract, distinct requests, each sent the moment it is allowed). Answers take
    time as well, so this is a minimum.
    """
    if requests < 0:
        raise ValueError("requests must not be negative")
    now = [0.0]
    pacer = Pacer(lambda: now[0], min_gap=min_gap, window=window, max_in_window=max_in_window,
                  contract_window=contract_window, max_per_contract=max_per_contract,
                  identical_gap=identical_gap, margin=margin)
    first = 0.0
    for i in range(requests):
        reservation = pacer.reserve(1, f"estimate-{i}")
        now[0] += reservation.wait
        if i == 0:
            first = now[0]
        pacer.sent(reservation)
    return now[0] - first


# --- internals ----------------------------------------------------------------------


class _Stop(Exception):
    """Ends the run after what was finished has been recorded."""

    def __init__(self, reason: str, message: str, *, lost: bool = False, pacing: bool = False,
                 fatal: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.lost = lost
        self.pacing = pacing
        self.fatal = fatal


@dataclass(slots=True)
class _Answer:
    kind: str  # bars, no_data, failed
    reason: str = ""
    code: int | None = None
    text: str = ""
    bars: tuple[events.HistoricalBar, ...] = ()
    stop: _Stop | None = None


@dataclass(slots=True)
class _Symbol:
    result: SymbolResult
    manifest: Manifest
    manifest_file: Path
    info: StockInfo


def _text_of(failures: Sequence[events.IbError]) -> tuple[str, int | None, str]:
    """(kind, code, text) the IB failures of one request stand for."""
    for failure in failures:
        if failure.code == PACING_ERROR_CODE and classify_162(failure.message) == KIND_PACING:
            return KIND_PACING, failure.code, failure.message
    if not failures:
        return KIND_OTHER, None, "IB reported an error but sent none that can be read"
    first = failures[0]
    if first.code == PACING_ERROR_CODE and classify_162(first.message) == KIND_NO_DATA:
        return KIND_NO_DATA, first.code, first.message
    return KIND_OTHER, first.code, first.message


def _minute_frame(times_and_rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(times_and_rows, columns=list(COLUMNS))
    return frame.astype({"time": "int64", "open": "float64", "high": "float64", "low": "float64",
                         "close": "float64", "volume": "int64"})


def _same_rows(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    return len(a) == len(b) and all(a[c].tolist() == b[c].tolist() for c in COLUMNS)


def check_month_file(path: Path, year: int, month: int) -> None:
    """Raise `ValueError` if `engine.feed.read_symbol_bars` would refuse the month file at `path`.

    The rules it applies to a month file: the columns and parseable data
    (`read_offline_bars`), naive dates, every row inside `year`-`month`, strictly
    increasing dates. Reads the file once; `path` must exist.
    """
    frame = read_offline_bars(path)
    dates = frame["date"]
    try:
        zone = dates.dt.tz
    except AttributeError:  # mixed offsets leave an object column
        raise ValueError(f"month file {path.name} has time-zone aware dates") from None
    if zone is not None:
        raise ValueError(f"month file {path.name} has time-zone aware dates")
    outside = (dates.dt.year != year) | (dates.dt.month != month)
    if outside.any():
        raise ValueError(f"month file {path.name} has a row outside {year:04d}-{month:02d}: "
                         f"{dates[outside].iloc[0]}")
    if not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError(f"month file {path.name} has dates that are not strictly increasing")


def merge_day(path: Path, day: date, new: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """The month frame with `day` replaced by `new`, and whether it differs from the file.

    Reads the month file at `path` if there is one (`ValueError` if it is damaged:
    not strictly increasing, or rows outside its month), drops the rows of `day`, adds
    `new` and sorts by time. The result has strictly increasing times (else
    `ValueError`). Nothing is written.
    """
    if not path.is_file():
        return new.reset_index(drop=True), True
    existing = read_offline_bars(path)
    dates = existing["date"]
    if not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError(f"month file {path.name} has times that are not strictly increasing")
    keep = existing.loc[dates.dt.date != day]
    merged = new.reset_index(drop=True) if keep.empty else (
        pd.concat([keep, new], ignore_index=True).sort_values("date", kind="stable")
        .reset_index(drop=True))
    if merged["date"].duplicated().any() or not merged["date"].is_monotonic_increasing:
        raise ValueError(f"merging {day} into {path.name} would not give strictly increasing times")
    return merged, not _same_rows(existing, merged)


def _with_ib_text(ib_text: str, explanation: str) -> str:
    """IB's own text (if any) followed by our explanation."""
    return f"{ib_text}; {explanation}" if ib_text else explanation


def _on_disk_text(ib_text: str, count: int, day: date) -> str:
    return _with_ib_text(ib_text, f"IB returned no bars for {day} but the month file already holds "
                         f"{count} bar(s) of that day; the existing bars were kept")


class _Run:
    def __init__(
        self, md: IbMarketData, connection: IbConnection, out_dir: Path, zone: str,
        clock: Callable[[], float], sleep: Callable[[float], None], on_progress: Progress | None,
        pacer: Pacer, request_timeout: float, resolve_timeout: float, pacing_backoff: float,
        pause_slice: float, settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        self.settle_seconds = settle_seconds
        self.md = md
        self.conn = connection
        self.out_dir = out_dir
        self.zone = zone
        self.clock = clock
        self.sleep = sleep
        self.on_progress = on_progress
        self.pacer = pacer
        self.request_timeout = request_timeout
        self.resolve_timeout = resolve_timeout
        self.pacing_backoff = pacing_backoff
        self.pause_slice = pause_slice
        self.result = FetchResult()
        self.server_now: datetime | None = None
        self.t0 = 0.0
        self.pacing_hits = 0
        self.days: list[date] = []
        self.current: _Symbol | None = None
        self.checked_months: set[Path] = set()  # month files read and found usable in this run

    # -- helpers ----------------------------------------------------------------

    def emit(self, kind: str, message: str, symbol: str | None = None, day: date | None = None,
             **detail: object) -> None:
        if self.on_progress is not None:
            self.on_progress(ProgressEvent(kind, message, symbol, day, detail))

    def fetched_utc(self) -> datetime:
        assert self.server_now is not None
        return self.server_now + timedelta(seconds=max(0.0, self.clock() - self.t0))

    def guard_connection(self) -> None:
        state = self.conn.state
        if state is ConnState.CLOSED:
            raise _Stop("connection closed", "the connection to TWS is closed", lost=True)
        if state is ConnState.LOST:
            raise _Stop("connection lost", "IB reported lost connectivity (code 1100)", lost=True)

    def pause(self, seconds: float) -> None:
        """Sleep `seconds` through the injected sleep, in slices, noticing a lost connection."""
        left = seconds
        while left > 0:  # counted, not read from the clock: bounded whatever the clock does
            self.guard_connection()
            step = min(self.pause_slice, left)
            self.sleep(step)
            left -= step

    def note_errors(self, symbol: str | None, day: date | None, errors: Sequence[events.IbError]) -> None:
        for err in errors:
            notice = err.code in NOTICE_CODES
            if len(self.result.ib_errors) >= MAX_RECORDED_ERRORS:
                self.result.ib_errors_dropped += 1
            else:
                self.result.ib_errors.append(
                    IbErrorRecord(symbol, day, err.code, err.message, err.req_id, notice))
            self.emit("ib_error", f"IB error {err.code}: {err.message}", symbol, day,
                      code=err.code, text=err.message, req_id=err.req_id, notice=notice)

    # -- the run ----------------------------------------------------------------

    def run(self, symbols: Sequence[str], start: date, end: date, server_time_timeout: float,
            farm_timeout: float) -> FetchResult:
        result = self.result
        for symbol in symbols:
            result.symbols[symbol] = SymbolResult(symbol)
        try:
            self._start(symbols, start, end, server_time_timeout, farm_timeout)
            for symbol in symbols:
                self._symbol(symbol)
        except _Stop as stop:
            self._stopped(stop)
        finally:
            self._gap_reports()
        self.emit("done", "fetch finished" if result.stop_reason is None
                  else f"fetch stopped early: {result.stop_reason}", stop_reason=result.stop_reason)
        return result

    def _stopped(self, stop: _Stop) -> None:
        result = self.result
        result.stop_reason = stop.reason
        result.connection_lost = result.connection_lost or stop.lost
        result.pacing_stopped = result.pacing_stopped or stop.pacing
        if stop.fatal:
            result.fatal = stop.message
        if self.current is not None:
            self.current.result.message = stop.message
        for sym in result.symbols.values():
            if sym.state == "not_started":
                sym.message = sym.message or f"not started: {stop.message}"
        self.emit("stopped", f"run stopped: {stop.message}", reason=stop.reason)

    def _start(self, symbols: Sequence[str], start: date, end: date, server_time_timeout: float,
               farm_timeout: float) -> None:
        try:
            self.md.request_server_time()
            server_now = self.md.wait_server_time(server_time_timeout)
        except NotConnected:
            raise _Stop("connection closed", "the connection to TWS is closed", lost=True) from None
        except DataLost:
            server_now = None
        if server_now is None:
            self.guard_connection()
            raise _Stop("no server time", "IB did not answer the server time request; nothing was "
                        "requested (the machine's clock is not used instead)", fatal=True)
        self.server_now = server_now
        self.t0 = self.clock()
        self.result.server_now = server_now
        self.emit("server_time", f"IB server time {server_now.isoformat()}",
                  server_time=server_now.isoformat())
        try:
            plan = plan_range(start, end, server_now)
        except ValueError as exc:
            raise _Stop("nothing to fetch", str(exc), fatal=True) from exc
        self.days = plan.days
        self.result.not_closed = list(plan.not_closed)
        count = len(symbols) * len(plan.days)
        seconds = estimate_duration(
            count, min_gap=self.pacer.min_gap, window=self.pacer.window,
            max_in_window=self.pacer.max_in_window, contract_window=self.pacer.contract_window,
            max_per_contract=self.pacer.max_per_contract, identical_gap=self.pacer.identical_gap,
            margin=self.pacer.margin)
        self.result.estimate = FetchEstimate(len(symbols), len(plan.days), count, seconds,
                                             len(plan.not_closed))
        self.emit("estimate", self.result.estimate.describe(), requests=count, min_seconds=seconds,
                  days=len(plan.days), symbols=len(symbols))
        if plan.not_closed:
            self.emit("not_closed", f"{len(plan.not_closed)} trading day(s) not closed yet, "
                      f"not fetched: {', '.join(d.isoformat() for d in plan.not_closed)}",
                      days=[d.isoformat() for d in plan.not_closed])
        if not self.conn.wait_for_data_farm(farm_timeout, farm="historical"):
            self.guard_connection()
            raise _Stop("no data farm", f"the historical data farm did not report ready within "
                        f"{farm_timeout:g} s; nothing was requested", fatal=True)
        self.emit("farm_ready", "the historical data farm is ready")

    def _symbol(self, symbol: str) -> None:
        res = self.result.symbols[symbol]
        res.not_closed = len(self.result.not_closed)
        self.emit("resolve", f"{symbol}: resolving the contract", symbol)
        try:
            info = self.md.resolve_stock(symbol, self.resolve_timeout)
        except ResolveConnectionClosed:
            raise _Stop("connection closed", "the connection closed while resolving "
                        f"{symbol}", lost=True) from None
        except ResolveConnectionLost:
            raise _Stop("connection lost", f"IB reported lost connectivity (1100) while resolving "
                        f"{symbol}", lost=True) from None
        except NotConnected:
            raise _Stop("connection closed", "the connection to TWS is closed", lost=True) from None
        except ResolveFailed as exc:
            res.state = "resolve_failed"
            res.message = str(exc)
            self.emit("symbol_stopped", f"{symbol}: {exc}", symbol, state=res.state)
            return
        res.con_id = info.con_id
        expected = Manifest(symbol, info.con_id, 1, DEFAULT_WHAT_TO_SHOW, DEFAULT_BAR_SIZE, self.zone)
        mfile = manifest_path(self.out_dir, symbol)
        manifest, reason = load_manifest(mfile, expected)
        if reason is not None:
            res.state = "manifest"
            res.message = (f"{reason}. The file was set aside as {mfile.name}.unusable (never deleted); "
                           f"nothing was fetched for {symbol}. Look at it, then run again to fetch "
                           "from scratch.")
            self.emit("symbol_stopped", f"{symbol}: {res.message}", symbol, state=res.state)
            return
        sym = _Symbol(res, manifest, mfile, info)
        self.current = sym
        pending = manifest.pending(self.days)
        res.planned = len(pending)
        res.already_done = len(self.days) - len(pending)
        self.emit("symbol_start", f"{symbol}: conId {info.con_id}, {len(pending)} day(s) to fetch, "
                  f"{res.already_done} already done", symbol, con_id=info.con_id,
                  pending=len(pending), already_done=res.already_done)
        res.state = "stopped"  # until the loop finishes
        for day in pending:
            if not self._day(sym, day):
                self.emit("symbol_stopped", f"{symbol}: {res.message}", symbol, state=res.state)
                self.current = None
                return
        res.state = "done"
        self.emit("symbol_done", f"{symbol}: {res.ok} ok, {res.no_data} no data, {res.failed} failed",
                  symbol, ok=res.ok, no_data=res.no_data, failed=res.failed)
        self.current = None

    # -- one day ----------------------------------------------------------------

    def _send(self, sym: _Symbol, day: date) -> int:
        con_id = sym.result.con_id
        assert con_id is not None
        request = day_request(day)
        if format_end_date_time(session_close_utc(day)) != request.end_date_time:
            raise _Stop("internal error", f"end time mismatch for {day}", fatal=True)
        reservation = self.pacer.reserve(con_id, pacing_request_key(con_id, request))
        try:
            if reservation.wait > 0:
                self.emit("pacing_wait", f"waiting {reservation.wait:.1f} s for IB's pacing limits",
                          sym.result.symbol, day, seconds=reservation.wait)
                self.pause(reservation.wait)
                self.guard_connection()  # 1100 or a close in the last slice: send nothing
            req_id = self.md.request_history(
                sym.info.contract(), session_close_utc(day), request.duration, use_rth=bool(request.use_rth))
        except NotConnected:
            self.pacer.cancel(reservation)
            raise _Stop("connection closed", "the connection to TWS is closed", lost=True) from None
        except BaseException:
            self.pacer.cancel(reservation)
            raise
        try:
            self.pacer.sent(reservation)
        except SentTooEarly as exc:
            self.md.cancel_history(req_id)
            raise _Stop("pacer", f"a request left {exc.early_by:.3f} s before its allowed time",
                        fatal=True) from exc
        return req_id

    def _answer(self, sym: _Symbol, day: date) -> _Answer:
        symbol = sym.result.symbol
        data_lost = False
        while True:
            self.guard_connection()
            req_id = self._send(sym, day)
            try:
                res = self.md.wait_history(req_id, self.request_timeout)
            finally:
                self.md.cancel_history(req_id)
            self.note_errors(symbol, day, res.errors)
            outcome = res.outcome
            if outcome is Outcome.END:
                return _Answer("bars", bars=res.bars)
            if outcome is Outcome.ERROR:
                kind, code, text = _text_of(res.failures)
                if kind == KIND_PACING:
                    self.pacing_hits += 1
                    if self.pacing_hits >= 2:
                        stop = _Stop("pacing violation", "IB reported a pacing violation twice; "
                                     "the run stops so IB is not hammered", pacing=True)
                        return _Answer("failed", "pacing", code, text, stop=stop)
                    self.emit("pacing_backoff", f"pacing violation: waiting {self.pacing_backoff:g} s, "
                              "then asking for this day once more", symbol, day, text=text)
                    self.pause(self.pacing_backoff)
                    continue
                if kind == KIND_NO_DATA and not res.bars:
                    return _Answer("no_data", code=code, text=text)
                return _Answer("failed", "error", code, text)
            if outcome is Outcome.DATA_LOST:
                if not data_lost:
                    data_lost = True
                    continue
                return _Answer("failed", "data_lost", CODE_DATA_LOST,
                               "IB restored the connection with code 1101 (data lost) twice")
            if outcome is Outcome.LOST:
                stop = _Stop("connection lost", "IB reported lost connectivity (code 1100)", lost=True)
                return _Answer("failed", "lost", CODE_CONNECTIVITY_LOST,
                               "IB reported lost connectivity (1100) and did not answer in time",
                               stop=stop)
            if outcome is Outcome.CLOSED:
                stop = _Stop("connection closed", "the connection to TWS closed", lost=True)
                return _Answer("failed", "closed", None, "the connection closed before IB answered",
                               stop=stop)
            return _Answer("failed", "timeout", None,
                           f"no complete answer from IB within {self.request_timeout:g} s")

    def _day(self, sym: _Symbol, day: date) -> bool:
        """Fetch and store one day; False when this symbol must not go on (damaged month file)."""
        res = sym.result
        damage = self._month_damage(sym, day)  # before any request is spent on this month
        answer = (self._answer(sym, day) if damage is None
                  else _Answer("failed", "damaged_file", None, damage))
        status, bars, code, text, reason = self._settle(sym, day, answer)
        if status == STATUS_OK:
            res.ok += 1
        elif status == STATUS_NO_DATA:
            res.no_data += 1
        else:
            res.failed += 1
            res.failures.append(DayFailure(res.symbol, day, code, text, reason))
        self.emit("day", f"{res.symbol} {day}: {status}" + (f" ({bars} bars)" if bars else "")
                  + (f", {code}: {text}" if status == STATUS_FAILED else ""),
                  res.symbol, day, status=status, bars=bars, code=code, text=text, reason=reason)
        if answer.stop is not None:
            raise answer.stop
        if reason == "damaged_file":
            res.message = (f"{text}. The file was left untouched; nothing more is fetched for "
                           f"{res.symbol} in this run (the other symbols go on). Repair or move "
                           "the file yourself, then run again.")
            return False
        return True

    def _month_damage(self, sym: _Symbol, day: date) -> str | None:
        """The failure text if the month file of `day` is refused, else None (checked once per month)."""
        path = month_file_path(self.out_dir, sym.result.symbol, day.year, day.month)
        if path in self.checked_months:
            return None
        if path.is_file():
            try:
                check_month_file(path, day.year, day.month)
            except (OSError, ValueError, TypeError) as exc:
                return f"month file {path.name} is damaged: {exc}"
        self.checked_months.add(path)
        return None

    def _bars_on_disk(self, path: Path, day: date) -> int:
        """How many bars of `day` the month file at `path` holds (0 if absent or unreadable)."""
        if not path.is_file():
            return 0
        try:
            return int((read_offline_bars(path)["date"].dt.date == day).sum())
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    def _too_soon(self, day: date) -> str | None:
        """The failure text when `day`'s session closed less than `settle_seconds` ago, else None."""
        closed_for = (self.fetched_utc() - session_close_utc(day)).total_seconds()
        if closed_for >= self.settle_seconds:
            return None
        return (f"no data yet (session closed less than {self.settle_seconds / 60:g} minutes ago)")

    def _record(self, sym: _Symbol, day: date, status: str, *, bars: int = 0,
                code: int | None = None, text: str = "") -> None:
        sym.manifest.record(day, status, bars=bars, error_code=code, error_text=text,
                            fetched_utc=self.fetched_utc(), complete_answer=status == STATUS_NO_DATA)
        save_manifest(sym.manifest_file, sym.manifest, sleep=self.sleep, clock=self.clock)

    def _settle(self, sym: _Symbol, day: date, answer: _Answer) -> tuple[str, int, int | None, str, str]:
        """Store the day; returns (status, bars, code, text, reason)."""
        symbol = sym.result.symbol
        try:
            return self._settle_day(sym, day, answer)
        except OSError as exc:
            sym.result.failed += 1
            sym.result.failures.append(DayFailure(symbol, day, None, f"disk error: {exc}", "disk"))
            raise _Stop("disk error", f"cannot write {symbol} {day}: {exc}", fatal=True) from exc

    def _settle_day(self, sym: _Symbol, day: date, answer: _Answer) -> tuple[str, int, int | None, str, str]:
        symbol = sym.result.symbol
        first = sym.result.first_day is None
        if answer.kind == "failed":
            self._record(sym, day, STATUS_FAILED, code=answer.code, text=answer.text)
            self._first(sym, day, STATUS_FAILED, None, None, first)
            return STATUS_FAILED, 0, answer.code, answer.text, answer.reason
        path = month_file_path(self.out_dir, symbol, day.year, day.month)
        if answer.kind == "no_data":
            soon = self._too_soon(day)
            if soon is not None:
                return self._fail(sym, day, _with_ib_text(answer.text, soon), "no_data_yet", first,
                                  code=answer.code)
            on_disk = self._bars_on_disk(path, day)
            if on_disk:
                return self._fail(sym, day, _on_disk_text(answer.text, on_disk, day),
                                  "no_data_but_bars_on_disk", first, code=answer.code)
            self._record(sym, day, STATUS_NO_DATA, code=answer.code, text=answer.text)
            self._first(sym, day, STATUS_NO_DATA, None, None, first)
            return STATUS_NO_DATA, 0, answer.code, answer.text, ""
        converted: list[dict[str, object]] = []
        try:
            converted = [bar_from_ib(b, self.zone).as_record() for b in answer.bars]
        except BadBar as exc:
            return self._fail(sym, day, f"unusable bar from IB: {exc}", "bad_bar", first)
        raw = _minute_frame(converted) if converted else _minute_frame([])
        assert self.server_now is not None
        try:
            kept = filter_day(raw, day, self.server_now, data_zone=self.zone) if len(raw) else raw
        except ValueError as exc:
            return self._fail(sym, day, str(exc), "invalid_data", first, raw)
        if kept.empty:
            soon = self._too_soon(day)
            if soon is not None:
                return self._fail(sym, day, soon, "no_data_yet", first, raw)
            text = (f"complete answer without bars for {day}"
                    + (f" ({len(raw)} bar(s) of other dates dropped)" if len(raw) else ""))
            on_disk = self._bars_on_disk(path, day)
            if on_disk:
                return self._fail(sym, day, _on_disk_text(text, on_disk, day),
                                  "no_data_but_bars_on_disk", first, raw)
            self._record(sym, day, STATUS_NO_DATA, text=text)
            self._first(sym, day, STATUS_NO_DATA, raw, kept, first)
            return STATUS_NO_DATA, 0, None, text, ""
        try:
            merged, changed = merge_day(path, day, kept)
        except (OSError, ValueError, TypeError) as exc:  # second line behind `_month_damage`
            return self._fail(sym, day, f"month file {path.name} is damaged: {exc}", "damaged_file",
                              first, raw)
        if changed:
            try:
                write_month_file(path, merged, sleep=self.sleep, clock=self.clock)
            except (ValueError, TypeError) as exc:  # a frame the writer refuses; OSError is a disk fault
                return self._fail(sym, day, f"month file {path.name} is damaged: {exc}",
                                  "damaged_file", first, raw)
        self._record(sym, day, STATUS_OK, bars=len(kept))
        self._first(sym, day, STATUS_OK, raw, kept, first)
        return STATUS_OK, len(kept), None, "", ""

    def _fail(self, sym: _Symbol, day: date, text: str, reason: str, first: bool,
              raw: pd.DataFrame | None = None, *, code: int | None = None,
              ) -> tuple[str, int, int | None, str, str]:
        self._record(sym, day, STATUS_FAILED, code=code, text=text)
        self._first(sym, day, STATUS_FAILED, raw, None, first)
        return STATUS_FAILED, 0, code, text, reason

    def _first(self, sym: _Symbol, day: date, status: str, raw: pd.DataFrame | None,
               kept: pd.DataFrame | None, first: bool) -> None:
        if not first:
            return
        raw_frame = raw if raw is not None else _minute_frame([])
        kept_frame = kept if kept is not None else _minute_frame([])

        def edge(frame: pd.DataFrame, i: int) -> datetime | None:
            return None if frame.empty else frame["date"].iloc[i].to_pydatetime()

        volume_source = kept_frame if not kept_frame.empty else raw_frame
        info = FirstDay(
            sym.result.symbol, day, status, len(raw_frame), edge(raw_frame, 0), edge(raw_frame, -1),
            len(kept_frame), edge(kept_frame, 0), edge(kept_frame, -1),
            None if volume_source.empty else int(volume_source["volume"].iloc[0]))
        sym.result.first_day = info
        self.emit("first_day", f"{info.symbol} {day}: {info.bars} bars {info.first_bar} to "
                  f"{info.last_bar}, first volume {info.first_volume}", info.symbol, day,
                  status=status, raw_bars=info.raw_count,
                  raw_first=None if info.raw_first is None else info.raw_first.isoformat(),
                  raw_last=None if info.raw_last is None else info.raw_last.isoformat(),
                  bars=info.bars,
                  first_bar=None if info.first_bar is None else info.first_bar.isoformat(),
                  last_bar=None if info.last_bar is None else info.last_bar.isoformat(),
                  first_volume=info.first_volume)

    # -- gap reports ------------------------------------------------------------

    def _gap_reports(self) -> None:
        for symbol, res in self.result.symbols.items():
            if res.con_id is None:
                continue
            try:
                try:
                    bars = read_symbol_bars(self.out_dir, symbol)
                except FileNotFoundError:
                    bars = _minute_frame([])
                rows = gap_report(self.days, bars, data_zone=self.zone)
                res.gaps = rows
                res.short_days = [(r.day, r.present, r.status) for r in rows
                                  if r.status in (GAP_SHORT, GAP_PARTIAL)]
                res.gap_files = write_gap_files(symbol_folder(self.out_dir, symbol), rows, symbol,
                                                sleep=self.sleep, clock=self.clock)
                counts: dict[str, int] = {}
                for row in rows:
                    counts[row.status] = counts.get(row.status, 0) + 1
                self.result.days_with_gaps += len(res.short_days)
                self.emit("gaps", f"{symbol}: gap report " + ", ".join(
                    f"{n} {s}" for s, n in sorted(counts.items())), symbol, counts=counts,
                    short_days=[(d.isoformat(), n, s) for d, n, s in res.short_days],
                    days_with_gaps=len(res.short_days))
            except Exception as exc:  # noqa: BLE001 - a broken report must not hide the run's result
                res.gap_error = f"{type(exc).__name__}: {exc}"
                self.emit("gaps", f"{symbol}: gap report failed: {res.gap_error}", symbol,
                          error=res.gap_error)


def check_layouts(out_dir: Path, symbols: Sequence[str]) -> None:
    """`FetchSetupError` for a symbol with a legacy single file or with both layouts."""
    for symbol in symbols:
        legacy = offline_data_path(out_dir, symbol)
        try:
            symbol_bar_files(out_dir, symbol)
        except FileNotFoundError:
            continue
        except ValueError as exc:
            raise FetchSetupError(str(exc)) from exc
        if legacy.is_file():
            raise FetchSetupError(
                f"{symbol} has the single file {legacy} in {out_dir}; fetched bars are written "
                "as month files, choose another folder or remove that file yourself")


def fetch(
    market_data: IbMarketData,
    symbols: Sequence[str],
    start: date,
    end: date,
    out_dir: Path,
    *,
    connection: IbConnection,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    zone: str = DATA_ZONE,
    on_progress: Progress | None = None,
    pacer: Pacer | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    resolve_timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    server_time_timeout: float = DEFAULT_SERVER_TIME_TIMEOUT,
    farm_timeout: float = DEFAULT_FARM_TIMEOUT,
    pacing_backoff: float = DEFAULT_PACING_BACKOFF,
    pause_slice: float = DEFAULT_PAUSE_SLICE,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> FetchResult:
    """Download the 1-minute regular-hours bars of `symbols` for `start` to `end` into `out_dir`.

    `market_data` must belong to the open `connection` (used for the farm wait and the
    connection state); `clock` is a monotonic clock in seconds and `sleep` the way to
    wait, both injected everywhere (the pacer, every timeout, the back-off). `zone` is
    the data time zone (default New York). `pacer` defaults to one `Pacer(clock)` with
    IB's limits; it is the only pacer of the run. `pacing_backoff` is the wait after a
    first pacing violation, `pause_slice` the longest single sleep inside any wait, so
    a lost connection is noticed. `settle_seconds` (default 3 hours): a day without bars
    counts as `no_data` only when its session closed at least that long before the
    server time, else it is `failed` and retried by the next run. Returns the `FetchResult`; it raises only
    `FetchSetupError` (bad arguments or layout, before anything is sent) and whatever a
    broken `on_progress` raises. Never raises for what IB does.
    """
    if isinstance(symbols, str) or not symbols:
        raise FetchSetupError("symbols must be a non-empty list of tickers")
    names = list(symbols)
    if len(set(names)) != len(names):
        raise FetchSetupError("a symbol is listed twice")
    for name in names:
        try:
            symbol_folder(out_dir, name)
        except ValueError as exc:
            raise FetchSetupError(str(exc)) from exc
    if start > end:
        raise FetchSetupError(f"start {start} is after end {end}")
    for label, value in (("request_timeout", request_timeout), ("resolve_timeout", resolve_timeout),
                         ("server_time_timeout", server_time_timeout), ("farm_timeout", farm_timeout),
                         ("pacing_backoff", pacing_backoff), ("pause_slice", pause_slice)):
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < float("inf"):
            raise FetchSetupError(f"{label} must be a finite number above 0, got {value!r}")
    if (isinstance(settle_seconds, bool) or not isinstance(settle_seconds, int | float)
            or not 0 <= settle_seconds < float("inf")):
        raise FetchSetupError(f"settle_seconds must be a finite number, at least 0, got {settle_seconds!r}")
    out = Path(out_dir)
    check_layouts(out, names)
    run = _Run(market_data, connection, out, zone, clock, sleep, on_progress,
               pacer if pacer is not None else Pacer(clock), request_timeout, resolve_timeout,
               pacing_backoff, pause_slice, settle_seconds)
    return run.run(names, start, end, server_time_timeout, farm_timeout)
