"""`GET /api/compare?runs=...`: statistics of several stored runs side by side.

One database (under pytest's temporary folders) holds three runs of the same
config: 1 realistic, 2 legacy (both with all four algos) and 3 realistic with
only the first two algos.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.api.routes_compare import last_folder_name
from MainStoinker.engine.cli import main
from MainStoinker.engine.run_documents import report_document
from MainStoinker.engine.store import RunMeta, SqliteStore, Store
from tests.api_support import NO_ENV, make_client, stored_run_database
from tests.test_api_health import copy_database
from tests.test_cli_store import full_store_run


@pytest.fixture(scope="session")
def compare_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    realistic = stored_run_database(tmp_path_factory, "realistic")
    env = full_store_run(tmp_path_factory, "realistic")[0]
    root = tmp_path_factory.mktemp("compare")
    database = copy_database(realistic, root / "runs.db")
    algos = json.loads(env.algos.read_text(encoding="utf-8"))
    subset = root / "subset.json"
    subset.write_text(json.dumps(algos[:2]), encoding="utf-8")
    for name, config, fills in (("legacy", env.algos, "legacy"), ("subset", subset, "realistic")):
        assert main(["backtest", "--algos", str(config), "--data", str(env.data),
                     "--out", str(root / name), "--quiet", "--fills", fills,
                     "--db", str(database)]) == 0
    return database


def reports(database: Path) -> dict[int, dict[str, Any]]:
    """The expected `report_document` of every stored run, built the way the report route does."""
    out: dict[int, dict[str, Any]] = {}
    with SqliteStore(database, env=NO_ENV) as store:
        for summary in store.list_runs():
            run = store.load_run(summary.run_id)
            out[summary.run_id] = json.loads(json.dumps(
                report_document(run, data_directory=last_folder_name(summary.data.directory))))
    return out


def test_compare_legacy_vs_realistic_equals_reports(compare_db: Path) -> None:
    expected = reports(compare_db)
    response = make_client(compare_db).get("/api/compare", params={"runs": "2,1"})
    assert response.status_code == 200
    body = response.json()
    assert [r["run_id"] for r in body["runs"]] == [2, 1]  # the order asked
    assert [r["fills"] for r in body["runs"]] == ["legacy", "realistic"]
    assert body["runs"] == [expected[2]["run"], expected[1]["run"]]
    assert set(body["algos"]) == set(expected[1]["accounts"]) and len(body["algos"]) == 4
    for idname, row in body["algos"].items():
        assert row == {"2": expected[2]["accounts"][idname], "1": expected[1]["accounts"][idname]}
    assert body["total"] == {"2": expected[2]["total"], "1": expected[1]["total"]}
    assert body["runs"][0]["costs"] is None and body["runs"][1]["costs"] is not None
    assert body["runs"][0]["data_directory"] == "OfflineData"


def test_algo_missing_in_one_run_is_null(compare_db: Path) -> None:
    expected = reports(compare_db)
    body = make_client(compare_db).get("/api/compare?runs=1,3").json()
    assert [r["run_id"] for r in body["runs"]] == [1, 3]
    present = set(expected[3]["accounts"])
    assert len(present) == 2 and set(body["algos"]) == set(expected[1]["accounts"])
    for idname, row in body["algos"].items():
        assert row["1"] == expected[1]["accounts"][idname]
        assert row["3"] == (expected[3]["accounts"][idname] if idname in present else None)
    assert sum(row["3"] is None for row in body["algos"].values()) == 2
    assert body["total"]["3"] == expected[3]["total"]


def test_three_runs_keep_the_order_asked(compare_db: Path) -> None:
    body = make_client(compare_db).get("/api/compare?runs=3,2,1").json()
    assert [r["run_id"] for r in body["runs"]] == [3, 2, 1]
    assert list(body["total"]) == ["3", "2", "1"]


@pytest.mark.parametrize("runs", ["1", "1,1", "1,2,1", "1,2,3,4,5,6", "", ",", "1,", ",1,2"])
def test_too_few_or_duplicate_runs_422(compare_db: Path, runs: str) -> None:
    response = make_client(compare_db).get("/api/compare", params={"runs": runs})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.runs"}


def test_missing_runs_parameter_422(compare_db: Path) -> None:
    response = make_client(compare_db).get("/api/compare")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.runs"}


def test_unknown_run_404(compare_db: Path) -> None:
    response = make_client(compare_db).get("/api/compare?runs=1,99")
    assert response.status_code == 404
    assert response.json() == {"detail": "no run 99"}


@pytest.mark.parametrize("runs", ["a,b", "1,x", "1.5,2", "1,-2", "0x1,2", "1, 2", "١,٢"])
def test_non_integer_run_422(compare_db: Path, runs: str) -> None:
    response = make_client(compare_db).get("/api/compare", params={"runs": runs})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.runs"}


@pytest.mark.parametrize(("directory", "shown"), [
    (r"C:\Users\someone\data\OfflineData", "OfflineData"),
    ("C:/Users/someone/data/OfflineData/", "OfflineData"),
    ("/home/someone/bars", "bars"),
    ("", "-"),
    ("C:\\", "-"),
    ("/", "-"),
])
def test_last_folder_name(directory: str, shown: str) -> None:
    assert last_folder_name(directory) == shown


def test_empty_or_root_directory_shows_dash_and_no_path_leaks(compare_db: Path, tmp_path: Path) -> None:
    copy = copy_database(compare_db, tmp_path / "dirs.db")
    con = sqlite3.connect(copy)
    try:
        for run_id, directory in ((1, ""), (2, "C:\\")):
            data = json.loads(con.execute("SELECT data_json FROM runs WHERE id=?", (run_id,))
                              .fetchone()[0])
            data["directory"] = directory
            con.execute("UPDATE runs SET data_json=? WHERE id=?", (json.dumps(data), run_id))
        con.commit()
    finally:
        con.close()
    response = make_client(copy).get("/api/compare?runs=1,2,3")
    body = response.json()
    assert [r["data_directory"] for r in body["runs"]] == ["-", "-", "OfflineData"]
    leaked = re.compile(r"[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp|var)/|Paul|Users", re.IGNORECASE)
    assert not leaked.search(response.text)


def test_runs_come_from_one_connection(compare_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import MainStoinker.api.db as api_db

    opened: list[Store] = []
    real = api_db.open_store_readonly

    def spy(spec: str, **kwargs: object) -> Store:
        store = real(spec, **kwargs)  # type: ignore[arg-type]
        opened.append(store)
        return store

    monkeypatch.setattr(api_db, "open_store_readonly", spy)
    assert make_client(compare_db).get("/api/compare?runs=1,2,3").status_code == 200
    assert len(opened) == 1


def test_compare_does_not_change_the_database(compare_db: Path, tmp_path: Path) -> None:
    copy = copy_database(compare_db, tmp_path / "ro.db")
    before = copy.read_bytes()
    assert make_client(copy).get("/api/compare?runs=1,2").status_code == 200
    assert copy.read_bytes() == before


def test_incomplete_and_failed_runs_can_be_compared(compare_db: Path, tmp_path: Path) -> None:
    copy = copy_database(compare_db, tmp_path / "shapes.db")
    with SqliteStore(copy, env=NO_ENV) as store:
        running = store.begin_run(RunMeta(kind="backtest", fills="legacy"))  # never ended
        failed = store.begin_run(RunMeta(kind="backtest", fills="legacy"))
        store.end_run(failed, "failed")
    client = make_client(copy)
    for first, second in ((1, running), (failed, 1), (running, failed)):
        response = client.get(f"/api/compare?runs={first},{second}")
        assert response.status_code == 200
        body = response.json()
        assert [r["run_id"] for r in body["runs"]] == [first, second]
        assert list(body["total"]) == [str(first), str(second)]
        assert all(set(row) == {str(first), str(second)} for row in body["algos"].values())
    flags = {r["run_id"]: (r["status"], r["incomplete"])
             for r in client.get(f"/api/compare?runs={running},{failed}").json()["runs"]}
    assert flags == {running: ("running", True), failed: ("failed", False)}


def test_schema_1_database_can_be_compared(compare_db: Path, tmp_path: Path) -> None:
    v1 = copy_database(compare_db, tmp_path / "v1.db")
    con = sqlite3.connect(v1)
    con.execute("PRAGMA user_version=1")
    con.close()
    response = make_client(v1).get("/api/compare?runs=1,2")
    assert response.status_code == 200
    assert response.json() == make_client(compare_db).get("/api/compare?runs=1,2").json()


@pytest.mark.parametrize("query", ["runs=1&runs=2", "runs=1,2&runs=3", "runs=%2B1,2", "runs=1,2,%20",
                                   "runs=%EF%BC%91,2"])
def test_repeated_or_signed_runs_parameter_422(compare_db: Path, query: str) -> None:
    response = make_client(compare_db).get("/api/compare?" + query)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.runs"}


def test_leading_zeros_name_the_same_run(compare_db: Path) -> None:
    client = make_client(compare_db)
    assert client.get("/api/compare?runs=01,2").json() == client.get("/api/compare?runs=1,2").json()
    assert client.get("/api/compare?runs=01,1").status_code == 422  # still a duplicate


def test_repeated_requests_in_one_process_agree(compare_db: Path) -> None:
    client = make_client(compare_db)
    first = client.get("/api/compare?runs=3,1").json()
    assert client.get("/api/compare?runs=1,99").status_code == 404  # an error in between
    assert client.get("/api/compare?runs=3,1").json() == first
    assert make_client(compare_db).get("/api/compare?runs=3,1").json() == first
