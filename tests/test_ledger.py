"""Unit tests for the pure accounting ledger."""

import ast
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from MainStoinker.engine import ledger as ledger_mod
from MainStoinker.engine.ledger import Ledger, commission_per_share, money

T0 = datetime(2024, 1, 2, 9, 30)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def round_trip(rec, ref, *, long=True, qty=100, entry=10.0, exit_=11.0, commission=0.0,
               reason="target", t_in=0, t_out=5):
    open_side, close_side = ("buy", "sell") if long else ("sell", "buy")
    rec.fill(ref, side=open_side, quantity=qty, price=entry, commission=commission,
             time=at(t_in), reason="entry")
    rec.fill(ref, side=close_side, quantity=qty, price=exit_, commission=commission,
             time=at(t_out), reason=reason)


def one_account(capital=10_000):
    led = Ledger()
    return led, led.open_account("algo1", "AAPL", capital)


def test_long_round_trip_profit():
    led, rec = one_account()
    round_trip(rec, 1, entry=10.0, exit_=11.5, qty=100)
    (rt,) = led.round_trips
    assert rt.side == "long"
    assert rt.gross == Decimal("150.0000")
    assert rt.net == Decimal("150.0000")
    assert rt.duration == timedelta(minutes=5)
    assert rt.exit_reason == "target"
    assert not rt.synthetic
    assert led.realised_pnl() == Decimal("150.0000")


def test_short_round_trip_profit_is_entry_minus_exit():
    led, rec = one_account()
    round_trip(rec, 1, long=False, entry=20.0, exit_=18.5, qty=10, reason="stop")
    (rt,) = led.round_trips
    assert rt.side == "short"
    assert rt.gross == Decimal("15.0000")
    assert rt.exit_reason == "stop"
    round_trip(rec, 2, long=False, entry=20.0, exit_=21.0, qty=10)
    assert led.round_trips[1].gross == Decimal("-10.0000")


def test_profit_scales_with_quantity():
    led, rec = one_account()
    for ref, qty in enumerate((1, 10, 250), start=1):
        round_trip(rec, ref, qty=qty, entry=50.0, exit_=50.25)
    assert [rt.gross for rt in led.round_trips] == [
        Decimal("0.2500"), Decimal("2.5000"), Decimal("62.5000")]


def test_commission_per_share_with_minimum():
    # Pure helper chosen over a ledger method: the cost model lives in the broker.
    assert commission_per_share(100, 0.005, 1.0) == Decimal("1.0000")  # 0.50 -> minimum
    assert commission_per_share(1000, 0.005, 1.0) == Decimal("5.0000")
    # And the ledger records whatever the broker charged, net = gross - commission.
    led, rec = one_account()
    c = float(commission_per_share(1000, 0.005, 1.0))
    round_trip(rec, 1, qty=1000, entry=10.0, exit_=10.1, commission=c)
    (rt,) = led.round_trips
    assert rt.gross == Decimal("100.0000")
    assert rt.commission == Decimal("10.0000")
    assert rt.net == Decimal("90.0000")


def test_decimal_matches_python_round_4():
    ties = [0.00005, 0.00015, 1.00005, 0.03125, 0.09375, 2.5e-5, 123.45675, 0.12345, -0.03125]
    rng = random.Random(7)
    values = ties + [rng.uniform(-1000, 1000) for _ in range(2000)]
    values += [round(rng.uniform(0, 500), 5) for _ in range(2000)]
    for x in values:
        assert float(money(x)) == round(x, 4), x
    assert money(0.03125) == Decimal("0.0312")  # exact tie goes to even
    assert money(0.09375) == Decimal("0.0938")


def test_cash_and_equity_after_mark():
    led, rec = one_account(10_000)
    rec.fill(1, side="buy", quantity=100, price=10.0, commission=1.0, time=at(0), reason="entry")
    assert led.cash() == Decimal("8999.0000")
    pts = led.mark(at(1), {"AAPL": 10.5})
    acct_pt = next(p for p in pts if p.account == "algo1")
    assert acct_pt.cash == Decimal("8999.0000")
    assert acct_pt.equity == Decimal("10049.0000")  # 8999 + 100 * 10.5
    round_trip(rec, 2, qty=10, entry=10.0, exit_=12.0, commission=0.0)
    assert led.cash() == Decimal("9019.0000")


def test_flat_algo_equity_is_capital():
    led, _ = one_account(12_345.5)
    pts = led.mark(at(1), {"AAPL": 99.0})
    assert [p.equity for p in pts] == [Decimal("12345.5000")] * 2
    assert led.equity("algo1") == Decimal("12345.5000")


def test_second_close_is_ignored_and_counted():
    led, rec = one_account()
    round_trip(rec, 1)
    rec.ignored_close(1)
    rec.ignored_close(1)
    assert led.ignored_closes() == 2
    assert led.ignored_closes("algo1") == 2
    assert len(led.round_trips) == 1
    assert len(led.fills) == 2  # counted, not filled


