"""`engine.series.extract_series`: an algo's indicator series as store records."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

from MainStoinker.engine.series import extract_series
from MainStoinker.engine.store import SeriesInfo, SeriesPoint

T0 = datetime(2024, 1, 2, 9, 30)
BAR_TIMES = [pd.Timestamp(T0 + timedelta(minutes=i)) for i in range(5)]


def fake_algo(frame, names=("a", "b"), types=("line", "marker-up"), name="algo-1"):
    return SimpleNamespace(name=name, AlgoData=frame, FrontEndDataStruct=list(names),
                           FrontEndDataType=list(types))


def frame(**columns):
    return pd.DataFrame({"time": [10, 20, 30, 40, 50], **columns})


def test_extract_aligns_rows_by_position():
    algo = fake_algo(frame(a=[1.0, 2.0, 3.0, 4.0, 5.0], b=[np.nan, 7.0, np.nan, np.nan, 9.0]))
    infos, points = extract_series(algo, BAR_TIMES)
    assert infos == [SeriesInfo("algo-1", "a", "line", 0), SeriesInfo("algo-1", "b", "marker-up", 1)]
    assert [(p.name, p.time, p.value) for p in points if p.name == "b"] == [
        ("b", BAR_TIMES[1].to_pydatetime(), 7.0), ("b", BAR_TIMES[4].to_pydatetime(), 9.0)]
    a_points = [p for p in points if p.name == "a"]
    assert [p.time for p in a_points] == [t.to_pydatetime() for t in BAR_TIMES]
    assert all(type(p.time) is datetime for p in points)
    assert all(type(info.position) is int for info in infos)
    assert all(isinstance(p, SeriesPoint) and type(p.value) is float for p in points)


def test_extract_ignores_time_column():
    data = frame(a=[1.0, 2.0, 3.0, 4.0, 5.0], b=[np.nan] * 5)
    data["time"] = [np.nan, 5, 5, 0, np.nan]  # stale / NaN: must not matter
    infos, points = extract_series(fake_algo(data), BAR_TIMES)
    assert [i.name for i in infos] == ["a", "b"]
    assert [(p.time, p.value) for p in points] == [
        (t.to_pydatetime(), v) for t, v in zip(BAR_TIMES, [1.0, 2.0, 3.0, 4.0, 5.0], strict=True)]


def test_length_mismatch_skips_algo_with_warning(capsys):
    short = fake_algo(frame(a=[1.0] * 5, b=[1.0] * 5).iloc[:4])
    assert extract_series(short, BAR_TIMES) == ([], [])
    assert extract_series(fake_algo(frame(a=[1.0] * 5, b=[1.0] * 5)), BAR_TIMES[:3]) == ([], [])
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 2 and all(line.startswith("warning: algo-1:") for line in lines)


def test_missing_attributes_store_nothing():
    good = frame(a=[1.0] * 5, b=[1.0] * 5)
    for algo in (
        SimpleNamespace(name="x"),
        SimpleNamespace(name="x", AlgoData=good, FrontEndDataStruct=["a"]),
        SimpleNamespace(name="x", AlgoData=good, FrontEndDataType=["line"]),
        SimpleNamespace(name="x", FrontEndDataStruct=["a"], FrontEndDataType=["line"]),
        # ParentAlgo's placeholders before an algo fills them in
        SimpleNamespace(name="x", AlgoData=0, FrontEndDataStruct=0, FrontEndDataType=0),
        SimpleNamespace(name="x", AlgoData=good, FrontEndDataStruct="ab", FrontEndDataType="ll"),
        SimpleNamespace(name="x", AlgoData=[1, 2, 3, 4, 5], FrontEndDataStruct=["a"],
                        FrontEndDataType=["line"]),
        SimpleNamespace(name="x", AlgoData=good, FrontEndDataStruct=["a", "b"],
                        FrontEndDataType=["line"]),
    ):
        assert extract_series(algo, BAR_TIMES) == ([], [])


def test_non_numeric_and_nan_values_dropped():
    data = frame(
        a=["1.5", "x", None, np.inf, -np.inf],
        b=pd.Series([1.0, np.nan, 3.0, None, 5.0], dtype=object),
    )
    infos, points = extract_series(fake_algo(data), BAR_TIMES)
    assert [i.name for i in infos] == ["a", "b"]
    values = {(p.name, p.time): p.value for p in points}
    assert values == {("a", BAR_TIMES[0].to_pydatetime()): 1.5,
                      ("b", BAR_TIMES[0].to_pydatetime()): 1.0,
                      ("b", BAR_TIMES[2].to_pydatetime()): 3.0,
                      ("b", BAR_TIMES[4].to_pydatetime()): 5.0}
    assert all(math.isfinite(p.value) for p in points)


def test_unknown_draw_type_kept_as_text():
    infos, _ = extract_series(
        fake_algo(frame(a=[1.0] * 5, b=[1.0] * 5), types=("marker-dot", "whatever")), BAR_TIMES)
    assert [i.draw_type for i in infos] == ["marker-dot", "whatever"]


def test_duplicate_name_keeps_the_first_and_unknown_column_is_left_out():
    algo = fake_algo(frame(a=[1.0] * 5, b=[2.0] * 5), names=("a", "a", "missing", "b"),
                     types=("line", "segment", "line", "marker-up"))
    infos, points = extract_series(algo, BAR_TIMES)
    assert infos == [SeriesInfo("algo-1", "a", "line", 0), SeriesInfo("algo-1", "b", "marker-up", 3)]
    assert {p.value for p in points if p.name == "a"} == {1.0}


def test_extract_does_not_modify_algodata():
    data = frame(a=["1", "x", None, np.inf, 5], b=[np.nan, 2.0, 3.0, 4.0, 5.0])
    algo = fake_algo(data)
    before = data.copy(deep=True)
    names_before, types_before = list(algo.FrontEndDataStruct), list(algo.FrontEndDataType)
    extract_series(algo, BAR_TIMES)
    assert algo.AlgoData is data
    pd.testing.assert_frame_equal(data, before)
    assert (algo.FrontEndDataStruct, algo.FrontEndDataType) == (names_before, types_before)


def test_records_are_acceptable_to_the_store(tmp_path):
    """The values, positions and times pass `write_series`' strict validation."""
    from decimal import Decimal

    from MainStoinker.engine.store import AlgoMeta, RunMeta, open_store

    infos, points = extract_series(fake_algo(frame(a=[1.0, 2, 3, 4, 5], b=[np.nan] * 5)),
                                   BAR_TIMES)
    store = open_store(str(tmp_path / "s.db"), env={})
    try:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy"))
        store.write_algos(run_id, [AlgoMeta("algo-1", "X", "AAPL", "legacy", Decimal(1), {})])
        store.write_series(run_id, infos, points)
        assert len(store.load_series(run_id, "algo-1")) == 5
    finally:
        store.close()
