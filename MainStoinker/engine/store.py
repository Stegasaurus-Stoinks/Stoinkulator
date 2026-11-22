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
  (naive local time of the data).
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
- A database file is refused inside a OneDrive folder (`is_cloud_synced`).

A `SqliteStore` belongs to the thread that made it (sqlite3's rule).
"""

from __future__ import annotations

import json
import os
import sqlite3
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

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
SUGGESTED_PATH = r"%LOCALAPPDATA%\Stoinkulator\runs.db"
_SCALE = Decimal(10000)
_QUANT = Decimal("0.0001")


class StoreError(Exception):
    """Anything that stops the store from doing what was asked."""


# --- plain records ------------------------------------------------------------


@dataclass(frozen=True)
class DataMeta:
    """Where a run's bars came from. Bars themselves are not stored."""

    source: str = ""
    directory: str = ""
    symbols: tuple[str, ...] = ()
    first_bar: datetime | None = None
    last_bar: datetime | None = None


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

    def end_run(self, run_id: int, status: RunStatus) -> None: ...

    def list_runs(self) -> tuple[RunSummary, ...]: ...

    def load_run(self, run_id: int) -> RunData: ...

    def close(self) -> None: ...


class NullStore:
    """Accepts everything and keeps nothing."""

    def __init__(self) -> None:
        self._next = 1

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

    def end_run(self, run_id: int, status: RunStatus) -> None:
        return None

    def list_runs(self) -> tuple[RunSummary, ...]:
        return ()

    def load_run(self, run_id: int) -> RunData:
        raise StoreError("the null store keeps no runs")

    def close(self) -> None:
        return None

    def __enter__(self) -> NullStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- OneDrive refusal ---------------------------------------------------------


def is_cloud_synced(path: str | os.PathLike[str], env: Mapping[str, str]) -> bool:
    """True if `path` is inside a OneDrive folder named by `env`.

    Looks at `OneDrive`, `OneDriveCommercial` and `OneDriveConsumer`
    (variable names case-insensitive), compares resolved paths and works when
    the file or its folders do not exist yet.
    """
    target = _norm(Path(path))
    wanted = {n.lower() for n in CLOUD_ENV_VARS}
    for name, value in env.items():
        if name.lower() in wanted and value.strip():
            root = _norm(Path(value))
            if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
                return True
    return False


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


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
)
SCHEMA_VERSION = len(MIGRATIONS)


def _to_e4(value: Decimal | int | str) -> int:
    scaled = Decimal(value) * _SCALE
    whole = scaled.to_integral_value()
    if scaled != whole:
        raise StoreError(f"{value} has more than 4 decimal places")
    return int(whole)


def _opt_e4(value: Decimal | None) -> int | None:
    return None if value is None else _to_e4(value)


def _from_e4(value: int) -> Decimal:
    return (Decimal(value) / _SCALE).quantize(_QUANT)


def _opt_from_e4(value: int | None) -> Decimal | None:
    return None if value is None else _from_e4(value)


def _listify(value: Any) -> Any:
    return [_listify(v) for v in value] if isinstance(value, tuple) else value


def _tuplify(value: Any) -> Any:
    return tuple(_tuplify(v) for v in value) if isinstance(value, list) else value


def _encode_ref(ref: Any) -> str:
    """broker_ref as JSON text. int, str, None and tuples of them come back equal;
    anything else is stored as its `repr` string."""
    try:
        return json.dumps(_listify(ref))
    except TypeError:
        return json.dumps(repr(ref))


def _decode_ref(text: str) -> Any:
    return _tuplify(json.loads(text))


def _dumps(value: Any, what: str) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{what} is not JSON-serialisable: {exc}") from exc


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _opt_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


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


class SqliteStore:
    """Runs in one SQLite file. Create it with `open_store` or directly."""

    def __init__(self, path: str | os.PathLike[str], *,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._equity_state: dict[tuple[int, str | None], _EquityState] = {}
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                self._path, isolation_level=None, timeout=10.0)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {self._path}: {exc}") from exc
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except StoreError:
            self.close()
            raise
        except sqlite3.Error as exc:
            self.close()
            raise StoreError(f"cannot use database {self._path}: {exc}") from exc

    # --- plumbing ---------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("the store is closed")
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise StoreError(str(exc)) from exc
            raise
        else:
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        conn = self._db()
        if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        with self._tx():
            # Re-read inside the write lock: another process may have migrated.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"database schema version {version} is newer than this code "
                                 f"understands ({SCHEMA_VERSION}); update the program")
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

    @staticmethod
    def _summary(r: tuple[Any, ...]) -> RunSummary:
        d = json.loads(r[7])
        data = DataMeta(d["source"], d["directory"], tuple(d["symbols"]),
                        _opt_dt(d["first_bar"]), _opt_dt(d["last_bar"]))
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
        return RunData(self._summary(row[:8]), json.loads(row[8]), algos, orders, fills, trips,
                       equity, ignored)


def open_store(spec: str, *, env: Mapping[str, str]) -> Store:
    """Open the store named by `spec`. Only SQLite file paths are supported now.

    `env` is the environment to judge cloud-synced folders by (pass
    `os.environ`). `spec` is a file path, or `sqlite:///path`; `:memory:` gives
    a private in-memory database. Another database can be added by recognising
    its URL scheme here.
    """
    if spec.startswith("sqlite:///"):
        spec = spec[len("sqlite:///"):]
    if "://" in spec:
        raise StoreError(f"unsupported database {spec.split('://', 1)[0]!r}; "
                         "only SQLite file paths are supported")
    if not spec.strip():
        raise StoreError("empty database path")
    if spec != ":memory:" and is_cloud_synced(Path(spec).expanduser(), env):
        raise StoreError(f"{spec} is inside a OneDrive folder; sync can corrupt a database "
                         f"while it is written. Use {SUGGESTED_PATH} instead.")
    return SqliteStore(Path(spec).expanduser() if spec != ":memory:" else spec)
