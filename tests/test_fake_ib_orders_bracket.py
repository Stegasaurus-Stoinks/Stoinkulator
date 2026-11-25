"""Tests of the bracket, OCA, outage and re-sync parts of ``tests/fake_ib_orders.py`` (#231).

Every IB behaviour these tests pin is the fake's model of it and is unverified against real IB
(see the scenario table in ``tests/fake_ib_orders.py``); the tests pin the model, not IB.
"""
from collections import namedtuple
from decimal import Decimal
from typing import Any

import pytest
from ibapi.contract import Contract
from ibapi.execution import ExecutionFilter
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from tests.fake_ib import Delivery
from tests.fake_ib_orders import SYNTHETIC_ACCOUNT, FakeOrderIbClient, FillPlan, OrderRecorder

Bracket = namedtuple("Bracket", "parent target stop")


def stock(symbol: str = "AAPL") -> Contract:
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = symbol, "STK", "SMART", "USD"
    return c


def order(action: str, qty: int, kind: str, **kw: Any) -> Order:
    o = Order()
    o.action, o.orderType, o.totalQuantity = action, kind, Decimal(qty)
    o.account = SYNTHETIC_ACCOUNT
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
    f.drain()
    out = f.delivered[f.mark:]  # type: ignore[attr-defined]
    f.mark = len(f.delivered)  # type: ignore[attr-defined]
    return out


