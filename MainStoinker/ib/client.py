"""A data-only Interactive Brokers client.

`DataOnlyClient` is an `EClient` + `EWrapper` that can read market data and
cannot change anything at TWS or on the account. It may be connected to a
live account in a data-only session, so the policy is an ALLOWLIST: an
outgoing message is refused unless its id is in `ALLOWED_MESSAGE_IDS` (the ten
ids stage 1 needs). A message id that ibapi adds later is refused until a
human adds it. Order, FA-replace, auto-open-orders, display-group and
log-level messages are all outside the list.

Two layers enforce it, both before anything reaches the socket:

1. Method level. `sendMsg` takes the id as an argument and refuses anything
   that is not exactly an `int` in the allowlist; `sendMsgProtoBuf` refuses
   everything (no allowed id has a protobuf form in ibapi 10.37.2, a test pins
   this). Both raise `MessageRefused`, a `RuntimeError` and deliberately not an
   `OSError`, so nothing in ibapi can swallow it.
2. Connection level. `client.conn` is wrapped, whenever it is assigned (also
   by `EClient.connect` itself), in a guard that forwards everything to the
   `ibapi.connection.Connection` but whose `sendMsg(raw_bytes)` parses the
   message id out of the frame (text id below server version 201, raw 4-byte
   id from 201; the `API\\0` handshake is the one frame without an id and is
   recognised exactly) and refuses anything not allowed, or that it cannot
   parse. This closes `EClient.sendMsg(client, 3, ...)` and `client.conn.sendMsg(frame)`.

`connect` refuses a port that is not an exact `int`, then runs the port guard
(`check_broker_port`) before `EClient.connect`, as the legacy `IBapi.connect`
does. The guard reads the environment mapping given to the constructor at call
time. `startApi` always sends an empty optional-capabilities string.

The handshake frame is accepted only as the first frame passed on by a given
guarded connection object (one bit of state per connection).

Out of scope, because Python cannot prevent it: code that replaces the guard
by writing the private attributes (`client._conn`, `client.__dict__`), that
reaches past it through `client.conn._inner` (the wrapped `Connection`) or
`client.conn.socket` (its raw socket), or that talks to a socket of its own.
Callers must be reviewed for that; the import boundary test (no `socket`) is
what stops this package doing it. Building a raw `EClient` or `Connection`
anywhere else in `MainStoinker/ib` is forbidden by convention (issue 115:
this module is the only one that imports ibapi's client, wrapper and
connection) and is not yet enforced by a test.

Callbacks only turn their arguments into events (`MainStoinker.ib.events`) and
hand them to `on_event`; they never call the client and never raise into
ibapi. Their signatures are those of `EWrapper` in ibapi 10.37, which is why
they carry no return annotation (the tests compare signatures for equality).
A consumer hook that raises is counted in `callback_failures`; the first
failure per hook is kept in `first_failures` and logged once at error level.

Threads, retries and queues are not here.
"""

import logging
import re
import struct
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData, TagValueList, TickAttrib, TickerId
from ibapi.contract import Contract, ContractDetails
from ibapi.message import OUT
from ibapi.ticktype import TickType
from ibapi.wrapper import EWrapper

from MainStoinker.ib import events
from MainStoinker.Util.broker_guard import BrokerPortRefused, check_broker_port

ALLOWED_MESSAGE_IDS: frozenset[int] = frozenset(
    {
        OUT.REQ_MKT_DATA,
        OUT.CANCEL_MKT_DATA,
        OUT.REQ_CONTRACT_DATA,
        OUT.REQ_HISTORICAL_DATA,
        OUT.CANCEL_HISTORICAL_DATA,
        OUT.REQ_CURRENT_TIME,
        OUT.REQ_MARKET_DATA_TYPE,
        OUT.START_API,
        OUT.REQ_HEAD_TIMESTAMP,
        OUT.CANCEL_HEAD_TIMESTAMP,
    }
)
"""The only outgoing message ids the data-only client sends."""

ORDER_MESSAGE_IDS: frozenset[int] = frozenset(
    {OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.EXERCISE_OPTIONS, OUT.REQ_GLOBAL_CANCEL}
)
"""Documentation and tests only: the ids that place, cancel or exercise an
order. They are refused because they are not in `ALLOWED_MESSAGE_IDS`; this set
is not consulted by the check."""

