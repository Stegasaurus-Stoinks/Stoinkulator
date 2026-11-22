"""Characterization of the orders ``Trade`` sends in live mode.

These tests pin CURRENT behaviour, including behaviour that looks wrong, so
that later refactors of the live order path can be proven behaviour-preserving.
Nothing here is a statement that the behaviour is correct; comments marked
"PINS CURRENT BEHAVIOUR" or "HAZARD" flag the surprising parts.

The fake broker is a recording subclass of ``IBapi`` (tests/fake_ibapi.py)
installed in place of the module-level ``ibapi`` that trade.py looks up at call
time. Each recorded order lists exactly the attributes that differ from a fresh
``Order()``; ``config.LiveTrading`` is only forced on after the fake is in place
and is restored to its original value afterwards.
"""
import logging
import math
import random

import pandas as pd
import pytest

import MainStoinker.TradeTools.trade as trade_module
from MainStoinker.MainStuff.Globals import config
from MainStoinker.TradeTools.trade import Trade
from tests.fake_ibapi import RecordingIBapi

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0
# Locale dependent, so the expectation is computed the way trade.py computes it
# (and pinned to a literal once, in test_good_till_date_literal_in_this_locale).
GTD = (T0 + pd.Timedelta(2, "min")).strftime("%X")
OCA = "ocat7"  # "oca" + trade ID "t" + patched random.randint (7)

STK = {
    "symbol": "AAPL",
    "secType": "STK",
    "exchange": "SMART",
    "primaryExchange": "SMART",
    "currency": "USD",
}
CRYPTO = {
    "symbol": "BTC",
    "secType": "CRYPTO",
    "exchange": "ZEROHASH",
    "primaryExchange": "ZEROHASH",
    "currency": "USD",
}
GET_ID = ("getNextOrderID",)

# Attributes every order built by IBKRHelper / addStoploss / addTP carries that
# a fresh Order() does not have. Written out once, by hand. Fields at their
# Order() default (transmit True, parentId 0, unset prices, empty tif/oca) are
# deliberately absent: a refactor that sets one differently must fail.
COMMON = {"eTradeOnly": False, "firmQuoteOnly": False}
SAME = object()


def placed(order_id, action, order_type, qty, *, arg_id=SAME, contract=STK, **extra):
    """One expected placeOrder call: the common fields plus ``extra`` (any of
    lmtPrice, auxPrice, tif, goodTillDate, ocaGroup, ocaType)."""
    return (
        "placeOrder",
        order_id if arg_id is SAME else arg_id,
        contract,
        {
            "orderId": order_id,
            "action": action,
            "orderType": order_type,
            "totalQuantity": qty,
            **COMMON,
            **extra,
        },
    )


def entry(order_id, action, **extra):
    return placed(order_id, action, "MKT", 10, tif="GTD", goodTillDate=GTD, **extra)


def stp(order_id, action, aux, *, oca=True):
    extra = {"ocaGroup": OCA, "ocaType": 2} if oca else {}
    return placed(order_id, action, "STP", 10, auxPrice=aux, **extra)


def take_profit(order_id, action, lmt):
    return placed(order_id, action, "LMT", 10, lmtPrice=lmt, ocaGroup=OCA, ocaType=2)


def close_mkt(order_id, action, **kw):
    return placed(order_id, action, "MKT", 10, ocaGroup=OCA, ocaType=2, **kw)


def bar(close, high=None, low=None):
    return {
        "date": T0 + pd.Timedelta(minutes=1),
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }


@pytest.fixture
def installed(monkeypatch):
    """The recording fake in place of trade.ibapi, live mode NOT enabled."""
    fake_api = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    assert trade_module.ibapi is fake_api
    monkeypatch.setattr(random, "randint", lambda a, b: 7)
    return fake_api


