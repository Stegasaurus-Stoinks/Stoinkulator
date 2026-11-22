"""The incremental pieces of the Elliot algo against brute-force recomputation.

Elliot used to recompute every extremum and re-run the whole wave search on
every bar. It now keeps two caches (see ElliotFuncs.IncrementalExtrema and
Algo._detect_waves). Everything here compares the cached results with a
from-scratch computation, at every prefix length, on real bars and on random
data with plateaus and ties. The per-bar end-to-end guard is
tests/test_elliot_state_golden.py; this file pins the building blocks and the
fallbacks (jumps, shrinks, replaced history, a second instance).

`reference_*` below are the ORIGINAL implementations, copied from the code
this replaced (they use argrelextrema and pandas cell access directly).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest
from scipy.signal import argrelextrema

from MainStoinker.Algos.Elliot import ElliotFuncs_new as EF
from MainStoinker.Algos.Elliot.Elliot_Algo import Algo
from MainStoinker.Algos.Elliot.ElliotImpulse import ElliotImpulse
from tests import harness

TICKERS = ["MSFT", "AAPL", "TSLA"]
MSFT_FAST_OTHERS_SLOW = [
    pytest.param(t, marks=() if t == "MSFT" else pytest.mark.slow) for t in TICKERS
]
COMPARATORS = [np.less_equal, np.greater_equal]


# ---- originals ---------------------------------------------------------------

def original_min_limit_rule_break(x, wavex, wavey, mins):
    new_list = [item for item in mins[wavex:x] if not (np.isnan(item))]
    return any(h < wavey for h in new_list)


def original_max_limit_rule_break(x, wavex, wavey, maxs):
    new_list = [item for item in maxs[wavex:x] if not (np.isnan(item))]
    return any(h > wavey for h in new_list)


def reference_columns(frame: pd.DataFrame, order: int, size: int) -> tuple[np.ndarray, np.ndarray]:
    """The original `_calculate_extrema`: the AlgoData 'mins' and 'maxs' columns.

    `size` is len(AlgoData), which is len(frame) except after the history shrinks
    (ParentAlgo.pre_update only ever adds rows).
    """
    ilocs_min = argrelextrema(frame["close"].values, np.less_equal, order=order)[0]
    ilocs_max = argrelextrema(frame["close"].values, np.greater_equal, order=order)[0]
    mins = [np.nan] * size
    for i in ilocs_min:
        if i < size:
            mins[i] = frame.iloc[i]["low"] * 0.999
    maxs = [np.nan] * size
    for i in ilocs_max:
        if i < size:
            maxs[i] = frame.iloc[i]["high"] * 1.001
    return np.array(mins, dtype=float), np.array(maxs, dtype=float)


def reference_waves(frame: pd.DataFrame, order: int) -> tuple[list, list]:
    """The original `_detect_waves`, verbatim apart from taking `order` as an argument."""
    plotSize = frame.shape[0]
    ilocs_min = argrelextrema(frame["low"].values, np.less_equal, order=order)[0]
    ilocs_max = argrelextrema(frame["high"].values, np.greater_equal, order=order)[0]
    mins = [np.nan] * plotSize
    for i in ilocs_min:
        if i < len(mins):
            mins[i] = frame.iloc[i]["low"] * 0.9999
    maxs = [np.nan] * plotSize
    for i in ilocs_max:
        if i < len(maxs):
            maxs[i] = frame.iloc[i]["high"] * 1.0001

    reach = 3
    finishedWaves = []
    tradingWaves = []
    for i in range(len(ilocs_min)):
        try:
            wave = ElliotImpulse(plotSize)
            wave.time_1 = ilocs_min[i]
            wave.price_1 = mins[ilocs_min[i]]
            ilocs_max_valid = EF.find_line(np.nan, wave.time_1, ilocs_max)
            for curPoint in ilocs_max_valid[0:reach+1]:
                if wave.checkpoint2(curPoint, maxs[curPoint], mins):
                    wave.time_2 = curPoint
                    wave.price_2 = maxs[curPoint]
                    ilocs_min_valid = EF.find_line(np.nan, wave.time_2, ilocs_min)
                    for curPoint in ilocs_min_valid[0:reach+1]:
                        if wave.checkpoint3(curPoint, mins[curPoint], maxs, mins):
                            wave.time_3 = curPoint
                            wave.price_3 = mins[curPoint]
                            EF.check_future_points(curPoint, ilocs_min_valid, reach, tradingWaves, wave)
                            ilocs_max_valid = EF.find_line(np.nan, wave.time_3, ilocs_max)
                            for curPoint in ilocs_max_valid[0:reach+1]:
                                if wave.checkpoint4(curPoint, maxs[curPoint], mins):
                                    wave.time_4 = curPoint
                                    wave.price_4 = maxs[curPoint]
                                    EF.check_future_points(
                                        curPoint, ilocs_max_valid, reach, tradingWaves, wave)
                                    ilocs_min_valid = EF.find_line(np.nan, wave.time_4, ilocs_min)
                                    for curPoint in ilocs_min_valid[0:reach+1]:
                                        if wave.checkpoint5(curPoint, mins[curPoint], maxs):
                                            wave.time_5 = curPoint
                                            wave.price_5 = mins[curPoint]
                                            EF.check_future_points(
                                                curPoint, ilocs_min_valid, reach, tradingWaves, wave)
                                            ilocs_max_valid = EF.find_line(np.nan, wave.time_5, ilocs_max)
                                            for curPoint in ilocs_max_valid[0:reach+1]:
                                                if wave.checkpoint6(curPoint, maxs[curPoint], mins):
                                                    wave.time_6 = curPoint
                                                    wave.price_6 = maxs[curPoint]
                                                    finishedWaves.append(ElliotImpulse(
                                                        wave.plotSize,
                                                        wave.time_1, wave.price_1,
                                                        wave.time_2, wave.price_2,
                                                        wave.time_3, wave.price_3,
                                                        wave.time_4, wave.price_4,
                                                        wave.time_5, wave.price_5,
                                                        wave.time_6, wave.price_6
                                                    ))
        except Exception as e:
            logging.getLogger("elliot-reference").debug(f"Wave detection error: {e}")
            continue
    return finishedWaves, tradingWaves


# ---- data -------------------------------------------------------------------

_REAL: dict[str, pd.DataFrame] = {}


def real_frame(ticker: str, rows: int | None = None) -> pd.DataFrame:
    if ticker not in _REAL:
        _REAL[ticker] = pd.read_csv(harness.OFFLINE_DATA / f"_{ticker}_offlinedata_", index_col=0)
    df = _REAL[ticker]
    return (df if rows is None else df.iloc[:rows]).reset_index(drop=True)


def synthetic_frame(seed: int, n: int, wavy: bool = True) -> pd.DataFrame:
    """OHLC bars with plenty of ties (prices on a coarse grid) and, if wavy, swings."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.4, n)
    close = 100 + np.cumsum(steps)
    if wavy:
        close = close + 4 * np.sin(np.arange(n) / rng.uniform(3, 9)) + np.arange(n) * 0.02
    close = np.round(close * 5) / 5
    spread = np.round(rng.uniform(0, 0.6, n) * 5) / 5
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="min"),
        "time": np.arange(n) * 60 + 1_700_000_000,
        "open": close,
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": 100,
    })


