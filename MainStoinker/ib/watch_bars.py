"""Owner-run live bar watcher: subscribe to IB's live-updating 1-minute bars and save the closed ones.

    python -m MainStoinker.ib.watch_bars --port 7496 --symbols AAPL,MSFT,TSLA --minutes 30

Only the owner runs it, in a real terminal, with TWS or IB Gateway open. It runs no algo and
places no trade: it is built on `IbConnection` and `IbMarketData` (a data-only client that
cannot send anything but data requests) and is the first observation step before paper
trading on live bars. Agents never run it.

How IB's live bars arrive. One `reqHistoricalData` per symbol with `keepUpToDate=True` (empty
end time, `formatDate=2`, 1-minute regular-hours bars): IB sends the backfill bars for the
requested duration, then the end marker, then `historicalDataUpdate` callbacks. Each update
carries the CURRENT state of the bar still forming (the same timestamp repeated with changing
high, low, close and volume); when a new minute starts the updates carry the new timestamp.
A bar is therefore known to be closed only when an update with a LATER timestamp arrives:
the previous update is then final and is emitted once. The forming bar is never emitted as
closed and never written. An update with an EARLIER timestamp than the forming bar (or one
already emitted) is a late revision: counted, reported, and nothing already emitted is
rewritten. A bar that no later update has closed 90 s after its minute ended is closed by
time (the end of the session has no later update) and marked so.

Offsets (`first seen +S s after the minute started`, `closed +S s after the minute ended`)
are IB's server time at start (1 s resolution) plus the elapsed monotonic clock, against the
bar's New York minute. The loop reads each symbol in turn for at most 0.25 s in all, so an
offset carries up to about 0.25 s of reading delay.

What this run is meant to show (nothing here is assumed): how often updates arrive, how long
after the minute boundary the first update of the next bar arrives, whether the last update
of a bar equals the bar a later history fetch returns, and what the final bar of the session
looks like at 16:00.

Files: closed bars only, `<out>/<SYMBOL>/<SYMBOL>_live_<YYYY-MM-DD>.csv` (New York date),
header `date,time,open,high,low,close,volume`, the month-file columns and formats, appended
and flushed per bar. A second run on the same day appends only bars later than the file's
last row. These files are deliberately not in the month-file layout and not in the bars
folder, so fetched history and live observation never mix.

Checks, in this sequence, before any client or connection is built: arguments; default paths
(from `USERPROFILE`, else `HOME`, in the environment mapping given to `main`, never the
machine's idea of "home"); output and recording paths (not in the repository or a OneDrive
folder, not a file or under one); the port guard (`check_broker_port`: paper ports always, a
live port only if the OWNER set the opt-in variable named in the refusal text, in their own
terminal; this module never sets it); the folders are created; the console gate (an interactive terminal; the owner
types the port number). The environment is read in `main` only. Every printed line passes
`scrub` and `ManagedAccounts` is never printed.

The session recording (on by default, `watch-bars-<date>-<n>.jsonl`) is written by
`SessionRecorder` and checked with `scan_recording` afterwards. Updates arrive every few
seconds per symbol, so a long watch writes a large recording and the recorder may slow the
reading down; use `--no-record` to avoid that. It is not optimised.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.engine.feed import Bar
from MainStoinker.ib import events
from MainStoinker.ib.bars import BadBar, bar_from_ib, data_time
from MainStoinker.ib.cli import (
    PathRefused,
    check_folder_usable,
    check_path,
    home_folder,
    make_folders,
    port_refusal_text,
    private_values,
    remove_empty,
    scrub,
)
from MainStoinker.ib.connection import (
    MAX_CLIENT_ID,
    REFUSED_CLIENT_IDS,
    BrokerConnectionRefusedHere,
    ClientIdInUse,
    ClientIdRefused,
    ConnectFailed,
    ConnState,
    IbConnection,
    NotConnected,
)
from MainStoinker.ib.history import DATA_ZONE, check_symbol
from MainStoinker.ib.market_data import (
    CLOSED,
    DATA_LOST,
    END,
    ERROR,
    DataLost,
    IbMarketData,
    Outcome,
    ResolveFailed,
)
from MainStoinker.ib.recorder import SessionRecorder, scan_recording
from MainStoinker.Util.broker_guard import BrokerPortRefused, check_broker_port

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_NO_CONNECTION = 3
EXIT_STOPPED = 4
EXIT_INTERRUPTED = 130

EXIT_CODES_TEXT = """\
exit codes:
  0    watched to the end and every symbol was streaming
  1    finished, but a symbol stopped (IB error, unknown symbol) or delivered no bars
  2    refused: console gate, port guard, bad arguments, unusable path
  3    could not connect to TWS or IB Gateway
  4    stopped by a lost or closed connection, or by an unexpected error
  130  interrupted with Ctrl-C (requests cancelled, connection and recording closed)
