"""A scripted fake of ibapi's ``EClient``, for the live-side tests.

It is a plain class, not a subclass of ``EClient``: it has the ``EClient``
method names and signatures the data-only code uses and no order-capable
method of any kind. It never imports ``ibapi.connection`` and never opens a
socket, so it needs no exception from the pytest broker block; it is injected
through the client factory, while the real factory stays blocked.

Behaviour follows ibapi 10.37 (``client.py``, ``connection.py``, ``reader.py``):

* ``connect`` returns once the handshake is done. ``wrapper.connectAck()`` is
  called on the calling thread, last, before ``connect`` returns (``client.py``
  348-354: after ``startApi``). Everything TWS says after the handshake
  (``nextValidId``, ``managedAccounts``, the 21xx notices) is queued and
  delivered by ``run``.
* A refused connection (``REFUSE``; also a DNS failure or a connect timeout) is
  one ``error(-1, t, 502, text)`` and then one ``connectionClosed``, both on the
  calling thread, before ``connect`` returns (``connection.py`` 44-47 swallows
  the refusal, ``sendMsg`` at ``client.py`` 317 raises, the ``except socket.error``
  at 355-359 reports 502 and calls ``disconnect()``). ``isConnected()`` is False
  afterwards and a later ``disconnect()`` reports nothing.
* ``HANG`` (TWS accepted the socket but never answers the handshake) blocks
  ``connect`` until ``disconnect()`` is called from another thread. Real ibapi
  then raises ``AttributeError`` on the connecting thread (``reset()`` has set
  ``conn`` to None, and the handshake loop at ``client.py`` 325-326 uses it), so
  the fake raises ``AttributeError`` from ``connect`` too, after the
  ``connectionClosed`` the disconnecting thread reported. If the test never
  releases the hang, ``hang_timeout`` ends it with ``HangNeverReleased`` (an
  ``AssertionError``): a silent timeout would let code that never disconnects pass.
* ``CLOSED_IN_HANDSHAKE`` (TWS accepts the socket and closes it during the
  handshake): ``connect`` returns silently (``client.py`` 326-331: ``reset()``
  and ``return``): no 502, no ``connectAck``, no ``connectionClosed``;
  ``isConnected()`` is False, the client holds no connection, and a later
  ``disconnect()`` reports nothing.
* A second ``connect`` while the client still holds a connection (before
  ``disconnect()``: hung, connected or peer-closed) raises ``AssertionError``.
  Real ibapi has no "already connected" error (``ALREADY_CONNECTED`` in
  ``errors.py`` is never used); it silently opens a second socket and orphans
  the first, so a retry that forgets ``disconnect()`` must fail loudly here.
* ``connectionClosed`` is called once per connection, by ``EClient.disconnect``
  and on the thread that runs it: the caller's thread for ``disconnect()``, the
  ``run`` thread for the ``finally`` of ``run()``. ``Connection.disconnect``
  never reports it, because ``EClient`` never sets ``Connection.wrapper``. A
  later ``disconnect()`` of a disconnected client reports nothing.
* A socket closed by the peer (``drop("connectionClosed")``, the end of the
  CLIENT_ID_IN_USE script) makes ``isConnected()`` False at once, as the real
  reader thread does; ``run`` then drains what is queued, ends, and its
  ``disconnect()`` reports ``connectionClosed``. A peer close does not reset the
  client, so draining works. After a caller ``disconnect()`` (which does reset,
  so ``serverVersion()`` is None) ``run`` raises ``TypeError`` as soon as it
  dequeues a message (``client.py`` 426), as real ibapi does; the message is lost.
* The error a request answers when not connected (504) carries the request id
  for ``reqMktData``, ``cancelMktData`` and ``reqHistoricalData`` and
  ``NO_VALID_ID`` for the others, as in ``client.py``; it comes on the calling
  thread. The client-side errors the fake raises itself (502, 504) pass four
  arguments, as ``client.py`` does; errors TWS sends (326, 1100, 21xx) pass five,
  as ``decoder.py`` does.
* Everything else the fake plays is delivered on the thread that runs
  ``run()``. A callback's arguments are deep-copied at delivery.
* Callback names and argument lists are checked against ``EWrapper`` when they
  are queued, so a wrong script fails loudly in the test that wrote it.

Known differences from real ibapi: the fake has no reader thread and no socket
timing, so a peer close happens on the thread that calls ``drop`` (or
``connect``); for CLIENT_ID_IN_USE ``isConnected()`` is already False when
``connect`` returns, where real TWS closes asynchronously and a poll right after
``connect`` usually still sees True. ``disconnect()`` claims the connection
atomically, so two threads disconnecting at once report one ``connectionClosed``
in total, where real ibapi can report two or raise. A hung connect released by
``disconnect()`` always raises ``AttributeError`` in the fake. Real ibapi may
either raise it or return cleanly with ``isConnected()`` False: which happens
depends on timing and on what the wrapper's ``connectionClosed`` handler does
(the connecting thread races it and ``reset()``). Code under test must handle
both.``host``, ``port`` and ``clientId`` are kept after a disconnect
(real ``reset()`` clears them). The message that makes ``run`` raise
``TypeError`` is dropped; no other decoder failure (``BadMessage``, over-long
messages) is modelled. The 1101, 1102 and 2110 texts of ``drop`` are IB's
documented wording, not taken from ibapi, which has no such table.

Nothing here sleeps. ``run`` blocks on a queue with a short timeout only to
notice a disconnect; a hang is a ``threading.Event`` released by
``disconnect``. Use ``run_in_thread`` and ``close`` (or the context manager) to
keep every thread joined.

Not safe to call ``run`` from two threads at once.
"""
import copy
import enum
import functools
import inspect
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData, TagValueList, TickerId
from ibapi.const import NO_VALID_ID
from ibapi.contract import Contract
from ibapi.errors import CONNECT_FAIL, NOT_CONNECTED
from ibapi.wrapper import EWrapper

