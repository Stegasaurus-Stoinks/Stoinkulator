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
  Every queued event carries the number of the attempt that produced it, and
  the queue is drained between attempts, so a close from attempt N is never
  read as the close of attempt N+1.
* `disconnect()` is called before every retry (after a failed attempt of any
  kind, including a `nextValidId` timeout): a second `connect` on a connected
  client would silently open a second socket.
* `run()` after a caller `disconnect()` with messages still queued raises
  `TypeError` on the run thread. The reader thread wraps `run()` and turns any
  exception into a `ReaderStopped` event.
* 1100, 1101, 1102 and 2110 arrive as `error()` callbacks while the socket
  stays up. 1100 marks the connection lost and nothing reconnects; 1101 means
  data was lost and every subscription must be requested again; 1102 means the
  subscriptions are kept. 326 (client id in use) is detected by its code and is
  never retried.
* `connectionClosed` arrives on the caller thread after a `disconnect()` and on
  the run thread after a peer close; the handlers here are thread-safe.

The retry loop catches narrowly: `OSError` (sockets, timeouts) and the
documented outcomes of a hung handshake. `MessageRefused`, `BrokerPortRefused`
and anything else propagate from the first attempt (after a `disconnect()`),
including a refusal by the client's frame guard, which leaves the socket open.

Threads: callbacks only put events on a queue; the caller thread consumes them
(`next_event`). Observers see every request and every raw callback, on the
thread where it happens, so they must be thread-safe.

Waiting and backoff use the injected `clock` and `sleep`. The clock bounds every
timeout; waits for something to happen block for at most `poll_interval` real
seconds at a time, then look at the clock.
"""

from __future__ import annotations

import enum
import logging
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

DATA_FARM_CODES: frozenset[int] = frozenset({2104, 2106, 2107, 2108, 2158})
CLIENT_ID_IN_USE = 326
CONNECTION_LOST = 1100
RESTORED_DATA_LOST = 1101
RESTORED_DATA_KEPT = 1102
REFUSED_CLIENT_IDS: frozenset[int] = frozenset({0, 123})
"""0 would take over orders placed by hand in TWS; 123 is the legacy code's id."""


class ConnState(enum.Enum):
    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    LOST = "lost"  # 1100: IB has lost its connection; the socket may still be up
    CLOSED = "closed"  # closed by close(), by the peer, or after a failed open


class ConnectFailed(RuntimeError):
    """`open()` gave up. `failures` lists every attempt with IB's code and message."""

    def __init__(self, message: str, failures: tuple[AttemptFailure, ...] = ()) -> None:
        super().__init__(message)
        self.failures = failures


class ClientIdInUse(ConnectFailed):
    """IB error 326: another API client already uses this client id. Never retried."""


class ClientIdRefused(ValueError):
    """The client id is not allowed (0 and 123, or not a positive int)."""


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
    """Optional hooks; an object may define either or both."""

    def on_request(self, name: str, args: tuple[Any, ...]) -> None: ...

    def on_raw(self, name: str, args: tuple[Any, ...]) -> None: ...


class _ConnectBox:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None


