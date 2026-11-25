"""Tests of ``tests/fake_ib_orders.py``: the scripted exchange for single orders."""
import ast
import inspect
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.execution import ExecutionFilter
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from tests.fake_ib import Delivery, FakeIbClient
from tests.fake_ib_orders import (
    DUPLICATE_ORDER_ID,
    ORDER_IMPLEMENTED,
    REJECT_CODES,
    SYNTHETIC_ACCOUNT,
    FakeOrderIbClient,
    FillPlan,
    OrderRecorder,
)


def stock(symbol: str = "AAPL") -> Contract:
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = symbol, "STK", "SMART", "USD"
    return c


def order(action: str = "BUY", qty: int = 10, kind: str = "MKT", **kw: Any) -> Order:
    o = Order()
    o.action, o.orderType, o.totalQuantity = action, kind, Decimal(qty)
    o.account = SYNTHETIC_ACCOUNT
    o.orderRef = "stoink-test"
    for k, v in kw.items():
        setattr(o, k, v)
    return o


@pytest.fixture
def fake():
    with FakeOrderIbClient(OrderRecorder(), next_valid_id=500) as f:
        f.connect("127.0.0.1", 7497, 24)
        f.drain()
        f.mark = len(f.delivered)  # type: ignore[attr-defined]
        yield f


def new(f: FakeOrderIbClient) -> list[Delivery]:
    """Deliver what is queued; return what arrived since the last call."""
    f.drain()
    out = f.delivered[f.mark:]  # type: ignore[attr-defined]
    f.mark = len(f.delivered)  # type: ignore[attr-defined]
    return out


def names(ds: list[Delivery]) -> list[str]:
    return [d.name for d in ds]


def only(ds: list[Delivery], name: str) -> list[tuple[Any, ...]]:
    return [d.args for d in ds if d.name == name]


# -- the acceptance tests ----------------------------------------------------


@pytest.mark.parametrize("name", ORDER_IMPLEMENTED)
def test_signatures_match_eclient(name: str) -> None:
    assert inspect.signature(getattr(FakeOrderIbClient, name)) == inspect.signature(
        getattr(EClient, name)
    )


def test_signatures_match_eclient_and_base_has_no_order_method() -> None:
    # The sibling adds the order methods; the data-only fake keeps having none.
    assert all(not hasattr(FakeIbClient, n) for n in ORDER_IMPLEMENTED)
    assert all(hasattr(FakeOrderIbClient, n) for n in ORDER_IMPLEMENTED)


def test_market_order_lifecycle_long_and_short(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order("BUY"))
    got = new(fake)
    assert names(got) == [
        "openOrder", "orderStatus", "openOrder", "orderStatus",
        "execDetails", "orderStatus", "commissionAndFeesReport",
    ]  # fmt: skip
    statuses = [a[1] for a in only(got, "orderStatus")]
    assert statuses == ["PreSubmitted", "Submitted", "Filled"]
    ex = only(got, "execDetails")[0][2]
    assert (ex.side, ex.shares, ex.price, ex.orderId, ex.acctNumber) == (
        "BOT", Decimal(10), 100.0, 500, SYNTHETIC_ACCOUNT,
    )  # fmt: skip
    last = only(got, "orderStatus")[-1]
    assert (last[2], last[3], last[4]) == (Decimal(10), Decimal(0), 100.0)

    fake.reqPositions()
    pos = only(new(fake), "position")
    assert [(p[0], p[2]) for p in pos] == [(SYNTHETIC_ACCOUNT, Decimal(10))]

    fake.placeOrder(501, stock(), order("SELL"))
    got = new(fake)
    assert only(got, "execDetails")[0][2].side == "SLD"
    fake.reqPositions()
    assert only(new(fake), "position") == []  # flat

    fake.placeOrder(502, stock(), order("SELL", 4))  # short from flat
    new(fake)
    fake.reqPositions()
    assert [p[2] for p in only(new(fake), "position")] == [Decimal(-4)]


