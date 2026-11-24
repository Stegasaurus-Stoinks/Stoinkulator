"""Follow-ups of the missing-bars work (issue 144).

1. an end-of-data close is stamped at the symbol's own last bar,
2. the run stores how many minutes each symbol missed, and `report` shows it,
3. a minute missing from every symbol, and stale or non-overlapping symbol
   files, are reported (and refused only when no minute is shared).

The algo assumptions about one-minute spacing are in
`tests/test_algos_minute_spacing.py` (xfail, strict).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.engine.cli import main
from MainStoinker.engine.feed import CsvBarFeed
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.store import DataMeta, RunMeta, open_store
from tests.test_cli_modes import EMA, EMA_MSFT, Env
from tests.test_engine_missing_bars import (
    ALL,
    Scripted,
    make_feed,
    minute,
    paper_engine,
    without,
)

VERSION = "0123456789abcdef0123456789abcdef01234567"
CONFIG = [{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}]


# ------------------------------------------------------------ 1. end-of-data stamp


def test_ledger_finish_stamps_a_symbol_at_its_own_time() -> None:
    ledger = Ledger()
    a = ledger.open_account("a", "AAA", 10_000)
    b = ledger.open_account("b", "BBB", 10_000)
    t0 = datetime(2024, 1, 4, 9, 30)
    a.fill("r", side="buy", quantity=1, price=10, commission=0, time=t0, reason="entry")
    b.fill("r", side="buy", quantity=1, price=20, commission=0, time=t0, reason="entry")
    end = datetime(2024, 1, 9, 15, 59)
    own = datetime(2024, 1, 5, 15, 59)
    ledger.finish(end, {"AAA": 11, "BBB": 21}, symbol_times={"AAA": own})
    trips = {t.account: t for t in ledger.round_trips}
    assert trips["a"].exit_time == own and trips["a"].duration == own - t0
    assert trips["b"].exit_time == end  # not listed: the run's last time
    assert ledger.equity_points[-1].time == end  # the final equity points stay at the end


def test_engine_stamps_an_early_ending_symbol_at_its_last_bar(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(7, 8, 9), BBB=ALL)
    ledger = Ledger()
    paper_engine(feed, [Scripted("a", "AAA", open_at=1), Scripted("b", "BBB", open_at=1)], ledger)
    trips = {t.account: t for t in ledger.round_trips}
    assert (trips["a"].exit_time, trips["a"].exit_price) == (minute(6), Decimal("106.5"))
    assert trips["a"].duration == minute(6) - trips["a"].entry_time
    assert trips["b"].exit_time == minute(9)


def test_aligned_data_calls_finish_exactly_as_before(tmp_path: Path) -> None:
    seen: list[dict] = []

    class Spy(Ledger):
        def finish(self, time, closes_by_symbol, commission=None, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(dict(kwargs))
            return super().finish(time, closes_by_symbol, commission, **kwargs)

    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3))
    paper_engine(feed, [Scripted("a", "AAA"), Scripted("b", "BBB")], Spy())
    assert seen == [{}]


# --------------------------------------------------------------- 3. feed coverage


def write_stamps(directory: Path, symbol: str, stamps: Sequence[pd.Timestamp]) -> None:
    """A symbol file: three warm-up bars on 2024-01-02, then `stamps` (replay bars)."""
    warm = [pd.Timestamp("2024-01-02 09:30") + pd.Timedelta(minutes=i) for i in range(3)]
    rows = ["x,date,time,open,high,low,close,volume"]
    for i, stamp in enumerate([*warm, *stamps]):
        rows.append(f"{i},{stamp},{int(stamp.timestamp())},10,11,9,10.5,100")
    (directory / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")


def day_minutes(day: str, start: str, count: int) -> list[pd.Timestamp]:
    first = pd.Timestamp(f"{day} {start}")
    return [first + pd.Timedelta(minutes=i) for i in range(count)]


def feed_of(tmp_path: Path, **stamps: Sequence[pd.Timestamp]) -> CsvBarFeed:
    for symbol, values in stamps.items():
        write_stamps(tmp_path, symbol, values)
    return CsvBarFeed(tmp_path, list(stamps), warmup_days=1)


def test_aligned_symbols_have_no_warnings(tmp_path: Path) -> None:
    both = day_minutes("2024-01-04", "09:30", 20) + day_minutes("2024-01-05", "09:30", 20)
    feed = feed_of(tmp_path, AAA=both, BBB=both)
    assert feed.coverage_warnings() == []
    assert feed.uncovered_minutes() == 0
    assert feed.shared_count() == 40


def test_a_minute_missing_from_every_symbol_is_counted_and_warned(tmp_path: Path) -> None:
    stamps = [s for s in day_minutes("2024-01-04", "09:30", 20)
              if s.minute not in (35, 36, 45)]  # 09:35 and 09:36 (run of two), 09:45
    feed = feed_of(tmp_path, AAA=stamps, BBB=stamps)
    assert feed.missing_counts() == {"AAA": 0, "BBB": 0}  # the union cannot see them
    assert feed.uncovered_minutes() == 3
    assert feed.coverage_warnings() == [
        "3 minutes inside replayed days have no bar for any symbol"]


def test_the_first_and_last_minute_of_a_day_are_not_judged(tmp_path: Path) -> None:
    # a late open and an early close are normal (half days): only inner gaps count
    stamps = day_minutes("2024-01-04", "09:33", 10) + day_minutes("2024-01-05", "09:30", 5)
    feed = feed_of(tmp_path, AAA=stamps)
    assert feed.uncovered_minutes() == 0


def test_a_gap_in_one_symbol_only_is_not_uncovered(tmp_path: Path) -> None:
    full = day_minutes("2024-01-04", "09:30", 20)
    feed = feed_of(tmp_path, AAA=full, BBB=[s for s in full if s.minute != 35])
    assert feed.missing_counts()["BBB"] == 1
    assert feed.uncovered_minutes() == 0


def test_a_symbol_ending_days_early_is_warned(tmp_path: Path) -> None:
    long = day_minutes("2024-01-04", "09:30", 10) + day_minutes("2024-01-08", "09:30", 10)
    short = day_minutes("2024-01-04", "09:30", 10)
    feed = feed_of(tmp_path, AAA=long, BBB=short)
    (warning,) = feed.coverage_warnings()
    assert warning == ("BBB: replay data ends 2024-01-04 09:39:00, but other symbols "
                       "run to 2024-01-08 09:39:00")
    assert feed.shared_count() == 10


def test_a_symbol_starting_days_late_is_warned(tmp_path: Path) -> None:
    long = day_minutes("2024-01-04", "09:30", 10) + day_minutes("2024-01-08", "09:30", 10)
    late = day_minutes("2024-01-08", "09:30", 10)
    feed = feed_of(tmp_path, AAA=long, BBB=late)
    (warning,) = feed.coverage_warnings()
    assert warning.startswith("BBB: replay data starts 2024-01-08 09:30:00, but other symbols")


def test_non_overlapping_symbols_are_warned_and_share_nothing(tmp_path: Path) -> None:
    feed = feed_of(tmp_path, AAA=day_minutes("2024-01-04", "09:30", 10),
                   BBB=day_minutes("2024-02-05", "09:30", 10))
    assert feed.shared_count() == 0
    assert "no minute has a bar for every symbol: the symbols' data do not overlap" \
        in feed.coverage_warnings()


def test_a_single_symbol_never_lacks_overlap(tmp_path: Path) -> None:
    feed = feed_of(tmp_path, AAA=day_minutes("2024-01-04", "09:30", 10))
    assert feed.coverage_warnings() == []


# ------------------------------------------------ the backtest command and the store


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    environment = Env(tmp_path, monkeypatch)
    environment.setup(CONFIG, ["AAPL", "MSFT"], rows=700)
    return environment


def drop_rows(env: Env, symbol: str, rows: Sequence[int]) -> None:
    path = env.data / f"_{symbol}_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[list(rows)]).to_csv(path)


def stored_data(db: Path) -> DataMeta:
    store = open_store(str(db), env={})
    try:
        return store.load_run(1).summary.data
    finally:
        store.close()


def test_the_run_stores_the_missing_counts(env: Env, tmp_path: Path) -> None:
    drop_rows(env, "MSFT", (390, 400, 401, 500, 699))
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    data = stored_data(db)
    assert data.replayed_minutes == 310
    assert data.missing_minutes == (("AAPL", 0), ("MSFT", 5))
    assert data.uncovered_minutes == 0 and data.warnings == ()


def test_an_aligned_run_stores_zeros_and_report_says_nothing(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    assert stored_data(db).missing_minutes == (("AAPL", 0), ("MSFT", 0))
    capsys.readouterr()
    assert main(["report", "1", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "missing bars" not in out and "data warning" not in out
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)["run"]
    assert run["missing_minutes"] == {"AAPL": 0, "MSFT": 0}
    assert run["replayed_minutes"] == 310 and run["uncovered_minutes"] == 0
    assert run["data_warnings"] == []


def test_report_shows_the_missing_counts(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    drop_rows(env, "MSFT", (390, 400, 401, 500, 699))
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    capsys.readouterr()
    assert main(["report", "1", "--db", str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "missing bars: MSFT 5 of 310 minutes" in lines
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)["run"]
    assert run["missing_minutes"] == {"AAPL": 0, "MSFT": 5} and run["replayed_minutes"] == 310


def test_a_run_stored_before_gaps_were_recorded_reads_back_as_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "runs.db"
    store = open_store(str(db), env={})
    try:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy",
                                         data=DataMeta(source="csv", symbols=("AAPL",))))
        store.end_run(run_id, "completed")
    finally:
        store.close()
    import sqlite3
    with sqlite3.connect(db) as con:  # a row as the earlier code wrote it
        con.execute("UPDATE runs SET data_json=json_remove(data_json, '$.replayed_minutes',"
                    " '$.missing_minutes', '$.uncovered_minutes', '$.warnings')")
    con.close()
    data = stored_data(db)
    assert (data.replayed_minutes, data.missing_minutes, data.uncovered_minutes, data.warnings) \
        == (None, None, None, ())
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)["run"]
    assert run["missing_minutes"] is None and run["data_warnings"] == []


def test_a_stale_file_runs_with_a_warning_on_stderr_and_in_the_store(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env.setup(CONFIG, ["AAPL", "MSFT"], rows=1300)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    last_day = pd.to_datetime(frame["date"]).dt.normalize().max()
    frame.loc[pd.to_datetime(frame["date"]) < last_day].to_csv(path)  # MSFT ends a day early
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    captured = capsys.readouterr()
    warnings = [line for line in captured.err.splitlines() if line.startswith("warning: MSFT")]
    assert len(warnings) == 1 and "replay data ends" in warnings[0]
    assert "warning" not in captured.out
    data = stored_data(db)
    assert len(data.warnings) == 1 and data.warnings[0].startswith("MSFT: replay data ends")
    assert main(["report", "1", "--db", str(db)]) == 0
    assert f"data warning: {data.warnings[0]}" in capsys.readouterr().out.splitlines()


def test_a_minute_missing_from_every_symbol_is_warned_by_the_command(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for symbol in ("AAPL", "MSFT"):
        drop_rows(env, symbol, (450, 451, 452))
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    err = capsys.readouterr().err
    assert "warning: 3 minutes inside replayed days have no bar for any symbol" in err
    assert stored_data(db).uncovered_minutes == 3


def test_files_that_share_no_minute_are_refused_and_store_nothing(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame["date"] = pd.to_datetime(frame["date"]) + pd.Timedelta(days=60)
    frame.to_csv(path)
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: no minute has a bar for every symbol (AAPL, MSFT)")
    assert not db.exists() and not env.out.exists()


def test_the_overview_note_text_is_in_the_dashboard_script() -> None:
    script = (Path(cli.__file__).resolve().parent.parent / "api" / "static" / "js" / "views"
              / "overview.js").read_text(encoding="utf-8")
    assert "Missing bars (skipped for those minutes)" in script
    assert "run.data_warnings" in script and "run.uncovered_minutes" in script


def test_the_store_roundtrips_the_new_data_fields(tmp_path: Path) -> None:
    store = open_store(str(tmp_path / "runs.db"), env={})
    try:
        meta = DataMeta(source="csv", symbols=("A", "B"), replayed_minutes=7,
                        missing_minutes=(("A", 0), ("B", 2)), uncovered_minutes=1,
                        warnings=("w one", "w two"))
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=meta))
        assert store.load_run(run_id).summary.data == meta
    finally:
        store.close()
