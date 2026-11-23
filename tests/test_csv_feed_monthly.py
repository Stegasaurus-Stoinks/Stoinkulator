"""Per-month bar files: `DIR/SYMBOL/SYMBOL_YYYY-MM.csv` next to the legacy single file.

The AAPL bars of `OfflineData` are shifted so they straddle a month end, then
written either as one legacy file or split into month files; both layouts must
feed the engine identically. Everything lives under `tmp_path`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from MainStoinker.engine.cli import main
from MainStoinker.engine.feed import (
    COLUMNS,
    CsvBarFeed,
    file_fingerprint,
    month_file_path,
    offline_data_path,
    read_offline_bars,
    read_symbol_bars,
    symbol_bar_files,
    symbol_fingerprint,
    write_month_file,
)
from MainStoinker.engine.store import SqliteStore

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRST_DAY = pd.Timestamp("2024-08-30")
EMA = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}


def shifted_aapl(rows: int = 1500) -> pd.DataFrame:
    """The first `rows` AAPL bars, moved so the first falls on 2024-08-30 (crosses into September)."""
    frame = read_offline_bars(REPO_ROOT / "OfflineData" / "_AAPL_offlinedata_").head(rows).copy()
    delta = FIRST_DAY - frame["date"].iloc[0].normalize()
    frame["date"] = frame["date"] + delta
    frame["time"] = frame["time"] + int(delta.total_seconds())
    return frame


def write_legacy(directory: Path, frame: pd.DataFrame, symbol: str = "AAPL") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = offline_data_path(directory, symbol)
    frame.to_csv(path)
    return path


def write_months(directory: Path, frame: pd.DataFrame, symbol: str = "AAPL") -> list[Path]:
    paths = []
    for (year, month), part in frame.groupby([frame["date"].dt.year, frame["date"].dt.month]):
        path = month_file_path(directory, symbol, int(year), int(month))
        write_month_file(path, part.reset_index(drop=True))
        paths.append(path)
    return paths


@pytest.fixture
def frame() -> pd.DataFrame:
    result = shifted_aapl()
    assert result["date"].dt.month.nunique() == 2
    return result


def test_feed_from_month_files_equals_feed_from_single_file(
    tmp_path: Path, frame: pd.DataFrame
) -> None:
    write_legacy(tmp_path / "single", frame)
    write_months(tmp_path / "months", frame)
    single = CsvBarFeed(tmp_path / "single", ["AAPL"])
    months = CsvBarFeed(tmp_path / "months", ["AAPL"])
    assert_frame_equal(single.warmup("AAPL"), months.warmup("AAPL"))
    assert list(single) == list(months)
    assert len(list(months)) > 0


def test_legacy_and_month_files_both_present_refused(tmp_path: Path, frame: pd.DataFrame) -> None:
    write_legacy(tmp_path, frame)
    write_months(tmp_path, frame)
    with pytest.raises(ValueError, match="both"):
        symbol_bar_files(tmp_path, "AAPL")
    with pytest.raises(ValueError, match="both"):
        CsvBarFeed(tmp_path, ["AAPL"])


def test_neither_layout_present_names_both(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"_AAPL_offlinedata_.*AAPL_YYYY-MM"):
        symbol_bar_files(tmp_path, "AAPL")


def test_row_outside_its_month_refused(tmp_path: Path, frame: pd.DataFrame) -> None:
    write_months(tmp_path, frame)
    august = month_file_path(tmp_path, "AAPL", 2024, 8)
    wrong = read_offline_bars(august)
    wrong.loc[1, "date"] = pd.Timestamp("2024-09-02 07:00:00")
    wrong.to_csv(august, index=False)
    with pytest.raises(ValueError, match=r"AAPL_2024-08\.csv.*outside 2024-08"):
        read_symbol_bars(tmp_path, "AAPL")


def test_overlap_between_month_files_refused(tmp_path: Path, frame: pd.DataFrame) -> None:
    """Dates must be strictly increasing: a repeated or backwards stamp is refused.

    Every row must lie in its file's month, so two month files cannot interleave;
    what remains reachable is a repeat or step back inside one file, checked here.
    """
    write_months(tmp_path, frame)
    assert len(read_symbol_bars(tmp_path, "AAPL")) == len(frame)  # adjacent months are fine
    august = month_file_path(tmp_path, "AAPL", 2024, 8)
    rows = read_offline_bars(august)
    rows.loc[len(rows) - 1, "date"] = rows["date"].iloc[len(rows) - 2]  # repeated stamp
    rows.to_csv(august, index=False)
    with pytest.raises(ValueError, match=r"AAPL_2024-08\.csv.*strictly increasing"):
        read_symbol_bars(tmp_path, "AAPL")
    rows = read_offline_bars(month_file_path(tmp_path, "AAPL", 2024, 9))
    rows.iloc[::-1].to_csv(month_file_path(tmp_path, "AAPL", 2024, 9), index=False)  # backwards
    write_month_file(august, read_offline_bars(august).iloc[:-1])  # August valid again
    with pytest.raises(ValueError, match=r"AAPL_2024-09\.csv.*strictly increasing"):
        read_symbol_bars(tmp_path, "AAPL")


def test_unrelated_files_in_symbol_folder_ignored(tmp_path: Path, frame: pd.DataFrame) -> None:
    paths = write_months(tmp_path, frame)
    folder = paths[0].parent
    before = symbol_fingerprint(tmp_path, "AAPL")
    (folder / "manifest.json").write_text("{}")
    (folder / "AAPL_2024-08.csv.123.tmp").write_text("garbage")
    (folder / "MSFT_2024-08.csv").write_text("garbage")
    (folder / "AAPL_2024-13.csv").write_text("garbage")
    (folder / "AAPL_2024-08.csv.bak").write_text("garbage")
    (folder / "AAPL_2024-07.csv").mkdir()
    assert symbol_bar_files(tmp_path, "AAPL") == paths
    assert symbol_fingerprint(tmp_path, "AAPL") == before
    assert len(read_symbol_bars(tmp_path, "AAPL")) == len(frame)


def test_symbol_fingerprint_of_legacy_file_equals_file_fingerprint(
    tmp_path: Path, frame: pd.DataFrame
) -> None:
    path = write_legacy(tmp_path, frame)
    assert symbol_fingerprint(tmp_path, "AAPL") == file_fingerprint(path)
    committed = REPO_ROOT / "OfflineData"
    assert symbol_fingerprint(committed, "AAPL") == file_fingerprint(
        offline_data_path(committed, "AAPL")
    )


def test_symbol_fingerprint_changes_when_a_month_is_added(
    tmp_path: Path, frame: pd.DataFrame
) -> None:
    august = frame[frame["date"].dt.month == 8].reset_index(drop=True)
    september = frame[frame["date"].dt.month == 9].reset_index(drop=True)
    write_month_file(month_file_path(tmp_path, "AAPL", 2024, 8), august)
    one = symbol_fingerprint(tmp_path, "AAPL")
    assert one == symbol_fingerprint(tmp_path, "AAPL")  # stable
    write_month_file(month_file_path(tmp_path, "AAPL", 2024, 9), september)
    two = symbol_fingerprint(tmp_path, "AAPL")
    assert two != one
    assert two[1] == sum(p.stat().st_size for p in symbol_bar_files(tmp_path, "AAPL"))
    write_month_file(month_file_path(tmp_path, "AAPL", 2024, 9), september.head(5))
    assert symbol_fingerprint(tmp_path, "AAPL") != two  # a changed month changes it too


def test_write_month_file_round_trip_and_atomic(
    tmp_path: Path, frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    august = frame[frame["date"].dt.month == 8].reset_index(drop=True)
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    write_month_file(path, august)
    assert_frame_equal(read_symbol_bars(tmp_path, "AAPL"), august)
    raw = path.read_bytes()
    assert b"\r" not in raw
    lines = raw.decode().split("\n")
    assert lines[0] == "date,time,open,high,low,close,volume"
    assert lines[1].split(",")[0] == august["date"].iloc[0].strftime("%Y-%m-%d %H:%M:%S")
    assert lines[-1] == ""
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left

    # A failure while replacing leaves the old file untouched and no temp file behind.
    def boom(*_args: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        write_month_file(path, august.head(3))
    assert path.read_bytes() == raw
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_write_month_file_refuses_bad_frames(tmp_path: Path, frame: pd.DataFrame) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 9)
    with pytest.raises(ValueError, match="columns"):
        write_month_file(path, frame[["date", "time"]])
    with pytest.raises(ValueError, match="empty"):
        write_month_file(path, frame.head(0))
    with pytest.raises(ValueError, match="outside the month"):
        write_month_file(path, frame.head(3))  # August rows into a September file
    assert not path.exists()


def test_month_file_readable_by_read_offline_bars(tmp_path: Path, frame: pd.DataFrame) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    august = frame[frame["date"].dt.month == 8].reset_index(drop=True)
    write_month_file(path, august)
    read = read_offline_bars(path)
    assert list(read.columns) == list(COLUMNS)
    assert_frame_equal(read, august)


@pytest.mark.parametrize("symbol", ["../x", "a/b", "..", "", "a\\b", ".hidden"])
def test_symbol_cannot_escape_data_directory(tmp_path: Path, symbol: str) -> None:
    with pytest.raises(ValueError, match="invalid symbol"):
        month_file_path(tmp_path, symbol, 2024, 8)
    with pytest.raises(ValueError, match="invalid symbol"):
        symbol_bar_files(tmp_path, symbol)
    with pytest.raises(ValueError, match="invalid month"):
        month_file_path(tmp_path, "AAPL", 2024, 13)


def test_backtest_command_on_month_folder(
    tmp_path: Path, frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    algos = tmp_path / "algos.json"
    algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [EMA]}]), encoding="utf-8")
    write_legacy(tmp_path / "single", frame)
    write_months(tmp_path / "months", frame)

    def run(data: str, out: str) -> Path:
        code = main(
            ["backtest", "--algos", str(algos), "--data", str(tmp_path / data),
             "--out", str(tmp_path / out), "--quiet", "--db", str(tmp_path / f"{out}.db")],
            env={},
        )
        assert code == 0
        return tmp_path / out

    def trades_of(out: Path) -> object:
        return json.loads((out / "trades.json").read_text(encoding="utf-8"))

    def stored_files(name: str) -> tuple[tuple[str, str, int], ...]:
        with SqliteStore(tmp_path / f"{name}.db", env={}) as store:
            return tuple(store.list_runs()[0].data.files)

    single_out = run("single", "out-single")
    months_out = run("months", "out-months")
    trades = trades_of(months_out)
    assert isinstance(trades, (list, dict)) and len(trades) > 0
    assert trades == trades_of(single_out)
    assert stored_files("out-single") == (
        ("AAPL", *file_fingerprint(offline_data_path(tmp_path / "single", "AAPL"))),
    )
    assert stored_files("out-months") == (
        ("AAPL", *symbol_fingerprint(tmp_path / "months", "AAPL")),
    )
