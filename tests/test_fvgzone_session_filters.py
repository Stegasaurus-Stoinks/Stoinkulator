"""FVGZone volume-by-slot and trend-day entry filters (issue 282).

Each scenario is three days of the same hand-built shape: two history days and a test day. The
setup of `tests/fvgzone_support.py` is at 09:57 to 09:59 and the retest at 10:00, with 1-minute
zone bars, so the zone's MIDDLE candle is the 09:58 bar. With two sessions of history
(`vol_slot_sessions` and `adr_sessions` of 2) the filters are warm on the third day.

History: every 09:58 bar trades 1000 shares (the slot mean is 1000); a session's range is 0.80
(99.90 to 100.70), so the ADR is 0.80. The retest closes at 100.45 and the session opens at 100.00:
D = 0.45.
"""
from __future__ import annotations

import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.feed import Bar
from tests.fvgzone_support import (
    D1,
    D2,
    D3,
    B,
    Rig,
    mirror,
    quiet,
    retest_up,
    run_of,
    stamp,
    summary,
    up_setup,
)

BASE = {"zone_minutes": 1, "trend_filter": False}
VOL = {**BASE, "vol_slot_mult": 1.5, "vol_slot_sessions": 2}
ADR = {**BASE, "trend_day_adr": 0.3, "adr_sessions": 2}


def day_bars(day: str, mid_volume: int = 1000, first: tuple | None = None) -> list[B]:
    """09:30 to 10:00: quiet, the setup (its middle bar 09:58 has `mid_volume`), the retest."""
    ctx = quiet(27)
    if first is not None:
        ctx[0] = first
    c1, c2, c3 = up_setup(100.0)
    return run_of(day, "09:30", [*ctx, c1, (*c2, mid_volume), c3, retest_up(100.0)])


def history() -> list[B]:
    return [*day_bars(D1), *day_bars(D2)]


def run(cfg: dict, test_day: list[B], hist: list[B] | None = None, *, flip: bool = False) -> Rig:
    bars = [*(history() if hist is None else hist), *test_day]
    return Rig(**cfg).feed(mirror(bars) if flip else bars)


def taken(rig: Rig) -> bool:
    return len(rig.trades) == 1 and summary(rig.trades[0])["at"] == stamp(D3, "10:00")


# ----- volume by slot -----


def test_volume_allowed_at_the_multiple_and_blocked_below_it():
    assert taken(run(VOL, day_bars(D3, 1500)))  # 1500 >= 1.5 x 1000: equal is allowed
    assert taken(run(VOL, day_bars(D3, 4000)))
    low = run(VOL, day_bars(D3, 1499))
    assert low.trades == []
    assert low.algo._traded == set()  # blocked: the zone is not used up


def test_volume_short_side():
    assert taken(run(VOL, day_bars(D3, 1500), flip=True))
    assert run(VOL, day_bars(D3, 1499), flip=True).trades == []


def test_volume_is_compared_with_the_slot_history_not_the_other_slots():
    # the 09:58 history is 1000; a busy 09:45 bar in the history must not matter
    busy = [B(x.at, x.o, x.h, x.l, x.c, 50_000) if x.at.strftime("%H:%M") == "09:45" else x
            for x in history()]
    assert taken(run(VOL, day_bars(D3, 1500), busy))
    # a history whose 09:58 bars are bigger raises the bar
    big = [B(x.at, x.o, x.h, x.l, x.c, 2000) if x.at.strftime("%H:%M") == "09:58" else x for x in history()]
    assert run(VOL, day_bars(D3, 2999), big).trades == []
    assert taken(run(VOL, day_bars(D3, 3000), big))


def test_volume_not_warm_is_blocked():
    one_day = run(VOL, day_bars(D3, 100_000), day_bars(D2))
    assert one_day.trades == [] and one_day.algo._traded == set()
    assert run(VOL, day_bars(D3, 100_000), []).trades == []


def test_volume_uses_only_earlier_sessions_not_the_current_one():
    # today's own huge 09:58 bar is not in today's mean: it joins the history only when the next day starts
    rig = Rig(**VOL).feed([*history(), *day_bars(D3, 100_000)])
    assert taken(rig)
    assert rig.algo._slot_volume.mean(stamp(D3, "09:58")) == 1000.0


# ----- trend day -----


def test_trend_day_allowed_at_the_multiple_and_blocked_above_it():
    assert taken(run({**ADR, "trend_day_adr": 0.5625}, day_bars(D3)))  # D 0.45 >= 0.5625 x 0.80 exactly
    blocked = run({**ADR, "trend_day_adr": 0.5626}, day_bars(D3))
    assert blocked.trades == [] and blocked.algo._traded == set()


