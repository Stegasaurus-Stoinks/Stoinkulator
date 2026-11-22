"""`python -m MainStoinker serve`: dispatch, refusals and the checks made before it starts.

Nothing here binds a port: `uvicorn.run` is replaced by a recorder wherever the
server would have started. Every database is under `tmp_path`.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from MainStoinker.api import serve
from MainStoinker.engine.store import SCHEMA_VERSION, SqliteStore
from tests.test_cli_modes import Env, ema_config

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
    """Replaces `uvicorn.run`; each call is recorded and nothing listens."""
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def one_line_error(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert len(captured.err.strip().splitlines()) == 1
    return captured.err


def _runpy_program(argv: list[str], *, block_web: bool) -> str:
    block = "sys.modules['fastapi'] = sys.modules['uvicorn'] = None\n" if block_web else ""
    return ("import runpy, sys\n" + block +
            f"sys.argv = {['MainStoinker', *argv]!r}\n"
            "runpy.run_module('MainStoinker', run_name='__main__', alter_sys=True)\n")


def _run_module(argv: list[str], *, block_web: bool) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "STOINK_DB"}
    return subprocess.run([sys.executable, "-c", _runpy_program(argv, block_web=block_web)],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=300)


def test_backtest_runs_and_report_work_without_web_deps(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.setup(ema_config(), ["AAPL"], rows=900)
    database = tmp_path / "runs.db"
    done = _run_module(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                        "--out", str(env.out), "--quiet", "--db", str(database)], block_web=True)
    assert done.returncode == 0, done.stderr
    assert database.exists()
    listed = _run_module(["runs", "--db", str(database)], block_web=True)
    assert listed.returncode == 0, listed.stderr
    shown = _run_module(["report", "1", "--db", str(database)], block_web=True)
    assert shown.returncode == 0, shown.stderr


def test_serve_without_web_deps_exit_2_one_line(db: Path) -> None:
    done = _run_module(["serve", "--db", str(db)], block_web=True)
    assert done.returncode == 2
    assert done.stdout == ""
    lines = done.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("error: ")
    assert "requirements-web.txt" in lines[0]


def test_serve_dispatches_from_main_module(db: Path) -> None:
    """Through `python -m MainStoinker`: a refusal that happens before the server starts."""
    done = _run_module(["serve", "--db", str(db), "--host", "0.0.0.0"], block_web=False)
    assert done.returncode == 2
    assert "--allow-remote" in done.stderr


def test_serve_defaults(db: Path, started: list[dict[str, Any]]) -> None:
    assert serve.main(["--db", str(db)], env=NO_ENV) == 0
    assert len(started) == 1
    assert started[0]["host"] == "127.0.0.1" and started[0]["port"] == 8750
    assert started[0]["app"].state.db_spec == str(db)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_serve_allows_local_hosts(db: Path, started: list[dict[str, Any]], host: str) -> None:
    assert serve.main(["--db", str(db), "--host", host, "--port", "9001"], env=NO_ENV) == 0
    assert started[0]["host"] == host and started[0]["port"] == 9001


def test_serve_missing_db_exit_2_and_not_created(
    tmp_path: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope" / "runs.db"
    assert serve.main(["--db", str(missing)], env=NO_ENV) == 2
    assert "does not exist" in one_line_error(capsys)
    assert not missing.exists() and not missing.parent.exists()
    assert started == []


def test_serve_not_a_database_exit_2(
    tmp_path: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("this is not a database, and it is long enough to not be a header" * 5)
    before = text.read_bytes()
    assert serve.main(["--db", str(text)], env=NO_ENV) == 2
    one_line_error(capsys)
    assert text.read_bytes() == before and started == []


def test_serve_without_any_db_exit_2(
    started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert serve.main([], env=NO_ENV) == 2
    assert "STOINK_DB" in one_line_error(capsys)
    assert serve.main([], env={"STOINK_DB": "   "}) == 2
    assert started == []


def test_serve_uses_stoink_db(db: Path, started: list[dict[str, Any]]) -> None:
    assert serve.main([], env={"STOINK_DB": str(db)}) == 0
    assert started[0]["app"].state.db_spec == str(db)


def test_serve_flag_wins_over_stoink_db(
    db: Path, tmp_path: Path, started: list[dict[str, Any]]
) -> None:
    other = tmp_path / "elsewhere" / "runs.db"
    assert serve.main(["--db", str(db)], env={"STOINK_DB": str(other)}) == 0
    assert started[0]["app"].state.db_spec == str(db)
    assert not other.exists()


def test_serve_refuses_remote_host_without_flag(
    db: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    for host in ("0.0.0.0", "192.168.1.20", "::", "example.com"):
        assert serve.main(["--db", str(db), "--host", host], env=NO_ENV) == 2
        assert "--allow-remote" in one_line_error(capsys)
    assert started == []


def test_serve_allows_remote_with_flag(db: Path, started: list[dict[str, Any]]) -> None:
    assert serve.main(["--db", str(db), "--host", "0.0.0.0", "--allow-remote"], env=NO_ENV) == 0
    assert len(started) == 1 and started[0]["host"] == "0.0.0.0"


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_serve_refuses_a_bad_port(
    db: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], port: str
) -> None:
    assert serve.main(["--db", str(db), "--port", port], env=NO_ENV) == 2
    one_line_error(capsys)
    assert started == []


def test_serve_newer_schema_exit_2(
    db: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.close()
    before = db.read_bytes()
    assert serve.main(["--db", str(db)], env=NO_ENV) == 2
    assert "newer" in one_line_error(capsys)
    assert db.read_bytes() == before and started == []


def test_serve_onedrive_db_refused(
    tmp_path: Path, started: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    synced = tmp_path / "sync"
    synced.mkdir()
    database = synced / "runs.db"
    with SqliteStore(database, env=NO_ENV):  # built with an environment that does not refuse
        pass
    assert serve.main(["--db", str(database)], env={"OneDrive": str(synced)}) == 2
    assert "OneDrive" in one_line_error(capsys)
    assert started == []
