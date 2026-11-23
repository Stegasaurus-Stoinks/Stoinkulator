"""The `fetch-bars` command: owner-run download of 1-minute bars from TWS or IB Gateway.

    python -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA --start 2024-08-16 --end 2024-08-26 --port 7496

Only the owner runs it, in a real terminal. It is built on `IbConnection` and
`IbMarketData` (a data-only client: it cannot send anything but data requests) and
`fetch` (the driver, which writes one CSV file per symbol per month and resumes).

Checks, in this sequence, before any client or connection is built
1. arguments (`--help` and argument errors end here);
2. the paths (an output or recording path inside the repository or a OneDrive folder is refused);
3. the port guard (`check_broker_port`): paper ports always, a live port only if the
   OWNER has set `STOINK_ALLOW_LIVE_PORT=1` in their own terminal; this module never sets it;
4. the console gate: stdin must be an interactive terminal and the owner types the port
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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from MainStoinker.engine.feed import read_symbol_bars
from MainStoinker.engine.store import is_cloud_synced
from MainStoinker.ib.connection import (
    MAX_CLIENT_ID,
    REFUSED_CLIENT_IDS,
    BrokerConnectionRefusedHere,
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
from MainStoinker.ib.history import check_symbol
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.recorder import SessionRecorder, scan_recording
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

DEFAULT_CLIENT_ID = 21
DEFAULT_CONNECT_TIMEOUT = 30.0
MAX_CONNECT_TIMEOUT = 300.0
VOLUME_NOTE = (
    "Volumes are stored exactly as IB sent them. Whether IB counts shares or 100-share lots "
    "is not settled yet: compare the first-day volumes above with a chart before relying on them."
)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker fetch-bars",
        description=(
            "Owner-run: download 1-minute regular-hours bars through TWS or IB Gateway into "
            "one CSV file per symbol per month, resuming where an earlier run stopped. Sends "
            "data requests only. Needs a real terminal; you type the port number to confirm."
        ),
        epilog=EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbols", nargs="+", required=True, metavar="SYMBOL",
                        help="tickers, comma or space separated, e.g. AAPL,MSFT,TSLA")
    parser.add_argument("--start", type=_iso_date, required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--end", type=_iso_date, required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--port", type=int, required=True, help="TWS or Gateway API port (no default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=r"bars folder (default %%USERPROFILE%%\Stoinkulator\bars; "
                             "not in the repo or OneDrive)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--record", default=None, metavar="FILE",
                       help=r"session recording file, never overwritten (default: a new file in "
                            r"%%USERPROFILE%%\Stoinkulator\recordings)")
    group.add_argument("--no-record", action="store_true", help="do not record the session")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT,
                        help="seconds per connection attempt; TWS may show a dialog (default 30)")
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
    args.symbols = names
    if args.start > args.end:
        parser.error(f"--start {args.start} is after --end {args.end}")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1 to 65535")
    if not 1 <= args.client_id <= MAX_CLIENT_ID or args.client_id in REFUSED_CLIENT_IDS:
        parser.error(f"--client-id must be 1 to {MAX_CLIENT_ID} and not {sorted(REFUSED_CLIENT_IDS)}")
    if not 0 < args.connect_timeout <= MAX_CONNECT_TIMEOUT:
        parser.error(f"--connect-timeout must be above 0 and at most {MAX_CONNECT_TIMEOUT:g}")
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
    return Path(env.get("USERPROFILE") or Path.home())


def next_recording_path(folder: Path, day: date) -> Path:
    """The first `fetch-bars-<date>-<n>.jsonl` in `folder` that does not exist yet."""
    for number in range(1, 10_000):
        candidate = folder / f"fetch-bars-{day:%Y-%m-%d}-{number:03d}.jsonl"
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


def resolve_paths(args: argparse.Namespace, env: Mapping[str, str], today: date, cwd: Path) -> None:
    """Fill `args.out`, `args.record` and `args.private`; `PathRefused` for a bad path."""
    home = home_folder(env)
    out = Path(args.out) if args.out else home / "Stoinkulator" / "bars"
    check_path(out, env, "the bars folder")
    record: Path | None = None
    if not args.no_record:
        if args.record:
            record = Path(args.record)
            check_path(record.parent, env, "the recording folder")
            check_path(record, env, "the recording file")
            if record.exists():
                raise PathRefused(f"the recording file {record} exists; it is never overwritten")
        else:
            folder = home / "Stoinkulator" / "recordings"
            check_path(folder, env, "the recording folder")
            record = next_recording_path(folder, today)
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


def describe_session(args: argparse.Namespace) -> str:
    estimate = rough_estimate(args)
    record = f"recorded to {args.record}" if args.record else "not recorded (--no-record)"
    return (
        f"This command connects to TWS or IB Gateway at {args.host}:{args.port} (client id "
        f"{args.client_id}) and downloads 1-minute regular-hours bars of {', '.join(args.symbols)} "
        f"from {args.start} to {args.end} into {args.out}. It sends data requests only; the client "
        f"cannot send anything else. Estimate: {estimate.describe()} (an upper bound: days already "
        f"on disk are skipped). Session {record}. Only the owner runs this."
    )


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


def port_refusal_text(exc: Exception) -> str:
    return (
        f"{exc}\n"
        "To allow it for this terminal only, YOU would run this in PowerShell, "
        "then start the command again:\n"
        f'    $env:{ALLOW_LIVE_ENV} = "1"\n'
        "This command never sets it."
    )


# -- the session --------------------------------------------------------------------------


def _progress(say: _Say) -> Callable[[ProgressEvent], None]:
    quiet = {"first_day", "gaps", "done"}  # shown in the summary

    def show(event: ProgressEvent) -> None:
        if event.kind in quiet:
            return
        if event.kind == "estimate":
            say(f"Estimate (from IB's server time): {event.message}")
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


def _summary(say: _Say, result: FetchResult, out_dir: Path) -> None:
    say()
    say("Summary")
    for name, res in result.symbols.items():
        say(f"  {name}: {res.ok} ok, {res.no_data} no data, {res.failed} failed, "
            f"{res.already_done} already done, {res.not_closed} not closed [{res.state}]")
        if res.state != "done" and res.message:
            say(f"    {res.message}")
        for failure in res.failures:
            code = "" if failure.code is None else f"{failure.code}: "
            say(f"    failed {failure.day}: {code}{failure.text}")
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
            counts: dict[str, int] = {}
            for row in res.gaps:
                counts[row.status] = counts.get(row.status, 0) + 1
            text = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no days"
            files = "" if res.gap_files is None else f"; gap files: {res.gap_files[0]} and {res.gap_files[1]}"
            say(f"  {name}: gaps: {text}{files}")
    if result.not_closed:
        say(f"Not closed yet, not fetched: {', '.join(d.isoformat() for d in result.not_closed)}")
    if result.ib_errors_dropped:
        say(f"({result.ib_errors_dropped} further IB errors were not kept)")


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


def _connect(say: _Say, conn: IbConnection, args: argparse.Namespace) -> int | None:
    """None when connected, else the exit code."""
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
        return EXIT_NO_CONNECTION
    except Exception as exc:  # noqa: BLE001 - reported, not a traceback
        say(f"Could not connect: {type(exc).__name__}: {exc}")
        return EXIT_NO_CONNECTION
    say("Connected.")
    return None


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
    say = _Say(out)
    try:
        recorder = recorder_factory(args)
    except (OSError, PathRefused) as exc:
        say(f"Cannot start the session recording: {type(exc).__name__}: {exc}")
        say(f"Result: {_LABELS[EXIT_REFUSED]} (exit {EXIT_REFUSED})")
        return EXIT_REFUSED
    conn: IbConnection | None = None
    code = EXIT_STOPPED
    try:
        conn = connection_factory(args, [recorder] if recorder is not None else [])
        refused = _connect(say, conn, args)
        if refused is not None:
            code = refused
        else:
            md = market_data_factory(conn)
            try:
                result = fetch(md, args.symbols, args.start, args.end, args.out, connection=conn,
                               clock=clock, sleep=sleep, on_progress=_progress(say))
            except FetchSetupError as exc:
                say(f"Refused: {exc}")
                code = EXIT_REFUSED
            else:
                _summary(say, result, args.out)
                code = exit_code_for(result)
    except KeyboardInterrupt:
        say()
        say("Interrupted (Ctrl-C). What was finished is on disk; run the same command again to resume.")
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - reported with the exit code, not a traceback
        say(f"Unexpected {type(exc).__name__}: {exc}")
        code = EXIT_STOPPED
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                say(f"Closing the connection raised {type(exc).__name__}: {exc}")
        if recorder is not None:
            recorder.close()
    if args.record is not None and recorder is not None:
        words = [w for w in args.private if len(w) >= 3]
        findings = scan_recording(args.record, words)
        say(f"Recording: {args.record}")
        say(f"Recording check: {len(findings)} finding(s). "
            "Read the file yourself before it is ever committed.")
        for finding in findings[:20]:
            say(f"  line {finding.line}: {finding.reason}")
    say(f"Result: {_LABELS[code]} (exit {code})")
    return code


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
) -> int:
    """Parse, check paths and port, pass the console gate, then `run_fetch_bars`.

    The keyword arguments are for tests. This is the only place the process environment
    is read.
    """
    args = parse_args(argv)  # --help and argument errors exit here, before any client exists
    variables: Mapping[str, str] = os.environ if env is None else env
    stream = sys.stdout if out is None else out
    say = _Say(stream)
    try:
        resolve_paths(args, variables, today or date.today(), Path.cwd())
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    if trading_days(args.start, args.end) == 0:
        say(f"Refused: no weekday from {args.start} to {args.end}; nothing to fetch.")
        return EXIT_REFUSED
    try:
        check_broker_port(args.port, variables)
    except BrokerPortRefused as exc:
        say(port_refusal_text(exc))
        return EXIT_REFUSED
    try:
        check_layouts(args.out, args.symbols)
    except FetchSetupError as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    tty = sys.stdin if stdin is None else stdin
    ask = input if input_fn is None else input_fn
    if not gate(args, tty, ask, say):
        return EXIT_REFUSED
    logging.getLogger("ibapi").setLevel(logging.WARNING)
    now = clock or time.monotonic
    command = ["fetch-bars", *(sys.argv[1:] if argv is None else argv)]

    def real_connection(a: Any, observers: list[Any]) -> IbConnection:
        return IbConnection(
            a.host, a.port, a.client_id, env=variables, attempts=2, retry_delay=3,
            connect_timeout=a.connect_timeout, ready_timeout=15, observers=observers,
        )

    def real_recorder(a: Any) -> SessionRecorder | None:
        if a.record is None:
            return None
        a.record.parent.mkdir(parents=True, exist_ok=True)
        return SessionRecorder(a.record, clock=now, command=command, port=a.port,
                               private_values=a.private)

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
