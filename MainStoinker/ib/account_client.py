"""An account-read-only Interactive Brokers client (stage 3, task T7, issue 232).

`AccountReadClient` is an `EClient` + `EWrapper` that can read the account's
orders, executions and positions and cannot place, change or cancel anything.
It is used for shadow mode and reconciliation; it works with TWS "Read-Only
API" switched on. It is a GUARD: the policy is an ALLOWLIST of outgoing message
ids (`ALLOWED_MESSAGE_IDS`), derived from the installed ibapi 10.37.2 source:

=====  ===================  =========================================
id     name                 why
=====  ===================  =========================================
71     START_API            the handshake step after connect
49     REQ_CURRENT_TIME     server clock
61     REQ_POSITIONS        positions (read)
64     CANCEL_POSITIONS     ends the positions subscription (not an order)
5      REQ_OPEN_ORDERS      this client's open orders (read)
16     REQ_ALL_OPEN_ORDERS  every open order, also those placed in TWS (read)
7      REQ_EXECUTIONS       executions (read), text form
207    REQ_EXECUTIONS + 200 the same as a protobuf frame (server version 201+)
99     REQ_COMPLETED_ORDERS completed orders (read)
=====  ===================  =========================================

Everything else is refused, in particular `PLACE_ORDER` (3, protobuf 203),
`CANCEL_ORDER` (4, protobuf 204), `REQ_GLOBAL_CANCEL` (58, protobuf 258),
`REQ_AUTO_OPEN_ORDERS` (15), `EXERCISE_OPTIONS` (21), `REQ_FA` (18) and
`REPLACE_FA` (19): none of them is in the list, and `NEVER_ALLOWED_IDS` (kept
for tests and documentation, not consulted by the check) names them with their
protobuf forms. A message id that ibapi adds later is refused until a human adds
it. ibapi 10.37.2 gives exactly four ids a protobuf form (`PROTOBUF_MSG_IDS`:
`REQ_EXECUTIONS`, `PLACE_ORDER`, `CANCEL_ORDER`, `REQ_GLOBAL_CANCEL`); of those only
`REQ_EXECUTIONS` (207) is allowed.

Two layers enforce it, both before anything reaches the socket:

1. Method level. `sendMsg` takes the id as an argument and refuses anything that
   is not exactly an `int` in `ALLOWED_TEXT_IDS`; `sendMsgProtoBuf` refuses
   anything that is not exactly an `int` in `ALLOWED_PROTOBUF_IDS` (only 207).
   Both raise `AccountMessageRefused`, a `RuntimeError` and deliberately not an
   `OSError`, so nothing in ibapi can swallow it.
2. Connection level. `client.conn` is wrapped, whenever it is assigned (also by
   `EClient.connect` itself), in a guard that forwards everything to the
   `ibapi.connection.Connection` but whose `sendMsg(raw_bytes)` parses the
   message id out of the frame with this module's own parser (`parse_frame`) and
   refuses anything not allowed or not parseable. This closes
   `EClient.sendMsg(client, 3, ...)` and `client.conn.sendMsg(frame)`.

The frame parser is this module's own (it does not import `client._check_frame`,
so `DataOnlyClient` stays untouched); a corpus test pins that it agrees with
`client._check_frame` on every frame it parses.

`connect` refuses a port that is not an exact `int`, then runs the port guard
(`check_broker_port`) before `EClient.connect`. `startApi` always sends an empty
optional-capabilities string.

Accounts. `managedAccounts` is checked here too: every id must look like a paper
id (`D[UFI][A-Z]?[0-9]+`, the rule of `connection._PAPER_ID`, copied so this module
stays independent). Tripwire: a `managedAccounts` list, an `openOrder`, a
`completedOrder`, an `execDetails` or a `position` naming a non-paper account (or
no account at all: fail closed) sets `tripped`, disconnects, delivers nothing
from the offending callback and raises `AccountTripwire`. After a trip every
callback except `connectionClosed` is dropped.

Out of scope, because Python cannot prevent it: code that replaces the guard by
writing the private attributes (`client._conn`, `client.__dict__`), reaches past
it through `client.conn._inner` or `client.conn.socket`, or talks to a socket of
its own. The payload of the one allowed protobuf frame (207) is not decoded: the
id says it is an executions request, and the id is all this layer checks.

Callbacks turn their arguments into events and hand them to `on_event`; they
never call the client and never raise into ibapi, except the tripwire. A
consumer hook that raises is counted in `callback_failures`. The events specific
to this client are defined here (`events.py` is not touched by the task).

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
from ibapi.commission_and_fees_report import CommissionAndFeesReport
from ibapi.common import PROTOBUF_MSG_ID, TickerId
from ibapi.contract import Contract
from ibapi.execution import Execution
from ibapi.message import OUT
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.wrapper import EWrapper

from MainStoinker.ib import events
from MainStoinker.Util.broker_guard import BrokerPortRefused, check_broker_port

ALLOWED_TEXT_IDS: frozenset[int] = frozenset(
    {
        OUT.START_API,
        OUT.REQ_CURRENT_TIME,
        OUT.REQ_POSITIONS,
        OUT.CANCEL_POSITIONS,
        OUT.REQ_OPEN_ORDERS,
        OUT.REQ_ALL_OPEN_ORDERS,
        OUT.REQ_EXECUTIONS,
        OUT.REQ_COMPLETED_ORDERS,
    }
)
"""Outgoing ids sent as text (or raw-id) frames."""

ALLOWED_PROTOBUF_IDS: frozenset[int] = frozenset({OUT.REQ_EXECUTIONS + PROTOBUF_MSG_ID})
"""Outgoing ids sent as protobuf frames: only the executions request."""

ALLOWED_MESSAGE_IDS: frozenset[int] = ALLOWED_TEXT_IDS | ALLOWED_PROTOBUF_IDS
"""The only outgoing message ids this client sends."""

NEVER_ALLOWED_IDS: frozenset[int] = frozenset(
    {
        OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.REQ_GLOBAL_CANCEL, OUT.REQ_AUTO_OPEN_ORDERS,
        OUT.EXERCISE_OPTIONS, OUT.REQ_FA, OUT.REPLACE_FA,
    }
)
"""Documentation and tests only: ids that must never be allowed, in text form.
Their protobuf forms are these plus 200. The check does not consult this set."""

_OUT_NAMES: dict[int, str] = {
    v: k for k, v in vars(OUT).items() if not k.startswith("_") and type(v) is int
}
_PAPER_ID = re.compile(r"D[UFI][A-Z]?[0-9]+")


class AccountMessageRefused(RuntimeError):
    """An outgoing message was refused by the account-read client (not allowlisted).

    Deliberately not an OSError, so nothing in ibapi can swallow it.
    """


ACCOUNT_CLIENT_ID = 24
"""The account and order connection's client id, fixed forever (#230, decision O9)."""


