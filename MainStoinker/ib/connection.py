"""The IB connection layer: open, readiness, retry, state events, clean close.

`IbConnection` is built on `DataOnlyClient` (through an injectable client
factory, so tests pass the scripted fake) and adds the lifecycle the client
leaves out. It builds no `EClient` or `Connection` of its own, reads no
environment (the `env` mapping is passed in) and sends no order: its `request`
takes only an allowlist of data requests, and the client refuses the rest.

Facts about ibapi 10.37.2 the design rests on
---------------------------------------------
* `EClient.connect` has no timeout. If TWS accepts the socket and does not
  answer (its accept-incoming-connection dialog is open) the handshake loop
  never ends. So `connect` runs on a helper thread; after `connect_timeout` the
  caller breaks it with `disconnect()`. The released `connect` then raises
  `AttributeError` or returns cleanly with `isConnected()` False; both are
  handled, and only that attempt's `AttributeError` or `OSError` is accepted.
* `connect` returning means nothing. Success is `nextValidId` arriving (and the
  connection not having closed), within `ready_timeout`. A refused socket is
  error 502 followed by `connectionClosed`, both delivered before `connect`
  returns; TWS closing the socket during the handshake delivers nothing at all.
* An `EClient` keeps one message queue for its whole life and `reset()` does not
  clear it, so a message left over from a failed attempt would be decoded by the
  next attempt on the same client. Hence **one fresh client per attempt**: the
  factory is called once per attempt, the previous client is disconnected, its
  threads joined (bounded) and it is dropped, never used again.
* `run()` after a caller `disconnect()` with messages still queued raises
  `TypeError` on the run thread. The reader thread wraps `run()` and turns any
  exception into a `ReaderStopped` event.
* 1100, 1101, 1102 and 2110 arrive as `error()` callbacks while the socket
  stays up. 1100 marks the connection lost and nothing reconnects; 1101 means
  data was lost and every subscription must be requested again; 1102 means the
  subscriptions are kept. 326 (client id in use) is detected by its code and is
  never retried.
* `connectionClosed` arrives on the caller thread after a `disconnect()` and on
  the run thread after a peer close, and with the real client the two can race.
  At most one `ConnectionClosed` per attempt reaches the consumer.

The retry loop catches narrowly: `OSError` (sockets, timeouts) and the
documented outcomes of a hung handshake. `MessageRefused`, `BrokerPortRefused`
and anything else propagate from the first attempt (after a `disconnect()`),
including a refusal by the client's frame guard, which leaves the socket open.

Per-attempt state
-----------------
Everything that belongs to one attempt (client, threads, event queue, lost flag,
data-farm codes) lives in one small `_Attempt` object, created by `_begin_attempt`.
The callbacks given to the factory are bound to that object, so a late callback of
an old client (from any thread) lands in the old object and can reach neither the
current attempt's queue nor its state. When an attempt ends its object is marked
dead and ignores further callbacks.

If a thread of an old client (the connect helper or the reader) is still alive
`join_timeout` after its `disconnect()`, that is logged at warning level and recorded
in the failure list, and the next attempt goes on with a new client. More than
`MAX_STUCK_THREADS` such threads alive at once makes `open()` fail (`ConnectFailed`),
so that threads cannot pile up.

Threads: callbacks only put events on a queue; the caller thread consumes them
(`next_event`). Observers see every request and every raw callback, on the thread
where it happens, so they must be thread-safe and fast. **An observer must not call
back into the connection**: `request()` from an observer on the thread that is
inside `request()` raises `RuntimeError` (it would deadlock), and `close()` from an
observer is not supported either.

Waiting and backoff use the injected `clock` and `sleep`. The clock bounds every
timeout; waits for something to happen block for at most `poll_interval` real
seconds at a time, then look at the clock.

close() during open()
---------------------
`close()` may be called from another thread (a signal handler, a watchdog). Once it
has started, `open()` makes no further connect attempt, wakes from every wait within
`poll_interval` (the connect wait and the ready wait) or at once (the default
backoff sleep waits on an event that `close()` sets), disconnects whatever is open,
ends in state CLOSED and raises `ConnectionClosedDuringOpen`. `close()` itself waits,
bounded by `join_timeout`, until `open()` has finished that cleanup (when it is called
on another thread than `open()`, the reader or the connect helper); if that bound is
exceeded it logs an error naming what is still running. Whichever of `open()`, the
connect helper and `close()` sees a connected client after the close flag is set
disconnects it, so no connect that finishes late stays open. An injected `sleep` is
called as it is: it must be interruptible or short, or the abort waits for it.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from MainStoinker.ib import events
from MainStoinker.ib.client import DataOnlyClient, MessageRefused
from MainStoinker.Util.broker_guard import BrokerPortRefused, check_broker_port

DATA_REQUESTS: frozenset[str] = frozenset(
    {
        "reqMktData",
        "cancelMktData",
        "reqContractDetails",
        "reqHistoricalData",
        "cancelHistoricalData",
        "reqCurrentTime",
        "reqMarketDataType",
        "reqHeadTimeStamp",
        "cancelHeadTimeStamp",
    }
)
"""The only request methods `IbConnection.request` calls (`reqIds` is not one)."""

FARM_KINDS: dict[str, frozenset[int]] = {
    "market": frozenset({2104, 2108}),  # market data farm: OK / inactive but available
    "historical": frozenset({2106, 2107}),  # HMDS farm: OK / inactive but available
    "secdef": frozenset({2158}),  # security definition farm: OK
}
"""IB data-farm status codes by farm kind, for `IbConnection.wait_for_data_farm`."""

DATA_FARM_CODES: frozenset[int] = frozenset().union(*FARM_KINDS.values())
MAX_ATTEMPTS = 100
MAX_STUCK_THREADS = 3
"""More abandoned threads than this still alive makes `open()` fail."""
MAX_REQUESTS_PER_SECOND = 50  # IB documents 50 messages per second
MAX_CLIENT_ID = 2**31 - 1
CLIENT_ID_IN_USE = 326
CONNECTION_LOST = 1100
RESTORED_DATA_LOST = 1101
RESTORED_DATA_KEPT = 1102
REFUSED_CLIENT_IDS: frozenset[int] = frozenset({0, 123})
"""0 would take over orders placed by hand in TWS; 123 is the legacy code's id."""
MAX_SECONDS: dict[str, float] = {
    "connect_timeout": 300,
    "ready_timeout": 300,
    "retry_delay": 600,
    "max_retry_delay": 600,
    "poll_interval": 10,
    "join_timeout": 60,
}
"""Upper bounds (seconds) of the constructor's timeouts and delays."""


