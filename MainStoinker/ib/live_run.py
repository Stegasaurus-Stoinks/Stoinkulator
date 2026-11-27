"""The live paper session driver: watcher, aligner, engine and run writer in one session (issue 197).

    run_live_session(args, connection_factory=..., market_data_factory=..., recorder_factory=...,
                     store=..., clock=..., sleep=..., wall=..., out=...) -> exit code

There is no command line here (the command is the next task). Everything is injected: no global,
no real clock, no real sleep, no socket. The driver holds `IbMarketData` only (a data-only
client: it cannot send anything but data requests); the brokers are the engine's simulated
`PaperBroker` (realistic fills, entries at the next bar's open). Nothing in this module can
place an order, and it imports nothing that can.

The sequence of a session
-------------------------

1. Refuse (exit 2, nothing built, no connection) unless the options are usable: paper mode,
   realistic fills, every algo's `"mode"` is `paper`, a warm-up can be read from the month files,
   `--minutes` is within the driver limits, no stop file is lying about, and today (New York, by this
   computer's clock) is a trading day by the market calendar (a weekend or a holiday is refused). A
   half day (13:00 close) follows the calendar:
   every time below that is derived from 16:00 follows 13:00, the end of the session is 13:20.
2. Close the runs an earlier session left behind (D2b): every `paper-live` run still marked
   running whose last write is stale (`live_counts.STALE_AFTER`) is ended `failed` with the reason
   `abandoned` and said so. A restart is a NEW run (D2); an earlier run is never resumed.
3. Connect and run the watcher (`watch_bars.run_watch`: connection with `require_paper=True`,
   `paper_session_ok` before any request, the account list watched all along, the session
   recording through the injected factory, exit 5 and its lines exactly as the watcher's), with a
   backfill since 09:30, a tee to the live files and to this driver's sink, this driver as the
   listener and `on_pass` as the pass hook.
4. Until every symbol's first backfill is over (or the symbol stopped), live-closed bars wait in
   the aligner. Then the engine starts (see "Warm-up, backfill and live" below) and, in the same
   pass, the run is begun in the store (kind `paper-live`, realistic fills, config and `DataMeta`
   in the formats `backtest` stores).
5. Each pass: the aligner is polled with `clock.stream_now` (never with raw server time, never
   while it is None); every released batch is announced to the writer (`fed`) and given to
   `engine.step`; the faults below are checked; the run is touched (`heartbeat`) when nothing was
   written for `HEARTBEAT_EVERY` s of wall time; the end of the session, the stop file and
   `--minutes` are checked. `on_pass` never raises into the watcher and is short (the engine step
   is what it costs).
6. The end, on EVERY path (watcher stopped, connection closed, not paper, Ctrl-C, fault, error):
   one last poll of what is decided, `engine.end_live()` (open positions are LEFT OPEN and listed
   in the output), then `writer.finish(status, reason, counts, data_meta, engine.gap_report)` with
   the counts and the full `DataMeta` (the last output line says what was STORED: a run that could
   not be closed is "still marked running in the database"); then the end-of-session check of the
   stored run
   (`report --verify`, printed; an UNVERIFIABLE verdict is an alarm about the record, a MISMATCH
   about the engine; neither changes the exit code).

Warm-up, backfill and live (the hand-over rule)
-----------------------------------------------

* Days before today: `live_warmup` frames (the owner's month files, `--warmup-days` trading days).
* Today's earlier bars: every bar the watcher hands to the sink with `how == "backfill"`, which is
  exactly the closed bars of each symbol's FIRST backfill snapshot (also those a file of an
  earlier run already holds). They are appended to that symbol's warm-up frame: algos see them in
  the frame, but no algo is updated on them and nothing trades on them (D1). A bar still forming in
  the snapshot is not among them: it comes later, closed live. They are stored in the run's warm-up
  description (`today_bars`, digest `today_sha256`), which is what lets `report --verify` rebuild
  the warm-up of a session that started after the open.
* Everything else (`how` `later`, `time`, `backfilled`) is a live bar and goes to the aligner at
  once, also while the engine has not started yet. The first bar stepped of a symbol is the first
  one the aligner releases for it.
* No minute can be twice or missing: the watcher writes a minute once and `decided` covers every
  minute it dropped. The driver still refuses an overlap itself: a live bar whose minute is not
  after the newest minute already given for that symbol (the last warm-up bar or the last bar given
  to the aligner) is not given on, counted (`overlap`) and said. A batch the engine refuses anyway
  (`ValueError`) is counted and said; the session goes on (checked with an empty `step`: only a
  failed engine answers `RuntimeError`).
* A batch released more than `STALE_BATCH_AGE` s after its minute ended (a re-requested
  backfill, a backlog) is history, not a bar to trade on: it is not stepped, counted as `late`, and
  the next bar follows a gap (the gap rule refuses entries on it).

Faults (each ends the session through the exit path, never a silent continue)
-----------------------------------------------------------------------------

    fault                                         status  end_reason       exit
    --------------------------------------------  ------  ---------------  ----
    a symbol's stream is labelled delayed         failed  delayed_data     7
    the aligner REFUSED a bar, or a bar was late  failed  data_fault       7
      after its symbol was decided, or a bar was
      late that no re-requested backfill explains
    a symbol silent for SILENT_LIMIT s in session failed  data_fault       7
    a symbol stopped (IB error, file error ...)   failed  symbol_stopped   1
    `stream_now` unmeasured for STREAM_WAIT_LIMIT failed  no_data          6
      s of the session (a symbol shows no bar)
    one step took more than STEP_HARD_SECONDS     failed  step_too_slow    7
    an unexpected error, a store error, an engine failed  error            4
      step that raised `RuntimeError` / any non-refusal
    the connection closed or is still lost        failed  connection_closed 4
    not a paper account (start: nothing stored;   failed  not_paper        5
      later: the run is ended)
    Ctrl-C                                        failed  interrupted      130
    the last session bar decided (or 16:20 plus   completed session_end    0
      SESSION_END_MARGIN; on a half day the
      close is 13:00 and the 13:20 follows)
    `--minutes` elapsed                          completed minutes_elapsed 0
    the stop file exists                          completed stop_file      0

The late allowance (exact): a LATE bar (its minute was already released) is NOT a fault only when
the watcher handed it over with `how == "backfilled"`, a bar a re-requested backfill wrote after
a data loss (code 1101): the watcher documents that a minute it skipped may still be written by
such a backfill, and an outage makes the symbols' re-requests arrive at different times. It is
counted `late` and written to the live file, never fed. A late bar of any other kind (`later`,
`time`) means the clock or the stream is wrong (the aligner is polled with the stream clock, so
a correct session has none): a fault. A late bar at or before its symbol's decided point is always
a fault. A step slower than `STEP_SLOW_SECONDS` is stored as a warning of the run and said.

The `args` this driver reads (a namespace; the command builds it)
-----------------------------------------------------------------

`paper` (must be True), `host`, `port`, `client_id`, `connect_timeout` (as the watcher reads
them), `entries` (the algo config, as `validate_algo_config` takes it; every config needs
`"mode": "paper"`), `bars` (the folder of the month files), `warmup_days`, `capital` (Decimal
or text), `costs` (a `CostModel`), `fills` (absent or `"realistic"`), `out` (the live files
folder), `record` / `private` (the session recording: `record` None means no recording),
`minutes`, `run_out` (the working folder of the algos, created if missing), `stop_file` (a path
or None), `code_version` (text, or a callable; absent: `current_code_version()`), `quiet`.
`symbols` is set from the entries (the feed order).

Exit codes: the watcher's (0 ok, 1 symbol stopped, 2 refused, 3 could not connect, 4 stopped by a
lost or closed connection or an error, 5 not paper, 6 no data, 130 Ctrl-C) and 7, new: the
driver stopped the run on a data fault (delayed data, refused or late bars, a silent symbol, a
step that took too long). See the table above for which reason goes with which code.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TextIO

import pandas as pd

import MainStoinker.engine.market_calendar as calendar
import MainStoinker.ib.trading_hours as trading_hours
from MainStoinker.engine.algos import close_algo_logs
from MainStoinker.engine.cli import (
    CliError,
    build_engine,
    check_algos_importable,
    config_symbols,
    make_verifier,
    validate_algo_config,
)
from MainStoinker.engine.codeversion import current_code_version
from MainStoinker.engine.engine import Engine, LivePosition
from MainStoinker.engine.feed import COLUMNS, Bar, _uncovered_minutes
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.live_counts import (
    COUNT_DROPPED,
    COUNT_FED,
    COUNT_LATE,
    HEARTBEAT_INTERVAL,
    LIVE_KINDS,
)
from MainStoinker.engine.live_feed import LATE, LiveBarAligner
from MainStoinker.engine.live_store import CountSpec, FinishOutcome, LiveRunWriter, describe_today_bars
from MainStoinker.engine.modes import DEFAULT_MODE, ModeError, build_router
from MainStoinker.engine.paper_broker import CostModel
from MainStoinker.engine.run_documents import liveness
from MainStoinker.engine.stats import fmt4
from MainStoinker.engine.store import AlgoMeta, DataMeta, Store, StoreError
from MainStoinker.engine.verify import StoredBarsFeed
from MainStoinker.engine.warmup import live_warmup
from MainStoinker.ib.cli import PAPER_HOST, PAPER_PORT, _Say
from MainStoinker.ib.watch_bars import (
    DELAY_THRESHOLD,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_NO_DATA,
    EXIT_NOT_PAPER,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STOPPED,
    MAX_DRIVER_MINUTES,
    MAX_SYMBOLS,
    SESSION_GRACE,
    StreamClock,
    Watcher,
    WatchSeams,
    _in_session,
    _ny,
    _session_end,
    _session_start,
    run_watch,
)

EXIT_FAULT = 7  # the driver stopped the run on a data fault (module text)
DEFAULT_CLIENT_ID = 20  # the engine's client id (the watcher command uses 22)
RUN_KIND = LIVE_KINDS[0]  # "paper-live"
DATA_SOURCE = "ib-live"
FILLS = "realistic"
MODE = "paper"

STEP_SLOW_SECONDS = 20.0  # a step slower than this is recorded (the run's warnings, the output)
STEP_HARD_SECONDS = 60.0  # a step slower than this ends the run (`step_too_slow`)
STALE_BATCH_AGE = 300.0  # a batch released this long after its minute ended is history: not stepped
HEARTBEAT_EVERY = 20.0  # wall seconds without a write before the run is touched (< HEARTBEAT_INTERVAL)
SESSION_END_MARGIN = 60.0  # seconds after the close plus 20 min (16:20; 13:20 on a half day): ends anyway
LAST_BARS_MARGIN = 10.0  # seconds after 16:20 server time before the last two minutes may go by time
LAST_BARS_HOLD = 30.0  # seconds past 16:00 to which the clock given to the aligner is held until then
# (10 s short of the 40 s at which the aligner's 160 s deadline reaches the minute 15:58)
STREAM_WAIT_LIMIT = 900.0  # session seconds with `stream_now` unmeasured before `no_data`
# (a delayed opening or a halt of several minutes must not end a healthy session)
SILENT_LIMIT = 600.0  # session seconds a symbol may be without any bar or decision
STOP_FILE_EVERY = 1.0  # seconds (monotonic clock) between two looks for the stop file

assert HEARTBEAT_EVERY < HEARTBEAT_INTERVAL.total_seconds()

# watcher `DropCounts` field -> `live_counts.DROP_REASONS` identifier (previous_session is not a
# field of `DropCounts`, so it is never stored by this driver)
DROP_FIELDS: tuple[tuple[str, str], ...] = (
    ("snapshot_only", "snapshot_only"),
    ("connection_loss", "connection_lost"),
    ("farm_break", "farm_break"),
    ("stalled_stream", "stalled"),
    ("rerequest_gap", "rerequest_gap"),
)


class SessionRefused(Exception):
    """The options cannot start a session; the message is one line (exit 2, nothing built)."""


@dataclass(frozen=True)
class _Fault:
    reason: str  # a `store.END_REASONS` entry
    code: int
    message: str


@dataclass(frozen=True)
class _Plan:
    """What the options say, checked: nothing here has touched IB or the store."""

    entries: list[dict[str, Any]]
    symbols: list[str]
    bars_dir: Path
    warmup_days: int
    capital: Decimal
    costs: CostModel
    run_out: Path
    stop_file: Path | None
    minutes: int
    code_version: str | Callable[[], str] | None
    preflight_day: date
    preflight: tuple[dict[str, pd.DataFrame], dict[str, Any], list[str]]


def _decimal(value: Any, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise SessionRefused(f"{what} is not a number: {value!r}") from exc
    if not number.is_finite():
        raise SessionRefused(f"{what} is not a finite number: {value!r}")
    return number


def _plan(args: argparse.Namespace, today: date) -> _Plan:
    """Check the options (module text, step 1); `SessionRefused` for the first problem."""
    if getattr(args, "paper", False) is not True:
        raise SessionRefused("a live session is paper only: `paper` must be True")
    if not calendar.is_trading_day(today):  # this computer's clock in New York; IB's decides once connected
        raise SessionRefused(f"{trading_hours.not_a_trading_day_text(today)}; nothing was started")
    fills = getattr(args, "fills", FILLS)
    if fills != FILLS:
        raise SessionRefused(f"a live session uses realistic fills only (got {fills!r})")
    try:
        entries = validate_algo_config(getattr(args, "entries", None))
        check_algos_importable_quiet(entries)
    except (CliError, ModeError) as exc:
        raise SessionRefused(f"the algo config cannot be used: {exc}") from exc
    for entry in entries:
        for cfg in entry["data"]:
            mode = cfg.get("mode", DEFAULT_MODE)
            if mode != MODE:
                raise SessionRefused(
                    f"algo {ascii(cfg['idname'])}: a live session runs algos in mode {MODE!r} only "
                    f"(got {ascii(mode)}; set \"mode\": \"{MODE}\")")
    symbols = config_symbols(entries)
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise SessionRefused(f"the algos name {len(symbols)} symbols; a session takes 1 to {MAX_SYMBOLS}")
    given = getattr(args, "symbols", None)
    if given is not None and list(given) != symbols:
        raise SessionRefused(f"symbols {list(given)} differ from the algos' symbols {symbols}")
    bars = getattr(args, "bars", None)
    if bars is None or not Path(bars).is_dir():
        raise SessionRefused("`bars` must be the folder of the warm-up month files")
    days = getattr(args, "warmup_days", 2)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise SessionRefused(f"`warmup_days` must be a whole number from 1, got {days!r}")
    capital = _decimal(getattr(args, "capital", None), "`capital`")
    if capital <= 0:
        raise SessionRefused("`capital` must be above 0")
    costs = getattr(args, "costs", None)
    if not isinstance(costs, CostModel):
        raise SessionRefused("`costs` must be a CostModel")
    minutes = getattr(args, "minutes", None)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_DRIVER_MINUTES:
        raise SessionRefused(f"`minutes` must be 1 to {MAX_DRIVER_MINUTES}, got {minutes!r}")
    run_out = getattr(args, "run_out", None)
    live_out = getattr(args, "out", None)
    if run_out is None or live_out is None:
        raise SessionRefused("`out` (the live files) and `run_out` (the algos' folder) are needed")
    stop_file = getattr(args, "stop_file", None)
    if stop_file is not None and Path(stop_file).exists():
        raise SessionRefused("the stop file exists already: remove it before starting a session")
    try:
        frames, description, warnings = live_warmup(Path(bars), symbols, today, days)
    except (FileNotFoundError, ValueError, TypeError) as exc:
        raise SessionRefused(f"no warm-up can be built from the month files: {exc}") from exc
    return _Plan(entries, symbols, Path(bars), days, capital, costs, Path(run_out),
                 None if stop_file is None else Path(stop_file), minutes,
                 getattr(args, "code_version", None), today, (frames, description, warnings))


def check_algos_importable_quiet(entries: Sequence[Mapping[str, Any]]) -> None:
    """`check_algos_importable` with the algos' import-time prints hidden."""
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        check_algos_importable(entries)


