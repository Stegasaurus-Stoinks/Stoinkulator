"""The `fetch-bars` and `probe-data` commands: owner-run, they talk to TWS or IB Gateway.

    python -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA --start 2024-08-16 --end 2024-08-26 --port 7496
    python -m MainStoinker probe-data --port 7496 --symbols AAPL,MSFT

`probe-data` (what market data this account gets) shares everything here: the checks below,
the console gate, the scrubbed printer, the recording and the session (`run_session`). Its
steps live in `MainStoinker/ib/probe.py`, loaded only when the command runs.

Only the owner runs it, in a real terminal. It is built on `IbConnection` and
`IbMarketData` (a data-only client: it cannot send anything but data requests) and
`fetch` (the driver, which writes one CSV file per symbol per month and resumes).

Checks, in this sequence, before any client or connection is built
1. arguments (`--help` and argument errors end here);
2. the paths (an output or recording path inside the repository or a OneDrive folder, or that
   is a file or lies under one, is refused; the default folders come from `USERPROFILE`, else
   `HOME`, in the environment mapping given to `main`, never from the machine's own idea of
   "home", and are refused when neither is set);
3. the dates (a range with no weekday, or starting after today in New York by this computer's
   clock, is refused as a pre-check; IB's server time decides after connecting);
4. the port guard (`check_broker_port`): paper ports always, a live port only if the
   OWNER has set `STOINK_ALLOW_LIVE_PORT=1` in their own terminal; this module never sets it;
5. the folder layout, then the output and recording folders are created (the last refusal);
   those this run made and left empty are removed again when it ends;
6. the console gate: stdin must be an interactive terminal and the owner types the port
   number. There is no option that skips it.

The environment is read in `main` only. Every printed line passes `scrub`: anything
shaped like an account id becomes `ACCOUNT`, and `ManagedAccounts` is never printed.

The session recording (on by default, a new file under `<home>/Stoinkulator/recordings`)
is written by `SessionRecorder`, which removes account numbers and the private words
built here (user name, machine name, home folder, cloud-sync folder names). After the run
the file is checked with `scan_recording`; read it yourself before it is ever committed.

Exit codes: see `EXIT_CODES_TEXT` (shown by `--help`).
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.engine.feed import read_symbol_bars
from MainStoinker.engine.store import is_cloud_synced
from MainStoinker.ib.connection import (
    MAX_CLIENT_ID,
    REFUSED_CLIENT_IDS,
    BrokerConnectionRefusedHere,
    ClientIdInUse,
    ClientIdRefused,
    ConnectFailed,
    IbConnection,
)
from MainStoinker.ib.fetch import (
    FetchEstimate,
    FetchResult,
    FetchSetupError,
    ProgressEvent,
    check_layouts,
    estimate_duration,
    fetch,
)
from MainStoinker.ib.history import DATA_ZONE, check_symbol
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.recorder import Finding, SessionRecorder, scan_recording
from MainStoinker.Util.broker_guard import ALLOW_LIVE_ENV, BrokerPortRefused, check_broker_port

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_NO_CONNECTION = 3
EXIT_STOPPED = 4
EXIT_INTERRUPTED = 130

EXIT_CODES_TEXT = """\
exit codes:
  0    complete: every day of every symbol is ok, no data (holiday) or already done
  1    finished, but some day failed or a symbol was not fetched (manifest, contract)
  2    refused: console gate, port guard, bad arguments, unusable path, bad folder layout
  3    could not connect to TWS or IB Gateway
  4    stopped early: connection lost, pacing violation twice, or a fatal fault;
       what was finished is on disk, run the same command again to resume
  130  interrupted with Ctrl-C (the connection and the recording were closed)
"""

PROBE_EXIT_CODES_TEXT = """\
exit codes:
  0    every step was done (delayed data is still a success)
  1    connected, but a step failed (including NO data for a symbol)
  2    refused: console gate, port guard, bad arguments, unusable path
  3    could not connect to TWS or IB Gateway
  130  interrupted with Ctrl-C (the connection and the recording were closed)
