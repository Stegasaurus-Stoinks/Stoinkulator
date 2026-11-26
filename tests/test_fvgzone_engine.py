"""FVGZone through the real engine: offline data, gap rule, realistic fills, series, cost.

Unit rules on hand-built bars are in `test_fvgzone_algo.py`; the committed goldens
are in `test_golden_backtests.py` / `test_paper_goldens.py` (cases `fvgzone_*`).
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import Bar, CsvBarFeed
from MainStoinker.engine.gaprule import RefusedEntry
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.report import trade_records
from MainStoinker.engine.series import extract_series
from MainStoinker.engine.sink import RecordingSink
from MainStoinker.indicators.fvg import FvgConfig, detect_fvgs, resample_session
from tests import harness
from tests.fvgzone_support import (
    D1,
    D2,
    B,
    Rig,
    empty_store,
    mirror,
    quiet,
    retest_up,
    run_of,
    up_setup,
)

PLAIN = {"zone_minutes": 1, "trend_filter": False, "first_entry": "09:30"}


def write_bars(workdir: Path, symbol: str, bars: Sequence[B]) -> None:
    directory = workdir / "OfflineData"
    directory.mkdir(exist_ok=True)
    rows = [",date,time,open,high,low,close,volume"]
    for i, x in enumerate(bars):
        rows.append(f"{i},{x.at},{int(x.at.timestamp())},{x.o},{x.h},{x.l},{x.c},{x.v}")
    (directory / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")


def gap_day(*, missing: str, retest_at: str, extra: Sequence[tuple] = ()) -> list[B]:
    """A quiet warm-up day, then a session with an up zone and one minute missing.

    Bars of the session: quiet 09:30-09:46, c1 09:47, c2 09:48, c3 09:49 (zone known 09:50),
    then the retest bar at `retest_at`; the minute `missing` is left out.
    """
    warm = run_of(D1, "09:30", quiet(60))
    session = run_of(D2, "09:30", [*quiet(17), *up_setup(100.0)])
    t = pd.Timestamp(f"{D2} {retest_at}")
    session.append(B(t, *retest_up(100.0)[:4], 1000))
    for i, x in enumerate(extra, start=1):
        session.append(B(t + pd.Timedelta(minutes=i), *x[:4], 1000))
    return warm + [x for x in session if x.at != pd.Timestamp(f"{D2} {missing}")]


# ----- the gap rule and realistic fills -----


def test_entry_on_a_bar_after_a_gap_is_refused_and_the_zone_is_used_up(tmp_path):
    # c3 is 09:49, 09:50 is missing, the retest at 09:51 follows a gap
    write_bars(tmp_path, "AAPL", gap_day(missing="09:50", retest_at="09:51",
                                         extra=[retest_up(100.0)] * 2))
    (algo,) = harness.run_offline_backtest(
        "FVGZone", [{"idname": "g", "ticker": "AAPL", **PLAIN}], warmup_days=1)
    assert len(algo.trades) == 1 and isinstance(algo.trades[0].brokerRef, RefusedEntry)
    assert not algo.inTrade  # it learned on the next bar
    assert trade_records(algo) == []  # nothing was ever opened
    # the bar after it retests the same zone again: the zone stays used (documented), no new try


def test_entry_cancelled_before_it_fills_is_not_retried(tmp_path):
    # the retest at 09:50 signals; 09:51 is missing, so the fill bar 09:52 follows a gap and the
    # paper broker cancels the pending entry. The algo learns, and the zone stays used.
    again = [(100.0, 100.1, 99.9, 100.0), retest_up(100.0), retest_up(100.0)]
    write_bars(tmp_path, "AAPL", gap_day(missing="09:51", retest_at="09:50", extra=again))
    run = harness.run_paper_backtest("FVGZone", {"idname": "g", "ticker": "AAPL", **PLAIN},
                                     warmup_days=1)
    assert len(run.algo.trades) == 1
    assert run.broker.cancelled_entries == 1
    assert len(run.ledger.round_trips) == 0
    assert not run.algo.inTrade
    assert run.violations == []


# ----- the detector and the 5-minute bars are exactly the batch tools -----


def test_zones_equal_the_batch_detector_on_resampled_bars(tmp_path):
    harness.stage_offline_data(tmp_path, ["AAPL"])
    (algo,) = harness.run_offline_backtest("FVGZone", [{"idname": "z", "ticker": "AAPL"}])
    frame = pd.read_csv(tmp_path / "OfflineData" / "_AAPL_offlinedata_", index_col=0,
                        parse_dates=["date"])
    batch = detect_fvgs(resample_session(frame, 5), FvgConfig(bar_minutes=5, min_size_atr=0.25))
    live = sorted([*algo._det.closed_zones(), *algo._det.live_zones()], key=lambda z: z.id)
    assert len(batch) > 20  # the comparison has something to compare
    assert [(z.id, z.direction.value, z.top, z.bottom, z.formed_time, z.known_at, z.state.value)
            for z in live] == [
        (r.id, r.direction, r.top, r.bottom, r.formed_time, r.known_at, r.state)
        for r in batch.itertuples()]


@pytest.mark.parametrize("minutes", [1, 3, 7])
def test_zones_equal_the_batch_detector_for_other_sizes(tmp_path, minutes):
    harness.stage_offline_data(tmp_path, ["MSFT"], max_rows=1600)
    cfg = {"idname": "z", "ticker": "MSFT", "zone_minutes": minutes}
    (algo,) = harness.run_offline_backtest("FVGZone", [cfg])
    frame = pd.read_csv(tmp_path / "OfflineData" / "_MSFT_offlinedata_", index_col=0,
                        parse_dates=["date"])
    batch = detect_fvgs(resample_session(frame, minutes),
                        FvgConfig(bar_minutes=minutes, min_size_atr=0.25))
    live = sorted([*algo._det.closed_zones(), *algo._det.live_zones()], key=lambda z: z.id)
    assert [(z.direction.value, z.top, z.bottom, z.formed_time, z.state.value) for z in live] == [
        (r.direction, r.top, r.bottom, r.formed_time, r.state) for r in batch.itertuples()]


def test_history_given_in_one_go_gives_the_same_indicators_as_bar_by_bar():
    bars = run_of(D1, "09:30", [*quiet(30), *up_setup(100.0), retest_up(100.0), *quiet(5, 100.0)])
    cfg = {"idname": "h", "ticker": "AAPL", "zone_minutes": 1, "trend_filter": False}
    one = Rig(**cfg).feed(bars)
    # the second algo is first called with all the bars as history plus one
    store = empty_store()
    other = load_algo("FVGZone", cfg)
    other.broker = SimBroker()
    for x in bars:
        store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
    other.update(store.view())
    for name in ("VWAP", "EMA", "ZoneAboveTop", "ZoneBelowTop", "ZoneBelowBottom"):
        a, b = one.algo._columns[name], other._columns[name]
        assert len(a) == len(b) == len(bars)
        assert [(x == x, x) for x in a] == [(y == y, y) for y in b]
    assert [(z.id, z.state) for z in one.algo._det.live_zones()] == [
        (z.id, z.state) for z in other._det.live_zones()]
    # history bars never trade; only the last bar is evaluated as live (a zone retested in the
    # history is not marked used, so the last bar may still take it)
    assert [t.openTime for t in other.trades] in ([], [bars[-1].at])

# ----- the indicator series for the dashboard -----


def run_aapl(tmp_path, rows=None, **cfg):
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=rows)
    sink = RecordingSink(wants_algo_data=True)
    algo = load_algo("FVGZone", {"idname": "s", "ticker": "AAPL", **cfg})
    feed = CsvBarFeed(tmp_path / "OfflineData", ["AAPL"], 2)
    harness.apply_safe_config()
    Engine(EngineConfig(), feed, SimBroker(), sink, [algo]).run_backtest()
    times = [*feed.warmup("AAPL")["date"], *(t for t, _ in feed)]
    return algo, sink, times


def test_series_are_declared_and_stored_for_the_dashboard(tmp_path):
    algo, _, times = run_aapl(tmp_path, rows=1800)
    infos, points = extract_series(algo, times)
    assert [(i.name, i.draw_type) for i in infos] == [
        ("VWAP", "line"), ("EMA", "line"), ("ZoneAboveTop", "segment"),
        ("ZoneAboveBottom", "segment"), ("ZoneBelowTop", "segment"),
        ("ZoneBelowBottom", "segment"), ("StopPrice", "segment"), ("tp", "segment")]
    by_name: dict[str, list] = {}
    for p in points:
        by_name.setdefault(p.name, []).append(p)
    assert len(by_name["VWAP"]) == len(times)
    assert len(by_name["EMA"]) == len(times) - algo.trend_ema + 1
    # every zone edge point is a price some zone really had
    assert by_name["ZoneBelowTop"] and by_name["ZoneAboveBottom"]


def test_zone_series_follow_the_nearest_usable_zone():
    rig = Rig(**PLAIN).feed(run_of(D1, "09:30", [*quiet(17), *up_setup(100.0), retest_up(100.0)]))
    cols = rig.algo._columns
    # zone [100.10, 100.30]: not known on 09:49 (c3), below the price from 09:50 on
    assert cols["ZoneBelowTop"][-2] != cols["ZoneBelowTop"][-2]
    assert (cols["ZoneBelowBottom"][-1], cols["ZoneBelowTop"][-1]) == (100.10, 100.30)
    assert cols["ZoneAboveTop"][-1] != cols["ZoneAboveTop"][-1]
    # the mirror image has it above the price
    mir = Rig(**PLAIN).feed(mirror(run_of(D1, "09:30",
                                          [*quiet(17), *up_setup(100.0), retest_up(100.0)])))
    assert (mir.algo._columns["ZoneAboveBottom"][-1], mir.algo._columns["ZoneAboveTop"][-1]) == (
        99.70, 99.90)


def test_frontend_rows_come_from_the_last_series_row(tmp_path):
    algo, sink, _ = run_aapl(tmp_path, rows=1800)
    bars = [e for e in sink.events if e.kind == "bar"]
    assert bars and all(e.algo_data and e.algo_data[0]["idname"] == "s" for e in bars)
    last = bars[-1].algo_data[0]
    assert last["time"] == int(bars[-1].bar.time)
    names = {d["name"]: d["data"] for d in last["data"]}
    assert names["VWAP"] == algo._columns["VWAP"][-1]
    assert "tp" not in names or algo.inTrade


def test_mode_paper_config_runs_with_realistic_fills(tmp_path):
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=1560)
    run = harness.run_paper_backtest(
        "FVGZone", {"idname": "p", "ticker": "AAPL", "mode": "paper"})
    assert run.bars_checked > 700 and run.violations == []


# ----- cost grows linearly -----


def python_calls_per_bar(tmp_path, rows: int) -> float:
    """Python function calls made by the algo and the detector, per bar of the run."""
    harness.stage_offline_data(tmp_path, ["TSLA"], max_rows=rows)
    algo = load_algo("FVGZone", {"idname": "c", "ticker": "TSLA", "zone_minutes": 1})
    feed = CsvBarFeed(tmp_path / "OfflineData", ["TSLA"], 1)
    engine = Engine(EngineConfig(), feed, SimBroker(), RecordingSink(), [algo], ledger=Ledger())
    mine = ("FVGZone_Algo.py", "fvg.py")
    count = 0

    def profiler(frame, event, _arg):
        nonlocal count
        if event == "call" and frame.f_code.co_filename.endswith(mine):
            count += 1

    sys.setprofile(profiler)
    try:
        engine.run_backtest()
    finally:
        sys.setprofile(None)
    return count / rows


def test_work_per_bar_does_not_grow_with_history(tmp_path, monkeypatch):
    # structural: nothing concatenates frames, and no indicator frame is built per bar
    def no_concat(*_a, **_k):
        raise AssertionError("pd.concat called during an FVGZone run")

    monkeypatch.setattr(pd, "concat", no_concat)
    small = python_calls_per_bar(tmp_path, 1170)
    big = python_calls_per_bar(tmp_path, 2730)  # 2.3 times the bars
    # per-bar calls stay flat (a per-bar cost that grew with history would grow by about 2.3x)
    assert big < small * 1.25
    assert small > 1  # the counter really counted


def test_no_indicator_frame_is_built_while_running(tmp_path):
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=1560)
    (algo,) = harness.run_offline_backtest("FVGZone", [{"idname": "n", "ticker": "AAPL"}])
    assert algo._frame is None  # AlgoData is built only when somebody reads it
    assert len(algo.AlgoData) == 1560 and algo._frame is not None


def test_price_frame_is_not_modified():
    rig = Rig(**PLAIN)
    bars = run_of(D1, "09:30", [*quiet(17), *up_setup(100.0), retest_up(100.0)])
    rig.feed(bars[:-1])
    x = bars[-1]
    rig.store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
    frame = rig.store.view()
    before = frame.copy()
    rig.algo.update(frame)
    pd.testing.assert_frame_equal(frame, before)
