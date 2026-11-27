"""`IbMarketData`: the narrow data face over an open `IbConnection`.

It can resolve a stock, request and cancel 1-minute history, set the market
data type, request and cancel quotes and ask for the server time. Nothing else:
it only ever calls the data requests `IbConnection.request` allows
(`DATA_REQUESTS`), holds the connection in a private attribute, and has no
method that changes anything at the broker or on the account.

How a caller uses it
--------------------
Request methods return at once (`request_history` and `request_quotes` return the
request id); results are read with the wait methods:

* `wait_history(req_id, timeout)` returns a `HistoryResult`: ALL the raw
  `events.HistoricalBar` received so far for that request id, in arrival order
  (convert each with `ib.bars.bar_from_ib`), the end marker, every `events.IbError`
  of that request, `late_bars` and an `outcome`. A second wait on the same id
  returns the same bars again plus whatever arrived since; it never returns a
  partial tail. It never raises for an outcome: the fetch driver classifies them.
  IB error 162 means both "pacing violation" and "no data": tell them apart by
  `IbError.message`. Codes 2100 to 2199 are IB notices, kept in `errors` but not
  an `ERROR` outcome. Bars that arrive after the end marker of a one-shot request
  are counted in `late_bars` and not added to `bars`.
* `next_event_for(req_id, timeout)` returns the next event of one stream (quotes,
  or the updates of a `keep_up_to_date` history request), or None. `poll_event`
  is the same read but says why nothing came back.
* `resolve_stock(symbol, timeout)` is request plus wait in one call and raises a
  distinct `ResolveFailed` subclass for every failure.
* `wait_server_time(timeout)` answers `request_server_time()`; `poll_server_time`
  says why nothing came back.

Outcomes
--------
Every wait ends with exactly one `Outcome` (`OUTCOMES` lists them; `HistoryResult`
carries it, `poll_event` and `poll_server_time` return it instead of a value,
`resolve_stock` raises the `ResolveFailed` subclass in the last column):

========== ======================================= ================================ ==========================
outcome    meaning                                 the caller should                `resolve_stock` raises
========== ======================================= ================================ ==========================
end        the end marker arrived (sticky)         use `bars`; cancel the id when   (success)
                                                   done (sends nothing)
error      IB answered with an error for this id   look at `failures`; the request  `ResolveError` /
           (sticky): the request is dead at IB     is dead; cancel the id (sends    `ResolveNotFound`
                                                   nothing) and frees its state
timeout    the deadline passed, nothing is wrong   wait again (bars accumulate) or  `ResolveTimeout`
                                                   give up
closed     the connection closed (sticky)          stop                             `ResolveConnectionClosed`
lost       the deadline passed while IB code 1100  retry the wait, or give up after `ResolveConnectionLost`
           (connectivity lost) is in force; a      its own overall deadline
           restore may still come
data_lost  IB restored with code 1101 while the    cancel the id, discard the       `ResolveDataLost`
           request was in flight: IB forgot it     day's bars, request it again
           (sticky for history)
========== ======================================= ================================ ==========================

When several states hold for a history request the order is `data_lost`, then `error`,
then `end`: an error is never hidden by an end marker that arrives after it, and a live
(`keep_up_to_date`) history request that gets an error after its end marker reports
`error` from `wait_history` and from `poll_event` (sticky). Every wait takes a finite
timeout of 0 or more (a bool, NaN, infinity or a negative number is a `ValueError`,
raised before anything is sent).

For the caller
--------------
* This object is the only reader of the connection's events; pass it the connection's
  clock. `IbConnection.wait_for_data_farm` does not consume events.
* `lost` arrives only at the wait's deadline, not at once on code 1100. Between waits
  the lost state is not exposed: check the connection's `state` to pause.
* After `timeout` the caller sends a cancel (`cancel_history`), and a re-request gets a
  new id; late bars of the old id are dropped by id and never mixed into the new one.
* Bars are raw `events.HistoricalBar`: the caller converts each with
  `ib.bars.bar_from_ib` and treats any `BadBar` as a failed day.
* Use one-shot history only for fetching; `keep_up_to_date` is for streams read with
  `poll_event`.

Connection loss. While code 1100 is in force a wait keeps waiting (it reads the
connection with `next_event(poll_interval)`, so it does not spin) until its deadline,
a restore or a close. A restore with code 1102 (`ConnectionRestored.data_lost` False)
simply continues the same wait. A restore with code 1101 (`data_lost` True) is
recorded per request id when the event is routed: every request that was in flight
(a history request without its end marker or error, a quote stream, a live history
stream, a contract lookup, a pending server time) is marked dead and reports
`data_lost` whichever wait read the event first. History results and `resolve_stock`
report it every time they are asked again, without waiting; a stream
(`poll_event`/`next_event_for`) and `wait_server_time` report it once, after the
events already queued, and the caller then re-subscribes. If the deadline passes
while still lost, the outcome is `lost`, so the timeout is always honoured.

Cancelling. The caller cancels every history and quote id when it is done with it
(ended, errored, timed out or data lost). `cancel_history` and `cancel_quotes` free
the per-id state; they are idempotent. They send nothing to IB for a request that
is already dead there: a one-shot history that ended, errored or was data lost, and a
history request that got an error. After a data-lost restore a quote stream and a
`keep_up_to_date` history request DO get the cancel: IB may have kept the subscription,
and cancelling an unknown id is harmless while a leaked subscription holds a market data
line.

Events and consumers
--------------------
`IbConnection.next_event` has one consumer, and this object is it: once an
`IbMarketData` exists, nothing else may call `connection.next_event`. Every event
read is filed under its request id, so a wait for one request never consumes or
loses the events of another; events of ids this object did not issue and events
without a request id (farm notices, `ConnectionLost`, `ConnectionRestored`, ...)
go to a bounded general list (`general_events`). Events of a request that was
cancelled (or whose wait gave up) are dropped and counted in `late_events`.
State is bounded: a stream that nobody reads keeps its newest events (10,000 quotes
and 10,000 live-history updates, overflow counted in `quote_overflow` and
`update_overflow`); only the last 10,000 cancelled ids are remembered individually.

Threads: the request and cancel methods are thread-safe (the connection
serialises sends). Events are routed only by the one reader, so before a request
takes its id (`request_history`, `request_quotes`, `resolve_stock`,
`request_server_time`) it reads and routes everything already queued; this keeps a
1101 restore that was queued before the request from marking it data lost. The wait
methods and those request methods are single-consumer: one started while a wait is
running, on any thread, raises `ConcurrentWait` at once, before an id is taken or
anything is sent, rather than splitting the event stream. `reqCurrentTime` carries no
request id, so a server-time request discards any `CurrentTime` already queued or
filed and the caller issues at most one server-time request at a time. All waits use the
injected `clock` (pass the connection's clock) and poll the connection in slices of
`poll_interval`; none sleeps. The deadline is checked after every event as well, so
a stream of other events cannot postpone a timeout.
"""

