"""A scripted fake of ibapi's ``EClient``, for the live-side tests.

It is a plain class, not a subclass of ``EClient``: it has the ``EClient``
method names and signatures the data-only code uses and no order-capable
method of any kind. It never imports ``ibapi.connection`` and never opens a
socket, so it needs no exception from the pytest broker block; it is injected
through the client factory, while the real factory stays blocked.

Behaviour follows ibapi 10.37 (``client.py``, ``connection.py``, ``reader.py``):

* ``connect`` returns once the handshake is done. ``wrapper.connectAck()`` is
  called on the calling thread, last, before ``connect`` returns (``client.py``:
  after ``startApi``). A refused connection is reported through
  ``wrapper.error(502)`` on the calling thread. Everything TWS says after the
  handshake (``nextValidId``, ``managedAccounts``, the 21xx notices) is queued
  and delivered by ``run``.
* ``connectionClosed`` is called exactly once per connection, by
  ``EClient.disconnect`` and on the thread that runs it: the caller's thread for
  ``disconnect()``, the ``run`` thread for the ``finally`` of ``run()``.
  ``Connection.disconnect`` never reports it, because ``EClient`` never sets
  ``Connection.wrapper``. A later ``disconnect()`` of an already disconnected
  client reports nothing.
* A socket closed by the peer (``drop("connectionClosed")``, the end of the
  CLIENT_ID_IN_USE script) makes ``isConnected()`` False at once, as the real
  reader thread does; ``run`` then drains what is queued, ends, and its
  ``disconnect()`` reports ``connectionClosed``. The one difference: the fake
  has no reader thread; the socket closes on the thread that calls ``drop`` (or
  ``connect``), and nothing is delivered from it.
* The error a request answers when not connected (504) carries the request id
  for ``reqMktData``, ``cancelMktData`` and ``reqHistoricalData`` and
  ``NO_VALID_ID`` for the others, as in ``client.py``; it comes on the calling
  thread.
* Everything else the fake plays is delivered on the thread that runs
  ``run()``. A callback's arguments are deep-copied at delivery.
* Callback names and argument lists are checked against ``EWrapper`` when they
  are queued, so a wrong script fails loudly in the test that wrote it.

Known differences: the connect-refused script reports 502 but no
``connectionClosed`` (real ``EClient.connect`` probably reports one, after a
second 502, but that depends on the OS socket and cannot be checked offline);
``disconnect()`` claims the connection atomically, so two threads
disconnecting at once report one ``connectionClosed`` in total, never a race.

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


class ConnectBehavior(enum.Enum):
    ACCEPT = "accept"
    REFUSE = "refuse"  # error 502 from connect()
    HANG = "hang"  # connect() blocks until disconnect() (TWS accept dialog open)
    CLIENT_ID_IN_USE = "client_id_in_use"  # error 326, then the socket closes
    NO_NEXT_VALID_ID = "no_next_valid_id"  # accepted, but nextValidId never comes


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
            self.wrapper.error(req_id, self._clock_ms(), NOT_CONNECTED.code(), NOT_CONNECTED.msg())
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
    ) -> None:
        self.wrapper = wrapper
        self.host: str | None = None
        self.port: int | None = None
        self.clientId: int | None = None
        self.connState = EClient.DISCONNECTED
        self.next_valid_id = next_valid_id
        self.managed_accounts = managed_accounts
        self.hang_timeout = hang_timeout
        self.connect_entered = threading.Event()  # set when a HANG connect starts waiting
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

    def drop(self, code: int | str = CONNECTION_LOST) -> None:
        """Simulate the connection going away.

        ``1100`` (or any int) pushes ``error(-1, t, code, ...)``; ``"connectionClosed"``
        closes the socket under the client: ``isConnected()`` is False at once and
        ``run()`` drains its queue, delivers ``connectionClosed`` (once) and returns.
        """
        if code == "connectionClosed":
            self._peer_close()
            return
        if not isinstance(code, int):
            raise ValueError(f"drop() takes an IB error code or 'connectionClosed', not {code!r}")
        text = "Connectivity between IB and Trader Workstation has been lost."
        self.push("error", NO_VALID_ID, self._clock_ms(), code, text, "")

    # -- recorded inspection helpers -----------------------------------------
    def calls_named(self, name: str) -> list[Call]:
        with self._lock:
            return [c for c in self.calls if c.name == name]

    @property
    def call_names(self) -> list[str]:
        with self._lock:
            return [c.name for c in self.calls]

    def run_in_thread(self) -> threading.Thread:
        """Run ``run()`` on a daemon thread that ``close`` joins."""
        t = threading.Thread(target=self.run, name="fake-ib-run", daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def close(self, timeout: float = 5.0) -> None:
        """Disconnect and join every thread started by ``run_in_thread``."""
        self._disconnect()
        for t in self._threads:
            t.join(timeout)
            if t.is_alive():  # pragma: no cover - only on a broken test
                raise RuntimeError(f"thread {t.name} did not end within {timeout}s")
        self._threads.clear()

    def __enter__(self) -> "FakeIbClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- EClient surface -----------------------------------------------------
    @_recorded
    def connect(self, host, port, clientId):
        self.host, self.port, self.clientId = host, port, clientId
        with self._lock:
            if self._connected():
                self.wrapper.error(NO_VALID_ID, self._clock_ms(), 501, "Already connected.", "")
                return
            self.connects += 1
            behavior = self._behaviors[min(self.connects, len(self._behaviors)) - 1]
            self._release.clear()
            self._conn = behavior is not ConnectBehavior.REFUSE
            self.connState = EClient.CONNECTING
        if behavior is ConnectBehavior.HANG:
            with self._lock:
                self._sock = True  # TCP is up; TWS has not answered the handshake
            self.connect_entered.set()
            if not self._release.wait(self.hang_timeout):  # safety valve, not ibapi behaviour
                with self._lock:
                    self._sock = self._conn = False
                    self.connState = EClient.DISCONNECTED
            return
        if behavior is ConnectBehavior.REFUSE:
            with self._lock:
                self.connState = EClient.DISCONNECTED
            self.wrapper.error(
                NO_VALID_ID, self._clock_ms(), CONNECT_FAIL.code(), CONNECT_FAIL.msg(), ""
            )
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
            for code, text in FARM_MESSAGES:
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
        with self._lock:
            had_conn = self._conn
            self._conn = False
            self._sock = False
            self.connState = EClient.DISCONNECTED
            self._server_version_now = None
        self._release.set()
        self._q.put((_WAKE, ()))
        if had_conn:
            self._deliver("connectionClosed", ())

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
