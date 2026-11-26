"""`backtest --start` and the warm-up an algo declares (issue 286).

Two halves: the declaration (`warmup_sessions()` of the parent algo and of FVGZone, the
calendar walk back from the start date) and the command (`--start/--end/--allow-cold`:
the warm-up reaches the algos through the engine's `_begin`, the same seeding a live
session's warm-up frames use, and never produces an order, a trade, a mark or a statistic).
Nothing here opens a socket or reads the user's folders; data is the committed offline
bars or synthetic month files under `tmp_path`.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.Algos.ParentAlgo import ParentAlgo
from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.cli import main
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import COLUMNS, CsvBarFeed, month_file_path, write_month_file
from MainStoinker.engine.sink import NullSink
from MainStoinker.engine.store import open_store
from MainStoinker.engine.warmup import MAX_WARMUP_SESSIONS, declared_sessions, first_session_needed
from tests.run_support import EMA, Env

VERSION = "0123456789abcdef0123456789abcdef01234567"
FVG_ID = {"idname": "x", "ticker": "AAPL"}


def fvg(**settings: object):
    return load_algo("FVGZone", {**FVG_ID, **settings})


# ----- what an algo declares -----


def test_the_parent_algo_needs_no_history():
    assert ParentAlgo({"idname": "p", "ticker": "AAPL"}).warmup_sessions() == 0
    # an algo that does not override it, and EMACrossing, whose need is plain: `long` bars
    assert load_algo("FunctionTest", {"idname": "f", "ticker": "AAPL", "RRRatio": 3}
                     ).warmup_sessions() == 0
    assert load_algo("EMACrossing", EMA).warmup_sessions() == 1
    assert load_algo("EMACrossing", {**EMA, "long": "391"}).warmup_sessions() == 2


@pytest.mark.parametrize(("settings", "expected"), [
    # defaults: 20 zone bars of 5 minutes (100 minutes) and a 50-bar EMA: one session
    ({}, 1),
    # htf 30 x 20: 20 completed 30-minute buckets, 13 per session -> 2 sessions
    ({"htf_minutes": 30}, 2),
    # volume by slot 20 sessions + 1 margin (half day), trend-day ADR 20 sessions
    ({"vol_slot_mult": 1.5, "trend_day_adr": 0.3}, 21),
    # each part on its own
    ({"vol_slot_mult": 1.5}, 21),
    ({"trend_day_adr": 0.3}, 20),
    ({"trend_day_adr": 0.3, "adr_sessions": 5, "vol_slot_mult": 2, "vol_slot_sessions": 7}, 8),
    # the EMA of 1-minute closes: ceil(trend_ema / 390), only with the trend filter
    ({"trend_ema": 390}, 1),
    ({"trend_ema": 391}, 2),
    ({"trend_ema": 2000, "trend_filter": False}, 1),
    # zone bars: 20 bars of 60 minutes = 1200 minutes -> 4 sessions
    ({"zone_minutes": 60}, 4),
    # one higher-timeframe bar per session when htf_minutes is 390: htf_ema sessions
    ({"htf_minutes": 390, "htf_ema": 3}, 3),
    # 45-minute buckets: 8 whole buckets fit a session; 20 of them -> 3 sessions
    ({"htf_minutes": 45}, 3),
])
def test_fvgzone_declares_the_largest_need_of_the_settings_that_are_on(settings, expected):
    assert fvg(**settings).warmup_sessions() == expected


def test_declared_sessions_checks_the_answer():
    class Bad:
        name = "bad"

        def warmup_sessions(self):
            return -1

    class Silent:
        name = "silent"

    assert declared_sessions([Silent()]) == [("silent", 0)]
    assert declared_sessions([fvg(), Silent()]) == [("x", 1), ("silent", 0)]
    with pytest.raises(ValueError, match="bad.*warmup_sessions"):
        declared_sessions([Bad()])
    Bad.warmup_sessions = lambda self: MAX_WARMUP_SESSIONS + 1  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="warmup_sessions"):
        declared_sessions([Bad()])
    Bad.warmup_sessions = lambda self: True  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="warmup_sessions"):
        declared_sessions([Bad()])


def test_first_session_needed_counts_trading_days_by_the_calendar():
    assert first_session_needed(date(2024, 8, 20), 0) == date(2024, 8, 20)
    assert first_session_needed(date(2024, 8, 20), 1) == date(2024, 8, 19)
    # a Monday: the weekend is skipped
    assert first_session_needed(date(2024, 8, 19), 2) == date(2024, 8, 15)
    # Labor Day (Monday 2024-09-02) is skipped; the start may itself be a closed day
    assert first_session_needed(date(2024, 9, 3), 1) == date(2024, 8, 30)
    assert first_session_needed(date(2024, 9, 2), 1) == date(2024, 8, 30)
    assert first_session_needed(date(2024, 9, 7), 1) == date(2024, 9, 6)  # a Saturday
    with pytest.raises(ValueError, match="sessions"):
        first_session_needed(date(2024, 9, 3), MAX_WARMUP_SESSIONS + 1)
    with pytest.raises(TypeError):
        first_session_needed(datetime(2024, 9, 3), 1)  # type: ignore[arg-type]


# ----- the feed -----


def synthetic(directory: Path, symbol: str = "AAPL", first: date = date(2024, 8, 1),
              last: date = date(2024, 10, 31), bars: int = 40, skip: tuple[date, ...] = ()) -> None:
    rows = []
    day = first
    while day <= last:
        if day.weekday() < 5 and day not in skip:
            start = datetime(day.year, day.month, day.day, 9, 30)
            for i in range(bars):
                stamp = start + timedelta(minutes=i)
                price = 100.0 + (stamp - datetime(2024, 8, 1)).days * 0.1 + (i % 7) * 0.2
                rows.append((stamp, int(stamp.timestamp()), price, price + 0.3, price - 0.3,
                             price + 0.1, 1000 + i))
        day += timedelta(days=1)
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    for (year, month), part in frame.groupby([frame["date"].dt.year, frame["date"].dt.month]):
        write_month_file(month_file_path(directory, symbol, int(year), int(month)),
                         part.reset_index(drop=True))


def days_of(frame: pd.DataFrame) -> list[date]:
    return sorted({stamp.date() for stamp in frame["date"]})


def test_feed_warmup_from_a_day_and_an_open_ended_window(tmp_path):
    synthetic(tmp_path)
    feed = CsvBarFeed(tmp_path, ["AAPL"], window=(date(2024, 9, 17), None),
                      warmup_from=date(2024, 9, 12))
    assert days_of(feed.warmup("AAPL")) == [date(2024, 9, 12), date(2024, 9, 13),
                                            date(2024, 9, 16)]
    replayed = sorted({stamp.date() for stamp, _ in feed})
    assert replayed[0] == date(2024, 9, 17) and replayed[-1] == date(2024, 10, 31)
    assert feed.first_day("AAPL") == date(2024, 8, 1)
    # nothing to warm up with: warm-up from the start day itself is empty
    assert CsvBarFeed(tmp_path, ["AAPL"], window=(date(2024, 9, 17), date(2024, 9, 17)),
                      warmup_from=date(2024, 9, 17)).warmup("AAPL").empty
    with pytest.raises(ValueError, match="needs a window"):
        CsvBarFeed(tmp_path, ["AAPL"], warmup_from=date(2024, 9, 12))
    with pytest.raises(KeyError):
        feed.first_day("MSFT")


# ----- warm-up bars can never trade (through the engine) -----


class EveryBar:
    """Tries to open a position on every bar it is updated with (through the engine's broker)."""

    def __init__(self) -> None:
        self.name = "every"
        self.ticker = "AAPL"
        self.broker: object = None
        self.updates: list[pd.Timestamp] = []
        self.frame_rows: list[int] = []

    def update(self, frame: pd.DataFrame) -> None:
        last = frame.iloc[-1]
        self.updates.append(last["date"])
        self.frame_rows.append(len(frame))
        self.broker.open_position(  # type: ignore[attr-defined]
            symbol="AAPL", volume=1, direction=1, price=float(last["close"]), time=last["date"],
            limit=False, stop_price=float(last["close"]) - 1.0, oca_group="g")

    def update_frontend(self) -> dict:
        return {}


def test_warmup_bars_never_reach_an_algo_or_its_broker(tmp_path):
    synthetic(tmp_path, first=date(2024, 9, 2), last=date(2024, 9, 13))
    feed = CsvBarFeed(tmp_path, ["AAPL"], window=(date(2024, 9, 10), date(2024, 9, 11)),
                      warmup_from=date(2024, 9, 5))
    warm = feed.warmup("AAPL")
    assert days_of(warm) == [date(2024, 9, 5), date(2024, 9, 6), date(2024, 9, 9)]
    broker = SimBroker()
    algo = EveryBar()
    Engine(EngineConfig(), feed, broker, NullSink(), [algo]).run_backtest()
    # exactly the replayed bars updated the algo, and every order is from a replayed bar
    assert len(algo.updates) == 80
    assert min(algo.updates) == pd.Timestamp("2024-09-10 09:30")
    assert max(algo.updates) == pd.Timestamp("2024-09-11 10:09")
    assert len(broker.calls) == 80 and all(c.method == "open_position" for c in broker.calls)
    assert all(c.args["time"] >= pd.Timestamp("2024-09-10") for c in broker.calls)
    # the algo does SEE the history: its first frame already holds the warm-up bars
    assert algo.frame_rows[0] == len(warm) + 1
    assert all(t >= pd.Timestamp("2024-09-10") for t in algo.updates)


def test_warmup_bars_are_in_no_mark_no_equity_and_no_trade(tmp_path, monkeypatch):
    """The same through the CLI with a ledger: nothing of the warm-up is stored as a result."""
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    env = Env(tmp_path, monkeypatch)
    synthetic(env.data)
    env.algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [EMA]}]), encoding="utf-8")
    db = str(tmp_path / "runs.db")
    assert env.run("--start", "2024-09-10", "--end", "2024-09-11", "--db", db) == 0
    store = open_store(db, env=os.environ)
    try:
        run = store.load_run(1)
    finally:
        store.close()
    assert run.summary.data.first_bar == datetime(2024, 9, 10, 9, 30)
    assert run.summary.data.replayed_minutes == 80
    assert all(point.time >= datetime(2024, 9, 10) for point in run.equity)
    assert all(trip.entry_time >= datetime(2024, 9, 10) for trip in run.round_trips)


