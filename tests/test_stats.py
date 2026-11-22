"""`MainStoinker.engine.stats`: hand-computed values, no engine run."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import MainStoinker.engine.stats as stats
from MainStoinker.engine.stats import account_summary, fmt4, max_drawdown, round_trip_record

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
