"""Characterization of the orders ``Trade`` sends in live mode.

These tests pin CURRENT behaviour, including behaviour that looks wrong, so
that later refactors of the live order path can be proven behaviour-preserving.
Nothing here is a statement that the behaviour is correct; comments marked
"PINS CURRENT BEHAVIOUR" flag the surprising parts.

The fake broker is a recording subclass of ``IBapi`` (tests/fake_ibapi.py)
installed in place of the module-level ``ibapi`` that trade.py looks up at call
time. ``config.LiveTrading`` is only forced on after the fake is in place and is
always restored.
"""
import logging
import random
import socket

import pandas as pd
import pytest
from ibapi.const import UNSET_DOUBLE

import MainStoinker.TradeTools.trade as trade_module
from MainStoinker.MainStuff.Globals import config
from MainStoinker.TradeTools.trade import Trade
from tests.fake_ibapi import RecordingIBapi

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0
# Locale dependent, so computed the way trade.py computes it.
GTD = (T0 + pd.Timedelta(2, "min")).strftime("%X")
OCA = "ocat7"  # "oca" + trade ID "t" + patched random.randint (7)

STK = {
    "symbol": "AAPL",
    "secType": "STK",
    "exchange": "SMART",
    "currency": "USD",
    "primaryExchange": "SMART",
}
CRYPTO = {
    "symbol": "BTC",
    "secType": "CRYPTO",
    "exchange": "ZEROHASH",
    "currency": "USD",
    "primaryExchange": "ZEROHASH",
}
GET_ID = ("getNextOrderID",)


def placed(order_id, action, order_type, qty, *, lmt=UNSET_DOUBLE, aux=UNSET_DOUBLE,
           tif="", gtd="", oca="", oca_type=0, contract=STK):
    """One expected placeOrder call. Unset prices are IB's UNSET_DOUBLE."""
    return (
        "placeOrder",
        order_id,
        contract,
        {
            "orderId": order_id,
            "action": action,
            "orderType": order_type,
            "totalQuantity": qty,
            "lmtPrice": lmt,
            "auxPrice": aux,
            "tif": tif,
            "goodTillDate": gtd,
            "ocaGroup": oca,
            "ocaType": oca_type,
            "transmit": True,
            "parentId": 0,
        },
    )


def bar(close, high=None, low=None):
    return {
        "date": T0 + pd.Timedelta(minutes=1),
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }


@pytest.fixture
def fake(monkeypatch):
    """Install the recording fake FIRST, only then turn live trading on."""
    fake_api = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    assert trade_module.ibapi is fake_api
    monkeypatch.setattr(random, "randint", lambda a, b: 7)
    object.__setattr__(config, "LiveTrading", True)
    try:
        yield fake_api
    finally:
        object.__setattr__(config, "LiveTrading", False)
        assert config.LiveTrading is False


def make_trade(direction=LONG, symbol="AAPL", limit_order=False):
    return Trade(symbol, 10, "t", 100.0, T0, direction, logging.getLogger("test"),
                 limitOrder=limit_order)


def run_lifecycle(fake_api, direction):
    """Open, fix stop, new TP, modify TP, check stop (fine), close."""
    trade = make_trade(direction)
    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(98.0 if direction else 102.0)
    trade.create_tp(103.0 if direction else 97.0, 10)
    trade.create_tp(104.0 if direction else 96.0, 10)
    fake_api.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    result = trade.check_stoploss(bar(100.5))
    assert result == 1
    trade.close_position(100.5, bar(100.5)["date"])
    return trade


LONG_LIFECYCLE = [
    GET_ID,
    placed(100, "BUY", "MKT", 10, tif="GTD", gtd=GTD),  # parent, no OCA group
    GET_ID,
    placed(101, "SELL", "STP", 10, aux=99.0, oca=OCA, oca_type=2),  # default 1% stop
    placed(101, "SELL", "STP", 10, aux=98.0, oca=OCA, oca_type=2),  # set_stopPrice
    GET_ID,
    placed(102, "SELL", "LMT", 10, lmt=103.0, oca=OCA, oca_type=2),  # new TP
    placed(102, "SELL", "LMT", 10, lmt=104.0, oca=OCA, oca_type=2),  # modified TP, same id
    # check_stoploss sends nothing
    GET_ID,
    placed(103, "SELL", "MKT", 10, oca=OCA, oca_type=2),  # close
]

SHORT_LIFECYCLE = [
    GET_ID,
    placed(100, "SELL", "MKT", 10, tif="GTD", gtd=GTD),
    GET_ID,
    placed(101, "BUY", "STP", 10, aux=101.0, oca=OCA, oca_type=2),
    placed(101, "BUY", "STP", 10, aux=102.0, oca=OCA, oca_type=2),
    GET_ID,
    placed(102, "BUY", "LMT", 10, lmt=97.0, oca=OCA, oca_type=2),
    placed(102, "BUY", "LMT", 10, lmt=96.0, oca=OCA, oca_type=2),
    GET_ID,
    placed(103, "BUY", "MKT", 10, oca=OCA, oca_type=2),
]


