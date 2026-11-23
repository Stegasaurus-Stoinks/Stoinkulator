"""Gaps in `tests/test_ib_client.py`: the wire, the thresholds, the callbacks.

Same rules as there: nothing connects, requests go to a stub connection that
records the frame it was given.
"""

import enum
import logging
import struct
import threading
import time
from decimal import Decimal
from typing import Any

import pytest
from ibapi import client as ibapi_client
from ibapi import comm
from ibapi import message as ibapi_message
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID, UNSET_DECIMAL, BarData, TickAttrib
from ibapi.message import OUT
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.wrapper import EWrapper

from MainStoinker.ib import client as client_module
from MainStoinker.ib import events
from MainStoinker.ib.client import ALLOWED_MESSAGE_IDS, DataOnlyClient, MessageRefused
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.test_ib_client import (
    OPT_IN,
    ORDER_AND_PROTOBUF_IDS,
    StubConn,
    connect_recorder,  # noqa: F401 (fixture)
    contract,
    make_client,
    with_stub,
)

# 100 and 200 are below MIN_SERVER_VER_PROTOBUF (201): the id is sent as text.
# 201 and 202 send a raw 4-byte id but still use sendMsg for orders; 203 and up
# send order requests as protobuf.
SERVER_VERSIONS = [100, 200, 201, 202, 203, 210]


def _wire_id(frame: bytes, server_version: int, proto: bool) -> int:
    (length,) = struct.unpack("!I", frame[:4])
    body = frame[4:]
    assert length == len(body)
    if proto or server_version >= 201:
        return int.from_bytes(body[:4], "big")
    return int(body[: body.index(b"\0")])


