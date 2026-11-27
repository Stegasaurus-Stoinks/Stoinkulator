"""Owner-run live bar watcher: subscribe to IB's live-updating 1-minute bars and save the closed ones.

    python -m MainStoinker.ib.watch_bars --paper --symbols AAPL,MSFT,TSLA --minutes 30

`--paper` means port 7497 (the TWS paper port, data only) on 127.0.0.1 with the live opt-in absent
from the environment, a connection built with `require_paper=True`, and the account list checked
before the first request and all along (exit code 5, never retried). Without `--paper` a live port
is refused unless the owner has made their own opt-in in their own terminal; this module never
sets it.

Only the owner runs it, in a real terminal, with TWS or IB Gateway open. It runs no algo and
places no trade: it is built on `IbConnection` and `IbMarketData` (a data-only client that
cannot send anything but data requests) and is the first observation step before paper
trading on live bars. Agents run it only as `--paper --no-prompt --minutes N` (see below).

`--paper --no-prompt` (unattended, bounded): the same, minus the console gate (no terminal, no
port typed, nothing read from stdin); the first output line says `no prompt`. Every other check
of paper mode still runs. `--no-prompt` without `--paper` is a usage error, and with it
`--minutes` is required (1 to 420), so the run is bounded.

How IB's live bars arrive. One `reqHistoricalData` per symbol with `keepUpToDate=True` (empty
end time, `formatDate=2`, 1-minute regular-hours bars): IB sends the backfill bars for the
requested duration, then the end marker, then `historicalDataUpdate` callbacks. Each update
carries the CURRENT state of the bar still forming (the same timestamp repeated with changing
high, low, close and volume); when a new minute starts the updates carry the new timestamp.
A bar is therefore known to be closed only when an update with a LATER timestamp arrives:
the previous update is then final and is emitted once. The forming bar is never emitted as
closed and never written. An update with an EARLIER timestamp than the forming bar (or one
already emitted) is a late revision: counted, reported, and nothing already emitted is
rewritten (a revision of a minute already written says that the file keeps the earlier values
and shows both). A bar that no later update has closed 90 s after its minute ended is closed by
time (the end of the session has no later update) and marked so.

A row in a file is a FINISHED bar with FINAL values. When that cannot be known no row is written,
one line says why, and the owner refetches the minute later with `fetch-bars`. A missing row is
acceptable, a partial row is not. Hence:

- The backfill is IB's snapshot at the time the request was SENT. Every snapshot bar followed by
  a later bar of the snapshot is final. The snapshot's LAST bar is closed only if its minute ended
  at least `CLOSE_GRACE` s before the request by the stream's clock (below); otherwise it is the
  forming bar. A minute known only from a snapshot (no live update for it afterwards, whatever
  closes it) is never written: "SYMBOL HH:MM not written: seen only in the backfill snapshot".
- The stream's clock is always IB's server time less the lag bound of that symbol: the smallest
  `server time - start of the newest bar of today` seen, from the backfill (at request time) and
  from every live update. It is an upper bound on the lag, so closing by it is never early. A
  bar is closed by time 90 s after its minute ended on that clock; a bar ending in the last two
  minutes of the session also waits for the close plus 20 minutes (16:20 New York server time;
  13:20 on a half day; covers delayed data). Before any bar of today exists nothing is closed by
  time.
- The session is the market calendar's (`engine/market_calendar.py`): 09:30 to 16:00, or to 13:00 on
  a half day, never on a weekend or a holiday. Every "16:00" and "16:20" in this text is the day's
  close and the close plus 20 minutes. A weekend or a holiday is refused before connecting (exit
  code 2). Once the contract details are in, today's close by the calendar is compared with the
  regular hours IB reports in them (no new request): a disagreement prints a warning and IB's
  hours are followed for today; hours IB reports as closed end the run before any request (exit 2).
- The DELAYED label is separate (it changes no judgement): in the regular session (server time) a
  symbol is declared DELAYED, once, when two live updates at least
  10 s apart both show a lag over 120 s, or the backfill showed it and one live update confirms
  it; a current update withdraws it. Bar lines and the summary then say "(delayed about N min)".
  A start after the open with no bar of today yet says so; after the close the output says "market
  closed: no new bars expected", or for a delayed stream "regular session ended; delayed bars
  continue for about N minutes".
- No data (exit code 6): a symbol that, in the regular session by IB's server clock (09:30 to the
  day's close), has received no bar of today's session and no live update for at least 120 s
  after its backfill ended (a start before 09:30 starts the clock at 09:30; the end of the watch
  counts too) is `no data received`: one line says so as soon as it is established, with a hint
  about the account's market data subscription, the summary says so instead of `streaming`, and
  the result says no data was received. Data that arrives later makes the symbol `streaming` and
  the result follows the final state. Outside the session nothing changes (market closed, exit 0).
- While the connection is reported lost (code 1100) nothing is closed by time, and a bar whose
  minute overlapped the outage is never written ("not written: spanned a connection loss"). While
  a re-requested backfill is outstanding nothing is closed for that symbol; when it arrives its
  closed bars are written for minutes not yet in the file, and a minute already in the file that
  differs is reported as a row that may be stale.
- A broken data farm (IB notices 2103 market data, 2105 historical, until 2104 / 2106 or 1101 /
  1102) counts as an outage exactly like 1100: nothing is closed by time, and a minute that
  overlapped it is not written ("spanned a data farm break"). If the OK notice never comes,
  nothing is closed by time for the rest of the run and the summary says so.
- Stale-update guard: a bar closed BY TIME is written only if its last live update arrived at
  most 15 s before its minute ended on the stream's clock (covers a silent stall and a lag that
  grows mid-run); otherwise "not written: last update came N s before the minute ended (stream
  stalled?)". Trade-off: a thin symbol whose minute had no trade in its last 15 s AND no later
  update (so only time can close it) loses that row; bars closed by a later update, the normal
  case with updates every few seconds, are never affected.
- Bars and updates of an earlier New York date than the server's (a start before 09:30
  backfills the previous session) are not written.
- A live file that cannot be opened or appended (locked, unreadable, damaged, a last row that
  is not a complete row) stops only that symbol, with one line, and the file is left untouched.

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

Seams for a driver (an engine that runs on the closed bars). All are optional and change nothing
when absent: the command line, its limits, its output and its exit codes are the ones above. A
driver calls `run_watch` with a `WatchSeams`; this module sends no request it did not send before.

- Output: a `ClosedBars` sink (`write(symbol, bar, how) -> bool`, `close()`, `written`). `LiveFiles` is
  one; `TeeBars` feeds several in sequence and returns the answer of the first (the files). Every row
  the watcher appends to a file (a `write` call, whatever the file answers) reaches every sink.
- Notifications: a `BarListener` is told `decided(symbol, through_epoch)` and `stopped(symbol)`.
- Pass hook: `on_pass(clock)` once at the end of every pass of the reading loop, with a
  `StreamClock`: `server_now` (IB server time, seconds) and `stream_now`. The watcher decides each
  symbol on that symbol's own stream clock, server time less its lag bound (closing by time, the
  snapshot rule), so with delayed data server time is far ahead of what it has decided. `stream_now`
  is the MINIMUM over the running symbols of (server time - lag bound), the clock under which every
  running symbol's "closed by time" decisions are already past: it is what a driver passes to an
  aligner's `poll`. It is `None` while ANY running symbol has no measured lag bound (do not poll
  yet: a delayed symbol whose backfill holds only the previous session is unmeasured until its
  first update, and minutes would be released without it) and `server_now` when no symbol is
  running. The watcher closes a bar by time at minute end
  + 90 s on the stream clock, an aligner releases by time at end + 100 s on the clock it is
  polled with: polled with `stream_now` both are on one clock and the 10 s margin holds for any
  lag; polled with `server_now` it does not. `Watcher.stream_states()` gives per symbol the lag
  bound, the delayed flag and whether it still runs. An exception from the hook ends the watch
  like any unexpected error (exit 4); it must not block for long.
- Limits: `minutes` (the run deadline, up to `MAX_DRIVER_MINUTES`) and `backfill_minutes` (up to
  `MAX_SINCE_OPEN_MINUTES`, 390 = the whole session) or `backfill_since_open` (the duration asks for
  today's bars from 09:30, see `since_open_duration`; each re-request asks again the same way).
  `parse_args` keeps its limits.
- `Watcher` is the public name of the class; `Watcher.drop_counts()`, `Watcher.first_backfills_over()`,
  `Watcher.is_down()` and `Watcher.end_cause` are read-only.
- Early stop: `Watcher.request_stop(reason)` (callable from the pass hook) ends the loop at the end of
  the running pass; requests are cancelled, the sinks and the connection closed as at the end of the
  watch time, and the output says "Stopped on the driver's request (reason)." `Watcher.end_cause`
  says afterwards why `run` ended: `deadline`, `stop_requested`, `idle` (no symbol streaming),
  `lost` (connection still lost), `closed`, `not_paper`, `interrupted`, `error` (also the start-up
  stops `no_farm` and `no_server_time`).

What `decided(symbol, M)` promises. `M` is the START epoch of a minute. After it, no row of `symbol`
is written, and no `write` call is made, for any minute at or before `M`; a late `decided` only
delays a driver, an early one would lose a bar. The watcher tells `decided(symbol, last_emitted)`
and nothing else, where `last_emitted` is the newest minute of the symbol that was written or
reported "not written", and only right after that happened (in `_emit`, after every sink has the
row, so a driver sees `bar` before `decided`; and in `_not_written`). Why it is never early: the one
place a row is written is `_emit`; it is reached from `_backfill` (a snapshot) and from
`_close_forming` (from `_update`, `_settle` and `_finish`). Both reach it only for a minute strictly
after `last_emitted`: `_backfill` skips (compares only) every snapshot bar at or before it, and
the forming bar is always later than it, because `last_emitted` is raised only by writing or
dropping a bar the forming bar is not (it is cleared first, or it is the bar itself) and a forming
bar is only created for a minute after it. `last_emitted` never falls. Case by case, then:
- initial and re-requested backfill snapshot (also a late one, also one that holds minutes the
  stream never showed): writes only minutes after the point, the last snapshot bar stays forming
  unless the snapshot rule closes it, and nothing is told before the snapshot is processed;
- lag bound, closing by time, the 16:20 rule, the stale-update guard: they decide whether and when
  the forming bar (later than the point) is written or dropped, never which minute;
- a drop ("snapshot only", spanned a loss or a farm break, stalled, re-request gap): the minute is
  never written afterwards, so the point may include it, and does;
- connection loss, data farm break, a re-request outstanding: nothing is closed and nothing is
  told; a minute skipped by an update for a LATER minute is NOT decided (a re-requested backfill
  may still write it), which is why this is not "an update for a later minute arrived";
- late revisions and bars of an earlier date: never written;
- a symbol that stopped (file error, IB error, cap on re-requests): `_emit` writes nothing after
  it; `stopped` is told once, and again nothing else;
- the end of the watch: the last closing by time in `_finish` is a write like any other and is told
  before `stopped(symbol)` is sent for every symbol still running.

Checks, in this sequence, before any client or connection is built: arguments; default paths
(from `USERPROFILE`, else `HOME`, in the environment mapping given to `main`, never the
machine's idea of "home"); output and recording paths (not in the repository or a OneDrive
folder, not a file or under one); the port guard (`check_broker_port`: paper ports always, a
live port only if the OWNER set the opt-in variable named in the refusal text, in their own
terminal; this module never sets it); the day (a weekend or a market holiday, by this computer's
clock in New York, is refused: nothing is created or connected); the folders are created; the
console gate (an interactive terminal; the owner types the port number). The environment is read in
`main` only. Every printed line passes `scrub` and `ManagedAccounts` is never printed.

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
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.engine import market_calendar as calendar
from MainStoinker.engine.feed import Bar
from MainStoinker.ib import events, trading_hours
from MainStoinker.ib.bars import BadBar, bar_from_ib, data_time
from MainStoinker.ib.cli import (
    NOT_PAPER_LABEL,
    NOT_PAPER_LINE,
    NOT_PAPER_MID_LABEL,
    NOT_PAPER_MID_LINE,
    PAPER_HOST,
    PAPER_MODE_LINE,
    PAPER_NO_PROMPT_LINE,
    PAPER_PORT,
    PathRefused,
    check_folder_usable,
    check_path,
    closed_without_rejection,
    first_banner_line,
    home_folder,
    make_folders,
    mid_run_not_paper_label,
    mid_run_not_paper_line,
    paper_refusal,
    paper_session_ok,
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
    NotPaperSession,
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
EXIT_NOT_PAPER = 5
EXIT_NO_DATA = 6
EXIT_INTERRUPTED = 130

EXIT_CODES_TEXT = """\
exit codes:
  0    watched to the end and every symbol was streaming (or the market was closed)
  1    finished, but a symbol stopped (IB error, unknown symbol, unusable live file), had no
       stream at the end (a re-request still outstanding) or delivered no bars
  2    refused: console gate, port guard, --paper conditions, bad arguments, unusable path, a
       weekend or a market holiday (also when IB's hours say the market is closed today)
  3    could not connect to TWS or IB Gateway
  4    stopped by a lost or closed connection, or by an unexpected error
  5    --paper only: the account TWS is signed into is not a paper account (checked before
       the first request and all along); at the start nothing was requested, later the watch was
       stopped; never retried
  6    finished, every symbol streaming or closed, but at least one symbol received no data: no
       bar of today's session and no live update for 120 s of the regular session (09:30 to the
       day's close, 16:00 or 13:00 on a half day, New York) after its backfill ended: check the
       account's market data subscription
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
MAX_SINCE_OPEN_MINUTES = 390  # a driver (not the command line) may ask for the whole session
MAX_REQUEST_MINUTES = 24 * 60  # a since-open request after the close reaches back to the open, at most a day
MAX_DRIVER_MINUTES = 600 # the longest run deadline a driver may ask for (the command line: MAX_MINUTES)
REQUEST_SPACING = 1.0  # seconds between two history requests
IDENTICAL_REQUEST_SPACING = 15.0  # IB refuses an identical history request within 15 s
MAX_REREQUESTS = 10  # per symbol; more means something is wrong, not a passing outage
IDLE_SLEEP = 0.05
POLL_BUDGET = 0.25  # seconds one pass over all symbols may wait for events
MIN_POLL_SLICE = 0.02
FORMING_EVERY = 10.0  # seconds between two forming-bar lines of one symbol
CLOSE_GRACE = 2  # a backfill bar this soon after its minute ended may still be forming
SETTLE_SECONDS = 90  # a bar with no later update this long after its minute is closed by time
LABEL_CONFIRM = 10.0  # live updates this far apart must both show the lag before DELAYED is declared
SESSION_GRACE = 20 * 60  # the last bars of a session wait for 16:20 (covers 15-minute delayed data)
FARM_BROKEN = {2103: "market", 2105: "historical"}  # IB notices: a data farm connection is broken
FARM_OK = {2104: "market", 2106: "historical"}  # ... and that farm is OK again
STALE_UPDATE = 15.0  # a bar closed by time needs a live update at most this long before its minute ended
CONNECTION_NOTICES = frozenset({1100, 1101, 1102})  # connectivity lost, restored with data lost, restored
DELAY_THRESHOLD = 120.0 # newest bar more than this behind server time in session: delayed data
NO_DATA_AFTER = 120.0  # session seconds without any bar of today or update before "no data received"
NO_DATA_HINT = ("check the market data subscription of this account (quotes answering IB 354 / 10167 "
                "mean none; a paper account needs the live account's data shared with it); on a "
                "market holiday or with delayed data this is expected")
SESSION_OPEN_MINUTE = 9 * 60 + 30
SESSION_CLOSE_MINUTE = 16 * 60
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
    """A live file cannot be read or appended to (the message says why, naming the path once).

    `locked` is True when the file could not be opened or appended to (another program may hold it),
    False when its content is the problem (damaged, not UTF-8): then closing programs does not help."""

    def __init__(self, message: str, *, locked: bool = False) -> None:
        super().__init__(message)
        self.locked = locked


class _Closed(Exception):
    """The connection closed: stop and summarise."""


class _NotPaper(Exception):
    """`--paper`: the account list is not (or no longer) a paper account: stop and summarise."""


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
            "port number to confirm. (With --paper --no-prompt it runs unattended: no prompt, "
            "data only, paper account only.)\n\n"
            "A bar is closed when an update with a later minute arrives. The session recording "
            "(on by default) grows with every update and may slow a long watch: use --no-record "
            "to avoid that.\n\n"
            "Example, a paper account (port 7497, data only, account check on):\n"
            "  python -m MainStoinker.ib.watch_bars --paper --symbols AAPL,MSFT,TSLA --minutes 30\n"
            "A live port is refused unless the owner has made their own opt-in."
        ),
        epilog=EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,  # `--paper`, `--no-prompt` and `--minutes` are matched exactly
    )
    parser.add_argument("--port", type=int, default=None,
                        help=f"TWS or Gateway API port (no default; with --paper: {PAPER_PORT})")
    parser.add_argument("--host", default=PAPER_HOST)
    parser.add_argument("--paper", action="store_true",
                        help=f"paper mode: only port {PAPER_PORT} on {PAPER_HOST}, the live opt-in "
                             "must not be set in the environment, and the account list IB sends "
                             "must show a paper account before anything is requested and all along "
                             "(else exit code 5, never retried)")
    parser.add_argument("--no-prompt", action="store_true",
                        help="only with --paper (and then --minutes is required): skip the console "
                             "gate (no terminal needed, no port to type); every other check of "
                             "paper mode still runs")
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID,
                        help=f"default {DEFAULT_CLIENT_ID} (the probe and fetch-bars use 21)")
    parser.add_argument("--symbols", nargs="+", default=["AAPL"], metavar="SYMBOL",
                        help=f"tickers, comma or space separated, at most {MAX_SYMBOLS} (default AAPL)")
    parser.add_argument("--minutes", type=int, default=None,
                        help=f"how long to watch, 1 to {MAX_MINUTES} (default {DEFAULT_MINUTES}; "
                             "required with --no-prompt)")
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
    if args.no_prompt and not args.paper:
        parser.error("--no-prompt is valid only together with --paper")
    if args.minutes is None:
        if args.no_prompt:
            parser.error(f"--minutes is required with --no-prompt (1 to {MAX_MINUTES})")
        args.minutes = DEFAULT_MINUTES
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
    if args.port is None:
        if not args.paper:
            parser.error("--port is required (or use --paper, which means port 7497)")
        args.port = PAPER_PORT
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
        first_banner_line(args),
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