# -- runs an earlier session left behind (D2b) ----------------------------------------------------


def close_abandoned_runs(store: Store, now: datetime, say: Callable[[str], None]) -> list[int]:
    """End every `paper-live` run that is still `running` with a stale last write, as `failed` /
    `abandoned`; return their ids. A running run that is not stale is left alone (and said).
    `now` must be timezone-aware."""
    closed: list[int] = []
    for summary in store.list_runs():
        if summary.kind != RUN_KIND or summary.status != "running":
            continue
        if liveness(summary, now) == "stale":
            store.end_run(summary.run_id, "failed", "abandoned")
            closed.append(summary.run_id)
            say(f"live: run {summary.run_id} was still marked running but its last write is stale: "
                "closed as failed (abandoned); its open positions stay listed as open")
        else:
            say(f"live: run {summary.run_id} is marked running and was written recently: left alone "
                "(another session may be running)")
    return closed


# -- the sink -------------------------------------------------------------------------------------


class _Sink:
    """The bar sink the watcher feeds after the live files (module text: the hand-over rule)."""

    def __init__(self, session: _Session) -> None:
        self._session = session
        self.written: dict[str, list[Path]] = {}  # this sink writes no file
        self.closes = 0

    def write(self, symbol: str, bar: Bar, how: str) -> bool:
        self._session.on_bar(symbol, bar, how)
        return True

    def close(self) -> None:
        self.closes += 1