"""
FETCH_BARS = "fetch-bars"
PROBE_DATA = "probe-data"
PROBE_MAX_SYMBOLS = 5
PROBE_QUOTE_SECONDS = 20.0
PROBE_MAX_QUOTE_SECONDS = 120
DEFAULT_CLIENT_ID = 21
DEFAULT_CONNECT_TIMEOUT = 30.0
MAX_CONNECT_TIMEOUT = 300.0
DEFAULT_SETTLE_HOURS = 3.0
MAX_SETTLE_HOURS = 24.0
MAX_SYMBOLS = 20
SHOWN_FINDINGS = 5
SECONDS_PER_SYMBOL_DAY = 10  # IB's pacing: one history request per 10 s; the estimate rule
VOLUME_NOTE = (
    "Volumes are stored exactly as IB sent them: shares only if the TWS API setting for US stock sizes "
    "in lots is UNTICKED (Global Configuration, API, Settings).\n"
    "Ticked, IB sends much smaller numbers that cannot be converted back: delete the symbol's folder "
    "under the output folder (or use a new --out) and run again; a rerun skips days already on disk.\n"
    "Check: a large stock's opening-minute volume is normally hundreds of thousands to millions of "
    "shares; tens of thousands means the setting is ticked."
)
PATHS_NOTE = "Note: this output shows local paths. Read it before sharing it."
ADVICE_LOST = ("Connection lost: check that TWS is running and logged in, then run the same command "
               "again to resume.")
ADVICE_CLIENT_ID = ("Client id in use: another API client is connected with this id. Close it or pass "
                    "another --client-id.")
ADVICE_CONNECT = ("Cannot connect: check the port and that the API is enabled in TWS "
                  "(Global Configuration, API, Settings).")
ADVICE_PACING = "Pacing stop: IB asked to slow down. Wait at least 10 minutes before running again."
ADVICE_FARM = ("No historical data farm: TWS is not connected to IB's history service yet. Wait a "
               "minute and retry.")
NO_DATA_YET_TEXT = "IB had no data for this day yet; it will be retried next run"

_ACCOUNT_SHAPE = re.compile(r"[A-Za-z]{1,2}[0-9]{5,}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ConnectionFactory = Callable[[Any, list[Any]], IbConnection]
MarketDataFactory = Callable[[IbConnection], IbMarketData]
RecorderFactory = Callable[[Any], SessionRecorder | None]


class PathRefused(ValueError):
    """An output or recording path is not allowed."""


def scrub(text: str) -> str:
    """Replace anything shaped like an account id (1-2 letters then 5+ digits) with ACCOUNT."""
    return _ACCOUNT_SHAPE.sub("ACCOUNT", text)


class _Say:
    """Prints scrubbed lines; nothing reaches the stream unscrubbed."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __call__(self, text: str = "") -> None:
        for line in (text.splitlines() or [""]):
            print(scrub(line), file=self._stream)
        self._stream.flush()


# -- arguments ----------------------------------------------------------------------------


def _iso_date(text: str) -> date:
    if not _DATE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, required=True, help="TWS or Gateway API port (no default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)


def _add_record_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--record", default=None, metavar="FILE",
                       help=r"session recording file, never overwritten (default: a new file in "
                            r"%%USERPROFILE%%\Stoinkulator\recordings)")
    group.add_argument("--no-record", action="store_true", help="do not record the session")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT,
                        help="seconds per connection attempt; TWS may show a dialog (default 30)")


