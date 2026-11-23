"""Extra tests for `IbConnection`: scripted connect sequences and lifecycle probes.

Written by the tester of task T4 (issue 117). Same rules as `test_ib_connection.py`:
no socket, no real sleeps beyond a few milliseconds, the clock and sleep are injected,
and every thread a test starts ends before the test returns.
"""

import ast
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.ib import events
from MainStoinker.ib.client import ALLOWED_MESSAGE_IDS, DataOnlyClient, MessageRefused
from MainStoinker.ib.connection import (
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
from tests.fake_ib import REQUEST_ARGS, ConnectBehavior, FakeIbClient

WAIT = 5.0
B = ConnectBehavior


class SeqClock:
    """Moves when the code sleeps, and by `jump` each time the next trigger has been true twice.

    `triggers` is a list of predicates, used one after the other: the first one is
    watched until it has been true on two consecutive reads, then the clock jumps and
    the next one is watched. Nothing here depends on real time.
    """

    def __init__(self, jump: float = 100.0) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.triggers: list[Callable[[], bool]] = []
        self.jump = jump
        self.jumps = 0
        self.auto = 0.0  # added on every read: lets a test flush a retry loop it expects to be over
        self._armed = False
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += self.auto
            if self.triggers and self.triggers[0]():
                if self._armed:
                    self.now += self.jump
                    self.jumps += 1
                    self.triggers.pop(0)
                    self._armed = False
                else:
                    self._armed = True
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


class ProbeFake(FakeIbClient):
    """The fake plus a gate for `run` and a hook for the first moments of a connect."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_connect: Callable[[Any, int], None] | None = None
        self.run_slow = threading.Event()  # set: `run` ignores a disconnect until released
        self.run_slow.set()
        self.run_started = threading.Event()
        self.gate_on: str | None = None  # a callback of this name blocks until `gate` is set
        self.gate = threading.Event()
        self.entered = threading.Event()

        self.boom_on: str | None = None  # a callback of this name raises on the run thread

    def _deliver(self, name: str, args: tuple[Any, ...]) -> None:
        if name == self.boom_on:
            raise ValueError("reader boom")
        if name == self.gate_on:
            self.entered.set()
            self.gate.wait(WAIT)
        super()._deliver(name, args)

    def connect(self, host: Any, port: Any, clientId: Any) -> None:
        try:
            return super().connect(host, port, clientId)
        finally:
            if self.on_connect is not None:
                self.on_connect(self, self.connects)

    def run(self) -> None:
        self.run_started.set()
        try:
            return super().run()
        finally:
            self.run_slow.wait(WAIT)


class QuietProbeFake(ProbeFake):
    """A hung connect released by disconnect() returns cleanly, as real ibapi may."""

    def connect(self, host: Any, port: Any, clientId: Any) -> None:
        try:
            return super().connect(host, port, clientId)
        except AttributeError:
            return None


class StuckConnectFake(ProbeFake):
    """`connect` blocks and ignores disconnect() until the test releases it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stuck_entered = threading.Event()
        self.stuck_release = threading.Event()

    def connect(self, host: Any, port: Any, clientId: Any) -> None:
        self.stuck_entered.set()
        self.stuck_release.wait(WAIT)
        return super().connect(host, port, clientId)


@dataclass
class Rig:
    conn: IbConnection
    clock: SeqClock
    fakes: list[Any] = field(default_factory=list)

    @property
    def fake(self) -> Any:
        return self.fakes[0]

    def raw_sequence(self) -> list[str]:
        return [n for n in self.fake.call_names if n in ("connect", "disconnect")]

    def sequence(self) -> list[str]:
        """connect/disconnect calls, a run of disconnects counted once (a hung attempt is
        disconnected twice: by the timeout and again when the attempt ends; harmless)."""
        out: list[str] = []
        for name in self.raw_sequence():
            if not (name == "disconnect" and out and out[-1] == "disconnect"):
                out.append(name)
        return out


def live_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.is_alive()}


