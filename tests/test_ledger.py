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
    rec.ignored(1, time=at(6), kind="close")
    rec.ignored(1, time=at(7), kind="close")
    assert led.ignored_closes() == 2
    assert led.ignored_closes("algo1") == 2
    assert len(led.round_trips) == 1
    assert len(led.fills) == 2  # counted, not filled


def test_end_of_data_close_is_synthetic_and_flagged():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=1.0, time=at(0), reason="entry")
    closed = led.finish(at(9), {"AAPL": 12.0}, commission=lambda name, q: Decimal("1"))
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


# --- independent hand-computed checks (added by the tester) ------------------


def test_short_open_position_cash_and_value_conventions():
    led, rec = one_account(1000)
    rec.fill(1, side="sell", quantity=10, price=20.0, commission=1.0, time=at(0), reason="entry")
    assert led.cash() == Decimal("1199.0000")  # 1000 + 200 proceeds - 1 commission
    (pt, _) = led.mark(at(1), {"AAPL": 18.0})
    assert pt.cash == Decimal("1199.0000")
    assert pt.equity == Decimal("1019.0000")  # 1199 - 10 * 18 owed back
    assert pt.unrealised == Decimal("19.0000")  # (20 - 18) * 10 - 1
    assert pt.equity == Decimal("1000.0000") + pt.realised + pt.unrealised
    (pt, _) = led.mark(at(2), {"AAPL": 25.0})
    assert pt.equity == Decimal("949.0000")  # 1199 - 250
    assert pt.unrealised == Decimal("-51.0000")


def test_commission_on_entry_and_exit_reduces_net_and_cash():
    led, rec = one_account(1000)
    round_trip(rec, 1, qty=10, entry=10.0, exit_=12.0, commission=1.5)
    (rt,) = led.round_trips
    assert (rt.gross, rt.commission, rt.net) == (
        Decimal("20.0000"), Decimal("3.0000"), Decimal("17.0000"))
    assert led.cash() == Decimal("1017.0000")
    assert led.equity() == Decimal("1017.0000")
    assert led.realised_pnl() == Decimal("17.0000")


def test_loss_larger_than_capital_does_not_raise():
    led, rec = one_account(100)
    round_trip(rec, 1, qty=100, entry=100.0, exit_=1.0, reason="stop")
    assert led.realised_pnl() == Decimal("-9900.0000")
    assert led.equity() == Decimal("-9800.0000")


def test_round_trip_opened_and_closed_in_one_bar():
    led, rec = one_account()
    round_trip(rec, 1, entry=10.0, exit_=10.5, qty=10, t_in=3, t_out=3)
    (rt,) = led.round_trips
    assert rt.duration == timedelta(0)
    assert rt.gross == Decimal("5.0000")
    assert not led.open_positions()


def test_sequential_round_trips_on_one_ref_are_independent():
    led, rec = one_account()
    for i in range(3):
        round_trip(rec, 7, long=bool(i % 2 == 0), entry=10.0, exit_=11.0, qty=10,
                   t_in=i * 10, t_out=i * 10 + 2)
    assert [rt.side for rt in led.round_trips] == ["long", "short", "long"]
    assert [rt.gross for rt in led.round_trips] == [
        Decimal("10.0000"), Decimal("-10.0000"), Decimal("10.0000")]
    assert [rt.duration for rt in led.round_trips] == [timedelta(minutes=2)] * 3
    assert led.realised_pnl() == Decimal("10.0000")


def test_finish_short_with_commission_callable_and_without():
    led, rec = one_account(1000)
    rec.fill(1, side="sell", quantity=10, price=20.0, commission=1.0, time=at(0), reason="entry")
    (rt,) = led.finish(at(1), {"AAPL": 18.0}, commission=lambda name, q: q * Decimal("0.1"))
    assert (rt.side, rt.gross, rt.commission, rt.net) == (
        "short", Decimal("20.0000"), Decimal("2.0000"), Decimal("18.0000"))
    assert led.equity() == Decimal("1018.0000")

    led2, rec2 = one_account(1000)
    rec2.fill(1, side="sell", quantity=10, price=20.0, commission=1.0, time=at(0), reason="entry")
    (rt2,) = led2.finish(at(1), {"AAPL": 18.0})
    assert rt2.commission == Decimal("1.0000")
    assert rt2.net == Decimal("19.0000")
    assert rt2.synthetic


