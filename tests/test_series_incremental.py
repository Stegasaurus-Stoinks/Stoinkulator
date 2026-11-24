"""`engine.series.SeriesTracker`: the incremental form of `extract_series`.

The union of a tracker's calls over a run fed bar by bar must be exactly what
`extract_series` gives at the end. Runs use short slices of OfflineData under
pytest's `tmp_path`; nothing touches a broker or a socket.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from MainStoinker.engine.series import SeriesTracker, extract_series
from MainStoinker.engine.store import SeriesInfo, SeriesPoint
from tests import harness

ALGO_CONFIG = harness.REPO_ROOT / "MainStoinker" / "MainStuff" / "Algo_config.json"
ENTRIES = json.loads(ALGO_CONFIG.read_text(encoding="utf-8"))
CASES = [(entry["ID"], cfg) for entry in ENTRIES for cfg in entry["data"]]

T0 = datetime(2024, 1, 2, 9, 30)
TIMES = [pd.Timestamp(T0 + timedelta(minutes=i)) for i in range(5)]


def fake_algo(frame, names=("a", "b"), types=("line", "marker-up"), name="algo-1"):
    return SimpleNamespace(name=name, AlgoData=frame, FrontEndDataStruct=list(names),
                           FrontEndDataType=list(types))


def frame(**columns):
    return pd.DataFrame({"time": [10, 20, 30, 40, 50], **columns})


def ordered(points):
    return sorted(points, key=lambda p: (p.account, p.name, p.time))


@pytest.mark.parametrize(("algo_id", "cfg"), CASES, ids=[c[1]["idname"] for c in CASES])
def test_tracker_union_equals_extract_series(algo_id, cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    harness.stage_offline_data(tmp_path, [cfg["ticker"]], max_rows=1100)
    tracker_calls: list[tuple[int, list[SeriesInfo], list[SeriesPoint]]] = []
    trackers: list[SeriesTracker] = []
    last_history: list[object] = []

    def wrap(algo):
        tracker = SeriesTracker(algo)
        trackers.append(tracker)
        original = algo.update

        def update(data):
            result = original(data)
            history = data["date"].tolist()
            last_history[:] = history
            tracker_calls.append((len(history), tracker.declare(), tracker.new_points(history)))
            return result

        algo.update = update

    (algo,) = harness.run_mixed_offline_backtest([(algo_id, cfg)], wrap=wrap)
    infos, points = extract_series(algo, last_history)
    assert tracker_calls, "the algo never updated"
    unreleased = (len(last_history), [], trackers[0].new_points(last_history, final=True))
    assert unreleased[2], "the last bars held no point: the final call would prove nothing"
    tracker_calls.append(unreleased)
    assert points, "the slice produced no series points: the test would prove nothing"
    got_infos = [i for _, declared, _ in tracker_calls for i in declared]
    got_points = [p for _, _, new in tracker_calls for p in new]
    assert got_infos == infos
    assert len(got_points) == len(set(got_points))  # nothing is returned twice
    assert ordered(got_points) == ordered(points)
    # Within one call: series in declaration order, then time.
    rank = {info.name: i for i, info in enumerate(infos)}
    for _, _, new in tracker_calls:
        keys = [(rank[p.name], p.time) for p in new]
        assert keys == sorted(keys)


def test_tracker_returns_late_filled_rows_once():
    data = frame(a=[1.0, np.nan, np.nan, np.nan, np.nan], b=[np.nan] * 5)
    algo = fake_algo(data)
    tracker = SeriesTracker(algo)
    assert tracker.declare() == [SeriesInfo("algo-1", "a", "line", 0),
                                 SeriesInfo("algo-1", "b", "marker-up", 1)]
    assert tracker.declare() == []
    first = tracker.new_points(TIMES)
    assert [(p.name, p.time, p.value) for p in first] == [("a", TIMES[0].to_pydatetime(), 1.0)]
    assert tracker.new_points(TIMES) == []
    algo.AlgoData.loc[1, "b"] = 7.0  # an earlier row is filled in afterwards
    algo.AlgoData.loc[4, "a"] = 5.0
    later = tracker.new_points(TIMES)
    assert [(p.name, p.time, p.value) for p in later] == [
        ("a", TIMES[4].to_pydatetime(), 5.0), ("b", TIMES[1].to_pydatetime(), 7.0)]
    assert tracker.new_points(TIMES) == []


def test_tracker_holds_back_the_newest_rows_until_final():
    algo = fake_algo(frame(a=[1.0, 2.0, 3.0, 4.0, 5.0], b=[np.nan] * 5))
    algo.order = 2
    tracker = SeriesTracker(algo)
    assert [p.time for p in tracker.new_points(TIMES)] == [t.to_pydatetime() for t in TIMES[:3]]
    assert tracker.new_points(TIMES) == []
    assert [p.time for p in tracker.new_points(TIMES, final=True)] == [
        t.to_pydatetime() for t in TIMES[3:]]
    assert [p.time for p in SeriesTracker(algo, settle=0).new_points(TIMES)] == [
        t.to_pydatetime() for t in TIMES]


def test_tracker_follows_extract_series_rules():
    data = frame(a=[1.0, np.inf, 3.0, np.nan, 5.0], b=[True, False, True, False, True],
                 c=["1", "x", None, "4", "5"])
    algo = fake_algo(data, names=("a", "b", "c", "a", "gone", ""),
                     types=("line", "line", "segment", "line", "line", "line"))
    tracker = SeriesTracker(algo)
    infos, points = extract_series(algo, TIMES)
    assert tracker.declare() == infos
    assert ordered(tracker.new_points(TIMES)) == ordered(points)
    assert tracker.new_points(TIMES) == []


def test_tracker_skips_on_length_mismatch_once(capsys):
    algo = fake_algo(frame(a=[1.0] * 5, b=[2.0] * 5))
    tracker = SeriesTracker(algo)
    assert tracker.new_points(TIMES[:3]) == []
    assert tracker.new_points(TIMES[:4]) == []
    assert tracker.new_points(TIMES[:2]) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("warning: algo-1:")
    # Once the history matches again the tracker carries on, as the end extraction would.
    got = tracker.new_points(TIMES)
    assert ordered(got) == ordered(extract_series(algo, TIMES)[1])
    assert capsys.readouterr().err == ""


def test_tracker_never_run_algo_is_silent(capsys):
    algo = fake_algo(frame(a=[1.0] * 5, b=[1.0] * 5).iloc[:0])
    tracker = SeriesTracker(algo)
    assert tracker.declare() == []
    assert tracker.new_points(TIMES) == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_tracker_repeated_bar_time_skips_for_good_with_one_warning(capsys):
    algo = fake_algo(frame(a=[1.0] * 5, b=[1.0] * 5))
    times = [*TIMES[:3], TIMES[1], TIMES[2]]
    tracker = SeriesTracker(algo)
    assert tracker.new_points(times) == []
    assert tracker.new_points(times) == []
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and str(TIMES[1]) in lines[0]


def test_extract_series_unchanged():
    data = frame(a=[1.0, 2.0, 3.0, 4.0, 5.0], b=[np.nan, 7.0, np.nan, np.nan, 9.0])
    algo = fake_algo(data)
    before = extract_series(algo, TIMES)
    tracker = SeriesTracker(algo)
    tracker.declare()
    tracker.new_points(TIMES)
    assert extract_series(algo, TIMES) == before  # using a tracker changes nothing
    assert before[0] == [SeriesInfo("algo-1", "a", "line", 0),
                         SeriesInfo("algo-1", "b", "marker-up", 1)]
    assert len(before[1]) == 7
    pd.testing.assert_frame_equal(algo.AlgoData, data)
