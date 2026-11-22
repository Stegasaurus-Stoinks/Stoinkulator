"""Tests for the Broker protocol and the record-only SimBroker."""

import pandas as pd
import pytest

from MainStoinker.engine.broker import Broker, BrokerCall, SimBroker

T0 = pd.Timestamp("2024-01-02 09:31")
T1 = pd.Timestamp("2024-01-02 09:45")


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
