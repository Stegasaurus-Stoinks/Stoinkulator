"""`runs` and `report`: reading stored runs back.

Every database lives under pytest's `tmp_path` (outside OneDrive) and is made
either by the real `backtest --db` command or, for hand-computed expectations,
by writing records straight into a `SqliteStore`. The OneDrive refusal is
tested by injecting the environment. `test_no_database_file_in_the_repository`
checks the repository at the end of the module.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import MainStoinker.engine.cli as cli
import MainStoinker.engine.cli_report as cli_report
import MainStoinker.engine.codeversion as codeversion
from MainStoinker.engine.cli import main
from MainStoinker.engine.ledger import EquityPoint, IgnoredEvent, OrderEvent, RoundTrip
from MainStoinker.engine.stats import run_statistics, statistics_record
from MainStoinker.engine.store import (
    AlgoMeta,
    DataMeta,
    RunMeta,
    SqliteStore,
    open_store,
)
from tests.test_cli_modes import Env, ema_config
from tests.test_cli_store import FAST_CASES, GOLDEN_PAPER, MULTI, full_store_run

REPO = Path(__file__).resolve().parent.parent
VERSION = "0123456789abcdef0123456789abcdef01234567"
D = Decimal
START = datetime(2026, 3, 4, 9, 30, 0, tzinfo=timezone.utc)
T0 = datetime(2024, 8, 19, 7, 55)
SHARED = ("trade_count", "wins", "commission", "net", "final_equity", "max_drawdown",
          "synthetic_closes", "ignored_events", "cancelled_entries")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "store" / "runs.db"


def backtest(env: Env, db: Path, *extra: str) -> int:
    return main(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                 "--out", str(env.out), "--quiet", "--db", str(db), *extra])


def out_of(capsys) -> str:
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


def error_of(capsys) -> str:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and len(captured.err.strip().splitlines()) == 1
    return captured.err.strip()


def table_rows(text: str) -> dict[str, list[str]]:
    """The report table as {row name: the nine other cells}."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("algo "))
    rows = {}
    for line in lines[start + 2:]:
        if not line.strip():
            break
        cells = line.split()
        rows[cells[0]] = cells[1:]
    return rows


def money2(text: str) -> str:
    """4-decimal text as the table shows it (2 decimals, half up)."""
    return format(D(text).quantize(D("0.01"), rounding="ROUND_HALF_UP"), "f")


def make_run(db: Path, *, algos, trips=(), equity=(), orders=(), ignored=(), status="completed",
             fills="realistic", seconds=2.0, code=VERSION, directory="", symbols=("AAPL",),
             config=None, kind="backtest") -> int:
    """Write one run straight into a store (the clock gives START, then START + seconds)."""
    times = iter([START, START + timedelta(seconds=seconds)])
    store = SqliteStore(db, clock=lambda: next(times))
    try:
        run_id = store.begin_run(RunMeta(
            kind, fills, code,
            config if config is not None else {
                "capital": "1000.0000",
                "costs": {"commission_per_share": "0.0050", "commission_minimum": "1.0000",
                          "slippage_per_share": "0.0000"}},
            DataMeta("csv", directory, tuple(symbols), T0, T0 + timedelta(days=1))))
        store.write_algos(run_id, algos)
        store.write_results(run_id, order_events=orders, round_trips=trips, equity=equity,
                            ignored_events=ignored)
        if status != "running":
            store.end_run(run_id, status)
        return run_id
    finally:
        store.close()


def trip(account: str, net: str, commission: str = "1", *, synthetic: bool = False) -> RoundTrip:
    n, c = D(net), D(commission)
    return RoundTrip(account, ("ref", net), "AAPL", "long", D("10"), T0, T0 + timedelta(minutes=5),
                     D("100"), D("101"), n + c, c, n, timedelta(minutes=5),
                     "end_of_data" if synthetic else "algo_exit", synthetic)


def point(account, equity: str, minute: int = 0) -> EquityPoint:
    value = D(equity)
    return EquityPoint(T0 + timedelta(minutes=minute), account, value, D("0"), D("0"), D("0"), value)