@pytest.fixture
def fake(installed):
    """As ``installed``, then live mode on (only now that the fake is in place)."""
    original = config.LiveTrading
    assert original is False  # the safe default must hold before we force it
    object.__setattr__(config, "LiveTrading", True)
    try:
        yield installed
    finally:
        object.__setattr__(config, "LiveTrading", original)


def make_trade(direction=LONG, symbol="AAPL", limit_order=False, price=100.0):
    return Trade(symbol, 10, "t", price, T0, direction, logging.getLogger("test"),
                 limitOrder=limit_order)


def run_lifecycle(fake_api, direction):
    """Open, fix stop, new TP, modify TP, check stop (fine), close."""
    trade = make_trade(direction)
    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(98.0 if direction else 102.0)
    trade.create_tp(103.0 if direction else 97.0, 10)
    trade.create_tp(104.0 if direction else 96.0, 10)
    fake_api.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    assert trade.check_stoploss(bar(100.5)) == 1
    trade.close_position(100.5, bar(100.5)["date"])
    return trade


LONG_LIFECYCLE = [
    GET_ID,
    entry(100, "BUY"),
    GET_ID,
    stp(101, "SELL", 99.0),  # default 1% stop
    stp(101, "SELL", 98.0),  # set_stopPrice, same id
    GET_ID,
    take_profit(102, "SELL", 103.0),  # new TP
    take_profit(102, "SELL", 104.0),  # modified TP, same id
    # check_stoploss sends nothing
    GET_ID,
    close_mkt(103, "SELL"),
]

SHORT_LIFECYCLE = [
    GET_ID,
    entry(100, "SELL"),
    GET_ID,
    stp(101, "BUY", 101.0),
    stp(101, "BUY", 102.0),
    GET_ID,
    take_profit(102, "BUY", 97.0),
    take_profit(102, "BUY", 96.0),
    GET_ID,
    close_mkt(103, "BUY"),
]


def test_long_market_lifecycle(fake):
    trade = run_lifecycle(fake, LONG)
    assert fake.calls == LONG_LIFECYCLE
    assert trade.status == "Closed"
    assert trade.parentId == 100
    assert trade.stopOrder.orderId == 101
    assert trade.tpOrderId == 102
    assert trade.ParentCloseId == 103
    # HAZARD (issue 15), the naked stop: the parent has no OCA group and
    # parentId 0, so the stop and take-profit are NOT children of the entry
    # order (that code is commented out in trade.py). The stop is transmitted
    # immediately, regardless of what happens to the entry: if the GTD entry
    # expires unfilled or is rejected, a live stop remains with no position.
    # The close order is MKT: closePrice below is only recorded, never sent.
    assert trade.closePrice == 100.5


def test_short_market_lifecycle(fake):
    trade = run_lifecycle(fake, SHORT)
    assert fake.calls == SHORT_LIFECYCLE
    assert trade.status == "Closed"
    # Same naked-stop hazard as the long lifecycle. Actions are correct for
    # shorts: the parent is uppercase "SELL" so addStoploss/addTP (which compare
    # with "BUY") give BUY orders.


def test_good_till_date_literal_in_this_locale():
    # %X is locale dependent. Under this process's locale it is the literal
    # below for the fixed open time (+2 minutes); other tests compute it.
    assert GTD == "07:02:00"


def test_trailing_stop_move_resends_stop(fake):
    trade = make_trade(LONG)  # default stop type is "Trailing"
    assert trade.stopLossType == "Trailing"
    fake.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 1  # not far enough: needs > 99.0 + 1.0
    assert fake.calls == []

    assert trade.check_stoploss(bar(102.5)) == 1  # 102.5 > 100, stop -> 102.5 - 1.0
    assert fake.calls == [stp(101, "SELL", 101.5)]
    assert trade.stopPrice == 101.5


def test_trailing_stop_move_resends_stop_short(fake):
    trade = make_trade(SHORT)
    fake.set_open_order_state(trade.stopOrder.orderId, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(100.0)) == 1  # needs < 101.0 - 1.0
    assert fake.calls == []

    assert trade.check_stoploss(bar(97.5)) == 1
    assert fake.calls == [stp(101, "BUY", 98.5)]


