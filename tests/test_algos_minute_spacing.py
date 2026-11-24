"""Algos that assume the rows of the price frame are one minute apart (issue 144, item 4).

Since the engine skips a symbol for a minute without a bar (decision 6), an
algo can receive a frame with real gaps. These tests record what goes wrong
as `xfail(strict=True)`, as the testing conventions require for a known
defect: fixing an algo means removing its marker in the same change. No algo
is changed by this task, and the intended behaviour of each is the owner's
decision (the asserted outcome is the least surprising one, not a decision).

Each defect has a control test (not xfail) that runs the same algo on
contiguous bars and shows the behaviour the defect departs from.

Findings, with line numbers of the algo files at the time of writing:

- FVG (`Algos/FVG/FVG_Algo.py`, lines 87 to 119): the last three rows
  (`StockData.iloc[-1]`, `[-2]`, `[-3]`) are taken as three consecutive
  candles. With a gap between them, the "gap" between row -3's high and row
  -1's low can be price movement over many minutes, not a fair-value gap, and
  a false FVG is recorded (the time stored is that of row -2). The rows may
  also span two sessions (the first bars of a day use the previous day's last
  rows); that case is the same assumption.
- OpeningRangeCandle (`Algos/OpeningRangeCandle/OpeningRangeCandle_Algo.py`,
  lines 92 to 110) and FridayFrenzy (`Algos/FridayFrenzy/FridayFrenzy_Algo.py`,
  lines 86 to 102, the same code): the first bar the algo sees on a calendar
  day becomes the "first candle" (`firstcandle`, `upperbound`, `lowerbound`).
  Nothing checks it is the 09:30 bar. When 09:30 is missing, the range of a
  later minute is used for the whole day.
- EMACrossing (`Algos/EMACrossing/EMACrossing_Algo.py`, lines 82 to 88): the
  end-of-day close fires only on a bar later than 15:55 of the current bar's
  own date. When every bar after 15:55 is missing, the next bar is on the next
  day (its own 15:55 is still ahead), so the position is carried overnight.
"""
from __future__ import annotations

import pandas as pd
import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from tests.test_opening_range_algos import ALGO_IDS, D1, D2, Rig, day, first_candle, quiet

COLUMNS = ["date", "time", "open", "high", "low", "close", "volume"]


def frame_of(rows: list[tuple[str, float, float, float, float]]) -> list[pd.DataFrame]:
    """The growing frames an algo is updated with, one per row from the second on
    (the algos read the second to last row, so the first rows are warm-up)."""
    frames = []
    for n in range(2, len(rows) + 1):
        frame = pd.DataFrame(
            [(pd.Timestamp(t), int(pd.Timestamp(t).timestamp()), o, h, low, c, 100)
             for t, o, h, low, c in rows[:n]],
            columns=COLUMNS,
        )
        frame["date"] = frame["date"].astype("datetime64[us]")
        frames.append(frame.astype({"time": "int64", "volume": "int64"}))
    return frames


# ------------------------------------------------------------------------- FVG


def fvg_rows(third: str) -> list[tuple[str, float, float, float, float]]:
    """Two quiet warm-up bars, then three up candles; the third is at `third` (HH:MM)."""
    return [
        ("2024-08-19 09:58", 100.0, 100.5, 99.5, 100.0),
        ("2024-08-19 09:59", 100.0, 100.5, 99.5, 100.0),
        ("2024-08-19 10:00", 100.0, 101.0, 99.9, 100.8),   # first candle, high 101.0
        ("2024-08-19 10:01", 100.8, 102.0, 100.7, 101.9),  # up candle
        (f"2024-08-19 {third}", 101.9, 103.0, 101.5, 102.8),  # low 101.5: 0.5 above the first high
    ]