_OUT_NAMES: dict[int, str] = {
    v: k for k, v in vars(OUT).items() if not k.startswith("_") and type(v) is int
}
_PROTOBUF_OFFSET = 200  # ibapi.common.PROTOBUF_MSG_ID


class MessageRefused(RuntimeError):
    """An outgoing message was refused by the data-only client (not allowlisted).

    Deliberately not an OSError, so nothing in ibapi can swallow it.
    """


def _describe(msg_id: object) -> str:
    if type(msg_id) is int:
        if msg_id in _OUT_NAMES:
            return f"{msg_id!r} ({_OUT_NAMES[msg_id]})"
        if msg_id - _PROTOBUF_OFFSET in _OUT_NAMES:
            return f"{msg_id!r} ({_OUT_NAMES[msg_id - _PROTOBUF_OFFSET]}, protobuf form)"
        return f"{msg_id!r} (not an ibapi OUT id)"
    return f"{msg_id!r} (not an int)"


def _refuse(msg_id: object, via: str) -> MessageRefused:
    return MessageRefused(
        f"The data-only IB client refuses outgoing message id {_describe(msg_id)} "
        f"({via}): only {sorted(ALLOWED_MESSAGE_IDS)} may be sent."
    )


def _refuse_frame(reason: str) -> MessageRefused:
    return MessageRefused(
        f"The data-only IB client refuses a frame on its connection: {reason}."
    )


def _check_id(msg_id: object) -> None:
    # Fail closed: anything that is not exactly an int is not a message id ibapi sends.
    if type(msg_id) is not int or msg_id not in ALLOWED_MESSAGE_IDS:
        raise _refuse(msg_id, "sendMsg")


# -- connection-level guard ---------------------------------------------------

_TEXT_ID = re.compile(rb"(0|[1-9][0-9]{0,5})\0")  # ibapi.comm.make_msg, server < 201
# EClient.connect sends `v<min>..<max>` and, only when `connectOptions` is a
# non-empty string, one space and the options. ibapi's own check
# (`isAsciiPrintable`) also lets tab, LF and CR through; the guard does not:
# options may contain only the printable ASCII bytes 0x20-0x7e, so a client
# that sets options with a control character fails closed in `connect`.
_HANDSHAKE_BODY = re.compile(rb"v[0-9]+\.\.[0-9]+(?: [\x20-\x7e]+)?")
_HANDSHAKE_PREFIX = b"API\0"


def _unframe(data: bytes) -> bytes | None:
    """The body of exactly one length-prefixed frame, or None."""
    if len(data) < 4:
        return None
    (length,) = struct.unpack("!I", data[:4])
    if length != len(data) - 4:
        return None
    return data[4:]


def _check_frame(raw: object, first_write: bool) -> None:
    """Raise `MessageRefused` unless `raw` is the handshake or an allowed message.

    The handshake is accepted only when `first_write` is true (the connection
    object has passed no frame on yet).

    Framing (ibapi 10.37.2): `comm.make_msg` writes a 4-byte big-endian length,
    then either `<decimal id>\\0<fields>` (server version below 201) or a raw
    4-byte big-endian id and the fields (201 and up); `comm.make_msg_proto`
    writes the length, a raw 4-byte id and the protobuf. The handshake is
    `API\\0` followed by a length-prefixed `v<min>..<max>[ options]`. The text
    and raw forms cannot be confused for an allowed id (allowed ids are below
    256, so a raw frame starts with a zero byte and a text frame with a digit).
    """
    if type(raw) is not bytes:
        raise _refuse_frame(f"frame is {type(raw).__name__}, not bytes")
    if raw.startswith(_HANDSHAKE_PREFIX):
        if not first_write:
            raise _refuse_frame("handshake frame after the first write")
        body = _unframe(raw[len(_HANDSHAKE_PREFIX):])
        if body is not None and _HANDSHAKE_BODY.fullmatch(body):
            return
        raise _refuse_frame("malformed handshake frame")
    body = _unframe(raw)
    if body is None:
        raise _refuse_frame("unparseable frame")
    match = _TEXT_ID.match(body)
    if match is not None:
        msg_id = int(match.group(1))
    elif len(body) >= 4:
        msg_id = int.from_bytes(body[:4], "big")
    else:
        raise _refuse_frame("unparseable frame")
    if msg_id not in ALLOWED_MESSAGE_IDS:
        raise _refuse(msg_id, "connection")