def coords(waves: list) -> list:
    """Every wave's point coordinates and plotSize, NaN spelled out so == works."""
    out = []
    for w in waves:
        row = [w.plotSize]
        for i in range(1, 7):
            for kind in ("time", "price"):
                v = getattr(w, f"{kind}_{i}")
                row.append("nan" if isinstance(v, float | np.floating) and np.isnan(v) else float(v))
        out.append(row)
    return out


def new_algo(order: int) -> Algo:
    return Algo({"idname": "elliot-test", "ticker": "TEST", "order": order})


def check_against_reference(algo: Algo, frame: pd.DataFrame) -> tuple[int, int]:
    """Feed `frame` to the algo's detection and compare with the originals."""
    algo.pre_update(frame)
    algo._calculate_extrema(frame)
    finished, trading = algo._detect_waves(frame)

    want_mins, want_maxs = reference_columns(frame, algo.order, len(algo.AlgoData))
    for name, want in (("mins", want_mins), ("maxs", want_maxs)):
        got = algo.AlgoData[name]
        assert got.dtype == np.float64
        np.testing.assert_array_equal(got.to_numpy(), want, err_msg=f"{name} at length {len(frame)}")
    ref_finished, ref_trading = reference_waves(frame, algo.order)
    assert coords(finished) == coords(ref_finished), f"finishedWaves at length {len(frame)}"
    assert coords(trading) == coords(ref_trading), f"tradingWaves at length {len(frame)}"
    return len(finished), len(trading)


# ---- IncrementalExtrema -------------------------------------------------------