@pytest.fixture
def rig_factory(request: pytest.FixtureRequest) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []
    before = live_threads()

    def make(
        behavior: Any = B.ACCEPT,
        *,
        fake_cls: type[FakeIbClient] = ProbeFake,
        fake_kw: dict[str, Any] | None = None,
        port: int = 7497,
        client_id: int = 21,
        env: Any = None,
        on_connect: Callable[[Any, int], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: SeqClock | None = None,
        **conn_kw: Any,
    ) -> Rig:
        clock = clock if clock is not None else SeqClock()
        fakes: list[Any] = []

        def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
            wrapper = DataOnlyClient(env, on_event, on_raw, logger)
            fake = fake_cls(wrapper, connect_behavior=behavior, **(fake_kw or {}))
            fake.on_connect = on_connect
            fakes.append(fake)
            return fake

        conn = IbConnection(
            "127.0.0.1", port, client_id, env={} if env is None else env,
            client_factory=factory, clock=clock, sleep=sleep or clock.sleep, poll_interval=0.002,
            **conn_kw,
        )
        rig = Rig(conn, clock, fakes)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.clock.auto = 50.0  # let any retry loop that is still running run out its attempts
        rig.conn.close()
        for fake in rig.fakes:
            fake.run_slow.set()
            fake.gate.set()
            if hasattr(fake, "stuck_release"):
                fake.stuck_release.set()
            fake.disconnect()  # a defect that leaves a socket behind must not leak into other tests
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        leaked = {n for n in live_threads() - before if n.startswith(("ib-", "probe-", "fake-"))}
        if not leaked:
            return
        time.sleep(0.002)
    raise AssertionError(f"threads left alive: {sorted(leaked)}")


def drain(conn: IbConnection) -> list[Any]:
    out = []
    while (ev := conn.next_event(0)) is not None:
        out.append(ev)
    return out


def wait_until(predicate: Callable[[], bool]) -> None:
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached")


def wait_for_event(conn: IbConnection, kind: type) -> Any:
    end = time.monotonic() + WAIT
    seen: list[Any] = []
    while time.monotonic() < end:
        ev = conn.next_event(0)
        if isinstance(ev, kind):
            return ev
        if ev is None:
            time.sleep(0.002)
        else:
            seen.append(ev)
    raise AssertionError(f"no {kind.__name__} within {WAIT}s; saw {seen}")


def settle(rig: Rig) -> None:
    """After open(): wait for the last thing the fake plays at connect, then drop what is queued.

    open() returns at nextValidId; managedAccounts (and any farm messages) are still on their
    way from the reader thread. The rig must have been built with no farm messages.
    """
    wait_for_event(rig.conn, events.ManagedAccounts)
    drain(rig.conn)


NO_FARM = {"farm_messages": ()}


def run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def target() -> None:
        try:
            out.append(("ok", fn()))
        except BaseException as exc:  # handed to the test thread
            out.append(("exc", exc))

    t = threading.Thread(target=target, name="probe-caller", daemon=True)
    t.start()
    return t, out


def no_connect_after_disconnect_pairs(seq: list[str]) -> None:
    """Never two connects in a row (every connect after the first follows a disconnect)."""
    for i, name in enumerate(seq):
        if name == "connect" and i > 0:
            assert seq[i - 1] == "disconnect", seq


# -- refuse, hang and handshake sequences ------------------------------------------------------


@pytest.mark.parametrize("refusals", [1, 2])
def test_refuse_then_accept_connects_once_per_attempt(
    rig_factory: Callable[..., Rig], refusals: int
) -> None:
    rig = rig_factory([B.REFUSE] * refusals + [B.ACCEPT], attempts=5)
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert rig.sequence() == ["connect", "disconnect"] * refusals + ["connect"]
    assert rig.clock.sleeps == [5, 10][:refusals]
    seen = drain(rig.conn)
    assert not [e for e in seen if isinstance(e, events.ConnectionClosed)]
    assert [e for e in seen if isinstance(e, events.NextValidId)]


def test_retry_forever_is_not_possible_by_default_and_max_wait_is_bounded(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(B.REFUSE)
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    assert rig.fake.connects == 3
    assert sum(rig.clock.sleeps) == 15


def test_backoff_is_capped_by_max_retry_delay(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.REFUSE, attempts=8, retry_delay=5, backoff=2.0, max_retry_delay=60)
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    assert rig.clock.sleeps == [5, 10, 20, 40, 60, 60, 60]


def test_hang_hang_accept(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.HANG, B.HANG, B.ACCEPT], attempts=3)
    rig.clock.triggers = [
        lambda: bool(rig.fakes) and rig.fake.hangs_entered >= 1,
        lambda: bool(rig.fakes) and rig.fake.hangs_entered >= 2,
    ]
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert rig.sequence() == ["connect", "disconnect", "connect", "disconnect", "connect"]
    assert rig.conn.attempts_made == 3
    assert rig.clock.sleeps == [5, 10]
    seen = drain(rig.conn)
    assert not [e for e in seen if isinstance(e, events.ConnectionClosed)]


def test_hang_forever_gives_up_after_the_attempts(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.HANG, attempts=2)
    rig.clock.triggers = [
        lambda: rig.fake.hangs_entered >= 1 if rig.fakes else False,
        lambda: rig.fake.hangs_entered >= 2 if rig.fakes else False,
    ]
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect", "disconnect"]
    assert len(info.value.failures) == 2
    assert all("abandoned" in f.message for f in info.value.failures)
    assert rig.conn.state is ConnState.CLOSED


def test_accept_without_next_valid_id_then_accept_disconnects_between(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT])
    rig.clock.triggers = [lambda: bool(rig.fakes) and len(rig.fake.delivered) >= 5]
    rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect"]
    assert rig.conn.next_valid_id == 1
    assert rig.clock.sleeps == [5]