class ConnState(enum.Enum):
    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    LOST = "lost"  # 1100: IB has lost its connection; the socket may still be up
    CLOSED = "closed"  # closed by close(), by the peer, or after a failed open


class ConnectFailed(RuntimeError):
    """`open()` gave up. `failures` lists every attempt so far with IB's code and message."""

    def __init__(self, message: str, failures: tuple[AttemptFailure, ...] = ()) -> None:
        super().__init__(message)
        self.failures = failures


class ClientIdInUse(ConnectFailed):
    """IB error 326: another API client already uses this client id. Never retried."""


class ConnectionClosedDuringOpen(ConnectFailed):
    """`close()` was called while `open()` was running; `open()` stopped and cleaned up."""


class ClientIdRefused(ValueError):
    """The client id is not allowed (0 and 123, or not an int from 1 to 2147483647)."""


class NotConnected(RuntimeError):
    """A request was made while the connection is not open."""


@dataclass(frozen=True)
class AttemptFailure:
    attempt: int
    code: int | None  # IB's error code, when there was one
    message: str

    def describe(self) -> str:
        where = f"IB error {self.code}: " if self.code is not None else ""
        return f"attempt {self.attempt}: {where}{self.message}"


@dataclass(frozen=True)
class ConnectionLost:
    """IB code 1100. Stop sending orders; nothing reconnects by itself."""

    code: int
    message: str


@dataclass(frozen=True)
class ConnectionRestored:
    """IB code 1101 (`data_lost` True: every subscription must be requested again)
    or 1102 (`data_lost` False: subscriptions are kept; still confirm state)."""

    code: int
    message: str
    data_lost: bool

    @property
    def resubscribe_required(self) -> bool:
        return self.data_lost