def test_trend_day_short_side():
    assert taken(run({**ADR, "trend_day_adr": 0.5625}, day_bars(D3), flip=True))
    assert run({**ADR, "trend_day_adr": 0.5626}, day_bars(D3), flip=True).trades == []


def test_trend_day_needs_the_side_sign():
    # the session opens at 101, above the entry close of 100.45: a down day, so a long is blocked...
    down_day = day_bars(D3, first=(101.0, 101.1, 100.9, 101.0))
    assert run(ADR, down_day).trades == []
    # ...and in the mirror image (the day opens at 99, below the short's entry close of 99.55) the short
    # is blocked too: |D| is large enough in both, but its sign is against the side
    assert run(ADR, down_day, flip=True).trades == []
    # the same bars with the side's sign (no deep first bar) pass with the same multiple
    assert taken(run(ADR, day_bars(D3)))


def test_trend_day_not_ready_is_blocked():
    assert run(ADR, day_bars(D3), day_bars(D2)).trades == []
    assert run(ADR, day_bars(D3), []).trades == []


def test_trend_day_without_a_0930_bar_is_blocked():
    late = day_bars(D3)[1:]  # the session's first bar is 09:31: no session open
    rig = run(ADR, late)
    assert rig.trades == [] and rig.algo._traded == set()
    assert rig.algo._daily_range.session_open is None


# ----- together, defaults, independence -----


def test_both_filters_must_pass():
    both = {**VOL, **ADR}
    assert taken(run(both, day_bars(D3, 1500)))
    assert run(both, day_bars(D3, 1499)).trades == []  # volume fails
    assert run({**both, "trend_day_adr": 0.5626}, day_bars(D3, 1500)).trades == []  # trend day fails


def test_a_blocked_zone_may_pass_on_a_later_retest():
    # trend day: blocked at 10:00 (D 0.45 < 0.5626 x 0.80), then the price rises and a second retest of
    # the same zone closes higher: D 0.60 >= 0.45
    cfg = {**ADR, "trend_day_adr": 0.5626}
    rig = run(cfg, day_bars(D3))
    assert rig.trades == [] and rig.algo._traded == set()
    rig.feed([B(stamp(D3, "10:01"), 100.60, 100.65, 100.25, 100.60)])  # low in the zone, close above it
    assert len(rig.trades) == 1 and summary(rig.trades[0])["at"] == stamp(D3, "10:01")


def test_off_by_default_and_changes_nothing_then():
    plain = Rig(**BASE).feed(day_bars(D1))
    off = {"vol_slot_mult": 0, "vol_slot_sessions": 7, "trend_day_adr": 0, "adr_sessions": 3}
    explicit = Rig(**BASE, **off).feed(day_bars(D1))
    assert plain.algo._slot_volume is None and plain.algo._daily_range is None
    assert explicit.algo._slot_volume is None and explicit.algo._daily_range is None
    assert len(plain.trades) == len(explicit.trades) == 1  # no history needed
    assert summary(plain.trades[0]) == summary(explicit.trades[0])


def test_warm_up_history_feeds_the_filters():
    rig = Rig(**{**VOL, **ADR})
    for x in [*history(), *day_bars(D3, 1500)]:  # appended without update: one update sees all earlier rows
        rig.store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
    rig.algo.update(rig.store.view())
    assert taken(rig)


def test_defaults():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL"})
    got = (algo.vol_slot_mult, algo.vol_slot_sessions, algo.trend_day_adr, algo.adr_sessions)
    assert got == (0, 20, 0, 20)
    on = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "vol_slot_mult": "1.5", "trend_day_adr": 0.3})
    assert (on.vol_slot_mult, on.trend_day_adr) == (1.5, 0.3)
    assert on._slot_volume.sessions == 20 and on._daily_range.sessions == 20
    assert on._slot_volume.bar_minutes == on.zone_minutes == 5