FARM_MESSAGES: tuple[tuple[int, str], ...] = (
    (2104, "Market data farm connection is OK:usfarm"),
    (2106, "HMDS data farm connection is OK:ushmds"),
    (2158, "Sec-def data farm connection is OK:secdefil"),
)
CLIENT_ID_IN_USE = 326
CONNECTION_LOST = 1100
DEFAULT_SERVER_VERSION = 222
DEFAULT_ACCOUNTS = "DU0000000"

# The EClient methods this fake implements. Nothing else of EClient exists on it.
IMPLEMENTED = (
    "connect",
    "disconnect",
    "isConnected",
    "run",
    "serverVersion",
    "reqHistoricalData",
    "cancelHistoricalData",
    "reqContractDetails",
    "reqHeadTimeStamp",
    "cancelHeadTimeStamp",
    "reqMktData",
    "cancelMktData",
    "reqMarketDataType",
    "reqCurrentTime",
)
# Requests whose not-connected 504 carries the request id (client.py); the rest use -1.
_504_WITH_REQ_ID = frozenset({"reqMktData", "cancelMktData", "reqHistoricalData"})
# Methods that need a connection: made while not connected they answer error 504.
_NEEDS_CONNECTION = frozenset(IMPLEMENTED) - {
    "connect",
    "disconnect",
    "isConnected",
    "run",
    "serverVersion",
}

_WAKE = "__wake__"  # internal queue item: re-check the loop condition


class HangNeverReleased(AssertionError):
    """A HANG connect reached ``hang_timeout``: the code under test never called disconnect()."""


# What TWS says for the connectivity codes ``drop`` can push (IB's documented wording).
DROP_TEXTS: dict[int, str] = {
    1100: "Connectivity between IB and Trader Workstation has been lost.",
    1101: "Connectivity between IB and TWS has been restored- data lost.*",
    1102: "Connectivity between IB and TWS has been restored- data maintained.",
    2110: "Connectivity between Trader Workstation and server is broken. "
    "It will be restored automatically.",
}


