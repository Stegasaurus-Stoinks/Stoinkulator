"""Volume by time slot and the daily range (issue 282): hand-computed cases and a brute-force oracle."""
from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from MainStoinker.indicators import DailyRange, SlotVolume

D1, D2, D3, D4, D5 = "2024-08-19", "2024-08-20", "2024-08-21", "2024-08-22", "2024-08-23"


def at(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}")


def feed_volumes(ind: SlotVolume, day: str, items: list[tuple[str, int]]) -> None:
    for hhmm, vol in items:
        ind.update(at(day, hhmm), vol)


# ----- SlotVolume -----


def test_slot_volume_by_hand():
    ind = SlotVolume(2, sessions=2)
    feed_volumes(ind, D1, [("09:30", 1), ("09:31", 2), ("09:32", 3), ("09:33", 4)])
    assert ind.volume(at(D1, "09:30")) == 3
    assert ind.volume(at(D1, "09:32")) == 7
    assert ind.mean(at(D1, "09:30")) is None  # no earlier session yet
    feed_volumes(ind, D2, [("09:30", 5), ("09:31", 6), ("09:32", 7), ("09:33", 8)])
    assert (ind.volume(at(D2, "09:30")), ind.volume(at(D2, "09:32"))) == (11, 15)
    assert ind.mean(at(D2, "09:30")) is None  # one earlier session of the two needed
    assert ind.history_count(at(D2, "09:30")) == 1
    feed_volumes(ind, D3, [("09:30", 10), ("09:31", 20)])
    start = at(D3, "09:30")
    assert ind.volume(start) == 30
    assert ind.mean(start) == 7.0  # (3 + 11) / 2
    assert ind.relative_volume(start) == pytest.approx(30 / 7)
    assert ind.at_least(start, 1.5) is True
    assert ind.at_least(start, 4.2857) is True  # 30 * 2 = 60 >= 4.2857 * 14 = 59.9998
    assert ind.at_least(start, 4.2858) is False  # 4.2858 * 14 = 60.0012
    assert ind.at_least(at(D3, "09:32"), 1.0) is None  # that bucket has not completed
    assert ind.volume(at(D3, "09:32")) is None


def test_the_current_session_is_not_in_its_own_mean():
    ind = SlotVolume(1, sessions=1)
    ind.update(at(D1, "09:30"), 100)
    ind.update(at(D1, "09:31"), 1)
    assert ind.mean(at(D1, "09:30")) is None
    ind.update(at(D2, "09:30"), 5000)
    ind.update(at(D2, "09:31"), 1)
    assert ind.mean(at(D2, "09:30")) == 100.0  # D1 only: D2's own 5000 is not in it
    ind.update(at(D3, "09:30"), 7)
    assert ind.mean(at(D3, "09:30")) == 5000.0  # D2 joined when D3 began


def test_history_is_the_most_recent_sessions_that_have_the_slot_across_half_days():
    ind = SlotVolume(60, sessions=2)
    feed_volumes(ind, D1, [("09:30", 100), ("10:30", 200), ("11:30", 300), ("12:30", 400), ("13:30", 500)])
    feed_volumes(ind, D2, [("09:30", 10), ("10:30", 20), ("11:30", 30)])  # a half day: no afternoon
    full = [("09:30", 1000), ("10:30", 2000), ("11:30", 3000), ("12:30", 4000), ("13:30", 5000)]
    feed_volumes(ind, D3, full)
    assert ind.mean(at(D3, "11:30")) is None  # slot 11:30: D1 only (D2's last bucket never completed)
    assert ind.history_count(at(D3, "11:30")) == 1
    feed_volumes(ind, D4, [("09:30", 50), ("10:30", 60), ("11:30", 70), ("12:30", 80)])
    assert ind.mean(at(D4, "09:30")) == (10 + 1000) / 2  # D1 fell out of the 2 most recent
    assert ind.mean(at(D4, "10:30")) == (20 + 2000) / 2
    assert ind.mean(at(D4, "11:30")) == (300 + 3000) / 2  # D2 has no 11:30: skipped, D1 and D3 count
    assert ind.mean(at(D4, "12:30")) == (400 + 4000) / 2
    assert ind.volume(at(D4, "12:30")) is None  # D4's 12:30 bucket is not complete yet
    assert ind.at_least(at(D4, "12:30"), 1.0) is None
    ind.update(at(D4, "13:30"), 90)
    assert ind.volume(at(D4, "12:30")) == 80
    assert ind.at_least(at(D4, "12:30"), 0.03) is True  # 80 * 2 = 160 >= 0.03 * 4400 = 132
    assert ind.at_least(at(D4, "12:30"), 0.04) is False  # 160 < 176