@pytest.mark.parametrize(
    "bad",
    [{"vol_slot_mult": -1}, {"vol_slot_mult": "x"}, {"vol_slot_mult": True}, {"vol_slot_mult": float("nan")},
     {"vol_slot_mult": float("inf")}, {"vol_slot_sessions": 0}, {"vol_slot_sessions": 1.5},
     {"vol_slot_sessions": "x"}, {"vol_slot_sessions": False}, {"trend_day_adr": -0.1},
     {"trend_day_adr": "x"}, {"trend_day_adr": None}, {"adr_sessions": 0}, {"adr_sessions": -3},
     {"adr_sessions": 2.5}, {"adr_sessions": True}],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_bad_parameters_fail_at_construction(bad):
    with pytest.raises(ValueError, match="FVGZone 't'"):
        load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", **bad})


# ----- volume against the previous bars (issue 293) -----

# zone bars of 1 minute: the 28 bars before the middle candle (09:58) are quiet at 1000 shares, so the
# mean of the previous 20 is 1000 with no history day at all
PREV = {**BASE, "vol_prev_mult": 1.5, "vol_prev_bars": 20}


def test_prev_volume_allowed_at_the_multiple_and_blocked_below_it():
    assert taken(Rig(**PREV).feed(day_bars(D3, 1500)))  # 1500 >= 1.5 x 1000: equal is allowed
    assert taken(Rig(**PREV).feed(day_bars(D3, 4000)))
    low = Rig(**PREV).feed(day_bars(D3, 1499))
    assert low.trades == []
    assert low.algo._traded == set()  # blocked: the zone is not used up


def test_prev_volume_short_side():
    assert taken(Rig(**PREV).feed(mirror(day_bars(D3, 1500))))
    assert Rig(**PREV).feed(mirror(day_bars(D3, 1499))).trades == []


def test_prev_volume_mean_is_of_the_bars_before_the_middle_candle_only():
    day = day_bars(D3, 1500)

    def with_volume(volume: int, keep) -> list[B]:
        return [B(x.at, x.o, x.h, x.l, x.c, volume) if keep(x.at) else x for x in day]

    # a busy 09:57 bar (just before the middle candle) is in the mean: (19 * 1000 + 20000) / 20 = 1950
    assert Rig(**PREV).feed(with_volume(20_000, lambda t: t == stamp(D3, "09:57"))).trades == []
    # busy bars after the middle candle are not
    assert taken(Rig(**PREV).feed(with_volume(90_000, lambda t: t >= stamp(D3, "09:59"))))
    # 09:37 is the 21st bar before 09:58: outside the 20
    assert taken(Rig(**PREV).feed(with_volume(90_000, lambda t: t == stamp(D3, "09:37"))))
    # 09:38 is the 20th: inside
    assert Rig(**PREV).feed(with_volume(90_000, lambda t: t == stamp(D3, "09:38"))).trades == []


def test_prev_volume_not_enough_earlier_bars_is_blocked():
    rig = Rig(**{**PREV, "vol_prev_bars": 29}).feed(day_bars(D3, 100_000))  # 28 precede the middle candle
    assert rig.trades == [] and rig.algo._traded == set()
    assert taken(Rig(**{**PREV, "vol_prev_bars": 28}).feed(day_bars(D3, 100_000)))


def test_prev_volume_bars_are_counted_across_the_session_boundary():
    cfg = {**PREV, "vol_prev_bars": 30}  # 28 bars today, so 2 come from the day before
    quiet_day = day_bars(D2)
    assert taken(Rig(**cfg).feed([*quiet_day, *day_bars(D3, 1500)]))
    assert Rig(**cfg).feed([*quiet_day, *day_bars(D3, 1499)]).trades == []
    # the last two bars of the day before are busy: the mean is (28 * 1000 + 2 * 31000) / 30 = 3000,
    # so 1.5 x it is 4500
    busy = [B(x.at, x.o, x.h, x.l, x.c, 31_000) if x.at >= stamp(D2, "09:59") else x for x in quiet_day]
    assert Rig(**cfg).feed([*busy, *day_bars(D3, 4499)]).trades == []
    assert taken(Rig(**cfg).feed([*busy, *day_bars(D3, 4500)]))
    # with no day before, 30 bars are not there: blocked
    assert Rig(**cfg).feed(day_bars(D3, 100_000)).trades == []


def test_prev_volume_works_from_a_warm_up_frame():
    rig = Rig(**PREV)
    for x in [*day_bars(D2), *day_bars(D3, 1500)]:  # appended without update: one update sees all
        rig.store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
    rig.algo.update(rig.store.view())
    assert taken(rig)


def test_prev_volume_counts_zone_bars_not_minutes():
    # 5-minute zone bars: the indicator is fed one volume per COMPLETED zone bar (the sum of its minutes)
    rig = Rig(zone_minutes=5, trend_filter=False, vol_prev_mult=1.5, vol_prev_bars=3)
    rig.feed(run_of(D1, "09:30", quiet(15, vol=100)))  # three zone bars of 500 shares
    ind = rig.algo._prev_volume
    assert [ind.volume(stamp(D1, t)) for t in ("09:30", "09:35", "09:40")] == [500, 500, 500]
    assert ind.mean(stamp(D1, "09:40")) is None  # only two zone bars before it
    rig.feed(run_of(D1, "09:45", quiet(5, vol=900)))
    assert ind.volume(stamp(D1, "09:45")) == 4500 and ind.mean(stamp(D1, "09:45")) == 500.0


# ----- filters_mode -----

ANY = {"filters_mode": "any"}


def test_any_passes_when_one_filter_passes():
    both = {**VOL, **ADR, **ANY}
    assert taken(run(both, day_bars(D3, 1500)))  # both pass
    assert taken(run({**both, "trend_day_adr": 0.5626}, day_bars(D3, 1500)))  # volume only
    assert taken(run(both, day_bars(D3, 1499)))  # trend day only
    assert taken(run(both, day_bars(D3, 1499), flip=True))


def test_any_with_none_passing_blocks_and_leaves_the_zone_unused():
    rig = run({**VOL, **ADR, **ANY, "trend_day_adr": 0.5626}, day_bars(D3, 1499))
    assert rig.trades == [] and rig.algo._traded == set()


def test_any_a_filter_that_is_not_ready_counts_as_not_passed():
    # one history day: the slot mean and the ADR (2 sessions each) are both not ready
    assert run({**VOL, **ADR, **ANY}, day_bars(D3, 100_000), day_bars(D2)).trades == []
    # the previous-bars volume is ready without history: it alone decides under "any"
    mixed = {**VOL, **PREV, **ANY}
    assert taken(run(mixed, day_bars(D3, 1500), day_bars(D2)))
    assert run(mixed, day_bars(D3, 1499), day_bars(D2)).trades == []
    # all three not ready (80 bars are never there): blocked
    cold = {**VOL, **PREV, **ANY, "vol_prev_bars": 80}
    assert run(cold, day_bars(D3, 100_000), day_bars(D2)).trades == []


def test_all_is_the_default_and_a_not_ready_filter_blocks_it():
    mixed = {**VOL, **PREV}
    assert run(mixed, day_bars(D3, 100_000), day_bars(D2)).trades == []  # the slot mean is not ready
    assert taken(run({**mixed, "filters_mode": "all"}, day_bars(D3, 1500)))  # warm: both pass
    assert run(mixed, day_bars(D3, 1499)).trades == []


def test_any_trend_day_without_a_0930_bar_does_not_pass():
    cfg = {**PREV, **ADR, **ANY}
    # no session open: the trend day cannot pass; the volume fails here and passes below
    assert Rig(**cfg).feed([*history(), *day_bars(D3, 1499)[1:]]).trades == []
    assert taken(Rig(**cfg).feed([*history(), *day_bars(D3, 1500)[1:]]))  # volume passes


def test_any_with_three_filters():
    three = {**VOL, **PREV, **ADR, **ANY, "trend_day_adr": 0.5626}
    assert taken(run(three, day_bars(D3, 1500)))
    assert run(three, day_bars(D3, 1499)).trades == []


def test_new_keys_off_by_default():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL"})
    assert (algo.vol_prev_mult, algo.vol_prev_bars, algo.filters_mode) == (0, 20, "all")
    assert algo._prev_volume is None
    on = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "vol_prev_mult": "1.5", "vol_prev_bars": 12})
    assert (on.vol_prev_mult, on.vol_prev_bars) == (1.5, 12)
    assert on._prev_volume.bars == 12
    plain = Rig(**BASE).feed(day_bars(D1))
    explicit = Rig(**BASE, vol_prev_mult=0, vol_prev_bars=7, filters_mode="all").feed(day_bars(D1))
    assert explicit.algo._prev_volume is None
    assert summary(plain.trades[0]) == summary(explicit.trades[0])