def test_finish_twice_and_mark_after_finish_are_harmless():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    assert len(led.finish(at(1), {"AAPL": 11.0})) == 1
    assert led.finish(at(2), {"AAPL": 50.0}) == []
    assert len(led.round_trips) == 1
    (pt, _) = led.mark(at(3), {"AAPL": 99.0})
    assert pt.equity == Decimal("1010.0000")
    assert pt.unrealised == Decimal("0.0000")


def test_mark_with_missing_symbol_keeps_last_mark():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    assert led.mark(at(1), {"MSFT": 5.0})[0].equity == Decimal("1000.0000")  # never marked
    assert led.mark(at(2), {"AAPL": 12.0})[0].equity == Decimal("1020.0000")
    assert led.mark(at(3), {})[0].equity == Decimal("1020.0000")  # keeps the last mark


def test_equity_identity_holds_at_every_mark_for_longs_and_shorts():
    rng = random.Random(11)
    led, rec = one_account(5000)
    ref, open_ = 0, None
    for t in range(300):
        if open_ is None:
            ref += 1
            open_ = (rng.random() < 0.5, rng.randint(1, 50))
            rec.fill(ref, side="buy" if open_[0] else "sell", quantity=open_[1],
                     price=round(rng.uniform(1, 200), 2), commission=rng.choice((0, 0.5, 1.25)),
                     time=at(t), reason="entry")
        elif rng.random() < 0.4:
            rec.fill(ref, side="sell" if open_[0] else "buy", quantity=open_[1],
                     price=round(rng.uniform(1, 200), 2), commission=rng.choice((0, 0.5, 1.25)),
                     time=at(t), reason="stop")
            open_ = None
        (pt, total) = led.mark(at(t), {"AAPL": round(rng.uniform(1, 200), 2)})
        assert pt.equity == Decimal("5000") + pt.realised + pt.unrealised
        assert total.equity == pt.equity


def test_money_accepts_decimal_int_and_float_noise():
    assert money(0.1 + 0.2) == Decimal("0.3000")
    assert money(5) == Decimal("5.0000")
    assert money(Decimal("1.23455")) == Decimal("1.2346")  # tie goes to even
    assert money(Decimal("1.23465")) == Decimal("1.2346")
    assert money(-2.5e-5) == Decimal("0.0000")
    assert money(1e12 + 0.25) == Decimal("1000000000000.2500")
    for x in (-0.03125, -0.09375, -1.00005, 99999.99995, -123456.78905):
        assert float(money(x)) == round(x, 4), x


def test_pandas_timestamps_give_a_duration():
    pd = pytest.importorskip("pandas")
    led, rec = one_account()
    rec.fill(1, side="buy", quantity=1, price=1.0, commission=0,
             time=pd.Timestamp("2024-01-02 09:30"), reason="entry")
    rec.fill(1, side="sell", quantity=1, price=2.0, commission=0,
             time=pd.Timestamp("2024-01-02 09:45"), reason="target")
    assert led.round_trips[0].duration == timedelta(minutes=15)