def test_trailing_stop_sends_unrounded_float(fake):
    trade = make_trade(LONG, price=101.37)
    assert trade.stopPrice == 100.36  # initial stop is rounded to 2 dp
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(103.123)) == 1
    # PINS CURRENT BEHAVIOUR: the trailed stop is price - stopDelta and is sent
    # to the broker UNROUNDED (only the initial stop is rounded to 2 dp).
    expected = 103.123 - abs(101.37 - 100.36)
    assert expected != round(expected, 2)
    assert fake.calls == [stp(101, "SELL", expected)]
    assert trade.stopPrice == expected


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


@pytest.mark.parametrize("state", ["Cancelled", "PreSubmitted"])
@pytest.mark.parametrize(
    ("direction", "through", "action", "stop_price"),
    [(LONG, 98.0, "SELL", 99.0), (SHORT, 102.0, "BUY", 101.0)],
)
def test_stop_in_other_states_falls_through_to_manual_close(
    fake, state, direction, through, action, stop_price
):
    trade = make_trade(direction)
    fake.set_open_order_state(101, state)
    fake.calls.clear()

    # PINS CURRENT BEHAVIOUR: only "Filled" or absent is treated as closed by
    # TWS. A Cancelled stop (no longer protecting the position) is handled like
    # a live one, so the manual check runs and sends a market close.
    assert trade.check_stoploss(bar(through)) == 0
    assert fake.calls == [GET_ID, close_mkt(102, action)]
    assert trade.closePrice == stop_price


def test_manual_stop_out_sends_close_order(fake):
    trade = make_trade(LONG)  # stop 99.0, order ids 100 (parent) and 101 (stop)
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(98.0)) == 0
    assert fake.calls == [GET_ID, close_mkt(102, "SELL")]
    assert trade.status == "Closed"
    assert trade.closePrice == 99.0  # the stop price, not the bar close


def test_manual_stop_out_sends_close_order_short(fake):
    trade = make_trade(SHORT)  # stop 101.0
    fake.set_open_order_state(101, "Submitted")
    fake.calls.clear()

    assert trade.check_stoploss(bar(102.0)) == 0
    assert fake.calls == [GET_ID, close_mkt(102, "BUY")]
    assert trade.closePrice == 101.0


def test_tp_hit_places_close_order(fake):
    trade = make_trade(LONG)
    trade.create_tp(103.0, 10)  # TP order id 102
    fake.calls.clear()

    assert trade.check_tp(bar(100.5, high=103.5)) == 1
    assert fake.calls == [GET_ID, close_mkt(103, "SELL")]
    assert trade.closePrice == 103.0  # the TP price, not the bar high


def test_tp_hit_places_close_order_short(fake):
    trade = make_trade(SHORT)
    trade.create_tp(97.0, 10)
    fake.calls.clear()

    assert trade.check_tp(bar(99.5, low=96.5)) == 1
    assert fake.calls == [GET_ID, close_mkt(103, "BUY")]
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
    assert trade.closePrice == 103.0
    assert trade.check_stoploss(crossing) == 0
    # HAZARD (issue 15): two closing orders are sent for one position. Neither
    # check looks at trade.status, and the stop is still listed "Submitted", so
    # the second close is also sent.
    assert fake.calls == [
        GET_ID,
        close_mkt(103, "SELL"),
        GET_ID,
        close_mkt(104, "SELL"),
    ]
    # PINS CURRENT BEHAVIOUR: the stop-out overwrites the take-profit's record.
    assert trade.closePrice == 99.0
    assert trade.closeTime == crossing["date"]
    assert trade.ParentCloseId == 104


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
        close_mkt(103, "BUY"),
        GET_ID,
        close_mkt(104, "BUY"),
    ]
    assert trade.closePrice == 101.0
    assert trade.closeTime == crossing["date"]


