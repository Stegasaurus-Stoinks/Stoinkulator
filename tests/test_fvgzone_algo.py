"""FVGZone (issue 277) on hand-built bars: entries, filters, arithmetic, time rules.

The algo is driven bar by bar the way the engine does it, on a `SimBroker`
(fills where the algo says; the engine paths are in `test_fvgzone_engine.py`).
The bar builders and the mirror helper are in `tests/fvgzone_support.py`.
"""
from __future__ import annotations

import pytest

from MainStoinker.engine.algos import load_algo
from tests.fvgzone_support import (
    D1,
    D2,
    Rig,
    mirror,
    mirrored,
    quiet,
    retest_up,
    run_of,
    scenario,
    stamp,
    summary,
    up_setup,
)

# Quiet context, one-minute zones, no trend filter: the zone rules alone.
PLAIN = {"zone_minutes": 1, "trend_filter": False}
# After the retest bar: price stays between stop and target, then ends the trade.
TARGET_BAR = [(100.50, 101.25, 100.45, 101.20)]  # high above the 101.19 target
STOP_BAR = [(100.40, 100.45, 100.00, 100.05)]  # close below the 100.08 stop
DRIFT = [(100.40, 100.50, 100.30, 100.40)]


def long_trade(**cfg):
    rig = Rig(**{**PLAIN, **cfg}).feed(scenario(D1, "09:50"))
    return rig, summary(rig.trades[0])


# ----- entry, stop, target, size -----


def test_long_entry_arithmetic():
    rig, t = long_trade()
    assert len(rig.trades) == 1
    # zone [100.10, 100.30]; entry reference = the retest bar's close
    assert t["side"] == 1
    assert t["at"] == stamp(D1, "09:50")
    assert t["price"] == 100.45
    assert t["stop"] == 100.08  # zone bottom - 0.02
    # R = 100.45 - 100.08 = 0.37; target = 100.45 + 2 * 0.37; size = floor(50 / 0.37)
    assert t["tp"] == 101.19
    assert t["shares"] == 135


def test_short_entry_is_the_mirror():
    rig = Rig(**PLAIN).feed(mirror(scenario(D1, "09:50")))
    (t,) = (summary(x) for x in rig.trades)
    assert t == mirrored(long_trade()[1])
    assert (t["side"], t["price"], t["stop"], t["tp"]) == (0, 99.55, 99.92, 98.81)


@pytest.mark.parametrize("after", [TARGET_BAR, STOP_BAR, DRIFT * 3], ids=["target", "stop", "open"])
@pytest.mark.parametrize("trend", [False, True], ids=["no_filter", "filter"])
def test_mirror_symmetry_of_decisions(after, trend):
    cfg = {"zone_minutes": 1, "trend_filter": trend, "trend_ema": 5}
    bars = scenario(D1, "09:50", after)
    long_run = Rig(**cfg).feed(bars)
    short_run = Rig(**cfg).feed(mirror(bars))
    assert len(long_run.trades) == len(short_run.trades) == 1
    assert summary(short_run.trades[0]) == mirrored(summary(long_run.trades[0]))
    assert long_run.algo.inTrade == short_run.algo.inTrade


def test_target_exit_fills_at_the_target():
    rig, _ = long_trade()
    rig.feed(run_of(D1, "09:51", TARGET_BAR))
    t = summary(rig.trades[0])
    assert (t["exit_at"], t["exit"]) == (stamp(D1, "09:51"), 101.19)
    assert not rig.algo.inTrade


def test_stop_exit_fills_at_the_stop():
    rig, _ = long_trade()
    rig.feed(run_of(D1, "09:51", STOP_BAR))
    t = summary(rig.trades[0])
    assert (t["exit_at"], t["exit"]) == (stamp(D1, "09:51"), 100.08)
    assert not rig.algo.inTrade


