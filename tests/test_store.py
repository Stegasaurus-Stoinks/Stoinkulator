"""The run store: protocol, NullStore, SqliteStore and schema v1.

Databases live only under pytest's tmp_path and `env` is always injected, so
nothing depends on (or touches) the real OneDrive or the repo.
"""

import ast
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from MainStoinker.engine.ledger import EquityPoint, Ledger
from MainStoinker.engine.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    AlgoMeta,
    DataMeta,
    NullStore,
    RunMeta,
    SeriesInfo,
    SeriesPoint,
    SqliteStore,
    StoreError,
    expand_equity,
    is_cloud_synced,
    open_store,
    open_store_readonly,
)

D = Decimal
T0 = datetime(2024, 1, 2, 9, 30)
NO_ENV: dict[str, str] = {}


def meta(kind: str = "backtest") -> RunMeta:
    return RunMeta(
        kind=kind, fills="realistic", code_version="abc123",
        config={"algos": ["a1", "a2"], "n": 3},
        data=DataMeta("csv", "OfflineData", ("AAPL", "MSFT"), T0, T0 + timedelta(days=1)))


def algos() -> list[AlgoMeta]:
    return [AlgoMeta("a1", "EMACrossing", "AAPL", "backtest", D("10000"), {"fast": 5}),
            AlgoMeta("a2", "Elliot", "MSFT", "paper", D("2500.5000"), {})]


def build_ledger() -> Ledger:
    """Two accounts, two sessions, a short, a loss, ignored requests, an end-of-data close."""
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 10000)
    a2 = led.open_account("a2", "MSFT", D("2500.5"))
    t = T0
    o1 = a1.order_placed(1, time=t, side="buy", quantity=10, order_type="market")
    a1.fill(1, side="buy", quantity=10, price=D("100.1234"), commission=1, time=t,
            reason="entry", order_id=o1)
    stop = a1.order_placed(1, time=t, side="sell", quantity=10, order_type="stop",
                           price=D("99.5"))
    a1.order_modified(stop, time=t + timedelta(minutes=1), price=D("99.75"))
    led.mark(t, {"AAPL": D("100.1234"), "MSFT": 50})
    led.mark(t + timedelta(minutes=1), {"AAPL": D("100.1234"), "MSFT": 50})  # unchanged
    led.mark(t + timedelta(minutes=2), {"AAPL": D("101.0001"), "MSFT": 50})
    a1.fill(1, side="sell", quantity=10, price=D("99.75"), commission=1,
            time=t + timedelta(minutes=3), reason="stop", order_id=stop)
    a1.ignored(1, time=t + timedelta(minutes=4), kind="close")
    # short on a2, loses money
    o2 = a2.order_placed("s", time=t, side="sell", quantity=5, order_type="market")
    a2.fill("s", side="sell", quantity=5, price=D("50"), commission=1, time=t,
            reason="entry", order_id=o2)
    a2.order_placed("s", time=t, side="buy", quantity=5, order_type="target", price=D("45"))
    led.mark(t + timedelta(minutes=3), {"AAPL": D("99.75"), "MSFT": D("52.25")})
    # second session
    t2 = t + timedelta(days=1)
    led.mark(t2, {"AAPL": D("99.75"), "MSFT": D("52.25")})
    led.mark(t2 + timedelta(minutes=1), {"AAPL": D("99.75"), "MSFT": D("52.25")})
    led.mark(t2 + timedelta(minutes=2), {"AAPL": D("99.75"), "MSFT": D("53")})
    led.finish(t2 + timedelta(minutes=3), {"MSFT": D("53")}, lambda _i, _q: 1)
    return led


def write_all(store, run_id: int, led: Ledger) -> None:
    store.write_results(run_id, order_events=led.orders, fills=led.fills,
                        round_trips=led.round_trips, equity=led.equity_points,
                        ignored_events=led.ignored_events)


def stored_run(store: SqliteStore, led: Ledger | None = None) -> tuple[int, Ledger]:
    led = led or build_ledger()
    run_id = store.begin_run(meta())
    store.write_algos(run_id, algos())
    write_all(store, run_id, led)
    store.end_run(run_id, "completed")
    return run_id, led


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


def test_null_store_accepts_everything():
    led = build_ledger()
    with NullStore() as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        write_all(store, run_id, led)
        store.end_run(run_id, "completed")
        assert store.list_runs() == ()
        assert store.begin_run(meta()) != run_id
        with pytest.raises(StoreError):
            store.load_run(run_id)


def test_roundtrip_run_algos_trades_fills_equity(db):
    with SqliteStore(db) as store:
        run_id, led = stored_run(store)
        data = store.load_run(run_id)
    assert data.summary.status == "completed"
    assert data.summary.kind == "backtest" and data.summary.fills == "realistic"
    assert data.summary.code_version == "abc123"
    assert data.summary.data == meta().data
    assert data.summary.started_at.tzinfo is not None
    assert data.summary.ended_at is not None and data.summary.ended_at >= data.summary.started_at
    assert data.config == {"algos": ["a1", "a2"], "n": 3}
    assert data.algos == tuple(algos())
    assert data.accounts() == led.accounts
    assert data.order_events == led.orders
    assert data.fills == led.fills
    assert data.round_trips == led.round_trips
    assert data.ignored_events == led.ignored_events
    assert len(led.round_trips) == 2 and len(led.fills) == 4
    # the full series is recoverable for the total and for each algo
    for account in (None, "a1", "a2"):
        full = [p for p in led.equity_points if p.account == account]
        grid = [p.time for p in full]
        assert expand_equity(data.equity_series(account), grid) == full


def test_money_exact_after_roundtrip(db):
    led = build_ledger()
    with SqliteStore(db) as store:
        run_id, _ = stored_run(store, led)
        data = store.load_run(run_id)
    assert D("100.1234") in {f.price for f in data.fills}
    assert all(isinstance(f.price, Decimal) for f in data.fills)
    for stored, orig in zip(data.round_trips, led.round_trips, strict=True):
        for name in ("entry_price", "exit_price", "gross", "commission", "net", "quantity"):
            assert getattr(stored, name) == getattr(orig, name)
            assert getattr(stored, name).as_tuple().exponent == -4
    assert data.algos[1].capital == D("2500.5000")


def test_negative_money_and_four_decimal_prices(db):
    t = T0
    pt = EquityPoint(t, "a1", D("-1234.5678"), D("-0.0001"), D("-99.9999"), D("0.0001"),
                     D("-1234.5679"))
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        store.write_results(run_id, equity=[pt])
        store.end_run(run_id, "completed")
        got = store.load_run(run_id).equity
        raw = sqlite3.connect(db).execute("SELECT cash_e4, equity_e4 FROM equity").fetchone()
    assert got == (pt,)
    assert raw == (-12345678, -12345679)


def test_more_than_four_decimals_refused(db):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos()[:1])
        bad = EquityPoint(T0, "a1", D("1.00001"), D(0), D(0), D(0), D("1.00001"))
        with pytest.raises(StoreError):
            store.write_results(run_id, equity=[bad])
        with pytest.raises(StoreError):
            store.write_algos(run_id, [AlgoMeta("z", "X", "AAPL", "backtest", D("1.00001"))])
        assert store.load_run(run_id).equity == ()  # the failed write left nothing


def test_synthetic_end_of_data_trade_roundtrips(db):
    led = build_ledger()
    synth = [f for f in led.fills if f.synthetic]
    assert len(synth) == 1 and synth[0].order_id is None and synth[0].reason == "end_of_data"
    with SqliteStore(db) as store:
        run_id, _ = stored_run(store, led)
        data = store.load_run(run_id)
    got = [f for f in data.fills if f.synthetic]
    assert got == synth and got[0].order_id is None
    trip = [t for t in data.round_trips if t.synthetic]
    assert len(trip) == 1 and trip[0].exit_reason == "end_of_data"
    # the target order of that position has no cancel event, and that is accepted as is
    target = [e for e in data.order_events if e.order_type == "target"]
    assert len(target) == 1
    assert not [e for e in data.order_events if e.kind == "cancelled"]


def test_broker_ref_kinds_roundtrip(db):
    led = Ledger()
    rec = led.open_account("a1", "AAPL", 1000)
    for ref in (7, "x", ("a", 1)):
        rec.order_placed(ref, time=T0, side="buy", quantity=1, order_type="market")
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos()[:1])
        write_all(store, run_id, led)
        assert [e.broker_ref for e in store.load_run(run_id).order_events] == [7, "x", ("a", 1)]


