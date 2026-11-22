"""Trade behaviour in simulated (non-live) mode.

Tests marked xfail(strict=True) document known defects. When one is fixed the
test starts passing, strict mode fails the run, and the marker must be removed.
(None remain in this file.)
"""
import logging

import pandas as pd
import pytest

from MainStoinker.TradeTools.trade import Trade

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0


def make_trade(direction=LONG, price=100.0, volume=10):
    return Trade("AAPL", volume, "t0", price, T0, direction, logging.getLogger("test"))


def bar(close, high=None, low=None, minutes=1):
    return {
        "date": T0 + pd.Timedelta(minutes=minutes),
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }


def test_default_emergency_stop_is_one_percent():
    assert make_trade(LONG).stopPrice == 99.0
    assert make_trade(SHORT).stopPrice == 101.0


def test_trailing_stop_follows_price_up_for_long():
    trade = make_trade(LONG)
    assert trade.check_stoploss(bar(102.5)) == 1
    assert trade.stopPrice == pytest.approx(101.5)


def test_trailing_stop_never_loosens():
    trade = make_trade(LONG)
    trade.check_stoploss(bar(102.5))
    trade.check_stoploss(bar(101.8))
    assert trade.stopPrice == pytest.approx(101.5)


def test_fixed_stop_does_not_trail():
    trade = make_trade(LONG)
    trade.set_stopLossType("Fixed")
    trade.check_stoploss(bar(105.0))
    assert trade.stopPrice == 99.0


def test_long_stop_hit_closes_at_stop_price():
    trade = make_trade(LONG)
    assert trade.check_stoploss(bar(98.0)) == 0
    assert trade.get_status() == "Closed"
    assert trade.closePrice == 99.0


def test_short_stop_hit_closes_at_stop_price():
    trade = make_trade(SHORT)
    assert trade.check_stoploss(bar(102.0)) == 0
    assert trade.closePrice == 101.0


def test_take_profit_closes_long_when_high_exceeds_target():
    trade = make_trade(LONG)
    trade.create_tp(103.0, 10)
    assert trade.check_tp(bar(102.0, high=102.5)) == 0
    assert trade.check_tp(bar(102.8, high=103.2)) == 1
    assert trade.closePrice == 103.0


def test_long_profit_is_close_minus_open():
    trade = make_trade(LONG)
    trade.close_position(104.0, T0 + pd.Timedelta(minutes=5))
    # issue 4: profit is (close - open) * volume (volume 10)
    assert trade.get_profit() == pytest.approx(40.0)


def test_short_profit_is_open_minus_close():
    trade = make_trade(SHORT)
    trade.close_position(96.0, T0 + pd.Timedelta(minutes=5))
    assert trade.get_profit() == pytest.approx(40.0)  # (100 - 96) * volume 10


def test_to_json_works_for_simulated_trade():
    trade = make_trade(LONG)
    trade.close_position(104.0, T0 + pd.Timedelta(minutes=5))
    # issue 4: profit is (close - open) * volume, so 40.0 (was 4.0 per share)
    assert trade.to_json()["profit"] == pytest.approx(40.0)


def test_open_simulated_trade_reports_status():
    assert make_trade(LONG).get_status() == "Open"


def test_short_fixed_stop_set_by_algo_does_not_trail_and_closes_at_stop():
    # The call sequence the opening-range algos use on entry.
    trade = make_trade(SHORT, price=100.0)
    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(103.0)
    assert trade.stopPrice == 103.0
    assert trade.check_stoploss(bar(95.0)) == 1  # price falls: no trailing
    assert trade.stopPrice == 103.0
    assert trade.check_stoploss(bar(103.0)) == 1  # at the stop is not a stop-out
    assert trade.check_stoploss(bar(103.5)) == 0
    assert trade.closePrice == 103.0
    assert trade.get_status() == "Closed"