def test_closed_in_handshake_then_accept(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.CLOSED_IN_HANDSHAKE, B.ACCEPT])
    rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect"]


def test_client_id_in_use_first_is_fatal_and_leaves_no_socket(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.CLIENT_ID_IN_USE, B.ACCEPT])
    with pytest.raises(ClientIdInUse):
        rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect"]
    assert rig.clock.sleeps == []
    assert not rig.fake.isConnected()
    assert rig.conn.state is ConnState.CLOSED


def test_client_id_in_use_after_a_refuse_is_still_fatal(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.REFUSE, B.CLIENT_ID_IN_USE, B.ACCEPT])
    with pytest.raises(ClientIdInUse) as info:
        rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect", "disconnect"]
    assert [f.code for f in info.value.failures] == [502, 326]
    assert rig.clock.sleeps == [5]


def test_502_followed_by_accept_does_not_fail_attempt_two(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.REFUSE, B.ACCEPT], attempts=2)
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    # attempt 1's 502 and its connectionClosed never reach the consumer
    seen = drain(rig.conn)
    assert not [e for e in seen if isinstance(e, events.ConnectionClosed)]
    assert not [e for e in seen if isinstance(e, events.IbError) and e.code == 502]


def test_peer_drop_during_handshake_is_a_failed_attempt(rig_factory: Callable[..., Rig]) -> None:
    # TWS accepts, sends nextValidId and closes at once: the fake closes the socket inside connect.
    def drop_first(fake: Any, n: int) -> None:
        if n == 1:
            fake.drop("connectionClosed")

    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=2, on_connect=drop_first)
    rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect"]
    assert rig.clock.sleeps == [5]
    assert rig.clock.jumps == 0  # the close was noticed without waiting out ready_timeout


# -- after ready: drops and connectivity codes -----------------------------------------------------


