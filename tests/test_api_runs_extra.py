"""Gaps around `GET /api/runs`: both fill models, every run shape, stored-directory forms, leaks.

Everything lives under pytest's temporary folders; the apps never see `os.environ`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.api.errors import UNAVAILABLE
from MainStoinker.engine.cli import main
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from tests.api_support import NO_ENV, make_client, stored_run_database
from tests.run_support import small_store_run
from tests.test_api_health import copy_database
from tests.test_api_runs import T0, cli_json, make_run


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_failed_run(db: Path) -> int:
    return make_run(db, directory="failed_dir", status="failed")


def add_run_without_trades(db: Path) -> int:
    store = SqliteStore(db, env=NO_ENV)
    try:
        run_id = store.begin_run(RunMeta("backtest", "realistic", "v1", {}, DataMeta(
            "csv", "empty_dir", (), T0, T0)))
        store.end_run(run_id, "completed")
        return run_id
    finally:
        store.close()


def without_directory(document: Any) -> Any:
    document = json.loads(json.dumps(document))
    document["run"].pop("data_directory", None)
    return document


@pytest.mark.parametrize("fills", ["realistic", "legacy"])
def test_every_run_shape_matches_the_cli_for_both_fill_models(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path,
    capsys: pytest.CaptureFixture[str], fills: str,
) -> None:
    source = stored_run_database(tmp_path_factory, fills)  # type: ignore[arg-type]
    db = copy_database(source, tmp_path / "shapes.db")
    ids = [1, make_run(db, directory="a", status="running"), add_failed_run(db),
           add_run_without_trades(db), make_run(db, directory="b")]
    client = make_client(db)
    listing = cli_json(capsys, "runs", "--db", str(db))["runs"]
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": listing[::-1]}
    for run_id in ids:
        expected = cli_json(capsys, "report", str(run_id), "--db", str(db))
        got = client.get(f"/api/runs/{run_id}")
        assert got.status_code == 200, run_id
        assert without_directory(got.json()) == without_directory(expected), run_id
    states = {r["run_id"]: (r["status"], r["incomplete"]) for r in response.json()["runs"]}
    assert states[ids[1]] == ("running", True) and states[ids[2]] == ("failed", False)


def test_schema_one_database(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v1 = copy_database(stored_run_database(tmp_path_factory, "realistic"), tmp_path / "v1.db")
    con = sqlite3.connect(v1)
    con.execute("PRAGMA user_version=1")
    con.close()
    before = digest(v1)
    client = make_client(v1)
    assert client.get("/api/runs").json() == {
        "runs": cli_json(capsys, "runs", "--db", str(v1))["runs"][::-1]}
    expected = cli_json(capsys, "report", "1", "--db", str(v1))
    assert without_directory(client.get("/api/runs/1").json()) == without_directory(expected)
    assert digest(v1) == before


@pytest.mark.parametrize("stored, shown", [
    ("//server/share/data/OfflineData", "OfflineData"),
    ("\\\\server\\share\\OfflineData", "OfflineData"),
    ("\\\\server\\share", "-"),  # a UNC share is a drive root: no folder name
    ("C:/a/b/c", "c"),
    ("C:\\a\\b\\c\\", "c"),
    ("C:\\a\\b\\c\\\\", "c"),
    ("relative", "relative"),
    ("rel/sub", "sub"),
    ("./x", "x"),
    ("..", ".."),
    ("D:\\", "-"),
    ("/", "-"),
    ("", "-"),
])
def test_data_directory_forms(tmp_path: Path, stored: str, shown: str) -> None:
    db = tmp_path / "forms.db"
    run_id = make_run(db, directory=stored)
    client = make_client(db)
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["run"]["data_directory"] == shown
    assert "\\" not in body["run"]["data_directory"]


def test_inputs(tmp_path: Path) -> None:
    db = tmp_path / "inputs.db"
    make_run(db)
    client = make_client(db)
    assert client.get("/api/runs/1/").status_code in (200, 307, 404)
    for bad in ("1.5", "01x", "%20", "1%2F2", "9" * 40):
        assert client.get(f"/api/runs/{bad}").status_code in (404, 422), bad
    assert client.get("/api/runs/18446744073709551617").status_code == 404
    assert client.get("/api/runs/00001").status_code in (200, 404)
    assert client.get("/api/runs/+1").status_code in (200, 422)
    empty = tmp_path / "empty.db"
    SqliteStore(empty, env=NO_ENV).close()
    assert make_client(empty).get("/api/runs/1").status_code == 404


def test_no_leaks_in_any_body_or_header(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path
) -> None:
    full, source = small_store_run(tmp_path_factory, "realistic")
    db = copy_database(source, tmp_path / "leaks.db")
    ids = [1, make_run(db, directory=str(full.data), status="running"), add_failed_run(db),
           add_run_without_trades(db), make_run(db, directory="\\\\server\\share\\" + str(full.data))]
    secrets = {str(db), str(db.parent), str(full.data), str(full.data.parent), tempfile.gettempdir(),
               str(Path.home()), os.environ.get("USERNAME", ""), os.environ.get("USER", ""),
               db.name}
    secrets |= {s.replace("\\", "/") for s in secrets} | {s.replace("\\", "\\\\") for s in secrets}
    secrets.discard("")
    client = make_client(db)
    targets = ["/api/runs", "/api/runs/999", "/api/runs/x", *[f"/api/runs/{i}" for i in ids]]
    for target in targets:
        response = client.get(target)
        blob = (response.text + repr(dict(response.headers))).lower()
        for secret in secrets:
            assert secret.lower() not in blob, (target, secret)


def test_locked_database_gives_the_fixed_503_quickly_for_both_routes(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    make_run(path)
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=DELETE")
    setup.close()
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    results: dict[str, tuple[int, float, str]] = {}

    def timed(target: str) -> None:
        began = time.monotonic()
        response = make_client(path).get(target)
        results[target] = (response.status_code, time.monotonic() - began, response.text)

    try:
        threads = [threading.Thread(target=timed, args=(t,)) for t in ("/api/runs", "/api/runs/1")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    for target, (code, waited, text) in results.items():
        assert code == 503, target
        assert json.loads(text) == {"detail": UNAVAILABLE}
        assert waited < 4.0, target


def test_many_requests_leave_the_database_bytes_unchanged(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path
) -> None:
    db = copy_database(stored_run_database(tmp_path_factory, "realistic"), tmp_path / "ro.db")
    make_run(db, status="running")
    before = digest(db)
    client = make_client(db)
    for _ in range(15):
        for target in ("/api/runs", "/api/runs/1", "/api/runs/2", "/api/runs/3"):
            client.get(target)
    assert digest(db) == before


def test_cli_still_oldest_first_after_route_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "order.db"
    for _ in range(3):
        make_run(db)
    make_client(db).get("/api/runs")
    assert main(["runs", "--db", str(db), "--json"]) == 0
    ids = [r["run_id"] for r in json.loads(capsys.readouterr().out)["runs"]]
    assert ids == [1, 2, 3]
