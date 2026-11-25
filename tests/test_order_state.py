"""Order state machine (`MainStoinker/engine/order_state.py`): pure, offline."""

from __future__ import annotations

import itertools
import random

import pytest

from MainStoinker.engine.order_state import (
    Ack,
    Applied,
    BracketState,
    Commission,
    ConnectionState,
    Execution,
    OrderLifecycle,
    OrderState,
    Rejected,
    Status,
    decode_order_ref,
    encode_order_ref,
)

REF = encode_order_ref("r1", "FunctionTest", "a1", "E")


def lifecycle(qty: int = 10, order_id: int = 1) -> OrderLifecycle:
    order = OrderLifecycle(REF, "BUY", qty)
    order.submit(order_id)
    return order


def ex(exec_id: str, shares: int, order_id: int = 1, price: float = 100.0) -> Execution:
    return Execution(order_id, exec_id, shares, price)


def run(events: list, qty: int = 10) -> OrderLifecycle:
    order = lifecycle(qty)
    for event in events:
        order.apply(event)
    return order


# ------------------------------------------------------------------ lifecycle transitions


def test_every_transition_to_each_terminal_state():
    order = OrderLifecycle(REF, "BUY", 10)
    assert order.state is OrderState.INTENT
    order.submit(1)
    assert order.state is OrderState.PENDING_SUBMIT
    order.apply(Ack(1))
    assert order.state is OrderState.WORKING
    order.apply(ex("e1", 4))
    assert order.state is OrderState.PARTIALLY_FILLED
    order.apply(ex("e2", 6))
    assert order.state is OrderState.FILLED and order.is_terminal

    for events, expected in (
        ([Status(1, "Submitted"), Status(1, "Cancelled")], OrderState.CANCELLED),
        ([Status(1, "ApiCancelled")], OrderState.CANCELLED),
        ([Status(1, "Submitted"), Rejected(1, 201, "no")], OrderState.REJECTED),
        ([Status(1, "Inactive")], OrderState.INACTIVE),
        ([Status(1, "PreSubmitted")], OrderState.WORKING),
        ([ex("e1", 3), Status(1, "Cancelled")], OrderState.CANCELLED),
    ):
        assert run(events).state is expected, events


def test_illegal_transitions_have_no_effect():
    done = run([Status(1, "Cancelled")])
    assert done.apply(Status(1, "Submitted")) is Applied.LATE
    assert done.apply(Ack(1)) is Applied.LATE
    assert done.apply(Rejected(1)) is Applied.LATE  # a second, contradictory terminal
    assert done.apply(Status(1, "Inactive")) is Applied.LATE
    assert done.state is OrderState.CANCELLED

    # a working status cannot take a partly filled order back to working
    part = run([ex("e1", 4)])
    part.apply(Status(1, "Submitted"))
    assert part.state is OrderState.PARTIALLY_FILLED

    # never submitted: intent stays intent whatever else is said about another id
    fresh = OrderLifecycle(REF, "BUY", 10)
    assert fresh.apply(Ack(99)) is Applied.IGNORED
    assert fresh.state is OrderState.INTENT


def test_events_for_another_order_ignored():
    order = lifecycle()
    assert order.apply(ex("e1", 5, order_id=2)) is Applied.IGNORED
    assert order.apply(Status(2, "Cancelled")) is Applied.IGNORED
    assert order.apply(ConnectionState("lost")) is Applied.IGNORED
    assert order.filled_qty == 0 and order.state is OrderState.PENDING_SUBMIT


def test_unknown_status_recorded_not_applied():
    order = lifecycle()
    assert order.apply(Status(1, "Weird")) is Applied.IGNORED
    assert order.anomalies and order.state is OrderState.PENDING_SUBMIT


def test_late_event_on_terminal_order_recorded_without_effect():
    order = run([Status(1, "Submitted"), Rejected(1, 201, "no")])
    assert order.apply(Status(1, "Submitted")) is Applied.LATE
    assert order.state is OrderState.REJECTED
    assert [o for _, o in order.log][-1] is Applied.LATE
    assert len(order.log) == 3  # recorded


# ------------------------------------------------------------------ the named acceptance tests

SCRIPT = [
    Ack(1), Status(1, "Submitted"), ex("e1", 4, price=100.0), ex("e2", 6, price=101.0),
    Commission("e1", 0.5), Commission("e2", 0.75), Status(1, "Filled", 10),
]


def test_all_permutations_of_fill_events_same_final_state():
    expected = None
    count = 0
    for perm in itertools.permutations(SCRIPT):
        order = run(list(perm))
        got = (order.state, order.filled_qty, order.avg_price, order.commission, order.overfilled)
        if expected is None:
            expected = got
        assert got == expected, perm
        count += 1
    assert count == 5040
    assert expected == (OrderState.FILLED, 10, pytest.approx(100.6), 1.25, False)