from __future__ import annotations

import enum
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ibapi.contract import Contract

from MainStoinker.ib import events
from MainStoinker.ib.connection import (
    ConnectionEvent,
    ConnectionLost,
    ConnectionRestored,
    ConnState,
    IbConnection,
    NotConnected,
    ReaderStopped,
)
from MainStoinker.ib.contracts import SEC_TYPE, check_primary_exchange, stock
from MainStoinker.ib.history import (
    DEFAULT_BAR_SIZE,
    DEFAULT_WHAT_TO_SHOW,
    check_symbol,
    format_end_date_time,
)


class Outcome(enum.StrEnum):
    """Why a wait ended (the table in the module docstring says what to do)."""

    END = "end"
    ERROR = "error"
    TIMEOUT = "timeout"
    CLOSED = "closed"
    LOST = "lost"
    DATA_LOST = "data_lost"


END = Outcome.END
ERROR = Outcome.ERROR
TIMEOUT = Outcome.TIMEOUT
CLOSED = Outcome.CLOSED
LOST = Outcome.LOST
DATA_LOST = Outcome.DATA_LOST
OUTCOMES: tuple[Outcome, ...] = tuple(Outcome)
_READY = "ready"  # internal: the thing a wait was reading for is there

FORMAT_DATE_EPOCH = 2
"""`formatDate=2`: IB sends bar times as epoch seconds (see `ib.bars`)."""
MARKET_DATA_TYPES = (1, 2, 3, 4)
"""IB's market data types: 1 live, 2 frozen, 3 delayed, 4 delayed frozen."""
NOTICE_CODES = range(2100, 2200)
"""IB codes that are notices (farm status, ...), not request failures."""
NO_SECURITY_DEFINITION = 200

