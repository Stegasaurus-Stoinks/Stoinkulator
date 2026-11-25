"""`python -m MainStoinker run`: a live paper session, paper only, no orders (issue 198).

    python -m MainStoinker run --algos Algo_config.json --bars BARS_DIR --db runs.db \\
        --paper --no-prompt --minutes 415

Connects to TWS or IB Gateway as a DATA client (client id 20), watches the live 1-minute bars of the
symbols of the algos file, and runs the algos on them through the engine with the simulated paper
broker (realistic fills, entries at the next bar's open). Nothing here can send an order: the
command builds `IbConnection` and `IbMarketData` (a data-only client) and the session driver
`live_run.run_live_session`, and imports nothing that can place an order. There is no option for
orders; stage 3 is a different task.

Refused (exit 2, before anything is built or connected; one line each):

* a usage error: `--no-prompt` without `--paper`, `--minutes` outside 1 to 420 or missing, a bad
  port, client id, warm-up or connect timeout (argparse, exit 2);
* no `--paper` (a live session is paper only), a port the guard refuses, and with `--paper` any port
  but 7497, any host but 127.0.0.1, or `STOINK_ALLOW_LIVE_PORT` present in the environment;
* `--fills legacy`, and any algo whose `"mode"` is not `paper` (`backtest`, `live`, or absent);
* an algos file that cannot be used, more than 5 symbols, bars that are not a folder;
* a path in the repository or in a OneDrive folder (`--out`, `--bars`, `--db`, `--stop-file`,
  `--record`, the algos' working folder), a path that is a file where a folder is needed, a stop file
  that exists, a recording file that exists, a `--db` that cannot be opened (or no `--db` and no
  `STOINK_DB` in the environment);
* another `paper-live` run that is marked running and was written recently (a stale one is closed
  as `failed` / `abandoned` first, by the driver, and said);
* the console gate, unless `--paper --no-prompt`: stdin must be a terminal and the port number typed.

`--paper --no-prompt` is the unattended form: no prompt, the first line says so. Every printed line is
scrubbed. The session recording is ON by default (a new `run-<date>-<n>.jsonl` under
`<home>/Stoinkulator/recordings`); `--no-record` turns it off. The default folders come from the
environment mapping handed to `main` (`USERPROFILE`, else `HOME`), never from the machine's idea of home.

A session that starts at about 09:25 New York (five minutes before the open) runs to the end of the
session at about 16:20, 415 minutes. Ctrl-C does not interrupt the end of a session: the run is
ended in the store first (see `live_run._Shield`), then the command exits with 130.

Exit codes: see `EXIT_CODES_TEXT` (shown by `--help`).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.engine.cli import (
    CliError,
    config_symbols,
    parse_fill_options,
    read_algo_config,
)
from MainStoinker.engine.live_counts import LIVE_KINDS
from MainStoinker.engine.modes import DEFAULT_MODE
from MainStoinker.engine.run_documents import liveness
from MainStoinker.engine.store import Store, StoreError, open_store, open_store_readonly
from MainStoinker.ib.cli import (
    EXIT_REFUSED,
    PAPER_MODE_LINE,
    PAPER_NO_PROMPT_LINE,
    PathRefused,
    _add_connection_arguments,
    _add_record_arguments,
    _check_connection_arguments,
    _Say,
    _wants_paper,
    check_folder_usable,
    check_path,
    first_banner_line,
    gate,
    home_folder,
    make_folders,
    paper_refusal,
    port_refusal_text,
    remove_empty,
    resolve_paths,
)
from MainStoinker.ib.connection import IbConnection
from MainStoinker.ib.history import DATA_ZONE
from MainStoinker.ib.live_run import DEFAULT_CLIENT_ID, MODE, run_live_session
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.recorder import SessionRecorder
from MainStoinker.ib.watch_bars import MAX_SYMBOLS
from MainStoinker.Util.broker_guard import BrokerPortRefused, check_broker_port

RUN = "run"
RUN_KIND = LIVE_KINDS[0]  # "paper-live"
MIN_MINUTES = 1
MAX_MINUTES = 420  # a session from 09:25 to 16:20 is 415 minutes; the driver itself would take 600
DEFAULT_WARMUP_DAYS = 2
LIVE_BUSY_TIMEOUT = 2.0  # seconds one attempt of a store call waits for another writer
NY = ZoneInfo(DATA_ZONE)

EXIT_CODES_TEXT = """\
exit codes:
  0    the session ended as planned: the end of the session (about 16:20 New York, at the latest
       16:21), --minutes elapsed or the stop file; the run is stored as completed
  1    a symbol stopped (IB error, unusable live file): the run is stored as failed
  2    refused: usage error, console gate, port guard, --paper conditions (port, host,
       STOINK_ALLOW_LIVE_PORT set), a mode other than paper, legacy fills, an unusable path or
       database, another paper-live run still running; nothing was built or connected
  3    could not connect to TWS or IB Gateway
  4    stopped by a lost or closed connection, or by an error (the run is stored as failed)
  5    the account TWS is signed into is not a paper account (at the start nothing was
       requested and nothing stored; later the run was ended); never retry
  6    no data received for a symbol in the session (the run is stored as failed, no_data)
  7    the session stopped the run on a data fault: delayed data, refused or late bars, a silent
       symbol, a step that took too long (the run is stored as failed)
  130  interrupted with Ctrl-C (the run was ended in the store first; its status says why)
