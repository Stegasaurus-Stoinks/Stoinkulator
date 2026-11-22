"""Read-only store access, probed from the outside (task D1, issue 77).

Complements the tests in test_store.py and test_cli_report.py: every write method,
unusual file states, odd paths and specs, a database that is not ours, and
`STOINK_DB` meeting the OneDrive refusal.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from MainStoinker.engine.cli import main
from MainStoinker.engine.store import (
    SCHEMA_VERSION,
    SqliteStore,
    StoreError,
    open_store_readonly,
)
from tests.test_cli_report import HAND_ALGOS, HAND_EQUITY, HAND_TRIPS, make_run
from tests.test_store import algos, build_ledger, meta, stored_run, write_all

NO_ENV: dict[str, str] = {}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


def _rows(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def test_every_write_method_raises_store_error_and_changes_nothing(db):
    with SqliteStore(db) as w:
        run_id, led = stored_run(w)
    before = _rows(db)
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        calls = [
            lambda: store.begin_run(meta()),
            lambda: store.write_algos(run_id, algos()),
            lambda: write_all(store, run_id, build_ledger()),
            lambda: store.write_results(run_id),
            lambda: store.end_run(run_id, "failed"),
            lambda: store.end_run(run_id, "completed"),
        ]
        for call in calls:
            with pytest.raises(StoreError):
                call()
        # still usable for reading afterwards
        assert len(store.list_runs()) == 1
        assert store.load_run(run_id).summary.status == "completed"
    finally:
        store.close()
    assert _rows(db) == before


def test_readonly_store_refuses_writes_after_close_with_store_error(db):
    with SqliteStore(db) as w:
        stored_run(w)
    store = open_store_readonly(str(db), env=NO_ENV)
    store.close()
    with pytest.raises(StoreError):
        store.list_runs()
    with pytest.raises(StoreError):
        store.begin_run(meta())


def test_readonly_refusals_for_unusable_files(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database " * 200)
    foreign = tmp_path / "foreign.db"  # a real SQLite file, but not a run database
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE x (a)")
    con.commit()
    con.close()
    before = {p: p.read_bytes() for p in (empty, junk, foreign)}
    for target in (folder, empty, junk, foreign, tmp_path / "missing.db"):
        with pytest.raises(StoreError):
            open_store_readonly(str(target), env=NO_ENV)
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "empty.db", "folder", "foreign.db", "junk.db"]


def test_readonly_specs_memory_and_sqlite_url(db):
    for spec in (":memory:", "sqlite:///:memory:"):
        with pytest.raises(StoreError, match="in-memory"):
            open_store_readonly(spec, env=NO_ENV)
    with pytest.raises(StoreError):
        open_store_readonly("", env=NO_ENV)
    with pytest.raises(StoreError, match="unsupported"):
        open_store_readonly("postgres://host/db", env=NO_ENV)
    with SqliteStore(db) as w:
        stored_run(w)
    for spec in (f"sqlite:///{db}", str(db)):
        store = open_store_readonly(spec, env=NO_ENV)
        try:
            assert len(store.list_runs()) == 1
        finally:
            store.close()


def test_readonly_path_with_percent_question_and_non_ascii(tmp_path):
    folder = tmp_path / "café 100% %41 +x"
    folder.mkdir()
    path = folder / "rüns 日本 #1 %20.db"
    with SqliteStore(path) as w:
        stored_run(w)
    for spec in (str(path), f"sqlite:///{path}"):
        store = open_store_readonly(spec, env=NO_ENV)
        try:
            assert len(store.list_runs()) == 1
        finally:
            store.close()


def test_readonly_accepts_older_schema_without_upgrading(db):
    with SqliteStore(db) as w:
        stored_run(w)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    assert SCHEMA_VERSION >= 1
    before = db.read_bytes()
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        assert len(store.list_runs()) == 1
    finally:
        store.close()
    assert db.read_bytes() == before


def test_cli_reads_leave_rollback_journal_database_untouched(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    before = db.read_bytes()
    mtime = db.stat().st_mtime_ns
    for command in (["runs"], ["report", "1"], ["runs", "--json"], ["report", "1", "--json"]):
        assert main([*command, "--db", str(db)], env=NO_ENV) == 0
        capsys.readouterr()
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    assert [p.name for p in db.parent.iterdir()] == [db.name]


def test_cli_reads_a_database_with_a_read_only_file_attribute(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    before = db.read_bytes()
    os.chmod(db, stat.S_IREAD)
    try:
        for command in (["runs"], ["report", "1"]):
            assert main([*command, "--db", str(db)], env=NO_ENV) == 0
            capsys.readouterr()
        assert db.read_bytes() == before
    finally:
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)


_HOLD_OPEN = (
    "import sys, time\n"
    "from MainStoinker.engine.store import SqliteStore\n"
    "s = SqliteStore(sys.argv[1])\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
    "s.close()\n"
)


def test_cli_reads_while_another_process_holds_the_database_open(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_OPEN, str(db)], stdin=subprocess.PIPE, env=env,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        main_before = db.read_bytes()
        names_before = sorted(p.name for p in db.parent.iterdir())
        sizes_before = {p.name: p.stat().st_size for p in db.parent.iterdir()
                        if p.name.endswith("-wal")}
        for command in (["runs"], ["report", "1"]):
            assert main([*command, "--db", str(db)], env=NO_ENV) == 0
            capsys.readouterr()
        assert db.read_bytes() == main_before
        assert sorted(p.name for p in db.parent.iterdir()) == names_before
        assert {p.name: p.stat().st_size for p in db.parent.iterdir()
                if p.name.endswith("-wal")} == sizes_before
    finally:
        holder.communicate("\n", timeout=30)


def test_stoink_db_runs_into_the_onedrive_refusal(tmp_path, capsys):
    root = tmp_path / "OneDrive - Corp"
    root.mkdir()
    target = root / "runs.db"
    make_run(tmp_path / "plain.db", algos=HAND_ALGOS)  # a real database, then moved under OneDrive
    (tmp_path / "plain.db").replace(target)
    env = {"STOINK_DB": str(target), "OneDrive": str(root)}
    for command in (["runs"], ["report", "1"]):
        assert main(command, env=env) == 2
        err = capsys.readouterr().err
        assert "OneDrive" in err
        assert r"%USERPROFILE%\Stoinkulator\runs.db" in err
    assert target.exists()


def test_stoink_db_with_sqlite_url_and_missing_file(tmp_path, db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    assert main(["runs"], env={"STOINK_DB": f"sqlite:///{db}"}) == 0
    capsys.readouterr()
    missing = tmp_path / "gone.db"
    assert main(["report", "1"], env={"STOINK_DB": str(missing)}) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()
