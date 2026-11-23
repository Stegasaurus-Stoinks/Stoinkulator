"""Extra tests of the fake IB client: gaps found while verifying T3, and the
scenarios later tasks (reconnect, pacing, live bars, delayed data) will script.

Same rules as test_fake_ib.py: no real socket, no real sleep, every thread joined.
"""
import inspect
import re
import threading
from decimal import Decimal
from typing import Any

import pytest
from ibapi.client import EClient
from ibapi.contract import Contract

from tests.fake_ib import IMPLEMENTED, ConnectBehavior, FakeIbClient, make_bar
from tests.test_fake_ib import REQUEST_ARGS, Recorder

WAIT = 5.0

# Encoder message ids (ibapi.message.OUT) that place, change or cancel an order,
# or otherwise only make sense for a trading client.
_ORDER_IDS = re.compile(
    r"OUT\.(PLACE_ORDER|CANCEL_ORDER|REQ_GLOBAL_CANCEL|EXERCISE_OPTIONS|REQ_IDS|"
    r"REQ_OPEN_ORDERS|REQ_ALL_OPEN_ORDERS|REQ_AUTO_OPEN_ORDERS|REQ_EXECUTIONS|"
    r"REQ_COMPLETED_ORDERS|REQ_POSITIONS|REQ_ACCOUNT_DATA)\b"
)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def connect(f: FakeIbClient) -> None:
    f.connect("127.0.0.1", 7497, 21)


def drain_handshake(rec: Recorder, n: int = 6) -> None:
    for _ in range(n):
        rec.get()


# -- no order path -----------------------------------------------------------


def test_no_eclient_method_that_sends_an_order_message_exists_on_the_fake() -> None:
    # Derived from ibapi itself, so a new order method in a later ibapi, or a new
    # entry in IMPLEMENTED, cannot slip past a hand-written list.
    order_methods = {
        name
        for name, fn in inspect.getmembers(EClient, inspect.isfunction)
        if _ORDER_IDS.search(inspect.getsource(fn))
        or re.search(r"order", name, re.IGNORECASE)
        or name in ("sendMsg", "sendMsgProtoBuf")
    }
    assert {"placeOrder", "cancelOrder", "reqGlobalCancel", "reqIds"} <= order_methods
    assert order_methods.isdisjoint(IMPLEMENTED)
    for name in order_methods:
        assert not hasattr(FakeIbClient, name), name
    # And nothing the fake implements is itself an order message sender in EClient.
    for name in IMPLEMENTED:
        assert not _ORDER_IDS.search(inspect.getsource(getattr(EClient, name))), name


