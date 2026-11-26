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