class ConnectBehavior(enum.Enum):
    ACCEPT = "accept"
    REFUSE = "refuse"  # error 502, then connectionClosed, both from connect()
    HANG = "hang"  # connect() blocks until disconnect() (TWS accept dialog open)
    CLIENT_ID_IN_USE = "client_id_in_use"  # error 326, then the socket closes
    NO_NEXT_VALID_ID = "no_next_valid_id"  # accepted, but nextValidId never comes
    CLOSED_IN_HANDSHAKE = "closed_in_handshake"  # socket accepted, then closed: silent return


@dataclass(frozen=True)
class Call:
    """One recorded call: the method name and its arguments in signature order."""

    name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Delivery:
    """One callback that reached the wrapper, and the thread that delivered it."""

    name: str
    args: tuple[Any, ...]
    thread_id: int


Played = tuple[Any, ...]  # (callback_name, *callback_args)
Handler = Callable[..., Iterable[Played] | None]


def make_bar(
    epoch: int | str,
    o: float,
    h: float,
    l: float,  # noqa: E741 - open/high/low/close, as everywhere in this repo
    c: float,
    volume: int | str | Decimal,
) -> BarData:
    """A ``BarData`` as ibapi delivers it with formatDate 2: epoch date, Decimal volume."""
    bar = BarData()
    bar.date = str(int(epoch))
    bar.open = o
    bar.high = h
    bar.low = l
    bar.close = c
    bar.volume = volume if isinstance(volume, Decimal) else Decimal(volume)
    bar.wap = Decimal(0)
    bar.barCount = 0
    return bar


def _check_callback(name: str, args: tuple[Any, ...]) -> None:
    fn = getattr(EWrapper, name, None)
    if fn is None or name.startswith("_"):
        raise AttributeError(f"EWrapper has no callback {name!r}")
    try:
        inspect.signature(fn).bind(None, *args)
    except TypeError as exc:
        raise TypeError(f"callback {name}{args!r} does not match EWrapper: {exc}") from exc