def test_invalid_fill_messages_and_duplicate_account():
    led, rec = one_account()
    with pytest.raises(ValueError, match="positive"):
        rec.fill(1, side="buy", quantity=0, price=1.0, commission=0, time=at(0), reason="entry")
    with pytest.raises(ValueError, match="side"):
        rec.fill(1, side="hold", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    rec.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    with pytest.raises(ValueError, match="opposite"):
        rec.fill(1, side="sell", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    with pytest.raises(ValueError, match="opposite"):
        rec.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="stop")
    with pytest.raises(ValueError, match="already exists"):
        led.open_account("algo1", "AAPL", 1)


def test_records_are_frozen_dataclasses():
    import dataclasses

    led, rec = one_account()
    round_trip(rec, 1)
    for obj in (led.round_trips[0], led.fills[0], led.accounts[0]):
        assert dataclasses.is_dataclass(obj)
        with pytest.raises(dataclasses.FrozenInstanceError):
            obj.account = "x"  # type: ignore[misc]


def test_fresh_ledgers_share_no_state():
    _, rec = one_account()
    round_trip(rec, 1)
    b = Ledger()
    assert b.fills == () and b.round_trips == () and b.accounts == ()
    rec_b = b.open_account("algo1", "AAPL", 1)
    assert rec_b.order_placed(1, time=at(0), side="buy", quantity=1, order_type="market") == 1


# --- review-fix tests: partial exits, finish, ignored events ------------------


@pytest.mark.parametrize("long", [True, False])
def test_partial_exit_keeps_equity_identity(long):
    led, rec = one_account(1000)
    open_side, close_side = ("buy", "sell") if long else ("sell", "buy")
    rec.fill(1, side=open_side, quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    exit_price = 12.0 if long else 8.0
    rec.fill(1, side=close_side, quantity=4, price=exit_price, commission=0, time=at(1),
             reason="target")
    assert led.realised_pnl() == Decimal("8.0000")  # 4 shares x 2.00, realised at the fill
    mark = 11.0 if long else 9.0
    (pt, _) = led.mark(at(2), {"AAPL": mark})
    assert pt.equity == Decimal("1014.0000")
    assert pt.realised + pt.unrealised == Decimal("14.0000")
    assert pt.equity == Decimal("1000") + pt.realised + pt.unrealised
    assert not led.round_trips  # still open


def test_partial_exit_commission_released_proportionally_and_round_trip_is_one_record():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=1.0, time=at(0), reason="entry")
    rec.fill(1, side="sell", quantity=4, price=12.0, commission=0.5, time=at(1), reason="target")
    # pending 1.5 -> 4/10 released = 0.6; realised = 8 - 0.6
    assert led.realised_pnl() == Decimal("7.4000")
    rec.fill(1, side="sell", quantity=6, price=13.0, commission=0.5, time=at(2), reason="stop")
    (rt,) = led.round_trips
    assert (rt.quantity, rt.gross, rt.commission, rt.net) == (
        Decimal("10.0000"), Decimal("26.0000"), Decimal("2.0000"), Decimal("24.0000"))
    assert rt.exit_price == Decimal("12.6000")
    assert rt.exit_reason == "stop"
    assert led.realised_pnl() == rt.net
    assert led.equity() == Decimal("1024.0000")


@pytest.mark.parametrize("seed", range(5))
def test_equity_identity_exact_with_entries_partial_exits_and_commissions(seed):
    rng = random.Random(seed)
    capital = Decimal("5000")
    led, rec = one_account(5000)
    ref, state = 0, None  # state = [long, open_qty]
    for t in range(400):
        comm = rng.choice((0, 0.35, 1.0, 1.25))
        px = round(rng.uniform(1, 200), 2)
        if state is None:
            ref += 1
            state = [rng.random() < 0.5, rng.randint(1, 40)]
            rec.fill(ref, side="buy" if state[0] else "sell", quantity=state[1], price=px,
                     commission=comm, time=at(t), reason="entry")
        elif rng.random() < 0.3:
            extra = rng.randint(1, 30)
            rec.fill(ref, side="buy" if state[0] else "sell", quantity=extra, price=px,
                     commission=comm, time=at(t), reason="entry")
            state[1] += extra
        elif rng.random() < 0.7:
            q = rng.randint(1, state[1])
            rec.fill(ref, side="sell" if state[0] else "buy", quantity=q, price=px,
                     commission=comm, time=at(t), reason="algo_exit")
            state[1] -= q
            if state[1] == 0:
                state = None
        if state is None:
            assert led.realised_pnl() == sum((rt.net for rt in led.round_trips), Decimal(0))
        (pt, total) = led.mark(at(t), {"AAPL": round(rng.uniform(1, 200), 2)})
        assert pt.equity == capital + pt.realised + pt.unrealised
        assert pt.equity == pt.cash + pt.position_value
        assert total.equity == pt.equity


def test_equity_point_has_position_value():
    led, rec = one_account(1000)
    rec.fill(1, side="sell", quantity=10, price=20.0, commission=0, time=at(0), reason="entry")
    (pt, total) = led.mark(at(1), {"AAPL": 18.0})
    assert pt.position_value == Decimal("-180.0000")
    assert total.position_value == Decimal("-180.0000")
    assert pt.equity == pt.cash + pt.position_value


def test_finish_appends_final_equity_point_matching_equity():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=1.0, time=at(0), reason="entry")
    led.mark(at(1), {"AAPL": 11.0})
    led.finish(at(2), {"AAPL": 12.0}, commission=lambda name, q: Decimal("1"))
    last_acct, last_total = led.equity_points[-2:]
    assert (last_acct.time, last_acct.account, last_total.account) == (at(2), "algo1", None)
    assert last_acct.equity == last_total.equity == led.equity() == Decimal("1018.0000")
    assert last_acct.position_value == Decimal("0.0000")
    assert last_acct.unrealised == Decimal("0.0000")
    assert last_acct.realised == Decimal("18.0000")


