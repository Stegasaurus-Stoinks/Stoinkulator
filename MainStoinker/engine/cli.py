"""Command line: run an offline backtest and write the trades to a file.

    python -m MainStoinker backtest --algos ALGOS.json --data DIR --out DIR

This is a backtest-only tool. It builds a `CsvBarFeed`, one simulated broker
per algo (a `SimBroker`, or with `--fills realistic` a `PaperBroker` reporting
to a `Ledger`; see `modes.build_router`), a `NullSink` and an `Engine`. There
is no live subcommand and no option that selects a feed or a real broker.
Each config object in an entry's `"data"` may carry `"mode"`: `backtest`
(default) or `paper`, both simulated; `"live"` (in any case) is refused during
validation with exit code 2, before anything is built. `"mode"` on the entry
itself, or spelled in another case, is an error. This module never imports or references `ibapi`,
`apiApi`, the socket.io client, `Globals` or the legacy entry script.
(Loading an algo imports its module, which pulls legacy modules in
transitively; that happens only while a run is in progress.)

Fills: `--fills legacy` (default) fills at the bar close with no costs and
writes `trades.json` exactly as before and removes a `ledger.json` left in
`--out` by an earlier realistic run (only that file name). `--fills realistic`
uses the paper broker's fill model and costs, writes `trades.json` from the
ledger (same shape) and adds `ledger.json`: per algo a `summary` and the
`round_trips`, in the format of `engine/stats.py` (see `tests/golden_paper`).
`--capital` and the cost options only apply to realistic fills; giving one
with legacy fills is an error rather than being silently ignored.

Storing runs: `--db SPEC` (a SQLite file path or `sqlite:///path`, see
`store.open_store`; `:memory:` is refused, nothing durable would be stored)
keeps each run in a database, for both fill models (a ledger is attached to
every run). Every input check comes first, including that each algo id can be
imported (`check_algos_importable`: an unknown id or a module that fails to
import is an input error). Then the store is opened, before anything is
written or run; any `StoreError` while opening (a OneDrive folder, a newer
schema, not a database, an unsupported scheme) is an input error too. So a bad
input stores nothing and creates no `--out` directory. (Constructing the algos
still happens inside the run, in `--out`, because legacy constructors write
side files into the working directory; an error raised by a constructor is
therefore a failed run.) The run is recorded with `begin_run` (kind
`backtest`, the fill model, the code version, the validated config and the data
directory, symbols and first / last replayed bar), the algos with
`write_algos`; after the engine has finished the output files are written,
then the ledger's records go in with one `write_results`, then
`end_run("completed")`. A storage failure therefore never discards a finished
run's `trades.json` / `ledger.json`. An exception during the run, including a
failure writing the output files, attempts `end_run("failed")` and is
re-raised unchanged. Legacy runs store `SimBroker`'s cost-free fills with
capital 10,000; the run's `fills` column says so. Without `--db` nothing is
opened and no file is created. `trades.json` and `ledger.json` are the same
with and without `--db`.

The code version stored with a run is `<HEAD sha>` for a clean checkout and
`<HEAD sha>-dirty:<12 hex>` otherwise (see `current_code_version`): changed
tracked files and untracked, non-ignored files both make a tree dirty.

Output files: a stale `ledger.json` is removed before `trades.json` is written
(both fill models) and a realistic run writes `ledger.json` last, so a failure
never leaves a ledger from another run next to new trades.

Working directory: legacy code writes side files (`tradey.txt`) into the
current directory. For the duration of the run the working directory is
changed to `--out`, so such files land there and nothing is written to the
directory the command was started from. All paths are resolved to absolute
paths before the change.

Exit codes: 0 success; 2 usage or input error (one line on stderr, no
traceback); 3 the store failed after it was opened (one line on stderr, no
traceback): `run finished but could not be stored: ...` when the engine had
finished (the output files are already written and stay in place), otherwise
`could not record the run: ...`. Any other exception propagates with its
traceback.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .algos import close_algo_logs, load_algo
from .engine import Engine, EngineConfig
from .feed import CsvBarFeed
from .ledger import Ledger
from .modes import DEFAULT_MODE, ModeError, build_router, parse_mode
from .paper_broker import CostModel
from .report import ledger_document, ledger_trade_records, trade_records
from .sink import NullSink
from .stats import fmt4
from .store import AlgoMeta, DataMeta, RunMeta, StoreError, open_store

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_MONEY = re.compile(_NUMBER)
_NEGATIVE_MONEY = re.compile("-" + _NUMBER)
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


class RunStoreError(Exception):
    """The store failed after it was opened; one line, exit code 3.

    `finished` is True when the engine had already finished (the output files
    are written), False when the run could not even be recorded.
    """

    def __init__(self, message: str, *, finished: bool) -> None:
        super().__init__(message)
        self.finished = finished


def validate_algo_config(parsed: object) -> list[dict[str, Any]]:
    """Check the parsed `Algo_config.json` shape and return it normalised.

    The shape is `[{"ID": <algo id>, "data": [{"idname": ..., "ticker": ...,
    ...}, ...]}, ...]`. Returns a new list of `{"ID": str, "data": [cfg, ...]}`
    in file order (each cfg is the original mapping, unchanged). Raises
    `CliError` for: not a non-empty list; an entry that is not an object or
    lacks "ID" / "data"; an ID that is not a plain identifier; "data" not a
    list; a config that is not an object or lacks a string "idname" /
    "ticker"; a ticker that cannot be a file-name part; a "mode" that is not
    a string or not `backtest` / `paper` (`live`, also as `Live` or ` live `,
    gets its own message: not available yet); a "mode" key on the entry
    instead of on a config, or a config key that is "mode" in another case or
    with surrounding whitespace (duplicate JSON keys are refused earlier, by
    `read_algo_config`);
    a duplicate idname; no configs at all. Does not check that the algo
    exists. Nothing is built here. User-supplied values in messages go
    through `ascii()` so a message can always be written to any stream.
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
        if "mode" in entry:
            raise CliError(
                f'algo config entry {i}: "mode" belongs on each config in "data", '
                "not next to ID and data"
            )
        algo_id, data = entry["ID"], entry["data"]
        if not isinstance(algo_id, str) or not algo_id.isidentifier() or algo_id.startswith("_"):
            raise CliError(
                f"invalid algo id {ascii(algo_id)} in entry {i}: "
                "must be a Python identifier not starting with '_'"
            )
        if not isinstance(data, list):
            raise CliError(f'"data" of algo {ascii(algo_id)} (entry {i}) must be a list')
        for j, cfg in enumerate(data):
            where = f"algo {ascii(algo_id)} config {j}"
            if not isinstance(cfg, dict):
                raise CliError(f"{where} is not a JSON object")
            for key in cfg:
                if key != "mode" and isinstance(key, str) and key.strip().lower() == "mode":
                    raise CliError(f'{where}: key {ascii(key)} must be spelled "mode" (lower case)')
            for key in ("idname", "ticker"):
                if key not in cfg:
                    raise CliError(f'{where} is missing "{key}"')
                if not isinstance(cfg[key], str) or not cfg[key]:
                    raise CliError(f'{where}: "{key}" must be a non-empty string')
            if not _SYMBOL.fullmatch(cfg["ticker"]):
                raise CliError(f"{where}: invalid ticker {ascii(cfg['ticker'])}")
            try:
                parse_mode(cfg["idname"], cfg.get("mode", DEFAULT_MODE))
            except ModeError as exc:
                raise CliError(str(exc)) from exc
            if cfg["idname"] in seen:
                raise CliError(f"duplicate idname {ascii(cfg['idname'])}: idnames must be unique")
            seen.add(cfg["idname"])
            total += 1
        entries.append({"ID": algo_id, "data": list(data)})
    if total == 0:
        raise CliError("algo config contains no algo configs: nothing to run")
    return entries


