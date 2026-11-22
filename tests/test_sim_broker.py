"""Tests for the Broker protocol and the record-only SimBroker."""

from decimal import Decimal

import pandas as pd
import pytest

from MainStoinker.engine.broker import Broker, BrokerCall, SimBroker
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.report import ledger_trade_records

T0 = pd.Timestamp("2024-01-02 09:31")
T1 = pd.Timestamp("2024-01-02 09:45")
T2 = pd.Timestamp("2024-01-02 10:00")


def _open(broker: SimBroker, symbol: str = "AAPL"):
    return broker.open_position(
        symbol=symbol,
        volume=10,
        direction=1,
        price=100.0,
        time=T0,
        limit=False,
        stop_price=99.0,
        oca_group="oca1",
    )


def test_sim_broker_satisfies_protocol():
    assert isinstance(SimBroker(), Broker)


def test_protocol_rejects_object_missing_a_method():
    class Incomplete:
        def open_position(self, **kwargs): ...
        def modify_stop(self, ref, stop_price): ...
        def set_take_profit(self, ref, price, quantity): ...
        def close_position(self, ref, *, price, time): ...

    assert not isinstance(Incomplete(), Broker)


def test_open_returns_incrementing_refs():
    broker = SimBroker()
    assert [_open(broker), _open(broker), _open(broker)] == [1, 2, 3]


def test_sim_broker_records_sequence():
    broker = SimBroker()
    ref = _open(broker)
    broker.modify_stop(ref, 99.5)
    broker.set_take_profit(ref, 105.0, 10)
    broker.set_take_profit(ref, 106.0, 5)
    broker.close_position(ref, price=104.0, time=T1)

    assert broker.calls == (
        BrokerCall(
            "open_position",
            1,
            {
                "symbol": "AAPL",
                "volume": 10,
                "direction": 1,
                "price": 100.0,
                "time": T0,
                "limit": False,
                "stop_price": 99.0,
                "oca_group": "oca1",
            },
        ),
        BrokerCall("modify_stop", 1, {"stop_price": 99.5}),
        BrokerCall("set_take_profit", 1, {"price": 105.0, "quantity": 10}),
        BrokerCall("set_take_profit", 1, {"price": 106.0, "quantity": 5}),
        BrokerCall("close_position", 1, {"price": 104.0, "time": T1}),
    )


def test_sim_broker_double_close_and_modify_after_close_do_not_raise():
    broker = SimBroker()
    ref = _open(broker)
    broker.close_position(ref, price=104.0, time=T1)
    broker.close_position(ref, price=103.0, time=T1)
    broker.modify_stop(ref, 98.0)
    broker.set_take_profit(ref, 110.0, 10)
    assert [c.method for c in broker.calls] == [
        "open_position",
        "close_position",
        "close_position",
        "modify_stop",
        "set_take_profit",
    ]


def test_unknown_ref_does_not_raise():
    broker = SimBroker()
    broker.modify_stop(999, 1.0)
    broker.set_take_profit("nope", 1.0, 1)
    broker.close_position(None, price=1.0, time=T1)
    assert broker.stop_is_working(999) is True
    assert len(broker.calls) == 3
    assert broker.calls[0].ref == 999


def test_stop_is_working_always_true_and_unrecorded():
    broker = SimBroker()
    ref = _open(broker)
    before = broker.calls
    assert broker.stop_is_working(ref) is True
    broker.close_position(ref, price=1.0, time=T1)
    assert broker.stop_is_working(ref) is True
    assert broker.calls[: len(before)] == before
    assert len(broker.calls) == len(before) + 1


def test_two_brokers_are_independent():
    a, b = SimBroker(), SimBroker()
    assert _open(a) == 1
    assert _open(a) == 2
    assert _open(b) == 1
    assert len(a.calls) == 2
    assert len(b.calls) == 1


def test_calls_view_cannot_corrupt_the_log():
    broker = SimBroker()
    ref = _open(broker)
    view = broker.calls
    assert isinstance(view, tuple)
    broker.modify_stop(ref, 98.0)
    assert len(view) == 1  # earlier snapshot unaffected by later calls
    assert len(broker.calls) == 2
    with pytest.raises(TypeError):
        broker.calls[0].args["symbol"] = "X"  # type: ignore[index]
    assert broker.calls[0].args["symbol"] == "AAPL"


# --- reporting to a ledger -----------------------------------------------


def _acct(idname: str = "algo-a", symbol: str = "AAPL"):
    ledger = Ledger()
    return ledger, SimBroker(account=ledger.open_account(idname, symbol, 100_000))


def _open_dir(broker: SimBroker, direction: int, price: float = 100.0, stop: float = 99.0,
              limit: bool = False):
    return broker.open_position(symbol="AAPL", volume=10, direction=direction, price=price,
                                time=T0, limit=limit, stop_price=stop, oca_group="g")


def test_account_does_not_change_calls_or_refs():
    plain, with_account = SimBroker(), _acct()[1]
    for broker in (plain, with_account):
        ref = _open(broker)
        broker.modify_stop(ref, 99.5)
        broker.set_take_profit(ref, 105.0, 10)
        broker.close_position(ref, price=104.0, time=T1)
        broker.close_position(ref, price=103.0, time=T1)
        broker.modify_stop(ref, 98.0)
        assert _open(broker) == 2
    assert plain.calls == with_account.calls


