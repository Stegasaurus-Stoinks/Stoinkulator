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

* `wait_history(req_id, timeout)` collects the bars of one history request into a
  `HistoryResult`: the raw `events.HistoricalBar` list (convert each with
  `ib.bars.bar_from_ib`), the end marker, every `events.IbError` of that request
  and an `outcome` (`END`, `ERROR`, `TIMEOUT`, `CLOSED`, `LOST`). It never raises
  for those outcomes: the fetch driver classifies them. IB error 162 means both
  "pacing violation" and "no data": tell them apart by `IbError.message`.
  Codes 2100 to 2199 are IB notices, kept in `errors` but not an `ERROR` outcome.
* `next_event_for(req_id, timeout)` returns the next event of one request (quotes,
  or the updates of a `keep_up_to_date` history request).
* `resolve_stock(symbol, timeout)` is request plus wait in one call and raises a
  distinct `ResolveFailed` subclass for every failure.
* `wait_server_time(timeout)` answers `request_server_time()`.

Events and consumers
--------------------
`IbConnection.next_event` has one consumer, and this object is it: once an
`IbMarketData` exists, nothing else may call `connection.next_event`. Every event
read is filed under its request id, so a wait for one request never consumes or
loses the events of another; events of ids this object did not issue and events
without a request id (farm notices, `ConnectionLost`, `ConnectionRestored`, ...)
go to a bounded general list (`general_events`). Events of a request that was
cancelled (or whose wait gave up) are dropped and counted in `late_events`.

