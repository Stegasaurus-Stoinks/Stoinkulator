# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""Gaps found by the tester of the missing-bars follow-ups (issue 144, PR 170).

The end-of-data stamp of an early-ending symbol through the real command (CLI, store, report),
the new run fields through the API and a run stored before they existed, the run notes of the
overview with node (absent and hostile fields), and feed edge cases: repeated timestamps, a gap
across a day boundary, daylight-saving dates, shifted windows and very large counts.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.engine.cli import main
from MainStoinker.engine.store import DataMeta, RunMeta, open_store
from tests.api_support import make_client
from tests.run_support import EMA, Env
from tests.test_api_static import FAKE_DOM, run_node
from tests.test_api_static import node_workdir as node_workdir  # noqa: F401  (fixture)
from tests.test_cli_modes import EMA_MSFT
from tests.test_missing_bars_followups import day_minutes, feed_of

VERSION = "0123456789abcdef0123456789abcdef01234567"
CONFIG = [{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    return Env(tmp_path, monkeypatch)


# ------------------------------------------------ item 1 through the real command


def test_a_symbol_ending_early_is_closed_at_its_own_last_bar_end_to_end(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """MSFT ends 100 minutes before AAPL with a trade open: closed at its own last bar and close."""
    env.setup(CONFIG, ["AAPL", "MSFT"], rows=1950)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame = frame.iloc[:-100]
    frame.to_csv(path)
    own_last = pd.Timestamp(frame["date"].iloc[-1])
    own_close = float(frame["close"].iloc[-1])
    run_last = pd.Timestamp(pd.read_csv(env.data / "_AAPL_offlinedata_", index_col=0)["date"].iloc[-1])
    assert own_last < run_last
    db = tmp_path / "store" / "runs.db"
    assert env.run("--fills", "realistic", "--db", str(db)) == 0
    store = open_store(str(db), env={})
    try:
        run = store.load_run(1)
    finally:
        store.close()
    synthetic = [t for t in run.round_trips if t.exit_reason == "end_of_data"]
    assert [t.account for t in synthetic] == ["MSFT-10/30EMA"]
    (trip,) = synthetic
    assert trip.exit_time == own_last.to_pydatetime()
    assert float(trip.exit_price) == own_close
    assert trip.duration == trip.exit_time - trip.entry_time
    # the equity series still run to the end of the data
    assert max(p.time for p in run.equity) == run_last.to_pydatetime()
    capsys.readouterr()
    assert main(["report", "1", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "missing bars: MSFT 100 of 1560 minutes" in out.splitlines()
    assert "1 synthetic end-of-data closes" in out


# ----------------------------------------------- API and runs stored by older code


def test_the_api_carries_the_new_fields_and_an_older_row_shows_none(
    env: Env, tmp_path: Path
) -> None:
    env.setup(CONFIG, ["AAPL", "MSFT"], rows=700)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[[390, 400, 401]]).to_csv(path)
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    run = make_client(db).get("/api/runs/1").json()["run"]
    assert run["missing_minutes"] == {"AAPL": 0, "MSFT": 3}
    assert run["replayed_minutes"] == 310 and run["uncovered_minutes"] == 0
    assert run["data_warnings"] == []
    with sqlite3.connect(db) as con:  # the row as the code before this change wrote it
        con.execute("UPDATE runs SET data_json=json_remove(data_json, '$.replayed_minutes',"
                    " '$.missing_minutes', '$.uncovered_minutes', '$.warnings')")
    con.close()
    response = make_client(db).get("/api/runs/1")
    assert response.status_code == 200
    old = response.json()["run"]
    assert old["missing_minutes"] is None and old["replayed_minutes"] is None
    assert old["uncovered_minutes"] is None and old["data_warnings"] == []
    listing = make_client(db).get("/api/runs")
    assert listing.status_code == 200


def test_report_survives_very_large_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "runs.db"
    store = open_store(str(db), env={})
    try:
        meta = DataMeta(source="csv", symbols=("AAA", "BBB"), replayed_minutes=987_654_321,
                        missing_minutes=(("AAA", 0), ("BBB", 123_456_789)), uncovered_minutes=10**12,
                        warnings=tuple(f"warning number {i}" for i in range(50)))
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=meta))
        store.end_run(run_id, "completed")
    finally:
        store.close()
    assert main(["report", "1", "--db", str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "missing bars: BBB 123456789 of 987654321 minutes" in lines
    assert f"minutes with no bar for any symbol: {10**12}" in lines
    assert sum(line.startswith("data warning: ") for line in lines) == 50
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)["run"]
    assert doc["uncovered_minutes"] == 10**12 and len(doc["data_warnings"]) == 50


# ------------------------------------------------------------ overview notes (node)


def test_overview_notes_cope_with_absent_null_and_hostile_fields(node_workdir: Path) -> None:  # noqa: F811
    out = run_node(node_workdir, "overview_gap_notes", FAKE_DOM + """
const { runNotes } = await import("./js/views/overview.js");
const total = { synthetic_closes: 0, cancelled_entries: 0, ignored_events: {} };
const hostile = "<img src=x onerror=alert(1)> & \\"quoted\\"";
const cases = {
  noRun: runNotes({ total }),
  emptyRun: runNotes({ run: {}, total }),
  nulls: runNotes({ run: { missing_minutes: null, replayed_minutes: null, uncovered_minutes: null, data_warnings: null }, total }),
  oldRun: runNotes({ run: { replayed_minutes: null, missing_minutes: null, uncovered_minutes: null, data_warnings: [] }, total }),
  zeros: runNotes({ run: { replayed_minutes: 10, missing_minutes: { AAPL: 0, MSFT: 0 }, uncovered_minutes: 0, data_warnings: [] }, total }),
  junk: runNotes({ run: { missing_minutes: "x", uncovered_minutes: "abc", data_warnings: "not a list" }, total }),
  real: runNotes({ run: { replayed_minutes: 310, missing_minutes: { AAPL: 0, MSFT: 5 }, uncovered_minutes: 1, data_warnings: ["a", hostile] }, total }),
  nullDoc: runNotes(null),
};
console.log(JSON.stringify(cases));
""")
    assert isinstance(out, dict)
    for name in ("noRun", "emptyRun", "nulls", "oldRun", "zeros", "junk", "nullDoc"):
        assert out[name] == [], name
    assert out["real"][0] == "Missing bars (skipped for those minutes): MSFT 5 of 310 minutes."
    assert out["real"][1] == "1 minute with no bar for any symbol."
    assert out["real"][2] == "Data warning: a."
    assert "onerror" in out["real"][3]  # kept as text; the page adds it as a text node, never as markup


def test_overview_adds_notes_as_text_nodes_only() -> None:
    script = (Path(cli.__file__).resolve().parent.parent / "api" / "static" / "js" / "views"
              / "overview.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert 'notes.map((note) => h("li", {}, note))' in script


# ------------------------------------------------------------------ feed edge cases


def test_repeated_timestamps_are_neither_uncovered_nor_shared_twice(tmp_path: Path) -> None:
    a = day_minutes("2024-01-04", "09:30", 3)
    feed = feed_of(tmp_path, AAA=[a[0], a[0], a[1], a[2]], BBB=[a[0], a[1], a[2]])
    assert feed.replay_counts() == {"AAA": 4, "BBB": 3}
    assert feed.missing_counts() == {"AAA": 0, "BBB": 1}
    assert feed.uncovered_minutes() == 0
    assert feed.shared_count() == 3
    assert feed.coverage_warnings() == []


def test_a_gap_across_a_day_boundary_is_not_uncovered(tmp_path: Path) -> None:
    stamps = day_minutes("2024-01-04", "15:55", 5) + day_minutes("2024-01-08", "09:30", 5)
    feed = feed_of(tmp_path, AAA=stamps, BBB=stamps)
    assert feed.uncovered_minutes() == 0 and feed.coverage_warnings() == []


def test_a_whole_missing_day_in_one_symbol_is_a_missing_count_not_a_warning(tmp_path: Path) -> None:
    long = (day_minutes("2024-01-04", "09:30", 5) + day_minutes("2024-01-05", "09:30", 5)
            + day_minutes("2024-01-08", "09:30", 5))
    short = [s for s in long if s.day != 5]
    feed = feed_of(tmp_path, AAA=long, BBB=short)
    assert feed.missing_counts() == {"AAA": 0, "BBB": 5}
    assert feed.coverage_warnings() == []  # starts and ends on the same days


@pytest.mark.parametrize("day", ["2024-03-10", "2024-11-03"])  # US daylight-saving changes
def test_daylight_saving_dates_count_like_any_other(tmp_path: Path, day: str) -> None:
    stamps = [s for s in day_minutes(day, "09:30", 10) if s.minute != 32]
    feed = feed_of(tmp_path, AAA=stamps, BBB=stamps)
    assert feed.uncovered_minutes() == 1
    assert feed.coverage_warnings() == ["1 minutes inside replayed days have no bar for any symbol"]


def test_windows_shifted_by_a_day_warn_at_both_ends_and_still_share(tmp_path: Path) -> None:
    days = ["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
    first = [s for d in days[:3] for s in day_minutes(d, "09:30", 4)]
    second = [s for d in days[1:] for s in day_minutes(d, "09:30", 4)]
    feed = feed_of(tmp_path, AAA=first, BBB=second)
    assert feed.shared_count() == 8
    warnings = feed.coverage_warnings()
    assert len(warnings) == 2
    assert warnings[0].startswith("AAA: replay data ends 2024-01-08")  # symbol order
    assert warnings[1].startswith("BBB: replay data starts 2024-01-05")


def test_single_bar_symbols_do_not_trip_the_checks(tmp_path: Path) -> None:
    one = day_minutes("2024-01-04", "09:30", 1)
    feed = feed_of(tmp_path, AAA=one, BBB=one)
    assert feed.uncovered_minutes() == 0 and feed.shared_count() == 1
    assert feed.coverage_warnings() == []