def test_partial_fills_sum_by_exec_id(fake: FakeOrderIbClient) -> None:
    fake.next_plan(FillPlan(fills=((3, 100.0), (3, 101.0), (4, 102.0))))
    fake.placeOrder(500, stock(), order(qty=10))
    got = new(fake)
    by_exec = {a[2].execId: a[2].shares for a in only(got, "execDetails")}
    assert len(by_exec) == 3 and sum(by_exec.values()) == Decimal(10)
    cum = [a[2] for a in only(got, "orderStatus")][2:]  # after PreSubmitted, Submitted
    assert cum == [Decimal(3), Decimal(6), Decimal(10)]
    final = only(got, "orderStatus")[-1]
    assert final[1] == "Filled" and final[3] == Decimal(0)
    assert final[4] == pytest.approx((3 * 100 + 3 * 101 + 4 * 102) / 10)
    assert len(only(got, "commissionAndFeesReport")) == 3


def test_duplicate_and_out_of_order_callbacks_are_scriptable(fake: FakeOrderIbClient) -> None:
    def scramble(played: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        execs = [p for p in played if p[0] == "execDetails"]
        rest = [p for p in played if p[0] != "execDetails"]
        # status first, then the execution twice, then the commission before all of it
        commission = [p for p in rest if p[0] == "commissionAndFeesReport"]
        status = [p for p in rest if p[0] != "commissionAndFeesReport"]
        return commission + status[::-1] + execs + execs

    fake.next_plan(FillPlan(transform=scramble))
    fake.placeOrder(500, stock(), order())
    got = new(fake)
    n = names(got)
    assert n.index("commissionAndFeesReport") < n.index("execDetails")
    assert n.index("orderStatus") < n.index("execDetails")  # Filled before its execution
    ids = [a[2].execId for a in only(got, "execDetails")]
    assert len(ids) == 2 and ids[0] == ids[1]  # a duplicate, the same execId


def test_commission_late_and_missing(fake: FakeOrderIbClient) -> None:
    fake.next_plan(FillPlan(commission=2.5, commission_delay=2))
    fake.placeOrder(500, stock(), order())
    got = new(fake)
    assert "execDetails" in names(got) and "commissionAndFeesReport" not in names(got)
    fake.tick()
    assert new(fake) == []
    fake.tick()
    late = only(new(fake), "commissionAndFeesReport")
    assert [(r[0].commissionAndFees, r[0].currency) for r in late] == [(2.5, "USD")]
    assert late[0][0].execId == only(got, "execDetails")[0][2].execId

    fake.next_plan(FillPlan(commission=None))  # IB never reports one
    fake.placeOrder(501, stock(), order())
    got = new(fake)
    fake.tick(10)
    fake.release_held()
    assert "commissionAndFeesReport" not in names(got) + names(new(fake))
    fake.reqExecutions(9, ExecutionFilter())
    again = new(fake)
    assert len(only(again, "execDetails")) == 2
    assert len(only(again, "commissionAndFeesReport")) == 1  # only the late one is known


@pytest.mark.parametrize("on", ["place", "cancel"])
@pytest.mark.parametrize("code", REJECT_CODES)
def test_reject_codes_carry_order_id(fake: FakeOrderIbClient, code: int, on: str) -> None:
    if on == "place":
        fake.script_error("place", code)
        fake.placeOrder(500, stock(), order())
    else:
        fake.placeOrder(500, stock(), order(kind="LMT", lmtPrice=90.0))
        new(fake)
        fake.script_error("cancel", code)
        fake.cancelOrder(500, OrderCancel())
    errors = only(new(fake), "error")
    assert errors, f"no error delivered for {code}"
    req_id, _t, got_code, text, _json = errors[0]
    assert (req_id, got_code) == (500, code)
    assert text and "{id}" not in text


def test_reject_201_makes_the_order_inactive_and_warning_399_lets_it_go_on(
    fake: FakeOrderIbClient,
) -> None:
    fake.script_error("place", 201)
    fake.placeOrder(500, stock(), order())
    got = new(fake)
    assert names(got) == ["error", "orderStatus"] and got[1].args[1] == "Inactive"
    assert fake.status_of(500) == "Inactive"
    fake.script_error("place", 399)
    fake.placeOrder(501, stock(), order())
    got = new(fake)
    assert names(got)[0] == "error" and fake.status_of(501) == "Filled"


def test_cancel_unknown_or_filled_order_is_an_error_with_the_id(fake: FakeOrderIbClient) -> None:
    fake.cancelOrder(777, OrderCancel())
    assert only(new(fake), "error")[0][:3:2] == (777, 10147)
    fake.placeOrder(500, stock(), order())
    new(fake)
    fake.cancelOrder(500, OrderCancel())
    assert only(new(fake), "error")[0][:3:2] == (500, 161)


def test_whatif_creates_no_order(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order(whatIf=True))
    got = new(fake)
    assert names(got) == ["openOrder"]  # no orderStatus, no execution
    state = got[0].args[3]
    assert state.initMarginChange == "1500.0" and state.commissionAndFees == 1.0
    assert fake.order_ids() == [] and fake.whatif_requests == [500]
    fake.reqAllOpenOrders()
    fake.reqCompletedOrders(False)
    fake.reqExecutions(1, ExecutionFilter())
    fake.reqPositions()
    got = new(fake)
    assert names(got) == [
        "openOrderEnd", "completedOrdersEnd", "execDetailsEnd", "positionEnd",
    ]  # fmt: skip
    # the id was not consumed
    fake.placeOrder(500, stock(), order())
    assert DUPLICATE_ORDER_ID not in [a[2] for a in only(new(fake), "error")]


def test_reused_order_id_is_error_103(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order())
    new(fake)
    fake.placeOrder(500, stock(), order("SELL", 3))
    got = new(fake)
    assert names(got) == ["error"]
    assert got[0].args[0] == 500 and got[0].args[2] == 103
    assert fake.order_ids() == [500]
    fake.reqPositions()
    assert [p[2] for p in only(new(fake), "position")] == [Decimal(10)]  # the second did nothing
    # a rejected order's id is used too
    fake.script_error("place", 110)
    fake.placeOrder(501, stock(), order())
    new(fake)
    fake.placeOrder(501, stock(), order())
    assert only(new(fake), "error")[0][2] == 103


def test_fake_never_imports_ibapi_connection() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(FakeOrderIbClient)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all("connection" not in a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "connection" not in (node.module or "")
            assert all(a.name != "connection" for a in node.names)


# -- the rest of the behaviour -----------------------------------------------


def test_order_callbacks_reach_run_on_its_thread() -> None:
    rec = OrderRecorder()
    with FakeOrderIbClient(rec) as f:
        f.connect("127.0.0.1", 7497, 24)
        f.placeOrder(1, stock(), order())
        t = f.run_in_thread()
        got = []
        while True:
            name, _args, tid = rec.get()
            got.append((name, tid))
            if name == "commissionAndFeesReport":
                break
        assert {tid for _, tid in got if _ != "connectAck"} == {t.ident}
        assert t.ident != threading.get_ident()
        f.drop("connectionClosed")
        t.join(5)


def test_not_connected_answers_504_with_the_order_id() -> None:
    f = FakeOrderIbClient(OrderRecorder())
    f.placeOrder(42, stock(), order())
    f.reqPositions()
    assert [(d.args[0], d.args[2]) for d in f.delivered] == [(42, 504), (-1, 504)]
    assert f.order_ids() == []


def test_resting_limit_order_fills_or_is_cancelled_later(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order(kind="LMT", lmtPrice=99.0))
    got = new(fake)
    assert fake.status_of(500) == "Submitted" and "execDetails" not in names(got)
    fake.fill(500, 4, 99.0)
    got = new(fake)
    assert only(got, "orderStatus")[-1][1] == "Submitted" and only(got, "orderStatus")[-1][3] == Decimal(6)
    fake.exchange_cancel(500)
    assert only(new(fake), "orderStatus")[0][1] == "Cancelled"

    fake.placeOrder(501, stock(), order(kind="STP", auxPrice=95.0))
    new(fake)
    fake.cancelOrder(501, OrderCancel())
    got = new(fake)
    assert [a[1] for a in only(got, "orderStatus")] == ["PendingCancel", "Cancelled"]
    assert only(got, "error")[0][:3:2] == (501, 202)


def test_final_plans_cancelled_and_inactive(fake: FakeOrderIbClient) -> None:
    fake.next_plan(FillPlan(fills=((4, 100.0),), final="Cancelled"))
    fake.placeOrder(500, stock(), order())
    assert only(new(fake), "orderStatus")[-1][1:4] == ("Cancelled", Decimal(4), Decimal(6))
    fake.next_plan(FillPlan(fills=(), final="Inactive"))
    fake.placeOrder(501, stock(), order())
    got = new(fake)
    assert [a[1] for a in only(got, "orderStatus")] == ["Inactive"]
    with pytest.raises(ValueError):
        fake.next_plan(FillPlan(final="Bogus"))
    fake.next_plan(FillPlan(fills=((4, 100.0),)))  # Filled, but short of 10
    with pytest.raises(ValueError):
        fake.placeOrder(502, stock(), order())


def test_open_orders_own_versus_all_and_completed(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order(kind="LMT", lmtPrice=90.0))  # ours, resting
    fake.placeOrder(501, stock(), order())  # ours, filled
    fake.seed_open_order(stock("MSFT"), order(kind="LMT", lmtPrice=1.0), order_id=900, client_id=0)
    new(fake)
    fake.reqOpenOrders()
    got = new(fake)
    assert [a[0] for a in only(got, "openOrder")] == [500] and names(got)[-1] == "openOrderEnd"
    fake.reqAllOpenOrders()
    got = new(fake)
    assert [a[0] for a in only(got, "openOrder")] == [500, 900] and names(got)[-1] == "openOrderEnd"
    fake.reqCompletedOrders(True)
    got = new(fake)
    assert [a[1].orderRef for a in only(got, "completedOrder")] == ["stoink-test"]
    assert only(got, "completedOrder")[0][1].totalQuantity == Decimal(10)
    assert only(got, "completedOrder")[0][2].completedStatus == "Filled"
    assert names(got)[-1] == "completedOrdersEnd"
    assert fake.allocate_id() == 901  # ids above every id seen


def test_executions_positions_and_filters(fake: FakeOrderIbClient) -> None:
    fake.seed_position(stock("MSFT"), 5, 300.0)
    fake.placeOrder(500, stock("AAPL"), order())
    fake.placeOrder(501, stock("TSLA"), order("SELL", 2))
    new(fake)
    flt = ExecutionFilter()
    flt.symbol = "TSLA"
    fake.reqExecutions(7, flt)
    got = new(fake)
    assert names(got) == ["execDetails", "commissionAndFeesReport", "execDetailsEnd"]
    assert got[0].args[0] == 7 and got[2].args == (7,)
    fake.reqPositions()
    got = new(fake)
    assert sorted((p[1].symbol, p[2]) for p in only(got, "position")) == [
        ("AAPL", Decimal(10)), ("MSFT", Decimal(5)), ("TSLA", Decimal(-2)),
    ]  # fmt: skip
    assert names(got)[-1] == "positionEnd"
    fake.cancelPositions()
    assert fake.calls_named("cancelPositions")


def test_execution_correction_is_same_order_new_exec_id(fake: FakeOrderIbClient) -> None:
    fake.placeOrder(500, stock(), order())
    first = only(new(fake), "execDetails")[0][2]
    new_id = fake.correct_execution(first.execId, price=100.5)
    got = new(fake)
    corrected = only(got, "execDetails")[0][2]
    assert corrected.execId == new_id != first.execId
    assert (corrected.orderId, corrected.permId, corrected.price) == (500, first.permId, 100.5)
    assert [r[0].execId for r in only(got, "commissionAndFeesReport")] == [new_id]


def test_order_callback_arguments_are_checked_against_ewrapper(fake: FakeOrderIbClient) -> None:
    with pytest.raises(TypeError):
        fake.push("orderStatus", 1, "Filled")
    with pytest.raises(AttributeError):
        fake.push("noSuchCallback")
