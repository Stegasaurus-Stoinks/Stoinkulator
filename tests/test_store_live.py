"""Schema 3 of the run store: what a live run needs (issue 191).

`runs.last_write_at` and `runs.end_reason`, the tables `run_bars` and `run_counts`,
and the methods `touch`, `write_bars`, `set_counts`, `update_run_data` and
`end_run(reason=...)`. Every database lives under `tmp_path`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import MainStoinker.engine.store as store_module
from MainStoinker.engine.store import (
    END_REASONS,
    MIGRATIONS,
    SCHEMA_VERSION,
    DataMeta,
    NullStore,
    SqliteStore,
    StoredBar,
    StoreError,
    open_store_readonly,
)
from tests.test_store import NO_ENV, T0, algos, meta, stored_run

V2_TABLES = ("runs", "algos", "order_events", "fills", "trades", "equity", "ignored_events",
             "algo_series", "algo_series_points")
NEW_RUN_COLUMNS = ("last_write_at", "end_reason")


class Clock:
    """An injected clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


def make_old_file(src: Path, dest: Path, version: int) -> None:
    """A database at an older schema: that version's DDL plus the rows of `src`.

    The DDL is the real `MIGRATIONS` of the code, applied up to `version`; rows
    are copied column by column, so a column the old schema lacks is dropped.
    """
    con = sqlite3.connect(dest)
    for step in MIGRATIONS[:version]:
        for statement in step:
            con.execute(statement)
    con.execute(f"PRAGMA user_version={version}")
    con.execute("ATTACH DATABASE ? AS src", (str(src),))
    for table in V2_TABLES[: 7 if version < 2 else len(V2_TABLES)]:
        cols = ", ".join(r[1] for r in con.execute(f"PRAGMA main.table_info({table})"))
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM src.{table}")
    con.commit()
    con.execute("DETACH DATABASE src")
    con.close()


