"""`backtest --db`: each run is stored, and nothing changes without it.

Every database lives under pytest's `tmp_path` (outside OneDrive) and is
opened through `open_store`; no test may create a database in the repository
(`test_no_database_file_in_the_repository` checks that at the end of the
module). The OneDrive refusal is tested by injecting the environment, not by
using the real one.
"""
from __future__ import annotations

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
from MainStoinker.engine.store import StoreError, open_store
from tests.test_cli_modes import EMA, EMA_MSFT, FUNC_MSFT, GOLDEN, Env, ema_config

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
    env.setup(MULTI, ["AAPL", "MSFT"], rows=900)
    assert run(env, "--fills", "realistic", "--capital", "2500.5", "--db", str(db)) == 0
    algos = stored(db).algos
    assert [(a.idname, a.algo_id, a.ticker, a.mode, a.capital) for a in algos] == [
        ("MSFT-functest", "FunctionTest", "MSFT", "backtest", Decimal("2500.5")),
        ("AAPL-10/30EMA", "EMACrossing", "AAPL", "backtest", Decimal("2500.5")),
        ("MSFT-10/30EMA", "EMACrossing", "MSFT", "paper", Decimal("2500.5")),
    ]
    assert [dict(a.params) for a in algos] == [FUNC_MSFT, EMA, {**EMA_MSFT, "mode": "paper"}]


def test_run_exists_while_the_engine_runs_and_results_are_written_once(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=900)
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
    env.setup([{"ID": "EMACrossing", "data": [EMA]}], ["AAPL"], rows=900)
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
    env.setup(MULTI, ["AAPL", "MSFT"], rows=900)
    for fills in ("legacy", "realistic"):
        assert run(env, "--fills", fills) == 0
        # (tradey.txt is a legacy side file the algos append to; it is not an output)
        plain = {p.name: p.read_bytes() for p in env.out.iterdir() if p.suffix == ".json"}
        assert "trades.json" in plain
        assert run(env, "--fills", fills, "--db", str(db)) == 0
        assert {p.name: p.read_bytes() for p in env.out.iterdir() if p.suffix == ".json"} == plain
        assert ("ledger.json" in plain) == (fills == "realistic")


def test_legacy_trades_json_with_db_equals_the_golden(env, db):
    env.setup(ema_config(mode="paper"), ["AAPL"], rows=None)
    assert run(env, "--db", str(db)) == 0
    golden = json.loads((GOLDEN / "emacrossing_aapl.json").read_text())
    assert env.trades_bytes() == (json.dumps({EMA["idname"]: golden}, indent=1) + "\n").encode()
    assert stored(db).summary.fills == "legacy"
    assert not (env.out / "ledger.json").exists()


def test_two_runs_one_database_are_isolated(env, db, capsys):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=900)
    assert run(env, "--fills", "realistic", "--db", str(db)) == 0
    env.setup(ema_config(), ["AAPL"], rows=900)
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
    env.setup(ema_config(), ["AAPL"], rows=900)
    for fills in ("legacy", "realistic"):
        assert run(env, "--fills", fills) == 0
    assert "stored run" not in capsys.readouterr().out
    found = [p for p in tmp_path.rglob("*") if any(s in p.name.lower() for s in (".db", "sqlite"))]
    assert found == []
    assert files_below(env.out) >= {"trades.json", "ledger.json"}
    assert list(env.start.iterdir()) == []


def test_output_lines_unchanged_without_db(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=900)
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
    env.setup(ema_config(), ["AAPL"], rows=900)
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
    env.setup(ema_config(), ["AAPL"], rows=900)
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
    env.setup(ema_config(), ["AAPL"], rows=900)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)

    def broken(self, run_id, status):
        raise ValueError("something else entirely")

    monkeypatch.setattr(store_module.SqliteStore, "end_run", broken)
    with pytest.raises(RuntimeError, match="engine exploded"):
        run(env, "--db", str(db))


def test_store_closed_even_when_end_run_fails(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=900)
    monkeypatch.setattr(cli.Engine, "run_backtest", boom)
    monkeypatch.setattr(store_module.SqliteStore, "end_run",
                        lambda self, run_id, status: (_ for _ in ()).throw(StoreError("x")))
    with pytest.raises(RuntimeError):
        run(env, "--db", str(db))
    db.unlink()
    assert not list(db.parent.glob("runs.db*"))


def test_keyboard_interrupt_marks_the_run_failed_and_propagates(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=900)

    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.Engine, "run_backtest", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(env, "--db", str(db))
    assert stored(db).summary.status == "failed"