@dataclass(frozen=True)
class ReaderStopped:
    """`client.run()` raised; `error` is what it raised."""

    error: BaseException


ConnectionEvent = events.Event | ConnectionLost | ConnectionRestored | ReaderStopped


class Observer(Protocol):
    """Optional hooks; an object may define either or both. Must not call back into the connection."""

    def on_request(self, name: str, args: tuple[Any, ...]) -> None: ...

    def on_raw(self, name: str, args: tuple[Any, ...]) -> None: ...


class _ConnectBox:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None


class _Attempt:
    """Everything that belongs to one connect attempt; never reused."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.client: Any = None
        self.helper: threading.Thread | None = None
        self.reader: threading.Thread | None = None
        self.queue: queue.Queue[ConnectionEvent] = queue.Queue()
        self.lost = False
        self.farm_codes: set[int] = set()
        self.closed_seen = False  # at most one ConnectionClosed per attempt is delivered
        self.dead = False  # set when the attempt is over: later callbacks are ignored


def _positive_number(name: str, value: Any, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{name} must be a finite number above 0 and at most {maximum}, not {value!r}")


class IbConnection:
    """Owns the IB client of the current attempt, its reader thread and its event queue.

    Public API: `open()`, `close()` (also `with`), `state`, `next_event(timeout)`,
    `wait_for_data_farm(timeout, farm=None)`, `request(name, *args)`,
    `next_request_id()`, `next_valid_id`, `attempts_made`, `reader_errors`,
    `observer_failures`. The raw client is private: every request goes through
    `request()`, which enforces the allowlist `DATA_REQUESTS` and the rate limit.

    Constructor numbers are validated: `attempts` is an int from 1 to `MAX_ATTEMPTS`
    (there is no way to retry forever), `max_requests_per_second` an int from 1 to
    `MAX_REQUESTS_PER_SECOND` (IB allows 50 messages per second), `backoff` at least
    1, and every timeout and delay finite, above 0 and at most its bound in
    `MAX_SECONDS` (`connect_timeout` and `ready_timeout` 300 s, `retry_delay` and
    `max_retry_delay` 600 s, `join_timeout` 60 s, `poll_interval` 10 s). The client id
    is checked by `open()` (an int from 1 to 2147483647, not 123; `bool` is refused).

    Timeouts. `connect_timeout` (default 10 s) bounds one `connect` call and
    `ready_timeout` (default 10 s) the wait for `nextValidId` after it. On the first
    connection from a new machine or a new client id TWS may show an "accept incoming
    connection" dialog and wait for a person to click it; the handshake is then not
    answered and each abandoned attempt can leave another dialog behind. A command
    that expects this should raise `connect_timeout` (the caller of the constructor
    decides; nothing here reads the environment).

    State LOST (IB code 1100, connectivity between TWS and IB lost) still allows
    `request()`. The socket to TWS is up, `request()` sends only data requests (no
    order can leave this layer) and the caller needs `cancelMktData` and
    `cancelHistoricalData` to clean up. The request may not be answered until 1101 or
    1102 arrives; a caller that must not act on stale data checks `state` first. Only
    CLOSED (and NEW or CONNECTING) refuse requests, with `NotConnected`.

    `wait_for_data_farm(timeout)` returns True on the first OK message of any farm:
    2104 (market data), 2106 (HMDS, historical data), 2158 (security definitions),
    or 2107 / 2108 (HMDS / market data farm inactive but available on demand). So it
    does NOT mean the HMDS farm is up. A caller that fetches historical bars passes
    `farm="historical"` (codes 2106, 2107); the other kinds are `"market"` (2104,
    2108) and `"secdef"` (2158). It stays true if a farm breaks later.

    A `sleep` passed to the constructor is used for the retry backoff and the rate
    limit. It must be interruptible or short: `close()` cannot wake it, so an abort of
    `open()` waits for it. The default (None) is a real sleep that `close()` wakes.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        env: Mapping[str, str],
        client_factory: Callable[..., Any] = DataOnlyClient,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        attempts: int = 3,
        connect_timeout: float = 10,
        ready_timeout: float = 10,
        retry_delay: float = 5,
        backoff: float = 2.0,
        max_retry_delay: float = 60,
        observers: Iterable[Any] = (),
        max_requests_per_second: int = 20,
        poll_interval: float = 0.05,
        join_timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
            raise ValueError(f"attempts must be an int from 1 to {MAX_ATTEMPTS}, not {attempts!r}")
        if type(max_requests_per_second) is not int or not (
            1 <= max_requests_per_second <= MAX_REQUESTS_PER_SECOND
        ):
            raise ValueError(
                f"max_requests_per_second must be an int from 1 to {MAX_REQUESTS_PER_SECOND}, "
                f"not {max_requests_per_second!r}"
            )
        for label, number in (
            ("connect_timeout", connect_timeout),
            ("ready_timeout", ready_timeout),
            ("retry_delay", retry_delay),
            ("max_retry_delay", max_retry_delay),
            ("poll_interval", poll_interval),
            ("join_timeout", join_timeout),
        ):
            _positive_number(label, number, MAX_SECONDS[label])
        if (
            isinstance(backoff, bool)
            or not isinstance(backoff, int | float)
            or not math.isfinite(backoff)
            or backoff < 1
        ):
            raise ValueError(f"backoff must be a finite number of at least 1, not {backoff!r}")
        self.host = host
        self.port = port
        self.client_id = client_id
        self._env = env
        self._factory = client_factory
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._interruptible_sleep = sleep is None
        self._attempts = attempts
        self._connect_timeout = connect_timeout
        self._ready_timeout = ready_timeout
        self._retry_delay = retry_delay
        self._backoff = backoff
        self._max_retry_delay = max_retry_delay
        self._observers = tuple(observers)
        self._rate = max_requests_per_second
        self._poll = poll_interval
        self._join_timeout = join_timeout
        self._log = logger if logger is not None else logging.getLogger(__name__)

        self._lock = threading.RLock()
        self._backlog: deque[ConnectionEvent] = deque()  # consumer thread only
        self._tls = threading.local()  # .internal: a thread this class started; .in_request
        self._att: _Attempt | None = None  # the current attempt
        self._stuck: list[threading.Thread] = []  # abandoned threads of old attempts
        self.attempts_made = 0
        self._phase = ConnState.NEW  # NEW, CONNECTING, READY or CLOSED
        self._close_started = False
        self._close_evt = threading.Event()  # set by close(); read by open()
        self._opener: threading.Thread | None = None  # the thread inside open()
        self._open_done = threading.Event()
        self._farm_cond = threading.Condition(self._lock)
        self._request_ids = 0
        self._request_times: deque[float] = deque()
        self._request_lock = threading.Lock()
        self.next_valid_id: int | None = None
        self.reader_errors: list[BaseException] = []
        self.observer_failures: list[BaseException] = []

    # -- public state ---------------------------------------------------------

    @property
    def state(self) -> ConnState:
        with self._lock:
            att = self._att
            if self._phase is ConnState.READY and att is not None and att.lost:
                return ConnState.LOST
            return self._phase

    def __enter__(self) -> IbConnection:
        if self.state is ConnState.NEW:
            self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- open -----------------------------------------------------------------

    def open(self) -> None:
        """Connect and wait for `nextValidId`, with bounded retries. See the module docstring.

        Raises `ConnectionClosedDuringOpen` if `close()` is called meanwhile.
        """
        if type(self.port) is not int:
            raise BrokerPortRefused(f"Refusing to connect: broker port {self.port!r} is not an int.")
        check_broker_port(self.port, self._env)
        if (
            type(self.client_id) is not int
            or not 1 <= self.client_id <= MAX_CLIENT_ID
            or self.client_id in REFUSED_CLIENT_IDS
        ):
            raise ClientIdRefused(
                f"Refusing client id {self.client_id!r}: it must be an int from 1 to {MAX_CLIENT_ID} "
                f"and not {sorted(REFUSED_CLIENT_IDS)} (0 would take over orders placed by hand in TWS)."
            )
        with self._lock:
            if self._phase is not ConnState.NEW or self._close_started:
                raise RuntimeError(f"open() on a connection that is {self._phase.value}")
            self._phase = ConnState.CONNECTING
            self._opener = threading.current_thread()
            self._open_done.clear()
        try:
            self._open_attempts()
        except BaseException:
            try:
                if self._close_evt.is_set():
                    # close() is (or was) running: make sure nothing of ours stays open.
                    self._end_attempt(self._att)
            finally:
                with self._lock:
                    self._phase = ConnState.CLOSED
            raise
        finally:
            with self._lock:
                self._opener = None
            self._open_done.set()

    def _open_attempts(self) -> None:
        failures: list[AttemptFailure] = []
        for n in range(1, self._attempts + 1):
            self._check_open()
            att = self._begin_attempt(n)
            try:
                failure, fatal = self._attempt_once(att)
            except BaseException:
                self._end_attempt(att)
                raise
            if failure is None:
                return
            failures.append(failure)
            self._log.warning(
                "IB connect attempt %d of %d to %s:%d failed: %s",
                n, self._attempts, self.host, self.port, failure.describe(),
            )
            for thread in self._end_attempt(att):
                self._stuck.append(thread)
                failures.append(
                    AttemptFailure(
                        n, None,
                        f"IB thread {thread.name} did not end within {self._join_timeout}s after "
                        "disconnect(); abandoned (the next attempt uses a new client)",
                    )
                )
            self._check_open()
            if fatal:
                raise ClientIdInUse(
                    f"IB client id {self.client_id} is already in use by another API "
                    f"connection to this TWS ({failure.describe()}); not retrying. "
                    "Close the other client or use another id.",
                    tuple(failures),
                )
            alive = [t for t in self._stuck if t.is_alive()]
            if len(alive) > MAX_STUCK_THREADS:
                raise ConnectFailed(
                    f"{len(alive)} abandoned IB threads ({', '.join(t.name for t in alive)}) are "
                    f"still alive (more than {MAX_STUCK_THREADS}); giving up after attempt {n} of "
                    f"{self._attempts}: " + "; ".join(f.describe() for f in failures),
                    tuple(failures),
                )
            if n < self._attempts:
                self._pause(min(self._retry_delay * self._backoff ** (n - 1), self._max_retry_delay))
        raise ConnectFailed(
            f"Could not connect to IB at {self.host}:{self.port} with client id "
            f"{self.client_id} after {self._attempts} attempt(s): "
            + "; ".join(f.describe() for f in failures),
            tuple(failures),
        )

    def _check_open(self) -> None:
        if self._close_evt.is_set():
            raise self._closed_error()

    @staticmethod
    def _closed_error(detail: str = "") -> ConnectionClosedDuringOpen:
        return ConnectionClosedDuringOpen("close() was called while open() was running" + detail)

    def _pause(self, seconds: float) -> None:
        """The backoff sleep: the injected one, or (default) a wait that `close()` ends."""
        if self._interruptible_sleep:
            self._close_evt.wait(seconds)
        else:
            self._sleep(seconds)

    def _begin_attempt(self, n: int) -> _Attempt:
        """Start attempt `n` with a fresh client and a fresh `_Attempt`."""
        att = _Attempt(n)
        with self._lock:
            self._att = att
            self.attempts_made = n
            self.next_valid_id = None
        self._backlog.clear()
        att.client = self._factory(
            env=self._env,
            on_event=lambda event: self._on_event(att, event),
            on_raw=lambda name, args: self._on_raw(att, name, args),
            logger=self._log,
        )
        return att

    def _disconnect(self, att: _Attempt) -> None:
        """`disconnect()` the attempt's client. A client that is already closed may raise."""
        try:
            att.client.disconnect()
        except (AttributeError, OSError, TypeError) as exc:
            self._log.debug(
                "IB client of attempt %d: disconnect() raised %r; treated as already closed",
                att.number, exc,
            )

    def _end_attempt(self, att: _Attempt | None) -> list[threading.Thread]:
        """Disconnect (always, before any retry), join the threads, retire the attempt.

        Returns the threads that are still alive after `join_timeout`.
        """
        if att is None:
            return []
        try:
            if att.client is not None:
                self._disconnect(att)
        finally:
            stuck = self._join_threads(att)
            att.dead = True
            self._backlog.clear()
        return stuck

    def _join_threads(self, att: _Attempt) -> list[threading.Thread]:
        stuck: list[threading.Thread] = []
        for role, thread in (("connect helper", att.helper), ("reader", att.reader)):
            if thread is None or thread is threading.current_thread():
                continue
            try:
                thread.join(self._join_timeout)
            except RuntimeError:  # close() raced with open() and the thread is not started yet
                continue
            if thread.is_alive():
                self._log.warning(
                    "IB %s thread %s did not end within %ss", role, thread.name, self._join_timeout
                )
                stuck.append(thread)
        return stuck

    def _connect_target(self, att: _Attempt, box: _ConnectBox) -> None:
        self._tls.internal = True
        try:
            att.client.connect(self.host, self.port, self.client_id)
        except BaseException as exc:  # handed to the caller thread, which decides
            box.error = exc
        finally:
            if self._close_evt.is_set():
                self._disconnect(att)  # a connect that finished after close() must not stay open
            box.done.set()

    def _attempt_once(self, att: _Attempt) -> tuple[AttemptFailure | None, bool]:
        n = att.number
        self._check_open()
        box = _ConnectBox()
        helper = threading.Thread(
            target=self._connect_target, args=(att, box), name=f"ib-connect-{n}", daemon=True
        )
        att.helper = helper
        helper.start()
        deadline = self._clock() + self._connect_timeout
        released = False
        while not box.done.wait(self._poll):
            if self._close_evt.is_set():
                self._disconnect(att)  # releases a hung connect; the check below aborts
                break
            if self._clock() >= deadline:
                released = True
                self._disconnect(att)
                break
        helper.join(self._join_timeout)
        if helper.is_alive():
            if self._close_evt.is_set():
                raise self._closed_error(f" (connect helper {helper.name} still running)")
            return AttemptFailure(
                n, None, f"connect helper {helper.name} did not end after disconnect(); abandoned"
            ), False
        self._check_open()  # a connect that succeeded concurrently is disconnected by the caller
        error = box.error
        if released:
            if error is not None and not isinstance(error, AttributeError | OSError):
                raise error
            return AttemptFailure(
                n, None,
                f"handshake not answered within {self._connect_timeout}s (is TWS waiting at its "
                "accept-incoming-connection dialog?); abandoned",
            ), False
        if error is not None:
            if isinstance(error, OSError):
                return AttemptFailure(n, None, f"{type(error).__name__}: {error}"), False
            raise error
        # connect() returned. That proves nothing: wait for nextValidId.
        reader = threading.Thread(
            target=self._read, args=(att,), name=f"ib-reader-{n}", daemon=True
        )
        att.reader = reader
        reader.start()
        return self._await_ready(att, reader)

    def _await_ready(self, att: _Attempt, reader: threading.Thread) -> tuple[AttemptFailure | None, bool]:
        n = att.number
        deadline = self._clock() + self._ready_timeout
        last_error: events.IbError | None = None
        closed = False
        while True:
            self._check_open()
            ev = self._pop(att, self._poll)
            if ev is None:
                if self._clock() >= deadline:
                    why = f"no nextValidId within {self._ready_timeout}s"
                    return self._no_ready(n, last_error, why), False
                if reader.is_alive():
                    continue
                ev = self._pop(att, 0)  # the reader ended: everything it queued is there
                if ev is None:
                    why = "connection closed before nextValidId" if closed else (
                        "connection closed during the handshake")
                    return self._no_ready(n, last_error, why), False
            self._backlog.append(ev)
            if isinstance(ev, events.NextValidId):
                with self._lock:
                    if self._close_evt.is_set():  # close() won: never end READY after it
                        raise self._closed_error()
                    self.next_valid_id = ev.order_id
                    self._phase = ConnState.READY
                return None, False
            if isinstance(ev, events.IbError):
                if ev.code == CLIENT_ID_IN_USE:
                    return AttemptFailure(n, ev.code, ev.message), True
                if not (2100 <= ev.code < 2200 or CONNECTION_LOST <= ev.code <= RESTORED_DATA_KEPT):
                    last_error = ev
            elif isinstance(ev, events.ConnectionClosed):
                closed = True
            elif isinstance(ev, ReaderStopped):
                return AttemptFailure(n, None, f"reader stopped: {ev.error!r}"), False

    @staticmethod
    def _no_ready(n: int, last_error: events.IbError | None, why: str) -> AttemptFailure:
        if last_error is not None:
            return AttemptFailure(n, last_error.code, last_error.message)
        return AttemptFailure(n, None, why)

    # -- reader and callbacks ----------------------------------------------------

    def _read(self, att: _Attempt) -> None:
        self._tls.internal = True
        try:
            att.client.run()
        except BaseException as exc:  # becomes an event; the thread never dies loudly
            self.reader_errors.append(exc)
            if not att.dead:
                att.queue.put(ReaderStopped(exc))

    def _on_raw(self, att: _Attempt, name: str, args: tuple[Any, ...]) -> None:
        if not att.dead:
            self._notify("on_raw", name, args)

    def _notify(self, hook: str, name: str, args: tuple[Any, ...]) -> None:
        for obs in self._observers:
            fn = getattr(obs, hook, None)
            if fn is None:
                continue
            try:
                fn(name, args)
            except Exception as exc:  # an observer must not break the connection
                self.observer_failures.append(exc)
                self._log.error("IB observer %r.%s raised %r", obs, hook, exc)

    def _on_event(self, att: _Attempt, event: events.Event) -> None:
        derived: ConnectionEvent | None = None
        farm_code: int | None = None
        with self._lock:
            if att.dead:
                return
            if isinstance(event, events.IbError):
                code = event.code
                if code in DATA_FARM_CODES:
                    farm_code = code
                elif code == CONNECTION_LOST:
                    att.lost = True
                    derived = ConnectionLost(code, event.message)
                elif code in (RESTORED_DATA_LOST, RESTORED_DATA_KEPT):
                    att.lost = False
                    derived = ConnectionRestored(
                        code, event.message, data_lost=code == RESTORED_DATA_LOST
                    )
            elif isinstance(event, events.ConnectionClosed):
                if att.closed_seen:  # the real client can report the close twice
                    return
                att.closed_seen = True
                if att is self._att and self._phase is ConnState.READY:
                    self._phase = ConnState.CLOSED
        att.queue.put(event)
        if derived is not None:
            if isinstance(derived, ConnectionRestored) and derived.data_lost:
                self._log.warning("IB code 1101: data lost; subscriptions must be requested again")
            elif isinstance(derived, ConnectionLost):
                self._log.warning("IB code 1100: connection to IB lost; not reconnecting")
            att.queue.put(derived)
        if farm_code is not None:
            # After the queue put: a waiter that wakes can read the event with next_event.
            with self._farm_cond:
                att.farm_codes.add(farm_code)
                self._farm_cond.notify_all()

    # -- consumer side -------------------------------------------------------------

    @staticmethod
    def _pop(att: _Attempt, wait: float) -> ConnectionEvent | None:
        try:
            if wait <= 0:
                return att.queue.get_nowait()
            return att.queue.get(timeout=wait)
        except queue.Empty:
            return None

    def next_event(self, timeout: float = 0.0) -> ConnectionEvent | None:
        """The next event, or None after `timeout` seconds of the injected clock."""
        if self._backlog:
            return self._backlog.popleft()
        deadline = self._clock() + timeout
        while True:
            att = self._att
            wait = min(self._poll, timeout) if timeout > 0 else 0
            if att is None:
                if wait > 0:
                    self._close_evt.wait(wait)
            else:
                ev = self._pop(att, wait)
                if ev is not None:
                    return ev
            if timeout <= 0 or self._clock() >= deadline:
                return None

    def wait_for_data_farm(self, timeout: float, farm: str | None = None) -> bool:
        """Wait for a data-farm OK message; warn and return False on timeout.

        With `farm=None` the first message of any farm counts (2104, 2106, 2107, 2108,
        2158), which does not mean the historical farm is up. `farm` is `"market"`,
        `"historical"` or `"secdef"` to wait for that kind (see `FARM_KINDS`); anything
        else raises `ValueError`. Does not consume events: they stay for `next_event`.
        Only the messages of the current attempt count.
        """
        if farm is None:
            wanted = DATA_FARM_CODES
        elif farm in FARM_KINDS:
            wanted = FARM_KINDS[farm]
        else:
            raise ValueError(f"farm must be None or one of {sorted(FARM_KINDS)}, not {farm!r}")
        deadline = self._clock() + timeout
        with self._farm_cond:
            while True:
                att = self._att
                if att is not None and att.farm_codes & wanted:
                    return True
                if self._clock() >= deadline:
                    self._log.warning(
                        "No IB data-farm message (%s) within %ss", sorted(wanted), timeout
                    )
                    return False
                self._farm_cond.wait(self._poll)

    # -- requests ----------------------------------------------------------------------

    def next_request_id(self) -> int:
        with self._lock:
            self._request_ids += 1
            return self._request_ids

    def request(self, name: str, *args: Any) -> Any:
        """Send one data request (see `DATA_REQUESTS`), rate limited.

        Allowed in states READY and LOST (see the class docstring). Raises
        `RuntimeError` when called from an observer of a request on the same thread.
        """
        if name not in DATA_REQUESTS:
            raise MessageRefused(
                f"IbConnection refuses request {name!r}: only {sorted(DATA_REQUESTS)} may be sent."
            )
        if getattr(self._tls, "in_request", False):
            raise RuntimeError(
                "request() was called from inside request() (an observer?); observers must not "
                "call back into the connection"
            )
        att = self._require_requestable(name)
        with self._request_lock:
            self._tls.in_request = True
            try:
                self._throttle()
                att = self._require_requestable(name)  # close() may have run while it waited
                self._notify("on_request", name, args)
                return getattr(att.client, name)(*args)
            finally:
                self._tls.in_request = False

    def _require_requestable(self, name: str) -> _Attempt:
        state = self.state
        att = self._att
        if state not in (ConnState.READY, ConnState.LOST) or att is None:
            raise NotConnected(f"Cannot send {name}: the connection is {state.value}")
        return att

    def _throttle(self) -> None:
        """At most `max_requests_per_second` in any one second of the injected clock.

        When full it sleeps through the injected `sleep` and then trusts that the wait
        happened: `now` is at least the moment it waited for, even if the clock was not
        advanced by that sleep. The test for an expired request is `times[0] + window
        <= now`, the same sum as `until`, so a frozen clock cannot make this loop spin
        (`now - window` is not exact in floats).
        """
        window = 1.0
        times = self._request_times
        now = max(self._clock(), times[-1] if times else -math.inf)
        while True:
            while times and times[0] + window <= now:
                times.popleft()
            if len(times) < self._rate:
                times.append(now)
                return
            until = times[0] + window
            self._sleep(until - now)
            now = max(self._clock(), until)

    # -- close ---------------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect and join the threads. Idempotent; safe after a failed open.

        From another thread it also aborts a running `open()` and waits (at most
        `join_timeout`) for it to finish its cleanup; see the module docstring.
        """
        with self._lock:
            if self._close_started:
                return
            self._close_started = True
            self._close_evt.set()
            opener = self._opener
            first = self._att
        own = getattr(self._tls, "internal", False) or opener is threading.current_thread()
        try:
            if first is not None and first.client is not None:
                self._disconnect(first)
        finally:
            if opener is not None and not own and not self._open_done.wait(self._join_timeout):
                self._log.error(
                    "close(): open() on thread %s did not finish within %ss; still running: %s",
                    opener.name, self._join_timeout, self._running_threads() or "nothing else",
                )
            with self._lock:
                att = self._att  # open() may have begun a newer attempt before it saw the flag
            if att is not None:
                if att.client is not None and (att is not first or att.client.isConnected()):
                    self._disconnect(att)
                self._join_threads(att)
                att.dead = True
            with self._lock:
                self._phase = ConnState.CLOSED

    def _running_threads(self) -> str:
        names = [
            t.name
            for att in (self._att,)
            if att is not None
            for t in (att.helper, att.reader)
            if t is not None and t.is_alive()
        ]
        return ", ".join(names)