def _check_connection_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1 to 65535")
    if not 1 <= args.client_id <= MAX_CLIENT_ID or args.client_id in REFUSED_CLIENT_IDS:
        parser.error(f"--client-id must be 1 to {MAX_CLIENT_ID} and not {sorted(REFUSED_CLIENT_IDS)}")
    if not 0 < args.connect_timeout <= MAX_CONNECT_TIMEOUT:
        parser.error(f"--connect-timeout must be above 0 and at most {MAX_CONNECT_TIMEOUT:g}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker fetch-bars",
        description=(
            "Owner-run: download 1-minute regular-hours bars through TWS or IB Gateway into "
            "one CSV file per symbol per month, resuming where an earlier run stopped. Sends "
            "data requests only. Needs a real terminal; you type the port number to confirm.\n\n"
            f"Time estimate: about {SECONDS_PER_SYMBOL_DAY} seconds per symbol-day (one request per "
            "symbol and trading day; IB allows one history request every 10 seconds). "
            f"At most {MAX_SYMBOLS} symbols per run."
        ),
        epilog=EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbols", nargs="+", required=True, metavar="SYMBOL",
                        help="tickers, comma or space separated, e.g. AAPL,MSFT,TSLA")
    parser.add_argument("--start", type=_iso_date, required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--end", type=_iso_date, required=True, metavar="YYYY-MM-DD")
    _add_connection_arguments(parser)
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=r"bars folder (default %%USERPROFILE%%\Stoinkulator\bars; "
                             "not in the repo or OneDrive)")
    _add_record_arguments(parser)
    parser.add_argument("--settle-hours", type=float, default=DEFAULT_SETTLE_HOURS, metavar="HOURS",
                        help="a day with no bars counts as a holiday only when its session closed at "
                             "least this many hours ago; sooner it is retried next run (0 to "
                             f"{MAX_SETTLE_HOURS:g}, default {DEFAULT_SETTLE_HOURS:g})")
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
        parser.error(f"--symbols names {len(names)} symbols; at most {MAX_SYMBOLS} per run. "
                     "Split the list into several runs")
    args.symbols = names
    if args.start > args.end:
        parser.error(f"--start {args.start} is after --end {args.end}")
    _check_connection_arguments(parser, args)
    if not 0 <= args.settle_hours <= MAX_SETTLE_HOURS:
        parser.error(f"--settle-hours must be 0 to {MAX_SETTLE_HOURS:g}")
    return args


def build_probe_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker probe-data",
        description=(
            "Owner-run: find out what market data this account gets. Connects, reads the "
            "data farms and IB's clock, resolves each symbol, streams quotes (live first, "
            "delayed if no price arrives) and fetches one day of 1-minute bars. Sends data "
            "requests only. Needs a real terminal; you type the port number to confirm."
        ),
        epilog=PROBE_EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_connection_arguments(parser)
    parser.add_argument("--symbols", default="AAPL", metavar="A,B",
                        help=f"tickers, comma separated, at most {PROBE_MAX_SYMBOLS} (default AAPL)")
    parser.add_argument("--quote-seconds", type=float, default=PROBE_QUOTE_SECONDS, metavar="S",
                        help=f"seconds of quotes per symbol, 1 to {PROBE_MAX_QUOTE_SECONDS} "
                             f"(default {PROBE_QUOTE_SECONDS:g})")
    parser.add_argument("--no-history", action="store_true", help="skip the one day of 1-minute bars")
    _add_record_arguments(parser)
    return parser


def parse_probe_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_probe_parser()
    args = parser.parse_args(argv)
    names: list[str] = []
    for raw in args.symbols.split(","):
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
    if len(names) > PROBE_MAX_SYMBOLS:
        parser.error(f"--symbols names {len(names)} symbols; at most {PROBE_MAX_SYMBOLS}")
    args.symbols = names
    _check_connection_arguments(parser, args)
    if not 1 <= args.quote_seconds <= PROBE_MAX_QUOTE_SECONDS:
        parser.error(f"--quote-seconds must be 1 to {PROBE_MAX_QUOTE_SECONDS}")
    return args


# -- paths ----------------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def check_path(path: Path, env: Mapping[str, str], what: str, repo: Path = REPO_ROOT) -> None:
    """Refuse `path` inside the repository or inside a OneDrive folder (`PathRefused`)."""
    if _inside(path, repo):
        raise PathRefused(f"{what} {path} is inside the repository; choose a folder outside it")
    if is_cloud_synced(path, env):
        raise PathRefused(f"{what} {path} is inside a OneDrive folder; sync can corrupt files "
                          "while they are written. Choose a folder outside OneDrive")


def home_folder(env: Mapping[str, str]) -> Path:
    """The home folder named by `USERPROFILE`, else `HOME`, in `env` and nowhere else.

    The machine's own idea of "home" is deliberately never consulted, so a test (or a
    wrong environment) can never make this command write into somebody's real profile.
    """
    for key in ("USERPROFILE", "HOME"):
        value = env.get(key, "").strip()
        if value:
            return Path(value)
    raise PathRefused("neither USERPROFILE nor HOME is set in the environment, so the default "
                      "folders cannot be placed. Nothing was started")


