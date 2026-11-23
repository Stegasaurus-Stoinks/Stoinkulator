"""The data-only IB client (`MainStoinker/ib/client.py`).

Nothing here connects: `EClient.connect` is replaced by a recorder, or the
pytest block in `tests/conftest.py` stops the real one, and requests are sent
to a stub connection object that only records.
"""

import ast
import inspect
import re
import socket
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi import message as ibapi_message
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID, PROTOBUF_MSG_IDS, BarData, TickAttrib
from ibapi.contract import Contract, ContractDetails
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.server_versions import MIN_SERVER_VER_PROTOBUF_PLACE_ORDER
from ibapi.wrapper import EWrapper

from MainStoinker.ib import events
from MainStoinker.ib.client import (
    ALLOWED_MESSAGE_IDS,
    ORDER_MESSAGE_IDS,
    DataOnlyClient,
    MessageRefused,
)
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.conftest import BrokerConnectionBlockedUnderPytest

OPT_IN = "STOINK_ALLOW_LIVE_PORT"
BELOW_PROTOBUF = MIN_SERVER_VER_PROTOBUF_PLACE_ORDER - 1
AT_PROTOBUF = MIN_SERVER_VER_PROTOBUF_PLACE_ORDER


class StubConn:
    """Stands in for ibapi's Connection; records what would reach the socket."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendMsg(self, msg: bytes) -> None:  # noqa: N802 (ibapi's name)
        self.sent.append(msg)

    def isConnected(self) -> bool:  # noqa: N802
        return True


def make_client(env: dict[str, str] | None = None, raw: Any = None):
    got: list[events.Event] = []
    client = DataOnlyClient({} if env is None else env, got.append, raw)
    return client, got


def with_stub(client: DataOnlyClient, server_version: int) -> StubConn:
    stub = StubConn()
    client.conn = stub
    client.serverVersion_ = server_version
    client.connState = EClient.CONNECTED
    return stub


def contract() -> Contract:
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = "AAPL", "STK", "SMART", "USD"
    return c


def market_order() -> Order:
    o = Order()
    o.action, o.orderType, o.totalQuantity = "BUY", "MKT", Decimal(1)
    return o


@pytest.fixture
def connect_recorder(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def record(self, host, port, clientId):
        calls.append((host, port, clientId))
        return "delegated"

    monkeypatch.setattr(EClient, "connect", record)
    return calls


# -- connect and the port guard ---------------------------------------------


@pytest.mark.parametrize("port", [7496, 4001, 5000])
def test_connect_refuses_live_port_before_any_connection(connect_recorder, port):
    client, _ = make_client()
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 21)
    assert connect_recorder == []


@pytest.mark.parametrize("port", [7497, 4002])
def test_connect_paper_port_delegates_unchanged(connect_recorder, port):
    client, _ = make_client()
    assert client.connect("127.0.0.1", port, 21) == "delegated"
    assert connect_recorder == [("127.0.0.1", port, 21)]


def test_connect_live_port_with_opt_in_delegates(connect_recorder):
    client, _ = make_client({OPT_IN: "1"})
    assert client.connect("127.0.0.1", 7496, 21) == "delegated"
    assert connect_recorder == [("127.0.0.1", 7496, 21)]


def test_connect_opt_in_must_be_exactly_one(connect_recorder):
    client, _ = make_client({OPT_IN: "true"})
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 21)
    assert connect_recorder == []


def test_connect_guard_reads_env_at_call_time(connect_recorder):
    env: dict[str, str] = {}
    client, _ = make_client(env)
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 21)
    env[OPT_IN] = "1"
    client.connect("127.0.0.1", 7496, 21)
    del env[OPT_IN]
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 21)
    assert connect_recorder == [("127.0.0.1", 7496, 21)]


def test_real_connect_under_pytest_is_blocked(monkeypatch):
    attempts: list[Any] = []

    def spy(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("real socket attempted")

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket, "create_connection", spy)
    client, got = make_client()
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        client.connect("127.0.0.1", 7497, 21)
    assert attempts == []
    assert got == []


# -- order refusal -----------------------------------------------------------


def _order_message_names() -> dict[str, int]:
    pattern = re.compile(r"PLACE_ORDER|CANCEL_ORDER|EXERCISE|GLOBAL_CANCEL")
    return {k: v for k, v in vars(ibapi_message.OUT).items()
            if not k.startswith("_") and isinstance(v, int) and pattern.search(k)}


def _eclient_methods_sending_order_messages() -> set[str]:
    names = "|".join(_order_message_names())
    uses = re.compile(rf"\bOUT\.(?:{names})\b")
    return {name for name, fn in inspect.getmembers(EClient, inspect.isfunction)
            if uses.search(inspect.getsource(fn))}


ORDER_AND_PROTOBUF_IDS = sorted(
    set(ORDER_MESSAGE_IDS) | {i + PROTOBUF_MSG_ID for i in ORDER_MESSAGE_IDS}
)


def test_order_ids_cover_every_order_message_of_the_installed_ibapi():
    ids = set(_order_message_names().values())
    assert ids == {3, 4, 21, 58}
    assert set(ORDER_MESSAGE_IDS) == ids
    assert PROTOBUF_MSG_ID == 200
    assert not (set(ORDER_AND_PROTOBUF_IDS) & ALLOWED_MESSAGE_IDS)


def test_allowlist_is_exactly_the_ten_stage_one_ids_by_number():
    # An ibapi upgrade that renumbers OUT must fail here, not silently re-point the list.
    named = {
        "REQ_MKT_DATA": 1, "CANCEL_MKT_DATA": 2, "REQ_CONTRACT_DATA": 9,
        "REQ_HISTORICAL_DATA": 20, "CANCEL_HISTORICAL_DATA": 25, "REQ_CURRENT_TIME": 49,
        "REQ_MARKET_DATA_TYPE": 59, "START_API": 71, "REQ_HEAD_TIMESTAMP": 87,
        "CANCEL_HEAD_TIMESTAMP": 90,
    }
    for name, number in named.items():
        assert getattr(ibapi_message.OUT, name) == number, name
    assert ALLOWED_MESSAGE_IDS == frozenset(named.values())
    assert isinstance(ALLOWED_MESSAGE_IDS, frozenset)


def test_no_allowed_id_has_a_protobuf_form_in_the_installed_ibapi():
    # sendMsgProtoBuf refuses everything. If an upgrade gives an allowed id a
    # protobuf form, a human must decide how to allow it.
    assert not (set(PROTOBUF_MSG_IDS) & ALLOWED_MESSAGE_IDS)
    assert set(PROTOBUF_MSG_IDS) == {
        ibapi_message.OUT.REQ_EXECUTIONS, 3, 4, 58}


def test_every_eclient_order_method_is_known_to_this_test():
    # If an ibapi upgrade adds a method that sends an order message, this fails
    # and the tests below must be extended.
    assert _eclient_methods_sending_order_messages() == {
        "placeOrder", "placeOrderProtoBuf", "cancelOrder", "cancelOrderProtoBuf",
        "reqGlobalCancel", "reqGlobalCancelProtoBuf", "exerciseOptions",
    }


def _order_calls(client: DataOnlyClient) -> dict[str, Any]:
    return {
        "placeOrder": lambda: client.placeOrder(1, contract(), market_order()),
        "cancelOrder": lambda: client.cancelOrder(1, OrderCancel()),
        "reqGlobalCancel": lambda: client.reqGlobalCancel(OrderCancel()),
        "exerciseOptions": lambda: client.exerciseOptions(
            1, contract(), 1, 1, "", 0, "", "", False),
        # The protobuf senders take a request message; they reach sendMsgProtoBuf
        # directly, with the id ibapi gives them.
        "placeOrderProtoBuf": lambda: client.placeOrderProtoBuf(_Proto()),
        "cancelOrderProtoBuf": lambda: client.cancelOrderProtoBuf(_Proto()),
        "reqGlobalCancelProtoBuf": lambda: client.reqGlobalCancelProtoBuf(_Proto()),
    }


class _Proto:
    def SerializeToString(self) -> bytes:  # noqa: N802 (protobuf's name)
        return b""


@pytest.mark.parametrize("server_version", [BELOW_PROTOBUF, AT_PROTOBUF])
@pytest.mark.parametrize("method", sorted(_eclient_methods_sending_order_messages()))
def test_order_messages_refused_before_the_socket(method, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(MessageRefused):
        _order_calls(client)[method]()
    assert stub.sent == []


@pytest.mark.parametrize("server_version", [BELOW_PROTOBUF, AT_PROTOBUF])
@pytest.mark.parametrize("msg_id", ORDER_AND_PROTOBUF_IDS)
def test_send_paths_refuse_every_order_message_id(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(MessageRefused):
        client.sendMsg(msg_id, "x")
    with pytest.raises(MessageRefused):
        client.sendMsgProtoBuf(msg_id, b"x")
    assert stub.sent == []


@pytest.mark.parametrize("bad_id", [True, 3.0, "3", None])
def test_send_paths_fail_closed_on_non_int_ids(bad_id):
    client, _ = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    with pytest.raises(MessageRefused):
        client.sendMsg(bad_id, "x")
    assert stub.sent == []


@pytest.mark.parametrize("server_version", [BELOW_PROTOBUF, AT_PROTOBUF])
def test_data_messages_pass_the_filter(server_version):
    client, got = make_client()
    stub = with_stub(client, server_version)
    client.reqCurrentTime()
    client.reqHeadTimeStamp(1, contract(), "TRADES", 1, 1)
    client.reqContractDetails(2, contract())
    client.reqHistoricalData(3, contract(), "", "1 D", "1 min", "TRADES", 1, 1, False, [])
    client.reqMarketDataType(3)
    client.reqMktData(4, contract(), "", False, False, [])
    assert len(stub.sent) == 6
    assert got == []  # nothing was reported as an error


def test_refusal_is_not_an_oserror():
    assert issubclass(MessageRefused, RuntimeError)
    assert not issubclass(MessageRefused, OSError)


# -- callbacks ----------------------------------------------------------------


def _overridden_ewrapper_methods() -> list[str]:
    return sorted(n for n, v in vars(DataOnlyClient).items()
                  if not n.startswith("_") and inspect.isfunction(v) and hasattr(EWrapper, n))


def test_overridden_callbacks_match_ewrapper_signatures():
    names = _overridden_ewrapper_methods()
    assert set(names) == {
        "error", "nextValidId", "connectAck", "connectionClosed", "managedAccounts",
        "historicalData", "historicalDataEnd", "historicalDataUpdate", "contractDetails",
        "contractDetailsEnd", "headTimestamp", "marketDataType", "tickPrice", "tickSize",
        "tickString", "currentTime",
    }
    for name in names:
        assert (inspect.signature(getattr(DataOnlyClient, name))
                == inspect.signature(getattr(EWrapper, name))), name


def _bar() -> BarData:
    b = BarData()
    b.date, b.open, b.high, b.low, b.close = "20240102 09:30:00 US/Pacific", 1.0, 2.0, 0.5, 1.5
    b.volume, b.wap, b.barCount = Decimal(100), Decimal("1.2"), 7
    return b


def _details() -> ContractDetails:
    d = ContractDetails()
    d.contract = contract()
    d.contract.conId, d.contract.primaryExchange = 265598, "NASDAQ"
    d.minTick, d.timeZoneId, d.longName = 0.01, "US/Eastern", "APPLE INC"
    d.tradingHours, d.liquidHours = "th", "lh"
    return d


def _attrib() -> TickAttrib:
    a = TickAttrib()
    a.canAutoExecute, a.pastLimit, a.preOpen = True, False, True
    return a


def _all_callback_calls() -> list[tuple[str, tuple[Any, ...]]]:
    return [
        ("error", (7, 1700000000000, 2104, "farm ok", "")),
        ("nextValidId", (5,)),
        ("connectAck", ()),
        ("connectionClosed", ()),
        ("managedAccounts", ("DU000",)),
        ("historicalData", (1, _bar())),
        ("historicalDataEnd", (1, "s", "e")),
        ("historicalDataUpdate", (1, _bar())),
        ("contractDetails", (1, _details())),
        ("contractDetailsEnd", (1,)),
        ("headTimestamp", (1, "20200101")),
        ("marketDataType", (1, 3)),
        ("tickPrice", (1, 1, 9.5, _attrib())),
        ("tickSize", (1, 0, Decimal(10))),
        ("tickString", (1, 45, "2024-01-02")),
        ("currentTime", (1700000000,)),
    ]


def test_every_callback_becomes_one_event_and_one_raw_record():
    raw: list[tuple[str, tuple[Any, ...]]] = []
    client, got = make_client(raw=lambda name, args: raw.append((name, args)))
    calls = _all_callback_calls()
    for name, args in calls:
        getattr(client, name)(*args)
    assert len(got) == len(calls)
    assert [(n, a) for n, a in raw] == calls
    assert client.callback_failures == 0
    assert got[1] == events.NextValidId(5)
    assert got[5] == events.HistoricalBar(
        1, "20240102 09:30:00 US/Pacific", 1.0, 2.0, 0.5, 1.5, Decimal(100), Decimal("1.2"), 7)
    assert got[7].update is True
    assert got[8] == events.ContractDetailsEvent(
        1, 265598, "AAPL", "STK", "SMART", "NASDAQ", "USD", "", "APPLE INC", 0.01,
        "US/Eastern", "th", "lh")
    assert got[12] == events.TickPrice(1, 1, 9.5, True, False, True)


def test_error_event_carries_code_and_message_not_time():
    client, got = make_client()
    client.error(7, 1700000000000, 2104, "Market data farm connection is OK:usfarm")
    assert got == [events.IbError(
        req_id=7, code=2104, message="Market data farm connection is OK:usfarm",
        error_time_ms=1700000000000)]
    assert got[0].code != 1700000000000
    assert got[0].message != "2104"


def test_callbacks_never_call_the_client(monkeypatch):
    client, _ = make_client()
    touched: list[str] = []

    def trap(name):
        def called(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"callback called client.{name}")
        return called

    for name in dir(EClient):
        if not name.startswith("_") and name not in dir(EWrapper) and callable(
                getattr(EClient, name)):
            monkeypatch.setattr(client, name, trap(name))
    for name, args in _all_callback_calls():
        getattr(client, name)(*args)
    assert touched == []


def test_callbacks_never_raise_into_ibapi():
    def boom(*args: Any) -> None:
        raise ValueError("consumer bug")

    client = DataOnlyClient({}, boom, boom)
    for name, args in _all_callback_calls():
        getattr(client, name)(*args)  # must not raise
    # Malformed arguments are contained too.
    client.historicalData(1, None)
    client.tickPrice(1, 1, 1.0, None)
    assert client.callback_failures > 0


def test_a_failing_raw_hook_does_not_lose_the_event():
    def boom(name: str, args: tuple[Any, ...]) -> None:
        raise ValueError("recorder bug")

    client, got = make_client(raw=boom)
    client.nextValidId(9)
    assert got == [events.NextValidId(9)]
    assert client.callback_failures == 1


# -- module hygiene -----------------------------------------------------------


def test_events_module_imports_no_ibapi():
    path = Path(events.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n.split(".")[0] == "ibapi"]