# -- where closed bars go and who is told about them ---------------------------------------------


class ClosedBars(Protocol):
    """Where the watcher puts a closed bar. `LiveFiles` is the default; `TeeBars` feeds several.

    `write(symbol, bar, how)` returns True if a row was appended to a file, False if the sink already
    had that minute or a later one; `how` is "backfill", "backfilled", "later" or "time". It may raise
    `LiveFileError`, which stops that symbol (the bar is then no row anywhere after it). `written`
    maps a symbol to the files this sink wrote (empty for a sink with no file)."""

    @property
    def written(self) -> Mapping[str, list[Path]]: ...

    def write(self, symbol: str, bar: Bar, how: str) -> bool: ...

    def close(self) -> None: ...


class BarListener(Protocol):
    """Told what the watcher knows about each symbol (see "What `decided` promises", above).

    `decided(symbol, through_epoch)`: no row of `symbol` is or will be written for a minute starting
    at or before `through_epoch`. `stopped(symbol)`: nothing more will come for `symbol`."""

    def decided(self, symbol: str, through_epoch: int) -> None: ...

    def stopped(self, symbol: str) -> None: ...


def _append(sink: ClosedBars, symbol: str, bar: Bar, how: str) -> bool:
    """One `write` call. `LiveFiles` ignores `how` and is called with the two arguments it always
    had (so what wraps or replaces its `write` keeps working); every other sink gets all three."""
    if type(sink) is LiveFiles:
        return sink.write(symbol, bar)
    return sink.write(symbol, bar, how)