def test_wal_mode_enabled(db):
    with SqliteStore(db):
        pass
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


def test_foreign_keys_enforced(db):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        led = build_ledger()
        with pytest.raises(StoreError):  # algos were never written
            store.write_results(run_id, fills=led.fills)


def test_schema_version_set_and_migration_idempotent(db):
    with SqliteStore(db) as store:
        run_id, led = stored_run(store)
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"runs", "algos", "order_events", "fills", "trades", "equity",
            "ignored_events"} <= tables
    # reopening neither fails, re-migrates nor loses data
    with SqliteStore(db) as store:
        assert [r.run_id for r in store.list_runs()] == [run_id]
        assert store.load_run(run_id).fills == led.fills
    with SqliteStore(db) as store:
        assert len(store.list_runs()) == 1


def test_newer_schema_refused(db):
    with SqliteStore(db):
        pass
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(StoreError, match="newer"):
        SqliteStore(db)
    # refusal leaves the file alone and unlocked
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    con.close()
    db.unlink()


def test_newer_schema_refusal_does_not_modify_the_file(db):
    """The switch to WAL rewrites the file header, so a newer database is refused first."""
    import hashlib

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(StoreError, match="newer"):
        SqliteStore(db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in db.parent.iterdir()) == [db.name]  # no -wal / -shm left behind
    db.unlink()


class FlakyWal:
    """Stands in for a connection: `PRAGMA journal_mode=WAL` fails with the given errors first."""

    def __init__(self, *errors: str) -> None:
        self.errors = list(errors)
        self.calls = 0

    def execute(self, sql: str):
        self.calls += 1
        if self.errors:
            raise sqlite3.OperationalError(self.errors.pop(0))


def test_wal_switch_retries_a_transient_disk_io_error():
    from MainStoinker.engine.store import _enable_wal

    conn = FlakyWal("disk I/O error")
    _enable_wal(conn, wait=5.0)  # type: ignore[arg-type]
    assert conn.calls == 2


def test_wal_switch_gives_up_on_a_persistent_disk_io_error_after_the_deadline():
    from MainStoinker.engine.store import _enable_wal

    conn = FlakyWal(*["disk I/O error"] * 1000)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _enable_wal(conn, wait=0.05)  # type: ignore[arg-type]
    assert conn.calls > 1


def test_wal_switch_raises_other_errors_at_once():
    from MainStoinker.engine.store import _enable_wal

    conn = FlakyWal("attempt to write a readonly database")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        _enable_wal(conn, wait=5.0)  # type: ignore[arg-type]
    assert conn.calls == 1


def test_not_a_database_refused(db):
    db.write_text("this is not sqlite" * 50)
    with pytest.raises(StoreError):
        SqliteStore(db)
    db.unlink()


def test_incomplete_run_listed_as_running(db):
    store = SqliteStore(db)
    run_id = store.begin_run(meta())
    store.write_algos(run_id, algos())
    store.write_results(run_id, equity=[EquityPoint(T0, "a1", D(1), D(0), D(0), D(0), D(1))])
    store.close()  # as if the process died before end_run
    with SqliteStore(db) as again:
        (summary,) = again.list_runs()
        assert summary.status == "running" and summary.ended_at is None
        data = again.load_run(run_id)
        assert data.algos == tuple(algos())
        assert len(data.equity) == 1


def test_end_run_failed_and_twice(db):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.end_run(run_id, "failed")
        assert store.list_runs()[0].status == "failed"
        with pytest.raises(StoreError):
            store.end_run(run_id, "completed")
        with pytest.raises(StoreError):
            store.write_results(run_id)
        with pytest.raises(StoreError):
            store.end_run(run_id + 1, "completed")
        run2 = store.begin_run(meta())
        with pytest.raises(StoreError):
            store.end_run(run2, "running")  # type: ignore[arg-type]
        with pytest.raises(StoreError):
            store.load_run(99)


def test_two_runs_isolated(db):
    led1, led2 = build_ledger(), Ledger()
    r = led2.open_account("a1", "AAPL", 10000)
    r.order_placed(1, time=T0, side="buy", quantity=1, order_type="market")
    led2.mark(T0, {})
    with SqliteStore(db) as store:
        run1, _ = stored_run(store, led1)
        run2 = store.begin_run(meta("paper"))
        store.write_algos(run2, algos()[:1])  # same idname as run 1: fine, per run
        write_all(store, run2, led2)
        store.end_run(run2, "completed")
        d1, d2 = store.load_run(run1), store.load_run(run2)
        runs = store.list_runs()
    assert [x.run_id for x in runs] == [run1, run2]
    assert d1.fills == led1.fills and d2.fills == ()
    assert len(d2.order_events) == 1 and len(d1.order_events) == len(led1.orders)
    assert d2.summary.kind == "paper" and len(d2.algos) == 1 and len(d1.algos) == 2
    assert {p.account for p in d2.equity} == {"a1", None}


def test_duplicate_algo_or_fill_refused(db):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        with pytest.raises(StoreError):
            store.write_algos(run_id, algos()[:1])
        led = build_ledger()
        store.write_results(run_id, fills=led.fills)
        with pytest.raises(StoreError):
            store.write_results(run_id, fills=led.fills[:1])
        assert store.load_run(run_id).fills == led.fills


def full_series(n_sessions: int = 3, per_session: int = 6) -> list[EquityPoint]:
    """Plateaus within sessions, repeated values across a session boundary."""
    out = []
    for s in range(n_sessions):
        base = datetime(2024, 1, 2 + s, 9, 30)
        for i in range(per_session):
            eq = D("1000") + (D("5.5") if i >= 3 else 0)  # same level at each session start
            out.append(EquityPoint(base + timedelta(minutes=i), "a1", D(1000), eq - 1000,
                                   D(0), D(0), eq))
    return out


def test_equity_change_only_is_lossless(db):
    full = full_series()
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        store.write_results(run_id, equity=full)
        store.end_run(run_id, "completed")
        stored = store.load_run(run_id).equity_series("a1")
    assert len(stored) < len(full)
    assert expand_equity(stored, [p.time for p in full]) == full
    for s in range(3):  # first and last point of every session are present
        day = [p for p in full if p.time.date() == full[0].time.date() + timedelta(days=s)]
        assert day[0] in stored and day[-1] in stored


def test_equity_lossless_across_incremental_writes(db):
    full = full_series() + [replace_time(p, minutes=1000) for p in full_series(1, 2)]
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        for i in range(0, len(full), 4):  # arbitrary chunking, even mid-plateau
            store.write_results(run_id, equity=full[i:i + 4])
        store.end_run(run_id, "completed")
        stored = store.load_run(run_id).equity_series("a1")
    assert expand_equity(stored, [p.time for p in full]) == full
    with SqliteStore(db) as other:  # same points written in one go give the same rows
        run2 = other.begin_run(meta())
        other.write_algos(run2, algos())
        other.write_results(run2, equity=full)
        other.end_run(run2, "completed")
        assert other.load_run(run2).equity_series("a1") == stored


def replace_time(p: EquityPoint, minutes: int) -> EquityPoint:
    return EquityPoint(p.time + timedelta(days=5, minutes=minutes), p.account, p.cash,
                       p.position_value, p.realised, p.unrealised, p.equity)


def test_equity_out_of_order_refused(db):
    a, b = full_series(1, 2)
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        with pytest.raises(StoreError):
            store.write_results(run_id, equity=[b, a])


def test_total_series_uses_null_algo(db):
    with SqliteStore(db) as store:
        run_id, led = stored_run(store)
        total = store.load_run(run_id).equity_series(None)
    assert total and all(p.account is None for p in total)
    assert total[-1].equity == led.equity()
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM equity WHERE algo IS NULL").fetchone()[0] > 0
    finally:
        con.close()


@pytest.mark.parametrize("key", ["OneDrive", "ONEDRIVECOMMERCIAL", "onedriveconsumer"])
def test_onedrive_path_refused(tmp_path, key):
    root = tmp_path / "Cloud" / "OneDrive - Corp"
    env = {key: str(root)}
    target = root / "Documents" / "runs.db"  # neither folder nor file exists
    assert is_cloud_synced(target, env)
    assert is_cloud_synced(root, env)
    with pytest.raises(StoreError, match=r"USERPROFILE%\\Stoinkulator\\runs.db"):
        open_store(str(target), env=env)
    assert not root.exists()  # refused before anything was created


