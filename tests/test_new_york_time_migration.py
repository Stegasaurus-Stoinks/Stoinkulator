"""The move from Pacific to New York data time changed times and nothing else (issue 132).

`tests/fixtures/pacific_goldens/` holds the golden files as they were before the move
(`golden/` = the legacy trade goldens, `golden_paper/` = the realistic-fill goldens). Shifting
every old time string by three hours must give exactly the current golden, so the trade count,
the order of trades, prices, quantities and nets are the same trades. For the paper goldens the
equity hash is the one field left out: it covers the times, so it changed on purpose (the
series itself is checked against the old hash by the script quoted in the PR).

The data files are checked against their own `time` column (epoch seconds).
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

HERE = Path(__file__).parent
OLD = HERE / "fixtures" / "pacific_goldens"
OFFLINE_DATA = HERE.parent / "OfflineData"
NEW_YORK = ZoneInfo("America/New_York")
SHIFT = timedelta(hours=3)
TIME_TEXT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
CASES = [
    "elliot_aapl", "elliot_msft", "elliot_tsla", "emacrossing_aapl",
    "fridayfrenzy_tsla", "functiontest_msft", "fvg_aapl", "openingrangecandle_aapl",
]


def shifted(node: object) -> object:
    """`node` with every `YYYY-MM-DD HH:MM:SS` string moved three hours later."""
    if isinstance(node, dict):
        return {k: shifted(v) for k, v in node.items()}
    if isinstance(node, list):
        return [shifted(v) for v in node]
    if isinstance(node, str) and TIME_TEXT.match(node):
        return (datetime.fromisoformat(node) + SHIFT).strftime("%Y-%m-%d %H:%M:%S")
    return node


def load(folder: str, case: str) -> object:
    return json.loads((HERE / folder / f"{case}.json").read_text(encoding="utf-8"))


def load_old(folder: str, case: str) -> object:
    return json.loads((OLD / folder / f"{case}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES)
def test_legacy_golden_is_the_old_one_three_hours_later(case: str) -> None:
    old, new = load_old("golden", case), load("golden", case)
    assert len(new) == len(old) > 0
    assert shifted(old) == new
    # The shift must actually have been applied (guards against an unchanged copy of the old file).
    assert old != new
    assert [t["openTime"] for t in new] == sorted(t["openTime"] for t in new)


@pytest.mark.parametrize("case", CASES)
def test_paper_golden_is_the_old_one_three_hours_later(case: str) -> None:
    old, new = load_old("golden_paper", case), load("golden_paper", case)
    assert len(new["trades"]) == len(old["trades"]) == new["summary"]["trade_count"]
    assert old.pop("equity_sha256") != new.pop("equity_sha256")  # times are part of the hash
    assert shifted(old) == new
    assert old["trades"] != new["trades"]


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_offline_data_dates_are_new_york_time_of_the_epoch_column(symbol: str) -> None:
    with (OFFLINE_DATA / f"_{symbol}_offlinedata_").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) > 2000
    for row in rows:
        expected = datetime.fromtimestamp(int(row["time"]), NEW_YORK).strftime("%Y-%m-%d %H:%M:%S")
        assert row["date"] == expected, row
    first_of_day: dict[str, str] = {}
    last_of_day: dict[str, str] = {}
    for row in rows:
        day, clock = row["date"].split(" ")
        first_of_day.setdefault(day, clock)
        last_of_day[day] = clock
    assert set(first_of_day.values()) == {"09:30:00"}
    assert set(last_of_day.values()) == {"15:59:00"}
