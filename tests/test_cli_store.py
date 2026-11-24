"""`backtest --db`: each run is stored, and nothing changes without it.

Every database lives under pytest's `tmp_path` (outside OneDrive) and is
opened through `open_store`; no test may create a database in the repository
(`test_no_database_file_in_the_repository` checks that at the end of the
module). The OneDrive refusal is tested by injecting the environment, not by
using the real one.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import MainStoinker.engine.cli as cli
import MainStoinker.engine.store as store_module
from MainStoinker.engine.cli import CliError, main
from MainStoinker.engine.feed import CsvBarFeed
from MainStoinker.engine.stats import account_summary, round_trip_record
from MainStoinker.engine.store import StoreError, expand_equity, open_store
from tests.run_support import FAST_CASES, ROWS, full_store_run
from tests.test_cli_modes import EMA, EMA_MSFT, FUNC_MSFT, GOLDEN, Env, ema_config
from tests.test_paper_goldens import equity_series_bytes

REPO = Path(__file__).resolve().parent.parent
VERSION = "0123456789abcdef0123456789abcdef01234567"
MULTI = [
    {"ID": "FunctionTest", "data": [FUNC_MSFT]},
    {"ID": "EMACrossing", "data": [EMA, {**EMA_MSFT, "mode": "paper"}]},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Never run git for a test that did not ask for the real thing.
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "store" / "runs.db"


def run(env: Env, *extra: str, environ: dict | None = None) -> int:
    return main(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                 "--out", str(env.out), "--quiet", *extra], env=environ)


def read(db: Path):
    return open_store(str(db), env=os.environ)


def one_line_error(capsys) -> str:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and len(captured.err.strip().splitlines()) == 1
    return captured.err


def stored(db: Path, run_id: int = 1):
    store = read(db)
    try:
        return store.load_run(run_id)
    finally:
        store.close()


def files_below(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# ---- the lifecycle ----------------------------------------------------------------


def test_db_option_stores_completed_run(env, db, capsys):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=600)
    assert run(env, "--fills", "realistic", "--capital", "5000", "--commission-min", "2",
               "--db", str(db)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {db.resolve()}"
    store = read(db)
    try:
        runs = store.list_runs()
        data = store.load_run(1)
    finally:
        store.close()
    assert [r.run_id for r in runs] == [1]
    summary = data.summary
    assert (summary.status, summary.kind, summary.fills) == ("completed", "backtest", "realistic")
    assert summary.code_version == VERSION
    assert summary.ended_at is not None and summary.ended_at >= summary.started_at
    feed = CsvBarFeed(env.data, ["MSFT", "AAPL"], 2)
    stamps = [stamp for stamp, _ in feed]
    assert (summary.data.source, summary.data.directory) == ("csv", str(env.data.resolve()))
    assert summary.data.symbols == ("MSFT", "AAPL")
    assert summary.data.first_bar == stamps[0].to_pydatetime()
    assert summary.data.last_bar == stamps[-1].to_pydatetime()
    # the validated entries, the costs and capital that were in force
    assert data.config["entries"] == MULTI
    assert data.config["warmup_days"] == 2 and data.config["capital"] == "5000.0000"
    assert data.config["costs"] == {"commission_per_share": "0.0050",
                                    "commission_minimum": "2.0000",
                                    "slippage_per_share": "0.0000"}
    assert data.order_events and data.fills and data.round_trips and data.equity
    assert {a.idname for a in data.algos} == {"MSFT-functest", "AAPL-10/30EMA", "MSFT-10/30EMA"}


def test_algos_stored_with_mode_params_and_capital(env, db):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=ROWS)
    assert run(env, "--fills", "realistic", "--capital", "2500.5", "--db", str(db)) == 0
    algos = stored(db).algos
    assert [(a.idname, a.algo_id, a.ticker, a.mode, a.capital) for a in algos] == [
        ("MSFT-functest", "FunctionTest", "MSFT", "backtest", Decimal("2500.5")),
        ("AAPL-10/30EMA", "EMACrossing", "AAPL", "backtest", Decimal("2500.5")),
        ("MSFT-10/30EMA", "EMACrossing", "MSFT", "paper", Decimal("2500.5")),
    ]
    assert [dict(a.params) for a in algos] == [FUNC_MSFT, EMA, {**EMA_MSFT, "mode": "paper"}]


def test_run_exists_while_the_engine_runs_and_results_are_written_once(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    seen: dict = {}
    real_run = cli.Engine.run_backtest

    def spy_run(self):
        other = read(db)  # a second connection, as the dashboard would have
        try:
            seen["runs"] = other.list_runs()
            seen["algos"] = other.load_run(1).algos
            seen["fills"] = other.load_run(1).fills
        finally:
            other.close()
        real_run(self)

    calls: list[int] = []
    real_write = store_module.SqliteStore.write_results

    def counting(self, run_id, **kwargs):
        calls.append(len(kwargs["fills"]))
        return real_write(self, run_id, **kwargs)

    monkeypatch.setattr(cli.Engine, "run_backtest", spy_run)
    monkeypatch.setattr(store_module.SqliteStore, "write_results", counting)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 0
    assert [r.status for r in seen["runs"]] == ["running"]
    assert [a.idname for a in seen["algos"]] == [EMA["idname"]] and seen["fills"] == ()
    assert len(calls) == 1 and calls[0] > 0
    assert stored(db).summary.status == "completed"


def test_stored_trades_equal_ledger_json(env, db):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=600)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 0
    doc = env.ledger()
    data = stored(db)
    total_trips = []
    total_capital = Decimal(0)
    total_ignored = {"close": 0, "modify_stop": 0, "take_profit": 0}
    series_ok = 0
    for algo in data.algos:
        account = doc["accounts"][algo.idname]
        trips = [t for t in data.round_trips if t.account == algo.idname]
        assert [round_trip_record(t) for t in trips] == account["round_trips"]
        series = data.equity_series(algo.idname)
        ignored = {k: sum(1 for e in data.ignored_events if e.account == algo.idname
                          and e.kind == k) for k in total_ignored}
        summary = account_summary(
            trips, series[-1].equity, algo.capital, equity_series=[p.equity for p in series],
            ignored_events=ignored,
            # not stored: the broker's count of entries it cancelled before filling
            cancelled_entries=account["summary"]["cancelled_entries"])
        assert summary == account["summary"]
        total_trips += trips
        total_capital += algo.capital
        for kind, count in ignored.items():
            total_ignored[kind] += count
        series_ok += len(trips) > 0
    assert series_ok >= 2 and len(total_trips) == doc["summary"]["trade_count"] > 0
    total_series = data.equity_series(None)
    assert account_summary(
        total_trips, total_series[-1].equity, total_capital,
        equity_series=[p.equity for p in total_series], ignored_events=total_ignored,
        cancelled_entries=doc["summary"]["cancelled_entries"]) == doc["summary"]
    # and the golden-shaped trades.json agrees with the stored entry fills
    trades = env.trades()
    for algo in data.algos:
        entries = [f for f in data.fills if f.account == algo.idname and f.reason == "entry"]
        assert [str(f.time) for f in entries] == [r["openTime"] for r in trades[algo.idname]]
        assert [float(f.price) for f in entries] == [r["openPrice"] for r in trades[algo.idname]]


def test_legacy_run_stores_cost_free_fills_marked_legacy(env, db):
    env.setup([{"ID": "EMACrossing", "data": [EMA]}], ["AAPL"], rows=ROWS)
    assert run(env, "--db", str(db)) == 0
    data = stored(db)
    assert data.summary.fills == "legacy" and data.summary.status == "completed"
    assert data.config["costs"] is None and data.config["capital"] == "10000.0000"
    assert [a.capital for a in data.algos] == [Decimal("10000")]
    trades = env.trades()[EMA["idname"]]
    assert trades and not (env.out / "ledger.json").exists()
    entries = [f for f in data.fills if f.reason == "entry"]
    assert len(entries) == len(trades)
    assert all(f.commission == 0 for f in data.fills)
    assert data.round_trips and all(t.commission == 0 and t.net == t.gross
                                    for t in data.round_trips)
    assert data.equity_series(EMA["idname"]) and data.equity_series(None)


def test_trades_json_still_written_with_db(env, db):
    """With --db the output files are byte-identical to the same run without it."""
    env.setup(MULTI, ["AAPL", "MSFT"], rows=ROWS)
    for fills in ("legacy", "realistic"):
        assert run(env, "--fills", fills) == 0
        # (tradey.txt is a legacy side file the algos append to; it is not an output)
        plain = {p.name: p.read_bytes() for p in env.out.iterdir() if p.suffix == ".json"}
        assert "trades.json" in plain
        assert run(env, "--fills", fills, "--db", str(db)) == 0
        assert {p.name: p.read_bytes() for p in env.out.iterdir() if p.suffix == ".json"} == plain
        assert ("ledger.json" in plain) == (fills == "realistic")


@pytest.mark.slow  # full data again; the fast suite covers the same behaviour (see below)
def test_legacy_trades_json_with_db_equals_the_golden(env, db):
    """Covered in the fast suite by `test_stored_legacy_run_matches_the_legacy_golden` (a legacy
    run with --db, trades.json equal to the golden, no ledger.json), by
    `test_trades_json_still_written_with_db` (the bytes do not depend on --db) and by
    `test_legacy_fills_output_byte_identical_to_golden` (the golden's byte format)."""
    env.setup(ema_config(mode="paper"), ["AAPL"], rows=None)
    assert run(env, "--db", str(db)) == 0
    golden = json.loads((GOLDEN / "emacrossing_aapl.json").read_text())
    assert env.trades_bytes() == (json.dumps({EMA["idname"]: golden}, indent=1) + "\n").encode()
    assert stored(db).summary.fills == "legacy"
    assert not (env.out / "ledger.json").exists()


def test_two_runs_one_database_are_isolated(env, db, capsys):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=ROWS)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 0
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env, "--db", str(db)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 2 in {db.resolve()}"
    first, second = stored(db, 1), stored(db, 2)
    assert (first.summary.run_id, second.summary.run_id) == (1, 2)
    assert (first.summary.fills, second.summary.fills) == ("realistic", "legacy")
    assert len(first.algos) == 3 and len(second.algos) == 1
    assert {f.account for f in second.fills} <= {EMA["idname"]}
    assert {f.account for f in first.fills} <= {a.idname for a in first.algos}
    assert first.fills and second.fills
    store = read(db)
    try:
        assert [r.status for r in store.list_runs()] == ["completed", "completed"]
    finally:
        store.close()
    # every table holds each run's own rows only
    conn = sqlite3.connect(db)
    try:
        for table in ("order_events", "fills", "trades", "equity"):
            counts = dict(conn.execute(f"SELECT run_id, COUNT(*) FROM {table} GROUP BY run_id"))
            assert set(counts) <= {1, 2}
        fills = dict(conn.execute("SELECT run_id, COUNT(*) FROM fills GROUP BY run_id"))
        assert fills == {1: len(first.fills), 2: len(second.fills)}
    finally:
        conn.close()


