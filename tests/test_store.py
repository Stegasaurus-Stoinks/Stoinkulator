"""The run store: protocol, NullStore, SqliteStore and schema v1.

Databases live only under pytest's tmp_path and `env` is always injected, so
nothing depends on (or touches) the real OneDrive or the repo.
"""

import ast
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from MainStoinker.engine.ledger import EquityPoint, Ledger
from MainStoinker.engine.store import (
    SCHEMA_VERSION,
    AlgoMeta,
    DataMeta,
    NullStore,
    RunMeta,
    SqliteStore,
    StoreError,
    expand_equity,
    is_cloud_synced,
    open_store,
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
    with pytest.raises(StoreError, match=r"LOCALAPPDATA%\\Stoinkulator\\runs.db"):
        open_store(str(target), env=env)
    assert not root.exists()  # refused before anything was created


def test_not_cloud_synced_cases(tmp_path):
    root = tmp_path / "OneDrive"
    env = {"OneDrive": str(root)}
    assert not is_cloud_synced(tmp_path / "OneDrive2" / "runs.db", env)
    assert not is_cloud_synced(tmp_path / "other" / "runs.db", env)
    assert not is_cloud_synced(root / "runs.db", NO_ENV)
    assert not is_cloud_synced(root / "runs.db", {"OneDrive": ""})
    assert not is_cloud_synced(root / "runs.db", {"Other": str(root)})
    # a path that climbs out of the folder is judged after resolving
    assert not is_cloud_synced(root / ".." / "runs.db", env)
    assert is_cloud_synced(tmp_path / "x" / ".." / "OneDrive" / "runs.db", env)


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
