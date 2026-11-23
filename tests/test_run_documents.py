"""The shared run documents equal what `runs --json` and `report --json` print."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

import MainStoinker.engine.cli_report as cli_report
import MainStoinker.engine.run_documents as run_documents
from MainStoinker.engine.cli import main
from MainStoinker.engine.run_documents import report_document, run_summary_record
from MainStoinker.engine.store import open_store_readonly
from tests.run_support import small_store_run
from tests.test_cli_report import HAND_ALGOS, HAND_EQUITY, HAND_TRIPS, make_run


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    """The realistic four-algo run (cached), its database, and the stored data directory."""
    _full, database = small_store_run(tmp_path_factory, "realistic")
    store = open_store_readonly(str(database), env={})
    try:
        entries = [(s, store.load_run(s.run_id)) for s in store.list_runs()]
    finally:
        store.close()
    assert len(entries) == 1
    return database, entries[0][0], entries[0][1]


def cli_json(capsys, *argv: str):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def test_report_document_equals_report_json(stored, capsys):
    database, _summary, run = stored
    printed = cli_json(capsys, "report", "1", "--db", str(database), "--json")
    shown = printed["run"]["data_directory"]
    assert report_document(run, data_directory=shown) == printed
    assert json.dumps(report_document(run, data_directory=shown), indent=1) == json.dumps(
        printed, indent=1)


def test_run_summary_record_equals_runs_json(stored, capsys):
    database, summary, run = stored
    printed = cli_json(capsys, "runs", "--db", str(database), "--json")
    assert printed == {"runs": [run_summary_record(summary, run)]}


def test_documents_contain_no_absolute_path(stored, tmp_path):
    _database, summary, run = stored
    absolute = summary.data.directory
    assert absolute  # the stored directory is an absolute path
    last = Path(absolute).name
    documents = [run_summary_record(summary, run),
                 report_document(run, data_directory=last)]
    text = json.dumps(documents)
    for forbidden in (absolute, absolute.replace("\\", "\\\\"), str(tmp_path),
                      str(tmp_path).replace("\\", "\\\\")):
        assert forbidden not in text
    assert documents[1]["run"]["data_directory"] == last


def test_documents_are_json_serialisable(stored):
    _database, summary, run = stored
    for document in (run_summary_record(summary, run),
                     report_document(run, data_directory="data")):
        assert json.loads(json.dumps(document)) == document


def test_run_documents_imports_no_pandas():
    source = Path(run_documents.__file__).read_text(encoding="utf-8")
    assert not [ln for ln in source.splitlines()
                if ln.startswith(("import ", "from ")) and "pandas" in ln]
    code = ("import sys, MainStoinker.engine.run_documents; "
            "sys.exit(1 if 'pandas' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code], check=False,
                          cwd=Path(__file__).resolve().parent.parent).returncode == 0


def test_cli_report_uses_the_shared_documents():
    assert cli_report.report_document is report_document
    assert cli_report.run_summary_record is run_summary_record


# ---- hand-made runs: every shape the CLI prints is the shared document ----------------


def stored_run(database: Path, run_id: int):
    store = open_store_readonly(str(database), env={})
    try:
        return next(s for s in store.list_runs() if s.run_id == run_id), store.load_run(run_id)
    finally:
        store.close()


@pytest.mark.parametrize("shape", ["no_trades", "trades", "running", "failed", "legacy"])
def test_documents_equal_cli_for_every_run_shape(tmp_path, capsys, shape):
    database = tmp_path / "runs.db"
    kwargs = {"no_trades": {}, "trades": {"trips": HAND_TRIPS, "equity": HAND_EQUITY},
              "running": {"trips": HAND_TRIPS[:1], "status": "running"},
              "failed": {"status": "failed"},
              "legacy": {"trips": HAND_TRIPS, "fills": "legacy", "config": {}}}[shape]
    make_run(database, algos=HAND_ALGOS, directory=str(tmp_path / "somebody" / "private" / "Data"),
             **kwargs)
    summary, run = stored_run(database, 1)
    printed = cli_json(capsys, "report", "1", "--db", str(database), "--json")
    assert report_document(run, data_directory=printed["run"]["data_directory"]) == printed
    assert cli_json(capsys, "runs", "--db", str(database), "--json") == {
        "runs": [run_summary_record(summary, run)]}
    # a bare folder name leaks neither the absolute path nor the user's folders
    text = json.dumps([report_document(run, data_directory="Data"),
                       run_summary_record(summary, run)])
    assert "somebody" not in text and "private" not in text


def test_none_handling_of_the_documents(tmp_path):
    database = tmp_path / "runs.db"
    make_run(database, algos=HAND_ALGOS, status="running")
    summary, run = stored_run(database, 1)
    record = run_summary_record(summary, run)
    assert (record["ended_at"], record["duration_seconds"], record["incomplete"]) == (
        None, None, True)
    document = report_document(run, data_directory="d")
    assert document["run"]["ended_at"] is None
    account = document["accounts"]["X-1"]
    assert account["win_rate"] is None and account["avg_win"] is None
    assert account["avg_loss"] is None and document["total"]["win_rate"] is None


def test_documents_are_pure(tmp_path):
    database = tmp_path / "runs.db"
    make_run(database, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    summary, run = stored_run(database, 1)
    before = (repr(summary), repr(run))
    first = report_document(run, data_directory="d")
    assert first == report_document(run, data_directory="d")
    snapshot = deepcopy(first)
    first["run"]["symbols"].append("X")  # a caller changing a document cannot reach the run
    first["accounts"].clear()
    assert report_document(run, data_directory="d") == snapshot
    run_summary_record(summary, run)
    assert (repr(summary), repr(run)) == before


def test_documents_carry_the_run_values(tmp_path):
    """Guards the equality tests: a wrong value in a document would show here."""
    database = tmp_path / "runs.db"
    make_run(database, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    summary, run = stored_run(database, 1)
    document = report_document(run, data_directory="elsewhere")
    assert document["run"]["data_directory"] == "elsewhere"
    assert document["total"]["trade_count"] == 3 and document["total"]["net"] == "9.8450"
    assert document["accounts"]["X-1"]["net"] == "9.8450"
    assert document["accounts"]["X-1"]["symbol"] == "AAPL"
    record = run_summary_record(summary, run)
    assert record["net"] == "9.8450" and record["trade_count"] == 3
