"""Provisional counts of a running live run (issue 220): the writer's heartbeat stores the counts
and the data record, the API marks them `provisional`, `finish` still writes the final ones."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.engine.store import DataMeta, SqliteStore, StoreBusyError
from tests.api_support import make_client
from tests.test_live_store import NO_ENV, Clock, Holder, Synth, finish, synth

COUNTS: Any = {("AAA", "fed", None): 2, ("AAA", "late", None): 1,
               ("AAA", "dropped", "farm_break"): 3}


def meta_so_far(step: int) -> DataMeta:
    return DataMeta(source="live", directory="bars", symbols=("AAA",), replayed_minutes=step,
                    missing_minutes=(("AAA", 0),), uncovered_minutes=0)


def table(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def synth_in(root: Path, name: str, actions: dict | None = None) -> Synth:
    folder = root / name
    folder.mkdir()
    return synth(folder, actions)


def test_a_running_run_shows_provisional_counts_through_the_store_and_the_api(
    tmp_path: Path
) -> None:
    s = synth(tmp_path)
    s.step(0)
    s.step(1)
    assert s.store.load_counts(1) == {}  # nothing before the first heartbeat with counts
    s.writer.heartbeat(COUNTS, meta_so_far(2))
    assert s.store.load_counts(1) == {"symbol": {
        "AAA:dropped:farm_break": 3, "AAA:fed": 2, "AAA:late": 1}}
    assert s.store.load_run(1).summary.data.replayed_minutes == 2
    assert s.store.list_runs()[0].status == "running"
    document = make_client(s.db).get("/api/runs/1").json()
    counts = document["live_counts"]
    assert counts["provisional"] is True
    assert counts["fed"] == {"AAA": 2} and counts["late"] == {"AAA": 1}
    assert counts["dropped"] == {"AAA": {"farm_break": 3}}
    assert document["run"]["replayed_minutes"] == 2
    s.store.close()


def test_the_counts_of_a_finished_run_are_final_and_not_provisional(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    s.writer.heartbeat(COUNTS, meta_so_far(1))
    assert finish(s, counts={("AAA", "fed", None): 1}).ok
    counts = make_client(s.db).get("/api/runs/1").json()["live_counts"]
    assert "provisional" not in counts and counts["fed"] == {"AAA": 1}
    s.store.close()


class BusyForProvisional(SqliteStore):
    """A store whose provisional write (and, with `busy_final`, the final counts and data
    writes) report a busy database on demand."""

    busy = False
    busy_final = False

    def write_provisional(self, run_id: int, counts: Any, data: Any) -> None:
        if self.busy:
            raise StoreBusyError("database is locked")
        super().write_provisional(run_id, counts, data)

    def set_counts(self, run_id: int, counts: Any, *, replace: bool = False) -> None:
        if self.busy_final:
            raise StoreBusyError("database is locked")
        super().set_counts(run_id, counts, replace=replace)

    def update_run_data(self, run_id: int, data: DataMeta) -> None:
        if self.busy_final:
            raise StoreBusyError("database is locked")
        super().update_run_data(run_id, data)


def test_a_busy_database_at_a_heartbeat_skips_the_provisional_write_and_fails_nothing(
    tmp_path: Path
) -> None:
    clock = Clock()
    store = BusyForProvisional(tmp_path / "synth.db", env=NO_ENV, clock=clock)
    pauses: list[float] = []
    s = synth(tmp_path, store=store, sleep=pauses.append)
    s.step(0)
    store.busy = True
    clock.now += timedelta(seconds=20)
    s.writer.heartbeat(COUNTS, meta_so_far(1))  # no exception
    assert s.writer.provisional_skipped == 1 and s.writer.error is None
    assert pauses == []  # no waiting inside a heartbeat
    assert store.load_counts(1) == {}
    assert store.list_runs()[0].last_write_at == clock.now  # the plain heartbeat still happened
    s.step(1)  # the step is not affected
    store.busy = False
    s.writer.heartbeat(COUNTS, meta_so_far(2))  # the next heartbeat stores it
    assert s.writer.provisional_skipped == 1
    assert store.load_counts(1)["symbol"]["AAA:fed"] == 2
    assert finish(s, counts={("AAA", "fed", None): 2}).ok
    store.close()


def test_the_provisional_write_never_waits_for_a_database_another_writer_holds(
    tmp_path: Path
) -> None:
    store = SqliteStore(tmp_path / "synth.db", env=NO_ENV, busy_timeout=2.0)  # LIVE_BUSY_TIMEOUT
    s = synth(tmp_path, store=store)
    s.step(0)
    holder = Holder(tmp_path / "synth.db")
    holder.hold()
    try:
        started = time.monotonic()
        s.writer._write_provisional(1, COUNTS, meta_so_far(1))  # noqa: SLF001
        waited = time.monotonic() - started
        with pytest.raises(StoreBusyError):
            store.write_provisional(1, {}, None)
    finally:
        holder.close()
    assert waited < 0.5, waited  # the store's own wait would be 2 s per attempt
    assert s.writer.provisional_skipped == 1 and s.writer.error is None
    # the store's wait is back to what it was for every other write
    assert store._db().execute("PRAGMA busy_timeout").fetchone() == (2000,)  # noqa: SLF001
    store.close()


def test_the_data_record_is_marked_provisional_until_finish_clears_the_mark(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    assert "provisional" not in json.loads(table(s.db, "SELECT data_json FROM runs")[0][0])
    s.writer.heartbeat(COUNTS, meta_so_far(1))
    assert json.loads(table(s.db, "SELECT data_json FROM runs")[0][0])["provisional"] is True
    assert s.store.load_run(1).summary.data.provisional is True
    assert finish(s, counts={("AAA", "fed", None): 1}).ok
    assert "provisional" not in json.loads(table(s.db, "SELECT data_json FROM runs")[0][0])
    assert s.store.load_run(1).summary.data.provisional is False
    s.store.close()


def test_the_mark_stays_when_finish_could_not_write_its_counts_and_data(tmp_path: Path) -> None:
    store = BusyForProvisional(tmp_path / "synth.db", env=NO_ENV)
    s = synth(tmp_path, store=store)
    s.step(0)
    s.writer.heartbeat(COUNTS, meta_so_far(1))
    store.busy_final = True
    outcome = finish(s, counts={("AAA", "fed", None): 1})
    assert outcome.status == "failed" and outcome.busy  # `end_run` was written
    assert store.load_run(1).summary.data.provisional is True
    assert store.load_counts(1)["symbol"]["AAA:fed"] == 2  # still the heartbeat's counts
    document = make_client(s.db).get("/api/runs/1").json()
    assert document["live_counts"]["provisional"] is True  # the API reads the same mark
    store.close()


def test_the_final_counts_replace_what_only_a_heartbeat_stored(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    s.writer.heartbeat({("AAA", "fed", None): 1, ("AAA", "dropped", "stalled"): 4},
                       meta_so_far(1))
    assert "AAA:dropped:stalled" in s.store.load_counts(1)["symbol"]
    assert finish(s, counts={("AAA", "fed", None): 1}).ok
    assert s.store.load_counts(1) == {"symbol": {"AAA:fed": 1}}
    s.store.close()


def test_a_completed_run_is_stored_exactly_as_without_provisional_writes(tmp_path: Path) -> None:
    actions = {1: lambda a, t: a.enter(t), 3: lambda a, t: a.enter(t)}
    plain = synth_in(tmp_path, "plain", actions)
    early = synth_in(tmp_path, "early", actions)
    for i in range(4):
        plain.step(i)
        early.step(i)
        early.writer.heartbeat({("AAA", "fed", None): i + 1, ("AAA", "late", None): i,
                                ("AAA", "dropped", "stalled"): 9}, meta_so_far(i + 1))
    final = {("AAA", "fed", None): 4, ("AAA", "dropped", "stalled"): 9, ("AAA", "late", None): 0}
    assert finish(plain, counts=final).ok and finish(early, counts=final).ok
    for sql in ("SELECT scope, key, value FROM run_counts ORDER BY scope, key",
                "SELECT data_json FROM runs", "SELECT config_json FROM runs",
                "SELECT * FROM fills", "SELECT * FROM equity ORDER BY algo, time",
                "SELECT * FROM run_bars ORDER BY symbol, time"):
        assert table(early.db, sql) == table(plain.db, sql), sql
    plain.store.close()
    early.store.close()
