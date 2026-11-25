"""Order state machine (`MainStoinker/engine/order_state.py`): pure, offline."""

from __future__ import annotations

import dataclasses
import itertools
import random
from decimal import Decimal

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


def lifecycle(qty: int = 10, order_id: int = 1, **kw) -> OrderLifecycle:
    order = OrderLifecycle(REF, "BUY", qty, **kw)
    order.submit(order_id)
    return order


def ex(exec_id: str, shares, order_id: int = 1, price: float = 100.0, **kw) -> Execution:
    return Execution(order_id, exec_id, shares, price, **kw)


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
    assert done.apply(Status(1, "Inactive")) is Applied.LATE
    assert done.state is OrderState.CANCELLED

    part = run([ex("e1", 4)])
    part.apply(Status(1, "Submitted"))
    assert part.state is OrderState.PARTIALLY_FILLED

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
    assert len(order.log) == 3


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
        got = (order.state, order.filled_qty, order.avg_price, order.commission, order.overfilled,
               order.anomalies)
        if expected is None:
            expected = got
        assert got == expected, perm
        count += 1
    assert count == 5040
    assert expected == (OrderState.FILLED, 10, pytest.approx(100.6), 1.25, False, ())


def test_permutations_with_cancel_race_same_final_state():
    script = [Ack(1), ex("e1", 4), Status(1, "Cancelled"), Commission("e1", 0.5)]
    seen = set()
    for perm in itertools.permutations(script):
        order = run(list(perm))
        seen.add((order.state, order.filled_qty, order.commission))
    assert seen == {(OrderState.CANCELLED, 4, 0.5)}


def test_cancel_racing_a_fill_to_completion():
    order = run([Status(1, "Cancelled")])
    assert order.state is OrderState.CANCELLED
    assert order.apply(ex("e1", 10)) is Applied.LATE
    assert order.state is OrderState.FILLED and order.filled_qty == 10
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
    # same id, different content: counted once, noted
    order.apply(ex("e1", 9))
    assert order.filled_qty in (4, 9)
    assert any("different content" in a for a in order.anomalies)


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
    assert order.commission == 0.0
    order.apply(ex("e1", 10))
    assert order.commission == 2.0
    assert run([ex("e1", 10)]).commission == 0.0


def test_overfill_on_one_order_is_flagged():
    order = run([ex("e1", 7), ex("e2", 7)])
    assert order.overfilled and order.state is OrderState.FILLED


# ------------------------------------------------------------------ B4 validation


@pytest.mark.parametrize("shares", [0, -1, 0.5, 2.5, True, False, float("nan"), float("inf"),
                                    Decimal("1.5"), Decimal("NaN"), "3", None])
def test_bad_shares_not_counted(shares):
    order = lifecycle()
    assert order.apply(ex("e1", shares)) is Applied.IGNORED
    assert order.filled_qty == 0 and order.anomalies


@pytest.mark.parametrize("price", [float("nan"), 0.0, -5.0, float("inf"), True, None])
def test_bad_price_not_counted(price):
    order = lifecycle()
    assert order.apply(ex("e1", 5, price=price)) is Applied.IGNORED
    assert order.filled_qty == 0 and order.avg_price is None and order.anomalies


def test_integral_float_and_decimal_shares_accepted():
    order = lifecycle()
    order.apply(ex("e1", 4.0))
    order.apply(ex("e2", Decimal("6")))
    assert order.filled_qty == 10 and order.state is OrderState.FILLED
    assert all(isinstance(e.shares, int) for e in order.executions.values())


def test_symbol_and_side_mismatch_not_counted():
    order = lifecycle(symbol="AAPL")
    assert order.apply(ex("e1", 5, symbol="MSFT")) is Applied.IGNORED
    assert order.apply(ex("e2", 5, side="SELL")) is Applied.IGNORED
    assert order.filled_qty == 0 and len(order.anomalies) == 2
    assert order.apply(ex("e3", 5, symbol="AAPL", side="BUY")) is Applied.APPLIED
    assert order.apply(ex("e4", 1)) is Applied.APPLIED  # unknown symbol/side on the event: accepted


