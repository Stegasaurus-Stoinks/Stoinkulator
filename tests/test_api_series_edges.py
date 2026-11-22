"""`GET /api/runs/{id}/series`: inclusive bounds, run states, inputs, 503.

Probes added by the tester of D10 on top of `test_api_series.py`; every database
lives under `tmp_path`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import MainStoinker.api.db as api_db
from MainStoinker.api.errors import UNAVAILABLE
from MainStoinker.engine.store import (
    DataMeta,
    RunMeta,
    SeriesInfo,
    SeriesPoint,
    SqliteStore,
    Store,
    StoreError,
)
from tests.api_support import make_client
from tests.test_store import T0, algos, meta

SECRET = "C:/Users/secret-person/hidden/runs.db"


def build(path: Path, infos: list[SeriesInfo], points: list[SeriesPoint],
          status: str | None = "completed") -> Path:
    with SqliteStore(path) as store:
        run = store.begin_run(meta())
        store.write_algos(run, algos())
        store.write_series(run, infos, points)
        if status is not None:
            store.end_run(run, status)  # type: ignore[arg-type]
    return path


def get(db: Path, params: str, run: int = 1) -> Any:
    return make_client(db).get(f"/api/runs/{run}/series{params}")


def test_window_bounds_are_inclusive_to_the_microsecond(tmp_path: Path) -> None:
    times = [T0, T0 + timedelta(microseconds=5), T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]
    db = build(tmp_path / "b.db", [SeriesInfo("a1", "v", "line", 0)],
               [SeriesPoint("a1", "v", t, float(i)) for i, t in enumerate(times)])

    def stamps(start: str, end: str) -> list[str]:
        r = get(db, f"?algo=a1&start={start}&end={end}")
        assert r.status_code == 200
        return [p["time"] for p in r.json()["series"][0]["points"]]

    assert stamps("2024-01-02T09:30:00", "2024-01-02T09:31:00") == [
        "2024-01-02 09:30:00", "2024-01-02 09:30:00.000005", "2024-01-02 09:31:00"]
    assert stamps("2024-01-02T09:30:00.000005", "2024-01-02T09:30:00.000005") == [
        "2024-01-02 09:30:00.000005"]
    assert stamps("2024-01-02 09:30:00.000006", "2024-01-02 09:30:59.999999") == []
    assert stamps("2024-01-02T09:31", "2024-01-02T09:31") == ["2024-01-02 09:31:00"]


def test_every_draw_type_and_gaps_in_positions(tmp_path: Path) -> None:
    kinds = ["line", "histogram", "marker", "segment", "no-such-type"]
    positions = [40, 3, 3, -1, 900]
    infos = [SeriesInfo("a1", f"s{i}", kind, p)
             for i, (kind, p) in enumerate(zip(kinds, positions, strict=True))]
    infos.append(SeriesInfo("a1", "zero", "line", 5))  # declared, never given a point
    db = build(tmp_path / "d.db", infos,
               [SeriesPoint("a1", f"s{i}", T0, float(i)) for i in range(5)])
    series = get(db, "?algo=a1&start=2024-01-02T09:30&end=2024-01-02T09:30").json()["series"]
    assert [(s["name"], s["draw_type"], s["position"]) for s in series] == [
        ("s3", "segment", -1), ("s1", "histogram", 3), ("s2", "marker", 3), ("zero", "line", 5),
        ("s0", "line", 40), ("s4", "no-such-type", 900)]  # ties on position fall back to name
    assert [len(s["points"]) for s in series] == [1, 1, 1, 0, 1, 1]


@pytest.mark.parametrize("status", ["failed", "running", "completed"])
def test_run_state_does_not_matter(tmp_path: Path, status: str) -> None:
    db = build(tmp_path / "s.db", [SeriesInfo("a1", "v", "line", 0)],
               [SeriesPoint("a1", "v", T0, 1.5)], status=None if status == "running" else status)
    r = get(db, "?algo=a1&start=2024-01-02T09:30&end=2024-01-02T09:31")
    assert r.status_code == 200
    assert r.json()["series"][0]["points"] == [{"time": "2024-01-02 09:30:00", "value": 1.5}]


def test_run_without_last_bar_needs_a_window(tmp_path: Path) -> None:
    infos = [SeriesInfo("a1", "v", "line", 0)]
    empty = RunMeta(kind="backtest", fills="realistic",
                    data=DataMeta("csv", "OfflineData", ("AAPL",), None, None))
    with SqliteStore(tmp_path / "n.db") as store:
        run = store.begin_run(empty)
        store.write_algos(run, algos())
        store.write_series(run, infos, [SeriesPoint("a1", "v", T0, 1.0)])
    assert get(tmp_path / "n.db", "?algo=a1").status_code == 422
    r = get(tmp_path / "n.db", "?algo=a1&start=2024-01-02T09:30&end=2024-01-02T09:31")
    assert r.status_code == 200 and len(r.json()["series"][0]["points"]) == 1


@pytest.mark.parametrize("query", [
    "?algo=AAPL-10/30EMA", "?algo=a1/..", "?algo=%2e%2e", "?algo=a1%00", "?algo=A1",
])
def test_odd_algo_names_are_404_without_a_stack_or_path(tmp_path: Path, query: str) -> None:
    db = build(tmp_path / "o.db", [SeriesInfo("a1", "v", "line", 0)], [])
    r = get(db, query)
    assert r.status_code == 404
    assert str(tmp_path) not in r.text and "Traceback" not in r.text


def test_percent_encoded_algo_name_is_decoded(tmp_path: Path) -> None:
    db = build(tmp_path / "p.db", [SeriesInfo("a1", "v", "line", 0)], [])
    assert get(db, "?algo=%61%31").status_code == 200


@pytest.mark.parametrize("error", [
    StoreError(f"cannot open database {SECRET}: unable to open"),
    sqlite3.OperationalError(f"disk I/O error {SECRET}"),
])
def test_broken_database_is_the_fixed_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          error: Exception) -> None:
    db = build(tmp_path / "e.db", [SeriesInfo("a1", "v", "line", 0)], [])

    def broken(spec: str, **kwargs: object) -> Store:
        raise error

    monkeypatch.setattr(api_db, "open_store_readonly", broken)
    r = get(db, "?algo=a1")
    assert r.status_code == 503
    assert r.json() == {"detail": UNAVAILABLE}
    assert "secret" not in r.text


def test_series_read_failing_midway_is_the_fixed_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = build(tmp_path / "m.db", [SeriesInfo("a1", "v", "line", 0)], [])

    def boom(self: SqliteStore, *args: object, **kwargs: object) -> None:
        raise StoreError(f"read failed in {SECRET}")

    monkeypatch.setattr(SqliteStore, "load_series", boom)
    r = get(db, "?algo=a1")
    assert r.status_code == 503 and r.json() == {"detail": UNAVAILABLE}


def test_repeated_requests_in_one_process_do_not_leak_state(tmp_path: Path) -> None:
    db = build(tmp_path / "l.db", [SeriesInfo("a1", "v", "line", 0)],
               [SeriesPoint("a1", "v", T0, 3.0), SeriesPoint("a1", "v", datetime(2024, 1, 3, 9, 30), 4.0)])
    client = make_client(db)
    first = client.get("/api/runs/1/series?algo=a1").json()
    client.get("/api/runs/1/series?algo=a1&start=2024-01-02T09:30&end=2024-01-02T09:30")
    assert client.get("/api/runs/1/series?algo=a1").json() == first