def check_folder_usable(path: Path, what: str) -> None:
    """Refuse a `path` that is a file or lies under a file (`PathRefused`); creates nothing."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return
        probe = probe.parent
    if not probe.is_dir():
        where = "is a file" if probe == path else f"lies under the file {probe}"
        raise PathRefused(f"{what} {path} {where}; choose a folder")


def make_folders(folders: Sequence[Path]) -> list[Path]:
    """Create each folder (and its missing parents); returns the ones this call created, outermost first.

    On an error the folders already created here are removed again (if still empty) and
    `PathRefused` is raised.
    """
    created: list[Path] = []
    for folder in folders:
        missing: list[Path] = []
        probe = folder
        while not probe.exists() and probe.parent != probe:
            missing.append(probe)
            probe = probe.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            created.extend(reversed(missing))
            remove_empty(created)
            raise PathRefused(f"cannot create the folder {folder}: {type(exc).__name__}: {exc}") from exc
        created.extend(reversed(missing))
    return created


def remove_empty(created: Sequence[Path]) -> None:
    """Remove the folders in `created` (innermost first) that are still empty; others stay."""
    for folder in reversed(created):
        try:
            folder.rmdir()
        except OSError:
            pass


def next_recording_path(folder: Path, day: date, command: str = FETCH_BARS) -> Path:
    """The first `<command>-<date>-<n>.jsonl` in `folder` that does not exist yet."""
    for number in range(1, 10_000):
        candidate = folder / f"{command}-{day:%Y-%m-%d}-{number:03d}.jsonl"
        if not candidate.exists():
            return candidate
    raise PathRefused(f"no free recording name in {folder}")


def private_values(env: Mapping[str, str], cwd: Path) -> tuple[str, ...]:
    """Words the recording must not contain: user, machine, home folder, cloud-sync folder names."""
    values: list[str] = []
    for key in ("USERNAME", "USER", "LOGNAME", "COMPUTERNAME", "HOSTNAME", "USERDOMAIN"):
        values.append(env.get(key, ""))
    values.append(platform.node())
    home = home_folder(env)
    values.append(str(home))
    values.append(home.name)
    for path in (home, cwd):
        for part in path.parts:
            if part.lower().startswith("onedrive"):
                values.append(part)
                values.append(part.split(" - ")[-1])
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        values.append(env.get(name, ""))
    seen: list[str] = []
    for value in values:
        value = value.strip()
        if value and value.lower() not in (v.lower() for v in seen):
            seen.append(value)
    return tuple(seen)


def resolve_paths(args: argparse.Namespace, env: Mapping[str, str], today: date, cwd: Path,
                  command: str = FETCH_BARS) -> None:
    """Fill `args.out` (fetch-bars only), `args.record` and `args.private`; `PathRefused` for a bad path."""
    home = home_folder(env)
    out: Path | None = None
    if command == FETCH_BARS:
        out = Path(args.out) if args.out else home / "Stoinkulator" / "bars"
        check_path(out, env, "the bars folder")
        check_folder_usable(out, "the bars folder")
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
            record = next_recording_path(folder, today, command)
    if out is not None:
        args.out = out
    args.record = record
    args.private = private_values(env, cwd)


# -- gate ----------------------------------------------------------------------------------


def trading_days(start: date, end: date) -> int:
    """Weekdays from `start` to `end` (holidays and days not closed yet are not known here)."""
    count, day = 0, start
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def rough_estimate(args: argparse.Namespace) -> FetchEstimate:
    """An upper bound before connecting; the real one comes from IB's server time."""
    days = trading_days(args.start, args.end)
    requests = days * len(args.symbols)
    return FetchEstimate(len(args.symbols), days, requests, estimate_duration(requests), 0)


def format_duration(seconds: float) -> str:
    """`50 s`, `7 min 05 s`, `1 h 30 min`: under a minute is shown in seconds."""
    total = int(seconds)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min {total % 60:02d} s"
    return f"{total // 3600} h {total % 3600 // 60:02d} min"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def describe_estimate(requests: int, symbols: int, days: int, min_seconds: float) -> str:
    text = f"up to {_count(requests, 'request')} ({_count(symbols, 'symbol')} x {_count(days, 'day')})"
    if min_seconds >= 1:  # never "at least 0 s"
        text += f", at least {format_duration(min_seconds)} at the pacing limits"
    return text