# ----- the command -----


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """FVGZone defaults on the committed offline bars (2024-08-16 to 2024-08-26)."""
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    env = Env(tmp_path, monkeypatch)
    env.setup([{"ID": "FVGZone", "data": [{"idname": "fvg", "ticker": "AAPL"}]}], ["AAPL"],
              rows=None)
    return env


def trades_of(env: Env, out: str, *extra: str) -> list[dict]:
    path = env.out.parent / out
    assert main(["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(path),
                 "--quiet", *extra]) == 0
    return json.loads((path / "trades.json").read_text(encoding="utf-8"))["fvg"]


def shape(records: list[dict]) -> list[dict]:
    """The trades without their running number (it counts from the run's first trade)."""
    return [{k: v for k, v in r.items() if k != "id"} for r in records]


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_start_equals_the_full_run_cut_at_entries_from_the_start(tmp_path, monkeypatch, symbol):
    """A run with `--start D` trades exactly as the run over all the data does from D on.

    FVGZone defaults (declared need: 1 session). It holds when
    (1) the data before D covers the declared need, so the ATR window (20 zone bars) is
        full and the EMA of 1-minute closes (50 bars) is ready: a session is 390 bars, so
        the seed's weight in the EMA is (1 - 2/51)^390, about 1e-7, far below a cent;
    (2) every other piece of state is per session (zones, the session VWAP, used-up and
        dead zones) or a bar-count window (the ATR) that the warm-up session fills, and
        nothing is open across D: FVGZone ends every day flat at 15:55, so no position of
        the full run spans D, and the algo's own 'left from an earlier session' branch
        does not fire; and
    (3) the entries compared are the ones from D on (the trade id counts from a run's
        first trade, so it is left out).
    It does NOT hold for a filter whose memory is longer than its declared window: an
    htf EMA (alpha 2/21) is far from converged after the 2 declared sessions (a seed
    weight of 0.905 per bucket), and the same comparison with `htf_minutes` 30 does differ
    on this data (seen while writing the test); "ready" is a bar count, not convergence.
    """
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    env = Env(tmp_path, monkeypatch)
    env.setup([{"ID": "FVGZone", "data": [{"idname": "fvg", "ticker": symbol}]}], [symbol],
              rows=None)
    assert fvg().warmup_sessions() == 1
    full = trades_of(env, "full")
    assert len(full) >= 5  # the comparison has something in it
    compared = 0
    # 2024-08-16 is the first day of data, so the first start with a session before it is 08-19
    for start in ("2024-08-19", "2024-08-20", "2024-08-21", "2024-08-22", "2024-08-23",
                  "2024-08-26"):
        expected = [r for r in full if r["openTime"] >= f"{start} 00:00:00"]
        assert shape(trades_of(env, f"start-{start}", "--start", start)) == shape(expected)
        compared += len(expected)
    assert compared > 0