# ---- without --db -----------------------------------------------------------------


def test_no_db_option_creates_no_db_file(env, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("the store must not be opened or the version computed without --db")

    monkeypatch.setattr(cli, "open_store", forbidden)
    monkeypatch.setattr(cli, "current_code_version", forbidden)
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    for fills in ("legacy", "realistic"):
        assert run(env, "--fills", fills) == 0
    assert "stored run" not in capsys.readouterr().out
    found = [p for p in tmp_path.rglob("*") if any(s in p.name.lower() for s in (".db", "sqlite"))]
    assert found == []
    assert files_below(env.out) >= {"trades.json", "ledger.json"}
    assert list(env.start.iterdir()) == []


def test_output_lines_unchanged_without_db(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("replayed ") and lines[-1] == f"wrote {env.out.resolve() / 'trades.json'}"
    assert len(lines) == 3


# ---- store errors -----------------------------------------------------------------


def test_onedrive_db_path_refused_exit_2(env, tmp_path, capsys):
    cloud = tmp_path / "sync"
    target = cloud / "runs.db"
    env.setup(ema_config(), ["AAPL"], rows=100)
    # judged by the injected environment ...
    assert run(env, "--db", str(target), environ={"OneDrive": str(cloud)}) == 2
    err = one_line_error(capsys)
    assert "OneDrive" in err
    assert not cloud.exists() and not env.out.exists()
    # ... and by a folder name, with no environment variable at all
    named = tmp_path / "OneDrive - Some Company" / "runs.db"
    assert run(env, "--fills", "realistic", "--db", str(named), environ={}) == 2
    assert "OneDrive" in one_line_error(capsys)
    assert not named.parent.exists() and not env.out.exists()
    # the same path is fine for an environment that does not name it
    assert run(env, "--db", str(target), environ={"OneDrive": str(tmp_path / "elsewhere")}) == 0
    assert target.exists()


@pytest.mark.parametrize("spec", ["postgres://user@host/db", "mysql://x", "sqlite+x://y", "  ", ""])
def test_unsupported_or_empty_database_spec_exit_2(env, capsys, spec):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, "--db", spec) == 2
    one_line_error(capsys)
    assert not env.out.exists()


def test_not_a_database_exit_2_and_file_untouched(env, db, capsys):
    db.parent.mkdir()
    junk = b"this is not a database file, " * 20
    db.write_bytes(junk)
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, "--db", str(db)) == 2
    one_line_error(capsys)
    assert not env.out.exists()
    assert db.read_bytes() == junk
    db.unlink()  # the failed open released the file