# -- what is judged is what goes on the wire --------------------------------


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_the_id_that_is_judged_is_the_id_on_the_wire(server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    for msg_id in range(0, 501):
        before = len(stub.sent)
        with pytest.raises(MessageRefused):
            client.sendMsgProtoBuf(msg_id, b"")
        if msg_id not in ALLOWED_MESSAGE_IDS:
            with pytest.raises(MessageRefused):
                client.sendMsg(msg_id, "1\0")
            assert len(stub.sent) == before, msg_id
            continue
        client.sendMsg(msg_id, "1\0")
        assert len(stub.sent) == before + 1
        assert _wire_id(stub.sent[-1], server_version, proto=False) == msg_id


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", ORDER_AND_PROTOBUF_IDS)
def test_refusal_by_keyword_arguments_writes_nothing(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(MessageRefused):
        client.sendMsg(msgId=msg_id, msg="x")
    with pytest.raises(MessageRefused):
        client.sendMsgProtoBuf(msg=b"x", msgId=msg_id)
    assert stub.sent == []


class _Three(enum.IntEnum):
    PLACE = 3
    OTHER = 1


@pytest.mark.parametrize(
    "bad_id",
    [" 3", "3 ", "03", "+3", "3\0", "203", b"3", b"\x00\x00\x00\x03", 3.0, 203.0,
     float("nan"), _Three.PLACE, _Three.OTHER, True, False, None, object(), [3], (3,)],
)
def test_ids_that_are_not_exactly_int_are_refused_and_nothing_is_written(bad_id):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused):
        client.sendMsg(bad_id, "x")
    with pytest.raises(MessageRefused):
        client.sendMsgProtoBuf(bad_id, b"x")
    assert stub.sent == []


@pytest.mark.parametrize("bad_id", [-3, -203, 2**32 + 3, 2**32 + 203, 2**40])
def test_out_of_range_ids_never_become_an_order_frame_on_a_modern_server(bad_id):
    client, _ = make_client()
    stub = with_stub(client, 203)
    for send in (lambda: client.sendMsg(bad_id, "x"), lambda: client.sendMsgProtoBuf(bad_id, b"x")):
        try:
            send()
        except (MessageRefused, OverflowError):
            pass
    assert stub.sent == []


def test_data_cancels_and_start_api_pass_at_modern_server_versions():
    for server_version in (203, 210):
        client, got = make_client()
        stub = with_stub(client, server_version)
        client.clientId = 21
        client.startApi()
        client.cancelHistoricalData(3)
        client.cancelMktData(4)
        client.cancelHeadTimeStamp(5)
        client.reqMarketDataType(3)
        client.reqCurrentTime()
        assert len(stub.sent) == 6, server_version
        assert got == []
        assert _wire_id(stub.sent[0], server_version, proto=False) == OUT.START_API


@pytest.mark.parametrize("server_version", [200, 202, 203])
def test_order_requests_are_refused_and_not_reported_as_ibapi_errors(server_version):
    # EClient wraps its encoding in `except ClientException` and reports through
    # wrapper.error; the refusal must not be turned into such a callback.
    client, got = make_client()
    stub = with_stub(client, server_version)
    calls = [
        lambda: client.placeOrder(1, contract(), _market_order()),
        lambda: client.cancelOrder(1, OrderCancel()),
        lambda: client.reqGlobalCancel(OrderCancel()),
        lambda: client.exerciseOptions(1, contract(), 1, 1, "", 0, "", "", False),
    ]
    for call in calls:
        with pytest.raises(MessageRefused):
            call()
    assert stub.sent == []
    assert got == []


def _market_order() -> Order:
    o = Order()
    o.action, o.orderType, o.totalQuantity = "BUY", "MKT", Decimal(1)
    return o


@pytest.mark.parametrize("server_version", [202, 203])
def test_order_request_without_a_connection_sends_nothing(server_version):
    # Below 203 ibapi reports "not connected" (504) before it reaches the filter;
    # from 203 its protobuf path does not check the connection and the filter
    # refuses. A client that never saw a server version raises TypeError in
    # ibapi's own useProtoBuf. In every case nothing is sent.
    client, got = make_client()
    client.serverVersion_ = server_version
    calls = [
        lambda: client.placeOrder(1, contract(), _market_order()),
        lambda: client.cancelOrder(1, OrderCancel()),
        lambda: client.reqGlobalCancel(OrderCancel()),
    ]
    for call in calls:
        try:
            call()
        except MessageRefused:
            assert server_version >= 203
    codes = [e.code for e in got if isinstance(e, events.IbError)]
    assert codes == ([504, 504, 504] if server_version < 203 else [])
    assert client.conn is None


# -- every outgoing message id of the installed ibapi is classified ----------

READ_ONLY_OR_NEEDED = {
    "REQ_MKT_DATA", "CANCEL_MKT_DATA", "REQ_ACCT_DATA", "REQ_EXECUTIONS", "REQ_IDS",
    "REQ_CONTRACT_DATA", "REQ_MKT_DEPTH", "CANCEL_MKT_DEPTH", "REQ_NEWS_BULLETINS",
    "CANCEL_NEWS_BULLETINS", "REQ_MANAGED_ACCTS", "REQ_FA", "REQ_HISTORICAL_DATA",
    "REQ_SCANNER_SUBSCRIPTION", "CANCEL_SCANNER_SUBSCRIPTION", "REQ_SCANNER_PARAMETERS",
    "CANCEL_HISTORICAL_DATA", "REQ_CURRENT_TIME", "REQ_REAL_TIME_BARS",
    "CANCEL_REAL_TIME_BARS", "REQ_FUNDAMENTAL_DATA", "CANCEL_FUNDAMENTAL_DATA",
    "REQ_CALC_IMPLIED_VOLAT", "REQ_CALC_OPTION_PRICE", "CANCEL_CALC_IMPLIED_VOLAT",
    "CANCEL_CALC_OPTION_PRICE", "REQ_MARKET_DATA_TYPE", "REQ_POSITIONS",
    "REQ_ACCOUNT_SUMMARY", "CANCEL_ACCOUNT_SUMMARY", "CANCEL_POSITIONS", "VERIFY_REQUEST",
    "VERIFY_MESSAGE", "QUERY_DISPLAY_GROUPS", "SUBSCRIBE_TO_GROUP_EVENTS",
    "UNSUBSCRIBE_FROM_GROUP_EVENTS", "START_API", "VERIFY_AND_AUTH_REQUEST",
    "VERIFY_AND_AUTH_MESSAGE", "REQ_POSITIONS_MULTI", "CANCEL_POSITIONS_MULTI",
    "REQ_ACCOUNT_UPDATES_MULTI", "CANCEL_ACCOUNT_UPDATES_MULTI", "REQ_SEC_DEF_OPT_PARAMS",
    "REQ_SOFT_DOLLAR_TIERS", "REQ_FAMILY_CODES", "REQ_MATCHING_SYMBOLS",
    "REQ_MKT_DEPTH_EXCHANGES", "REQ_SMART_COMPONENTS", "REQ_NEWS_ARTICLE",
    "REQ_NEWS_PROVIDERS", "REQ_HISTORICAL_NEWS", "REQ_HEAD_TIMESTAMP",
    "REQ_HISTOGRAM_DATA", "CANCEL_HISTOGRAM_DATA", "CANCEL_HEAD_TIMESTAMP",
    "REQ_MARKET_RULE", "REQ_PNL", "CANCEL_PNL", "REQ_PNL_SINGLE", "CANCEL_PNL_SINGLE",
    "REQ_HISTORICAL_TICKS", "REQ_TICK_BY_TICK_DATA", "CANCEL_TICK_BY_TICK_DATA",
    "REQ_COMPLETED_ORDERS", "REQ_WSH_META_DATA", "CANCEL_WSH_META_DATA",
    "REQ_WSH_EVENT_DATA", "CANCEL_WSH_EVENT_DATA", "REQ_USER_INFO",
    "REQ_CURRENT_TIME_IN_MILLIS", "REQ_OPEN_ORDERS", "REQ_ALL_OPEN_ORDERS",
}  # fmt: skip
ORDER_WRITES = {"PLACE_ORDER", "CANCEL_ORDER", "EXERCISE_OPTIONS", "REQ_GLOBAL_CANCEL"}
# Messages that change something at TWS or the account without being an order.
STATE_CHANGING = {
    "REPLACE_FA": "replaces the financial-advisor allocation groups/profiles/aliases",
    "REQ_AUTO_OPEN_ORDERS": "binds orders created in TWS to this API client",
    "UPDATE_DISPLAY_GROUP": "changes the contract shown in a TWS display group",
    "SET_SERVER_LOGLEVEL": "changes TWS's log level",
}


def test_every_outgoing_message_id_of_the_installed_ibapi_is_classified():
    names = {k for k, v in vars(ibapi_message.OUT).items()
             if not k.startswith("_") and isinstance(v, int)}
    classified = READ_ONLY_OR_NEEDED | ORDER_WRITES | set(STATE_CHANGING)
    assert names == classified, (
        f"unclassified: {sorted(names - classified)}; gone: {sorted(classified - names)}"
    )
    assert not (READ_ONLY_OR_NEEDED & (ORDER_WRITES | set(STATE_CHANGING)))


@pytest.mark.parametrize("name", sorted(STATE_CHANGING))
def test_account_or_tws_state_changing_messages_are_refused(name):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused):
        client.sendMsg(getattr(OUT, name), "x")
    assert stub.sent == []