class TeeBars:
    """Feeds the same bar to several sinks, in the sequence given, and answers as the first one.

    The first sink is the files: its answer is the watcher's "a row was appended", and when it raises
    (`LiveFileError`) no later sink gets the bar. Every later sink gets every bar the first one was
    asked to write, also one the file already had (False): a later run of the day still hands the
    whole backfill on. A later sink must not raise. `close` closes every sink; the first error is
    raised after all were tried."""

    def __init__(self, first: ClosedBars, *more: ClosedBars) -> None:
        self._sinks: tuple[ClosedBars, ...] = (first, *more)

    @property
    def written(self) -> dict[str, list[Path]]:
        merged: dict[str, list[Path]] = {}
        for sink in self._sinks:
            for symbol, paths in sink.written.items():
                into = merged.get(symbol, [])
                into.extend(p for p in paths if p not in into)
                merged[symbol] = into
        return merged

    def write(self, symbol: str, bar: Bar, how: str) -> bool:
        answer = _append(self._sinks[0], symbol, bar, how)
        for sink in self._sinks[1:]:
            _append(sink, symbol, bar, how)
        return answer

    def close(self) -> None:
        failure: Exception | None = None
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001 - every sink gets its close
                failure = failure or exc
        if failure is not None:
            raise failure


def _check_limits(minutes: int | None, backfill_minutes: int | None, since_open: bool) -> None:
    """The limits a driver may ask for (the command line keeps `MAX_MINUTES` and `MAX_BACKFILL_MINUTES`)."""
    if minutes is not None and not 1 <= minutes <= MAX_DRIVER_MINUTES:
        raise ValueError(f"minutes must be 1 to {MAX_DRIVER_MINUTES}, not {minutes!r}")
    if backfill_minutes is not None and not 1 <= backfill_minutes <= MAX_SINCE_OPEN_MINUTES:
        raise ValueError(f"backfill_minutes must be 1 to {MAX_SINCE_OPEN_MINUTES}, not {backfill_minutes!r}")
    if since_open and backfill_minutes is not None:
        raise ValueError("backfill_since_open and backfill_minutes exclude each other")


def since_open_minutes(server_now: float) -> int:
    """Minutes of backfill that reach back to 09:30 New York of the date of `server_now`: the whole
    minutes since the open rounded up, plus one for the bar still forming; 1 before the open. After
    the close it still reaches back to the open (16:30 asks for 421 minutes), at most a day."""
    elapsed = server_now - _session_start(_ny(server_now).date())
    if elapsed <= 0:
        return 1
    return min(MAX_REQUEST_MINUTES, math.ceil(elapsed / 60) + 1)


def since_open_duration(server_now: float) -> str:
    """The IB duration string of a backfill since the open, as `"<seconds> S"` like the usual one."""
    return f"{since_open_minutes(server_now) * 60} S"


@dataclass(frozen=True)
class WatchSeams:
    """What a driver adds to `run_watch`; the default changes nothing.

    `extra_sinks` come after the live files in a `TeeBars` (and are closed with it); `listener` and
    `on_pass` are described in the module docstring; `minutes` replaces `args.minutes` and
    `backfill_minutes` replaces `args.backfill_minutes` (or `backfill_since_open` asks for today's
    bars from 09:30 at every request), each within the driver limits; `on_watcher` is called with
    the `Watcher` right after it is built (to read `drop_counts()` later)."""

    extra_sinks: tuple[ClosedBars, ...] = ()
    listener: BarListener | None = None
    on_pass: Callable[[StreamClock], None] | None = None
    minutes: int | None = None
    backfill_minutes: int | None = None
    backfill_since_open: bool = False
    on_watcher: Callable[[Watcher], None] | None = None

    def __post_init__(self) -> None:
        _check_limits(self.minutes, self.backfill_minutes, self.backfill_since_open)


@dataclass(frozen=True)
class StreamClock:
    """What `on_pass` gets: IB server time and the clock to poll an aligner with (see the module
    docstring). `stream_now` is None until a running symbol has a measured lag bound."""

    server_now: float
    stream_now: float | None


@dataclass(frozen=True)
class StreamState:
    """One symbol's stream as the watcher sees it: the smallest lag seen (s; None: not measured),
    whether it is labelled delayed, and whether it still runs (False once stopped)."""

    lag_bound: float | None
    delayed: bool
    running: bool


@dataclass(frozen=True)
class DropCounts:
    """What one symbol did not write or revised, as the summary counts it (read-only)."""

    snapshot_only: int
    connection_loss: int
    farm_break: int
    stalled_stream: int
    rerequest_gap: int
    late_revisions: int
    stale_rows: int
    bad_bars: int
    rerequests: int


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

    def write(self, symbol: str, bar: Bar, how: str = "") -> bool:
        """Append `bar`; False (nothing written) if the file already has this minute or a later one.
        `how` (why the bar is closed) is not used here."""
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
            reason = exc.strerror or type(exc).__name__  # the text of OSError names the path again
            raise LiveFileError(f"cannot open or append to {path}: {type(exc).__name__}: {reason}",
                                locked=True) from exc
        except ValueError as exc:  # UnicodeDecodeError: the file is not UTF-8 text
            raise LiveFileError(f"cannot read {path}: {type(exc).__name__}; it was left untouched") from exc
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
                if len(lines) > 1:
                    fields = lines[-1].split(",")
                    if len(fields) != len(HEADER.split(",")):
                        raise ValueError("the last row is not complete")
                    last = int(fields[1])
                    for number in fields[2:]:
                        float(number)
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
    updates: int  # live updates received for this minute since the last backfill snapshot of it
    first_seen: float | None  # IB server time (epoch s) of the first update; None: from the backfill
    last_at: float | None = None  # IB server time (epoch s) of the last live update


@dataclass
class _Sym:
    name: str
    info: Any = None
    req_id: int | None = None
    cancelled: bool = False
    phase: str = "new"  # new, backfill, live, stopped
    stop_reason: str = ""
    requested_at: float = 0.0  # monotonic clock when the last request was sent
    requested_server: float = 0.0  # IB server time when the last request was sent
    rerequests: int = 0
    newest: int | None = None  # start of the newest bar of the server's New York date seen
    lag_bound: float | None = None  # smallest `server time - newest bar start` seen, s
    delayed: bool = False
    backfill_over: bool = False  # the backfill alone showed a lag above the label threshold
    over_since: float | None = None  # server time of the first live update showing it
    closed_noted: bool = False
    nothing_yet_noted: bool = False
    previous_session: int = 0  # backfill bars of an earlier date, not written
    previous_updates: int = 0  # live updates of an earlier date, ignored
    snapshot_only: int = 0  # minutes not written: seen only in a backfill snapshot
    lost_not_written: int = 0  # minutes not written: they overlapped a connection loss
    farm_not_written: int = 0  # minutes not written: they overlapped a data farm break
    stale_not_written: int = 0  # minutes not written: closed by time after a stalled stream
    gap_not_written: int = 0  # minutes not written: dropped by a re-requested backfill
    skipped_closes: int = 0  # bars closed by an update that skipped empty minutes
    stale: int = 0  # late revisions that differ from the row already emitted
    emitted_bars: dict[int, tuple[Bar, bool]] = field(default_factory=dict)
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
    live_since: float | None = None  # server time when the first backfill ended
    last_update_at: float | None = None  # server time of the last live update, whatever its date
    no_data_noted: bool = False  # the "no data received" line was printed

    @property
    def had_bars(self) -> bool:
        return bool(self.emitted) or self.forming is not None or self.previous_session > 0

    @property
    def lag(self) -> float:
        return (self.lag_bound or 0.0) if self.delayed else 0.0

    @property
    def tag(self) -> str:
        return f" (delayed about {max(1, round(self.lag / 60))} min)" if self.delayed else ""


