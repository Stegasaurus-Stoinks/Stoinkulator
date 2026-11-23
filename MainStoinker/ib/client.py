"""A data-only Interactive Brokers client.

`DataOnlyClient` is an `EClient` + `EWrapper` that can read market data and
cannot place, modify or cancel an order:

* `sendMsg` and `sendMsgProtoBuf`, through which every request of ibapi 10.37
  is sent (only the pre-API handshake writes to the connection directly),
  refuse the order message ids with `OrderPathRefused` before anything reaches
  the socket.
* `connect` runs the port guard (`check_broker_port`) before `EClient.connect`,
  as the legacy `IBapi.connect` does. The guard reads the environment mapping
  given to the constructor at call time.

Callbacks only turn their arguments into events (`MainStoinker.ib.events`) and
hand them to `on_event`; they never call the client and never raise into
ibapi. Their signatures are those of `EWrapper` in ibapi 10.37, which is why
they carry no return annotation (the tests compare signatures for equality).

Threads, retries and queues are not here.
"""

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID, BarData, TickAttrib, TickerId
from ibapi.contract import ContractDetails
from ibapi.message import OUT
from ibapi.ticktype import TickType
from ibapi.wrapper import EWrapper

from MainStoinker.ib import events
from MainStoinker.Util.broker_guard import check_broker_port

ORDER_MESSAGE_IDS: frozenset[int] = frozenset(
    {OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.EXERCISE_OPTIONS, OUT.REQ_GLOBAL_CANCEL}
)
"""Outgoing message ids that place, cancel or exercise an order."""

REFUSED_MESSAGE_IDS: frozenset[int] = ORDER_MESSAGE_IDS | frozenset(
    i + PROTOBUF_MSG_ID for i in ORDER_MESSAGE_IDS
)
"""The ids `sendMsg` and `sendMsgProtoBuf` refuse: each order id and its protobuf form."""


class OrderPathRefused(RuntimeError):
    """An order message was refused by the data-only client.

    Deliberately not an OSError, so nothing in ibapi can swallow it.
    """


def _refuse_if_order(msg_id: object) -> None:
    # Fail closed: anything that is not exactly an int is not a message id ibapi sends.
    if type(msg_id) is not int or msg_id in REFUSED_MESSAGE_IDS:
        raise OrderPathRefused(
            f"The data-only IB client refuses outgoing message id {msg_id!r}: "
            "it cannot place, modify or cancel an order."
        )


class DataOnlyClient(EWrapper, EClient):
    """Market-data-only IB client. See the module docstring."""

    def __init__(
        self,
        env: Mapping[str, str],
        on_event: Callable[[events.Event], None],
        on_raw: Callable[[str, tuple[Any, ...]], None] | None = None,
    ) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._env = env
        self._on_event = on_event
        self._on_raw = on_raw
        self.callback_failures = 0

    # -- guards ------------------------------------------------------------

    def connect(self, host: str, port: int, clientId: int):
        """Run the port guard, then `EClient.connect` (which has no timeout)."""
        check_broker_port(port, self._env)
        return super().connect(host, port, clientId)

    def sendMsg(self, msgId: int, msg: str):
        _refuse_if_order(msgId)
        return super().sendMsg(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes):
        _refuse_if_order(msgId)
        return super().sendMsgProtoBuf(msgId, msg)

    # -- delivery ----------------------------------------------------------

    def _deliver(
        self, name: str, args: tuple[Any, ...], build: Callable[[], events.Event]
    ) -> None:
        """Raw hook, then event hook; a failure in either is counted, never raised."""
        if self._on_raw is not None:
            try:
                self._on_raw(name, args)
            except Exception:
                self.callback_failures += 1
        try:
            self._on_event(build())
        except Exception:
            self.callback_failures += 1

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