def test_long_market_lifecycle(fake):
    trade = run_lifecycle(fake, LONG)
    assert fake.calls == LONG_LIFECYCLE
    assert trade.status == "Closed"
    assert trade.parentId == 100
    assert trade.stopOrder.orderId == 101
    assert trade.tpOrderId == 102
    assert trade.ParentCloseId == 103
    # PINS CURRENT BEHAVIOUR: the parent has no OCA group and parentId 0, so the
    # stop and take-profit are not attached to the entry order (the code that
    # would do so is commented out in trade.py). The stop is live as soon as it
    # is sent, whether or not the parent fills.
    # The close order is MKT: closePrice below is only recorded, never sent.
    assert trade.closePrice == 100.5


def test_short_market_lifecycle(fake):
    trade = run_lifecycle(fake, SHORT)
    assert fake.calls == SHORT_LIFECYCLE
    assert trade.status == "Closed"
    # Actions are correct for shorts: parent uppercase "SELL" so addStoploss and
    # addTP (which compare with "BUY") give BUY orders. No wrong-sided orders.


def test_trailing_stop_move_resends_stop(fake):
    trade = make_trade(LONG)  # default stop type is "Trailing"
    assert trade.stopLossType == "Trailing"
    fake.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 1  # not far enough: needs > 99.0 + 1.0
    assert fake.calls == []

    assert trade.check_stoploss(bar(102.5)) == 1  # 102.5 > 100, stop -> 102.5 - 1.0
    assert fake.calls == [
        placed(101, "SELL", "STP", 10, aux=101.5, oca=OCA, oca_type=2),
    ]
    assert trade.stopPrice == 101.5


def test_trailing_stop_move_resends_stop_short(fake):
    trade = make_trade(SHORT)
    fake.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 1  # needs < 101.0 - 1.0
    assert fake.calls == []

    assert trade.check_stoploss(bar(97.5)) == 1
    assert fake.calls == [
        placed(101, "BUY", "STP", 10, aux=98.5, oca=OCA, oca_type=2),
    ]


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_stop_filled_returns_0_without_orders(fake, direction):
    trade = make_trade(direction)
    fake.set_open_order_state(trade.stopOrder.orderId, "Filled")
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 0
    assert fake.calls == []
    # PINS CURRENT BEHAVIOUR: status stays "Open" although the stop has filled.
    assert trade.status == "Open"


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_stop_missing_from_open_orders_returns_0_and_leaves_status_open(fake, direction):
    trade = make_trade(direction)
    fake.clear_open_orders()
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 0
    assert fake.calls == []
    assert trade.status == "Open"
    assert trade.position is True
    assert not hasattr(trade, "closePrice")


def test_manual_stop_out_sends_close_order(fake):
    trade = make_trade(LONG)  # stop 99.0, order ids 100 (parent) and 101 (stop)
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(98.0)) == 0
    assert fake.calls == [GET_ID, placed(102, "SELL", "MKT", 10, oca=OCA, oca_type=2)]
    assert trade.status == "Closed"
    assert trade.closePrice == 99.0  # the stop price, not the bar close


def test_manual_stop_out_sends_close_order_short(fake):
    trade = make_trade(SHORT)  # stop 101.0
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(102.0)) == 0
    assert fake.calls == [GET_ID, placed(102, "BUY", "MKT", 10, oca=OCA, oca_type=2)]
    assert trade.closePrice == 101.0


def test_tp_hit_places_close_order(fake):
    trade = make_trade(LONG)
    trade.create_tp(103.0, 10)  # TP order id 102
    fake.calls.clear()

    assert trade.check_tp(bar(100.5, high=103.5)) == 1
    assert fake.calls == [GET_ID, placed(103, "SELL", "MKT", 10, oca=OCA, oca_type=2)]
    assert trade.closePrice == 103.0  # the TP price, not the bar high


def test_tp_hit_places_close_order_short(fake):
    trade = make_trade(SHORT)
    trade.create_tp(97.0, 10)
    fake.calls.clear()

    assert trade.check_tp(bar(99.5, low=96.5)) == 1
    assert fake.calls == [GET_ID, placed(103, "BUY", "MKT", 10, oca=OCA, oca_type=2)]
    assert trade.closePrice == 97.0


def test_tp_not_reached_sends_nothing(fake):
    trade = make_trade(LONG)
    trade.create_tp(103.0, 10)
    fake.calls.clear()

    assert trade.check_tp(bar(100.5, high=103.0)) == 0  # strictly greater needed
    assert fake.calls == []