def _num(value: float) -> str:
    return repr(float(value))


def _mmm(values: Sequence[float], spec: str = ".1f") -> str:
    """`min / median / max` of `values`, each formatted with `spec`; `n/a` when there are none."""
    if not values:
        return "n/a"
    return " / ".join(format(v, spec) for v in (min(values), statistics.median(values), max(values)))


def _hm(epoch: int) -> str:
    return f"{data_time(epoch):%H:%M}"


def _ny(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone(NY)


def _in_session(epoch: float, extra: calendar.Overrides | None = None) -> bool:
    """The regular session by IB's clock: 09:30 up to the close of that New York day by the market
    calendar (16:00, or 13:00 on a half day); never on a weekend or a holiday. `extra` is the
    calendar table of this run (IB's hours for today when they differ, see `Watcher.calendar_extra`)."""
    moment = _ny(epoch)
    found = calendar.session(moment.date(), extra)
    if found is None:
        return False
    minute = moment.hour * 60 + moment.minute
    return found.open_minute <= minute < found.close_minute


def _session_end(day: date, extra: calendar.Overrides | None = None) -> int:
    """Epoch of the close of `day`'s regular session: the calendar's close (16:00, 13:00 on a half
    day). A day with no session has no close: 16:00 stands in, so every offset from it stays defined."""
    found = calendar.session(day, extra)
    minute = calendar.FULL_CLOSE_MINUTE if found is None else found.close_minute
    return int(datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=NY).timestamp())


def _session_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).timestamp())


def _session_exposure(since: float, now: float, outages: Sequence[Sequence[Any]] = (),
                      extra: calendar.Overrides | None = None) -> float:
    """Seconds of the regular session (09:30 to the day's close by the market calendar, server
    time) that lie between `since` and `now` on the New York date of `now`, less the time of any
    outage (`[start, end, kind]`, end `inf` while open) inside that window."""
    day = _ny(now).date()
    if not calendar.is_trading_day(day, extra):
        return 0.0
    start = max(since, _session_start(day))
    end = min(now, _session_end(day, extra))
    if end <= start:
        return 0.0
    spans = sorted((max(o[0], start), min(o[1], end)) for o in outages if o[0] < end and o[1] > start)
    down = 0.0
    reached = start
    for begin, finish in spans:
        begin = max(begin, reached)
        if finish > begin:
            down += finish - begin
            reached = finish
    return (end - start) - down