@pytest.mark.parametrize(
    "bad",
    [{"vol_prev_mult": -1}, {"vol_prev_mult": "x"}, {"vol_prev_mult": True}, {"vol_prev_mult": float("nan")},
     {"vol_prev_mult": None}, {"vol_prev_bars": 0}, {"vol_prev_bars": 2.5}, {"vol_prev_bars": "x"},
     {"vol_prev_bars": True}, {"filters_mode": "ANY"}, {"filters_mode": ""}, {"filters_mode": None},
     {"filters_mode": 1}, {"filters_mode": True}, {"filters_mode": ["all"]},
     # "any" needs at least two session filters on
     {"filters_mode": "any"}, {"filters_mode": "any", "vol_slot_mult": 1.5},
     {"filters_mode": "any", "vol_prev_mult": 1.5}, {"filters_mode": "any", "trend_day_adr": 0.3},
     {"filters_mode": "any", "vol_slot_mult": 0, "vol_prev_mult": 0, "trend_day_adr": 0.3}],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_bad_new_parameters_fail_at_construction(bad):
    with pytest.raises(ValueError, match="FVGZone 't'"):
        load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", **bad})


@pytest.mark.parametrize("on", [
    {"vol_slot_mult": 1.5, "vol_prev_mult": 1.5}, {"vol_slot_mult": 1.5, "trend_day_adr": 0.3},
    {"vol_prev_mult": 1.5, "trend_day_adr": 0.3},
    {"vol_slot_mult": 1.5, "vol_prev_mult": 1.5, "trend_day_adr": 0.3}])
def test_any_is_accepted_with_two_or_three_filters_on(on):
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "filters_mode": "any", **on})
    assert algo.filters_mode == "any"