def test_anomalies_are_a_sorted_tuple_independent_of_order():
    events = [ex("e1", 0), ex("e2", 0.5), Status(1, "Weird")]
    results = {run(list(p)).anomalies for p in itertools.permutations(events)}
    assert len(results) == 1
    only = next(iter(results))
    assert isinstance(only, tuple) and list(only) == sorted(only)


# ------------------------------------------------------------------ S1 / F2 statuses


def test_contradictory_terminal_statuses_deterministic_with_conflict():
    for first, second, winner in (
        (Status(1, "Cancelled"), Rejected(1), OrderState.REJECTED),
        (Rejected(1), Status(1, "Cancelled"), OrderState.REJECTED),
        (Status(1, "Inactive"), Status(1, "Cancelled"), OrderState.CANCELLED),
        (Status(1, "Cancelled"), Status(1, "Inactive"), OrderState.CANCELLED),
    ):
        order = run([first, second])
        assert order.state is winner
        assert any(a.startswith("status_conflict") for a in order.anomalies)
    assert run([Status(1, "Cancelled")]).anomalies == ()


def test_filled_by_executions_beats_a_terminal_status():
    order = run([Rejected(1), ex("e1", 10)])
    assert order.state is OrderState.FILLED


def test_inactive_is_not_final_and_order_independent():
    for events in ([Status(1, "Submitted"), Status(1, "Inactive")],
                   [Status(1, "Inactive"), Status(1, "Submitted")]):
        order = run(events)
        assert order.state is OrderState.INACTIVE and not order.is_terminal and not order.is_live
        assert "inactive_seen" in order.anomalies
    order.confirm_live(10)  # reconciliation: the open-orders snapshot shows it working
    assert order.state is OrderState.WORKING and order.is_live and order.anomalies == ()
    order.apply(Status(1, "Cancelled"))
    assert order.state is OrderState.CANCELLED
    with pytest.raises(ValueError):
        order.confirm_live(10)  # terminal


def test_inactive_then_terminal_status_clears_inactive_seen():
    order = run([Status(1, "Inactive"), Status(1, "Cancelled")])
    assert order.state is OrderState.CANCELLED
    assert "inactive_seen" not in order.anomalies


def test_quantity_conflict_takes_the_smallest_until_confirmed():
    for events in ([Ack(1, 10), Status(1, "Submitted", quantity=4)],
                   [Status(1, "Submitted", quantity=4), Ack(1, 10)]):
        order = run(events)
        assert order.quantity == 4 and any(a.startswith("quantity_conflict") for a in order.anomalies)
    order.confirm_live(10)
    assert order.quantity == 10 and order.anomalies == ()


# ------------------------------------------------------------------ F1 exec id corrections


def test_correction_replaces_original_full_fill():
    for events in ([ex("X.01", 10), ex("X.02", 10)], [ex("X.02", 10), ex("X.01", 10)]):
        order = run(events)
        assert order.filled_qty == 10 and not order.overfilled
        assert list(order.executions) == ["X.02"]


def test_correction_replaces_original_partial_fill():
    for events in itertools.permutations([ex("X", 4), ex("X.01", 4), ex("X.02", 3)]):
        order = run(list(events))
        assert order.filled_qty == 3 and order.state is OrderState.PARTIALLY_FILLED


def test_correction_before_original_and_commission_follows_winner():
    order = run([Commission("X", 1.0), Commission("X.01", 2.0), ex("X.01", 5), ex("X", 4)])
    assert order.filled_qty == 5 and order.commission == 2.0
    assert order.has_exec("X")  # a commission for the replaced id can still be routed


def test_distinct_bases_still_add():
    assert run([ex("A.01", 4), ex("B.01", 6)]).filled_qty == 10


# ------------------------------------------------------------------ orderRef codec


