"""`IbConnection(require_paper=True)`: the paper-account check, against the scripted fake.

Nothing here opens a socket or reads the environment. One fake per attempt, built by the
injected factory. Every account-shaped value is assembled at run time from parts, so no
account-shaped literal is committed. Time is injected; the only real waits are polls of a
few milliseconds.
"""

import ast
import logging
import queue
import sys
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.ib import connection as connection_module
from MainStoinker.ib import events
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import (
    ConnectFailed,
    ConnState,
    IbConnection,
    NotConnected,
    NotPaperSession,
    _all_paper_accounts,
    _Attempt,
)
from tests.fake_ib import REQUEST_ARGS, FakeIbClient

WAIT = 5.0
SOURCE = Path(connection_module.__file__)
DIGITS = "7391824"
OTHER_DIGITS = "5502913"


def paper(digits: str = DIGITS, letters: str = "U") -> str:
    return "D" + letters + digits


class OrderedFake(FakeIbClient):
    """The fake with a chosen handshake: `order` is "id_first" (the fake's own),
    "accounts_first" or "no_accounts" (managedAccounts never comes)."""

    def __init__(self, *args: Any, order: str = "id_first", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.order = order

    def connect(self, host: Any, port: Any, clientId: Any) -> None:  # noqa: N803
        super().connect(host, port, clientId)
        items: list[Any] = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        accounts = [i for i in items if i[0] == "managedAccounts"]
        rest = [i for i in items if i[0] != "managedAccounts"]
        if self.order == "accounts_first":
            items = accounts + rest
        elif self.order == "no_accounts":
            items = rest
        for item in items:
            self._q.put(item)


class StepClock:
    """Frozen, except that `sleep` advances it and, once the latest fake has finished
    `connect`, every reading adds `step` (so a ready deadline passes without real waiting)."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step
        self.fakes: list[Any] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            if self.step and self.fakes and any(
                d.name == "connectAck" for d in self.fakes[-1].delivered
            ):
                self.now += self.step
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class Rig:
    def __init__(self, conn: IbConnection, fakes: list[OrderedFake]) -> None:
        self.conn = conn
        self.fakes = fakes

    @property
    def fake(self) -> OrderedFake:
        return self.fakes[-1]

    def requests_reaching_fakes(self) -> list[str]:
        return [n for f in self.fakes for n in f.call_names if n in REQUEST_ARGS]


@pytest.fixture
def build(request: pytest.FixtureRequest) -> Callable[..., Rig]:
    def make(
        accounts: str,
        *,
        order: str = "id_first",
        port: int = 7497,
        require_paper: bool = True,
        step: float = 0.0,
        env: dict[str, str] | None = None,
        **conn_kw: Any,
    ) -> Rig:
        clock = StepClock(step)
        fakes: list[OrderedFake] = []
        clock.fakes = fakes

        def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
            wrapper = DataOnlyClient(env, on_event, on_raw, logger)
            fake = OrderedFake(wrapper, managed_accounts=accounts, order=order)
            fakes.append(fake)
            return fake

        conn = IbConnection(
            "127.0.0.1", port, 21, env=env if env is not None else {}, client_factory=factory,
            clock=clock, sleep=clock.sleep, poll_interval=0.002, require_paper=require_paper,
            **conn_kw,
        )
        request.addfinalizer(conn.close)
        return Rig(conn, fakes)

    return make


def wait_until(predicate: Callable[[], bool]) -> bool:
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.002)
    return False


# -- the pattern ----------------------------------------------------------------------------


def not_paper_ids() -> dict[str, str]:
    return {
        "live U": "U" + DIGITS,
        "F": "F" + DIGITS,
        "I": "I" + DIGITS,
        "lower case": "du" + DIGITS,
        "digits only": DIGITS,
        "D alone": "D",
        "D and digits only": "D" + DIGITS,
        "DA": "DA" + DIGITS,
        "DX": "DX" + DIGITS,
        "DU without digits": "DU",
        "suffix": paper() + "x",
        "two letters after DU": "DUXY" + DIGITS,
        "inner space": "D U" + DIGITS,
        "lower case letter": "DUx" + DIGITS,
        "letter after digits": "DU123A",
        "letter only": "DUX",
        "non-ASCII letter": "DUÄ123",
        "Greek capital alpha": "DUΑ123",
        "live U, seven digits": "U1234567",
        "DA short": "DA123",
        "DX short": "DX1",
        "D and three digits": "D123",
        "UX": "UX123456",
        "space inside": " DUX 123",
    }


@pytest.mark.parametrize("label", list(not_paper_ids()))
def test_pattern_rejects(label: str) -> None:
    assert _all_paper_accounts(not_paper_ids()[label]) == (False, 1, 1)


def test_pattern_accepts_and_counts() -> None:
    assert _all_paper_accounts(paper()) == (True, 1, 0)
    assert _all_paper_accounts(paper(letters="I")) == (True, 1, 0)
    assert _all_paper_accounts(paper(letters="F")) == (True, 1, 0)
    assert _all_paper_accounts(f" {paper()} , {paper(OTHER_DIGITS, 'F')} ,") == (True, 2, 0)
    assert _all_paper_accounts(f"{paper()},U{DIGITS}") == (False, 2, 1)
    assert _all_paper_accounts("") == (False, 0, 0)
    assert _all_paper_accounts(" , ,") == (False, 0, 0)


@pytest.mark.parametrize(
    "ident",
    ["DUX123456", "DUA1", "DFB123", "DIZ9", "DU123456", "DF1", "DI22"],
)
def test_pattern_accepts_each_paper_form(ident: str) -> None:
    assert _all_paper_accounts(ident) == (True, 1, 0)


def test_pattern_refuses_a_list_mixing_paper_and_live() -> None:
    assert _all_paper_accounts("DUX123456,U1234567") == (False, 2, 1)
    assert _all_paper_accounts("DUX123456, DU123456") == (True, 2, 0)


# -- default behaviour -----------------------------------------------------------------------


def test_default_is_unchanged_with_a_live_looking_account(build: Callable[..., Rig]) -> None:
    rig = build("U" + DIGITS, require_paper=False)
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert rig.conn.paper_verified is False
    seen: list[Any] = []

    def forwarded() -> bool:  # the list may be queued just after nextValidId: wait for it
        while (ev := rig.conn.next_event(0)) is not None:
            seen.append(ev)
        return any(isinstance(e, events.ManagedAccounts) for e in seen)

    assert wait_until(forwarded)  # still forwarded


def test_require_paper_must_be_a_bool() -> None:
    with pytest.raises(ValueError, match="require_paper"):
        IbConnection("127.0.0.1", 7497, 21, env={}, require_paper=1)  # type: ignore[arg-type]


# -- paper ids are accepted --------------------------------------------------------------------


@pytest.mark.parametrize("order", ["id_first", "accounts_first"])
@pytest.mark.parametrize(
    "accounts",
    [
        paper(),
        f"{paper()},{paper(OTHER_DIGITS, 'F')}",
        f" {paper()} , {paper(OTHER_DIGITS)} ,",
        paper(letters="I"),
        "DUX123456",
        "DUX123456,DU123456",
    ],
)
def test_paper_accounts_become_ready(build: Callable[..., Rig], accounts: str, order: str) -> None:
    rig = build(accounts, order=order)
    assert rig.conn.paper_verified is False
    rig.conn.open()
    assert rig.conn.state is ConnState.READY
    assert rig.conn.paper_verified is True
    assert rig.conn.next_valid_id == 1
    assert len(rig.fakes) == 1
    rig.conn.request("reqCurrentTime")
    assert rig.fake.calls_named("reqCurrentTime")
    # the account list is not passed to the consumer
    seen = []
    while (ev := rig.conn.next_event(0)) is not None:
        seen.append(ev)
    assert not any(isinstance(e, events.ManagedAccounts) for e in seen)
    assert any(isinstance(e, events.NextValidId) for e in seen)


def test_gateway_paper_port_is_accepted(build: Callable[..., Rig]) -> None:
    rig = build(paper(), port=4002)
    rig.conn.open()
    assert rig.conn.paper_verified is True


# -- anything else is refused, once, without a request -------------------------------------------


def assert_refused(rig: Rig, exc: NotPaperSession, secrets: list[str], caplog: Any) -> None:
    assert rig.conn.state is ConnState.CLOSED
    assert rig.conn.paper_verified is False
    assert len(rig.fakes) == 1
    assert rig.fake.connects == 1
    assert rig.fake.calls_named("disconnect")
    assert rig.requests_reaching_fakes() == []
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")
    assert rig.requests_reaching_fakes() == []
    assert "not a paper account session" in str(exc) and "nothing was requested" in str(exc)
    logged = caplog.text
    for secret in secrets:
        assert secret not in str(exc)
        assert secret not in logged
        assert secret not in repr(exc)
    rig.conn.close()  # a no-op after the refusal
    assert rig.conn.state is ConnState.CLOSED


@pytest.mark.parametrize("order", ["id_first", "accounts_first"])
@pytest.mark.parametrize("label", list(not_paper_ids()))
def test_non_paper_ids_are_refused(
    build: Callable[..., Rig], label: str, order: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ident = not_paper_ids()[label]
    rig = build(ident, order=order, attempts=3)
    with pytest.raises(NotPaperSession) as info:
        rig.conn.open()
    assert_refused(rig, info.value, [ident, DIGITS] if len(ident) > 2 else [], caplog)


def test_mixed_list_is_refused(build: Callable[..., Rig], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    live = "U" + OTHER_DIGITS
    rig = build(f"{paper()},{live}")
    with pytest.raises(NotPaperSession) as info:
        rig.conn.open()
    assert_refused(rig, info.value, [live, OTHER_DIGITS, paper(), DIGITS], caplog)
    assert "2 account id(s) listed, 1 failed" in str(info.value)


@pytest.mark.parametrize("accounts", ["", " , ,"])
def test_empty_list_is_refused(build: Callable[..., Rig], accounts: str, caplog: Any) -> None:
    rig = build(accounts)
    with pytest.raises(NotPaperSession) as info:
        rig.conn.open()
    assert_refused(rig, info.value, [], caplog)
    assert "0 account id(s) listed" in str(info.value)


def test_refusal_is_not_retried_or_swallowed(build: Callable[..., Rig]) -> None:
    assert not issubclass(NotPaperSession, ConnectFailed)
    assert not issubclass(NotPaperSession, OSError)
    assert issubclass(NotPaperSession, RuntimeError)
    rig = build("U" + DIGITS, attempts=5)
    caught: list[str] = []
    try:
        try:
            rig.conn.open()
        except ConnectFailed:
            caught.append("ConnectFailed")
        except OSError:
            caught.append("OSError")
    except NotPaperSession:
        caught.append("NotPaperSession")
    assert caught == ["NotPaperSession"]
    assert rig.conn.attempts_made == 1
    assert rig.fake.connects == 1


# -- the account list never arrives ------------------------------------------------------------


def test_missing_account_list_is_retried_then_connect_failed(build: Callable[..., Rig]) -> None:
    rig = build(paper(), order="no_accounts", attempts=2, step=1.0, ready_timeout=100)
    with pytest.raises(ConnectFailed) as info:
        rig.conn.open()
    assert "account list never arrived" in str(info.value)
    assert len(rig.fakes) == 2
    assert all(f.calls_named("disconnect") for f in rig.fakes)
    assert rig.conn.state is ConnState.CLOSED
    assert rig.conn.paper_verified is False
    assert rig.requests_reaching_fakes() == []


# -- ports ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("port", [7496, 4001, 7498, 4000, 1234])
@pytest.mark.parametrize("env", [{}, {"STOINK_ALLOW_LIVE_PORT": "1"}])
def test_non_paper_port_is_refused_before_any_client(
    build: Callable[..., Rig], port: int, env: dict[str, str]
) -> None:
    rig = build(paper(), port=port, env=env)
    with pytest.raises(NotPaperSession):
        rig.conn.open()
    assert rig.fakes == []  # the factory was never called
    assert rig.conn.state is ConnState.CLOSED
    assert rig.conn.attempts_made == 0


def test_live_port_without_require_paper_is_unchanged(build: Callable[..., Rig]) -> None:
    from MainStoinker.Util.broker_guard import BrokerPortRefused

    rig = build(paper(), port=7496, require_paper=False)
    with pytest.raises(BrokerPortRefused):
        rig.conn.open()
    assert rig.fakes == []


# -- a later list while READY ------------------------------------------------------------------


def test_later_failing_list_closes_the_connection(
    build: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = build(paper())
    rig.conn.open()
    assert rig.conn.paper_verified is True
    live = "U" + OTHER_DIGITS
    rig.fake.push("managedAccounts", live)
    assert wait_until(lambda: rig.conn.state is ConnState.CLOSED)
    assert rig.conn.paper_verified is False
    assert wait_until(lambda: rig.fake.calls_named("disconnect") != [])
    seen = []
    assert wait_until(lambda: seen.append(rig.conn.next_event(0)) or any(
        isinstance(e, events.ConnectionClosed) for e in seen))
    with pytest.raises(NotConnected):
        rig.conn.request("reqCurrentTime")
    assert rig.requests_reaching_fakes() == []
    assert live not in caplog.text and OTHER_DIGITS not in caplog.text
    assert not any(isinstance(e, events.ManagedAccounts) for e in seen if e is not None)


def test_later_passing_list_keeps_the_connection(build: Callable[..., Rig]) -> None:
    rig = build(paper())
    rig.conn.open()
    rig.fake.push("managedAccounts", paper(OTHER_DIGITS))
    rig.fake.push("error", -1, 0, 2104, "Market data farm connection is OK:usfarm", "")
    assert rig.conn.wait_for_data_farm(WAIT)  # the second push was delivered after the first
    assert rig.conn.state is ConnState.READY
    assert rig.conn.paper_verified is True


# -- READY is decided in one place ---------------------------------------------------------------


def gate_grants_ready(module: types.ModuleType, accounts_ok: bool) -> bool:
    """Ask the module's state-change method for READY for a live attempt with a running reader."""
    conn = module.IbConnection("127.0.0.1", 7497, 21, env={}, require_paper=True)
    att = module._Attempt(1)
    stop = threading.Event()
    reader = threading.Thread(target=stop.wait, daemon=True)
    reader.start()
    try:
        att.reader = reader
        att.accounts_ok = accounts_ok
        conn._att = att
        conn._phase = module.ConnState.CONNECTING
        return bool(conn._set_phase(module.ConnState.READY, att))
    finally:
        stop.set()
        reader.join(WAIT)


def test_set_phase_needs_the_paper_check() -> None:
    assert gate_grants_ready(connection_module, accounts_ok=False) is False
    assert gate_grants_ready(connection_module, accounts_ok=True) is True
    assert isinstance(_Attempt(1).accounts_ok, bool)


def test_mutant_without_the_paper_condition_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    source = SOURCE.read_text(encoding="utf-8")
    needle = "self._require_paper and not (att.accounts_ok and att.accounts_bad is None)"
    assert source.count(needle) == 1
    mutant_source = source.replace(needle, "False")
    mutant = types.ModuleType("mutant_ib_connection")
    monkeypatch.setitem(sys.modules, mutant.__name__, mutant)
    exec(compile(mutant_source, "mutant_ib_connection.py", "exec"), mutant.__dict__)  # noqa: S102
    assert gate_grants_ready(mutant, accounts_ok=False) is True  # the mutant grants READY ...
    # ... which the gate test above treats as a failure: it asserts False for the real module.


def test_phase_is_assigned_only_in_init_and_set_phase() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    owners: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, ast.AnnAssign) else [])
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_phase":
                    owners.add(func.name)
    assert owners == {"__init__", "_set_phase"}