"""

DEFAULT_CLIENT_ID = 22
DEFAULT_CONNECT_TIMEOUT = 30.0
MAX_CONNECT_TIMEOUT = 300.0
MAX_SYMBOLS = 5
DEFAULT_MINUTES = 30
MAX_MINUTES = 420
DEFAULT_BACKFILL_MINUTES = 10
MAX_BACKFILL_MINUTES = 120
REQUEST_SPACING = 1.0  # seconds between two history requests
IDENTICAL_REQUEST_SPACING = 15.0  # IB refuses an identical history request within 15 s
MAX_REREQUESTS = 10  # per symbol; more means something is wrong, not a passing outage
IDLE_SLEEP = 0.05
POLL_BUDGET = 0.25  # seconds one pass over all symbols may wait for events
MIN_POLL_SLICE = 0.02
FORMING_EVERY = 10.0  # seconds between two forming-bar lines of one symbol
CLOSE_GRACE = 2  # a backfill bar this soon after its minute ended may still be forming
SETTLE_SECONDS = 90  # a bar with no later update this long after its minute is closed by time
BACKFILL_TIMEOUT = 90.0
SERVER_TIME_TIMEOUT = 10.0
RESOLVE_TIMEOUT = 10.0
FARM_TIMEOUT = 30.0
MAX_MISSING_SHOWN = 30
SHOWN_FINDINGS = 5
HEADER = "date,time,open,high,low,close,volume"
NY = ZoneInfo(DATA_ZONE)
PATHS_NOTE = "Note: this output shows local paths. Read it before sharing it."
COMPARE_NOTE = ("For comparison: run fetch-bars for today after 19:00 New York and compare "
                "with the file(s) above.")
OFFSET_NOTE = ("Offsets use IB's server time at the start (1 s resolution) plus this computer's "
               "monotonic clock; each has up to about 0.25 s of reading delay.")

ConnectionFactory = Callable[[Any, list[Any]], IbConnection]
MarketDataFactory = Callable[[IbConnection], IbMarketData]
RecorderFactory = Callable[[Any], SessionRecorder | None]


class LiveFileError(RuntimeError):
    """A live file cannot be read or appended to (the message says why)."""


class _Closed(Exception):
    """The connection closed: stop and summarise."""


class _Say:
    """Prints scrubbed lines; nothing reaches the stream unscrubbed."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __call__(self, text: str = "") -> None:
        for line in (text.splitlines() or [""]):
            print(scrub(line), file=self._stream)
        self._stream.flush()


# -- arguments ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker.ib.watch_bars",
        description=(
            "Owner-run: watch IB's live-updating 1-minute regular-hours bars for a few symbols, "
            "print each bar as it closes and save the closed bars to one CSV file per symbol and "
            "day. It connects to TWS or IB Gateway and sends DATA REQUESTS ONLY: it runs no algo "
            "and places no trade. Only the owner runs it, from a real terminal; you type the "
            "port number to confirm.\n\n"
            "A bar is closed when an update with a later minute arrives. The session recording "
            "(on by default) grows with every update and may slow a long watch: use --no-record "
            "to avoid that."
        ),
        epilog=EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, required=True, help="TWS or Gateway API port (no default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID,
                        help=f"default {DEFAULT_CLIENT_ID} (the probe and fetch-bars use 21)")
    parser.add_argument("--symbols", nargs="+", default=["AAPL"], metavar="SYMBOL",
                        help=f"tickers, comma or space separated, at most {MAX_SYMBOLS} (default AAPL)")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help=f"how long to watch, 1 to {MAX_MINUTES} (default {DEFAULT_MINUTES})")
    parser.add_argument("--backfill-minutes", type=int, default=DEFAULT_BACKFILL_MINUTES,
                        help=f"minutes of bars IB sends before the live ones, 1 to "
                             f"{MAX_BACKFILL_MINUTES} (default {DEFAULT_BACKFILL_MINUTES})")
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=r"folder for the live files (default %%USERPROFILE%%\Stoinkulator\live; "
                             "not in the repo or OneDrive)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--record", default=None, metavar="FILE",
                       help=r"session recording file, never overwritten (default: a new file in "
                            r"%%USERPROFILE%%\Stoinkulator\recordings)")
    group.add_argument("--no-record", action="store_true", help="do not record the session")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT,
                        help="seconds per connection attempt; TWS may show a dialog (default 30)")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the throttled forming-bar line")
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    names: list[str] = []
    for chunk in args.symbols:
        for raw in chunk.split(","):
            if not raw.strip():
                continue
            try:
                name = check_symbol(raw.strip().upper())
            except ValueError as exc:
                parser.error(f"bad --symbols entry {raw!r}: {exc}")
            if name not in names:
                names.append(name)
    if not names:
        parser.error("--symbols names no symbol")
    if len(names) > MAX_SYMBOLS:
        parser.error(f"--symbols names {len(names)} symbols; at most {MAX_SYMBOLS}")
    args.symbols = names
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1 to 65535")
    if not 1 <= args.client_id <= MAX_CLIENT_ID or args.client_id in REFUSED_CLIENT_IDS:
        parser.error(f"--client-id must be 1 to {MAX_CLIENT_ID} and not {sorted(REFUSED_CLIENT_IDS)}")
    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be 1 to {MAX_MINUTES}")
    if not 1 <= args.backfill_minutes <= MAX_BACKFILL_MINUTES:
        parser.error(f"--backfill-minutes must be 1 to {MAX_BACKFILL_MINUTES}")
    if not 0 < args.connect_timeout <= MAX_CONNECT_TIMEOUT:
        parser.error(f"--connect-timeout must be above 0 and at most {MAX_CONNECT_TIMEOUT:g}")
    return args


# -- paths and gate -------------------------------------------------------------------------


