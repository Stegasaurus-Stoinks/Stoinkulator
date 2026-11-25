"""The account-read-only IB client (`MainStoinker/ib/account_client.py`, issue 232).

It is a guard: it must be unable to send an order message. Nothing here
connects: `EClient.connect` is replaced by a recorder, or the pytest block in
`tests/conftest.py` stops the real one, and requests go to a stub connection
object that only records.
"""

import inspect
import re
import socket
from decimal import Decimal
from typing import Any

import pytest
from ibapi import comm
from ibapi import message as ibapi_message
from ibapi.client import EClient
from ibapi.commission_and_fees_report import CommissionAndFeesReport
from ibapi.common import PROTOBUF_MSG_ID, PROTOBUF_MSG_IDS
from ibapi.contract import Contract
from ibapi.execution import Execution, ExecutionFilter
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.order_state import OrderState
from ibapi.server_versions import MIN_SERVER_VER_PROTOBUF, MIN_SERVER_VER_PROTOBUF_PLACE_ORDER
from ibapi.wrapper import EWrapper

from MainStoinker.ib import account_client, events
from MainStoinker.ib import client as data_client
from MainStoinker.ib.account_client import (
    ALLOWED_MESSAGE_IDS,
    NEVER_ALLOWED_IDS,
    AccountMessageRefused,
    AccountReadClient,
    AccountTripwire,
    check_frame,
    parse_frame,
)
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.conftest import BrokerConnectionBlockedUnderPytest

OPT_IN = "STOINK_ALLOW_LIVE_PORT"
BELOW_PROTOBUF = MIN_SERVER_VER_PROTOBUF - 1  # text frames
AT_PROTOBUF = MIN_SERVER_VER_PROTOBUF  # raw ids, protobuf executions request
AT_PROTOBUF_PLACE = MIN_SERVER_VER_PROTOBUF_PLACE_ORDER  # protobuf order frames
SERVER_VERSIONS = [BELOW_PROTOBUF, AT_PROTOBUF, AT_PROTOBUF_PLACE]

PAPER = "DUX123456"
LIVE_LOOKING = "UX123456"  # not a paper id (does not begin with D)