HAND_ALGOS = [AlgoMeta("X-1", "Algo", "AAPL", "backtest", D("1000"))]
HAND_TRIPS = [trip("X-1", "12.3450"), trip("X-1", "-2.5000"), trip("X-1", "0.0000")]
HAND_EQUITY = [point(a, v, m) for m, v in enumerate(("1000", "1012.345", "1009.845", "1009.845"))
               for a in ("X-1", None)]


# ---- runs -------------------------------------------------------------------------


def test_runs_lists_in_order(env, db, capsys):
    env.setup(ema_config(), ["AAPL"], rows=600)
    assert backtest(env, db) == 0  # legacy fills
    assert backtest(env, db, "--fills", "realistic") == 0
    capsys.readouterr()
    assert main(["runs", "--db", str(db)]) == 0
    lines = out_of(capsys).splitlines()
    header, rule, first, second = lines
    assert header.split()[:3] == ["id", "started", "(UTC)"]
    assert set(rule.replace(" ", "")) == {"-"}
    one, two = first.split(), second.split()
    assert (one[0], two[0]) == ("1", "2")
    assert "legacy" in one and "realistic" in two
    assert one.count("completed") == 1 and two.count("completed") == 1
    assert "backtest" in one and VERSION[:10] in one and VERSION[:10] in two
    assert str(env.data.parent.parent) not in "\n".join(lines)  # no stored directory


def test_runs_marks_incomplete(db, capsys):
    make_run(db, algos=HAND_ALGOS, status="running")
    make_run(db, algos=HAND_ALGOS, status="failed")
    make_run(db, algos=HAND_ALGOS)
    assert main(["runs", "--db", str(db)]) == 0
    rows = [line.split() for line in out_of(capsys).splitlines()[2:]]
    assert [r[0] for r in rows] == ["1", "2", "3"]
    assert "incomplete" in rows[0] and "running" not in rows[0]
    assert "failed" in rows[1] and "completed" in rows[2]
    assert rows[0][3] == "-"  # no duration while running


def test_runs_columns_and_formats(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY, seconds=75,
             code=VERSION + "-dirty:abcdef123456", directory="/some/where/OfflineData")
    assert main(["runs", "--db", str(db)]) == 0
    text = out_of(capsys)
    assert text.splitlines()[0].split() == ["id", "started", "(UTC)", "duration", "status", "kind",
                                            "fills", "algos", "trades", "net", "code"]
    assert text.splitlines()[2].split() == ["1", "2026-03-04", "09:30:00", "1m15s", "completed",
                                            "backtest", "realistic", "1", "3", "9.85",
                                            "0123456789+dirty"]
    assert "where" not in text and "OfflineData" not in text


def test_runs_json(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY, seconds=1.5,
             symbols=("AAPL", "MSFT"))
    make_run(db, algos=HAND_ALGOS, status="running", directory="/secret/user/data")
    assert main(["runs", "--db", str(db), "--json"]) == 0
    text = out_of(capsys)
    assert "secret" not in text
    first, second = json.loads(text)["runs"]
    assert list(first) == ["run_id", "started_at", "ended_at", "duration_seconds", "status",
                           "incomplete", "kind", "fills", "code_version", "symbols", "algo_count",
                           "trade_count", "net"]
    assert first == {
        "run_id": 1, "started_at": "2026-03-04T09:30:00+00:00",
        "ended_at": "2026-03-04T09:30:01.500000+00:00", "duration_seconds": 1.5,
        "status": "completed", "incomplete": False, "kind": "backtest", "fills": "realistic",
        "code_version": VERSION, "symbols": ["AAPL", "MSFT"], "algo_count": 1, "trade_count": 3,
        "net": "9.8450"}
    assert (second["status"], second["incomplete"], second["ended_at"],
            second["duration_seconds"]) == ("running", True, None, None)


def test_runs_on_an_empty_database(db, capsys):
    open_store(str(db), env=os.environ).close()
    assert main(["runs", "--db", str(db)]) == 0
    assert out_of(capsys).strip() == "no runs stored"
    assert main(["runs", "--db", str(db), "--json"]) == 0
    assert json.loads(out_of(capsys)) == {"runs": []}


# ---- report: hand-computed -----------------------------------------------------------