def describe_session(args: argparse.Namespace) -> str:
    estimate = rough_estimate(args)
    record = f"{args.record}" if args.record else "none (--no-record)"
    return "\n".join([
        "This command connects to TWS or IB Gateway. Only the owner runs it.",
        f"  Connects to:      {args.host}:{args.port}",
        f"  Client id:        {args.client_id}",
        f"  Symbols:          {', '.join(args.symbols)}",
        f"  Dates:            {args.start} to {args.end}",
        f"  Output folder:    {args.out}",
        f"  Recording file:   {record}",
        "  Estimate:         " + describe_estimate(estimate.requests, estimate.symbols, estimate.days,
                                                   estimate.min_seconds),
        "                    (an upper bound: days already on disk are skipped)",
        "  It sends data requests only: the client cannot send anything else.",
    ])


def describe_probe(args: argparse.Namespace) -> str:
    record = f"{args.record}" if args.record else "none (--no-record)"
    history = "none (--no-history)" if args.no_history else "one day of 1-minute bars per symbol"
    return "\n".join([
        "This command connects to TWS or IB Gateway. Only the owner runs it.",
        f"  Connects to:      {args.host}:{args.port}",
        f"  Client id:        {args.client_id}",
        f"  Symbols:          {', '.join(args.symbols)}",
        f"  Quotes:           about {args.quote_seconds:g} s per symbol (delayed data is tried "
        "if no live price comes)",
        f"  History:          {history}",
        f"  Recording file:   {record}",
        "  It sends data requests only: the client cannot send anything else.",
    ])


def gate(args: argparse.Namespace, stdin: TextIO, input_fn: Callable[[str], str], say: _Say,
         describe: Callable[[argparse.Namespace], str] = describe_session) -> bool:
    isatty = getattr(stdin, "isatty", None)
    if isatty is None or not isatty():
        say("Refusing to run: this command connects to TWS and only the owner runs it, "
            "from an interactive terminal (stdin is not one).")
        return False
    say(describe(args))
    try:
        typed = input_fn(f"To proceed, type the port number ({args.port}) and press Enter: ")
    except (EOFError, KeyboardInterrupt):
        say("No confirmation given. Nothing was started.")
        return False
    if typed.strip() != str(args.port):
        say("The text typed is not the port number. Nothing was started.")
        return False
    return True


def port_refusal_text(exc: Exception) -> str:
    return (
        f"{exc}\n"
        "To allow it for this terminal only, YOU would run this in PowerShell, "
        "then start the command again:\n"
        f'    $env:{ALLOW_LIVE_ENV} = "1"\n'
        "This command never sets it."
    )


# -- the session --------------------------------------------------------------------------


def failure_text(code: int | None, text: str, reason: str = "") -> str:
    """One failed day in words: `failed, IB 162: <text>`, `failed: <text>` without a code."""
    if reason == "no_data_yet":
        return f"failed: {NO_DATA_YET_TEXT}"
    if reason == "damaged_file":
        return f"failed: {text}; the file was left untouched"
    if code is None:
        return f"failed: {text}"
    return f"failed, IB {code}: {text}"


def _progress(say: _Say) -> Callable[[ProgressEvent], None]:
    quiet = {"first_day", "gaps", "done"}  # shown in the summary

    def show(event: ProgressEvent) -> None:
        if event.kind in quiet:
            return
        detail = event.detail
        if event.kind == "estimate" and {"requests", "symbols", "days", "min_seconds"} <= set(detail):
            say("Estimate (from IB's server time): " + describe_estimate(
                int(detail["requests"]), int(detail["symbols"]), int(detail["days"]),  # type: ignore[call-overload]
                float(detail["min_seconds"])))  # type: ignore[arg-type]
        elif event.kind == "estimate":
            say(f"Estimate (from IB's server time): {event.message}")
        elif event.kind == "day" and detail.get("status") == "failed":
            code = detail.get("code")
            say(f"  {event.symbol} {event.day}: " + failure_text(
                code if isinstance(code, int) else None, str(detail.get("text", "")),
                str(detail.get("reason", ""))))
        elif event.kind == "stopped":
            say(f"STOPPED: {event.message}")
        else:
            say(f"  {event.message}")

    return show


