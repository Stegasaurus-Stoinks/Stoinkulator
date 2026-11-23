"""Tests of the scripted fake IB client (tests/fake_ib.py), against ibapi 10.37.

No test here opens a socket, sleeps for real or leaves a thread running: every
thread is started through the fake (joined by ``close``) or joined by the test.
"""
import ast
import inspect
import socket
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.client import EClient
from ibapi.common import BarData
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from tests.fake_ib import (
    CLIENT_ID_IN_USE,
    IMPLEMENTED,
    REQUEST_ARGS,
    WAIT,
    Call,
    ConnectBehavior,
    FakeIbClient,
    HangNeverReleased,
    Recorder,
    make_bar,
)

ORDER_CAPABLE = (
    "placeOrder",
    "placeOrderProtoBuf",
    "cancelOrder",
    "cancelOrderProtoBuf",
    "reqGlobalCancel",
    "reqGlobalCancelProtoBuf",
    "exerciseOptions",
    "reqIds",
    "reqOpenOrders",
    "reqAllOpenOrders",
    "reqAutoOpenOrders",
    "reqExecutions",
    "reqCompletedOrders",
    "reqPositions",
    "reqAccountUpdates",
    "sendMsg",
    "sendMsgProtoBuf",
)



@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def fake(rec: Recorder):
    with FakeIbClient(rec) as f:
        yield f


def connect(fake: FakeIbClient) -> None:
    fake.connect("127.0.0.1", 7497, 21)


# -- the acceptance tests ----------------------------------------------------


@pytest.mark.parametrize("name", IMPLEMENTED)
def test_fake_signatures_match_eclient(name: str) -> None:
    assert inspect.signature(getattr(FakeIbClient, name)) == inspect.signature(
        getattr(EClient, name)
    )


def test_fake_callbacks_exist_on_ewrapper_with_matching_arity(rec: Recorder) -> None:
    # Everything the fake itself plays, in every connect behaviour, plus the
    # callbacks a scripted session plays, is checked against EWrapper.
    seen: set[str] = set()
    for behavior in ConnectBehavior:
        if behavior in (
            ConnectBehavior.HANG,
            ConnectBehavior.REFUSE,
            ConnectBehavior.CLOSED_IN_HANDSHAKE,
        ):
            continue
        with FakeIbClient(rec, connect_behavior=behavior) as f:
            connect(f)
            f.respond("reqCurrentTime", [("currentTime", 1)])
            f.reqCurrentTime()
            f.drop(1100)
            f.drop("connectionClosed")
            f.run()
            seen.update(d.name for d in f.delivered)
            for d in f.delivered:
                assert callable(getattr(EWrapper, d.name))
                inspect.signature(getattr(EWrapper, d.name)).bind(None, *d.args)
    assert {"connectAck", "nextValidId", "managedAccounts", "error", "connectionClosed"} <= seen

    f = FakeIbClient(rec)
    with pytest.raises(TypeError):
        f.push("nextValidId")  # arity
    with pytest.raises(TypeError):
        f.push("tickPrice", 1, 2, 3)  # arity
    with pytest.raises(AttributeError):
        f.push("noSuchCallback", 1)
    with pytest.raises(AttributeError):
        f.respond("reqCurrentTime", [("noSuchCallback",)])
    f.push("nextValidId", 5)  # right arity is accepted