def test_report_money_two_decimals(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    assert main(["report", "1", "--db", str(db)]) == 0
    text = out_of(capsys)
    expected = ["3", "1", "33.3", "9.85", "3.00", "12.35", "-2.50", "2.50", "1009.85"]
    # 9.845 and 12.345 round half up; the break-even trip counts as a trade but is no win or loss
    assert table_rows(text) == {"X-1": expected, "TOTAL": expected}
    assert "capital per algo: 1000.0000" in text
    assert ("costs: commission 0.0050/share, minimum 1.0000 per order, slippage 0.0000/share"
            in text)
    assert text.count("TOTAL") == 2  # the row and the footer
    # every table line has the same width, up to trailing spaces
    start = next(i for i, ln in enumerate(text.splitlines()) if ln.startswith("algo "))
    assert len({len(ln) for ln in text.splitlines()[start:start + 4]}) == 1


def test_report_header(db, capsys, tmp_path, monkeypatch):
    inside = tmp_path / "work" / "OfflineData"
    inside.mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "work")
    make_run(db, algos=HAND_ALGOS, directory=str(inside), symbols=("AAPL", "MSFT"),
             code=VERSION + "-dirty:abc")
    assert main(["report", "1", "--db", str(db)]) == 0
    text = out_of(capsys)
    lines = text.splitlines()
    assert lines[0] == "run 1" and lines[1] == "status: completed"
    assert "kind: backtest" in lines and "fills: realistic" in lines
    assert "started: 2026-03-04 09:30:00 UTC" in lines
    assert "ended: 2026-03-04 09:30:02 UTC  (duration 2.0s)" in lines
    assert "code version: 0123456789+dirty" in lines
    assert "symbols: AAPL, MSFT" in lines
    assert "bars: 2024-08-19 07:55:00 to 2024-08-20 07:55:00" in lines
    assert "data directory: OfflineData" in lines  # relative to the current directory


def test_report_data_directory_outside_the_current_directory(db, capsys, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "somebody" / "private" / "OfflineData"
    make_run(db, algos=HAND_ALGOS, directory=str(outside))
    assert main(["report", "1", "--db", str(db)]) == 0
    text = out_of(capsys)
    assert "data directory: OfflineData" in text.splitlines()
    assert "somebody" not in text and "private" not in text
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    assert json.loads(out_of(capsys))["run"]["data_directory"] == "OfflineData"


def test_failed_and_running_runs_are_reported_with_a_note(db, capsys):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS[:1], status="running")
    make_run(db, algos=HAND_ALGOS, status="failed")
    assert main(["report", "1", "--db", str(db)]) == 0
    text = out_of(capsys)
    assert text.splitlines()[1].startswith("status: incomplete (still marked running")
    assert "ended: -" in text and table_rows(text)["TOTAL"][0] == "1"
    assert main(["report", "2", "--db", str(db)]) == 0
    text = out_of(capsys)
    assert text.splitlines()[1].startswith("status: failed. The run did not finish")
    assert table_rows(text)["TOTAL"][:2] == ["0", "0"]
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    run = json.loads(out_of(capsys))["run"]
    assert (run["status"], run["incomplete"]) == ("running", True)


def test_report_without_trades_shows_dashes(db, capsys):
    make_run(db, algos=HAND_ALGOS)
    assert main(["report", "1", "--db", str(db)]) == 0
    row = table_rows(out_of(capsys))["X-1"]
    assert row == ["0", "0", "-", "0.00", "0.00", "-", "-", "0.00", "1000.00"]
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    acct = json.loads(out_of(capsys))["accounts"]["X-1"]
    assert acct["win_rate"] is None and acct["avg_win"] is None and acct["avg_loss"] is None


def test_report_synthetic_ignored_and_cancelled_are_shown(db, capsys):
    trips = [trip("X-1", "5"), trip("X-1", "-1", synthetic=True)]
    orders = [OrderEvent(1, "X-1", "r", "placed", T0, "buy", D("1"), "entry", None),
              OrderEvent(1, "X-1", "r", "cancelled", T0)]
    ignored = [IgnoredEvent("X-1", "r", "close", T0), IgnoredEvent("X-1", "r", "close", T0)]
    make_run(db, algos=HAND_ALGOS, trips=trips, orders=orders, ignored=ignored)
    assert main(["report", "1", "--db", str(db)]) == 0
    assert ("TOTAL: 1 synthetic end-of-data closes (counted in the trades), 2 ignored events, "
            "1 cancelled entries") in out_of(capsys)
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    total = json.loads(out_of(capsys))["total"]
    assert (total["synthetic_closes"], total["cancelled_entries"], total["trade_count"]) == (1, 1, 2)
    assert total["ignored_events"] == {"close": 2, "modify_stop": 0, "take_profit": 0}


