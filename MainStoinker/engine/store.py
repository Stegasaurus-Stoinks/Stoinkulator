"""Persistence for runs: the `Store` protocol, `NullStore` and `SqliteStore`.

A store keeps what the ledger produced (frozen records from `ledger.py`) so
that runs can be listed, inspected and compared later without re-running them.
It never computes anything itself and imports nothing legacy, no pandas and no
ibapi. Times are accepted as `datetime` (a pandas Timestamp is one) and stored
with `isoformat()`.

SQLite details (stdlib `sqlite3` only):
- WAL journal, `foreign_keys=ON`, schema version in `PRAGMA user_version`.
  Migrations are an ordered list of scripts applied in one transaction. A
  database newer than this code is refused with `StoreError`.
- Money, prices and quantities are INTEGER columns in units of 0.0001 (`_e4`)
  and come back as exact `Decimal`. A value finer than 0.0001 is refused.
- Run start and end are UTC ISO text. Bar times are stored exactly as given
  (naive America/New_York time of the data; runs stored before that change keep Pacific times).
- `begin_run` and `write_algos` commit at once. A run that is never ended
  stays `running`; a second connection can read committed data meanwhile.
- Order events are stored as the event log (`order_events` table, one row per
  placed / modified / cancelled event), not collapsed into an `orders` table.
  An order whose position was closed by the synthetic end-of-data fill has no
  cancel event, and that fill has `order_id` None; both are stored as is.
- Equity is a step function: a point is stored only when its values differ
  from the last stored point of the same series, plus the first and the last
  point of each calendar date. `expand_equity` rebuilds the full series on a
  time grid exactly. The compressor keeps per-run state in the store object,
  so `write_results` may be called repeatedly (each record exactly once) and
  `end_run` flushes the final point of every series.
- A database file is refused inside a OneDrive folder (`is_cloud_synced`),
  by the OneDrive environment variables and by any path component whose name
  starts with "OneDrive". `SqliteStore` itself applies the refusal, so it
  cannot be bypassed by constructing the store directly.
- Opening a fresh file from several threads or processes at once is safe:
  the switch to WAL and the migration wait for each other.

Writing a completed run: write the equity series once, after `ledger.finish`.
`finish` replaces the points of the last bar, so equity written before it and
again after it would be stored twice. `order_events`, `trades` and
`ignored_events` are not de-duplicated either (only fills have a key); give
each record exactly once. A reader of a `running` run sees equity only up to
the last stored change, not up to the latest bar.

A `SqliteStore` belongs to the thread that made it (sqlite3's rule).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

from .ledger import (
    AccountInfo,
    EquityPoint,
    Fill,
    IgnoredEvent,
    OrderEvent,
    RoundTrip,
)

RunStatus = Literal["running", "completed", "failed"]
CLOUD_ENV_VARS: tuple[str, ...] = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
SUGGESTED_PATH = r"%USERPROFILE%\Stoinkulator\runs.db"
_SCALE = Decimal(10000)
_QUANT = Decimal("0.0001")
_INT64_MAX = 2**63 - 1
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


class StoreError(Exception):
    """Anything that stops the store from doing what was asked."""


def _readonly_uri(path: Path) -> str:
    """SQLite `file:` URI (with `?mode=ro`) for an absolute path; pure, no I/O.

    Separators become `/` and everything but `/` and `:` is percent-encoded,
    so `#`, `%`, `?`, spaces and non-ASCII stay part of the file name. A drive
    path gives `file:///C:/a/b.db`; a UNC path `\\\\server\\share\\b.db` gives
    `file:////server/share/b.db` (empty authority, which SQLite accepts where
    `Path.as_uri()`'s `file://server/...` is rejected); a POSIX path gives
    `file:///tmp/b.db`.
    """
    text = str(path).replace("\\", "/")
    prefix = "file://" if text.startswith("/") else "file:///"
    return prefix + quote(text, safe="/:") + "?mode=ro"


# --- plain records ------------------------------------------------------------


@dataclass(frozen=True)
class DataMeta:
    """Where a run's bars came from. Bars themselves are not stored."""

    source: str = ""
    directory: str = ""
    symbols: tuple[str, ...] = ()
    first_bar: datetime | None = None
    last_bar: datetime | None = None
    # (symbol, sha256 hex, size in bytes) of each data file; kept in `data_json`.
    # Rows written before schema 2 have no such key and read back as ().
    files: tuple[tuple[str, str, int], ...] = ()


@dataclass(frozen=True)
class RunMeta:
    """What `begin_run` records. `config` must be JSON-serialisable."""

    kind: str  # "backtest", "paper", ...
    fills: str  # fill model: "legacy" or "realistic"
    code_version: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    data: DataMeta = field(default_factory=DataMeta)


@dataclass(frozen=True)
class AlgoMeta:
    idname: str
    algo_id: str
    ticker: str
    mode: str
    capital: Decimal
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SeriesInfo:
    """One indicator series of an algo: what it is called and how to draw it."""

    account: str  # the algo's idname
    name: str
    draw_type: str
    position: int  # drawing order / pane, as the algo declared it


@dataclass(frozen=True)
class SeriesPoint:
    """One value of an indicator series. `time` is stored as given (data time)."""

    account: str
    name: str
    time: datetime
    value: float  # finite


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    started_at: datetime  # UTC, aware
    ended_at: datetime | None  # None while status is "running"
    status: RunStatus
    kind: str
    fills: str
    code_version: str
    data: DataMeta


@dataclass(frozen=True)
class RunData:
    """Everything stored for one run, as the ledger's own record types.

    `equity` holds the stored (change-only) points of every series, in write
    order. Each series (one per algo, plus the account total whose `account`
    is None) is a step function: its value at time t is the last stored point
    at or before t. Use `equity_series` and `expand_equity` to read it.
    """

    summary: RunSummary
    config: Mapping[str, Any]
    algos: tuple[AlgoMeta, ...]
    order_events: tuple[OrderEvent, ...]
    fills: tuple[Fill, ...]
    round_trips: tuple[RoundTrip, ...]
    equity: tuple[EquityPoint, ...]
    ignored_events: tuple[IgnoredEvent, ...]
    # Only the infos of the indicator series; their points are not loaded here,
    # use `load_series`. Empty for a schema-1 database.
    series: tuple[SeriesInfo, ...] = ()

    def accounts(self) -> tuple[AccountInfo, ...]:
        return tuple(AccountInfo(a.idname, a.ticker, a.capital) for a in self.algos)

    def equity_series(self, account: str | None = None) -> tuple[EquityPoint, ...]:
        """Stored points of one algo, or of the account total when `account` is None."""
        return tuple(p for p in self.equity if p.account == account)


def expand_equity(points: Sequence[EquityPoint], times: Iterable[datetime]) -> list[EquityPoint]:
    """Rebuild a full series from stored step points on a grid of times.

    `points` are one series in time order. Each grid time gets the last point
    at or before it, restamped with that time; times before the first point
    are skipped. Grid times are the bar times of the run.
    """
    stamps = [p.time for p in points]
    out: list[EquityPoint] = []
    for t in times:
        i = bisect_right(stamps, t)
        if i:
            out.append(replace(points[i - 1], time=t))
    return out


# --- protocol and null store --------------------------------------------------


class Store(Protocol):
    @property
    def schema_version(self) -> int: ...

    def begin_run(self, meta: RunMeta) -> int: ...

    def write_algos(self, run_id: int, algos: Sequence[AlgoMeta]) -> None: ...

    def write_results(
        self,
        run_id: int,
        *,
        order_events: Iterable[OrderEvent] = (),
        fills: Iterable[Fill] = (),
        round_trips: Iterable[RoundTrip] = (),
        equity: Iterable[EquityPoint] = (),
        ignored_events: Iterable[IgnoredEvent] = (),
    ) -> None: ...

    def write_series(self, run_id: int, infos: Sequence[SeriesInfo],
                     points: Iterable[SeriesPoint] = ()) -> None: ...

    def end_run(self, run_id: int, status: RunStatus) -> None: ...

    def list_runs(self) -> tuple[RunSummary, ...]: ...

    def load_run(self, run_id: int) -> RunData: ...

    def load_series(self, run_id: int, algo: str, start: datetime | None = None,
                    end: datetime | None = None) -> tuple[SeriesPoint, ...]: ...

    def close(self) -> None: ...


class NullStore:
    """Accepts everything and keeps nothing."""

    def __init__(self) -> None:
        self._next = 1

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    def begin_run(self, meta: RunMeta) -> int:
        run_id = self._next
        self._next += 1
        return run_id

    def write_algos(self, run_id: int, algos: Sequence[AlgoMeta]) -> None:
        return None

    def write_results(
        self,
        run_id: int,
        *,
        order_events: Iterable[OrderEvent] = (),
        fills: Iterable[Fill] = (),
        round_trips: Iterable[RoundTrip] = (),
        equity: Iterable[EquityPoint] = (),
        ignored_events: Iterable[IgnoredEvent] = (),
    ) -> None:
        return None

    def write_series(self, run_id: int, infos: Sequence[SeriesInfo],
                     points: Iterable[SeriesPoint] = ()) -> None:
        return None

    def end_run(self, run_id: int, status: RunStatus) -> None:
        return None

    def list_runs(self) -> tuple[RunSummary, ...]:
        return ()

    def load_run(self, run_id: int) -> RunData:
        raise StoreError("the null store keeps no runs")

    def load_series(self, run_id: int, algo: str, start: datetime | None = None,
                    end: datetime | None = None) -> tuple[SeriesPoint, ...]:
        return ()

    def close(self) -> None:
        return None

    def __enter__(self) -> NullStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- OneDrive refusal ---------------------------------------------------------


def is_cloud_synced(path: str | os.PathLike[str], env: Mapping[str, str]) -> bool:
    """True if `path` is inside a OneDrive folder.

    Two independent checks on the resolved path (works when the file or its
    folders do not exist yet, and with a `\\\\?\\` prefix): it lies inside a
    folder named by `OneDrive`, `OneDriveCommercial` or `OneDriveConsumer` in
    `env` (names case-insensitive), or one of its folders is named like
    "OneDrive..." (case-insensitive). The second check works without any
    environment variable, e.g. in a scheduled task or service account.
    """
    clean = _clean(Path(path))
    if any(part.lower().startswith("onedrive") for part in Path(clean).parts[1:]):
        return True
    target = os.path.normcase(clean)
    wanted = {n.lower() for n in CLOUD_ENV_VARS}
    for name, value in env.items():
        if name.lower() in wanted and value.strip():
            root = _norm(Path(value))
            if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
                return True
    return False


def _clean(path: Path) -> str:
    text = str(path.resolve(strict=False))
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def _norm(path: Path) -> str:
    return os.path.normcase(_clean(path))


def _refuse_cloud(path: Path, env: Mapping[str, str]) -> None:
    if is_cloud_synced(path, env):
        raise StoreError(f"{path} is inside a OneDrive folder; sync can corrupt a database "
                         f"while it is written. Use {SUGGESTED_PATH} instead.")


# --- SQLite -------------------------------------------------------------------

# Ordered migrations: entry i takes the database from version i to i + 1.
# Statements are run one by one inside a single transaction.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            kind TEXT NOT NULL,
            fills TEXT NOT NULL,
            config_json TEXT NOT NULL,
            data_json TEXT NOT NULL,
            code_version TEXT NOT NULL)""",
        """CREATE TABLE algos (
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            idname TEXT NOT NULL,
            algo_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            mode TEXT NOT NULL,
            params_json TEXT NOT NULL,
            capital_e4 INTEGER NOT NULL,
            UNIQUE (run_id, idname))""",
        """CREATE TABLE order_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            algo TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            broker_ref TEXT NOT NULL,
            kind TEXT NOT NULL,
            time TEXT NOT NULL,
            side TEXT,
            quantity_e4 INTEGER,
            order_type TEXT,
            price_e4 INTEGER,
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        """CREATE TABLE fills (
            run_id INTEGER NOT NULL,
            fill_id INTEGER NOT NULL,
            order_id INTEGER,
            algo TEXT NOT NULL,
            broker_ref TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity_e4 INTEGER NOT NULL,
            price_e4 INTEGER NOT NULL,
            commission_e4 INTEGER NOT NULL,
            time TEXT NOT NULL,
            reason TEXT NOT NULL,
            synthetic INTEGER NOT NULL,
            PRIMARY KEY (run_id, fill_id),
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        """CREATE TABLE trades (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            algo TEXT NOT NULL,
            broker_ref TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity_e4 INTEGER NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            entry_price_e4 INTEGER NOT NULL,
            exit_price_e4 INTEGER NOT NULL,
            gross_e4 INTEGER NOT NULL,
            commission_e4 INTEGER NOT NULL,
            net_e4 INTEGER NOT NULL,
            duration_us INTEGER NOT NULL,
            exit_reason TEXT NOT NULL,
            synthetic INTEGER NOT NULL,
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        # algo is NULL for the account total
        """CREATE TABLE equity (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            algo TEXT,
            time TEXT NOT NULL,
            cash_e4 INTEGER NOT NULL,
            position_value_e4 INTEGER NOT NULL,
            realised_e4 INTEGER NOT NULL,
            unrealised_e4 INTEGER NOT NULL,
            equity_e4 INTEGER NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        """CREATE TABLE ignored_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            algo TEXT NOT NULL,
            broker_ref TEXT NOT NULL,
            kind TEXT NOT NULL,
            time TEXT NOT NULL,
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        "CREATE INDEX idx_order_events_run_algo ON order_events (run_id, algo)",
        "CREATE INDEX idx_fills_run_algo ON fills (run_id, algo)",
        "CREATE INDEX idx_trades_run_algo ON trades (run_id, algo)",
        "CREATE INDEX idx_equity_run_algo ON equity (run_id, algo)",
        "CREATE INDEX idx_ignored_run_algo ON ignored_events (run_id, algo)",
    ),
    # 2: indicator series (tables only; nothing existing is altered)
    (
        """CREATE TABLE algo_series (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            algo TEXT NOT NULL,
            name TEXT NOT NULL,
            draw_type TEXT NOT NULL,
            position INTEGER NOT NULL,
            UNIQUE (run_id, algo, name),
            FOREIGN KEY (run_id, algo) REFERENCES algos(run_id, idname) ON DELETE CASCADE)""",
        """CREATE TABLE algo_series_points (
            series_id INTEGER NOT NULL REFERENCES algo_series(id) ON DELETE CASCADE,
            time TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (series_id, time)) WITHOUT ROWID""",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)