@pytest.mark.parametrize("order", [1, 2, 3, 10])
@pytest.mark.parametrize("comparator", COMPARATORS)
def test_extrema_match_scipy_at_every_prefix_of_random_data(order, comparator):
    for seed in range(30):
        rng = np.random.default_rng(seed)
        n = int(rng.integers(0, 90))                    # includes lengths below 2 * order + 1
        data = rng.integers(0, 4, n).astype(float)      # few distinct values: plateaus and ties everywhere
        tracker = EF.IncrementalExtrema(comparator, order)
        for m in range(1, n + 1):
            got = tracker.update(data[:m])
            want = argrelextrema(data[:m], comparator, order=order)[0]
            assert got.dtype == want.dtype
            np.testing.assert_array_equal(got, want, err_msg=f"seed {seed}, prefix {m}")


@pytest.mark.parametrize("order", [1, 2, 10])
def test_extrema_with_continuous_random_data(order):
    for seed in range(10):
        data = np.random.default_rng(100 + seed).normal(size=120)
        for comparator in COMPARATORS:
            tracker = EF.IncrementalExtrema(comparator, order)
            for m in range(1, len(data) + 1):
                np.testing.assert_array_equal(
                    tracker.update(data[:m]), argrelextrema(data[:m], comparator, order=order)[0]
                )


@pytest.mark.parametrize("ticker", MSFT_FAST_OTHERS_SLOW)
def test_extrema_match_scipy_at_every_prefix_of_real_data(ticker):
    frame = real_frame(ticker)
    for column, comparator in (("low", np.less_equal), ("high", np.greater_equal), ("close", np.less_equal)):
        data = frame[column].to_numpy()
        tracker = EF.IncrementalExtrema(comparator, 10)
        for m in range(1, len(data) + 1):
            np.testing.assert_array_equal(
                tracker.update(data[:m]), argrelextrema(data[:m], comparator, order=10)[0],
                err_msg=f"{ticker} {column} prefix {m}",
            )
        assert tracker.generation == 1, "never reset on a pure append-only feed"


@pytest.mark.parametrize("order", [1, 2, 10])
@pytest.mark.parametrize("comparator", COMPARATORS)
def test_extrema_below_len_minus_order_are_final(order, comparator):
    """The invariant the incremental scheme rests on: index i < n - order never changes."""
    for seed in range(20):
        rng = np.random.default_rng(seed)
        data = rng.integers(0, 4, 70).astype(float)
        full = argrelextrema(data, comparator, order=order)[0]
        for n in range(1, len(data) + 1):
            prefix = argrelextrema(data[:n], comparator, order=order)[0]
            np.testing.assert_array_equal(prefix[prefix < n - order], full[full < n - order])


def test_extrema_with_jumps_by_several_bars():
    rng = np.random.default_rng(7)
    data = rng.integers(0, 5, 400).astype(float)
    tracker = EF.IncrementalExtrema(np.less_equal, 10)
    n = 0
    while n < len(data):
        n = min(len(data), n + int(rng.integers(0, 40)))   # 0 = same length again
        if n == 0:
            continue
        want = argrelextrema(data[:n], np.less_equal, order=10)[0]
        np.testing.assert_array_equal(tracker.update(data[:n]), want)
    assert tracker.generation == 1


def test_extrema_reset_when_history_shrinks_or_is_replaced():
    rng = np.random.default_rng(3)
    data = rng.integers(0, 5, 200).astype(float)
    tracker = EF.IncrementalExtrema(np.greater_equal, 5)

    def check(arr):
        np.testing.assert_array_equal(tracker.update(arr), argrelextrema(arr, np.greater_equal, order=5)[0])

    check(data[:150])
    assert tracker.generation == 1
    check(data[:100])                       # shorter: start over
    assert tracker.generation == 2
    check(data[:120])
    changed = data.copy()
    changed[3] += 1.0                       # an early value differs
    check(changed[:121])
    assert tracker.generation == 3
    other = rng.integers(0, 5, 300).astype(float)
    check(other)                            # a completely different, longer history
    assert tracker.generation == 4
    check(other)                            # same data again is fine and not a reset
    assert tracker.generation == 4


def test_extrema_handle_nan_and_other_dtypes():
    data = np.array([3, 1, np.nan, 2, 2, 0, np.nan, 5, 1, 1, 4, 4, 0, 3, 2, 1, 7, 7, 6, 0], dtype=float)
    for comparator in COMPARATORS:
        tracker = EF.IncrementalExtrema(comparator, 2)
        for m in range(1, len(data) + 1):
            np.testing.assert_array_equal(
                tracker.update(data[:m]), argrelextrema(data[:m], comparator, order=2)[0]
            )
        assert tracker.generation == 1, "NaN in an unchanged prefix must not look like a change"
    ints = np.array([5, 1, 3, 3, 0, 9, 2, 2, 8, 1], dtype=np.int64)
    objs = np.array([5.0, 1.0, 3.0, 3.0, 0.0, 9.0, 2.0, 2.0, 8.0, 1.0], dtype=object)
    for arr in (ints, objs):
        tracker = EF.IncrementalExtrema(np.less_equal, 1)
        for m in range(1, len(arr) + 1):
            np.testing.assert_array_equal(
                tracker.update(arr[:m]), argrelextrema(ints[:m].astype(float), np.less_equal, order=1)[0]
            )


