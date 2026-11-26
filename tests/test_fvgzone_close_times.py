"""FVGZone end-of-day times relative to the close (issue 284): full days as before, half days earlier.

2024-11-29 (the day after Thanksgiving) is a half day (close 13:00); 2024-08-19 and
2024-11-27 are full days. Hand-built bars, `SimBroker` for the rules and `PaperBroker`
through the engine's own harness for "nothing is left open".
"""
from __future__ import annotations

from datetime import date

import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.market_calendar import is_half_day
from tests import harness
from tests.fvgzone_support import D1, Rig, quiet, run_of, scenario, stamp, summary
from tests.test_fvgzone_algo import DRIFT, PLAIN
from tests.test_fvgzone_engine import write_bars

HALF = "2024-11-29"
FULL = "2024-11-27"


def test_the_days_used_here_are_what_they_say():
    assert is_half_day(date(2024, 11, 29))
    assert not is_half_day(date(2024, 11, 27))


@pytest.mark.parametrize("retest_at, trades", [("12:30", 1), ("12:31", 0)], ids=["at_last", "after_last"])
def test_entries_stop_at_12_30_on_a_half_day(retest_at, trades):
    assert len(Rig(**PLAIN).feed(scenario(HALF, retest_at)).trades) == trades


def test_the_same_late_retest_still_trades_on_a_full_day():
    assert len(Rig(**PLAIN).feed(scenario(FULL, "12:31")).trades) == 1
    assert len(Rig(**PLAIN).feed(scenario(FULL, "15:30")).trades) == 1
    assert len(Rig(**PLAIN).feed(scenario(FULL, "15:31")).trades) == 0


def test_a_half_day_position_is_closed_at_12_55():
    rig = Rig(**PLAIN).feed(scenario(HALF, "12:20", DRIFT * 39))  # bars to 12:59
    t = summary(rig.trades[0])
    assert t["exit_at"] == stamp(HALF, "12:55") and t["exit"] == 100.40
    assert not rig.algo.inTrade


def test_a_full_day_position_is_closed_at_15_55_as_before():
    rig = Rig(**PLAIN).feed(scenario(D1, "15:20", DRIFT * 36))
    assert summary(rig.trades[0])["exit_at"] == stamp(D1, "15:55")


def test_the_times_follow_each_day_in_one_run():
    # a full day, then the half day: the half day's own limits apply, and back again
    rig = Rig(**PLAIN)
    rig.feed(scenario(FULL, "12:31", DRIFT * 3))  # trades on the full day
    assert len(rig.trades) == 1
    rig.feed(scenario("2024-11-29", "12:31", DRIFT))  # no entry on the half day
    assert len(rig.trades) == 1
    rig.feed(scenario("2024-12-02", "12:31", DRIFT))  # a full day again
    assert len(rig.trades) == 2


def test_an_explicit_absolute_time_is_held_to_the_half_day_limit():
    # 15:55 / 15:30 on a half day behave as 12:55 / 12:30; on a full day they are as given
    rig = Rig(**PLAIN, last_entry="15:30", flat_time="15:55").feed(scenario(HALF, "12:20", DRIFT * 39))
    assert summary(rig.trades[0])["exit_at"] == stamp(HALF, "12:55")
    assert len(Rig(**PLAIN, last_entry="15:30", flat_time="15:55").feed(scenario(HALF, "12:31")).trades) == 0
    assert len(Rig(**PLAIN, last_entry="15:30", flat_time="15:55").feed(scenario(FULL, "12:31")).trades) == 1


def test_an_earlier_absolute_time_is_kept_on_a_half_day():
    rig = Rig(**PLAIN, last_entry="12:20", flat_time="12:40").feed(scenario(HALF, "12:20", DRIFT * 39))
    assert summary(rig.trades[0])["exit_at"] == stamp(HALF, "12:40")
    assert len(Rig(**PLAIN, last_entry="12:20", flat_time="12:40").feed(scenario(HALF, "12:21")).trades) == 0