def test_start_with_an_end_stops_there(offline):
    both = shape(trades_of(offline, "a", "--start", "2024-08-21", "--end", "2024-08-22"))
    assert both and all("2024-08-21" <= r["openTime"][:10] <= "2024-08-22" for r in both)
    # the second day's trades are the same whether the run goes on or ends there
    longer = shape(trades_of(offline, "b", "--start", "2024-08-21"))
    assert longer[:len(both)] == both and len(longer) > len(both)


def run_args(env: Env, *extra: str) -> list[str]:
    return ["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
            "--quiet", *extra]


def refused(env: Env, capsys, *extra: str) -> str:
    """Run the command, expect exit code 2 (an input error), return what it said on stderr."""
    capsys.readouterr()
    assert main(run_args(env, *extra)) == 2
    return capsys.readouterr().err


def test_refused_when_the_bars_do_not_reach_back(offline, capsys):
    # defaults need 1 session; nothing is before 2024-08-16, so 2024-08-15 is the first day needed
    text = refused(offline, capsys, "--start", "2024-08-16")
    assert "needs 1 trading sessions of warm-up" in text and "fvg (1)" in text
    assert "beginning 2024-08-15" in text and "bars of AAPL begin 2024-08-16" in text
    assert "--allow-cold" in text
    assert not offline.out.exists()  # refused before anything was written
    # a larger need: htf 30 wants 2 sessions, only 1 exists before 2024-08-19 (a Monday)
    offline.algos.write_text(json.dumps(
        [{"ID": "FVGZone", "data": [{"idname": "fvg", "ticker": "AAPL", "htf_minutes": 30}]}]),
        encoding="utf-8")
    text = refused(offline, capsys, "--start", "2024-08-19")
    assert "needs 2 trading sessions" in text and "beginning 2024-08-15" in text
    assert "begin 2024-08-16 (1 of 2 sessions available)" in text


