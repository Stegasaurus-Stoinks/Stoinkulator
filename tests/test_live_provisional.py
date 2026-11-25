"""Provisional counts of a running live run (issue 220): the writer's heartbeat stores the counts
and the data record, the API marks them `provisional`, `finish` still writes the final ones."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from MainStoinker.engine.store import DataMeta, SqliteStore, StoreBusyError
from tests.api_support import make_client
from tests.test_live_store import NO_ENV, Clock, Synth, finish, synth

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
    """A store whose count and data-record writes report a busy database on demand."""

    busy = False

    def set_counts(self, run_id: int, counts: Any) -> None:
        if self.busy:
            raise StoreBusyError("database is locked")
        super().set_counts(run_id, counts)

    def update_run_data(self, run_id: int, data: DataMeta) -> None:
        if self.busy:
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
    assert s.writer.provisional_skipped == 2 and s.writer.error is None
    assert pauses == []  # no waiting inside a heartbeat
    assert store.load_counts(1) == {}
    assert store.list_runs()[0].last_write_at == clock.now  # the plain heartbeat still happened
    s.step(1)  # the step is not affected
    store.busy = False
    s.writer.heartbeat(COUNTS, meta_so_far(2))  # the next heartbeat stores it
    assert s.writer.provisional_skipped == 2
    assert store.load_counts(1)["symbol"]["AAA:fed"] == 2
    assert finish(s, counts={("AAA", "fed", None): 2}).ok
    store.close()


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