def columns(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def tables(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def user_version(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def v2_file(tmp_path: Path) -> tuple[Path, object]:
    """A schema-2 database with one completed run, and what that run reads as."""
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        expected = w.load_run(1)
    dest = tmp_path / "v2.db"
    make_old_file(src, dest, 2)
    return dest, expected


def running(store: SqliteStore) -> int:
    run_id = store.begin_run(meta("paper"))
    store.write_algos(run_id, algos())
    return run_id


def bar(minute: int = 0, symbol: str = "AAPL", close: float = 101.25) -> StoredBar:
    return StoredBar(symbol, T0 + timedelta(minutes=minute), 100.0, 102.5, 99.75, close, 1234.0)


# --- migration -------------------------------------------------------------------


def test_schema_version_is_three():
    assert SCHEMA_VERSION == 3 == len(MIGRATIONS)


def test_migrates_v2_database_and_old_runs_read_with_none(v2_file):
    path, expected = v2_file
    assert user_version(path) == 2
    assert not set(NEW_RUN_COLUMNS) & columns(path, "runs")
    with SqliteStore(path) as store:
        assert store.schema_version == 3
        (summary,) = store.list_runs()
        assert summary.last_write_at is None and summary.end_reason is None
        loaded = store.load_run(1)
        assert loaded == expected
        assert loaded.summary.last_write_at is None and loaded.summary.end_reason is None
        assert loaded.summary.status == "completed"
    assert user_version(path) == 3
    assert set(NEW_RUN_COLUMNS) <= columns(path, "runs")
    assert {"run_bars", "run_counts"} <= tables(path)
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM run_bars").fetchone()[0] == 0
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_a_migrated_database_takes_live_writes(v2_file):
    path, _ = v2_file
    with SqliteStore(path) as store:
        run_id = running(store)
        store.write_bars(run_id, [bar(0)])
        store.end_run(run_id, "failed", "error")
        old, new = store.list_runs()
        assert (old.end_reason, new.end_reason) == (None, "error")


def test_migration_is_atomic(v2_file, monkeypatch):
    """A failure after the ALTERs and the first CREATE leaves the version-2 schema intact."""
    path, expected = v2_file
    broken = (*MIGRATIONS[:2], (*MIGRATIONS[2][:-1], "CREATE TABLE boom ("))
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(StoreError):
        SqliteStore(path)
    assert user_version(path) == 2
    assert not set(NEW_RUN_COLUMNS) & columns(path, "runs")
    assert not {"run_bars", "run_counts", "boom"} & tables(path)
    monkeypatch.undo()
    with SqliteStore(path) as store:  # a retry migrates cleanly
        assert store.schema_version == 3
        assert store.load_run(1) == expected


def test_newer_database_is_refused_with_a_clear_error(db):
    SqliteStore(db).close()
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(StoreError, match="newer than this code"):
        SqliteStore(db)
    with pytest.raises(StoreError, match="newer than this code"):
        open_store_readonly(str(db), env=NO_ENV)


def test_readonly_opens_every_version(tmp_path):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
        running(w)
    for version in range(1, SCHEMA_VERSION + 1):
        path = tmp_path / f"v{version}.db"
        if version == SCHEMA_VERSION:
            with SqliteStore(src) as w:  # fold the WAL in
                pass
            path.write_bytes(src.read_bytes())
            con = sqlite3.connect(path)
            con.execute("PRAGMA journal_mode=DELETE")
            con.close()
        else:
            make_old_file(src, path, version)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        files = sorted(p.name for p in tmp_path.iterdir())
        with open_store_readonly(str(path), env=NO_ENV) as store:
            assert store.schema_version == version
            runs = store.list_runs()
            assert [r.status for r in runs] == ["completed", "running"]
            assert all(r.last_write_at is None and r.end_reason is None for r in runs)
            assert store.load_run(1).summary.status == "completed"
            assert store.load_run(2).summary == runs[1]
            with pytest.raises(StoreError, match="read-only"):
                store.touch(2)
        assert user_version(path) == version  # not migrated
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before  # not written
        assert sorted(p.name for p in tmp_path.iterdir()) == files  # no -wal / -shm left


# --- the new methods -------------------------------------------------------------


def test_touch_updates_last_write(db):
    clock = Clock()
    with SqliteStore(db, clock=clock) as store:
        run_id = running(store)
        assert store.list_runs()[0].last_write_at is None
        store.touch(run_id)
        assert store.list_runs()[0].last_write_at == clock.now
        clock.now += timedelta(seconds=30)
        store.touch(run_id)
        summary = store.load_run(run_id).summary
        assert summary.last_write_at == clock.now
        assert summary.last_write_at.tzinfo is not None
        with pytest.raises(StoreError, match="no run"):
            store.touch(99)
        store.end_run(run_id, "completed")
        with pytest.raises(StoreError, match="already completed"):
            store.touch(run_id)


def test_the_other_live_writers_update_last_write_too(db):
    clock = Clock()
    with SqliteStore(db, clock=clock) as store:
        run_id = running(store)
        for step, write in enumerate([
                lambda: store.write_bars(run_id, [bar(0)]),
                lambda: store.set_counts(run_id, {"symbol": {"AAPL": 1}}),
                lambda: store.update_run_data(run_id, DataMeta(source="live"))], start=1):
            clock.now += timedelta(seconds=step)
            write()
            assert store.list_runs()[0].last_write_at == clock.now


def test_end_run_records_reason(db):
    with SqliteStore(db) as store:
        ids = []
        for status, reason in [("completed", "session_end"), ("failed", "connection_closed"),
                               ("completed", None)]:
            run_id = running(store)
            store.end_run(run_id, status, reason)
            ids.append(run_id)
        summaries = store.list_runs()
        assert [(s.status, s.end_reason) for s in summaries] == [
            ("completed", "session_end"), ("failed", "connection_closed"), ("completed", None)]
        assert store.load_run(ids[1]).summary.end_reason == "connection_closed"
        assert all(s.ended_at is not None for s in summaries)
    assert END_REASONS == ("session_end", "minutes_elapsed", "stop_file", "connection_closed",
                           "not_paper", "interrupted", "error", "abandoned")


def test_end_run_refuses_an_unknown_reason_and_keeps_the_run_running(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        with pytest.raises(StoreError, match="unknown end reason"):
            store.end_run(run_id, "completed", "stopped")
        with pytest.raises(StoreError, match="cannot end a run as"):
            store.end_run(run_id, "stopped", "stop_file")  # type: ignore[arg-type]
        assert store.list_runs()[0].status == "running"
        store.end_run(run_id, "completed", "stop_file")
        assert store.list_runs()[0].end_reason == "stop_file"


def test_write_bars_rejects_duplicate_minute(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        store.write_bars(run_id, [bar(0), bar(1), bar(0, "MSFT")])
        with pytest.raises(StoreError, match="already stored"):
            store.write_bars(run_id, [bar(2), bar(1)])  # minute 1 of AAPL again
        with pytest.raises(StoreError, match="already stored"):
            store.write_bars(run_id, [bar(3), bar(3)])  # twice in one call
        store.write_bars(run_id, [bar(2)])  # a failed call kept nothing, so 2 is free
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT symbol, time, open, high, low, close, volume FROM run_bars"
                " ORDER BY symbol, time").fetchall()
        finally:
            con.close()
        assert [(r[0], r[1]) for r in rows] == [
            ("AAPL", "2024-01-02T09:30:00"), ("AAPL", "2024-01-02T09:31:00"),
            ("AAPL", "2024-01-02T09:32:00"), ("MSFT", "2024-01-02T09:30:00")]
        assert rows[0][2:] == (100.0, 102.5, 99.75, 101.25, 1234.0)
        # the same minute under another run is a different row
        other = running(store)
        store.write_bars(other, [bar(0)])


def test_write_bars_refuses_bad_values_and_closed_runs(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        for bad in (bar(0, close=float("nan")), bar(0, symbol=""),
                    StoredBar("AAPL", T0.replace(tzinfo=timezone.utc), 1, 1, 1, 1, 1)):
            with pytest.raises(StoreError):
                store.write_bars(run_id, [bad])
        with pytest.raises(StoreError, match="no run"):
            store.write_bars(99, [bar(0)])
        store.end_run(run_id, "completed")
        with pytest.raises(StoreError, match="already completed"):
            store.write_bars(run_id, [bar(0)])
        con = sqlite3.connect(db)
        try:
            assert con.execute("SELECT COUNT(*) FROM run_bars").fetchone()[0] == 0
        finally:
            con.close()


def test_set_counts_upserts(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        other = running(store)
        store.set_counts(run_id, {"symbol": {"AAPL": 3, "MSFT": 2}, "algo": {"a1": 1}})
        store.set_counts(run_id, {"symbol": {"AAPL": 7}, "algo": {"a2": 4}})
        store.set_counts(other, {"symbol": {"AAPL": 99}})
        con = sqlite3.connect(db)
        try:
            rows = con.execute("SELECT run_id, scope, key, value FROM run_counts"
                               " ORDER BY run_id, scope, key").fetchall()
        finally:
            con.close()
        assert rows == [(run_id, "algo", "a1", 1), (run_id, "algo", "a2", 4),
                        (run_id, "symbol", "AAPL", 7), (run_id, "symbol", "MSFT", 2),
                        (other, "symbol", "AAPL", 99)]
        with pytest.raises(StoreError):
            store.set_counts(run_id, {"symbol": {"AAPL": 1.5}})  # type: ignore[dict-item]
        with pytest.raises(StoreError):
            store.set_counts(run_id, {"symbol": {"AAPL": True}})  # type: ignore[dict-item]


def test_update_run_data_replaces_the_data_and_keeps_the_gap_rule(db):
    from MainStoinker.engine.gaprule import GapRuleReport

    with SqliteStore(db) as store:
        run_id = running(store)
        report = GapRuleReport((("AAPL", 2),), ())
        store.write_gap_rule(run_id, report)
        last = T0 + timedelta(minutes=389)
        store.update_run_data(run_id, DataMeta(
            source="live", symbols=("AAPL",), first_bar=T0, last_bar=last, replayed_minutes=390,
            warnings=("late bar",)))
        data = store.load_run(run_id).summary.data
        assert (data.source, data.last_bar, data.replayed_minutes) == ("live", last, 390)
        assert data.warnings == ("late bar",)
        assert data.gap_rule == report


# --- the #70 items ---------------------------------------------------------------


def test_busy_database_raises_store_error(db):
    with SqliteStore(db, busy_timeout=0.05) as store:
        run_id = running(store)
        other = sqlite3.connect(db, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        try:
            for call in (
                    lambda: store.touch(run_id),
                    lambda: store.write_bars(run_id, [bar(0)]),
                    lambda: store.set_counts(run_id, {"symbol": {"AAPL": 1}}),
                    lambda: store.update_run_data(run_id, DataMeta()),
                    lambda: store.end_run(run_id, "failed", "error")):
                with pytest.raises(StoreError, match="locked") as info:
                    call()
                assert isinstance(info.value.__cause__, sqlite3.OperationalError)
        finally:
            other.execute("ROLLBACK")
            other.close()
        store.touch(run_id)  # the connection is usable again
        assert store.list_runs()[0].status == "running"


def test_a_file_that_will_not_switch_to_wal_is_refused(db, monkeypatch):
    """The pragma can be refused without an error; the store must notice."""
    monkeypatch.setattr(store_module, "_enable_wal", lambda conn, wait=10.0: None)
    with pytest.raises(StoreError, match="WAL"):
        SqliteStore(db)
    monkeypatch.undo()
    with SqliteStore(db) as store:  # the real path switches and passes the check
        assert store.schema_version == SCHEMA_VERSION
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


def test_an_in_memory_store_does_not_need_wal():
    with SqliteStore(":memory:") as store:
        assert store.schema_version == SCHEMA_VERSION


def test_null_store_accepts_the_new_methods():
    store = NullStore()
    run_id = store.begin_run(meta())
    store.touch(run_id)
    store.write_bars(run_id, [bar(0)])
    store.set_counts(run_id, {"symbol": {"AAPL": 1}})
    store.update_run_data(run_id, DataMeta())
    store.end_run(run_id, "completed", "session_end")


# --- readers (issue 193) ---------------------------------------------------------


def test_load_counts_reads_what_set_counts_wrote(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        other = running(store)
        assert store.load_counts(run_id) == {}
        store.set_counts(run_id, {"symbol": {"MSFT:fed": 2, "AAPL:fed": 3}, "algo": {"a1": 1}})
        store.set_counts(run_id, {"symbol": {"AAPL:fed": 7}})
        store.set_counts(other, {"symbol": {"AAPL:fed": 99}})
        assert store.load_counts(run_id) == {"algo": {"a1": 1},
                                             "symbol": {"AAPL:fed": 7, "MSFT:fed": 2}}
        with pytest.raises(StoreError):
            store.load_counts(99)


def test_load_last_bars_is_the_latest_bar_of_each_symbol(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        other = running(store)
        assert store.load_last_bars(run_id) == ()
        store.write_bars(run_id, [bar(2, "MSFT", 50.0), bar(0, "AAPL", 1.0), bar(3, "AAPL", 3.0)])
        store.write_bars(run_id, [bar(1, "AAPL", 2.0), bar(9, "MSFT", 55.5)])
        store.write_bars(other, [bar(20, "AAPL", 8.0)])
        assert store.load_last_bars(run_id) == (bar(3, "AAPL", 3.0), bar(9, "MSFT", 55.5))
        with pytest.raises(StoreError):
            store.load_last_bars(99)


@pytest.mark.parametrize("version", [1, 2])
def test_readers_return_empty_on_older_schemas(tmp_path, version):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
    old = tmp_path / f"v{version}.db"
    make_old_file(src, old, version)
    store = open_store_readonly(str(old), env=NO_ENV)
    try:
        assert store.schema_version == version
        assert store.load_counts(1) == {}
        assert store.load_last_bars(1) == ()
        with pytest.raises(StoreError):
            store.load_counts(2)
    finally:
        store.close()


def test_readers_work_on_a_readonly_current_database(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        store.write_bars(run_id, [bar(0)])
        store.set_counts(run_id, {"symbol": {"AAPL:late": 4}})
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        assert store.load_counts(run_id) == {"symbol": {"AAPL:late": 4}}
        assert store.load_last_bars(run_id) == (bar(0),)
    finally:
        store.close()


def test_null_store_readers_are_empty():
    store = NullStore()
    assert store.load_counts(1) == {} and store.load_last_bars(1) == ()


# --- load_bars (issue 194) -----------------------------------------------------------


def test_load_bars_filters_by_symbol_window_and_limit(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        other = running(store)
        assert store.load_bars(run_id, "AAPL") == ()
        store.write_bars(run_id, [bar(3, "AAPL", 3.0), bar(0, "AAPL", 1.0), bar(1, "AAPL", 2.0),
                                  bar(2, "MSFT", 50.0), bar(5, "AAPL", 5.0)])
        store.write_bars(other, [bar(0, "AAPL", 9.0)])
        assert store.load_bars(run_id, "AAPL") == (
            bar(0, "AAPL", 1.0), bar(1, "AAPL", 2.0), bar(3, "AAPL", 3.0), bar(5, "AAPL", 5.0))
        assert store.load_bars(run_id, "MSFT") == (bar(2, "MSFT", 50.0),)
        assert store.load_bars(run_id, "NONE") == ()
        # both bounds are inclusive
        assert store.load_bars(run_id, "AAPL", T0 + timedelta(minutes=1),
                               T0 + timedelta(minutes=3)) == (bar(1, "AAPL", 2.0),
                                                              bar(3, "AAPL", 3.0))
        assert store.load_bars(run_id, "AAPL", start=T0 + timedelta(minutes=3)) == (
            bar(3, "AAPL", 3.0), bar(5, "AAPL", 5.0))
        assert store.load_bars(run_id, "AAPL", end=T0) == (bar(0, "AAPL", 1.0),)
        assert store.load_bars(run_id, "AAPL", limit=2) == (bar(0, "AAPL", 1.0),
                                                            bar(1, "AAPL", 2.0))
        assert store.load_bars(run_id, "AAPL", limit=0) == ()
        with pytest.raises(StoreError):
            store.load_bars(99, "AAPL")


@pytest.mark.parametrize("version", [1, 2])
def test_load_bars_is_empty_on_older_schemas(tmp_path, version):
    src = tmp_path / "src.db"
    with SqliteStore(src) as w:
        stored_run(w)
    old = tmp_path / f"v{version}.db"
    make_old_file(src, old, version)
    store = open_store_readonly(str(old), env=NO_ENV)
    try:
        assert store.load_bars(1, "AAPL") == ()
        with pytest.raises(StoreError):
            store.load_bars(2, "AAPL")
    finally:
        store.close()


def test_load_bars_works_on_a_readonly_current_database_and_null_store(db):
    with SqliteStore(db) as store:
        run_id = running(store)
        store.write_bars(run_id, [bar(0), bar(1)])
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        assert store.load_bars(run_id, "AAPL") == (bar(0), bar(1))
    finally:
        store.close()
    assert NullStore().load_bars(1, "AAPL") == ()
