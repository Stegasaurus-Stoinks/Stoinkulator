"""Command line: run an offline backtest and write the trades to a file.

    python -m MainStoinker backtest --algos ALGOS.json --data DIR --out DIR

This is a backtest-only tool. It builds exactly four things: a `CsvBarFeed`,
a `SimBroker`, a `NullSink` and an `Engine`. There is no live subcommand and
no option that selects a broker or a feed, and this module never imports or
references `ibapi`, `apiApi`, the socket.io client, `Globals` or the legacy
entry script. (Loading an algo imports its module, which pulls legacy modules
in transitively; that happens only while a run is in progress.)

Working directory: legacy code writes side files (`tradey.txt`) into the
current directory. For the duration of the run the working directory is
changed to `--out`, so such files land there and nothing is written to the
directory the command was started from. All paths are resolved to absolute
paths before the change.

Exit codes: 0 success; 2 usage or input error (one line on stderr, no
traceback). Any other exception propagates with its traceback.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .algos import close_algo_logs, load_algo
from .broker import SimBroker
from .engine import Engine, EngineConfig
from .feed import CsvBarFeed
from .report import trade_records
from .sink import NullSink

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
LOG_NAME = "backtest.log"
TRADES_NAME = "trades.json"


class CliError(Exception):
    """A usage or input problem; the message is shown as one line, exit code 2."""


def validate_algo_config(parsed: object) -> list[dict[str, Any]]:
    """Check the parsed `Algo_config.json` shape and return it normalised.

    The shape is `[{"ID": <algo id>, "data": [{"idname": ..., "ticker": ...,
    ...}, ...]}, ...]`. Returns a new list of `{"ID": str, "data": [cfg, ...]}`
    in file order (each cfg is the original mapping, unchanged). Raises
    `CliError` for: not a non-empty list; an entry that is not an object or
    lacks "ID" / "data"; an ID that is not a plain identifier; "data" not a
    list; a config that is not an object or lacks a string "idname" /
    "ticker"; a ticker that cannot be a file-name part; a duplicate idname;
    no configs at all. Does not check that the algo exists.
    """
    if not isinstance(parsed, list):
        raise CliError("algo config must be a JSON list of {ID, data} entries")
    if not parsed:
        raise CliError("algo config is empty: no algos to run")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise CliError(f"algo config entry {i} is not a JSON object")
        if "ID" not in entry:
            raise CliError(f'algo config entry {i} is missing "ID"')
        if "data" not in entry:
            raise CliError(f'algo config entry {i} is missing "data"')
        algo_id, data = entry["ID"], entry["data"]
        if not isinstance(algo_id, str) or not algo_id.isidentifier() or algo_id.startswith("_"):
            raise CliError(
                f"invalid algo id {algo_id!r} in entry {i}: "
                "must be a Python identifier not starting with '_'"
            )
        if not isinstance(data, list):
            raise CliError(f'"data" of algo {algo_id!r} (entry {i}) must be a list')
        for j, cfg in enumerate(data):
            where = f"algo {algo_id!r} config {j}"
            if not isinstance(cfg, dict):
                raise CliError(f"{where} is not a JSON object")
            for key in ("idname", "ticker"):
                if key not in cfg:
                    raise CliError(f'{where} is missing "{key}"')
                if not isinstance(cfg[key], str) or not cfg[key]:
                    raise CliError(f'{where}: "{key}" must be a non-empty string')
            if not _SYMBOL.fullmatch(cfg["ticker"]):
                raise CliError(f"{where}: invalid ticker {cfg['ticker']!r}")
            if cfg["idname"] in seen:
                raise CliError(f"duplicate idname {cfg['idname']!r}: idnames must be unique")
            seen.add(cfg["idname"])
            total += 1
        entries.append({"ID": algo_id, "data": list(data)})
    if total == 0:
        raise CliError("algo config contains no algo configs: nothing to run")
    return entries


def config_symbols(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Tickers in first-appearance order across the validated config."""
    return list(dict.fromkeys(cfg["ticker"] for entry in entries for cfg in entry["data"]))


