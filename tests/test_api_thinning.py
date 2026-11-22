"""`thinning.thin`: pure thinning of an equity series that keeps the largest drawdown."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

import pytest

from MainStoinker.api.thinning import thin
from MainStoinker.engine.stats import max_drawdown

CAPITAL = Decimal("10000.0000")


@dataclass(frozen=True)
class Pt:
    index: int
    equity: Decimal


def walk(count: int, seed: int = 7) -> list[Pt]:
    rng = random.Random(seed)
    value = CAPITAL
    points = []
    for i in range(count):
        value += Decimal(rng.randint(-500, 480)) / 100
        points.append(Pt(i, value))
    return points


def test_short_series_unchanged() -> None:
    points = walk(50)
    assert thin(points, 50) == points
    assert thin(points, 100) == points
    assert thin([], 10) == []


@pytest.mark.parametrize("max_points", [10, 11, 12, 30, 100, 301])
def test_result_size_bounded(max_points: int) -> None:
    points = walk(5000)
    out = thin(points, max_points, CAPITAL)
    assert 2 <= len(out) <= max_points
    assert len(thin(walk(100_000, seed=3), 10, CAPITAL)) <= 10


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("max_points", [10, 25, 60])
def test_max_drawdown_preserved(seed: int, max_points: int) -> None:
    points = walk(2000, seed)
    out = thin(points, max_points, CAPITAL)
    assert max_drawdown([p.equity for p in out], CAPITAL) == max_drawdown(
        [p.equity for p in points], CAPITAL)


def test_max_drawdown_preserved_when_the_peak_is_the_starting_capital() -> None:
    """A series that only ever falls: the peak is the capital, no point carries it."""
    points = [Pt(i, CAPITAL - 1 - (i * 7919) % 500) for i in range(1000)]
    full = max_drawdown([p.equity for p in points], CAPITAL)
    assert full > 0
    out = thin(points, 10, CAPITAL)
    assert max_drawdown([p.equity for p in out], CAPITAL) == full


def test_first_and_last_kept() -> None:
    points = walk(3000)
    out = thin(points, 40, CAPITAL)
    assert out[0] is points[0] and out[-1] is points[-1]


def test_time_order_kept() -> None:
    points = walk(3000)
    out = thin(points, 40, CAPITAL)
    assert [p.index for p in out] == sorted({p.index for p in out})
    assert len(out) < len(points)


def test_below_minimum_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        thin(walk(100), 5)