def _to_e4(value: Decimal | int | str) -> int:
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise StoreError(f"{value} is not a finite number")
        scaled = number * _SCALE
        whole = scaled.to_integral_value()
    except InvalidOperation as exc:
        raise StoreError(f"{value!r} is not a number") from exc
    if scaled != whole:
        raise StoreError(f"{value} has more than 4 decimal places")
    if abs(whole) > _INT64_MAX:
        raise StoreError(f"{value} is too large to store")
    return int(whole)


def _opt_e4(value: Decimal | None) -> int | None:
    return None if value is None else _to_e4(value)


def _from_e4(value: int) -> Decimal:
    return (Decimal(value) / _SCALE).quantize(_QUANT)


def _finite(value: Any) -> float:
    if isinstance(value, (bool, str, bytes)) or type(value).__name__ in ("bool", "bool_"):
        raise StoreError(f"series value {value!r} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreError(f"series value {value!r} is not a number") from exc
    if not math.isfinite(number):
        raise StoreError(f"series value {value!r} is not finite")
    return number


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise StoreError(f"{what} {value!r} must be a non-empty string")
    return value


def _series_time(value: Any) -> str:
    """Text of a series point time: a naive `datetime` that is not NaT.

    `pd.NaT` is a `datetime` subclass that is unequal to itself and has no real
    ISO text; it is refused here without importing pandas.
    """
    if not isinstance(value, datetime) or value != value:
        raise StoreError(f"series time {value!r} must be a datetime (not NaT)")
    if value.tzinfo is not None:
        raise StoreError(f"series time {value!r} must be naive (data time)")
    return value.isoformat()


def _series_position(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) > _INT64_MAX:
        raise StoreError(f"series position {value!r} must be a 64-bit integer")
    return value


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _data_file(entry: Any) -> list[Any]:
    ok = (isinstance(entry, (tuple, list)) and len(entry) == 3
          and isinstance(entry[0], str) and isinstance(entry[1], str)
          and _SHA256.fullmatch(entry[1]) is not None
          and isinstance(entry[2], int) and not isinstance(entry[2], bool) and entry[2] >= 0)
    if not ok:
        raise StoreError(f"data file {entry!r} must be (symbol, sha256 hex, size >= 0)")
    return list(entry)


def _opt_from_e4(value: int | None) -> Decimal | None:
    return None if value is None else _from_e4(value)


def _listify(value: Any) -> Any:
    return [_listify(v) for v in value] if isinstance(value, tuple) else value


def _tuplify(value: Any) -> Any:
    return tuple(_tuplify(v) for v in value) if isinstance(value, list) else value


def _encode_ref(ref: Any) -> str:
    """broker_ref as JSON text. int, str, None and tuples of them come back equal;
    anything else cannot be read back as it was and raises `StoreError`."""
    _check_ref(ref)
    return json.dumps(_listify(ref))


def _check_ref(ref: Any) -> None:
    if isinstance(ref, tuple):
        for item in ref:
            _check_ref(item)
    elif ref is not None and not isinstance(ref, (int, str)):
        raise StoreError(f"broker ref {ref!r} of type {type(ref).__name__} cannot be stored; "
                         "use an int, a str, None or a tuple of those")


def _decode_ref(text: str) -> Any:
    return _tuplify(json.loads(text))


def _dumps(value: Any, what: str) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{what} is not JSON-serialisable: {exc}") from exc


def _dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError(f"stored time {value!r} cannot be read") from exc


def _opt_dt(value: str | None) -> datetime | None:
    return None if value is None else _dt(value)


@dataclass
class _EquityState:
    """Compressor state of one series within one run."""

    prev: EquityPoint
    prev_written: bool
    last_values: tuple[Decimal, ...]


def _values(p: EquityPoint) -> tuple[Decimal, ...]:
    return (p.cash, p.position_value, p.realised, p.unrealised, p.equity)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _enable_wal(conn: sqlite3.Connection, wait: float = 10.0) -> None:
    """Switch to WAL. The switch needs an exclusive lock and fails at once, ignoring
    the busy timeout, when another opener holds it; retry until it is free. A
    transient "disk I/O error" (seen on Windows right after a writer was killed
    hard) is retried the same way; any other error raises at once."""
    deadline = time.monotonic() + wait
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            transient = "locked" in text or "busy" in text or "disk i/o error" in text
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # the caller is already handling the original error


class SqliteStore:
    """Runs in one SQLite file. Create it with `open_store` or directly.

    A path inside a OneDrive folder is refused here too. `env` is the
    environment that judgement uses; None means `os.environ` at call time.
    `busy_timeout` is how many seconds a statement waits for another
    connection's lock (default 10); when it runs out the operation raises
    `StoreError` and the connection stays usable.
    """

    def __init__(self, path: str | os.PathLike[str], *,
                 clock: Callable[[], datetime] = _utc_now,
                 env: Mapping[str, str] | None = None,
                 busy_timeout: float = 10.0,
                 readonly: bool = False) -> None:
        self._clock = clock
        self._readonly = readonly
        self._schema_version = 0
        self._equity_state: dict[tuple[int, str | None], _EquityState] = {}
        self._path = str(path)
        if readonly:
            self._open_readonly(busy_timeout, os.environ if env is None else env)
            return
        if self._path != ":memory:":
            file = Path(self._path).expanduser()
            _refuse_cloud(file, os.environ if env is None else env)
            try:
                file.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"cannot create the folder for {self._path}: {exc}") from exc
        try:
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                self._path, isolation_level=None, timeout=busy_timeout)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {self._path}: {exc}") from exc
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            # A newer database is refused before anything is written to it: the
            # switch to WAL rewrites the file header.
            self._refuse_newer(self._conn.execute("PRAGMA user_version").fetchone()[0])
            _enable_wal(self._conn, busy_timeout)
            self._migrate()
            self._schema_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        except StoreError:
            self.close()
            raise
        except sqlite3.Error as exc:
            self.close()
            raise StoreError(f"cannot use database {self._path}: {exc}") from exc

    def _open_readonly(self, busy_timeout: float, env: Mapping[str, str]) -> None:
        """Open an existing file with `mode=ro`: no folder, no WAL switch, no migration."""
        if self._path == ":memory:":
            raise StoreError("an in-memory database holds nothing to read")
        file = Path(self._path).expanduser()
        _refuse_cloud(file, env)
        try:
            if not file.exists():
                raise StoreError(f"database {self._path} does not exist")
            if not file.is_file():
                raise StoreError(f"database {self._path} is not a file")
            uri = _readonly_uri(file.resolve())
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                uri, uri=True, isolation_level=None, timeout=busy_timeout)
        except OSError as exc:
            raise StoreError(f"cannot open database {self._path}: {exc}") from exc
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {self._path}: {exc}") from exc
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._refuse_newer(version)
            if version < 1:
                raise StoreError(f"{self._path} is not a run database (schema version 0)")
            self._schema_version = version
        except StoreError:
            self.close()
            raise
        except sqlite3.Error as exc:
            self.close()
            raise StoreError(f"cannot use database {self._path}: {exc}") from exc

    @property
    def schema_version(self) -> int:
        """The database's schema version as read when the store was opened.

        A writable store has migrated first, so it is `SCHEMA_VERSION`; a
        read-only store reports whatever the file holds (1 to `SCHEMA_VERSION`).
        """
        return self._schema_version

    # --- plumbing ---------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("the store is closed")
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. Any `sqlite3.Error` from BEGIN, the body or COMMIT
        (a locked database, a full disk) is raised as `StoreError` with the original
        as `__cause__`; the transaction is rolled back and the connection stays usable."""
        conn = self._db()
        if self._readonly:
            raise StoreError("the store was opened read-only")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise StoreError(f"cannot start a transaction: {exc}") from exc
        try:
            yield conn
        except BaseException as exc:
            _rollback(conn)
            if isinstance(exc, sqlite3.Error):
                raise StoreError(str(exc)) from exc
            raise
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise StoreError(f"commit failed: {exc}") from exc

    @staticmethod
    def _refuse_newer(version: int) -> None:
        if version > SCHEMA_VERSION:
            raise StoreError(f"database schema version {version} is newer than this code "
                             f"understands ({SCHEMA_VERSION}); update the program")

    def _migrate(self) -> None:
        conn = self._db()
        if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        with self._tx():
            # Re-read inside the write lock: another process may have migrated.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self._refuse_newer(version)
            for step in range(version, SCHEMA_VERSION):
                for statement in MIGRATIONS[step]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={step + 1}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writing ----------------------------------------------------------

    def begin_run(self, meta: RunMeta) -> int:
        data = meta.data
        data_json = _dumps({
            "source": data.source,
            "directory": data.directory,
            "symbols": list(data.symbols),
            "first_bar": None if data.first_bar is None else data.first_bar.isoformat(),
            "last_bar": None if data.last_bar is None else data.last_bar.isoformat(),
            "files": [_data_file(entry) for entry in data.files],
        }, "data")
        config_json = _dumps(dict(meta.config), "config")
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status, kind, fills, config_json, data_json,"
                " code_version) VALUES (?, 'running', ?, ?, ?, ?, ?)",
                (self._clock().astimezone(timezone.utc).isoformat(), meta.kind, meta.fills,
                 config_json, data_json, meta.code_version))
            assert cur.lastrowid is not None
            return cur.lastrowid

    def write_algos(self, run_id: int, algos: Sequence[AlgoMeta]) -> None:
        rows = [(run_id, a.idname, a.algo_id, a.ticker, a.mode, _dumps(dict(a.params), "params"),
                 _to_e4(a.capital)) for a in algos]
        with self._tx() as conn:
            self._require_run(conn, run_id)
            conn.executemany(
                "INSERT INTO algos (run_id, idname, algo_id, ticker, mode, params_json,"
                " capital_e4) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def write_results(
        self,
        run_id: int,
        *,
        order_events: Iterable[OrderEvent] = (),
        fills: Iterable[Fill] = (),
        round_trips: Iterable[RoundTrip] = (),
        equity: Iterable[EquityPoint] = (),
        ignored_events: Iterable[IgnoredEvent] = (),
    ) -> None:
        """Append ledger records to a running run, in one transaction.

        Give each record once: a repeated fill id raises `StoreError`. Algos
        must have been written first. Equity points per series must arrive in
        time order, also across calls; only the changed ones are stored.

        For a completed run write equity once, after `ledger.finish` (which
        replaces points). `order_events`, `trades` and `ignored_events` are
        not de-duplicated. A reader of a running run sees equity only up to
        the last stored change.
        """
        saved = {k: replace(v) for k, v in self._equity_state.items()}
        try:
            with self._tx() as conn:
                self._require_run(conn, run_id, running=True)
                conn.executemany(
                    "INSERT INTO order_events (run_id, algo, order_id, broker_ref, kind, time,"
                    " side, quantity_e4, order_type, price_e4) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(run_id, e.account, e.order_id, _encode_ref(e.broker_ref), e.kind,
                      e.time.isoformat(), e.side, _opt_e4(e.quantity), e.order_type,
                      _opt_e4(e.price)) for e in order_events])
                conn.executemany(
                    "INSERT INTO fills (run_id, fill_id, order_id, algo, broker_ref, symbol,"
                    " side, quantity_e4, price_e4, commission_e4, time, reason, synthetic)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(run_id, f.fill_id, f.order_id, f.account, _encode_ref(f.broker_ref),
                      f.symbol, f.side, _to_e4(f.quantity), _to_e4(f.price),
                      _to_e4(f.commission), f.time.isoformat(), f.reason, int(f.synthetic))
                     for f in fills])
                conn.executemany(
                    "INSERT INTO trades (run_id, algo, broker_ref, symbol, side, quantity_e4,"
                    " entry_time, exit_time, entry_price_e4, exit_price_e4, gross_e4,"
                    " commission_e4, net_e4, duration_us, exit_reason, synthetic)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(run_id, t.account, _encode_ref(t.broker_ref), t.symbol, t.side,
                      _to_e4(t.quantity), t.entry_time.isoformat(), t.exit_time.isoformat(),
                      _to_e4(t.entry_price), _to_e4(t.exit_price), _to_e4(t.gross),
                      _to_e4(t.commission), _to_e4(t.net), t.duration // timedelta(microseconds=1),
                      t.exit_reason, int(t.synthetic)) for t in round_trips])
                conn.executemany(
                    "INSERT INTO ignored_events (run_id, algo, broker_ref, kind, time)"
                    " VALUES (?,?,?,?,?)",
                    [(run_id, i.account, _encode_ref(i.broker_ref), i.kind, i.time.isoformat())
                     for i in ignored_events])
                self._write_equity(conn, run_id, equity)
        except BaseException:
            self._equity_state = saved
            raise

    def _write_equity(self, conn: sqlite3.Connection, run_id: int,
                      points: Iterable[EquityPoint]) -> None:
        rows: list[tuple[Any, ...]] = []

        def keep(p: EquityPoint) -> None:
            rows.append((run_id, p.account, p.time.isoformat(), _to_e4(p.cash),
                         _to_e4(p.position_value), _to_e4(p.realised), _to_e4(p.unrealised),
                         _to_e4(p.equity)))

        for p in points:
            key = (run_id, p.account)
            st = self._equity_state.get(key)
            if st is None:
                keep(p)
                self._equity_state[key] = _EquityState(p, True, _values(p))
                continue
            if p.time < st.prev.time:
                raise StoreError("equity points must arrive in time order")
            new_session = p.time.date() != st.prev.time.date()
            if new_session and not st.prev_written:
                keep(st.prev)  # last point of the session just ended
                st.last_values = _values(st.prev)
            if new_session or _values(p) != st.last_values:
                keep(p)
                st.last_values = _values(p)
                st.prev_written = True
            else:
                st.prev_written = False
            st.prev = p
        if rows:
            conn.executemany(
                "INSERT INTO equity (run_id, algo, time, cash_e4, position_value_e4, realised_e4,"
                " unrealised_e4, equity_e4) VALUES (?,?,?,?,?,?,?,?)", rows)

    def end_run(self, run_id: int, status: RunStatus) -> None:
        """Flush the last equity point of every series and close the run."""
        if status not in ("completed", "failed"):
            raise StoreError(f"cannot end a run as {status!r}")
        with self._tx() as conn:
            self._require_run(conn, run_id, running=True)
            pending = [(key, st) for key, st in self._equity_state.items()
                       if key[0] == run_id and not st.prev_written]
            conn.executemany(
                "INSERT INTO equity (run_id, algo, time, cash_e4, position_value_e4, realised_e4,"
                " unrealised_e4, equity_e4) VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, p.account, p.time.isoformat(), _to_e4(p.cash), _to_e4(p.position_value),
                  _to_e4(p.realised), _to_e4(p.unrealised), _to_e4(p.equity))
                 for _, st in pending for p in (st.prev,)])
            conn.execute("UPDATE runs SET status=?, ended_at=? WHERE id=?",
                         (status, self._clock().astimezone(timezone.utc).isoformat(), run_id))
        for key in [k for k in self._equity_state if k[0] == run_id]:
            del self._equity_state[key]

    def write_series(self, run_id: int, infos: Sequence[SeriesInfo],
                     points: Iterable[SeriesPoint] = ()) -> None:
        """Add indicator series (and optionally their points) to a running run.

        One transaction. A series is declared once (a repeated name for an
        algo raises `StoreError`); points may come in this call or later ones
        but must belong to a declared series. Every value must be a finite
        number; NaN and infinity are refused and nothing is written.
        """
        info_rows = [
            (run_id, _text(i.account, "series account"), _text(i.name, "series name"),
             _text(i.draw_type, "series draw_type"), _series_position(i.position))
            for i in infos]
        point_rows: list[tuple[str, str, str, float]] = []
        for p in points:
            point_rows.append((_text(p.account, "series account"), _text(p.name, "series name"),
                               _series_time(p.time), _finite(p.value)))
        with self._tx() as conn:
            if self._schema_version < 2:
                raise StoreError("this database has no series tables (schema version 1)")
            self._require_run(conn, run_id, running=True)
            conn.executemany(
                "INSERT INTO algo_series (run_id, algo, name, draw_type, position)"
                " VALUES (?,?,?,?,?)", info_rows)
            ids = {(a, n): i for i, a, n in conn.execute(
                "SELECT id, algo, name FROM algo_series WHERE run_id=?", (run_id,))}
            rows = []
            for algo, name, time_text, value in point_rows:
                if (algo, name) not in ids:
                    raise StoreError(f"series {name!r} of {algo!r} was not declared")
                rows.append((ids[(algo, name)], time_text, value))
            try:
                conn.executemany(
                    "INSERT INTO algo_series_points (series_id, time, value) VALUES (?,?,?)",
                    rows)
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"a series point is already stored for that time: {exc}") from exc

    @staticmethod
    def _require_run(conn: sqlite3.Connection, run_id: int, *, running: bool = False) -> None:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"no run {run_id}")
        if running and row[0] != "running":
            raise StoreError(f"run {run_id} is already {row[0]}")

    # --- reading ----------------------------------------------------------

    def list_runs(self) -> tuple[RunSummary, ...]:
        rows = self._db().execute(
            "SELECT id, started_at, ended_at, status, kind, fills, code_version, data_json"
            " FROM runs ORDER BY id").fetchall()
        return tuple(self._summary(r) for r in rows)

    def load_series(self, run_id: int, algo: str, start: datetime | None = None,
                    end: datetime | None = None) -> tuple[SeriesPoint, ...]:
        """Points of every series of one algo, ordered by series name then time.

        `start` and `end` (both inclusive, either may be None) bound the time
        window, so one day can be fetched without reading every point. A
        schema-1 database has no series: the result is empty and the series
        tables are not queried.
        """
        conn = self._db()
        sql = "SELECT time, value FROM algo_series_points WHERE series_id=?"
        bounds: list[str] = []
        if start is not None:
            sql += " AND time >= ?"
            bounds.append(start.isoformat())
        if end is not None:
            sql += " AND time <= ?"
            bounds.append(end.isoformat())
        sql += " ORDER BY time"
        try:
            self._require_run(conn, run_id)
            if self._schema_version < 2:
                return ()
            out: list[SeriesPoint] = []
            for series_id, name in conn.execute(
                    "SELECT id, name FROM algo_series WHERE run_id=? AND algo=? ORDER BY name",
                    (run_id, algo)).fetchall():
                out.extend(SeriesPoint(algo, name, _dt(t), v)
                           for t, v in conn.execute(sql, (series_id, *bounds)))
            return tuple(out)
        except sqlite3.Error as exc:
            raise StoreError(str(exc)) from exc

    @staticmethod
    def _summary(r: tuple[Any, ...]) -> RunSummary:
        d = json.loads(r[7])
        data = DataMeta(d["source"], d["directory"], tuple(d["symbols"]),
                        _opt_dt(d["first_bar"]), _opt_dt(d["last_bar"]),
                        tuple((f[0], f[1], f[2]) for f in d.get("files", ())))
        return RunSummary(r[0], _dt(r[1]), _opt_dt(r[2]), r[3], r[4], r[5], r[6], data)

    def load_run(self, run_id: int) -> RunData:
        conn = self._db()
        conn.execute("BEGIN")  # one snapshot for all the reads
        try:
            return self._load(conn, run_id)
        except sqlite3.Error as exc:
            raise StoreError(str(exc)) from exc
        finally:
            conn.execute("COMMIT")

    def _load(self, conn: sqlite3.Connection, run_id: int) -> RunData:
        row = conn.execute(
            "SELECT id, started_at, ended_at, status, kind, fills, code_version, data_json,"
            " config_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"no run {run_id}")
        algos = tuple(
            AlgoMeta(r[0], r[1], r[2], r[3], _from_e4(r[5]), json.loads(r[4]))
            for r in conn.execute(
                "SELECT idname, algo_id, ticker, mode, params_json, capital_e4 FROM algos"
                " WHERE run_id=? ORDER BY rowid", (run_id,)))
        orders = tuple(
            OrderEvent(r[0], r[1], _decode_ref(r[2]), r[3], _dt(r[4]), r[5], _opt_from_e4(r[6]),
                       r[7], _opt_from_e4(r[8]))
            for r in conn.execute(
                "SELECT order_id, algo, broker_ref, kind, time, side, quantity_e4, order_type,"
                " price_e4 FROM order_events WHERE run_id=? ORDER BY seq", (run_id,)))
        fills = tuple(
            Fill(r[0], r[1], r[2], _decode_ref(r[3]), r[4], r[5], _from_e4(r[6]), _from_e4(r[7]),
                 _from_e4(r[8]), _dt(r[9]), r[10], bool(r[11]))
            for r in conn.execute(
                "SELECT fill_id, order_id, algo, broker_ref, symbol, side, quantity_e4, price_e4,"
                " commission_e4, time, reason, synthetic FROM fills WHERE run_id=?"
                " ORDER BY fill_id", (run_id,)))
        trips = tuple(
            RoundTrip(r[0], _decode_ref(r[1]), r[2], r[3], _from_e4(r[4]), _dt(r[5]), _dt(r[6]),
                      _from_e4(r[7]), _from_e4(r[8]), _from_e4(r[9]), _from_e4(r[10]),
                      _from_e4(r[11]), timedelta(microseconds=r[12]), r[13], bool(r[14]))
            for r in conn.execute(
                "SELECT algo, broker_ref, symbol, side, quantity_e4, entry_time, exit_time,"
                " entry_price_e4, exit_price_e4, gross_e4, commission_e4, net_e4, duration_us,"
                " exit_reason, synthetic FROM trades WHERE run_id=? ORDER BY seq", (run_id,)))
        equity = tuple(
            EquityPoint(_dt(r[0]), r[1], _from_e4(r[2]), _from_e4(r[3]), _from_e4(r[4]),
                        _from_e4(r[5]), _from_e4(r[6]))
            for r in conn.execute(
                "SELECT time, algo, cash_e4, position_value_e4, realised_e4, unrealised_e4,"
                " equity_e4 FROM equity WHERE run_id=? ORDER BY seq", (run_id,)))
        ignored = tuple(
            IgnoredEvent(r[0], _decode_ref(r[1]), r[2], _dt(r[3]))
            for r in conn.execute(
                "SELECT algo, broker_ref, kind, time FROM ignored_events WHERE run_id=?"
                " ORDER BY seq", (run_id,)))
        series: tuple[SeriesInfo, ...] = ()
        if self._schema_version >= 2:
            series = tuple(
                SeriesInfo(r[0], r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT algo, name, draw_type, position FROM algo_series WHERE run_id=?"
                    " ORDER BY position, id", (run_id,)))
        return RunData(self._summary(row[:8]), json.loads(row[8]), algos, orders, fills, trips,
                       equity, ignored, series)


def open_store(spec: str, *, env: Mapping[str, str], busy_timeout: float = 10.0) -> Store:
    """Open the store named by `spec`. Only SQLite file paths are supported now.

    `env` is the environment to judge cloud-synced folders by (pass
    `os.environ`). `spec` is a file path, or `sqlite:///path`; `:memory:` gives
    a private in-memory database. `busy_timeout` is the lock wait in seconds.
    Another database can be added by recognising its URL scheme here.
    """
    if spec.startswith("sqlite:///"):
        spec = spec[len("sqlite:///"):]
    if not spec.strip():
        raise StoreError("empty database path")
    found = _SCHEME.match(spec)
    if found and len(found.group(1)) > 1:  # a single letter is a Windows drive
        raise StoreError(f"unsupported database {found.group(1)!r}; "
                         "only SQLite file paths are supported")
    return SqliteStore(Path(spec).expanduser() if spec != ":memory:" else spec, env=env,
                       busy_timeout=busy_timeout)


def open_store_readonly(spec: str, *, env: Mapping[str, str],
                        busy_timeout: float = 10.0) -> Store:
    """Open an existing database for reading only; never writes to the file.

    No folder is created, the journal mode is not switched and no migration
    runs. A missing file or a schema newer than this code raises `StoreError`;
    every schema from 1 to `SCHEMA_VERSION` is accepted as is. Write methods
    raise `StoreError`. `spec` as for `open_store`.

    The database file itself is never modified. Reading a WAL-mode database
    needs SQLite's shared-memory index, so a read-only open of a cleanly
    closed one may leave an empty `-wal` file and a `-shm` file beside it; the
    next writer reuses and cleans them up. `immutable=1` is deliberately not
    used: it is unsafe if a writer starts while the file is open.
    """
    if spec.startswith("sqlite:///"):
        spec = spec[len("sqlite:///"):]
    if not spec.strip():
        raise StoreError("empty database path")
    found = _SCHEME.match(spec)
    if found and len(found.group(1)) > 1:
        raise StoreError(f"unsupported database {found.group(1)!r}; "
                         "only SQLite file paths are supported")
    return SqliteStore(Path(spec).expanduser() if spec != ":memory:" else spec, env=env,
                       busy_timeout=busy_timeout, readonly=True)