def test_cloud_refusal_suggests_userprofile(tmp_path):
    env = {"OneDrive": str(tmp_path / "OneDrive")}
    target = tmp_path / "OneDrive" / "runs.db"
    for opener in (open_store, open_store_readonly):
        with pytest.raises(StoreError) as info:
            opener(str(target), env=env)
        assert r"%USERPROFILE%\Stoinkulator\runs.db" in str(info.value)


# ---- read-only access ---------------------------------------------------------------


def test_readonly_open_cannot_write(db):
    with SqliteStore(db) as w:
        run_id, led = stored_run(w)
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        assert len(store.list_runs()) == 1
        with pytest.raises(StoreError):
            store.begin_run(meta())
        with pytest.raises(StoreError):
            store.write_algos(run_id, algos())
        with pytest.raises(StoreError):
            write_all(store, run_id, led)
        with pytest.raises(StoreError):
            store.end_run(run_id, "completed")
        with pytest.raises(sqlite3.OperationalError):
            store._conn.execute("INSERT INTO runs (started_at, status, kind, fills, config_json,"
                                " data_json, code_version) VALUES ('x','running','k','f','{}','{}','')")
    finally:
        store.close()
    with SqliteStore(db) as check:
        assert len(check.list_runs()) == 1


def test_readonly_open_does_not_migrate_or_create(db, tmp_path):
    missing = tmp_path / "nope" / "runs.db"
    with pytest.raises(StoreError, match="does not exist"):
        open_store_readonly(str(missing), env=NO_ENV)
    assert not missing.exists() and not missing.parent.exists()
    # a v1 file: build it the old way, then compare bytes after reading
    with SqliteStore(db) as w:
        stored_run(w)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    before = db.read_bytes()
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        runs = store.list_runs()
        store.load_run(runs[0].run_id)
    finally:
        store.close()
    assert db.read_bytes() == before
    assert sorted(p.name for p in db.parent.iterdir()) == [db.name]


def test_readonly_newer_schema_refused(db):
    with SqliteStore(db):
        pass
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.close()
    before = db.read_bytes()
    with pytest.raises(StoreError, match="newer"):
        open_store_readonly(str(db), env=NO_ENV)
    assert db.read_bytes() == before


def test_readonly_reads_wal_db_without_wal_and_shm_files(db):
    with SqliteStore(db) as w:
        stored_run(w)
    assert not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists()
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        assert len(store.list_runs()) == 1
    finally:
        store.close()