@pytest.mark.parametrize(
    "cfg, shares, tp",
    [
        ({}, 135, 101.19),
        ({"RRRatio": 3}, 135, 101.56),  # 100.45 + 3 * 0.37
        ({"RRRatio": 1.5}, 135, 101.01),  # distance 0.555 rounds half up to 0.56
        ({"stop_buffer": 0.05}, 125, 101.25),  # R = 0.40
        ({"risk_dollars": 10}, 27, 101.19),
        ({"risk_dollars": 0.10}, 1, 101.19),  # at least one share
        ({"max_shares": 100}, 100, 101.19),  # at most max_shares
    ],
    ids=["default", "rr3", "rr1.5", "buffer", "risk10", "min_one", "max_shares"],
)
def test_size_and_target_arithmetic(cfg, shares, tp):
    _, t = long_trade(**cfg)
    assert (t["shares"], t["tp"]) == (shares, tp)


# ----- skip rules -----


def risky_scenario(gap_top: float, close: float, **cfg):
    """A zone [100.10, gap_top] retested with a close at `close`."""
    ctx = quiet(10)
    c1 = (100.00, 100.10, 99.95, 100.05)
    c2 = (100.05, gap_top + .30, 100.05, gap_top + .20)
    c3 = (gap_top + .20, gap_top + .40, gap_top, gap_top + .30)
    retest = (gap_top + .20, gap_top + .25, gap_top - .01, close)
    rig = Rig(**{**PLAIN, "first_entry": "09:30", **cfg}).feed(
        run_of(D1, "09:30", [*ctx, c1, c2, c3, retest]))
    return rig


def test_setup_below_min_risk_is_skipped_and_the_zone_stays_usable():
    # zone [100.10, 100.16]; close 100.17 gives R = 100.17 - 100.08 = 0.09 < 0.10
    rig = risky_scenario(100.16, 100.17)
    assert rig.trades == []
    assert [why for _, why in rig.algo.skipped] == ["risk below min_risk"]
    # a later retest of the same zone with more room (R = 0.22) is taken: the skip did not use it up
    rig.feed(run_of(D1, "09:44", [(100.30, 100.32, 100.14, 100.30)]))
    assert len(rig.trades) == 1 and summary(rig.trades[0])["price"] == 100.30


def test_setup_above_max_risk_atr_is_skipped():
    rig = risky_scenario(100.16, 100.40, max_risk_atr=0.5)
    assert rig.trades == [] and len(rig.algo.skipped) == 1
    assert "max_risk_atr" in rig.algo.skipped[0][1]
    # the same setup with the default allowance (3 x ATR) is taken
    assert len(risky_scenario(100.16, 100.40).trades) == 1


# ----- trend filter -----


def spike_context(level: float, vol: int, n: int) -> list[tuple]:
    return [(level, level + .10, level - .10, level, vol)] * n


def test_trend_filter_blocks_against_vwap_and_off_switch_lifts_it():
    # heavy bars at 101 keep VWAP near 101; two wide bars bridge down to 100 (no gap between
    # them), where the setup sits: the retest closes at 100.45, below VWAP and above the EMA
    heavy = spike_context(101.00, 100_000, 4)
    bridge = [(101.00, 101.05, 99.95, 100.00, 10), (100.00, 101.00, 99.90, 100.00, 10)]
    ctx = [*heavy, *bridge, *quiet(2, 100.0, 10)]
    bars = scenario(D1, "09:41", context=ctx)
    cfg = {"zone_minutes": 1, "first_entry": "09:30"}
    on = Rig(**cfg, trend_filter=True, trend_ema=5).feed(bars)
    off = Rig(**cfg, trend_filter=False).feed(bars)
    assert on.trades == []
    assert len(off.trades) == 1
    vwap, ema = on.algo._columns["VWAP"][-1], on.algo._columns["EMA"][-1]
    assert vwap > 100.45 > ema  # only VWAP blocks the long