def test_unknown_attribute_is_an_attribute_error(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    for name in ("placeOrder", "sendMsg", "conn", "reqIds"):
        with pytest.raises(AttributeError):
            getattr(f, name)


# -- scripting robustness ----------------------------------------------------


def test_handler_that_raises_surfaces_in_the_calling_thread(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)

        def boom(*args: Any) -> None:
            raise RuntimeError("scripted failure")

        f.respond("reqCurrentTime", boom)
        with pytest.raises(RuntimeError, match="scripted failure"):
            f.reqCurrentTime()
        assert f.call_names == ["connect", "reqCurrentTime"]


def test_handler_with_a_bad_callback_fails_at_queue_time(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        f.respond("reqCurrentTime", lambda: [("noSuchCallback", 1)])
        with pytest.raises(AttributeError):
            f.reqCurrentTime()
        f.respond("reqCurrentTime", lambda: [("currentTime",)])  # wrong arity
        with pytest.raises(TypeError):
            f.reqCurrentTime()


def test_respond_with_a_list_plays_the_same_every_time(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        f.run_in_thread()
        drain_handshake(rec)
        f.respond("reqCurrentTime", [("currentTime", 7)])
        f.reqCurrentTime()
        f.reqCurrentTime()
        assert [rec.get()[:2] for _ in range(2)] == [("currentTime", (7,))] * 2


def test_push_before_connect_waits_and_a_disconnect_does_not_lose_it(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    f.push("marketDataType", 1, 3)
    assert rec.events.empty()
    connect(f)
    t = f.run_in_thread()
    assert rec.names(2) == ["connectAck", "marketDataType"]
    f.close()
    assert not t.is_alive()


def test_push_after_disconnect_is_delivered_by_the_next_run(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    connect(f)
    f.disconnect()
    f.push("currentTime", 5)
    f.run()  # not open, but the queue is not empty: ibapi's loop condition too
    names = [rec.get()[0] for _ in range(rec.events.qsize())]
    assert "currentTime" in names
    f.close()


def test_drop_while_a_request_is_pending_still_delivers_the_response(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        f.respond("reqCurrentTime", [("currentTime", 9)])
        f.reqCurrentTime()
        f.drop(1100)
        f.run_in_thread()
        names = [rec.get()[0] for _ in range(8)]
        assert names.index("currentTime") < len(names) - 1
        assert names[-1] == "error"


def test_hang_is_released_by_close(rec: Recorder) -> None:
    f = FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG)
    t = threading.Thread(target=connect, args=(f,))
    t.start()
    assert f.connect_entered.wait(WAIT)
    f.close()
    t.join(WAIT)
    assert not t.is_alive()


def test_hang_timeout_ends_the_connect_by_itself(rec: Recorder) -> None:
    f = FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG, hang_timeout=0.05)
    connect(f)  # returns after the (injected, short) timeout
    assert not f.isConnected()


def test_no_thread_is_left_after_close(rec: Recorder) -> None:
    before = set(threading.enumerate())
    f = FakeIbClient(rec)
    connect(f)
    f.run_in_thread()
    f.close()
    assert set(threading.enumerate()) <= before


def test_recorded_arguments_are_deep_copies(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        contract = Contract()
        contract.symbol = "AAPL"
        opts: list[Any] = []
        f.reqHeadTimeStamp(3, contract, "TRADES", 1, 2)
        f.reqContractDetails(4, contract)
        contract.symbol = "MSFT"
        opts.append(1)
        assert f.calls_named("reqHeadTimeStamp")[0].args[1].symbol == "AAPL"
        assert f.calls_named("reqContractDetails")[0].args[1].symbol == "AAPL"


def test_two_fakes_do_not_share_state(rec: Recorder) -> None:
    a, b = FakeIbClient(rec), FakeIbClient(Recorder())
    connect(a)
    a.respond("reqCurrentTime", [("currentTime", 1)])
    assert b.calls == [] and b.connects == 0
    assert a.connects == 1
    b.reqCurrentTime()  # not connected: 504, and no handler of a's
    assert b.call_names == ["reqCurrentTime"]
    a.close()
    b.close()


# -- scenarios later tasks will script ---------------------------------------


def test_reconnect_after_1100_then_1102_and_after_a_socket_drop(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    connect(f)
    t = f.run_in_thread()
    drain_handshake(rec)
    f.drop(1100)
    f.push("error", -1, 1, 1102, "Connectivity restored - data maintained", "")
    codes = [rec.get()[1][2] for _ in range(2)]
    assert codes == [1100, 1102]
    assert f.isConnected()
    f.drop("connectionClosed")
    assert rec.names(1) == ["connectionClosed"]
    t.join(WAIT)
    assert not t.is_alive() and not f.isConnected()
    connect(f)  # second connect on the same fake
    assert f.connects == 2 and f.isConnected()
    f.run_in_thread()
    assert rec.names(1) == ["connectAck"]
    f.close()


def test_pacing_violation_162_on_one_request_id(rec: Recorder) -> None:
    def history(req_id: int, *rest: Any) -> list[tuple[Any, ...]]:
        if req_id == 2:
            return [("error", 2, 1, 162, "Historical Market Data Service error message: pacing", "")]
        return [("historicalData", req_id, make_bar(1, 1, 2, 0.5, 1.5, 10)),
                ("historicalDataEnd", req_id, "s", "e")]

    with FakeIbClient(rec) as f:
        connect(f)
        f.run_in_thread()
        drain_handshake(rec)
        f.respond("reqHistoricalData", history)
        for rid in (1, 2):
            f.reqHistoricalData(rid, Contract(), "", "1 D", "1 min", "TRADES", 1, 2, False, [])
        got = [rec.get() for _ in range(3)]
        assert [(g[0], g[1][0]) for g in got] == [
            ("historicalData", 1), ("historicalDataEnd", 1), ("error", 2)
        ]
        assert got[2][1][2] == 162


def test_keep_up_to_date_stream_delivers_updates_over_time(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        f.run_in_thread()
        drain_handshake(rec)
        f.respond("reqHistoricalData", lambda rid, *a: [
            ("historicalData", rid, make_bar(100, 1, 1, 1, 1, 5)),
            ("historicalDataEnd", rid, "s", "e"),
        ])
        f.reqHistoricalData(8, Contract(), "", "1 D", "1 min", "TRADES", 1, 2, True, [])
        assert f.calls_named("reqHistoricalData")[0].args[8] is True
        assert rec.names(2) == ["historicalData", "historicalDataEnd"]
        for i in range(3):  # later, from the test thread
            f.push("historicalDataUpdate", 8, make_bar(100 + 60 * (i // 2), 1, 2, 1, 1.5, 6 + i))
        updates = [rec.get() for _ in range(3)]
        assert [u[0] for u in updates] == ["historicalDataUpdate"] * 3
        assert [u[1][1].volume for u in updates] == [Decimal(6), Decimal(7), Decimal(8)]
        f.cancelHistoricalData(8)
        assert f.call_names[-1] == "cancelHistoricalData"


def test_delayed_data_probe_market_data_type_3_then_ticks(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        f.run_in_thread()
        drain_handshake(rec)

        def mkt(rid: int, *a: Any) -> list[tuple[Any, ...]]:
            return [("marketDataType", rid, 3), ("tickPrice", rid, 68, 189.5, None)]

        f.respond("reqMktData", mkt)
        f.reqMarketDataType(3)
        f.reqMktData(11, Contract(), "", False, False, [])
        assert f.calls_named("reqMarketDataType")[0].args == (3,)
        first, second = rec.get(), rec.get()
        assert first[:2] == ("marketDataType", (11, 3))
        assert second[0] == "tickPrice" and second[1][1] == 68
        f.cancelMktData(11)


def test_repeated_runs_in_one_process_leak_nothing(rec: Recorder) -> None:
    for _ in range(3):
        r = Recorder()
        with FakeIbClient(r) as f:
            connect(f)
            f.run_in_thread()
            drain_handshake(r)
            assert f.connects == 1
        assert r.names(1) == ["connectionClosed"]  # from close(), once, and nothing else
        assert r.events.empty()
    assert not [t for t in threading.enumerate() if t.name == "fake-ib-run"]


def test_every_implemented_method_is_called_under_a_socket_spy(
    monkeypatch: pytest.MonkeyPatch, rec: Recorder
) -> None:
    # test_fake_never_opens_a_socket only calls connect, reqCurrentTime, run and
    # disconnect; a socket opened in any other method would go unnoticed there.
    import socket

    touched: list[str] = []

    def spy(*a: Any, **k: Any) -> Any:
        touched.append("socket")
        raise AssertionError("socket used by the fake")

    monkeypatch.setattr(socket, "socket", spy)
    monkeypatch.setattr(socket, "create_connection", spy)
    monkeypatch.setattr(socket, "getaddrinfo", spy)
    calls: dict[str, tuple[Any, ...]] = {
        "serverVersion": (),
        "isConnected": (),
        "reqCurrentTime": (),
        "reqMktData": (1, Contract(), "", False, False, []),
        "cancelMktData": (1,),
        "reqMarketDataType": (3,),
        "reqContractDetails": (2, Contract()),
        "reqHeadTimeStamp": (3, Contract(), "TRADES", 1, 2),
        "cancelHeadTimeStamp": (3,),
        "reqHistoricalData": (4, Contract(), "", "1 D", "1 min", "TRADES", 1, 2, True, []),
        "cancelHistoricalData": (4,),
    }
    assert set(calls) | {"connect", "disconnect", "run"} == set(IMPLEMENTED)
    with FakeIbClient(rec) as f:
        connect(f)
        for name, args in calls.items():
            getattr(f, name)(*args)
        f.run_in_thread()
    assert touched == []


# -- fidelity to ibapi 10.37: thread and ordering behaviour -------------------
#
# The real client is driven here without a socket: an EClient that was never
# connected (the 504 path), or one whose Connection holds a stand-in for the
# socket (the disconnect path). The fake must do what that real code does.


class _StubSocket:
    def close(self) -> None:
        pass


def _real_connected_client(wrapper: Recorder) -> EClient:
    from ibapi.connection import Connection

    client = EClient(wrapper)
    client.conn = Connection("127.0.0.1", 7497)  # __init__ opens nothing
    client.conn.socket = _StubSocket()
    client.setConnState(EClient.CONNECTED)
    return client


def _closed(rec: Recorder) -> list[int]:
    """Thread ids of every connectionClosed in rec, in order."""
    out = []
    while not rec.events.empty():
        name, _, tid = rec.get()
        if name == "connectionClosed":
            out.append(tid)
    return out


def test_connect_ack_is_called_inside_connect_on_the_calling_thread() -> None:
    # client.py: connectAck() is the last statement of connect(), after startApi.
    seen: list[Any] = []

    class Ack(Recorder):
        def connectAck(self) -> None:
            seen.append((threading.get_ident(), fake.isConnected(), fake.serverVersion()))
            super().connectAck()

    ack = Ack()
    with FakeIbClient(ack) as fake:
        connect(fake)  # no run() thread exists yet
        assert seen == [(threading.get_ident(), True, fake.serverVersion())]
        assert seen[0][2] is not None
        assert ack.names(1) == ["connectAck"]
        assert ack.events.empty(), "nextValidId, managedAccounts and the 21xx wait for run()"
        fake.run_in_thread()
        assert ack.names(2) == ["nextValidId", "managedAccounts"]
        assert [d.name for d in fake.delivered][:1] == ["connectAck"]


def test_peer_close_is_not_connected_at_once_and_closed_once_on_the_run_thread(
    rec: Recorder,
) -> None:
    # Real: the reader thread closes the socket (isConnected False, nothing reported
    # because Connection.wrapper is never set); run() drains, then its finally
    # disconnect() reports connectionClosed once, on the run thread.
    real_rec = Recorder()
    real = _real_connected_client(real_rec)
    real.conn.socket = None  # the reader saw the close
    assert not real.isConnected()
    assert real_rec.events.empty()
    real.run()
    assert _closed(real_rec) == [threading.get_ident()]
    real.disconnect()
    assert real_rec.events.empty()

    with FakeIbClient(rec) as fake:
        connect(fake)
        fake.drop("connectionClosed")
        assert not fake.isConnected(), "not connected at once, not when run() consumes an event"
        run_thread = fake.run_in_thread()
        run_thread.join(WAIT)
        assert not run_thread.is_alive()
        names = [rec.get() for _ in range(rec.events.qsize())]
        closed = [n for n in names if n[0] == "connectionClosed"]
        assert [c[2] for c in closed] == [run_thread.ident]
        fake.disconnect()
        assert rec.events.empty(), "a second connectionClosed would be a double callback"
        assert [d.name for d in fake.delivered].count("connectionClosed") == 1


def test_peer_close_with_no_run_reports_nothing_until_disconnect(rec: Recorder) -> None:
    with FakeIbClient(rec) as fake:
        connect(fake)
        fake.drop("connectionClosed")
        assert not fake.isConnected()
        assert [d.name for d in fake.delivered] == ["connectAck"]
        fake.disconnect()  # the caller's disconnect reports it, once, on the caller
        assert [d.name for d in fake.delivered] == ["connectAck", "connectionClosed"]
        assert fake.delivered[-1].thread_id == threading.get_ident()


def test_caller_disconnect_closes_once_on_the_caller_thread_and_run_adds_nothing(
    rec: Recorder,
) -> None:
    real_rec = Recorder()
    real = _real_connected_client(real_rec)
    real.disconnect()
    assert not real.isConnected()
    assert _closed(real_rec) == [threading.get_ident()]
    real.run()  # the finally disconnect() finds no connection object
    real.disconnect()
    assert real_rec.events.empty()

    with FakeIbClient(rec) as fake:
        connect(fake)
        run_thread = fake.run_in_thread()
        fake.disconnect()
        assert not fake.isConnected(), "at once"
        run_thread.join(WAIT)
        assert not run_thread.is_alive()
        fake.disconnect()
        assert [d.thread_id for d in fake.delivered if d.name == "connectionClosed"] == [
            threading.get_ident()
        ]


def test_disconnect_of_a_client_that_never_connected_reports_nothing(rec: Recorder) -> None:
    real_rec = Recorder()
    EClient(real_rec).disconnect()
    assert real_rec.events.empty()
    fake = FakeIbClient(rec)
    fake.disconnect()
    fake.close()
    assert rec.events.empty()


_REQUESTS = sorted(set(REQUEST_ARGS))


@pytest.mark.parametrize("method", _REQUESTS)
def test_not_connected_504_matches_real_ibapi_per_method(method: str, rec: Recorder) -> None:
    args = REQUEST_ARGS[method]
    real_rec = Recorder()
    getattr(EClient(real_rec), method)(*args)
    real = real_rec.get()
    fake = FakeIbClient(rec)
    getattr(fake, method)(*args)
    got = rec.get()
    assert got[0] == real[0] == "error"
    assert got[2] == threading.get_ident() == real[2]
    # reqId, code and text; the timestamp is the clock
    assert (got[1][0], got[1][2], got[1][3]) == (real[1][0], real[1][2], real[1][3])
    assert got[1][2] == 504
    assert rec.events.empty()


def test_not_connected_504_uses_the_request_id_where_ibapi_does() -> None:
    # Guards the parametrized test above against being vacuous: with id 77, the
    # real client answers 77 for exactly these three and -1 for the rest.
    ids = {}
    for method in _REQUESTS:
        real_rec = Recorder()
        getattr(EClient(real_rec), method)(*REQUEST_ARGS[method])
        ids[method] = real_rec.get()[1][0]
    assert {m for m, i in ids.items() if i == 77} == {
        "reqMktData",
        "cancelMktData",
        "reqHistoricalData",
    }
    assert {i for i in ids.values() if i != 77} == {-1}


def test_is_connected_and_server_version_are_counted_not_recorded(rec: Recorder) -> None:
    with FakeIbClient(rec) as f:
        connect(f)
        for _ in range(5):
            f.isConnected()
        f.serverVersion()
        f.reqCurrentTime()
        assert f.call_names == ["connect", "reqCurrentTime"]
        assert (f.is_connected_calls, f.server_version_calls) == (5, 1)


def test_respond_with_a_list_hands_the_wrapper_a_fresh_object_each_time(rec: Recorder) -> None:
    bar = make_bar(100, 1, 2, 0.5, 1.5, 10)
    contract = Contract()
    contract.symbol = "AAPL"
    with FakeIbClient(rec) as f:
        connect(f)
        f.run_in_thread()
        drain_handshake(rec)
        f.respond("reqHistoricalData", [("historicalData", 9, bar)])
        f.reqHistoricalData(9, contract, "", "1 D", "1 min", "TRADES", 1, 2, False, [])
        f.reqHistoricalData(9, contract, "", "1 D", "1 min", "TRADES", 1, 2, False, [])
        first, second = rec.get()[1][1], rec.get()[1][1]
        assert first is not second and first is not bar and second is not bar
        first.close = 999.0  # a wrapper that mutates what it is given
        assert second.close == 1.5 and bar.close == 1.5
        f.respond(
            "reqContractDetails",
            lambda *a: [("contractDetailsEnd", 1), ("contractDetails", 1, contract)],
        )
        f.reqContractDetails(1, contract)
        rec.get()
        delivered = rec.get()[1][1]
        assert delivered is not contract and delivered.symbol == "AAPL"
