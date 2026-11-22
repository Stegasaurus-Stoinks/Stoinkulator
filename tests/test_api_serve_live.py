"""`serve` as a real process, and the host/robustness cases the in-process tests do not reach.

The one test that listens starts `python -m MainStoinker serve` on 127.0.0.1 with a free
port against a database under `tmp_path`, and always kills it (and its launcher child).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from MainStoinker.api import serve
from MainStoinker.api.errors import UNAVAILABLE
from MainStoinker.engine.store import SqliteStore
from tests.api_support import make_client

REPO = Path(__file__).resolve().parent.parent
NO_ENV: dict[str, str] = {}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "store" / "runs.db"
    with SqliteStore(path, env=NO_ENV):
        pass
    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setattr(serve, "_port_free", lambda host, port: True)
    return calls


LOOK_ALIKES = ["", "127.0.0.2", "[::1]", "localhost.", "0", "127.0.0.1 ", "::ffff:127.0.0.1"]


@pytest.mark.parametrize("host", LOOK_ALIKES)
def test_serve_refuses_look_alike_hosts(
    db: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], host: str
) -> None:
    assert serve.main(["--db", str(db), "--host", host], env=NO_ENV) == 2
    err = capsys.readouterr().err
    assert "--allow-remote" in err and len(err.strip().splitlines()) == 1
    assert started == []


def test_serve_host_localhost_is_case_insensitive(db: Path, started: list[dict[str, Any]]) -> None:
    assert serve.main(["--db", str(db), "--host", "LOCALHOST"], env=NO_ENV) == 0
    assert len(started) == 1


@pytest.mark.parametrize("port", ["abc", "8750.5", ""])
def test_serve_refuses_a_non_numeric_port(
    db: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], port: str
) -> None:
    with pytest.raises(SystemExit) as raised:  # argparse's own refusal, exit status 2
        serve.main(["--db", str(db), "--port", port], env=NO_ENV)
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1
    assert started == []


def test_database_locked_by_a_writer_is_503_without_path(tmp_path: Path) -> None:
    """A rollback-journal database held by an exclusive lock cannot be read: a fixed 503."""
    path = tmp_path / "locked" / "runs.db"
    with SqliteStore(path, env=NO_ENV):
        pass
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=DELETE")
    setup.close()
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    results: dict[str, tuple[int, float, str]] = {}

    def timed(client: TestClient, key: str, target: str, delay: float) -> None:
        time.sleep(delay)
        began = time.monotonic()
        response = client.get(target)
        results[key] = (response.status_code, time.monotonic() - began, response.text)

    try:
        # One client used as a context manager keeps one event loop for both requests, as a
        # real server has: a database call on the loop thread would delay the static page too.
        with make_client(path) as client:
            health = threading.Thread(target=timed, args=(client, "health", "/api/health", 0))
            static = threading.Thread(target=timed, args=(client, "static", "/", 0.4))
            health.start()
            static.start()
            health.join(30)
            static.join(30)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    code, waited, text = results["health"]
    assert code == 503
    assert json.loads(text) == {"detail": UNAVAILABLE}
    assert "locked" not in text
    assert 1.0 < waited < 3.0  # the 2 s busy timeout, not the store's default of 10 s
    code, waited, _ = results["static"]
    assert code == 200 and waited < 1.0  # not held up behind the locked database
    assert make_client(path).get("/api/health").status_code == 200  # and it recovers


def test_overlong_static_path_is_404(db: Path) -> None:
    response = make_client(db).get("/" + "a" * 300)
    assert response.status_code == 404
    assert make_client(db).get("/" + "/".join(["a" * 100] * 6)).status_code == 404


@pytest.mark.parametrize("name", [
    "%3C", "%3E", "%7C", "%2A", "%3F", "a%3Cb", "a%7Cb%2A", "CON", "NUL", "con.txt", "aux",
    "COM1", "a%00b", "%3A", "D%3A", "D%3Asetup.py", "..%5C..%5Csetup.py", "a.%3C", "x/../../y",
])
def test_static_names_windows_rejects_are_404(db: Path, name: str) -> None:
    response = make_client(db).get("/" + name)
    assert response.status_code == 404
    assert "Traceback" not in response.text and "Error" not in response.text


@pytest.mark.parametrize("name", ["index.html.", "index.html%20", "C%3A"])
def test_static_names_windows_normalises_are_never_a_500(db: Path, name: str) -> None:
    """Windows drops a trailing dot or space (and `C:` is the drive's current folder): such a
    name may reach the placeholder page itself, never anything else and never an error."""
    response = make_client(db).get("/" + name)
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert "Stoinkulator" in response.text


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":  # the venv launcher starts the real interpreter as a child
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    proc.wait(timeout=30)


def _fetch(port: int, method: str, target: str, headers: dict[str, str] | None = None
           ) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, target, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, \
            response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def test_real_server_is_read_only_local_and_leaves_the_port_free(tmp_path: Path) -> None:
    database = tmp_path / "live" / "runs.db"
    with SqliteStore(database, env=NO_ENV):
        pass
    before = _sha(database)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    env = {k: v for k, v in os.environ.items() if k != "STOINK_DB"}
    env["PYTHONPATH"] = str(REPO)  # import this checkout, not an editable install elsewhere
    output = tmp_path / "server-output.txt"
    with output.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "MainStoinker", "serve", "--db", str(database),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=tmp_path, env=env, stdout=sink, stderr=subprocess.STDOUT,
        )
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            tail = output.read_text(encoding="utf-8", errors="replace")[-2000:]
            pytest.fail(f"the server did not start; its output:\n{tail}")
        status, headers, body = _fetch(port, "GET", "/api/health")
        assert status == 200 and '"run_count":0' in body
        wrong = _fetch(port, "GET", "/api/health", {"Host": "evil.example"})
        assert wrong[0] == 400 and wrong[2] == "Invalid host header"
        assert _fetch(port, "GET", "/api/health", {"Origin": "http://evil.example"})[1].keys().isdisjoint(
            {"access-control-allow-origin", "access-control-allow-credentials"})
        for target in ("/docs", "/openapi.json", "/redoc", "/..%2f..%2fsetup.py",
                       "/%2e%2e/%2e%2e/setup.py", "/static/../../setup.py"):
            code, _, text = _fetch(port, "GET", target)
            assert code == 404 and "find_packages" not in text, target
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert _fetch(port, method, "/api/health")[0] == 405
    finally:
        _kill_tree(proc)
    assert _sha(database) == before
    # a read-only open of a WAL database may leave its -wal and -shm files; nothing else appears
    assert {p.name for p in database.parent.iterdir()} <= {"runs.db", "runs.db-wal", "runs.db-shm"}
    freed = socket.socket()
    try:
        freed.bind(("127.0.0.1", port))
    finally:
        freed.close()
    database.unlink()  # every connection was closed: the file can be deleted on Windows