def statuses(ds: list[Delivery]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for d in ds:
        if d.name == "orderStatus":
            out.setdefault(d.args[0], []).append(d.args[1])
    return out


def error_codes(ds: list[Delivery]) -> list[int]:
    return [d.args[2] for d in ds if d.name == "error"]


def place_bracket(
    f: FakeOrderIbClient, *, qty: int = 10, oca: bool = True, oca_type: int = 1
) -> Bracket:
    """BUY parent (resting LMT) with a target and a stop; the last child transmits the group."""
    pid = f.allocate_id()
    group = f"oca-{pid}" if oca else ""
    f.placeOrder(pid, stock(), order("BUY", qty, "LMT", lmtPrice=100.0, transmit=False))
    f.placeOrder(pid + 1, stock(), order("SELL", qty, "LMT", lmtPrice=105.0, parentId=pid,
                                         ocaGroup=group, ocaType=oca_type if oca else 0,
                                         transmit=False))
    f.placeOrder(pid + 2, stock(), order("SELL", qty, "STP", auxPrice=95.0, parentId=pid,
                                         ocaGroup=group, ocaType=oca_type if oca else 0,
                                         transmit=True))
    f.allocate_id()
    f.allocate_id()
    return Bracket(pid, pid + 1, pid + 2)


def positions(f: FakeOrderIbClient) -> dict[str, Decimal]:
    f.reqPositions()
    return {d.args[1].symbol: d.args[2] for d in new(f) if d.name == "position"}


def exec_ids(ds: list[Delivery], name: str = "execDetails") -> list[str]:
    return [d.args[2].execId for d in ds if d.name == name]


# -- the acceptance tests ----------------------------------------------------


def test_bracket_held_until_transmit(fake: FakeOrderIbClient) -> None:
    pid = fake.allocate_id()
    fake.placeOrder(pid, stock(), order("BUY", 10, "LMT", lmtPrice=100.0, transmit=False))
    fake.placeOrder(pid + 1, stock(), order("SELL", 10, "LMT", lmtPrice=105.0, parentId=pid, transmit=False))
    # Nothing works until the transmit=True order arrives: no callback, no open order.
    assert new(fake) == []
    assert fake.untransmitted_ids() == [pid, pid + 1]
    fake.reqOpenOrders()
    assert [d.name for d in new(fake)] == ["openOrderEnd"]
    fake.cancelOrder(pid, OrderCancel())  # an untransmitted order is not known to TWS (unverified)
    assert error_codes(new(fake)) == [10147]

    fake.placeOrder(pid + 2, stock(), order("SELL", 10, "STP", auxPrice=95.0, parentId=pid, transmit=True))
    got = statuses(new(fake))
    assert fake.untransmitted_ids() == []
    assert got[pid] == ["PreSubmitted", "Submitted"]  # the parent works at once
    assert got[pid + 1] == ["PreSubmitted"] and got[pid + 2] == ["PreSubmitted"]  # children wait
    now = [fake.status_of(i) for i in (pid, pid + 1, pid + 2)]
    assert now == ["Submitted", "PreSubmitted", "PreSubmitted"]
    with pytest.raises(ValueError, match="held until its parent"):
        fake.fill(pid + 1, 10, 105.0)

    fake.fill(pid, 10, 100.0)  # the parent fills: the children go live
    got = statuses(new(fake))
    assert got[pid + 1] == ["Submitted"] and got[pid + 2] == ["Submitted"]
    assert got[pid] == ["Filled"]


@pytest.mark.parametrize("how", ["error_201", "plan_inactive", "error_110", "exchange_cancel", "api_cancel"])
def test_parent_reject_cancels_children(fake: FakeOrderIbClient, how: str) -> None:
    if how == "error_201":
        fake.script_error("place", 201)
    elif how == "error_110":
        fake.script_error("place", 110)
    elif how == "plan_inactive":
        fake.next_plan(FillPlan(fills=(), final="Inactive"))
    b = place_bracket(fake)
    got = new(fake)
    if how in ("exchange_cancel", "api_cancel"):  # accepted, then cancelled with no fill
        assert fake.status_of(b.parent) == "Submitted"
        if how == "exchange_cancel":
            fake.exchange_cancel(b.parent)
        else:
            fake.cancelOrder(b.parent, OrderCancel())
        got = new(fake)
    assert statuses(got)[b.target][-1] == "Cancelled"
    assert statuses(got)[b.stop][-1] == "Cancelled"
    assert fake.status_of(b.target) == "Cancelled" and fake.status_of(b.stop) == "Cancelled"
    if how == "error_201":
        assert 201 in error_codes(got) and fake.status_of(b.parent) == "Inactive"
    if how == "error_110":  # the parent never existed, its children are cancelled anyway
        assert b.parent not in fake.order_ids()
    fake.reqOpenOrders()
    assert [d.name for d in new(fake)] == ["openOrderEnd"]
    assert positions(fake) == {}


@pytest.mark.parametrize(("policy", "child_size"), [("keep", Decimal(10)), ("filled", Decimal(4))])
def test_partial_parent_remainder_cancelled_child_size_configurable(
    fake: FakeOrderIbClient, policy: str, child_size: Decimal
) -> None:
    fake.child_size_on_partial_cancel = policy
    fake.next_plan(FillPlan(fills=((4, 100.0),), final="Cancelled"))
    b = place_bracket(fake)
    got = new(fake)
    # Four shares filled, six cancelled: the children went live, not cancelled.
    assert statuses(got)[b.parent][-1] == "Cancelled"
    assert fake.status_of(b.target) == "Submitted" and fake.status_of(b.stop) == "Submitted"
    fake.reqOpenOrders()
    sizes = {d.args[0]: d.args[2].totalQuantity for d in new(fake) if d.name == "openOrder"}
    assert sizes == {b.target: child_size, b.stop: child_size}
    assert positions(fake) == {"AAPL": Decimal(4)}


def test_oca_sibling_cancelled(fake: FakeOrderIbClient) -> None:
    b = place_bracket(fake)
    fake.fill(b.parent, 10, 100.0)
    new(fake)
    fake.fill(b.target, 10, 105.0)
    got = statuses(new(fake))
    assert got[b.target] == ["Filled"]
    assert got[b.stop] == ["Cancelled"]
    assert fake.status_of(b.stop) == "Cancelled"
    with pytest.raises(ValueError, match="late_fill"):
        fake.fill(b.stop, 10, 95.0)
    assert positions(fake) == {}  # flat: the cancelled stop cannot fill


@pytest.mark.parametrize("implicit", [False, True])
def test_children_without_oca_group_cancel_each_other_only_if_scripted(
    fake: FakeOrderIbClient, implicit: bool
) -> None:
    # Unverified in IB: whether bracket children cancel each other with no ocaGroup. The fake
    # does not decide: ``implicit_bracket_oca`` says (default off).
    fake.implicit_bracket_oca = implicit
    b = place_bracket(fake, oca=False)
    fake.fill(b.parent, 10, 100.0)
    fake.fill(b.target, 10, 105.0)
    new(fake)
    assert fake.status_of(b.stop) == ("Cancelled" if implicit else "Submitted")


def test_oca_type_2_and_3_reduce_the_sibling_instead_of_cancelling(fake: FakeOrderIbClient) -> None:
    for oca_type in (2, 3):
        b = place_bracket(fake, oca_type=oca_type)
        fake.fill(b.parent, 10, 100.0)
        new(fake)
        fake.fill(b.target, 4, 105.0)  # a partial fill must not cancel the stop
        got = new(fake)
        assert fake.status_of(b.stop) == "Submitted"
        assert statuses(got)[b.stop] == ["Submitted"]
        fake.reqOpenOrders()
        sizes = {d.args[0]: d.args[2].totalQuantity for d in new(fake) if d.name == "openOrder"}
        assert sizes[b.stop] == Decimal(6)  # reduced by the 4 filled
        fake.fill(b.target, 6, 105.0)  # the rest: nothing is left for the stop
        new(fake)
        assert fake.status_of(b.target) == "Filled" and fake.status_of(b.stop) == "Cancelled"


def test_oca_type_1_cancels_on_a_partial_fill_and_other_types_are_refused(
    fake: FakeOrderIbClient,
) -> None:
    b = place_bracket(fake, oca_type=1)
    fake.fill(b.parent, 10, 100.0)
    fake.fill(b.target, 4, 105.0)
    assert fake.status_of(b.stop) == "Cancelled"
    pid = fake.allocate_id()
    with pytest.raises(ValueError, match="ocaType 0"):
        fake.placeOrder(pid, stock(), order("SELL", 1, "LMT", lmtPrice=1.0, ocaGroup="g", ocaType=0))
    assert pid not in fake.order_ids()


def test_stop_target_race_overfill_scriptable(fake: FakeOrderIbClient) -> None:
    b = place_bracket(fake)
    fake.fill(b.parent, 10, 100.0)
    new(fake)
    fake.fill_race([(b.target, 10, 105.0), (b.stop, 10, 95.0)])
    got = new(fake)
    assert statuses(got)[b.target] == ["Filled"] and statuses(got)[b.stop] == ["Filled"]
    assert "Cancelled" not in [s for v in statuses(got).values() for s in v]
    assert len(exec_ids(got)) == 2
    assert positions(fake) == {"AAPL": Decimal(-10)}  # long 10, sold 20: the overfill
    # The same pair played one after the other is not an overfill: the second is cancelled.
    c = place_bracket(fake)
    fake.fill(c.parent, 10, 100.0)
    fake.fill(c.target, 10, 105.0)
    assert fake.status_of(c.stop) == "Cancelled"
    # ...unless the script says the stop's fill crossed the cancel.
    fake.fill(c.stop, 10, 95.0, late_fill=True)
    assert positions(fake) == {"AAPL": Decimal(-20)}


def test_fill_during_outage_seen_on_resync(fake: FakeOrderIbClient) -> None:
    b = place_bracket(fake)
    fake.fill(b.parent, 10, 100.0)
    seen = new(fake)
    entry = exec_ids(seen)
    assert len(entry) == 1

    fake.begin_outage()
    fake.fill(b.target, 10, 105.0)  # executes while the API hears nothing
    during = new(fake)
    assert error_codes(during) == [1100]
    assert [d.name for d in during] == ["error"]
    assert {item[0] for item in fake.lost_callbacks} >= {"execDetails", "orderStatus"}
    fake.end_outage(1101)  # data lost: nothing is replayed
    assert error_codes(new(fake)) == [1101]

    # Re-sync: the open orders, the executions and the positions tell what happened.
    fake.reqOpenOrders()
    assert [d.name for d in new(fake)] == ["openOrderEnd"]  # target filled, stop OCA-cancelled
    fake.reqExecutions(31, ExecutionFilter())
    synced = new(fake)
    ids = exec_ids(synced)
    assert len(ids) == 2 and ids[0] == entry[0]  # the missed exit is there
    assert [d.args[0].execId for d in synced if d.name == "commissionAndFeesReport"] == ids
    assert positions(fake) == {}  # flat again


@pytest.mark.parametrize("replay", [False, True])
def test_outage_1102_replays_only_when_scripted(fake: FakeOrderIbClient, replay: bool) -> None:
    b = place_bracket(fake)
    fake.fill(b.parent, 10, 100.0)
    new(fake)
    fake.begin_outage()
    fake.fill(b.stop, 10, 95.0)
    assert error_codes(new(fake)) == [1100]
    fake.end_outage(1102, replay=replay)
    got = new(fake)
    assert error_codes(got) == [1102]
    if replay:
        assert statuses(got)[b.stop] == ["Filled"] and len(exec_ids(got)) == 1
    else:  # the default: lost for good, only a re-sync shows it
        assert statuses(got) == {} and exec_ids(got) == []
        fake.reqExecutions(50, ExecutionFilter())
        assert len(exec_ids(new(fake))) == 2
    with pytest.raises(ValueError, match="1101 or 1102"):
        fake.end_outage(1100)
    with pytest.raises(ValueError, match="replay needs 1102"):
        fake.end_outage(1101, replay=True)


def test_fill_while_socket_closed_seen_on_resync(fake: FakeOrderIbClient) -> None:
    b = place_bracket(fake)
    fake.fill(b.parent, 10, 100.0)
    new(fake)
    fake.drop("connectionClosed")
    assert not fake.isConnected()
    fake.fill(b.target, 10, 105.0)  # nobody is connected
    assert new(fake) == []
    fake.reqOpenOrders()  # the client's calls answer 504 while down
    assert error_codes(new(fake)) == [504]
    fake.disconnect()
    new(fake)
    fake.connect("127.0.0.1", 7497, 24)
    assert "execDetails" not in [d.name for d in new(fake)]  # the lost fill is not delivered late
    fake.reqExecutions(32, ExecutionFilter())
    assert len(exec_ids(new(fake))) == 2
    assert positions(fake) == {}
    fake.reqCompletedOrders(False)
    done = [d.args[2].completedStatus for d in new(fake) if d.name == "completedOrder"]
    assert sorted(done) == ["Cancelled", "Filled", "Filled"]


def test_resync_replays_known_exec_ids(fake: FakeOrderIbClient) -> None:
    fake.next_plan(FillPlan(fills=((4, 100.0), (6, 101.0))))
    pid = fake.allocate_id()
    fake.placeOrder(pid, stock(), order("BUY", 10, "MKT"))
    seen = new(fake)
    known = exec_ids(seen)
    assert len(known) == 2

    fake.reqExecutions(40, ExecutionFilter())
    first = new(fake)
    assert exec_ids(first) == known  # every execution again, same ids, same order
    assert [d.args[0] for d in first if d.name == "execDetails"] == [40, 40]
    assert [d.args[0].execId for d in first if d.name == "commissionAndFeesReport"] == known
    assert first[-1].name == "execDetailsEnd"
    fake.reqExecutions(41, ExecutionFilter())
    assert exec_ids(new(fake)) == known  # and again: a re-sync is repeatable

    # A fill the client missed adds one id to the replay; the known ones stay.
    b = place_bracket(fake, qty=5)
    fake.begin_outage()
    fake.fill(b.parent, 5, 100.0)
    fake.end_outage(1101)
    new(fake)
    fake.reqExecutions(42, ExecutionFilter())
    again = exec_ids(new(fake))
    assert again[:2] == known and len(again) == 3 and again[2] not in known
    assert positions(fake) == {"AAPL": Decimal(15)}


def test_child_of_unknown_parent_is_refused_loudly(fake: FakeOrderIbClient) -> None:
    with pytest.raises(ValueError, match="parentId 999"):
        fake.placeOrder(fake.allocate_id(), stock(), order("SELL", 1, "STP", auxPrice=1.0, parentId=999))


def test_cancelling_a_held_child_leaves_the_rest(fake: FakeOrderIbClient) -> None:
    b = place_bracket(fake, oca=False)
    fake.cancelOrder(b.stop, OrderCancel())
    new(fake)
    fake.fill(b.parent, 10, 100.0)
    assert fake.status_of(b.stop) == "Cancelled" and fake.status_of(b.target) == "Submitted"


def test_filled_policy_never_leaves_an_impossible_state(fake: FakeOrderIbClient) -> None:
    # The review's probe: no OCA group, parent 4 of 10, target fills 4, stop fills 3, then the
    # parent's rest is cancelled.
    fake.child_size_on_partial_cancel = "filled"
    fake.next_plan(FillPlan(fills=((4, 100.0),), final="Submitted"))
    b = place_bracket(fake, oca=False)
    fake.fill(b.target, 4, 105.0)
    fake.fill(b.stop, 3, 95.0)
    new(fake)
    fake.cancelOrder(b.parent, OrderCancel())
    got = new(fake)
    assert fake.status_of(b.target) == "Filled"  # already done: untouched
    assert fake.status_of(b.stop) == "Submitted"  # sized 4, 3 filled, 1 left
    last = [d.args for d in got if d.name == "orderStatus" and d.args[0] == b.stop][-1]
    assert (last[2], last[3]) == (Decimal(3), Decimal(1))

    # A child that already filled the new size ends Filled and leaves the open orders.
    fake.next_plan(FillPlan(fills=((4, 100.0),), final="Submitted"))
    c = place_bracket(fake, oca=False)
    fake.fill(c.stop, 4, 95.0)
    new(fake)
    fake.exchange_cancel(c.parent)
    assert fake.status_of(c.stop) == "Filled"
    last = [d.args for d in new(fake) if d.name == "orderStatus" and d.args[0] == c.stop][-1]
    assert last[1:4] == ("Filled", Decimal(4), Decimal(0))
    fake.reqOpenOrders()
    open_ids = {d.args[0] for d in new(fake) if d.name == "openOrder"}
    assert c.stop not in open_ids and c.target in open_ids and b.stop in open_ids

    # More filled than the parent: the size clamps to the fill, never below it.
    fake.next_plan(FillPlan(fills=((2, 100.0),), final="Submitted"))
    d_ = place_bracket(fake, oca=False)
    fake.fill(d_.stop, 5, 95.0)
    new(fake)
    fake.exchange_cancel(d_.parent)
    assert fake.status_of(d_.stop) == "Filled"
    last = [d.args for d in new(fake) if d.name == "orderStatus" and d.args[0] == d_.stop][-1]
    assert last[3] == Decimal(0)


def test_earlier_open_order_callbacks_are_not_rewritten(fake: FakeOrderIbClient) -> None:
    fake.child_size_on_partial_cancel = "filled"
    fake.next_plan(FillPlan(fills=((4, 100.0),), final="Cancelled"))
    b = place_bracket(fake)
    fake.drain()
    opens = [d.args for d in fake.delivered if d.name == "openOrder" and d.args[0] == b.target]
    sizes = [a[2].totalQuantity for a in opens]
    # PreSubmitted at 10, then live at 10 (the first fill), then resized to the 4 filled.
    assert sizes == [Decimal(10), Decimal(10), Decimal(4)]
    assert [a[3].status for a in opens] == ["PreSubmitted", "Submitted", "Submitted"]