def test_allow_cold_starts_anyway_and_says_so(offline, capsys, tmp_path):
    db = str(tmp_path / "runs.db")
    assert main(run_args(offline, "--start", "2024-08-19", "--allow-cold", "--db", db)) == 0
    captured = capsys.readouterr()
    assert "cold start" not in captured.err + captured.out  # the data covers it: not cold
    assert "warm-up: 1 sessions needed (from 2024-08-16), 1 loaded" in captured.out
    # an actual shortfall: the very first day has nothing before it
    assert main(run_args(offline, "--start", "2024-08-16", "--allow-cold", "--db", db)) == 0
    captured = capsys.readouterr()
    assert "warning: cold start: --start 2024-08-16 needs 1 trading sessions" in captured.err
    assert "warm-up: 1 sessions needed (from 2024-08-15), 0 loaded, cold start" in captured.out
    store = open_store(db, env=os.environ)
    try:
        cold = store.load_run(2).config["start"]
        warm = store.load_run(1).config["start"]
    finally:
        store.close()
    assert cold["cold"] is True and cold["warmup_sessions_loaded"] == 0
    assert warm["cold"] is False and warm["warmup_sessions_loaded"] == 1


def test_the_stored_run_records_start_end_and_warmup_and_report_shows_them(offline, capsys,
                                                                           tmp_path):
    db = str(tmp_path / "runs.db")
    assert main(run_args(offline, "--start", "2024-08-21", "--end", "2024-08-23", "--db", db)) == 0
    assert main(run_args(offline, "--start", "2024-08-22", "--db", db)) == 0
    assert main(run_args(offline, "--db", db)) == 0  # no --start: nothing of it stored
    store = open_store(db, env=os.environ)
    try:
        first, second, third = (store.load_run(i) for i in (1, 2, 3))
    finally:
        store.close()
    assert first.config["start"] == {
        "from": "2024-08-21", "to": "2024-08-23", "warmup_sessions": 1,
        "warmup_first_day": "2024-08-20", "warmup_sessions_loaded": 1, "cold": False}
    assert second.config["start"]["to"] is None
    assert "warmup_days" not in first.config and "window" not in first.config
    assert "start" not in third.config and third.config["warmup_days"] == 2
    assert first.summary.data.first_bar == datetime(2024, 8, 21, 9, 30)
    capsys.readouterr()
    assert main(["report", "1", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "start: 2024-08-21, end: 2024-08-23" in out
    assert "warm-up: 1 sessions needed (from 2024-08-20), 1 loaded" in out
    assert main(["report", "2", "--db", db]) == 0
    assert "end: the end of the data" in capsys.readouterr().out
    assert main(["report", "1", "--db", db, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["start"] == first.config["start"]
    assert main(["report", "3", "--db", db, "--json"]) == 0
    assert "start" not in json.loads(capsys.readouterr().out)["run"]
    assert main(["report", "3", "--db", db]) == 0
    assert "warm-up:" not in capsys.readouterr().out


@pytest.mark.parametrize(("extra", "message"), [
    (["--end", "2024-08-22"], "--end needs --start"),
    (["--allow-cold"], "--allow-cold needs --start"),
    (["--start", "2024-08-21", "--replay-from", "2024-08-21"], "--start cannot be combined"),
    (["--start", "2024-08-21", "--replay-to", "2024-08-22"], "--start cannot be combined"),
    (["--start", "2024-08-21", "--warmup-days", "2"], "--start cannot be combined"),
    (["--start", "21.08.2024"], "--start must be a date as YYYY-MM-DD"),
    (["--start", "2024-08-21", "--end", "x"], "--end must be a date as YYYY-MM-DD"),
    (["--start", "2024-08-21", "--end", "2024-08-20"], "--end 2024-08-20 is before --start"),
    (["--start", "2030-01-01"], "no data for any symbol between 2030-01-01 and the end of the data"),
])
def test_start_argument_errors(offline, capsys, extra, message):
    assert message in refused(offline, capsys, *extra)
    assert not offline.out.exists()


def test_without_start_nothing_changes(offline, tmp_path):
    """No --start: the window, the warm-up and the stored config are the old ones."""
    default = trades_of(offline, "plain")
    again = trades_of(offline, "plain2", "--warmup-days", "2")
    assert default == again and default