class ClientIdRefused(RuntimeError):
    """`connect` was given a client id other than `ACCOUNT_CLIENT_ID`."""


class AccountTripwire(RuntimeError):
    """A callback named a non-paper account (or none); the client disconnected."""


def _describe(msg_id: object) -> str:
    if type(msg_id) is int:
        if msg_id in _OUT_NAMES:
            return f"{msg_id!r} ({_OUT_NAMES[msg_id]})"
        if msg_id - PROTOBUF_MSG_ID in _OUT_NAMES:
            return f"{msg_id!r} ({_OUT_NAMES[msg_id - PROTOBUF_MSG_ID]}, protobuf form)"
        return f"{msg_id!r} (not an ibapi OUT id)"
    return f"{msg_id!r} (not an int)"


def _refuse(msg_id: object, via: str) -> AccountMessageRefused:
    return AccountMessageRefused(
        f"The account-read IB client refuses outgoing message id {_describe(msg_id)} "
        f"({via}): only {sorted(ALLOWED_MESSAGE_IDS)} may be sent."
    )


def _refuse_frame(reason: str) -> AccountMessageRefused:
    return AccountMessageRefused(
        f"The account-read IB client refuses a frame on its connection: {reason}."
    )


# -- frame parser (this module's own) -----------------------------------------