def test_end_of_data_close_is_synthetic_and_flagged():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=1.0, time=at(0), reason="entry")
    closed = led.finish(at(9), {"AAPL": 12.0}, commission=lambda q: Decimal("1"))
    (rt,) = closed
    assert led.round_trips == (rt,)
    assert rt.synthetic
    assert rt.exit_reason == "end_of_data"
    assert rt.exit_price == Decimal("12.0000")
    assert rt.gross == Decimal("20.0000")
    assert rt.commission == Decimal("2.0000")
    assert rt.net == Decimal("18.0000")
    assert led.fills[-1].synthetic
    assert not led.open_positions()
    assert led.equity() == Decimal("1018.0000")


def test_open_position_unrealised_pnl():
    led, rec = one_account()
    rec.fill(1, side="sell", quantity=10, price=20.0, commission=0.5, time=at(0), reason="entry")
    led.mark(at(1), {"AAPL": 19.0})
    assert led.unrealised_pnl() == Decimal("9.5000")  # short gains 10, minus commission paid
    assert led.realised_pnl() == Decimal("0.0000")
    led.mark(at(2), {"AAPL": 21.0})
    assert led.unrealised_pnl() == Decimal("-10.5000")
    (pos,) = led.open_positions()
    assert (pos.side, pos.quantity, pos.average_price) == ("short", Decimal("10.0000"),
                                                           Decimal("20.0000"))
    # Identity: equity = capital + realised + unrealised.
    assert led.equity() == Decimal("10000.0000") + led.realised_pnl() + led.unrealised_pnl()


def test_account_equity_is_sum_of_algos():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "MSFT", 2000)
    a.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    b.fill(1, side="sell", quantity=5, price=100.0, commission=0, time=at(0), reason="entry")
    pts = led.mark(at(1), {"AAPL": 11.0, "MSFT": 98.0})
    by_acct = {p.account: p for p in pts}
    assert by_acct["a"].equity == Decimal("1010.0000")
    assert by_acct["b"].equity == Decimal("2010.0000")
    assert by_acct[None].equity == by_acct["a"].equity + by_acct["b"].equity
    assert by_acct[None].cash == by_acct["a"].cash + by_acct["b"].cash
    assert len(led.equity_points) == 3


def test_two_accounts_same_ref_do_not_collide():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "AAPL", 1000)
    a.fill(1, side="buy", quantity=1, price=10.0, commission=0, time=at(0), reason="entry")
    b.fill(1, side="sell", quantity=2, price=10.0, commission=0, time=at(0), reason="entry")
    assert len(led.open_positions()) == 2
    a.fill(1, side="sell", quantity=1, price=12.0, commission=0, time=at(1), reason="target")
    assert len(led.open_positions()) == 1
    (pos,) = led.open_positions()
    assert (pos.account, pos.side) == ("b", "short")
    assert led.realised_pnl("a") == Decimal("2.0000")
    assert led.realised_pnl("b") == Decimal("0.0000")


def test_ids_are_run_wide_and_order_events_recorded():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "MSFT", 1000)
    o1 = a.order_placed(1, time=at(0), side="buy", quantity=10, order_type="market")
    o2 = b.order_placed(1, time=at(0), side="buy", quantity=10, order_type="market")
    assert (o1, o2) == (1, 2)
    a.order_modified(o1, time=at(1), price=9.5)
    a.order_cancelled(o1, time=at(2))
    assert [e.kind for e in led.orders] == ["placed", "placed", "modified", "cancelled"]
    assert led.orders[2].broker_ref == 1
    with pytest.raises(KeyError):
        b.order_cancelled(o1, time=at(3))  # another account's order
    f1 = a.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    f2 = b.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    assert (f1, f2) == (1, 2)


def test_invalid_fills_are_rejected():
    led, rec = one_account()
    with pytest.raises(ValueError):
        rec.fill(1, side="sell", quantity=1, price=1.0, commission=0, time=at(0), reason="stop")
    rec.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    with pytest.raises(ValueError):
        rec.fill(1, side="sell", quantity=2, price=1.0, commission=0, time=at(1), reason="stop")
    with pytest.raises(ValueError):
        rec.fill(1, side="sell", quantity=1, price=1.0, commission=0, time=at(1), reason="bogus")


def test_ledger_imports_no_pandas_or_numpy_and_has_no_module_state():
    path = Path(ledger_mod.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            else:
                raise AssertionError("ledger must not use relative imports")
    assert not imported & {"pandas", "numpy", "MainStoinker", "ibapi", "sqlite3"}
    # No module-level mutable containers.
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            assert not isinstance(value, ast.List | ast.Dict | ast.Set), ast.dump(node)