def next_recording_path(folder: Path, day: date) -> Path:
    """The first `watch-bars-<date>-<n>.jsonl` in `folder` that does not exist yet."""
    for number in range(1, 10_000):
        candidate = folder / f"watch-bars-{day:%Y-%m-%d}-{number:03d}.jsonl"
        if not candidate.exists():
            return candidate
    raise PathRefused(f"no free recording name in {folder}")


def resolve_paths(args: argparse.Namespace, env: Mapping[str, str], today: date, cwd: Path) -> None:
    """Fill `args.out` (Path), `args.record` (Path or None) and `args.private`; `PathRefused` if bad."""
    home = home_folder(env)
    out = Path(args.out) if args.out else home / "Stoinkulator" / "live"
    check_path(out, env, "the live folder")
    check_folder_usable(out, "the live folder")
    for symbol in args.symbols:
        check_folder_usable(out / symbol, f"the {symbol} folder")
    record: Path | None = None
    if not args.no_record:
        if args.record:
            record = Path(args.record)
            check_path(record.parent, env, "the recording folder")
            check_path(record, env, "the recording file")
            check_folder_usable(record.parent, "the recording folder")
            if record.exists():
                raise PathRefused(f"the recording file {record} exists; it is never overwritten")
        else:
            folder = home / "Stoinkulator" / "recordings"
            check_path(folder, env, "the recording folder")
            check_folder_usable(folder, "the recording folder")
            record = next_recording_path(folder, today)
    args.out = out
    args.record = record
    args.private = private_values(env, cwd)


def describe_session(args: argparse.Namespace) -> str:
    record = f"{args.record}" if args.record else "none (--no-record)"
    return "\n".join([
        "This command connects to TWS or IB Gateway. Only the owner runs it.",
        f"  Connects to:      {args.host}:{args.port}",
        f"  Client id:        {args.client_id}",
        f"  Symbols:          {', '.join(args.symbols)}",
        f"  Watches for:      {args.minutes} minutes (backfill {args.backfill_minutes} minutes)",
        f"  Live files:       {args.out}",
        f"  Recording file:   {record}",
        "  It sends data requests only: it runs no algo and places no trade.",
    ])


def gate(args: argparse.Namespace, stdin: TextIO, input_fn: Callable[[str], str], say: _Say) -> bool:
    isatty = getattr(stdin, "isatty", None)
    if isatty is None or not isatty():
        say("Refusing to run: this command connects to TWS and only the owner runs it, "
            "from an interactive terminal (stdin is not one).")
        return False
    say(describe_session(args))
    try:
        typed = input_fn(f"To proceed, type the port number ({args.port}) and press Enter: ")
    except (EOFError, KeyboardInterrupt):
        say("No confirmation given. Nothing was started.")
        return False
    if typed.strip() != str(args.port):
        say("The text typed is not the port number. Nothing was started.")
        return False
    return True


# -- the live files -------------------------------------------------------------------------