def test_trend_filter_blocks_against_ema_and_off_switch_lifts_it():
    # a heavy start at 100 puts VWAP at ~100; a light run at 103 pulls the EMA up; the setup
    # (base 101.50) closes its retest at 101.95: above VWAP, below the EMA
    ctx = spike_context(100.00, 1_000_000, 5) + spike_context(103.00, 10, 3)
    bars = scenario(D1, "09:41", base=101.50, context=ctx)
    cfg = {"zone_minutes": 1, "first_entry": "09:30"}
    on = Rig(**cfg, trend_filter=True, trend_ema=3).feed(bars)
    off = Rig(**cfg, trend_filter=False).feed(bars)
    vwap, ema = on.algo._columns["VWAP"][-1], on.algo._columns["EMA"][-1]
    assert vwap < 101.95 < ema  # only the EMA blocks the long
    assert on.trades == []
    assert len(off.trades) == 1


def test_trend_filter_passes_when_both_agree():
    rig = Rig(zone_minutes=1, trend_filter=True, trend_ema=5).feed(scenario(D1, "09:50"))
    assert len(rig.trades) == 1 and summary(rig.trades[0])["side"] == 1


def test_trend_filter_needs_a_warm_ema():
    # default trend_ema is 50 bars; the retest comes after only 19 bars of the session
    rig = Rig(zone_minutes=1).feed(scenario(D1, "09:50"))
    assert rig.trades == []
    assert rig.algo._columns["EMA"][-1] != rig.algo._columns["EMA"][-1]  # NaN: not ready


def test_vwap_and_ema_series_match_independent_calculations():
    bars = scenario(D1, "09:50", TARGET_BAR)
    rig = Rig(zone_minutes=1, trend_filter=True, trend_ema=5).feed(bars)
    typical = [(x.h + x.l + x.c) / 3 for x in bars]
    vwap, volume = 0.0, 0
    for i, x in enumerate(bars):
        vwap += typical[i] * x.v
        volume += x.v
        assert rig.algo._columns["VWAP"][i] == pytest.approx(vwap / volume, abs=1e-9)
    ema = None
    for i, x in enumerate(bars):
        ema = x.c if ema is None else ema + (2 / 6) * (x.c - ema)
        if i >= 4:
            assert rig.algo._columns["EMA"][i] == pytest.approx(ema, abs=1e-9)


# ----- one trade per zone, one position at a time -----


def test_one_trade_per_zone():
    rig = Rig(**PLAIN).feed(scenario(D1, "09:50", TARGET_BAR))
    assert len(rig.trades) == 1 and not rig.algo.inTrade
    # the zone is retested twice more after the exit: it was used, so nothing happens
    rig.feed(run_of(D1, "09:52", [retest_up(100.0), retest_up(100.0)]))
    assert len(rig.trades) == 1


def test_one_position_at_a_time_and_the_next_zone_after_the_exit():
    # a second up zone forms while the first trade is open (price stays between stop and target)
    during = [
        (100.50, 100.60, 100.40, 100.55),
        (100.55, 100.70, 100.45, 100.65),  # high1 = 100.70
        (100.65, 101.00, 100.65, 100.95),
        (100.95, 101.10, 100.95, 101.05),  # low3 = 100.95 > 100.70: zone [100.70, 100.95]
        (101.05, 101.08, 100.90, 100.99),  # retest of zone 2 while still in trade 1: ignored
    ]
    rig = Rig(**PLAIN).feed(scenario(D1, "09:50", during))
    assert len(rig.trades) == 1 and rig.algo.inTrade
    # the first trade ends at its target; then zone 2 is retested and taken
    rig.feed(run_of(D1, "09:56", [(101.0, 101.30, 100.95, 101.25)]))
    assert not rig.algo.inTrade  # the exit bar itself also retested zone 2: no entry on it
    assert len(rig.trades) == 1
    rig.feed(run_of(D1, "09:57", [(101.25, 101.26, 100.90, 101.10)]))
    assert len(rig.trades) == 2
    second = summary(rig.trades[1])
    assert second["at"] == stamp(D1, "09:57") and second["stop"] == 100.68


# ----- zones: known_at, expiry, state -----