def test_fake_delivers_callbacks_on_run_thread(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    run_thread = fake.run_in_thread()
    fake.respond("reqCurrentTime", [("currentTime", 1234)])
    fake.reqCurrentTime()
    fake.push("marketDataType", 7, 3)
    fake.drop(1100)
    got = [rec.get() for _ in range(9)]  # 7 from the handshake + 2 pushed (+ response)
    assert {"currentTime", "marketDataType", "connectAck"} <= {g[0] for g in got}
    # connectAck is the one exception: ibapi calls it from connect(), on the caller.
    assert [g[2] for g in got if g[0] == "connectAck"] == [threading.get_ident()]
    assert {g[2] for g in got if g[0] != "connectAck"} == {run_thread.ident}
    assert run_thread.ident != threading.get_ident()
    assert {d.thread_id for d in fake.delivered if d.name != "connectAck"} == {run_thread.ident}


def connect_in_thread(fake: FakeIbClient, errors: list[BaseException]) -> threading.Thread:
    """connect() on its own thread; whatever it raises is appended to ``errors``."""

    def target() -> None:
        try:
            connect(fake)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            errors.append(exc)

    t = threading.Thread(target=target, name="fake-ib-connect")
    t.start()
    return t


def test_fake_hang_released_by_disconnect(rec: Recorder) -> None:
    fake = FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG)
    errors: list[BaseException] = []
    t = connect_in_thread(fake, errors)
    try:
        assert fake.connect_entered.wait(WAIT)
        t.join(0.05)
        assert t.is_alive(), "a hanging connect must not return by itself"
        assert not fake.isConnected()
        fake.disconnect()
        t.join(WAIT)
        assert not t.is_alive()
    finally:
        fake.disconnect()
        t.join(WAIT)
    assert not fake.isConnected()
    # Real ibapi: the connecting thread dies with AttributeError (conn is None after reset()).
    assert [type(e) for e in errors] == [AttributeError]
    assert "isConnected" in str(errors[0])
    # A hung connect has no connectAck. Its socket is open, so disconnect() reports
    # connectionClosed once (client.py), on the thread that called disconnect().
    assert rec.get()[::2] == ("connectionClosed", threading.get_ident())
    assert rec.events.empty()


def test_hang_never_released_fails_loudly_not_silently(rec: Recorder) -> None:
    fake = FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG, hang_timeout=0.05)
    with pytest.raises(HangNeverReleased, match="never released"):
        connect(fake)
    assert issubclass(HangNeverReleased, AssertionError)
    assert not fake.isConnected()
    fake.connect_entered.clear()
    fake.script_connect(ConnectBehavior.ACCEPT)
    connect(fake)  # the client holds no connection afterwards, so a retry is possible
    assert fake.isConnected()
    fake.close()


def test_connect_entered_is_usable_per_attempt(rec: Recorder) -> None:
    fake = FakeIbClient(rec, connect_behavior=[ConnectBehavior.HANG, ConnectBehavior.HANG])
    errors: list[BaseException] = []
    for attempt in (1, 2):
        t = connect_in_thread(fake, errors)
        assert fake.wait_for_hang(attempt, WAIT), f"hang {attempt} never started"
        assert fake.connect_entered.is_set() and fake.hangs_entered == attempt
        fake.disconnect()
        t.join(WAIT)
        assert not t.is_alive()
    assert [type(e) for e in errors] == [AttributeError] * 2
    assert not fake.wait_for_hang(3, 0.01)
    fake.close()


def test_fake_has_no_order_methods() -> None:
    f = FakeIbClient(Recorder())
    for name in ORDER_CAPABLE:
        assert not hasattr(FakeIbClient, name), name
        assert not hasattr(f, name), name
    # Whatever EClient method exists on the fake is one that was chosen on purpose.
    public = {n for n in dir(FakeIbClient) if not n.startswith("_")}
    assert {n for n in public if hasattr(EClient, n)} == set(IMPLEMENTED)
    assert "__getattr__" not in vars(FakeIbClient)
    assert not issubclass(FakeIbClient, EClient)