def test_update_tp_raises_type_error(fake):
    trade = make_trade(LONG)
    fake.calls.clear()
    # PINS CURRENT BEHAVIOUR: update_tp calls create_tp(tp) without the
    # required quantity, so it can never work.
    with pytest.raises(TypeError):
        trade.update_tp(103.0)
    assert fake.calls == []


def test_limit_entry_and_close_use_open_price_as_limit(fake):
    trade = make_trade(LONG, limit_order=True)
    assert fake.calls == [
        GET_ID,
        placed(100, "BUY", "LMT", 10, lmtPrice=100.0, tif="GTD", goodTillDate=GTD),
        GET_ID,
        stp(101, "SELL", 99.0),
    ]
    fake.calls.clear()

    trade.close_position(105.0, bar(105.0)["date"])
    # PINS CURRENT BEHAVIOUR: the close limit is the ENTRY price (openPrice),
    # not the price passed to close_position (105.0).
    assert fake.calls == [
        GET_ID,
        placed(102, "SELL", "LMT", 10, lmtPrice=100.0, ocaGroup=OCA, ocaType=2),
    ]


def test_limit_entry_and_close_use_open_price_as_limit_short(fake):
    trade = make_trade(SHORT, limit_order=True)
    assert fake.calls == [
        GET_ID,
        placed(100, "SELL", "LMT", 10, lmtPrice=100.0, tif="GTD", goodTillDate=GTD),
        GET_ID,
        stp(101, "BUY", 101.0),
    ]
    fake.calls.clear()

    trade.close_position(95.0, bar(95.0)["date"])
    assert fake.calls == [
        GET_ID,
        placed(102, "BUY", "LMT", 10, lmtPrice=100.0, ocaGroup=OCA, ocaType=2),
    ]


@pytest.mark.parametrize("symbol", ["BTC", "ETH"])
def test_crypto_symbol_uses_crypto_contract(fake, symbol):
    contract = {**CRYPTO, "symbol": symbol}
    trade = make_trade(LONG, symbol=symbol)
    trade.close_position(100.0, bar(100.0)["date"])
    assert fake.calls == [
        GET_ID,
        entry(100, "BUY", contract=contract),
        GET_ID,
        placed(101, "SELL", "STP", 10, auxPrice=99.0, ocaGroup=OCA, ocaType=2,
               contract=contract),
        GET_ID,
        close_mkt(102, "SELL", contract=contract),
    ]


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_cancel_order_never_called(fake, direction):
    run_lifecycle(fake, direction)
    # The OCA group is what closes the stop; Trade never cancels explicitly.
    assert not [c for c in fake.calls if c[0] == "cancelOrder"]


# -- order-id timeout path ----------------------------------------------------
# The real getNextOrderID returns None on timeout and leaves nextValidOrderId at
# its previous value. trade.py reads the id from the ATTRIBUTE in open_position,
# addStoploss and addTP, but from the RETURN VALUE in close_position.


def test_timeout_on_stop_id_places_stop_under_parent_id(fake):
    fake.timeout_next(1, after=1)  # parent's request succeeds, stop's times out
    trade = make_trade(LONG)
    # HAZARD (issue 15): the stop is placed under the PARENT's id (100). The
    # broker would see this as a modification of the entry order.
    assert fake.calls == [
        GET_ID,
        entry(100, "BUY"),
        GET_ID,  # timed out
        stp(100, "SELL", 99.0),
    ]
    assert trade.stopOrder.orderId == 100
    fake.calls.clear()
    trade.set_stopPrice(98.0)
    assert fake.calls == [stp(100, "SELL", 98.0)]  # later re-sends reuse it too


def test_timeout_on_tp_id_places_tp_under_stop_id(fake):
    trade = make_trade(LONG)
    fake.calls.clear()
    fake.timeout_next(1)
    trade.create_tp(103.0, 10)
    # HAZARD (issue 15): the take-profit takes the id of the stop (101), the
    # last id handed out, because the attribute was not advanced.
    assert fake.calls == [GET_ID, take_profit(101, "SELL", 103.0)]
    assert trade.tpOrderId == trade.stopOrder.orderId == 101