def test_output_failure_after_results_marks_the_run_failed(env, db, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=900)

    def refuse(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_json_atomic", refuse)
    with pytest.raises(OSError, match="disk full"):
        run(env, "--fills", "realistic", "--db", str(db))
    assert stored(db).summary.status == "failed"
    db.unlink()


def test_store_closed_on_success(env, db):
    env.setup(ema_config(), ["AAPL"], rows=900)
    assert run(env, "--db", str(db)) == 0
    db.unlink()  # a connection left open would make this fail on Windows
    assert not list(db.parent.glob("runs.db*"))


def test_db_in_a_new_folder_and_sqlite_url_form(env, tmp_path, capsys):
    env.setup(ema_config(), ["AAPL"], rows=900)
    target = tmp_path / "a" / "b" / "runs.db"
    assert run(env, "--db", "sqlite:///" + str(target)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {target.resolve()}"
    assert stored(target).summary.status == "completed"


def test_relative_db_path_is_relative_to_the_start_directory(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=900)
    assert run(env, "--db", "rel.db") == 0
    assert (env.start / "rel.db").exists() and not (env.out / "rel.db").exists()
    assert capsys.readouterr().out.splitlines()[-1] == f"stored run 1 in {(env.start / 'rel.db').resolve()}"


# ---- code version -----------------------------------------------------------------


class Done:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout, self.returncode = stdout, returncode


def fake_git(sha: bytes, diff: bytes, calls: list | None = None):
    def runner(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return Done(sha + b"\n")
        if args[:3] == ["git", "diff", "HEAD"]:
            return Done(diff)
        raise AssertionError(args)

    return runner


def test_code_version_clean(tmp_path):
    assert cli.current_code_version(tmp_path, run=fake_git(VERSION.encode(), b"")) == VERSION


def test_code_version_dirty_suffix(tmp_path):
    diff = b"diff --git a/x b/x\n+changed\n"
    calls: list = []
    version = cli.current_code_version(tmp_path, run=fake_git(VERSION.encode(), diff, calls))
    assert version == f"{VERSION}-dirty:{hashlib.sha256(diff).hexdigest()[:12]}"
    assert len(version.split("-dirty:")[1]) == 12
    # git is run without a shell, in the given directory, with a timeout
    assert [c[0][:3] for c in calls] == [["git", "rev-parse", "HEAD"], ["git", "diff", "HEAD"]]
    for args, kwargs in calls:
        assert isinstance(args, list) and not kwargs.get("shell")
        assert kwargs["cwd"] == str(tmp_path) and 0 < kwargs["timeout"] <= 60
    assert "--no-ext-diff" in calls[1][0]
    # a different change gives a different suffix
    other = cli.current_code_version(tmp_path, run=fake_git(VERSION.encode(), diff + b"+more\n"))
    assert other != version and other.startswith(VERSION + "-dirty:")


def test_code_version_defaults_to_the_package_directory():
    calls: list = []
    cli.current_code_version(run=fake_git(VERSION.encode(), b"", calls))
    assert calls[0][1]["cwd"] == str(Path(cli.__file__).resolve().parent)


@pytest.mark.parametrize("failure", [
    FileNotFoundError("git"), PermissionError("no"), OSError("broken"),
    subprocess.TimeoutExpired("git", 15), subprocess.SubprocessError("x"),
])
def test_code_version_unknown_when_git_is_unavailable(tmp_path, failure):
    def runner(args, **kwargs):
        raise failure

    assert cli.current_code_version(tmp_path, run=runner) == "unknown"


@pytest.mark.parametrize("sha, code", [(b"", 128), (b"fatal", 128), (b"abc123", 0), (b"", 0),
                                       (b"zz" * 20, 0)])
def test_code_version_unknown_for_a_non_repository_or_odd_output(tmp_path, sha, code):
    assert cli.current_code_version(
        tmp_path, run=lambda args, **kw: Done(sha, code)) == "unknown"


def test_code_version_unknown_when_the_diff_fails(tmp_path):
    def runner(args, **kwargs):
        return Done(VERSION.encode(), 0) if args[1] == "rev-parse" else Done(b"", 1)

    assert cli.current_code_version(tmp_path, run=runner) == "unknown"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_code_version_with_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*words: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *words],
            cwd=repo, capture_output=True, text=True, check=True, timeout=60)
        return done.stdout.strip()

    git("init", "-q")
    (repo / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    head = git("rev-parse", "HEAD")
    assert cli.current_code_version(repo) == head
    (repo / "sub").mkdir()
    assert cli.current_code_version(repo / "sub") == head  # any folder inside the repository
    (repo / "a.txt").write_text("two\n")
    expected = hashlib.sha256(subprocess.run(
        ["git", "diff", "HEAD", "--no-ext-diff", "--no-textconv"], cwd=repo,
        capture_output=True, check=True, timeout=60).stdout).hexdigest()[:12]
    assert cli.current_code_version(repo) == f"{head}-dirty:{expected}"
    git("checkout", "--", "a.txt")
    assert cli.current_code_version(repo) == head


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_code_version_unknown_outside_a_repository(tmp_path):
    probe = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                           timeout=60)
    if probe.returncode == 0:
        pytest.skip("the temporary directory is inside a repository on this machine")
    assert cli.current_code_version(tmp_path) == "unknown"


def test_code_version_is_stored_and_computed_once_per_run(env, db, monkeypatch):
    calls: list[int] = []

    def version() -> str:
        calls.append(1)
        return "abc-dirty:123456789abc"

    monkeypatch.setattr(cli, "current_code_version", version)
    env.setup(ema_config(), ["AAPL"], rows=900)
    assert run(env, "--db", str(db)) == 0
    assert calls == [1] and stored(db).summary.code_version == "abc-dirty:123456789abc"
    # an injected callable wins over the default
    args = cli.build_parser().parse_args(
        ["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
         "--quiet", "--db", str(db)])
    assert cli.run_backtest_command(args, code_version=lambda: "injected") == 0
    assert stored(db, 2).summary.code_version == "injected" and calls == [1]


def test_importing_cli_does_not_run_git():
    code = ("import subprocess\n"
            "def no(*a, **k):\n    raise SystemExit('git was run at import time')\n"
            "subprocess.run = no\nsubprocess.Popen = no\n"
            "import MainStoinker.engine.cli\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr


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


def test_no_database_file_in_the_repository():
    found = []
    for _folder, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "__pycache__", ".pytest_cache"}]
        found += [n for n in names
                  if n.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal"))]
    assert found == []