def test_extrema_never_write_to_the_callers_array():
    data = np.array([3.0, 1.0, 2.0, 0.0, 5.0, 1.0, 1.0, 4.0])
    data.flags.writeable = False
    tracker = EF.IncrementalExtrema(np.less_equal, 2)
    for m in range(1, len(data) + 1):
        tracker.update(data[:m])


@pytest.mark.parametrize("order", [0, -1])
def test_extrema_invalid_order_fails_like_scipy(order):
    data = np.arange(10.0)
    with pytest.raises(ValueError, match="Order must be an int >= 1"):
        argrelextrema(data, np.less_equal, order=order)
    with pytest.raises(ValueError, match="Order must be an int >= 1"):
        EF.IncrementalExtrema(np.less_equal, order).update(data)


# ---- LineAfter and the rule-break scans ---------------------------------------

def test_line_after_matches_find_line():
    rng = np.random.default_rng(5)
    for _ in range(200):
        ilocs = np.unique(rng.integers(0, 60, int(rng.integers(0, 25)))).astype(np.intp)
        x = np.int64(rng.integers(-2, 65))
        old = EF.find_line(np.nan, x, ilocs)
        new = EF.LineAfter(ilocs, x)
        assert len(new) == len(old)
        assert bool(new) == bool(old)
        assert list(new[0:4]) == old[0:4]
        if old:
            assert new[-1] == old[-1]
        for limit in (0, 10, 30, 70):
            want = len(old) >= 4 and old[3] < limit
            assert new.settled(limit, 3) == want


def test_rule_break_scans_match_the_originals():
    rng = np.random.default_rng(11)
    for _ in range(500):
        n = int(rng.integers(1, 40))
        arr = [np.nan if rng.random() < 0.7 else np.float64(rng.integers(0, 6)) for _ in range(n)]
        wavex = int(rng.integers(0, n))
        x = int(rng.integers(wavex, n + 1))
        wavey = np.float64(rng.integers(0, 6))
        new_min = EF.min_limit_rule_break(x, wavex, wavey, arr)
        new_max = EF.max_limit_rule_break(x, wavex, wavey, arr)
        assert new_min == original_min_limit_rule_break(x, wavex, wavey, arr)
        assert new_max == original_max_limit_rule_break(x, wavex, wavey, arr)


# ---- whole detection ----------------------------------------------------------

@pytest.mark.parametrize(
    "ticker, rows",
    [
        ("MSFT", 400),
        pytest.param("AAPL", 400, marks=pytest.mark.slow),
        ("TSLA", 600),
    ],
)
def test_detection_matches_the_original_at_every_bar_of_real_data(ticker, rows):
    frame = real_frame(ticker, rows)
    algo = new_algo(10)
    finished = trading = 0
    for n in range(40, len(frame) + 1):
        f, t = check_against_reference(algo, frame.iloc[:n])
        finished += f
        trading += t
    assert finished and trading, "the comparison should see waves, or it proves little"
    assert algo._start_cache, "settled searches should have been remembered"


@pytest.mark.parametrize("order", [1, 2, 3, 4, 5, 10])
def test_detection_matches_the_original_on_random_bars(order):
    seen_finished = seen_trading = 0
    for seed in range(4):
        frame = synthetic_frame(seed, 110, wavy=bool(seed % 2))
        algo = new_algo(order)
        for n in range(2, len(frame) + 1):
            f, t = check_against_reference(algo, frame.iloc[:n])
            seen_finished += f
            seen_trading += t
    if order >= 3:
        assert seen_finished or seen_trading, "random data produced no waves at all"


def test_settled_starts_are_not_searched_again(monkeypatch):
    calls = []
    original = Algo._search_start

    def counting(self, i, *args):
        calls.append(i)
        return original(self, i, *args)

    monkeypatch.setattr(Algo, "_search_start", counting)
    frame = real_frame("MSFT", 700)
    algo = new_algo(10)
    starts = 0
    for n in range(300, len(frame) + 1):
        algo.pre_update(frame.iloc[:n])
        algo._calculate_extrema(frame.iloc[:n])
        algo._detect_waves(frame.iloc[:n])
        starts += len(argrelextrema(frame["low"].values[:n], np.less_equal, order=10)[0])
    assert len(calls) < starts / 2, (len(calls), starts)


