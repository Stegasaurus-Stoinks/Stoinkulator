"""The shared run documents equal what `runs --json` and `report --json` print."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import MainStoinker.engine.cli_report as cli_report
import MainStoinker.engine.run_documents as run_documents
from MainStoinker.engine.cli import main
from MainStoinker.engine.run_documents import report_document, run_summary_record
from MainStoinker.engine.store import open_store_readonly
from tests.test_cli_store import full_store_run


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    """The realistic four-algo run (cached), its database, and the stored data directory."""
    _full, database = full_store_run(tmp_path_factory, "realistic")
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