@pytest.mark.parametrize("role", ["E", "S", "T", "X"])
def test_order_ref_round_trip_and_length(role):
    text = encode_order_ref("20261124a", "FunctionTest", "17", role)
    assert text == f"stk1|20261124a|FunctionTest|17|{role}"
    assert len(text) <= 32
    parsed = decode_order_ref(text)
    assert (parsed.run, parsed.algo, parsed.ref, parsed.role) == ("20261124a", "FunctionTest", "17", role)
    assert encode_order_ref(parsed.run, parsed.algo, parsed.ref, parsed.role) == text


def test_order_ref_limits_and_refusals():
    assert len(encode_order_ref("a" * 10, "b" * 10, "c" * 3, "E")) == 32
    assert len(encode_order_ref("17", "OpeningRangeCandle", "123", "E")) <= 32
    with pytest.raises(ValueError, match="limit"):
        encode_order_ref("a" * 10, "b" * 10, "c" * 4, "E")
    for bad in (("r|1", "A", "1", "E"), ("", "A", "1", "E"), ("r", "A", "1", "Q"), ("r", "A B", "1", "E")):
        with pytest.raises(ValueError):
            encode_order_ref(*bad)
    for bad_text in ("", "hand placed", "stk2|r|A|1|E", "stk1|r|A|E", "stk1|r|A|1|Q", "stk1|r|A|1|E|x"):
        with pytest.raises(ValueError):
            decode_order_ref(bad_text)


@pytest.mark.parametrize("name", ["U1234567", "du1234567", "DUX123456", "DU12345", "abc12345"])
def test_order_ref_refuses_account_shapes(name):
    with pytest.raises(ValueError, match="account"):
        encode_order_ref("r", name, "1", "E")
    with pytest.raises(ValueError, match="account"):
        encode_order_ref(name, "A", "1", "E")


@pytest.mark.parametrize("name", ["DIA1", "EMA_DI5", "LONG_DF1", "DUO1234", "AB1234", "20261124a"])
def test_order_ref_accepts_algo_like_names(name):
    assert decode_order_ref(encode_order_ref("r", name, "1", "E")).algo == name


# ------------------------------------------------------------------ bracket


def bracket(qty: int = 10, has_target: bool = True) -> BracketState:
    b = BracketState("r1", "FT", "a1", "BUY", qty, has_target=has_target)
    b.bind("E", 1)
    b.bind("S", 2)
    if has_target:
        b.bind("T", 3)
    return b


def view(b: BracketState) -> tuple:
    return dataclasses.astuple(b.snapshot())


def run_bracket(events: list, **kw) -> BracketState:
    b = bracket(**kw)
    for event in events:
        b.apply(event)
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
    assert b.pending_cancels == ("S", "T")
    assert b.cancel_requests() == ["S", "T"]
    b.mark_cancel_sent("S")
    assert b.cancel_requests() == ["T"]
    b.mark_cancel_sent("T")
    assert b.cancel_requests() == []
    assert b.pending_cancels == ("S", "T")  # until IB confirms
    b.apply(Status(2, "Cancelled"))
    b.apply(Status(3, "Cancelled"))
    assert b.pending_cancels == ()
    assert b.entry.state is OrderState.REJECTED
    assert b.legs["S"].state is OrderState.CANCELLED and b.legs["T"].state is OrderState.CANCELLED
    assert b.is_closed and b.fatal is None and b.held == 0


def test_parent_rejected_without_target():
    b = bracket(has_target=False)
    b.apply(Rejected(1))
    assert b.cancel_requests() == ["S"]