def test_readonly_open_of_closed_wal_db_leaves_main_file_identical(db):
    with SqliteStore(db) as w:
        stored_run(w)
    before = db.read_bytes()
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        runs = store.list_runs()
        assert len(runs) == 1
    finally:
        store.close()
    assert db.read_bytes() == before
    wal = Path(str(db) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    with SqliteStore(db) as w2:  # a writer afterwards still works and sees the same runs
        assert w2.list_runs() == runs
        w2.begin_run(meta())
        assert len(w2.list_runs()) == 2


def test_readonly_folder_is_not_a_file(tmp_path):
    with pytest.raises(StoreError, match="is not a file"):
        open_store_readonly(str(tmp_path), env=NO_ENV)


def test_readonly_sees_committed_run_while_writer_open(db):
    writer = SqliteStore(db)
    try:
        run_id = writer.begin_run(meta())
        reader = open_store_readonly(str(db), env=NO_ENV)
        try:
            assert [r.status for r in reader.list_runs()] == ["running"]
            writer.write_algos(run_id, algos())
            assert [a.idname for a in reader.load_run(run_id).algos] == ["a1", "a2"]
        finally:
            reader.close()
    finally:
        writer.close()


def test_readonly_path_with_spaces_and_hash(tmp_path):
    folder = tmp_path / "my runs #1 %20 x"
    folder.mkdir()
    path = folder / "r&d 100% #2.db"
    with SqliteStore(path) as w:
        stored_run(w)
    store = open_store_readonly(str(path), env=NO_ENV)
    try:
        assert len(store.list_runs()) == 1
    finally:
        store.close()


def test_not_cloud_synced_cases(tmp_path):
    root = tmp_path / "Cloud"
    env = {"OneDrive": str(root)}
    assert not is_cloud_synced(tmp_path / "Cloud2" / "runs.db", env)
    assert not is_cloud_synced(tmp_path / "other" / "runs.db", env)
    assert not is_cloud_synced(root / "runs.db", NO_ENV)
    assert not is_cloud_synced(root / "runs.db", {"OneDrive": ""})
    assert not is_cloud_synced(root / "runs.db", {"Other": str(root)})
    # a path that climbs out of the folder is judged after resolving
    assert not is_cloud_synced(root / ".." / "runs.db", env)
    assert is_cloud_synced(tmp_path / "x" / ".." / "Cloud" / "runs.db", env)


@pytest.mark.parametrize("folder", ["OneDrive", "onedrive", "OneDrive - Corp", "ONEDRIVE2"])
def test_onedrive_named_folder_refused_without_env(tmp_path, folder):
    target = tmp_path / folder / "sub" / "runs.db"
    assert is_cloud_synced(target, NO_ENV)
    with pytest.raises(StoreError, match="OneDrive"):
        open_store(str(target), env=NO_ENV)
    with pytest.raises(StoreError, match="OneDrive"):
        SqliteStore(target, env=NO_ENV)
    assert not (tmp_path / folder).exists()
    assert not is_cloud_synced(tmp_path / "Dropbox" / "runs.db", NO_ENV)
    assert not is_cloud_synced(tmp_path / "myonedrive" / "runs.db", NO_ENV)  # only a prefix


@pytest.mark.skipif(os.name != "nt", reason="the extended-length prefix is Windows only")
def test_onedrive_guard_handles_extended_length_prefix(tmp_path):
    named = "\\\\?\\" + str(tmp_path / "OneDrive" / "runs.db")
    assert is_cloud_synced(named, NO_ENV)
    root = tmp_path / "Cloud"
    assert is_cloud_synced("\\\\?\\" + str(root / "runs.db"), {"OneDrive": str(root)})
    assert is_cloud_synced(root / "runs.db", {"OneDrive": "\\\\?\\" + str(root)})
    assert not is_cloud_synced("\\\\?\\" + str(tmp_path / "Other" / "runs.db"), NO_ENV)


def test_direct_construction_applies_the_cloud_refusal(tmp_path, monkeypatch):
    root = tmp_path / "Cloud"
    env = {"OneDrive": str(root)}
    with pytest.raises(StoreError, match="OneDrive"):
        SqliteStore(root / "runs.db", env=env)
    assert not root.exists()
    # the default env is os.environ, read when the store is made
    monkeypatch.setenv("OneDrive", str(root))
    with pytest.raises(StoreError, match="OneDrive"):
        SqliteStore(root / "runs.db")
    assert not root.exists()
    monkeypatch.delenv("OneDrive")
    with SqliteStore(root / "runs.db") as store:
        assert store.list_runs() == ()


def test_open_store_variants(tmp_path):
    path = tmp_path / "deep" / "er" / "runs.db"
    with open_store(str(path), env=NO_ENV) as store:
        assert isinstance(store, SqliteStore)
    assert path.exists()
    with open_store("sqlite:///" + str(path), env=NO_ENV) as store:
        assert store.list_runs() == ()
    with pytest.raises(StoreError, match="unsupported"):
        open_store("postgresql://u@h/db", env=NO_ENV)
    with pytest.raises(StoreError):
        open_store("  ", env=NO_ENV)


def test_reader_sees_committed_run_while_writer_open(db):
    writer = SqliteStore(db)
    reader = SqliteStore(db)
    try:
        run_id = writer.begin_run(meta())
        writer.write_algos(run_id, algos())
        (summary,) = reader.list_runs()
        assert summary.run_id == run_id and summary.status == "running"
        assert len(reader.load_run(run_id).algos) == 2
        led = build_ledger()
        write_all(writer, run_id, led)  # committed too
        assert reader.load_run(run_id).fills == led.fills
        writer.end_run(run_id, "completed")
        assert reader.list_runs()[0].status == "completed"
    finally:
        writer.close()
        reader.close()


def test_two_stores_on_one_file_get_distinct_run_ids(db):
    with SqliteStore(db) as a, SqliteStore(db) as b:
        ids = [a.begin_run(meta()), b.begin_run(meta()), a.begin_run(meta())]
        assert len(set(ids)) == 3
        assert [r.run_id for r in b.list_runs()] == ids


def test_close_releases_file_and_blocks_use(db):
    store = SqliteStore(db)
    store.begin_run(meta())
    store.close()
    store.close()  # idempotent
    with pytest.raises(StoreError, match="closed"):
        store.list_runs()
    for suffix in ("-wal", "-shm", ""):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()  # Windows refuses while a handle is open
    assert not db.exists()


def test_context_manager_closes_on_error(db):
    with pytest.raises(RuntimeError), SqliteStore(db) as store:
        raise RuntimeError("boom")
    with pytest.raises(StoreError):
        store.list_runs()
    db.unlink()


def test_non_json_config_refused(db):
    with SqliteStore(db) as store:
        with pytest.raises(StoreError):
            store.begin_run(RunMeta("backtest", "legacy", config={"x": object()}))
        assert store.list_runs() == ()


def test_utc_clock_injected(db):
    ticks = iter([datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
                  datetime(2024, 5, 1, 13, 30, tzinfo=timezone(timedelta(hours=1)))])
    with SqliteStore(db, clock=lambda: next(ticks)) as store:
        run_id = store.begin_run(meta())
        store.end_run(run_id, "completed")
        (s,) = store.list_runs()
    assert s.started_at == datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert s.ended_at == datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert s.ended_at.utcoffset() == timedelta(0)


def test_store_module_imports_no_pandas_or_legacy():
    path = Path(__file__).resolve().parent.parent / "MainStoinker" / "engine" / "store.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(("." * node.level) + (node.module or ""))
    assert not mods & {"pandas", "numpy", "ibapi", "MainStoinker"}


def test_no_database_files_in_repo():
    repo = Path(__file__).resolve().parent.parent
    assert not list(repo.glob("*.db")) and not list((repo / "MainStoinker").rglob("*.db"))


# --- added by the tester: gaps found while verifying ---------------------------


def test_real_backtests_roundtrip_through_the_store(db):
    """Real engine runs (a plain one and one with an end-of-data close) stored and reloaded."""
    from tests import harness

    cases = [
        ("EMACrossing", {"idname": "AAPL-ema", "ticker": "AAPL", "short": "10", "long": "30"}),
        ("Elliot", {"idname": "elliot-msft", "ticker": "MSFT", "order": 10,
                    "trailingStopDelta": 0.10}),
    ]
    synthetic_seen = False
    for n, (algo_id, cfg) in enumerate(cases):
        harness.stage_offline_data(Path.cwd(), [cfg["ticker"]], max_rows=700)
        led = Ledger()
        harness.run_offline_backtest(algo_id, [cfg], ledger=led)
        path = db.parent / f"real{n}.db"
        with SqliteStore(path) as store:
            run_id = store.begin_run(meta())
            store.write_algos(run_id, [AlgoMeta(a.idname, algo_id, a.symbol, "backtest", a.capital)
                                       for a in led.accounts])
            write_all(store, run_id, led)
            store.end_run(run_id, "completed")
        with SqliteStore(path) as store:  # a fresh connection: nothing from the writer's memory
            data = store.load_run(run_id)
        assert led.round_trips and led.fills
        assert data.accounts() == led.accounts
        assert data.order_events == led.orders
        assert data.fills == led.fills
        assert data.round_trips == led.round_trips
        assert data.ignored_events == led.ignored_events
        for account in [None, *[a.idname for a in led.accounts]]:
            full = [p for p in led.equity_points if p.account == account]
            assert expand_equity(data.equity_series(account), [p.time for p in full]) == full
        assert len(data.equity) < len(led.equity_points)
        synthetic_seen |= any(f.synthetic for f in data.fills)
        harness.reset_runtime_state()
    assert synthetic_seen  # Elliot MSFT ends with an open trade


def test_sql_sum_of_net_equals_decimal_sum(db):
    led = build_ledger()
    with SqliteStore(db) as store:
        stored_run(store, led)
    con = sqlite3.connect(db)
    try:
        total = con.execute("SELECT SUM(net_e4) FROM trades").fetchone()[0]
    finally:
        con.close()
    assert Decimal(total) / 10000 == sum(t.net for t in led.round_trips)
    assert any(t.net < 0 for t in led.round_trips)  # the sum includes a loss


def test_large_and_tiny_values_roundtrip(db):
    big = D("9999999999999.9999")
    pts = [EquityPoint(T0 + timedelta(minutes=i), "a1", v, D(0), D(0), D(0), v)
           for i, v in enumerate([big, -big, D("0.0001"), D("-0.0001"), D(5), D("1.50000")])]
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        store.write_results(run_id, equity=pts)
        store.end_run(run_id, "completed")
        got = store.load_run(run_id).equity
    assert got == tuple(pts)
    assert got[-1].cash.as_tuple().exponent == -4


def test_results_written_in_chunks_equal_one_write(db):
    led = build_ledger()
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        orders, fills, trips = list(led.orders), list(led.fills), list(led.round_trips)
        ignored, equity = list(led.ignored_events), list(led.equity_points)
        for i in range(0, max(len(orders), len(fills), len(equity)), 2):
            store.write_results(run_id, order_events=orders[i:i + 2], fills=fills[i:i + 2],
                                round_trips=trips[i:i + 2], ignored_events=ignored[i:i + 2],
                                equity=equity[i:i + 2])
        store.end_run(run_id, "completed")
        data = store.load_run(run_id)
    assert data.order_events == led.orders and data.fills == led.fills
    assert data.round_trips == led.round_trips and data.ignored_events == led.ignored_events
    for account in (None, "a1", "a2"):
        full = [p for p in led.equity_points if p.account == account]
        assert expand_equity(data.equity_series(account), [p.time for p in full]) == full


def test_failed_equity_write_does_not_corrupt_compressor_state(db):
    """A write that fails after the compressor saw a changed point must not lose it on retry."""
    def pt(minute: int, value: str) -> EquityPoint:
        return EquityPoint(T0 + timedelta(minutes=minute), "a1", D(1000), D(0), D(0), D(0),
                           D(value))
    bad = EquityPoint(T0 + timedelta(minutes=3), "a1", D("1.00001"), D(0), D(0), D(0), D(1))
    good = [pt(0, "1000"), pt(1, "1000")]
    retry = [pt(2, "1500"), pt(3, "1500"), pt(4, "1500")]
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        store.write_results(run_id, equity=good)
        with pytest.raises(StoreError):
            store.write_results(run_id, equity=[retry[0], bad])
        assert len(store.load_run(run_id).equity) == 1  # the failed call stored nothing
        store.write_results(run_id, equity=retry)  # the same points again
        store.end_run(run_id, "completed")
        stored = store.load_run(run_id).equity_series("a1")
    full = good + retry
    assert expand_equity(stored, [p.time for p in full]) == full


def test_failed_run_keeps_what_was_written(db):
    with SqliteStore(db) as store:
        run_id, led = stored_run(store)  # completed; make a second, failed one
        run2 = store.begin_run(meta())
        store.write_algos(run2, algos())
        write_all(store, run2, led)
        store.end_run(run2, "failed")
        summaries = {s.run_id: s for s in store.list_runs()}
        assert summaries[run2].status == "failed" and summaries[run2].ended_at is not None
        assert store.load_run(run2).fills == led.fills
        assert store.load_run(run_id).summary.status == "completed"


def test_pandas_timestamps_come_back_as_equal_datetimes(db):
    pd = pytest.importorskip("pandas")
    stamps = [pd.Timestamp("2024-01-02 09:30"), pd.Timestamp("2024-01-02 09:31:00.123456")]
    pts = [EquityPoint(t, "a1", D(i), D(0), D(0), D(0), D(i)) for i, t in enumerate(stamps, 1)]
    with SqliteStore(db) as store:
        run_id = store.begin_run(RunMeta("backtest", "legacy", data=DataMeta(first_bar=stamps[0])))
        store.write_algos(run_id, algos())
        store.write_results(run_id, equity=pts)
        store.end_run(run_id, "completed")
        data = store.load_run(run_id)
    assert [p.time for p in data.equity] == stamps
    assert all(type(p.time) is datetime for p in data.equity)
    assert data.summary.data.first_bar == stamps[0]


def test_repeated_runs_in_one_process_do_not_leak_state(tmp_path):
    led = build_ledger()
    first = None
    for n in range(3):
        with SqliteStore(tmp_path / f"r{n}.db") as store:
            run_id, _ = stored_run(store, led)
            data = store.load_run(run_id)
        assert run_id == 1  # every file starts fresh
        rows = (data.order_events, data.fills, data.round_trips, data.equity,
                data.ignored_events)
        first = first or rows
        assert rows == first


def test_reader_in_another_process_sees_running_run(db):
    script = db.parent / "reader.py"
    script.write_text(
        "import sys\n"
        "from MainStoinker.engine.store import SqliteStore\n"
        "with SqliteStore(sys.argv[1]) as s:\n"
        "    (run,) = s.list_runs()\n"
        "    data = s.load_run(run.run_id)\n"
        "    print(run.status, len(data.algos), len(data.fills))\n",
        encoding="utf-8")
    repo = Path(__file__).resolve().parent.parent
    led = build_ledger()
    with SqliteStore(db) as writer:  # still open and the run still running
        run_id = writer.begin_run(meta())
        writer.write_algos(run_id, algos())
        write_all(writer, run_id, led)
        out = subprocess.run([sys.executable, str(script), str(db)], cwd=repo,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["running", "2", str(len(led.fills))]
        writer.end_run(run_id, "completed")


@pytest.mark.skipif(os.name != "nt", reason="only Windows paths are case-insensitive")
def test_onedrive_match_ignores_path_case_on_windows(tmp_path):
    root = tmp_path / "OneDrive"
    assert is_cloud_synced(Path(str(root).upper()) / "runs.db", {"OneDrive": str(root)})
    assert is_cloud_synced(root / "runs.db", {"OneDrive": str(root).lower()})


def test_onedrive_trailing_slash_relative_and_dotdot(tmp_path, monkeypatch):
    root = tmp_path / "OneDrive"
    env = {"OneDrive": str(root) + "/"}
    assert is_cloud_synced(root / "runs.db", env)
    assert is_cloud_synced(root / "a" / ".." / "runs.db", env)
    assert not is_cloud_synced(root / ".." / "Dropbox2" / "runs.db", env)
    root.mkdir()
    monkeypatch.chdir(root)
    assert is_cloud_synced("runs.db", env)  # relative to the working folder
    monkeypatch.chdir(tmp_path)
    assert not is_cloud_synced("runs.db", env)


def test_onedrive_refusal_covers_sqlite_url_and_spares_memory(tmp_path):
    root = tmp_path / "OneDrive"
    env = {"OneDriveConsumer": str(root)}
    with pytest.raises(StoreError, match="OneDrive"):
        open_store("sqlite:///" + str(root / "runs.db"), env=env)
    assert not root.exists()
    with open_store(":memory:", env={"OneDrive": ":memory:"}) as store:
        assert store.list_runs() == ()
    with open_store("sqlite:///:memory:", env=env) as store:
        assert store.list_runs() == ()


def test_real_environment_refuses_a_path_under_this_machines_onedrive():
    roots = [os.environ[k] for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
             if os.environ.get(k)]
    if not roots:
        pytest.skip("no OneDrive on this machine")
    folder = Path(roots[0]) / "zz_stoink_store_test_never_created"
    try:
        with pytest.raises(StoreError, match="OneDrive"):
            open_store(str(folder / "runs.db"), env=os.environ)
        assert not folder.exists()
    finally:
        if folder.exists():  # only reached if the guard failed
            shutil.rmtree(folder)


def test_store_module_has_no_module_level_mutable_state():
    import MainStoinker.engine.store as mod

    leaked = [n for n, v in vars(mod).items()
              if not n.startswith("__") and isinstance(v, (list, dict, set, bytearray))]
    assert leaked == []


@pytest.mark.parametrize("value", ["1E+30", "-1E+30", "922337203685477.5808", "Infinity",
                                   "-Infinity"])
def test_value_too_large_for_sqlite_is_a_store_error(db, value):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        with pytest.raises(StoreError):
            store.write_algos(run_id, [AlgoMeta("z", "X", "AAPL", "backtest", D(value))])
        assert store.load_run(run_id).algos == ()
        # the largest storable value still works
        store.write_algos(run_id, [AlgoMeta("z", "X", "AAPL", "backtest",
                                            D("922337203685477.5807"))])


def test_nan_is_refused_with_an_accurate_message(db):
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        for nan in ("NaN", "sNaN"):
            with pytest.raises(StoreError) as info:
                store.write_algos(run_id, [AlgoMeta("z", "X", "AAPL", "backtest", D(nan))])
            assert "decimal places" not in str(info.value)
            assert "finite" in str(info.value) or "number" in str(info.value)


def test_parent_that_is_a_file_is_a_store_error(tmp_path):
    (tmp_path / "afile").write_text("x")
    with pytest.raises(StoreError):
        SqliteStore(tmp_path / "afile" / "runs.db")


def test_first_open_from_many_threads_at_once(tmp_path):
    import threading

    n = 8
    for attempt in range(6):
        path = tmp_path / f"fresh{attempt}.db"
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []

        def opener(path=path, barrier=barrier, errors=errors) -> None:
            try:
                barrier.wait()
                store = SqliteStore(path, env=NO_ENV)
                try:
                    assert store.list_runs() == ()
                finally:
                    store.close()
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=opener) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []
        con = sqlite3.connect(path)
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            con.close()


@pytest.mark.parametrize("ref", [1.5, b"x", ["a"], ("a", [1]), ("a", object()), {"k": 1},
                                 Decimal(1)])
def test_unrepresentable_broker_ref_refused(db, ref):
    led = Ledger()
    rec = led.open_account("a1", "AAPL", 1000)
    rec.order_placed(ref, time=T0, side="buy", quantity=1, order_type="market")
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos()[:1])
        with pytest.raises(StoreError, match="broker ref"):
            write_all(store, run_id, led)
        assert store.load_run(run_id).order_events == ()


