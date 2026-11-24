"""`backtest --replay-from/--replay-to`: replay a chosen New York day window.

Data is synthetic month files under `tmp_path` (weekdays of Aug to Oct 2024, 40
one-minute bars a day from 09:30), so a window can sit in the middle of several
months. Nothing here opens a socket or reads the user's folders.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.engine.cli import main
from MainStoinker.engine.feed import COLUMNS, CsvBarFeed, month_file_path, write_month_file
from MainStoinker.engine.store import open_store
from tests.run_support import EMA, Env

VERSION = "0123456789abcdef0123456789abcdef01234567"
BARS_PER_DAY = 40


def all_days() -> list[date]:
    day, days = date(2024, 8, 1), []
    while day <= date(2024, 10, 31):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def month_data(directory: Path, symbol: str = "AAPL", skip: tuple[date, ...] = ()) -> None:
    rows = []
    for day in all_days():
        if day in skip:
            continue
        start = datetime(day.year, day.month, day.day, 9, 30)
        for i in range(BARS_PER_DAY):
            stamp = start + timedelta(minutes=i)
            price = 100.0 + (stamp - datetime(2024, 8, 1)).days * 0.1 + (i % 7) * 0.2
            rows.append((stamp, int(stamp.timestamp()), price, price + 0.3, price - 0.3,
                         price + 0.1, 1000 + i))
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    for (year, month), part in frame.groupby([frame["date"].dt.year, frame["date"].dt.month]):
        write_month_file(month_file_path(directory, symbol, int(year), int(month)),
                         part.reset_index(drop=True))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    result = Env(tmp_path, monkeypatch)
    month_data(result.data)
    result.algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [EMA]}]), encoding="utf-8")
    return result


def days_of(frame: pd.DataFrame) -> list[date]:
    return sorted({stamp.date() for stamp in frame["date"]})


def replay_days(feed: CsvBarFeed) -> list[date]:
    return sorted({stamp.date() for stamp, _ in feed})


def test_window_selects_days_and_warmup_before(tmp_path, env):
    # Mid-September, three warm-up days across a weekend.
    feed = CsvBarFeed(env.data, ["AAPL"], 3, window=(date(2024, 9, 17), date(2024, 9, 18)))
    assert replay_days(feed) == [date(2024, 9, 17), date(2024, 9, 18)]
    assert feed.replay_counts() == {"AAPL": 2 * BARS_PER_DAY}
    assert days_of(feed.warmup("AAPL")) == [date(2024, 9, 12), date(2024, 9, 13),
                                            date(2024, 9, 16)]
    # across a weekend: Monday's warm-up is Thursday, Friday
    feed = CsvBarFeed(env.data, ["AAPL"], 2, window=(date(2024, 9, 16), date(2024, 9, 16)))
    assert days_of(feed.warmup("AAPL")) == [date(2024, 9, 12), date(2024, 9, 13)]
    assert replay_days(feed) == [date(2024, 9, 16)]
    # zero warm-up days, and more than the data has
    assert CsvBarFeed(env.data, ["AAPL"], 0, window=(date(2024, 9, 16),) * 2).warmup("AAPL").empty
    early = CsvBarFeed(env.data, ["AAPL"], 9, window=(date(2024, 8, 2), date(2024, 8, 2)))
    assert days_of(early.warmup("AAPL")) == [date(2024, 8, 1)]
    # a single day is the default of --replay-to; the month boundary is crossed for warm-up
    assert main(["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out",
                 str(env.out), "--quiet", "--warmup-days", "2", "--replay-from", "2024-10-01",
                 "--db", str(env.out.parent / "runs.db")]) == 0
    store = open_store(str(env.out.parent / "runs.db"), env=os.environ)
    try:
        run = store.load_run(1)
    finally:
        store.close()
    assert run.config["window"] == {"from": "2024-10-01", "to": "2024-10-01"}
    assert run.summary.data.first_bar == datetime(2024, 10, 1, 9, 30)
    assert run.summary.data.last_bar == datetime(2024, 10, 1, 10, 9)
    assert run.summary.data.replayed_minutes == BARS_PER_DAY


def test_window_is_shown_by_report(env, capsys):
    db = str(env.out.parent / "runs.db")
    assert env.run("--replay-from", "2024-09-10", "--replay-to", "2024-09-11", "--db", db) == 0
    capsys.readouterr()
    assert main(["report", "1", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "bars: 2024-09-10 09:30:00 to 2024-09-11 10:09:00" in out


def test_default_unchanged(env, tmp_path):
    db = str(tmp_path / "runs.db")
    assert env.run("--db", db) == 0
    store = open_store(db, env=os.environ)
    try:
        run = store.load_run(1)
    finally:
        store.close()
    assert "window" not in run.config
    first_day = date(2024, 8, 1)
    # the default warm-up is the first two calendar days, the rest is replayed
    assert run.summary.data.first_bar == datetime(2024, 8, 5, 9, 30)
    feed = CsvBarFeed(env.data, ["AAPL"])
    assert days_of(feed.warmup("AAPL")) == [first_day, date(2024, 8, 2)]
    assert replay_days(feed)[0] == date(2024, 8, 5)
    assert feed.replay_counts() == CsvBarFeed(env.data, ["AAPL"], 2, window=None).replay_counts()
    assert (env.out / "trades.json").is_file()


def test_window_outside_data_is_usage_error(env, tmp_path, capsys):
    db = tmp_path / "runs.db"
    for args in (["--replay-from", "2025-01-02"],
                 ["--replay-from", "2024-09-14", "--replay-to", "2024-09-15"],  # a weekend
                 ["--replay-from", "2024-07-01", "--replay-to", "2024-07-31"]):
        assert env.run(*args, "--db", str(db)) == 2
        err = capsys.readouterr().err
        assert err.startswith("error: no data for any symbol between")
    assert not db.exists() and not env.out.exists()


@pytest.mark.parametrize("args, message", [
    (["--replay-from", "2024-09-10", "--replay-to", "2024-09-09"], "is before --replay-from"),
    (["--replay-from", "2024/09/10"], "--replay-from must be a date as YYYY-MM-DD"),
    (["--replay-from", "20240910"], "--replay-from must be a date"),
    (["--replay-from", "2024-02-30"], "--replay-from must be a date"),
    (["--replay-from", "2024-09-10", "--replay-to", "tomorrow"], "--replay-to must be a date"),
    (["--replay-to", "2024-09-10"], "--replay-to needs --replay-from"),
])
def test_bad_window_is_usage_error_and_writes_nothing(env, tmp_path, capsys, args, message):
    db = tmp_path / "runs.db"
    assert env.run(*args, "--db", str(db)) == 2
    err = capsys.readouterr().err
    assert message in err and len(err.strip().splitlines()) == 1
    assert not db.exists() and not env.out.exists()


def test_window_in_the_middle_of_several_months_replays_only_those_days(env, tmp_path):
    db = str(tmp_path / "runs.db")
    assert env.run("--replay-from", "2024-09-30", "--replay-to", "2024-10-02",
                   "--warmup-days", "1", "--db", db) == 0
    store = open_store(db, env=os.environ)
    try:
        run = store.load_run(1)
    finally:
        store.close()
    data = run.summary.data
    assert data.replayed_minutes == 3 * BARS_PER_DAY
    assert (data.first_bar, data.last_bar) == (datetime(2024, 9, 30, 9, 30),
                                               datetime(2024, 10, 2, 10, 9))
    assert run.config["warmup_days"] == 1
    assert (env.out / "trades.json").is_file()


def test_a_symbol_without_data_in_the_window_is_still_refused(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    month_data(env.data, "AAPL")
    month_data(env.data, "MSFT", skip=(date(2024, 9, 17),))
    env.algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [
        EMA, {**EMA, "idname": "MSFT-10/30EMA", "ticker": "MSFT"}]}]), encoding="utf-8")
    assert env.run("--replay-from", "2024-09-17") == 2
    assert not env.out.exists()