def test_an_absolute_time_after_the_default_is_still_exact_on_a_full_day():
    # no cap on a full day: an explicit 15:58 flat time is 15:58, not 15:55
    rig = Rig(**PLAIN, last_entry="15:30", flat_time="15:58").feed(scenario(D1, "15:20", DRIFT * 40))
    assert summary(rig.trades[0])["exit_at"] == stamp(D1, "15:58")


def test_the_relative_keys_move_the_times():
    rig = Rig(**PLAIN, flat_before_close=10, last_entry_before_close=45)
    assert (rig.algo.last_entry, rig.algo.flat_time) == (15 * 60 + 15, 15 * 60 + 50)
    assert len(Rig(**PLAIN, last_entry_before_close=45).feed(scenario(D1, "15:16")).trades) == 0
    assert len(Rig(**PLAIN, last_entry_before_close=45).feed(scenario(D1, "15:15")).trades) == 1
    half = Rig(**PLAIN, flat_before_close=10).feed(scenario(HALF, "12:20", DRIFT * 39))
    assert summary(half.trades[0])["exit_at"] == stamp(HALF, "12:50")


def test_a_day_the_calendar_calls_closed_uses_the_full_day_times():
    # data on Thanksgiving itself (a holiday): the algo just trades by the full-day times
    assert len(Rig(**PLAIN).feed(scenario("2024-11-28", "15:30")).trades) == 1


def test_defaults_are_the_old_full_day_times():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL"})
    assert (algo.last_entry, algo.flat_time) == (15 * 60 + 30, 15 * 60 + 55)
    assert (algo.last_entry_before_close, algo.flat_before_close) == (30, 5)


@pytest.mark.parametrize(
    "bad",
    [
        {"flat_before_close": 0}, {"flat_before_close": 2.5}, {"flat_before_close": 391},
        {"last_entry_before_close": 0}, {"last_entry_before_close": "soon"},
        {"flat_before_close": 30, "last_entry_before_close": 30},
        {"flat_before_close": 30, "last_entry_before_close": 10},
        {"flat_time": "15:55", "last_entry_before_close": 5},  # last entry 15:55, not before flat
        {"first_entry": "15:31"},  # after the default last entry, 15:30
    ],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_invalid_relative_settings_fail_at_construction(bad):
    with pytest.raises(ValueError, match="FVGZone 't'"):
        load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", **bad})


def test_null_absolute_times_mean_unset():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "flat_time": None, "last_entry": None})
    assert (algo.last_entry, algo.flat_time) == (15 * 60 + 30, 15 * 60 + 55)


# ----- through the engine with realistic fills -----


def half_day_data(retest_at: str, drift_bars: int):
    warm = run_of(FULL, "09:30", quiet(60))
    return warm + scenario(HALF, retest_at, DRIFT * drift_bars)


def test_nothing_is_left_open_on_a_half_day_through_the_paper_broker(tmp_path):
    write_bars(tmp_path, "AAPL", half_day_data("12:20", 39))  # bars to 12:59, never a stop or target
    run = harness.run_paper_backtest(
        "FVGZone", {"idname": "h", "ticker": "AAPL", **PLAIN, "mode": "paper"}, warmup_days=1)
    (trip,) = run.ledger.round_trips
    assert trip.entry_time.strftime("%H:%M") == "12:21"
    assert trip.exit_time == stamp(HALF, "12:56")  # asked to close on the 12:55 bar, fills at the next open
    assert not run.algo.inTrade and run.violations == []
    assert len(run.ledger.ignored_events) == 0


def test_entries_stop_at_12_30_through_the_paper_broker(tmp_path):
    write_bars(tmp_path, "AAPL", half_day_data("12:31", 10))
    run = harness.run_paper_backtest(
        "FVGZone", {"idname": "h", "ticker": "AAPL", **PLAIN, "mode": "paper"}, warmup_days=1)
    assert len(run.ledger.round_trips) == 0 and not run.algo.trades
    assert not run.algo.inTrade and run.violations == []