class _FlakyConn:
    """Wraps a connection and fails chosen statements, to reach error paths."""

    def __init__(self, real, fail=()):
        self.real, self.fail = real, set(fail)

    def execute(self, sql, *args):
        if sql in self.fail:
            raise sqlite3.OperationalError(f"disk I/O error on {sql}")
        return self.real.execute(sql, *args)

    def executemany(self, sql, *args):
        return self.real.executemany(sql, *args)

    def close(self):
        self.real.close()


def test_failed_commit_rolls_back_and_leaves_store_usable(db):
    with SqliteStore(db) as store:
        real = store._conn
        flaky = _FlakyConn(real, fail={"COMMIT"})
        store._conn = flaky
        with pytest.raises(StoreError, match="commit"):
            store.begin_run(meta())
        assert not real.in_transaction  # rolled back, not left open
        flaky.fail.clear()
        assert store.list_runs() == ()
        run_id = store.begin_run(meta())
        assert [r.run_id for r in store.list_runs()] == [run_id]


def test_failed_rollback_does_not_hide_the_original_error(db):
    with SqliteStore(db) as store:
        real = store._conn
        store._conn = _FlakyConn(real, fail={"ROLLBACK"})
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos()[:1])
        with pytest.raises(StoreError, match="UNIQUE"):
            store.write_algos(run_id, algos()[:1])  # duplicate; then ROLLBACK fails too
        store._conn = real
        real.execute("ROLLBACK")  # tidy up the transaction the failed rollback left open


def test_failed_begin_is_a_store_error_and_leaves_store_usable(db):
    with SqliteStore(db) as store:
        real = store._conn
        flaky = _FlakyConn(real, fail={"BEGIN IMMEDIATE"})
        store._conn = flaky
        with pytest.raises(StoreError, match="transaction") as info:
            store.begin_run(meta())
        assert isinstance(info.value.__cause__, sqlite3.OperationalError)
        assert not real.in_transaction
        flaky.fail.clear()
        run_id = store.begin_run(meta())
        assert [r.run_id for r in store.list_runs()] == [run_id]


def hold_write_lock(db: Path) -> sqlite3.Connection:
    """A second connection that holds the database's write lock until closed."""
    other = sqlite3.connect(db, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    return other


def test_a_locked_database_is_a_store_error_for_begin_run_and_write_results(db):
    import time

    with SqliteStore(db, busy_timeout=0.2) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, algos())
        other = hold_write_lock(db)
        try:
            started = time.monotonic()
            with pytest.raises(StoreError, match="locked") as info:
                store.begin_run(meta())
            assert isinstance(info.value.__cause__, sqlite3.OperationalError)
            with pytest.raises(StoreError, match="locked"):
                write_all(store, run_id, build_ledger())
            with pytest.raises(StoreError, match="locked"):
                store.end_run(run_id, "failed")
            assert time.monotonic() - started < 5  # a short wait, not the default ten seconds
        finally:
            other.execute("ROLLBACK")
            other.close()
        # the connection is usable again and nothing half-written is there
        assert [r.status for r in store.list_runs()] == ["running"]
        assert store.load_run(run_id).fills == ()
        write_all(store, run_id, build_ledger())
        store.end_run(run_id, "completed")
        assert store.load_run(run_id).summary.status == "completed"