def run_fvg(third: str) -> list:
    algo = load_algo("FVG", {"idname": "T", "ticker": "TST", "RRRatio": 3})
    algo.broker = SimBroker()  # type: ignore[attr-defined]
    for frame in frame_of(fvg_rows(third)):
        algo.update(frame)  # type: ignore[attr-defined]
    return list(algo.FVGs)  # type: ignore[attr-defined]


def test_fvg_control_three_consecutive_candles_make_a_gap() -> None:
    assert len(run_fvg("10:02")) == 1


@pytest.mark.xfail(strict=True, reason="FVG treats three rows as three consecutive candles "
                   "(FVG_Algo.py lines 87-119): minutes 10:02 to 10:06 are missing, so the "
                   "price jump over five minutes is recorded as a fair-value gap")
def test_fvg_is_not_formed_across_missing_minutes() -> None:
    assert run_fvg("10:07") == []


# --------------------------------------------- OpeningRangeCandle and FridayFrenzy


@pytest.fixture(params=ALGO_IDS)
def algo_id(request: pytest.FixtureRequest) -> str:
    return request.param


def test_opening_range_control_the_0930_bar_sets_the_range(algo_id: str) -> None:
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100), quiet(100)]))
    rig.feed(day(D2, [first_candle(200)]))
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (202, 198)


@pytest.mark.xfail(strict=True, reason="the first bar seen on a day is taken as the opening "
                   "candle without checking it is the 09:30 bar (OpeningRangeCandle_Algo.py "
                   "lines 92-110, FridayFrenzy_Algo.py lines 86-102)")
def test_opening_range_is_not_taken_from_a_later_minute(algo_id: str) -> None:
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100), quiet(100)]))
    rig.feed(day(D2, [first_candle(200)], "09:31"))  # 09:30 missing: 09:31 becomes the range
    assert (rig.algo.upperbound, rig.algo.lowerbound) != (202, 198)


# ------------------------------------------------------------------- EMACrossing


def ema_rows(last_minute: str) -> list[tuple[str, float, float, float, float]]:
    """A fall then a rise (the trend crosses up and a trade opens), bars one minute apart from
    15:00 to `last_minute`, then the first bar of the next day."""
    rows = []
    price = 100.0
    stamp = pd.Timestamp("2024-08-19 15:00")
    end = pd.Timestamp(f"2024-08-19 {last_minute}")
    n = 0
    while stamp <= end:
        price += -1.0 if n < 15 else 2.0
        rows.append((str(stamp), price, price + 0.5, price - 0.5, price))
        stamp += pd.Timedelta(minutes=1)
        n += 1
    rows.append(("2024-08-20 09:30", price + 1, price + 1.5, price + 0.5, price + 1))
    return rows


def run_ema(last_minute: str) -> tuple[list[bool], object]:
    algo = load_algo("EMACrossing", {"idname": "T", "ticker": "TST", "short": 2, "long": 4})
    algo.broker = SimBroker()  # type: ignore[attr-defined]
    states = []
    for frame in frame_of(ema_rows(last_minute)):
        algo.update(frame)  # type: ignore[attr-defined]
        states.append(bool(algo.inTrade))  # type: ignore[attr-defined]
    return states, algo


def test_ema_control_a_bar_after_1555_closes_the_position() -> None:
    states, algo = run_ema("15:56")
    assert any(states)  # a trade was opened
    assert states[-2] is False  # the 15:56 bar closed it, before the next day's bar
    assert algo.trades[0].closeTime is not None  # type: ignore[attr-defined]


@pytest.mark.xfail(strict=True, reason="the end-of-day close needs a bar after 15:55 on the same "
                   "date (EMACrossing_Algo.py lines 82-88): with 15:56 to 15:59 missing the "
                   "position is carried into the next session")
def test_ema_does_not_carry_a_position_overnight_when_the_last_minutes_are_missing() -> None:
    states, _ = run_ema("15:54")
    assert any(states)  # a trade was opened
    assert states[-1] is False  # the next day's first bar finds it still open