def test_tp_then_stop_on_same_bar_sends_two_closes(fake):
    # Same call order as ParentAlgo.pre_update: check_tp, then check_stoploss.
    trade = make_trade(LONG)  # stop 99.0, stop id 101
    trade.create_tp(103.0, 10)  # id 102
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()
    crossing = bar(98.0, high=104.0)

    assert trade.check_tp(crossing) == 1
    assert trade.check_stoploss(crossing) == 0
    # PINS CURRENT BEHAVIOUR / KNOWN HAZARD (issue 15): two closing orders are
    # sent for one position. Neither check looks at trade.status, and the stop
    # is still listed as "Submitted", so the second close is also sent.
    assert fake.calls == [
        GET_ID,
        placed(103, "SELL", "MKT", 10, oca=OCA, oca_type=2),
        GET_ID,
        placed(104, "SELL", "MKT", 10, oca=OCA, oca_type=2),
    ]


def test_tp_then_stop_on_same_bar_sends_two_closes_short(fake):
    trade = make_trade(SHORT)  # stop 101.0
    trade.create_tp(97.0, 10)
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()
    crossing = bar(102.0, low=96.0)

    assert trade.check_tp(crossing) == 1
    assert trade.check_stoploss(crossing) == 0
    assert fake.calls == [  # issue 15 hazard, as above
        GET_ID,
        placed(103, "BUY", "MKT", 10, oca=OCA, oca_type=2),
        GET_ID,
        placed(104, "BUY", "MKT", 10, oca=OCA, oca_type=2),
    ]


def test_limit_entry_and_close_use_open_price_as_limit(fake):
    trade = make_trade(LONG, limit_order=True)
    assert fake.calls == [
        GET_ID,
        placed(100, "BUY", "LMT", 10, lmt=100.0, tif="GTD", gtd=GTD),
        GET_ID,
        placed(101, "SELL", "STP", 10, aux=99.0, oca=OCA, oca_type=2),
    ]
    fake.calls.clear()

    trade.close_position(105.0, bar(105.0)["date"])
    # PINS CURRENT BEHAVIOUR: the close limit is the ENTRY price (openPrice),
    # not the price passed to close_position (105.0).
    assert fake.calls == [
        GET_ID,
        placed(102, "SELL", "LMT", 10, lmt=100.0, oca=OCA, oca_type=2),
    ]


def test_limit_entry_and_close_use_open_price_as_limit_short(fake):
    trade = make_trade(SHORT, limit_order=True)
    assert fake.calls == [
        GET_ID,
        placed(100, "SELL", "LMT", 10, lmt=100.0, tif="GTD", gtd=GTD),
        GET_ID,
        placed(101, "BUY", "STP", 10, aux=101.0, oca=OCA, oca_type=2),
    ]
    fake.calls.clear()

    trade.close_position(95.0, bar(95.0)["date"])
    assert fake.calls == [
        GET_ID,
        placed(102, "BUY", "LMT", 10, lmt=100.0, oca=OCA, oca_type=2),
    ]


@pytest.mark.parametrize("symbol", ["BTC", "ETH"])
def test_crypto_symbol_uses_crypto_contract(fake, symbol):
    contract = {**CRYPTO, "symbol": symbol}
    trade = make_trade(LONG, symbol=symbol)
    trade.close_position(100.0, bar(100.0)["date"])
    assert fake.calls == [
        GET_ID,
        placed(100, "BUY", "MKT", 10, tif="GTD", gtd=GTD, contract=contract),
        GET_ID,
        placed(101, "SELL", "STP", 10, aux=99.0, oca=OCA, oca_type=2, contract=contract),
        GET_ID,
        placed(102, "SELL", "MKT", 10, oca=OCA, oca_type=2, contract=contract),
    ]


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_cancel_order_never_called(fake, direction):
    run_lifecycle(fake, direction)
    assert not [c for c in fake.calls if c[0] == "cancelOrder"]
    # The OCA group is what closes the stop; Trade never cancels explicitly.
    assert not [c for c in fake.calls if c[0] in ("reqIds", "readOrders", "reqAllOpenOrders")]


def test_live_flag_restored():
    # Runs after fixture-using tests in this file; the fixture also asserts this
    # in its teardown.
    assert config.LiveTrading is False


def test_non_live_trade_sends_nothing_even_with_fake_installed(monkeypatch):
    fake_api = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    assert config.LiveTrading is False
    trade = make_trade(LONG)
    trade.close_position(100.0, bar(100.0)["date"])
    assert fake_api.calls == []


def test_no_socket_possible(fake, monkeypatch):
    attempts = []

    def spy_connect(*args, **kwargs):
        attempts.append(("connect", args))
        raise AssertionError("socket connect attempted")

    def spy_create_connection(*args, **kwargs):
        attempts.append(("create_connection", args))
        raise AssertionError("socket.create_connection attempted")

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "create_connection", spy_create_connection)

    run_lifecycle(fake, LONG)
    assert attempts == []
    assert fake.calls == LONG_LIFECYCLE