def test_fake_never_opens_a_socket(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> None:
    touched: list[str] = []

    def spy(name: str):
        def f(*a: Any, **k: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"{name} used by the fake")

        return f

    class SpySocket:
        def __init__(self, *a: Any, **k: Any) -> None:
            touched.append("socket.socket")
            raise AssertionError("socket.socket used by the fake")

    monkeypatch.setattr(socket, "socket", SpySocket)
    monkeypatch.setattr(socket, "create_connection", spy("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", spy("getaddrinfo"))
    import ibapi.connection as ibconn

    monkeypatch.setattr(ibconn.Connection, "__init__", spy("Connection.__init__"))
    monkeypatch.setattr(ibconn.Connection, "connect", spy("Connection.connect"))
    monkeypatch.setattr(EClient, "connect", spy("EClient.connect"))

    for behavior in ConnectBehavior:
        if behavior is ConnectBehavior.HANG:
            continue
        with FakeIbClient(rec, connect_behavior=behavior) as f:
            connect(f)
            for method, args in REQUEST_ARGS.items():  # every request, not only one
                getattr(f, method)(*args)
            f.isConnected()
            f.serverVersion()
            run_thread = f.run_in_thread()
            f.drop("connectionClosed")  # a peer close: run() drains its queue, then ends
            run_thread.join(WAIT)  # joined before the block ends: close() must not race run()
            assert not run_thread.is_alive()
    # ... and the same requests while not connected (the 504 path).
    unconnected = FakeIbClient(rec)
    for method, args in REQUEST_ARGS.items():
        getattr(unconnected, method)(*args)
    assert set(REQUEST_ARGS) | {"connect", "disconnect", "run", "isConnected", "serverVersion"} == set(
        IMPLEMENTED
    )
    with FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG) as f:
        t = connect_in_thread(f, errors := [])
        assert f.connect_entered.wait(WAIT)
        f.disconnect()
        t.join(WAIT)
        assert [type(e) for e in errors] == [AttributeError]
    assert touched == []


def test_fake_module_does_not_import_ibapi_connection() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(FakeIbClient)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all("connection" not in a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "connection" not in (node.module or "")
            assert all(a.name != "connection" for a in node.names)


# -- connect behaviours ------------------------------------------------------


def test_accept_plays_the_handshake_on_run(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    assert fake.isConnected()
    assert fake.serverVersion() is not None
    # Only connectAck is there before run(); the rest waits for run().
    assert rec.events.qsize() == 1, "callbacks other than connectAck wait for run()"
    fake.run_in_thread()
    first = [rec.get() for _ in range(6)]
    assert [g[0] for g in first] == ["connectAck", "nextValidId", "managedAccounts"] + ["error"] * 3
    assert first[1][1] == (1,)
    assert [g[1][2] for g in first[3:]] == [2104, 2106, 2158]
    assert all(g[1][0] == -1 for g in first[3:])


def test_refuse_reports_502_on_the_calling_thread(rec: Recorder) -> None:
    f = FakeIbClient(rec, connect_behavior=ConnectBehavior.REFUSE)
    connect(f)
    # client.py 355-359: error 502 (four arguments), then disconnect() -> connectionClosed,
    # both on the calling thread, before connect() returns.
    name, args, tid = rec.get()
    assert (name, args[2], tid) == ("error", 502, threading.get_ident())
    assert "Couldn't connect to TWS" in args[3]
    assert len(f.delivered[0].args) == 4
    assert rec.get()[::2] == ("connectionClosed", threading.get_ident())
    assert not f.isConnected()
    assert rec.events.empty()
    f.disconnect()
    assert rec.events.empty(), "the refused connection is already closed"
    assert [d.name for d in f.delivered] == ["error", "connectionClosed"]


def test_client_id_in_use_is_326_then_close(rec: Recorder) -> None:
    with FakeIbClient(rec, connect_behavior=ConnectBehavior.CLIENT_ID_IN_USE) as f:
        connect(f)
        # TWS closed the socket right after the error: not connected at once,
        # before anything has run (ibapi: the reader thread sees the close).
        assert not f.isConnected()
        assert rec.names(1) == ["connectAck"]
        t = f.run_in_thread()
        name, args, error_tid = rec.get()
        assert (name, args[2]) == ("error", CLIENT_ID_IN_USE)
        # connectionClosed comes once, from run()'s disconnect, on the run thread.
        assert rec.get()[::2] == ("connectionClosed", t.ident) == ("connectionClosed", error_tid)
        t.join(WAIT)
        assert not t.is_alive(), "run() ends when the socket closes"
        assert rec.events.empty()
        f.disconnect()
        assert rec.events.empty(), "a later disconnect reports nothing"


def test_accept_without_next_valid_id(rec: Recorder) -> None:
    with FakeIbClient(rec, connect_behavior=ConnectBehavior.NO_NEXT_VALID_ID) as f:
        connect(f)
        f.run_in_thread()
        names = rec.names(5)
        assert "nextValidId" not in names
        assert names[:2] == ["connectAck", "managedAccounts"]
        assert f.isConnected()


def test_connect_behaviours_script_per_attempt(rec: Recorder) -> None:
    f = FakeIbClient(rec, connect_behavior=[ConnectBehavior.REFUSE] * 2 + [ConnectBehavior.ACCEPT])
    for _ in range(2):
        connect(f)
        assert not f.isConnected()
    connect(f)
    assert f.isConnected()
    assert f.connects == 3
    assert len(f.calls_named("connect")) == 3
    f.close()


def test_connect_twice_without_disconnect_raises_assertion_error(
    fake: FakeIbClient, rec: Recorder
) -> None:
    # Real ibapi has no 501: it opens a second socket and orphans the first.
    connect(fake)
    with pytest.raises(AssertionError, match="second socket"):
        connect(fake)
    assert fake.connects == 1
    assert rec.names(1) == ["connectAck"]
    assert rec.events.empty()
    fake.disconnect()
    connect(fake)  # after a disconnect it is fine
    assert fake.connects == 2


def test_connect_after_a_refused_or_closed_attempt_is_allowed(
    rec: Recorder,
) -> None:
    f = FakeIbClient(rec, connect_behavior=[ConnectBehavior.REFUSE, ConnectBehavior.CLOSED_IN_HANDSHAKE,
                                            ConnectBehavior.ACCEPT])
    connect(f)  # refused: the client disconnected itself
    connect(f)  # closed in the handshake: reset by the client itself
    connect(f)
    assert f.isConnected() and f.connects == 3
    f.close()


def test_closed_in_handshake_returns_silently(rec: Recorder) -> None:
    # client.py 326-331: recvMsg() saw the close -> reset() and return. No callback at all.
    f = FakeIbClient(rec, connect_behavior=ConnectBehavior.CLOSED_IN_HANDSHAKE)
    connect(f)
    assert rec.events.empty() and f.delivered == []
    assert not f.isConnected()
    assert f.connState == EClient.DISCONNECTED and f.serverVersion() is None
    f.disconnect()
    assert rec.events.empty(), "no connection object is left, so disconnect() reports nothing"
    f.run()  # nothing queued, nothing open: returns at once
    assert rec.events.empty()


def test_drop_texts_per_code(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    fake.run_in_thread()
    for _ in range(6):
        rec.get()
    for code, needle in ((1100, "lost"), (1101, "data lost"), (1102, "data maintained"),
                         (2110, "broken")):
        fake.drop(code)
        got = rec.get()
        assert (got[0], got[1][2]) == ("error", code)
        assert needle in got[1][3]
    fake.drop(1300, "Socket port has been reset")
    assert rec.get()[1][3] == "Socket port has been reset"
    with pytest.raises(ValueError, match="needs a text"):
        fake.drop(1300)


# -- requests, responses, pushes, drops --------------------------------------


def test_requests_are_recorded_with_arguments_in_signature_order(fake: FakeIbClient) -> None:
    connect(fake)
    contract = Contract()
    contract.symbol = "AAPL"
    fake.reqHistoricalData(4, contract, "", "1 D", "1 min", "TRADES", 1, 2, False, [])
    contract.symbol = "MSFT"  # mutated after the call: the record keeps AAPL
    fake.reqMktData(5, contract, "", False, False, [])
    fake.cancelMktData(5)
    fake.reqMarketDataType(3)
    assert fake.call_names == [
        "connect",
        "reqHistoricalData",
        "reqMktData",
        "cancelMktData",
        "reqMarketDataType",
    ]
    hist = fake.calls_named("reqHistoricalData")[0]
    assert isinstance(hist, Call)
    assert hist.args[0] == 4 and hist.args[1].symbol == "AAPL"
    assert hist.args[2:] == ("", "1 D", "1 min", "TRADES", 1, 2, False, [])
    assert fake.calls_named("reqMktData")[0].args[:1] == (5,)
    assert fake.calls_named("connect")[0].args == ("127.0.0.1", 7497, 21)


def test_respond_plays_history_and_make_bar(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    fake.run_in_thread()
    for _ in range(6):
        rec.get()

    def history(req_id, contract, end, duration, *rest):
        return [
            ("historicalData", req_id, make_bar(1700000000 + 60 * i, 1, 2, 0.5, 1.5, 100 + i))
            for i in range(2)
        ] + [("historicalDataEnd", req_id, "s", "e")]

    fake.respond("reqHistoricalData", history)
    fake.reqHistoricalData(9, Contract(), "", "1 D", "1 min", "TRADES", 1, 2, False, [])
    events = [rec.get() for _ in range(3)]
    assert [e[0] for e in events] == ["historicalData", "historicalData", "historicalDataEnd"]
    assert events[1][1][0] == 9
    assert events[1][1][1].date == "1700000060"
    assert events[1][1][1].volume == Decimal(101)


def test_respond_rejects_non_requests(fake: FakeIbClient) -> None:
    with pytest.raises(ValueError):
        fake.respond("placeOrder", [])
    with pytest.raises(ValueError):
        fake.respond("connect", [])


def test_unscripted_request_is_recorded_and_silent(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    fake.run_in_thread()
    for _ in range(6):
        rec.get()
    fake.reqContractDetails(1, Contract())
    fake.push("contractDetailsEnd", 1)  # a barrier: queue order proves nothing came before it
    assert rec.get()[0] == "contractDetailsEnd"
    assert rec.events.empty()


def test_request_while_not_connected_answers_504(fake: FakeIbClient, rec: Recorder) -> None:
    fake.reqCurrentTime()
    name, args, tid = rec.get()
    assert (name, args[2], tid) == ("error", 504, threading.get_ident())
    assert fake.call_names == ["reqCurrentTime"]


def test_drop_1100_is_an_error_not_a_close(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    fake.run_in_thread()
    for _ in range(6):
        rec.get()
    fake.drop(1100)
    name, args, _ = rec.get()
    assert (name, args[0], args[2]) == ("error", -1, 1100)
    assert fake.isConnected()


def test_drop_connection_closed_ends_run(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    t = fake.run_in_thread()
    for _ in range(6):
        rec.get()
    fake.drop("connectionClosed")
    name, _, tid = rec.get()
    assert (name, tid) == ("connectionClosed", t.ident)
    t.join(WAIT)
    assert not t.is_alive()
    assert not fake.isConnected()
    assert fake.serverVersion() is None
    with pytest.raises(ValueError):
        fake.drop("nonsense")


def test_disconnect_closes_once_and_stops_run(fake: FakeIbClient, rec: Recorder) -> None:
    connect(fake)
    t = fake.run_in_thread()
    for _ in range(6):
        rec.get()
    fake.disconnect()
    fake.disconnect()
    t.join(WAIT)
    assert not t.is_alive()
    assert rec.names(1) == ["connectionClosed"]
    assert rec.events.empty()


def test_close_joins_threads(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    connect(f)
    t = f.run_in_thread()
    for _ in range(6):  # let run() deliver what is queued: a disconnect first would lose it
        rec.get()
    f.close()
    assert not t.is_alive()


def test_make_bar_is_a_bar_data_with_decimal_volume() -> None:
    bar = make_bar(1700000000, 1.0, 2.0, 0.5, 1.5, 250)
    assert isinstance(bar, BarData)
    assert (bar.date, bar.open, bar.high, bar.low, bar.close) == ("1700000000", 1.0, 2.0, 0.5, 1.5)
    assert bar.volume == Decimal(250) and isinstance(bar.volume, Decimal)
    assert make_bar(1, 1, 1, 1, 1, Decimal("2.5")).volume == Decimal("2.5")
