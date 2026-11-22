"""`GET /api/health` and the app-wide rules: GET only, no CORS, read-only, 503 without paths.

Databases are built under pytest's temporary folders (the stored realistic run
is shared with the other store tests); the app is never given `os.environ`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import MainStoinker.api.db as api_db
from MainStoinker.api import API_VERSION
from MainStoinker.api.app import create_app
from MainStoinker.api.db import get_store
from MainStoinker.api.errors import UNAVAILABLE, NotFound
from MainStoinker.engine.store import (
    SCHEMA_VERSION,
    RunMeta,
    SqliteStore,
    Store,
    StoreError,
)
from tests.api_support import NO_ENV, make_client, stored_run_database


@pytest.fixture(scope="session")
def full_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stored_run_database(tmp_path_factory, "realistic")


def add_route(client: TestClient, path: str, endpoint: Callable[..., Any]) -> None:
    """Add a probe route ahead of the static mount, which would otherwise answer first."""
    app = client.app
    app.add_api_route(path, endpoint, methods=["GET"])  # type: ignore[attr-defined]
    app.router.routes.insert(0, app.router.routes.pop())  # type: ignore[attr-defined]


def copy_database(source: Path, target: Path) -> Path:
    """A consistent copy (the source may be in WAL mode) with the journal switched off."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    return target


def test_health_counts_runs_and_incomplete(full_db: Path, tmp_path: Path) -> None:
    body = make_client(full_db).get("/api/health")
    assert body.status_code == 200
    assert body.json() == {"api_version": API_VERSION, "schema_version": SCHEMA_VERSION,
                           "run_count": 1, "incomplete_runs": []}
    copy = copy_database(full_db, tmp_path / "copy.db")
    with SqliteStore(copy, env=NO_ENV) as store:
        store.begin_run(RunMeta(kind="backtest", fills="legacy"))  # never ended: still running
    again = make_client(copy).get("/api/health").json()
    assert again["run_count"] == 2
    assert again["incomplete_runs"] == [2]


def test_health_on_an_empty_database(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    with SqliteStore(empty, env=NO_ENV):
        pass
    assert make_client(empty).get("/api/health").json() == {
        "api_version": API_VERSION, "schema_version": SCHEMA_VERSION,
        "run_count": 0, "incomplete_runs": []}


def test_v1_database_served(full_db: Path, tmp_path: Path) -> None:
    v1 = copy_database(full_db, tmp_path / "v1.db")
    con = sqlite3.connect(v1)
    con.execute("PRAGMA user_version=1")
    con.close()
    response = make_client(v1).get("/api/health")
    assert response.status_code == 200
    assert response.json()["schema_version"] == 1
    assert response.json()["run_count"] == 1


def flat_routes(routes: Iterable[Any]) -> list[Any]:
    """Every route, looking inside the routers FastAPI keeps as nested `include_router` branches."""
    flat: list[Any] = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            flat += flat_routes(inner.routes)
        else:
            flat.append(route)
    return flat


def test_only_get_routes_exist(full_db: Path) -> None:
    app = make_client(full_db).app
    routes = flat_routes(app.routes)  # type: ignore[attr-defined]
    api = [r for r in routes if isinstance(r, APIRoute)]
    assert any(r.path == "/api/health" for r in api)
    for route in routes:
        methods = getattr(route, "methods", None)  # the static mount has none
        if methods is not None:
            assert set(methods) <= {"GET", "HEAD"}, (route.path, methods)
    paths = app.openapi()["paths"]  # type: ignore[attr-defined]
    assert paths and all(set(m) == {"get"} for m in paths.values())


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_post_put_delete_are_405(full_db: Path, method: str) -> None:
    client = make_client(full_db)
    response = getattr(client, method)("/api/health")
    assert response.status_code == 405
    assert client.request(method.upper(), "/").status_code == 405


def test_no_cors_headers(full_db: Path) -> None:
    client = make_client(full_db)
    origin = {"Origin": "http://evil.example"}
    for response in (
        client.get("/api/health", headers=origin),
        client.options("/api/health", headers={**origin, "Access-Control-Request-Method": "GET"}),
        client.get("/", headers=origin),
    ):
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


def test_api_cannot_write_database(full_db: Path, tmp_path: Path) -> None:
    copy = copy_database(full_db, tmp_path / "ro.db")
    client = make_client(copy)

    async def probe(store: Store = Depends(get_store)) -> dict[str, str]:  # noqa: B008
        assert isinstance(store, SqliteStore)
        try:
            store._db().execute("INSERT INTO runs DEFAULT VALUES")
        except sqlite3.Error as exc:
            return {"raised": type(exc).__name__, "message": str(exc)}
        return {"raised": ""}

    add_route(client, "/api/_probe", probe)

    before = copy.read_bytes()
    for _ in range(25):
        assert client.get("/api/health").status_code == 200
    answer = client.get("/api/_probe").json()
    assert answer["raised"] and "readonly" in answer["message"]
    assert copy.read_bytes() == before
    check = sqlite3.connect(copy)
    try:
        assert check.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        check.close()


def test_connection_is_closed_after_each_request(
    full_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Store] = []
    real = api_db.open_store_readonly

    def spy(spec: str, **kwargs: object) -> Store:
        store = real(spec, **kwargs)  # type: ignore[arg-type]
        opened.append(store)
        return store

    monkeypatch.setattr(api_db, "open_store_readonly", spy)
    client = make_client(full_db)
    for _ in range(3):
        assert client.get("/api/health").status_code == 200
    assert len(opened) == 3
    for store in opened:
        with pytest.raises(StoreError, match="closed"):
            store.list_runs()


SECRET = r"C:\Users\someone\secret-folder\runs.db"


@pytest.mark.parametrize("error", [
    StoreError(f"cannot open database {SECRET}: unable to open"),
    sqlite3.OperationalError(f"disk I/O error {SECRET}"),
])
def test_store_error_is_503_one_line_without_path(
    full_db: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(spec: str, **kwargs: object) -> Store:
        raise error

    monkeypatch.setattr(api_db, "open_store_readonly", broken)
    response = make_client(full_db).get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE}
    assert "secret" not in response.text and "\n" not in response.text.strip()


def test_database_removed_after_start_is_503_without_path(tmp_path: Path) -> None:
    gone = tmp_path / "vanished" / "runs.db"
    response = make_client(gone).get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE}
    assert "vanished" not in response.text
    assert not gone.exists() and not gone.parent.exists()


def test_not_found_and_validation_handlers(full_db: Path) -> None:
    client = make_client(full_db)

    async def run(run_id: int) -> dict[str, int]:
        raise NotFound(f"no run {run_id}\nin this database")

    add_route(client, "/api/_runs/{run_id}", run)

    missing = client.get("/api/_runs/7")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "no run 7 in this database"}
    invalid = client.get("/api/_runs/abc")
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid parameter: path.run_id"}
    assert client.get("/api/nothing-here").status_code == 404


def test_static_placeholder_is_served_at_root(full_db: Path) -> None:
    response = make_client(full_db).get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Stoinkulator" in response.text


def test_create_app_opens_nothing(tmp_path: Path) -> None:
    """Building the app never touches the database: a missing file is only a 503 per request."""
    missing = tmp_path / "later" / "runs.db"
    create_app(str(missing), NO_ENV)
    assert not missing.parent.exists()