_TEXT_ID = re.compile(rb"(0|[1-9][0-9]{0,5})\0")  # ibapi.comm.make_msg, server < 201
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


def parse_frame(raw: object, first_write: bool) -> int | None:
    """The message id of one outgoing frame; None for the connection handshake.

    Raises `AccountMessageRefused` for anything it cannot parse exactly.

    Framing (ibapi 10.37.2): `comm.make_msg` writes a 4-byte big-endian length,
    then either `<decimal id>\\0<fields>` (server version below 201) or a raw
    4-byte big-endian id and the fields (201 and up); `comm.make_msg_proto`
    writes the length, a raw 4-byte id and the protobuf. The handshake is
    `API\\0` followed by a length-prefixed `v<min>..<max>[ options]` and is
    accepted only when `first_write` is true. Ids are small, so a raw frame
    starts with a zero byte and a text frame with a digit: they cannot be
    confused.
    """
    if type(raw) is not bytes:
        raise _refuse_frame(f"frame is {type(raw).__name__}, not bytes")
    if raw.startswith(_HANDSHAKE_PREFIX):
        if not first_write:
            raise _refuse_frame("handshake frame after the first write")
        body = _unframe(raw[len(_HANDSHAKE_PREFIX):])
        if body is not None and _HANDSHAKE_BODY.fullmatch(body):
            return None
        raise _refuse_frame("malformed handshake frame")
    body = _unframe(raw)
    if body is None:
        raise _refuse_frame("unparseable frame")
    match = _TEXT_ID.match(body)
    if match is not None:
        return int(match.group(1))
    if len(body) >= 4:
        return int.from_bytes(body[:4], "big")
    raise _refuse_frame("unparseable frame")


def check_frame(raw: object, first_write: bool) -> None:
    """Raise `AccountMessageRefused` unless `raw` is the handshake or an allowed message."""
    msg_id = parse_frame(raw, first_write)
    if msg_id is not None and msg_id not in ALLOWED_MESSAGE_IDS:
        raise _refuse(msg_id, "connection")


class _GuardedConnection:
    """Wraps an `ibapi.connection.Connection`; checks every frame it is asked to send.

    Everything else is forwarded to the wrapped object. It opens and owns no socket.
    """

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_wrote", False)  # the one bit of state

    def sendMsg(self, msg: bytes):  # noqa: N802 (ibapi's name)
        check_frame(msg, first_write=not self._wrote)
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


# -- events of the account reads ----------------------------------------------


@dataclass(frozen=True)
class OpenOrder:
    """An open order (also one placed by hand in TWS) from `openOrder`."""

    order_id: int
    perm_id: int
    client_id: int
    account: str
    symbol: str
    sec_type: str
    con_id: int
    action: str
    order_type: str
    total_quantity: Decimal
    lmt_price: float
    aux_price: float
    tif: str
    order_ref: str
    parent_id: int
    oca_group: str
    transmit: bool
    status: str


@dataclass(frozen=True)
class OpenOrderEnd:
    pass


@dataclass(frozen=True)
class CompletedOrder:
    """A completed order from `completedOrder` (no order id: TWS sends none)."""

    perm_id: int
    client_id: int
    account: str
    symbol: str
    sec_type: str
    action: str
    order_type: str
    total_quantity: Decimal
    order_ref: str
    status: str


@dataclass(frozen=True)
class CompletedOrdersEnd:
    pass


@dataclass(frozen=True)
class OrderStatus:
    order_id: int
    status: str
    filled: Decimal
    remaining: Decimal
    avg_fill_price: float
    perm_id: int
    parent_id: int
    last_fill_price: float
    client_id: int
    why_held: str
    mkt_cap_price: float


@dataclass(frozen=True)
class ExecDetails:
    req_id: int
    exec_id: str
    order_id: int
    perm_id: int
    client_id: int
    account: str
    time: str
    symbol: str
    sec_type: str
    side: str
    shares: Decimal
    price: float
    avg_price: float
    cum_qty: Decimal
    exchange: str
    order_ref: str