def _day_volume(out_dir: Path, symbol: str, day: date) -> int | None:
    try:
        frame = read_symbol_bars(out_dir, symbol)
        rows = frame[frame["date"].dt.date == day]
        return int(rows["volume"].sum()) if len(rows) else None
    except Exception:  # noqa: BLE001 - a summary line must not hide the run's result
        return None


def _hm(value: datetime | None) -> str:
    return "-" if value is None else f"{value:%H:%M}"


def gap_counts_text(res: Any) -> str:
    """`2 complete, 1 failed, 0 no data`: the gap report calls a day without bars `no_data`,
    also a day that FAILED, so the failed days (from `res.failures`) are counted on their own."""
    failed_days = {f.day for f in res.failures}
    counts: dict[str, int] = {}
    for row in res.gaps:
        status = "failed" if row.status == "no_data" and row.day in failed_days else row.status
        counts[status] = counts.get(status, 0) + 1
    if "failed" in counts and "no_data" not in counts:
        counts["no_data"] = 0
    return ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(counts.items())) or "no days"


def _summary(say: _Say, result: FetchResult, out_dir: Path) -> None:
    say()
    say("Summary")
    for name, res in result.symbols.items():
        say(f"  {name}: {res.ok} ok, {res.no_data} no data, {res.failed} failed, "
            f"{res.already_done} already done, {res.not_closed} not closed [{res.state}]")
        if res.state != "done" and res.message:
            say(f"    {res.message}")
        for failure in res.failures:
            say(f"    {failure.day}: " + failure_text(failure.code, failure.text, failure.reason))
    say("First fetched day of each symbol (check against a chart; times are New York):")
    for name, res in result.symbols.items():
        first = res.first_day
        if first is None:
            say(f"  {name}: no day was requested")
            continue
        total = _day_volume(out_dir, name, first.day) if first.status == "ok" else None
        say(f"  {name} {first.day}: {first.status}; first bar {_hm(first.first_bar)}, last bar "
            f"{_hm(first.last_bar)}, {first.bars} bars, first bar volume "
            f"{'-' if first.first_volume is None else first.first_volume}, day total volume "
            f"{'-' if total is None else total}")
    say(VOLUME_NOTE)
    for name, res in result.symbols.items():
        if res.gap_error:
            say(f"  {name}: gap report failed: {res.gap_error}")
        elif res.con_id is not None:
            text = gap_counts_text(res)
            files = "" if res.gap_files is None else f"; gap files: {res.gap_files[0]} and {res.gap_files[1]}"
            say(f"  {name}: gaps: {text}{files}")
    say(f"Days with missing minutes (short or partial, bars kept): {result.days_with_gaps}")
    for name, res in result.symbols.items():
        if res.short_days:
            say(f"  {name}: " + ", ".join(f"{day} {bars} bars ({status})"
                                          for day, bars, status in res.short_days))
    if result.not_closed:
        say(f"Not closed yet, not fetched: {', '.join(d.isoformat() for d in result.not_closed)}")
    if result.ib_errors_dropped:
        say(f"({result.ib_errors_dropped} further IB errors were not kept)")


def advice_for(result: FetchResult) -> list[str]:
    """One line for each distinct cause that stopped the run."""
    lines: list[str] = []
    if result.connection_lost:
        lines.append(ADVICE_LOST)
    if result.pacing_stopped:
        lines.append(ADVICE_PACING)
    if result.stop_reason == "no data farm":
        lines.append(ADVICE_FARM)
    return lines


def exit_code_for(result: FetchResult) -> int:
    if result.fatal or result.stop_reason is not None:
        return EXIT_STOPPED
    if any(r.state != "done" or r.failed for r in result.symbols.values()):
        return EXIT_FAILED
    return EXIT_OK


_LABELS = {
    EXIT_OK: "complete",
    EXIT_FAILED: "finished with failed days or symbols not fetched",
    EXIT_REFUSED: "refused, nothing was fetched",
    EXIT_NO_CONNECTION: "could not connect",
    EXIT_STOPPED: "stopped early, what was finished is on disk; run again to resume",
    EXIT_INTERRUPTED: "interrupted with Ctrl-C",
}


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


SessionBody = Callable[[_Say, IbConnection, IbMarketData, list[str]], int]
"""What a command does once connected: returns its exit code; appends its advice lines to the list."""

