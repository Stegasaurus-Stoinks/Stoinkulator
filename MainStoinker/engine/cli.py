"""Command line: run an offline backtest and write the trades to a file.

    python -m MainStoinker backtest --algos ALGOS.json --data DIR --out DIR

This is a backtest-only tool. It builds a `CsvBarFeed`, one simulated broker
per algo (a `SimBroker`, or with `--fills realistic` a `PaperBroker` reporting
to a `Ledger`; see `modes.build_router`), a `NullSink` and an `Engine`. There
is no live subcommand and no option that selects a feed or a real broker.
An algo config object may carry `"mode"`: `backtest` (default) or `paper`,
both simulated; `"live"` is refused during validation with exit code 2,
before anything is built. This module never imports or references `ibapi`,
`apiApi`, the socket.io client, `Globals` or the legacy entry script.
(Loading an algo imports its module, which pulls legacy modules in
transitively; that happens only while a run is in progress.)

Fills: `--fills legacy` (default) fills at the bar close with no costs and
writes `trades.json` exactly as before. `--fills realistic` uses the paper
broker's fill model and costs, writes `trades.json` from the ledger (same
shape) and adds `ledger.json` (round trips, commission, net, final equity).
`--capital` and the cost options only apply to realistic fills; giving one
with legacy fills is an error rather than being silently ignored.

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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .algos import close_algo_logs, load_algo
from .engine import Engine, EngineConfig
from .feed import CsvBarFeed
from .ledger import Ledger
from .modes import DEFAULT_MODE, ModeError, build_router, ledger_document, parse_mode
from .paper_broker import CostModel
from .report import ledger_trade_records, trade_records
from .sink import NullSink

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
LOG_NAME = "backtest.log"
TRADES_NAME = "trades.json"
LEDGER_NAME = "ledger.json"

DEFAULT_CAPITAL = "10000"
DEFAULT_COMMISSION_PER_SHARE = "0.005"
DEFAULT_COMMISSION_MIN = "1.00"
DEFAULT_SLIPPAGE_PER_SHARE = "0"
_FINEST = Decimal("0.0001")
_LARGEST = Decimal("1e12")
# Options that only make sense with realistic fills: option dest -> flag.
_REALISTIC_ONLY = {
    "capital": "--capital",
    "commission_per_share": "--commission-per-share",
    "commission_min": "--commission-min",
    "slippage_per_share": "--slippage-per-share",
}


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
    "ticker"; a ticker that cannot be a file-name part; a "mode" that is not
    a string or not `backtest` / `paper` (`live` gets its own message: not
    available yet); a duplicate idname; no configs at all. Does not check
    that the algo exists. Nothing is built here.
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
            try:
                parse_mode(cfg["idname"], cfg.get("mode", DEFAULT_MODE))
            except ModeError as exc:
                raise CliError(str(exc)) from exc
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
        "--out",
        required=True,
        metavar="DIR",
        help="output directory (created if missing); gets trades.json "
        "(and ledger.json with --fills realistic)",
    )
    bt.add_argument(
        "--warmup-days", type=int, default=2, metavar="N", help="days of warm-up history (default 2)"
    )
    bt.add_argument(
        "--fills",
        choices=("legacy", "realistic"),
        default="legacy",
        help=(
            "fill model for the whole run: legacy (default) fills at the bar close with no "
            "costs and writes trades.json as before; realistic uses the paper broker (next-bar "
            "open entries, stops, targets, commission) and also writes ledger.json"
        ),
    )
    bt.add_argument(
        "--capital",
        default=None,
        metavar="AMOUNT",
        help=f"starting capital per algo, realistic fills only (default {DEFAULT_CAPITAL})",
    )
    bt.add_argument(
        "--commission-per-share",
        default=None,
        metavar="AMOUNT",
        help=f"commission per share, realistic fills only (default {DEFAULT_COMMISSION_PER_SHARE})",
    )
    bt.add_argument(
        "--commission-min",
        default=None,
        metavar="AMOUNT",
        help=f"minimum commission per order, realistic fills only (default {DEFAULT_COMMISSION_MIN})",
    )
    bt.add_argument(
        "--slippage-per-share",
        default=None,
        metavar="AMOUNT",
        help=(
            "slippage per share on market and stop fills, realistic fills only "
            f"(default {DEFAULT_SLIPPAGE_PER_SHARE}). Money options must be >= 0 (capital > 0), "
            "finite, with at most 4 decimals"
        ),
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


def parse_money(text: str, flag: str, *, positive: bool = False) -> Decimal:
    """Parse a money option from its text (never through float).

    Raises `CliError` for text that is not a number, a non-finite value, a
    negative value (or zero when `positive`), a value finer than 0.0001 (it
    would round away in the ledger) or an absurdly large one.
    """
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise CliError(f"{flag} must be a number, got {text!r}") from None
    if not value.is_finite():
        raise CliError(f"{flag} must be a finite number, got {text!r}")
    if value < 0 or (positive and value == 0):
        raise CliError(f"{flag} must be {'greater than' if positive else 'at least'} 0, got {text!r}")
    if value > _LARGEST:
        raise CliError(f"{flag} is too large: {text!r}")
    if value != value.quantize(_FINEST):
        raise CliError(f"{flag} must have at most 4 decimal places, got {text!r}")
    return value


def parse_fill_options(args: argparse.Namespace) -> tuple[CostModel, Decimal]:
    """Validate `--fills` and the cost options; return the cost model and capital.

    Capital and the cost options only apply to realistic fills: given with
    legacy fills they are a `CliError`.
    """
    if args.fills == "legacy":
        given = [flag for dest, flag in _REALISTIC_ONLY.items() if getattr(args, dest) is not None]
        if given:
            raise CliError(
                f"{', '.join(given)} only apply with --fills realistic "
                "(legacy fills have no costs and no ledger)"
            )

    def option(dest: str, default: str, *, positive: bool = False) -> Decimal:
        text = getattr(args, dest)
        return parse_money(default if text is None else text, _REALISTIC_ONLY[dest],
                           positive=positive)

    capital = option("capital", DEFAULT_CAPITAL, positive=True)
    costs = CostModel(
        option("commission_per_share", DEFAULT_COMMISSION_PER_SHARE),
        option("commission_min", DEFAULT_COMMISSION_MIN),
        option("slippage_per_share", DEFAULT_SLIPPAGE_PER_SHARE),
    )
    return costs, capital


def run_backtest_command(args: argparse.Namespace) -> int:
    """Execute the parsed `backtest` command. Raises `CliError` for input problems."""
    # Resolve everything against the starting directory, before any chdir.
    algos_path = Path(args.algos).resolve()
    data_dir = Path(args.data).resolve()
    out_dir = Path(args.out).resolve()
    if args.warmup_days < 0:
        raise CliError("--warmup-days must be 0 or more")
    costs, capital = parse_fill_options(args)

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
    ledger_path = out_dir / LEDGER_NAME
    log_path = out_dir / LOG_NAME if args.log else None

    realistic = args.fills == "realistic"
    idnames = [cfg["idname"] for entry in entries for cfg in entry["data"]]
    # Legacy fills attach no ledger: the output is written from the algos' own trades.
    ledger = Ledger() if realistic else None
    try:
        router = build_router(entries, fills=args.fills, costs=costs, capital=capital,
                              ledger=ledger)
    except ModeError as exc:
        raise CliError(str(exc)) from exc

    algos: list = []
    summary: dict[str, str] = {}
    try:
        with _working_directory(out_dir), _maybe_quiet(args.quiet):
            _load_algos(entries, log_path, algos)
            engine = Engine(EngineConfig(), feed, router, NullSink(), algos,
                            ledger=ledger, end_commission=router.end_commission)
            engine.run_backtest()
            if ledger is None:
                result = {
                    idname: trade_records(algo)
                    for idname, algo in zip(idnames, algos, strict=True)
                }
            else:
                result = {idname: ledger_trade_records(ledger, idname) for idname in idnames}
    finally:
        close_algo_logs(algos)

    write_json_atomic(trades_path, result)
    if ledger is not None:
        document = ledger_document(ledger, idnames, costs=costs, capital=capital)
        write_json_atomic(ledger_path, document)
        summary = {
            idname: (f"net {acct['summary']['net']}, commission {acct['summary']['commission']}, "
                     f"final equity {acct['summary']['final_equity']}")
            for idname, acct in document["accounts"].items()
        }
    print(f"replayed {bar_count} bars for {', '.join(feed.symbols())}")
    for idname, records in result.items():
        extra = f" ({summary[idname]})" if idname in summary else ""
        print(f"{idname}: {len(records)} trades{extra}")
    print(f"wrote {trades_path}")
    if ledger is not None:
        print(f"wrote {ledger_path}")
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