class StubConn:
    """Stands in for ibapi's Connection; records what would reach the socket."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.disconnected = False

    def sendMsg(self, msg: bytes) -> None:  # noqa: N802 (ibapi's name)
        self.sent.append(msg)

    def isConnected(self) -> bool:  # noqa: N802
        return not self.disconnected

    def disconnect(self) -> None:
        self.disconnected = True


def make_client(env: dict[str, str] | None = None, raw: Any = None):
    got: list[Any] = []
    client = AccountReadClient({} if env is None else env, got.append, raw)
    return client, got


def with_stub(client: AccountReadClient, server_version: int) -> StubConn:
    stub = StubConn()
    client.conn = stub
    client.serverVersion_ = server_version
    client.clientId = 24
    client.connState = EClient.CONNECTED
    return stub


def contract() -> Contract:
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency, c.conId = "AAPL", "STK", "SMART", "USD", 265598
    return c


def market_order(account: str = PAPER) -> Order:
    o = Order()
    o.action, o.orderType, o.totalQuantity, o.account = "BUY", "MKT", Decimal(1), account
    return o


def frame(msg_id: int, server_version: int, proto: bool = False) -> bytes:
    if proto:
        return comm.make_msg_proto(msg_id, b"x")
    return comm.make_msg(msg_id, server_version >= MIN_SERVER_VER_PROTOBUF, "x\0")


@pytest.fixture
def connect_recorder(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def record(self, host, port, clientId):
        calls.append((host, port, clientId))
        return "delegated"

    monkeypatch.setattr(EClient, "connect", record)
    return calls


# -- the allowlist ------------------------------------------------------------


def test_allowlist_exact():
    # By number and by name: an ibapi upgrade that renumbers OUT must fail here,
    # not silently re-point the list.
    named = {
        "START_API": 71, "REQ_CURRENT_TIME": 49, "REQ_POSITIONS": 61, "CANCEL_POSITIONS": 64,
        "REQ_OPEN_ORDERS": 5, "REQ_ALL_OPEN_ORDERS": 16, "REQ_EXECUTIONS": 7,
        "REQ_COMPLETED_ORDERS": 99,
    }
    for name, number in named.items():
        assert getattr(ibapi_message.OUT, name) == number, name
    assert PROTOBUF_MSG_ID == 200
    assert ALLOWED_MESSAGE_IDS == frozenset(named.values()) | {207}
    assert ALLOWED_MESSAGE_IDS == frozenset({71, 49, 61, 64, 5, 16, 7, 207, 99})
    assert isinstance(ALLOWED_MESSAGE_IDS, frozenset)
    assert account_client.ALLOWED_TEXT_IDS == ALLOWED_MESSAGE_IDS - {207}
    assert account_client.ALLOWED_PROTOBUF_IDS == frozenset({207})


def test_never_allowed_ids_are_outside_the_list_in_text_and_protobuf_form():
    assert NEVER_ALLOWED_IDS == {3, 4, 58, 15, 21, 18, 19}
    forms = set(NEVER_ALLOWED_IDS) | {i + PROTOBUF_MSG_ID for i in NEVER_ALLOWED_IDS}
    assert not forms & ALLOWED_MESSAGE_IDS
    # The ids ibapi gives a protobuf form: of the four only the executions request is allowed.
    assert set(PROTOBUF_MSG_IDS) == {7, 3, 4, 58}
    assert {i + PROTOBUF_MSG_ID for i in PROTOBUF_MSG_IDS} & ALLOWED_MESSAGE_IDS == {207}


def _eclient_send_sites() -> dict[str, set[str]]:
    uses = re.compile(r"\bOUT\.([A-Z_]+)\b")
    return {name: set(uses.findall(inspect.getsource(fn)))
            for name, fn in inspect.getmembers(EClient, inspect.isfunction)
            if uses.search(inspect.getsource(fn))}


def test_every_eclient_method_that_names_an_out_id_is_known():
    # An ibapi upgrade that adds an order-sending method must fail here.
    order_names = {"PLACE_ORDER", "CANCEL_ORDER", "REQ_GLOBAL_CANCEL", "EXERCISE_OPTIONS"}
    sites = _eclient_send_sites()
    senders = {name for name, used in sites.items() if used & order_names}
    assert senders == {
        "placeOrder", "placeOrderProtoBuf", "cancelOrder", "cancelOrderProtoBuf",
        "reqGlobalCancel", "reqGlobalCancelProtoBuf", "exerciseOptions",
    }


ALL_OUT_IDS = sorted(
    v for k, v in vars(ibapi_message.OUT).items() if not k.startswith("_") and type(v) is int
)
# Every OUT id, its protobuf form, and ids that are not ibapi's at all.
CANDIDATE_IDS = sorted(
    set(ALL_OUT_IDS) | {i + PROTOBUF_MSG_ID for i in ALL_OUT_IDS} | {0, 6, 199, 200, 300, 999}
)
REFUSED_IDS = [i for i in CANDIDATE_IDS if i not in ALLOWED_MESSAGE_IDS]


def test_every_non_allowlisted_id_is_covered_by_the_parametrised_tests():
    assert len(REFUSED_IDS) == len(CANDIDATE_IDS) - len(ALLOWED_MESSAGE_IDS)
    assert {3, 4, 58, 203, 204, 258, 15, 21, 18, 19} <= set(REFUSED_IDS)


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", REFUSED_IDS)
def test_every_id_not_on_the_allowlist_is_refused_on_every_path(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(AccountMessageRefused):
        client.sendMsg(msg_id, "x")
    with pytest.raises(AccountMessageRefused):
        client.sendMsgProtoBuf(msg_id, b"x")
    with pytest.raises(AccountMessageRefused):  # EClient.sendMsg called around the override
        EClient.sendMsg(client, msg_id, "x")
    with pytest.raises(AccountMessageRefused):
        client.conn.sendMsg(frame(msg_id, server_version))
    with pytest.raises(AccountMessageRefused):
        client.conn.sendMsg(frame(msg_id, server_version, proto=True))
    assert stub.sent == []


@pytest.mark.parametrize("bad_id", [True, 3.0, "3", None, 71.0, "71", False])
def test_send_paths_fail_closed_on_non_int_ids(bad_id):
    client, _ = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountMessageRefused):
        client.sendMsg(bad_id, "x")
    with pytest.raises(AccountMessageRefused):
        client.sendMsgProtoBuf(bad_id, b"x")
    assert stub.sent == []


def test_protobuf_and_text_forms_are_not_interchangeable_at_method_level():
    client, _ = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountMessageRefused):
        client.sendMsg(207, "x")  # a protobuf id by the text path
    with pytest.raises(AccountMessageRefused):
        client.sendMsgProtoBuf(7, b"x")  # a text id by the protobuf path
    assert stub.sent == []


def _order_calls(client: AccountReadClient) -> dict[str, Any]:
    class Proto:
        def SerializeToString(self) -> bytes:  # noqa: N802 (protobuf's name)
            return b""

    return {
        "placeOrder": lambda: client.placeOrder(1, contract(), market_order()),
        "cancelOrder": lambda: client.cancelOrder(1, OrderCancel()),
        "reqGlobalCancel": lambda: client.reqGlobalCancel(OrderCancel()),
        "exerciseOptions": lambda: client.exerciseOptions(
            1, contract(), 1, 1, "", 0, "", "", False),
        "placeOrderProtoBuf": lambda: client.placeOrderProtoBuf(Proto()),
        "cancelOrderProtoBuf": lambda: client.cancelOrderProtoBuf(Proto()),
        "reqGlobalCancelProtoBuf": lambda: client.reqGlobalCancelProtoBuf(Proto()),
        "reqAutoOpenOrders": lambda: client.reqAutoOpenOrders(True),
        "requestFA": lambda: client.requestFA(1),
        "replaceFA": lambda: client.replaceFA(1, 1, "<x/>"),
    }


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("method", sorted(_order_calls(AccountReadClient({}, print))))
def test_place_cancel_globalcancel_refused_text_and_protobuf(method, server_version):
    client, got = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(AccountMessageRefused):
        _order_calls(client)[method]()
    assert stub.sent == []
    assert got == []


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_order_frames_built_by_ibapi_itself_are_refused_at_the_connection(server_version):
    # Bypass the client methods: build the exact bytes ibapi would write and
    # hand them to the connection the client holds.
    client, _ = make_client()
    stub = with_stub(client, server_version)
    for msg_id in (3, 4, 58):
        for proto in (False, True):
            with pytest.raises(AccountMessageRefused):
                client.conn.sendMsg(frame(msg_id + (PROTOBUF_MSG_ID if proto else 0),
                                          server_version, proto))
    assert stub.sent == []


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_allowed_reads_pass_the_filter(server_version):
    client, got = make_client()
    stub = with_stub(client, server_version)
    client.startApi()
    client.reqCurrentTime()
    client.reqPositions()
    client.cancelPositions()
    client.reqOpenOrders()
    client.reqAllOpenOrders()
    client.reqExecutions(7, ExecutionFilter())
    client.reqCompletedOrders(True)
    assert len(stub.sent) == 8
    assert got == []  # nothing was reported as an error
    ids = [parse_frame(f, first_write=False) for f in stub.sent]
    expected_exec = 207 if server_version >= AT_PROTOBUF else 7
    assert ids == [71, 49, 61, 64, 5, 16, expected_exec, 99]
    assert set(ids) <= ALLOWED_MESSAGE_IDS


def test_refusal_is_not_an_oserror():
    assert issubclass(AccountMessageRefused, RuntimeError)
    assert not issubclass(AccountMessageRefused, OSError)
    assert issubclass(AccountTripwire, RuntimeError)
    assert not issubclass(AccountTripwire, OSError)


# -- the handshake and the frame parser ---------------------------------------


def _handshake(options: str = "") -> bytes:
    body = b"v100..203" + (b" " + options.encode() if options else b"")
    return b"API\0" + len(body).to_bytes(4, "big") + body


def test_handshake_only_as_first_frame_on_a_connection():
    client, _ = make_client()
    stub = StubConn()
    client.conn = stub
    client.conn.sendMsg(_handshake())
    with pytest.raises(AccountMessageRefused):
        client.conn.sendMsg(_handshake())
    assert stub.sent == [_handshake()]
    with pytest.raises(AccountMessageRefused):
        check_frame(b"API\0" + (3).to_bytes(4, "big") + b"abc", first_write=True)
    with pytest.raises(AccountMessageRefused):
        check_frame(_handshake("a\tb"), first_write=True)


def _corpus() -> list[Any]:
    out: list[Any] = [None, "x", 3, bytearray(b"1\0"), b"", b"\0\0\0", b"\0\0\0\0",
                      (1).to_bytes(4, "big") + b"1", b"\0\0\0\x01\0", b"\0\0\0\x02\x01",
                      _handshake(), _handshake("+PACEAPI"), _handshake("a\tb"),
                      b"API\0", b"API\0\0\0\0\0", b"API\0" + (5).to_bytes(4, "big") + b"vx..y",
                      b"\0\0\0\x03\0\0\0", b"\0\0\0\x05\0\0\0\x31x"]
    for msg_id in (*range(0, 260), 300, 999, 5000, 2**31, 2**32 - 1):
        for text in (False, True):
            body = (b"%d\0" % msg_id) if text else msg_id.to_bytes(4, "big")
            body += b"fields\0"
            out.append(len(body).to_bytes(4, "big") + body)
            out.append(len(body).to_bytes(4, "big") + body + b"extra")  # wrong length
            out.append(len(body).to_bytes(4, "big") + body[:-1])  # truncated
        out.append(comm.make_msg_proto(msg_id, b"\x08\x01"))
    out += [b"007\0x", b"+7\0x", b" 7\0x", b"7x\0", b"1234567\0x"]
    for text in ("x", "1\0", "7\0"):
        for raw in (False, True):
            if raw or text.endswith("\0"):
                body = text.encode()
                out.append(len(body).to_bytes(4, "big") + body)
    return out


class _Everything:
    def __contains__(self, item: object) -> bool:
        return True


def test_frame_parser_agrees_with_data_client_corpus(monkeypatch):
    # The data client's parser with every id allowed decides only whether the
    # frame parses; ours must parse exactly the same frames, and on top of that
    # accept an id only if it is on our own list.
    corpus = _corpus()
    assert len(corpus) > 1000
    with monkeypatch.context() as m:
        m.setattr(data_client, "ALLOWED_MESSAGE_IDS", _Everything())
        wide = []
        for raw in corpus:
            for first in (True, False):
                try:
                    data_client._check_frame(raw, first)
                    wide.append(True)
                except data_client.MessageRefused:
                    wide.append(False)
    ours_wide = []
    ours_real = []
    for raw in corpus:
        for first in (True, False):
            try:
                msg_id = parse_frame(raw, first)
                ours_wide.append(True)
                ours_real.append(msg_id is None or msg_id in ALLOWED_MESSAGE_IDS)
            except AccountMessageRefused:
                ours_wide.append(False)
                ours_real.append(False)
    assert ours_wide == wide
    assert any(wide) and not all(wide)
    # The real lists: identical wherever both allow, and ours never accepts an
    # unlisted id.
    for raw in corpus:
        for first in (True, False):
            try:
                check_frame(raw, first)
                accepted = True
            except AccountMessageRefused:
                accepted = False
            try:
                data_client._check_frame(raw, first)
                theirs = True
            except data_client.MessageRefused:
                theirs = False
            if accepted:
                msg_id = parse_frame(raw, first)
                assert msg_id is None or msg_id in ALLOWED_MESSAGE_IDS
            if theirs and accepted is False:
                msg_id = parse_frame(raw, first)
                assert msg_id not in ALLOWED_MESSAGE_IDS  # the data list allows more, never less
    shared = account_client.ALLOWED_MESSAGE_IDS & data_client.ALLOWED_MESSAGE_IDS
    assert shared == {49, 71}
    for msg_id in shared:
        for text in (b"%d\0x\0" % msg_id, msg_id.to_bytes(4, "big") + b"x"):
            raw = len(text).to_bytes(4, "big") + text
            check_frame(raw, False)
            data_client._check_frame(raw, False)


# -- connect and the port guard -----------------------------------------------


@pytest.mark.parametrize("port", [7496, 4001, 5000])
def test_connect_refuses_live_port_before_any_connection(connect_recorder, port):
    client, _ = make_client()
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 24)
    assert connect_recorder == []


@pytest.mark.parametrize("port", [7497, 4002])
def test_connect_paper_port_delegates_unchanged(connect_recorder, port):
    client, _ = make_client()
    assert client.connect("127.0.0.1", port, 24) == "delegated"
    assert connect_recorder == [("127.0.0.1", port, 24)]


def test_connect_opt_in_must_be_exactly_one_and_is_read_at_call_time(connect_recorder):
    env: dict[str, str] = {OPT_IN: "true"}
    client, _ = make_client(env)
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 24)
    env[OPT_IN] = "1"
    client.connect("127.0.0.1", 7496, 24)
    del env[OPT_IN]
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 24)
    assert connect_recorder == [("127.0.0.1", 7496, 24)]


@pytest.mark.parametrize("port", ["7497", 7497.0, True, None])
def test_connect_refuses_a_port_that_is_not_an_exact_int(connect_recorder, port):
    client, _ = make_client()
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 24)
    assert connect_recorder == []


def test_real_connect_under_pytest_is_blocked(monkeypatch):
    attempts: list[Any] = []

    def spy(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("real socket attempted")

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket, "create_connection", spy)
    client, got = make_client()
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        client.connect("127.0.0.1", 7497, 24)
    assert attempts == []
    assert got == []


def test_connect_wraps_the_connection_ibapi_assigns(monkeypatch):
    inner = StubConn()

    def fake_connect(self, host, port, clientId):
        self.conn = inner  # what EClient.connect does
        return None

    monkeypatch.setattr(EClient, "connect", fake_connect)
    client, _ = make_client()
    client.connect("127.0.0.1", 7497, 24)
    assert isinstance(client.conn, account_client._GuardedConnection)
    client.serverVersion_ = AT_PROTOBUF_PLACE
    with pytest.raises(AccountMessageRefused):
        client.conn.sendMsg(frame(203, AT_PROTOBUF_PLACE, proto=True))
    assert inner.sent == []


def test_start_api_sends_no_optional_capabilities():
    client, _ = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    client.optCapab = "+PACEAPI"
    client.startApi()
    assert client.optCapab == ""
    assert len(stub.sent) == 1


# -- accounts and the tripwire ------------------------------------------------


def _exec(account: str = PAPER) -> Execution:
    e = Execution()
    e.execId, e.orderId, e.permId, e.acctNumber = "0001.1", 5, 77, account
    e.side, e.shares, e.price, e.avgPrice, e.cumQty = "BOT", Decimal(10), 10.5, 10.5, Decimal(10)
    e.time, e.exchange, e.orderRef = "20260101 09:35:04 US/Eastern", "ISLAND", "stoink-1"
    return e


def _state() -> OrderState:
    s = OrderState()
    s.status = "Submitted"
    return s


def test_managed_accounts_paper_passes_and_non_paper_trips():
    client, got = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    client.managedAccounts(f"{PAPER},DUX654321,")
    assert client.tripped is None
    assert got == [events.ManagedAccounts(f"{PAPER},DUX654321,")]
    client2, got2 = make_client()
    stub2 = with_stub(client2, AT_PROTOBUF)
    with pytest.raises(AccountTripwire):
        client2.managedAccounts(f"{PAPER},{LIVE_LOOKING}")
    assert client2.tripped is not None
    assert stub2.disconnected
    assert [type(e) for e in got2] == [events.ConnectionClosed]  # the account list never delivered
    assert stub.disconnected is False


@pytest.mark.parametrize("text", ["", " , ", "du123456", "DUX12345X", "DUX 123", "XDU1"])
def test_managed_accounts_that_do_not_look_like_paper_ids_trip(text):
    client, _ = make_client()
    with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountTripwire):
        client.managedAccounts(text)


CALLBACKS = {
    "position": lambda c, a: c.position(a, contract(), Decimal(10), 10.5),
    "openOrder": lambda c, a: c.openOrder(5, contract(), market_order(a), _state()),
    "completedOrder": lambda c, a: c.completedOrder(contract(), market_order(a), _state()),
    "execDetails": lambda c, a: c.execDetails(7, contract(), _exec(a)),
}


@pytest.mark.parametrize("name", sorted(CALLBACKS))
@pytest.mark.parametrize("account", [LIVE_LOOKING, "", "DU123456 ", "xDU1", "U1234567"])
def test_non_paper_account_in_each_callback_trips(name, account):
    client, got = make_client()
    stub = with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountTripwire) as err:
        CALLBACKS[name](client, account)
    assert account not in str(err.value) or account == ""  # the id is not echoed
    assert client.tripped is not None
    assert stub.disconnected
    assert [type(e) for e in got] == [events.ConnectionClosed]
    # after the trip everything else is dropped and every account callback raises again
    client.nextValidId(1)
    client.orderStatus(1, "Filled", Decimal(1), Decimal(0), 1.0, 1, 0, 1.0, 24, "", 0.0)
    assert [type(e) for e in got] == [events.ConnectionClosed]
    with pytest.raises(AccountTripwire):
        CALLBACKS[name](client, PAPER)


def test_non_paper_account_callback_trips():
    # The acceptance case in one place: a paper account passes, a non-paper one kills the session.
    for name, call in CALLBACKS.items():
        ok_client, ok_got = make_client()
        with_stub(ok_client, AT_PROTOBUF)
        call(ok_client, PAPER)
        assert ok_client.tripped is None, name
        assert len(ok_got) == 1, name
        bad_client, bad_got = make_client()
        stub = with_stub(bad_client, AT_PROTOBUF)
        with pytest.raises(AccountTripwire):
            call(bad_client, LIVE_LOOKING)
        assert stub.disconnected, name
        assert not [e for e in bad_got if not isinstance(e, events.ConnectionClosed)], name


def test_tripwire_runs_before_the_raw_hook_sees_the_callback():
    raw: list[str] = []
    client, _ = make_client(raw=lambda name, args: raw.append(name))
    with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountTripwire):
        client.position(LIVE_LOOKING, contract(), Decimal(1), 1.0)
    assert raw == ["connectionClosed"]


# -- events -------------------------------------------------------------------


def test_account_read_callbacks_become_events():
    client, got = make_client()
    with_stub(client, AT_PROTOBUF)
    order = market_order()
    order.permId, order.orderRef, order.tif, order.transmit = 77, "stoink-1", "GTC", True
    order.parentId, order.ocaGroup, order.lmtPrice, order.auxPrice = 4, "oca1", 11.5, 9.5
    client.openOrder(5, contract(), order, _state())
    client.openOrderEnd()
    client.completedOrder(contract(), order, _state())
    client.completedOrdersEnd()
    client.orderStatus(5, "Filled", Decimal(10), Decimal(0), 10.5, 77, 4, 10.5, 24, "", 0.0)
    client.execDetails(7, contract(), _exec())
    client.execDetailsEnd(7)
    report = CommissionAndFeesReport()
    report.execId, report.commissionAndFees, report.currency, report.realizedPNL = (
        "0001.1", 1.0, "USD", 0.0)
    client.commissionAndFeesReport(report)
    client.position(PAPER, contract(), Decimal(10), 10.5)
    client.positionEnd()
    client.currentTime(1700000000)
    assert [type(e).__name__ for e in got] == [
        "OpenOrder", "OpenOrderEnd", "CompletedOrder", "CompletedOrdersEnd", "OrderStatus",
        "ExecDetails", "ExecDetailsEnd", "CommissionReport", "Position", "PositionEnd",
        "CurrentTime",
    ]
    assert got[0] == account_client.OpenOrder(
        order_id=5, perm_id=77, client_id=order.clientId, account=PAPER, symbol="AAPL",
        sec_type="STK", con_id=265598, action="BUY", order_type="MKT",
        total_quantity=Decimal(1), lmt_price=11.5, aux_price=9.5, tif="GTC",
        order_ref="stoink-1", parent_id=4, oca_group="oca1", transmit=True, status="Submitted")
    assert got[4] == account_client.OrderStatus(
        5, "Filled", Decimal(10), Decimal(0), 10.5, 77, 4, 10.5, 24, "", 0.0)
    assert got[5].exec_id == "0001.1" and got[5].shares == Decimal(10)
    assert got[5].account == PAPER and got[5].order_ref == "stoink-1"
    assert got[7] == account_client.CommissionReport("0001.1", 1.0, "USD", 0.0)
    assert got[8] == account_client.Position(PAPER, 265598, "AAPL", "STK", "USD", Decimal(10), 10.5)
    assert client.callback_failures == 0


def test_callbacks_never_raise_into_ibapi_except_the_tripwire():
    def boom(*args: Any) -> None:
        raise ValueError("consumer bug")

    client = AccountReadClient({}, boom, boom)
    with_stub(client, AT_PROTOBUF)
    client.nextValidId(1)
    client.openOrderEnd()
    client.position(PAPER, contract(), Decimal(1), 1.0)
    client.execDetails(1, contract(), _exec())
    # four callbacks, each with a failing raw hook and a failing event hook
    assert client.callback_failures == 8
    assert set(client.first_failures) == {"on_raw", "on_event"}
    assert client.tripped is None


def test_callbacks_never_call_the_client(monkeypatch):
    client, _ = make_client()
    with_stub(client, AT_PROTOBUF)
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
    for call in CALLBACKS.values():
        call(client, PAPER)
    client.nextValidId(1)
    client.managedAccounts(PAPER)
    assert touched == []


# -- signatures ---------------------------------------------------------------


def _overridden_ewrapper_methods() -> list[str]:
    return sorted(n for n, v in vars(AccountReadClient).items()
                  if not n.startswith("_") and inspect.isfunction(v) and hasattr(EWrapper, n))


def test_signatures_match_ewrapper():
    names = _overridden_ewrapper_methods()
    assert set(names) == {
        "error", "nextValidId", "connectAck", "connectionClosed", "managedAccounts",
        "currentTime", "openOrder", "openOrderEnd", "completedOrder", "completedOrdersEnd",
        "orderStatus", "execDetails", "execDetailsEnd", "commissionAndFeesReport",
        "position", "positionEnd",
    }
    for name in names:
        assert (inspect.signature(getattr(AccountReadClient, name))
                == inspect.signature(getattr(EWrapper, name))), name


# -- client id and a final trip -----------------------------------------------


def test_client_id_constant_is_24():
    assert account_client.ACCOUNT_CLIENT_ID == 24  # decision O9 in issue 230


@pytest.mark.parametrize("client_id", [0, 1, 20, 22, 23, 25, -1, 2**31])
def test_connect_refuses_any_client_id_other_than_24(connect_recorder, client_id):
    client, _ = make_client()
    with pytest.raises(account_client.ClientIdRefused):
        client.connect("127.0.0.1", 7497, client_id)
    assert connect_recorder == []


@pytest.mark.parametrize("client_id", ["24", 24.0, True, None, b"24"])
def test_connect_refuses_a_client_id_that_is_not_an_exact_int(connect_recorder, client_id):
    client, _ = make_client()
    with pytest.raises(account_client.ClientIdRefused):
        client.connect("127.0.0.1", 7497, client_id)
    assert connect_recorder == []


def test_connect_accepts_client_id_24(connect_recorder):
    client, _ = make_client()
    assert client.connect("127.0.0.1", 7497, 24) == "delegated"
    assert connect_recorder == [("127.0.0.1", 7497, 24)]


def test_client_id_is_checked_before_the_port_guard(connect_recorder):
    client, _ = make_client()
    with pytest.raises(account_client.ClientIdRefused):
        client.connect("127.0.0.1", 7496, 0)
    assert connect_recorder == []


def test_a_tripped_client_refuses_to_connect_again(connect_recorder):
    client, _ = make_client()
    with_stub(client, AT_PROTOBUF)
    with pytest.raises(AccountTripwire):
        client.position(LIVE_LOOKING, contract(), Decimal(1), 1.0)
    with pytest.raises(AccountTripwire):
        client.connect("127.0.0.1", 7497, 24)
    assert connect_recorder == []


# -- ibapi's real decoder reaches the overrides -------------------------------


def _decoder(client: AccountReadClient, server_version: int = AT_PROTOBUF_PLACE):
    from ibapi.decoder import Decoder

    return Decoder(client, server_version)


def _b(*items: object) -> list[bytes]:
    return [str(i).encode() for i in items]


def _position_fields(account: str) -> list[bytes]:
    # POSITION_DATA after the message id: version 3.
    return _b(3, account, 265598, "AAPL", "STK", "", 0.0, "", "", "SMART", "USD", "AAPL",
              "NMS", 10, 10.5)


def test_decoder_text_position_and_managed_accounts_reach_the_overrides():
    from ibapi.message import IN

    client, got = make_client()
    with_stub(client, AT_PROTOBUF_PLACE)
    dec = _decoder(client)
    dec.interpret(_b(1, PAPER), IN.MANAGED_ACCTS)
    dec.interpret(_position_fields(PAPER), IN.POSITION_DATA)
    dec.interpret(_b(1), IN.POSITION_END)
    assert got == [
        events.ManagedAccounts(PAPER),
        account_client.Position(PAPER, 265598, "AAPL", "STK", "USD", Decimal(10), 10.5),
        account_client.PositionEnd(),
    ]


def test_decoder_text_position_non_paper_trips_and_delivers_nothing():
    from ibapi.message import IN

    client, got = make_client()
    stub = with_stub(client, AT_PROTOBUF_PLACE)
    dec = _decoder(client)
    with pytest.raises(AccountTripwire):
        dec.interpret(_position_fields(LIVE_LOOKING), IN.POSITION_DATA)
    assert stub.disconnected
    assert [type(e) for e in got] == [events.ConnectionClosed]


def test_decoder_text_managed_accounts_non_paper_trips():
    from ibapi.message import IN

    client, got = make_client()
    stub = with_stub(client, AT_PROTOBUF_PLACE)
    with pytest.raises(AccountTripwire):
        _decoder(client).interpret(_b(1, f"{PAPER},{LIVE_LOOKING}"), IN.MANAGED_ACCTS)
    assert stub.disconnected
    assert [type(e) for e in got] == [events.ConnectionClosed]


def _open_order_proto(account: str) -> bytes:
    from ibapi.protobuf.OpenOrder_pb2 import OpenOrder as OpenOrderProto

    p = OpenOrderProto()
    p.orderId = 5
    p.contract.symbol, p.contract.secType, p.contract.conId = "AAPL", "STK", 265598
    p.order.action, p.order.orderType, p.order.totalQuantity = "BUY", "MKT", "10"
    p.order.account, p.order.permId, p.order.orderRef = account, 77, "stoink-1"
    p.orderState.status = "Submitted"
    return p.SerializeToString()


def _execution_proto(account: str) -> bytes:
    from ibapi.protobuf.ExecutionDetails_pb2 import ExecutionDetails as ExecutionDetailsProto

    p = ExecutionDetailsProto()
    p.reqId = 7
    p.contract.symbol, p.contract.secType = "AAPL", "STK"
    p.execution.execId, p.execution.acctNumber, p.execution.orderId = "0001.1", account, 5
    p.execution.side, p.execution.shares, p.execution.price = "BOT", "10", 10.5
    return p.SerializeToString()


def test_decoder_protobuf_open_order_and_execution_reach_the_overrides():
    from ibapi.message import IN

    client, got = make_client()
    with_stub(client, AT_PROTOBUF_PLACE)
    dec = _decoder(client)
    dec.processProtoBuf(_open_order_proto(PAPER), IN.OPEN_ORDER)
    dec.processProtoBuf(_execution_proto(PAPER), IN.EXECUTION_DATA)
    assert [type(e).__name__ for e in got] == ["OpenOrder", "ExecDetails"]
    assert got[0].account == PAPER and got[0].order_id == 5 and got[0].order_ref == "stoink-1"
    assert got[1].account == PAPER and got[1].exec_id == "0001.1"


@pytest.mark.parametrize("which", ["open_order", "execution"])
def test_decoder_protobuf_non_paper_trips_and_delivers_nothing(which):
    from ibapi.message import IN

    client, got = make_client()
    stub = with_stub(client, AT_PROTOBUF_PLACE)
    dec = _decoder(client)
    payload, msg_id = ((_open_order_proto(LIVE_LOOKING), IN.OPEN_ORDER) if which == "open_order"
                       else (_execution_proto(LIVE_LOOKING), IN.EXECUTION_DATA))
    with pytest.raises(AccountTripwire):
        dec.processProtoBuf(payload, msg_id)
    assert stub.disconnected
    assert [type(e) for e in got] == [events.ConnectionClosed]