@pytest.mark.parametrize("order", [1, 2])
def test_nothing_is_remembered_for_orders_below_three(order):
    algo = new_algo(order)
    frame = real_frame("MSFT", 300)
    for n in range(100, 140):
        check_against_reference(algo, frame.iloc[:n])
    assert algo._start_cache == {}


# ---- fallbacks: the history is not "the previous one plus one bar" -------------

def test_detection_after_jumps_shrinks_and_replaced_history():
    frame = real_frame("MSFT", 900)
    algo = new_algo(10)
    lengths = [*range(200, 240), 300, 301, 302, 360, *range(361, 366), 150, 151, 152, 500, 501, 900, 899, 900]
    for n in lengths:
        check_against_reference(algo, frame.iloc[:n])
    generations = (algo._low_min.generation, algo._high_max.generation)
    assert generations[0] > 1 and generations[1] > 1, "shrinking should have reset the caches"

    # An early bar replaced at the same length, then growth: caches must not survive.
    changed = frame.copy()
    changed.loc[60, "low"] -= 0.5
    changed.loc[200, "high"] += 0.5
    for n in (900, 899, 880, 881, 882):
        check_against_reference(algo, changed.iloc[:n])
    check_against_reference(algo, frame.iloc[:882])
    assert algo._start_cache


def test_a_second_instance_started_on_a_long_history_agrees():
    frame = real_frame("AAPL", 800)
    seasoned = new_algo(10)
    for n in range(300, 801):
        seasoned.pre_update(frame.iloc[:n])
        seasoned._calculate_extrema(frame.iloc[:n])
        finished, trading = seasoned._detect_waves(frame.iloc[:n])

    fresh = new_algo(10)
    f, t = check_against_reference(fresh, frame.iloc[:800])
    assert (f, t) == (len(finished), len(trading))
    np.testing.assert_array_equal(fresh.AlgoData["mins"].to_numpy(), seasoned.AlgoData["mins"].to_numpy())
    np.testing.assert_array_equal(fresh.AlgoData["maxs"].to_numpy(), seasoned.AlgoData["maxs"].to_numpy())
    assert coords(fresh._detect_waves(frame.iloc[:800])[0]) == coords(finished)
    # and both go on agreeing with the originals
    longer = real_frame("AAPL", 830)
    for n in range(801, 831):
        check_against_reference(fresh, longer.iloc[:n])
        check_against_reference(seasoned, longer.iloc[:n])


def test_update_after_a_jump_matches_a_fresh_algo_started_there():
    """Whole `update`s: trading state depends on every bar seen, but the detection part
    is a pure function of the frame, so after a jump the observable detection output
    equals that of an algo that never saw the bars in between."""
    frame = real_frame("TSLA", 700)
    a = new_algo(10)
    for n in range(400, 440):
        a.update(frame.iloc[:n])
    a.inTrade = False
    a.update(frame.iloc[:520])                      # jump by 80 bars

    b = new_algo(10)
    b.update(frame.iloc[:520])                      # started directly on the long history
    assert coords(a.finishedWaves) == coords(b.finishedWaves)
    assert coords(a.tradingWaves) == coords(b.tradingWaves)
    pd.testing.assert_series_equal(a.AlgoData["mins"], b.AlgoData["mins"])
    pd.testing.assert_series_equal(a.AlgoData["maxs"], b.AlgoData["maxs"])


def test_frames_with_read_only_arrays_are_not_written_to():
    frame = real_frame("MSFT", 400)
    for column in ("low", "high", "close"):
        frame[column].values.flags.writeable = False
    algo = new_algo(10)
    for n in range(100, 140):
        check_against_reference(algo, frame.iloc[:n])


def test_mins_and_maxs_frames_are_still_available():
    frame = real_frame("MSFT", 300)
    algo = new_algo(10)
    algo.pre_update(frame)
    algo._calculate_extrema(frame)
    mins_df = algo.mins_df
    assert list(mins_df.columns) == ["time", "price"]
    assert len(mins_df) == algo.AlgoData["mins"].notna().sum() > 0
    np.testing.assert_array_equal(algo.mins_array, algo.AlgoData["mins"].to_numpy())
    assert len(algo.maxs_df) == algo.AlgoData["maxs"].notna().sum() > 0
    np.testing.assert_array_equal(algo.maxs_array, algo.AlgoData["maxs"].to_numpy())
