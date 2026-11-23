"""Extra tests of the fake IB client: gaps found while verifying T3, and the
scenarios later tasks (reconnect, pacing, live bars, delayed data) will script.

Same rules as test_fake_ib.py: no real socket, no real sleep, every thread joined.
"""
import inspect
import re
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from ibapi.client import EClient
from ibapi.contract import Contract

from tests.fake_ib import (
    IMPLEMENTED,
    REQUEST_ARGS,
    WAIT,
    ConnectBehavior,
    FakeIbClient,
    HangNeverReleased,
    Recorder,
    make_bar,
)

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


def test_run_after_caller_disconnect_with_queued_messages_raises_type_error(rec: Recorder) -> None:
    # client.py 426: reset() left serverVersion_ None; `None >= MIN_SERVER_VER_PROTOBUF` raises
    # for the first queued message. Pinned against the real client in the comparison tests.
    f = FakeIbClient(rec)
    connect(f)
    f.disconnect()
    f.push("currentTime", 5)
    assert isinstance(_run_catching(f.run), TypeError)
    assert [d.name for d in f.delivered] == ["connectAck", "connectionClosed"]
    f.close()


def test_run_after_peer_close_still_drains_the_queue(rec: Recorder) -> None:
    f = FakeIbClient(rec)
    connect(f)
    f.push("currentTime", 5)
    f.drop("connectionClosed")  # no reset(): serverVersion() stays set
    assert _run_catching(f.run) is None
    names = [d.name for d in f.delivered]
    assert names[:1] == ["connectAck"] and "currentTime" in names and names[-1] == "connectionClosed"
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
    errors: list[BaseException] = []
    t = threading.Thread(target=_catching(lambda: connect(f), errors))
    t.start()
    assert f.connect_entered.wait(WAIT)
    f.close()
    t.join(WAIT)
    assert not t.is_alive()
    assert [type(e) for e in errors] == [AttributeError]


def test_hang_timeout_is_an_error_not_a_silent_failed_connect(rec: Recorder) -> None:
    f = FakeIbClient(rec, connect_behavior=ConnectBehavior.HANG, hang_timeout=0.05)
    with pytest.raises(HangNeverReleased, match="never released"):
        connect(f)  # the (injected, short) valve fires
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


def _real_connected_client(wrapper: Recorder, server_version: int | None = None) -> EClient:
    from ibapi.connection import Connection

    client = EClient(wrapper)
    client.conn = Connection("127.0.0.1", 7497)  # __init__ opens nothing
    client.conn.socket = _StubSocket()
    client.setConnState(EClient.CONNECTED)
    if server_version is not None:
        from ibapi import decoder

        client.serverVersion_ = server_version
        client.decoder = decoder.Decoder(wrapper, server_version)
    return client


def _catching(fn: Callable[[], Any], errors: list[BaseException]) -> Callable[[], None]:
    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            errors.append(exc)

    return target


def _run_catching(run: Callable[[], Any]) -> BaseException | None:
    """Run ``run()`` on a daemon thread with a join timeout, so a mistake fails, not hangs."""
    errors: list[BaseException] = []
    t = threading.Thread(target=_catching(run, errors), name="run-under-test", daemon=True)
    t.start()
    t.join(WAIT)
    assert not t.is_alive(), "run() did not end"
    return errors[0] if errors else None


class _RawRecorder(Recorder):
    """Records ``error`` with exactly the positional arguments it was called with."""

    def error(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        assert not kwargs
        self._put("error", *args)


class _StubConnection:
    """A stand-in for ibapi's ``Connection``, installed under ``ibapi.client.Connection``.

    It has no socket and no DNS: ``EClient.connect`` runs for real against it. It
    keeps the real ``Connection``'s observable behaviour for each mode:

    * ``refuse``: ``connect`` swallows the refusal (connection.py 44-47, its wrapper
      is None) and ``sendMsg`` then raises ``OSError`` (an unconnected socket).
    * ``hang``: TCP is up, ``recvMsg`` returns an empty buffer on every 1 s timeout.
      ``disconnect`` leaves ``isConnected`` True, which pins the dominant ordering:
      the handshake loop reaches ``self.conn`` after ``reset()`` made it None.
    * ``close``: the first ``recvMsg`` sees the close (``isConnected`` False, empty buffer).
    """

    mode = "refuse"
    entered = threading.Event()

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.open = False

    def connect(self) -> None:
        self.open = self.mode != "refuse"

    def sendMsg(self, msg: bytes) -> int:
        if self.mode == "refuse":
            raise OSError(10057, "socket is not connected")
        return len(msg)

    def recvMsg(self) -> bytes:
        if self.mode == "close":
            self.open = False
        else:
            self.entered.set()
            threading.Event().wait(0.002)  # stands in for the socket timeout
        return b""

    def isConnected(self) -> bool:
        return self.open

    def disconnect(self) -> None:
        if self.mode != "hang":
            self.open = False


@pytest.fixture
def stub_connection(monkeypatch: pytest.MonkeyPatch) -> type[_StubConnection]:
    class Stub(_StubConnection):
        entered = threading.Event()

    monkeypatch.setattr("ibapi.client.Connection", Stub)
    return Stub


def _outcome(rec: Recorder, client: Any) -> dict[str, Any]:
    """What a connect left behind, comparable between the real client and the fake."""
    me = threading.get_ident()
    events = []
    while not rec.events.empty():
        name, args, tid = rec.get()
        extra = (args[2], len(args)) if name == "error" else ()
        events.append((name, extra, tid == me))
    return {"events": events, "connected": client.isConnected(), "state": client.connState}


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
    t = threading.Thread(target=real.run, daemon=True)  # reported on the run thread
    t.start()
    t.join(WAIT)
    assert not t.is_alive()
    assert _closed(real_rec) == [t.ident]
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
    assert _run_catching(real.run) is None  # the finally disconnect() finds no connection object
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


# -- the fake against the real EClient.connect, driven through a stand-in Connection ----------
#
# ``EClient.connect`` runs for real; only ``ibapi.client.Connection`` is replaced by
# _StubConnection (no socket, no DNS lookup). Each test makes the real client do the
# scenario, makes the fake do it, and compares what the wrapper saw and the state left.


def test_refused_connect_matches_real_ibapi(stub_connection: type[_StubConnection]) -> None:
    stub_connection.mode = "refuse"
    real_rec = _RawRecorder()
    real = EClient(real_rec)
    real.connect("127.0.0.1", 7497, 21)
    expected = _outcome(real_rec, real)
    assert [e[:2] for e in expected["events"]] == [("error", (502, 4)), ("connectionClosed", ())]
    assert all(e[2] for e in expected["events"]), "both on the calling thread"
    assert not expected["connected"]
    real.disconnect()
    assert real_rec.events.empty()

    fake_rec = _RawRecorder()
    fake = FakeIbClient(fake_rec, connect_behavior=ConnectBehavior.REFUSE)
    connect(fake)
    assert _outcome(fake_rec, fake) == expected
    fake.disconnect()
    assert fake_rec.events.empty()


def test_hung_connect_released_by_disconnect_matches_real_ibapi(
    stub_connection: type[_StubConnection],
) -> None:
    stub_connection.mode = "hang"
    real_rec = _RawRecorder()
    real = EClient(real_rec)
    real_errors: list[BaseException] = []
    t = threading.Thread(
        target=_catching(lambda: real.connect("127.0.0.1", 7497, 21), real_errors), daemon=True
    )
    t.start()
    assert stub_connection.entered.wait(WAIT)
    assert not real.isConnected()  # state CONNECTING
    real.disconnect()
    t.join(WAIT)
    assert not t.is_alive()
    assert [type(e) for e in real_errors] == [AttributeError]
    expected = _outcome(real_rec, real)
    assert expected["events"] == [("connectionClosed", (), True)]

    fake_rec = _RawRecorder()
    fake = FakeIbClient(fake_rec, connect_behavior=ConnectBehavior.HANG)
    fake_errors: list[BaseException] = []
    t = threading.Thread(target=_catching(lambda: connect(fake), fake_errors), daemon=True)
    t.start()
    assert fake.wait_for_hang(1, WAIT)
    assert not fake.isConnected()
    fake.disconnect()
    t.join(WAIT)
    assert not t.is_alive()
    assert [type(e) for e in fake_errors] == [AttributeError]
    assert _outcome(fake_rec, fake) == expected


def test_socket_closed_during_handshake_matches_real_ibapi(
    stub_connection: type[_StubConnection],
) -> None:
    stub_connection.mode = "close"
    real_rec = _RawRecorder()
    real = EClient(real_rec)
    real.connect("127.0.0.1", 7497, 21)
    expected = _outcome(real_rec, real)
    assert expected == {"events": [], "connected": False, "state": EClient.DISCONNECTED}
    assert real.serverVersion() is None and real.conn is None
    real.disconnect()
    assert real_rec.events.empty(), "nothing is left to close"

    fake_rec = _RawRecorder()
    fake = FakeIbClient(fake_rec, connect_behavior=ConnectBehavior.CLOSED_IN_HANDSHAKE)
    connect(fake)
    assert _outcome(fake_rec, fake) == expected
    assert fake.serverVersion() is None
    fake.disconnect()
    assert fake_rec.events.empty()


def test_run_after_caller_disconnect_matches_real_ibapi() -> None:
    real_rec = _RawRecorder()
    real = _real_connected_client(real_rec)
    real.msg_queue.put(b"\x00\x00\x00\x31" + b"1\x001700000000\x00")  # a queued currentTime
    real.disconnect()
    error = _run_catching(real.run)
    assert isinstance(error, TypeError), error
    expected = _outcome(real_rec, real)
    assert [e[0] for e in expected["events"]] == ["connectionClosed"]

    fake_rec = _RawRecorder()
    fake = FakeIbClient(fake_rec)
    connect(fake)
    fake.disconnect()  # the connect's handshake messages are still queued
    assert isinstance(_run_catching(fake.run), TypeError)
    got = _outcome(fake_rec, fake)
    assert [e[0] for e in got["events"]] == ["connectAck", "connectionClosed"]
    assert got["connected"] == expected["connected"] and got["state"] == expected["state"]


def test_run_after_peer_close_drains_the_queue_on_the_real_client_too() -> None:
    real_rec = _RawRecorder()
    real = _real_connected_client(real_rec, server_version=222)
    real.msg_queue.put(b"\x00\x00\x00\x31" + b"1\x001700000000\x00")
    real.conn.socket = None  # what the reader does when TWS closes
    assert _run_catching(real.run) is None
    assert [e[0] for e in _outcome(real_rec, real)["events"]] == ["currentTime", "connectionClosed"]


def test_second_connect_on_real_ibapi_builds_a_second_connection(
    stub_connection: type[_StubConnection],
) -> None:
    # Why the fake raises instead of reporting 501: the real client has no such check.
    stub_connection.mode = "hang"
    real = EClient(Recorder())
    first = _StubConnection("h", 1)
    real.conn = first  # a connection object exists, as after a first connect()
    errors: list[BaseException] = []
    t = threading.Thread(
        target=_catching(lambda: real.connect("127.0.0.1", 7497, 21), errors), daemon=True
    )
    t.start()
    assert stub_connection.entered.wait(WAIT)
    assert real.conn is not first, "connect() built a new Connection over the old one"
    real.disconnect()
    t.join(WAIT)
    assert not t.is_alive()