def _header_only(path: Path) -> bool:
    """True when the recording holds the session header and no event (as `cli.py` decides it)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return len([line for line in text.split("\n") if line.strip()]) <= 1


class Watcher:
    """The session: requests, the reading loop, the closed-bar logic and the summary.

    `files` is where closed bars go (`LiveFiles`, or a `TeeBars` that also feeds a driver). The rest
    after `wall` is for a driver and changes nothing when absent: `listener` (told `decided` and
    `stopped`), `on_pass` (called with IB's server time at the end of every pass of the loop),
    `minutes` and `backfill_minutes` (instead of those of `args`) and `backfill_since_open`."""

    def __init__(self, args: argparse.Namespace, conn: IbConnection, md: IbMarketData, say: _Say,
                 files: ClosedBars, clock: Callable[[], float], sleep: Callable[[float], None],
                 wall: Callable[[], datetime], *, listener: BarListener | None = None,
                 on_pass: Callable[[StreamClock], None] | None = None, minutes: int | None = None,
                 backfill_minutes: int | None = None, backfill_since_open: bool = False) -> None:
        _check_limits(minutes, backfill_minutes, backfill_since_open)
        self.args = args
        self.conn = conn
        self.md = md
        self.say = say
        self.files = files
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.listener = listener
        self.on_pass = on_pass
        self.minutes: int = args.minutes if minutes is None else minutes
        self.since_open = backfill_since_open
        self.backfill_minutes: int | None = (
            None if backfill_since_open
            else args.backfill_minutes if backfill_minutes is None else backfill_minutes)
        self.syms = [_Sym(name) for name in args.symbols]
        self.server0: int | None = None
        self.clock0 = 0.0
        self.lost_noted = False
        self.lost_at_end = False
        self.closed = False
        # [start, end, kind] in server time; kind "loss" (1100) or a data farm ("market", "historical");
        # end is inf while the outage lasts
        self.outages: list[list[Any]] = []
        self.last_request: float | None = None  # monotonic clock of the last history request
        self._general_seen: dict[int, events.IbError] = {}
        self._stop_reason: str | None = None  # set by `request_stop`
        self.end_cause = ""  # why `run` ended, set once it has (see `end_cause` in the module text)
        # the market calendar's table for this run: empty, or today's close as IB reports it when that
        # differs from the calendar (`_cross_check`); read by `in_session` and `session_end`
        self.calendar_extra: dict[date, int | None] = {}

    # -- time -----------------------------------------------------------------------------

    def in_session(self, epoch: float) -> bool:
        """True in the regular session of that moment's New York day: 09:30 up to the close by the
        market calendar (13:00 on a half day), never on a weekend or a holiday."""
        return _in_session(epoch, self.calendar_extra)

    def session_end(self, day: date) -> int:
        """Epoch of the close of `day`'s regular session (16:00; 13:00 on a half day)."""
        return _session_end(day, self.calendar_extra)

    def server_now(self) -> float:
        assert self.server0 is not None
        return self.server0 + (self.clock() - self.clock0)

    def _lost(self) -> bool:
        return self.conn.state is ConnState.LOST

    def _outage_kinds(self, bar_time: int) -> set[str]:
        """The kinds ("loss", "market", "historical") of the outages that overlapped the minute
        starting at `bar_time`."""
        return {kind for start, end, kind in self.outages if start < bar_time + 60 and end > bar_time}

    def _down(self) -> bool:
        """True while the connection is lost or a data farm is reported broken."""
        return self._lost() or any(o[1] == math.inf for o in self.outages)

    def first_backfills_over(self) -> bool:
        """True once every symbol's first backfill has been handled (its bars are in the sinks) or
        the symbol has stopped: a driver that polls an aligner starts after this."""
        return all(not s.first_backfill or s.phase == "stopped" for s in self.syms)

    def request_stop(self, reason: str = "") -> None:
        """Ask the loop to end: it does so at the end of the pass that is running (when called from
        the pass hook) or at the start of the next one. Requests are then cancelled, the sinks and
        the connection closed, exactly as at the end of the watch time. Idempotent: the first
        reason stays."""
        if self._stop_reason is None:
            self._stop_reason = reason or "stop requested"

    @property
    def stop_requested(self) -> bool:
        return self._stop_reason is not None

    def is_down(self) -> bool:
        """True while the connection is reported lost or a data farm is reported broken (nothing is
        closed by time then); read-only, any time."""
        return self._down()

    def stream_clock(self) -> StreamClock:
        """Server time and the minimum over the running symbols of (server time - lag bound):
        the clock every running symbol's closing by time has caught up with. `stream_now` is None
        while ANY running symbol has no measured lag bound (a symbol that is delayed and has shown
        no bar of today yet would otherwise be left behind: minutes would be released without it
        and its bars would arrive late); with no symbol running it is the server time."""
        now = self.server_now()
        running = [s for s in self.syms if s.phase != "stopped"]
        if not running:
            return StreamClock(now, now)
        bounds = [s.lag_bound for s in running if s.lag_bound is not None]
        if len(bounds) < len(running):
            return StreamClock(now, None)
        return StreamClock(now, now - max(bounds))

    def stream_states(self) -> dict[str, StreamState]:
        """Per symbol: lag bound, delayed flag, running or stopped (read-only, any time)."""
        return {s.name: StreamState(s.lag_bound, s.delayed, s.phase != "stopped") for s in self.syms}

    def drop_counts(self) -> dict[str, DropCounts]:
        """Per symbol, the counts the summary prints (a snapshot; read at any time)."""
        return {s.name: DropCounts(
            snapshot_only=s.snapshot_only, connection_loss=s.lost_not_written,
            farm_break=s.farm_not_written, stalled_stream=s.stale_not_written,
            rerequest_gap=s.gap_not_written, late_revisions=s.late, stale_rows=s.stale,
            bad_bars=s.bad, rerequests=s.rerequests) for s in self.syms}

    # -- the session ----------------------------------------------------------------------

    def run(self) -> int:
        code = EXIT_STOPPED
        self.end_cause = "error"  # until something says otherwise
        try:
            code = self._session()
        except KeyboardInterrupt:
            self.say()
            self.say("Interrupted (Ctrl-C). Requests are cancelled and the connection is closed.")
            code = EXIT_INTERRUPTED
            self.end_cause = "interrupted"
        except _NotPaper:
            code = EXIT_NOT_PAPER  # said once, with the result, by `run_watch`
            self.end_cause = "not_paper"
        except _Closed:
            if self._not_paper():
                code = EXIT_NOT_PAPER  # the list failed and closed the connection: said by `run_watch`
                self.end_cause = "not_paper"
            else:
                self.say("STOPPED: the connection to TWS closed.")
                code = EXIT_STOPPED
                self.end_cause = "closed"
        except Exception as exc:  # noqa: BLE001 - reported, not a traceback
            self.say(f"Unexpected {type(exc).__name__}: {exc}")
            code = EXIT_STOPPED
            self.end_cause = "error"
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
            self.end_cause = "no_farm"
            return EXIT_STOPPED
        md.request_server_time()
        try:
            server = md.wait_server_time(SERVER_TIME_TIMEOUT)
        except DataLost:
            server = None
        if server is None:
            say(f"STOPPED: no server time from IB within {SERVER_TIME_TIMEOUT:g} s.")
            self.end_cause = "no_server_time"
            return EXIT_STOPPED
        self.server0 = int(server.timestamp())
        self.clock0 = self.clock()
        diff = (server - self.wall()).total_seconds()
        say(f"IB server time {server.astimezone(NY):%Y-%m-%d %H:%M:%S} New York; "
            f"IB minus this computer's clock = {diff:+.1f} s (IB time has 1 s resolution)")
        today = _ny(self.server0).date()
        if not calendar.is_trading_day(today):  # this computer's clock said otherwise: IB's decides
            say(f"STOPPED: by IB's server date, {trading_hours.not_a_trading_day_text(today)} (market "
                "calendar); nothing was requested.")
            self.end_cause = "not_trading_day"
            return EXIT_REFUSED
        for sym in self.syms:
            self._resolve(sym)
        if not self._cross_check(today):
            return EXIT_REFUSED
        self._request_all()
        return self._loop()

    def _cross_check(self, today: date) -> bool:
        """Data only: compare the market calendar's hours for `today` with the regular trading hours
        IB reports in the contract details every symbol's lookup already returned (no new request).
        Prints one line. On a disagreement it prints a warning and follows IB for today: IB says
        closed: nothing is requested (False); IB says another close: `calendar_extra` carries it and
        every time derived from the close follows it. Hours that cannot be read are not compared (one
        line), the calendar stands. Several symbols that disagree among themselves: the earliest
        close, and a closed answer, win."""
        answers = [(s, trading_hours.ib_hours_for(s.info.liquid_hours, s.info.time_zone_id, today))
                   for s in self.syms if s.info is not None]
        calendar_close = calendar.close_minute(today)
        shown = f"{calendar_close // 60:02d}:{calendar_close % 60:02d}"
        known = [(s, h) for s, h in answers if h.kind != trading_hours.UNKNOWN]
        if not known:
            reasons = sorted({h.why for _, h in answers}) or ["no symbol was looked up"]
            self.say(f"Trading hours: IB's hours for today were not compared with the market calendar "
                     f"({'; '.join(reasons)}); following the calendar: close {shown}.")
            return True
        if any(h.kind == trading_hours.CLOSED for _, h in known):
            names = ", ".join(s.name for s, h in known if h.kind == trading_hours.CLOSED)
            self.say(f"WARNING: IB reports no regular session today for {names}, but the market calendar "
                     f"has one (close {shown}). Following IB: nothing is requested.")
            self.end_cause = "not_trading_day"
            return False
        ib_close = min(h.close_minute for _, h in known if h.close_minute is not None)
        if len({h.close_minute for _, h in known}) > 1:
            self.say("WARNING: the symbols' trading hours differ from each other; following the earliest "
                     "close.")
        if ib_close == calendar_close:
            self.say(f"Trading hours: IB's hours for today agree with the market calendar (close {shown}).")
            return True
        reported = f"{ib_close // 60:02d}:{ib_close % 60:02d}"
        self.say(f"WARNING: IB reports the regular session closing at {reported} today, the market "
                 f"calendar says {shown}. Following IB for today: every time derived from the close "
                 f"({reported}) follows it.")
        self.calendar_extra[today] = ib_close
        return True

    def _resolve(self, sym: _Sym) -> None:
        try:
            sym.info = self.md.resolve_stock(sym.name, RESOLVE_TIMEOUT)
        except ResolveFailed as exc:
            self._stop(sym, f"cannot resolve: {exc}")
            return
        info = sym.info
        self.say(f"{sym.name}: conId {info.con_id}, primary exchange {info.primary_exchange}, "
                 f"time zone {info.time_zone_id}")

    def _request(self, sym: _Sym) -> str:
        """Send one history request, at least `REQUEST_SPACING` after the previous one (any symbol).
        Returns the duration asked for."""
        if self.last_request is not None:
            wait = REQUEST_SPACING - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        # IB's backfill is its snapshot at the time the request was sent: note that time now
        sym.requested_server = self.server_now()
        if self.since_open:
            duration = since_open_duration(sym.requested_server)
        else:
            assert self.backfill_minutes is not None
            duration = f"{self.backfill_minutes * 60} S"
        sent = self.clock()
        sym.req_id = self.md.request_history(sym.info.contract(), None, duration, use_rth=True,
                                             keep_up_to_date=True)
        sym.cancelled = False
        sym.requested_at = self.last_request = sent
        sym.phase = "backfill"
        return duration

    def _request_all(self) -> None:
        for sym in self.syms:
            if sym.info is None:
                continue
            try:
                duration = self._request(sym)
            except NotConnected as exc:
                raise _Closed from exc
            if self.since_open:
                self.say(f"{sym.name}: requested live bars with the backfill since the open "
                         f"({duration})")
            else:
                self.say(f"{sym.name}: requested live bars with {self.backfill_minutes} minutes "
                         "of backfill")

    def _loop(self) -> int:
        deadline = self.clock() + self.minutes * 60
        while self.clock() < deadline and self._stop_reason is None:
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
                    self._assess(sym)
                    self._check_no_data(sym)
                    self._settle(sym)
            if self.on_pass is not None:
                self.on_pass(self.stream_clock())  # after this pass's bars and notifications
            if self._stop_reason is not None:
                break  # asked to stop by the hook: no idle sleep, the end is handled below
            if not progressed:
                self.sleep(IDLE_SLEEP)
        self.lost_at_end = self.conn.state is ConnState.LOST
        self.say()
        if self._stop_reason is not None:
            self.say(f"Stopped on the driver's request ({self._stop_reason}).")
            self.end_cause = "stop_requested"
        elif self.clock() >= deadline:
            self.say(f"Watch time over ({self.minutes} minutes).")
            self.end_cause = "deadline"
        else:
            self.say("No symbol is streaming any more.")
            self.end_cause = "idle"
        if self.lost_at_end:
            self.end_cause = "lost"
            self.say("The connection to IB is still lost (code 1100).")
            return EXIT_STOPPED
        for sym in self.syms:  # the end of the watch may be what completes the 120 s
            self._check_no_data(sym)
        no_data = [s for s in self.syms if self._no_data(s)]
        failed = any(s.phase in ("stopped", "backfill") or (not s.had_bars and s not in no_data)
                     for s in self.syms)
        if failed:
            return EXIT_FAILED
        return EXIT_NO_DATA if no_data else EXIT_OK

    # -- reading ----------------------------------------------------------------------------

    def _open_outage(self, kind: str) -> bool:
        """Start an outage of `kind` unless one is already open; True if one was started."""
        if any(o[2] == kind and o[1] == math.inf for o in self.outages):
            return False
        self.outages.append([self.server_now(), math.inf, kind])
        return True

    def _close_outages(self, kinds: Sequence[str]) -> None:
        for outage in self.outages:
            if outage[2] in kinds and outage[1] == math.inf:
                outage[1] = self.server_now()

    def _not_paper(self) -> bool:
        """In paper mode: True when `paper_verified` is not True and the connection was not merely
        closed (see `cli.closed_without_rejection`: a failing list, or any doubt, is not paper)."""
        return (getattr(self.args, "paper", False) is True and self.conn.paper_verified is not True
                and not closed_without_rejection(self.conn))

    def _check_state(self) -> None:
        if self._not_paper():
            raise _NotPaper  # before anything else: a failing account list ends the watch
        state = self.conn.state
        if state is ConnState.CLOSED:
            raise _Closed
        if state is ConnState.LOST and not self.lost_noted:
            self.lost_noted = True
            self._open_outage("loss")
            self.say("IB connectivity LOST (code 1100): waiting for it to be restored; "
                     "the watch time keeps running; no bar is closed by time until it is restored.")
        elif state is ConnState.READY and self.lost_noted:
            self.lost_noted = False
            self._close_outages(["loss"])
            self.say("IB connectivity restored.")

    def _show_general(self) -> None:
        for event in self.md.general_events:
            if isinstance(event, events.IbError) and id(event) not in self._general_seen:
                self._general_seen[id(event)] = event  # keeps the object, so its id is not reused
                notice = 2100 <= event.code < 2200 or event.code in CONNECTION_NOTICES
                kind = "notice" if notice else "error"
                self.say(f"IB {kind} {event.code}: {event.message}")
                self._farm_notice(event.code)

    def _farm_notice(self, code: int) -> None:
        """A broken data farm (2103 market data, 2105 historical) is an outage like a lost connection
        until its OK notice (2104, 2106) or a restore (1101, 1102): nothing closes by time, and a
        minute that overlapped it is not written."""
        if code in FARM_BROKEN:
            if self._open_outage(FARM_BROKEN[code]):
                self.say(f"IB data farm break ({code}): no bar is closed by time until it is OK again.")
        elif code in FARM_OK:
            self._close_outages([FARM_OK[code]])
        elif code in (1101, 1102):
            self._close_outages(list(FARM_BROKEN.values()))

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
            if sym.live_since is None:
                sym.live_since = self.server_now()
            if sym.phase == "backfill":  # not "stopped" (an unwritable file stops it inside)
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

    def _stop(self, sym: _Sym, reason: str, line: str | None = None) -> None:
        """Stop `sym` only: cancel its request once, say so in one line (`line`, else the standard one)."""
        if sym.phase == "stopped":
            return
        self._cancel(sym)
        sym.phase = "stopped"
        sym.stop_reason = reason
        self.say(line or f"{sym.name}: stopped watching ({reason}); the other symbols continue")
        if self.listener is not None:
            self.listener.stopped(sym.name)

    # -- bars -------------------------------------------------------------------------------

    def _convert(self, sym: _Sym, raw: events.HistoricalBar) -> Bar | None:
        try:
            return bar_from_ib(raw)
        except BadBar as exc:
            sym.bad += 1
            self.say(f"{sym.name}: bad bar skipped: {exc}")
            return None

    def _observe(self, sym: _Sym, bar: Bar, at: float) -> float | None:
        """Note that `bar` existed at server time `at`: the data clock is then at least its start, so
        `at - newest` bounds the lag of the stream from above. The smallest bound seen is kept and
        judges every close by time; the bound of this one observation is returned (None: not today)."""
        if _ny(bar.time).date() != _ny(at).date():
            return None
        if sym.newest is None or bar.time > sym.newest:
            sym.newest = bar.time
        bound = at - sym.newest
        if sym.lag_bound is None or bound < sym.lag_bound:
            sym.lag_bound = bound
        return bound

    def _assess(self, sym: _Sym) -> None:
        """Say once, when the regular session is over, what to expect."""
        now = self.server_now()
        if self.in_session(now):
            sym.closed_noted = False
            return
        if not sym.closed_noted:
            sym.closed_noted = True
            if sym.delayed:
                self.say(f"{sym.name}: regular session ended; delayed bars continue for about "
                         f"{max(1, round(sym.lag / 60))} minutes")
            else:
                self.say(f"{sym.name}: market closed: no new bars expected")

    def _no_data(self, sym: _Sym) -> bool:
        """True when `sym` is streaming but has received no bar of today's session and no live update
        for at least `NO_DATA_AFTER` s of the regular session since its backfill ended (the clock
        starts at 09:30 for a pre-open start). It is the state now: data arriving later clears it.

        The clock does not run while the connection is lost or a data farm is broken, and nothing is
        declared while such an outage is open: an outage is not a missing subscription. Updates of
        an earlier date are not data of today, but while any update keeps arriving the stream is alive
        and the symbol is not declared: the 120 s run from the last update (of any date), so a stream
        that goes silent without ever showing a bar of today is declared after 120 s."""
        if sym.phase != "live" or sym.live_since is None or sym.newest is not None:
            return False
        if self._down():
            return False
        since = max(sym.live_since, sym.last_update_at or 0.0)
        return (_session_exposure(since, self.server_now(), self.outages, self.calendar_extra)
                >= NO_DATA_AFTER)

    def _check_no_data(self, sym: _Sym) -> None:
        """Say once, as soon as it is established, that `sym` has received no data."""
        if sym.no_data_noted or not self._no_data(sym):
            return
        sym.no_data_noted = True
        self.say(f"{sym.name}: no data received: no bar of today's session and no live update for "
                 f"{NO_DATA_AFTER:g} s of the session; {NO_DATA_HINT}")

    def _label(self, sym: _Sym, observed: float | None, now: float) -> None:
        """The DELAYED label (it never changes how bars are judged; the lag bound does that): declared
        when two live updates at least `LABEL_CONFIRM` s apart both show a lag over the threshold, or
        when the backfill showed it and one live update confirms it; withdrawn by a current update."""
        if observed is None:
            return
        if observed > DELAY_THRESHOLD:
            if sym.over_since is None:
                sym.over_since = now
            confirmed = sym.backfill_over or now - sym.over_since >= LABEL_CONFIRM
            if confirmed and not sym.delayed and self.in_session(now):
                sym.delayed = True
                self.say(f"{sym.name}: bars are about {max(1, round(sym.lag / 60))} minutes behind IB's "
                         "clock: this looks like delayed market data (the paper account may not share "
                         "the live account's real-time data)")
            return
        sym.over_since = None
        sym.backfill_over = False
        if sym.delayed:
            sym.delayed = False
            self.say(f"{sym.name}: bars are current again (at most {observed:.0f} s behind IB's clock): "
                     "treating the stream as live")

    def _not_written(self, sym: _Sym, bar: Bar, why: str) -> None:
        """A minute that is not known to be finished with final values: no row, one line."""
        if sym.last_emitted is None or bar.time > sym.last_emitted:
            sym.last_emitted = bar.time
        self.say(f"{sym.name} {_hm(bar.time)} not written: {why}")
        self._tell_decided(sym)

    def _tell_decided(self, sym: _Sym) -> None:
        """Tell the listener `decided(symbol, last_emitted)`: the newest minute of `sym` that was
        written or dropped. Nothing else is ever told, and only right after `last_emitted` moved;
        see "What `decided` promises" in the module docstring for why no row can follow for a
        minute at or before it."""
        if self.listener is not None and sym.last_emitted is not None:
            self.listener.decided(sym.name, sym.last_emitted)

    def _backfill(self, sym: _Sym, raw: Sequence[events.HistoricalBar]) -> None:
        sent = sym.requested_server  # IB's snapshot is at least this old, not as old as `now`
        now = self.server_now()
        by_time: dict[int, Bar] = {}
        for item in raw:
            bar = self._convert(sym, item)
            if bar is not None:
                by_time[bar.time] = bar
        everything = [by_time[t] for t in sorted(by_time)]
        today = _ny(sent).date()
        bars = [b for b in everything if _ny(b.time).date() >= today]
        older = len(everything) - len(bars)
        for bar in bars:
            self._observe(sym, bar, sent)
        if bars:
            sym.backfill_over = sent - bars[-1].time > DELAY_THRESHOLD
        self._assess(sym)
        # the data clock when the request was sent: the snapshot's newest bar is forming unless the
        # smallest lag bound seen so far (live updates before a re-request) says it had ended
        judged = None if sym.lag_bound is None else sent - sym.lag_bound
        initial = sym.first_backfill
        sym.first_backfill = False
        added = 0
        for index, bar in enumerate(bars):
            if sym.phase == "stopped":
                break
            if sym.last_emitted is not None and bar.time <= sym.last_emitted:
                self._compare(sym, bar)
                continue
            forming = sym.forming
            if forming is not None and forming.bar.time < bar.time:
                sym.forming = None  # older than the snapshot's window: its final values are not here
                sym.gap_not_written += 1
                self._not_written(sym, forming.bar, "its final values were not in the re-requested "
                                  "backfill")
                forming = None
            last = index == len(bars) - 1
            if not last or (judged is not None and bar.time + 60 + CLOSE_GRACE <= judged):
                if forming is not None and forming.bar.time == bar.time:
                    sym.forming = None  # the backfill carries the final version
                if self._emit(sym, bar, now, updates=None, first_seen=None,
                              how="backfill" if initial else "backfilled"):
                    added += 1
            elif forming is None:
                sym.forming = _Forming(bar, 0, None)
            elif forming.bar.time == bar.time:
                forming.bar = bar  # the snapshot, not the earlier live values, until a live update
                forming.updates = 0
                forming.first_seen = None
        if older:
            sym.previous_session += older
            self.say(f"{sym.name}: backfill holds {older} bars of the previous session: not written")
        if bars:
            text = f"{sym.name}: {'backfill' if initial else 're-requested backfill'}: {len(bars)} bars, "
            text += f"first {_hm(bars[0].time)}, last {_hm(bars[-1].time)}"
            text += f"; {added} new closed bars written"
            if sym.forming is not None and sym.forming.updates == 0:
                text += f"; the {_hm(sym.forming.bar.time)} bar is still forming"
        else:
            text = f"{sym.name}: {'backfill' if initial else 're-requested backfill'}: 0 bars"
        self.say(text)
        if sym.newest is None and self.in_session(now) and not sym.nothing_yet_noted:
            sym.nothing_yet_noted = True
            self.say(f"{sym.name}: no bars of today's session yet; with delayed data the first bars "
                     "appear about 15 minutes after the open")

    def _compare(self, sym: _Sym, bar: Bar) -> None:
        """A backfill bar of a minute this run already emitted: say if it differs from what is in the file."""
        earlier = sym.emitted_bars.get(bar.time)
        if earlier is None:
            return
        was = earlier[0]
        if (was.open, was.high, was.low, was.close, was.volume) == (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            return
        sym.stale += 1
        self.say(f"{sym.name}: the re-requested backfill differs for {_hm(bar.time)}: the file keeps the "
                 f"earlier values; written {_ohlc(was)} volume {was.volume}; newer {_ohlc(bar)} "
                 f"volume {bar.volume}; this row may be stale")

    def _update(self, sym: _Sym, raw: events.HistoricalBar) -> None:
        now = self.server_now()
        sym.last_update_at = now
        bar = self._convert(sym, raw)
        if bar is None:
            return
        if _ny(bar.time).date() < _ny(now).date():
            sym.previous_updates += 1
            if sym.previous_updates == 1:
                self.say(f"{sym.name}: update for {_hm(bar.time)} of the previous session ignored: "
                         "not written")
            return
        self._label(sym, self._observe(sym, bar, now), now)
        self._assess(sym)
        forming = sym.forming
        if sym.last_emitted is not None and bar.time <= sym.last_emitted:
            self._late(sym, bar, f"already emitted up to {_hm(sym.last_emitted)}")
        elif forming is None:
            sym.forming = _Forming(bar, 1, now, now)
        elif bar.time == forming.bar.time:
            forming.bar = bar
            forming.updates += 1
            forming.last_at = now
        elif bar.time < forming.bar.time:
            self._late(sym, bar, f"the forming minute is {_hm(forming.bar.time)}")
        else:
            self._close_forming(sym, now, "later", bar.time)
            sym.forming = _Forming(bar, 1, now, now)
        self._show_forming(sym)

    def _late(self, sym: _Sym, bar: Bar, why: str) -> None:
        sym.late += 1
        earlier = sym.emitted_bars.get(bar.time)
        if earlier is None:
            self.say(f"{sym.name}: late revision for {_hm(bar.time)} ({why}); counted, "
                     "nothing already emitted is rewritten")
            return
        was, wrote = earlier
        if (was.open, was.high, was.low, was.close, was.volume) == (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            self.say(f"{sym.name}: late revision for {_hm(bar.time)} ({why}); identical to the bar "
                     "already emitted; counted")
            return
        sym.stale += 1
        label = "written" if wrote else "emitted, the file already had that minute from an earlier run"
        self.say(f"{sym.name}: late revision for {_hm(bar.time)} ({why}): the file keeps the earlier "
                 f"values; {label} {_ohlc(was)} volume {was.volume}; newer {_ohlc(bar)} "
                 f"volume {bar.volume}; this row may be stale; counted, nothing already emitted "
                 "is rewritten")

    def _show_forming(self, sym: _Sym) -> None:
        forming = sym.forming
        if self.args.quiet or forming is None or forming.updates == 0 or sym.phase == "stopped":
            return
        now = self.clock()
        if now - sym.last_forming_line < FORMING_EVERY:
            return
        sym.last_forming_line = now
        bar = forming.bar
        server = datetime.fromtimestamp(self.server_now(), tz=UTC).astimezone(NY)
        self.say(f"{server:%H:%M:%S}  {sym.name}  forming {_hm(bar.time)}  {_ohlc(bar)}  "
                 f"volume {bar.volume}  (update {forming.updates}){sym.tag}")

    def _close_forming(self, sym: _Sym, now: float, how: str, closer: int | None = None) -> None:
        """Close the forming bar; `closer` is the minute of the update that closed it, if any."""
        forming = sym.forming
        assert forming is not None
        sym.forming = None
        self._emit(sym, forming.bar, now, updates=forming.updates, first_seen=forming.first_seen,
                   how=how, closer=closer, last_at=forming.last_at)

    def _settle(self, sym: _Sym) -> None:
        """Close the forming bar by time. Never while the connection is lost, while a re-requested
        backfill is outstanding, or before any bar of today was seen. The clock is always IB's server
        time less the smallest lag bound seen for this symbol; it must be 90 s past the bar's end, and
        a bar that ends in the last two minutes of the session also waits for 16:20 server time."""
        forming = sym.forming
        if forming is None or sym.phase != "live" or self._down() or sym.lag_bound is None:
            return
        bar = forming.bar
        now = self.server_now()
        if now - sym.lag_bound < bar.time + 60 + SETTLE_SECONDS:
            return
        end_of_session = self.session_end(_ny(bar.time).date())
        if bar.time + 60 + SETTLE_SECONDS > end_of_session and now < end_of_session + SESSION_GRACE:
            return
        self._close_forming(sym, now, "time")

    def _emit(self, sym: _Sym, bar: Bar, now: float, *, updates: int | None, first_seen: float | None,
              how: str, closer: int | None = None, last_at: float | None = None) -> bool:
        """Write and print one closed bar. True if a row was appended to its file.

        A bar closed from live data is written only if it is known finished with final values: it must
        have had a live update after the backfill snapshot (`updates` above 0), its minute must not
        have overlapped a connection loss or a data farm break, and, closed by time, its last live
        update (arrived at server time `last_at`) must be within `STALE_UPDATE` s of the minute's end
        on the stream's clock. Otherwise nothing is written and one line says why."""
        if sym.phase == "stopped":
            return False
        if how in ("later", "time"):
            if not updates:
                sym.snapshot_only += 1
                self._not_written(sym, bar, "seen only in the backfill snapshot (no live update)")
                return False
            kinds = self._outage_kinds(bar.time)
            if kinds:
                if "loss" in kinds:
                    sym.lost_not_written += 1
                    self._not_written(sym, bar, "spanned a connection loss")
                else:
                    sym.farm_not_written += 1
                    self._not_written(sym, bar, "spanned a data farm break")
                return False
            if how == "time" and last_at is not None and sym.lag_bound is not None:
                gap = bar.time + 60 - (last_at - sym.lag_bound)  # by the stream's clock
                if gap > STALE_UPDATE:
                    sym.stale_not_written += 1
                    self._not_written(sym, bar, f"last update came {gap:.0f} s before the minute "
                                                "ended (stream stalled?)")
                    return False
        try:
            written = _append(self.files, sym.name, bar, how)
        except LiveFileError as exc:
            advice = ("close it in other programs (for example Excel) and run again" if exc.locked
                      else "check or move the file, then run again")
            self._stop(sym, "file not writable",
                       f"{sym.name}: {exc}: {advice}; {sym.name} stopped, the other symbols continue")
            return False
        sym.emitted.append(bar.time)
        sym.emitted_bars[bar.time] = (bar, written)
        sym.last_emitted = bar.time
        sym.last_bar = bar
        self._tell_decided(sym)  # after every sink has the row
        head = f"{_hm(bar.time)}  {sym.name}  {_ohlc(bar)}  volume {bar.volume}"
        tag = sym.tag
        if how == "backfill":
            sym.backfill_written += int(written)
            return written
        if how == "backfilled":
            sym.backfilled_late += 1
            self.say(f"{head}  (backfilled{'' if written else ', already in the file'}){tag}")
            return written
        kept = "" if written else " (already in the file)"
        if how == "time":
            sym.closed_by_time += 1
            self.say(f"{head}  (closed by time: no later update arrived within {SETTLE_SECONDS} s "
                     f"of the minute's end; updates {updates}){tag}{kept}")
            return written
        sym.live_closed += 1
        if first_seen is None or updates is None:
            self.say(f"{head}  (updates {updates}, first update not seen: the bar was forming "
                     f"at the backfill){tag}{kept}")
            return written
        if sym.delayed:  # offsets against IB's clock mean nothing for delayed bars
            sym.update_counts.append(updates)
            self.say(f"{head}  (updates {updates}, closed by a later update){tag}{kept}")
            return written
        first_delay = first_seen - bar.time
        sym.update_counts.append(updates)
        sym.first_delays.append(first_delay)
        skipped = 0 if closer is None else max(0, (closer - bar.time) // 60 - 1)
        if skipped:
            sym.skipped_closes += 1
            ended = (f"closed by the {_hm(closer)} update, after {skipped} minute"
                     f"{'' if skipped == 1 else 's'} without a bar")
        else:
            close_delay = now - (bar.time + 60)
            ended = f"closed {close_delay:+.1f} s after the minute ended"
            sym.close_delays.append(close_delay)
        self.say(f"{head}  (updates {updates}, first seen {first_delay:+.1f} s after the minute "
                 f"started, {ended}){kept}")
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
        if self.listener is not None:
            for sym in self.syms:  # nothing more will come for any symbol still running
                if sym.phase != "stopped":
                    try:
                        self.listener.stopped(sym.name)
                    except Exception as exc:  # noqa: BLE001
                        self.say(f"Telling the listener that {sym.name} ended raised "
                                 f"{type(exc).__name__}: {exc}")

    def _summary(self) -> None:
        say = self.say
        say()
        say("Summary")
        for sym in self.syms:
            if self._no_data(sym):
                state = f"no data received ({NO_DATA_HINT})"
            elif sym.phase == "live":
                state = "streaming"
            elif sym.phase == "backfill":
                state = ("NOT streaming at the end: a request was outstanding (a backfill was still "
                         "awaited), so no bar was closed after it")
            else:
                state = f"stopped: {sym.stop_reason}" if sym.phase == "stopped" else "never requested"
            say(f"  {sym.name}: {state}{sym.tag}")
            if sym.delayed:
                say(f"    stream: DELAYED, bars about {max(1, round(sym.lag / 60))} min "
                    f"({sym.lag:.0f} s at most) behind IB's clock; files are correct but late")
            elif sym.lag_bound is not None and sym.lag_bound > DELAY_THRESHOLD:
                say(f"    stream: not labelled delayed, but bars were up to {sym.lag_bound:.0f} s behind "
                    "IB's clock (outside the session, or not confirmed by live updates)")
            elif sym.lag_bound is not None:
                say(f"    stream: LIVE, bars at most {sym.lag_bound:.0f} s behind IB's clock")
            else:
                say("    stream: lag not measured (no bar of today seen)")
            say(f"    closed bars emitted live: {sym.live_closed} (closed by time {sym.closed_by_time}); "
                f"backfill bars written: {sym.backfill_written}; backfilled after an outage: "
                f"{sym.backfilled_late}")
            say(f"    minutes not written: snapshot only {sym.snapshot_only}, connection loss "
                f"{sym.lost_not_written}, data farm break {sym.farm_not_written}, stalled stream "
                f"{sym.stale_not_written}, re-request gap {sym.gap_not_written}; closes left out of "
                f"the close-delay statistics: skipped minute "
                f"{sym.skipped_closes}{', delayed stream' if sym.delayed else ''}")
            if sym.previous_updates:
                say(f"    updates of the previous session ignored: {sym.previous_updates}")
            if sym.previous_session:
                say(f"    backfill bars of the previous session, not written: {sym.previous_session}")
            missing = _missing_minutes(sym.emitted)
            if missing:
                shown = ", ".join(missing[:MAX_MISSING_SHOWN])
                extra = len(missing) - MAX_MISSING_SHOWN
                more = f" ... and {extra} more" if extra > 0 else ""
                say(f"    missing minutes inside the span ({len(missing)}): {shown}{more}")
            else:
                say("    missing minutes inside the span: none")
            say(f"    late revisions: {sym.late}; rows that may be stale: {sym.stale}; "
                f"bad bars skipped: {sym.bad}; re-requests after data loss: {sym.rerequests}")
            say(f"    updates per bar (min / median / max): {_mmm(sym.update_counts, 'g')}")
            say(f"    first update after the minute started, s (min / median / max): "
                f"{_mmm(sym.first_delays)}")
            say(f"    closed after the minute ended, s (min / median / max): {_mmm(sym.close_delays)}")
            if sym.last_bar is not None:
                bar = sym.last_bar
                say(f"    last closed bar: {_hm(bar.time)}  {_ohlc(bar)}  volume {bar.volume}")
            else:
                say("    last closed bar: none")
            if sym.forming is not None and sym.phase != "stopped":
                say(f"    still forming, not written: {_hm(sym.forming.bar.time)}  "
                    f"{_ohlc(sym.forming.bar)}  volume {sym.forming.bar.volume}")
            for path in self.files.written.get(sym.name, []):
                say(f"    file: {path}")
        open_farms = sorted({o[2] for o in self.outages if o[1] == math.inf and o[2] != "loss"})
        if open_farms:
            say(f"A data farm break ({', '.join(open_farms)}) was never reported OK again: nothing was "
                "closed by time after it.")
        say(OFFSET_NOTE)
        say(COMPARE_NOTE)


_Watcher = Watcher  # the name it had before it was public


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
    EXIT_FAILED: "finished, but a symbol stopped, had no stream at the end or delivered no bars",
    EXIT_REFUSED: "refused, nothing was watched",
    EXIT_NO_CONNECTION: "could not connect",
    EXIT_STOPPED: "stopped early (lost or closed connection, or an unexpected error)",
    EXIT_INTERRUPTED: "interrupted with Ctrl-C",
    EXIT_NOT_PAPER: NOT_PAPER_LABEL,
    EXIT_NO_DATA: "watched to the end, but no data received for at least one symbol",
}
ADVICE_CLIENT_ID = ("Client id in use: another API client is connected with this id. Close it or pass "
                    "another --client-id.")
ADVICE_CONNECT = ("Cannot connect: check the port and that the API is enabled in TWS "
                  "(Global Configuration, API, Settings).")


def _connect(say: _Say, conn: IbConnection, args: argparse.Namespace, advice: list[str],
             paper: bool = False) -> int | None:
    """None when connected, else the exit code (and the advice for the cause)."""
    say(f"Connecting to TWS at {args.host}:{args.port} (client id {args.client_id}, 2 attempts, "
        f"{args.connect_timeout:g} s each; TWS may ask you to accept the connection) ...")
    try:
        conn.open()
    except (BrokerPortRefused, ClientIdRefused, BrokerConnectionRefusedHere) as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except NotPaperSession:
        if not paper:
            raise
        say(NOT_PAPER_LINE)  # never the exception text, never retried
        return EXIT_NOT_PAPER
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
    seams: WatchSeams | None = None,
) -> int:
    """Connect, watch, summarise; always cancels every request and closes the connection and recorder.

    `args` carries `symbols`, `minutes`, `backfill_minutes`, `quiet`, `out` (Path), `record` (Path or
    None), `private`, `host`, `port`, `client_id` and `connect_timeout`. `clock` is monotonic
    seconds, `sleep` an interruptible sleep, `wall` this computer's clock as an aware datetime.
    `seams` is for a driver (module docstring); without it the run is the command's. Returns the
    exit code. The sinks of `seams` are closed with the live files, in every case.
    """
    say = _Say(out)
    seam = seams or WatchSeams()
    labels = dict(_LABELS)  # a mid-run not-paper rejection replaces the not-paper label
    files = LiveFiles(args.out, say)
    sink: ClosedBars = TeeBars(files, *seam.extra_sinks) if seam.extra_sinks else files
    try:
        recorder = recorder_factory(args)
    except (OSError, PathRefused) as exc:
        say(f"Cannot start the session recording: {type(exc).__name__}: {exc}")
        say(f"Result: {labels[EXIT_REFUSED]} (exit {EXIT_REFUSED})")
        try:
            sink.close()
        except Exception as close_exc:  # noqa: BLE001 - one line, never a traceback
            say(f"Closing the bar sinks raised {type(close_exc).__name__}: {close_exc}")
        return EXIT_REFUSED
    conn: IbConnection | None = None
    code = EXIT_STOPPED
    advice: list[str] = []
    not_connected = False
    paper = getattr(args, "paper", False) is True
    verified = False
    verified_at_start = False  # verified, then closed or lost without a failing list
    try:
        conn = connection_factory(args, [recorder] if recorder is not None else [])
        refused = _connect(say, conn, args, advice, paper)
        if refused is not None:
            code = refused
            not_connected = True
        elif paper and not paper_session_ok(conn, say):  # before the market data client, before any request
            code = EXIT_NOT_PAPER
            not_connected = True
        else:
            verified = paper
            md = market_data_factory(conn)
            watcher = Watcher(args, conn, md, say, sink, clock, sleep, wall, listener=seam.listener,
                              on_pass=seam.on_pass, minutes=seam.minutes,
                              backfill_minutes=seam.backfill_minutes,
                              backfill_since_open=seam.backfill_since_open)
            if seam.on_watcher is not None:
                seam.on_watcher(watcher)
            code = watcher.run()
    except KeyboardInterrupt:
        say()
        say("Interrupted (Ctrl-C).")
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - reported with the exit code, not a traceback
        say(f"Unexpected {type(exc).__name__}: {exc}")
        code = EXIT_STOPPED
    finally:
        said_not_paper = False
        if verified and conn is not None and conn.paper_verified is not True:
            if closed_without_rejection(conn):
                verified_at_start = True  # closed or lost, no failing list: the watch's own outcome stands
            else:
                # the account list failed after READY: whatever the watch saw, this is the result
                mid_line = mid_run_not_paper_line(conn)
                say(mid_line)
                labels[EXIT_NOT_PAPER] = mid_run_not_paper_label(mid_line)
                code = EXIT_NOT_PAPER
                advice.clear()
                said_not_paper = True
        try:
            sink.close()
        except Exception as exc:  # noqa: BLE001 - the live files never raise; a driver's sink might
            say(f"Closing the bar sinks raised {type(exc).__name__}: {exc}")
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                say(f"Closing the connection raised {type(exc).__name__}: {exc}")
            if verified and not said_not_paper and conn.paper_rejected:
                # a failing list delivered while close() joined the reader: this is the result
                say(NOT_PAPER_MID_LINE)
                labels[EXIT_NOT_PAPER] = NOT_PAPER_MID_LABEL
                code = EXIT_NOT_PAPER
                advice.clear()
                verified_at_start = False
        if recorder is not None:
            recorder.close()
    for line in advice:
        say(f"What to do: {line}")
    if args.record is not None and recorder is not None:
        if not_connected and _header_only(args.record):
            try:
                args.record.unlink(missing_ok=True)  # made by this run; nothing was recorded but the header
            except OSError as exc:
                say(f"Could not delete the empty recording {args.record}: {type(exc).__name__}")
        else:
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
    say(f"Result: {labels[code]} (exit {code})"
        + ("; paper account verified at start" if verified_at_start else ""))
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
    if args.paper:
        refusal = paper_refusal(args.host, args.port, variables)  # before anything else is built
        if refusal is not None:
            say(refusal)
            return EXIT_REFUSED
        say(PAPER_NO_PROMPT_LINE if args.no_prompt else PAPER_MODE_LINE)
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
    day = today or wall_clock().astimezone(NY).date()  # this computer's clock; IB's decides once connected
    if not calendar.is_trading_day(day):
        say(f"Refused: {trading_hours.not_a_trading_day_text(day)}. Nothing was started.")
        return EXIT_REFUSED
    if calendar.is_half_day(day):
        close = calendar.close_minute(day)
        say(f"Note: {day.isoformat()} is a half day by the market calendar: the regular session ends "
            f"at {close // 60:02d}:{close % 60:02d} New York (the last bars of the session are held "
            f"for {SESSION_GRACE // 60} minutes after that).")
    folders = [args.out] + ([args.record.parent] if args.record is not None else [])
    try:
        created = make_folders(folders)  # the last refusal: the folders must be creatable
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    try:
        if args.no_prompt:  # `parse_args` accepted it only with --paper; nothing is read from stdin
            say(describe_session(args))
        else:
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
                require_paper=a.paper,
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