def test_partial_parent_fill_protects_filled_qty():
    b = bracket(10)
    assert b.resize_requests() == []
    b.apply(ex("e1", 4, order_id=1))
    assert b.held == 4
    assert b.pending_resizes == (("S", 4), ("T", 4))
    b.mark_resized("S", 4)
    b.mark_resized("T", 4)
    assert b.resize_requests() == []
    assert b.pending_resizes == (("S", 4), ("T", 4))  # not applied until IB confirms
    assert b.legs["S"].quantity == 10
    b.apply(Status(2, "Submitted", quantity=4))
    b.apply(Ack(3, quantity=4))
    assert b.pending_resizes == () and b.legs["S"].quantity == 4 and b.legs["T"].quantity == 4
    b.apply(ex("e2", 2, order_id=1))
    assert b.resize_requests() == [("S", 6), ("T", 6)]
    b.apply(Status(1, "Cancelled"))
    assert b.cancel_requests() == [] and b.held == 6
    assert not b.is_closed


def test_stop_target_race_one_exit():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(ex("x1", 10, order_id=2))
    assert b.terminal_exit == "S" and b.held == 0
    assert b.cancel_requests() == ["T"]
    b.mark_cancel_sent("T")
    b.apply(Status(3, "Cancelled"))
    assert b.legs["T"].state is OrderState.CANCELLED
    assert b.terminal_exit == "S" and b.fatal is None and b.is_closed
    c = bracket()
    c.apply(ex("e1", 10, order_id=1))
    c.apply(ex("x1", 10, order_id=3))
    assert c.terminal_exit == "T" and c.cancel_requests() == ["S"]


def test_partial_exit_resizes_other_exit_instead_of_cancelling_it():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(ex("x1", 1, order_id=3))  # the target fills 1
    assert b.held == 9
    assert b.cancel_requests() == []
    assert b.resize_requests() == [("S", 9)]  # the target's remainder (10 - 1) already equals held
    assert not b.unprotected and b.terminal_exit is None


def test_entry_fills_after_partial_exit_resizes_up():
    b = bracket()
    b.apply(ex("e1", 5, order_id=1))
    b.apply(Status(2, "Submitted", quantity=5))
    b.apply(Status(3, "Submitted", quantity=5))
    b.apply(ex("x1", 2, order_id=2))  # stop fills 2 of 5
    assert b.held == 3
    b.apply(ex("e2", 5, order_id=1))
    assert b.held == 8
    assert b.unprotected  # the stop has 3 left against 8 held
    assert ("S", 10) in b.pending_resizes  # 2 filled + 8 held
    assert b.cancel_requests() == []


def test_exit_complete_for_held_cancels_other_and_open_entry():
    b = bracket()
    b.apply(ex("e1", 4, order_id=1))
    b.apply(Status(2, "Submitted", quantity=4))
    b.apply(ex("x1", 4, order_id=2))  # the stop closes the whole held quantity
    assert b.held == 0 and b.terminal_exit == "S"
    assert b.pending_cancels == ("E", "T")


def test_overfill_is_fatal():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    assert b.fatal is None
    b.apply(ex("x1", 10, order_id=2))
    b.apply(ex("x2", 10, order_id=3))  # both exits filled
    assert b.fatal and "overfill" in b.fatal
    b.apply(Status(2, "Cancelled"))
    assert b.fatal
    assert b.resize_requests() == []

    single = bracket()
    single.apply(ex("e1", 11, order_id=1))
    assert single.fatal and "leg E" in single.fatal


def test_overfill_latches_even_if_a_correction_clears_it():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(ex("X", 12, order_id=2))
    assert b.fatal
    b.apply(ex("X.01", 10, order_id=2))
    assert b.fatal and "latched" in b.fatal


def test_b1_exit_before_entry_is_not_fatal_in_any_order():
    events = [ex("e1", 10, order_id=1), ex("s1", 10, order_id=2)]
    for perm in itertools.permutations(events):
        b = bracket()
        for event in perm:
            b.apply(event)
        assert b.fatal is None and b.held == 0 and not b.exit_ahead_of_entry
    b = bracket()
    b.apply(ex("s1", 10, order_id=2))
    assert b.fatal is None and b.exit_ahead_of_entry
    assert b.cancel_requests() == []  # nothing is cancelled on a transient
    b.apply(ex("e1", 4, order_id=1))
    assert b.exit_ahead_of_entry and b.fatal is None
    b.apply(ex("e2", 6, order_id=1))
    assert not b.exit_ahead_of_entry and b.fatal is None