_DURATION = re.compile(r"[1-9][0-9]{0,5} [SDWMY]")
_GENERAL_LIMIT = 1000
_QUOTE_LIMIT = 10_000
_UPDATE_LIMIT = 10_000
_ERROR_LIMIT = 1000
_DROPPED_LIMIT = 10_000


class ConcurrentWait(RuntimeError):
    """A wait was started while another wait on the same `IbMarketData` was running."""


class DataLost(RuntimeError):
    """Raised by `next_event_for` and `wait_server_time` once when IB lost the data (1101).

    `req_id` is the stream's request id (None for the server time).
    """

    def __init__(self, req_id: int | None) -> None:
        super().__init__(
            f"IB restored the connection with code 1101: request {req_id!r} must be requested again"
        )
        self.req_id = req_id


class ResolveFailed(RuntimeError):
    """`resolve_stock` could not return exactly one contract; the subclass says why."""

    def __init__(self, symbol: str, message: str) -> None:
        super().__init__(f"cannot resolve {symbol}: {message}")
        self.symbol = symbol


class ResolveNotFound(ResolveFailed):
    """IB knows no such stock (no match, or error 200)."""


class ResolveAmbiguous(ResolveFailed):
    """Several different contracts match; `matches` lists (con_id, primary exchange)."""

    def __init__(self, symbol: str, matches: tuple[tuple[int, str], ...]) -> None:
        super().__init__(symbol, f"{len(matches)} different contracts match {list(matches)}")
        self.matches = matches


class ResolveError(ResolveFailed):
    """IB answered the request with an error (`code`, `text`) or an unusable contract."""

    def __init__(self, symbol: str, message: str, code: int | None = None) -> None:
        super().__init__(symbol, message)
        self.code = code


class ResolveTimeout(ResolveFailed):
    """No complete answer within the timeout of the injected clock."""


class ResolveConnectionClosed(ResolveFailed):
    """The connection closed before IB finished answering."""


class ResolveConnectionLost(ResolveFailed):
    """IB reported lost connectivity (1100) before it finished answering."""


class ResolveDataLost(ResolveFailed):
    """IB restored the connection with code 1101 (data lost) before it finished answering."""


@dataclass(frozen=True)
class StockInfo:
    """What `resolve_stock` found: one stock, as IB identifies it."""

    symbol: str
    con_id: int
    primary_exchange: str
    currency: str
    min_tick: float
    time_zone_id: str
    liquid_hours: str = field(default="", compare=False)
    """The contract details' regular trading hours (already part of the answer; not a new request).
    Not part of the comparison: two lookups of one stock are the same stock."""

    def contract(self) -> Contract:
        """A new STK / SMART / USD contract carrying the conId and primary exchange."""
        return stock(self.symbol, self.primary_exchange, self.con_id)


@dataclass(frozen=True)
class HistoryResult:
    """The outcome of `wait_history`: everything that arrived so far and why the wait stopped."""

    req_id: int
    bars: tuple[events.HistoricalBar, ...]
    end: events.HistoricalDataEnd | None
    errors: tuple[events.IbError, ...]
    outcome: Outcome
    late_bars: int = 0
    """Bars that arrived after the end marker of a one-shot request (not in `bars`)."""

    @property
    def failures(self) -> tuple[events.IbError, ...]:
        """The errors that are not IB notices (codes 2100 to 2199)."""
        return tuple(e for e in self.errors if e.code not in NOTICE_CODES)


