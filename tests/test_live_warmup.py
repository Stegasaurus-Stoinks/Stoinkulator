"""`live_warmup`: warm-up frames from month files strictly before today.

Small month files are built under `tmp_path`; nothing else is read.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.feed import (
    COLUMNS,
    CsvBarFeed,
    month_file_path,
    symbol_fingerprint,
    write_month_file,
)
from MainStoinker.engine.warmup import live_warmup

BARS_PER_DAY = 3


def day_rows(day: str) -> list[dict[str, object]]:
    start = pd.Timestamp(f"{day} 09:30:00")
    rows = []
    for i in range(BARS_PER_DAY):
        stamp = start + pd.Timedelta(minutes=i)
        price = 100.0 + i
        rows.append(
            {
                "date": stamp,
                "time": int(stamp.timestamp()),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 1000 + i,
            }
        )
    return rows


def write_days(root: Path, symbol: str, days: list[str]) -> None:
    """Write the given days (3 bars each) into the month files of `symbol`."""
    by_month: dict[tuple[int, int], list[dict[str, object]]] = {}
    for day in days:
        stamp = pd.Timestamp(day)
        by_month.setdefault((stamp.year, stamp.month), []).extend(day_rows(day))
    for (year, month), rows in by_month.items():
        frame = pd.DataFrame(rows)[list(COLUMNS)]
        write_month_file(month_file_path(root, symbol, year, month), frame)


def test_excludes_today_rows(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-07", "2024-10-08", "2024-10-09"])
    frames, description, warnings = live_warmup(tmp_path, ["AAPL"], date(2024, 10, 9), 5)
    frame = frames["AAPL"]
    assert list(frame.columns) == list(COLUMNS)
    assert frame["date"].dt.date.unique().tolist() == [date(2024, 10, 7), date(2024, 10, 8)]
    assert (frame["date"] < pd.Timestamp("2024-10-09")).all()
    assert description["symbols"]["AAPL"]["last_bar"] == "2024-10-08 09:32:00"
    assert description["symbols"]["AAPL"]["rows"] == 2 * BARS_PER_DAY
    assert any("only 2 trading days" in w for w in warnings)


def test_frame_shape_matches_feed_warmup(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-07", "2024-10-08", "2024-10-09"])
    frames, _, _ = live_warmup(tmp_path, ["AAPL"], date(2024, 10, 9), 2)
    feed_frame = CsvBarFeed(tmp_path, ["AAPL"], warmup_days=2).warmup("AAPL")
    pd.testing.assert_frame_equal(frames["AAPL"], feed_frame.iloc[: len(frames["AAPL"])])
    assert frames["AAPL"].index.equals(pd.RangeIndex(len(frames["AAPL"])))


def test_last_n_trading_days_across_month_boundary(tmp_path: Path) -> None:
    days = ["2024-09-26", "2024-09-27", "2024-09-30", "2024-10-01", "2024-10-02"]
    write_days(tmp_path, "AAPL", days)
    frames, description, warnings = live_warmup(tmp_path, ["AAPL"], date(2024, 10, 3), 3)
    assert frames["AAPL"]["date"].dt.date.unique().tolist() == [
        date(2024, 9, 30),
        date(2024, 10, 1),
        date(2024, 10, 2),
    ]
    info = description["symbols"]["AAPL"]
    assert info["days"] == ["2024-09-30", "2024-10-01", "2024-10-02"]
    assert info["first_bar"] == "2024-09-30 09:30:00"
    assert info["files"] == ["AAPL_2024-09.csv", "AAPL_2024-10.csv"]
    assert warnings == []


def test_monday_reaches_back_over_weekend(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-03", "2024-10-04", "2024-10-07"])
    frames, _, warnings = live_warmup(tmp_path, ["AAPL"], date(2024, 10, 7), 2)
    assert frames["AAPL"]["date"].dt.date.unique().tolist() == [
        date(2024, 10, 3),
        date(2024, 10, 4),
    ]
    assert warnings == []


def test_warns_when_previous_day_missing(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-07", "2024-10-08"])
    write_days(tmp_path, "MSFT", ["2024-10-07", "2024-10-08", "2024-10-09"])
    _, _, warnings = live_warmup(tmp_path, ["AAPL", "MSFT"], date(2024, 10, 10), 2)
    assert len(warnings) == 1
    assert "AAPL" in warnings[0]
    assert "2024-10-08" in warnings[0]
    assert "2024-10-09" in warnings[0]


def test_missing_symbol_is_an_error(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-07", "2024-10-08"])
    with pytest.raises(FileNotFoundError, match="NOPE"):
        live_warmup(tmp_path, ["AAPL", "NOPE"], date(2024, 10, 9), 2)


def test_description_fingerprints_match_symbol_fingerprint(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-09-30", "2024-10-01"])
    write_days(tmp_path, "MSFT", ["2024-10-01"])
    _, description, _ = live_warmup(tmp_path, ["AAPL", "MSFT"], date(2024, 10, 2), 2)
    for symbol in ("AAPL", "MSFT"):
        sha, size = symbol_fingerprint(tmp_path, symbol)
        assert description["symbols"][symbol]["sha256"] == sha
        assert description["symbols"][symbol]["size"] == size


def test_only_bars_dir_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bars = tmp_path / "bars"
    write_days(bars, "AAPL", ["2024-10-07"])
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    live_warmup(bars, ["AAPL"], date(2024, 10, 8), 1)
    assert list(elsewhere.iterdir()) == []


def test_no_rows_before_today_is_an_error(tmp_path: Path) -> None:
    write_days(tmp_path, "AAPL", ["2024-10-08"])
    with pytest.raises(ValueError, match="no bars before"):
        live_warmup(tmp_path, ["AAPL"], date(2024, 10, 8), 2)