def test_b1_exit_ahead_does_not_stop_resize_requests():
    b = bracket()
    b.apply(ex("e1", 5, order_id=1))
    b.apply(ex("x1", 6, order_id=3))  # target ahead of the entry's reported fills
    assert b.exit_ahead_of_entry and b.fatal is None
    b.apply(ex("e2", 5, order_id=1))
    assert b.held == 4 and b.fatal is None
    assert b.resize_requests() == [("S", 4)]


def test_b3_cancelled_entry_that_later_fills_is_unprotected():
    b = bracket()
    b.apply(Status(1, "Cancelled"))
    assert b.cancel_requests() == ["S", "T"]
    b.mark_cancel_sent("S")
    b.mark_cancel_sent("T")
    b.apply(ex("e1", 10, order_id=1))
    assert b.entry.state is OrderState.FILLED and b.held == 10
    assert b.unprotected and b.awaiting_stop_resolution and b.needs_protection == 0
    b.apply(Status(2, "Cancelled"))  # the old stop is gone: now new protection is wanted
    assert b.unprotected and not b.awaiting_stop_resolution and b.needs_protection == 10


def test_b3_unprotected_is_derived_from_the_stop():
    b = bracket()
    assert not b.unprotected and b.needs_protection == 0
    b.apply(ex("e1", 10, order_id=1))
    assert not b.unprotected and b.needs_protection == 0
    b.apply(Status(2, "Cancelled"))
    assert b.unprotected and b.needs_protection == 10
    # a stop with less remaining than held is unprotected but live: resize, not new protection
    c = bracket()
    c.apply(ex("e1", 4, order_id=1))
    c.apply(Status(2, "Submitted", quantity=4))
    c.apply(ex("e2", 6, order_id=1))
    assert c.unprotected and c.needs_protection == 0
    assert ("S", 10) in c.resize_requests()


def test_f2_inactive_stop_is_unprotected_but_still_cancelled():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(Status(2, "Submitted"))
    assert not b.unprotected
    b.apply(Status(2, "Inactive"))
    assert b.unprotected and b.awaiting_stop_resolution and b.needs_protection == 0
    b.apply(ex("x1", 10, order_id=3))  # the target closes the position
    assert b.pending_cancels == ("S",)  # the Inactive stop still gets cancelled
    b.apply(Status(2, "Submitted"))  # a later Submitted does not make it count as live again
    assert not b.legs["S"].is_live and b.pending_cancels == ("S",)
    assert any("inactive_seen" in a for a in b.anomalies)


def test_n4_inactive_either_order_same_snapshot():
    snaps = set()
    for events in ([Status(2, "Submitted"), Status(2, "Inactive")],
                   [Status(2, "Inactive"), Status(2, "Submitted")]):
        b = bracket()
        b.apply(ex("e1", 10, order_id=1))
        for e in events:
            b.apply(e)
        assert b.unprotected and b.awaiting_stop_resolution
        snaps.add(view(b))
    assert len(snaps) == 1
    b.confirm_live("S", 10)
    assert not b.unprotected and not b.awaiting_stop_resolution and b.anomalies == ()


def test_n4_stale_quantity_report_is_conservative_both_ways():
    for events in ([Status(2, "Submitted", quantity=10), Status(2, "Submitted", quantity=4)],
                   [Status(2, "Submitted", quantity=4), Status(2, "Submitted", quantity=10)]):
        b = bracket()
        b.apply(ex("e1", 10, order_id=1))
        for e in events:
            b.apply(e)
        assert b.legs["S"].quantity == 4
        assert b.unprotected and b.pending_resizes[0] == ("S", 10)
        assert any("quantity_conflict" in a for a in b.anomalies)
    b.confirm_live("S", 10)
    assert not b.unprotected and b.pending_resizes == () and b.anomalies == ()


# ------------------------------------------------------------------ N1 uncertain legs, N2 short, N3 two stops