@dataclass
class _Issued:
    kind: str  # "history", "quotes" or "contract"
    live: bool = False  # a subscription that stays open until cancelled
    box: deque[Any] | None = None  # events read with next_event_for (or by resolve_stock)
    bars: list[events.HistoricalBar] = field(default_factory=list)
    errors: list[events.IbError] = field(default_factory=list)
    end: events.HistoricalDataEnd | None = None
    late_bars: int = 0
    failed: bool = False  # history: a non-notice error arrived (the request is dead at IB)
    data_lost: bool = False  # a 1101 restore arrived while this was in flight
    stream_lost: bool = False  # data_lost, not yet reported to a stream reader

    @property
    def finished(self) -> bool:
        """A one-shot history request that has delivered its end marker."""
        return self.end is not None and not self.live

    @property
    def dead(self) -> bool:
        """IB no longer holds this request, so a cancel has nothing to cancel."""
        return self.finished or self.failed or self.data_lost


class IbMarketData:
    """The data face of an open `IbConnection` (see the module docstring)."""

    def __init__(
        self,
        connection: IbConnection,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.05,
    ) -> None:
        if isinstance(poll_interval, bool) or not (
            isinstance(poll_interval, int | float) and 0 < poll_interval <= 10
        ):
            raise ValueError(f"poll_interval must be above 0 and at most 10, got {poll_interval!r}")
        self._connection = connection
        self._clock = clock
        self._poll = float(poll_interval)
        self._lock = threading.RLock()
        self._waiter = threading.Lock()
        self._issued: dict[int, _Issued] = {}
        self._dropped: set[int] = set()
        self._dropped_fifo: deque[int] = deque()
        self._dropped_floor = -1  # ids up to here that are not open count as dropped too
        self._general: deque[ConnectionEvent] = deque(maxlen=_GENERAL_LIMIT)
        self._closed = False
        self._lost = False
        self._time_pending = False
        self._time_lost = False
        self.late_events = 0
        self.quote_overflow = 0
        self.update_overflow = 0

    @property
    def general_events(self) -> tuple[ConnectionEvent, ...]:
        """Events not filed under a request of this object (a snapshot, oldest first)."""
        with self._lock:
            return tuple(self._general)

    # -- requests -----------------------------------------------------------------

    def request_history(
        self,
        contract: Contract,
        end_utc: datetime | None,
        duration: str,
        *,
        use_rth: bool = True,
        keep_up_to_date: bool = False,
    ) -> int:
        """Request 1-minute TRADES bars with epoch times (`formatDate=2`); returns the request id.

        `end_utc` is an aware datetime (any zone; sent as UTC through
        `ib.history.format_end_date_time`) or None for "now". `keep_up_to_date`
        needs `end_utc=None` (IB rejects an end time with it) and sends an empty
        end time. `duration` is IB's "<n> <S|D|W|M|Y>", for example "1 D".
        """
        _check_stock(contract)
        if not isinstance(duration, str) or not _DURATION.fullmatch(duration):
            raise ValueError(f"duration must look like '1 D' or '3600 S', got {duration!r}")
        if not isinstance(use_rth, bool) or not isinstance(keep_up_to_date, bool):
            raise ValueError("use_rth and keep_up_to_date must be bool")
        if keep_up_to_date and end_utc is not None:
            raise ValueError("keep_up_to_date needs end_utc=None: IB rejects an end time with it")
        end = "" if end_utc is None else format_end_date_time(end_utc)
        self._route_queued()
        req_id = self._connection.next_request_id()
        box = deque(maxlen=_UPDATE_LIMIT) if keep_up_to_date else None
        self._register(req_id, _Issued("history", live=keep_up_to_date, box=box))
        try:
            self._connection.request(
                "reqHistoricalData", req_id, contract, end, duration, DEFAULT_BAR_SIZE,
                DEFAULT_WHAT_TO_SHOW, int(use_rth), FORMAT_DATE_EPOCH, keep_up_to_date, [],
            )
        except BaseException:
            self._forget(req_id)
            raise
        return req_id

    def cancel_history(self, req_id: int) -> bool:
        """Cancel a history request and free its state; True if a cancel was sent.

        Idempotent: a second call and a closed connection send nothing and return
        False. A request that is already dead at IB (it ended and is not
        `keep_up_to_date`, it got an error, or a data-lost restore, code 1101,
        happened) sends nothing either. The caller cancels every id when done. An
        id that was not issued by `request_history` raises `ValueError`.
        """
        return self._cancel("history", req_id, "cancelHistoricalData")

    def set_market_data_type(self, market_data_type: int) -> None:
        """Choose live (1), frozen (2), delayed (3) or delayed frozen (4) quotes."""
        if type(market_data_type) is not int or market_data_type not in MARKET_DATA_TYPES:
            raise ValueError(
                f"market data type must be one of {MARKET_DATA_TYPES}, got {market_data_type!r}"
            )
        self._connection.request("reqMarketDataType", market_data_type)

    def request_quotes(self, contract: Contract) -> int:
        """Stream quotes for `contract`; returns the request id.

        Never a snapshot and never a regulatory snapshot (IB bills those).
        """
        _check_stock(contract)
        self._route_queued()
        req_id = self._connection.next_request_id()
        self._register(req_id, _Issued("quotes", live=True, box=deque(maxlen=_QUOTE_LIMIT)))
        try:
            self._connection.request("reqMktData", req_id, contract, "", False, False, [])
        except BaseException:
            self._forget(req_id)
            raise
        return req_id

    def cancel_quotes(self, req_id: int) -> bool:
        """Cancel a quote stream; idempotent and silent after the connection closed.

        After a data-lost restore the cancel is still sent: IB may have kept the stream.
        """
        return self._cancel("quotes", req_id, "cancelMktData")

    def request_server_time(self) -> None:
        """Ask for IB's clock; read the answer with `wait_server_time`.

        `reqCurrentTime` has no request id, so any `CurrentTime` already queued or
        filed (a late answer to an earlier request) is discarded first: the wait only
        returns an answer that arrived after this request. Issue at most one at a time.
        """
        self._route_queued()
        with self._lock:
            self._general = deque(
                (e for e in self._general if not isinstance(e, events.CurrentTime)),
                maxlen=_GENERAL_LIMIT,
            )
            self._time_pending = True
            self._time_lost = False
        self._connection.request("reqCurrentTime")

    # -- waiting ------------------------------------------------------------------

    def wait_history(self, req_id: int, timeout: float) -> HistoryResult:
        """Everything received so far for one history request, once it ends or `timeout` passes.

        Waits for the end marker, an error, a data-lost restore, a close or the
        deadline (see the outcome table in the module docstring). Bars accumulate
        inside the request: every result holds all bars so far, in arrival order.
        """
        _check_timeout(timeout)
        info = self._known(req_id, "history")
        with self._waiting():
            deadline = self._clock() + timeout
            outcome = self._wait(deadline, lambda: self._history_state(info))
            with self._lock:
                return HistoryResult(
                    req_id, tuple(info.bars), info.end, tuple(info.errors), _outcome(outcome),
                    info.late_bars,
                )

    def poll_event(self, req_id: int, timeout: float) -> events.Event | Outcome:
        """The next event of a stream (quotes or `keep_up_to_date` history), or why none came.

        Returns an `Outcome` (`TIMEOUT`, `CLOSED`, `LOST` or `DATA_LOST`) instead of an
        event. `DATA_LOST` comes once, after the events already queued: the stream is
        gone at IB, so cancel the id and request it again.
        """
        _check_timeout(timeout)
        info = self._known(req_id, None)
        with self._waiting():
            if info.box is None or info.kind == "contract":
                raise ValueError(f"request id {req_id!r} is not a stream of this object")
            deadline = self._clock() + timeout
            outcome = self._wait(deadline, lambda: self._stream_state(info))
        if outcome == _READY:
            event = self._take(info)
            if event is not None:
                return event
            return TIMEOUT  # a cancel emptied the box under the wait
        return _outcome(outcome)

    def next_event_for(self, req_id: int, timeout: float) -> events.Event | None:
        """The next event of a stream, or None on timeout, loss or a closed connection.

        Raises `DataLost` once when IB lost the stream (code 1101). Use `poll_event` to tell
        the Nones apart.
        """
        result = self.poll_event(req_id, timeout)
        if isinstance(result, Outcome):
            if result is DATA_LOST:
                raise DataLost(req_id)
            return None
        return result

    def poll_server_time(self, timeout: float) -> datetime | Outcome:
        """IB's time as an aware UTC datetime, or an `Outcome` saying why there is none."""
        _check_timeout(timeout)
        found: list[events.CurrentTime] = []
        with self._waiting():
            deadline = self._clock() + timeout
            outcome = self._wait(deadline, lambda: self._time_state(found))
        if outcome == _READY:
            return datetime.fromtimestamp(found[0].time, tz=UTC)
        return _outcome(outcome)

    def wait_server_time(self, timeout: float) -> datetime | None:
        """IB's time as an aware UTC datetime, or None on timeout, loss or a closed connection.

        Raises `DataLost` once when the request was lost by a code 1101 restore.
        """
        result = self.poll_server_time(timeout)
        if isinstance(result, Outcome):
            if result is DATA_LOST:
                raise DataLost(None)
            return None
        return result

    def resolve_stock(self, symbol: str, timeout: float = 10.0) -> StockInfo:
        """Look `symbol` up (`reqContractDetails`); exactly one contract must match.

        Raises `ResolveNotFound`, `ResolveAmbiguous`, `ResolveError`, `ResolveTimeout`,
        `ResolveConnectionClosed`, `ResolveConnectionLost` or `ResolveDataLost` (all
        `ResolveFailed`). An invalid symbol raises `ValueError`, and a wait that is
        already running raises `ConcurrentWait`, both before anything is sent.
        """
        check_symbol(symbol)
        _check_timeout(timeout)
        found: list[events.ContractDetailsEvent] = []
        with self._waiting():  # before the send: a refused resolve must not ask IB anything
            self._route_all_queued()
            req_id = self._connection.next_request_id()
            info = _Issued("contract", box=deque())
            self._register(req_id, info)
            try:
                deadline = self._clock() + timeout
                self._connection.request("reqContractDetails", req_id, stock(symbol))
                while True:
                    event = self._take(info)
                    if event is None:
                        outcome = self._wait(deadline, lambda: self._contract_state(info))
                        if outcome != _READY:
                            raise _OUTCOME_ERRORS[_outcome(outcome)](
                                symbol, _OUTCOME_TEXT[_outcome(outcome)]
                            )
                        continue
                    if isinstance(event, events.ContractDetailsEvent):
                        found.append(event)
                    elif isinstance(event, events.ContractDetailsEnd):
                        break
                    elif isinstance(event, events.IbError) and event.code not in NOTICE_CODES:
                        if event.code == NO_SECURITY_DEFINITION:
                            raise ResolveNotFound(symbol, f"IB error {event.code}: {event.message}")
                        raise ResolveError(
                            symbol, f"IB error {event.code}: {event.message}", event.code
                        )
            finally:
                self._forget(req_id)  # there is no cancel for contract details: drop late answers
        return _single_stock(symbol, found)

    # -- internals ----------------------------------------------------------------

    def _register(self, req_id: int, info: _Issued) -> None:
        with self._lock:
            self._issued[req_id] = info

    def _forget(self, req_id: int) -> None:
        with self._lock:
            self._issued.pop(req_id, None)
            if req_id in self._dropped:
                return
            self._dropped.add(req_id)
            self._dropped_fifo.append(req_id)
            while len(self._dropped_fifo) > _DROPPED_LIMIT:
                oldest = self._dropped_fifo.popleft()
                self._dropped.discard(oldest)
                self._dropped_floor = max(self._dropped_floor, oldest)

    def _is_dropped(self, req_id: int) -> bool:
        return req_id in self._dropped or req_id <= self._dropped_floor

    def _known(self, req_id: int, kind: str | None) -> _Issued:
        with self._lock:
            info = self._issued.get(req_id)
        if info is None or (kind is not None and info.kind != kind):
            raise ValueError(f"request id {req_id!r} is not an open {kind or 'request'} of this object")
        return info

    def _cancel(self, kind: str, req_id: int, name: str) -> bool:
        with self._lock:
            info = self._issued.get(req_id)
            if info is None and self._is_dropped(req_id):
                return False  # already cancelled
            if info is None or info.kind != kind:
                raise ValueError(f"request id {req_id!r} is not a {kind} request of this object")
            send = not info.dead or (info.live and info.data_lost)
            self._forget(req_id)
        if not send:
            return False
        try:
            self._connection.request(name, req_id)
        except NotConnected:
            return False
        return True

    def _route_queued(self) -> None:
        """Route every event already queued, as the one reader (`ConcurrentWait` if a wait runs)."""
        with self._waiting():
            self._route_all_queued()

    def _route_all_queued(self) -> None:
        while True:
            event = self._connection.next_event(0.0)
            if event is None:
                return
            self._route(event)

    @contextmanager
    def _waiting(self) -> Iterator[None]:
        if not self._waiter.acquire(blocking=False):
            raise ConcurrentWait(
                "another wait is running on this IbMarketData: the connection's event stream "
                "has one consumer, so waits cannot overlap"
            )
        try:
            yield
        finally:
            self._waiter.release()

    def _take(self, info: _Issued) -> Any:
        with self._lock:
            box = info.box
            return box.popleft() if box else None

    # What each wait is reading for. Each returns an Outcome (decided), _READY (the item is
    # there) or None (keep waiting); they run under the lock so a state change is seen whole.

    def _history_state(self, info: _Issued) -> str | None:
        with self._lock:
            if info.data_lost:
                return DATA_LOST
            if info.failed:
                return ERROR
            if info.end is not None:
                return END
        return None

    def _stream_state(self, info: _Issued) -> str | None:
        with self._lock:
            if info.box:
                if info.kind == "history" and _is_failure(info.box[0]):
                    return ERROR  # sticky: the error stays at the head of the box
                return _READY
            if info.kind == "history" and info.failed:
                return ERROR
            if info.stream_lost:
                info.stream_lost = False  # reported once
                return DATA_LOST
        return None

    def _contract_state(self, info: _Issued) -> str | None:
        with self._lock:
            if info.box:
                return _READY
            if info.data_lost:
                return DATA_LOST
        return None

    def _time_state(self, found: list[events.CurrentTime]) -> str | None:
        with self._lock:
            for item in self._general:
                if isinstance(item, events.CurrentTime):
                    self._general.remove(item)
                    found.append(item)
                    return _READY
            if self._time_lost:
                self._time_lost = False  # reported once
                return DATA_LOST
        return None

    def _wait(self, deadline: float, decide: Callable[[], str | None]) -> str:
        """Read connection events, filing each, until `decide()` says so; else why not.

        Returns an `Outcome` value or `_READY`. The deadline is checked on every
        pass, after each routed event included, so traffic for other requests cannot
        postpone it. While lost the wait keeps reading (blocking up to `poll_interval`
        per read) until the deadline, and then reports `LOST` rather than `TIMEOUT`.
        """
        while True:
            decided = decide()
            if decided is not None:
                return decided
            with self._lock:
                closed = self._closed
            if closed:
                return CLOSED
            expired = self._clock() >= deadline
            event = self._connection.next_event(0.0 if expired else self._poll)
            if event is not None:
                self._route(event)
                if not expired:
                    continue
                decided = decide()  # the last event may be the one this wait wanted
                return decided if decided is not None else self._gave_up()
            if self._connection.state is ConnState.CLOSED:
                return CLOSED
            if expired:
                return self._gave_up()

    def _gave_up(self) -> str:
        with self._lock:
            if self._closed:
                return CLOSED
            return LOST if self._lost else TIMEOUT

    def _route(self, event: ConnectionEvent) -> None:
        with self._lock:
            if isinstance(event, events.ConnectionClosed | ReaderStopped):
                self._closed = True
            elif isinstance(event, ConnectionLost):
                self._lost = True
            elif isinstance(event, ConnectionRestored):
                self._lost = False
                if event.data_lost:
                    self._mark_data_lost()
            elif isinstance(event, events.CurrentTime):
                self._time_pending = False
            req_id = getattr(event, "req_id", -1)
            if isinstance(req_id, int) and req_id >= 0:
                info = self._issued.get(req_id)
                if info is not None:
                    self._file(info, event)
                    return
                if self._is_dropped(req_id):
                    self.late_events += 1
                    return
            self._general.append(event)

    def _mark_data_lost(self) -> None:
        """A 1101 restore: every request still in flight is dead at IB (lock held)."""
        for info in self._issued.values():
            if info.dead:
                continue
            info.data_lost = True
            info.stream_lost = info.live
        if self._time_pending:
            self._time_pending = False
            self._time_lost = True

    def _file(self, info: _Issued, event: ConnectionEvent) -> None:
        """Put an event of a known request where its reader looks (lock held)."""
        if info.kind != "history":
            self._box(info, event)
            return
        if isinstance(event, events.HistoricalBar):
            if info.end is None:
                info.bars.append(event)
            elif info.live:
                self._box(info, event)  # an update of a keep_up_to_date request
            else:
                info.late_bars += 1
        elif isinstance(event, events.HistoricalDataEnd):
            if info.end is None:
                info.end = event
        elif isinstance(event, events.IbError):
            if len(info.errors) < _ERROR_LIMIT:
                info.errors.append(event)
            if event.code not in NOTICE_CODES and not info.finished:
                info.failed = True
                if info.live and info.end is not None:
                    self._box(info, event)  # the stream reader sees it: poll_event gives error

    def _box(self, info: _Issued, event: ConnectionEvent) -> None:
        box = info.box
        if box is None:
            return
        if box.maxlen is not None and len(box) == box.maxlen:
            if info.kind == "quotes":
                self.quote_overflow += 1
            else:
                self.update_overflow += 1
        box.append(event)