def test_long_entry_records_buy_order_and_fill():
    ledger, broker = _acct()
    ref = _open(broker)
    (order,) = ledger.orders
    assert (order.kind, order.side, order.quantity, order.order_type, order.price, order.time) == (
        "placed", "buy", Decimal(10), "market", Decimal("100"), T0)
    (fill,) = ledger.fills
    assert (fill.reason, fill.side, fill.quantity, fill.price, fill.commission, fill.time) == (
        "entry", "buy", Decimal(10), Decimal("100"), Decimal(0), T0)
    assert fill.order_id == order.order_id and fill.broker_ref == ref
    assert ledger.open_positions()[0].side == "long"


def test_short_and_limit_entry():
    ledger, broker = _acct()
    _open_dir(broker, 0, limit=True)
    assert ledger.orders[0].side == "sell" and ledger.orders[0].order_type == "limit"
    assert ledger.fills[0].side == "sell"
    assert ledger.open_positions()[0].side == "short"


def test_exit_reason_stop_target_and_algo_exit():
    ledger, broker = _acct()
    stopped, targeted, manual, moved = (_open(broker) for _ in range(4))
    broker.set_take_profit(targeted, 105.0, 10)
    broker.modify_stop(moved, 101.0)
    broker.close_position(stopped, price=99.0, time=T1)  # initial stop
    broker.close_position(targeted, price=105.0, time=T1)
    broker.close_position(manual, price=102.0, time=T1)
    broker.close_position(moved, price=101.0, time=T1)  # the stop it was last told
    reasons = {f.broker_ref: f.reason for f in ledger.fills if f.reason != "entry"}
    assert reasons == {stopped: "stop", targeted: "target", manual: "algo_exit", moved: "stop"}


def test_exit_fills_at_given_price_and_time_with_zero_commission():
    ledger, broker = _acct()
    ref = _open_dir(broker, 0, stop=101.0)
    broker.close_position(ref, price=97.5, time=T1)
    exit_fill = ledger.fills[-1]
    assert (exit_fill.side, exit_fill.quantity, exit_fill.price, exit_fill.time) == (
        "buy", Decimal(10), Decimal("97.5"), T1)
    (trip,) = ledger.round_trips
    assert (trip.side, trip.gross, trip.commission, trip.net) == (
        "short", Decimal(25), Decimal(0), Decimal(25))


def test_later_close_and_requests_after_close_are_ignored_with_times():
    ledger, broker = _acct()
    ref = _open(broker)
    broker.close_position(ref, price=104.0, time=T1)
    broker.close_position(ref, price=103.0, time=T2)
    broker.modify_stop(ref, 98.0)
    broker.set_take_profit(ref, 110.0, 10)
    assert len(ledger.fills) == 2 and len(ledger.round_trips) == 1
    assert [(e.kind, e.time) for e in ledger.ignored_events] == [
        ("close", T2), ("modify_stop", T2), ("take_profit", T2)]
    assert ledger.round_trips[0].exit_price == Decimal(104)


def test_modify_after_close_uses_the_close_time_when_no_later_close():
    ledger, broker = _acct()
    ref = _open(broker)
    broker.close_position(ref, price=104.0, time=T1)
    broker.modify_stop(ref, 98.0)
    assert [(e.kind, e.time) for e in ledger.ignored_events] == [("modify_stop", T1)]


def test_modify_stop_and_take_profit_while_open_record_nothing_ignored():
    ledger, broker = _acct()
    ref = _open(broker)
    broker.modify_stop(ref, 99.5)
    broker.set_take_profit(ref, 105.0, 10)
    assert ledger.ignored_events == () and len(ledger.fills) == 1


def test_unknown_ref_with_account_is_ignored_not_raised():
    ledger, broker = _acct()
    broker.modify_stop(999, 1.0)  # no time known yet: nothing to record
    broker.close_position(999, price=1.0, time=T1)
    broker.set_take_profit(999, 1.0, 1)  # falls back to the latest time seen
    assert ledger.fills == ()
    assert [(e.kind, e.time) for e in ledger.ignored_events] == [
        ("close", T1), ("take_profit", T1)]


def test_ledger_trade_records_open_trade_and_synthetic_close_show_as_open():
    ledger, broker = _acct("algo-a")
    done = _open(broker)
    broker.close_position(done, price=104.0, time=T1)
    _open(broker)  # left open; the ledger closes it at end of data
    ledger.finish(T2, {"AAPL": 107.0})
    assert len(ledger.round_trips) == 2 and ledger.round_trips[1].synthetic
    records = ledger_trade_records(ledger)
    assert records == [
        {"id": "algo-a0", "symbol": "AAPL", "volume": 10, "direction": 1,
         "openTime": str(T0), "openPrice": 100.0, "closeTime": str(T1), "closePrice": 104.0},
        {"id": "algo-a1", "symbol": "AAPL", "volume": 10, "direction": 1,
         "openTime": str(T0), "openPrice": 100.0, "closeTime": "None", "closePrice": None},
    ]
    assert ledger_trade_records(ledger, "nobody") == []