def test_n1_rejected_with_request_outstanding_is_uncertain():
    b = bracket()
    b.apply(ex("e1", 4, order_id=1))
    b.apply(Status(2, "Submitted"))
    b.mark_resized("S", 4)
    b.apply(Rejected(2, 201, "Order rejected"))
    assert b.uncertain == ("S",) and b.unprotected and b.awaiting_stop_resolution
    assert b.legs["S"].state is OrderState.WORKING  # not interpreted: neither rejected nor protected
    assert b.needs_protection == 0
    assert ("S", 4) in b.resize_requests()  # the request is back
    assert any(a.startswith("request_or_leg_rejected") for a in b.anomalies)
    b.confirm_live("S", 4)
    assert b.uncertain == () and not b.unprotected and b.anomalies == ()


def test_n1_ack_and_rejected_either_order_same_snapshot():
    snaps = set()
    for events in ([Ack(2), Rejected(2, 1, "x")], [Rejected(2, 1, "x"), Ack(2)]):
        b = bracket()
        b.apply(ex("e1", 10, order_id=1))
        b.mark_cancel_sent("S")
        for e in events:
            b.apply(e)
        assert b.uncertain == ("S",) and b.unprotected
        assert b.legs["S"].state is OrderState.WORKING
        snaps.add(view(b))
    assert len(snaps) == 1


def test_n1_rejected_without_request_is_a_real_rejection():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.apply(Rejected(2, 1, "no"))
    assert b.legs["S"].state is OrderState.REJECTED and b.uncertain == ()
    assert b.unprotected and b.needs_protection == 10


def test_n1_terminal_status_ends_uncertainty():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    b.mark_cancel_sent("S")
    b.apply(Rejected(2, 1, "x"))
    assert b.uncertain == ("S",)
    b.apply(Status(2, "Cancelled"))
    assert b.uncertain == () and b.needs_protection == 10
    assert not any("request_or_leg_rejected" in a for a in b.anomalies)


def test_n2_short_by_when_entry_finished():
    b = bracket()
    b.apply(ex("e1", 4, order_id=1))
    b.apply(Status(1, "Cancelled"))
    b.apply(ex("x1", 10, order_id=2))
    b.apply(Status(3, "Cancelled"))
    assert b.held == 0 and b.short_by == 6 and b.fatal is None
    s = b.snapshot()
    assert s.exits_exceed_entry and s.short_by == 6 and not s.is_closed and not b.is_closed
    assert not b.exit_ahead_of_entry
    b.apply(ex("e2", 6, order_id=1))  # a late entry execution clears it
    assert b.short_by == 0 and b.entry.state is OrderState.FILLED and b.is_closed


def test_n2_entry_live_is_only_exit_ahead():
    b = bracket()
    b.apply(ex("x1", 3, order_id=2))
    assert b.exit_ahead_of_entry and b.short_by == 0 and not b.snapshot().exits_exceed_entry


def test_n3_two_stops_scenario():
    b = bracket()
    b.apply(Ack(2))
    b.apply(Status(1, "Cancelled"))
    b.mark_cancel_sent("S")
    b.mark_cancel_sent("T")
    b.apply(ex("e1", 10, order_id=1))  # a late entry fill
    assert b.unprotected and b.awaiting_stop_resolution and b.needs_protection == 0
    b.apply(Rejected(2, 161, "x"))  # the old stop's cancel was refused (or it was rejected)
    assert b.legs["S"].state is OrderState.WORKING
    assert b.unprotected and b.awaiting_stop_resolution and b.needs_protection == 0
    b.apply(Status(2, "Cancelled"))
    assert b.needs_protection == 10 and not b.awaiting_stop_resolution


# ------------------------------------------------------------------ S2 input validation and requests