def test_a_missing_minute_is_allowed_and_a_trailing_partial_bucket_is_dropped():
    ind = SlotVolume(5, sessions=1)
    feed_volumes(ind, D1, [("09:30", 1), ("09:31", 2), ("09:33", 4), ("09:35", 10), ("09:36", 20)])
    assert ind.volume(at(D1, "09:30")) == 7  # 09:32 and 09:34 missing: complete once 09:35 arrived
    assert ind.volume(at(D1, "09:35")) is None  # still open
    ind.update(at(D2, "09:30"), 1)
    ind.update(at(D2, "09:31"), 1)
    assert ind.mean(at(D2, "09:30")) == 7.0
    assert ind.mean(at(D2, "09:35")) is None  # D1's 09:35 bucket was cut by the session end: dropped


def test_bars_before_the_open_are_ignored_and_buckets_align_to_0930():
    ind = SlotVolume(30, sessions=1)
    feed_volumes(ind, D1, [("04:00", 999), ("09:29", 999), ("09:30", 1), ("09:59", 2), ("10:00", 5),
                           ("10:29", 6), ("10:30", 100)])
    assert ind.volume(at(D1, "09:30")) == 3
    assert ind.volume(at(D1, "10:00")) == 11
    assert ind.volume(570) == 3  # a minute of day works too (09:30)


def test_the_multiple_is_exact_for_a_decimal():
    ind = SlotVolume(1, sessions=1)
    ind.update(at(D1, "09:30"), 10)
    ind.update(at(D1, "09:31"), 1)
    ind.update(at(D2, "09:30"), 3)
    ind.update(at(D2, "09:31"), 1)
    # 3 >= 0.3 x 10 exactly (as floats, 0.3 * 10 is 3.0000000000000004)
    assert ind.at_least(at(D2, "09:30"), 0.3) is True
    ind2 = SlotVolume(1, sessions=1)
    for hhmm, v in (("09:30", 10), ("09:31", 1)):
        ind2.update(at(D1, hhmm), v)
    for hhmm, v in (("09:30", 2), ("09:31", 1)):
        ind2.update(at(D2, hhmm), v)
    assert ind2.at_least(at(D2, "09:30"), 0.3) is False


def test_a_zero_mean_slot_has_no_relative_volume_but_at_least_is_still_exact():
    ind = SlotVolume(1, sessions=1)
    for day in (D1, D2):
        ind.update(at(day, "09:30"), 0)
        ind.update(at(day, "09:31"), 1)
    assert ind.relative_volume(at(D2, "09:30")) is None
    assert ind.at_least(at(D2, "09:30"), 1.5) is True  # 0 >= 0


@pytest.mark.parametrize("bar_minutes", [0, -1, 1.5, "5", True, None])
def test_slot_volume_bad_bar_minutes(bar_minutes):
    with pytest.raises(ValueError, match="bar_minutes"):
        SlotVolume(bar_minutes)  # type: ignore[arg-type]


@pytest.mark.parametrize("sessions", [0, -3, 2.5, "20", False, None])
def test_bad_sessions(sessions):
    with pytest.raises(ValueError, match="sessions"):
        SlotVolume(5, sessions)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sessions"):
        DailyRange(sessions)  # type: ignore[arg-type]


@pytest.mark.parametrize("volume", [math.nan, math.inf, -1, "x", None, True])
def test_slot_volume_bad_volume(volume):
    ind = SlotVolume(5)
    with pytest.raises(ValueError, match="volume"):
        ind.update(at(D1, "09:30"), volume)


