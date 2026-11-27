"""Volume against the previous bars (issue 293): hand-computed cases, no look-ahead, across sessions."""
from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from MainStoinker.indicators import PrevBarsVolume

D1, D2, D3 = "2024-08-19", "2024-08-20", "2024-08-21"


def at(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}")


def feed(ind: PrevBarsVolume, day: str, start: str, volumes: list[int]) -> list[pd.Timestamp]:
    """Consecutive 5-minute bars from `start`; returns their labels."""
    t0 = at(day, start)
    labels = [t0 + pd.Timedelta(minutes=5 * i) for i in range(len(volumes))]
    for label, volume in zip(labels, volumes, strict=True):
        ind.update(label, volume)
    return labels


def test_by_hand():
    ind = PrevBarsVolume(3)
    a, b, c, d, e = feed(ind, D1, "09:30", [10, 20, 30, 100, 50])
    # the first three bars have fewer than 3 bars before them
    assert [ind.mean(x) for x in (a, b, c)] == [None, None, None]
    assert ind.at_least(c, 1.0) is None
    assert ind.mean(d) == 20.0  # (10 + 20 + 30) / 3
    assert ind.volume(d) == 100
    assert ind.relative_volume(d) == pytest.approx(5.0)
    assert ind.mean(e) == 50.0  # (20 + 30 + 100) / 3: the bar itself is not in it
    assert ind.at_least(d, 5.0) is True  # 100 * 3 = 300 >= 5 * 60 exactly
    assert ind.at_least(d, 5.01) is False
    assert ind.at_least(e, 1.0) is True  # 50 * 3 = 150 >= 150
    assert ind.at_least(e, 1.01) is False
    assert ind.at_least(d.hour * 60 + d.minute, 5.0) is True  # a minute of day works as the start


def test_the_bars_are_counted_across_the_session_boundary():
    ind = PrevBarsVolume(3)
    feed(ind, D1, "15:50", [10, 20])
    first, second = feed(ind, D2, "09:30", [30, 100])
    assert ind.mean(first) is None  # only two bars before it (both of the day before)
    assert ind.mean(second) == 20.0  # (10 + 20 + 30) / 3: two bars of D1, one of D2
    assert ind.at_least(second, 5.0) is True


def test_no_look_ahead_the_answer_of_a_bar_never_changes():
    ind = PrevBarsVolume(2)
    a, b, c = feed(ind, D1, "09:30", [4, 6, 10])
    first = (ind.mean(c), ind.at_least(c, 2.0), ind.relative_volume(c))
    assert first == (5.0, True, 2.0)
    later = feed(ind, D1, "10:00", [1000, 1, 1])
    assert (ind.mean(c), ind.at_least(c, 2.0), ind.relative_volume(c)) == first
    assert ind.mean(later[0]) == 8.0  # (6 + 10) / 2


def test_a_bar_not_fed_yet_or_of_an_earlier_date_is_unavailable():
    ind = PrevBarsVolume(1)
    a, b = feed(ind, D1, "09:30", [5, 7])
    assert ind.at_least(at(D1, "11:00"), 1.0) is None and ind.volume(at(D1, "11:00")) is None
    feed(ind, D2, "09:30", [9])
    assert ind.volume(a) is None and ind.mean(b) is None  # the memory holds the current date only
    assert ind.volume(at(D2, "09:30")) == 9


def test_zero_mean_has_no_relative_volume_but_compares():
    ind = PrevBarsVolume(2)
    _, _, c = feed(ind, D1, "09:30", [0, 0, 5])
    assert ind.mean(c) == 0.0 and ind.relative_volume(c) is None
    assert ind.at_least(c, 100.0) is True  # 5 * 2 >= 100 * 0


def test_exact_comparison_does_not_depend_on_float_division():
    equal = PrevBarsVolume(20)
    labels = feed(equal, D1, "09:30", [1000] * 20 + [1500])
    assert equal.at_least(labels[20], 1.5) is True  # equal is allowed: 1500 * 20 == 1.5 * 20000
    below = PrevBarsVolume(20)
    labels = feed(below, D1, "09:30", [1000] * 20 + [1499])
    assert below.at_least(labels[20], 1.5) is False


def test_matches_a_brute_force_oracle():
    rng = random.Random(293)
    ind = PrevBarsVolume(4)
    seen: list[int] = []
    for day in (D1, D2, D3):
        volumes = [rng.randint(0, 5000) for _ in range(12)]
        for label, volume in zip(feed(ind, day, "09:30", volumes), volumes, strict=True):
            before = seen[-4:]
            seen.append(volume)
            if len(before) < 4:
                assert ind.mean(label) is None and ind.at_least(label, 1.3) is None
            else:
                assert ind.mean(label) == pytest.approx(sum(before) / 4)
                assert ind.at_least(label, 1.3) == (volume * 4 >= 1.3 * sum(before))


def test_memory_is_bounded_by_a_session_and_the_window():
    ind = PrevBarsVolume(5)
    for n in range(40):
        day = (pd.Timestamp(D1) + pd.Timedelta(days=n)).strftime("%Y-%m-%d")
        feed(ind, day, "09:30", [100] * 78)
        assert len(ind._window) == 5 and len(ind._today) == 78


@pytest.mark.parametrize("bars", [0, -1, 2.5, "3", True, None])
def test_bad_bars(bars):
    with pytest.raises(ValueError, match="bars"):
        PrevBarsVolume(bars)


@pytest.mark.parametrize("volume", [-1, math.nan, math.inf, "x", True, None])
def test_bad_volume(volume):
    with pytest.raises(ValueError, match="volume"):
        PrevBarsVolume(2).update(at(D1, "09:30"), volume)


def test_time_must_move_forward():
    ind = PrevBarsVolume(2)
    ind.update(at(D1, "09:30"), 1)
    for bad in (at(D1, "09:30"), at(D1, "09:25")):
        with pytest.raises(ValueError, match="not after"):
            ind.update(bad, 1)


@pytest.mark.parametrize("multiple", [0, -1.5, math.nan, math.inf, "x", True])
def test_bad_multiple_is_an_error_even_when_nothing_is_known(multiple):
    ind = PrevBarsVolume(2)
    with pytest.raises(ValueError, match="multiple"):
        ind.at_least(at(D1, "09:30"), multiple)


def test_bad_start():
    ind = PrevBarsVolume(2)
    for bad in ("09:30", None, True, 1.5):
        with pytest.raises(ValueError, match="start"):
            ind.volume(bad)
