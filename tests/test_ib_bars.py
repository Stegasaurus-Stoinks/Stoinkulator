"""`MainStoinker/ib/bars.py`: IB historical bar -> engine `Bar`, and the time conversion."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.feed import Bar
from MainStoinker.ib import bars as bars_module
from MainStoinker.ib.bars import DATA_TIME_ZONE, BadBar, bar_from_ib
from MainStoinker.ib.events import HistoricalBar

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = ("_AAPL_offlinedata_", "_MSFT_offlinedata_", "_TSLA_offlinedata_")


def epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def ib_bar(
    date: object = "1723815000", o: float = 10.0, h: float = 11.0, low: float = 9.0,
    c: float = 10.5, volume: object = Decimal(100),
) -> HistoricalBar:
    return HistoricalBar(0, date, o, h, low, c, volume, Decimal(0), 0)  # type: ignore[arg-type]


def test_data_time_zone_is_new_york() -> None:
    assert DATA_TIME_ZONE == "America/New_York"


def test_bar_time_reproduces_committed_offline_data() -> None:
    total = 0
    for name in OFFLINE:
        frame = pd.read_csv(ROOT / "OfflineData" / name, index_col=0)
        assert len(frame) > 0, name
        for row in frame.itertuples(index=False):
            got = bar_from_ib(ib_bar(str(row.time), row.open, row.high, row.low, row.close,
                                     Decimal(int(row.volume))))
            assert got.time == row.time, (name, row)
            assert got.date == pd.Timestamp(row.date), (name, row.date, got.date)
            assert got.date.tzinfo is None
            assert (got.open, got.high, got.low, got.close) == (
                row.open, row.high, row.low, row.close)
            assert got.volume == row.volume
            total += 1
    assert total > 1000


def test_bar_from_ib_returns_an_engine_bar_with_plain_types() -> None:
    got = bar_from_ib(ib_bar(str(epoch(2024, 8, 16, 13, 30)), 1, 2, 1, 2, 5))
    assert isinstance(got, Bar)
    assert isinstance(got.date, pd.Timestamp)
    assert type(got.time) is int
    assert type(got.volume) is int
    assert all(type(v) is float for v in (got.open, got.high, got.low, got.close))
    assert got.date == pd.Timestamp("2024-08-16 09:30:00")


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (epoch(2024, 3, 8, 14, 30), "2024-03-08 09:30:00"),   # EST, the Friday before
        (epoch(2024, 3, 8, 13, 30), "2024-03-08 08:30:00"),
        (epoch(2024, 3, 9, 14, 30), "2024-03-09 09:30:00"),   # Saturday, still EST
        (epoch(2024, 3, 10, 13, 30), "2024-03-10 09:30:00"),  # the change day, EDT after 07:00 UTC
        (epoch(2024, 3, 11, 13, 30), "2024-03-11 09:30:00"),  # EDT, the Monday after
        (epoch(2024, 3, 11, 14, 30), "2024-03-11 10:30:00"),
        (epoch(2024, 11, 1, 13, 30), "2024-11-01 09:30:00"),  # EDT, the Friday before
        (epoch(2024, 11, 3, 14, 30), "2024-11-03 09:30:00"),  # the change day, EST after 06:00 UTC
        (epoch(2024, 11, 4, 14, 30), "2024-11-04 09:30:00"),  # EST, the Monday after
        (epoch(2024, 11, 4, 13, 30), "2024-11-04 08:30:00"),
        (epoch(2024, 12, 2, 14, 30), "2024-12-02 09:30:00"),  # winter
        (epoch(2024, 12, 2, 21, 0), "2024-12-02 16:00:00"),
    ],
)
def test_bar_time_across_dst(utc: int, expected: str) -> None:
    got = bar_from_ib(ib_bar(str(utc)))
    assert got.date == pd.Timestamp(expected)
    assert got.time == utc


def test_bar_time_ignores_the_machine_time_zone() -> None:
    code = (
        "from MainStoinker.ib.bars import data_time\n"
        "print(data_time(1733149800), data_time(1723815000), data_time(1710081000))\n"
    )
    expected = "2024-12-02 09:30:00 2024-08-16 09:30:00 2024-03-10 10:30:00"
    for tz in ("Pacific/Honolulu", "America/Phoenix", "UTC", "HST10", "MST7", "JST-9"):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "TZ": tz}
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
            cwd=ROOT, env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, tz


def test_bars_module_never_asks_for_the_local_zone() -> None:
    tree = ast.parse(Path(bars_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "astimezone":
                assert node.args or node.keywords, "astimezone() without a zone is the local zone"
            assert node.func.attr not in {"localtime", "tzset", "now", "today", "utcnow"}
            if node.func.attr == "fromtimestamp":
                assert any(k.arg == "tz" for k in node.keywords)
        if isinstance(node, ast.Name):
            assert node.id != "tzlocal"


def test_zone_is_a_parameter_and_unknown_zone_is_refused() -> None:
    utc = epoch(2024, 8, 16, 13, 30)
    assert bar_from_ib(ib_bar(str(utc)), "UTC").date == pd.Timestamp("2024-08-16 13:30:00")
    assert bar_from_ib(ib_bar(str(utc)), "America/Los_Angeles").date == pd.Timestamp(
        "2024-08-16 06:30:00")
    with pytest.raises(ValueError, match="unknown time zone"):
        bar_from_ib(ib_bar(str(utc)), "Mars/Olympus")


def test_volume_decimal_to_int_and_fraction_refused() -> None:
    got = bar_from_ib(ib_bar(volume=Decimal("12345")))
    assert got.volume == 12345 and type(got.volume) is int
    assert bar_from_ib(ib_bar(volume=Decimal("1E+3"))).volume == 1000
    assert bar_from_ib(ib_bar(volume=Decimal("12.0"))).volume == 12
    assert bar_from_ib(ib_bar(volume=Decimal(0))).volume == 0  # a bar without volume is legal
    assert bar_from_ib(ib_bar(volume=7)).volume == 7
    for bad in (Decimal("12.5"), Decimal("0.1"), Decimal(-1), Decimal(-5), Decimal("NaN"),
                Decimal("Infinity"), -3, 1.5, "12", None, True):
        with pytest.raises(BadBar, match="volume"):
            bar_from_ib(ib_bar(volume=bad))


@pytest.mark.parametrize(
    "date",
    [
        "20240816  09:30:00",    # formatDate=1
        "20240816 09:30:00",
        "20240816",              # a daily bar
        "2024-08-16 09:30:00",
        "",
        "abc",
        "-60",
        "1723815000.0",
        "1723815000 ",
        "١٧٢٣٨١٥٠٠٠",             # Arabic-Indic digits are not epoch digits
        None,
        1723815000,
    ],
)
def test_non_epoch_date_refused(date: object) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_bar(date))


def test_daily_and_formatted_dates_are_named_in_the_message() -> None:
    with pytest.raises(BadBar, match="daily"):
        bar_from_ib(ib_bar("20240816"))
    with pytest.raises(BadBar, match="formatDate"):
        bar_from_ib(ib_bar("20240816  09:30:00"))


@pytest.mark.parametrize(
    "date",
    [
        "0",
        "946684799",             # one second before the lower bound (and not a minute)
        "946684740",             # a whole minute, still below 2000-01-01
        "4102444860",            # a whole minute above 2100-01-01
        "1723815000000",         # milliseconds
        "1723815001",            # not a whole minute
        "99999999999",
    ],
)
def test_epoch_out_of_range_or_not_a_minute_refused(date: str) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_bar(date))


def test_epoch_range_edges_are_accepted() -> None:
    assert bar_from_ib(ib_bar("946684800")).time == 946684800
    assert bar_from_ib(ib_bar("4102444800")).time == 4102444800


@pytest.mark.parametrize(
    ("o", "h", "low", "c"),
    [
        (-1, -1, -1, -1),            # IB's "no data" bar
        (0.0, 1.0, 0.0, 1.0),        # zero price
        (10, 11, -1, 10),            # one sentinel price
        (float("nan"), 11, 9, 10),
        (10, float("inf"), 9, 10),
        (10, 11, 9, float("-inf")),
        (10, 9.99, 9, 10),           # high below open
        (10, 10.5, 9, 10.6),         # high below close
        (10, 11, 10.1, 10.5),        # low above open
        (10, 11, 10.6, 10.5),        # low above close
        (10, 9, 11, 10),             # low above high
        ("10", 11, 9, 10),
        (None, 11, 9, 10),
        (True, 11, 9, 10),
    ],
)
def test_inconsistent_or_sentinel_prices_refused(o: object, h: object, low: object, c: object) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_bar(o=o, h=h, low=low, c=c))  # type: ignore[arg-type]


def test_flat_and_edge_touching_bars_are_accepted() -> None:
    assert bar_from_ib(ib_bar(o=5, h=5, low=5, c=5)).high == 5.0
    got = bar_from_ib(ib_bar(o=10, h=11, low=9, c=11))  # close at the high, open above low
    assert (got.high, got.low) == (11.0, 9.0)
    assert bar_from_ib(ib_bar(o=0.0001, h=0.0001, low=0.0001, c=0.0001)).open == 0.0001


def test_bad_bar_is_a_value_error() -> None:
    assert issubclass(BadBar, ValueError)