def read_algo_config(path: Path) -> list[dict[str, Any]]:
    """Read and validate an algo config file; every problem is a `CliError`."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"cannot read algos file {path}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise CliError(f"algos file {path} is not valid JSON: {exc}") from exc
    return validate_algo_config(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker",
        description="Stoinkulator command line (offline backtests only).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    bt = sub.add_parser(
        "backtest",
        help="replay offline bars through algos and write the trades",
        description=(
            "Replay offline 1-minute bars through the algos in a config file, with a "
            "simulated broker, and write the trades to <out>/trades.json. Never "
            "connects to a broker. Relative paths are relative to the directory the "
            "command was started in. While the run is in progress the working "
            "directory is --out, so side files written by legacy code (tradey.txt) "
            "land there and nothing is written to the starting directory."
        ),
    )
    bt.add_argument(
        "--algos", required=True, metavar="JSON", help="algo config file (Algo_config.json shape)"
    )
    bt.add_argument(
        "--data", required=True, metavar="DIR", help="directory holding _<SYMBOL>_offlinedata_ files"
    )
    bt.add_argument(
        "--out", required=True, metavar="DIR", help="output directory (created if missing); gets trades.json"
    )
    bt.add_argument(
        "--warmup-days", type=int, default=2, metavar="N", help="days of warm-up history (default 2)"
    )
    bt.add_argument("--log", action="store_true", help="write algo logs to <out>/backtest.log")
    bt.add_argument(
        "--quiet", action="store_true", help="discard the algos' stdout prints during the run"
    )
    return parser


@contextlib.contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def _maybe_quiet(quiet: bool) -> Iterator[None]:
    if not quiet:
        yield
        return
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        yield


def write_json_atomic(path: Path, payload: object) -> None:
    """Write `payload` as the golden-file JSON format via a temp file and `os.replace`."""
    text = json.dumps(payload, indent=1) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _load_algos(entries: Sequence[Mapping[str, Any]], log_path: Path | None, algos: list) -> None:
    """Load every algo into `algos` (so the caller can close logs after a failure)."""
    for entry in entries:
        for cfg in entry["data"]:
            try:
                algos.append(load_algo(entry["ID"], cfg, log_path=log_path, log_level=logging.INFO))
            except ValueError as exc:
                # load_algo's own errors (unknown / invalid / no `Algo` class) chain
                # a cause or name the id; an algo's constructor errors do neither.
                if exc.__cause__ is not None or str(exc).startswith("invalid algo id"):
                    raise CliError(str(exc)) from exc
                raise


def run_backtest_command(args: argparse.Namespace) -> int:
    """Execute the parsed `backtest` command. Raises `CliError` for input problems."""
    # Resolve everything against the starting directory, before any chdir.
    algos_path = Path(args.algos).resolve()
    data_dir = Path(args.data).resolve()
    out_dir = Path(args.out).resolve()
    if args.warmup_days < 0:
        raise CliError("--warmup-days must be 0 or more")

    entries = read_algo_config(algos_path)
    if not data_dir.is_dir():
        raise CliError(f"data directory not found: {data_dir}")
    try:
        feed = CsvBarFeed(data_dir, config_symbols(entries), args.warmup_days)
    except (ValueError, FileNotFoundError) as exc:
        raise CliError(str(exc)) from exc
    bar_count = sum(1 for _ in feed)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(f"cannot create output directory {out_dir}: {exc}") from exc
    trades_path = out_dir / TRADES_NAME
    log_path = out_dir / LOG_NAME if args.log else None

    algos: list = []
    try:
        with _working_directory(out_dir), _maybe_quiet(args.quiet):
            _load_algos(entries, log_path, algos)
            engine = Engine(EngineConfig(), feed, SimBroker(), NullSink(), algos)
            engine.run_backtest()
            idnames = [cfg["idname"] for entry in entries for cfg in entry["data"]]
            result = {
                idname: trade_records(algo) for idname, algo in zip(idnames, algos, strict=True)
            }
    finally:
        close_algo_logs(algos)

    write_json_atomic(trades_path, result)
    print(f"replayed {bar_count} bars for {', '.join(feed.symbols())}")
    for idname, records in result.items():
        print(f"{idname}: {len(records)} trades")
    print(f"wrote {trades_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the exit code; argparse errors and `--help` raise `SystemExit`."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backtest":
            return run_backtest_command(args)
    except CliError as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover

