"""`IbConnection` (MainStoinker/ib/connection.py), against the scripted fake client.

Nothing here opens a socket. The client factory builds a `FakeIbClient` whose
wrapper is a real `DataOnlyClient` (never connected), so the callbacks turn into
the same events as in production. The default factory is used only in the test
that shows the pytest connection block propagating.

Time is injected: `Clock` advances only when the code sleeps (retry back-off,
rate limit) or, in the timeout tests, when a trigger that watches the fake fires.
Waits for something to happen still block for a few real milliseconds at a time
(`poll_interval`), never for a timeout.
"""

import ast
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from ibapi.client import EClient
from ibapi.errors import CONNECT_FAIL

from MainStoinker.ib import events
from MainStoinker.ib.client import DataOnlyClient, MessageRefused
from MainStoinker.ib.connection import (
    DATA_FARM_CODES,
    DATA_REQUESTS,
    ClientIdInUse,
    ClientIdRefused,
    ConnectFailed,
    ConnectionLost,
    ConnectionRestored,
    ConnState,
    IbConnection,
    NotConnected,
    ReaderStopped,
)
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.conftest import BrokerConnectionBlockedUnderPytest
from tests.fake_ib import (
    REQUEST_ARGS,
    ConnectBehavior,
    FakeIbClient,
    HangNeverReleased,
)

WAIT = 5.0
CONNECTION_PATH = Path(__file__).resolve().parent.parent / "MainStoinker" / "ib" / "connection.py"


class Clock:
    """A clock that moves when the code sleeps, or once when `trigger` has been true twice."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.trigger: Callable[[], bool] | None = None
        self.jump = 0.0
        self._armed = False
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            if self.trigger is not None and self.trigger():
                if self._armed:
                    self.now += self.jump
                    self.trigger = None
                    self._armed = False
                else:
                    self._armed = True
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


class HookFake(FakeIbClient):
    """The fake, with hooks for run-thread timing (the base class is not edited)."""

    def __init__(self, *args: Any, gated_run: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_gate = threading.Event()
        if not gated_run:
            self.run_gate.set()
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate_on: str | None = None
        self.boom_on: str | None = None

    def run(self) -> None:
        self.run_gate.wait(WAIT)
        return super().run()

    def _deliver(self, name: str, args: tuple[Any, ...]) -> None:
        if name == self.boom_on:
            raise ValueError("reader boom")
        if name == self.gate_on:
            self.entered.set()
            self.gate.wait(WAIT)
        super()._deliver(name, args)

    def disconnect(self) -> None:
        super().disconnect()
        self.gate.set()


class QuietHangFake(HookFake):
    """A hung connect released by disconnect() returns cleanly, as real ibapi may."""

    def connect(self, host: Any, port: Any, clientId: Any) -> None:
        try:
            return super().connect(host, port, clientId)
        except AttributeError:
            return None


class StubClient:
    """No fake behaviour at all: connect raises what it is told to. Records disconnects."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.connects = 0
        self.disconnects = 0
        self.socket_open = False

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.connects += 1
        self.socket_open = True  # as after a handshake the frame guard refused
        if self.error is not None:
            raise self.error

    def disconnect(self) -> None:
        self.disconnects += 1
        self.socket_open = False

    def isConnected(self) -> bool:  # noqa: N802 (ibapi's name)
        return False

    def run(self) -> None:
        return None


@dataclass
class Rig:
    conn: IbConnection
    fakes: list[Any] = field(default_factory=list)
    clock: Clock = field(default_factory=Clock)

    @property
    def fake(self) -> Any:
        return self.fakes[0]


@pytest.fixture
def build(request: pytest.FixtureRequest) -> Callable[..., Rig]:
    def make(
        behavior: Any = ConnectBehavior.ACCEPT,
        *,
        fake_cls: type[FakeIbClient] = HookFake,
        fake_kw: dict[str, Any] | None = None,
        port: int = 7497,
        client_id: int = 21,
        env: dict[str, str] | None = None,
        **conn_kw: Any,
    ) -> Rig:
        clock = Clock()
        fakes: list[Any] = []

        def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
            wrapper = DataOnlyClient(env, on_event, on_raw, logger)
            fake = fake_cls(wrapper, connect_behavior=behavior, **(fake_kw or {}))
            fakes.append(fake)
            return fake

        conn = IbConnection(
            "127.0.0.1", port, client_id, env=env if env is not None else {},
            client_factory=factory, clock=clock, sleep=clock.sleep, poll_interval=0.002,
            **conn_kw,
        )
        request.addfinalizer(conn.close)
        return Rig(conn, fakes, clock)

    return make


def stub_rig(
    request: pytest.FixtureRequest, error: BaseException | None, **conn_kw: Any
) -> tuple[IbConnection, StubClient, Clock]:
    clock = Clock()
    stub = StubClient(error)
    conn = IbConnection(
        "127.0.0.1", 7497, 21, env={}, client_factory=lambda **kw: stub,
        clock=clock, sleep=clock.sleep, poll_interval=0.002, **conn_kw,
    )
    request.addfinalizer(conn.close)
    return conn, stub, clock


def drain(conn: IbConnection) -> list[Any]:
    out = []
    while (ev := conn.next_event(0)) is not None:
        out.append(ev)
    return out


def wait_for_event(conn: IbConnection, kind: type) -> Any:
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        ev = conn.next_event(0)
        if isinstance(ev, kind):
            return ev
        if ev is None:
            time.sleep(0.002)
    raise AssertionError(f"no {kind.__name__} within {WAIT}s")


def wait_until(predicate: Callable[[], bool]) -> None:
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached")


def names(fake: Any, *wanted: str) -> list[str]:
    return [n for n in fake.call_names if n in wanted]


def assert_no_events_of_attempt_one(conn: IbConnection) -> None:
    """The disconnect() that ended attempt 1 reported connectionClosed; the consumer must
    not see it (nor read it as the close of attempt 2)."""
    seen = drain(conn)
    assert any(isinstance(e, events.NextValidId) for e in seen)
    assert not any(isinstance(e, events.ConnectionClosed) for e in seen), seen


def assert_disconnect_between_connects(fake: Any) -> None:
    """Every connect after the first is preceded by a disconnect of the previous one."""
    seq = names(fake, "connect", "disconnect")
    assert seq.count("connect") >= 2
    for i, name in enumerate(seq):
        if name == "connect" and i > 0:
            assert seq[i - 1] == "disconnect", seq


# -- open: guards before anything is built ----------------------------------------


def test_port_refused_before_any_client_is_built(build: Callable[..., Rig]) -> None:
    rig = build(port=7496)
    with pytest.raises(BrokerPortRefused):
        rig.conn.open()
    assert rig.fakes == []
    assert rig.clock.sleeps == []
    assert rig.conn.attempts_made == 0


@pytest.mark.parametrize("client_id", [0, 123, -1, True, "21"])
def test_client_id_zero_refused(build: Callable[..., Rig], client_id: Any) -> None:
    rig = build(client_id=client_id)
    with pytest.raises(ClientIdRefused):
        rig.conn.open()
    assert rig.fakes == []


def test_env_mapping_is_what_the_port_guard_reads(build: Callable[..., Rig]) -> None:
    # The mapping is passed in; the layer never reads os.environ.
    rig = build(port=7496, env={"STOINK_ALLOW_LIVE_PORT": "1"})
    rig.conn.open()
    assert rig.conn.state is ConnState.READY


def test_open_twice_is_refused(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    with pytest.raises(RuntimeError):
        rig.conn.open()


# -- readiness ------------------------------------------------------------------------


def test_ready_only_after_next_valid_id(build: Callable[..., Rig]) -> None:
    rig = build(fake_kw={"gated_run": True, "next_valid_id": 7})
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            rig.conn.open()
            outcome.append(None)
        except BaseException as exc:  # reported to the test thread
            outcome.append(exc)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    wait_until(lambda: bool(rig.fakes) and rig.fake.connState == EClient.CONNECTED)
    t.join(0.1)  # connect() has returned; nextValidId has not come
    assert t.is_alive()
    assert rig.conn.state is ConnState.CONNECTING
    assert rig.conn.next_valid_id is None
    rig.fake.run_gate.set()
    t.join(WAIT)
    assert not t.is_alive()
    assert outcome == [None]
    assert rig.conn.state is ConnState.READY
    assert rig.conn.next_valid_id == 7
    assert any(isinstance(e, events.NextValidId) for e in drain(rig.conn))


# -- failure reporting and bounded attempts --------------------------------------------


def test_failed_connect_reports_ib_code_and_message(
    build: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = build(ConnectBehavior.REFUSE, attempts=1)
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert "502" in str(info.value)
    assert CONNECT_FAIL.msg() in str(info.value)
    assert [(f.attempt, f.code, f.message) for f in info.value.failures] == [
        (1, 502, CONNECT_FAIL.msg())
    ]
    assert any("502" in r.getMessage() and CONNECT_FAIL.msg() in r.getMessage() for r in caplog.records)
    assert rig.conn.state is ConnState.CLOSED


def test_bounded_attempts_then_gives_up(build: Callable[..., Rig]) -> None:
    rig = build(ConnectBehavior.REFUSE)
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert rig.fake.connects == 3
    assert rig.clock.sleeps == [5, 10]  # two retry sleeps, backing off
    assert len(info.value.failures) == 3
    # disconnect() before every retry, and after the last attempt
    assert names(rig.fake, "connect", "disconnect") == ["connect", "disconnect"] * 3


def test_late_close_of_a_refused_attempt_is_not_read_as_the_next_one(build: Callable[..., Rig]) -> None:
    rig = build([ConnectBehavior.REFUSE, ConnectBehavior.ACCEPT])
    rig.conn.open()
    assert rig.fake.connects == 2
    seen = drain(rig.conn)
    assert not any(isinstance(e, events.ConnectionClosed) for e in seen)
    assert not any(isinstance(e, events.IbError) and e.code == 502 for e in seen)
    assert rig.conn.state is ConnState.READY


@pytest.mark.parametrize("fake_cls", [HookFake, QuietHangFake])
def test_hung_handshake_abandoned_after_timeout(
    build: Callable[..., Rig], caplog: pytest.LogCaptureFixture, fake_cls: type[FakeIbClient]
) -> None:
    rig = build([ConnectBehavior.HANG, ConnectBehavior.ACCEPT], fake_cls=fake_cls)
    rig.clock.trigger = lambda: bool(rig.fakes) and rig.fake.hangs_entered >= 1
    rig.clock.jump = 100.0
    with caplog.at_level(logging.WARNING):
        rig.conn.open()
    assert rig.fake.connects == 2
    assert rig.conn.attempts_made == 2
    assert_disconnect_between_connects(rig.fake)
    assert any("abandoned" in r.getMessage() for r in caplog.records)
    assert_no_events_of_attempt_one(rig.conn)
    assert rig.conn.state is ConnState.READY


def test_socket_closed_in_handshake_is_detected_without_a_callback(build: Callable[..., Rig]) -> None:
    rig = build([ConnectBehavior.CLOSED_IN_HANDSHAKE, ConnectBehavior.ACCEPT])
    rig.conn.open()
    assert rig.fake.connects == 2
    assert rig.clock.sleeps == [5]  # one retry delay, and no timeout was needed to notice
    assert rig.clock.now == 5


def test_missing_next_valid_id_counts_as_failed_attempt(
    build: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = build([ConnectBehavior.NO_NEXT_VALID_ID, ConnectBehavior.ACCEPT])
    # connectAck, managedAccounts and the three farm messages have been delivered:
    # nothing more will come, so the ready timeout runs out.
    rig.clock.trigger = lambda: bool(rig.fakes) and len(rig.fake.delivered) >= 5
    rig.clock.jump = 100.0
    with caplog.at_level(logging.WARNING):
        rig.conn.open()
    assert rig.fake.connects == 2
    assert any("no nextValidId" in r.getMessage() for r in caplog.records)
    assert_no_events_of_attempt_one(rig.conn)
    # the connected client is disconnected before the second connect
    assert_disconnect_between_connects(rig.fake)


def test_client_id_in_use_is_fatal_without_retry(build: Callable[..., Rig]) -> None:
    rig = build(ConnectBehavior.CLIENT_ID_IN_USE)
    with pytest.raises(ClientIdInUse) as info:
        rig.conn.open()
    assert rig.fake.connects == 1
    assert rig.clock.sleeps == []
    assert info.value.failures[0].code == 326
    assert "client id 21" in str(info.value)
    assert "326" in str(info.value)
    assert rig.conn.state is ConnState.CLOSED
    assert "disconnect" in rig.fake.call_names


def test_pytest_block_propagates_through_ib_connection(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    conn = IbConnection("127.0.0.1", 7497, 21, env={}, clock=clock, sleep=clock.sleep, poll_interval=0.002)
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        conn.open()
    assert conn.attempts_made == 1
    assert clock.sleeps == []
    conn.close()


@pytest.mark.parametrize(
    "error",
    [
        MessageRefused("frame refused"),
        BrokerPortRefused("port refused"),
        AssertionError("a test failure is not a connect failure"),
        HangNeverReleased("never released"),
        AttributeError("not a released hang: this attempt was never abandoned"),
        ValueError("anything else"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_non_socket_errors_propagate_without_retry(
    request: pytest.FixtureRequest, error: BaseException
) -> None:
    conn, stub, clock = stub_rig(request, error)
    with pytest.raises(type(error)) as info:
        conn.open()
    assert info.value is error
    assert stub.connects == 1
    assert clock.sleeps == []
    # the socket may be open after a refused handshake: disconnect() was called
    assert stub.disconnects >= 1
    assert not stub.socket_open
    conn.close()
    conn.close()


def test_socket_errors_are_retried(request: pytest.FixtureRequest) -> None:
    conn, stub, clock = stub_rig(request, ConnectionRefusedError("no TWS"), attempts=2)
    with pytest.raises(ConnectFailed) as info:
        conn.open()
    assert stub.connects == 2
    assert clock.sleeps == [5]
    assert "ConnectionRefusedError" in str(info.value)


# -- threads, events -----------------------------------------------------------------------


def test_callbacks_on_reader_thread_consumed_on_caller_thread(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    # open() returns at nextValidId; managedAccounts and the farm messages are still on
    # their way from the reader thread. Consume until the last of them has arrived.
    got: list[Any] = []

    def consumed_all() -> bool:
        while (ev := rig.conn.next_event(0)) is not None:  # consumed here, on the caller thread
            got.append(ev)
        codes = {e.code for e in got if isinstance(e, events.IbError)}
        return any(isinstance(e, events.ManagedAccounts) for e in got) and {2104, 2106, 2158} <= codes

    wait_until(consumed_all)
    me = threading.get_ident()
    by_name = {d.name: d.thread_id for d in rig.fake.delivered}
    assert by_name["nextValidId"] != me
    assert by_name["managedAccounts"] == by_name["nextValidId"]  # one reader thread
    assert events.NextValidId(1) in got
    assert any(isinstance(e, events.ManagedAccounts) for e in got)
    assert {e.code for e in got if isinstance(e, events.IbError)} >= {2104, 2106, 2158}


def test_reader_exception_becomes_event(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    rig.fake.boom_on = "currentTime"
    rig.fake.push("currentTime", 1)
    ev = wait_for_event(rig.conn, ReaderStopped)
    assert isinstance(ev.error, ValueError)
    assert rig.conn.reader_errors == [ev.error]


def test_close_after_caller_disconnect_with_queued_messages_makes_an_event_not_a_crash(
    build: Callable[..., Rig],
) -> None:
    rig = build()
    rig.conn.open()
    drain(rig.conn)
    rig.fake.gate_on = "currentTime"
    rig.fake.push("currentTime", 1)
    rig.fake.push("currentTime", 2)
    assert rig.fake.entered.wait(WAIT)  # the reader holds the first, the second is queued
    rig.conn.close()  # disconnect() resets the client; the reader then dequeues the second
    assert [type(e) for e in rig.conn.reader_errors] == [TypeError]
    assert not [t for t in threading.enumerate() if t.name.startswith("ib-reader")]


def test_close_disconnects_and_joins_reader(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    assert [t for t in threading.enumerate() if t.name.startswith("ib-reader")]
    rig.conn.close()
    assert "disconnect" in rig.fake.call_names
    assert not [t for t in threading.enumerate() if t.name.startswith("ib-reader")]
    assert not rig.fake.isConnected()
    assert rig.conn.state is ConnState.CLOSED


def test_close_is_idempotent(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    rig.conn.close()
    before = list(rig.fake.call_names)
    rig.conn.close()
    assert rig.fake.call_names == before
    never_opened = build()
    never_opened.conn.close()
    never_opened.conn.close()
    assert never_opened.fakes == []


def test_close_after_failed_open_is_safe(build: Callable[..., Rig]) -> None:
    rig = build(ConnectBehavior.REFUSE)
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    rig.conn.close()
    rig.conn.close()
    assert rig.conn.state is ConnState.CLOSED


def test_context_manager_opens_and_closes(build: Callable[..., Rig]) -> None:
    rig = build()
    with rig.conn as conn:
        assert conn.state is ConnState.READY
    assert rig.conn.state is ConnState.CLOSED
    assert not [t for t in threading.enumerate() if t.name.startswith("ib-reader")]


# -- data farm, connection state -------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(DATA_FARM_CODES))
def test_data_farm_wait_codes_and_timeout(build: Callable[..., Rig], code: int) -> None:
    rig = build(fake_kw={"farm_messages": ()})
    rig.conn.open()
    rig.fake.push("error", -1, 1, code, "farm", "")
    assert rig.conn.wait_for_data_farm(1.0) is True
    assert any(isinstance(e, events.IbError) and e.code == code for e in drain(rig.conn))  # not consumed


def test_data_farm_wait_times_out_with_a_warning(
    build: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = build(fake_kw={"farm_messages": ()})
    rig.conn.open()
    rig.fake.push("error", -1, 1, 2110, "not a farm message", "")  # 2110 is not accepted
    rig.clock.trigger = lambda: True
    rig.clock.jump = 100.0
    with caplog.at_level(logging.WARNING):
        assert rig.conn.wait_for_data_farm(10.0) is False
    assert any("data-farm" in r.getMessage() for r in caplog.records)


def test_connection_lost_1100_event_and_state(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    rig.fake.drop(1100)
    ev = wait_for_event(rig.conn, ConnectionLost)
    assert ev.code == 1100
    assert rig.conn.state is ConnState.LOST
    # no reconnect: the socket is still up and nothing connected again
    assert rig.fake.isConnected()
    assert rig.fake.connects == 1
    assert "disconnect" not in rig.fake.call_names


def test_restored_1102_keeps_subscriptions_and_1101_asks_for_them_again(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    rig.conn.request("reqMktData", *REQUEST_ARGS["reqMktData"])
    rig.fake.drop(1100)
    wait_for_event(rig.conn, ConnectionLost)
    rig.fake.drop(1102)
    kept = wait_for_event(rig.conn, ConnectionRestored)
    assert kept.data_lost is False and not kept.resubscribe_required
    assert rig.conn.state is ConnState.READY
    rig.fake.drop(1100)
    wait_for_event(rig.conn, ConnectionLost)
    rig.fake.drop(1101)
    lost = wait_for_event(rig.conn, ConnectionRestored)
    assert lost.data_lost is True and lost.resubscribe_required
    assert rig.conn.state is ConnState.READY
    # the connection does not resubscribe by itself
    assert len(rig.fake.calls_named("reqMktData")) == 1
    assert rig.fake.connects == 1


def test_peer_close_is_an_event_and_closes_the_state(build: Callable[..., Rig]) -> None:
    rig = build()
    rig.conn.open()
    drain(rig.conn)
    rig.fake.drop("connectionClosed")
    wait_for_event(rig.conn, events.ConnectionClosed)
    assert rig.conn.state is ConnState.CLOSED


# -- requests -------------------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.now: Callable[[], float] = lambda: 0.0
        self.requests: list[tuple[str, tuple[Any, ...], float]] = []
        self.raw: list[str] = []
        self._lock = threading.Lock()

    def on_request(self, name: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            self.requests.append((name, args, self.now()))

    def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            self.raw.append(name)


def test_rate_limit_spaces_requests(build: Callable[..., Rig]) -> None:
    rec = Recorder()
    rig = build(observers=(rec,))
    rec.now = lambda: rig.clock.now
    rig.conn.open()
    for _ in range(45):
        rig.conn.request("reqCurrentTime")
    times = [t for _, _, t in rec.requests]
    assert times == [0.0] * 20 + [1.0] * 20 + [2.0] * 5
    assert rig.clock.sleeps == [1.0, 1.0]
    assert len(rig.fake.calls_named("reqCurrentTime")) == 45


def test_request_refuses_names_outside_allowlist(build: Callable[..., Rig]) -> None:
    rec = Recorder()
    rig = build(observers=(rec,))
    rig.conn.open()
    for name in ("placeOrder", "reqIds", "reqExecutions", "cancelOrder", "connect"):
        with pytest.raises(MessageRefused):
            rig.conn.request(name)
    assert rec.requests == []
    assert not {"placeOrder", "reqIds", "reqExecutions", "cancelOrder"} & set(rig.fake.call_names)


def test_request_allowlist_is_exactly_the_data_requests(build: Callable[..., Rig]) -> None:
    assert DATA_REQUESTS == set(REQUEST_ARGS)
    rig = build()
    rig.conn.open()
    for name, args in REQUEST_ARGS.items():
        rig.conn.request(name, *args)
    assert set(rig.fake.call_names) >= set(REQUEST_ARGS)


def test_request_needs_an_open_connection(build: Callable[..., Rig]) -> None:
    rig = build()
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")
    rig.conn.open()
    rig.conn.close()
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")


def test_next_request_id_counts_up(build: Callable[..., Rig]) -> None:
    rig = build()
    assert [rig.conn.next_request_id() for _ in range(3)] == [1, 2, 3]


def test_observers_see_requests_and_raw_callbacks(build: Callable[..., Rig]) -> None:
    rec = Recorder()
    rig = build(observers=(rec,))
    rig.conn.open()
    rig.conn.request("reqMarketDataType", 3)
    rig.fake.drop(1100)
    wait_for_event(rig.conn, ConnectionLost)
    assert [(n, a) for n, a, _ in rec.requests] == [("reqMarketDataType", (3,))]
    assert {"connectAck", "nextValidId", "managedAccounts", "error"} <= set(rec.raw)


def test_observers_given_to_the_constructor_and_a_failing_one_is_counted(build: Callable[..., Rig]) -> None:
    class Boom:
        def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
            raise ValueError("observer boom")

    rec = Recorder()
    rig = build(observers=(Boom(), rec))
    rig.conn.open()
    rig.conn.request("reqCurrentTime")
    assert rec.raw and rec.requests
    assert rig.conn.observer_failures
    assert rig.conn.state is ConnState.READY


# -- static properties of the module -------------------------------------------------------------------


def test_module_reads_no_environment_and_builds_no_raw_client() -> None:
    tree = ast.parse(CONNECTION_PATH.read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "os" not in imported
    assert "ibapi" not in imported
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names_used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"environ", "_inner", "socket", "_conn", "conn"} & attrs
    assert not {"EClient", "Connection"} & names_used