class IbConnection:
    """Owns one IB client, its reader thread and its event queue. See the module docstring."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        env: Mapping[str, str],
        client_factory: Callable[..., Any] = DataOnlyClient,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
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
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self.host = host
        self.port = port
        self.client_id = client_id
        self._env = env
        self._factory = client_factory
        self._clock = clock
        self._sleep = sleep
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
        self._queue: queue.Queue[tuple[int, ConnectionEvent]] = queue.Queue()
        self._backlog: deque[ConnectionEvent] = deque()  # consumer thread only
        self._attempt = 0
        self.attempts_made = 0
        self._phase = ConnState.NEW  # NEW, CONNECTING, READY or CLOSED
        self._lost = False
        self._close_started = False
        self._client: Any = None
        self._reader: threading.Thread | None = None
        self._farm_codes: set[int] = set()
        self._farm_seen = threading.Event()
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
            if self._phase is ConnState.READY and self._lost:
                return ConnState.LOST
            return self._phase

    @property
    def client(self) -> Any:
        """The client, for read-only inspection; send requests through `request`."""
        return self._client

    def __enter__(self) -> IbConnection:
        if self.state is ConnState.NEW:
            self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- open -----------------------------------------------------------------

    def open(self) -> None:
        """Connect and wait for `nextValidId`, with bounded retries. See the module docstring."""
        if type(self.port) is not int:
            raise BrokerPortRefused(f"Refusing to connect: broker port {self.port!r} is not an int.")
        check_broker_port(self.port, self._env)
        if type(self.client_id) is not int or self.client_id <= 0 or self.client_id in REFUSED_CLIENT_IDS:
            raise ClientIdRefused(
                f"Refusing client id {self.client_id!r}: it must be a positive int and not "
                f"{sorted(REFUSED_CLIENT_IDS)} (0 would take over orders placed by hand in TWS)."
            )
        with self._lock:
            if self._phase is not ConnState.NEW:
                raise RuntimeError(f"open() on a connection that is {self._phase.value}")
            self._phase = ConnState.CONNECTING
        try:
            self._client = self._factory(
                env=self._env, on_event=self._on_event, on_raw=self._on_raw, logger=self._log
            )
            failures: list[AttemptFailure] = []
            for n in range(1, self._attempts + 1):
                self._begin_attempt(n)
                try:
                    failure, fatal = self._attempt_once(n)
                except BaseException:
                    self._end_attempt()
                    raise
                if failure is None:
                    return
                failures.append(failure)
                self._log.warning(
                    "IB connect attempt %d of %d to %s:%d failed: %s",
                    n, self._attempts, self.host, self.port, failure.describe(),
                )
                self._end_attempt()
                if fatal:
                    raise ClientIdInUse(
                        f"IB client id {self.client_id} is already in use by another API "
                        f"connection to this TWS ({failure.describe()}); not retrying. "
                        "Close the other client or use another id.",
                        tuple(failures),
                    )
                if n < self._attempts:
                    self._sleep(min(self._retry_delay * self._backoff ** (n - 1), self._max_retry_delay))
            raise ConnectFailed(
                f"Could not connect to IB at {self.host}:{self.port} with client id "
                f"{self.client_id} after {self._attempts} attempt(s): "
                + "; ".join(f.describe() for f in failures),
                tuple(failures),
            )
        except BaseException:
            with self._lock:
                self._phase = ConnState.CLOSED
            raise

    def _begin_attempt(self, n: int) -> None:
        self._attempt = n
        self.attempts_made = n
        self._backlog.clear()
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _end_attempt(self) -> None:
        """Disconnect (always, before any retry), join the reader, drop what is queued."""
        try:
            if self._client is not None:
                self._client.disconnect()
        finally:
            self._join_reader()
            self._backlog.clear()
            self._drain()

    def _join_reader(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(self._join_timeout)
            if reader.is_alive():
                self._log.warning("IB reader thread did not end within %ss", self._join_timeout)

    def _connect_target(self, box: _ConnectBox) -> None:
        try:
            self._client.connect(self.host, self.port, self.client_id)
        except BaseException as exc:  # handed to the caller thread, which decides
            box.error = exc
        finally:
            box.done.set()

    def _attempt_once(self, n: int) -> tuple[AttemptFailure | None, bool]:
        client = self._client
        box = _ConnectBox()
        helper = threading.Thread(
            target=self._connect_target, args=(box,), name=f"ib-connect-{n}", daemon=True
        )
        helper.start()
        deadline = self._clock() + self._connect_timeout
        released = False
        while not box.done.wait(self._poll):
            if self._clock() >= deadline:
                released = True
                client.disconnect()
                break
        helper.join(self._join_timeout)
        if helper.is_alive():
            raise ConnectFailed(
                f"The IB connect helper thread of attempt {n} did not end after disconnect()."
            )
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
            target=self._read, args=(client, n), name=f"ib-reader-{n}", daemon=True
        )
        self._reader = reader
        reader.start()
        return self._await_ready(n, reader)

    def _await_ready(self, n: int, reader: threading.Thread) -> tuple[AttemptFailure | None, bool]:
        deadline = self._clock() + self._ready_timeout
        last_error: events.IbError | None = None
        closed = False
        while True:
            item = self._pop(self._poll)
            if item is None:
                if self._clock() >= deadline:
                    why = f"no nextValidId within {self._ready_timeout}s"
                    return self._no_ready(n, last_error, why), False
                if reader.is_alive():
                    continue
                item = self._pop(0)  # the reader ended: everything it queued is there
                if item is None:
                    why = "connection closed before nextValidId" if closed else (
                        "connection closed during the handshake")
                    return self._no_ready(n, last_error, why), False
            attempt, ev = item
            if attempt != n:
                continue  # a late event of an earlier attempt
            self._backlog.append(ev)
            if isinstance(ev, events.NextValidId):
                with self._lock:
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

    def _read(self, client: Any, attempt: int) -> None:
        try:
            client.run()
        except BaseException as exc:  # becomes an event; the thread never dies loudly
            self.reader_errors.append(exc)
            self._queue.put((attempt, ReaderStopped(exc)))

    def _on_raw(self, name: str, args: tuple[Any, ...]) -> None:
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

    def _on_event(self, event: events.Event) -> None:
        attempt = self._attempt
        derived: ConnectionEvent | None = None
        with self._lock:
            if isinstance(event, events.IbError):
                code = event.code
                if code in DATA_FARM_CODES:
                    self._farm_codes.add(code)
                    self._farm_seen.set()
                elif code == CONNECTION_LOST:
                    self._lost = True
                    derived = ConnectionLost(code, event.message)
                elif code in (RESTORED_DATA_LOST, RESTORED_DATA_KEPT):
                    self._lost = False
                    derived = ConnectionRestored(code, event.message, data_lost=code == RESTORED_DATA_LOST)
            elif isinstance(event, events.ConnectionClosed) and self._phase is ConnState.READY:
                self._phase = ConnState.CLOSED
        self._queue.put((attempt, event))
        if derived is not None:
            if isinstance(derived, ConnectionRestored) and derived.data_lost:
                self._log.warning("IB code 1101: data lost; subscriptions must be requested again")
            elif isinstance(derived, ConnectionLost):
                self._log.warning("IB code 1100: connection to IB lost; not reconnecting")
            self._queue.put((attempt, derived))

    # -- consumer side -------------------------------------------------------------

    def _pop(self, wait: float) -> tuple[int, ConnectionEvent] | None:
        try:
            if wait <= 0:
                return self._queue.get_nowait()
            return self._queue.get(timeout=wait)
        except queue.Empty:
            return None

    def next_event(self, timeout: float = 0.0) -> ConnectionEvent | None:
        """The next event, or None after `timeout` seconds of the injected clock."""
        if self._backlog:
            return self._backlog.popleft()
        deadline = self._clock() + timeout
        while True:
            item = self._pop(min(self._poll, timeout) if timeout > 0 else 0)
            if item is not None:
                if item[0] == self._attempt:
                    return item[1]
                continue
            if timeout <= 0 or self._clock() >= deadline:
                return None

    def wait_for_data_farm(self, timeout: float) -> bool:
        """Wait for 2104, 2106, 2107, 2108 or 2158; warn and return False on timeout.

        Does not consume events: they stay for `next_event`.
        """
        deadline = self._clock() + timeout
        while not self._farm_seen.is_set():
            if self._clock() >= deadline:
                self._log.warning("No IB data-farm message (%s) within %ss", sorted(DATA_FARM_CODES), timeout)
                return False
            self._farm_seen.wait(self._poll)
        return True

    # -- requests ----------------------------------------------------------------------

    def next_request_id(self) -> int:
        with self._lock:
            self._request_ids += 1
            return self._request_ids

    def request(self, name: str, *args: Any) -> Any:
        """Send one data request (see `DATA_REQUESTS`), rate limited."""
        if name not in DATA_REQUESTS:
            raise MessageRefused(
                f"IbConnection refuses request {name!r}: only {sorted(DATA_REQUESTS)} may be sent."
            )
        if self.state not in (ConnState.READY, ConnState.LOST):
            raise NotConnected(f"Cannot send {name}: the connection is {self.state.value}")
        with self._request_lock:
            self._throttle()
            self._notify("on_request", name, args)
            return getattr(self._client, name)(*args)

    def _throttle(self) -> None:
        window = 1.0
        while True:
            now = self._clock()
            while self._request_times and self._request_times[0] <= now - window:
                self._request_times.popleft()
            if len(self._request_times) < self._rate:
                self._request_times.append(now)
                return
            self._sleep(self._request_times[0] + window - now)

    # -- close ---------------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect and join the reader. Idempotent; safe after a failed open."""
        with self._lock:
            if self._close_started:
                return
            self._close_started = True
        try:
            if self._client is not None:
                self._client.disconnect()
        finally:
            self._join_reader()
            with self._lock:
                self._phase = ConnState.CLOSED
