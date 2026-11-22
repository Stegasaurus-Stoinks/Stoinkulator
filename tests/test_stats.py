"""`MainStoinker.engine.stats`: hand-computed values, no engine run."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import MainStoinker.engine.stats as stats
from MainStoinker.engine.ledger import EquityPoint, IgnoredEvent, OrderEvent
from MainStoinker.engine.stats import account_summary, fmt4, max_drawdown, round_trip_record
from MainStoinker.engine.store import AlgoMeta

D = Decimal


@dataclass
class Trip:
    net: Decimal
    commission: Decimal = D("2.0000")
    synthetic: bool = False
    side: str = "long"
    quantity: Decimal = D("10")
    entry_time: datetime = datetime(2024, 8, 19, 7, 55)
    exit_time: datetime = datetime(2024, 8, 19, 8, 39)
    entry_price: Decimal = D("224.03")
    exit_price: Decimal = D("224.24")
    exit_reason: str = "algo_exit"
    account: str = "A"

    @property
    def gross(self) -> Decimal:
        return self.net + self.commission


def test_round_trip_record_formats_everything_as_four_decimals():
    trip = Trip(net=D("0.1"), quantity=D("10"))
    assert round_trip_record(trip) == {
        "side": "long",
        "quantity": "10.0000",
        "entry_time": "2024-08-19 07:55:00",
        "exit_time": "2024-08-19 08:39:00",
        "entry_price": "224.0300",
        "exit_price": "224.2400",
        "gross": "2.1000",
        "commission": "2.0000",
        "net": "0.1000",
        "exit_reason": "algo_exit",
        "synthetic": False,
    }
    assert list(round_trip_record(trip)) == [
        "side", "quantity", "entry_time", "exit_time", "entry_price", "exit_price", "gross",
        "commission", "net", "exit_reason", "synthetic"]


def test_no_trades():
    summary = account_summary([], D("10000"), D("10000"), equity_series=[D("10000")] * 3)
    assert summary == {
        "trade_count": 0, "wins": 0, "commission": "0.0000", "net": "0.0000",
        "final_equity": "10000.0000", "max_drawdown": "0.0000", "ignored_events": {},
        "cancelled_entries": 0, "synthetic_closes": 0,
    }
    assert account_summary([], D("5"), D("5"))["max_drawdown"] == "0.0000"  # no series at all


def test_all_losers():
    trips = [Trip(net=D("-2.5"), commission=D("1")), Trip(net=D("-0.5"), commission=D("1")),
             Trip(net=D("0"), commission=D("1"))]  # a break-even trade is not a win
    summary = account_summary(trips, D("9997"), D("10000"),
                              equity_series=[D("9997.5"), D("9997")])
    assert summary["trade_count"] == 3 and summary["wins"] == 0
    assert summary["net"] == "-3.0000" and summary["commission"] == "3.0000"
    assert summary["final_equity"] == "9997.0000"
    assert summary["max_drawdown"] == "3.0000"  # from the starting capital


def test_wins_count_only_positive_net_and_cancelled_and_ignored_pass_through():
    trips = [Trip(net=D("5")), Trip(net=D("-1")), Trip(net=D("0.0001"))]
    summary = account_summary(trips, D("10004"), D("10000"),
                              ignored_events={"close": 2, "modify_stop": 0}, cancelled_entries=4)
    assert summary["wins"] == 2
    assert summary["ignored_events"] == {"close": 2, "modify_stop": 0}
    assert summary["cancelled_entries"] == 4
    assert summary["net"] == "4.0001"


def test_max_drawdown_peak_to_trough():
    series = [D(v) for v in ("100", "110", "105", "90", "120", "118", "95", "130")]
    # peak 110 to trough 90 is 20; peak 120 to trough 95 is 25; the last peak never falls
    assert max_drawdown(series, D("100")) == D("25")
    assert max_drawdown([D("100"), D("105"), D("110")], D("100")) == D("0")  # only rises
    assert max_drawdown([], D("100")) == D("0")


def test_starting_capital_is_the_first_peak():
    # never above the starting capital: the fall is measured from it
    assert max_drawdown([D("98"), D("97"), D("99")], D("100")) == D("3")
    # with the first value as the peak instead, the drawdown would be only 1
    assert max_drawdown([D("98"), D("97"), D("99")], D("98")) == D("1")


def test_synthetic_close_is_counted_and_part_of_the_totals():
    trips = [Trip(net=D("3")), Trip(net=D("-1"), synthetic=True, exit_reason="end_of_data")]
    summary = account_summary(trips, D("10002"), D("10000"))
    assert summary["synthetic_closes"] == 1 and summary["trade_count"] == 2
    assert summary["net"] == "2.0000" and summary["commission"] == "4.0000"
    assert round_trip_record(trips[1])["synthetic"] is True
    assert round_trip_record(trips[1])["exit_reason"] == "end_of_data"


def test_fmt4_rounds_to_four_places_and_never_uses_an_exponent():
    assert fmt4(D("1E+3")) == "1000.0000"
    assert fmt4(D("0.5")) == "0.5000"
    assert fmt4(D("1E-4")) == "0.0001"


def test_stats_is_pure():
    tree = ast.parse(Path(stats.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported <= {"__future__", "collections.abc", "decimal", "typing"}
    assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"
                   for n in ast.walk(tree))


# ---- run statistics (win rate, averages, per-algo and account totals) ------------------

T0 = datetime(2024, 8, 19, 7, 55)


def trips_of(*nets: str, commission: str = "1", account: str = "A") -> list[Trip]:
    return [Trip(net=D(n), commission=D(commission), account=account) for n in nets]


def test_win_rate_and_averages():
    # nets +6 +2 -3 -1 0: two wins, two losses, one break-even
    st = stats.trade_statistics(trips_of("6", "2", "-3", "-1", "0"), D("10000"),
                                equity_series=[D("10004")])
    assert (st["trade_count"], st["wins"], st["losses"], st["break_even"]) == (5, 2, 2, 1)
    assert st["win_rate"] == D("0.4")  # wins / trades, the break-even trade counts as a trade
    assert st["total_win"] == D("8") and st["total_loss"] == D("-4")
    assert st["avg_win"] == D("4") and st["avg_loss"] == D("-2")
    assert st["net"] == D("4") and st["commission"] == D("5")
    assert st["final_equity"] == D("10004") and st["capital"] == D("10000")


def test_averages_are_rounded_to_four_places_in_the_record():
    st = stats.trade_statistics(trips_of("1", "1", "2"), D("100"))  # average win 4 / 3
    rec = stats.statistics_record(st)
    assert rec["avg_win"] == "1.3333" and rec["win_rate"] == "1.0000"
    assert rec["avg_loss"] is None
    assert rec["net"] == "4.0000" and rec["total_loss"] == "0.0000"


def test_no_trades_no_division_error():
    st = stats.trade_statistics([], D("10000"))
    assert st["trade_count"] == 0 and st["wins"] == 0 and st["losses"] == 0
    assert st["win_rate"] is None and st["avg_win"] is None and st["avg_loss"] is None
    assert st["net"] == D("0") and st["max_drawdown"] == D("0")
    assert st["final_equity"] == D("10000")  # no series: the capital
    rec = stats.statistics_record(st)
    assert rec["win_rate"] is None and rec["avg_win"] is None and rec["avg_loss"] is None
    assert rec["net"] == "0.0000" and rec["final_equity"] == "10000.0000"


def test_break_even_trade_is_neither_win_nor_loss():
    st = stats.trade_statistics(trips_of("0", "0.0000"), D("100"))
    assert (st["trade_count"], st["wins"], st["losses"], st["break_even"]) == (2, 0, 0, 2)
    assert st["win_rate"] == D("0") and st["avg_win"] is None and st["avg_loss"] is None


def test_all_losers_statistics():
    st = stats.trade_statistics(trips_of("-2.5", "-0.5"), D("10000"),
                                equity_series=[D("9998"), D("9997")])
    assert st["wins"] == 0 and st["losses"] == 2 and st["win_rate"] == D("0")
    assert st["avg_win"] is None and st["avg_loss"] == D("-1.5")
    assert st["max_drawdown"] == D("3")  # from the starting capital
    assert st["final_equity"] == D("9997")


def test_drawdown_peak_to_trough():
    series = [D(v) for v in ("10100", "10300", "10050", "10200", "9900", "10400")]
    # peak 10300 to trough 9900 is 400; the later rise to 10400 never falls again
    st = stats.trade_statistics([], D("10000"), equity_series=series)
    assert st["max_drawdown"] == D("400")
    assert st["max_drawdown"] == max_drawdown(series, D("10000"))
    assert st["final_equity"] == D("10400")


def test_synthetic_end_of_data_trade_counted_and_marked():
    trips = [Trip(net=D("3")), Trip(net=D("-1"), synthetic=True, exit_reason="end_of_data")]
    st = stats.trade_statistics(trips, D("10000"))
    assert st["synthetic_closes"] == 1
    assert (st["trade_count"], st["wins"], st["losses"]) == (2, 1, 1)  # counted in the totals
    assert st["net"] == D("2") and st["total_loss"] == D("-1")
    assert round_trip_record(trips[1])["synthetic"] is True


def _event(order_id, kind, order_type=None, account="A"):
    return OrderEvent(order_id, account, ("ref", order_id), kind, T0, "buy", D("1"), order_type,
                      None)


def test_cancelled_entries_are_derived_from_order_events():
    events = [
        _event(1, "placed", "entry"), _event(2, "placed", "stop"), _event(1, "cancelled"),
        _event(2, "cancelled"),                                # a cancelled stop is not an entry
        _event(3, "placed", "entry"), _event(3, "modified"),  # modified only: not cancelled
        _event(4, "placed", "entry"), _event(4, "cancelled"), _event(4, "cancelled"),  # once
        _event(1, "placed", "entry", account="B"),             # same id, other algo: not cancelled
        _event(9, "cancelled"),                                # never placed an entry
    ]
    assert stats.cancelled_entry_counts(events) == {"A": 2}
    assert stats.cancelled_entry_counts([]) == {}


def _point(account, equity, minute=0):
    value = D(equity)
    return EquityPoint(T0.replace(minute=minute), account, value, D("0"), D("0"), D("0"), value)


def test_account_aggregates_algos():
    run = SimpleNamespace(
        algos=[AlgoMeta("A", "Algo", "AAPL", "backtest", D("1000")),
               AlgoMeta("B", "Algo", "MSFT", "backtest", D("500"))],
        round_trips=[*trips_of("10", "-4", account="A"),
                     *trips_of("-1", account="B"),
                     Trip(net=D("2"), commission=D("1"), account="B", synthetic=True)],
        order_events=[_event(1, "placed", "entry"), _event(1, "cancelled"),
                      _event(5, "placed", "entry", account="B"),
                      _event(5, "cancelled", account="B")],
        ignored_events=[IgnoredEvent("A", 1, "close", T0), IgnoredEvent("B", 2, "close", T0),
                        IgnoredEvent("B", 3, "take_profit", T0)],
        equity=[_point("A", "1000", 0), _point("A", "1008", 1), _point("A", "996", 2),
                _point("B", "500", 0), _point("B", "499", 1), _point("B", "501", 2),
                _point(None, "1500", 0), _point(None, "1507", 1), _point(None, "1497", 2)],
    )
    run.equity_series = lambda account=None: [p for p in run.equity if p.account == account]
    result = stats.run_statistics(run, ("close", "modify_stop", "take_profit"))
    a, b, total = result["accounts"]["A"], result["accounts"]["B"], result["total"]
    assert list(result["accounts"]) == ["A", "B"]
    assert (a["trade_count"], a["wins"], a["losses"], a["net"]) == (2, 1, 1, D("6"))
    assert a["max_drawdown"] == D("12") and a["final_equity"] == D("996")  # peak 1008 -> 996
    assert a["cancelled_entries"] == 1 and a["capital"] == D("1000")
    assert a["ignored_events"] == {"close": 1, "modify_stop": 0, "take_profit": 0}
    assert (b["trade_count"], b["wins"], b["losses"], b["net"]) == (2, 1, 1, D("1"))
    assert b["synthetic_closes"] == 1 and b["max_drawdown"] == D("1")
    assert b["ignored_events"] == {"close": 1, "modify_stop": 0, "take_profit": 1}
    # the total: summed capital, the stored total series, every trip
    assert total["capital"] == D("1500")
    assert total["trade_count"] == 4 and total["wins"] == 2 and total["losses"] == 2
    assert total["net"] == a["net"] + b["net"] == D("7")
    assert total["commission"] == D("4")
    assert total["max_drawdown"] == D("10") and total["final_equity"] == D("1497")
    assert total["synthetic_closes"] == 1 and total["cancelled_entries"] == 2
    assert total["ignored_events"] == {"close": 2, "modify_stop": 0, "take_profit": 1}
    assert total["avg_win"] == D("6") and total["avg_loss"] == D("-2.5")  # (10+2)/2, (-4-1)/2


def test_cancelled_entry_counts_equal_the_paper_brokers_own_count():
    """The derivation from order events must agree with `PaperBroker.cancelled_entries`, which
    is what `ledger.json` records, in each way an entry can be cancelled (and in the ways a
    stop or target is cancelled without any entry being so)."""
    from tests.test_paper_broker import bar, make, open_pos, opened

    def counted(ledger):
        return stats.cancelled_entry_counts(ledger.orders).get("a", 0)

    # an entry that never fills (session ends), with its stop and a target working
    ledger, broker = make()
    ref = open_pos(broker)
    broker.set_take_profit(ref, 110.0, 10)
    broker.on_bar("AAPL", bar(0, 100.0, day=1))
    assert broker.cancelled_entries == 1 == counted(ledger)
    assert [e.kind for e in ledger.orders].count("cancelled") == 3  # entry, stop, target

    # closed while still pending
    ledger, broker = make()
    broker.close_position(open_pos(broker), price=100.0, time=bar(0, 100.0).date)
    assert broker.cancelled_entries == 1 == counted(ledger)

    # still pending when the data ends, twice over
    ledger, broker = make()
    open_pos(broker)
    broker.on_end({})
    broker.on_end({})
    assert broker.cancelled_entries == 1 == counted(ledger)

    # a filled entry whose stop or target is cancelled is not a cancelled entry
    for high, low in ((111.0, 99.0), (105.0, 94.0)):
        ledger, broker = make()
        opened(broker, stop=95.0, tp=110.0)
        broker.on_bar("AAPL", bar(2, 100.0, high, low))
        assert [e.kind for e in ledger.orders].count("cancelled") == 1
        assert broker.cancelled_entries == 0 == counted(ledger)

    # a filled entry closed by the algo cancels its stop, not an entry
    ledger, broker = make()
    ref = opened(broker)
    broker.close_position(ref, price=100.0, time=bar(2, 100.0).date)
    assert broker.cancelled_entries == 0 == counted(ledger)

    # one cancelled and one filled position on the same account
    ledger, broker = make()
    first = open_pos(broker)
    second = open_pos(broker, minute=0)
    broker.close_position(first, price=100.0, time=bar(0, 100.0).date)
    broker.on_bar("AAPL", bar(1, 100.0, 100.5, 99.5))
    assert broker.position_state(second) == "open"
    assert broker.cancelled_entries == 1 == counted(ledger)


def test_ledger_document_names_an_unknown_idname_instead_of_stopiteration():
    import pytest

    from MainStoinker.engine.ledger import Ledger
    from MainStoinker.engine.paper_broker import CostModel
    from MainStoinker.engine.report import ledger_document

    ledger = Ledger()
    ledger.open_account("a", "AAPL", D("1000"))
    free = CostModel(D(0), D(0), D(0))
    with pytest.raises(ValueError, match="unknown idname 'nope'"):
        ledger_document(ledger, ["a", "nope"], fills="realistic", costs=free, capital=D("1000"))
    assert ledger_document(ledger, ["a"], fills="realistic", costs=free, capital=D("1000"))["accounts"]