# -- the session ----------------------------------------------------------------------------------


@dataclass
class _Counts:
    fed: dict[str, int]
    stale: dict[str, int]  # batches released too late to trade on (stored as late)
    overlap: dict[str, int]  # live bars not given on: not after the symbol's newest minute
    late_allowed: dict[str, int]  # late bars of a re-requested backfill
    late_other: dict[str, int]  # late bars of any other kind: a fault
    span: dict[str, int]  # stepped minutes from the symbol's first fed minute (missing = span - fed)

    @classmethod
    def new(cls, symbols: Sequence[str]) -> _Counts:
        return cls(*(dict.fromkeys(symbols, 0) for _ in range(6)))


@dataclass
class _Session:
    """The state of one session and the watcher's listener, pass hook and sink callbacks."""

    plan: _Plan
    store: Store
    clock: Callable[[], float]
    wall: Callable[[], datetime]
    say: Callable[[str], None]
    devnull: TextIO
    sleep: Callable[[float], None] | None = None  # the pause between the writer's busy retries
    aligner: LiveBarAligner = field(init=False)
    counts: _Counts = field(init=False)
    watcher: Watcher | None = None
    # what the watcher told us
    warm_today: dict[str, list[Bar]] = field(default_factory=dict)
    last_given: dict[str, int] = field(default_factory=dict)  # newest minute given on, per symbol
    alive_at: dict[str, float] = field(default_factory=dict)  # newest minute seen or decided
    decided_at: dict[str, float] = field(default_factory=dict)  # newest minute decided
    # the engine and the run
    engine: Engine | None = None
    writer: LiveRunWriter | None = None
    ledger: Ledger | None = None
    algos: list[Any] = field(default_factory=list)
    run_id: int | None = None
    engine_started: bool = False
    engine_dead: bool = False  # a step raised (or was interrupted): no more steps
    in_step: bool = False
    today: date | None = None
    description: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    files: tuple[tuple[str, str, int], ...] = ()
    stamps: list[pd.Timestamp] = field(default_factory=list)
    # outcome
    fault: _Fault | None = None
    fatal: BaseException | None = None
    stop_reason: str | None = None  # a clean stop the driver asked for
    refused_steps: list[str] = field(default_factory=list)
    slow_steps: list[tuple[str, float]] = field(default_factory=list)
    longest_step: float = 0.0
    last_stamp: datetime | None = None
    last_server_now: float | None = None
    unmeasured_since: float | None = None
    quiet_floor: float = -math.inf  # server time of the last pass during an outage
    started_server: float = -math.inf  # server time when the engine started
    last_stop_look: float = -math.inf
    watch_over: bool = False

    def __post_init__(self) -> None:
        self.aligner = LiveBarAligner(self.plan.symbols)
        self.counts = _Counts.new(self.plan.symbols)
        self.warm_today = {s: [] for s in self.plan.symbols}

    # -- the watcher's callbacks ------------------------------------------------------------

    def set_watcher(self, watcher: Watcher) -> None:
        self.watcher = watcher

    def _extra(self) -> Mapping[date, int | None] | None:
        """The market calendar's table of this run (IB's close for today when it differs)."""
        return getattr(self.watcher, "calendar_extra", None)

    def _session_end(self, day: date) -> int:
        """Epoch of the close of `day`'s regular session: 16:00, 13:00 on a half day."""
        return _session_end(day, self._extra())

    def decided(self, symbol: str, through_epoch: int) -> None:
        self.aligner.decided(symbol, through_epoch)
        self.alive_at[symbol] = max(self.alive_at.get(symbol, -math.inf), float(through_epoch))
        self.decided_at[symbol] = max(self.decided_at.get(symbol, -math.inf), float(through_epoch))

    def stopped(self, symbol: str) -> None:
        states = self.watcher.stream_states() if self.watcher is not None else {}
        state = states.get(symbol)
        if state is not None and state.running:
            self.watch_over = True  # the end of the watch tells every running symbol: not a stop
            return
        self.aligner.stopped(symbol)

    def on_bar(self, symbol: str, bar: Bar, how: str) -> None:
        self.alive_at[symbol] = max(self.alive_at.get(symbol, -math.inf), float(bar.time))
        if how == "backfill" and not self.engine_started:
            self.warm_today[symbol].append(bar)  # today's earlier bar: warm-up only (D1)
            self.last_given[symbol] = max(self.last_given.get(symbol, -1), int(bar.time))
            return
        last = self.last_given.get(symbol)
        if how == "backfill" or (last is not None and int(bar.time) <= last):
            self.counts.overlap[symbol] += 1
            self.say(f"live: {symbol} {_hm(bar.time)} is not after the symbol's newest minute "
                     f"({how}): not given on (overlap, counted)")
            return
        answer = self.aligner.bar(symbol, bar)
        self.last_given[symbol] = int(bar.time)
        if answer == LATE:
            if how == "backfilled":
                self.counts.late_allowed[symbol] += 1
                self.say(f"live: {symbol} {_hm(bar.time)} came late from a re-requested backfill: "
                         "in the live file, not fed (counted late)")
            else:
                self.counts.late_other[symbol] += 1

    # -- the pass hook ----------------------------------------------------------------------

    def on_pass(self, clock: StreamClock) -> None:
        """Called by the watcher after every pass; never raises (an error ends the session)."""
        try:
            self._pass(clock)
        except Exception as exc:  # noqa: BLE001 - the session ends through the exit path
            self._fatal(exc)

    def _fatal(self, exc: BaseException) -> None:
        if self.fatal is None:
            self.fatal = exc
            self.say(f"live: ERROR {type(exc).__name__}: {exc}")
        self._request_stop("error")

    def _request_stop(self, reason: str) -> None:
        if self.watcher is not None:
            self.watcher.request_stop(reason)

    def _stop_clean(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self.say(f"live: ending the session ({reason})")
        self._request_stop(reason)

    def _set_fault(self, reason: str, code: int, message: str) -> None:
        if self.fault is None:
            self.fault = _Fault(reason, code, message)
            self.say(f"live: FAULT ({reason}): {message}")
        self._request_stop(reason)

    def _pass(self, clock: StreamClock) -> None:
        watcher = self.watcher
        assert watcher is not None
        self.last_server_now = clock.server_now
        if watcher.stop_requested:
            return
        states = watcher.stream_states()
        dead = sorted(name for name, state in states.items() if not state.running)
        if dead:
            self._set_fault("symbol_stopped", EXIT_FAILED,
                            f"{', '.join(dead)} stopped (see the watcher's lines above); a run with a "
                            "symbol missing is ended, its positions could not be managed")
            return
        if self._stop_file_exists():
            self._stop_clean("stop_file")
            return
        if not self.engine_started:
            if not watcher.first_backfills_over():
                return
            self._start(clock)
            if self.fatal is not None or not self.engine_started:
                return
        if clock.stream_now is not None:
            self._poll(clock.stream_now, self._aligner_clock(clock.stream_now, clock.server_now))
        if self.fatal is not None or self.engine_dead:
            return
        self._check_faults(clock, states)
        self._heartbeat()
        self._check_session_end(clock, states)

    def _stop_file_exists(self) -> bool:
        stop_file = self.plan.stop_file
        if stop_file is None:
            return False
        now = self.clock()
        if now - self.last_stop_look < STOP_FILE_EVERY:
            return False
        self.last_stop_look = now
        return stop_file.exists()

    # -- starting the engine ----------------------------------------------------------------

    def _algo_env(self) -> contextlib.ExitStack:
        """Algos run in the run's folder with their prints hidden (as `backtest` does)."""
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.chdir(self.plan.run_out))
        stack.enter_context(contextlib.redirect_stdout(self.devnull))
        return stack

    def _start(self, clock: StreamClock) -> None:
        """First backfills are over: warm-up, engine, run, `start_live`. Any error is fatal."""
        try:
            self._start_engine(clock)
        except Exception as exc:  # noqa: BLE001
            self._fatal(exc)

    def _start_engine(self, clock: StreamClock) -> None:
        plan = self.plan
        today = _ny(clock.server_now).date()
        if today == plan.preflight_day:
            frames, description, warnings = plan.preflight
        else:  # the clock of this computer and IB's disagree about the date
            frames, description, warnings = live_warmup(plan.bars_dir, plan.symbols, today,
                                                        plan.warmup_days)
        frames = {s: f.copy() for s, f in frames.items()}
        description = dict(description)
        for symbol in plan.symbols:
            bars = sorted(self.warm_today[symbol], key=lambda b: b.time)
            if bars:
                frames[symbol] = pd.concat([frames[symbol], _frame_of(bars)],
                                           ignore_index=True)[list(COLUMNS)]
        # today's earlier bars are part of the algos' history but not of the month files: a later
        # check of the run (`report --verify`) rebuilds them from here, and checks their digest
        added, digests = describe_today_bars(self.warm_today)
        description["today_bars"] = added
        description["today_sha256"] = digests
        self.today = today
        self.started_server = clock.server_now
        self.description = description
        self.warnings = list(warnings)
        self.files = tuple(
            (s, str(description["symbols"][s]["data_snapshot_sha256"]),
             int(description["symbols"][s]["data_snapshot_size"])) for s in plan.symbols)
        plan.run_out.mkdir(parents=True, exist_ok=True)
        ledger = Ledger()
        self.ledger = ledger
        router = build_router(plan.entries, fills=FILLS, costs=plan.costs, capital=plan.capital,
                              ledger=ledger)
        bad = [i for i, mode in router.modes.items() if mode != MODE]
        if bad:
            raise SessionRefused(f"algo(s) {bad} are not in mode {MODE!r}")
        with self._algo_env():
            self.engine = build_engine(plan.entries, StoredBarsFeed(frames, []), router, ledger,
                                       self.algos)
        self.writer = LiveRunWriter(self.store, ledger, self.engine.algos,
                                    **({} if self.sleep is None else {"sleep": self.sleep}))
        metas = [AlgoMeta(cfg["idname"], entry["ID"], cfg["ticker"], router.modes[cfg["idname"]],
                          plan.capital, cfg) for entry in plan.entries for cfg in entry["data"]]
        version = plan.code_version
        code_version = version() if callable(version) else (
            version if isinstance(version, str) else current_code_version())
        config = {
            "entries": plan.entries,
            "warmup_days": plan.warmup_days,
            "capital": fmt4(plan.capital),
            "costs": {
                "commission_per_share": fmt4(plan.costs.commission_per_share),
                "commission_minimum": fmt4(plan.costs.commission_minimum),
                "slippage_per_share": fmt4(plan.costs.slippage_per_share),
            },
            "live": {"minutes": plan.minutes, "backfill": "since_open"},
        }
        self.run_id = self.writer.begin(
            fills=FILLS, code_version=code_version, config=config,
            data=DataMeta(source=DATA_SOURCE, directory=str(plan.bars_dir),
                          symbols=tuple(plan.symbols), files=self.files,
                          warnings=tuple(self.warnings)),
            algos=metas, warmup=frames, warmup_description=description)
        self.last_stamp = self.wall()
        self.engine.start_live(frames, on_step=self.writer.on_step)
        self.engine_started = True
        held = {s: len(self.warm_today[s]) for s in plan.symbols}
        self.say(f"live: run {self.run_id} begun ({RUN_KIND}, {FILLS} fills); warm-up "
                 f"{plan.warmup_days} day(s) from the month files plus today's closed backfill bars "
                 f"{held}; stepping from the next closed bar")

    # -- polling and stepping ---------------------------------------------------------------

    def _aligner_clock(self, stream_now: float, server_now: float) -> float:
        """The clock to poll the aligner with: `stream_now`, except that the last two minutes of the
        session are not released by time before the watcher is done with them (issue 271).

        The watcher holds the last two bars of a session until 16:20 server time (D7), far past the
        aligner's deadline of 100 s after a minute's end. Polled with the plain clock, the aligner
        released 15:59 as soon as the first symbol's bar arrived (after a quiet stretch its clock
        is clamped to 180 s past the newest bar, which is already past that deadline) and the other
        symbol's bar, closed a moment later in the same instant, came late. Until
        `LAST_BARS_MARGIN` s after the watcher's own deferral ends the clock is held where those
        minutes are not yet due; they are released at once when every symbol has decided them, or by
        time after that. Every other minute is polled as before."""
        if self.today is None:
            return stream_now
        end = float(self._session_end(self.today))
        if server_now < end + SESSION_GRACE + LAST_BARS_MARGIN:
            return min(stream_now, end + LAST_BARS_HOLD)
        return stream_now

    def _poll(self, stream_now: float, aligner_clock: float) -> None:
        try:
            batches = self.aligner.poll(aligner_clock)
        except ValueError as exc:  # a clock that cannot be seconds: loud, never skipped
            self._fatal(exc)
            return
        self._feed(batches, stream_now)

    def _feed(self, batches: Sequence[tuple[pd.Timestamp, dict[str, Bar]]], stream_now: float) -> None:
        for stamp, bars in batches:
            if self.fatal is not None or self.engine_dead:
                return
            self._step(stamp, bars, stream_now)

    def _step(self, stamp: pd.Timestamp, bars: dict[str, Bar], stream_now: float) -> None:
        assert self.engine is not None and self.writer is not None
        minute = int(next(iter(bars.values())).time)
        # the last two bars of a session arrive at about 16:20 by design (D7): they are not history
        by_design = minute >= self._session_end(_ny(minute).date()) - 120
        if not by_design and stream_now - (minute + 60) > STALE_BATCH_AGE:
            for symbol in bars:
                self.counts.stale[symbol] += 1
            self.say(f"live: batch {_hm(minute)} released {stream_now - minute - 60:.0f} s after its "
                     f"minute ended (more than {STALE_BATCH_AGE:.0f} s): history, not stepped "
                     f"({', '.join(bars)}; counted late)")
            return
        started = self.clock()
        self.writer.fed(stamp, bars)
        self.in_step = True
        try:
            with self._algo_env():
                self.engine.step(stamp, bars)
        except ValueError as exc:
            self.in_step = False
            if self._engine_failed(stamp):
                self.engine_dead = True
                self._fatal(exc)
                return
            self.refused_steps.append(f"{_hm(minute)}: {exc}")
            self.say(f"live: the engine refused the batch {_hm(minute)} ({exc}); counted, the "
                     "session goes on")
            return
        except Exception as exc:  # noqa: BLE001 - the engine is failed: end through the exit path
            self.in_step = False
            self.engine_dead = True
            self._fatal(exc)
            return
        self.in_step = False  # a KeyboardInterrupt leaves it True: the engine is failed
        elapsed = self.clock() - started
        self.longest_step = max(self.longest_step, elapsed)
        self.last_stamp = self.wall()
        self.stamps.append(stamp)
        for symbol in self.plan.symbols:
            if symbol in bars:
                self.counts.fed[symbol] += 1
            if self.counts.fed[symbol]:  # a minute the symbol lacks only counts after its first
                self.counts.span[symbol] += 1
        if elapsed > STEP_SLOW_SECONDS:
            self.slow_steps.append((_hm(minute), elapsed))
            self.say(f"live: the step of {_hm(minute)} took {elapsed:.1f} s (more than "
                     f"{STEP_SLOW_SECONDS:g} s)")
        if elapsed > STEP_HARD_SECONDS:
            self._set_fault("step_too_slow", EXIT_FAULT,
                            f"the step of {_hm(minute)} took {elapsed:.1f} s (limit "
                            f"{STEP_HARD_SECONDS:g} s): the engine cannot keep up with the stream")

    def _engine_failed(self, stamp: pd.Timestamp) -> bool:
        """After a `ValueError` from `step`: True when the engine is failed (an empty step is a
        no-op on a running engine and `RuntimeError` on a failed one)."""
        assert self.engine is not None
        try:
            self.engine.step(stamp, {})
        except RuntimeError:
            return True
        return False

    # -- faults, heartbeat, the end -----------------------------------------------------------

    def _check_faults(self, clock: StreamClock, states: Mapping[str, Any]) -> None:
        watcher = self.watcher
        assert watcher is not None
        delayed = sorted(name for name, state in states.items() if state.running and state.delayed)
        if delayed:
            self._set_fault("delayed_data", EXIT_FAULT,
                            f"{', '.join(delayed)}: the stream is labelled delayed; the first "
                            "sessions need real-time data (is the paper account sharing the live "
                            "account's market data?)")
            return
        refused = {s: n for s, n in self.aligner.refused_counts().items() if n}
        if refused:
            self._set_fault("data_fault", EXIT_FAULT, f"the aligner refused bars: {refused}")
            return
        behind = {s: n for s, n in self.aligner.late_after_decided_counts().items() if n}
        if behind:
            self._set_fault("data_fault", EXIT_FAULT,
                            f"bars came late after their symbol was decided: {behind}")
            return
        other = {s: n for s, n in self.counts.late_other.items() if n}
        if other:
            self._set_fault("data_fault", EXIT_FAULT,
                            f"late bars that no re-requested backfill explains: {other} (the "
                            f"aligner's clamp rose {self.aligner.clock_clamped_count()} time(s))")
            return
        now = clock.server_now
        if watcher.is_down():
            self.quiet_floor = now
            self.unmeasured_since = None
        if not _in_session(now, self._extra()):
            self.unmeasured_since = None
            return
        if clock.stream_now is None:
            if self.unmeasured_since is None:
                self.unmeasured_since = max(now, float(_session_start(_ny(now).date())))
            elif now - self.unmeasured_since > STREAM_WAIT_LIMIT:
                self._set_fault("no_data", EXIT_NO_DATA,
                                f"no lag measured for a symbol for {STREAM_WAIT_LIMIT:g} s of the "
                                "session: a symbol shows no bar (a delayed or unsubscribed stream?)")
                return
        else:
            self.unmeasured_since = None
        opened = float(_session_start(_ny(now).date()))
        for symbol in self.plan.symbols:
            reference = max(self.alive_at.get(symbol, -math.inf) + 60, opened, self.quiet_floor,
                            self.started_server)
            if now - reference > SILENT_LIMIT:
                lag = states[symbol].lag_bound
                if lag is not None and lag > DELAY_THRESHOLD:  # silent AND its bars are behind: delayed
                    self._set_fault("delayed_data", EXIT_FAULT,
                                    f"{symbol}: no bar for {now - reference:.0f} s and its newest bar "
                                    f"is at least {lag:.0f} s behind IB's clock: delayed data")
                else:
                    self._set_fault("data_fault", EXIT_FAULT,
                                    f"{symbol} has shown no bar and no decision for "
                                    f"{now - reference:.0f} s of the session (limit {SILENT_LIMIT:g} s)")
                return

    def _heartbeat(self) -> None:
        assert self.writer is not None
        now = self.wall()
        if self.last_stamp is None or (now - self.last_stamp).total_seconds() >= HEARTBEAT_EVERY:
            # the counts and the data record as they stand: provisional, `finish` writes the final ones
            self.writer.heartbeat(_counts_of(self),
                                  _data_meta(self, self.engine) if self.engine is not None else None)
            self.last_stamp = now

    def _check_session_end(self, clock: StreamClock, states: Mapping[str, Any]) -> None:
        if self.today is None:
            return
        end = self._session_end(self.today)
        running = [name for name, state in states.items() if state.running]
        last_minute = end - 60
        if running and all(self.decided_at.get(s, -math.inf) >= last_minute for s in running):
            self._stop_clean("session_end")
        elif clock.server_now >= end + SESSION_GRACE + SESSION_END_MARGIN:
            self._stop_clean("session_end")

    # -- the end ----------------------------------------------------------------------------

    def flush(self) -> None:
        """One last poll of what is decided (module text, step 6), unless the engine is dead."""
        watcher = self.watcher
        if (not self.engine_started or self.fatal is not None or self.engine_dead or self.in_step
                or watcher is None or watcher.server0 is None):
            return
        try:
            clock = watcher.stream_clock()
            if clock.stream_now is None:
                return
            batches = self.aligner.poll(self._aligner_clock(clock.stream_now, clock.server_now))
        except ValueError as exc:
            self._fatal(exc)
            return
        self._feed(batches, clock.stream_now)

    def pending(self) -> int:
        return self.aligner.pending_minutes()