def test_opening_waits_no_longer_than_the_given_busy_timeout(db):
    import time

    SqliteStore(db).close()  # created and migrated
    other = hold_write_lock(db)
    try:
        started = time.monotonic()
        store = SqliteStore(db, busy_timeout=0.2)  # nothing to migrate: opens fine
        try:
            with pytest.raises(StoreError):
                store.begin_run(meta())
        finally:
            store.close()
        assert time.monotonic() - started < 5
    finally:
        other.execute("ROLLBACK")
        other.close()


@pytest.mark.parametrize("spec", ["file:x.db?mode=memory", "file:x.db", "sqlite:x.db",
                                  "mysql://h/db", "sqlite:///file:x.db"])
def test_unsupported_scheme_refused_without_creating_a_file(tmp_path, monkeypatch, spec):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(StoreError, match="unsupported"):
        open_store(spec, env=NO_ENV)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows paths")
@pytest.mark.parametrize("sep", ["\\", "/"])
def test_windows_drive_letter_paths_are_plain_paths(tmp_path, sep):
    spec = str(tmp_path / "runs.db").replace("\\", sep)
    assert spec[1] == ":"
    with open_store(spec, env=NO_ENV) as store:
        assert store.list_runs() == ()
    with open_store("sqlite:///" + spec, env=NO_ENV) as store:
        assert store.list_runs() == ()


@pytest.mark.parametrize(("path", "uri"), [
    (r"C:\Users\me\runs.db", "file:///C:/Users/me/runs.db?mode=ro"),
    (r"C:\my runs\runs.db", "file:///C:/my%20runs/runs.db?mode=ro"),
    (r"C:\a#b\100%\what.db", "file:///C:/a%23b/100%25/what.db?mode=ro"),
    ("C:\\caf\u00e9\\runs.db", "file:///C:/caf%C3%A9/runs.db?mode=ro"),
    (r"\\server\share\runs.db", "file:////server/share/runs.db?mode=ro"),
    (r"\\server\my share\a#b.db", "file:////server/my%20share/a%23b.db?mode=ro"),
])
def test_readonly_uri_is_correct_for_drive_and_unc_paths(path, uri):
    from pathlib import PureWindowsPath

    from MainStoinker.engine.store import _readonly_uri
    assert _readonly_uri(PureWindowsPath(path)) == uri  # type: ignore[arg-type]


def test_readonly_uri_for_posix_path():
    from pathlib import PurePosixPath

    from MainStoinker.engine.store import _readonly_uri
    assert _readonly_uri(PurePosixPath("/tmp/a b.db")) == "file:///tmp/a%20b.db?mode=ro"  # type: ignore[arg-type]


def test_schema_version_is_kept_for_both_open_paths(db):
    with open_store(str(db), env=NO_ENV) as store:
        assert store.schema_version == SCHEMA_VERSION
    with open_store_readonly(str(db), env=NO_ENV) as store:
        assert store.schema_version == SCHEMA_VERSION
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA user_version=1")
    conn.close()
    with open_store_readonly(str(db), env=NO_ENV) as store:
        assert store.schema_version == 1


# --- schema 2: indicator series and data fingerprints ---------------------------

V1_TABLES = ("runs", "algos", "order_events", "fills", "trades", "equity", "ignored_events")
SHA_A = "a" * 64
SHA_B = "b" * 64


def make_v1_file(src: Path, dest: Path) -> None:
    """A version-1 file: the v1 DDL, the rows of `src` copied in, `files` key stripped."""
    con = sqlite3.connect(dest)
    for statement in MIGRATIONS[0]:
        con.execute(statement)
    con.execute("PRAGMA user_version=1")
    con.execute("ATTACH DATABASE ? AS src", (str(src),))
    for table in V1_TABLES:
        cols = ", ".join(r[1] for r in con.execute(f"PRAGMA main.table_info({table})"))
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM src.{table}")
    con.commit()
    con.execute("DETACH DATABASE src")
    import json
    for run_id, text in con.execute("SELECT id, data_json FROM runs").fetchall():
        d = json.loads(text)
        d.pop("files", None)
        con.execute("UPDATE runs SET data_json=? WHERE id=?", (json.dumps(d), run_id))
    con.commit()
    con.close()


def series_fixture() -> tuple[list[SeriesInfo], list[SeriesPoint]]:
    infos = [SeriesInfo("a1", "ema", "line", 1), SeriesInfo("a1", "rsi", "histogram", 0),
             SeriesInfo("a2", "ema", "line", 0)]
    points = [SeriesPoint("a1", "ema", T0 + timedelta(minutes=m), 100.5 + m) for m in range(6)]
    points += [SeriesPoint("a1", "rsi", T0 + timedelta(minutes=m), 50.0 - m) for m in range(3)]
    points += [SeriesPoint("a2", "ema", T0 + timedelta(minutes=1), 7.25)]
    return infos, points


def running_run(store: SqliteStore) -> int:
    run_id = store.begin_run(meta())
    store.write_algos(run_id, algos())
    return run_id


def test_migration_v1_to_v2_keeps_existing_runs(db, tmp_path):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        expected = w.load_run(1)
    make_v1_file(src, db)
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    con.close()
    with SqliteStore(db) as store:
        assert store.schema_version == SCHEMA_VERSION
        loaded = store.load_run(1)
        assert loaded == expected
        assert loaded.series == ()
        assert loaded.summary.data.files == ()
        assert store.load_series(1, "a1") == ()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"algo_series", "algo_series_points"} <= tables


def test_v2_series_roundtrip(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        store.end_run(run_id, "completed")
    with SqliteStore(db) as store:
        data = store.load_run(run_id)
        # only the infos come with the run, ordered by position; no points
        assert data.series == (infos[1], infos[2], infos[0])
        a1 = store.load_series(run_id, "a1")
        assert a1 == tuple(sorted((p for p in points if p.account == "a1"),
                                  key=lambda p: (p.name, p.time)))
        assert all(isinstance(p.value, float) for p in a1)
        assert store.load_series(run_id, "a2") == (points[-1],)
        assert store.load_series(run_id, "nobody") == ()
        with pytest.raises(StoreError, match="no run"):
            store.load_series(99, "a1")


def test_series_points_added_in_later_calls(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points[:2])
        store.write_series(run_id, [], points[2:])
        assert len(store.load_series(run_id, "a1")) == 9
        with pytest.raises(StoreError, match="not declared"):
            store.write_series(run_id, [], [SeriesPoint("a1", "zzz", T0, 1.0)])
        with pytest.raises(StoreError):
            store.write_series(run_id, [infos[0]])  # declared twice
        assert len(store.load_series(run_id, "a1")) == 9


def issued_sql_for_window(store, run_id, start, end) -> list[tuple[str, tuple]]:
    """The points queries `load_series` really issues (statement text and a sample binding)."""
    issued: list[str] = []
    conn = store._db()
    conn.set_trace_callback(issued.append)
    try:
        store.load_series(run_id, "a1", start, end)
    finally:
        conn.set_trace_callback(None)
    return [(q, ()) for q in issued if "FROM algo_series_points" in q]


def _points_plan(store, queries) -> list[str]:
    conn = store._db()
    out: list[str] = []
    for query, _ in queries:  # the trace shows the statement with its values bound
        out.extend(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query))
    return out


def test_series_window_query(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        t = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
        win = store.load_series(run_id, "a1", t(2), t(4))
        assert [(p.name, p.time) for p in win] == [
            ("ema", t(2)), ("ema", t(3)), ("ema", t(4)), ("rsi", t(2))]
        assert {p.time for p in store.load_series(run_id, "a1", start=t(5))} == {t(5)}
        assert {p.time for p in store.load_series(run_id, "a1", end=t(0))} == {t(0)}
        assert store.load_series(run_id, "a1", t(10), t(20)) == ()
        # the window is applied in SQL, not after loading everything
        plan = _points_plan(store, issued_sql_for_window(store, run_id, t(2), t(4)))
        assert plan, "no points query was issued"
        for detail in plan:
            assert "SEARCH algo_series_points USING PRIMARY KEY" in detail
            assert "series_id=?" in detail and "time>?" in detail and "time<?" in detail
            assert "SCAN" not in detail


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "x", None])
def test_series_values_must_be_finite(db, bad):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, infos, [*points, SeriesPoint("a1", "ema", T0, bad)])
        # nothing was written, not even the infos
        assert store.load_run(run_id).series == ()
        assert store.load_series(run_id, "a1") == ()