def test_bind_and_mark_validate():
    b = BracketState("r1", "FT", "a1", "BUY", 10)
    b.bind("E", 1)
    b.bind("S", 2)
    with pytest.raises(ValueError):
        b.bind("T", 2)  # id already bound
    with pytest.raises(ValueError):
        b.bind("S", 5)  # leg already bound
    with pytest.raises(ValueError):
        b.bind("Z", 9)
    b.bind("T", 3)
    b.apply(ex("e1", 4, order_id=1))
    for role in ("E", "Z", "X"):
        with pytest.raises(ValueError):
            b.mark_resized(role, 3)
    with pytest.raises(ValueError):
        b.mark_resized("S", 50)
    with pytest.raises(ValueError):
        b.mark_resized("S", 0)
    with pytest.raises(ValueError):
        b.mark_cancel_sent("Z")
    b.mark_resized("S", 4)
    assert b.entry.quantity == 10 and b.entry.state is OrderState.PARTIALLY_FILLED

def test_unknown_order_id_execution_is_an_anomaly():
    b = bracket()
    assert b.apply(ex("zz", 1, order_id=99)) is Applied.IGNORED
    assert any("unknown order id 99" in a for a in b.anomalies)
    assert b.entry_filled_qty == 0
    b.apply(Status(99, "Cancelled"))  # a hand-placed order's status is not an anomaly
    assert len(b.anomalies) == 1


def test_bad_execution_in_bracket_is_visible():
    b = bracket()
    b.apply(ex("e1", 0.5, order_id=1))
    assert b.entry_filled_qty == 0 and any("leg E" in a for a in b.anomalies)


def test_bracket_routing_commission_connection_duplicates():
    b = bracket()
    b.apply(Commission("e1", 1.0))
    assert b.apply(Commission("e1", 1.0)) is Applied.DUPLICATE
    b.apply(ex("e1", 10, order_id=1))
    assert b.entry.commission == 1.0
    assert b.apply(ex("e1", 10, order_id=1)) is Applied.DUPLICATE
    b.apply(ConnectionState("lost"))
    assert b.connection == "lost"
    assert b.apply(Commission("e1", 1.0)) is Applied.DUPLICATE
    assert b.entry_filled_qty == 10


# ------------------------------------------------------------------ F4 snapshots, permutations


def test_snapshots_are_frozen_and_state_is_read_only():
    b = bracket()
    b.apply(ex("e1", 10, order_id=1))
    snap = b.snapshot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.held = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.legs[0][1].filled_qty = 0  # type: ignore[misc]
    with pytest.raises(TypeError):
        b.legs["X"] = b.entry  # type: ignore[index]
    with pytest.raises(TypeError):
        b.entry.executions["z"] = ex("z", 1)  # type: ignore[index]
    assert isinstance(b.entry.log, tuple)
    assert snap.held == 10 and snap.legs[0][1].filled_qty == 10


STOP_OUT = [
    Ack(1), ex("e1", 6, order_id=1), ex("e2", 4, order_id=1), Status(1, "Filled", 10),
    Ack(2), ex("s1", 10, order_id=2), Status(3, "Cancelled"), Commission("e1", 0.5),
]
PARTIAL = [
    ex("e1", 4, order_id=1), Status(1, "Cancelled"), Ack(2), Ack(3), ex("t1", 1, order_id=3),
    Commission("t1", 0.25),
]
CANCEL_THEN_FILL = [
    Status(1, "Cancelled"), ex("e1", 10, order_id=1), Ack(2, quantity=10), Status(2, "Cancelled")]


@pytest.mark.parametrize(
    "script", [STOP_OUT, PARTIAL, CANCEL_THEN_FILL], ids=["stop_out", "partial", "cancel_fill"])