@dataclass(frozen=True)
class ExecDetailsEnd:
    req_id: int


@dataclass(frozen=True)
class CommissionReport:
    """Commission of one execution; arrives after the fill, maybe late or never."""

    exec_id: str
    commission: float
    currency: str
    realized_pnl: float


@dataclass(frozen=True)
class Position:
    account: str
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    position: Decimal
    avg_cost: float


@dataclass(frozen=True)
class PositionEnd:
    pass


# -- the client ---------------------------------------------------------------


@dataclass(frozen=True)
class CallbackFailure:
    """The first exception a consumer hook raised."""

    hook: str
    exc_type: str
    message: str
    traceback: str


def _is_paper_id(account: object) -> bool:
    return type(account) is str and _PAPER_ID.fullmatch(account) is not None


def _all_paper_list(text: object) -> bool:
    if type(text) is not str:
        return False
    ids = [item.strip() for item in text.split(",") if item.strip()]
    return bool(ids) and all(_is_paper_id(i) for i in ids)


class AccountReadClient(EWrapper, EClient):
    """Account-reads-only IB client. See the module docstring."""

    _conn: Any = None

    def __init__(
        self,
        env: Mapping[str, str],
        on_event: Callable[[Any], None],
        on_raw: Callable[[str, tuple[Any, ...]], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._failure_lock = threading.Lock()
        self.callback_failures = 0
        self.first_failures: dict[str, CallbackFailure] = {}
        self.tripped: AccountTripwire | None = None
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
        """Refuse a tripped client, a client id other than 24 and a non-int port, run
        the port guard, then `EClient.connect`. All refusals come before any socket."""
        if self.tripped is not None:
            raise AccountTripwire(
                "Refusing to connect: this client tripped on a non-paper account and is final."
            )
        if type(clientId) is not int or clientId != ACCOUNT_CLIENT_ID:
            raise ClientIdRefused(
                f"Refusing to connect: the account client id is fixed at {ACCOUNT_CLIENT_ID} "
                f"(decision O9), not {clientId!r}; client id 0 would bind TWS-owned orders."
            )
        if type(port) is not int:
            raise BrokerPortRefused(
                f"Refusing to connect: broker port {port!r} is not an int."
            )
        check_broker_port(port, self._env)
        return super().connect(host, port, clientId)

    def startApi(self):
        self.optCapab = ""
        return super().startApi()

    def sendMsg(self, msgId: int, msg: str):
        if type(msgId) is not int or msgId not in ALLOWED_TEXT_IDS:
            raise _refuse(msgId, "sendMsg")
        return super().sendMsg(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes):
        if type(msgId) is not int or msgId not in ALLOWED_PROTOBUF_IDS:
            raise _refuse(msgId, "sendMsgProtoBuf")
        return super().sendMsgProtoBuf(msgId, msg)

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
        self, name: str, args: tuple[Any, ...], build: Callable[[], Any]
    ) -> None:
        """Raw hook, then event hook; a failure in either is counted, never raised.

        After a trip only `connectionClosed` still goes through.
        """
        if self.tripped is not None and name != "connectionClosed":
            return
        if self._on_raw is not None:
            try:
                self._on_raw(name, args)
            except Exception:
                self._record_failure("on_raw")
        try:
            self._on_event(build())
        except Exception:
            self._record_failure("on_event")

    def _trip(self, where: str, account: object) -> None:
        """Record, disconnect and raise. Nothing of the offending callback is delivered."""
        exc = AccountTripwire(
            f"{where} names a non-paper account (or none): refusing and disconnecting "
            f"(account text length {len(account) if isinstance(account, str) else '?'})."
        )
        self.tripped = exc
        try:
            self.disconnect()
        except Exception:  # the connection is being torn down anyway
            self._logger.exception("disconnect after the account tripwire failed")
        raise exc

    def _require_paper(self, where: str, account: object) -> None:
        if self.tripped is not None:
            raise self.tripped
        if not _is_paper_id(account):
            self._trip(where, account)

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
        if self.tripped is None and not _all_paper_list(accountsList):
            self._trip("managedAccounts", accountsList)
        self._deliver(
            "managedAccounts", (accountsList,), lambda: events.ManagedAccounts(accountsList)
        )

    def currentTime(self, time: int):
        self._deliver("currentTime", (time,), lambda: events.CurrentTime(time))

    def openOrder(
        self, orderId: int, contract: Contract, order: Order, orderState: OrderState
    ):
        self._require_paper("openOrder", order.account)
        self._deliver(
            "openOrder", (orderId, contract, order, orderState),
            lambda: OpenOrder(
                order_id=orderId, perm_id=order.permId, client_id=order.clientId,
                account=order.account, symbol=contract.symbol, sec_type=contract.secType,
                con_id=contract.conId, action=order.action, order_type=order.orderType,
                total_quantity=order.totalQuantity, lmt_price=order.lmtPrice,
                aux_price=order.auxPrice, tif=order.tif, order_ref=order.orderRef,
                parent_id=order.parentId, oca_group=order.ocaGroup,
                transmit=bool(order.transmit), status=orderState.status,
            ),
        )

    def openOrderEnd(self):
        self._deliver("openOrderEnd", (), OpenOrderEnd)

    def completedOrder(self, contract: Contract, order: Order, orderState: OrderState):
        self._require_paper("completedOrder", order.account)
        self._deliver(
            "completedOrder", (contract, order, orderState),
            lambda: CompletedOrder(
                perm_id=order.permId, client_id=order.clientId, account=order.account,
                symbol=contract.symbol, sec_type=contract.secType, action=order.action,
                order_type=order.orderType, total_quantity=order.totalQuantity,
                order_ref=order.orderRef, status=orderState.status,
            ),
        )

    def completedOrdersEnd(self):
        self._deliver("completedOrdersEnd", (), CompletedOrdersEnd)

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Decimal,
        remaining: Decimal,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ):
        self._deliver(
            "orderStatus",
            (orderId, status, filled, remaining, avgFillPrice, permId, parentId,
             lastFillPrice, clientId, whyHeld, mktCapPrice),
            lambda: OrderStatus(
                orderId, status, filled, remaining, avgFillPrice, permId, parentId,
                lastFillPrice, clientId, whyHeld, mktCapPrice,
            ),
        )

    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        self._require_paper("execDetails", execution.acctNumber)
        self._deliver(
            "execDetails", (reqId, contract, execution),
            lambda: ExecDetails(
                req_id=reqId, exec_id=execution.execId, order_id=execution.orderId,
                perm_id=execution.permId, client_id=execution.clientId,
                account=execution.acctNumber, time=execution.time, symbol=contract.symbol,
                sec_type=contract.secType, side=execution.side, shares=execution.shares,
                price=execution.price, avg_price=execution.avgPrice,
                cum_qty=execution.cumQty, exchange=execution.exchange,
                order_ref=execution.orderRef,
            ),
        )

    def execDetailsEnd(self, reqId: int):
        self._deliver("execDetailsEnd", (reqId,), lambda: ExecDetailsEnd(reqId))

    def commissionAndFeesReport(self, commissionAndFeesReport: CommissionAndFeesReport):
        self._deliver(
            "commissionAndFeesReport", (commissionAndFeesReport,),
            lambda: CommissionReport(
                exec_id=commissionAndFeesReport.execId,
                commission=commissionAndFeesReport.commissionAndFees,
                currency=commissionAndFeesReport.currency,
                realized_pnl=commissionAndFeesReport.realizedPNL,
            ),
        )

    def position(
        self, account: str, contract: Contract, position: Decimal, avgCost: float
    ):
        self._require_paper("position", account)
        self._deliver(
            "position", (account, contract, position, avgCost),
            lambda: Position(
                account=account, con_id=contract.conId, symbol=contract.symbol,
                sec_type=contract.secType, currency=contract.currency,
                position=position, avg_cost=avgCost,
            ),
        )

    def positionEnd(self):
        self._deliver("positionEnd", (), PositionEnd)