def _all_out_ids() -> dict[str, int]:
    return {k: v for k, v in vars(ibapi_message.OUT).items()
            if not k.startswith("_") and isinstance(v, int)}


def test_there_are_81_out_ids_and_the_allowlist_is_a_subset():
    ids = _all_out_ids()
    assert len(ids) == 81
    assert ALLOWED_MESSAGE_IDS <= set(ids.values())


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("name,msg_id", sorted(_all_out_ids().items()))
def test_every_out_id_is_allowed_or_refused_with_nothing_written(name, msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    if msg_id in ALLOWED_MESSAGE_IDS:
        client.sendMsg(msg_id, "1\0")
        assert len(stub.sent) == 1
        assert _wire_id(stub.sent[0], server_version, proto=False) == msg_id
    else:
        with pytest.raises(MessageRefused, match=name):
            client.sendMsg(msg_id, "1\0")
        # the same frame, handed to the connection directly, is refused too
        frame = comm.make_msg(msg_id, server_version >= 201, "1\0")
        with pytest.raises(MessageRefused):
            client.conn.sendMsg(frame)
        assert stub.sent == []
    with pytest.raises(MessageRefused):
        client.sendMsgProtoBuf(msg_id, b"")
    assert len(stub.sent) == (1 if msg_id in ALLOWED_MESSAGE_IDS else 0)


# -- connect and the guard -----------------------------------------------------


@pytest.mark.parametrize("value", ["true", " 1", "1 ", "01", "0", "", "yes", "True", 1, True, None])
def test_connect_opt_in_other_values_refuse(connect_recorder, value):  # noqa: F811
    client, _ = make_client({OPT_IN: value})  # type: ignore[dict-item]
    for port in (7496, 4001):
        with pytest.raises(BrokerPortRefused):
            client.connect("127.0.0.1", port, 21)
    assert connect_recorder == []


@pytest.mark.parametrize("port", ["7496", "4001", 7496.0, True, b"7496", " 7497", "7497 ", "+7497",
                                  "7_497", -7497, None, 0, 7497.5])
def test_connect_odd_ports_are_refused_without_opt_in(connect_recorder, port):  # noqa: F811
    client, _ = make_client()
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 21)  # type: ignore[arg-type]
    assert connect_recorder == []


@pytest.mark.parametrize("port", [7497.0, True, " 7497", "٧٤٩٧"])
def test_connect_odd_ports_are_refused_even_with_opt_in_when_not_an_int(connect_recorder, port):  # noqa: F811
    # The opt-in does not make a malformed port acceptable.
    client, _ = make_client({OPT_IN: "1"})
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 21)  # type: ignore[arg-type]
    assert connect_recorder == []


def test_refused_connect_leaves_the_client_untouched(connect_recorder):  # noqa: F811
    client, got = make_client()
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 21)
    assert (client.host, client.port, client.clientId, client.conn) == (None, None, None, None)
    assert client.connState == EClient.DISCONNECTED
    assert got == []  # a refusal is raised, not reported through a callback


def test_each_connect_call_is_judged_on_its_own(connect_recorder):  # noqa: F811
    client, _ = make_client()
    client.connect("127.0.0.1", 7497, 21)
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", 7496, 21)
    client.connect("127.0.0.1", 4002, 21)
    assert [c[1] for c in connect_recorder] == [7497, 4002]


def test_two_clients_do_not_share_env_or_counters(connect_recorder):  # noqa: F811
    opted, _ = make_client({OPT_IN: "1"})
    plain, _ = make_client({})
    opted.connect("127.0.0.1", 7496, 21)
    with pytest.raises(BrokerPortRefused):
        plain.connect("127.0.0.1", 7496, 21)

    def boom(*_args: Any) -> None:
        raise ValueError("x")

    failing = DataOnlyClient({}, boom)
    failing.connectAck()
    assert failing.callback_failures == 1
    assert plain.callback_failures == opted.callback_failures == 0