def zone5_bars() -> list:
    """Quiet to 09:59, then candle 1 (10:00), 2 (10:05), 3 (10:10) of 5 one-minute bars each."""
    ctx = quiet(30)
    c1 = [(100.00, 100.10, 99.95, 100.05)] * 5  # high 100.10
    c2 = [(100.05, 100.60, 100.05, 100.55)] * 5
    c3 = [(100.55, 100.70, 100.35, 100.65)] * 4 + [(100.60, 100.70, 100.30, 100.65)]  # low 100.30
    return [*ctx, *c1, *c2, *c3]  # 09:30 .. 10:14; zone [100.10, 100.30] known at 10:15


def test_zone_is_unusable_before_known_at():
    bars = zone5_bars()
    # the 10:14 bar (last of candle 3) itself dips to 100.30 and closes above it, but the zone
    # exists only from 10:15
    rig = Rig(zone_minutes=5, trend_filter=False, first_entry="09:30").feed(run_of(D1, "09:30", bars))
    assert rig.trades == []
    assert [z.known_at for z in rig.algo._det.live_zones()] == [stamp(D1, "10:15")]
    # the same bar one minute later is a valid retest
    rig.feed(run_of(D1, "10:15", [(100.60, 100.62, 100.28, 100.55)]))
    assert len(rig.trades) == 1
    assert summary(rig.trades[0])["at"] == stamp(D1, "10:15")


def test_five_minute_zone_formed_by_the_0940_bar_cannot_trigger_before_0945():
    # candle 3 labelled 09:40: the first possible entry bar is 09:45
    ctx = []
    c1 = [(100.00, 100.10, 99.95, 100.05)] * 5  # 09:30
    c2 = [(100.05, 100.60, 100.05, 100.55)] * 5  # 09:35
    c3 = [(100.55, 100.70, 100.35, 100.65)] * 4 + [(100.60, 100.70, 100.30, 100.65)]  # 09:40
    cfg = {"zone_minutes": 5, "trend_filter": False, "first_entry": "09:30", "min_size_atr": None}
    rig = Rig(**cfg).feed(run_of(D1, "09:30", [*ctx, *c1, *c2, *c3]))
    assert rig.trades == []
    assert [z.known_at for z in rig.algo._det.live_zones()] == [stamp(D1, "09:45")]


def test_zones_do_not_survive_the_session():
    rig = Rig(**PLAIN).feed(scenario(D1, "09:50", TARGET_BAR))
    # next day: the same retest geometry against yesterday's zone [100.10, 100.30] does nothing
    day2 = run_of(D2, "09:30", [*quiet(20), retest_up(100.0)])
    rig.feed(day2)
    assert len(rig.trades) == 1


def test_a_zone_the_detector_has_closed_is_not_traded():
    # the zone is invalidated by a close below its bottom, then price returns above and retests
    after = [(100.20, 100.22, 99.90, 100.00)]  # close 100.00 < bottom 100.10: invalidated
    back = [(100.05, 100.40, 100.04, 100.35), (100.35, 100.40, 100.25, 100.45)]
    bars = scenario(D1, "09:50", [])
    rig = Rig(**PLAIN).feed(bars[:-1])  # up to c3, before the retest bar
    rig.feed(run_of(D1, "09:50", [*after, *back]))
    assert rig.trades == []


def test_detection_continues_while_in_a_trade():
    during = [
        (100.50, 100.60, 100.40, 100.55),
        (100.55, 100.70, 100.45, 100.65),
        (100.65, 101.00, 100.65, 100.95),
        (100.95, 101.10, 100.95, 101.05),
    ]
    rig = Rig(**PLAIN).feed(scenario(D1, "09:50", during))
    assert rig.algo.inTrade
    live = rig.algo._det.live_zones()
    assert [(z.bottom, z.top) for z in live] == [(100.10, 100.30), (100.70, 100.95)]


# ----- time rules -----


@pytest.mark.parametrize(
    "retest_at, trades",
    [("09:44", 0), ("09:45", 1), ("15:30", 1), ("15:31", 0)],
    ids=["before_first", "at_first", "at_last", "after_last"],
)
def test_entries_only_inside_the_window(retest_at, trades):
    rig = Rig(**PLAIN).feed(scenario(D1, retest_at))
    assert len(rig.trades) == trades