# ---- report against real runs ---------------------------------------------------------


def test_report_matches_stats(env, db, capsys):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=900)
    assert backtest(env, db, "--fills", "realistic") == 0
    capsys.readouterr()
    store = open_store(str(db), env=os.environ)
    try:
        data = store.load_run(1)
    finally:
        store.close()
    expected = run_statistics(data, ("close", "modify_stop", "take_profit"))
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    document = json.loads(out_of(capsys))
    assert list(document["accounts"]) == [a.idname for a in data.algos]
    for idname, st in expected["accounts"].items():
        shown = document["accounts"][idname]
        assert {k: v for k, v in shown.items() if k not in ("symbol", "algo", "mode")} == \
            statistics_record(st)
    assert document["total"] == statistics_record(expected["total"])
    # and the same numbers as ledger.json of that run
    ledger = env.ledger()
    for idname, acct in ledger["accounts"].items():
        for key in SHARED:
            assert document["accounts"][idname][key] == acct["summary"][key], (idname, key)
    for key in SHARED:
        assert document["total"][key] == ledger["summary"][key], key
    assert document["run"]["fills"] == "realistic" and document["run"]["capital"] == "10000.0000"
    assert document["total"]["synthetic_closes"] + document["total"]["cancelled_entries"] > 0


def test_multi_algo_total_row_is_the_sums(env, db, capsys):
    env.setup(MULTI, ["AAPL", "MSFT"], rows=900)
    assert backtest(env, db, "--fills", "realistic", "--capital", "5000") == 0
    capsys.readouterr()
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    document = json.loads(out_of(capsys))
    accounts, total = document["accounts"].values(), document["total"]
    assert len(document["accounts"]) == 3
    for key in ("trade_count", "wins", "losses", "break_even", "synthetic_closes",
                "cancelled_entries"):
        assert total[key] == sum(a[key] for a in accounts), key
    for key in ("net", "commission", "total_win", "total_loss", "capital"):
        assert D(total[key]) == sum(D(a[key]) for a in accounts), key
    assert total["capital"] == "15000.0000"
    assert total["trade_count"] > 0
    assert D(total["final_equity"]) == sum(D(a["final_equity"]) for a in accounts)
    # the table agrees with the JSON
    assert main(["report", "1", "--db", str(db)]) == 0
    rows = table_rows(out_of(capsys))
    assert list(rows) == [*document["accounts"], "TOTAL"]
    for name, shown in rows.items():
        st = total if name == "TOTAL" else document["accounts"][name]
        rate = "-" if st["win_rate"] is None else format(
            (D(st["win_rate"]) * 100).quantize(D("0.1"), rounding="ROUND_HALF_UP"), "f")
        assert shown[:3] == [str(st["trade_count"]), str(st["wins"]), rate], name
        assert shown[3] == money2(st["net"]) and shown[4] == money2(st["commission"])
        assert shown[7] == money2(st["max_drawdown"]) and shown[8] == money2(st["final_equity"])
        for cell, key in ((shown[5], "avg_win"), (shown[6], "avg_loss")):
            assert cell == ("-" if st[key] is None else money2(st[key]))