def test_peer_drop_right_after_ready(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    drain(rig.conn)
    rig.fake.drop("connectionClosed")
    wait_for_event(rig.conn, events.ConnectionClosed)
    assert rig.conn.state is ConnState.CLOSED
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")
    assert rig.fake.connects == 1  # nothing reconnects
    rig.conn.close()
    assert rig.conn.state is ConnState.CLOSED


def test_1100_then_1102_events_in_order_and_state(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    drain(rig.conn)
    rig.fake.drop(1100)
    rig.fake.drop(1102)
    wait_for_event(rig.conn, ConnectionRestored)
    assert rig.conn.state is ConnState.READY


def test_1100_then_1102_event_order(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(fake_kw=NO_FARM)
    rig.conn.open()
    settle(rig)
    rig.fake.drop(1100)
    rig.fake.drop(1102)
    got: list[Any] = []
    end = time.monotonic() + WAIT
    while time.monotonic() < end and not any(isinstance(e, ConnectionRestored) for e in got):
        ev = rig.conn.next_event(0)
        if ev is None:
            time.sleep(0.002)
        else:
            got.append(ev)
    kinds = [type(e).__name__ for e in got]
    assert kinds == ["IbError", "ConnectionLost", "IbError", "ConnectionRestored"], kinds
    assert [e.code for e in got if isinstance(e, events.IbError)] == [1100, 1102]


def test_1100_then_1101_asks_to_resubscribe_and_nothing_reconnects(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    rig.conn.request("reqMktData", *REQUEST_ARGS["reqMktData"])
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    ev = wait_for_event(rig.conn, ConnectionRestored)
    assert ev.code == 1101 and ev.resubscribe_required
    assert len(rig.fake.calls_named("reqMktData")) == 1
    assert rig.raw_sequence() == ["connect"]


def test_1100_then_socket_drop_ends_closed_not_lost(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    rig.fake.drop(1100)
    wait_for_event(rig.conn, ConnectionLost)
    assert rig.conn.state is ConnState.LOST
    rig.fake.drop("connectionClosed")
    wait_for_event(rig.conn, events.ConnectionClosed)
    assert rig.conn.state is ConnState.CLOSED
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")


def test_1100_with_no_follow_up_stays_lost_and_does_not_reconnect(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    rig.fake.drop(1100)
    wait_for_event(rig.conn, ConnectionLost)
    assert rig.conn.state is ConnState.LOST
    assert rig.raw_sequence() == ["connect"]
    assert rig.clock.sleeps == []


@pytest.mark.parametrize(
    ("code", "req_id"), [(2110, -1), (9999, -1), (200, 5), (162, 7), (10167, 3)]
)
def test_other_codes_are_plain_error_events_and_change_no_state(
    rig_factory: Callable[..., Rig], code: int, req_id: int
) -> None:
    rig = rig_factory(fake_kw=NO_FARM)
    rig.conn.open()
    settle(rig)
    rig.fake.push("error", req_id, 1, code, "text", "")
    ev = wait_for_event(rig.conn, events.IbError)
    assert (ev.req_id, ev.code) == (req_id, code)
    assert rig.conn.state is ConnState.READY
    assert drain(rig.conn) == []  # no derived connection event


@pytest.mark.xfail(
    strict=True,
    reason="T4 defect: _lost is not reset between attempts, so a 1100 seen in a failed attempt "
    "makes the next, healthy connection report LOST",
)
def test_1100_before_the_handshake_ends_does_not_poison_the_next_attempt(
    rig_factory: Callable[..., Rig],
) -> None:
    def push_1100(fake: Any, n: int) -> None:
        if n == 1:
            fake.drop(1100)

    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], on_connect=push_1100)
    rig.clock.triggers = [lambda: bool(rig.fakes) and len(rig.fake.delivered) >= 6]
    rig.conn.open()
    assert rig.conn.state is ConnState.READY  # attempt 2 is a fresh connection


# -- close() from another thread, in each phase ---------------------------------------------------


def assert_aborted_by_close(rig: Rig, out: list[Any], connects_at_close: int) -> None:
    assert out, "open() did not return after close()"
    assert out[0][0] == "exc", "open() returned normally after close()"
    assert rig.fake.connects == connects_at_close, (
        f"connect() after close(): {rig.raw_sequence()}"
    )
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.fake.isConnected()


CLOSE_DURING_OPEN = (
    "T4 defect: close() from another thread does not abort open(); the retry loop connects "
    "again after close() and may end READY with a live socket that close() (idempotent) "
    "can no longer release"
)


@pytest.mark.parametrize(
    "fake_cls",
    [
        ProbeFake,
        pytest.param(
            QuietProbeFake, marks=pytest.mark.xfail(strict=True, reason=CLOSE_DURING_OPEN)
        ),
    ],
)
def test_close_while_connect_hangs(rig_factory: Callable[..., Rig], fake_cls: type[FakeIbClient]) -> None:
    rig = rig_factory(B.HANG, fake_cls=fake_cls)
    t, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and rig.fake.hangs_entered >= 1)
    rig.conn.close()
    rig.clock.auto = 50.0  # lets a retry loop that should be over run out
    t.join(WAIT)
    assert not t.is_alive()
    assert_aborted_by_close(rig, out, 1)


@pytest.mark.xfail(strict=True, reason=CLOSE_DURING_OPEN)
def test_close_while_waiting_for_next_valid_id(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.NO_NEXT_VALID_ID)
    t, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and len(rig.fake.delivered) >= 5)
    rig.conn.close()
    rig.clock.auto = 50.0
    t.join(WAIT)
    assert not t.is_alive()
    assert_aborted_by_close(rig, out, 1)


@pytest.mark.xfail(strict=True, reason=CLOSE_DURING_OPEN)
def test_close_while_sleeping_in_backoff(rig_factory: Callable[..., Rig]) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_sleep(seconds: float) -> None:
        entered.set()
        release.wait(WAIT)

    rig = rig_factory(B.REFUSE, sleep=blocking_sleep)
    t, out = run_in_thread(rig.conn.open)
    assert entered.wait(WAIT)
    rig.conn.close()
    release.set()
    t.join(WAIT)
    assert not t.is_alive()
    assert_aborted_by_close(rig, out, 1)


@pytest.mark.xfail(strict=True, reason=CLOSE_DURING_OPEN)
def test_close_during_open_leaves_no_live_connection_behind(rig_factory: Callable[..., Rig]) -> None:
    # The same race with a retry that succeeds: open() reports READY after close() returned.
    entered, release = threading.Event(), threading.Event()

    def blocking_sleep(seconds: float) -> None:
        entered.set()
        release.wait(WAIT)

    rig = rig_factory([B.REFUSE, B.ACCEPT], sleep=blocking_sleep)
    t, out = run_in_thread(rig.conn.open)
    assert entered.wait(WAIT)
    rig.conn.close()
    release.set()
    t.join(WAIT)
    assert not t.is_alive()
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.fake.isConnected()


def test_close_while_ready_and_a_consumer_is_blocked_in_next_event(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(fake_kw=NO_FARM)
    rig.conn.open()
    settle(rig)
    t, out = run_in_thread(lambda: rig.conn.next_event(60.0))  # the clock never moves
    time.sleep(0.02)
    rig.conn.close()
    t.join(WAIT)
    assert not t.is_alive(), "a consumer blocked in next_event is not woken by close()"
    assert isinstance(out[0][1], events.ConnectionClosed)
    assert rig.conn.state is ConnState.CLOSED


def test_close_twice_open_twice_and_open_after_close(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    with pytest.raises(RuntimeError):
        rig.conn.open()
    assert rig.fake.connects == 1
    rig.conn.close()
    rig.conn.close()
    assert rig.raw_sequence().count("disconnect") == 1
    with pytest.raises(RuntimeError):
        rig.conn.open()
    assert rig.fake.connects == 1 and len(rig.fakes) == 1
    assert rig.conn.state is ConnState.CLOSED


def test_close_before_open_then_open_builds_no_client(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.close()
    with pytest.raises(RuntimeError):
        rig.conn.open()
    assert rig.fakes == []


def test_context_manager_closes_when_the_body_raises(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    with pytest.raises(KeyError), rig.conn:
        assert rig.conn.state is ConnState.READY
        raise KeyError("body")
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.fake.isConnected()
    assert rig.raw_sequence() == ["connect", "disconnect"]


def test_context_manager_when_open_fails_leaves_nothing_behind(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.REFUSE, attempts=2)
    with pytest.raises(ConnectFailed), rig.conn:
        pass
    assert rig.conn.state is ConnState.CLOSED
    assert rig.sequence() == ["connect", "disconnect", "connect", "disconnect"]


# -- observers and consumer callbacks that raise -----------------------------------------------------


def test_observer_raising_on_request_does_not_stop_the_request(rig_factory: Callable[..., Rig]) -> None:
    class Boom:
        def on_request(self, name: str, args: tuple[Any, ...]) -> None:
            raise RuntimeError("observer")

    rig = rig_factory(observers=(Boom(),))
    rig.conn.open()
    rig.conn.request("reqCurrentTime")
    assert len(rig.fake.calls_named("reqCurrentTime")) == 1
    assert [type(e) for e in rig.conn.observer_failures] == [RuntimeError]


def test_observer_raising_on_raw_does_not_lose_events_or_readiness(rig_factory: Callable[..., Rig]) -> None:
    class Boom:
        def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
            raise RuntimeError("observer")

    rig = rig_factory(observers=(Boom(),))
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    got = drain(rig.conn)
    assert any(isinstance(e, events.NextValidId) for e in got)


# -- KeyboardInterrupt --------------------------------------------------------------------------------


def test_keyboard_interrupt_in_backoff_sleep_leaves_nothing_open(rig_factory: Callable[..., Rig]) -> None:
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    rig = rig_factory(B.REFUSE, sleep=interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig.conn.open()
    assert rig.raw_sequence() == ["connect", "disconnect"]
    assert not rig.fake.isConnected()
    assert rig.conn.state is ConnState.CLOSED


def test_keyboard_interrupt_while_waiting_for_next_valid_id_closes_and_joins(
    rig_factory: Callable[..., Rig],
) -> None:
    class InterruptClock(SeqClock):
        armed = False

        def __call__(self) -> float:
            if self.armed:
                raise KeyboardInterrupt
            return super().__call__()

    clock = InterruptClock()
    rig = rig_factory(B.NO_NEXT_VALID_ID, clock=clock)
    clock.triggers = [lambda: setattr(clock, "armed", len(rig.fake.delivered) >= 5) or False]
    with pytest.raises(KeyboardInterrupt):
        rig.conn.open()
    assert not rig.fake.isConnected()
    assert rig.conn.state is ConnState.CLOSED
    assert not [t for t in threading.enumerate() if t.name.startswith("ib-reader") and t.is_alive()]


def test_keyboard_interrupt_while_connect_hangs_releases_the_helper(rig_factory: Callable[..., Rig]) -> None:
    class InterruptClock(SeqClock):
        armed = False

        def __call__(self) -> float:
            if self.armed:
                raise KeyboardInterrupt
            return super().__call__()

    clock = InterruptClock()
    rig = rig_factory(B.HANG, clock=clock)
    clock.triggers = [lambda: setattr(clock, "armed", rig.fake.hangs_entered >= 1) or False]
    with pytest.raises(KeyboardInterrupt):
        rig.conn.open()
    assert not rig.fake.isConnected()
    wait_until(lambda: not [t for t in threading.enumerate() if t.name.startswith("ib-connect")])
    assert rig.conn.state is ConnState.CLOSED


# -- reader trouble ---------------------------------------------------------------------------------


def test_reader_dying_during_the_handshake_is_a_failed_attempt(rig_factory: Callable[..., Rig]) -> None:
    def boom_first(fake: Any, n: int) -> None:
        fake.boom_on = "managedAccounts" if n == 1 else None

    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], on_connect=boom_first)
    rig.conn.open()
    assert rig.sequence() == ["connect", "disconnect", "connect"]
    assert rig.conn.state is ConnState.READY
    assert rig.clock.sleeps == [5]


def test_reader_dying_after_ready_does_not_leave_the_state_ready(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    drain(rig.conn)
    rig.fake.boom_on = "currentTime"
    rig.fake.push("currentTime", 1)
    wait_for_event(rig.conn, ReaderStopped)
    wait_until(lambda: rig.conn.state is not ConnState.READY)
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")


# -- the attempt tag, and a connect that will not end -----------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="T4 defect: a reader that outlives join_timeout is left running on the same client; "
    "its late events are tagged with the next attempt and read as that attempt's",
)
def test_a_reader_slower_than_join_timeout_does_not_leak_events_into_the_next_attempt(
    rig_factory: Callable[..., Rig],
) -> None:
    def hook(fake: Any, n: int) -> None:
        if n == 1:
            fake.gate_on = "currentTime"
            fake.push("currentTime", 1)  # attempt 1's reader blocks in this callback
        elif n == 2:
            fake.gate.set()  # ... and wakes while attempt 2 is connecting
            wait_until(lambda: any(d.name == "currentTime" for d in fake.delivered))
            time.sleep(0.02)

    rig = rig_factory(
        [B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=2, join_timeout=0.01, on_connect=hook
    )
    rig.clock.triggers = [lambda: bool(rig.fakes) and rig.fake.entered.is_set()]
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert not [e for e in drain(rig.conn) if isinstance(e, events.CurrentTime)]
    assert len([t for t in threading.enumerate() if t.name.startswith("ib-reader")]) == 1


def test_connect_that_never_returns_is_reported_not_retried(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.ACCEPT, fake_cls=StuckConnectFake, join_timeout=0.01)
    rig.clock.triggers = [lambda: bool(rig.fakes) and rig.fake.stuck_entered.is_set()]
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert "did not end" in str(info.value)
    assert info.value.failures == ()
    helpers = [t for t in threading.enumerate() if t.name == "ib-connect-1"]
    assert helpers and all(t.daemon for t in helpers)
    assert rig.conn.state is ConnState.CLOSED
    assert rig.clock.sleeps == []


# -- the data farm mapping --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [2103, 2105, 2157, 2110])
def test_broken_farm_codes_do_not_count_as_a_data_farm(rig_factory: Callable[..., Rig], code: int) -> None:
    rig = rig_factory(fake_kw={"farm_messages": ()})
    rig.conn.open()
    rig.fake.push("error", -1, 1, code, "farm broken", "")
    wait_for_event(rig.conn, events.IbError)
    rig.clock.triggers = [lambda: True]
    assert rig.conn.wait_for_data_farm(10.0) is False


def test_farm_messages_that_came_during_open_satisfy_the_wait(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    assert rig.conn.wait_for_data_farm(5.0) is True


# -- request path ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["placeOrder", "cancelOrder", "reqGlobalCancel", "reqIds", "reqExecutions", "reqAccountSummary",
     "startApi", "sendMsg", "disconnect", "connect", "run", "__class__", "_on_event", "",
     "REQMKTDATA", "reqMktData ", "reqMktData\0"],
)
def test_names_outside_the_list_are_refused_before_the_client_is_touched(
    rig_factory: Callable[..., Rig], name: str
) -> None:
    rig = rig_factory()
    rig.conn.open()
    before = list(rig.fake.call_names)
    with pytest.raises(MessageRefused):
        rig.conn.request(name, 1, 2, 3)
    assert rig.fake.call_names == before


def test_request_refuses_a_non_string_name(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    before = list(rig.fake.call_names)
    for bad in (None, 5, b"reqMktData", ("reqMktData",)):
        with pytest.raises(MessageRefused):
            rig.conn.request(bad)  # type: ignore[arg-type]
    assert rig.fake.call_names == before


def test_every_data_request_name_sends_only_an_allowlisted_message() -> None:
    from ibapi.client import EClient
    from ibapi.contract import Contract

    from tests.test_ib_client import with_stub

    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = "AAPL", "STK", "SMART", "USD"
    args: dict[str, tuple[Any, ...]] = {
        name: tuple(c if isinstance(a, Contract) else a for a in vals)
        for name, vals in REQUEST_ARGS.items()
    }
    assert set(args) == DATA_REQUESTS
    for server_version in (200, 222):
        for name in sorted(DATA_REQUESTS):
            client = DataOnlyClient({}, lambda ev: None)
            stub = with_stub(client, server_version)
            assert client.connState == EClient.CONNECTED
            getattr(client, name)(*args[name])
            assert len(stub.sent) == 1, name
            body = stub.sent[0][4:]
            if server_version >= 201:
                msg_id = int.from_bytes(body[:4], "big")
            else:
                msg_id = int(body.split(b"\0", 1)[0])
            assert msg_id in ALLOWED_MESSAGE_IDS, (name, msg_id)


def test_throttle_never_lets_more_than_the_rate_into_any_one_second(rig_factory: Callable[..., Rig]) -> None:
    times: list[float] = []

    class Rec:
        def on_request(self, name: str, args: tuple[Any, ...]) -> None:
            times.append(rig.clock.now)

    rig = rig_factory(observers=(Rec(),), max_requests_per_second=5)
    rig.conn.open()
    # binary fractions only: with a fake clock, rounding could otherwise stall the throttle's loop
    gaps = [
        0, 0, 0, 0, 0, 0.125, 0.125, 0.375, 0, 0, 0.875, 0, 0, 0, 0,
        0.25, 0.25, 0.25, 0, 0, 0, 0, 0, 2.5, 0, 0,
    ]
    for gap in gaps:
        rig.clock.now += gap
        rig.conn.request("reqCurrentTime")
    assert len(times) == len(gaps)
    for t in times:
        assert len([u for u in times if t - 1.0 < u <= t]) <= 5, (t, times)
    assert rig.clock.sleeps  # it waited on the injected sleep; it did not raise


@pytest.mark.parametrize("rate", [0, -1])
@pytest.mark.xfail(
    strict=True,
    reason="T4 defect: max_requests_per_second below 1 is not rejected; request() then dies with "
    "IndexError inside the throttle",
)
def test_a_rate_below_one_is_rejected_at_construction(rate: int) -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, max_requests_per_second=rate)


def test_attempts_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, attempts=0)


# -- client ids ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", [0, 123, -5, True, False, "21", None, 21.0])
def test_bad_client_ids_are_refused_before_a_client_is_built(
    rig_factory: Callable[..., Rig], client_id: Any
) -> None:
    rig = rig_factory(client_id=client_id)
    with pytest.raises(ClientIdRefused):
        rig.conn.open()
    assert rig.fakes == []
    assert rig.conn.state is ConnState.NEW


@pytest.mark.parametrize("client_id", [20, 21])
def test_the_owner_client_ids_are_accepted(rig_factory: Callable[..., Rig], client_id: int) -> None:
    rig = rig_factory(client_id=client_id)
    rig.conn.open()
    assert rig.fake.calls_named("connect")[0].args[2] == client_id


@pytest.mark.parametrize("port", ["7497", 7497.0, True, None, 7496, 4001, 0, 1])
def test_bad_ports_are_refused_before_a_client_is_built(rig_factory: Callable[..., Rig], port: Any) -> None:
    rig = rig_factory(port=port)
    with pytest.raises(BrokerPortRefused):
        rig.conn.open()
    assert rig.fakes == []


# -- environment ---------------------------------------------------------------------------------------


class PoisonedEnviron:
    """Any use of os.environ (read, write, iteration, membership) fails the test."""

    def _boom(self, *a: object, **k: object) -> Any:
        raise AssertionError("os.environ was used")

    __getitem__ = __setitem__ = __delitem__ = __iter__ = __len__ = __contains__ = _boom
    get = setdefault = pop = keys = items = values = copy = update = _boom


def test_os_environ_is_never_used(
    rig_factory: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = rig_factory(port=7496, env={"STOINK_ALLOW_LIVE_PORT": "1"})
    # Only for the connection's own work: pytest's reporter reads os.environ too.
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", PoisonedEnviron())
        rig.conn.open()
        rig.conn.request("reqCurrentTime")
        rig.fake.drop(1100)
        wait_for_event(rig.conn, ConnectionLost)
        rig.conn.close()


def test_the_env_mapping_decides_the_port_not_the_process(rig_factory: Callable[..., Rig]) -> None:
    refused = rig_factory(port=7496, env={})
    with pytest.raises(BrokerPortRefused):
        refused.conn.open()
    assert refused.fakes == []
    wrong_value = rig_factory(port=7496, env={"STOINK_ALLOW_LIVE_PORT": "0"})
    with pytest.raises(BrokerPortRefused):
        wrong_value.conn.open()


def test_module_source_has_no_environment_or_raw_socket_access() -> None:
    path = Path(__file__).resolve().parent.parent / "MainStoinker" / "ib" / "connection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"environ", "getenv", "putenv", "environb"} & (attrs | names)
    assert not {"os", "socket", "subprocess", "__import__", "eval", "exec", "open"} & names
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "STOINK_ALLOW_LIVE_PORT" not in strings


# -- repeated use in one process ------------------------------------------------------------------------


def test_repeated_connections_share_no_state(rig_factory: Callable[..., Rig]) -> None:
    for _ in range(3):
        a = rig_factory()
        b = rig_factory(B.REFUSE, attempts=1)
        a.conn.open()
        with pytest.raises(ConnectFailed):
            b.conn.open()
        assert a.conn.state is ConnState.READY and b.conn.state is ConnState.CLOSED
        assert a.conn.next_request_id() == 1 and b.conn.next_request_id() == 1
        a.fake.drop(1100)
        wait_for_event(a.conn, ConnectionLost)
        assert a.conn.state is ConnState.LOST and b.conn.state is ConnState.CLOSED
        a.conn.close()
        b.conn.close()