FindingsReport = Callable[[_Say, Path, list[Finding]], None]


@dataclass(frozen=True)
class SessionKind:
    """What differs between the commands in `run_session`."""

    labels: dict[int, str]
    interrupted: str
    unexpected: int
    report_findings: FindingsReport


def report_findings(say: _Say, record: Path, findings: list[Finding]) -> None:
    """The recording check of `fetch-bars`: every finding by line number and reason."""
    say(f"Recording check: {len(findings)} finding(s). "
        "Read the file yourself before it is ever committed.")
    list_findings(say, findings)


def list_findings(say: _Say, findings: Sequence[Finding]) -> None:
    if findings:
        say("  Where to look (line number and reason only):")
    for finding in findings[:SHOWN_FINDINGS]:
        say(f"  line {finding.line}: {finding.reason}")
    if len(findings) > SHOWN_FINDINGS:
        say(f"  ... and {len(findings) - SHOWN_FINDINGS} more")


FETCH_KIND = SessionKind(
    labels=_LABELS,
    interrupted="Interrupted (Ctrl-C). What was finished is on disk; run the same command again to resume.",
    unexpected=EXIT_STOPPED,
    report_findings=report_findings,
)


def _header_only(path: Path) -> bool:
    """True when the recording holds the session header and no event."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return len([line for line in text.split("\n") if line.strip()]) <= 1


def run_session(
    args: argparse.Namespace,
    kind: SessionKind,
    body: SessionBody,
    *,
    connection_factory: ConnectionFactory,
    market_data_factory: MarketDataFactory,
    recorder_factory: RecorderFactory,
    out: TextIO,
) -> int:
    """Start the recording, connect, run `body`, always close the connection and the recorder.

    `args` carries `record` (Path or None), `private`, `host`, `port`, `client_id` and
    `connect_timeout`. A recording of a connection that was refused or could not be opened
    holds only the header, so this run deletes the file it made. Returns the exit code.
    """
    say = _Say(out)
    try:
        recorder = recorder_factory(args)
    except (OSError, PathRefused) as exc:
        say(f"Cannot start the session recording: {type(exc).__name__}: {exc}")
        say(f"Result: {kind.labels[EXIT_REFUSED]} (exit {EXIT_REFUSED})")
        return EXIT_REFUSED
    conn: IbConnection | None = None
    code = kind.unexpected
    advice: list[str] = []
    not_connected = False
    try:
        conn = connection_factory(args, [recorder] if recorder is not None else [])
        refused = _connect(say, conn, args, advice)
        if refused is not None:
            code = refused
            not_connected = True
        else:
            code = body(say, conn, market_data_factory(conn), advice)
    except KeyboardInterrupt:
        say()
        say(kind.interrupted)
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - reported with the exit code, not a traceback
        say(f"Unexpected {type(exc).__name__}: {exc}")
        code = kind.unexpected
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                say(f"Closing the connection raised {type(exc).__name__}: {exc}")
        if recorder is not None:
            recorder.close()
    for line in advice:  # each cause is added once, so each line is distinct
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
            kind.report_findings(say, args.record, findings)
    say(f"Result: {kind.labels[code]} (exit {code})")
    say(PATHS_NOTE)
    return code


def run_fetch_bars(
    args: argparse.Namespace,
    *,
    connection_factory: ConnectionFactory,
    market_data_factory: MarketDataFactory,
    recorder_factory: RecorderFactory,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    """Connect, fetch, summarise; always closes the connection and the recorder.

    `args` carries `symbols`, `start`, `end`, `out` (Path), `record` (Path or None),
    `private`, `host`, `port`, `client_id` and `connect_timeout`. Returns the exit code.
    """

    def body(say: _Say, conn: IbConnection, md: IbMarketData, advice: list[str]) -> int:
        try:
            result = fetch(md, args.symbols, args.start, args.end, args.out, connection=conn,
                           clock=clock, sleep=sleep, on_progress=_progress(say),
                           settle_seconds=args.settle_hours * 3600.0)
        except FetchSetupError as exc:
            say(f"Refused: {exc}")
            return EXIT_REFUSED
        _summary(say, result, args.out)
        advice.extend(advice_for(result))
        return exit_code_for(result)

    return run_session(args, FETCH_KIND, body, connection_factory=connection_factory,
                       market_data_factory=market_data_factory, recorder_factory=recorder_factory, out=out)


# -- the entry point ----------------------------------------------------------------------


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
    today: date | None = None,
    command: str = FETCH_BARS,
    wall: Callable[[], datetime] | None = None,
) -> int:
    """Parse, check paths and port, pass the console gate, then run the command.

    `command` is `fetch-bars` or `probe-data` (`probe_data_main`). The keyword arguments
    are for tests. This is the only place the process environment is read.
    """
    probe = command == PROBE_DATA
    # --help and argument errors exit here, before any client exists
    args = parse_probe_args(argv) if probe else parse_args(argv)
    variables: Mapping[str, str] = os.environ if env is None else env
    stream = sys.stdout if out is None else out
    say = _Say(stream)
    try:
        resolve_paths(args, variables, today or date.today(), Path.cwd(), command)
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    if not probe:
        if trading_days(args.start, args.end) == 0:
            say(f"Refused (pre-check from the dates alone): no weekday from {args.start} to {args.end}; "
                "nothing to fetch.")
            return EXIT_REFUSED
        new_york_today = today or datetime.now(ZoneInfo(DATA_ZONE)).date()
        if args.start > new_york_today:
            say(f"Refused (pre-check from this computer's clock; after connecting IB's server time "
                f"decides): --start {args.start} is after today in New York ({new_york_today}), so no "
                "day of the range has closed. Nothing to fetch.")
            return EXIT_REFUSED
    try:
        check_broker_port(args.port, variables)
    except BrokerPortRefused as exc:
        say(port_refusal_text(exc))
        return EXIT_REFUSED
    if not probe:
        try:
            check_layouts(args.out, args.symbols)
        except FetchSetupError as exc:
            say(f"Refused: {exc}")
            return EXIT_REFUSED
    folders = ([] if probe else [args.out]) + ([args.record.parent] if args.record is not None else [])
    try:
        created = make_folders(folders)  # last of the refusals: the folders must be creatable
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    try:
        return _after_checks(command, args, variables, stream, say, stdin, input_fn, connection_factory,
                             market_data_factory, recorder_factory, clock, sleep, wall)
    finally:
        remove_empty(created)  # folders this run made and left empty (a refused gate, no connection)


def probe_data_main(argv: Sequence[str] | None = None, **kwargs: Any) -> int:
    """`probe-data`: the same checks, gate and session as `fetch-bars`, then the probe steps."""
    return main(argv, command=PROBE_DATA, **kwargs)


def _after_checks(
    command: str,
    args: argparse.Namespace,
    variables: Mapping[str, str],
    stream: TextIO,
    say: _Say,
    stdin: TextIO | None,
    input_fn: Callable[[str], str] | None,
    connection_factory: ConnectionFactory | None,
    market_data_factory: MarketDataFactory | None,
    recorder_factory: RecorderFactory | None,
    clock: Callable[[], float] | None,
    sleep: Callable[[float], None] | None,
    wall: Callable[[], datetime] | None = None,
) -> int:
    tty = sys.stdin if stdin is None else stdin
    ask = input if input_fn is None else input_fn
    probe = command == PROBE_DATA
    if not gate(args, tty, ask, say, describe_probe if probe else describe_session):
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
        # the subcommand and the symbols only: no dates, port, host or paths in the header
        return SessionRecorder(a.record, clock=now, command=[command, *a.symbols], port=a.port,
                               private_values=a.private)

    if probe:
        from MainStoinker.ib.probe import run_probe_data  # lazy: probe.py imports this module

        return run_probe_data(
            args,
            connection_factory=connection_factory or real_connection,
            market_data_factory=market_data_factory or IbMarketData,
            recorder_factory=recorder_factory or real_recorder,
            out=stream,
            clock=now,
            sleep=sleep or time.sleep,
            wall=wall,
        )
    return run_fetch_bars(
        args,
        connection_factory=connection_factory or real_connection,
        market_data_factory=market_data_factory or IbMarketData,
        recorder_factory=recorder_factory or real_recorder,
        out=stream,
        clock=now,
        sleep=sleep or time.sleep,
    )


if __name__ == "__main__":
    sys.exit(main())