@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_report_equals_ledger_and_golden_paper(tmp_path_factory, capsys, case):
    """The stored realistic run of four algos, reported through the real command: every
    shared figure equals the golden's summary and the run's own ledger.json."""
    full, database = full_store_run(tmp_path_factory, "realistic")
    capsys.readouterr()  # whatever the (cached) backtest printed
    idname = FAST_CASES[case][1]["idname"]
    golden = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())["summary"]
    assert main(["report", "1", "--db", str(database), "--json"]) == 0
    document = json.loads(out_of(capsys))
    acct = document["accounts"][idname]
    assert {k: acct[k] for k in SHARED} == {k: golden[k] for k in SHARED}
    assert {k: acct[k] for k in SHARED} == {k: full.ledger()["accounts"][idname]["summary"][k]
                                              for k in SHARED}
    assert {k: document["total"][k] for k in SHARED} == {k: full.ledger()["summary"][k]
                                                         for k in SHARED}
    # the table shows the same numbers
    assert main(["report", "1", "--db", str(database)]) == 0
    row = table_rows(out_of(capsys))[idname]
    assert row[0] == str(golden["trade_count"]) and row[1] == str(golden["wins"])
    assert (row[3], row[4], row[7], row[8]) == (
        money2(golden["net"]), money2(golden["commission"]), money2(golden["max_drawdown"]),
        money2(golden["final_equity"]))
    # derived figures agree with the golden's trades
    trades = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())["trades"]
    nets = [D(t["net"]) for t in trades]
    assert acct["total_win"] == format(sum((n for n in nets if n > 0), D("0")).quantize(
        D("0.0001")), "f")
    assert acct["losses"] == sum(1 for n in nets if n < 0)
    assert acct["wins"] + acct["losses"] + acct["break_even"] == acct["trade_count"]


def test_legacy_run_report(tmp_path_factory, capsys):
    full, database = full_store_run(tmp_path_factory, "legacy")
    capsys.readouterr()
    assert main(["report", "1", "--db", str(database)]) == 0
    text = out_of(capsys)
    assert "fills: legacy" in text.splitlines()
    assert "fill model: legacy model (bar-close fills, no commission)" in text
    assert "capital per algo" not in text and "costs:" not in text
    rows = table_rows(text)
    assert all(r[4] == "0.00" for r in rows.values())  # no commission
    assert rows["TOTAL"][0] == str(sum(int(r[0]) for n, r in rows.items() if n != "TOTAL"))
    assert main(["report", "1", "--db", str(database), "--json"]) == 0
    document = json.loads(out_of(capsys))
    assert document["run"]["fills"] == "legacy"
    assert document["run"]["capital"] is None and document["run"]["costs"] is None
    for acct in document["accounts"].values():
        assert acct["commission"] == "0.0000" and acct["cancelled_entries"] == 0
        assert acct["capital"] == "10000.0000"


# ---- errors ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [["runs"], ["report", "1"]])
def test_missing_db_option_exit_2(capsys, command):
    assert main(command) == 2
    assert error_of(capsys) == "error: --db is required: give the database file (or sqlite:///path)"


@pytest.mark.parametrize("spec_of", [str, lambda p: "sqlite:///" + str(p)])
@pytest.mark.parametrize("command", [["runs"], ["report", "1"]])
def test_missing_database_is_an_error_and_is_not_created(tmp_path, capsys, command, spec_of):
    missing = tmp_path / "new_folder" / "runs.db"
    assert main([*command, "--db", spec_of(missing)]) == 2
    assert "does not exist" in error_of(capsys)
    assert not missing.exists() and not missing.parent.exists()
    assert not list(tmp_path.rglob("*.db*"))


def test_report_unknown_run_exit_2(db, capsys):
    make_run(db, algos=HAND_ALGOS)
    before = db.stat().st_size
    for text in ("2", "0", "99999999999999999999999"):
        assert main(["report", text, "--db", str(db)]) == 2
        assert error_of(capsys).startswith("error: no run ")
    assert db.stat().st_size == before


@pytest.mark.parametrize("text", ["abc", "1.5", "-1", "+1", "", " 1", "1 ", "١", "1e3"])
def test_report_run_id_must_be_a_whole_number(db, capsys, text):
    make_run(db, algos=HAND_ALGOS)
    assert main(["report", text, "--db", str(db)]) == 2
    assert error_of(capsys).startswith("error: run id must be a whole number")


def test_memory_database_refused(capsys):
    for spec in (":memory:", "sqlite:///:memory:"):
        assert main(["runs", "--db", spec]) == 2
        assert "holds nothing to read" in error_of(capsys)


def test_not_a_database_and_empty_file_and_directory(tmp_path, capsys):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a sqlite database at all " * 20)
    assert main(["runs", "--db", str(junk)]) == 2
    error_of(capsys)
    assert junk.read_bytes().startswith(b"this is not")
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert main(["runs", "--db", str(empty)]) == 2
    assert "is empty" in error_of(capsys)
    assert empty.stat().st_size == 0  # reading did not turn it into a database
    assert main(["report", "1", "--db", str(tmp_path)]) == 2
    assert "is not a file" in error_of(capsys)


