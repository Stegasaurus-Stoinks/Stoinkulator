"""`GET /api/runs/{id}/series`: windows, ordering, availability and the 422 rules.

Series are written straight through the store into databases under `tmp_path`;
`test_series_of_stored_backtest` goes through a real `backtest --db`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.api.routes_series import MAX_ECHO
from MainStoinker.engine.store import SeriesInfo, SeriesPoint, SqliteStore
from tests.api_support import make_client
from tests.test_api_bars import store_run, write_bars
from tests.test_cli_modes import EMA, Env
from tests.test_store import T0, algos, make_v1_file, meta, stored_run

DAY2 = T0 + timedelta(days=1)  # the run's last bar: 2024-01-03 09:30


def _points(name: str, start: datetime, values: list[float]) -> list[SeriesPoint]:
    return [SeriesPoint("a1", name, start + timedelta(minutes=i), v) for i, v in enumerate(values)]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """One run, algos a1 and a2; a1 has three series declared out of position order."""
    path = tmp_path / "runs.db"
    infos = [SeriesInfo("a1", "ema", "line", 2), SeriesInfo("a1", "rsi", "histogram", 0),
             SeriesInfo("a1", "empty", "segment", 1), SeriesInfo("a2", "other", "line", 0)]
    points = (_points("ema", T0, [100.0, 100.1234567, 123456.789])
              + _points("ema", DAY2, [200.5, 201.25])
              + _points("rsi", DAY2, [50.0, 49.0])
              + _points("rsi", T0, [55.0])
              + [SeriesPoint("a2", "other", DAY2, 7.0)])
    with SqliteStore(path) as store:
        stored_run(store)  # run 1: no series
        run2 = store.begin_run(meta())  # run 2: the series above
        store.write_algos(run2, algos())
        store.write_series(run2, infos, points)
        store.end_run(run2, "completed")
    return path


def get(db: Path, params: str, run: int = 2) -> Any:
    return make_client(db).get(f"/api/runs/{run}/series{params}")


def test_series_for_window(db: Path) -> None:
    r = get(db, "?algo=a1&start=2024-01-02T09:30:00&end=2024-01-02T09:31:00")
    assert r.status_code == 200
    body = r.json()
    assert body["algo"] == "a1" and body["available"] is True
    by_name = {s["name"]: s for s in body["series"]}
    assert by_name["ema"]["points"] == [{"time": "2024-01-02 09:30:00", "value": 100.0},
                                        {"time": "2024-01-02 09:31:00", "value": 100.123457}]
    assert by_name["ema"]["draw_type"] == "line" and by_name["ema"]["position"] == 2
    assert by_name["rsi"]["points"] == [{"time": "2024-01-02 09:30:00", "value": 55.0}]
    # a declared series with no point in the window is still listed
    assert by_name["empty"] == {"name": "empty", "draw_type": "segment", "position": 1,
                                "points": []}


@pytest.mark.parametrize(("stored", "sent"), [
    (12345.65, 12345.65),  # a price keeps its cents however large it is
    (20123.25, 20123.25),
    (0.1234567, 0.123457),
    (-0.1234567, -0.123457),
    (0.0, 0.0),
])
def test_values_rounded_to_six_decimal_places(tmp_path: Path, stored: float, sent: float) -> None:
    path = tmp_path / "r.db"
    with SqliteStore(path) as store:
        run = store.begin_run(meta())
        store.write_algos(run, algos())
        store.write_series(run, [SeriesInfo("a1", "v", "line", 0)],
                           [SeriesPoint("a1", "v", T0, stored)])
        store.end_run(run, "completed")
    r = get(path, "?algo=a1&start=2024-01-02T09:30&end=2024-01-02T09:31", run=1)
    assert r.status_code == 200
    assert [p["value"] for p in r.json()["series"][0]["points"]] == [sent]


def test_run_status_is_reported(tmp_path: Path, db: Path) -> None:
    assert get(db, "?algo=a1").json()["run_status"] == "completed"
    assert get(db, "?algo=a1", run=1).json()["run_status"] == "completed"  # unavailable too
    path = tmp_path / "f.db"
    with SqliteStore(path) as store:
        run = store.begin_run(meta())
        store.write_algos(run, algos())
        store.write_series(run, [SeriesInfo("a1", "v", "line", 0)], [])
        store.end_run(run, "failed")
    assert get(path, "?algo=a1", run=1).json()["run_status"] == "failed"


def test_series_of_stored_backtest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "current_code_version", lambda: "0" * 40)
    env = Env(tmp_path / "bt", monkeypatch)
    env.setup([{"ID": "EMACrossing", "data": [EMA]}], ["AAPL"], rows=600)
    database = tmp_path / "runs.db"
    assert cli.main(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                     "--out", str(env.out), "--quiet", "--db", str(database)]) == 0
    r = make_client(database).get("/api/runs/1/series", params={"algo": EMA["idname"]})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["run_status"] == "completed"
    by_name = {s["name"]: s for s in body["series"]}
    for name in ("MA20", "MA50"):
        assert by_name[name]["draw_type"] == "line"
        assert by_name[name]["points"]
        assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", p["time"])
                   for p in by_name[name]["points"])


def test_default_window_is_the_day_of_the_last_bar(db: Path) -> None:
    body = get(db, "?algo=a1").json()
    ema = next(s for s in body["series"] if s["name"] == "ema")
    assert [p["time"] for p in ema["points"]] == ["2024-01-03 09:30:00", "2024-01-03 09:31:00"]


def test_series_order_by_position(db: Path) -> None:
    body = get(db, "?algo=a1").json()
    assert [s["name"] for s in body["series"]] == ["rsi", "empty", "ema"]
    assert [s["position"] for s in body["series"]] == [0, 1, 2]


def test_other_algos_series_are_not_mixed_in(db: Path) -> None:
    body = get(db, "?algo=a2").json()
    assert [s["name"] for s in body["series"]] == ["other"]
    assert body["series"][0]["points"] == [{"time": "2024-01-03 09:30:00", "value": 7.0}]


def test_v1_run_reports_unavailable(tmp_path: Path, db: Path) -> None:
    v1 = tmp_path / "v1.db"
    make_v1_file(db, v1)
    r = get(v1, "?algo=a1", run=1)
    assert r.status_code == 200
    assert r.json() == {"algo": "a1", "run_status": "completed", "available": False, "series": []}


def test_run_without_series_reports_unavailable(db: Path) -> None:
    r = get(db, "?algo=a1", run=1)  # run 1 was stored with no series
    assert r.status_code == 200
    assert r.json() == {"algo": "a1", "run_status": "completed", "available": False, "series": []}


def test_unknown_algo_404(db: Path) -> None:
    r = get(db, "?algo=nobody")
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]


def test_unknown_algo_detail_is_bounded(db: Path) -> None:
    r = get(db, "?algo=" + "x" * 8000)
    assert r.status_code == 404
    assert r.json()["detail"] == f"no algo {'x' * MAX_ECHO}... in run 2"


def test_unknown_run_404(db: Path) -> None:
    assert get(db, "?algo=a1", run=99).status_code == 404


@pytest.mark.parametrize("params", ["", "?algo="])
def test_algo_required_422(db: Path, params: str) -> None:
    r = get(db, params)
    assert r.status_code == 422
    assert "algo" in r.json()["detail"]


@pytest.mark.parametrize("params", [
    "&start=2024-01-02T09:30:00",  # only one bound
    "&start=2024-01-02T10:00:00&end=2024-01-02T09:30:00",  # end before start
    "&start=garbage&end=2024-01-02T09:30:00",  # not a date-time
    "&start=2024-01-02T09:30:00Z&end=2024-01-02T10:00:00",  # timezone
    "&start=2024-01-02&end=2024-01-02T10:00:00",  # date only
])
def test_bad_window_422_with_the_rules_message(db: Path, params: str) -> None:
    r = get(db, "?algo=a1" + params)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"].endswith(".")


def test_window_span_is_seven_calendar_days(db: Path) -> None:
    assert get(db, "?algo=a1&start=2024-01-05T09:30:00&end=2024-01-11T16:00:00").status_code == 200
    r = get(db, "?algo=a1&start=2024-01-05T09:30:00&end=2024-01-12T16:00:00")
    assert r.status_code == 422


def test_unknown_run_or_algo_beats_a_bad_window(db: Path) -> None:
    assert get(db, "?algo=a1&start=garbage&end=garbage", run=99).status_code == 404
    assert get(db, "?algo=nobody&start=garbage&end=garbage").status_code == 404


@pytest.mark.parametrize("window", [
    "&start=2024-01-02 09:30&end=2024-01-08 09:30",  # accepted
    "&start=2024-01-02T10:00:00&end=2024-01-02T09:30:00",  # refused
])
def test_bars_and_series_routes_answer_the_same_window(db: Path, tmp_path: Path, window: str) -> None:
    directory = tmp_path / "bars-data"
    directory.mkdir()
    write_bars(directory)
    bars_db = tmp_path / "bars.db"
    run = store_run(bars_db, directory)
    bars = make_client(bars_db).get(f"/api/runs/{run}/bars?symbol=AAPL{window}")
    assert get(db, "?algo=a1" + window).status_code == bars.status_code