class LiveFiles:
    """Appends closed bars to `<root>/<SYMBOL>/<SYMBOL>_live_<YYYY-MM-DD>.csv`, flushed per bar."""

    def __init__(self, root: Path, say: _Say) -> None:
        self._root = root
        self._say = say
        self._handles: dict[Path, TextIO] = {}
        self._last: dict[Path, int | None] = {}
        self.written: dict[str, list[Path]] = {}

    def path_for(self, symbol: str, bar: Bar) -> Path:
        return self._root / symbol / f"{symbol}_live_{bar.date:%Y-%m-%d}.csv"

    def write(self, symbol: str, bar: Bar) -> bool:
        """Append `bar`; False (nothing written) if the file already has this minute or a later one."""
        path = self.path_for(symbol, bar)
        try:
            handle = self._handles.get(path) or self._open(symbol, path)
            last = self._last[path]
            if last is not None and bar.time <= last:
                return False
            handle.write(
                f"{bar.date:%Y-%m-%d %H:%M:%S},{bar.time},{bar.open!r},{bar.high!r},{bar.low!r},"
                f"{bar.close!r},{bar.volume}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            raise LiveFileError(f"cannot write {path}: {type(exc).__name__}: {exc}") from exc
        self._last[path] = bar.time
        return True

    def _open(self, symbol: str, path: Path) -> TextIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        last: int | None = None
        fresh = True
        needs_newline = False
        if path.exists() and path.stat().st_size > 0:
            fresh = False
            text = path.read_text(encoding="utf-8")
            lines = [line for line in text.splitlines() if line.strip()]
            if not lines or lines[0].strip() != HEADER:
                raise LiveFileError(f"{path} exists but its header is not {HEADER!r}; it was left untouched")
            try:
                last = int(lines[-1].split(",")[1]) if len(lines) > 1 else None
            except (IndexError, ValueError) as exc:
                raise LiveFileError(f"{path} exists but its last row is unreadable; "
                                    "it was left untouched") from exc
            needs_newline = not text.endswith("\n")
            shown = "-" if last is None else f"{data_time(last):%H:%M}"
            self._say(f"{symbol}: {path.name} exists with {len(lines) - 1} rows, last {shown}; "
                      "appending only later bars")
        handle = path.open("a", encoding="utf-8", newline="\n")
        if needs_newline:
            handle.write("\n")
        if fresh:
            handle.write(HEADER + "\n")
            handle.flush()
        self._handles[path] = handle
        self._last[path] = last
        paths = self.written.get(symbol, [])
        if path not in paths:
            paths.append(path)
        self.written[symbol] = paths
        return handle

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()


# -- per-symbol state -----------------------------------------------------------------------


@dataclass
class _Forming:
    bar: Bar
    updates: int
    first_seen: float | None  # IB server time (epoch s) of the first update; None: from the backfill


@dataclass
class _Sym:
    name: str
    info: Any = None
    req_id: int | None = None
    cancelled: bool = False
    phase: str = "new"  # new, backfill, live, stopped
    stop_reason: str = ""
    requested_at: float = 0.0
    rerequests: int = 0
    first_backfill: bool = True
    last_emitted: int | None = None
    forming: _Forming | None = None
    emitted: list[int] = field(default_factory=list)
    last_bar: Bar | None = None
    live_closed: int = 0
    backfill_written: int = 0
    backfilled_late: int = 0
    closed_by_time: int = 0
    late: int = 0
    bad: int = 0
    update_counts: list[int] = field(default_factory=list)
    first_delays: list[float] = field(default_factory=list)
    close_delays: list[float] = field(default_factory=list)
    last_forming_line: float = -math.inf

    @property
    def had_bars(self) -> bool:
        return bool(self.emitted) or self.forming is not None


def _num(value: float) -> str:
    return repr(float(value))


def _mmm(values: Sequence[float], spec: str = ".1f") -> str:
    """`min / median / max` of `values`, each formatted with `spec`; `n/a` when there are none."""
    if not values:
        return "n/a"
    return " / ".join(format(v, spec) for v in (min(values), statistics.median(values), max(values)))


def _hm(epoch: int) -> str:
    return f"{data_time(epoch):%H:%M}"


class _Watcher:
    """The session: requests, the reading loop, the closed-bar logic and the summary."""

    def __init__(self, args: argparse.Namespace, conn: IbConnection, md: IbMarketData, say: _Say,
                 files: LiveFiles, clock: Callable[[], float], sleep: Callable[[float], None],
                 wall: Callable[[], datetime]) -> None:
        self.args = args
        self.conn = conn
        self.md = md
        self.say = say
        self.files = files
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.syms = [_Sym(name) for name in args.symbols]
        self.server0: int | None = None
        self.clock0 = 0.0
        self.lost_noted = False
        self.lost_at_end = False
        self.closed = False
        self._general_seen: dict[int, events.IbError] = {}

    # -- time -----------------------------------------------------------------------------

    def server_now(self) -> float:
        assert self.server0 is not None
        return self.server0 + (self.clock() - self.clock0)

    # -- the session ----------------------------------------------------------------------

    def run(self) -> int:
        code = EXIT_STOPPED
        try:
            code = self._session()
        except KeyboardInterrupt:
            self.say()
            self.say("Interrupted (Ctrl-C). Requests are cancelled and the connection is closed.")
            code = EXIT_INTERRUPTED
        except _Closed:
            self.say("STOPPED: the connection to TWS closed.")
            code = EXIT_STOPPED
        except Exception as exc:  # noqa: BLE001 - reported, not a traceback
            self.say(f"Unexpected {type(exc).__name__}: {exc}")
            code = EXIT_STOPPED
        finally:
            self._finish()
        self._summary()
        return code

    def _session(self) -> int:
        say, md = self.say, self.md
        say("Waiting for the data farms ...")
        market = self.conn.wait_for_data_farm(FARM_TIMEOUT, farm="market")
        historical = self.conn.wait_for_data_farm(FARM_TIMEOUT, farm="historical")
        say(f"  market data farm {'up' if market else 'NOT seen'}, "
            f"historical data farm {'up' if historical else 'NOT seen'}")
        if not historical:
            say("STOPPED: no historical data farm: TWS is not connected to IB's history service yet. "
                "Wait a minute and retry.")
            return EXIT_STOPPED
        md.request_server_time()
        try:
            server = md.wait_server_time(SERVER_TIME_TIMEOUT)
        except DataLost:
            server = None
        if server is None:
            say(f"STOPPED: no server time from IB within {SERVER_TIME_TIMEOUT:g} s.")
            return EXIT_STOPPED
        self.server0 = int(server.timestamp())
        self.clock0 = self.clock()
        diff = (server - self.wall()).total_seconds()
        say(f"IB server time {server.astimezone(NY):%Y-%m-%d %H:%M:%S} New York; "
            f"IB minus this computer's clock = {diff:+.1f} s (IB time has 1 s resolution)")
        for sym in self.syms:
            self._resolve(sym)
        self._request_all()
        return self._loop()

    def _resolve(self, sym: _Sym) -> None:
        try:
            sym.info = self.md.resolve_stock(sym.name, RESOLVE_TIMEOUT)
        except ResolveFailed as exc:
            self._stop(sym, f"cannot resolve: {exc}")
            return
        info = sym.info
        self.say(f"{sym.name}: conId {info.con_id}, primary exchange {info.primary_exchange}, "
                 f"time zone {info.time_zone_id}")

    def _request(self, sym: _Sym) -> None:
        duration = f"{self.args.backfill_minutes * 60} S"
        sym.req_id = self.md.request_history(sym.info.contract(), None, duration, use_rth=True,
                                             keep_up_to_date=True)
        sym.cancelled = False
        sym.requested_at = self.clock()
        sym.phase = "backfill"

    def _request_all(self) -> None:
        first = True
        for sym in self.syms:
            if sym.info is None:
                continue
            if not first:
                self.sleep(REQUEST_SPACING)
            first = False
            try:
                self._request(sym)
            except NotConnected as exc:
                raise _Closed from exc
            self.say(f"{sym.name}: requested live bars with {self.args.backfill_minutes} minutes "
                     "of backfill")

    def _loop(self) -> int:
        deadline = self.clock() + self.args.minutes * 60
        while self.clock() < deadline:
            active = [s for s in self.syms if s.phase in ("backfill", "live")]
            if not active:
                break
            self._check_state()
            self._show_general()
            slice_ = max(MIN_POLL_SLICE, POLL_BUDGET / len(active))
            progressed = False
            for sym in active:
                if sym.phase in ("backfill", "live"):
                    progressed |= self._service(sym, slice_)
                if sym.phase in ("backfill", "live"):
                    self._settle(sym)
            if not progressed:
                self.sleep(IDLE_SLEEP)
        self.lost_at_end = self.conn.state is ConnState.LOST
        self.say()
        self.say(f"Watch time over ({self.args.minutes} minutes)." if self.clock() >= deadline
                 else "No symbol is streaming any more.")
        if self.lost_at_end:
            self.say("The connection to IB is still lost (code 1100).")
            return EXIT_STOPPED
        failed = any(s.phase == "stopped" or not s.had_bars for s in self.syms)
        return EXIT_FAILED if failed else EXIT_OK

    # -- reading ----------------------------------------------------------------------------

    def _check_state(self) -> None:
        state = self.conn.state
        if state is ConnState.CLOSED:
            raise _Closed
        if state is ConnState.LOST and not self.lost_noted:
            self.lost_noted = True
            self.say("IB connectivity LOST (code 1100): waiting for it to be restored; "
                     "the watch time keeps running.")
        elif state is ConnState.READY and self.lost_noted:
            self.lost_noted = False
            self.say("IB connectivity restored.")

    def _show_general(self) -> None:
        for event in self.md.general_events:
            if isinstance(event, events.IbError) and id(event) not in self._general_seen:
                self._general_seen[id(event)] = event  # keeps the object, so its id is not reused
                kind = "notice" if 2100 <= event.code < 2200 else "error"
                self.say(f"IB {kind} {event.code}: {event.message}")

    def _service(self, sym: _Sym, slice_: float) -> bool:
        """Read one thing for `sym`; True if something was received."""
        assert sym.req_id is not None
        if sym.phase == "backfill":
            return self._service_backfill(sym, slice_)
        result = self.md.poll_event(sym.req_id, slice_)
        if isinstance(result, Outcome):
            if result is CLOSED:
                raise _Closed
            if result is DATA_LOST:
                self._rerequest(sym)
                return True
            if result is ERROR:
                self._request_failed(sym)
                return True
            return False  # timeout, or lost (reported from the connection state)
        if isinstance(result, events.HistoricalBar):
            self._update(sym, result)
        return True

    def _request_failed(self, sym: _Sym) -> None:
        """IB answered the request with an error (`poll_event` says `error` and keeps the text in
        the request's result, so it is read from there): print it verbatim and stop this symbol."""
        assert sym.req_id is not None
        failures = self.md.wait_history(sym.req_id, 0.0).failures
        for failure in failures:
            self.say(f"IB error {failure.code} for {sym.name}: {failure.message}")
        self._stop(sym, f"IB error {failures[0].code}" if failures else "IB error")

    def _service_backfill(self, sym: _Sym, slice_: float) -> bool:
        assert sym.req_id is not None
        result = self.md.wait_history(sym.req_id, slice_)
        outcome = result.outcome
        if outcome is END:
            self._backfill(sym, result.bars)
            sym.phase = "live"
            return True
        if outcome is ERROR:
            self._request_failed(sym)
            return True
        if outcome is DATA_LOST:
            self._rerequest(sym)
            return True
        if outcome is CLOSED:
            raise _Closed
        if outcome is Outcome.TIMEOUT and self.clock() - sym.requested_at > BACKFILL_TIMEOUT:
            self.say(f"{sym.name}: no backfill end marker from IB within {BACKFILL_TIMEOUT:g} s")
            self._stop(sym, "no backfill from IB")
            return True
        return False

    def _rerequest(self, sym: _Sym) -> None:
        """IB restored with code 1101 (data lost): cancel the stream and ask for it once more."""
        self.say(f"IB restored the connection with data lost (code 1101): requesting {sym.name} again")
        self._cancel(sym)
        if sym.rerequests >= MAX_REREQUESTS:
            self._stop(sym, f"data lost {MAX_REREQUESTS + 1} times")
            return
        sym.rerequests += 1
        wait = IDENTICAL_REQUEST_SPACING - (self.clock() - sym.requested_at)
        if wait > 0:
            self.sleep(wait)
        try:
            self._request(sym)
        except NotConnected as exc:
            raise _Closed from exc

    def _cancel(self, sym: _Sym) -> None:
        if sym.req_id is None or sym.cancelled:
            return
        sym.cancelled = True
        try:
            self.md.cancel_history(sym.req_id)
        except (ValueError, NotConnected):
            pass

    def _stop(self, sym: _Sym, reason: str) -> None:
        self._cancel(sym)
        sym.phase = "stopped"
        sym.stop_reason = reason
        self.say(f"{sym.name}: stopped watching ({reason}); the other symbols continue")

    # -- bars -------------------------------------------------------------------------------

    def _convert(self, sym: _Sym, raw: events.HistoricalBar) -> Bar | None:
        try:
            return bar_from_ib(raw)
        except BadBar as exc:
            sym.bad += 1
            self.say(f"{sym.name}: bad bar skipped: {exc}")
            return None

    def _backfill(self, sym: _Sym, raw: Sequence[events.HistoricalBar]) -> None:
        now = self.server_now()
        by_time: dict[int, Bar] = {}
        for item in raw:
            bar = self._convert(sym, item)
            if bar is not None:
                by_time[bar.time] = bar
        bars = [by_time[t] for t in sorted(by_time)]
        initial = sym.first_backfill
        sym.first_backfill = False
        added = 0
        for bar in bars:
            if sym.last_emitted is not None and bar.time <= sym.last_emitted:
                continue
            forming = sym.forming
            if bar.time + 60 + CLOSE_GRACE <= now:
                if forming is not None and forming.bar.time < bar.time:
                    self._close_forming(sym, now, "later")
                elif forming is not None and forming.bar.time == bar.time:
                    sym.forming = None  # the backfill carries the final version
                if self._emit(sym, bar, now, updates=None, first_seen=None,
                              how="backfill" if initial else "backfilled"):
                    added += 1
            elif forming is None:
                sym.forming = _Forming(bar, 0, None)
            elif forming.bar.time == bar.time:
                forming.bar = bar
            elif forming.bar.time < bar.time:
                self._close_forming(sym, now, "later")
                sym.forming = _Forming(bar, 0, None)
        if bars:
            text = f"{sym.name}: {'backfill' if initial else 're-requested backfill'}: {len(bars)} bars, "
            text += f"first {_hm(bars[0].time)}, last {_hm(bars[-1].time)}"
            text += f"; {added} new closed bars written"
            if sym.forming is not None and sym.forming.updates == 0:
                text += f"; the {_hm(sym.forming.bar.time)} bar is still forming"
        else:
            text = f"{sym.name}: {'backfill' if initial else 're-requested backfill'}: 0 bars"
        self.say(text)

    def _update(self, sym: _Sym, raw: events.HistoricalBar) -> None:
        now = self.server_now()
        bar = self._convert(sym, raw)
        if bar is None:
            return
        forming = sym.forming
        if sym.last_emitted is not None and bar.time <= sym.last_emitted:
            self._late(sym, bar, f"already emitted up to {_hm(sym.last_emitted)}")
        elif forming is None:
            sym.forming = _Forming(bar, 1, now)
        elif bar.time == forming.bar.time:
            forming.bar = bar
            forming.updates += 1
        elif bar.time < forming.bar.time:
            self._late(sym, bar, f"the forming minute is {_hm(forming.bar.time)}")
        else:
            self._close_forming(sym, now, "later")
            sym.forming = _Forming(bar, 1, now)
        self._show_forming(sym)

    def _late(self, sym: _Sym, bar: Bar, why: str) -> None:
        sym.late += 1
        self.say(f"{sym.name}: late revision for {_hm(bar.time)} ({why}); counted, "
                 "nothing already emitted is rewritten")

    def _show_forming(self, sym: _Sym) -> None:
        forming = sym.forming
        if self.args.quiet or forming is None or forming.updates == 0:
            return
        now = self.clock()
        if now - sym.last_forming_line < FORMING_EVERY:
            return
        sym.last_forming_line = now
        bar = forming.bar
        server = datetime.fromtimestamp(self.server_now(), tz=UTC).astimezone(NY)
        self.say(f"{server:%H:%M:%S}  {sym.name}  forming {_hm(bar.time)}  {_ohlc(bar)}  "
                 f"volume {bar.volume}  (update {forming.updates})")

    def _close_forming(self, sym: _Sym, now: float, how: str) -> None:
        forming = sym.forming
        assert forming is not None
        sym.forming = None
        self._emit(sym, forming.bar, now, updates=forming.updates, first_seen=forming.first_seen,
                   how=how)

    def _settle(self, sym: _Sym) -> None:
        forming = sym.forming
        if forming is not None and self.server_now() >= forming.bar.time + 60 + SETTLE_SECONDS:
            self._close_forming(sym, self.server_now(), "time")

    def _emit(self, sym: _Sym, bar: Bar, now: float, *, updates: int | None, first_seen: float | None,
              how: str) -> bool:
        """Write and print one closed bar. True if a row was appended to its file."""
        try:
            written = self.files.write(sym.name, bar)
        except LiveFileError as exc:
            self.say(f"{sym.name}: {exc}")
            self._stop(sym, "cannot write the live file")
            return False
        sym.emitted.append(bar.time)
        sym.last_emitted = bar.time
        sym.last_bar = bar
        head = f"{_hm(bar.time)}  {sym.name}  {_ohlc(bar)}  volume {bar.volume}"
        if how == "backfill":
            sym.backfill_written += int(written)
            return written
        if how == "backfilled":
            sym.backfilled_late += 1
            self.say(f"{head}  (backfilled{'' if written else ', already in the file'})")
            return written
        if how == "time":
            sym.closed_by_time += 1
            self.say(f"{head}  (closed by time: no later update arrived within {SETTLE_SECONDS} s "
                     f"of the minute's end; updates {updates})")
            return written
        sym.live_closed += 1
        if first_seen is None or updates is None:
            self.say(f"{head}  (updates {updates}, first update not seen: the bar was forming "
                     "at the backfill)")
            return written
        first_delay = first_seen - bar.time
        close_delay = now - (bar.time + 60)
        sym.update_counts.append(updates)
        sym.first_delays.append(first_delay)
        sym.close_delays.append(close_delay)
        self.say(f"{head}  (updates {updates}, first seen +{first_delay:.1f} s after the minute "
                 f"started, closed +{close_delay:.1f} s after the minute ended)"
                 f"{'' if written else ' (already in the file)'}")
        return written

    # -- the end ----------------------------------------------------------------------------

    def _finish(self) -> None:
        """Cancel every request and close what the end of the minute allows. Never raises."""
        try:
            if self.server0 is not None:
                for sym in self.syms:
                    if sym.phase in ("backfill", "live"):
                        self._settle(sym)
        except Exception as exc:  # noqa: BLE001 - the summary must still appear
            self.say(f"Closing the last bars raised {type(exc).__name__}: {exc}")
        for sym in self.syms:
            try:
                self._cancel(sym)
            except Exception as exc:  # noqa: BLE001
                self.say(f"Cancelling {sym.name} raised {type(exc).__name__}: {exc}")

    def _summary(self) -> None:
        say = self.say
        say()
        say("Summary")
        for sym in self.syms:
            state = "streaming" if sym.phase in ("backfill", "live") else (
                f"stopped: {sym.stop_reason}" if sym.phase == "stopped" else "never requested")
            say(f"  {sym.name}: {state}")
            say(f"    closed bars emitted live: {sym.live_closed} (closed by time {sym.closed_by_time}); "
                f"backfill bars written: {sym.backfill_written}; backfilled after an outage: "
                f"{sym.backfilled_late}")
            missing = _missing_minutes(sym.emitted)
            if missing:
                shown = ", ".join(missing[:MAX_MISSING_SHOWN])
                extra = len(missing) - MAX_MISSING_SHOWN
                more = f" ... and {extra} more" if extra > 0 else ""
                say(f"    missing minutes inside the span ({len(missing)}): {shown}{more}")
            else:
                say("    missing minutes inside the span: none")
            say(f"    late revisions: {sym.late}; bad bars skipped: {sym.bad}; "
                f"re-requests after data loss: {sym.rerequests}")
            say(f"    updates per bar (min / median / max): {_mmm(sym.update_counts, 'g')}")
            say(f"    first update after the minute started, s (min / median / max): "
                f"{_mmm(sym.first_delays)}")
            say(f"    closed after the minute ended, s (min / median / max): {_mmm(sym.close_delays)}")
            if sym.last_bar is not None:
                bar = sym.last_bar
                say(f"    last closed bar: {_hm(bar.time)}  {_ohlc(bar)}  volume {bar.volume}")
            else:
                say("    last closed bar: none")
            if sym.forming is not None:
                say(f"    still forming, not written: {_hm(sym.forming.bar.time)}  "
                    f"{_ohlc(sym.forming.bar)}  volume {sym.forming.bar.volume}")
            for path in self.files.written.get(sym.name, []):
                say(f"    file: {path}")
        say(OFFSET_NOTE)
        say(COMPARE_NOTE)


def _ohlc(bar: Bar) -> str:
    return f"O {_num(bar.open)} H {_num(bar.high)} L {_num(bar.low)} C {_num(bar.close)}"


def _missing_minutes(times: Sequence[int]) -> list[str]:
    """The minutes without a bar between consecutive emitted bars of the same New York day."""
    missing: list[str] = []
    ordered = sorted(set(times))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if data_time(earlier).date() != data_time(later).date():
            continue
        for epoch in range(earlier + 60, later, 60):
            missing.append(_hm(epoch))
    return missing


# -- the run ----------------------------------------------------------------------------------

_LABELS = {
    EXIT_OK: "watched to the end, every symbol streaming",
    EXIT_FAILED: "finished, but a symbol stopped or delivered no bars",
    EXIT_REFUSED: "refused, nothing was watched",
    EXIT_NO_CONNECTION: "could not connect",
    EXIT_STOPPED: "stopped early (lost or closed connection, or an unexpected error)",
    EXIT_INTERRUPTED: "interrupted with Ctrl-C",
}
ADVICE_CLIENT_ID = ("Client id in use: another API client is connected with this id. Close it or pass "
                    "another --client-id.")
ADVICE_CONNECT = ("Cannot connect: check the port and that the API is enabled in TWS "
                  "(Global Configuration, API, Settings).")


def _connect(say: _Say, conn: IbConnection, args: argparse.Namespace, advice: list[str]) -> int | None:
    """None when connected, else the exit code (and the advice for the cause)."""
    say(f"Connecting to TWS at {args.host}:{args.port} (client id {args.client_id}, 2 attempts, "
        f"{args.connect_timeout:g} s each; TWS may ask you to accept the connection) ...")
    try:
        conn.open()
    except (BrokerPortRefused, ClientIdRefused, BrokerConnectionRefusedHere) as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except ConnectFailed as exc:
        for failure in exc.failures:
            say(f"  {failure.describe()}")
        say(f"Could not connect: {exc}")
        advice.append(ADVICE_CLIENT_ID if isinstance(exc, ClientIdInUse) else ADVICE_CONNECT)
        return EXIT_NO_CONNECTION
    except Exception as exc:  # noqa: BLE001 - reported, not a traceback
        say(f"Could not connect: {type(exc).__name__}: {exc}")
        advice.append(ADVICE_CONNECT)
        return EXIT_NO_CONNECTION
    say("Connected.")
    return None


def run_watch(
    args: argparse.Namespace,
    *,
    connection_factory: ConnectionFactory,
    market_data_factory: MarketDataFactory,
    recorder_factory: RecorderFactory,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime],
) -> int:
    """Connect, watch, summarise; always cancels every request and closes the connection and recorder.

    `args` carries `symbols`, `minutes`, `backfill_minutes`, `quiet`, `out` (Path), `record` (Path or
    None), `private`, `host`, `port`, `client_id` and `connect_timeout`. `clock` is monotonic
    seconds, `sleep` an interruptible sleep, `wall` this computer's clock as an aware datetime.
    Returns the exit code.
    """
    say = _Say(out)
    try:
        recorder = recorder_factory(args)
    except (OSError, PathRefused) as exc:
        say(f"Cannot start the session recording: {type(exc).__name__}: {exc}")
        say(f"Result: {_LABELS[EXIT_REFUSED]} (exit {EXIT_REFUSED})")
        return EXIT_REFUSED
    files = LiveFiles(args.out, say)
    conn: IbConnection | None = None
    code = EXIT_STOPPED
    advice: list[str] = []
    try:
        conn = connection_factory(args, [recorder] if recorder is not None else [])
        refused = _connect(say, conn, args, advice)
        if refused is not None:
            code = refused
        else:
            md = market_data_factory(conn)
            code = _Watcher(args, conn, md, say, files, clock, sleep, wall).run()
    except KeyboardInterrupt:
        say()
        say("Interrupted (Ctrl-C).")
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - reported with the exit code, not a traceback
        say(f"Unexpected {type(exc).__name__}: {exc}")
        code = EXIT_STOPPED
    finally:
        files.close()
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                say(f"Closing the connection raised {type(exc).__name__}: {exc}")
        if recorder is not None:
            recorder.close()
    for line in advice:
        say(f"What to do: {line}")
    if args.record is not None and recorder is not None:
        words = [w for w in args.private if len(w) >= 3]
        findings = scan_recording(args.record, words)
        say(f"Recording: {args.record}")
        say(f"Recording check: {len(findings)} finding(s). "
            "Read the file yourself before it is ever committed.")
        if findings:
            say("  Where to look (line number and reason only):")
        for finding in findings[:SHOWN_FINDINGS]:
            say(f"  line {finding.line}: {finding.reason}")
        if len(findings) > SHOWN_FINDINGS:
            say(f"  ... and {len(findings) - SHOWN_FINDINGS} more")
    say(f"Result: {_LABELS[code]} (exit {code})")
    say(PATHS_NOTE)
    return code


