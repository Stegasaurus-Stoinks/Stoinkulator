"""Gaps in `tests/test_ib_client.py`: the wire, the thresholds, the callbacks.

Same rules as there: nothing connects, requests go to a stub connection that
records the frame it was given.
"""

import enum
import struct
import threading
from decimal import Decimal
from typing import Any

import pytest
from ibapi import message as ibapi_message
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID, UNSET_DECIMAL, BarData, TickAttrib
from ibapi.message import OUT
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from MainStoinker.ib import events
from MainStoinker.ib.client import REFUSED_MESSAGE_IDS, DataOnlyClient, OrderPathRefused
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.test_ib_client import (
    OPT_IN,
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
        if msg_id in REFUSED_MESSAGE_IDS:
            with pytest.raises(OrderPathRefused):
                client.sendMsg(msg_id, "1\0")
            with pytest.raises(OrderPathRefused):
                client.sendMsgProtoBuf(msg_id, b"")
            assert len(stub.sent) == before, msg_id
            continue
        client.sendMsg(msg_id, "1\0")
        client.sendMsgProtoBuf(msg_id, b"")
        assert len(stub.sent) == before + 2
        assert _wire_id(stub.sent[-2], server_version, proto=False) == msg_id
        assert _wire_id(stub.sent[-1], server_version, proto=True) == msg_id


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", sorted(REFUSED_MESSAGE_IDS))
def test_refusal_by_keyword_arguments_writes_nothing(msg_id, server_version):
    client, _ = make_client()
    stub = with_stub(client, server_version)
    with pytest.raises(OrderPathRefused):
        client.sendMsg(msgId=msg_id, msg="x")
    with pytest.raises(OrderPathRefused):
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
    with pytest.raises(OrderPathRefused):
        client.sendMsg(bad_id, "x")
    with pytest.raises(OrderPathRefused):
        client.sendMsgProtoBuf(bad_id, b"x")
    assert stub.sent == []


@pytest.mark.parametrize("bad_id", [-3, -203, 2**32 + 3, 2**32 + 203, 2**40])
def test_out_of_range_ids_never_become_an_order_frame_on_a_modern_server(bad_id):
    client, _ = make_client()
    stub = with_stub(client, 203)
    for send in (lambda: client.sendMsg(bad_id, "x"), lambda: client.sendMsgProtoBuf(bad_id, b"x")):
        try:
            send()
        except (OrderPathRefused, OverflowError):
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
        with pytest.raises(OrderPathRefused):
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
        except OrderPathRefused:
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


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(
            strict=True, reason=f"refused set is a denylist of orders only: {why}"))
        for n, why in sorted(STATE_CHANGING.items())
        if n in {"REPLACE_FA", "REQ_AUTO_OPEN_ORDERS", "UPDATE_DISPLAY_GROUP"}
    ],
)
def test_account_or_tws_state_changing_messages_are_refused(name):
    client, _ = make_client()
    stub = with_stub(client, 203)
    with pytest.raises(OrderPathRefused):
        client.sendMsg(getattr(OUT, name), "x")
    assert stub.sent == []


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
    assert REFUSED_MESSAGE_IDS == {3, 4, 21, 58, 203, 204, 221, 258}


def test_stub_is_the_only_thing_written_to():
    # Guards the helpers themselves: the stub records whole frames only.
    client, _ = make_client()
    stub = with_stub(client, 203)
    assert isinstance(client.conn, StubConn)
    client.reqCurrentTime()
    assert all(isinstance(f, bytes) for f in stub.sent) and len(stub.sent) == 1