def test_slot_volume_time_must_increase_and_queries_validate():
    ind = SlotVolume(5)
    ind.update(at(D1, "09:31"), 1)
    for when in (at(D1, "09:31"), at(D1, "09:30")):
        with pytest.raises(ValueError, match="not after"):
            ind.update(when, 1)
    for bad in (0, -1, math.nan, math.inf, "x", True, None):
        with pytest.raises(ValueError, match="multiple"):
            ind.at_least(at(D1, "09:30"), bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a time"):
        ind.volume("09:30")  # type: ignore[arg-type]


# ----- DailyRange -----


def bar(ind: DailyRange, day: str, hhmm: str, o: float, h: float, lo: float, c: float | None = None) -> None:
    ind.update(at(day, hhmm), o, h, lo, o if c is None else c)


def test_daily_range_by_hand():
    ind = DailyRange(sessions=2)
    assert ind.session_open is None and ind.adr is None and not ind.ready
    bar(ind, D1, "09:30", 10, 12, 9)
    bar(ind, D1, "09:31", 11, 15, 10)  # session range 15 - 9 = 6
    assert ind.session_open == 10
    bar(ind, D2, "09:30", 20, 21, 19)
    bar(ind, D2, "09:31", 20, 21, 19)  # range 2
    assert ind.session_open == 20 and ind.sessions_seen == 1 and not ind.ready and ind.adr is None
    assert ind.distance_at_least(100, 0.1) is None  # not ready: blocked by the caller
    bar(ind, D3, "09:30", 30, 31, 29)
    assert ind.ready and ind.sessions_seen == 2
    assert ind.adr == 4.0 and ind.range_sum == 8 and ind.session_open == 30
    assert ind.distance_at_least(1.2, 0.3) is True  # 1.2 * 2 = 2.4 >= 0.3 * 8 = 2.4, exact
    assert ind.distance_at_least(1.19, 0.3) is False
    assert ind.distance_at_least(-5, 0.3) is False  # the sign is the caller's: a negative distance fails


def test_the_current_session_is_not_in_the_adr_and_the_window_is_bounded():
    ind = DailyRange(sessions=2)
    for day, high in ((D1, 11), (D2, 12), (D3, 14)):
        bar(ind, day, "09:30", 10, high, 10)
    assert ind.adr == 1.5  # D1 (1) and D2 (2); D3's own 4 is not in it
    bar(ind, D3, "09:31", 10, 100, 10)  # a wild move today changes nothing
    assert ind.adr == 1.5
    bar(ind, D4, "09:30", 10, 10, 10)
    assert ind.adr == (2 + 90) / 2  # D1 fell out
    assert ind.range_sum == 92


def test_a_session_that_starts_after_0930_has_no_session_open_but_still_counts_in_the_adr():
    ind = DailyRange(sessions=1)
    bar(ind, D1, "09:31", 10, 13, 10)
    assert ind.session_open is None
    bar(ind, D1, "09:32", 10, 10, 8)
    assert ind.session_open is None
    bar(ind, D2, "09:30", 50, 50, 50)
    assert ind.session_open == 50 and ind.adr == 5.0  # D1: 13 - 8


def test_daily_range_ignores_bars_before_the_open():
    ind = DailyRange(sessions=1)
    bar(ind, D1, "04:00", 1, 1000, 0)
    bar(ind, D1, "09:30", 10, 12, 9)
    bar(ind, D2, "09:29", 1, 1000, 0)
    assert ind.session_open == 10 and ind.sessions_seen == 0  # the pre-open bar did not start D2
    bar(ind, D2, "09:30", 20, 20, 20)
    assert ind.adr == 3.0


def test_integer_input_is_exact():
    ind = DailyRange(sessions=3)
    for i, day in enumerate((D1, D2, D3, D4)):
        bar(ind, day, "09:30", 10000, 10000 + 7 + i, 10000)  # cents
    assert ind.range_sum == 7 + 8 + 9
    assert ind.adr == 8.0
    assert ind.distance_at_least(2.4, 0.3) is True  # 2.4 * 3 = 7.2 >= 0.3 * 24 = 7.2
    assert ind.distance_at_least(2.3, 0.3) is False


@pytest.mark.parametrize("bad", [(math.nan, 1, 1, 1), (1, math.inf, 1, 1), (1, 1, -math.inf, 1),
                                 (1, 1, 1, math.nan), (1, 1, 2, 1), ("x", 1, 1, 1), (None, 1, 1, 1)])
def test_daily_range_bad_bars(bad):
    ind = DailyRange()
    with pytest.raises(ValueError):
        ind.update(at(D1, "09:30"), *bad)


def test_daily_range_time_must_increase_and_distance_validates():
    ind = DailyRange(sessions=1)
    bar(ind, D1, "09:30", 1, 1, 1)
    with pytest.raises(ValueError, match="not after"):
        bar(ind, D1, "09:30", 1, 1, 1)
    for bad in (0, -1, math.nan, "x", True):
        with pytest.raises(ValueError, match="multiple"):
            ind.distance_at_least(1, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="distance"):
        ind.distance_at_least(math.nan, 0.3)


# ----- an oracle: pandas group-bys over random sessions, half days and missing minutes -----


def random_sessions(seed: int, days: int = 8) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    for k in range(days):
        day = (pd.Timestamp(D1) + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
        end = rng.choice([390, 390, 210, 120])  # full days and half days
        px = 100.0 + rng.random()
        for m in range(end):
            if m and rng.random() < 0.1:
                continue  # a missing minute
            o = round(px, 2)
            px += rng.uniform(-0.3, 0.3)
            h, lo = round(max(o, px) + rng.random() * 0.1, 2), round(min(o, px) - rng.random() * 0.1, 2)
            when = at(day, "09:30") + pd.Timedelta(minutes=m)
            rows.append((when, o, h, lo, round(px, 2), rng.randint(0, 5000)))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_against_a_brute_force_oracle(seed):
    frame = random_sessions(seed)
    bars = int(random.Random(seed).choice([5, 15, 30]))
    sessions = 3
    frame["day"] = frame["date"].dt.date
    frame["slot"] = ((frame["date"].dt.hour * 60 + frame["date"].dt.minute - 570) // bars) * bars + 570
    # a bucket is complete when a later bar of its session exists or all its minutes are there
    per_day = {}
    for day, grp in frame.groupby("day"):
        last = grp["date"].max()
        buckets = {}
        for slot, g in grp.groupby("slot"):
            done = (g["date"].max() < last) or len(g) == bars
            if done:
                buckets[int(slot)] = int(g["volume"].sum())
        per_day[day] = (buckets, int(grp["high"].mul(100).round().max() - grp["low"].mul(100).round().min()))
    days = sorted(per_day)

    slot_ind, range_ind = SlotVolume(bars, sessions), DailyRange(sessions)
    checked = 0
    for when, o, h, lo, c, v in zip(frame["date"], frame["open"], frame["high"], frame["low"], frame["close"],
                                    frame["volume"], strict=True):
        slot_ind.update(when, int(v))
        range_ind.update(when, round(o * 100), round(h * 100), round(lo * 100), round(c * 100))
        day = when.date()
        i = days.index(day)
        earlier = days[:i]
        for slot, volume in per_day[day][0].items():
            if slot_ind.volume(slot) is None:
                continue
            assert slot_ind.volume(slot) == volume
            have = [per_day[d][0][slot] for d in earlier if slot in per_day[d][0]][-sessions:]
            if len(have) < sessions:
                assert slot_ind.mean(slot) is None and slot_ind.at_least(slot, 1.5) is None
            else:
                assert slot_ind.mean(slot) == sum(have) / sessions
                assert slot_ind.at_least(slot, 1.5) is (volume * sessions >= 1.5 * sum(have))
                checked += 1
        ranges = [per_day[d][1] for d in earlier][-sessions:]
        if len(ranges) < sessions:
            assert not range_ind.ready and range_ind.adr is None
        else:
            assert range_ind.range_sum == sum(ranges) and range_ind.adr == sum(ranges) / sessions
    assert checked > 50