def test_permutations_with_cancel_race_same_final_state():
    script = [Ack(1), ex("e1", 4), Status(1, "Cancelled"), Commission("e1", 0.5)]
    seen = set()
    for perm in itertools.permutations(script):
        order = run(list(perm))
        seen.add((order.state, order.filled_qty, order.commission))
    assert seen == {(OrderState.CANCELLED, 4, 0.5)}


def test_cancel_racing_a_fill_to_completion():
    # the cancel is confirmed first, then the execution that beat it arrives: the fill wins
    order = run([Status(1, "Cancelled")])
    assert order.state is OrderState.CANCELLED
    assert order.apply(ex("e1", 10)) is Applied.LATE
    assert order.state is OrderState.FILLED and order.filled_qty == 10
    # and the other way round
    assert run([ex("e1", 10), Status(1, "Cancelled")]).state is OrderState.FILLED


def test_duplicate_events_idempotent():
    clean = run(SCRIPT)
    rng = random.Random(226)
    for _ in range(50):
        noisy_events = list(SCRIPT) + [rng.choice(SCRIPT) for _ in range(8)]
        rng.shuffle(noisy_events)
        noisy = run(noisy_events)
        assert (noisy.state, noisy.filled_qty, noisy.commission) == (
            clean.state, clean.filled_qty, clean.commission)


def test_duplicate_exec_id_counted_once():
    order = lifecycle()
    assert order.apply(ex("e1", 4)) is Applied.APPLIED
    assert order.apply(ex("e1", 4)) is Applied.DUPLICATE
    assert order.filled_qty == 4 and order.state is OrderState.PARTIALLY_FILLED
    assert order.apply(Commission("e1", 1.0)) is Applied.APPLIED
    assert order.apply(Commission("e1", 1.0)) is Applied.DUPLICATE
    assert order.commission == 1.0
    # same id, different content: first kept, noted
    assert order.apply(ex("e1", 9)) is Applied.DUPLICATE
    assert order.filled_qty == 4 and order.anomalies


def test_status_never_changes_quantity():
    order = lifecycle()
    order.apply(Status(1, "Filled", filled=10))
    order.apply(Status(1, "Submitted", filled=7))
    assert order.filled_qty == 0
    assert order.state is OrderState.WORKING
    assert order.fill_unconfirmed
    order.apply(ex("e1", 10))
    assert order.filled_qty == 10 and order.state is OrderState.FILLED
    assert not order.fill_unconfirmed


def test_partial_fills_sum():
    order = lifecycle(10)
    for i, shares in enumerate((1, 2, 3), start=1):
        order.apply(ex(f"e{i}", shares, price=10.0 * i))
    assert order.filled_qty == 6 and order.state is OrderState.PARTIALLY_FILLED
    assert order.avg_price == pytest.approx((10 + 40 + 90) / 6)
    order.apply(ex("e4", 4))
    assert order.state is OrderState.FILLED


def test_commission_before_execution_and_missing():
    order = lifecycle()
    order.apply(Commission("e1", 2.0))
    assert order.commission == 0.0  # execution not known yet
    order.apply(ex("e1", 10))
    assert order.commission == 2.0
    assert run([ex("e1", 10)]).commission == 0.0  # missing commission is zero, not an error


def test_overfill_on_one_order_is_flagged():
    order = run([ex("e1", 7), ex("e2", 7)])
    assert order.overfilled and order.state is OrderState.FILLED


# ------------------------------------------------------------------ orderRef codec


@pytest.mark.parametrize("role", ["E", "S", "T", "X"])
def test_order_ref_round_trip_and_length(role):
    text = encode_order_ref("20261124a", "FunctionTest", "17", role)
    assert text == f"stk1|20261124a|FunctionTest|17|{role}"
    assert len(text) <= 32
    parsed = decode_order_ref(text)
    assert (parsed.run, parsed.algo, parsed.ref, parsed.role) == ("20261124a", "FunctionTest", "17", role)
    assert encode_order_ref(parsed.run, parsed.algo, parsed.ref, parsed.role) == text  # restart safe


def test_order_ref_limits_and_refusals():
    assert len(encode_order_ref("a" * 10, "b" * 10, "c" * 3, "E")) == 32
    with pytest.raises(ValueError, match="limit"):
        encode_order_ref("a" * 10, "b" * 10, "c" * 4, "E")
    for bad in (("r|1", "A", "1", "E"), ("", "A", "1", "E"), ("r", "A", "1", "Q"), ("r", "A B", "1", "E")):
        with pytest.raises(ValueError):
            encode_order_ref(*bad)
    with pytest.raises(ValueError, match="account"):
        encode_order_ref("DU1234567", "A", "1", "E")
    for bad_text in ("", "hand placed", "stk2|r|A|1|E", "stk1|r|A|E", "stk1|r|A|1|Q", "stk1|r|A|1|E|x"):
        with pytest.raises(ValueError):
            decode_order_ref(bad_text)


# ------------------------------------------------------------------ bracket