def test_finish_replaces_points_already_stored_at_the_same_time():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    led.mark(at(1), {"AAPL": 11.0})
    led.finish(at(1), {"AAPL": 11.0})  # same bar as the last mark: replace, not duplicate
    assert [p.time for p in led.equity_points] == [at(1), at(1)]
    assert led.equity_points[0].position_value == Decimal("0.0000")
    # Nothing open and identical values at the same time: still no duplicate.
    led2, _ = one_account(1000)
    led2.mark(at(1), {"AAPL": 5.0})
    led2.finish(at(1), {"AAPL": 5.0})
    assert len(led2.equity_points) == 2


def test_finish_commission_is_per_account():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "AAPL", 1000)
    a.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    b.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    seen = []

    def per_account(idname, qty):
        seen.append((idname, qty))
        return Decimal("1") if idname == "a" else Decimal("3")

    led.finish(at(1), {"AAPL": 10.0}, commission=per_account)
    assert seen == [("a", Decimal("10.0000")), ("b", Decimal("10.0000"))]
    assert led.realised_pnl("a") == Decimal("-1.0000")
    assert led.realised_pnl("b") == Decimal("-3.0000")


def test_end_of_data_reason_fill_is_synthetic_even_when_recorded_directly():
    led, rec = one_account()
    rec.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    rec.fill(1, side="sell", quantity=1, price=2.0, commission=0, time=at(1), reason="end_of_data")
    assert led.fills[-1].synthetic
    assert led.round_trips[0].synthetic
    assert not led.fills[0].synthetic


def test_finish_without_any_close_raises_naming_account_and_symbol():
    led = Ledger()
    a = led.open_account("algoA", "AAPL", 1000)
    a.fill(1, side="buy", quantity=1, price=1.0, commission=0, time=at(0), reason="entry")
    with pytest.raises(ValueError, match="algoA.*AAPL"):
        led.finish(at(1), {"MSFT": 5.0})
    assert led.open_positions()  # nothing was closed
    assert led.round_trips == ()


def test_finish_uses_last_mark_when_close_is_missing():
    led, rec = one_account(1000)
    rec.fill(1, side="buy", quantity=10, price=10.0, commission=0, time=at(0), reason="entry")
    led.mark(at(1), {"AAPL": 12.0})
    (rt,) = led.finish(at(2), {})
    assert rt.exit_price == Decimal("12.0000")
    assert led.equity() == Decimal("1020.0000")


def test_ignored_events_are_recorded_and_counted_per_kind_and_account():
    led = Ledger()
    a = led.open_account("a", "AAPL", 1000)
    b = led.open_account("b", "MSFT", 1000)
    a.ignored(1, time=at(1), kind="close")
    a.ignored(1, time=at(2), kind="modify_stop")
    a.ignored(2, time=at(3), kind="take_profit")
    b.ignored(1, time=at(4), kind="close")
    assert led.ignored_count() == 4
    assert led.ignored_count("close") == 2
    assert led.ignored_count("modify_stop", "a") == 1
    assert led.ignored_count("take_profit", "b") == 0
    assert led.ignored_count(account="a") == 3
    assert led.ignored_closes("b") == 1
    ev = led.ignored_events[0]
    assert (ev.account, ev.broker_ref, ev.kind, ev.time) == ("a", 1, "close", at(1))
    with pytest.raises(ValueError, match="kind"):
        a.ignored(1, time=at(5), kind="bogus")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        led.ignored_count(account="nope")