def test_timeout_on_close_id_places_close_under_none(fake):
    trade = make_trade(LONG)
    fake.calls.clear()
    fake.timeout_next(1)
    trade.close_position(100.5, bar(100.5)["date"])
    # HAZARD (issue 15): close_position uses the RETURN value, which is None on
    # timeout, so the close is placed with orderId None; the trade is still
    # marked closed.
    assert fake.calls == [GET_ID, close_mkt(None, "SELL")]
    assert trade.ParentCloseId is None
    assert trade.status == "Closed"


def test_timeout_on_second_trade_entry_id_reuses_last_id(fake):
    first = make_trade(LONG)
    first.close_position(100.5, bar(100.5)["date"])  # ids 100, 101, close 102
    fake.calls.clear()
    fake.timeout_next(1)
    second = make_trade(LONG)
    # HAZARD (issue 15): the second entry reuses the last id handed out (102,
    # the first trade's CLOSE order). The stop request then succeeds (103).
    assert fake.calls == [
        GET_ID,  # timed out
        entry(102, "BUY"),
        GET_ID,
        stp(103, "SELL", 99.0),
    ]
    assert second.parentId == 102


@pytest.mark.parametrize(
    ("direction", "parent", "opposite", "tp_price"),
    [(LONG, "BUY", "SELL", 103.0), (SHORT, "SELL", "BUY", 97.0)],
)
def test_id_access_styles_are_distinguishable(fake, direction, parent, opposite, tp_price):
    # getNextOrderID returns 500 + k while nextValidOrderId advances 100 + k.
    # Attribute reads (entry, stop, TP) give 100, 101, 102; the return-value
    # read (close) gives 503. Swapping either style changes these ids.
    fake.decouple_return_value(500)
    stop_price = 99.0 if direction else 101.0
    trade = make_trade(direction)
    trade.create_tp(tp_price, 10)
    trade.close_position(100.5, bar(100.5)["date"])
    assert fake.calls == [
        GET_ID,
        entry(100, parent),
        GET_ID,
        stp(101, opposite, stop_price),
        GET_ID,
        take_profit(102, opposite, tp_price),
        GET_ID,
        close_mkt(503, opposite),
    ]
    assert trade.parentId == 100
    assert trade.ParentCloseId == 503


# -- OCA fallback in open_position ----------------------------------------------


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    ("direction", "parent", "opposite", "stop0", "stop1"),
    [(LONG, "BUY", "SELL", 99.0, 98.0), (SHORT, "SELL", "BUY", 101.0, 102.0)],
)
def test_oca_fallback_when_first_stop_placement_raises(
    fake, when, direction, parent, opposite, stop0, stop1
):
    fake.raise_on_next_stp(when)
    trade = make_trade(direction)
    expected = [GET_ID, entry(100, parent), GET_ID]
    if when == "after":
        # The first stop DID reach the broker before the exception.
        expected.append(stp(101, opposite, stop0))
    # The retry builds a second stop under a new id, with no OCA group.
    expected += [GET_ID, stp(102, opposite, stop0, oca=False)]
    assert fake.calls == expected
    # PINS CURRENT BEHAVIOUR / HAZARD (issue 15): trade.stopOrder is the SECOND
    # stop (id 102, no OCA group). In the "after" case the first stop (101, OCA
    # group) is orphaned but still live at the broker: two stops for one entry.
    assert trade.stopOrder.orderId == 102
    assert trade.stopOrder.ocaGroup == ""
    assert trade.status == "Open"

    fake.calls.clear()
    trade.set_stopPrice(stop1)
    assert fake.calls == [stp(102, opposite, stop1, oca=False)]  # only the second

    fake.calls.clear()
    trade.close_position(100.5, bar(100.5)["date"])
    # The close carries the OCA group, which the retried stop is not in.
    assert fake.calls == [GET_ID, close_mkt(103, opposite)]


