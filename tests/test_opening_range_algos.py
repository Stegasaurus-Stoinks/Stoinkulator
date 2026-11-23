"""OpeningRangeCandle and FridayFrenzy: daily reset, end-of-day close, gap check
and the same-bar crashes (issues 16 and 12).

Both algos are driven through the real classes with small synthetic bar
sequences, one `update(frame)` call per bar on a growing frame, the way the
engine does it. Every test runs against both algo ids so the two stay in step.

Price conventions used by the scenario builders (base price `b`):
first candle high b+2, low b-2 (so range 4); a quiet bar stays inside the
range. The first bar after the opening candle only moves the state machine
from init to waiting and is not evaluated (existing behaviour).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from tests import harness

ALGO_IDS = ["OpeningRangeCandle", "FridayFrenzy"]
COLUMNS = ["date", "time", "open", "high", "low", "close", "volume"]

D1, D2 = "2024-08-19", "2024-08-20"

OHLC = tuple[float, float, float, float]


class Bar(NamedTuple):
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float


def day(date: str, ohlc: list[OHLC], start: str = "09:30") -> list[Bar]:
    """Consecutive 1-minute bars from `start` on `date`."""
    t0 = pd.Timestamp(f"{date} {start}")
    return [Bar(t0 + pd.Timedelta(minutes=i), *x) for i, x in enumerate(ohlc)]


def at(date: str, hhmm: str, ohlc: OHLC) -> list[Bar]:
    return day(date, [ohlc], hhmm)


def plus(date: str, hhmm: str, minutes: int) -> str:
    return (pd.Timestamp(f"{date} {hhmm}") + pd.Timedelta(minutes=minutes)).strftime("%H:%M")


# ------------------------------------------------------------------ scenarios


def first_candle(b: float) -> OHLC:
    return (b, b + 2, b - 2, b + 1)


def quiet(b: float) -> OHLC:
    return (b, b + 1, b - 1, b)


def breakout_up(b: float) -> OHLC:  # opens inside, closes above the upper bound b+2
    return (b + 1, b + 4, b + 0.5, b + 3)


def drift_up(b: float) -> OHLC:  # stays above the bound without touching it: no retest
    return (b + 3, b + 4, b + 2.5, b + 3.5)


def retest_up(b: float) -> OHLC:  # dips to the bound, closes above it: retest, enter at b+2.5
    return (b + 3, b + 3.5, b + 1.9, b + 2.5)


def breakout_down(b: float) -> OHLC:  # opens inside, closes below the lower bound b-2
    return (b - 1, b - 0.5, b - 4, b - 3)


def retest_down(b: float) -> OHLC:  # rallies to the bound, closes below it: enter at b-2.5
    return (b - 3, b - 1.5, b - 3.5, b - 2.5)


def target_bar(b: float, direction: str, rr: float) -> OHLC:
    if direction == "long":
        tp = b + 2 + rr * 4
        return (b + 2.5, tp + 0.5, b + 2, tp - 1)
    tp = b - 2 - rr * 4
    return (b - 2.5, b - 2, tp - 0.5, tp + 1)


def hold_bar(b: float, direction: str) -> OHLC:  # in a trade, touches neither stop nor target
    if direction == "long":
        return (b + 2.5, b + 3, b + 2, b + 2.5)
    return (b - 2.5, b - 2, b - 3, b - 2.5)


def setup(b: float, direction: str, rr: float = 3, *, with_target: bool = True) -> list[OHLC]:
    """first candle, quiet, breakout, retest (entry), and optionally the target bar."""
    if direction == "long":
        bars = [first_candle(b), quiet(b), breakout_up(b), retest_up(b)]
    else:
        bars = [first_candle(b), quiet(b), breakout_down(b), retest_down(b)]
    if with_target:
        bars.append(target_bar(b, direction, rr))
    return bars


# ------------------------------------------------------------------------ rig


class Rig:
    """One algo instance fed bar by bar, with a SimBroker recording its orders."""

    def __init__(self, algo_id: str, rr: float = 3) -> None:
        self.broker = SimBroker()
        self.algo = load_algo(algo_id, {"idname": "T", "ticker": "TST", "RRRatio": rr})
        self.algo.broker = self.broker  # type: ignore[attr-defined]
        # two warm-up bars from an earlier day: the algos read AlgoData.iloc[-2]
        self.rows: list[tuple] = []
        for ts in ("2024-08-16 15:58", "2024-08-16 15:59"):
            self._add(Bar(pd.Timestamp(ts), 90, 91, 89, 90))

    def _add(self, bar: Bar) -> None:
        self.rows.append(
            (bar.ts, int(bar.ts.timestamp()), bar.open, bar.high, bar.low, bar.close, 100)
        )

    def feed(self, bars: list[Bar]) -> None:
        for bar in bars:
            self._add(bar)
            frame = pd.DataFrame(self.rows, columns=COLUMNS)
            frame["date"] = frame["date"].astype("datetime64[us]")
            frame = frame.astype({"time": "int64", "volume": "int64"})
            self.algo.update(frame)  # type: ignore[attr-defined]

    @property
    def state(self) -> str:
        return self.algo.TradeState.current_state_value  # type: ignore[attr-defined]

    @property
    def trades(self) -> list[dict]:
        return harness.trade_records(self.algo)

    def calls(self, method: str) -> list:
        return [c for c in self.broker.calls if c.method == method]


def trade_tuple(rec: dict) -> tuple:
    return (rec["direction"], rec["openTime"], rec["openPrice"], rec["closeTime"], rec["closePrice"])


@pytest.fixture(params=ALGO_IDS)
def algo_id(request: pytest.FixtureRequest) -> str:
    return request.param


# ---------------------------------------------------------------------- tests


def test_first_candle_of_each_day_sets_the_range(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100)]))
    assert rig.state == "initState"
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (102, 98)
    rig.feed(day(D1, [quiet(100)], "09:31"))
    assert rig.state == "waitingState"
    rig.feed(day(D1, [quiet(100)], "09:32"))
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (102, 98)

    # day 2: the first bar is the new range, not a continuation of day 1
    rig.feed(day(D2, [first_candle(200)]))
    assert rig.state == "initState"
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (202, 198)
    rig.feed(day(D2, [quiet(200)], "09:31"))
    assert rig.state == "waitingState"
    rig.feed(day(D2, [quiet(200)], "09:32"))
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (202, 198)
    assert rig.trades == []


def test_one_trade_per_day_and_reset_next_day(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, setup(100, "long")))
    assert rig.state == "doneTradingState"
    assert len(rig.trades) == 1
    rig.feed(day(D2, setup(200, "long")))
    assert rig.state == "doneTradingState"
    first, second = rig.trades
    assert (first["id"], second["id"]) == ("T0", "T1")
    assert trade_tuple(first) == (1, f"{D1} 09:33:00", 102.5, f"{D1} 09:34:00", 114.0)
    assert trade_tuple(second) == (1, f"{D2} 09:33:00", 202.5, f"{D2} 09:34:00", 214.0)
    assert len(rig.calls("open_position")) == 2


def test_no_second_trade_same_day_after_close(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, setup(100, "long")))  # closes at the target on the 09:34 bar
    assert rig.state == "doneTradingState"
    # a second complete, valid setup later the same day
    rig.feed(day(D1, [breakout_up(100), retest_up(100), target_bar(100, "long", 3)], "10:00"))
    assert rig.state == "doneTradingState"
    assert len(rig.trades) == 1
    assert len(rig.calls("open_position")) == 1


def test_retest_timeout_ends_the_day_but_next_day_trades(algo_id):
    rig = Rig(algo_id)
    timeout = rig.algo.retestTimeout
    # breakout at 09:32; first bar strictly more than `timeout` minutes later gives up
    rig.feed(day(D1, [first_candle(100), quiet(100), breakout_up(100), drift_up(100)]))
    assert rig.state == "breakoutState"
    # exactly `timeout` minutes after the breakout is still within the limit
    rig.feed(at(D1, plus(D1, "09:32", timeout), drift_up(100)))
    assert rig.state == "breakoutState"
    rig.feed(at(D1, plus(D1, "09:32", timeout + 1), drift_up(100)))
    assert rig.state == "doneTradingState"
    # a perfect retest afterwards is ignored for the rest of the day
    rig.feed(at(D1, plus(D1, "09:32", timeout + 2), retest_up(100)))
    assert rig.state == "doneTradingState"
    assert rig.trades == []

    rig.feed(day(D2, setup(200, "long")))
    assert [trade_tuple(t) for t in rig.trades] == [
        (1, f"{D2} 09:33:00", 202.5, f"{D2} 09:34:00", 214.0)
    ]


@pytest.mark.parametrize(
    "late_bar",
    [
        pytest.param(retest_up, id="retest_condition_met"),
        pytest.param(lambda b: (b + 1, b + 1.5, b + 0.5, b + 1), id="closed_back_inside"),
    ],
)
def test_retest_timeout_bar_cannot_enter_or_fail(algo_id, late_bar):
    """Issue 12a: the bar that times out the retest must not also act on it."""
    rig = Rig(algo_id)
    timeout = rig.algo.retestTimeout
    rig.feed(day(D1, [first_candle(100), quiet(100), breakout_up(100)]))
    assert rig.state == "breakoutState"
    rig.feed(at(D1, plus(D1, "09:32", timeout + 1), late_bar(100)))
    assert rig.state == "doneTradingState"
    assert rig.trades == []
    assert rig.broker.calls == ()
    assert rig.algo.inTrade is False


@pytest.mark.parametrize("direction", ["long", "short"])
def test_stop_and_take_profit_on_same_bar_close_once(algo_id, direction):
    """Issue 12b: a bar through both stop and target closes once, at the stop."""
    rig = Rig(algo_id)
    rig.feed(day(D1, setup(100, "long" if direction == "long" else "short", with_target=False)))
    assert rig.state == "inTradeState"
    if direction == "long":  # stop 98, target 114: high above target, close below stop
        both = (102.5, 115, 90, 95)
        stop = 98.0
    else:  # stop 102, target 86: low below target, close above stop
        both = (97.5, 110, 85, 105)
        stop = 102.0
    rig.feed(at(D1, "09:34", both))
    assert rig.state == "doneTradingState"
    assert rig.algo.inTrade is False
    closes = rig.calls("close_position")
    assert len(closes) == 1
    assert closes[0].args["price"] == stop  # the stop is checked first, and fills at the stop
    assert closes[0].args["time"] == pd.Timestamp(f"{D1} 09:34")
    (rec,) = rig.trades
    assert (rec["closeTime"], rec["closePrice"]) == (f"{D1} 09:34:00", stop)


def test_breakout_candle_that_gapped_outside_the_range_is_ignored(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100), quiet(100)]))
    assert rig.state == "waitingState"
    # opens above the range and closes above it: gapped across the bound
    rig.feed(at(D1, "09:32", (103, 105, 102.5, 104)))
    assert rig.state == "waitingState"
    # opens exactly on the bound: not strictly inside, also ignored
    rig.feed(at(D1, "09:33", (102, 105, 101.5, 104)))
    assert rig.state == "waitingState"
    # gapped below and closed below
    rig.feed(at(D1, "09:34", (97, 97.5, 95, 96)))
    assert rig.state == "waitingState"
    # a later candle that opens inside and closes beyond the bound is a breakout
    rig.feed(at(D1, "09:35", breakout_up(100)))
    assert rig.state == "breakoutState"
    assert rig.algo.direction == "up"
    assert rig.algo.breakout_time == pd.Timestamp(f"{D1} 09:35")


def test_end_of_day_close_at_cutoff(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, setup(100, "long", with_target=False)))
    assert rig.state == "inTradeState"
    rig.feed(day(D1, [hold_bar(100, "long")] * 5, "15:50"))  # 15:50 .. 15:54
    assert rig.state == "inTradeState"
    rig.feed(at(D1, "15:55", hold_bar(100, "long")))  # the cut-off minute itself: still open
    assert rig.state == "inTradeState"
    assert rig.calls("close_position") == []
    rig.feed(at(D1, "15:56", (102.5, 103.2, 102.4, 103.1)))  # first bar after the cut-off
    assert rig.state == "doneTradingState"
    assert rig.algo.inTrade is False
    closes = rig.calls("close_position")
    assert len(closes) == 1
    assert closes[0].args["price"] == 103.1
    assert closes[0].args["time"] == pd.Timestamp(f"{D1} 15:56")
    rig.feed(day(D1, [hold_bar(100, "long")] * 3, "15:57"))
    assert len(rig.calls("close_position")) == 1
    assert [trade_tuple(t) for t in rig.trades] == [
        (1, f"{D1} 09:33:00", 102.5, f"{D1} 15:56:00", 103.1)
    ]


@pytest.mark.parametrize(
    "retest_at, enters",
    [("15:54", True), ("15:55", False), ("15:57", False)],
)
def test_no_entry_at_or_after_cutoff(algo_id, retest_at, enters):
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100), quiet(100)]))
    rig.feed(at(D1, "15:50", breakout_up(100)))
    assert rig.state == "breakoutState"
    n_drift = int(retest_at[-2:]) - 51
    rig.feed(day(D1, [drift_up(100)] * n_drift, "15:51"))
    rig.feed(at(D1, retest_at, retest_up(100)))
    if enters:
        assert rig.state == "inTradeState"
        assert len(rig.trades) == 1
    else:
        assert rig.state == "doneTradingState"
        assert rig.trades == []
        assert rig.broker.calls == ()


def test_breakout_state_reaching_cutoff_is_done_for_the_day(algo_id):
    rig = Rig(algo_id)
    rig.feed(day(D1, [first_candle(100), quiet(100)]))
    rig.feed(at(D1, "15:50", breakout_up(100)))
    rig.feed(at(D1, "15:54", drift_up(100)))
    assert rig.state == "breakoutState"
    rig.feed(at(D1, "15:55", drift_up(100)))
    assert rig.state == "doneTradingState"
    assert rig.trades == []


def test_trade_still_open_at_day_change_is_closed_before_reset(algo_id, caplog):
    rig = Rig(algo_id)
    rig.feed(day(D1, setup(100, "long", with_target=False)))
    rig.feed(day(D1, [hold_bar(100, "long")] * 2, "15:29"))  # half-day session ends 15:30
    assert rig.state == "inTradeState"
    assert rig.algo.inTrade is True

    with caplog.at_level(logging.WARNING):
        rig.feed(day(D2, [first_candle(200)]))  # close 201

    closes = rig.calls("close_position")
    assert len(closes) == 1
    assert closes[0].args["price"] == 201
    assert closes[0].args["time"] == pd.Timestamp(f"{D2} 09:30")
    assert [trade_tuple(t) for t in rig.trades] == [
        (1, f"{D1} 09:33:00", 102.5, f"{D2} 09:30:00", 201.0)
    ]
    assert any(
        r.levelno == logging.WARNING and r.name.startswith("algo:") for r in caplog.records
    )
    # ... and that same bar is the new day's opening candle
    assert rig.state == "initState"
    assert rig.algo.inTrade is False
    assert (rig.algo.upperbound, rig.algo.lowerbound) == (202, 198)
    rig.feed(day(D2, [quiet(200)], "09:31"))
    assert rig.state == "waitingState"


@pytest.mark.parametrize("direction", ["long", "short"])
def test_long_and_short_setups(algo_id, direction):
    rr = 2
    rig = Rig(algo_id, rr=rr)
    rig.feed(day(D1, setup(100, direction, rr, with_target=False)))
    assert rig.state == "inTradeState"
    (open_call,) = rig.calls("open_position")
    (tp_call,) = rig.calls("set_take_profit")
    trade = rig.algo.trade
    if direction == "long":  # range 98..102, entry at the retest close 102.5
        assert open_call.args["direction"] == 1
        assert open_call.args["price"] == 102.5
        assert trade.stopPrice == 98  # opposite side of the range
        assert tp_call.args["price"] == 110.0  # 102 + 2 * 4
    else:
        assert open_call.args["direction"] == 0
        assert open_call.args["price"] == 97.5
        assert trade.stopPrice == 102
        assert tp_call.args["price"] == 90.0  # 98 - 2 * 4
    assert tp_call.args["quantity"] == 10
    assert trade.volume == 10
    assert rig.algo.tp == tp_call.args["price"]

    rig.feed(at(D1, "09:34", target_bar(100, direction, rr)))
    assert rig.state == "doneTradingState"
    (rec,) = rig.trades
    assert rec["closePrice"] == tp_call.args["price"]


# Captured from the golden files as they were before issue 16: the first replay
# day (2024-08-19) trade of each algo. Neither the gap check nor the end-of-day
# close changes it, so it must still be there when only that day is replayed.
FIRST_DAY = [
    (
        "OpeningRangeCandle",
        {"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3},
        {
            "id": "AAPL-orc0",
            "symbol": "AAPL",
            "volume": 10,
            "direction": 0,
            "openTime": "2024-08-19 09:33:00",
            "openPrice": 225.08,
            "closeTime": "2024-08-19 10:17:00",
            "closePrice": 223.34,
            "status": "Closed",
        },
    ),
    (
        "FridayFrenzy",
        {"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3},
        {
            "id": "TSLA-ff0",
            "symbol": "TSLA",
            "volume": 10,
            "direction": 0,
            "openTime": "2024-08-19 09:43:00",
            "openPrice": 216.47,
            "closeTime": "2024-08-19 09:48:00",
            "closePrice": 219.47,
            "status": "Closed",
        },
    ),
]


@pytest.mark.parametrize("algo_id, cfg, expected", FIRST_DAY, ids=["orc_aapl", "ff_tsla"])
def test_single_day_behaviour_matches_before_change(algo_id, cfg, expected):
    # warm-up day 2024-08-16 plus the first replay day 2024-08-19 only
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]], max_rows=2 * 390)
    (algo,) = harness.run_offline_backtest(algo_id, [cfg])
    assert harness.trade_records(algo) == [expected]