def test_newer_schema_refused_and_left_untouched(db, capsys):
    make_run(db, algos=HAND_ALGOS)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    before = db.read_bytes()
    assert main(["runs", "--db", str(db)]) == 2
    assert "newer than this code" in error_of(capsys)
    assert main(["report", "1", "--db", str(db)]) == 2
    error_of(capsys)
    assert db.read_bytes() == before


def test_unsupported_database_scheme(capsys):
    assert main(["runs", "--db", "postgres://host/db"]) == 2
    assert "unsupported database" in error_of(capsys)


@pytest.mark.parametrize("command", [["runs"], ["report", "1"]])
def test_onedrive_folder_refused_with_injected_environment(tmp_path, capsys, command):
    synced = tmp_path / "OneDrive - Someone" / "runs.db"
    synced.parent.mkdir()
    conn = sqlite3.connect(synced)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    assert main([*command, "--db", str(synced)], env={}) == 2
    assert "OneDrive" in error_of(capsys)
    # by environment variable only, for a folder that is not named like OneDrive
    plain = tmp_path / "cloud" / "runs.db"
    plain.parent.mkdir()
    plain.write_bytes(synced.read_bytes())
    assert main([*command, "--db", str(plain)], env={"OneDrive": str(tmp_path / "cloud")}) == 2
    assert "OneDrive" in error_of(capsys)


@pytest.mark.parametrize("command", [["runs"], ["report", "1"]])
def test_store_error_while_reading_is_one_line(db, capsys, monkeypatch, command):
    make_run(db, algos=HAND_ALGOS)
    from MainStoinker.engine.store import StoreError

    def boom(self, *args):
        raise StoreError("the database is locked")

    monkeypatch.setattr(SqliteStore, "load_run", boom)
    monkeypatch.setattr(SqliteStore, "list_runs", boom)
    assert main([*command, "--db", str(db)]) == 2
    assert error_of(capsys) == "error: the database is locked"


def test_sqlite_error_while_reading_is_one_line(db, capsys, monkeypatch):
    make_run(db, algos=HAND_ALGOS)

    def boom(self, *args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SqliteStore, "load_run", boom)
    assert main(["report", "1", "--db", str(db)]) == 2
    assert error_of(capsys) == "error: database error: disk I/O error"


# ---- reading while another process writes; no side effects -----------------------------