def test_all_permutations_of_bracket_events_same_final_state(script):
    expected = None
    count = 0
    for perm in itertools.permutations(script):
        got = view(run_bracket(list(perm)))
        if expected is None:
            expected = got
        assert got == expected, perm
        count += 1
    assert count > 0
    snap = dict(zip((f.name for f in dataclasses.fields(type(run_bracket([]).snapshot()))), expected,
                    strict=True))
    if script is STOP_OUT:
        assert (snap["held"], snap["fatal"], snap["unprotected"], snap["is_closed"],
                snap["terminal_exit"]) == (0, None, False, True, "S")
        assert snap["pending_cancels"] == () and snap["pending_resizes"] == ()
        assert not snap["exit_ahead_of_entry"]
    elif script is PARTIAL:
        assert (snap["held"], snap["fatal"], snap["unprotected"], snap["is_closed"]) == (
            3, None, False, False)
        assert snap["pending_cancels"] == () and snap["pending_resizes"] == (("S", 3), ("T", 4))
    else:
        assert (snap["held"], snap["fatal"], snap["unprotected"], snap["needs_protection"]) == (
            10, None, True, 10)


# ------------------------------------------------------------------ seeded random brackets (N1-N4)


def _random_bracket_case(rng: random.Random):
    """(entry fill, caller marks, events): marks are caller actions, applied before the shuffled events."""
    filled = rng.choice([0, 3, 5, 10])
    marks = []
    if rng.random() < 0.4:
        marks.append(("cancel", rng.choice("ST")))
    if filled and rng.random() < 0.4:
        marks.append(("resize", "S", rng.randint(1, filled)))
    counter = itertools.count(1)
    events: list = []
    for _ in range(rng.randint(3, 8)):
        kind = rng.choice(["ack", "status", "status", "entry", "exit", "reject", "cancel_entry", "comm"])
        leg = rng.choice([2, 3])
        qty = rng.choice([None, None, 4, 6, 10])
        if kind == "ack":
            events.append(Ack(rng.choice([1, 2, 3]), qty))
        elif kind == "status":
            name = rng.choice(["Submitted", "Inactive", "PreSubmitted", "Cancelled", "Filled"])
            events.append(Status(leg, name, quantity=qty))
        elif kind == "entry":
            events.append(ex(f"e{next(counter)}", rng.randint(1, 5), order_id=1))
        elif kind == "exit":
            events.append(ex(f"x{next(counter)}", rng.randint(1, 4), order_id=leg))
        elif kind == "reject":
            events.append(Rejected(rng.choice([1, 2, 3]), rng.randint(1, 300)))
        elif kind == "cancel_entry":
            events.append(Status(1, rng.choice(["Cancelled", "Submitted"])))
        else:
            events.append(Commission(f"e{rng.randint(1, 4)}", 1.0))
    events += rng.sample(events, k=min(2, len(events)))  # duplicates
    return filled, marks, events


def _build(filled: int, marks: list, events: list) -> BracketState:
    b = bracket()
    if filled:
        b.apply(ex("e0", filled, order_id=1))
    for mark in marks:
        if mark[0] == "cancel":
            b.mark_cancel_sent(mark[1])
        else:
            b.mark_resized(mark[1], mark[2])
    for event in events:
        b.apply(event)
    return b


def _check_invariants(b: BracketState, marks: list) -> None:
    cancel_sent = {m[1] for m in marks if m[0] == "cancel"}
    s = b.snapshot()
    for role, quantity in s.pending_resizes:
        assert quantity - b.legs[role].filled_qty <= s.held  # requested protection never exceeds held
    stop = b.legs["S"]
    certain = (stop.is_live and "S" not in cancel_sent and "S" not in s.uncertain
               and stop.remaining >= s.held)
    if s.held > 0 and not certain:
        assert s.unprotected
    if s.is_closed:
        assert s.held == 0 and s.short_by == 0
    if s.needs_protection:
        assert not s.awaiting_stop_resolution and not stop.is_live


def test_random_brackets_order_independent_and_invariants():
    rng = random.Random(2261)
    for _ in range(1200):
        filled, marks, events = _random_bracket_case(rng)
        base = _build(filled, marks, events)
        _check_invariants(base, marks)
        expected = view(base)
        for _ in range(4):
            shuffled = list(events)
            rng.shuffle(shuffled)
            other = _build(filled, marks, shuffled)
            assert view(other) == expected, (filled, marks, events, shuffled)