# -- the entry point --------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
    out: TextIO | None = None,
    connection_factory: ConnectionFactory | None = None,
    market_data_factory: MarketDataFactory | None = None,
    recorder_factory: RecorderFactory | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wall: Callable[[], datetime] | None = None,
    today: date | None = None,
) -> int:
    """Parse, check paths and port, create the folders, pass the console gate, then `run_watch`.

    The keyword arguments are for tests. This is the only place the process environment is read.
    """
    args = parse_args(argv)  # --help and argument errors exit here, before any client exists
    variables: Mapping[str, str] = os.environ if env is None else env
    stream = sys.stdout if out is None else out
    say = _Say(stream)
    wall_clock = wall or (lambda: datetime.now(UTC))
    try:
        resolve_paths(args, variables, today or datetime.now(NY).date(), Path.cwd())
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    try:
        check_broker_port(args.port, variables)
    except BrokerPortRefused as exc:
        say(port_refusal_text(exc))
        return EXIT_REFUSED
    folders = [args.out] + ([args.record.parent] if args.record is not None else [])
    try:
        created = make_folders(folders)  # the last refusal: the folders must be creatable
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    try:
        tty = sys.stdin if stdin is None else stdin
        ask = input if input_fn is None else input_fn
        if not gate(args, tty, ask, say):
            return EXIT_REFUSED
        logging.getLogger("ibapi").setLevel(logging.WARNING)
        now = clock or time.monotonic

        def real_connection(a: Any, observers: list[Any]) -> IbConnection:
            return IbConnection(
                a.host, a.port, a.client_id, env=variables, attempts=2, retry_delay=3,
                connect_timeout=a.connect_timeout, ready_timeout=15, observers=observers,
            )

        def real_recorder(a: Any) -> SessionRecorder | None:
            if a.record is None:
                return None
            a.record.parent.mkdir(parents=True, exist_ok=True)
            # the subcommand and the symbols only: no port, host or paths in the header
            return SessionRecorder(a.record, clock=now, command=["watch-bars", *a.symbols],
                                   port=a.port, private_values=a.private)

        return run_watch(
            args,
            connection_factory=connection_factory or real_connection,
            market_data_factory=market_data_factory or IbMarketData,
            recorder_factory=recorder_factory or real_recorder,
            out=stream,
            clock=now,
            sleep=sleep or time.sleep,
            wall=wall_clock,
        )
    finally:
        remove_empty(created)  # folders this run made and left empty (a refused gate, no connection)


if __name__ == "__main__":
    sys.exit(main())
