"""The higher-timeframe trend indicator (issue 280), on hand-built and random bars."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from MainStoinker.indicators import TrendConfig, TrendIndicator, trend_series
from MainStoinker.indicators.fvg import resample_session

D1, D2 = "2024-08-19", "2024-08-20"


def at(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}")


def feed_buckets(ind: TrendIndicator, closes: list[float], *, day: str = D1, minutes: int = 2,
                 start: str = "09:30") -> list[str]:
    """One bucket per close: every minute of it but the last closes at the previous close.

    Returns the direction after each bucket's last minute.
    """
    t = at(day, start)
    out = []
    prev = closes[0]
    for c in closes:
        for k in range(minutes):
            last = k == minutes - 1
            px = c if last else prev
            ind.update(t, px, px + 0.1, px - 0.1, px)
            t += pd.Timedelta(minutes=1)
        prev = c
        out.append(ind.direction)
    return out


# ----- hand-computed cases -----


def test_ema_and_direction_by_hand():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2))  # alpha = 2/3
    dirs = feed_buckets(ind, [10.0, 11.0, 12.0, 11.0, 11.2])
    # e1 = 10, e2 = 10 + 2/3 = 10.6667, e3 = 11.5556, e4 = 11.1852, e5 = 11.1951
    assert dirs == ["none", "up", "up", "down", "up"]
    assert ind.ema == pytest.approx(11.195061728395)
    assert ind.last_close == 11.2
    assert ind.bars_seen == 5
    assert ind.ready
    assert ind.known_at == at(D1, "09:40")


def test_ema_values_at_each_bucket():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2))
    seen = []
    t = at(D1, "09:30")
    for c in [10.0, 11.0, 12.0]:
        for _ in range(2):
            ind.update(t, c, c, c, c)
            t += pd.Timedelta(minutes=1)
        seen.append(ind.ema)
    assert seen == pytest.approx([10.0, 10 + 2 / 3, 10 + 2 / 3 + 2 / 3 * (12 - (10 + 2 / 3))])


def test_not_ready_is_none_and_ready_needs_ema_bars_buckets():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=4))
    dirs = feed_buckets(ind, [10.0, 11.0, 12.0, 13.0, 14.0])
    assert dirs == ["none", "none", "none", "up", "up"]
    assert [ind.ready, ind.bars_seen] == [True, 5]


def test_mirror_gives_down():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2))
    assert feed_buckets(ind, [20.0, 19.0, 18.0, 17.0]) == ["none", "down", "down", "down"]


def test_flat_closes_and_ema_bars_one_are_none():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2))
    assert feed_buckets(ind, [10.0] * 4) == ["none"] * 4
    one = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=1))
    assert feed_buckets(one, [10.0, 11.0, 12.0]) == ["none"] * 3  # the EMA is the last close
    assert one.ready


def test_fresh_indicator_state():
    ind = TrendIndicator()
    assert (ind.direction, ind.ready, ind.ema, ind.last_close, ind.bars_seen, ind.known_at) == (
        "none", False, None, None, 0, None)
    assert ind.config == TrendConfig(bar_minutes=30, ema_bars=20, cross_session=True)


# ----- buckets: completion, alignment, dropped partial bucket -----


def test_a_bucket_counts_only_when_complete():
    ind = TrendIndicator(TrendConfig(bar_minutes=5, ema_bars=1))
    for i in range(4):  # 09:30 to 09:33: four of five minutes
        ind.update(at(D1, f"09:3{i}"), 10, 11, 9, 10 + i)
    assert ind.bars_seen == 0 and ind.ema is None
    ind.update(at(D1, "09:34"), 10, 11, 9, 20.0)  # the last minute completes it at once
    assert (ind.bars_seen, ind.last_close, ind.known_at) == (1, 20.0, at(D1, "09:35"))


def test_a_later_bar_completes_a_bucket_with_a_missing_minute():
    ind = TrendIndicator(TrendConfig(bar_minutes=5, ema_bars=1))
    for hhmm, c in [("09:30", 10), ("09:31", 11), ("09:33", 12)]:  # 09:32 and 09:34 missing
        ind.update(at(D1, hhmm), c, c, c, c)
    assert ind.bars_seen == 0
    ind.update(at(D1, "09:35"), 13, 13, 13, 13)  # the next bucket begins: 09:30 is final
    assert (ind.bars_seen, ind.last_close, ind.known_at) == (1, 12.0, at(D1, "09:35"))


def test_buckets_are_aligned_to_0930_not_to_the_clock():
    # 30-minute buckets from 09:30: 09:30-09:59, 10:00-10:29. The bucket after 09:59 starts at 10:00.
    ind = TrendIndicator(TrendConfig(bar_minutes=30, ema_bars=1))
    frame = _minute_frame(D1, "09:30", 90)
    for row in frame.itertuples():
        ind.update(row.date, row.open, row.high, row.low, row.close)
    assert ind.bars_seen == 3 and ind.known_at == at(D1, "11:00")
    # a 45-minute bucket: 09:30-10:14, 10:15-10:59; both complete by 11:00
    odd = TrendIndicator(TrendConfig(bar_minutes=45, ema_bars=1))
    for row in frame.itertuples():
        odd.update(row.date, row.open, row.high, row.low, row.close)
    assert odd.bars_seen == 2 and odd.known_at == at(D1, "11:00")


def test_bars_before_the_open_are_ignored():
    ind = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=1))
    ind.update(at(D1, "09:28"), 99, 99, 99, 99)
    ind.update(at(D1, "09:29"), 98, 98, 98, 98)
    assert ind.bars_seen == 0
    ind.update(at(D1, "09:30"), 10, 10, 10, 10)
    ind.update(at(D1, "09:31"), 11, 11, 11, 11)
    assert (ind.bars_seen, ind.last_close) == (1, 11.0)


def test_partial_last_bucket_is_dropped_across_a_session_change():
    ind = TrendIndicator(TrendConfig(bar_minutes=5, ema_bars=1))
    for i in range(7):  # 09:30-09:36: bucket 0 complete, bucket 1 has two of five
        ind.update(at(D1, "09:30") + pd.Timedelta(minutes=i), 10 + i, 10 + i, 10 + i, 10.0 + i)
    assert (ind.bars_seen, ind.last_close) == (1, 14.0)
    ind.update(at(D2, "09:30"), 99, 99, 99, 99.0)  # next session: the two-minute bucket is gone
    assert (ind.bars_seen, ind.last_close) == (1, 14.0)
    for i in range(1, 5):
        ind.update(at(D2, "09:30") + pd.Timedelta(minutes=i), 99, 99, 99, 99.0)
    assert (ind.bars_seen, ind.last_close, ind.known_at) == (2, 99.0, at(D2, "09:35"))


def test_buckets_agree_with_resample_session():
    frame = _random_frame(3, seed=1)
    ind = TrendIndicator(TrendConfig(bar_minutes=7, ema_bars=1))
    closes = []
    seen = 0
    for row in frame.itertuples():
        ind.update(row.date, row.open, row.high, row.low, row.close)
        if ind.bars_seen != seen:
            closes.append(ind.last_close)
            seen = ind.bars_seen
    # cross_session: bars_seen keeps counting over the days
    expected = resample_session(frame, 7)["close"].tolist()
    assert closes == expected


# ----- sessions -----


def test_cross_session_carries_the_ema_over_days():
    carried = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2, cross_session=True))
    feed_buckets(carried, [10.0, 11.0], day=D1)
    ema_day1 = carried.ema
    feed_buckets(carried, [12.0], day=D2)
    assert carried.bars_seen == 3 and carried.ready
    assert carried.ema == pytest.approx(ema_day1 + 2 / 3 * (12.0 - ema_day1))
    assert carried.direction == "up"


def test_without_cross_session_the_ema_starts_each_day():
    fresh = TrendIndicator(TrendConfig(bar_minutes=2, ema_bars=2, cross_session=False))
    feed_buckets(fresh, [10.0, 11.0, 12.0], day=D1)
    assert fresh.ready and fresh.direction == "up"
    fresh.update(at(D2, "09:30"), 5, 5, 5, 5.0)  # a new session: nothing carried, nothing ready
    assert (fresh.ready, fresh.direction, fresh.ema, fresh.bars_seen) == (False, "none", None, 0)
    fresh.update(at(D2, "09:31"), 5, 5, 5, 5.0)
    assert (fresh.bars_seen, fresh.ema) == (1, 5.0)
    assert feed_buckets(fresh, [6.0], day=D2, start="09:32") == ["up"]
    assert fresh.ema == pytest.approx(5 + 2 / 3)


# ----- no look-ahead -----


def test_direction_at_each_minute_uses_only_bars_up_to_it():
    frame = _random_frame(3, seed=7)
    cfg = TrendConfig(bar_minutes=10, ema_bars=3)
    full = trend_series(frame, cfg)
    assert len(full) == len(frame)
    for cut in [0, 1, 9, 10, 11, 39, 40, 389, 390, 391, 500, 779, 780, 900, len(frame) - 1]:
        part = trend_series(frame.iloc[: cut + 1], cfg)
        pd.testing.assert_series_equal(part.iloc[-1], full.iloc[cut], check_names=False)
        ind = TrendIndicator(cfg)
        for row in frame.iloc[: cut + 1].itertuples():
            ind.update(row.date, row.open, row.high, row.low, row.close)
        assert ind.direction == full.iloc[cut]["direction"]
    assert set(full["direction"]) == {"up", "down", "none"}


def test_changing_future_bars_changes_nothing_before_them():
    frame = _random_frame(2, seed=3)
    cfg = TrendConfig(bar_minutes=15, ema_bars=2)
    cut = 500
    other = frame.copy()
    other.loc[other.index > cut, ["open", "high", "low", "close"]] *= 3.0
    a, b = trend_series(frame, cfg), trend_series(other, cfg)
    pd.testing.assert_frame_equal(a.iloc[: cut + 1], b.iloc[: cut + 1])
    assert not a.iloc[cut + 1:].equals(b.iloc[cut + 1:])


def test_an_open_bucket_never_moves_the_direction():
    ind = TrendIndicator(TrendConfig(bar_minutes=5, ema_bars=1))
    ind2 = TrendIndicator(TrendConfig(bar_minutes=5, ema_bars=2))
    for i in range(10):
        ind2.update(at(D1, "09:30") + pd.Timedelta(minutes=i), 10, 10, 10, 10.0)
    before = (ind2.direction, ind2.ema)
    for i, px in enumerate([500.0, 0.01, 777.0, 1.0]):  # wild moves inside the open bucket
        ind2.update(at(D1, "09:40") + pd.Timedelta(minutes=i), px, px, px, px)
        assert (ind2.direction, ind2.ema) == before
    assert ind.direction == "none"


def test_trend_series_columns_and_input_untouched():
    frame = _random_frame(1, seed=5)
    copy = frame.copy()
    out = trend_series(frame, TrendConfig(bar_minutes=10, ema_bars=2))
    assert list(out.columns) == ["date", "direction", "ready", "ema", "last_close", "bars_seen", "known_at"]
    assert out["date"].tolist() == frame["date"].tolist()
    assert math.isnan(out["ema"].iloc[0]) and pd.isna(out["known_at"].iloc[0])
    pd.testing.assert_frame_equal(frame, copy)
    indexed = trend_series(frame.set_index("date"), TrendConfig(bar_minutes=10, ema_bars=2))
    pd.testing.assert_frame_equal(indexed, out)


# ----- bad input -----


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field", [0, 1, 2, 3])
def test_non_finite_prices_raise(bad, field):
    ind = TrendIndicator()
    px = [10.0, 10.0, 10.0, 10.0]
    px[field] = bad
    with pytest.raises(ValueError, match="not finite"):
        ind.update(at(D1, "09:30"), *px)


def test_time_going_backwards_or_repeating_raises():
    ind = TrendIndicator()
    ind.update(at(D1, "09:31"), 10, 10, 10, 10)
    with pytest.raises(ValueError, match="not after"):
        ind.update(at(D1, "09:30"), 10, 10, 10, 10)
    with pytest.raises(ValueError, match="not after"):
        ind.update(at(D1, "09:31"), 10, 10, 10, 10)


@pytest.mark.parametrize(
    "kwargs",
    [{"bar_minutes": 1}, {"bar_minutes": 0}, {"bar_minutes": -5}, {"bar_minutes": 2.5}, {"bar_minutes": True},
     {"ema_bars": 0}, {"ema_bars": -1}, {"ema_bars": 1.5}, {"cross_session": "yes"}],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_bad_config_raises(kwargs):
    with pytest.raises(ValueError):
        TrendConfig(**kwargs)


# ----- helpers -----


def _minute_frame(day: str, start: str, n: int, base: float = 100.0) -> pd.DataFrame:
    times = pd.date_range(at(day, start), periods=n, freq="min")
    px = base + np.arange(n) * 0.01
    return pd.DataFrame({"date": times, "open": px, "high": px + 0.05, "low": px - 0.05, "close": px})


def _random_frame(days: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    parts = []
    price = 100.0
    for d in range(days):
        times = pd.date_range(pd.Timestamp("2024-08-19") + pd.Timedelta(days=d, hours=9, minutes=30),
                              periods=390, freq="min")
        steps = np.cumsum(rng.normal(0, 0.05, size=390)) + np.linspace(0, rng.normal(0, 2), 390)
        close = price + steps
        price = float(close[-1])
        parts.append(pd.DataFrame({"date": times, "open": close, "high": close + 0.02,
                                   "low": close - 0.02, "close": close}))
    return pd.concat(parts, ignore_index=True)