def _hm(epoch: float) -> str:
    return _ny(epoch).strftime("%H:%M")


def _frame_of(bars: Sequence[Bar]) -> pd.DataFrame:
    """Bars as a frame shaped like the month files' (`COLUMNS`, the same dtypes)."""
    return pd.DataFrame({
        "date": pd.to_datetime([b.date for b in bars]),
        "time": [int(b.time) for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": [float(b.close) for b in bars],
        "volume": [int(b.volume) for b in bars],
    }).astype({"time": "int64", "volume": "int64", "open": "float64", "high": "float64",
               "low": "float64", "close": "float64"})[list(COLUMNS)]


# -- the end of a session -------------------------------------------------------------------------


def _outcome(session: _Session, watcher_code: int) -> tuple[str, str | None, int]:
    """`(status, end_reason, exit code)` of the session (module text: the table of faults)."""
    watcher = session.watcher
    cause = watcher.end_cause if watcher is not None else ""
    if watcher_code == EXIT_NOT_PAPER:
        return "failed", "not_paper", EXIT_NOT_PAPER
    if watcher is None:  # never connected, or refused before the watcher: its code stands
        return "failed", "error", watcher_code
    if cause == "not_trading_day":  # IB's date or hours say the market is closed: nothing was requested
        return "failed", "error", EXIT_REFUSED
    if session.fault is not None:
        return "failed", session.fault.reason, session.fault.code
    if session.fatal is not None:
        return "failed", "error", EXIT_STOPPED
    if watcher_code == EXIT_INTERRUPTED:
        return "failed", "interrupted", EXIT_INTERRUPTED
    if cause == "stop_requested" and session.stop_reason is not None:
        return "completed", session.stop_reason, EXIT_OK
    if cause in ("closed", "lost"):
        return "failed", "connection_closed", EXIT_STOPPED
    if cause == "deadline":
        if watcher_code == EXIT_OK:
            return "completed", "minutes_elapsed", EXIT_OK
        if watcher_code == EXIT_NO_DATA:
            return "failed", "no_data", EXIT_NO_DATA
        return "failed", "symbol_stopped", EXIT_FAILED
    if cause == "idle":
        return "failed", "symbol_stopped", EXIT_FAILED
    return "failed", "error", EXIT_STOPPED


def _counts_of(session: _Session) -> dict[CountSpec, int]:
    """The run's counts, keys for `live_counts.count_key`: fed and late of every symbol and the
    watcher's drops (only reasons that happened)."""
    counts: dict[CountSpec, int] = {}
    late = session.aligner.late_counts()
    late_decided = session.aligner.late_after_decided_counts()
    for symbol in session.plan.symbols:
        counts[(symbol, COUNT_FED, None)] = session.counts.fed[symbol]
        counts[(symbol, COUNT_LATE, None)] = (late[symbol] + late_decided[symbol]
                                              + session.counts.stale[symbol])
    if session.watcher is not None:
        for symbol, drops in session.watcher.drop_counts().items():
            for attribute, reason in DROP_FIELDS:
                value = getattr(drops, attribute)
                if value:
                    counts[(symbol, COUNT_DROPPED, reason)] = int(value)
    return counts


def _data_meta(session: _Session, engine: Engine) -> DataMeta:
    """The full `DataMeta` of the run at its end, from the bars actually fed."""
    plan = session.plan
    stamps = session.stamps
    fed = session.counts.fed
    warnings = list(session.warnings)
    if session.slow_steps:
        warnings.append(f"{len(session.slow_steps)} step(s) took more than {STEP_SLOW_SECONDS:g} s; "
                        f"the longest {session.longest_step:.1f} s")
    if session.refused_steps:
        warnings.append(f"{len(session.refused_steps)} batch(es) refused by the engine, not stepped "
                        f"(first {session.refused_steps[0]})")
    if any(session.counts.overlap.values()):
        warnings.append(f"live bars not given on (overlap): {_nonzero(session.counts.overlap)}")
    return DataMeta(
        source=DATA_SOURCE, directory=str(plan.bars_dir), symbols=tuple(plan.symbols),
        first_bar=stamps[0].to_pydatetime() if stamps else None,
        last_bar=stamps[-1].to_pydatetime() if stamps else None,
        files=session.files, replayed_minutes=len(stamps),
        missing_minutes=tuple((s, session.counts.span[s] - fed[s]) for s in plan.symbols),
        uncovered_minutes=_uncovered_minutes(stamps), warnings=tuple(warnings),
        gap_rule=engine.gap_report)


def _nonzero(counts: Mapping[str, int]) -> dict[str, int]:
    return {s: n for s, n in counts.items() if n}


def _describe_position(position: LivePosition) -> str:
    since = "?" if position.entry_time is None else position.entry_time.strftime("%H:%M")
    levels = ", ".join(f"{name} {value}" for name, value in
                       (("stop", position.stop), ("target", position.target)) if value is not None)
    extra = f", {levels}" if levels else ""
    return (f"  OPEN POSITION (left open): {position.account} {position.symbol} {position.side} "
            f"{position.quantity} at {position.entry_price} since {since}{extra}")


def _stored_end(session: _Session, status: str, reason: str | None) -> FinishOutcome | None:
    """What the database holds for this session's run when it is no longer `running` (the end was
    committed, then `writer.finish` was interrupted before it returned), else None. If it is not
    what the session asked for, the outcome carries an error line, so the last line says so."""
    try:
        for summary in session.store.list_runs():
            if summary.run_id == session.run_id and summary.status != "running":
                errors: tuple[str, ...] = ()
                if (summary.status, summary.end_reason) != (status, reason):
                    errors = ("the end of the run was stored, but Ctrl-C came before the rest of it "
                              "was written",)
                return FinishOutcome(summary.status, summary.end_reason, errors)
    except Exception:  # noqa: BLE001 - unknown: the caller finishes the run again
        return None
    return None


def _finish_run(session: _Session, status: str, reason: str | None, say: Callable[[str], None],
                shield: _Shield) -> tuple[FinishOutcome | None, tuple[LivePosition, ...]]:
    """Flush, `end_live`, `writer.finish`, each shielded from Ctrl-C. Returns (the writer's
    outcome, open positions); the outcome is None when no run was begun."""
    positions: tuple[LivePosition, ...] = ()
    engine, writer = session.engine, session.writer
    shield.call(session.flush)
    if engine is not None and session.engine_started:
        entered = False

        def end_live() -> None:
            nonlocal positions, entered
            if entered:  # a retry after an interrupt: the engine has ended, never end it twice
                say("live: the end-of-session step may be incomplete: Ctrl-C reached it, so pending "
                    "entries may not have been cancelled and requested exits may not have been "
                    "filled; the open positions are read from the ledger")
                if session.ledger is not None:
                    positions = tuple(
                        LivePosition(p.account, p.broker_ref, p.symbol, p.side, p.quantity,
                                     p.average_price, None, None, None)
                        for p in session.ledger.open_positions())
                return
            entered = True
            try:
                with session._algo_env():  # noqa: SLF001
                    positions = engine.end_live()
            except Exception as exc:  # noqa: BLE001
                say(f"live: end_live raised {type(exc).__name__}: {exc}")

        shield.call(end_live)
    if writer is None or engine is None or session.run_id is None:
        return None, positions
    attempted = False

    def finish() -> FinishOutcome:
        nonlocal attempted
        if attempted:  # a retry: if the end of the run was committed, only its bookkeeping is missing
            ended = _stored_end(session, status, reason)
            if ended is not None:
                return ended
        attempted = True
        return writer.finish(status, reason, _counts_of(session),  # type: ignore[arg-type]
                             _data_meta(session, engine), engine.gap_report)

    outcome = shield.call(finish)
    if outcome is None:  # interrupted again and again: the run may still be marked running
        say("live: the run could not be ended: it may still be marked running")
        return FinishOutcome(None, None, ("the end of the run was interrupted again and again",)), \
            positions
    for line in outcome.errors:
        say(f"live: the run's record: {line}")
    return outcome, positions


def _stored_status_text(outcome: FinishOutcome | None, status: str, reason: str | None) -> str:
    """What the database says about the run after the session, for the last output line.

    The session's own verdict (`status`, `reason`) is what it meant to store; this is what was
    stored: the same when the writer closed the run cleanly, otherwise the truth.
    """
    if outcome is None or outcome.ok:
        return f"{status}, end reason {reason}"
    if outcome.status is None:
        why = "the database stayed busy" if outcome.busy else "the store refused the end"
        return (f"the run could not be closed: still marked running in the database ({why}); "
                f"the session meant {status}, end reason {reason}")
    return (f"stored as {outcome.status}, end reason {outcome.reason} (the session meant "
            f"{status}, end reason {reason}; the run's record has errors)")


def _verify(session: _Session, store: Store, say: Callable[[str], None]) -> None:
    """The end-of-session check of the stored run (`report RUN --verify`), printed."""
    plan = session.plan
    if session.run_id is None:
        return
    if not session.stamps:
        say("live: verify: no bar was fed, nothing to verify")
        return
    try:
        run = store.load_run(session.run_id)
        result = make_verifier(str(plan.bars_dir))(store, run)
    except Exception as exc:  # noqa: BLE001 - the session has ended; say, never raise
        say(f"live: verify: could not run: {type(exc).__name__}: {exc}")
        return
    for line in result.lines():
        say(f"live: {line}")
    if result.outcome == "unverifiable":
        say("live: ALARM: the run is UNVERIFIABLE: an alarm about the RECORD (what was stored), "
            "not about the engine")
    elif result.outcome == "mismatch":
        say("live: ALARM: the replay of the stored run DIFFERS: an alarm about the ENGINE")


def run_live_session(
    args: argparse.Namespace,
    *,
    connection_factory: Callable[[Any, list[Any]], Any],
    market_data_factory: Callable[[Any], Any],
    recorder_factory: Callable[[Any], Any],
    store: Store,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime],
    out: TextIO,
) -> int:
    """Run one live paper session (module text); returns the exit code.

    `store` is open and stays open (the caller closes it); `clock` is monotonic seconds, `sleep` an
    interruptible sleep, `wall` this computer's clock as an aware datetime. The factories are
    those of `watch_bars.run_watch`.
    """
    say = _Say(out)
    try:
        today = _ny(wall().timestamp()).date()
        plan = _plan(args, today)
    except SessionRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    try:
        close_abandoned_runs(store, wall(), say)
        # the command sets this: stale runs are closed above, so a run still marked running now was
        # written recently and may be another live session: two sessions must not run at once
        if getattr(args, "refuse_if_running", False) is True:
            running = [s.run_id for s in store.list_runs()
                       if s.kind == RUN_KIND and s.status == "running"]
            if running:
                say(f"Refused: another {RUN_KIND} run is marked running and was written recently "
                    f"(run {', '.join(str(r) for r in running)}): wait until it ends or its last write "
                    "is stale")
                return EXIT_REFUSED
    except (StoreError, ValueError) as exc:
        say(f"Refused: the run store cannot be read or written: {exc}")
        return EXIT_REFUSED
    args.symbols = list(plan.symbols)
    if not hasattr(args, "quiet"):
        args.quiet = True
    for name, default in (("host", PAPER_HOST), ("port", PAPER_PORT), ("client_id", DEFAULT_CLIENT_ID),
                          ("connect_timeout", 30.0), ("record", None), ("private", ())):
        if not hasattr(args, name):
            setattr(args, name, default)
    args.minutes = plan.minutes
    with open(os.devnull, "w") as devnull:
        session = _Session(plan, store, clock, wall, say, devnull, sleep)
        seams = WatchSeams(extra_sinks=(_Sink(session),), listener=session,
                           on_pass=session.on_pass, minutes=plan.minutes,
                           backfill_since_open=True, on_watcher=session.set_watcher)
        code = EXIT_STOPPED
        try:
            code = run_watch(args, connection_factory=connection_factory,
                             market_data_factory=market_data_factory,
                             recorder_factory=recorder_factory, out=out, clock=clock,
                             sleep=sleep, wall=wall, seams=seams)
        finally:  # every path: the watcher returned, or something it did not catch got out
            result = _end(session, code, store, say)
        return result


