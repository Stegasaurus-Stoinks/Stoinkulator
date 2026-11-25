"""The ledger's run-wide sequence (issue 227, item 1 of issue 214).

Fills from a real broker arrive between bars, so time alone cannot order fills
and order events: one counter numbers order events, fills and ignored events.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from MainStoinker.engine.ledger import Fill, IgnoredEvent, Ledger, OrderEvent
from tests.test_run_documents_live import live_ledger, load, store_live_run

T0 = datetime(2024, 8, 16, 9, 30)


def at(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def mixed_ledger() -> Ledger:
    led = Ledger()
    acct = led.open_account("a", "AAPL", 1000)
    order = acct.order_placed("r", time=at(0), side="buy", quantity=1, order_type="market")
    acct.fill("r", side="buy", quantity=1, price=D("10"), commission=0, time=at(0),
              reason="entry", order_id=order)
    stop = acct.order_placed("r", time=at(0), side="sell", quantity=1, order_type="stop",
                             price=D("9"))
    acct.order_modified(stop, time=at(1), price=D("9.5"))
    acct.ignored("r", time=at(1), kind="close")
    acct.fill("r", side="sell", quantity=1, price=D("9.5"), commission=0, time=at(1),
              reason="stop", order_id=stop)
    acct.order_cancelled(stop, time=at(1))
    return led


def test_seq_strictly_increasing_across_kinds():
    led = mixed_ledger()
    records = [*led.orders, *led.fills, *led.ignored_events]
    assert {type(r) for r in records} == {OrderEvent, Fill, IgnoredEvent}
    seqs = sorted(r.seq for r in records)
    assert seqs == list(range(1, len(records) + 1))  # one counter, no gaps, no repeats
    # and in the order the calls were made
    kinds = [type(r).__name__ + getattr(r, "kind", "")
             for r in sorted(records, key=lambda r: r.seq)]
    assert kinds == ["OrderEventplaced", "Fill", "OrderEventplaced", "OrderEventmodified",
                     "IgnoredEventclose", "Fill", "OrderEventcancelled"]


def test_seq_is_per_run_not_per_account_and_not_shared_between_ledgers():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "MSFT", 1000)
    a.order_placed("x", time=at(0), side="buy", quantity=1, order_type="market")
    b.order_placed("y", time=at(0), side="buy", quantity=1, order_type="market")
    assert [e.seq for e in led.orders] == [1, 2]
    other = Ledger()
    other.open_account("a", "AAPL", 1000).order_placed(
        "x", time=at(0), side="buy", quantity=1, order_type="market")
    assert other.orders[0].seq == 1


def test_seq_is_invisible_to_equality_fields_and_the_real_fill_fields_default_to_none():
    led = mixed_ledger()
    for cls in (OrderEvent, Fill, IgnoredEvent):
        assert "seq" not in {f.name for f in dataclasses.fields(cls)}
    fill = led.fills[0]
    assert fill.exec_time is None and fill.commission_final is None
    assert dataclasses.replace(fill) == fill  # a copy without a seq is still equal
    # the frozen repr-based digests of simulated runs must not see any of the new data
    for name in ("seq", "exec_time", "commission_final"):
        assert name not in repr(fill)
    assert "seq" not in repr(led.orders[0]) and "seq" not in repr(led.ignored_events[0])


def test_a_stored_run_loads_without_a_seq_and_equals_the_ledger(tmp_path: Path):
    """Records written by the unchanged store come back with no sequence, and still equal."""
    led = live_ledger()
    path = tmp_path / "runs.db"
    store_live_run(path, led=led, counts=False)
    run, _ = load(path)
    assert all(r.seq is None for r in [*run.fills, *run.order_events])
    assert list(run.fills) == list(led.fills)
    assert list(run.order_events) == list(led.orders)