class _GuardedConnection:
    """Wraps an `ibapi.connection.Connection`; checks every frame it is asked to send.

    Everything else (`connect`, `disconnect`, `isConnected`, `recvMsg`, the
    attributes `EReader` and `EClient` read and write) is forwarded to the
    wrapped object. It opens and owns no socket.
    """

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_wrote", False)  # the one bit of state

    def sendMsg(self, msg: bytes):  # noqa: N802 (ibapi's name)
        _check_frame(msg, first_write=not self._wrote)
        object.__setattr__(self, "_wrote", True)
        return self._inner.sendMsg(msg)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._inner, name)

    def __repr__(self) -> str:
        return f"<guarded {self._inner!r}>"


@dataclass(frozen=True)
class CallbackFailure:
    """The first exception a consumer hook raised."""

    hook: str
    exc_type: str
    message: str
    traceback: str


class DataOnlyClient(EWrapper, EClient):
    """Market-data-only IB client. See the module docstring."""

    _conn: Any = None

    def __init__(
        self,
        env: Mapping[str, str],
        on_event: Callable[[events.Event], None],
        on_raw: Callable[[str, tuple[Any, ...]], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._failure_lock = threading.Lock()
        self.callback_failures = 0
        self.first_failures: dict[str, CallbackFailure] = {}
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._env = env
        self._on_event = on_event
        self._on_raw = on_raw

    # -- guards ------------------------------------------------------------

    @property
    def conn(self) -> Any:
        return self._conn

    @conn.setter
    def conn(self, value: Any) -> None:
        # Every assignment, including the one inside EClient.connect, is wrapped.
        if value is None or isinstance(value, _GuardedConnection):
            self._conn = value
        else:
            self._conn = _GuardedConnection(value)

    def connect(self, host: str, port: int, clientId: int):
        """Refuse a non-int port, run the port guard, then `EClient.connect`
        (which has no timeout)."""
        if type(port) is not int:
            raise BrokerPortRefused(
                f"Refusing to connect: broker port {port!r} is not an int."
            )
        check_broker_port(port, self._env)
        return super().connect(host, port, clientId)

    def startApi(self):
        # ibapi sends optionalCapabilities (a TWS feature-flag string such as
        # "+PACEAPI") after the client id; it carries nothing order related, but
        # the data-only client sends none at all.
        self.optCapab = ""
        return super().startApi()

    def reqMktData(
        self,
        reqId: TickerId,
        contract: Contract,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: TagValueList,
    ):
        # IB bills a regulatory snapshot per request (0.01 USD each in 10.37.2),
        # so only exactly False is accepted. Refused before anything is built or
        # written. (`snapshot`, `genericTickList` and `mktDataOptions` are not
        # billed or state-changing: see the review notes on issue 115.)
        if regulatorySnapshot is not False:
            raise MessageRefused(
                "The data-only IB client refuses reqMktData with "
                f"regulatorySnapshot={regulatorySnapshot!r}: regulatory snapshots "
                "are billed and are refused."
            )
        return super().reqMktData(
            reqId, contract, genericTickList, snapshot, regulatorySnapshot, mktDataOptions
        )

    def sendMsg(self, msgId: int, msg: str):
        _check_id(msgId)
        return super().sendMsg(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes):
        # None of the allowed ids has a protobuf form in ibapi 10.37.2.
        raise _refuse(msgId, "sendMsgProtoBuf")

    # -- delivery ----------------------------------------------------------

    def _record_failure(self, hook: str) -> None:
        with self._failure_lock:
            self.callback_failures += 1
            if hook in self.first_failures:
                return
            exc = sys.exc_info()[1]
            assert exc is not None
            failure = CallbackFailure(
                hook=hook,
                exc_type=type(exc).__name__,
                message=str(exc),
                traceback="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
            self.first_failures[hook] = failure
        self._logger.error(
            "IB consumer hook %r raised %s: %s (first failure; later ones are only counted)\n%s",
            hook, failure.exc_type, failure.message, failure.traceback,
        )

    def _deliver(
        self, name: str, args: tuple[Any, ...], build: Callable[[], events.Event]
    ) -> None:
        """Raw hook, then event hook; a failure in either is counted, never raised."""
        if self._on_raw is not None:
            try:
                self._on_raw(name, args)
            except Exception:
                self._record_failure("on_raw")
        try:
            self._on_event(build())
        except Exception:
            self._record_failure("on_event")

    # -- EWrapper callbacks (10.37 signatures) -----------------------------

    def error(
        self,
        reqId: TickerId,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson="",
    ):
        self._deliver(
            "error", (reqId, errorTime, errorCode, errorString, advancedOrderRejectJson),
            lambda: events.IbError(
                req_id=reqId, code=errorCode, message=errorString,
                error_time_ms=errorTime, advanced_order_reject_json=advancedOrderRejectJson,
            ),
        )

    def nextValidId(self, orderId: int):
        self._deliver("nextValidId", (orderId,), lambda: events.NextValidId(orderId))

    def connectAck(self):
        self._deliver("connectAck", (), events.ConnectAck)

    def connectionClosed(self):
        self._deliver("connectionClosed", (), events.ConnectionClosed)

    def managedAccounts(self, accountsList: str):
        self._deliver(
            "managedAccounts", (accountsList,), lambda: events.ManagedAccounts(accountsList)
        )

    def _bar(self, reqId: int, bar: BarData, update: bool) -> events.HistoricalBar:
        return events.HistoricalBar(
            req_id=reqId, date=bar.date, open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume, wap=bar.wap, bar_count=bar.barCount,
            update=update,
        )

    def historicalData(self, reqId: int, bar: BarData):
        self._deliver("historicalData", (reqId, bar), lambda: self._bar(reqId, bar, False))

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        self._deliver(
            "historicalDataEnd", (reqId, start, end),
            lambda: events.HistoricalDataEnd(reqId, start, end),
        )

    def historicalDataUpdate(self, reqId: int, bar: BarData):
        self._deliver(
            "historicalDataUpdate", (reqId, bar), lambda: self._bar(reqId, bar, True)
        )

    def contractDetails(self, reqId: int, contractDetails: ContractDetails):
        def build() -> events.ContractDetailsEvent:
            c = contractDetails.contract
            return events.ContractDetailsEvent(
                req_id=reqId, con_id=c.conId, symbol=c.symbol, sec_type=c.secType,
                exchange=c.exchange, primary_exchange=c.primaryExchange,
                currency=c.currency, local_symbol=c.localSymbol,
                long_name=contractDetails.longName, min_tick=contractDetails.minTick,
                time_zone_id=contractDetails.timeZoneId,
                trading_hours=contractDetails.tradingHours,
                liquid_hours=contractDetails.liquidHours,
            )

        self._deliver("contractDetails", (reqId, contractDetails), build)

    def contractDetailsEnd(self, reqId: int):
        self._deliver("contractDetailsEnd", (reqId,), lambda: events.ContractDetailsEnd(reqId))

    def headTimestamp(self, reqId: int, headTimestamp: str):
        self._deliver(
            "headTimestamp", (reqId, headTimestamp),
            lambda: events.HeadTimestamp(reqId, headTimestamp),
        )

    def marketDataType(self, reqId: TickerId, marketDataType: int):
        self._deliver(
            "marketDataType", (reqId, marketDataType),
            lambda: events.MarketDataType(reqId, marketDataType),
        )

    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
        self._deliver(
            "tickPrice", (reqId, tickType, price, attrib),
            lambda: events.TickPrice(
                reqId, tickType, price, bool(attrib.canAutoExecute),
                bool(attrib.pastLimit), bool(attrib.preOpen),
            ),
        )

    def tickSize(self, reqId: TickerId, tickType: TickType, size: Decimal):
        self._deliver(
            "tickSize", (reqId, tickType, size), lambda: events.TickSize(reqId, tickType, size)
        )

    def tickString(self, reqId: TickerId, tickType: TickType, value: str):
        self._deliver(
            "tickString", (reqId, tickType, value),
            lambda: events.TickString(reqId, tickType, value),
        )

    def currentTime(self, time: int):
        self._deliver("currentTime", (time,), lambda: events.CurrentTime(time))
