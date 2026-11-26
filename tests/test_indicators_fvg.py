"""Fair value gap detector (MainStoinker/indicators/fvg.py).

Hand-built cases per rule, the mirror property, incremental == batch, the old
algo's rule re-implemented here (the algo is not imported), and a per-bar cost
bound by counting (no wall clock, safe under xdist).
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.indicators.fvg import (
    Direction,
    FvgConfig,
    FvgDetector,
    State,
    detect_fvgs,
    resample_session,
)

OFFLINE = Path(__file__).resolve().parent.parent / "OfflineData"
T0 = datetime(2024, 1, 2, 9, 30)
NO_ATR = FvgConfig(min_size_atr=None)


def at(i: int, day: int = 0) -> datetime:
    return T0 + timedelta(days=day, minutes=i)


def feed(det: FvgDetector, bars, start: int = 0, day: int = 0):
    """bars: (open, high, low, close); returns the flat list of events."""
    out = []
    for k, (o, h, lo, c) in enumerate(bars):
        out += det.update(at(start + k, day), o, h, lo, c)
    return out


def kinds(events):
    return [e.kind for e in events]


# candle 1 high 10.00; candle 3 low 10.10: an up gap [10.00, 10.10]
UP3 = [(9.5, 10.0, 9.4, 9.9), (9.9, 10.3, 9.9, 10.2), (10.2, 10.4, 10.1, 10.3)]
# mirror: candle 1 low 10.00; candle 3 high 9.90 -> a down gap [9.90, 10.00]
DOWN3 = [(10.5, 10.6, 10.0, 10.1), (10.1, 10.1, 9.7, 9.8), (9.8, 9.9, 9.6, 9.7)]


def test_up_gap_zone_fields():
    det = FvgDetector(NO_ATR)
    ev = feed(det, UP3)
    assert kinds(ev) == ["formed"]
    z = ev[0].zone
    assert (z.direction, z.top, z.bottom, z.size) == (Direction.UP, 10.10, 10.00, pytest.approx(0.10))
    assert z.midpoint == pytest.approx(10.05)
    assert z.formed_time == at(2) and z.candle_times == (at(0), at(1), at(2))
    assert z.state is State.OPEN and z.touches == 0 and z.age_bars == 0
    assert z.size_atr is None  # fewer than atr_min_bars bars of history


def test_down_gap_zone_fields():
    det = FvgDetector(NO_ATR)
    z = feed(det, DOWN3)[0].zone
    assert (z.direction, z.top, z.bottom) == (Direction.DOWN, 10.00, 9.90)


def test_no_gap_when_candle_ranges_overlap():
    det = FvgDetector(NO_ATR)
    assert feed(det, [(9, 10, 9, 10), (10, 10.5, 9.9, 10.2), (10.2, 10.6, 10.0, 10.5)]) == []


def test_default_needs_no_same_direction_but_old_shape_does():
    # candle 2 is a down candle: textbook gap, not the old three-same-direction shape
    mixed = [(9.5, 10.0, 9.4, 9.9), (10.3, 10.3, 9.9, 10.0), (10.2, 10.4, 10.1, 10.3)]
    assert kinds(feed(FvgDetector(NO_ATR), mixed)) == ["formed"]
    strict = replace(NO_ATR, require_same_direction=True)
    assert feed(FvgDetector(strict), mixed) == []
    assert kinds(feed(FvgDetector(strict), UP3)) == ["formed"]
    assert kinds(feed(FvgDetector(strict), DOWN3)) == ["formed"]
    down_mixed = [(10.5, 10.6, 10.0, 10.1), (9.7, 10.1, 9.7, 10.0), (9.8, 9.9, 9.6, 9.7)]
    assert kinds(feed(FvgDetector(NO_ATR), down_mixed)) == ["formed"]
    assert feed(FvgDetector(strict), down_mixed) == []


@pytest.mark.parametrize("low3,formed", [(10.05, False), (10.06, True), (10.04, False)])
def test_min_size_is_exact_and_exclusive(low3, formed):
    bars = [(9.5, 10.0, 9.4, 9.9), (9.9, 10.3, 9.9, 10.2), (10.2, 10.4, low3, 10.3)]
    assert (kinds(feed(FvgDetector(NO_ATR), bars)) == ["formed"]) is formed


@pytest.mark.parametrize("h1c", [22455, 8, 330, 10010, 29])
def test_exact_threshold_is_refused_whatever_the_float_noise(h1c):
    # float subtraction of two cent prices can land on either side of 0.05; ticks cannot
    def bars(gap_ticks: int):
        h1 = h1c / 100
        return [
            (h1 - 1, h1, h1 - 1.2, h1 - 0.1),
            (h1, h1 + 1, h1, h1 + 0.9),
            (h1 + 0.9, h1 + 1.2, (h1c + gap_ticks) / 100, h1 + 1),
        ]

    assert feed(FvgDetector(NO_ATR), bars(5)) == []
    assert kinds(feed(FvgDetector(NO_ATR), bars(6))) == ["formed"]
    assert kinds(feed(FvgDetector(replace(NO_ATR, min_size_inclusive=True)), bars(5))) == ["formed"]


def test_down_min_size_boundary():
    bars = [(10.5, 10.6, 10.00, 10.1), (10.1, 10.1, 9.7, 9.8), (9.8, 9.95, 9.6, 9.7)]
    assert feed(FvgDetector(NO_ATR), bars) == []
    bars[2] = (9.8, 9.94, 9.6, 9.7)
    assert kinds(feed(FvgDetector(NO_ATR), bars)) == ["formed"]


def test_no_gap_across_two_sessions():
    det = FvgDetector(NO_ATR)
    ev = det.update(datetime(2024, 1, 2, 15, 59), 9.5, 10.0, 9.4, 9.9)
    ev += det.update(datetime(2024, 1, 3, 9, 30), 9.9, 10.3, 9.9, 10.2)
    ev += det.update(datetime(2024, 1, 3, 9, 31), 10.2, 10.4, 10.1, 10.3)
    assert ev == []
    # the same bars inside one session do form a gap
    assert kinds(feed(FvgDetector(NO_ATR), UP3)) == ["formed"]


def test_no_gap_across_a_missing_minute():
    det = FvgDetector(NO_ATR)
    ev = det.update(at(0), *UP3[0])
    ev += det.update(at(2), *UP3[1])  # minute 1 is missing
    ev += det.update(at(3), *UP3[2])
    assert ev == []


def test_bar_minutes_sets_the_spacing():
    cfg = replace(NO_ATR, bar_minutes=5)
    det = FvgDetector(cfg)
    ev = [e for k, b in enumerate(UP3) for e in det.update(at(5 * k), *b)]
    assert kinds(ev) == ["formed"]
    det = FvgDetector(cfg)  # one-minute spacing is refused for 5-minute bars
    assert feed(det, UP3) == []


def test_out_of_order_and_repeated_time_raise():
    det = FvgDetector(NO_ATR)
    det.update(at(1), 1, 2, 1, 2)
    with pytest.raises(ValueError):
        det.update(at(1), 1, 2, 1, 2)
    with pytest.raises(ValueError):
        det.update(at(0), 1, 2, 1, 2)


# ---------- lifecycle ----------


def test_up_gap_open_touched_filled():
    det = FvgDetector(NO_ATR)
    feed(det, UP3)
    # a bar that stays above the zone changes nothing
    assert feed(det, [(10.3, 10.5, 10.2, 10.4)], start=3) == []
    z = det.live_zones()[0]
    assert (z.state, z.touches, z.age_bars) == (State.OPEN, 0, 1)
    # trades into the zone (low 10.06 < top 10.10), depth 0.04 of 0.10
    ev = feed(det, [(10.4, 10.4, 10.06, 10.2)], start=4)
    assert kinds(ev) == ["touched"]
    z = det.live_zones()[0]
    assert (z.state, z.touches) == (State.TOUCHED, 1)
    assert z.deepest_fill == pytest.approx(0.4) and z.last_change_time == at(4)
    # a shallower second visit counts a touch but emits nothing and keeps the depth
    assert feed(det, [(10.2, 10.3, 10.08, 10.2)], start=5) == []
    z = det.live_zones()[0]
    assert (z.touches, z.deepest_fill) == (2, pytest.approx(0.4))
    # reaches the far edge and closes above it: filled, and no longer live
    ev = feed(det, [(10.2, 10.3, 10.00, 10.1)], start=6)
    assert kinds(ev) == ["filled"]
    assert ev[0].zone.deepest_fill == 1.0 and ev[0].zone.state is State.FILLED
    assert det.live_zones() == [] and det.closed_zones()[0].state is State.FILLED


def test_first_bar_into_the_zone_and_through_it_emits_touched_then_the_end():
    det = FvgDetector(NO_ATR)
    feed(det, UP3)
    ev = feed(det, [(10.4, 10.4, 10.0, 10.05)], start=3)
    assert kinds(ev) == ["touched", "filled"]


def test_candle_three_does_not_touch_its_own_zone():
    det = FvgDetector(NO_ATR)
    feed(det, UP3)
    z = det.live_zones()[0]
    assert (z.state, z.touches) == (State.OPEN, 0)  # candle 3 low (10.10) equals top


def test_up_invalidation_close_beyond_far_edge_and_wick_variants():
    close_cfg = NO_ATR
    det = FvgDetector(close_cfg)
    feed(det, UP3)
    ev = feed(det, [(10.3, 10.3, 9.8, 9.95)], start=3)  # closes below 10.00
    assert kinds(ev) == ["touched", "invalidated"]
    # wick below the far edge but close back above: filled by default ...
    det = FvgDetector(close_cfg)
    feed(det, UP3)
    assert kinds(feed(det, [(10.3, 10.3, 9.8, 10.05)], start=3)) == ["touched", "filled"]
    # ... invalidated when the wick rule is chosen
    det = FvgDetector(replace(close_cfg, invalidate_on="wick_beyond_far_edge"))
    feed(det, UP3)
    assert kinds(feed(det, [(10.3, 10.3, 9.8, 10.05)], start=3)) == ["touched", "invalidated"]
    # a wick exactly at the far edge is a fill under both rules
    det = FvgDetector(replace(close_cfg, invalidate_on="wick_beyond_far_edge"))
    feed(det, UP3)
    assert kinds(feed(det, [(10.3, 10.3, 10.0, 10.05)], start=3)) == ["touched", "filled"]


def test_down_gap_lifecycle_is_the_mirror():
    det = FvgDetector(NO_ATR)
    feed(det, DOWN3)  # zone [9.90, 10.00], near edge 9.90
    assert feed(det, [(9.7, 9.85, 9.6, 9.7)], start=3) == []
    ev = feed(det, [(9.7, 9.94, 9.6, 9.8)], start=4)  # high 9.94: 0.04 of 0.10
    assert kinds(ev) == ["touched"]
    assert det.live_zones()[0].deepest_fill == pytest.approx(0.4)
    ev = feed(det, [(9.8, 10.0, 9.7, 9.9)], start=5)
    assert kinds(ev) == ["filled"]
    det = FvgDetector(NO_ATR)
    feed(det, DOWN3)
    assert kinds(feed(det, [(9.8, 10.2, 9.7, 10.05)], start=3)) == ["touched", "invalidated"]


def test_expire_after_bars():
    det = FvgDetector(replace(NO_ATR, expire_after_bars=3))
    feed(det, UP3)
    flat = (10.5, 10.6, 10.12, 10.5)
    assert feed(det, [flat, flat], start=3) == []
    ev = feed(det, [flat], start=5)
    assert kinds(ev) == ["expired"] and ev[0].zone.age_bars == 3
    assert det.live_zones() == []


def test_expire_at_session_end_and_the_switch():
    det = FvgDetector(NO_ATR)
    feed(det, UP3)
    ev = det.update(datetime(2024, 1, 3, 9, 30), 10.5, 10.6, 10.4, 10.5)
    assert kinds(ev) == ["expired"] and det.live_zones() == []
    keep = FvgDetector(replace(NO_ATR, expire_at_session_end=False))
    feed(keep, UP3)
    assert keep.update(datetime(2024, 1, 3, 9, 30), 10.5, 10.6, 10.4, 10.5) == []
    assert len(keep.live_zones()) == 1
    # the carried zone still fills on the next day
    ev = keep.update(datetime(2024, 1, 3, 9, 31), 10.5, 10.5, 10.0, 10.1)
    assert kinds(ev) == ["touched", "filled"]


# ---------- ATR and displacement ----------


def calm(n: int, start: int = 0):
    """n flat bars of range 0.10 around 50: true range 0.10 each."""
    return [(50.0, 50.05, 49.95, 50.0)] * n


def test_atr_filter_and_minimum_history():
    # history of ten bars with true range 0.10; then candle 1 (50.05 high) and a gap of 0.06
    base = calm(10)
    c1 = (50.0, 50.05, 49.95, 50.0)
    c2 = (50.0, 50.2, 50.0, 50.15)
    c3_small = (50.15, 50.3, 50.11, 50.2)  # gap 50.11 - 50.05 = 0.06 < 0.25 * ATR(0.10)? no: 0.025
    cfg = FvgConfig(min_size_atr=0.5)  # needs gap >= 0.05; ATR ~0.10
    ok = feed(FvgDetector(cfg), [*base, c1, c2, c3_small])
    assert kinds(ok) == ["formed"] and ok[0].zone.size_atr == pytest.approx(0.6)
    strict = FvgConfig(min_size_atr=0.7)
    assert feed(FvgDetector(strict), [*base, c1, c2, c3_small]) == []
    assert kinds(feed(FvgDetector(FvgConfig(min_size_atr=None)), [*base, c1, c2, c3_small])) == ["formed"]
    # not enough history (atr_min_bars=5): candle 1 is the 3rd bar
    assert feed(FvgDetector(cfg), [c1, c2, c3_small]) == []
    assert kinds(feed(FvgDetector(replace(cfg, atr_min_bars=1)), [c1, c2, c3_small])) == ["formed"]


def test_atr_window_ends_at_candle_one():
    # candles 2 and 3 are huge; if they were in the mean the filter would pass wrongly
    base = calm(10)
    c1 = (50.0, 50.05, 49.95, 50.0)
    c2 = (50.0, 53.0, 50.0, 52.9)
    c3 = (52.9, 53.2, 50.2, 53.0)  # gap 0.15 against an ATR of 0.10 -> 1.5
    z = feed(FvgDetector(FvgConfig(min_size_atr=1.4)), [*base, c1, c2, c3])
    assert kinds(z) == ["formed"] and z[0].zone.size_atr == pytest.approx(1.5)
    assert feed(FvgDetector(FvgConfig(min_size_atr=1.6)), [*base, c1, c2, c3]) == []


def test_atr_window_is_a_rolling_fixed_size_mean():
    cfg = FvgConfig(min_size_atr=None, atr_bars=4, atr_min_bars=4)
    det = FvgDetector(cfg)
    wide = [(50, 52, 48, 50)] * 6  # true range 4, then (flat after) 0.10
    feed(det, wide)
    feed(det, calm(4), start=6)
    assert len(det._tr) == 4 and det._tr_sum == 4 * 10  # ticks: only the calm bars remain


def test_atr_cross_session_switch():
    # a gap in the third bar of day 2; ATR history is mostly day 1
    day1 = calm(10)
    d2 = [(50.0, 50.05, 49.95, 50.0), (50.0, 50.2, 50.0, 50.15), (50.15, 50.3, 50.11, 50.2)]
    for cross, formed in ((True, True), (False, False)):
        det = FvgDetector(FvgConfig(min_size_atr=0.5, atr_cross_session=cross))
        feed(det, day1)
        ev = feed(det, d2, day=1)
        assert (kinds(ev).count("formed") == 1) is formed


def test_min_displacement_body():
    base = calm(10)
    c1 = (50.0, 50.05, 49.95, 50.0)
    c2 = (50.0, 50.2, 50.0, 50.15)  # body 0.15 vs mean body 0 -> any positive multiple passes
    c3 = (50.15, 50.3, 50.11, 50.2)
    cfg = FvgConfig(min_size_atr=None, min_displacement_body=2.0)
    assert kinds(feed(FvgDetector(cfg), [*base, c1, c2, c3])) == ["formed"]
    # bodies of 0.05 in the history: mean 0.05; candle 2 body 0.15 is 3x
    base2 = [(50.0, 50.1, 49.95, 50.05)] * 10
    bars = [*base2, c1, c2, c3]
    assert kinds(feed(FvgDetector(replace(cfg, min_displacement_body=3.3)), bars)) == ["formed"]
    assert feed(FvgDetector(replace(cfg, min_displacement_body=3.4)), bars) == []
    assert feed(FvgDetector(cfg), [c1, c2, c3]) == []  # not enough history


def test_config_validation():
    for bad in (
        {"min_size": -1}, {"atr_bars": 0}, {"atr_min_bars": 30}, {"bar_minutes": 0},
        {"invalidate_on": "x"}, {"expire_after_bars": 0},
    ):
        with pytest.raises(ValueError):
            FvgConfig(**bad)


# ---------- queries ----------


def test_queries_and_snapshot():
    det = FvgDetector(replace(NO_ATR, expire_at_session_end=False))
    feed(det, UP3)  # up [10.00, 10.10]
    feed(det, [(10.3, 10.3, 10.2, 10.25)], start=3)
    # a second, down gap far above, next day so nothing forms between them: [11.90, 12.00]
    feed(det, [(12.5, 12.6, 12.0, 12.1), (12.1, 12.1, 11.7, 11.8), (11.8, 11.9, 11.6, 11.7)], day=1)
    assert [z.direction for z in det.live_zones()] == [Direction.UP, Direction.DOWN]
    assert [z.direction for z in det.live_zones("down")] == [Direction.DOWN]
    assert det.zone_containing(10.05).direction is Direction.UP
    assert det.zone_containing(11.0) is None
    assert det.zone_containing(10.00) is not None and det.zone_containing(10.10) is not None
    assert det.nearest_zone(11.0).direction is Direction.DOWN  # 0.90 vs 0.90: the tie goes to the newest
    assert det.nearest_zone(10.9).direction is Direction.UP  # 0.80 vs 1.00
    assert det.nearest_zone(11.0, direction="up").direction is Direction.UP
    assert det.nearest_zone(11.0, side="below").direction is Direction.UP
    assert det.nearest_zone(11.0, side="above").direction is Direction.DOWN
    assert det.nearest_zone(13.0, side="above") is None
    assert det.distance_to_nearest(10.9) == pytest.approx(0.80)
    assert det.distance_to_nearest(10.05) == 0.0
    assert det.distance_to_nearest(13.0, side="above") is None
    with pytest.raises(ValueError):
        det.nearest_zone(11.0, side="left")
    snap = det.snapshot()  # last close 11.70
    assert snap["live"] == 2 and snap["live_up"] == 1 and snap["live_down"] == 1
    assert snap["in_zone"] == 0.0 and snap["newest_direction"] == -1.0
    assert snap["dist_above"] == pytest.approx(0.20) and snap["dist_below"] == pytest.approx(1.6)
    assert all(isinstance(v, float) for v in snap.values())
    # copies: changing a returned zone does not change the detector
    det.live_zones()[0].touches = 99
    assert det.live_zones()[0].touches == 0


def test_snapshot_before_any_bar():
    snap = FvgDetector().snapshot()
    assert snap["live"] == 0.0 and snap["in_zone"] == 0.0


def test_detection_does_not_depend_on_anything_but_bars():
    # same bars, same zones, whatever is called in between
    a, b = FvgDetector(NO_ATR), FvgDetector(NO_ATR)
    ea = feed(a, UP3 + DOWN3[:1] * 0)
    for _ in range(3):
        b.snapshot(), b.live_zones(), b.nearest_zone(10.0)
    eb = feed(b, UP3)
    assert [e.zone for e in ea] == [e.zone for e in eb]


# ---------- data helpers ----------


def load(symbol: str, rows: int, skip: int = 0) -> pd.DataFrame:
    cols = ["date", "open", "high", "low", "close", "volume"]
    frame = pd.read_csv(OFFLINE / f"_{symbol}_offlinedata_", usecols=cols)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.iloc[skip : skip + rows].reset_index(drop=True)


def run(frame: pd.DataFrame, cfg: FvgConfig):
    det = FvgDetector(cfg)
    events = []
    times = frame["date"].dt.to_pydatetime()
    rows = zip(times, frame["open"], frame["high"], frame["low"], frame["close"], strict=True)
    for t, o, h, lo, c in rows:
        events += det.update(t, o, h, lo, c)
    return det, events


CONFIGS = [
    FvgConfig(),
    FvgConfig(min_size_atr=None),
    FvgConfig(expire_after_bars=30, invalidate_on="wick_beyond_far_edge"),
    FvgConfig(min_displacement_body=1.0, atr_cross_session=False, expire_at_session_end=False),
]


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_incremental_equals_batch_on_offline_data(symbol):
    frame = load(symbol, 1200)
    before = frame.copy()
    for cfg in CONFIGS:
        det, events = run(frame, cfg)
        batch = detect_fvgs(frame, cfg)
        assert len(batch) > 0
        pd.testing.assert_frame_equal(frame, before)  # never modified
        final = {}
        for e in events:
            final[e.zone.id] = e.zone
        for z in det.live_zones():
            final[z.id] = z
        assert list(batch["id"]) == sorted(final)
        for row in batch.itertuples():
            z = final[row.id]
            assert (row.direction, row.top, row.bottom, row.state) == (
                z.direction.value, z.top, z.bottom, z.state.value
            )
            assert (row.formed_time, row.touches, row.deepest_fill, row.age_bars, row.last_change_time) == (
                z.formed_time, z.touches, z.deepest_fill, z.age_bars, z.last_change_time
            )
            assert (row.time_1, row.time_2, row.time_3) == z.candle_times


def test_batch_keeps_every_zone_beyond_max_closed():
    frame = load("TSLA", 1200)
    cfg = FvgConfig(max_closed=2)
    det, _ = run(frame, cfg)
    assert len(det.closed_zones()) == 2
    assert len(detect_fvgs(frame, cfg)) > 2 + len(det.live_zones())


def test_batch_with_a_datetime_index_and_no_volume():
    frame = load("AAPL", 600).drop(columns="volume")
    expected = detect_fvgs(frame)
    indexed = frame.set_index("date")
    pd.testing.assert_frame_equal(detect_fvgs(indexed), expected)


def mirrored(frame: pd.DataFrame, k_cents: int) -> pd.DataFrame:
    out = frame.copy()
    cents = {c: (frame[c] * 100).round().astype(int) for c in ("open", "high", "low", "close")}
    out["open"] = (k_cents - cents["open"]) / 100
    out["close"] = (k_cents - cents["close"]) / 100
    out["high"] = (k_cents - cents["low"]) / 100
    out["low"] = (k_cents - cents["high"]) / 100
    return out


def assert_mirror(frame: pd.DataFrame, cfg: FvgConfig, k_cents: int) -> int:
    a = detect_fvgs(frame, cfg)
    b = detect_fvgs(mirrored(frame, k_cents), cfg)
    assert len(a) == len(b)
    flip = {"up": "down", "down": "up"}
    assert [flip[d] for d in a["direction"]] == list(b["direction"])
    def cents(col: pd.Series) -> list[int]:
        return list((col * 100).round().astype(int))

    assert [k_cents - x for x in cents(a["bottom"])] == cents(b["top"])
    assert [k_cents - x for x in cents(a["top"])] == cents(b["bottom"])
    for col in ("state", "touches", "formed_time", "age_bars", "last_change_time", "id"):
        assert list(a[col]) == list(b[col])
    assert a["deepest_fill"].tolist() == pytest.approx(b["deepest_fill"].tolist())
    return len(a)


def random_frame(seed: int, n: int = 400) -> pd.DataFrame:
    rng = random.Random(seed)
    price = 5000
    rows = []
    t = datetime(2024, 3, 4, 9, 30)
    for i in range(n):
        o = price
        c = o + rng.randint(-40, 40)
        h = max(o, c) + rng.randint(0, 25)
        lo = min(o, c) - rng.randint(0, 25)
        rows.append((t, o / 100, h / 100, lo / 100, c / 100, 1000))
        price = c
        t += timedelta(minutes=1)
        if i % 130 == 129:  # a new session now and then
            t = datetime(t.year, t.month, t.day, 9, 30) + timedelta(days=1)
        elif rng.random() < 0.02:
            t += timedelta(minutes=1)  # a missing minute
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


@pytest.mark.parametrize("seed", range(12))
def test_mirror_property_on_random_bars(seed):
    cfg = CONFIGS[seed % len(CONFIGS)]
    total = assert_mirror(random_frame(seed), cfg, 100000)
    if seed == 0:
        assert total > 5  # the generator does produce gaps


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_mirror_property_on_offline_data(symbol):
    frame = load(symbol, 900)
    wick = replace(FvgConfig(), invalidate_on="wick_beyond_far_edge", expire_after_bars=20)
    for cfg in (FvgConfig(), wick):
        assert assert_mirror(frame, cfg, 100000) > 0


# ---------- the old rule ----------


def old_rule_zones(frame: pd.DataFrame):
    """FVG_Algo's rule on a continuous series, applied by hand.

    Three bars all `open < close` (or all `open > close`) and a float gap larger
    than 0.05. Triples spanning two dates, with a missing minute, or with a gap
    of exactly five ticks (where float arithmetic and exact arithmetic differ)
    are left out, as the issue says.
    """
    out = set()
    d = frame.reset_index(drop=True)
    for i in range(2, len(d)):
        a, b, c = d.iloc[i - 2], d.iloc[i - 1], d.iloc[i]
        if not (a["date"].date() == b["date"].date() == c["date"].date()):
            continue
        minute = pd.Timedelta(minutes=1)
        if c["date"] - b["date"] != minute or b["date"] - a["date"] != minute:
            continue
        if a["open"] < a["close"] and b["open"] < b["close"] and c["open"] < c["close"]:
            if c["low"] - a["high"] > 0.05 and round((c["low"] - a["high"]) * 100) != 5:
                out.add((c["date"].to_pydatetime(), "up", c["low"], a["high"]))
        elif a["open"] > a["close"] and b["open"] > b["close"] and c["open"] > c["close"]:
            if a["low"] - c["high"] > 0.05 and round((a["low"] - c["high"]) * 100) != 5:
                out.add((c["date"].to_pydatetime(), "down", a["low"], c["high"]))
    return out


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_old_rule_equivalence_on_offline_data(symbol):
    frame = load(symbol, 1500)
    cfg = FvgConfig(require_same_direction=True, min_size_atr=None, min_size=0.05)
    _, events = run(frame, cfg)
    new = {
        (e.zone.formed_time, e.zone.direction.value, e.zone.top, e.zone.bottom)
        for e in events
        if e.kind == "formed"
    }
    old = old_rule_zones(frame)
    assert old, "the slice must contain gaps for this test to mean anything"
    # the exact-0.05 cases are the only ones the new rule may differ on, and it refuses them
    assert new == old


# ---------- cost per bar ----------


def sawtooth(n: int) -> list[tuple[float, float, float, float]]:
    """A 6-bar cycle: an up gap forms and is filled within two bars, so live zones stay few."""
    cycle = [
        (100.0, 100.5, 99.5, 100.2),
        (100.2, 102.0, 100.2, 101.9),
        (101.9, 102.5, 101.0, 102.2),  # gap [100.5, 101.0]
        (102.2, 102.3, 100.4, 100.6),  # fills it
        (100.6, 100.8, 100.2, 100.5),
        (100.5, 100.6, 99.6, 100.0),
    ]
    return [cycle[i % 6] for i in range(n)]


def test_work_per_bar_does_not_grow_with_bars_seen():
    def measure(n: int):
        det = FvgDetector(FvgConfig(max_closed=50))
        most_live = most_visits = 0
        for k, b in enumerate(sawtooth(n)):
            det.update(at(k), *b)
            most_live = max(most_live, len(det.live_zones()))
            most_visits = max(most_visits, det.last_visits)
        return det, most_live, most_visits

    small, live_s, visits_s = measure(300)
    big, live_b, visits_b = measure(1200)
    assert small.bars_seen == 300 and big.bars_seen == 1200
    assert len(big.closed_zones()) == 50  # many zones came and went; history is bounded
    assert live_b == live_s <= 3
    assert visits_b == visits_s <= 3
    assert big.total_visits <= 3 * big.bars_seen
    # per-bar state is fixed size
    cfg = big.config
    assert len(big._tr) <= cfg.atr_bars and len(big._body) <= cfg.atr_bars
    assert len(big._bars) == 3 and len(big._snaps) == 3


# ---------- resample ----------


def test_resample_session_aligns_to_the_open_and_drops_the_incomplete_last_bar():
    frame = load("AAPL", 1000)
    day_one = frame[frame["date"].dt.date == frame["date"].dt.date.iloc[0]]
    assert len(day_one) == 390
    before = frame.copy()
    out = resample_session(frame, 60)
    pd.testing.assert_frame_equal(frame, before)
    first = out[out["date"].dt.date == day_one["date"].dt.date.iloc[0]]
    assert list(first["date"].dt.strftime("%H:%M")) == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30"]
    # 15:30 would hold 30 minutes only: dropped
    row = first.iloc[1]
    chunk = day_one.iloc[60:120]
    assert (row["open"], row["close"]) == (chunk["open"].iloc[0], chunk["close"].iloc[-1])
    assert row["high"] == chunk["high"].max() and row["low"] == chunk["low"].min()
    assert row["volume"] == chunk["volume"].sum()
    five = resample_session(frame, 5)
    assert len(five[five["date"].dt.date == day_one["date"].dt.date.iloc[0]]) == 78
    assert list(resample_session(frame, 1)["close"].iloc[:390]) == list(day_one["close"])


def test_resampled_bars_feed_the_detector_with_five_minute_spacing():
    frame = load("MSFT", 2000)
    bars5 = resample_session(frame, 5)
    cfg = FvgConfig(bar_minutes=5)
    assert len(detect_fvgs(bars5, cfg)) > 0
    assert len(detect_fvgs(bars5, FvgConfig(bar_minutes=1))) == 0