_OUTCOME_ERRORS: dict[Outcome, type[ResolveFailed]] = {
    TIMEOUT: ResolveTimeout,
    CLOSED: ResolveConnectionClosed,
    LOST: ResolveConnectionLost,
    DATA_LOST: ResolveDataLost,
}
_OUTCOME_TEXT: dict[Outcome, str] = {
    TIMEOUT: "no complete answer from IB in time",
    CLOSED: "the connection closed before IB answered",
    LOST: "IB reported lost connectivity (1100) before it answered",
    DATA_LOST: "IB restored the connection with code 1101 (data lost) before it answered",
}


def _outcome(value: str) -> Outcome:
    return Outcome(value)


def _is_failure(event: object) -> bool:
    return isinstance(event, events.IbError) and event.code not in NOTICE_CODES


def _check_timeout(timeout: float) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError(f"timeout must be a finite number of 0 or more, got {timeout!r}")


def _check_stock(contract: Contract) -> None:
    if not isinstance(contract, Contract):
        raise ValueError(f"contract must be an ibapi Contract, got {type(contract).__name__}")
    if contract.secType != SEC_TYPE:
        raise ValueError(f"only stock contracts are supported, got secType {contract.secType!r}")
    check_primary_exchange(contract.primaryExchange)


def _single_stock(symbol: str, found: list[events.ContractDetailsEvent]) -> StockInfo:
    by_id: dict[int, events.ContractDetailsEvent] = {}
    for details in found:
        by_id.setdefault(details.con_id, details)
    if not by_id:
        raise ResolveNotFound(symbol, "IB returned no contract")
    if len(by_id) > 1:
        raise ResolveAmbiguous(
            symbol, tuple((d.con_id, d.primary_exchange) for d in by_id.values())
        )
    (details,) = by_id.values()
    if details.sec_type != SEC_TYPE or details.symbol != symbol or details.con_id <= 0:
        raise ResolveError(
            symbol,
            f"IB returned {details.sec_type} {details.symbol!r} conId {details.con_id}, "
            "not this stock",
        )
    info = StockInfo(
        symbol, details.con_id, details.primary_exchange, details.currency,
        details.min_tick, details.time_zone_id, liquid_hours=details.liquid_hours or "",
    )
    try:
        info.contract()
    except ValueError as exc:
        raise ResolveError(symbol, f"IB's answer is unusable: {exc}") from exc
    return info
