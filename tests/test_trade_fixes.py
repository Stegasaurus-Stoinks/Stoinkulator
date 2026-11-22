"""Legacy Trade fixes: profit, to_json, open status, get_stats, update_tp, printStats.

Issues 3, 4, 5 and 7. None of these tests touches the live order path.
"""
import logging
import math

import pandas as pd
import pytest

from MainStoinker.Algos.ParentAlgo import ParentAlgo
from MainStoinker.TradeTools.trade import Trade

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0
LOG = logging.getLogger("test")


def make_trade(direction=LONG, price=100.0, volume=10, ID="t0", broker=None):
    return Trade("AAPL", volume, ID, price, T0, direction, LOG, broker=broker)


def closed(direction, close, volume=10, ID="t0"):
    trade = make_trade(direction, volume=volume, ID=ID)
    trade.close_position(close, T0 + pd.Timedelta(minutes=5))
    return trade


class RecordingBroker:
    def __init__(self):
        self.tp_calls = []

    def open_position(self, **kwargs):
        return "ref"

    def set_take_profit(self, ref, price, quantity):
        self.tp_calls.append((ref, price, quantity))


def test_pnl_long_short_volume():
    assert closed(LONG, 104.0)._pnl() == pytest.approx(40.0)
    assert closed(LONG, 96.0)._pnl() == pytest.approx(-40.0)
    assert closed(SHORT, 96.0)._pnl() == pytest.approx(40.0)
    assert closed(SHORT, 104.0)._pnl() == pytest.approx(-40.0)
    assert closed(LONG, 104.0, volume=3)._pnl() == pytest.approx(12.0)
    assert closed(SHORT, 96.0, volume=3)._pnl() == pytest.approx(12.0)
    for trade in (closed(LONG, 104.0), closed(SHORT, 104.0)):
        assert trade.get_profit() == trade._pnl()


def test_get_stats_short_sign_and_volume():
    stats = closed(SHORT, 96.0, volume=5).get_stats(Fulldisplay=False)
    assert stats["PL"] == pytest.approx(20.0)
    assert stats["openPrice"] == 100.0
    assert stats["closePrice"] == 96.0
    stats = closed(SHORT, 104.0, volume=5).get_stats(Fulldisplay=False)
    assert stats["PL"] == pytest.approx(-20.0)
    assert closed(LONG, 104.0, volume=5).get_stats(Fulldisplay=False)["PL"] == pytest.approx(20.0)


def test_get_stats_short_display_uses_tradeID(capsys):
    trade = closed(SHORT, 96.0, ID="abc")
    trade.get_stats(Fulldisplay=False)
    out = capsys.readouterr().out
    assert "Trade  abc" in out
    assert "Profit:  40.0" in out
    trade.get_stats(Fulldisplay=True)
    assert "ID:  abc" in capsys.readouterr().out


@pytest.mark.parametrize("with_broker", [False, True])
def test_to_json_open_sim_trade(with_broker):
    trade = make_trade(LONG, broker=RecordingBroker() if with_broker else None)
    assert trade.get_status() == "Open"
    assert trade.in_position() is True
    data = trade.to_json()
    assert data["status"] == "Open"
    assert data["stoplossID"] is None
    assert data["volume"] == 10
    for key in ("closePrice", "closeTime", "duration", "profit"):
        assert math.isnan(data[key])


def test_to_json_closed_sim_trade_has_no_stop_order_id():
    data = closed(SHORT, 96.0).to_json()
    assert data["stoplossID"] is None
    assert data["status"] == "Closed"
    assert data["profit"] == pytest.approx(40.0)
    assert data["duration"] == pd.Timedelta(minutes=5)


@pytest.mark.parametrize("quantity", [None, 4])
def test_update_tp_uses_volume_by_default(quantity):
    broker = RecordingBroker()
    trade = make_trade(volume=7, broker=broker)
    if quantity is None:
        trade.update_tp(103.0)
    else:
        trade.update_tp(103.0, quantity)
    assert trade.tp == 103.0
    assert broker.tp_calls == [("ref", 103.0, 7 if quantity is None else quantity)]


def make_algo(trades):
    algo = ParentAlgo({"idname": "pa", "ticker": "AAPL"})
    algo.trades = trades
    return algo


def test_printstats_no_trades(capsys):
    make_algo([]).printStats(False)
    out = capsys.readouterr().out
    assert "Total Trades Placed:  0" in out
    assert "Win Rate:  0 %" in out
    assert "Average Win:  0" in out


def test_printstats_no_winners(capsys):
    algo = make_algo([closed(LONG, 96.0, ID="a"), closed(SHORT, 104.0, ID="b")])
    algo.printStats(False)
    out = capsys.readouterr().out
    assert "Win Rate:  0 %" in out
    assert "Average Win:  0" in out
    assert "Average Loss:  -40.0" in out
    assert "Total Profit: $ -80.0" in out


def test_printstats_mixed_trades(capsys):
    algo = make_algo([closed(LONG, 104.0, ID="a"), closed(SHORT, 104.0, ID="b")])
    algo.printStats(True)
    out = capsys.readouterr().out
    assert "Win Rate:  50 %" in out
    assert "Average Win:  40.0" in out
    assert "Average Loss:  -40.0" in out
