"""Unit tests for the FVG gap class and one engine test on real bars.

The lifecycle is pinned as the code defines it, including two quirks that are
recorded as-is (not endorsed):
- an up-gap's first test and its retest can happen on the same bar, because
  the TESTED check runs straight after the VALID check in `updateFVG`;
- a down-gap is invalidated before it can be "retested" by a close inside it.
"""
from pathlib import Path

import pandas as pd

from MainStoinker.Algos.FVG.FVG import FVG
from tests import harness


def bar(o: float, h: float, low: float, c: float) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": low, "close": c})


def up_gap() -> FVG:
    # upperbound, lowerbound, time, direction (1 = up candles formed it)
    return FVG(10.0, 9.0, 0, 1)


def down_gap() -> FVG:
    return FVG(10.0, 9.0, 0, 0)


def test_new_gap_is_valid():
    gap = up_gap()
    assert gap.Status == "VALID"
    assert gap.range == 1.0


def test_up_gap_retest_sequence():
    gap = up_gap()
    # No contact with the gap: stays VALID.
    gap.updateFVG(bar(11.5, 12.0, 11.2, 11.8))
    assert gap.Status == "VALID"
    # Pulls back into the top of the gap but closes above it, wick small:
    # first test. The same call then runs the TESTED check, so it ends RETESTED.
    gap.updateFVG(bar(11.0, 11.1, 9.95, 10.9))
    assert gap.Status == "RETESTED"


def test_up_gap_retested_is_sticky_until_invalidated():
    gap = up_gap()
    gap.updateFVG(bar(11.0, 11.1, 9.95, 10.9))
    assert gap.Status == "RETESTED"
    gap.updateFVG(bar(10.9, 11.0, 10.5, 10.8))  # still above the gap
    assert gap.Status == "RETESTED"
    gap.updateFVG(bar(10.5, 10.6, 8.5, 8.8))  # closes below lowerbound
    assert gap.Status == "INVALID"


def test_up_gap_fully_tested_when_low_sweeps_range():
    gap = up_gap()
    gap.updateFVG(bar(11.0, 11.1, 8.9, 10.9))
    assert gap.Status == "FULLYTESTED"


def test_up_gap_invalidated_by_close_below_lowerbound():
    gap = up_gap()
    gap.updateFVG(bar(9.5, 9.6, 8.4, 8.9))
    assert gap.Status == "INVALID"


def test_traded_gap_is_left_alone():
    gap = up_gap()
    gap.Status = "TRADED"
    assert gap.updateFVG(bar(9.5, 9.6, 8.4, 8.9)) == "TRADED"
    assert gap.Status == "TRADED"


def test_down_gap_retest_sequence():
    gap = down_gap()
    # Pokes above lowerbound but closes below it: first test.
    gap.updateFVG(bar(8.8, 9.2, 8.7, 8.9))
    assert gap.Status == "TESTED"
    # Next bar again trades above lowerbound, closes at or below it: RETESTED.
    gap.updateFVG(bar(8.8, 9.1, 8.6, 8.8))
    assert gap.Status == "RETESTED"


def test_down_gap_tested_holds_without_a_retest():
    gap = down_gap()
    gap.updateFVG(bar(8.8, 9.2, 8.7, 8.9))
    assert gap.Status == "TESTED"
    gap.updateFVG(bar(8.8, 8.9, 8.5, 8.6))  # high stays below lowerbound
    assert gap.Status == "TESTED"


def test_down_gap_fully_tested_when_high_sweeps_range():
    gap = down_gap()
    gap.updateFVG(bar(8.8, 10.2, 8.7, 8.9))
    assert gap.Status == "FULLYTESTED"


def test_down_gap_invalidated_by_close_above_lowerbound():
    gap = down_gap()
    gap.updateFVG(bar(9.2, 9.8, 9.1, 9.5))
    assert gap.Status == "INVALID"


def test_down_gap_invalidated_after_tested():
    gap = down_gap()
    gap.updateFVG(bar(8.8, 9.2, 8.7, 8.9))
    assert gap.Status == "TESTED"
    gap.updateFVG(bar(9.0, 9.9, 8.9, 9.5))
    assert gap.Status == "INVALID"


def test_checkifinrange_is_exclusive():
    gap = up_gap()
    assert gap.checkifinrange(9.5)
    assert not gap.checkifinrange(9.0)
    assert not gap.checkifinrange(10.0)


def test_engine_run_reaches_retested_on_first_replay_day(monkeypatch):
    """Real AAPL bars, first replay day only (390 bars after 2 warm-up days):
    at least one gap must reach RETESTED inside updateFVG."""
    harness.stage_offline_data(Path.cwd(), ["AAPL"], max_rows=3 * 390)
    seen: list[str] = []
    original = FVG.updateFVG

    def spy(self, current):
        result = original(self, current)
        seen.append(self.Status)
        return result

    monkeypatch.setattr(FVG, "updateFVG", spy)
    harness.run_offline_backtest("FVG", [{"idname": "AAPL-fvg", "ticker": "AAPL", "RRRatio": 3}])
    assert "RETESTED" in seen