def bracket(qty: int = 10, has_target: bool = True) -> BracketState:
    b = BracketState("r1", "FT", "a1", "BUY", qty, has_target=has_target)
    b.bind("E", 1)
    b.bind("S", 2)
    if has_target:
        b.bind("T", 3)
    return b


def test_bracket_refs_and_sides():
    b = bracket()
    assert [decode_order_ref(b.legs[r].order_ref).role for r in "EST"] == ["E", "S", "T"]
    assert b.legs["E"].side == "BUY" and b.legs["S"].side == "SELL" and b.legs["T"].side == "SELL"
    assert "T" not in bracket(has_target=False).legs


def test_parent_rejected_children_cancelled():
    b = bracket()
    for leg in (1, 2, 3):
        b.apply(Ack(leg))
    b.apply(Rejected(1, 201, "rejected"))
    assert b.cancel_requests() == ["S", "T"]
    b.mark_cancel_sent("S")
    assert b.cancel_requests() == ["T"]
    b.mark_cancel_sent("T")
    assert b.cancel_requests() == []
    b.apply(Status(2, "Cancelled"))
    b.apply(Status(3, "Cancelled"))
    assert b.entry.state is OrderState.REJECTED
    assert b.legs["S"].state is OrderState.CANCELLED and b.legs["T"].state is OrderState.CANCELLED
    assert b.is_closed and b.fatal is None and b.position_qty == 0


def test_parent_rejected_without_target():
    b = bracket(has_target=False)
    b.apply(Rejected(1))
    assert b.cancel_requests() == ["S"]


def test_partial_parent_fill_protects_filled_qty():
    b = bracket(10)
    assert b.resize_requests() == []
    b.apply(ex("e1", 4, order_id=1))
    assert b.desired_protection_qty == 4 and b.position_qty == 4
    assert sorted(b.resize_requests()) == [("S", 4), ("T", 4)]
    b.mark_resized("S", 4)
    b.mark_resized("T", 4)
    assert b.resize_requests() == []
    assert b.legs["S"].quantity == 4 and b.legs["T"].quantity == 4
    b.apply(ex("e2", 2, order_id=1))
    assert sorted(b.resize_requests()) == [("S", 6), ("T", 6)]
    # remainder of the entry cancelled: protection stays, sized to 6, nothing to cancel
    b.apply(Status(1, "Cancelled"))
    assert b.cancel_requests() == [] and b.desired_protection_qty == 6
    assert not b.unprotected and not b.is_closed


def test_stop_target_race_one_exit():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(ex("x1", 10, order_id=2))  # the stop fills
    assert b.terminal_exit == "S" and b.position_qty == 0
    assert b.cancel_requests() == ["T"]
    b.mark_cancel_sent("T")
    b.apply(Status(3, "Cancelled"))
    assert b.legs["T"].state is OrderState.CANCELLED
    assert b.terminal_exit == "S" and b.fatal is None and b.is_closed
    # the cancel is late on a target that filled first instead
    c = bracket()
    c.apply(ex("e1", 10, order_id=1))
    c.apply(ex("x1", 10, order_id=3))
    assert c.terminal_exit == "T" and c.cancel_requests() == ["S"]


def test_partial_exit_cancels_other_exit():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(ex("x1", 3, order_id=2))
    assert b.cancel_requests() == ["T"]
    assert b.terminal_exit is None  # the stop is only partly filled
    assert b.resize_requests() == []


def test_overfill_is_fatal():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    assert b.fatal is None
    b.apply(ex("x1", 10, order_id=2))
    b.apply(ex("x2", 10, order_id=3))  # both exits filled
    assert b.fatal and "overfill" in b.fatal
    first = b.fatal
    b.apply(Status(2, "Cancelled"))
    assert b.fatal == first  # sticky
    assert b.resize_requests() == []

    single = bracket()
    single.apply(ex("e1", 11, order_id=1))
    assert single.fatal and "leg E" in single.fatal

    # exits larger than what the entry filled
    short = bracket()
    short.apply(ex("e1", 4, order_id=1))
    short.apply(ex("x1", 5, order_id=2))
    assert short.fatal


def test_bracket_routing_commission_connection_duplicates():
    b = bracket()
    b.apply(Commission("e1", 1.0))  # before its execution
    assert b.apply(Commission("e1", 1.0)) is Applied.DUPLICATE
    b.apply(ex("e1", 10, order_id=1))
    assert b.entry.commission == 1.0
    assert b.apply(ex("e1", 10, order_id=1)) is Applied.DUPLICATE
    assert b.apply(ex("zz", 1, order_id=99)) is Applied.IGNORED
    b.apply(ConnectionState("lost"))
    assert b.connection == "lost"
    assert b.apply(Commission("e1", 1.0)) is Applied.DUPLICATE
    assert b.entry_filled_qty == 10


def test_unprotected_when_stop_gone_with_position():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(Status(2, "Cancelled"))
    assert b.unprotected