def test_readable_while_a_writer_holds_a_transaction(env, db, capsys):
    env.setup(ema_config(), ["AAPL"], rows=600)
    assert backtest(env, db, "--fills", "realistic") == 0
    capsys.readouterr()
    writer = sqlite3.connect(db, isolation_level=None, timeout=0.1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO runs (started_at, status, kind, fills, config_json, data_json,"
                       " code_version) VALUES ('2030-01-01T00:00:00+00:00', 'running', 'x', 'legacy',"
                       " '{}', '{}', '')")
        # the uncommitted run is invisible; the committed one reads fine, with no wait
        assert main(["runs", "--db", str(db)]) == 0
        assert len(out_of(capsys).splitlines()) == 3
        assert main(["report", "1", "--db", str(db)]) == 0
        assert "TOTAL" in out_of(capsys)
        assert main(["report", "2", "--db", str(db)]) == 2
        assert error_of(capsys) == "error: no run 2"
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_runs_and_report_need_no_git_algos_data_or_ledger(db, tmp_path, capsys, monkeypatch):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY,
             directory=str(tmp_path / "gone" / "OfflineData"))

    def forbidden(*args, **kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr(cli, "current_code_version", forbidden)
    monkeypatch.setattr(codeversion, "current_code_version", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "load_algo", forbidden)
    monkeypatch.setattr(cli, "check_algos_importable", forbidden)
    monkeypatch.setattr(cli, "CsvBarFeed", forbidden)
    monkeypatch.setattr(cli, "Ledger", forbidden)
    monkeypatch.setattr(cli, "build_router", forbidden)
    monkeypatch.chdir(tmp_path)
    for command in (["runs"], ["runs", "--json"], ["report", "1"], ["report", "1", "--json"]):
        assert main([*command, "--db", str(db)]) == 0
        assert out_of(capsys)
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == []  # no files written


def test_cli_report_imports_no_algo_or_legacy_module():
    source = Path(cli_report.__file__).read_text(encoding="utf-8")
    imports = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
    assert not any(w in ln for ln in imports for w in ("algos", "feed", "codeversion", "Globals",
                                                       "apiApi", "ibapi", "subprocess"))


def test_no_database_file_in_the_repository():
    found = []
    for _folder, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "__pycache__", ".pytest_cache"}]
        found += [n for n in names
                  if n.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal"))]
    assert found == []


def test_run_id_arguments_are_read_without_a_database_round_trip(db, capsys):
    """`report` of an id beyond 64 bits is an unknown run, not an OverflowError traceback."""
    make_run(db, algos=HAND_ALGOS)
    assert main(["report", str(2**64), "--db", str(db)]) == 2
    assert error_of(capsys) == f"error: no run {2**64}"



def test_non_ascii_idname_does_not_crash_a_cp1252_console(db, monkeypatch, capsys):
    name = "caf\u00e9-\u65e5\u672c"
    algos = [AlgoMeta(name, "Algo", "AAPL", "backtest", D("1000"))]
    make_run(db, algos=algos, trips=[trip(name, "3.0000")],
             equity=[point(name, "1003"), point(None, "1003")])
    real = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
    monkeypatch.setattr("sys.stdout", real)
    assert main(["report", "1", "--db", str(db)]) == 0
    assert main(["runs", "--db", str(db)]) == 0
    text = real.buffer.getvalue().decode("cp1252")
    assert "caf\u00e9-\\u65e5\\u672c" in text  # what cp1252 cannot show is escaped, not a traceback
    assert main(["report", "1", "--json", "--db", str(db)]) == 0
    document = real.buffer.getvalue().decode("cp1252").split("\n{", 1)[1]
    assert document.isascii() and name in json.loads("{" + document)["accounts"]


def test_repeated_invocations_in_one_process_leak_nothing(db, tmp_path, capsys):
    other = tmp_path / "other" / "runs.db"
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    make_run(other, algos=HAND_ALGOS, trips=[trip("X-1", "-9")],
             equity=[point("X-1", "991"), point(None, "991")])
    seen = {}
    for _ in range(2):
        for name, path in (("a", db), ("b", other)):
            assert main(["report", "1", "--db", str(path)]) == 0
            assert seen.setdefault(("report", name), out_of(capsys)) == seen[("report", name)]
            assert main(["runs", "--json", "--db", str(path)]) == 0
            assert seen.setdefault(("runs", name), out_of(capsys)) == seen[("runs", name)]
    assert seen[("report", "a")] != seen[("report", "b")]
    assert json.loads(seen[("runs", "a")])["runs"][0]["net"] == "9.8450"
    assert json.loads(seen[("runs", "b")])["runs"][0]["net"] == "-9.0000"


def test_runs_and_report_import_no_algo_broker_or_legacy_module(db):
    make_run(db, algos=HAND_ALGOS, trips=HAND_TRIPS, equity=HAND_EQUITY)
    banned = "('MainStoinker.Algos', 'MainStoinker.DataCollection', 'MainStoinker.TradeTools', " \
             "'MainStoinker.MainStuff', 'ibapi')"
    code = ("import sys\nfrom MainStoinker.engine.cli import main\n"
            "assert main(['runs', '--db', sys.argv[1]]) == 0\n"
            "assert main(['report', '1', '--db', sys.argv[1]]) == 0\n"
            f"bad = [m for m in sys.modules if m.startswith({banned})]\n"
            "print(bad)\nsys.exit(1 if bad else 0)\n")
    result = subprocess.run([sys.executable, "-c", code, str(db)], cwd=db.parent,
                            env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("command", [["runs", "extra"], ["report", "1", "extra"], ["report"]])
def test_extra_or_missing_arguments_are_a_usage_error(db, command):
    make_run(db, algos=HAND_ALGOS)
    with pytest.raises(SystemExit) as stop:
        main([*command, "--db", str(db)])
    assert stop.value.code == 2
