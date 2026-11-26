"""FVGZone higher-timeframe trend filter (issue 280): `htf_minutes` and `htf_ema`.

The retest is at 11:30 after 117 context bars that ramp up or down, so that the 30-minute
buckets 09:30, 10:00, 10:30 and 11:00 (this last one holds the three setup bars) are
complete when the retest bar arrives.
"""
from __future__ import annotations

import pytest

from MainStoinker.Algos.FVGZone.FVGZone_Algo import SERIES
from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.feed import Bar
from tests.fvgzone_support import D1, B, Rig, mirror, scenario, stamp, summary

N_CONTEXT = 117
HTF = {"zone_minutes": 1, "trend_filter": False, "htf_minutes": 30, "htf_ema": 3}


def ramp(start: float, step: float) -> list[tuple]:
    out = []
    for i in range(N_CONTEXT):
        p = round(start + i * step, 2)
        out.append((p, round(p + .10, 2), round(p - .10, 2), p))
    return out


UP = ramp(94.0, 0.05)  # ends at 99.80, the setup then opens at 100
DOWN = ramp(108.0, -0.05)  # ends at 102.20, the setup then drops to 100


def bars_with(context):
    return scenario(D1, "11:30", context=context)


def test_long_is_taken_with_the_trend_up():
    rig = Rig(**HTF).feed(bars_with(UP))
    assert rig.algo._htf.direction == "up"
    (t,) = (summary(x) for x in rig.trades)
    assert (t["side"], t["at"], t["price"]) == (1, stamp(D1, "11:30"), 100.45)


def test_long_is_skipped_with_the_trend_down_and_the_zone_is_not_used_up():
    rig = Rig(**HTF).feed(bars_with(DOWN))
    assert rig.algo._htf.direction == "down"
    assert rig.trades == []
    assert rig.algo._traded == set()  # like a failed trend_filter: the zone can be retested later
    # The same zone, retested again once the trend reads up, is taken. The state is set by hand:
    # reaching it with bars would take hours of context.
    rig.algo._htf.direction = "up"
    rig.feed([B(stamp(D1, "11:31"), 100.40, 100.45, 100.25, 100.40)])
    assert len(rig.trades) == 1 and summary(rig.trades[0])["at"] == stamp(D1, "11:31")


def test_short_is_taken_with_the_trend_down_and_skipped_with_it_up():
    taken = Rig(**HTF).feed(mirror(bars_with(UP)))
    assert taken.algo._htf.direction == "down"
    (t,) = (summary(x) for x in taken.trades)
    assert (t["side"], t["price"]) == (0, 99.55)
    blocked = Rig(**HTF).feed(mirror(bars_with(DOWN)))
    assert blocked.algo._htf.direction == "up"
    assert blocked.trades == []


def test_trend_not_ready_blocks_both_sides():
    cfg = {**HTF, "htf_ema": 5}  # only four buckets have completed
    long_run = Rig(**cfg).feed(bars_with(UP))
    short_run = Rig(**cfg).feed(mirror(bars_with(UP)))
    assert long_run.algo._htf.direction == "none" and not long_run.algo._htf.ready
    assert long_run.trades == [] and short_run.trades == []


def test_it_is_off_by_default_and_changes_nothing_then():
    for context in (UP, DOWN):
        plain = Rig(zone_minutes=1, trend_filter=False).feed(bars_with(context))
        explicit = Rig(zone_minutes=1, trend_filter=False, htf_minutes=0, htf_ema=7).feed(bars_with(context))
        assert plain.algo._htf is None and explicit.algo._htf is None
        assert len(plain.trades) == len(explicit.trades) == 1
        assert summary(plain.trades[0]) == summary(explicit.trades[0])


def test_it_is_checked_in_addition_to_the_trend_filter():
    cfg = {"zone_minutes": 1, "trend_filter": True, "trend_ema": 5, "htf_minutes": 30, "htf_ema": 3}
    # on the rising ramp the VWAP/EMA filter and the htf agree with the long
    assert len(Rig(**cfg).feed(bars_with(UP)).trades) == 1
    # with the 1-minute filter on and the htf off, the falling ramp's decision is the filter's...
    only_filter = Rig(**{**cfg, "htf_minutes": 0}).feed(bars_with(DOWN))
    assert only_filter.trades == []
    # ...and a filter that passes is still not enough when the htf says down
    only_htf_blocks = Rig(**{**cfg, "trend_filter": False}).feed(bars_with(DOWN))
    assert only_htf_blocks.trades == []
    assert len(Rig(**{**cfg, "trend_filter": False, "htf_minutes": 0}).feed(bars_with(DOWN)).trades) == 1


def test_warm_up_history_feeds_the_trend():
    bars = bars_with(UP)
    rig = Rig(**HTF)
    for x in bars:  # appended without update: the algo's first update sees all earlier rows at once
        rig.store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
    rig.algo.update(rig.store.view())
    assert rig.algo._htf.ready and rig.algo._htf.direction == "up"
    assert len(rig.trades) == 1


def test_series_has_the_htf_ema_only_when_on():
    off = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL"})
    assert off.FrontEndDataStruct == [name for name, _ in SERIES]
    on = Rig(**HTF).feed(bars_with(UP))
    assert on.algo.FrontEndDataStruct == [*(name for name, _ in SERIES), "HTFEMA"]
    assert on.algo.FrontEndDataType[-1] == "line"
    column = on.algo.AlgoData["HTFEMA"]
    assert column.iloc[:89].isna().all()  # three 30-minute buckets are complete only from 10:59
    assert column.iloc[-1] == pytest.approx(on.algo._htf.ema)
    assert on.algo.update_frontend()["data"][-1]["name"] == "HTFEMA"
    plain = Rig(zone_minutes=1, trend_filter=False).feed(bars_with(UP))
    assert "HTFEMA" not in plain.algo.AlgoData.columns


def test_defaults():
    algo = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL"})
    assert (algo.htf_minutes, algo.htf_ema) == (0, 20)
    on = load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", "htf_minutes": "30"})
    assert (on.htf_minutes, on.htf_ema, on._htf.config.bar_minutes) == (30, 20, 30)


@pytest.mark.parametrize(
    "bad",
    [{"htf_minutes": 1}, {"htf_minutes": -1}, {"htf_minutes": 391}, {"htf_minutes": 2.5},
     {"htf_minutes": "x"}, {"htf_minutes": True}, {"htf_ema": 0}, {"htf_ema": 1.5},
     {"htf_ema": "x"}, {"htf_ema": False}, {"htf_minutes": 30, "htf_ema": -2}],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_bad_htf_parameters_fail_at_construction(bad):
    with pytest.raises(ValueError, match="FVGZone 't'"):
        load_algo("FVGZone", {"idname": "t", "ticker": "AAPL", **bad})