class _Shield:
    """Ctrl-C does not interrupt the end of a session: the run must be ended in the store first.

    While active, SIGINT only counts (when this is the main thread), and a `KeyboardInterrupt`
    that still gets out of a step (a signal handler that was not ours, a test) is counted and the
    step is run again. Not every step can simply be repeated, so the steps handle a retry
    themselves (`_finish_run`): `end_live` is never called twice (the open positions are then read
    from the ledger and the output says the step may be incomplete), and `writer.finish` is not
    repeated once the run is stored as ended. The count decides the exit code afterwards: 130."""

    ATTEMPTS = 5

    def __init__(self, say: Callable[[str], None]) -> None:
        self._say = say
        self.hits = 0
        self._previous: Any = None
        self._installed = False

    def _on_signal(self, signum: int, frame: Any) -> None:
        self.hits += 1

    def __enter__(self) -> _Shield:
        try:
            self._previous = signal.signal(signal.SIGINT, self._on_signal)
            self._installed = True
        except (ValueError, OSError):  # not the main thread: the retry below is what shields
            self._installed = False
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._installed:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGINT, self._previous)

    def call(self, step: Callable[[], Any], default: Any = None) -> Any:
        for _ in range(self.ATTEMPTS):
            try:
                return step()
            except KeyboardInterrupt:
                self.hits += 1
                self._say("live: Ctrl-C while the run is being ended: the run is finished first, "
                          "then the session exits with 130")
        self._say("live: Ctrl-C again and again: a step of the end could not be completed")
        return default


