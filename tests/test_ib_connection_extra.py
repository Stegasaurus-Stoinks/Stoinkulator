"""Extra tests for `IbConnection`: scripted connect sequences and lifecycle probes.

Written by the tester of task T4 (issue 117). Same rules as `test_ib_connection.py`:
no socket, no real sleeps beyond a few milliseconds, the clock and sleep are injected,
and every thread a test starts ends before the test returns.
"""

import ast
import logging
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
    MAX_SECONDS,
    MAX_STUCK_THREADS,
    ClientIdInUse,
    ClientIdRefused,
    ConnectFailed,
    ConnectionClosedDuringOpen,
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


class SignalClock(SeqClock):
    """Sets `seen` when the thread named `watch` reads the clock."""

    def __init__(self) -> None:
        super().__init__()
        self.watch = ""
        self.seen = threading.Event()

    def __call__(self) -> float:
        if threading.current_thread().name == self.watch:
            self.seen.set()
        return super().__call__()


class PausingConnection(IbConnection):
    """Signals when open() is about to wait in the backoff pause."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.paused = threading.Event()

    def _pause(self, seconds: float) -> None:
        self.paused.set()
        super()._pause(seconds)


class ProbeFake(FakeIbClient):
    """The fake plus a gate for `run` and a hook for the first moments of a connect."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_connect: Callable[[Any, int], None] | None = None
        self.attempt_no = 1  # the attempt this fake (one per attempt) was built for
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
                self.on_connect(self, self.attempt_no)

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
        """The fake of the latest attempt: there is one fake (one client) per attempt."""
        return self.fakes[-1]

    @property
    def connects(self) -> int:
        return sum(f.connects for f in self.fakes)

    @property
    def hangs(self) -> int:
        return sum(f.hangs_entered for f in self.fakes)

    def any_connected(self) -> bool:
        return any(f.isConnected() for f in self.fakes)

    def raw_sequence(self) -> list[str]:
        """connect/disconnect calls of every fake, attempt by attempt."""
        return [n for f in self.fakes for n in f.call_names if n in ("connect", "disconnect")]

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
        fake_kw: dict[str, Any] | list[dict[str, Any]] | None = None,
        port: int = 7497,
        client_id: int = 21,
        env: Any = None,
        on_connect: Callable[[Any, int], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: SeqClock | None = None,
        conn_cls: type[IbConnection] = IbConnection,
        **conn_kw: Any,
    ) -> Rig:
        clock = clock if clock is not None else SeqClock()
        fakes: list[Any] = []
        script = list(behavior) if isinstance(behavior, list) else [behavior]
        kws = fake_kw if isinstance(fake_kw, list) else [fake_kw or {}]

        def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
            # one fake per attempt, each scripted with the next behaviour (the last repeats)
            wrapper = DataOnlyClient(env, on_event, on_raw, logger)
            one = script[min(len(fakes), len(script) - 1)]
            fake = fake_cls(wrapper, connect_behavior=one, **kws[min(len(fakes), len(kws) - 1)])
            fake.on_connect = on_connect
            fakes.append(fake)
            fake.attempt_no = len(fakes)
            return fake

        conn = conn_cls(
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


def run_in_thread(
    fn: Callable[[], Any], name: str = "probe-caller"
) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def target() -> None:
        try:
            out.append(("ok", fn()))
        except BaseException as exc:  # handed to the test thread
            out.append(("exc", exc))

    t = threading.Thread(target=target, name=name, daemon=True)
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
    assert rig.connects == 3
    assert sum(rig.clock.sleeps) == 15


def test_backoff_is_capped_by_max_retry_delay(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.REFUSE, attempts=8, retry_delay=5, backoff=2.0, max_retry_delay=60)
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    assert rig.clock.sleeps == [5, 10, 20, 40, 60, 60, 60]


def test_hang_hang_accept(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory([B.HANG, B.HANG, B.ACCEPT], attempts=3)
    rig.clock.triggers = [
        lambda: rig.hangs >= 1,
        lambda: rig.hangs >= 2,
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
        lambda: rig.hangs >= 1,
        lambda: rig.hangs >= 2,
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
    assert not rig.any_connected()
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
    assert type(out[0][1]) is ConnectionClosedDuringOpen, out[0][1]  # not a bare AttributeError
    assert rig.connects == connects_at_close, (
        f"connect() after close(): {rig.raw_sequence()}"
    )
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.any_connected()


@pytest.mark.parametrize("fake_cls", [ProbeFake, QuietProbeFake])
def test_close_while_connect_hangs(rig_factory: Callable[..., Rig], fake_cls: type[FakeIbClient]) -> None:
    rig = rig_factory(B.HANG, fake_cls=fake_cls)
    t, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and rig.fake.hangs_entered >= 1)
    rig.conn.close()
    t.join(WAIT)
    assert not t.is_alive()
    assert_aborted_by_close(rig, out, 1)
    assert rig.clock.sleeps == []  # no backoff, no second attempt


def test_close_while_waiting_for_next_valid_id(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.NO_NEXT_VALID_ID)
    t, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and len(rig.fake.delivered) >= 5)
    rig.conn.close()
    t.join(WAIT)
    assert not t.is_alive()
    assert_aborted_by_close(rig, out, 1)
    assert rig.clock.jumps == 0 and rig.clock.sleeps == []  # woken by close, not by a timeout


def test_close_between_attempts_makes_no_further_connect(rig_factory: Callable[..., Rig]) -> None:
    holder: list[IbConnection] = []

    def closing_sleep(seconds: float) -> None:
        holder[0].close()  # the backoff has begun; nothing is connected now

    rig = rig_factory([B.REFUSE, B.ACCEPT], sleep=closing_sleep)
    holder.append(rig.conn)
    with pytest.raises(ConnectionClosedDuringOpen):
        rig.conn.open()
    assert rig.connects == 1
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.any_connected()


def test_a_connect_that_succeeds_while_close_runs_is_disconnected(rig_factory: Callable[..., Rig]) -> None:
    holder: list[IbConnection] = []

    def close_when_connect_returns(fake: Any, n: int) -> None:
        holder[0].close()  # on the connect helper thread, after the handshake was accepted

    rig = rig_factory(B.ACCEPT, on_connect=close_when_connect_returns)
    holder.append(rig.conn)
    with pytest.raises(ConnectionClosedDuringOpen):
        rig.conn.open()
    assert rig.connects == 1
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.any_connected()


def test_close_from_another_thread_while_the_default_backoff_sleeps_wakes_it(
    rig_factory: Callable[..., Rig],
) -> None:
    # No injected sleep: the default waits on an event that close() sets. A 60 s delay
    # would block this test for a minute if close() could not end it.
    clock = SeqClock()
    made: list[Any] = []

    def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
        fake = ProbeFake(DataOnlyClient(env, on_event, on_raw, logger), connect_behavior=B.REFUSE)
        made.append(fake)
        return fake

    conn = PausingConnection(
        "127.0.0.1", 7497, 21, env={}, client_factory=factory, clock=clock, poll_interval=0.002,
        retry_delay=60, max_retry_delay=60,
    )
    t, out = run_in_thread(conn.open)
    assert conn.paused.wait(WAIT)  # open() is at the backoff wait (or about to be: both end the same)
    conn.close()
    t.join(WAIT)
    assert not t.is_alive(), "close() did not wake the default backoff sleep"
    assert type(out[0][1]) is ConnectionClosedDuringOpen
    assert made[0].connects == 1
    assert conn.state is ConnState.CLOSED


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
    assert not rig.any_connected()


def test_close_while_ready_and_a_consumer_is_blocked_in_next_event(rig_factory: Callable[..., Rig]) -> None:
    clock = SignalClock()
    rig = rig_factory(fake_kw=NO_FARM, clock=clock)
    rig.conn.open()
    settle(rig)
    clock.watch = "probe-caller"
    t, out = run_in_thread(lambda: rig.conn.next_event(60.0))  # the clock never moves
    assert clock.seen.wait(WAIT)  # the consumer is inside next_event
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
    assert rig.connects == 1
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
    assert not rig.any_connected()
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
    assert not rig.any_connected()
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
    assert not rig.any_connected()
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
    assert not rig.any_connected()
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


# -- one client per attempt: nothing of an old client reaches the next attempt --------------------


def test_each_attempt_gets_a_fresh_client_and_requests_go_to_the_last_one(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory([B.REFUSE, B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=3)
    rig.clock.triggers = [lambda: len(rig.fakes) > 1 and len(rig.fakes[1].delivered) >= 5]
    rig.conn.open()
    assert len(rig.fakes) == 3 and len({id(f) for f in rig.fakes}) == 3
    assert [f.connects for f in rig.fakes] == [1, 1, 1]  # no client is connected twice
    assert not rig.fakes[0].isConnected() and not rig.fakes[1].isConnected()
    rig.conn.request("reqCurrentTime")
    assert [len(f.calls_named("reqCurrentTime")) for f in rig.fakes] == [0, 0, 1]
    # the old clients are never used again: no run() and no call after their disconnect()
    for old in rig.fakes[:2]:
        assert "disconnect" in old.call_names
        assert len(old.calls_named("run")) <= 1


def test_a_stuck_reader_of_an_old_client_is_logged_and_the_next_attempt_uses_a_new_client(
    rig_factory: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    def hook(fake: Any, n: int) -> None:
        if n == 1:
            fake.gate_on = "currentTime"
            fake.push("currentTime", 1)  # attempt 1's reader blocks in this callback

    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=3, join_timeout=0.01, on_connect=hook)
    rig.clock.triggers = [lambda: rig.fakes[0].entered.is_set()]
    with caplog.at_level(logging.WARNING):
        rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert len(rig.fakes) == 2 and rig.connects == 2
    assert any("ib-reader-1" in r.getMessage() and "did not end" in r.getMessage() for r in caplog.records)
    assert len(rig.fakes[0].calls_named("run")) == 1  # the old client never ran a second time
    assert len(rig.fakes[1].calls_named("run")) == 1


def test_open_gives_up_when_too_many_old_threads_are_stuck_and_reports_every_failure(
    rig_factory: Callable[..., Rig],
) -> None:
    def hook(fake: Any, n: int) -> None:
        fake.gate_on = "currentTime"
        fake.push("currentTime", 1)

    rig = rig_factory(B.NO_NEXT_VALID_ID, attempts=10, join_timeout=0.01, on_connect=hook)
    rig.clock.triggers = [
        (lambda i=i: len(rig.fakes) > i and rig.fakes[i].entered.is_set())
        for i in range(MAX_STUCK_THREADS + 1)
    ]
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert not isinstance(info.value, ConnectionClosedDuringOpen)
    assert len(rig.fakes) == MAX_STUCK_THREADS + 1  # attempts stop at the first one over the bound
    failures = info.value.failures
    assert [f.attempt for f in failures if "did not end" in f.message] == list(
        range(1, MAX_STUCK_THREADS + 2)
    )
    assert len(failures) == 2 * (MAX_STUCK_THREADS + 1)  # each attempt: its own failure + the stuck thread
    assert f"ib-reader-{MAX_STUCK_THREADS + 1}" in str(info.value)
    assert rig.conn.state is ConnState.CLOSED


def test_a_connect_that_never_returns_does_not_stop_the_next_attempt_but_is_bounded(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(B.ACCEPT, fake_cls=StuckConnectFake, attempts=10, join_timeout=0.01)
    rig.clock.auto = 50.0  # every clock read is past the connect timeout
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert len(rig.fakes) == MAX_STUCK_THREADS + 1
    # item 10: the failures of all attempts so far, also when raised for stuck threads
    expected = [n for n in range(1, MAX_STUCK_THREADS + 2) for _ in (0, 1)]
    assert [f.attempt for f in info.value.failures] == expected
    assert all("did not end" in f.message for f in info.value.failures)
    helpers = [t for t in threading.enumerate() if t.name.startswith("ib-connect-")]
    assert helpers and all(t.daemon for t in helpers)
    assert rig.conn.state is ConnState.CLOSED


STALE = ["nextValidId", "1100", "farm", "326"]


def inject_late(old: Any, what: str) -> None:
    """A late callback of an old client, as its reader or the disconnect() on it could make."""
    wrapper = old.wrapper
    if what == "nextValidId":
        wrapper.nextValidId(999)
    elif what == "1100":
        wrapper.error(-1, 0, 1100, "late 1100", "")
    elif what == "farm":
        wrapper.error(-1, 0, 2106, "late farm", "")
    elif what == "326":
        wrapper.error(-1, 0, 326, "late 326", "")
    else:
        raise AssertionError(what)


def test_a_late_next_valid_id_of_attempt_one_never_makes_attempt_two_ready(
    rig_factory: Callable[..., Rig],
) -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    class Rec:
        def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
            seen.append((name, args))

    def hook(fake: Any, n: int) -> None:
        if n == 2:  # on the connect helper of attempt 2, as the old thread tag would have read it
            inject_late(rig.fakes[0], "nextValidId")

    rig = rig_factory(
        B.NO_NEXT_VALID_ID, attempts=2, fake_kw=NO_FARM, on_connect=hook, observers=(Rec(),)
    )
    rig.clock.triggers = [
        lambda: len(rig.fakes[0].delivered) >= 2,
        lambda: len(rig.fakes) > 1 and len(rig.fakes[1].delivered) >= 2,
    ]
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    assert rig.conn.next_valid_id is None
    assert rig.conn.state is ConnState.CLOSED
    assert ("nextValidId", (999,)) not in seen


def test_the_reviewer_probe_a_held_back_next_valid_id_999_is_never_reported(
    rig_factory: Callable[..., Rig],
) -> None:
    # Attempt 1: no nextValidId, but a late nextValidId(999) queued; its reader is held on
    # managedAccounts past the ready deadline. Attempt 2: no nextValidId at all.
    def hook(fake: Any, n: int) -> None:
        if n == 1:
            fake.gate_on = "managedAccounts"
            fake.push("nextValidId", 999)
        else:
            rig.fakes[0].gate.set()  # the old reader wakes up while attempt 2 waits

    rig = rig_factory(
        B.NO_NEXT_VALID_ID, attempts=2, join_timeout=0.01, fake_kw=NO_FARM, on_connect=hook
    )
    rig.clock.triggers = [
        lambda: rig.fakes[0].entered.is_set(),
        lambda: len(rig.fakes) > 1 and len(rig.fakes[1].delivered) >= 2,
    ]
    with pytest.raises(ConnectFailed):
        rig.conn.open()
    assert rig.conn.next_valid_id is None
    assert rig.conn.state is ConnState.CLOSED


@pytest.mark.parametrize("what", ["1100", "farm", "326"])
def test_stale_state_changing_callbacks_of_attempt_one_do_not_touch_attempt_two(
    rig_factory: Callable[..., Rig], what: str
) -> None:
    def hook(fake: Any, n: int) -> None:
        if n == 2:
            inject_late(rig.fakes[0], what)

    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=2, fake_kw=NO_FARM, on_connect=hook)
    rig.clock.triggers = [lambda: len(rig.fakes[0].delivered) >= 2]
    rig.conn.open()  # not ClientIdInUse for a stale 326
    assert rig.conn.state is ConnState.READY  # not LOST for a stale 1100
    got = drain(rig.conn)
    assert not [e for e in got if isinstance(e, ConnectionLost)]
    assert not [e for e in got if isinstance(e, events.IbError) and e.code in (1100, 326, 2106)]
    rig.clock.triggers = [lambda: True]
    assert rig.conn.wait_for_data_farm(10.0) is False  # a stale farm code is not this attempt's


def test_farm_codes_of_a_failed_attempt_do_not_satisfy_the_wait_of_the_next(
    rig_factory: Callable[..., Rig],
) -> None:
    # Attempt 1 sees the three farm messages and then fails; attempt 2 sees none.
    rig = rig_factory([B.NO_NEXT_VALID_ID, B.ACCEPT], attempts=2, fake_kw=[{}, NO_FARM])
    rig.clock.triggers = [lambda: len(rig.fakes[0].delivered) >= 5]
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert {d.name for d in rig.fakes[0].delivered} >= {"error"}
    rig.clock.triggers = [lambda: True]
    assert rig.conn.wait_for_data_farm(10.0) is False
    for kind in ("market", "historical", "secdef"):
        rig.clock.triggers = [lambda: True]
        assert rig.conn.wait_for_data_farm(10.0, farm=kind) is False


# -- the READY guard, and close() racing open() ---------------------------------------------------


class CloseAtReady(IbConnection):
    """Runs close() to completion right after nextValidId is taken, before READY is set."""

    def _pop(self, att: Any, wait: float) -> Any:
        ev = IbConnection._pop(att, wait)
        if isinstance(ev, events.NextValidId):
            self.close()
        return ev


def test_close_that_completes_just_before_ready_is_not_overwritten_by_ready(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(conn_cls=CloseAtReady)
    with pytest.raises(ConnectionClosedDuringOpen):
        rig.conn.open()
    assert rig.conn.state is ConnState.CLOSED
    assert rig.conn.next_valid_id is None
    assert not rig.any_connected()
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")


class CleanupGateFake(ProbeFake):
    """disconnect() on the thread named probe-caller (the one inside open()) blocks."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cleanup_entered = threading.Event()
        self.cleanup_release = threading.Event()

    def disconnect(self) -> None:
        if threading.current_thread().name == "probe-caller":
            self.cleanup_entered.set()
            self.cleanup_release.wait(WAIT)
        super().disconnect()


def test_close_waits_for_open_to_finish_its_cleanup(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(B.HANG, fake_cls=CleanupGateFake)
    opener, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and rig.hangs >= 1)
    closer, _ = run_in_thread(rig.conn.close, name="probe-closer")
    assert rig.fake.cleanup_entered.wait(WAIT)  # open() is in the middle of its cleanup
    closer.join(0.05)
    assert closer.is_alive(), "close() returned while open() was still cleaning up"
    rig.fake.cleanup_release.set()
    closer.join(WAIT)
    opener.join(WAIT)
    assert not closer.is_alive() and not opener.is_alive()
    assert type(out[0][1]) is ConnectionClosedDuringOpen
    assert rig.conn.state is ConnState.CLOSED
    assert not rig.any_connected()


def test_close_stops_waiting_for_open_after_join_timeout_and_says_so(
    rig_factory: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = rig_factory(B.HANG, fake_cls=CleanupGateFake, join_timeout=0.01)
    opener, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and rig.hangs >= 1)
    with caplog.at_level(logging.ERROR):
        rig.conn.close()  # open() is blocked in its cleanup: close() gives up after join_timeout
    assert rig.fake.cleanup_entered.is_set()
    assert any(
        r.levelno == logging.ERROR and "did not finish" in r.getMessage() and "probe-caller" in r.getMessage()
        for r in caplog.records
    )
    assert rig.conn.state is ConnState.CLOSED
    rig.fake.cleanup_release.set()
    opener.join(WAIT)
    assert not opener.is_alive()
    assert type(out[0][1]) is ConnectionClosedDuringOpen


def test_a_connect_that_finishes_after_close_returned_is_disconnected_by_the_helper(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(B.ACCEPT, fake_cls=StuckConnectFake, join_timeout=0.01)
    opener, out = run_in_thread(rig.conn.open)
    wait_until(lambda: bool(rig.fakes) and rig.fake.stuck_entered.is_set())
    rig.conn.close()  # the connect has not assigned its connection yet: nothing to disconnect
    assert rig.conn.state is ConnState.CLOSED
    opener.join(WAIT)
    assert not opener.is_alive() and type(out[0][1]) is ConnectionClosedDuringOpen
    rig.fake.stuck_release.set()  # now the connect completes
    wait_until(lambda: not [t for t in threading.enumerate() if t.name.startswith("ib-connect")])
    assert not rig.any_connected()
    assert rig.conn.state is ConnState.CLOSED


# -- disconnect() of the real client can race and raise; ConnectionClosed is delivered once -------


class RaisingDisconnectFake(ProbeFake):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.disconnect_raises: BaseException | None = None  # raised by the next disconnect() only

    def disconnect(self) -> None:
        super().disconnect()
        error, self.disconnect_raises = self.disconnect_raises, None
        if error is not None:
            raise error


RACE_ERRORS = [AttributeError("conn is None"), OSError("closed"), TypeError("x")]


@pytest.mark.parametrize("error", RACE_ERRORS, ids=lambda e: type(e).__name__)
def test_close_treats_a_disconnect_race_as_already_closed(
    rig_factory: Callable[..., Rig], error: BaseException
) -> None:
    rig = rig_factory(fake_cls=RaisingDisconnectFake)
    rig.conn.open()
    rig.fake.disconnect_raises = error
    rig.conn.close()
    assert rig.conn.state is ConnState.CLOSED
    assert not [t for t in threading.enumerate() if t.name.startswith("ib-reader")]


def test_close_does_not_hide_other_errors_from_disconnect(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(fake_cls=RaisingDisconnectFake)
    rig.conn.open()
    rig.fake.disconnect_raises = ValueError("not a race")
    with pytest.raises(ValueError):
        rig.conn.close()
    assert rig.conn.state is ConnState.CLOSED
    rig.fake.disconnect_raises = None
    rig.conn.close()  # idempotent


def test_a_failed_attempt_whose_disconnect_raises_a_race_error_still_retries(
    rig_factory: Callable[..., Rig],
) -> None:
    def hook(fake: Any, n: int) -> None:
        if n == 1:
            fake.disconnect_raises = AttributeError("conn is None")

    rig = rig_factory([B.REFUSE, B.ACCEPT], fake_cls=RaisingDisconnectFake, attempts=2, on_connect=hook)
    rig.conn.open()
    assert rig.conn.state is ConnState.READY


def test_at_most_one_connection_closed_per_attempt_reaches_the_consumer(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(fake_kw=NO_FARM)
    rig.conn.open()
    settle(rig)
    rig.fake.wrapper.connectionClosed()  # run()'s finally ...
    rig.fake.wrapper.connectionClosed()  # ... and the caller's disconnect(), as with real ibapi
    rig.conn.close()
    got = drain(rig.conn)
    assert len([e for e in got if isinstance(e, events.ConnectionClosed)]) == 1


# -- a released hang that then raises something else is not retried ---------------------------------


class HangThenOther(ProbeFake):
    def connect(self, host: Any, port: Any, clientId: Any) -> None:
        try:
            return super().connect(host, port, clientId)
        except AttributeError as exc:
            raise ValueError("the released connect failed in another way") from exc


def test_a_released_hang_that_raises_something_else_propagates_without_retry(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory([B.HANG, B.ACCEPT], fake_cls=HangThenOther)
    rig.clock.triggers = [lambda: rig.hangs >= 1]
    with pytest.raises(ValueError, match="another way"):
        rig.conn.open()
    assert rig.connects == 1 and len(rig.fakes) == 1
    assert rig.clock.sleeps == []
    assert rig.conn.state is ConnState.CLOSED


# -- observers must not call back into the connection ------------------------------------------------


def test_an_observer_that_calls_request_gets_an_error_instead_of_a_deadlock(
    rig_factory: Callable[..., Rig],
) -> None:
    class Reentrant:
        conn: IbConnection

        def on_request(self, name: str, args: tuple[Any, ...]) -> None:
            self.conn.request("reqCurrentTime")

    obs = Reentrant()
    rig = rig_factory(observers=(obs,))
    obs.conn = rig.conn
    rig.conn.open()
    t, out = run_in_thread(lambda: rig.conn.request("reqMarketDataType", 3))
    t.join(WAIT)
    assert not t.is_alive(), "request() deadlocked on an observer that called request()"
    assert out[0][0] == "ok"
    assert [type(e) for e in rig.conn.observer_failures] == [RuntimeError]
    assert len(rig.fake.calls_named("reqMarketDataType")) == 1
    assert rig.fake.calls_named("reqCurrentTime") == []
    rig.conn.request("reqCurrentTime")  # the guard is released afterwards
    assert len(rig.fake.calls_named("reqCurrentTime")) == 1


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


FARM_CASES = [
    # (code pushed, kinds that must be satisfied)
    (2104, {"market"}),
    (2108, {"market"}),
    (2106, {"historical"}),
    (2107, {"historical"}),
    (2158, {"secdef"}),
]


@pytest.mark.parametrize(("code", "satisfied"), FARM_CASES)
@pytest.mark.parametrize("kind", ["market", "historical", "secdef"])
def test_wait_for_a_specific_farm_kind(
    rig_factory: Callable[..., Rig], code: int, satisfied: set[str], kind: str
) -> None:
    rig = rig_factory(fake_kw=NO_FARM)
    rig.conn.open()
    rig.fake.push("error", -1, 1, code, "farm", "")
    wait_for_event(rig.conn, events.IbError)
    if kind in satisfied:
        assert rig.conn.wait_for_data_farm(1.0, farm=kind) is True
    else:
        rig.clock.triggers = [lambda: True]
        assert rig.conn.wait_for_data_farm(10.0, farm=kind) is False
    # the default still accepts any farm
    assert rig.conn.wait_for_data_farm(1.0) is True


def test_the_historical_wait_is_not_satisfied_by_the_market_farm_alone(
    rig_factory: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = rig_factory(fake_kw={"farm_messages": ((2104, "Market data farm connection is OK:usfarm"),)})
    rig.conn.open()
    assert rig.conn.wait_for_data_farm(5.0, farm="market") is True
    rig.clock.triggers = [lambda: True]
    assert rig.conn.wait_for_data_farm(10.0, farm="historical") is False
    assert any("2106" in r.getMessage() for r in caplog.records)
    # the historical farm answers later: the same call now succeeds
    rig.fake.push("error", -1, 1, 2106, "HMDS data farm connection is OK:ushmds", "")
    assert rig.conn.wait_for_data_farm(5.0, farm="historical") is True


@pytest.mark.parametrize("farm", ["hmds", "", "MARKET", 2106, ("market",)])
def test_an_unknown_farm_kind_is_refused(rig_factory: Callable[..., Rig], farm: Any) -> None:
    rig = rig_factory()
    rig.conn.open()
    with pytest.raises(ValueError):
        rig.conn.wait_for_data_farm(1.0, farm=farm)


def test_the_farm_event_is_readable_as_soon_as_the_wait_returns(rig_factory: Callable[..., Rig]) -> None:
    for _ in range(20):
        rig = rig_factory(fake_kw=NO_FARM)
        rig.conn.open()
        rig.fake.push("error", -1, 1, 2106, "HMDS data farm connection is OK", "")
        assert rig.conn.wait_for_data_farm(5.0, farm="historical") is True
        got = drain(rig.conn)
        assert any(isinstance(e, events.IbError) and e.code == 2106 for e in got)


def test_the_raw_client_is_not_part_of_the_public_surface(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.conn.open()
    assert not hasattr(rig.conn, "client")
    public = {n for n in dir(rig.conn) if not n.startswith("_")}
    assert not {"client", "_inner", "disconnect", "connect", "run"} & public


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


@pytest.mark.parametrize("rate", [0, -1, 51, 1000, 20.0, True, "20", None])
def test_a_rate_outside_one_to_fifty_is_rejected_at_construction(rate: Any) -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, max_requests_per_second=rate)


@pytest.mark.parametrize("rate", [1, 20, 50])
def test_rates_inside_the_range_are_accepted(rate: int) -> None:
    IbConnection("127.0.0.1", 7497, 21, env={}, max_requests_per_second=rate)


@pytest.mark.parametrize("attempts", [0, -1, 101, 10_000, 3.0, True, None, "3"])
def test_attempts_outside_one_to_a_hundred_are_rejected(attempts: Any) -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, attempts=attempts)


@pytest.mark.parametrize("attempts", [1, 100])
def test_attempts_at_the_bounds_are_accepted(attempts: int) -> None:
    IbConnection("127.0.0.1", 7497, 21, env={}, attempts=attempts)


BAD_NUMBERS = [0, -1, -0.5, float("inf"), float("nan"), True, "5", None]


@pytest.mark.parametrize(
    "name",
    ["connect_timeout", "ready_timeout", "retry_delay", "max_retry_delay", "poll_interval", "join_timeout"],
)
@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_timeouts_and_delays_must_be_finite_and_positive(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, **{name: value})


@pytest.mark.parametrize("value", [0.99, 0, -2, float("inf"), float("nan"), True, "2", None])
def test_backoff_below_one_or_not_finite_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, backoff=value)


def test_backoff_of_one_and_fractional_timeouts_are_accepted() -> None:
    IbConnection("127.0.0.1", 7497, 21, env={}, backoff=1, connect_timeout=0.5, join_timeout=0.01)


def test_throttle_terminates_when_the_clock_does_not_advance() -> None:
    sleeps: list[float] = []

    def frozen_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) < 50, "the throttle is spinning on a clock that does not move"

    conn, fake = _ready_connection(sleep=frozen_sleep, max_requests_per_second=2)
    for _ in range(7):
        conn.request("reqCurrentTime")
    assert len(fake.calls_named("reqCurrentTime")) == 7
    assert 1 <= len(sleeps) < 10
    assert all(s > 0 for s in sleeps)


@pytest.mark.parametrize(
    "frozen", [0.2, 0.1, 0.7, 1 / 3, 2 / 3, 1e9 + 0.3, 123456.789, 5.55, 1e-9, 0.0]
)
def test_throttle_terminates_on_a_clock_frozen_at_an_awkward_float(frozen: float) -> None:
    # (0.2 + 1.0) - 1.0 < 0.2 in floats: comparing against `now - window` never expires.
    sleeps: list[float] = []

    def frozen_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) < 50, f"the throttle is spinning on a clock frozen at {frozen!r}"

    conn, fake = _ready_connection(frozen, sleep=frozen_sleep, max_requests_per_second=1)
    for _ in range(4):
        conn.request("reqCurrentTime")
    assert len(fake.calls_named("reqCurrentTime")) == 4
    assert 1 <= len(sleeps) <= 4
    assert all(s > 0 for s in sleeps)


def test_the_bounds_of_timeouts_and_delays_are_the_documented_ones() -> None:
    assert MAX_SECONDS == {
        "connect_timeout": 300,
        "ready_timeout": 300,
        "retry_delay": 600,
        "max_retry_delay": 600,
        "poll_interval": 10,
        "join_timeout": 60,
    }


@pytest.mark.parametrize("name", sorted(MAX_SECONDS))
def test_a_timeout_or_delay_above_its_bound_is_rejected_and_the_bound_itself_accepted(name: str) -> None:
    IbConnection("127.0.0.1", 7497, 21, env={}, **{name: MAX_SECONDS[name]})
    for too_much in (MAX_SECONDS[name] + 0.001, MAX_SECONDS[name] * 2, 1e9):
        with pytest.raises(ValueError):
            IbConnection("127.0.0.1", 7497, 21, env={}, **{name: too_much})


def _ready_connection(frozen: float = 0.0, **kw: Any) -> tuple[IbConnection, Any]:
    """A READY connection over the fake whose clock never moves (it stays at `frozen`)."""
    fakes: list[Any] = []

    def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
        fakes.append(ProbeFake(DataOnlyClient(env, on_event, on_raw, logger)))
        return fakes[0]

    conn = IbConnection(
        "127.0.0.1", 7497, 21, env={}, client_factory=factory, clock=lambda: frozen,
        poll_interval=0.002, **kw,
    )
    conn.open()
    _CLOSE_LATER.append(conn)
    return conn, fakes[0]


_CLOSE_LATER: list[IbConnection] = []


@pytest.fixture(autouse=True)
def _close_helper_connections() -> Iterator[None]:
    yield
    while _CLOSE_LATER:
        _CLOSE_LATER.pop().close()


def test_attempts_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        IbConnection("127.0.0.1", 7497, 21, env={}, attempts=0)


# -- client ids ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", [0, 123, -5, True, False, "21", None, 21.0, 2**31, 2**40])
def test_bad_client_ids_are_refused_before_a_client_is_built(
    rig_factory: Callable[..., Rig], client_id: Any
) -> None:
    rig = rig_factory(client_id=client_id)
    with pytest.raises(ClientIdRefused):
        rig.conn.open()
    assert rig.fakes == []
    assert rig.conn.state is ConnState.NEW


@pytest.mark.parametrize("client_id", [1, 20, 21, 2**31 - 1])
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
