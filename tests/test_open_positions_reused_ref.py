"""One lifecycle rule for open positions (issue 227, item 1 of issue 214).

`Engine.end_live` and the run documents must agree when a broker reference is
reused: levels and entry time are cleared when the reference goes flat.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from MainStoinker.engine.engine import Engine
from MainStoinker.engine.ledger import Ledger, open_position_levels
from MainStoinker.engine.run_documents import open_positions
from MainStoinker.engine.stats import fmt4
from tests.test_run_documents_live import load, store_live_run

T0 = datetime(2024, 8, 16, 9, 30)


def at(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def reused_ref_ledger() -> Ledger:
    """Reference "x": a long with a stop and a target closes by its stop; "x" is reused.

    The reopened position has a new stop and no target.
    """
    led = Ledger()
    acct = led.open_account("a1", "AAPL", 10000)
    order = acct.order_placed("x", time=at(0), side="buy", quantity=2, order_type="market")
    acct.fill("x", side="buy", quantity=2, price=D("10"), commission=0, time=at(0),
              reason="entry", order_id=order)
    stop = acct.order_placed("x", time=at(0), side="sell", quantity=2, order_type="stop",
                             price=D("9"))
    acct.order_placed("x", time=at(0), side="sell", quantity=2, order_type="target",
                      price=D("12"))
    acct.fill("x", side="sell", quantity=2, price=D("9"), commission=0, time=at(3),
              reason="stop", order_id=stop)
    acct.fill("x", side="buy", quantity=1, price=D("11"), commission=0, time=at(5),
              reason="entry")
    acct.order_placed("x", time=at(5), side="sell", quantity=1, order_type="stop",
                      price=D("10.5"))
    return led


def test_engine_and_documents_agree_on_reused_ref(tmp_path: Path):
    led = reused_ref_ledger()
    path = tmp_path / "runs.db"
    store_live_run(path, led=led, counts=False)
    run, _ = load(path)
    (doc,) = open_positions(run)
    (held,) = Engine._open_positions(SimpleNamespace(_ledger=led))
    # the new position: entered at minute 5 with its own stop, and no inherited target
    assert held.entry_time == at(5)
    assert held.stop == D("10.5") and held.target is None
    assert doc["entry_time"] == held.entry_time.isoformat()
    assert doc["stop"] == fmt4(held.stop) and doc["target"] is None


def test_the_shared_function_clears_levels_and_entry_time_when_flat():
    led = reused_ref_ledger()
    levels = open_position_levels(led.fills, led.orders)
    assert set(levels) == {("a1", "x")}
    only = levels[("a1", "x")]
    assert (only.entry_time, only.stop, only.target) == (at(5), D("10.5"), None)


def test_a_flat_reference_has_no_lifecycle():
    led = Ledger()
    acct = led.open_account("a", "AAPL", 1000)
    acct.fill("x", side="buy", quantity=1, price=D("10"), commission=0, time=at(0), reason="entry")
    acct.fill("x", side="sell", quantity=1, price=D("10"), commission=0, time=at(1),
              reason="algo_exit")
    assert open_position_levels(led.fills, led.orders) == {}