def _end(session: _Session, watcher_code: int, store: Store, say: Callable[[str], None]) -> int:
    """Everything after the watcher returned (module text, step 6); returns the exit code.

    The whole of it is shielded from Ctrl-C (`_Shield`); an interrupt that came during it is
    answered with exit 130 once the run is ended."""
    with _Shield(say) as shield:
        code = _end_run(session, watcher_code, store, say, shield)
    return EXIT_INTERRUPTED if shield.hits else code


def _end_run(session: _Session, watcher_code: int, store: Store, say: Callable[[str], None],
             shield: _Shield) -> int:
    status, reason, code = _outcome(session, watcher_code)
    if session.run_id is None:
        shield.call(lambda: close_algo_logs(session.algos))
        say(f"live: no run was stored ({reason}): nothing was fed")
        return code
    outcome, positions = _finish_run(session, status, reason, say, shield)
    shield.call(lambda: close_algo_logs(session.algos))
    if shield.hits:
        code = EXIT_INTERRUPTED
    elif outcome is None or not outcome.ok:
        code = code if status == "failed" else EXIT_STOPPED
    say(f"live: run {session.run_id} {status} ({reason}): {sum(session.counts.fed.values())} bar(s) "
        f"fed in {len(session.stamps)} minute(s); fed {_nonzero(session.counts.fed)}, late "
        f"{_nonzero(session.aligner.late_counts())}, not stepped (too old) "
        f"{_nonzero(session.counts.stale)}, overlap {_nonzero(session.counts.overlap)}, engine "
        f"refused {len(session.refused_steps)}, longest step {session.longest_step:.1f} s")
    pending = session.pending()
    if pending:
        say(f"live: {pending} minute(s) held in the aligner at the end were not decided and are "
            "not fed (their bars are in the live files)")
    if positions:
        say(f"live: {len(positions)} position(s) left OPEN at the end of the run (listed, never "
            "closed by the session):")
        for position in positions:
            say(_describe_position(position))
    else:
        say("live: no position left open")
    shield.call(lambda: _verify(session, store, say))  # read-only: the run is ended already
    if shield.hits:
        code = EXIT_INTERRUPTED
    say(f"Live session: {_stored_status_text(outcome, status, reason)} (exit {code})")
    return code


__all__ = [
    "DROP_FIELDS", "EXIT_FAULT", "SessionRefused", "close_abandoned_runs", "run_live_session",
]