def test_series_written_only_to_running_run(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.end_run(run_id, "completed")
        with pytest.raises(StoreError, match="already completed"):
            store.write_series(run_id, infos, points)
        with pytest.raises(StoreError, match="no run"):
            store.write_series(99, infos, points)
        assert store.load_run(run_id).series == ()


def test_series_for_unknown_algo_refused(db):
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("ghost", "ema", "line", 0)])
        assert store.load_run(run_id).series == ()


def test_series_deleted_with_run_cascade(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        keep = running_run(store)
        store.write_series(keep, infos[:1], points[:1])
        conn = store._db()
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # the points have no run column: they belong to a series, and go with it
        assert conn.execute("SELECT COUNT(*) FROM algo_series WHERE run_id=?",
                            (run_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM algo_series WHERE run_id=?",
                            (keep,)).fetchone()[0] == 1
        # only the kept run's single point is left
        assert conn.execute("SELECT COUNT(*) FROM algo_series_points").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_readonly_v1_database_has_no_series(db, tmp_path):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        expected = w.load_run(1)
    make_v1_file(src, db)
    before = db.read_bytes()
    # the series tables do not exist in this file: any query against them would raise
    con = sqlite3.connect(db)
    assert not {"algo_series", "algo_series_points"} & {
        r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    with open_store_readonly(str(db), env=NO_ENV) as store:
        assert store.schema_version == 1
        data = store.load_run(1)
        assert data == expected and data.series == ()
        assert store.load_series(1, "a1") == ()
        assert store.load_series(1, "a1", T0, T0 + timedelta(days=1)) == ()
        with pytest.raises(StoreError, match="no run"):
            store.load_series(7, "a1")
        with pytest.raises(StoreError, match="read-only"):
            store.write_series(1, [SeriesInfo("a1", "ema", "line", 0)])
    assert db.read_bytes() == before


def test_readonly_v2_database_reads_series(db):
    infos, points = series_fixture()
    with SqliteStore(db) as w:
        run_id = running_run(w)
        w.write_series(run_id, infos, points)
        w.end_run(run_id, "completed")
    with open_store_readonly(str(db), env=NO_ENV) as store:
        assert len(store.load_run(run_id).series) == 3
        assert len(store.load_series(run_id, "a1")) == 9


def test_data_files_roundtrip_and_absent_key_reads_empty(db):
    files = (("AAPL", SHA_A, 1234), ("MSFT", SHA_B, 99))
    data = DataMeta("csv", "OfflineData", ("AAPL", "MSFT"), T0, T0 + timedelta(days=1), files)
    with SqliteStore(db) as store:
        with_files = store.begin_run(RunMeta("backtest", "legacy", "x", {}, data))
        without = store.begin_run(meta())
        assert DataMeta().files == ()
        assert store.load_run(with_files).summary.data.files == files
        assert store.list_runs()[0].data.files == files
        assert store.load_run(without).summary.data.files == ()
        # a row written before schema 2 has no "files" key at all
        store._db().execute(
            "UPDATE runs SET data_json=json_remove(data_json, '$.files') WHERE id=?",
            (with_files,))
        assert store.load_run(with_files).summary.data.files == ()
        assert store.list_runs()[0].data.files == ()


def test_null_store_accepts_series():
    infos, points = series_fixture()
    with NullStore() as store:
        assert store.schema_version == SCHEMA_VERSION
        run_id = store.begin_run(meta())
        store.write_series(run_id, infos, points)
        store.write_series(run_id, infos, [SeriesPoint("a1", "ema", T0, float("nan"))])
        assert store.load_series(run_id, "a1") == ()
        assert store.load_series(run_id, "a1", T0, T0) == ()


def test_store_protocol_declares_schema_version_and_series():
    from MainStoinker.engine.store import Store
    for name in ("schema_version", "write_series", "load_series"):
        assert hasattr(Store, name)
        assert hasattr(NullStore, name) and hasattr(SqliteStore, name)


# --- schema 2: gaps found by the tester -----------------------------------------


def test_failed_migration_leaves_a_v1_file_untouched(db, tmp_path, monkeypatch):
    import MainStoinker.engine.store as store_module

    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        expected = w.load_run(1)
    make_v1_file(src, db)
    broken = (*MIGRATIONS[:1], (MIGRATIONS[1][0], "CREATE TABLE boom ("), *MIGRATIONS[2:])
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(StoreError):
        SqliteStore(db)
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert not tables & {"algo_series", "algo_series_points"}
    monkeypatch.undo()
    for _ in range(2):  # a retry migrates; opening again does nothing more
        with SqliteStore(db) as store:
            assert store.schema_version == SCHEMA_VERSION
            assert store.load_run(1) == expected


def test_migrated_file_passes_integrity_and_foreign_key_checks(db, tmp_path):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
    make_v1_file(src, db)
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_several_writers_migrate_the_same_v1_file_at_once(tmp_path):
    import threading

    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        expected = w.load_run(1)
    n = 6
    for attempt in range(4):
        path = tmp_path / f"v1_{attempt}.db"
        make_v1_file(src, path)
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []

        def opener(path=path, barrier=barrier, errors=errors) -> None:
            try:
                barrier.wait()
                with SqliteStore(path, env=NO_ENV) as store:
                    assert store.schema_version == SCHEMA_VERSION
                    assert store.load_run(1) == expected
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=opener) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []


def test_series_declared_twice_in_one_call_writes_nothing(db):
    with SqliteStore(db) as store:
        run_id = running_run(store)
        twin = SeriesInfo("a1", "ema", "line", 0)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("a1", "rsi", "line", 1), twin, twin])
        assert store.load_run(run_id).series == ()


def test_points_cannot_use_a_series_declared_in_another_run(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        first = running_run(store)
        store.write_series(first, infos)
        second = running_run(store)
        with pytest.raises(StoreError, match="not declared"):
            store.write_series(second, [], points[:1])
        assert store.load_series(second, "a1") == ()
        assert store.load_series(first, "a1") == ()


def test_series_write_with_nothing_is_a_no_op(db):
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, [])
        store.write_series(run_id, [], [])
        assert store.load_run(run_id).series == ()
        assert store.load_series(run_id, "a1") == ()