# -- scripted handshakes: a failing list is sticky and wins over every other failure --------------


def good_list() -> str:
    return paper()


def bad_list() -> str:
    return "U" + OTHER_DIGITS


class ScriptFake(FakeIbClient):
    """The fake with its handshake replaced: `script` is a list of (callback, args) that the
    run thread delivers, in that order, after the fake's own connectAck."""

    def __init__(self, *args: Any, script: list[tuple[str, tuple[Any, ...]]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.script = script

    def connect(self, host: Any, port: Any, clientId: Any) -> None:  # noqa: N803
        super().connect(host, port, clientId)
        items: list[Any] = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        for item in items:
            if item[0] not in ("nextValidId", "managedAccounts"):
                self._q.put(item)
        for item in self.script:
            self._q.put(item)


NEXT_ID: tuple[str, tuple[Any, ...]] = ("nextValidId", (1,))


def accounts_item(text: str) -> tuple[str, tuple[Any, ...]]:
    return ("managedAccounts", (text,))


@pytest.fixture
def scripted(
    request: pytest.FixtureRequest,
) -> Callable[..., tuple[IbConnection, list[ScriptFake], StepClock]]:
    def make(
        script: list[tuple[str, tuple[Any, ...]]],
        cls: type[IbConnection] = IbConnection,
        **conn_kw: Any,
    ) -> tuple[IbConnection, list[ScriptFake], StepClock]:
        clock = StepClock()
        fakes: list[ScriptFake] = []

        def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> Any:
            fake = ScriptFake(DataOnlyClient(env, on_event, on_raw, logger), script=script)
            fakes.append(fake)
            return fake

        conn = cls(
            "127.0.0.1", 7497, 21, env={}, client_factory=factory, clock=clock,
            sleep=clock.sleep, poll_interval=0.002, require_paper=True, **conn_kw,
        )
        request.addfinalizer(conn.close)
        return conn, fakes, clock

    return make


def sent(fakes: list[ScriptFake]) -> list[str]:
    return [n for f in fakes for n in f.call_names if n in REQUEST_ARGS]


@pytest.mark.parametrize("repeat", range(8))
@pytest.mark.parametrize(
    "order",
    [
        "bad good id",
        "bad id good",
        "id bad good",
        "good bad id",
        "good id bad",
        "id good bad",
    ],
)
def test_a_failing_list_is_sticky_in_every_order(
    scripted: Callable[..., Any], order: str, repeat: int
) -> None:
    parts = {
        "bad": accounts_item(bad_list()),
        "good": accounts_item(good_list()),
        "id": NEXT_ID,
    }
    conn, fakes, _clock = scripted([parts[p] for p in order.split()], attempts=3)
    try:
        conn.open()
        outcome = "ready"
    except NotPaperSession:
        outcome = "not paper"
    if outcome == "ready":
        # Only possible when the failing list was the last message: it then closes the session.
        assert order.split()[-1] == "bad"
        assert wait_until(lambda: conn.state is ConnState.CLOSED)
        assert conn.paper_verified is False
        with pytest.raises(NotConnected):
            conn.request("reqCurrentTime")
    else:
        assert len(fakes) == 1
        assert conn.state is ConnState.CLOSED
    assert conn.paper_verified is False
    assert sent(fakes) == []


class InjectingConnection(IbConnection):
    """Delivers a failing list on the first empty wait of the ready loop, then ends the wait
    in the way `mode` names, so the opener sees the failure only after the list was recorded."""

    mode = "deadline"
    clock: StepClock

    def _pop(self, att: _Attempt, wait: float) -> Any:  # type: ignore[override]
        injected = getattr(att, "injected", False)
        if injected and self.mode == "reader_ended" and wait <= 0:
            return None
        event = IbConnection._pop(att, wait)
        if injected or event is not None or wait <= 0:
            return event
        att.injected = True  # type: ignore[attr-defined]
        self._on_event(att, events.ManagedAccounts(bad_list()))
        if self.mode == "deadline":
            self.clock.now += 1000.0
            return None
        if self.mode == "reader_ended":
            att.client.disconnect()
            assert att.reader is not None
            att.reader.join(WAIT)
            return None
        if self.mode == "reader_stopped":
            return connection_module.ReaderStopped(RuntimeError("boom"))
        return events.IbError(-1, 326, "client id in use")


@pytest.mark.parametrize("mode", ["deadline", "reader_ended", "reader_stopped", "client_id_in_use"])
def test_failing_list_beats_every_other_failure_of_the_attempt(
    scripted: Callable[..., Any], mode: str
) -> None:
    conn, fakes, clock = scripted([], cls=InjectingConnection, attempts=3, ready_timeout=1.0)
    conn.mode = mode
    conn.clock = clock
    with pytest.raises(NotPaperSession) as info:
        conn.open()
    assert not isinstance(info.value, ConnectFailed)
    assert len(fakes) == 1  # no second attempt, no reconnect to the same TWS
    assert conn.attempts_made == 1
    assert fakes[0].calls_named("disconnect")
    assert conn.state is ConnState.CLOSED
    assert conn.paper_verified is False
    assert sent(fakes) == []
    assert bad_list() not in str(info.value) and OTHER_DIGITS not in str(info.value)


def test_a_close_during_open_still_wins_over_a_failing_list(scripted: Callable[..., Any]) -> None:
    from MainStoinker.ib.connection import ConnectionClosedDuringOpen

    class Closing(InjectingConnection):
        def _pop(self, att: _Attempt, wait: float) -> Any:  # type: ignore[override]
            event = super()._pop(att, wait)
            if getattr(att, "injected", False):
                self._close_evt.set()
            return event

    conn, fakes, clock = scripted([], cls=Closing, attempts=3, ready_timeout=1.0)
    conn.mode = "deadline"
    conn.clock = clock
    with pytest.raises(ConnectionClosedDuringOpen):
        conn.open()
    assert len(fakes) == 1


# -- no request reaches the client once a failing list is recorded ---------------------------------


class Hook:
    def __init__(self) -> None:
        self.before_request: Callable[[], None] = lambda: None

    def on_request(self, name: str, args: tuple[Any, ...]) -> None:
        self.before_request()

    def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
        pass


def test_failing_list_between_the_last_check_and_the_call_stops_the_request(
    scripted: Callable[..., Any],
) -> None:
    hook = Hook()
    conn, fakes, _clock = scripted([NEXT_ID, accounts_item(good_list())], observers=[hook])
    conn.open()
    assert conn.paper_verified is True

    def deliver_failing_list_from_another_thread() -> None:
        t = threading.Thread(target=fakes[0].wrapper.managedAccounts, args=(bad_list(),))
        t.start()
        t.join(WAIT)
        assert not t.is_alive()
        assert conn.state is ConnState.CLOSED

    hook.before_request = deliver_failing_list_from_another_thread
    with pytest.raises(NotConnected):
        conn.request("reqCurrentTime")
    assert fakes[0].calls_named("reqCurrentTime") == []
    with pytest.raises(NotConnected):
        conn.request("reqCurrentTime")
    assert fakes[0].calls_named("reqCurrentTime") == []


def test_failing_list_delivered_inside_the_client_call_does_not_deadlock(
    scripted: Callable[..., Any],
) -> None:
    conn, fakes, _clock = scripted([NEXT_ID, accounts_item(good_list())])
    conn.open()
    fake = fakes[0]
    done = threading.Event()
    results: list[object] = []

    def call_that_delivers_on_the_calling_thread() -> None:
        fake.wrapper.managedAccounts(bad_list())  # same thread, inside the request

    fake.reqCurrentTime = call_that_delivers_on_the_calling_thread  # type: ignore[method-assign]

    def worker() -> None:
        try:
            results.append(conn.request("reqCurrentTime"))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert done.wait(WAIT), "request() deadlocked"
    assert results == [None]  # it had already passed the last check when the list arrived
    assert conn.state is ConnState.CLOSED
    assert conn.paper_verified is False
    with pytest.raises(NotConnected):
        conn.request("reqCurrentTime")
