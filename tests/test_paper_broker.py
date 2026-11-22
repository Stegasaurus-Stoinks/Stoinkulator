"""PaperBroker fill model, costs and ledger reporting, on synthetic bars (no legacy code except Trade)."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal

import pandas as pd
import pytest

from MainStoinker.engine.broker import Broker
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import COLUMNS, Bar
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.router import BarAwareBroker, MappingRouter
from MainStoinker.engine.sink import NullSink
from MainStoinker.TradeTools.trade import Trade

T0 = pd.Timestamp("2024-01-03 09:30:00")
FREE = CostModel(Decimal(0), Decimal(0), Decimal(0))
CAPITAL = 10_000


def t(minute: int, day: int = 0) -> pd.Timestamp:
    return T0 + pd.Timedelta(days=day) + pd.Timedelta(minutes=minute)


def bar(minute: int, o: float, h: float | None = None, low: float | None = None,
        c: float | None = None, day: int = 0) -> Bar:
    stamp = t(minute, day)
    return Bar(stamp, int(stamp.timestamp()), o, o if h is None else h,
               o if low is None else low, o if c is None else c, 100)


def make(costs: CostModel = FREE, symbol: str = "AAPL") -> tuple[Ledger, PaperBroker]:
    ledger = Ledger()
    return ledger, PaperBroker(ledger.open_account("a", symbol, CAPITAL), costs)


def open_pos(broker: PaperBroker, *, long: bool = True, stop: float = 95.0, volume: float = 10,
             symbol: str = "AAPL", minute: int = 0):
    return broker.open_position(symbol=symbol, volume=volume, direction=1 if long else 0,
                                price=100.0, time=t(minute), limit=False, stop_price=stop,
                                oca_group="g")


def opened(broker: PaperBroker, *, long: bool = True, stop: float = 95.0, tp: float | None = None,
           fill_bar: Bar | None = None):
    """Open a position and feed the fill bar (open 100 unless given); returns the ref."""
    ref = open_pos(broker, long=long, stop=stop)
    if tp is not None:
        broker.set_take_profit(ref, tp, 10)
    broker.on_bar("AAPL", fill_bar or bar(1, 100.0, 100.5, 99.5))
    return ref


def reasons(ledger: Ledger) -> list[str]:
    return [f.reason for f in ledger.fills]


def test_paper_broker_satisfies_broker_and_bar_aware_protocols() -> None:
    _, broker = make()
    assert isinstance(broker, Broker)
    assert isinstance(broker, BarAwareBroker)


def test_entry_fills_at_next_open() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    assert broker.position_state(ref) == "pending"
    assert ledger.fills == ()
    broker.on_bar("AAPL", bar(1, 101.0, 102.0, 100.5, 101.5))
    (fill,) = ledger.fills
    assert (fill.reason, fill.side, fill.price, fill.quantity) == ("entry", "buy", 101, 10)
    assert fill.time == t(1)
    assert broker.position_state(ref) == "open"


def test_short_entry_sells_at_next_open() -> None:
    ledger, broker = make()
    ref = open_pos(broker, long=False, stop=105.0)
    broker.on_bar("AAPL", bar(1, 99.0))
    (fill,) = ledger.fills
    assert (fill.side, fill.price) == ("sell", 99)
    assert broker.position_state(ref) == "open"


def test_entry_at_session_end_is_cancelled_and_stop_not_working() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    assert broker.stop_is_working(ref)
    broker.on_bar("AAPL", bar(0, 100.0, day=1))
    assert broker.position_state(ref) == "cancelled"
    assert not broker.stop_is_working(ref)
    assert broker.cancelled_entries == 1
    assert ledger.fills == ()
    assert [(e.kind, e.order_type) for e in ledger.orders] == [
        ("placed", "entry"), ("placed", "stop"), ("cancelled", None), ("cancelled", None)]
    assert all(e.time == t(0, 1) for e in ledger.orders if e.kind == "cancelled")


def test_entry_with_no_next_bar_cancelled_on_end() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    last = bar(0, 100.0)
    broker.on_end({"AAPL": last})
    assert broker.position_state(ref) == "cancelled"
    assert broker.cancelled_entries == 1
    assert ledger.fills == ()
    # a later bar changes nothing
    broker.on_bar("AAPL", bar(1, 100.0))
    assert ledger.fills == ()


def test_long_stop_touch_fills_at_stop() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 99.0, 99.5, 95.0, 96.0))
    last = ledger.fills[-1]
    assert (last.reason, last.side, last.price, last.time) == ("stop", "sell", 95, t(2))
    assert broker.position_state(ref) == "closed"
    assert not broker.stop_is_working(ref)


def test_long_stop_above_the_low_is_not_hit() -> None:
    ledger, broker = make()
    opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 99.0, 99.5, 95.01, 96.0))
    assert reasons(ledger) == ["entry"]


def test_long_stop_gap_fills_at_open() -> None:
    ledger, broker = make()
    opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 93.0, 94.0, 92.0, 93.5))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("stop", 93)


def test_short_stop_mirror() -> None:
    ledger, broker = make()
    ref = opened(broker, long=False, stop=105.0)
    broker.on_bar("AAPL", bar(2, 101.0, 104.99, 100.0))
    assert reasons(ledger) == ["entry"]
    broker.on_bar("AAPL", bar(3, 101.0, 105.0, 100.0))
    assert (ledger.fills[-1].side, ledger.fills[-1].price, ledger.fills[-1].reason) == (
        "buy", 105, "stop")
    assert broker.position_state(ref) == "closed"


def test_short_stop_gap_fills_at_open() -> None:
    ledger, broker = make()
    opened(broker, long=False, stop=105.0)
    broker.on_bar("AAPL", bar(2, 107.0, 108.0, 106.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("stop", 107)


def test_target_requires_trade_through() -> None:
    ledger, broker = make()
    ref = opened(broker, tp=110.0)
    broker.on_bar("AAPL", bar(2, 105.0, 110.0, 104.0))  # touch only
    assert reasons(ledger) == ["entry"]
    broker.on_bar("AAPL", bar(3, 105.0, 110.01, 104.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price, ledger.fills[-1].side) == (
        "target", 110, "sell")
    assert broker.position_state(ref) == "closed"


def test_short_target_requires_trade_through() -> None:
    ledger, broker = make()
    opened(broker, long=False, stop=105.0, tp=90.0)
    broker.on_bar("AAPL", bar(2, 95.0, 96.0, 90.0))
    assert reasons(ledger) == ["entry"]
    broker.on_bar("AAPL", bar(3, 95.0, 96.0, 89.99))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price, ledger.fills[-1].side) == (
        "target", 90, "buy")


def test_target_gap_fills_at_open() -> None:
    ledger, broker = make()
    opened(broker, tp=110.0)
    broker.on_bar("AAPL", bar(2, 112.0, 113.0, 111.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("target", 112)


def test_short_target_gap_fills_at_open() -> None:
    ledger, broker = make()
    opened(broker, long=False, stop=105.0, tp=90.0)
    broker.on_bar("AAPL", bar(2, 88.0, 89.0, 87.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("target", 88)


def test_stop_wins_when_bar_covers_both() -> None:
    ledger, broker = make()
    opened(broker, stop=95.0, tp=110.0)
    broker.on_bar("AAPL", bar(2, 100.0, 120.0, 90.0))
    assert reasons(ledger) == ["entry", "stop"]
    assert ledger.fills[-1].price == 95
    # the short mirror
    ledger2, broker2 = make()
    opened(broker2, long=False, stop=105.0, tp=90.0)
    broker2.on_bar("AAPL", bar(2, 100.0, 110.0, 80.0))
    assert reasons(ledger2) == ["entry", "stop"]
    assert ledger2.fills[-1].price == 105


def test_stop_checked_on_entry_bar() -> None:
    ledger, broker = make()
    ref = open_pos(broker, stop=95.0)
    broker.on_bar("AAPL", bar(1, 100.0, 101.0, 94.0))
    assert reasons(ledger) == ["entry", "stop"]
    assert [f.price for f in ledger.fills] == [100, 95]
    assert {f.time for f in ledger.fills} == {t(1)}
    assert broker.position_state(ref) == "closed"


def test_target_checked_on_entry_bar() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    broker.set_take_profit(ref, 102.0, 10)
    broker.on_bar("AAPL", bar(1, 100.0, 103.0, 99.0))
    assert reasons(ledger) == ["entry", "target"]


def test_modify_stop_on_pending_entry_applies_at_fill() -> None:
    ledger, broker = make()
    ref = open_pos(broker, stop=95.0)
    broker.modify_stop(ref, 98.0)
    broker.set_take_profit(ref, 99.5, 10)
    broker.on_bar("AAPL", bar(1, 100.0, 100.0, 98.0))  # hits 98 but not 95
    assert reasons(ledger) == ["entry", "stop"]
    assert ledger.fills[-1].price == 98


def test_modify_stop_on_open_position_applies_from_next_bar() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.modify_stop(ref, 99.0)
    broker.on_bar("AAPL", bar(2, 100.0, 100.0, 99.0))
    assert ledger.fills[-1].reason == "stop"
    assert ledger.fills[-1].price == 99


def test_stop_prices_are_not_rounded_to_the_tick() -> None:
    ledger, broker = make()
    opened(broker, stop=95.123456)
    broker.on_bar("AAPL", bar(2, 99.0, 99.0, 95.0))
    assert ledger.fills[-1].price == Decimal("95.1235")  # only the ledger's 4 decimals


def test_algo_close_fills_next_open() -> None:
    ledger, broker = make()
    ref = opened(broker)
    broker.close_position(ref, price=102.0, time=t(1))
    assert broker.position_state(ref) == "exiting"
    assert len(ledger.fills) == 1
    broker.on_bar("AAPL", bar(2, 103.0, 104.0, 102.0))
    last = ledger.fills[-1]
    assert (last.reason, last.side, last.price, last.time) == ("algo_exit", "sell", 103, t(2))
    assert broker.position_state(ref) == "closed"
    assert len(ledger.round_trips) == 1


def test_algo_close_at_session_end_fills_at_request_close() -> None:
    ledger, broker = make()
    ref = opened(broker)
    broker.close_position(ref, price=102.0, time=t(1))
    broker.on_bar("AAPL", bar(0, 110.0, day=1))
    last = ledger.fills[-1]
    assert (last.reason, last.price, last.time) == ("algo_exit", 102, t(1))
    assert broker.position_state(ref) == "closed"


def test_session_end_close_is_recorded_after_the_equity_point_of_that_bar() -> None:
    ledger, broker = make(CostModel())
    ref = open_pos(broker)
    broker.on_bar("AAPL", bar(1, 101.0, 102.0, 100.5, 102.0))
    (before,) = ledger.mark(t(1), {"AAPL": 102.0})[-1:]
    broker.close_position(ref, price=102.0, time=t(1))
    broker.on_bar("AAPL", bar(0, 110.0, day=1))
    exit_fill = ledger.fills[-1]
    assert exit_fill.time == t(1) and exit_fill.commission == 1
    # the stored point for t(1) is unchanged: it still holds the position and lacks the exit cost
    stored = ledger.equity_points[-1]
    assert stored is before
    assert stored.position_value == 1020
    assert stored.equity == 10_009
    (after,) = ledger.mark(t(0, 1), {"AAPL": 110.0})[-1:]
    assert after.position_value == 0
    assert after.equity == 10_008  # 10 gross - 2 commissions
    for p in ledger.equity_points:
        assert p.equity == CAPITAL + p.realised + p.unrealised


def test_close_on_pending_entry_cancels_it() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    broker.close_position(ref, price=100.0, time=t(0))
    assert broker.position_state(ref) == "cancelled"
    assert broker.cancelled_entries == 1
    assert ledger.ignored_count() == 0
    broker.on_bar("AAPL", bar(1, 100.0, 101.0, 80.0))
    assert ledger.fills == ()


def test_exiting_position_no_longer_has_a_resting_bracket() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0, tp=110.0)
    broker.close_position(ref, price=100.0, time=t(1))
    assert not broker.stop_is_working(ref)
    # exiting: modifications and a second close are ignored
    broker.modify_stop(ref, 99.0)
    broker.set_take_profit(ref, 101.0, 10)
    broker.close_position(ref, price=100.0, time=t(1))
    assert [e.kind for e in ledger.ignored_events] == ["modify_stop", "take_profit", "close"]
    # the next bar would have hit the stop, but the algo's exit fills at its open
    broker.on_bar("AAPL", bar(2, 100.5, 120.0, 90.0))
    assert reasons(ledger) == ["entry", "algo_exit"]
    assert ledger.fills[-1].price == Decimal("100.5")
    cancelled = [e for e in ledger.orders if e.kind == "cancelled"]
    assert len(cancelled) == 2 and all(e.time == t(1) for e in cancelled)


def test_exiting_position_is_filled_by_on_end_at_requested_price() -> None:
    ledger, broker = make()
    ref = opened(broker)
    broker.close_position(ref, price=102.0, time=t(1))
    broker.on_end({"AAPL": bar(1, 100.0)})
    last = ledger.fills[-1]
    assert (last.reason, last.price, last.time) == ("algo_exit", 102, t(1))
    assert broker.position_state(ref) == "closed"


def test_on_end_leaves_open_positions_open() -> None:
    ledger, broker = make()
    ref = opened(broker, tp=110.0)
    broker.on_end({"AAPL": bar(1, 100.0)})
    assert broker.position_state(ref) == "open"
    assert broker.stop_is_working(ref)
    assert reasons(ledger) == ["entry"]
    assert ledger.round_trips == ()


def test_close_after_broker_close_ignored_and_counted() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 99.0, 99.0, 94.0))
    fills = ledger.fills
    broker.close_position(ref, price=96.0, time=t(2))
    assert ledger.ignored_count("close") == 1
    assert ledger.ignored_events[0].time == t(2)
    assert ledger.fills == fills


def test_close_on_cancelled_position_is_ignored_and_counted() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    broker.on_bar("AAPL", bar(0, 100.0, day=1))
    broker.close_position(ref, price=100.0, time=t(0, 1))
    assert ledger.ignored_count("close") == 1


def test_modify_after_close_ignored_and_counted() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 99.0, 99.0, 94.0))
    broker.modify_stop(ref, 96.0)
    broker.set_take_profit(ref, 120.0, 10)
    assert ledger.ignored_count("modify_stop") == 1
    assert ledger.ignored_count("take_profit") == 1
    assert ledger.ignored_events[0].time == t(2)
    assert reasons(ledger) == ["entry", "stop"]


def test_modify_on_cancelled_position_ignored() -> None:
    ledger, broker = make()
    ref = open_pos(broker)
    broker.on_end({})
    broker.modify_stop(ref, 96.0)
    broker.set_take_profit(ref, 120.0, 10)
    assert ledger.ignored_count() == 2


def test_commission_minimum_and_per_share() -> None:
    costs = CostModel()
    assert costs.commission_per_share == Decimal("0.005")
    assert costs.commission_minimum == 1
    assert costs.slippage_per_share == 0
    assert costs.commission(10) == Decimal("1.0000")  # 0.05 -> minimum
    assert costs.commission(200) == Decimal("1.0000")  # exactly the minimum
    assert costs.commission(400) == Decimal("2.0000")
    ledger, broker = make(CostModel(Decimal("0.01"), Decimal("1.5"), Decimal(0)))
    ref = open_pos(broker, volume=300)
    broker.on_bar("AAPL", bar(1, 100.0))
    broker.close_position(ref, price=100.0, time=t(1))
    broker.on_bar("AAPL", bar(2, 100.0))
    assert [f.commission for f in ledger.fills] == [3, 3]
    ledger2, broker2 = make(CostModel(Decimal("0.01"), Decimal("1.5"), Decimal(0)))
    open_pos(broker2, volume=10)
    broker2.on_bar("AAPL", bar(1, 100.0))
    assert ledger2.fills[0].commission == Decimal("1.5")


def test_cost_model_accepts_numbers_and_rejects_negatives() -> None:
    costs = CostModel(0.01, 2, 0.5)
    assert costs.commission_per_share == Decimal("0.01")
    assert costs.commission_minimum == Decimal("2")
    assert costs.slippage_per_share == Decimal("0.5")
    for kwargs in ({"commission_per_share": -1}, {"commission_minimum": -0.01},
                   {"slippage_per_share": -0.01}, {"slippage_per_share": float("nan")}):
        with pytest.raises(ValueError):
            CostModel(**kwargs)
    with pytest.raises(AttributeError):
        costs.slippage_per_share = Decimal(0)  # type: ignore[misc]


def test_slippage_applies_to_market_fills_only() -> None:
    """Market entries and exits are slipped against the trader; target fills are not."""
    costs = CostModel(Decimal(0), Decimal(0), Decimal("0.02"))
    ledger, broker = make(costs)
    ref = open_pos(broker, stop=90.0)
    broker.set_take_profit(ref, 110.0, 10)
    broker.on_bar("AAPL", bar(1, 100.0, 101.0, 99.0))
    assert ledger.fills[0].price == Decimal("100.02")
    broker.on_bar("AAPL", bar(2, 105.0, 111.0, 104.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("target", 110)
    # short entry slips down, algo exit (a buy) slips up
    ledger2, broker2 = make(costs)
    ref2 = open_pos(broker2, long=False, stop=110.0)
    broker2.on_bar("AAPL", bar(1, 100.0))
    broker2.close_position(ref2, price=100.0, time=t(1))
    broker2.on_bar("AAPL", bar(2, 98.0))
    assert [f.price for f in ledger2.fills] == [Decimal("99.98"), Decimal("98.02")]


def test_slippage_applies_to_stop_fills() -> None:
    costs = CostModel(Decimal(0), Decimal(0), Decimal("0.02"))
    ledger, broker = make(costs)
    opened(broker, stop=95.0)
    broker.on_bar("AAPL", bar(2, 99.0, 99.0, 94.0))
    assert ledger.fills[-1].price == Decimal("94.98")
    ledger2, broker2 = make(costs)
    opened(broker2, long=False, stop=105.0)
    broker2.on_bar("AAPL", bar(2, 107.0, 108.0, 106.0))  # gap: open 107, slip up
    assert ledger2.fills[-1].price == Decimal("107.02")


def test_session_end_exit_fallback_has_no_slippage() -> None:
    ledger, broker = make(CostModel(Decimal(0), Decimal(0), Decimal("0.02")))
    ref = opened(broker)
    broker.close_position(ref, price=102.0, time=t(1))
    broker.on_bar("AAPL", bar(0, 110.0, day=1))
    assert ledger.fills[-1].price == 102


def test_limit_entry_not_implemented() -> None:
    ledger, broker = make()
    with pytest.raises(NotImplementedError):
        broker.open_position(symbol="AAPL", volume=10, direction=1, price=100.0, time=t(0),
                             limit=True, stop_price=95.0, oca_group="g")
    assert ledger.orders == ()


def test_partial_tp_rejected() -> None:
    ledger, broker = make()
    ref = open_pos(broker, volume=10)
    with pytest.raises(ValueError):
        broker.set_take_profit(ref, 110.0, 5)
    broker.set_take_profit(ref, 110.0, 10)  # whole quantity is fine
    assert [e.order_type for e in ledger.orders if e.kind == "placed"] == [
        "entry", "stop", "target"]


def test_unknown_ref_and_bad_volume_are_errors() -> None:
    _, broker = make()
    with pytest.raises(KeyError):
        broker.stop_is_working(99)
    with pytest.raises(ValueError):
        open_pos(broker, volume=0)


def test_on_bar_ignores_other_symbols() -> None:
    ledger, broker = make()
    ref = open_pos(broker, stop=95.0)
    broker.on_bar("MSFT", bar(1, 100.0, 100.0, 10.0))
    broker.on_bar("MSFT", bar(0, 100.0, day=1))
    assert broker.position_state(ref) == "pending"
    assert ledger.fills == ()
    broker.on_bar("AAPL", bar(1, 100.0))
    assert broker.position_state(ref) == "open"
    broker.on_bar("MSFT", bar(2, 1.0, 1.0, 1.0))
    assert broker.position_state(ref) == "open"
    assert reasons(ledger) == ["entry"]


def test_bracket_sibling_is_cancelled_when_one_leg_fills() -> None:
    ledger, broker = make()
    opened(broker, stop=95.0, tp=110.0)
    broker.on_bar("AAPL", bar(2, 100.0, 111.0, 99.0))  # target fills
    stop_order = next(e for e in ledger.orders if e.order_type == "stop")
    tp_order = next(e for e in ledger.orders if e.order_type == "target")
    cancels = [e for e in ledger.orders if e.kind == "cancelled"]
    assert [c.order_id for c in cancels] == [stop_order.order_id]
    assert ledger.fills[-1].order_id == tp_order.order_id

    ledger2, broker2 = make()
    opened(broker2, stop=95.0, tp=110.0)
    broker2.on_bar("AAPL", bar(2, 100.0, 105.0, 94.0))  # stop fills
    stop2 = next(e for e in ledger2.orders if e.order_type == "stop")
    tp2 = next(e for e in ledger2.orders if e.order_type == "target")
    cancels2 = [e for e in ledger2.orders if e.kind == "cancelled"]
    assert [c.order_id for c in cancels2] == [tp2.order_id]
    assert ledger2.fills[-1].order_id == stop2.order_id


def test_orders_are_recorded_in_the_ledger() -> None:
    ledger, broker = make()
    ref = open_pos(broker, stop=95.0)
    broker.modify_stop(ref, 96.0)
    broker.set_take_profit(ref, 110.0, 10)
    broker.set_take_profit(ref, 111.0, 10)
    broker.on_bar("AAPL", bar(1, 100.0, 101.0, 99.0))
    broker.modify_stop(ref, 97.0)
    broker.close_position(ref, price=100.0, time=t(1))
    broker.on_bar("AAPL", bar(2, 100.0))
    summary = [(e.kind, e.order_type, e.side, e.price) for e in ledger.orders]
    assert summary == [
        ("placed", "entry", "buy", None),
        ("placed", "stop", "sell", 95),
        ("modified", None, None, 96),
        ("placed", "target", "sell", 110),
        ("modified", None, None, 111),
        ("modified", None, None, 97),
        ("cancelled", None, None, None),
        ("cancelled", None, None, None),
        ("placed", "exit", "sell", None),
    ]
    entry, stop, exit_order = (next(e for e in ledger.orders if e.order_type == k)
                               for k in ("entry", "stop", "exit"))
    assert [f.order_id for f in ledger.fills] == [entry.order_id, exit_order.order_id]
    assert stop.broker_ref == ref
    target = next(e for e in ledger.orders if e.order_type == "target")
    modified = [e.order_id for e in ledger.orders if e.kind == "modified"]
    assert modified == [stop.order_id, target.order_id, stop.order_id]
    assert all(e.quantity in (None, 10) for e in ledger.orders)


def test_two_positions_are_tracked_independently() -> None:
    ledger, broker = make()
    a = open_pos(broker, stop=95.0)
    b = open_pos(broker, long=False, stop=105.0)
    broker.on_bar("AAPL", bar(1, 100.0, 100.0, 96.0))
    broker.on_bar("AAPL", bar(2, 100.0, 106.0, 96.0))  # stops b only
    assert broker.position_state(a) == "open"
    assert broker.position_state(b) == "closed"
    assert broker.costs == FREE


def test_trade_stands_down_after_broker_stop() -> None:
    ledger = Ledger()
    recorder = ledger.open_account("a", "AAPL", CAPITAL)
    calls: list[str] = []

    class Spy(PaperBroker):
        def modify_stop(self, ref, stop_price):
            calls.append("modify_stop")
            super().modify_stop(ref, stop_price)

        def set_take_profit(self, ref, price, quantity):
            calls.append("set_take_profit")
            super().set_take_profit(ref, price, quantity)

        def close_position(self, ref, *, price, time):
            calls.append("close_position")
            super().close_position(ref, price=price, time=time)

        def stop_is_working(self, ref):
            calls.append("stop_is_working")
            return super().stop_is_working(ref)

    broker = Spy(recorder, FREE)
    trade = Trade("AAPL", 10, "t0", 100.0, t(0), 1, logging.getLogger("test"), broker=broker)
    assert trade.stopPrice == 99.0
    broker.on_bar("AAPL", bar(1, 100.0, 100.5, 98.5, 98.6))
    assert reasons(ledger) == ["entry", "stop"]
    assert ledger.fills[-1].price == 99
    calls.clear()
    curpoint = {"date": t(1), "close": 98.6, "high": 100.5, "low": 98.5}
    assert trade.check_stoploss(curpoint) == 0
    assert calls == ["stop_is_working"]
    assert ledger.ignored_count() == 0
    assert len(ledger.fills) == 2


def test_entry_bar_that_opens_beyond_the_stop_fills_both_at_the_open() -> None:
    ledger, broker = make()
    ref = open_pos(broker, stop=95.0)
    broker.on_bar("AAPL", bar(1, 90.0, 91.0, 89.0))  # gaps below the stop of 95
    assert [(f.reason, f.price) for f in ledger.fills] == [("entry", 90), ("stop", 90)]
    assert broker.position_state(ref) == "closed"
    assert ledger.open_positions() == ()
    ledger2, broker2 = make()
    ref2 = open_pos(broker2, long=False, stop=105.0)
    broker2.on_bar("AAPL", bar(1, 110.0, 111.0, 109.0))
    assert [(f.reason, f.price, f.side) for f in ledger2.fills] == [
        ("entry", 110, "sell"), ("stop", 110, "buy")]
    assert broker2.position_state(ref2) == "closed"
    # with slippage both legs move against the trader
    ledger3, broker3 = make(CostModel(Decimal(0), Decimal(0), Decimal("0.02")))
    open_pos(broker3, stop=95.0)
    broker3.on_bar("AAPL", bar(1, 90.0, 91.0, 89.0))
    assert [f.price for f in ledger3.fills] == [Decimal("90.02"), Decimal("89.98")]


def test_position_held_overnight_is_checked_against_the_next_days_bars() -> None:
    # long: a quiet next day leaves it open, a gap below the stop fills at the open
    ledger, broker = make()
    ref = opened(broker, stop=95.0, tp=110.0)
    broker.on_bar("AAPL", bar(0, 100.0, 101.0, 99.0, day=1))
    assert broker.position_state(ref) == "open"
    assert reasons(ledger) == ["entry"]
    broker.on_bar("AAPL", bar(1, 93.0, 94.0, 92.0, day=1))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("stop", 93)
    assert ledger.fills[-1].time == t(1, 1)
    # short: gap above the stop
    ledger2, broker2 = make()
    opened(broker2, long=False, stop=105.0, tp=90.0)
    broker2.on_bar("AAPL", bar(0, 107.0, 108.0, 106.0, day=1))
    assert (ledger2.fills[-1].reason, ledger2.fills[-1].price) == ("stop", 107)
    # gaps through the target on the next day
    ledger3, broker3 = make()
    opened(broker3, stop=95.0, tp=110.0)
    broker3.on_bar("AAPL", bar(0, 112.0, 113.0, 111.0, day=1))
    assert (ledger3.fills[-1].reason, ledger3.fills[-1].price) == ("target", 112)
    ledger4, broker4 = make()
    opened(broker4, long=False, stop=105.0, tp=90.0)
    broker4.on_bar("AAPL", bar(0, 88.0, 89.0, 87.0, day=1))
    assert (ledger4.fills[-1].reason, ledger4.fills[-1].price) == ("target", 88)


def test_trailing_stop_modified_each_bar_long_and_short() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.modify_stop(ref, 97.0)
    broker.on_bar("AAPL", bar(2, 100.0, 101.0, 97.5))
    broker.modify_stop(ref, 98.0)
    broker.on_bar("AAPL", bar(3, 100.0, 102.0, 98.5))
    broker.modify_stop(ref, 99.0)
    assert reasons(ledger) == ["entry"]
    broker.on_bar("AAPL", bar(4, 100.0, 100.0, 99.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("stop", 99)
    assert [e.price for e in ledger.orders if e.kind == "modified"] == [97, 98, 99]
    ledger2, broker2 = make()
    ref2 = opened(broker2, long=False, stop=105.0)
    broker2.modify_stop(ref2, 103.0)
    broker2.on_bar("AAPL", bar(2, 100.0, 102.9, 99.0))
    broker2.modify_stop(ref2, 102.0)
    broker2.on_bar("AAPL", bar(3, 100.0, 101.9, 99.0))
    assert reasons(ledger2) == ["entry"]
    broker2.on_bar("AAPL", bar(4, 100.0, 102.0, 99.0))
    assert (ledger2.fills[-1].side, ledger2.fills[-1].price) == ("buy", 102)


def test_target_set_twice_uses_the_last_price_and_one_order() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=90.0, tp=110.0)
    broker.set_take_profit(ref, 105.0, 10)
    broker.on_bar("AAPL", bar(2, 100.0, 105.5, 99.0))
    assert (ledger.fills[-1].reason, ledger.fills[-1].price) == ("target", 105)
    assert [e.order_type for e in ledger.orders if e.kind == "placed"].count("target") == 1
    ledger2, broker2 = make()
    ref2 = opened(broker2, long=False, stop=110.0, tp=90.0)
    broker2.set_take_profit(ref2, 95.0, 10)
    broker2.on_bar("AAPL", bar(2, 100.0, 101.0, 94.5))
    assert (ledger2.fills[-1].reason, ledger2.fills[-1].price, ledger2.fills[-1].side) == (
        "target", 95, "buy")


def test_short_algo_close_next_open_and_session_end_fallback() -> None:
    costs = CostModel(Decimal("0.005"), Decimal(1), Decimal("0.02"))
    ledger, broker = make(costs)
    ref = open_pos(broker, long=False, stop=110.0)
    broker.on_bar("AAPL", bar(1, 100.0))
    broker.close_position(ref, price=101.0, time=t(1))
    assert broker.position_state(ref) == "exiting"
    broker.on_bar("AAPL", bar(2, 98.0))
    # sell 10 at 100 - 0.02, buy 10 at 98 + 0.02, 1.00 commission each way
    assert [f.price for f in ledger.fills] == [Decimal("99.98"), Decimal("98.02")]
    (trip,) = ledger.round_trips
    assert (trip.side, trip.gross, trip.commission, trip.net) == (
        "short", Decimal("19.6"), Decimal(2), Decimal("17.6"))
    assert ledger.equity() == CAPITAL + Decimal("17.6")
    # session end: the exit fills at the requested price, with the requesting time, no slippage
    ledger2, broker2 = make(costs)
    ref2 = open_pos(broker2, long=False, stop=110.0)
    broker2.on_bar("AAPL", bar(1, 100.0))
    broker2.close_position(ref2, price=101.0, time=t(1))
    broker2.on_bar("AAPL", bar(0, 90.0, day=1))
    last = ledger2.fills[-1]
    assert (last.reason, last.side, last.price, last.time) == ("algo_exit", "buy", 101, t(1))
    assert broker2.position_state(ref2) == "closed"


def test_close_twice_and_close_after_stop_leave_one_exit() -> None:
    ledger, broker = make()
    ref = opened(broker, stop=95.0)
    broker.close_position(ref, price=100.0, time=t(1))
    broker.close_position(ref, price=100.0, time=t(1))
    assert ledger.ignored_count("close") == 1
    broker.on_bar("AAPL", bar(2, 99.0, 99.0, 90.0))
    assert reasons(ledger) == ["entry", "algo_exit"]
    broker.close_position(ref, price=99.0, time=t(2))
    broker.on_end({})
    assert ledger.ignored_count("close") == 2
    assert reasons(ledger) == ["entry", "algo_exit"]
    assert len(ledger.round_trips) == 1


def test_prices_stay_exact_decimals() -> None:
    ledger, broker = make()
    open_pos(broker, stop=0.1)
    broker.on_bar("AAPL", bar(1, 0.1 + 0.2, 0.4, 0.2))  # 0.30000000000000004 as a float
    assert ledger.fills[0].price == Decimal("0.3000")
    assert isinstance(ledger.fills[0].price, Decimal)
    ledger2, broker2 = make(CostModel(Decimal(0), Decimal(0), Decimal("0.01")))
    ref = open_pos(broker2, stop=123.4567)
    broker2.on_bar("AAPL", bar(1, 123.4567, 124.0, 123.4568))
    assert ledger2.fills[0].price == Decimal("123.4667")  # not 123.46670000000002
    broker2.on_bar("AAPL", bar(2, 124.0, 124.0, 123.4567))
    assert ledger2.fills[1].price == Decimal("123.4467")
    assert broker2.position_state(ref) == "closed"
    # exact money: 10 x (123.4467 - 123.4667) = -0.2
    assert ledger2.round_trips[0].gross == Decimal("-0.2")


def test_commission_is_charged_on_every_fill_including_the_minimum() -> None:
    costs = CostModel(Decimal("0.005"), Decimal(1), Decimal(0))
    ledger, broker = make(costs)
    ref = open_pos(broker, volume=1000)  # 5.00 per fill
    broker.on_bar("AAPL", bar(1, 100.0))
    broker.close_position(ref, price=100.0, time=t(1))
    broker.on_bar("AAPL", bar(2, 100.0))
    assert [f.commission for f in ledger.fills] == [5, 5]
    ledger2, broker2 = make(costs)
    open_pos(broker2, long=False, stop=105.0, volume=10)  # minimum applies
    broker2.on_bar("AAPL", bar(1, 100.0, 106.0, 99.0))
    assert [(f.reason, f.commission) for f in ledger2.fills] == [("entry", 1), ("stop", 1)]
    assert ledger2.round_trips[0].commission == 2


def test_target_fill_has_no_slippage_and_stop_fill_has() -> None:
    costs = CostModel(Decimal(0), Decimal(0), Decimal("0.05"))
    ledger, broker = make(costs)
    opened(broker, long=False, stop=105.0, tp=90.0)
    broker.on_bar("AAPL", bar(2, 95.0, 96.0, 89.0))
    assert ledger.fills[-1].price == 90  # short target buys at 90, no slippage
    ledger2, broker2 = make(costs)
    opened(broker2, long=False, stop=105.0, tp=90.0)
    broker2.on_bar("AAPL", bar(2, 100.0, 105.5, 95.0))
    assert ledger2.fills[-1].price == Decimal("105.05")  # buy stop, slipped up


def test_two_symbols_on_one_broker_do_not_cross_talk() -> None:
    ledger, broker = make()
    a = open_pos(broker, symbol="AAPL", stop=95.0)
    broker.on_bar("AAPL", bar(1, 100.0))  # a opens
    m = open_pos(broker, symbol="MSFT", stop=190.0, minute=1)  # pending on another symbol
    broker.on_bar("AAPL", bar(2, 90.0, 91.0, 80.0))  # stops a; must not touch m
    assert broker.position_state(a) == "closed"
    assert broker.position_state(m) == "pending"
    broker.on_bar("MSFT", bar(2, 200.0))
    assert broker.position_state(m) == "open"
    assert reasons(ledger) == ["entry", "stop", "entry"]


def test_empty_and_repeated_end_of_data_are_harmless() -> None:
    ledger, broker = make()
    broker.on_end({})
    broker.on_end({})
    assert ledger.fills == () and ledger.orders == ()
    ref = open_pos(broker)
    broker.on_end({})
    broker.on_end({})
    assert broker.cancelled_entries == 1
    assert broker.position_state(ref) == "cancelled"
    assert [e.kind for e in ledger.orders].count("cancelled") == 2


def test_brokers_share_no_state() -> None:
    ledger_a, a = make()
    ledger_b, b = make()
    ref_a = open_pos(a)
    ref_b = open_pos(b)
    assert ref_a == ref_b  # refs are per broker, so each ledger account sees its own
    a.on_bar("AAPL", bar(1, 100.0))
    assert b.position_state(ref_b) == "pending"
    assert ledger_b.fills == ()
    assert len(ledger_a.fills) == 1


def test_ledger_agrees_with_broker_state_and_identity_holds_at_every_mark() -> None:
    costs = CostModel(Decimal("0.005"), Decimal(1), Decimal("0.01"))
    ledger, broker = make(costs)
    long_ref = open_pos(broker, stop=85.0)
    short_ref = open_pos(broker, long=False, stop=105.0, minute=0)
    broker.set_take_profit(short_ref, 90.0, 10)
    script = [
        bar(1, 100.0, 101.0, 99.0, 100.5),
        bar(2, 101.0, 102.0, 100.0, 101.5),
        bar(3, 99.0, 100.0, 89.0, 92.0),   # short target (trade-through), long untouched
        bar(4, 92.0, 93.0, 91.0, 93.0),
        bar(5, 93.0, 94.0, 90.0, 91.0),
        bar(0, 90.0, 91.0, 85.0, 86.0, day=1),
    ]
    broker.on_bar("AAPL", script[0])
    ledger.mark(script[0].date, {"AAPL": script[0].close})
    assert broker.position_state(long_ref) == "open" and broker.position_state(short_ref) == "open"
    assert len(ledger.open_positions()) == 2
    for b in script[1:3]:
        broker.on_bar("AAPL", b)
        ledger.mark(b.date, {"AAPL": b.close})
    assert broker.position_state(short_ref) == "closed"
    assert [p.side for p in ledger.open_positions()] == ["long"]
    broker.close_position(long_ref, price=93.0, time=script[3].date)
    for b in script[3:]:
        broker.on_bar("AAPL", b)
        ledger.mark(b.date, {"AAPL": b.close})
    assert broker.position_state(long_ref) == "closed"
    assert ledger.open_positions() == ()
    assert all(f.order_id is not None for f in ledger.fills)
    for p in ledger.equity_points:
        assert p.equity == CAPITAL + p.realised + p.unrealised
    assert ledger.equity_points[-1].position_value == 0


# --- end to end through Engine + MappingRouter + Ledger ----------------------

def _end_bar(minute: int, base: float) -> Bar:
    stamp = t(minute)
    return Bar(stamp, int(stamp.timestamp()), base, base + 1, base - 1, base + 0.5, 100)


class _Feed:
    def __init__(self, rows: int) -> None:
        self.rows = rows

    def symbols(self) -> list[str]:
        return ["AAPL", "MSFT"]

    def warmup(self, symbol: str) -> pd.DataFrame:
        records = [_end_bar(-1, 50.0).as_record()]
        frame = pd.DataFrame.from_records(records, columns=list(COLUMNS))
        return frame.astype({"time": "int64", "volume": "int64"})

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        for i in range(self.rows):
            yield t(i), {"AAPL": _end_bar(i, 100.0 + i), "MSFT": _end_bar(i, 200.0 + i)}


class _Algo:
    """Opens at its first update; asks to close at update number `close_at`, if given."""

    def __init__(self, name: str, ticker: str, *, open_at: int = 1, close_at: int | None = None):
        self.name, self.ticker = name, ticker
        self.open_at, self.close_at = open_at, close_at
        self.n = 0
        self.ref = None
        self.broker: PaperBroker

    def update(self, frame: pd.DataFrame) -> None:
        self.n += 1
        row = frame.iloc[-1]
        if self.n == self.open_at:
            self.ref = self.broker.open_position(
                symbol=self.ticker, volume=10, direction=1, price=float(row["close"]),
                time=row["date"], limit=False, stop_price=1.0, oca_group="g")
        if self.n == self.close_at:
            self.broker.close_position(self.ref, price=float(row["close"]), time=row["date"])

    def update_frontend(self) -> dict:
        return {}


def test_engine_end_to_end_on_end_runs_before_finish_and_identity_holds() -> None:
    ledger = Ledger()
    costs = CostModel()
    algos = [
        _Algo("hold", "AAPL", open_at=1),                 # still open at the end
        _Algo("exiting", "MSFT", open_at=1, close_at=4),  # close requested on the last bar
        _Algo("late", "AAPL", open_at=4),                 # entry requested on the last bar
    ]
    brokers = {a.name: PaperBroker(ledger.open_account(a.name, a.ticker, CAPITAL), costs)
               for a in algos}
    engine = Engine(EngineConfig(), _Feed(4), MappingRouter(brokers), NullSink(), algos,
                    ledger=ledger, end_commission=lambda idname, qty: brokers[idname].costs.commission(qty))
    engine.run_backtest()

    # "late" was cancelled by on_end, so finish had nothing to close for it
    assert brokers["late"].cancelled_entries == 1
    assert brokers["late"].position_state(algos[2].ref) == "cancelled"
    trips = {rt.account: rt for rt in ledger.round_trips}
    assert set(trips) == {"hold", "exiting"}
    # "exiting" was filled by on_end at the requested close, not by finish
    assert (trips["exiting"].exit_reason, trips["exiting"].synthetic) == ("algo_exit", False)
    assert trips["exiting"].exit_price == Decimal("203.5")
    assert trips["exiting"].exit_time == t(3)
    # "hold" is closed synthetically by finish, charged through end_commission
    assert (trips["hold"].exit_reason, trips["hold"].synthetic) == ("end_of_data", True)
    assert trips["hold"].commission == 2  # entry 1.00 + synthetic exit 1.00
    assert trips["hold"].entry_price == 101  # next bar's open, not the signal close
    assert len([f for f in ledger.fills if f.synthetic]) == 1
    # every equity point satisfies the identity, the last one equals the live equity
    for p in ledger.equity_points:
        capital = CAPITAL * (len(algos) if p.account is None else 1)
        assert p.equity == capital + p.realised + p.unrealised
    total = [p for p in ledger.equity_points if p.account is None]
    assert total[-1].equity == ledger.equity()
    assert ledger.unrealised_pnl() == 0 and ledger.open_positions() == ()