def test_newer_schema_exit_2(env, db, capsys):
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, "--db", str(db)) == 2
    assert "newer" in one_line_error(capsys)
    assert not env.out.exists()
    db.unlink()


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_refused_config_with_db_creates_no_database(env, db, capsys, fills):
    env.setup([{"ID": "EMACrossing", "data": [EMA, {**EMA_MSFT, "mode": "live"}]}],
              ["AAPL", "MSFT"], rows=60)
    assert run(env, "--fills", fills, "--db", str(db)) == 2
    assert "mode 'live' is not available yet" in one_line_error(capsys)
    assert not db.exists() and not db.parent.exists() and not env.out.exists()


@pytest.mark.parametrize("extra", [["--warmup-days", "-1"], ["--capital", "5"]])
def test_bad_options_with_db_create_no_database(env, db, capsys, extra):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, *extra, "--db", str(db)) == 2
    one_line_error(capsys)
    assert not db.parent.exists()


def test_missing_data_with_db_creates_no_database(env, db, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    shutil.rmtree(env.data)
    assert run(env, "--db", str(db)) == 2
    one_line_error(capsys)
    assert not db.parent.exists()


# ---- failures ---------------------------------------------------------------------


def boom(self):
    raise RuntimeError("engine exploded")


def test_failed_run_marked_failed(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)
    with pytest.raises(RuntimeError, match="engine exploded"):
        run(env, "--fills", "realistic", "--db", str(db))
    data = stored(db)
    assert data.summary.status == "failed" and data.summary.ended_at is not None
    assert data.fills == () and data.round_trips == ()
    assert [a.idname for a in data.algos] == [EMA["idname"]]
    assert not (env.out / "trades.json").exists()
    # the store was closed: the file can be deleted (Windows refuses otherwise)
    db.unlink()
    assert not list(db.parent.glob("runs.db*"))


def test_end_run_failing_does_not_hide_the_original_error(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)

    def broken(self, run_id, status):
        raise StoreError("end_run is broken")

    monkeypatch.setattr(store_module.SqliteStore, "end_run", broken)
    with pytest.raises(RuntimeError, match="engine exploded") as info:
        run(env, "--db", str(db))
    assert not isinstance(info.value, StoreError)
    monkeypatch.undo()
    store = read(db)
    try:
        assert [r.status for r in store.list_runs()] == ["running"]  # honest: never ended
    finally:
        store.close()


def test_end_run_failing_with_a_bare_exception_does_not_hide_it_either(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)

    def broken(self, run_id, status):
        raise ValueError("something else entirely")

    monkeypatch.setattr(store_module.SqliteStore, "end_run", broken)
    with pytest.raises(RuntimeError, match="engine exploded"):
        run(env, "--db", str(db))


def test_store_closed_even_when_end_run_fails(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)
    monkeypatch.setattr(store_module.SqliteStore, "end_run",
                        lambda self, run_id, status: (_ for _ in ()).throw(StoreError("x")))
    with pytest.raises(RuntimeError):
        run(env, "--db", str(db))
    db.unlink()
    assert not list(db.parent.glob("runs.db*"))


def test_keyboard_interrupt_marks_the_run_failed_and_propagates(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)

    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.Engine, "run_backtest", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(env, "--db", str(db))
    assert stored(db).summary.status == "failed"


def test_output_failure_after_results_marks_the_run_failed(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)

    def refuse(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_json_atomic", refuse)
    with pytest.raises(OSError, match="disk full"):
        run(env, "--fills", "realistic", "--db", str(db))
    assert stored(db).summary.status == "failed"
    db.unlink()


def test_store_closed_on_success(env, db):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env, "--db", str(db)) == 0
    db.unlink()  # a connection left open would make this fail on Windows
    assert not list(db.parent.glob("runs.db*"))


def test_db_in_a_new_folder_and_sqlite_url_form(env, tmp_path, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    target = tmp_path / "a" / "b" / "runs.db"
    assert run(env, "--db", "sqlite:///" + str(target)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {target.resolve()}"
    assert stored(target).summary.status == "completed"


def test_backtest_uses_stoink_db(env, tmp_path, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    target = tmp_path / "named" / "env.db"
    assert run(env, environ={"STOINK_DB": str(target)}) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {target.resolve()}"
    assert stored(target).summary.status == "completed"
    # the flag wins, and an empty value is unset
    other = tmp_path / "flag.db"
    assert run(env, "--db", str(other), environ={"STOINK_DB": str(target)}) == 0
    assert stored(other).summary.run_id == 1 and len(read_runs(target)) == 1
    capsys.readouterr()
    assert run(env, environ={"STOINK_DB": ""}) == 0
    assert "stored run" not in capsys.readouterr().out


def test_whitespace_stoink_db_is_unset(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env, environ={"STOINK_DB": "   "}) == 0
    assert "stored run" not in capsys.readouterr().out


def test_relative_db_path_is_relative_to_the_start_directory(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env, "--db", "rel.db") == 0
    assert (env.start / "rel.db").exists() and not (env.out / "rel.db").exists()
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {(env.start / 'rel.db').resolve()}"


# ---- code version -----------------------------------------------------------------


# (the code-version computation itself is tested in test_codeversion.py)


def test_code_version_is_stored_and_computed_once_per_run(env, db, monkeypatch):
    calls: list[int] = []

    def version() -> str:
        calls.append(1)
        return "abc-dirty:123456789abc"

    monkeypatch.setattr(cli, "current_code_version", version)
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    assert run(env, "--db", str(db)) == 0
    assert calls == [1] and stored(db).summary.code_version == "abc-dirty:123456789abc"
    # an injected callable wins over the default
    args = cli.build_parser().parse_args(
        ["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
         "--quiet", "--db", str(db)])
    assert cli.run_backtest_command(args, code_version=lambda: "injected") == 0
    assert stored(db, 2).summary.code_version == "injected" and calls == [1]


# Only a git command fails the check. Something else may legitimately start a process during an
# import: on Windows `platform` falls back to the `ver` shell command when its WMI query fails
# (seen on a machine under heavy load), and numpy/pandas import `platform`.
GIT_GUARD = ("import subprocess\n"
             "def guard(real):\n"
             "    def wrapper(*a, **k):\n"
             "        args = a[0] if a else k.get('args')\n"
             "        if 'git' in str(args).lower():\n"
             "            raise SystemExit('git was run at import time: %r' % (args,))\n"
             "        return real(*a, **k)\n"
             "    return wrapper\n"
             "subprocess.run = guard(subprocess.run)\n"
             "subprocess.Popen = guard(subprocess.Popen)\n")


def test_importing_cli_does_not_run_git():
    done = subprocess.run([sys.executable, "-c", GIT_GUARD + "import MainStoinker.engine.cli\n"],
                          cwd=REPO, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr


def test_the_git_guard_of_that_check_still_catches_git():
    done = subprocess.run([sys.executable, "-c", GIT_GUARD + "subprocess.run(['git', '--version'])\n"],
                          cwd=REPO, capture_output=True, text=True, timeout=300)
    assert done.returncode != 0 and "git was run at import time" in done.stderr


# ---- verification additions: goldens through the store, processes, output failures ----

GOLDEN_PAPER = Path(__file__).parent / "golden_paper"
LEGACY_GOLDEN = Path(__file__).parent / "golden"
# FAST_CASES and full_store_run (the full-data stored runs, built once per session) are in
# tests/run_support.py and re-exported here for the tests that import them from this module.

@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_stored_realistic_run_equals_golden_paper(tmp_path_factory, case):
    """One database run of four algos: each algo's stored rows reproduce its golden
    (trades, summary, and the equity series expanded to the bar grid by hash)."""
    full, database = full_store_run(tmp_path_factory, "realistic")
    _algo, cfg, symbol = FAST_CASES[case]
    golden = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())
    data = stored(database)
    assert len(data.algos) == 4 and data.summary.status == "completed"
    idname = cfg["idname"]
    trips = [t for t in data.round_trips if t.account == idname]
    assert [round_trip_record(t) for t in trips] == golden["trades"]
    stamps = [stamp.to_pydatetime() for stamp, _ in CsvBarFeed(full.data, [symbol], 2)]
    grid = expand_equity(data.equity_series(idname), stamps)
    assert len(grid) == golden["equity_points"]
    assert hashlib.sha256(equity_series_bytes(grid)).hexdigest() == golden["equity_sha256"]
    ignored = {k: sum(1 for e in data.ignored_events if e.account == idname and e.kind == k)
               for k in ("close", "modify_stop", "take_profit")}
    assert account_summary(
        trips, grid[-1].equity, Decimal(golden["capital"]), equity_series=[p.equity for p in grid],
        ignored_events=ignored, cancelled_entries=golden["summary"]["cancelled_entries"],
    ) == golden["summary"]
    # ledger.json of the same run agrees with the golden's account
    assert full.ledger()["accounts"][idname]["summary"] == golden["summary"]


@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_stored_legacy_run_matches_the_legacy_golden(tmp_path_factory, case):
    """Legacy fills with --db: trades.json is the legacy golden byte for byte, and the stored
    round trips are the golden's closed trades, without commission."""
    full, database = full_store_run(tmp_path_factory, "legacy")
    idname = FAST_CASES[case][1]["idname"]
    golden = json.loads((LEGACY_GOLDEN / f"{case}.json").read_text())
    assert full.trades()[idname] == golden
    assert not (full.out / "ledger.json").exists()
    data = stored(database)
    assert data.summary.fills == "legacy"
    closed = [g for g in golden if g["status"] == "Closed"]
    trips = [t for t in data.round_trips if t.account == idname]
    assert [(str(t.entry_time), float(t.entry_price), str(t.exit_time), float(t.exit_price),
             t.side, int(t.quantity)) for t in trips if not t.synthetic] == [
        (g["openTime"], g["openPrice"], g["closeTime"], g["closePrice"],
         "long" if g["direction"] == 1 else "short", g["volume"]) for g in closed]
    assert all(t.commission == 0 and t.net == t.gross for t in trips)


def test_a_failed_output_write_after_the_run_is_failed_not_completed(env, db):
    """`trades.json` cannot be replaced (it is a directory): the run row says failed."""
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    (env.out / "trades.json").mkdir(parents=True)
    with pytest.raises(OSError):
        run(env, "--db", str(db))
    data = stored(db)
    assert data.summary.status == "failed" and data.summary.ended_at is not None
    db.unlink()  # closed


def test_out_pointing_at_a_file_is_an_error_and_no_run_is_stored(env, db, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    env.out.write_text("not a directory")
    assert run(env, "--db", str(db)) == 2
    one_line_error(capsys)
    assert env.out.read_text() == "not a directory"
    if db.exists():
        store = read(db)
        try:
            assert store.list_runs() == ()
        finally:
            store.close()
        db.unlink()


def test_begin_run_failure_is_one_line_exit_3_and_closes_the_store(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)

    def broken(self, meta):
        raise StoreError("attempt to write a readonly database")

    monkeypatch.setattr(store_module.SqliteStore, "begin_run", broken)
    assert run(env, "--db", str(db)) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err == ("error: could not record the run: "
                            "attempt to write a readonly database\n")
    assert not (env.out / "trades.json").exists()  # the engine never ran
    db.unlink()  # a connection left open would make this fail on Windows
    assert not list(db.parent.glob("runs.db*"))


def test_write_algos_failure_is_a_record_failure_exit_3(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)

    def broken(self, run_id, algos):
        raise StoreError("disk full")

    monkeypatch.setattr(store_module.SqliteStore, "write_algos", broken)
    assert run(env, "--db", str(db)) == 3
    assert capsys.readouterr().err == "error: could not record the run: disk full\n"
    assert [r.status for r in read_runs(db)] == ["failed"]
    db.unlink()


@pytest.mark.parametrize("method", ["write_results", "end_run"])
def test_storage_failure_after_the_run_keeps_the_output_files(env, db, monkeypatch, capsys,
                                                              method):
    """The files are written before the results are stored, so a storage failure
    (exit 3, one line) cannot discard a finished run's trades.json / ledger.json."""
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    seen: dict = {}
    real = getattr(store_module.SqliteStore, method)

    def broken(self, *a, **kw):
        if method == "end_run" and a[1] == "failed":
            return real(self, *a, **kw)  # the honest marker still goes in
        seen["files"] = sorted(p.name for p in env.out.glob("*.json"))
        raise StoreError("database or disk is full")

    monkeypatch.setattr(store_module.SqliteStore, method, broken)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 3
    assert capsys.readouterr().err == (
        "error: run finished but could not be stored: database or disk is full\n")
    assert seen["files"] == ["ledger.json", "trades.json"]  # already there when storing began
    assert sorted(p.name for p in env.out.glob("*.json")) == ["ledger.json", "trades.json"]
    assert env.trades()[EMA["idname"]]
    assert [r.status for r in read_runs(db)] == ["failed"]
    db.unlink()  # the store was closed


def test_end_run_completed_failing_leaves_a_running_row_and_the_files(env, db, monkeypatch,
                                                                       capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)

    def broken(self, run_id, status):
        raise StoreError("locked")

    monkeypatch.setattr(store_module.SqliteStore, "end_run", broken)
    assert run(env, "--db", str(db)) == 3
    assert capsys.readouterr().err == "error: run finished but could not be stored: locked\n"
    assert (env.out / "trades.json").exists()
    monkeypatch.undo()
    assert [r.status for r in read_runs(db)] == ["running"]  # honest: never ended
    db.unlink()


def test_output_files_are_written_before_results_and_before_the_run_is_completed(
        env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    order: list[str] = []
    real_write, real_results = cli.write_json_atomic, store_module.SqliteStore.write_results
    real_end = store_module.SqliteStore.end_run

    def write(path, payload):
        order.append("file:" + path.name)
        return real_write(path, payload)

    def results(self, run_id, **kw):
        order.append("results")
        return real_results(self, run_id, **kw)

    def end(self, run_id, status):
        order.append("end:" + status)
        return real_end(self, run_id, status)

    monkeypatch.setattr(cli, "write_json_atomic", write)
    monkeypatch.setattr(store_module.SqliteStore, "write_results", results)
    monkeypatch.setattr(store_module.SqliteStore, "end_run", end)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 0
    assert order == ["file:trades.json", "file:ledger.json", "results", "end:completed"]


def test_exit_codes_are_documented(capsys):
    assert "0 success; 2 usage or input error" in cli.__doc__ and "3 the store failed" in cli.__doc__
    with pytest.raises(SystemExit):
        main(["backtest", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "exit codes: 0 success; 2 usage or input error" in text and "3 the database failed" in text


def test_a_non_store_error_after_the_store_opened_still_propagates(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=100)
    monkeypatch.setattr(store_module.SqliteStore, "begin_run",
                        lambda self, meta: (_ for _ in ()).throw(ValueError("odd")))
    with pytest.raises(ValueError, match="odd"):
        run(env, "--db", str(db))
    db.unlink()


# ---- input errors store nothing ---------------------------------------------------


def test_unknown_algo_id_with_db_exits_2_and_stores_nothing(env, db, capsys):
    env.setup([{"ID": "NoSuchAlgo", "data": [EMA]}], ["AAPL"], rows=100)
    assert run(env, "--db", str(db)) == 2
    assert "unknown algo 'NoSuchAlgo'" in one_line_error(capsys)
    assert not db.exists() and not db.parent.exists() and not env.out.exists()


def test_algo_that_fails_to_import_with_db_exits_2_and_stores_nothing(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    real = cli.importlib.import_module

    def broken(name, *a, **kw):
        if name.endswith("EMACrossing_Algo"):
            raise ImportError("cannot import name 'x'")
        return real(name, *a, **kw)

    monkeypatch.setattr(cli.importlib, "import_module", broken)
    assert run(env, "--db", str(db)) == 2
    err = one_line_error(capsys)
    assert "EMACrossing" in err and "ImportError" in err
    assert not db.parent.exists() and not env.out.exists()


def test_algo_module_without_an_algo_class_exits_2(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    monkeypatch.setattr(cli.importlib, "import_module", lambda name, *a, **k: object())
    assert run(env, "--db", str(db)) == 2
    assert "has no class 'Algo'" in one_line_error(capsys)
    assert not db.parent.exists() and not env.out.exists()


@pytest.mark.parametrize("spec", [":memory:", "sqlite:///:memory:"])
def test_db_memory_is_refused_on_the_command_line(env, capsys, spec):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, "--db", spec) == 2
    assert ":memory:" in one_line_error(capsys)
    assert not env.out.exists()


def test_open_store_in_memory_is_still_allowed_in_the_library():
    store = open_store(":memory:", env={})
    try:
        assert store.list_runs() == ()
    finally:
        store.close()


def _spawn(env: Env, db: Path, out: Path, *extra: str, code: str | None = None):
    argv = ["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(out),
            "--quiet", "--db", str(db), *extra]
    program = code or "import sys; from MainStoinker.engine.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.Popen([sys.executable, "-c", program, *argv], cwd=env.start,
                            env={**os.environ, "PYTHONPATH": str(REPO)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_two_processes_write_different_runs_to_one_database_at_once(env, db):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=ROWS)
    other = env.inputs / "other.json"
    other.write_text(json.dumps(ema_config()))
    first = _spawn(env, db, env.out / "a", "--fills", "realistic")
    env.algos, saved = other, env.algos
    second = _spawn(env, db, env.out / "b", "--fills", "legacy")
    env.algos = saved
    results = [p.communicate(timeout=300) for p in (first, second)]
    assert [p.returncode for p in (first, second)] == [0, 0], results
    store = read(db)
    try:
        runs = {r.run_id: r for r in store.list_runs()}
        assert sorted(r.status for r in runs.values()) == ["completed", "completed"]
        by_fills = {r.fills: store.load_run(r.run_id) for r in runs.values()}
    finally:
        store.close()
    assert len(by_fills["realistic"].algos) == 3 and len(by_fills["legacy"].algos) == 1
    for data in by_fills.values():
        names = {a.idname for a in data.algos}
        assert data.fills and {f.account for f in data.fills} <= names
        assert {t.account for t in data.round_trips} <= names


def test_a_killed_process_leaves_a_running_row_and_the_database_stays_usable(env, db):
    import time

    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    stall = ("import sys, time\n"
             "import MainStoinker.engine.cli as cli\n"
             "cli.Engine.run_backtest = lambda self: time.sleep(600)\n"
             "sys.exit(cli.main(sys.argv[1:]))\n")
    proc = _spawn(env, db, env.out / "killed", "--fills", "realistic", code=stall)
    try:
        deadline = time.monotonic() + 120
        seen = False
        while time.monotonic() < deadline and proc.poll() is None and not seen:
            if db.exists():
                with contextlib.suppress(StoreError):  # the child may still be creating it
                    seen = [r.status for r in read_runs(db)] == ["running"]
            time.sleep(0.1)
        assert seen, "the run row never appeared"
    finally:
        proc.kill()
        proc.communicate(timeout=60)
    # On Windows the first open right after a kill can fail once ("disk I/O error")
    # while the dead process's handles are released; it works a moment later.
    for attempt in range(100):
        try:
            (row,) = read_runs(db)
            break
        except StoreError:
            if attempt == 99:
                raise
            time.sleep(0.1)
    assert (row.status, row.ended_at) == ("running", None)
    assert run(env, "--db", str(db)) == 0
    assert [r.status for r in read_runs(db)] == ["running", "completed"]
    assert not (env.out / "killed" / "trades.json").exists()


def read_runs(db: Path):
    store = read(db)
    try:
        return store.list_runs()
    finally:
        store.close()


# ---- a locked database ------------------------------------------------------------


def hold_write_lock(db: Path) -> sqlite3.Connection:
    other = sqlite3.connect(db, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    return other


@pytest.fixture
def short_wait(monkeypatch):
    """The command opens its store with a 0.2 s lock wait instead of 10 s."""
    real = cli.open_store
    monkeypatch.setattr(cli, "open_store",
                        lambda spec, *, env: real(spec, env=env, busy_timeout=0.2))


def test_a_locked_database_at_begin_run_is_exit_3_one_line_no_traceback(env, db, short_wait,
                                                                        capsys):
    env.setup(ema_config(), ["AAPL"], rows=300)
    open_store(str(db), env=os.environ).close()  # exists and is migrated
    other = hold_write_lock(db)
    try:
        assert run(env, "--db", str(db)) == 3
        err = capsys.readouterr().err
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert err.startswith("error: could not record the run: ") and "locked" in err
    assert len(err.splitlines()) == 1 and "Traceback" not in err
    assert not env.out.exists()  # nothing ran, and the directory made for it is gone
    assert read_runs(db) == ()
    db.unlink()


def test_a_locked_database_at_write_results_is_exit_3_with_the_files_in_place(
        env, db, short_wait, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    real_run = cli.Engine.run_backtest
    held: list[sqlite3.Connection] = []

    def run_then_lock(self):
        real_run(self)
        held.append(hold_write_lock(db))  # taken after begin_run, before results are stored

    monkeypatch.setattr(cli.Engine, "run_backtest", run_then_lock)
    try:
        assert run(env, "--fills", "realistic", "--db", str(db)) == 3
        err = capsys.readouterr().err
    finally:
        for other in held:
            other.execute("ROLLBACK")
            other.close()
    assert err.startswith("error: run finished but could not be stored: ") and "locked" in err
    assert len(err.splitlines()) == 1 and "Traceback" not in err
    assert sorted(p.name for p in env.out.glob("*.json")) == ["ledger.json", "trades.json"]
    assert [r.status for r in read_runs(db)] == ["running"]  # honest: it could not be marked
    db.unlink()


# ---- the output directory and the stale ledger ------------------------------------


def test_out_below_a_file_is_refused_before_the_database_exists(env, db, tmp_path, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    (tmp_path / "plain").write_text("a file")
    env.out = tmp_path / "plain" / "out"
    assert run(env, "--db", str(db)) == 2
    assert "not a directory" in one_line_error(capsys)
    assert not db.exists() and not db.parent.exists()


def test_out_that_cannot_be_made_is_exit_2_and_records_no_run(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **kw):
        if self == env.out:
            raise PermissionError("denied")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert run(env, "--db", str(db)) == 2
    assert "cannot create output directory" in one_line_error(capsys)
    assert not env.out.exists()
    monkeypatch.undo()
    assert read_runs(db) == ()  # an empty database is what this case leaves
    db.unlink()


def test_out_made_by_a_run_that_could_not_be_recorded_is_removed(env, db, tmp_path,
                                                                 monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    env.out = tmp_path / "a" / "b" / "out"
    monkeypatch.setattr(store_module.SqliteStore, "begin_run",
                        lambda self, meta: (_ for _ in ()).throw(StoreError("locked")))
    assert run(env, "--db", str(db)) == 3
    assert capsys.readouterr().err == "error: could not record the run: locked\n"
    assert not (tmp_path / "a").exists()  # every folder this run made, as far as it is empty
    db.unlink()


def test_an_existing_out_directory_is_kept_when_the_run_could_not_be_recorded(
        env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    env.out.mkdir()
    (env.out / "keep.txt").write_text("mine")
    monkeypatch.setattr(store_module.SqliteStore, "begin_run",
                        lambda self, meta: (_ for _ in ()).throw(StoreError("locked")))
    assert run(env, "--db", str(db)) == 3
    assert (env.out / "keep.txt").read_text() == "mine"
    env.out.joinpath("keep.txt").unlink()
    monkeypatch.undo()
    db.unlink()
    capsys.readouterr()


def test_a_stale_ledger_that_cannot_be_removed_is_exit_2_and_stores_nothing(env, db, capsys):
    env.setup(ema_config(), ["AAPL"], rows=300)
    stale = env.out / "ledger.json"
    stale.mkdir(parents=True)  # unlink refuses a directory (a non-empty one on every platform)
    (stale / "x").write_text("x")
    assert run(env, "--db", str(db)) == 2
    assert "cannot remove stale" in one_line_error(capsys)
    assert read_runs(db) == ()  # the documented outcome: no run row at all
    assert not (env.out / "trades.json").exists()
    db.unlink()


# ---- --quiet is exactly what it was -----------------------------------------------


def test_quiet_without_db_prints_only_the_result_lines_in_a_fresh_process(env):
    """Legacy algo modules print when they are imported ("initializing socket connection").
    In-process tests cannot see that (the modules are already imported), so run the real
    command in a fresh interpreter."""
    env.setup(ema_config(), ["AAPL"], rows=300)
    done = subprocess.run(
        [sys.executable, "-m", "MainStoinker", "backtest", "--algos", str(env.algos),
         "--data", str(env.data), "--out", str(env.out), "--quiet"],
        cwd=env.start, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True,
        text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 3, done.stdout
    assert lines[0].startswith("replayed ") and lines[1].startswith(f"{EMA['idname']}: ")
    assert lines[2] == f"wrote {env.out.resolve() / 'trades.json'}"
    assert list(env.start.iterdir()) == []  # and nothing written to the start directory


# ---- housekeeping -----------------------------------------------------------------


def test_help_describes_db(capsys):
    with pytest.raises(SystemExit):
        main(["backtest", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--db SPEC" in text and "OneDrive" in text


def test_cli_error_type_is_what_main_turns_into_exit_2(env, db, monkeypatch, capsys):
    """A store error while opening is a CliError, so main prints one line and returns 2."""
    def refuse(spec, *, env):
        raise StoreError("nope")

    monkeypatch.setattr(cli, "open_store", refuse)
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert run(env, "--db", str(db)) == 2
    assert capsys.readouterr().err == "error: nope\n"
    assert issubclass(CliError, Exception)


# ---- indicator series and data fingerprints (D3) -----------------------------------

ELLIOT_MSFT = {"idname": "MSFT-elliot", "ticker": "MSFT", "order": 10, "trailingStopDelta": 0.1}


@pytest.fixture
def seen_frames(monkeypatch) -> dict[str, dict]:
    """Records each algo's final frame (per column: non-NaN count and values) at capture time."""
    frames: dict[str, dict] = {}
    real = cli.extract_series

    def spy(algo, bar_times):
        frames[algo.name] = {"rows": len(algo.AlgoData), "bars": len(bar_times),
                             "frame": algo.AlgoData.copy(deep=True)}
        return real(algo, bar_times)

    monkeypatch.setattr(cli, "extract_series", spy)
    return frames


def series_of(db: Path, algo: str, run_id: int = 1):
    store = read(db)
    try:
        data = store.load_run(run_id)
        points = store.load_series(run_id, algo)
    finally:
        store.close()
    return data, points


def test_symbol_pattern_is_the_feeds():
    from MainStoinker.engine import feed

    assert cli._SYMBOL is feed._SYMBOL


def test_stored_run_has_series_for_every_frontend_column(env, db, seen_frames):
    env.setup(ema_config(), ["AAPL"], rows=None)
    assert run(env, "--db", str(db)) == 0
    data, points = series_of(db, EMA["idname"])
    assert [(s.account, s.name, s.draw_type, s.position) for s in data.series] == [
        (EMA["idname"], "MA20", "line", 0), (EMA["idname"], "MA50", "line", 1),
        (EMA["idname"], "StopPrice", "segment", 2), (EMA["idname"], "Trade", "baseline", 3)]
    seen = seen_frames[EMA["idname"]]
    assert seen["rows"] == seen["bars"]
    for name in ("MA20", "MA50", "StopPrice", "Trade"):
        stored_count = sum(1 for p in points if p.name == name)
        assert stored_count == int(seen["frame"][name].notna().sum())
    assert sum(1 for p in points if p.name == "MA20") > 1000
    assert [p.time for p in points if p.name == "MA20"] == sorted(
        p.time for p in points if p.name == "MA20")


def test_elliot_markers_stored(env, db):
    env.setup([{"ID": "Elliot", "data": [ELLIOT_MSFT]}], ["MSFT"], rows=2000)
    assert run(env, "--db", str(db)) == 0
    data, points = series_of(db, ELLIOT_MSFT["idname"])
    kinds = {s.name: s.draw_type for s in data.series}
    assert kinds == {"mins": "marker-up", "maxs": "marker-down", "StopPrice": "segment",
                     "Trade": "baseline"}
    assert any(p.name == "mins" for p in points) and any(p.name == "maxs" for p in points)


def test_series_cover_warmup_bars(env, db):
    env.setup(ema_config(), ["AAPL"], rows=1200)
    assert run(env, "--db", str(db)) == 0
    data, points = series_of(db, EMA["idname"])
    first = min(p.time for p in points if p.name == "MA20")
    assert first < data.summary.data.first_bar  # computed over the warm-up days


def test_series_of_two_algos_on_two_symbols_are_kept_apart(env, db):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=ROWS)
    assert run(env, "--db", str(db)) == 0
    data, aapl = series_of(db, EMA["idname"])
    _, msft = series_of(db, EMA_MSFT["idname"])
    assert {s.account for s in data.series} == {EMA["idname"], EMA_MSFT["idname"]}
    assert aapl and msft and aapl != msft


def test_data_files_fingerprint_stored(env, db):
    env.setup(ema_config(), ["AAPL"], rows=300)
    assert run(env, "--db", str(db)) == 0
    files = stored(db).summary.data.files
    raw = (env.data / "_AAPL_offlinedata_").read_bytes()
    assert files == (("AAPL", hashlib.sha256(raw).hexdigest(), len(raw)),)


def test_series_store_failure_exit_3_run_failed(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)

    def broken(self, run_id, infos, points=()):
        raise StoreError("disk full")

    monkeypatch.setattr(store_module.SqliteStore, "write_series", broken)
    assert run(env, "--db", str(db)) == 3
    assert capsys.readouterr().err == "error: run finished but could not be stored: disk full\n"
    assert (env.out / "trades.json").exists()
    assert [r.status for r in read_runs(db)] == ["failed"]
    db.unlink()


def test_series_are_written_after_results_and_before_the_run_ends(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    order: list[str] = []
    for method in ("write_results", "write_series", "end_run"):
        real = getattr(store_module.SqliteStore, method)

        def wrapped(self, *a, _real=real, _method=method, **kw):
            order.append(_method)
            return _real(self, *a, **kw)

        monkeypatch.setattr(store_module.SqliteStore, method, wrapped)
    assert run(env, "--db", str(db)) == 0
    assert order == ["write_results", "write_series", "end_run"]


def test_without_db_nothing_is_extracted(env, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=300)

    def refuse(*a, **kw):
        raise AssertionError("extract_series must not run without --db")

    monkeypatch.setattr(cli, "extract_series", refuse)
    assert run(env) == 0


def test_an_algo_with_mismatched_frame_is_skipped_with_a_warning(env, db, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    real = cli.extract_series

    def short(algo, bar_times):
        return real(algo, bar_times[:-1])

    monkeypatch.setattr(cli, "extract_series", short)
    assert run(env, "--db", str(db)) == 0
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("warning: ")
    assert stored(db).series == ()  # the run itself is stored and completed
    assert stored(db).summary.status == "completed"


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_trades_and_ledger_json_unchanged_with_capture(env, db, fills):
    env.setup(ema_config(), ["AAPL"], rows=1500)
    assert run(env, "--fills", fills) == 0
    plain = {p.name: p.read_bytes() for p in env.out.glob("*.json")}
    assert "trades.json" in plain
    assert run(env, "--fills", fills, "--db", str(db)) == 0
    assert stored(db).series  # capture really happened
    assert {p.name: p.read_bytes() for p in env.out.glob("*.json")} == plain


def test_repeated_bar_time_skips_the_algo_and_the_run_completes(env, db, capsys):
    import pandas as pd

    env.setup(ema_config(), ["AAPL"], rows=ROWS)
    path = env.data / "_AAPL_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    repeated = frame["date"].iloc[ROWS - 20]
    frame = pd.concat([frame.iloc[:ROWS - 19], frame.iloc[ROWS - 20:]], ignore_index=True)
    frame.to_csv(path)
    assert run(env, "--db", str(db)) == 0
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith(f"warning: {EMA['idname']}:")
    assert repeated in err[0]
    data = stored(db)
    assert data.summary.status == "completed"
    assert data.series == ()
    assert (env.out / "trades.json").exists()
    assert data.round_trips or data.fills or data.equity  # results are stored


def test_an_exception_while_reading_one_algos_series_skips_that_algo(
        env, db, monkeypatch, capsys):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=ROWS)
    real = cli.extract_series

    class Broken:
        name = EMA["idname"]
        ticker = "AAPL"
        FrontEndDataStruct = ["MA20"]  # noqa: RUF012
        FrontEndDataType = ["line"]  # noqa: RUF012

        @property
        def AlgoData(self):  # noqa: N802
            raise RuntimeError("frame is gone")

    def extract(algo, bar_times):
        return real(Broken() if algo.name == EMA["idname"] else algo, bar_times)

    monkeypatch.setattr(cli, "extract_series", extract)
    assert run(env, "--db", str(db)) == 0
    err = capsys.readouterr().err.splitlines()
    assert err == [f"warning: {EMA['idname']}: indicator series not stored (RuntimeError)"]
    data = stored(db)
    assert data.summary.status == "completed"
    assert {s.account for s in data.series} == {EMA_MSFT["idname"]}


def test_write_series_is_called_once_per_algo_with_series(env, db, monkeypatch):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=ROWS)
    accounts: list[set[str]] = []
    real = store_module.SqliteStore.write_series

    def spy(self, run_id, infos, points=()):
        points = list(points)
        accounts.append({i.account for i in infos} | {p.account for p in points})
        return real(self, run_id, infos, points)

    monkeypatch.setattr(store_module.SqliteStore, "write_series", spy)
    assert run(env, "--db", str(db)) == 0
    assert accounts == [{EMA["idname"]}, {EMA_MSFT["idname"]}]
    assert {s.account for s in stored(db).series} == {EMA["idname"], EMA_MSFT["idname"]}


def test_a_store_error_part_way_keeps_earlier_series_and_fails_the_run(
        env, db, monkeypatch, capsys):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=ROWS)
    real = store_module.SqliteStore.write_series
    calls: list[int] = []

    def second_fails(self, run_id, infos, points=()):
        calls.append(1)
        if len(calls) == 2:
            raise StoreError("disk full")
        return real(self, run_id, infos, points)

    monkeypatch.setattr(store_module.SqliteStore, "write_series", second_fails)
    assert run(env, "--db", str(db)) == 3
    assert "disk full" in capsys.readouterr().err
    data = stored(db)
    assert data.summary.status == "failed"
    assert {s.account for s in data.series} == {EMA["idname"]}


def test_no_database_file_in_the_repository():
    found = []
    for _folder, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "__pycache__", ".pytest_cache"}]
        found += [n for n in names
                  if n.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal"))]
    assert found == []