def test_series_window_edges(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        t = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
        assert store.load_series(run_id, "a1", t(4), t(2)) == ()  # start after end
        exact = store.load_series(run_id, "a1", t(3), t(3))
        assert [(p.name, p.time) for p in exact] == [("ema", t(3))]
        between = store.load_series(run_id, "a1", t(2) + timedelta(seconds=1),
                                    t(3) + timedelta(seconds=59))
        assert [(p.name, p.time) for p in between] == [("ema", t(3))]
        assert store.load_series(run_id, "a1", end=t(100)) == store.load_series(run_id, "a1")
        # sub-second times sort and compare correctly next to whole seconds
        store.write_series(run_id, [], [
            SeriesPoint("a2", "ema", t(1) + timedelta(microseconds=500000), 1.0),
            SeriesPoint("a2", "ema", t(1) + timedelta(seconds=1), 2.0)])
        got = store.load_series(run_id, "a2")
        assert [p.value for p in got] == [7.25, 1.0, 2.0]
        assert [p.value for p in store.load_series(run_id, "a2", t(1), t(1))] == [7.25]


def test_duplicate_point_times_are_refused_and_write_nothing(db):
    info = SeriesInfo("a1", "ema", "line", 0)
    with SqliteStore(db) as store:
        run_id = running_run(store)
        # within one call: nothing is written, not even the declaration
        with pytest.raises(StoreError):
            store.write_series(run_id, [info], [
                SeriesPoint("a1", "ema", T0, 1.0), SeriesPoint("a1", "ema", T0, 2.0)])
        assert store.load_run(run_id).series == ()
        assert store.load_series(run_id, "a1") == ()
        # across calls: the first point stays, the later batch is rolled back whole
        store.write_series(run_id, [info], [SeriesPoint("a1", "ema", T0, 1.0)])
        with pytest.raises(StoreError):
            store.write_series(run_id, [], [
                SeriesPoint("a1", "ema", T0 + timedelta(minutes=1), 5.0),
                SeriesPoint("a1", "ema", T0, 2.0)])
        assert [p.value for p in store.load_series(run_id, "a1")] == [1.0]
        # the same time in another series is fine
        store.write_series(run_id, [SeriesInfo("a1", "rsi", "line", 1)],
                           [SeriesPoint("a1", "rsi", T0, 3.0)])
        assert len(store.load_series(run_id, "a1")) == 2


def test_stored_time_that_cannot_be_parsed_raises_store_error(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        store._db().execute("UPDATE algo_series_points SET time='NaT' WHERE time=?",
                              (T0.isoformat(),))
        with pytest.raises(StoreError, match="cannot be read"):
            store.load_series(run_id, "a1")


def _bad_times() -> list:
    import numpy as np

    return [None, "2024-01-02T09:30:00", np.datetime64("2024-01-02T09:30:00"),
            datetime(2024, 1, 2).date(), datetime(2024, 1, 2, tzinfo=timezone.utc), 5,
            datetime(2024, 1, 2, 9, 30).timestamp()]


def test_nat_is_refused_without_store_importing_pandas(db):
    import pandas as pd

    assert isinstance(pd.NaT, datetime) and pd.NaT.isoformat() == "NaT"
    with SqliteStore(db) as store:
        run_id = running_run(store)
        info = SeriesInfo("a1", "ema", "line", 0)
        with pytest.raises(StoreError):
            store.write_series(run_id, [info], [SeriesPoint("a1", "ema", pd.NaT, 1.0)])
        assert store.load_run(run_id).series == ()
        # a real Timestamp is fine
        store.write_series(run_id, [info], [SeriesPoint("a1", "ema", pd.Timestamp(T0), 1.0)])
        assert [p.time for p in store.load_series(run_id, "a1")] == [T0]


@pytest.mark.parametrize("index", range(7))
def test_series_time_must_be_a_naive_datetime(db, index):
    bad = _bad_times()[index]
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 0)],
                               [SeriesPoint("a1", "ema", bad, 1.0)])
        assert store.load_run(run_id).series == ()
        assert store.load_series(run_id, "a1") == ()


@pytest.mark.parametrize("bad", ["x", None, 1.7, True, False, 2**63, -(2**63) - 1, 2**70])
def test_series_position_must_be_a_64_bit_int(db, bad):
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("a1", "ema", "line", bad)])
        assert store.load_run(run_id).series == ()
        store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 2**63 - 1),
                                    SeriesInfo("a1", "rsi", "line", -(2**63) + 1)])
        assert len(store.load_run(run_id).series) == 2


@pytest.mark.parametrize("field", ["account", "name", "draw_type"])
@pytest.mark.parametrize("bad", ["", 5, None, b"x"])
def test_series_text_fields_must_be_non_empty_strings(db, field, bad):
    values = {"account": "a1", "name": "ema", "draw_type": "line"}
    values[field] = bad
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo(values["account"], values["name"],
                                                   values["draw_type"], 0)])
        store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 0)])
        if field != "draw_type":
            with pytest.raises(StoreError):
                store.write_series(run_id, [], [
                    SeriesPoint(values["account"], values["name"], T0, 1.0)])
        assert store.load_series(run_id, "a1") == ()


@pytest.mark.parametrize("bad", [True, False, "1.5", b"1", "", None])
def test_series_values_refuse_bool_and_text(db, bad):
    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 0)],
                               [SeriesPoint("a1", "ema", T0, bad)])
        assert store.load_run(run_id).series == ()


def test_series_values_refuse_numpy_bool(db):
    import numpy as np

    with SqliteStore(db) as store:
        run_id = running_run(store)
        with pytest.raises(StoreError):
            store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 0)],
                               [SeriesPoint("a1", "ema", T0, np.bool_(True))])


@pytest.mark.parametrize("bad", [
    ("AAPL", SHA_A), ("AAPL", SHA_A, 1, 2), ("AAPL", SHA_A, "12"), ("AAPL", SHA_A, -1),
    ("AAPL", SHA_A, True), ("AAPL", SHA_A, 1.0), (5, SHA_A, 1), ("AAPL", "abc", 1),
    ("AAPL", "A" * 64, 1), ("AAPL", "g" * 64, 1), ("AAPL", SHA_A + "a", 1),
    ("AAPL", None, 1), "AAPL", None])
def test_data_files_are_validated(db, bad):
    data = DataMeta("csv", "OfflineData", ("AAPL",), T0, T0, (("MSFT", SHA_B, 1), bad))
    with SqliteStore(db) as store:
        with pytest.raises(StoreError):
            store.begin_run(RunMeta("backtest", "legacy", "x", {}, data))
        assert store.list_runs() == ()


def test_series_storage_layout(db):
    with SqliteStore(db) as store:
        conn = store._db()
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(algo_series_points)")}
        assert list(cols) == ["series_id", "time", "value"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(algo_series)")] == [
            "id", "run_id", "algo", "name", "draw_type", "position"]
        pk = sorted((r[5], r[1]) for r in conn.execute("PRAGMA table_info(algo_series_points)")
                    if r[5])
        assert pk == [(1, "series_id"), (2, "time")]
        fks = {(r[2], r[3], r[6]) for r in conn.execute(
            "PRAGMA foreign_key_list(algo_series_points)")}
        assert fks == {("algo_series", "series_id", "CASCADE")}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='algo_series_points'"
                           ).fetchone()[0]
        assert "WITHOUT ROWID" in sql


def test_series_file_growth_is_bounded(db):
    """8 algos x 5 series x 2,300 points (a realistic day-scale run)."""
    algo_ids = [f"algo{i}" for i in range(8)]
    names = [f"s{i}" for i in range(5)]
    infos = [SeriesInfo(a, n, "line", k) for a in algo_ids for k, n in enumerate(names)]
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta())
        store.write_algos(run_id, [
            AlgoMeta(a, "x", "AAPL", "backtest", D("1000"), {}) for a in algo_ids])
        base = db.stat().st_size
        store.write_series(run_id, infos, (
            SeriesPoint(a, n, T0 + timedelta(minutes=m), 100.0 + m * 0.01 + k)
            for a in algo_ids for k, n in enumerate(names) for m in range(2300)))
        store.end_run(run_id, "completed")
        store._db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    grown = db.stat().st_size - base
    points = 8 * 5 * 2300
    # measured 3.74 MB (about 41 bytes per point); the old two-index layout took 8.2 MB
    assert grown < 5_000_000, f"{grown} bytes for {points} points ({grown / points:.0f} each)"


def test_series_values_are_stored_as_floats(db):
    import numpy as np

    with SqliteStore(db) as store:
        run_id = running_run(store)
        values = [3, np.float64(2.5), np.float32(0.5), Decimal("1.25"), -0.0]
        store.write_series(run_id, [SeriesInfo("a1", "ema", "line", 0)], [
            SeriesPoint("a1", "ema", T0 + timedelta(minutes=i), v) for i, v in enumerate(values)])
        got = store.load_series(run_id, "a1")
        assert [p.value for p in got] == [3.0, 2.5, 0.5, 1.25, 0.0]
        assert all(type(p.value) is float for p in got)
        with pytest.raises(StoreError):
            store.write_series(run_id, [], [SeriesPoint("a1", "ema", T0, Decimal("NaN"))])
        with pytest.raises(StoreError):
            store.write_series(run_id, [], [SeriesPoint("a1", "ema", T0, 10**400)])


def test_load_run_does_not_read_series_points(db):
    infos, points = series_fixture()
    with SqliteStore(db) as store:
        run_id = running_run(store)
        store.write_series(run_id, infos, points)
        issued: list[str] = []
        store._db().set_trace_callback(issued.append)
        data = store.load_run(run_id)
        store._db().set_trace_callback(None)
    assert len(data.series) == 3
    assert not [q for q in issued if "algo_series_points" in q]


def test_series_calls_on_a_closed_store_raise_store_error(db):
    store = SqliteStore(db)
    run_id = running_run(store)
    store.close()
    with pytest.raises(StoreError, match="closed"):
        store.load_series(run_id, "a1")
    with pytest.raises(StoreError, match="closed"):
        store.write_series(run_id, [])