Threads: the request and cancel methods are thread-safe (the connection
serialises sends). The wait methods are single-consumer: a second wait started
while another is running, on any thread, raises `ConcurrentWait` at once rather
than splitting the event stream. All waits use the injected `clock` (pass the
connection's clock) and poll the connection in slices of `poll_interval`; none
sleeps.

Connection loss: a wait returns `CLOSED` once the connection has closed (a
`ConnectionClosed` or `ReaderStopped` event, or its state CLOSED with nothing
left to read) and `LOST` while IB code 1100 is in force (until 1101 or 1102
arrives). After `LOST` or a data-lost restore (1101) the caller re-requests.
Cancels after the connection closed are silent no-ops.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
from MainStoinker.ib.contracts import SEC_TYPE, stock
from MainStoinker.ib.history import (
    DEFAULT_BAR_SIZE,
    DEFAULT_WHAT_TO_SHOW,
    check_symbol,
    format_end_date_time,
)

END = "end"
ERROR = "error"
TIMEOUT = "timeout"
CLOSED = "closed"
LOST = "lost"
_EVENT = "event"

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


class ConcurrentWait(RuntimeError):
    """A wait was started while another wait on the same `IbMarketData` was running."""


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


@dataclass(frozen=True)
class StockInfo:
    """What `resolve_stock` found: one stock, as IB identifies it."""

    symbol: str
    con_id: int
    primary_exchange: str
    currency: str
    min_tick: float
    time_zone_id: str

    def contract(self) -> Contract:
        """A new STK / SMART / USD contract carrying the conId and primary exchange."""
        return stock(self.symbol, self.primary_exchange, self.con_id)


@dataclass(frozen=True)
class HistoryResult:
    """The outcome of `wait_history`: what arrived and why the wait stopped."""

    req_id: int
    bars: tuple[events.HistoricalBar, ...]
    end: events.HistoricalDataEnd | None
    errors: tuple[events.IbError, ...]
    outcome: str  # END, ERROR, TIMEOUT, CLOSED or LOST

    @property
    def failures(self) -> tuple[events.IbError, ...]:
        """The errors that are not IB notices (codes 2100 to 2199)."""
        return tuple(e for e in self.errors if e.code not in NOTICE_CODES)


@dataclass
class _Issued:
    kind: str  # "history", "quotes" or "contract"
    live: bool = False  # a subscription that stays open until cancelled
    finished: bool = False  # a one-shot history request that has delivered its end marker


class IbMarketData:
    """The data face of an open `IbConnection` (see the module docstring)."""

    def __init__(
        self,
        connection: IbConnection,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.05,
    ) -> None:
        if not (isinstance(poll_interval, int | float) and 0 < poll_interval <= 10):
            raise ValueError(f"poll_interval must be above 0 and at most 10, got {poll_interval!r}")
        self._connection = connection
        self._clock = clock
        self._poll = float(poll_interval)
        self._lock = threading.RLock()
        self._waiter = threading.Lock()
        self._issued: dict[int, _Issued] = {}
        self._boxes: dict[int, deque[Any]] = {}
        self._dropped: set[int] = set()
        self._general: deque[ConnectionEvent] = deque(maxlen=_GENERAL_LIMIT)
        self._closed = False
        self._lost = False
        self.late_events = 0
        self.quote_overflow = 0

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
        req_id = self._connection.next_request_id()
        self._register(req_id, _Issued("history", live=keep_up_to_date), None)
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
        """Cancel a history request; True if a cancel was sent.

        Idempotent: a second call, a request that has already ended (not
        `keep_up_to_date`) and a closed connection all send nothing and return
        False. An id that was not issued by `request_history` raises `ValueError`.
        """
        return self._cancel("history", req_id, "cancelHistoricalData")

    def set_market_data_type(self, market_data_type: int) -> None:
        """Choose live (1), frozen (2), delayed (3) or delayed frozen (4) quotes."""
        if isinstance(market_data_type, bool) or market_data_type not in MARKET_DATA_TYPES:
            raise ValueError(
                f"market data type must be one of {MARKET_DATA_TYPES}, got {market_data_type!r}"
            )
        self._connection.request("reqMarketDataType", market_data_type)

    def request_quotes(self, contract: Contract) -> int:
        """Stream quotes for `contract`; returns the request id.

        Never a snapshot and never a regulatory snapshot (IB bills those).
        """
        _check_stock(contract)
        req_id = self._connection.next_request_id()
        self._register(req_id, _Issued("quotes", live=True), _QUOTE_LIMIT)
        try:
            self._connection.request("reqMktData", req_id, contract, "", False, False, [])
        except BaseException:
            self._forget(req_id)
            raise
        return req_id

    def cancel_quotes(self, req_id: int) -> bool:
        """Cancel a quote stream; idempotent and silent after the connection closed."""
        return self._cancel("quotes", req_id, "cancelMktData")

    def request_server_time(self) -> None:
        """Ask for IB's clock; read the answer with `wait_server_time`."""
        with self._lock:
            self._general = deque(
                (e for e in self._general if not isinstance(e, events.CurrentTime)),
                maxlen=_GENERAL_LIMIT,
            )
        self._connection.request("reqCurrentTime")

    # -- waiting ------------------------------------------------------------------

    def wait_history(self, req_id: int, timeout: float) -> HistoryResult:
        """Collect one history request until its end marker, an error, loss or `timeout`."""
        self._known(req_id, "history")
        bars: list[events.HistoricalBar] = []
        errors: list[events.IbError] = []
        end: events.HistoricalDataEnd | None = None
        deadline = self._clock() + timeout
        with self._waiting():
            while True:
                event, why = self._pump_until(
                    lambda: self._take(req_id), max(0.0, deadline - self._clock())
                )
                if event is None:
                    outcome = why
                    break
                if isinstance(event, events.HistoricalBar):
                    bars.append(event)
                elif isinstance(event, events.HistoricalDataEnd):
                    end = event
                    outcome = END
                    with self._lock:
                        info = self._issued.get(req_id)
                        if info is not None and not info.live:
                            info.finished = True
                    break
                elif isinstance(event, events.IbError):
                    errors.append(event)
                    if event.code not in NOTICE_CODES:
                        outcome = ERROR
                        break
        return HistoryResult(req_id, tuple(bars), end, tuple(errors), outcome)

    def next_event_for(self, req_id: int, timeout: float) -> events.Event | None:
        """The next event of request `req_id`, or None on timeout, loss or a closed connection."""
        self._known(req_id, None)
        deadline = self._clock() + timeout
        with self._waiting():
            event, _ = self._pump_until(
                lambda: self._take(req_id), max(0.0, deadline - self._clock())
            )
        return event

    def wait_server_time(self, timeout: float) -> datetime | None:
        """IB's time as an aware UTC datetime, or None on timeout, loss or a closed connection."""
        with self._waiting():
            event, _ = self._pump_until(self._take_server_time, timeout)
        if event is None:
            return None
        return datetime.fromtimestamp(event.time, tz=UTC)

    def resolve_stock(self, symbol: str, timeout: float = 10.0) -> StockInfo:
        """Look `symbol` up (`reqContractDetails`); exactly one contract must match.

        Raises `ResolveNotFound`, `ResolveAmbiguous`, `ResolveError`, `ResolveTimeout`,
        `ResolveConnectionClosed` or `ResolveConnectionLost` (all `ResolveFailed`).
        An invalid symbol raises `ValueError` before anything is sent.
        """
        check_symbol(symbol)
        req_id = self._connection.next_request_id()
        self._register(req_id, _Issued("contract"), None)
        found: list[events.ContractDetailsEvent] = []
        deadline = self._clock() + timeout
        try:
            self._connection.request("reqContractDetails", req_id, stock(symbol))
            with self._waiting():
                while True:
                    event, why = self._pump_until(
                        lambda: self._take(req_id), max(0.0, deadline - self._clock())
                    )
                    if event is None:
                        raise _OUTCOME_ERRORS[why](symbol, _OUTCOME_TEXT[why])
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

    def _register(self, req_id: int, info: _Issued, limit: int | None) -> None:
        with self._lock:
            self._issued[req_id] = info
            self._boxes[req_id] = deque(maxlen=limit)

    def _forget(self, req_id: int) -> None:
        with self._lock:
            self._issued.pop(req_id, None)
            self._boxes.pop(req_id, None)
            self._dropped.add(req_id)

    def _known(self, req_id: int, kind: str | None) -> None:
        with self._lock:
            info = self._issued.get(req_id)
        if info is None or (kind is not None and info.kind != kind):
            raise ValueError(f"request id {req_id!r} is not an open {kind or 'request'} of this object")

    def _cancel(self, kind: str, req_id: int, name: str) -> bool:
        with self._lock:
            info = self._issued.get(req_id)
            if info is None and req_id in self._dropped:
                return False  # already cancelled
            if info is None or info.kind != kind:
                raise ValueError(f"request id {req_id!r} is not a {kind} request of this object")
            send = not info.finished
            self._forget(req_id)
        if not send:
            return False
        try:
            self._connection.request(name, req_id)
        except NotConnected:
            return False
        return True

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

    def _take(self, req_id: int) -> events.Event | None:
        with self._lock:
            box = self._boxes.get(req_id)
            return box.popleft() if box else None

    def _take_server_time(self) -> events.CurrentTime | None:
        with self._lock:
            for item in self._general:
                if isinstance(item, events.CurrentTime):
                    self._general.remove(item)
                    return item
        return None

    def _pump_until(
        self, take: Callable[[], Any], timeout: float
    ) -> tuple[Any, str]:
        """Read connection events, filing each, until `take()` yields one; else why not."""
        deadline = self._clock() + timeout
        while True:
            item = take()
            if item is not None:
                return item, _EVENT
            with self._lock:
                closed, lost = self._closed, self._lost
            if closed:
                return None, CLOSED
            # While lost, only what is already queued is read (a restore may be in it).
            event = self._connection.next_event(0.0 if lost else self._poll)
            if event is not None:
                self._route(event)
                continue
            if lost:
                return None, LOST
            if self._connection.state is ConnState.CLOSED:
                return None, CLOSED
            if self._clock() >= deadline:
                return None, TIMEOUT

    def _route(self, event: ConnectionEvent) -> None:
        with self._lock:
            if isinstance(event, events.ConnectionClosed | ReaderStopped):
                self._closed = True
            elif isinstance(event, ConnectionLost):
                self._lost = True
            elif isinstance(event, ConnectionRestored):
                self._lost = False
            req_id = getattr(event, "req_id", -1)
            if isinstance(req_id, int) and req_id >= 0:
                box = self._boxes.get(req_id)
                if box is not None:
                    if box.maxlen is not None and len(box) == box.maxlen:
                        self.quote_overflow += 1
                    box.append(event)
                    return
                if req_id in self._dropped:
                    self.late_events += 1
                    return
            self._general.append(event)


_OUTCOME_ERRORS: dict[str, type[ResolveFailed]] = {
    TIMEOUT: ResolveTimeout,
    CLOSED: ResolveConnectionClosed,
    LOST: ResolveConnectionLost,
}
_OUTCOME_TEXT: dict[str, str] = {
    TIMEOUT: "no complete answer from IB in time",
    CLOSED: "the connection closed before IB answered",
    LOST: "IB reported lost connectivity (1100) before it answered",
}


def _check_stock(contract: Contract) -> None:
    if not isinstance(contract, Contract):
        raise ValueError(f"contract must be an ibapi Contract, got {type(contract).__name__}")
    if contract.secType != SEC_TYPE:
        raise ValueError(f"only stock contracts are supported, got secType {contract.secType!r}")


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
        details.min_tick, details.time_zone_id,
    )
    try:
        info.contract()
    except ValueError as exc:
        raise ResolveError(symbol, f"IB's answer is unusable: {exc}") from exc
    return info