def config_symbols(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Tickers in first-appearance order across the validated config."""
    return list(dict.fromkeys(cfg["ticker"] for entry in entries for cfg in entry["data"]))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`object_pairs_hook` that refuses a key given twice in one JSON object.

    `json` would otherwise keep the last value silently, so a config could
    carry `"mode": "live"` and `"mode": "paper"` and only the second would count.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise CliError(f"duplicate key {ascii(key)} in the algo config")
        seen[key] = value
    return seen


def read_algo_config(path: Path) -> list[dict[str, Any]]:
    """Read and validate an algo config file; every problem is a `CliError`."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"cannot read algos file {path}: {exc}") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_no_duplicate_keys)
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
            "land there and nothing is written to the starting directory. Each config "
            'in an algo\'s "data" may set "mode": "backtest" (default) or "paper" '
            "(both simulated); \"live\" is not available yet and is refused. A legacy-fills "
            "run deletes a ledger.json left in --out by an earlier realistic run."
        ),
        epilog=(
            "exit codes: 0 success; 2 usage or input error (nothing is stored, nothing is "
            "written to --out); 3 the database failed after it was opened (the run could "
            "not be recorded, or it finished and its output files were written but it "
            "could not be stored)"
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
        "(and ledger.json with --fills realistic; legacy fills remove a stale one)",
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
    bt.add_argument(
        "--db",
        default=None,
        metavar="SPEC",
        help=(
            "also store the run in this SQLite database (a file path or sqlite:///path; "
            "created if missing). A path inside a OneDrive folder is refused. Without "
            "--db nothing is stored and no database file is created"
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


def check_algos_importable(entries: Sequence[Mapping[str, Any]]) -> None:
    """Raise `CliError` unless every algo id's module imports and has an `Algo` class.

    Done before the store is opened and before `--out` exists, so a typo in an
    algo id is an input error that stores nothing. It only imports (the same
    module name `load_algo` uses); constructing the algos waits until the run
    is in `--out`, because legacy constructors write side files into the
    working directory.
    """
    for algo_id in dict.fromkeys(entry["ID"] for entry in entries):
        module_name = f"MainStoinker.Algos.{algo_id}.{algo_id}_Algo"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name
            if missing is None or module_name == missing or module_name.startswith(missing + "."):
                raise CliError(f"unknown algo {algo_id!r}: cannot import {module_name}") from exc
            raise CliError(f"algo {algo_id!r} cannot be imported: {exc}") from exc
        except Exception as exc:
            raise CliError(
                f"algo {algo_id!r} cannot be imported: {type(exc).__name__}: {exc}") from exc
        if not hasattr(module, "Algo"):
            raise CliError(f"algo {algo_id!r}: {module_name} has no class 'Algo'")


def parse_money(text: str, flag: str, *, positive: bool = False) -> Decimal:
    """Parse a money option from its text (never through float).

    Only plain decimal text is accepted: ASCII digits with an optional
    fraction and an optional exponent (`10`, `0.005`, `.5`, `1e3`); no sign, no
    spaces, no underscores, no `nan` / `inf`, no other Unicode digits. Raises
    `CliError` for anything else, for zero when `positive`, for a value finer
    than 0.0001 (it would round away in the ledger) or an absurdly large one.
    A negative number gets its own message.
    """
    shown = ascii(text)
    if _NEGATIVE_MONEY.fullmatch(text):
        raise CliError(f"{flag} must not be negative or carry a sign (including -0), got {shown}")
    if not _MONEY.fullmatch(text):
        raise CliError(f"{flag} must be a plain decimal number such as 0.005, got {shown}")
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern only lets valid text through
        raise CliError(f"{flag} must be a number, got {shown}") from None
    if positive and value == 0:
        raise CliError(f"{flag} must be greater than 0, got {shown}")
    if value > _LARGEST:
        raise CliError(f"{flag} is too large: {shown}")
    try:
        finer = value != value.quantize(_FINEST)
    except InvalidOperation:  # an exponent so small the quantum is out of range
        finer = True
    if finer:
        raise CliError(f"{flag} must have at most 4 decimal places, got {shown}")
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


_GIT_TIMEOUT = 15
_MAX_HASHED_FILE = 5 * 1024 * 1024  # a larger file contributes its name and size only
_CHUNK = 1024 * 1024
# Variables that would point git somewhere other than the checkout it is run in.
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _blob_ids(algorithm: str, path: Path, link: bool) -> set[str]:
    """The git object ids the file's bytes would have as a blob, without running filters.

    Two candidates: the raw bytes, and the bytes with CRLF turned into LF (what
    `core.autocrlf` / `text` attributes store for a file checked out with CRLF).
    The caller has checked the file is at most `_MAX_HASHED_FILE`.
    """
    if link:
        data = os.fsencode(os.readlink(path))
    else:
        with open(path, "rb") as handle:
            data = handle.read()
    ids = set()
    for body in (data, data.replace(b"\r\n", b"\n")):
        digest = hashlib.new(algorithm)
        digest.update(b"blob %d\0" % len(body))
        digest.update(body)
        ids.add(digest.hexdigest())
    return ids


def _feed(digest: Any, path: Path, link: bool) -> None:
    """Stream a file's bytes (or a link's target) into `digest`."""
    if link:
        digest.update(os.fsencode(os.readlink(path)))
        return
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)


def _kind(path: Path) -> tuple[str, int]:
    """('file' | 'link' | 'other' | 'missing', size) of a path, without following links."""
    try:
        info = path.lstat()
    except OSError:
        return "missing", 0
    if stat.S_ISLNK(info.st_mode):
        try:
            return "link", len(os.fsencode(os.readlink(path)))
        except OSError:
            return "missing", 0
    if stat.S_ISREG(info.st_mode):
        return "file", info.st_size
    return "other", 0


def current_code_version(
    repo: Path | None = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    git: str | None = None,
) -> str:
    """The git revision of the code: `<HEAD sha>`, with `-dirty:<12 hex>` when modified.

    `repo` is a directory inside the repository (default: the folder of this
    package). The tree counts as modified when a tracked file differs from
    HEAD (content or deleted) or an untracked, non-ignored file exists; the
    suffix is the first 12 hex digits of a sha256 over those files, sorted by
    path: for each its path, kind, size and (up to 5 MB, streamed) bytes. A
    file over 5 MB contributes its path and size only (and is compared with
    HEAD by size only). Returns `"unknown"` when git is missing or too slow,
    `repo` is not in a repository, or the output is not a commit id.

    Reading the version must not change the checkout, so git is only asked
    read-only questions that never use the index's stat data, never `git
    diff` / `git status` / `diff-index` (those refresh and may rewrite
    `.git/index`, take `index.lock` and run `clean` filters; `diff-index` does
    so for any file whose mtime is not older than the index, the "racy"
    case): `rev-parse HEAD`, `rev-parse --show-toplevel`, `ls-tree -r -l HEAD`
    (HEAD's blob ids and sizes) and `ls-files --others --exclude-standard`
    (names only). Every call passes `-c core.fsmonitor=false` and runs with
    `GIT_OPTIONAL_LOCKS=0`. Tracked files are compared with HEAD here: the
    hash git would give the file's bytes, as they are and with CRLF read as LF
    (a checkout under `core.autocrlf`), is looked up among HEAD's blobs. No
    other conversion is attempted, so a file that a custom filter changes
    counts as modified, and a file mode change alone is not noticed. The git
    executable is
    resolved once with `shutil.which` (`git` overrides it for tests). Git is
    run directly (no shell) with an explicit working directory and a timeout;
    called per run, never at import time. `run` is injectable for tests.
    """
    exe = git if git is not None else shutil.which("git")
    if not exe:
        return "unknown"
    where = Path(__file__).resolve().parent if repo is None else Path(repo)
    child_env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    child_env["GIT_OPTIONAL_LOCKS"] = "0"

    def ask(*words: str) -> bytes | None:
        try:
            done = run([exe, "-c", "core.fsmonitor=false", *words], cwd=str(where),
                       env=child_env, capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=_GIT_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    head = ask("rev-parse", "HEAD")
    sha = "" if head is None else head.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        return "unknown"
    top = ask("rev-parse", "--show-toplevel")
    tree = ask("ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD")
    others = ask("ls-files", "--others", "--exclude-standard", "--full-name", "-z")
    if top is None or tree is None or others is None:
        return "unknown"
    root = Path(os.fsdecode(top).rstrip("\r\n"))
    algorithm = "sha1" if len(sha) == 40 else "sha256"

    # (path as git prints it, tag, absolute path): T tracked and changed, D tracked and
    # deleted, U untracked.
    found: list[tuple[bytes, bytes, Path]] = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        header, tab, name = record.partition(b"\t")
        parts = header.split()
        if not tab or len(parts) != 4:
            return "unknown"
        _mode, otype, blob, blob_size = parts
        if otype != b"blob":
            continue  # a submodule is not part of this checkout's files
        path = root / os.fsdecode(name)
        kind, size = _kind(path)
        if kind == "missing":
            found.append((name, b"D", path))
            continue
        try:
            if kind in ("file", "link") and max(size, int(blob_size)) <= _MAX_HASHED_FILE:
                same = blob.decode("ascii") in _blob_ids(algorithm, path, kind == "link")
            else:  # too big to hash: name and size are all that is compared
                same = kind == "file" and size == int(blob_size)
        except (OSError, ValueError):
            same = False
        if not same:
            found.append((name, b"T", path))
    found.extend((name, b"U", root / os.fsdecode(name)) for name in others.split(b"\0") if name)
    if not found:
        return sha

    digest = hashlib.sha256()
    for name, tag, path in sorted(found, key=lambda item: (item[0], item[1])):
        kind, size = _kind(path) if tag != b"D" else ("missing", 0)
        digest.update(tag + b"\0" + name + b"\0" + kind.encode() + b"\0" + b"%d" % size + b"\0")
        if kind in ("file", "link") and size <= _MAX_HASHED_FILE:
            try:
                _feed(digest, path, kind == "link")
            except OSError:
                digest.update(b"unreadable")
        digest.update(b"\0")
    return f"{sha}-dirty:{digest.hexdigest()[:12]}"


def _db_display(spec: str) -> str:
    """The database path as shown in the success line (absolute for a file)."""
    path = spec[len("sqlite:///"):] if spec.startswith("sqlite:///") else spec
    return path if path == ":memory:" else str(Path(path).expanduser().resolve())


def run_backtest_command(
    args: argparse.Namespace,
    *,
    attach_ledger: bool = False,
    env: Mapping[str, str] | None = None,
    code_version: Callable[[], str] | None = None,
) -> int:
    """Execute the parsed `backtest` command. Raises `CliError` for input problems.

    `attach_ledger` (internal seam) books a ledger even for a legacy-fills run
    (`--db` does this). Which file is written never depends on it: legacy
    fills write `trades.json` from the algos' own trades and no `ledger.json`,
    realistic fills write both from the ledger. `env` is the environment the
    store judges OneDrive folders by (default `os.environ` at call time);
    `code_version` replaces `current_code_version` (tests).
    """
    # Resolve everything against the starting directory, before any chdir.
    algos_path = Path(args.algos).resolve()
    data_dir = Path(args.data).resolve()
    out_dir = Path(args.out).resolve()
    if args.warmup_days < 0:
        raise CliError("--warmup-days must be 0 or more")
    if args.db is not None and args.db.removeprefix("sqlite:///") == ":memory:":
        raise CliError("--db :memory: would keep nothing once the command ends; "
                       "give a database file")
    costs, capital = parse_fill_options(args)

    entries = read_algo_config(algos_path)
    check_algos_importable(entries)
    if not data_dir.is_dir():
        raise CliError(f"data directory not found: {data_dir}")
    try:
        feed = CsvBarFeed(data_dir, config_symbols(entries), args.warmup_days)
    except (ValueError, FileNotFoundError) as exc:
        raise CliError(str(exc)) from exc
    # One pass over the batches (they are in memory): count them and note the
    # first and last replayed bar, which the stored run records.
    bar_count = 0
    first_bar: datetime | None = None
    last_bar: datetime | None = None
    for stamp, _bars in feed:
        bar_count += 1
        if first_bar is None:
            first_bar = stamp
        last_bar = stamp

    realistic = args.fills == "realistic"
    idnames = [cfg["idname"] for entry in entries for cfg in entry["data"]]
    ledger = Ledger() if (realistic or attach_ledger or args.db is not None) else None
    try:
        router = build_router(entries, fills=args.fills, costs=costs, capital=capital,
                              ledger=ledger)
    except ModeError as exc:
        raise CliError(str(exc)) from exc

    # Every input problem is behind us, the algo ids included. The store comes
    # next, before the output directory is touched, so a refused database leaves
    # nothing behind.
    store = None
    db_shown = ""
    if args.db is not None:
        try:
            store = open_store(args.db, env=os.environ if env is None else env)
        except StoreError as exc:
            raise CliError(str(exc)) from exc
        db_shown = _db_display(args.db)
    try:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CliError(f"cannot create output directory {out_dir}: {exc}") from exc
        trades_path = out_dir / TRADES_NAME
        ledger_path = out_dir / LEDGER_NAME
        log_path = out_dir / LOG_NAME if args.log else None
        run_id: int | None = None
        engine_done = False
        try:
            if store is not None:
                run_id = store.begin_run(RunMeta(
                    kind="backtest",
                    fills=args.fills,
                    code_version=(code_version or current_code_version)(),
                    config={
                        "entries": entries,
                        "warmup_days": args.warmup_days,
                        "capital": fmt4(capital),
                        "costs": {
                            "commission_per_share": fmt4(costs.commission_per_share),
                            "commission_minimum": fmt4(costs.commission_minimum),
                            "slippage_per_share": fmt4(costs.slippage_per_share),
                        } if realistic else None,
                    },
                    data=DataMeta(source="csv", directory=str(data_dir),
                                  symbols=tuple(feed.symbols()), first_bar=first_bar,
                                  last_bar=last_bar),
                ))
                store.write_algos(run_id, [
                    AlgoMeta(cfg["idname"], entry["ID"], cfg["ticker"], router.modes[cfg["idname"]],
                             capital, cfg)
                    for entry in entries for cfg in entry["data"]
                ])
            algos: list = []
            try:
                with _working_directory(out_dir), _maybe_quiet(args.quiet):
                    _load_algos(entries, log_path, algos)
                    engine = Engine(EngineConfig(), feed, router, NullSink(), algos,
                                    ledger=ledger, end_commission=router.end_commission)
                    engine.run_backtest()
                    engine_done = True
                    if realistic:
                        assert ledger is not None
                        result = {i: ledger_trade_records(ledger, i) for i in idnames}
                    else:
                        result = {
                            idname: trade_records(algo)
                            for idname, algo in zip(idnames, algos, strict=True)
                        }
            finally:
                close_algo_logs(algos)

            summary: dict[str, str] = {}
            document = None
            if realistic:
                assert ledger is not None
                document = ledger_document(
                    ledger, idnames, fills=args.fills, costs=costs, capital=capital,
                    cancelled_entries={i: router.cancelled_entries(i) for i in idnames},
                )
                summary = {
                    idname: (f"net {acct['summary']['net']}, "
                             f"commission {acct['summary']['commission']}, "
                             f"final equity {acct['summary']['final_equity']}")
                    for idname, acct in document["accounts"].items()
                }
            # A ledger.json from an earlier run into this directory must never sit
            # beside this run's trades.json: remove it first, write the new one last.
            removed_stale = ledger_path.exists() or ledger_path.is_symlink()
            try:
                ledger_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CliError(f"cannot remove stale {ledger_path}: {exc}") from exc
            write_json_atomic(trades_path, result)
            if document is not None:
                write_json_atomic(ledger_path, document)
            # The output files are done; only now is the run stored, so a storage
            # failure cannot take a finished run's files away.
            if store is not None:
                assert run_id is not None and ledger is not None
                store.write_results(
                    run_id,
                    order_events=ledger.orders,
                    fills=ledger.fills,
                    round_trips=ledger.round_trips,
                    equity=ledger.equity_points,
                    ignored_events=ledger.ignored_events,
                )
                store.end_run(run_id, "completed")
        except BaseException as exc:
            if store is not None and run_id is not None:
                # Whatever goes wrong here must not hide the error being handled.
                with contextlib.suppress(Exception):
                    store.end_run(run_id, "failed")
            if isinstance(exc, StoreError):
                raise RunStoreError(str(exc), finished=engine_done) from exc
            raise
    finally:
        if store is not None:
            store.close()

    print(f"replayed {bar_count} bars for {', '.join(feed.symbols())}")
    for idname, records in result.items():
        extra = f" ({summary[idname]})" if idname in summary else ""
        print(f"{idname}: {len(records)} trades{extra}")
    print(f"wrote {trades_path}")
    if realistic:
        print(f"wrote {ledger_path}")
    elif removed_stale:
        print(f"removed stale {ledger_path} (left by an earlier realistic run)")
    if run_id is not None:
        print(f"stored run {run_id} in {db_shown}")
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Entry point. Returns the exit code; argparse errors and `--help` raise `SystemExit`.

    `env` is the environment the store judges OneDrive folders by; None means
    `os.environ` when the command runs.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backtest":
            return run_backtest_command(args, env=env)
    except CliError as exc:
        _report("error: " + str(exc))
        return 2
    except RunStoreError as exc:
        what = ("run finished but could not be stored" if exc.finished
                else "could not record the run")
        _report(f"error: {what}: {exc}")
        return 3
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


def _report(message: str) -> None:
    """One line on stderr. Values from the config are shown through ascii(); paths and
    OS messages are not, so never let an unencodable character turn this into a traceback."""
    line = " ".join(message.split())
    encoding = getattr(sys.stderr, "encoding", None) or "ascii"
    print(line.encode(encoding, "backslashreplace").decode(encoding), file=sys.stderr)