def test_guard_sees_the_port_ibapi_will_use(connect_recorder):  # noqa: F811
    client, _ = make_client()
    client.connect("127.0.0.1", 7497, 21)
    assert connect_recorder[0][1] == 7497 and type(connect_recorder[0][1]) is int


# -- events and callbacks -----------------------------------------------------


def test_events_are_frozen_and_hashable():
    client, got = make_client()
    b = BarData()
    b.date, b.open, b.high, b.low, b.close = "1700000000", -1.0, -1.0, -1.0, -1.0
    client.historicalData(1, b)
    client.error(-1, 1700000000000, 2104, "ok")
    client.tickPrice(1, 1, -1.0, TickAttrib())
    client.tickSize(1, 0, Decimal(5))
    client.managedAccounts("DU1,DU2")
    client.connectAck()
    client.connectionClosed()
    for ev in got:
        assert hash(ev) == hash(ev)
        assert {ev: 1}[ev] == 1
        with pytest.raises(Exception, match="cannot assign"):
            ev.req_id = 5  # type: ignore[attr-defined]
        with pytest.raises(Exception, match="cannot assign"):
            ev.new_attribute = 5  # type: ignore[attr-defined]


def test_bar_and_tick_values_are_kept_exactly():
    client, got = make_client()
    b = BarData()  # untouched: volume and wap are ibapi's UNSET_DECIMAL
    b.date = "1700000000"  # formatDate=2 gives epoch seconds as a string
    b.open, b.close = -1.0, 0.1 + 0.2
    client.historicalData(9, b)
    ev = got[0]
    assert ev.date == "1700000000" and isinstance(ev.date, str)
    assert ev.open == -1.0
    assert ev.close == 0.1 + 0.2
    assert ev.volume is b.volume and ev.volume == UNSET_DECIMAL
    assert isinstance(ev.volume, Decimal)
    b2 = BarData()
    b2.volume = Decimal("12345678.9")
    client.historicalDataUpdate(9, b2)
    assert got[1].volume == Decimal("12345678.9") and got[1].update is True
    client.tickPrice(1, 4, -1.0, TickAttrib())
    assert got[2].price == -1.0
    client.tickSize(1, 5, Decimal("0.5"))
    assert got[3].size == Decimal("0.5")


def test_error_with_ibapi_default_json_and_with_a_reject_payload():
    client, got = make_client()
    client.error(3, 1700000000123, 201, "Order rejected")
    client.error(3, 1700000000124, 201, "Order rejected", '{"x": 1}')
    assert got[0].advanced_order_reject_json == ""
    assert got[1].advanced_order_reject_json == '{"x": 1}'
    assert got[1].error_time_ms == 1700000000124 and got[1].code == 201