def _recorded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Record a call, and answer 504 when it needs a connection and there is none."""
    sig = inspect.signature(fn)
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(self: "FakeIbClient", *a: Any, **k: Any) -> Any:
        bound = sig.bind(self, *a, **k)
        bound.apply_defaults()
        args = tuple(copy.deepcopy(v) for v in list(bound.args)[1:])
        self._record(Call(name, args))
        if name in _NEEDS_CONNECTION and not self._connected():
            req_id = args[0] if name in _504_WITH_REQ_ID else NO_VALID_ID
            self._deliver(
                "error", (req_id, self._clock_ms(), NOT_CONNECTED.code(), NOT_CONNECTED.msg())
            )
            return None
        result = fn(self, *bound.args[1:], **bound.kwargs)
        if name in _NEEDS_CONNECTION:
            self._play_response(name, args)
        return result

    return wrapper


class FakeIbClient:
    """Scripted stand-in for ``EClient``; see the module docstring."""

    def __init__(
        self,
        wrapper: EWrapper,
        *,
        connect_behavior: ConnectBehavior | Iterable[ConnectBehavior] = ConnectBehavior.ACCEPT,
        next_valid_id: int = 1,
        managed_accounts: str = DEFAULT_ACCOUNTS,
        server_version: int = DEFAULT_SERVER_VERSION,
        clock_ms: Callable[[], int] | None = None,
        hang_timeout: float = 30.0,
        farm_messages: Iterable[tuple[int, str]] = FARM_MESSAGES,
    ) -> None:
        self.wrapper = wrapper
        self.farm_messages = tuple(farm_messages)
        self.host: str | None = None
        self.port: int | None = None
        self.clientId: int | None = None
        self.connState = EClient.DISCONNECTED
        self.next_valid_id = next_valid_id
        self.managed_accounts = managed_accounts
        self.hang_timeout = hang_timeout
        # Cleared at each connect() entry, set when a HANG connect starts waiting. For a
        # script with several hangs use hangs_entered / wait_for_hang(n), which count.
        self.connect_entered = threading.Event()
        self.hangs_entered = 0
        self._hang_cond = threading.Condition()
        self.calls: list[Call] = []
        self.delivered: list[Delivery] = []
        self.connects = 0
        # isConnected() and serverVersion() are polled, so they are counted, not recorded.
        self.is_connected_calls = 0
        self.server_version_calls = 0

        self._server_version = server_version
        self._server_version_now: int | None = None
        self._clock = clock_ms or (lambda: 1_700_000_000_000)
        self._lock = threading.RLock()
        self._q: queue.Queue[Any] = queue.Queue()
        self._conn = False  # EClient.conn is not None: from connect() to disconnect()
        self._sock = False  # the socket is open (Connection.isConnected)
        self._release = threading.Event()
        self._behaviors: list[ConnectBehavior] = []
        self._handlers: dict[str, Handler | list[Played]] = {}
        self._threads: list[threading.Thread] = []
        self.run_errors: list[BaseException] = []  # what ended run_in_thread threads
        self._expected_run_errors: list[type[BaseException]] = []
        self.script_connect(connect_behavior)

    # -- scripting -----------------------------------------------------------
    def script_connect(self, *behaviors: ConnectBehavior | Iterable[ConnectBehavior]) -> None:
        """Set the behaviour of the next connects, one per attempt; the last one repeats."""
        flat: list[ConnectBehavior] = []
        for b in behaviors:
            flat.extend([b] if isinstance(b, ConnectBehavior) else list(b))
        if not flat:
            raise ValueError("script_connect needs at least one behavior")
        with self._lock:
            self._behaviors = flat

    def respond(self, method: str, handler: Handler | list[Played]) -> None:
        """Play callbacks when ``method`` is called.

        ``handler`` is called on the caller's thread with the request's arguments in
        signature order and returns ``(callback_name, *callback_args)`` tuples (or
        None); a plain list is played unchanged each time. The callbacks are
        delivered by ``run()``.
        """
        if method not in _NEEDS_CONNECTION:
            raise ValueError(f"{method!r} is not a request the fake answers")
        if not callable(handler):
            for item in handler:
                _check_callback(item[0], tuple(item[1:]))
        with self._lock:
            self._handlers[method] = handler

    def push(self, callback: str, *args: Any) -> None:
        """Queue one wrapper callback, to be delivered by ``run()`` (call from the test thread)."""
        _check_callback(callback, args)
        self._q.put((callback, args))

    def drop(self, code: int | str = CONNECTION_LOST, text: str | None = None) -> None:
        """Simulate the connection going away.

        An int pushes ``error(-1, t, code, text, "")`` with IB's wording for 1100,
        1101, 1102 and 2110 (any other code needs ``text``); ``"connectionClosed"``
        closes the socket under the client: ``isConnected()`` is False at once and
        ``run()`` drains its queue, delivers ``connectionClosed`` (once) and returns.
        """
        if code == "connectionClosed":
            self._peer_close()
            return
        if not isinstance(code, int):
            raise ValueError(f"drop() takes an IB error code or 'connectionClosed', not {code!r}")
        if text is None:
            if code not in DROP_TEXTS:
                raise ValueError(f"drop({code}) needs a text; known codes: {sorted(DROP_TEXTS)}")
            text = DROP_TEXTS[code]
        self.push("error", NO_VALID_ID, self._clock_ms(), code, text, "")

    def wait_for_hang(self, count: int = 1, timeout: float = 5.0) -> bool:
        """Wait until ``count`` HANG connects in total have started waiting."""
        with self._hang_cond:
            return self._hang_cond.wait_for(lambda: self.hangs_entered >= count, timeout)

    # -- recorded inspection helpers -----------------------------------------
    def calls_named(self, name: str) -> list[Call]:
        with self._lock:
            return [c for c in self.calls if c.name == name]

    @property
    def call_names(self) -> list[str]:
        with self._lock:
            return [c.name for c in self.calls]

    def run_in_thread(self) -> threading.Thread:
        """Run ``run()`` on a daemon thread that ``close`` joins.

        An exception ``run()`` raises (such as the ``TypeError`` real ibapi raises
        for a message dequeued after a caller ``disconnect()``) is appended to
        ``run_errors``. ``close()`` raises if one is there that the test did not
        announce with ``expect_run_error``.
        """

        def target() -> None:
            try:
                self.run()
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised by close()
                with self._lock:
                    self.run_errors.append(exc)

        t = threading.Thread(target=target, name="fake-ib-run", daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def expect_run_error(self, *types: type[BaseException]) -> None:
        """Declare that a run thread may end with one of ``types``; ``close`` then accepts it."""
        with self._lock:
            self._expected_run_errors.extend(types)

    def close(self, timeout: float = 5.0, *, check_run_errors: bool = True) -> None:
        """Disconnect and join every thread started by ``run_in_thread``.

        Raises ``AssertionError`` if a run thread ended with an exception that is not
        an instance of a type given to ``expect_run_error``. ``run_errors`` is cleared.
        """
        self._disconnect()
        for t in self._threads:
            t.join(timeout)
            if t.is_alive():  # pragma: no cover - only on a broken test
                raise RuntimeError(f"thread {t.name} did not end within {timeout}s")
        self._threads.clear()
        with self._lock:
            errors, self.run_errors = self.run_errors, []
            expected = tuple(self._expected_run_errors)
        unexpected = [e for e in errors if not isinstance(e, expected)]
        if unexpected and check_run_errors:
            raise AssertionError(
                f"run() on a thread ended with an unexpected exception: {unexpected[0]!r}; "
                "drain the queue before close() or call expect_run_error()"
            ) from unexpected[0]

    def __enter__(self) -> "FakeIbClient":
        return self

    def __exit__(self, exc_type: object, *exc: object) -> None:
        # While another exception propagates, do not mask it with a run-thread error.
        self.close(check_run_errors=exc_type is None)

    # -- EClient surface -----------------------------------------------------
    @_recorded
    def connect(self, host, port, clientId):
        self.host, self.port, self.clientId = host, port, clientId
        with self._lock:
            if self._conn:
                raise AssertionError(
                    "connect() called again before disconnect(): real ibapi has no 'already "
                    "connected' error (ALREADY_CONNECTED in errors.py is never used); it would "
                    "silently open a second socket and orphan the first, so the code under test "
                    "must disconnect() before it connects again"
                )
            self.connects += 1
            behavior = self._behaviors[min(self.connects, len(self._behaviors)) - 1]
            self._release.clear()
            self.connect_entered.clear()
            self._conn = True  # client.py 299: self.conn = Connection(...) comes first
            self.connState = EClient.CONNECTING
        if behavior is ConnectBehavior.HANG:
            with self._lock:
                self._sock = True  # TCP is up; TWS has not answered the handshake
            with self._hang_cond:
                self.connect_entered.set()
                self.hangs_entered += 1
                self._hang_cond.notify_all()
            if not self._release.wait(self.hang_timeout):  # safety valve, not ibapi behaviour
                self._reset()
                raise HangNeverReleased(
                    f"connect() hung for {self.hang_timeout}s and the test never released it: "
                    "the code under test must call disconnect() to end a hung connect"
                )
            # client.py 325-326: disconnect() has reset() the client, so conn is None here.
            raise AttributeError("'NoneType' object has no attribute 'isConnected'")
        if behavior is ConnectBehavior.REFUSE:
            # client.py 355-359: error 502 (four arguments), then disconnect() -> connectionClosed.
            self._deliver(
                "error", (NO_VALID_ID, self._clock_ms(), CONNECT_FAIL.code(), CONNECT_FAIL.msg())
            )
            self._disconnect()
            return
        if behavior is ConnectBehavior.CLOSED_IN_HANDSHAKE:
            # client.py 326-331: recvMsg() saw the close; reset() and return, silently.
            self._reset()
            return
        with self._lock:
            self._sock = True
            self._server_version_now = self._server_version
            self.connState = EClient.CONNECTED
        if behavior is ConnectBehavior.CLIENT_ID_IN_USE:
            self.push(
                "error",
                NO_VALID_ID,
                self._clock_ms(),
                CLIENT_ID_IN_USE,
                "Duplicate client id. Client id is already in use.",
                "",
            )
        else:
            if behavior is not ConnectBehavior.NO_NEXT_VALID_ID:
                self.push("nextValidId", self.next_valid_id)
            self.push("managedAccounts", self.managed_accounts)
            for code, text in self.farm_messages:
                self.push("error", NO_VALID_ID, self._clock_ms(), code, text, "")
        # client.py: connectAck is the last thing connect() does, on this thread.
        self._deliver("connectAck", ())
        if behavior is ConnectBehavior.CLIENT_ID_IN_USE:
            self._peer_close()  # TWS closes right after the error; run() will report it

    @_recorded
    def disconnect(self):
        self._disconnect()

    def isConnected(self):
        with self._lock:
            self.is_connected_calls += 1
        return self._connected()

    @_recorded
    def run(self):
        try:
            while self._connected() or not self._q.empty():
                try:
                    name, args = self._q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if name == _WAKE:
                    continue
                with self._lock:
                    version = self._server_version_now
                if version is None:
                    # client.py 426: reset() by a caller disconnect() left serverVersion_
                    # None, and `None >= MIN_SERVER_VER_PROTOBUF` raises. The message is lost.
                    raise TypeError(
                        "'>=' not supported between instances of 'NoneType' and 'int'"
                    )
                self._deliver(name, args)
        finally:
            self._disconnect()

    def serverVersion(self):
        with self._lock:
            self.server_version_calls += 1
            return self._server_version_now

    @_recorded
    def reqCurrentTime(self):
        return None

    @_recorded
    def reqMktData(
        self,
        reqId: TickerId,
        contract: Contract,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: TagValueList,
    ):
        return None

    @_recorded
    def cancelMktData(self, reqId: TickerId):
        return None

    @_recorded
    def reqMarketDataType(self, marketDataType: int):
        return None

    @_recorded
    def reqContractDetails(self, reqId: int, contract: Contract):
        return None

    @_recorded
    def reqHistoricalData(
        self,
        reqId: TickerId,
        contract: Contract,
        endDateTime: str,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: int,
        formatDate: int,
        keepUpToDate: bool,
        chartOptions: TagValueList,
    ):
        return None

    @_recorded
    def cancelHistoricalData(self, reqId: TickerId):
        return None

    @_recorded
    def reqHeadTimeStamp(
        self,
        reqId: TickerId,
        contract: Contract,
        whatToShow: str,
        useRTH: int,
        formatDate: int,
    ):
        return None

    @_recorded
    def cancelHeadTimeStamp(self, reqId: TickerId):
        return None

    # -- internals -----------------------------------------------------------
    def _clock_ms(self) -> int:
        return int(self._clock())

    def _connected(self) -> bool:
        """EClient.isConnected: state CONNECTED and the socket still open."""
        with self._lock:
            return self.connState == EClient.CONNECTED and self._sock

    def _record(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)

    def _disconnect(self) -> None:
        """EClient.disconnect; ``run`` ends with it too, unrecorded.

        ``connectionClosed`` is reported once, on this thread, and only if the
        client still had a connection object (client.py: ``if self.conn is not None``).
        """
        with self._lock:  # claim the connection atomically: one thread reports
            had_conn = self._conn
            self._conn = False
            self._sock = False
            self.connState = EClient.DISCONNECTED
        # Real order (client.py 366-371): report connectionClosed, then reset().
        # A thread hung in connect() is released only by the reset, as in ibapi.
        if had_conn:
            self._deliver("connectionClosed", ())
        self._reset()

    def _reset(self) -> None:
        """EClient.reset(): no connection object, no server version, state DISCONNECTED."""
        with self._lock:
            self._conn = False
            self._sock = False
            self.connState = EClient.DISCONNECTED
            self._server_version_now = None
        self._release.set()
        self._q.put((_WAKE, ()))

    def _peer_close(self) -> None:
        """The peer closed the socket: the reader thread's part, which reports nothing."""
        with self._lock:
            self._sock = False
        self._q.put((_WAKE, ()))

    def _play_response(self, method: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            handler = self._handlers.get(method)
        if handler is None:
            return
        played = handler(*args) if callable(handler) else handler
        for item in played or ():
            self.push(item[0], *item[1:])

    def _deliver(self, name: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            args = copy.deepcopy(args)  # a handler list or a pushed object is never shared
            self.delivered.append(Delivery(name, args, threading.get_ident()))
        getattr(self.wrapper, name)(*args)


WAIT = 5.0  # an upper bound for a join or a queue get, never a sleep


# Every request the fake implements, with valid arguments (request id 77 where it has one).
REQUEST_ARGS: dict[str, tuple[Any, ...]] = {
    "reqCurrentTime": (),
    "reqMktData": (77, Contract(), "", False, False, []),
    "cancelMktData": (77,),
    "reqMarketDataType": (3,),
    "reqContractDetails": (77, Contract()),
    "reqHeadTimeStamp": (77, Contract(), "TRADES", 1, 2),
    "cancelHeadTimeStamp": (77,),
    "reqHistoricalData": (77, Contract(), "", "1 D", "1 min", "TRADES", 1, 2, True, []),
    "cancelHistoricalData": (77,),
}


class Recorder(EWrapper):
    """An EWrapper that puts every callback it gets on a queue, with its thread id."""

    def __init__(self) -> None:
        super().__init__()
        self.events: queue.Queue[tuple[str, tuple[Any, ...], int]] = queue.Queue()

    def _put(self, name: str, *args: Any) -> None:
        self.events.put((name, args, threading.get_ident()))

    def get(self) -> tuple[str, tuple[Any, ...], int]:
        return self.events.get(timeout=WAIT)

    def names(self, n: int) -> list[str]:
        return [self.get()[0] for _ in range(n)]

    def connectAck(self):
        self._put("connectAck")

    def nextValidId(self, orderId):
        self._put("nextValidId", orderId)

    def managedAccounts(self, accountsList):
        self._put("managedAccounts", accountsList)

    def connectionClosed(self):
        self._put("connectionClosed")

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        self._put("error", reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)

    def historicalData(self, reqId, bar):
        self._put("historicalData", reqId, bar)

    def historicalDataEnd(self, reqId, start, end):
        self._put("historicalDataEnd", reqId, start, end)

    def historicalDataUpdate(self, reqId, bar):
        self._put("historicalDataUpdate", reqId, bar)

    def contractDetails(self, reqId, contractDetails):
        self._put("contractDetails", reqId, contractDetails)

    def contractDetailsEnd(self, reqId):
        self._put("contractDetailsEnd", reqId)

    def tickPrice(self, reqId, tickType, price, attrib):
        self._put("tickPrice", reqId, tickType, price, attrib)

    def marketDataType(self, reqId, marketDataType):
        self._put("marketDataType", reqId, marketDataType)

    def currentTime(self, time):
        self._put("currentTime", time)

