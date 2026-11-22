"""`GET /api/runs` and `GET /api/runs/{run_id}` against databases under pytest's temp folders.

The big realistic database is the one `backtest --db` stored for the four fast
golden-paper cases; the route output is compared with the CLI's own JSON and
with the golden summaries. Small hand-made databases cover the edge cases.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.api.routes_runs import last_folder_name
from MainStoinker.engine.cli import main
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from tests.api_support import NO_ENV, make_client, stored_run_database
from tests.test_api_health import copy_database
from tests.test_cli_store import FAST_CASES, GOLDEN_PAPER, full_store_run

SHARED = ("trade_count", "wins", "commission", "net", "final_equity", "max_drawdown",
          "synthetic_closes", "ignored_events", "cancelled_entries")
T0 = datetime(2024, 8, 19, 7, 55)


@pytest.fixture(scope="session")
def full_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stored_run_database(tmp_path_factory, "realistic")


def cli_json(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    capsys.readouterr()
    assert main([*args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def make_run(db: Path, *, directory: str = "", status: str = "completed") -> int:
    times = iter([datetime(2026, 3, 4, 9, 30, tzinfo=timezone.utc),
                  datetime(2026, 3, 4, 9, 31, tzinfo=timezone.utc)])
    store = SqliteStore(db, clock=lambda: next(times), env=NO_ENV)
    try:
        run_id = store.begin_run(RunMeta("backtest", "legacy", "v1", {}, DataMeta(
            "csv", directory, ("AAPL",), T0, T0 + timedelta(days=1))))
        if status != "running":
            store.end_run(run_id, status)
        return run_id
    finally:
        store.close()


def test_runs_route_equals_runs_json_reversed(
    full_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = copy_database(full_db, tmp_path / "three.db")
    make_run(db, directory="x")
    make_run(db, directory="y", status="running")
    expected = cli_json(capsys, "runs", "--db", str(db))["runs"]
    assert [r["run_id"] for r in expected] == [1, 2, 3]
    response = make_client(db).get("/api/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": expected[::-1]}
    assert [r["run_id"] for r in response.json()["runs"]] == [3, 2, 1]


def test_runs_on_an_empty_database(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    SqliteStore(empty, env=NO_ENV).close()
    assert make_client(empty).get("/api/runs").json() == {"runs": []}


@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_run_detail_equals_report_json_except_data_directory(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    _full, database = full_store_run(tmp_path_factory, "realistic")
    expected = cli_json(capsys, "report", "1", "--db", str(database))
    response = make_client(database).get("/api/runs/1")
    assert response.status_code == 200
    body = response.json()
    for document in (expected, body):
        del document["run"]["data_directory"]
    assert body == expected
    idname = FAST_CASES[case][1]["idname"]
    golden = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())["summary"]
    assert {k: body["accounts"][idname][k] for k in SHARED} == {k: golden[k] for k in SHARED}
    assert {k: body["total"][k] for k in ("trade_count", "net")} == {
        k: expected["total"][k] for k in ("trade_count", "net")}


def test_run_detail_data_directory_is_last_component(tmp_path: Path) -> None:
    db = tmp_path / "dirs.db"
    stored = {
        "windows": r"C:\Users\someone\data\OfflineData",
        "posix": "/home/someone/data/OfflineData",
        "trailing": "/home/someone/data/OfflineData/",
        "mixed": r"C:\Users\someone/data\OfflineData",
        "empty": "",
        "drive_root": "C:\\",
        "slash_root": "/",
        "bare_drive": "C:",
    }
    ids = {name: make_run(db, directory=path) for name, path in stored.items()}
    client = make_client(db)
    for name in ("windows", "posix", "trailing", "mixed"):
        run = client.get(f"/api/runs/{ids[name]}").json()["run"]
        assert run["data_directory"] == "OfflineData", name
    for name in ("empty", "drive_root", "slash_root", "bare_drive"):
        run = client.get(f"/api/runs/{ids[name]}").json()["run"]
        assert run["data_directory"] == "-", name
    assert last_folder_name("") == "-" and last_folder_name("a") == "a"


def test_unknown_run_404(full_db: Path) -> None:
    client = make_client(full_db)
    for target in ("/api/runs/2", "/api/runs/0", "/api/runs/-1",
                   "/api/runs/99999999999999999999999"):
        response = client.get(target)
        assert response.status_code == 404, target
        assert response.json() == {"detail": f"no run {target.rsplit('/', 1)[1]}"}
        assert str(full_db.parent) not in response.text


@pytest.mark.parametrize("bad", ["abc", "1.5", "1e3", " ", "0x1"])
def test_run_id_not_integer_422(full_db: Path, bad: str) -> None:
    response = make_client(full_db).get(f"/api/runs/{bad}")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: path.run_id"}


def test_incomplete_run_flagged(tmp_path: Path) -> None:
    db = tmp_path / "running.db"
    done = make_run(db)
    running = make_run(db, status="running")
    client = make_client(db)
    listing = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert listing[running]["incomplete"] is True and listing[running]["status"] == "running"
    assert listing[running]["ended_at"] is None and listing[running]["duration_seconds"] is None
    assert listing[done]["incomplete"] is False
    detail = client.get(f"/api/runs/{running}").json()["run"]
    assert detail["incomplete"] is True and detail["status"] == "running"
    assert detail["ended_at"] is None
    assert client.get(f"/api/runs/{done}").json()["run"]["incomplete"] is False


def test_times_are_utc_and_money_is_strings(full_db: Path) -> None:
    client = make_client(full_db)
    summary = client.get("/api/runs").json()["runs"][0]
    assert summary["started_at"].endswith("+00:00") and summary["ended_at"].endswith("+00:00")
    assert isinstance(summary["net"], str) and len(summary["net"].split(".")[1]) == 4
    total = client.get("/api/runs/1").json()["total"]
    assert isinstance(total["net"], str) and len(total["net"].split(".")[1]) == 4


def test_no_absolute_path_or_username_in_responses(
    tmp_path_factory: pytest.TempPathFactory, full_db: Path
) -> None:
    full, database = full_store_run(tmp_path_factory, "realistic")
    client = make_client(database)
    texts = [client.get("/api/runs").text, client.get("/api/runs/1").text,
             client.get("/api/runs/2").text]
    forbidden = {str(full.data), str(full.data.parent), str(database), str(database.parent),
                 str(full_db), str(full.data).replace("\\", "/"),
                 str(full.data).replace("\\", "\\\\"), str(database).replace("\\", "\\\\")}
    for name in (os.environ.get("USERNAME"), os.environ.get("USER"), _login()):
        if name:
            forbidden.add(name)
    for text in texts:
        for item in forbidden:
            assert item and item.lower() not in text.lower(), item
    assert client.get("/api/runs/1").json()["run"]["data_directory"] == full.data.name


def _login() -> str:
    try:
        return os.getlogin()
    except OSError:
        return ""


def test_detail_does_not_change_between_calls(full_db: Path) -> None:
    client = make_client(full_db)
    assert client.get("/api/runs/1").json() == client.get("/api/runs/1").json()