def test_window_is_configurable():
    bars = scenario(D1, "10:00")
    assert len(Rig(**PLAIN, first_entry="10:01").feed(bars).trades) == 0
    assert len(Rig(**PLAIN, last_entry="09:59").feed(bars).trades) == 0
    assert len(Rig(**PLAIN, first_entry="10:00", last_entry="10:00").feed(bars).trades) == 1


def test_open_position_is_closed_at_flat_time():
    drift = DRIFT * 36  # 15:21 .. 15:56
    rig = Rig(**PLAIN).feed(scenario(D1, "15:20", drift))
    t = summary(rig.trades[0])
    assert t["exit_at"] == stamp(D1, "15:55")
    assert t["exit"] == 100.40  # the 15:55 bar's close
    assert not rig.algo.inTrade


def test_flat_time_is_configurable():
    rig = Rig(**PLAIN, last_entry="15:20", flat_time="15:25").feed(scenario(D1, "15:20", DRIFT * 10))
    assert summary(rig.trades[0])["exit_at"] == stamp(D1, "15:25")


def test_nothing_is_carried_overnight():
    # the data stops before flat_time: the next session's first bar closes the position
    rig = Rig(**PLAIN).feed(scenario(D1, "15:20", DRIFT * 5))
    assert rig.algo.inTrade
    rig.feed(run_of(D2, "09:30", [(100.70, 100.80, 100.60, 100.70)]))
    t = summary(rig.trades[0])
    assert (t["exit_at"], t["exit"]) == (stamp(D2, "09:30"), 100.70)
    assert not rig.algo.inTrade
    # and no entry came with it
    assert len(rig.trades) == 1


# ----- configuration -----


@pytest.mark.parametrize(
    "bad",
    [
        {"zone_minutes": 0}, {"zone_minutes": 2.5}, {"zone_minutes": "five"},
        {"zone_minutes": True}, {"trend_filter": "yes"}, {"trend_ema": 0},
        {"RRRatio": 0}, {"RRRatio": -1}, {"stop_buffer": -0.01}, {"stop_buffer": 0.015},
        {"min_risk": 0.123}, {"max_risk_atr": 0}, {"risk_dollars": 0}, {"max_shares": 0},
        {"first_entry": "9:45"}, {"first_entry": "25:00"}, {"last_entry": "09:00"},
        {"first_entry": "15:40", "last_entry": "15:30"}, {"last_entry": "15:55"},
        {"flat_time": "09:45"}, {"min_size_atr": -1}, {"RRRatio": float("nan")},
        {"colour": "red"},
    ],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_invalid_or_unknown_parameters_fail_at_construction(bad):
    with pytest.raises(ValueError, match="FVGZone 't'"):
        load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", **bad})


def test_defaults_and_general_keys():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "mode": "paper"})
    assert (algo.zone_minutes, algo.trend_filter, algo.trend_ema, algo.RRRatio) == (5, True, 50, 2)
    assert (algo.stop_buffer_ticks, algo.min_risk_ticks, algo.max_risk_atr) == (2, 10, 3)
    assert (algo.risk_dollars, algo.max_shares) == (50, 500)
    assert (algo.first_entry, algo.last_entry, algo.flat_time) == (9 * 60 + 45, 15 * 60 + 30, 15 * 60 + 55)
    assert algo._fvg_config.bar_minutes == 5 and algo._fvg_config.min_size_atr == 0.25


def test_string_numbers_are_accepted_like_the_other_algos():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "RRRatio": "3", "zone_minutes": "1"})
    assert (algo.RRRatio, algo.zone_minutes) == (3.0, 1)


def test_up_setup_helpers_describe_the_zone():
    # guards the builders the other tests rely on
    c1, _, c3 = up_setup(100.0)
    assert (c1[1], c3[2]) == (100.10, 100.30)