The last line printed names the stored status and end reason of the run.
"""

PAPER_ONLY_LINE = ("Refused: `run` is paper only: add --paper (port 7497 on 127.0.0.1, the account "
                   "check on). Nothing was started.")


def build_parser(paper: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker run",
        allow_abbrev=False,  # `--paper`, `--no-prompt` and `--minutes` are matched exactly
        description=(
            "Run the algos of an algos file on live 1-minute bars from TWS or IB Gateway, in "
            "paper mode only: realistic simulated fills, no order is ever sent. The run is stored "
            "in the database (kind paper-live) and can be checked afterwards with `report RUN "
            "--verify --bars DIR`. Only the owner runs it for now (an unattended form for "
            "agents is a separate decision). "
            "Every algo needs \"mode\": \"paper\".\n\n"
            "Start about 09:25 New York for a full session (it ends at about 16:20, 415 minutes). "
            "Ctrl-C does not interrupt the end of the session: the run is ended in the database "
            "first.\n\n"
            "Example, unattended on a paper account:\n"
            "  python -m MainStoinker run --algos ALGOS.json --bars BARS --db runs.db --paper "
            "--no-prompt --minutes 415"
        ),
        epilog=EXIT_CODES_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--algos", required=True, metavar="JSON",
                        help="algo config file (Algo_config.json shape); every algo \"mode\": \"paper\"; "
                             "its symbols are the symbols of the session (at most 5)")
    parser.add_argument("--bars", required=True, metavar="DIR",
                        help="the folder of the month files (fetch-bars output) for the warm-up; "
                             "not in the repo or OneDrive")
    parser.add_argument("--db", default=None, metavar="SPEC",
                        help="the run database (a file path or sqlite:///path; created if missing; "
                             "not in the repo or OneDrive). Defaults to STOINK_DB in the environment")
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=r"folder for the live files (default %%USERPROFILE%%\Stoinkulator\live; "
                             "not in the repo or OneDrive; the watcher must not write it at the same "
                             "time)")
    parser.add_argument("--warmup-days", type=int, default=DEFAULT_WARMUP_DAYS, metavar="N",
                        help=f"trading days of warm-up from the month files (default "
                             f"{DEFAULT_WARMUP_DAYS}, at least 1)")
    parser.add_argument("--fills", choices=("realistic", "legacy"), default="realistic",
                        help="only realistic is accepted; legacy is refused")
    parser.add_argument("--capital", default=None, metavar="AMOUNT",
                        help="starting capital per algo (as backtest)")
    parser.add_argument("--commission-per-share", default=None, metavar="AMOUNT",
                        help="commission per share (as backtest)")
    parser.add_argument("--commission-min", default=None, metavar="AMOUNT",
                        help="minimum commission per order (as backtest)")
    parser.add_argument("--slippage-per-share", default=None, metavar="AMOUNT",
                        help="slippage per share on market and stop fills (as backtest)")
    _add_connection_arguments(parser, paper)
    parser.set_defaults(client_id=DEFAULT_CLIENT_ID)
    parser.add_argument("--minutes", type=int, required=True, metavar="N",
                        help=f"how long the session may last, {MIN_MINUTES} to {MAX_MINUTES} "
                             "(415 covers 09:25 to 16:20)")
    parser.add_argument("--stop-file", default=None, metavar="PATH",
                        help="when this file appears the run is ended cleanly (completed, stop_file); "
                             "it must not exist at the start")
    _add_record_arguments(parser)
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_parser(_wants_paper(argv))
    args = parser.parse_args(argv)
    _check_connection_arguments(parser, args)
    if not MIN_MINUTES <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be {MIN_MINUTES} to {MAX_MINUTES}")
    if args.warmup_days < 1:
        parser.error("--warmup-days must be 1 or more")
    return args


def describe_run(args: argparse.Namespace) -> str:
    record = f"{args.record}" if args.record else "none (--no-record)"
    return "\n".join([
        first_banner_line(args),
        f"  Connects to:      {args.host}:{args.port}",
        f"  Client id:        {args.client_id}",
        f"  Symbols:          {', '.join(args.symbols)}",
        f"  Runs for:         at most {args.minutes} minutes (or to the end of the session)",
        f"  Algos:            {args.algos} (every algo in mode paper)",
        f"  Run database:     {args.db}",
        f"  Live files:       {args.out}",
        f"  Recording file:   {record}",
        "  It sends data requests only: no order is ever sent; fills are simulated.",
    ])


def _db_path(spec: str, home: Path) -> Path | None:
    """The database file of `spec`; a leading `~` means `home` (from the environment mapping given
    to `main`, never the process's own idea of home)."""
    path = spec[len("sqlite:///"):] if spec.startswith("sqlite:///") else spec
    if path == ":memory:":
        return None
    if path == "~" or path.startswith(("~/", "~\\")):
        return home / path[2:] if len(path) > 2 else home
    return Path(path)


def _open_live_store(spec: str, env: Mapping[str, str]) -> Store:
    """The live run's writable store, with a SHORT busy timeout: the writer repeats a busy call
    itself (`live_store.BUSY_RETRIES`), so one step can stall for at most
    (1 + BUSY_RETRIES) * LIVE_BUSY_TIMEOUT + BUSY_RETRIES * BUSY_PAUSE seconds, well under the
    driver's step limit (the store's default of 10 s would give about 63 s)."""
    return open_store(spec, env=env, busy_timeout=LIVE_BUSY_TIMEOUT)


def _running_runs(db_file: Path, variables: Mapping[str, str], now: datetime) -> list[int]:
    """Ids of the `paper-live` runs marked running whose last write is not stale. Reads the
    database without writing to it (no folder, no WAL switch, no migration); a missing file holds
    no run. `StoreError` when the file cannot be read."""
    if not db_file.exists():
        return []
    store = open_store_readonly(str(db_file), env=variables)
    try:
        return [s.run_id for s in store.list_runs()
                if s.kind == RUN_KIND and s.status == "running" and liveness(s, now) == "running"]
    finally:
        store.close()


def _check_inputs(args: argparse.Namespace, variables: Mapping[str, str]) -> None:
    """Every refusal that needs no connection and no store; `CliError` or `PathRefused`.

    On success `args` carries what the session driver reads (entries, symbols, costs, capital,
    run_out, a resolved out, stop_file and db)."""
    if args.fills != "realistic":
        raise CliError(f"a live session uses realistic fills only: --fills {args.fills} is refused")
    args.costs, capital = parse_fill_options(args)
    args.capital = capital
    entries = read_algo_config(Path(args.algos))
    for entry in entries:
        for cfg in entry["data"]:
            mode = cfg.get("mode", DEFAULT_MODE)
            if mode != MODE:
                raise CliError(f"algo {ascii(cfg['idname'])}: a live session runs algos in mode "
                               f"{MODE!r} only (got {ascii(mode)}; set \"mode\": \"{MODE}\")")
    symbols = config_symbols(entries)
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise CliError(f"the algos name {len(symbols)} symbols; a session takes 1 to {MAX_SYMBOLS}")
    args.entries, args.symbols = entries, symbols
    home = home_folder(variables)
    bars = Path(args.bars)
    check_path(bars, variables, "the bars folder")
    if not bars.is_dir():
        raise PathRefused(f"the bars folder {bars} is not a folder (the month files of the warm-up)")
    out = Path(args.out) if args.out else home / "Stoinkulator" / "live"
    check_path(out, variables, "the live folder")
    check_folder_usable(out, "the live folder")
    for symbol in symbols:
        check_folder_usable(out / symbol, f"the {symbol} folder")
    run_out = home / "Stoinkulator" / "run-out"
    check_path(run_out, variables, "the algos' working folder")
    check_folder_usable(run_out, "the algos' working folder")
    stop_file = None if args.stop_file is None else Path(args.stop_file)
    if stop_file is not None:
        check_path(stop_file, variables, "the stop file")
        check_folder_usable(stop_file.parent, "the stop file's folder")
        if stop_file.exists():
            raise PathRefused(f"the stop file {stop_file} exists already: remove it before starting")
    spec = args.db if args.db is not None else (variables.get("STOINK_DB", "").strip() or None)
    if spec is None:
        raise CliError("--db is needed (a run database), or STOINK_DB in the environment")
    db_file = _db_path(spec, home)
    if db_file is None:
        raise CliError("--db :memory: would keep nothing once the command ends; give a database file")
    check_path(db_file, variables, "the run database")
    check_folder_usable(db_file.parent, "the run database's folder")
    if db_file.is_dir():
        raise PathRefused(f"the run database {db_file} is a folder; give a database file")
    args.db = str(db_file)
    args.out, args.run_out, args.stop_file = out, run_out, stop_file


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
    out: TextIO | None = None,
    connection_factory: Callable[[Any, list[Any]], IbConnection] | None = None,
    market_data_factory: Callable[[IbConnection], IbMarketData] | None = None,
    recorder_factory: Callable[[Any], SessionRecorder | None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wall: Callable[[], datetime] | None = None,
    today: date | None = None,
    store_opener: Callable[[str, Mapping[str, str]], Store] | None = None,
) -> int:
    """Parse, check everything, open the store, pass the console gate, then `run_live_session`.

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
        check_broker_port(args.port, variables)
    except BrokerPortRefused as exc:
        say(port_refusal_text(exc))
        return EXIT_REFUSED
    if not args.paper:
        say(PAPER_ONLY_LINE)
        return EXIT_REFUSED
    day = today or wall_clock().astimezone(NY).date()
    try:
        _check_inputs(args, variables)
        resolve_paths(args, variables, day, Path.cwd(), RUN)  # the recording and the private words
    except (CliError, PathRefused) as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    # Nothing is written to the database before the console gate: this only reads it. Closing the
    # stale runs of an earlier session is a write, and the driver does it after the gate.
    try:
        running = _running_runs(Path(args.db), variables, wall_clock())
    except StoreError as exc:
        say(f"Refused: the run database cannot be used: {exc}")
        return EXIT_REFUSED
    if running:
        say(f"Refused: another {RUN_KIND} run is marked running and was written recently (run "
            f"{', '.join(str(r) for r in running)}): wait until it ends or its last write is stale")
        return EXIT_REFUSED
    folders = [args.out, args.run_out] + ([args.record.parent] if args.record is not None else [])
    try:
        created = make_folders(folders)  # the last refusal before the store: the folders must be creatable
    except PathRefused as exc:
        say(f"Refused: {exc}")
        return EXIT_REFUSED
    store: Store | None = None
    try:
        if args.no_prompt:  # `parse_args` accepted it only with --paper; nothing is read from stdin
            say(describe_run(args))
        else:
            tty = sys.stdin if stdin is None else stdin
            ask = input if input_fn is None else input_fn
            if not gate(args, tty, ask, say, describe_run):
                return EXIT_REFUSED
        try:  # the first writable open of the database: every refusal and the gate are behind us
            store = (store_opener or _open_live_store)(args.db, variables)
        except StoreError as exc:
            say(f"Refused: the run database cannot be used: {exc}")
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
            return SessionRecorder(a.record, clock=now, command=[RUN, *a.symbols], port=a.port,
                                   private_values=a.private)

        args.refuse_if_running = True  # a second live session is refused (after stale runs are closed)
        args.quiet = True  # no forming-bar line over a whole day
        return run_live_session(
            args,
            connection_factory=connection_factory or real_connection,
            market_data_factory=market_data_factory or IbMarketData,
            recorder_factory=recorder_factory or real_recorder,
            store=store,
            clock=now,
            sleep=sleep or time.sleep,
            wall=wall_clock,
            out=stream,
        )
    finally:
        if store is not None:
            store.close()
        remove_empty(created)  # folders this run made and left empty (a refusal, no connection)


__all__ = ["EXIT_CODES_TEXT", "MAX_MINUTES", "build_parser", "main", "parse_args"]
