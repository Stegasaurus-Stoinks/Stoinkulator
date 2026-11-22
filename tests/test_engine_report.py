"""`trade_records` lives in the engine package; the harness re-exports it."""
from types import SimpleNamespace

import pandas as pd

from MainStoinker.engine import report
from tests import harness


def test_trade_records_is_the_harness_function():
    assert harness.trade_records is report.trade_records


def test_trade_records_keys_and_types():
    t0 = pd.Timestamp("2024-08-19 07:00:00")
    closed = SimpleNamespace(
        tradeID="x0", symbol="AAPL", volume=10, direction=True, openTime=t0, openPrice=100.123456,
        closeTime=t0 + pd.Timedelta(minutes=5), closePrice=101.5, status="Closed",
    )
    still_open = SimpleNamespace(
        tradeID="x1", symbol="AAPL", volume=10, direction=0, openTime=t0, openPrice=99,
    )
    records = report.trade_records(SimpleNamespace(trades=[closed, still_open]))
    keys = ["id", "symbol", "volume", "direction", "openTime", "openPrice", "closeTime",
            "closePrice", "status"]
    assert [list(r) for r in records] == [keys, keys]
    assert records[0] == {
        "id": "x0", "symbol": "AAPL", "volume": 10, "direction": 1,
        "openTime": "2024-08-19 07:00:00", "openPrice": 100.1235,
        "closeTime": "2024-08-19 07:05:00", "closePrice": 101.5, "status": "Closed",
    }
    assert type(records[0]["direction"]) is int
    assert records[1]["closeTime"] == "None"
    assert records[1]["closePrice"] is None
    assert records[1]["status"] is None
    assert records[1]["openPrice"] == 99.0
    assert report.trade_records(SimpleNamespace(trades=[])) == []