def test_callbacks_from_another_thread_are_delivered_and_isolated_failures_counted():
    seen: list[tuple[str, int]] = []
    lock = threading.Lock()

    def on_event(ev: events.Event) -> None:
        with lock:
            seen.append((threading.current_thread().name, hash(ev)))

    client = DataOnlyClient({}, on_event)

    def worker() -> None:
        for i in range(50):
            client.nextValidId(i)

    t = threading.Thread(target=worker, name="reader-like", daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert len(seen) == 50 and {n for n, _ in seen} == {"reader-like"}
    assert client.callback_failures == 0


def test_a_consumer_failure_is_counted_per_failing_hook_and_never_raises():
    def boom(*_a: Any) -> None:
        raise RuntimeError("consumer")

    client = DataOnlyClient({}, boom, boom)
    client.currentTime(1)
    assert client.callback_failures == 2  # the raw hook and the event hook
    # BaseException (a deliberate stop) is not swallowed.

    def stop(*_a: Any) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        DataOnlyClient({}, stop).currentTime(1)


def test_overridden_send_paths_keep_ibapi_parameter_names():
    import inspect

    for name in ("sendMsg", "sendMsgProtoBuf", "connect"):
        assert (list(inspect.signature(getattr(DataOnlyClient, name)).parameters)
                == list(inspect.signature(getattr(EClient, name)).parameters)), name


def test_protobuf_offset_is_two_hundred():
    assert PROTOBUF_MSG_ID == 200
    assert ORDER_AND_PROTOBUF_IDS == [3, 4, 21, 58, 203, 204, 221, 258]


def test_stub_is_the_only_thing_written_to():
    # Guards the helpers themselves: the stub records whole frames only.
    client, _ = make_client()
    stub = with_stub(client, 203)
    assert isinstance(client.conn._inner, StubConn)
    client.reqCurrentTime()
    assert all(isinstance(f, bytes) for f in stub.sent) and len(stub.sent) == 1


# -- layer two: the guard on the connection object -----------------------------


def _frame(server_version: int, msg_id: int, text: str = "1\0") -> bytes:
    return comm.make_msg(msg_id, server_version >= 201, text)


def _handshake(options: str = "") -> bytes:
    version = "v100..203" + (" " + options if options else "")
    return b"API\0" + comm.make_initial_msg(version)


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", ORDER_AND_PROTOBUF_IDS)
def test_unbound_eclient_sendmsg_is_refused_at_the_connection(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(MessageRefused):
        EClient.sendMsg(client, msg_id, "x")
    with pytest.raises(MessageRefused):
        EClient.sendMsgProtoBuf(client, msg_id, b"x")
    assert stub.sent == []


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", ORDER_AND_PROTOBUF_IDS)
def test_raw_frames_written_to_conn_are_refused(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    for frame in (_frame(server_version, msg_id), comm.make_msg_proto(msg_id, b"x"),
                  _frame(True, msg_id), _frame(False, msg_id)):
        with pytest.raises(MessageRefused):
            client.conn.sendMsg(frame)
    assert stub.sent == []


def _framed(body: bytes) -> bytes:
    return struct.pack("!I", len(body)) + body


UNPARSEABLE_OR_UNALLOWED = [
    b"",
    b"\0\0\0",
    b"\0\0\0\0",
    _framed(b""),
    _framed(b"1"),  # a text id with no terminator, and too short to be a raw id
    _framed(b"\0\0\0"),
    _framed(b"\0\0\0\x03x"),  # raw PLACE_ORDER
    _framed(b"\0\0\0\xcbx"),  # raw 203
    _framed(b"\x01\0\0\x01x"),  # 2**24 + 1
    _framed(b"3\0x"),
    _framed(b"03\0"),
    _framed(b" 3\0"),
    _framed(b"+3\0"),
    _framed(b"3.0\0"),
    _framed(b"0\0"),
    _framed(b"abc\0"),
    _framed(b"\xff\xff\xff\xff"),
    _frame(203, OUT.REQ_MKT_DATA) + b"x",  # trailing byte
    _frame(203, OUT.REQ_MKT_DATA)[:-1],  # truncated
    _frame(203, OUT.REQ_MKT_DATA) + _frame(203, OUT.PLACE_ORDER),  # two frames in one write
    _frame(100, OUT.REQ_MKT_DATA) + _frame(100, OUT.PLACE_ORDER),
    struct.pack("!I", 2**32 - 1) + b"x",
]


@pytest.mark.parametrize("frame", UNPARSEABLE_OR_UNALLOWED)
def test_unparseable_or_unallowed_frames_fail_closed(frame):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(frame)
    assert stub.sent == []


@pytest.mark.parametrize(
    "frame", ["x", bytearray(_frame(203, 1)), memoryview(_frame(203, 1)), None, 3]
)
def test_frames_that_are_not_plain_bytes_fail_closed(frame):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(frame)
    assert stub.sent == []


def test_bytes_subclass_frames_fail_closed():
    class Sneaky(bytes):
        pass

    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(Sneaky(_frame(203, OUT.REQ_MKT_DATA)))
    assert stub.sent == []


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_allowed_frames_pass_the_connection_guard(server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    for msg_id in sorted(ALLOWED_MESSAGE_IDS):
        frame = _frame(server_version, msg_id)
        assert client.conn.sendMsg(frame) is None
        assert stub.sent[-1] == frame
    assert len(stub.sent) == len(ALLOWED_MESSAGE_IDS)


@pytest.mark.parametrize("options", ["", "+PACEAPI", "x y z"])
def test_the_handshake_frame_is_recognised_exactly(options):
    client, _ = make_client()
    stub = with_stub(client, 100)
    client.conn.sendMsg(_handshake(options))
    assert stub.sent == [_handshake(options)]


@pytest.mark.parametrize(
    "frame",
    [
        b"API\0",
        b"API",
        b"API\0" + _frame(203, OUT.REQ_MKT_DATA),  # an allowed message is not a handshake
        b"API\0" + _frame(203, OUT.PLACE_ORDER),
        b"API\0" + comm.make_initial_msg("v100..203\0"),
        b"API\0" + comm.make_initial_msg("v100..203\n3\0"),
        b"API\0" + comm.make_initial_msg("hello"),
        b"API\0" + comm.make_initial_msg("v100..203") + b"x",
        b"API\0" + comm.make_initial_msg("v100..203") + _frame(203, OUT.PLACE_ORDER),
        b"api\0" + comm.make_initial_msg("v100..203"),
        b"API\1" + comm.make_initial_msg("v100..203"),
        b"API\0API\0" + comm.make_initial_msg("v100..203"),
    ],
)
def test_malformed_handshakes_are_refused(frame):
    client, _ = make_client()
    stub = with_stub(client, 100)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(frame)
    assert stub.sent == []


def test_handshake_length_prefix_must_equal_body_length():
    body = b"v100..203"
    for prefix_len in (len(body) - 1, len(body) + 1, 0, 2**32 - 1):
        frame = b"API\0" + struct.pack("!I", prefix_len) + body
        client, _ = make_client()
        stub = with_stub(client, 100)
        with pytest.raises(MessageRefused):
            client.conn.sendMsg(frame)
        assert stub.sent == []
    client, _ = make_client()
    stub = with_stub(client, 100)
    client.conn.sendMsg(b"API\0" + struct.pack("!I", len(body)) + body)
    assert len(stub.sent) == 1


@pytest.mark.parametrize("bad", ["\0", "\t", "\n", "\r", "\x1f", "\x7f", "\x80", "\xff"])
def test_handshake_options_must_be_printable_ascii(bad):
    # Allowed: one space then bytes 0x20-0x7e. (ibapi's own check would also let
    # tab, LF and CR through; the guard is stricter and fails closed.)
    for options in (bad, "ok" + bad, bad + "ok"):
        client, _ = make_client()
        stub = with_stub(client, 100)
        frame = b"API\0" + _framed(b"v100..203 " + options.encode("latin-1"))
        with pytest.raises(MessageRefused):
            client.conn.sendMsg(frame)
        assert stub.sent == []


def test_handshake_options_accept_every_printable_ascii_byte():
    options = "".join(chr(c) for c in range(0x20, 0x7F))
    client, _ = make_client()
    stub = with_stub(client, 100)
    client.conn.sendMsg(_handshake(options))
    assert len(stub.sent) == 1


@pytest.mark.parametrize(
    "body",
    [b"01\0", b"00\0", b"02\0", b"09\0", b"020\0", b"0001\0", b"0049\0", b"0087\0", b"090\0"],
)
def test_text_ids_with_a_leading_zero_are_refused(body):
    # `01` would read as REQ_MKT_DATA (1) if leading zeros were accepted.
    client, _ = make_client()
    stub = with_stub(client, 100)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(_framed(body))
    assert stub.sent == []


def test_text_id_without_leading_zero_is_still_accepted():
    client, _ = make_client()
    stub = with_stub(client, 100)
    client.conn.sendMsg(_framed(b"1\0"))
    client.conn.sendMsg(_framed(b"20\0"))
    assert len(stub.sent) == 2


def test_handshake_is_accepted_only_as_the_first_write():
    client, _ = make_client()
    stub = with_stub(client, 100)
    client.conn.sendMsg(_handshake())
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(_handshake())
    assert stub.sent == [_handshake()]
    # after an ordinary frame, a handshake is refused too
    client2, _ = make_client()
    stub2 = with_stub(client2, 100)
    client2.conn.sendMsg(_frame(100, OUT.REQ_CURRENT_TIME))
    with pytest.raises(MessageRefused):
        client2.conn.sendMsg(_handshake())
    assert stub2.sent == [_frame(100, OUT.REQ_CURRENT_TIME)]


def test_the_first_write_state_is_per_wrapped_connection():
    client, _ = make_client()
    with_stub(client, 100)
    client.conn.sendMsg(_handshake())
    stub2 = StubConn()
    client.conn = stub2  # a new connection object: its own first write
    client.conn.sendMsg(_handshake())
    assert stub2.sent == [_handshake()]


def test_a_refused_frame_does_not_use_up_the_first_write():
    client, _ = make_client()
    stub = with_stub(client, 100)
    with pytest.raises(MessageRefused):
        client.conn.sendMsg(_frame(100, OUT.PLACE_ORDER))
    client.conn.sendMsg(_handshake())
    assert stub.sent == [_handshake()]


# -- regulatory snapshots are billed per request and stay off -----------------


@pytest.mark.parametrize("value", [True, 1, "false", "False", None, 0, "", [], 1.0])
def test_regulatory_snapshot_other_than_false_is_refused(value):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(MessageRefused, match="regulatory snapshots are billed"):
        client.reqMktData(4, contract(), "", False, value, [])
    assert stub.sent == []


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_regulatory_snapshot_false_is_byte_identical_to_plain_eclient(server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    plain = EClient(EWrapper())
    plain_stub = StubConn()
    plain.conn = plain_stub
    plain.serverVersion_ = server_version
    plain.connState = EClient.CONNECTED
    client.reqMktData(4, contract(), "233", True, False, [])
    plain.reqMktData(4, contract(), "233", True, False, [])
    assert len(stub.sent) == 1
    assert stub.sent == plain_stub.sent


def test_req_mkt_data_signature_equals_eclients():
    import inspect

    assert inspect.signature(DataOnlyClient.reqMktData) == inspect.signature(EClient.reqMktData)


def test_the_wire_field_that_carries_regulatory_snapshot_is_zero():
    # The refusal matters because True would put "1" in that field.
    client, _ = make_client()
    stub = with_stub(client, 203)
    client.reqMktData(4, contract(), "", False, False, [])
    fields = stub.sent[0][4:].split(b"\0")
    assert fields[-3:] == [b"0", b"", b""]  # regulatorySnapshot, options, end


def test_every_assignment_to_conn_is_wrapped_and_forwarded():
    client, _ = make_client()
    stub = with_stub(client, 203)
    assert client.conn is not stub
    # reads and writes of attributes reach the wrapped object
    stub.marker = 1
    assert client.conn.marker == 1
    client.conn.other = 2
    assert stub.other == 2
    assert client.conn.isConnected() is True
    # assigning an already-wrapped connection does not wrap twice
    wrapped = client.conn
    client.conn = wrapped
    assert client.conn is wrapped
    client.conn = None
    assert client.conn is None
    # out of scope (cannot be prevented in Python): code that writes the private
    # attribute `_conn` or the instance __dict__ replaces the guard.
    raw = StubConn()
    client.conn = raw
    assert client.conn._inner is raw


# NOTE: this replaces `ibapi.client.Connection`, the class that the pytest
# broker-connection block in tests/conftest.py patches. It is safe only because
# the fake neither subclasses `Connection` nor touches `socket`. This pattern is
# allowed in this file and in tests/test_fake_ib_extra.py only and must not be
# copied elsewhere.
class FakeConnection:
    """Stands in for ibapi.connection.Connection: no socket, scripted replies."""

    instances: list["FakeConnection"] = []

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.sent: list[bytes] = []
        self.connected = False
        self.replies = [_framed(b"203\x0020240101 00:00:00 EST\0")]
        self.wrapper: Any = None
        FakeConnection.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def sendMsg(self, msg: bytes) -> int:  # noqa: N802
        self.sent.append(msg)
        return len(msg)

    def recvMsg(self) -> bytes:  # noqa: N802
        if self.replies:
            return self.replies.pop(0)
        time.sleep(0.005)
        return b""


def test_connect_disconnect_and_reader_work_through_the_wrapper(monkeypatch):
    # The real EClient.connect, handshake, reader thread, startApi and disconnect,
    # against a fake Connection (no socket is created). This sidesteps the
    # pytest broker-connection block in tests/conftest.py by replacing the class
    # it patches; it is safe only because the fake neither subclasses
    # `Connection` nor touches `socket`. Allowed in this file and
    # tests/test_fake_ib_extra.py only; do not copy it.
    # The endpoint is TEST-NET-1 (192.0.2.1, not routable) on a port that is not
    # a TWS port. The port guard refuses every non-paper port without the
    # opt-in, so the opt-in is supplied through this client's own env mapping
    # only, never through os.environ. Even if the swap failed, nothing would
    # point at a TWS port.
    FakeConnection.instances = []
    monkeypatch.setattr(ibapi_client, "Connection", FakeConnection)
    client, got = make_client({OPT_IN: "1"})
    client.connect("192.0.2.1", 49152, 21)
    reader = client.reader
    try:
        fake = FakeConnection.instances[0]
        assert fake.host == "192.0.2.1" and fake.port == 49152
        assert fake.sent[0] == _handshake_for_connect()
        assert fake.sent[1] == _frame(203, OUT.START_API, "2\x0021\0\0")
        assert len(fake.sent) == 2
        assert client.isConnected()
        assert client.conn._inner is fake
        assert client.reader.is_alive()
        assert got == [events.ConnectAck()]
        # an order frame written through the wrapper never reaches the fake
        with pytest.raises(MessageRefused):
            client.conn.sendMsg(_frame(203, OUT.PLACE_ORDER))
        with pytest.raises(MessageRefused):
            EClient.sendMsg(client, OUT.PLACE_ORDER, "x")
        assert len(fake.sent) == 2
    finally:
        try:
            client.disconnect()
        finally:
            reader.join(5)
    assert not reader.is_alive()
    assert client.conn is None
    assert not client.isConnected()
    assert got[-1] == events.ConnectionClosed()


def _handshake_for_connect() -> bytes:
    from ibapi.server_versions import MAX_CLIENT_VER, MIN_CLIENT_VER

    return b"API\0" + comm.make_initial_msg(f"v{MIN_CLIENT_VER}..{MAX_CLIENT_VER}")


# -- the allowed requests are unchanged on the wire -----------------------------


def _requests(c: EClient) -> list[Any]:
    return [
        lambda: c.startApi(),
        lambda: c.reqMktData(4, contract(), "", False, False, []),
        lambda: c.cancelMktData(4),
        lambda: c.reqContractDetails(2, contract()),
        lambda: c.reqHistoricalData(3, contract(), "", "1 D", "1 min", "TRADES", 1, 1, False, []),
        lambda: c.cancelHistoricalData(3),
        lambda: c.reqCurrentTime(),
        lambda: c.reqMarketDataType(3),
        lambda: c.reqHeadTimeStamp(1, contract(), "TRADES", 1, 1),
        lambda: c.cancelHeadTimeStamp(1),
    ]


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_allowed_requests_are_byte_identical_to_an_unwrapped_eclient(server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    client.clientId = 21
    plain = EClient(EWrapper())
    plain_stub = StubConn()
    plain.conn = plain_stub
    plain.serverVersion_ = server_version
    plain.connState = EClient.CONNECTED
    plain.clientId = 21
    plain.optCapab = ""  # the data-only client sends none
    for ours, theirs in zip(_requests(client), _requests(plain), strict=True):
        ours()
        theirs()
    assert stub.sent == plain_stub.sent
    if server_version >= 210:
        assert len(stub.sent) == 10


# -- startApi -------------------------------------------------------------------


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_start_api_sends_version_client_id_and_empty_capabilities(server_version):
    # ibapi's startApi sends `2\0<clientId>\0<optionalCapabilities>\0`. The
    # capabilities field is a TWS feature-flag string (e.g. "+PACEAPI"); the
    # data-only client always sends it empty, however it was set.
    client, _ = make_client()
    stub = with_stub(client, server_version)
    client.clientId = 21
    client.setOptionalCapabilities("+PACEAPI")
    client.startApi()
    client.optCapab = "x"
    client.startApi()
    assert stub.sent == [_frame(server_version, OUT.START_API, "2\x0021\0\0")] * 2


# -- the port argument -----------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {OPT_IN: "1"}])
@pytest.mark.parametrize("port", ["7497", "4002", "7496", "007497", 7497.0, True])
def test_connect_refuses_a_port_that_is_not_an_exact_int(connect_recorder, monkeypatch, port, env):  # noqa: F811
    spy: list[Any] = []
    monkeypatch.setattr(client_module, "check_broker_port", lambda *a: spy.append(a))
    client, _ = make_client(env)
    with pytest.raises(BrokerPortRefused):
        client.connect("127.0.0.1", port, 21)  # type: ignore[arg-type]
    assert spy == []  # refused before the guard
    assert connect_recorder == []  # and before ibapi


# -- consumer failures -----------------------------------------------------------


def _capturing_logger(name: str) -> tuple[logging.Logger, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(name)
    logger.handlers = [Capture()]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, records


def test_first_failure_per_hook_is_kept_and_logged_once():
    logger, records = _capturing_logger("t115.first_failure")

    def bad_raw(*_a: Any) -> None:
        raise KeyError("raw-1")

    count = {"n": 0}

    def bad_event(_ev: Any) -> None:
        count["n"] += 1
        raise ValueError(f"event-{count['n']}")

    client = DataOnlyClient({}, bad_event, bad_raw, logger=logger)
    for i in range(3):
        client.nextValidId(i)
    assert client.callback_failures == 6
    assert set(client.first_failures) == {"on_raw", "on_event"}
    first = client.first_failures["on_event"]
    assert (first.exc_type, first.message) == ("ValueError", "event-1")
    assert "Traceback" in first.traceback and "bad_event" in first.traceback
    assert client.first_failures["on_raw"].exc_type == "KeyError"
    # one error record per hook, not per failure
    assert [r.levelno for r in records] == [logging.ERROR, logging.ERROR]
    text = "\n".join(r.getMessage() for r in records)
    assert "event-1" in text and "event-2" not in text and "raw-1" in text


def test_no_failure_means_nothing_recorded_or_logged():
    logger, records = _capturing_logger("t115.no_failure")
    client = DataOnlyClient({}, lambda ev: None, lambda n, a: None, logger=logger)
    client.currentTime(1)
    assert client.callback_failures == 0 and client.first_failures == {} and records == []


def test_default_logger_is_the_module_logger(caplog):
    def boom(*_a: Any) -> None:
        raise RuntimeError("consumer")

    client = DataOnlyClient({}, boom)
    with caplog.at_level(logging.ERROR, logger="MainStoinker.ib.client"):
        client.currentTime(1)
        client.currentTime(2)
    assert len([r for r in caplog.records if r.name == "MainStoinker.ib.client"]) == 1


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_still_propagate_and_are_not_recorded(exc):
    def stop(*_a: Any) -> None:
        raise exc

    client = DataOnlyClient({}, stop)
    with pytest.raises(exc):
        client.currentTime(1)
    assert client.first_failures == {}

    client = DataOnlyClient({}, lambda ev: None, stop)
    with pytest.raises(exc):
        client.currentTime(1)
    assert client.first_failures == {}


def test_message_of_a_refusal_names_the_id_and_out_name():
    client, _ = make_client()
    with_stub(client, 203)
    with pytest.raises(MessageRefused, match=r"3 \(PLACE_ORDER\)"):
        client.sendMsg(OUT.PLACE_ORDER, "x")
    with pytest.raises(MessageRefused, match=r"19 \(REPLACE_FA\)"):
        client.sendMsg(OUT.REPLACE_FA, "x")
    with pytest.raises(MessageRefused, match=r"203 \(PLACE_ORDER, protobuf form\)"):
        client.sendMsgProtoBuf(203, b"")
    with pytest.raises(MessageRefused, match=r"1 \(REQ_MKT_DATA\)"):
        client.sendMsgProtoBuf(OUT.REQ_MKT_DATA, b"")