# -- to_json / status in live mode ---------------------------------------------


def test_to_json_open_live_trade(fake):
    trade = make_trade(LONG)
    assert trade.get_status() == "Open"
    assert trade.in_position() is True

    data = trade.to_json()
    # PINS CURRENT BEHAVIOUR: to_json on an open trade WRITES NaN into the
    # trade's closePrice and closeTime attributes.
    assert math.isnan(trade.closePrice)
    assert math.isnan(trade.closeTime)
    nan_keys = {"closePrice", "closeTime", "duration", "profit"}
    assert {k for k, v in data.items() if isinstance(v, float) and math.isnan(v)} == nan_keys
    assert {k: v for k, v in data.items() if k not in nan_keys} == {
        "symbol": "AAPL",
        "ID": "t",
        "stoplossID": 101,
        "volume": 10,
        "openPrice": 100.0,
        "openTime": T0,
        "direction": 1,
        "stopDelta": 1.0,
        "status": "Open",
    }


def test_to_json_closed_live_trade(fake):
    trade = make_trade(LONG)
    close_time = T0 + pd.Timedelta(minutes=1)
    trade.close_position(100.5, close_time)
    assert trade.get_status() == "Closed"
    assert trade.in_position() is False
    assert trade.to_json() == {
        "symbol": "AAPL",
        "ID": "t",
        "stoplossID": 101,
        "volume": 10,
        "openPrice": 100.0,
        "openTime": T0,
        "direction": 1,
        "stopDelta": 1.0,
        "status": "Closed",
        "closePrice": 100.5,
        "closeTime": close_time,
        "duration": pd.Timedelta(minutes=1),
        "profit": 0.5,
    }


# -- non-live guard: the same operations send nothing ----------------------------


@pytest.mark.parametrize(
    ("direction", "stop", "tp1", "tp2", "trail_bar", "trailed", "tp_bar", "tp_close",
     "stop_bar", "stop_close"),
    [
        (LONG, 98.0, 103.0, 104.0, bar(102.5), 101.5, bar(102.5, high=104.5), 104.0,
         bar(98.0), 99.0),
        (SHORT, 102.0, 97.0, 96.0, bar(97.5), 98.5, bar(97.5, low=95.5), 96.0,
         bar(102.0), 101.0),
    ],
)
def test_non_live_lifecycle_sends_nothing(
    installed, direction, stop, tp1, tp2, trail_bar, trailed, tp_bar, tp_close,
    stop_bar, stop_close,
):
    assert config.LiveTrading is False
    trade = make_trade(direction)
    # Same as the simulated path in test_trade_sim.py: no status/position until
    # a close (a known defect, xfail there), and no stopOrder.
    assert not hasattr(trade, "status")
    assert not hasattr(trade, "position")
    assert not hasattr(trade, "stopOrder")

    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(stop)
    assert trade.stopPrice == stop
    trade.create_tp(tp1, 10)
    trade.create_tp(tp2, 10)
    assert trade.tp == tp2
    assert not hasattr(trade, "tpOrderId")

    trade.set_stopLossType("Trailing")
    assert trade.check_stoploss(trail_bar) == 1
    assert trade.stopPrice == trailed

    assert trade.check_tp(tp_bar) == 1
    assert trade.closePrice == tp_close
    assert trade.status == "Closed"
    assert trade.position is False

    stopped = make_trade(direction)
    assert stopped.check_stoploss(stop_bar) == 0
    assert stopped.closePrice == stop_close
    assert stopped.status == "Closed"

    closed = make_trade(direction)
    closed.close_position(100.5, stop_bar["date"])
    assert closed.closePrice == 100.5
    assert closed.status == "Closed"
    assert closed.position is False

    assert installed.calls == []
