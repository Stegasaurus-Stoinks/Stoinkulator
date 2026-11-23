"""Edge cases of the per-month bar files: strict reader, writer bytes and atomicity, fingerprint.

Companion to `test_csv_feed_monthly.py`; every file lives under `tmp_path`.
"""
from __future__ import annotations

import hashlib
import threading
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

HEADER = "date,time,open,high,low,close,volume"
REPO_ROOT = Path(__file__).resolve().parent.parent


def row(stamp: str, price: float = 100.0, volume: int = 1000) -> str:
    seconds = int(pd.Timestamp(stamp).timestamp())
    return f"{stamp},{seconds},{price},{price + 1},{price - 1},{price + 0.5},{volume}"


def put(path: Path, lines: list[str], newline: str = "\n", header: str | None = HEADER) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ([header] if header is not None else []) + lines
    path.write_bytes((newline.join(body) + newline).encode("utf-8"))
    return path


def frame_of(*stamps: str) -> pd.DataFrame:
    rows = [r.split(",") for r in (row(s, 100 + i / 8, 1000 + i) for i, s in enumerate(stamps))]
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    frame["date"] = pd.to_datetime(frame["date"])
    for name in ("time", "volume"):
        frame[name] = frame[name].astype("int64")
    for name in ("open", "high", "low", "close"):
        frame[name] = frame[name].astype("float64")
    return frame


# ---------------------------------------------------------------- reader strictness

@pytest.mark.parametrize(
    "stamp, month, ok",
    [
        ("2024-08-01 00:00:00", (2024, 8), True),    # first minute of the month
        ("2024-08-31 23:59:00", (2024, 8), True),    # last minute of the month
        ("2024-07-31 23:59:00", (2024, 8), False),   # one minute before
        ("2024-09-01 00:00:00", (2024, 8), False),   # one minute after
        ("2024-02-29 09:30:00", (2024, 2), True),    # leap day
        ("2024-12-31 23:59:00", (2024, 12), True),
        ("2025-01-01 00:00:00", (2024, 12), False),  # year boundary
        ("2023-08-15 09:30:00", (2024, 8), False),   # right month, wrong year
    ],
)
def test_month_boundaries_are_exact(tmp_path: Path, stamp: str, month: tuple[int, int], ok: bool) -> None:
    put(month_file_path(tmp_path, "AAPL", *month), [row(stamp)])
    if ok:
        assert len(read_symbol_bars(tmp_path, "AAPL")) == 1
    else:
        with pytest.raises(ValueError, match="outside"):
            read_symbol_bars(tmp_path, "AAPL")


def test_impossible_leap_day_is_a_value_error(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2023, 2), [row("2023-02-28 09:30:00").replace("02-28", "02-29")])
    with pytest.raises(ValueError):
        read_symbol_bars(tmp_path, "AAPL")


def test_year_boundary_months_join_and_same_month_other_year_sorts(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2024, 12), [row("2024-12-31 23:59:00")])
    put(month_file_path(tmp_path, "AAPL", 2025, 1), [row("2025-01-01 00:00:00")])
    put(month_file_path(tmp_path, "AAPL", 2023, 12), [row("2023-12-29 09:30:00")])
    frame = read_symbol_bars(tmp_path, "AAPL")
    assert [str(d) for d in frame["date"]] == [
        "2023-12-29 09:30:00", "2024-12-31 23:59:00", "2025-01-01 00:00:00",
    ]
    assert list(frame.index) == [0, 1, 2]


def test_unsorted_and_duplicate_rows_inside_a_file_refused(tmp_path: Path) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    put(path, [row("2024-08-02 09:31:00"), row("2024-08-02 09:30:00")])
    with pytest.raises(ValueError, match="strictly increasing"):
        read_symbol_bars(tmp_path, "AAPL")
    put(path, [row("2024-08-02 09:30:00"), row("2024-08-02 09:30:00")])
    with pytest.raises(ValueError, match="strictly increasing"):
        read_symbol_bars(tmp_path, "AAPL")


def test_empty_and_header_only_month_files_refused_naming_the_file(tmp_path: Path) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    with pytest.raises(ValueError, match=r"AAPL_2024-08\.csv"):
        read_symbol_bars(tmp_path, "AAPL")
    put(path, [])
    with pytest.raises(ValueError, match=r"AAPL_2024-08\.csv.*no rows"):
        read_symbol_bars(tmp_path, "AAPL")


def test_one_bad_month_among_good_ones_refuses_the_whole_symbol(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2024, 8), [row("2024-08-02 09:30:00")])
    put(month_file_path(tmp_path, "AAPL", 2024, 9), [])
    with pytest.raises(ValueError, match=r"AAPL_2024-09\.csv"):
        read_symbol_bars(tmp_path, "AAPL")
    with pytest.raises(ValueError):
        CsvBarFeed(tmp_path, ["AAPL"])


def test_wrong_columns_refused(tmp_path: Path) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    put(path, ["2024-08-02 09:30:00,1,2,3,4,5"], header="date,time,open,high,low,close")
    with pytest.raises(ValueError, match=r"AAPL_2024-08\.csv"):
        read_symbol_bars(tmp_path, "AAPL")


def test_reordered_columns_read_back_in_the_standard_order(tmp_path: Path) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    put(path, ["1000,2024-08-02 09:30:00,1722600600,100.5,101,99,100"],
        header="volume,date,time,close,high,low,open")
    frame = read_symbol_bars(tmp_path, "AAPL")
    assert list(frame.columns) == list(COLUMNS)
    assert frame["open"].iloc[0] == 100 and frame["close"].iloc[0] == 100.5


def test_legacy_format_file_with_index_column_in_the_month_folder_reads(tmp_path: Path) -> None:
    """The reader only selects columns, so the legacy index column is tolerated (pinned, not refused)."""
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    put(path, ["0," + row("2024-08-02 09:30:00")], header="," + HEADER)
    assert_frame_equal(read_symbol_bars(tmp_path, "AAPL"), frame_of("2024-08-02 09:30:00"))


def test_crlf_and_bom_month_files_read_like_lf(tmp_path: Path) -> None:
    lines = [row("2024-08-02 09:30:00"), row("2024-08-02 09:31:00", 101)]
    put(month_file_path(tmp_path / "lf", "AAPL", 2024, 8), lines)
    put(month_file_path(tmp_path / "crlf", "AAPL", 2024, 8), lines, newline="\r\n")
    bom = month_file_path(tmp_path / "bom", "AAPL", 2024, 8)
    put(bom, lines)
    bom.write_bytes(b"\xef\xbb\xbf" + bom.read_bytes())
    expected = read_symbol_bars(tmp_path / "lf", "AAPL")
    assert_frame_equal(read_symbol_bars(tmp_path / "crlf", "AAPL"), expected)
    assert_frame_equal(read_symbol_bars(tmp_path / "bom", "AAPL"), expected)


@pytest.mark.parametrize("name", ["AAPL_2024-00.csv", "AAPL_2024-13.csv", "AAPL_12345-08.csv",
                                  "AAPL_24-08.csv", "AAPL_2024-8.csv", "AAPL_2024-08.CSV",
                                  "aapl_2024-08.csv", "AAPL_2024-08.csv.tmp"])
def test_non_matching_names_are_not_month_files(tmp_path: Path, name: str) -> None:
    put(tmp_path / "AAPL" / name, [row("2024-08-02 09:30:00")])
    with pytest.raises(FileNotFoundError):
        symbol_bar_files(tmp_path, "AAPL")


def test_month_file_name_case_matters(tmp_path: Path) -> None:
    put(tmp_path / "AAPL" / "aapl_2024-08.csv", [row("2024-08-02 09:30:00")])
    with pytest.raises(FileNotFoundError):
        symbol_bar_files(tmp_path, "AAPL")


def test_both_layouts_present_even_when_the_month_folder_has_one_file(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2024, 8), [row("2024-08-02 09:30:00")])
    offline_data_path(tmp_path, "AAPL").write_text("," + HEADER + "\n0," + row("2024-08-02 09:30:00") + "\n")
    with pytest.raises(ValueError, match="both"):
        symbol_fingerprint(tmp_path, "AAPL")
    with pytest.raises(ValueError, match="both"):
        read_symbol_bars(tmp_path, "AAPL")


@pytest.mark.xfail(
    strict=True,
    reason="T7 defect: the month-name pattern matches non-ASCII digits; the reader accepts "
    "such a file and symbol_fingerprint then raises UnicodeEncodeError",
)
def test_unicode_digit_file_name_is_not_a_month_file(tmp_path: Path) -> None:
    """A non-ASCII-digit year in a file name must not count as a month file."""
    put(tmp_path / "AAPL" / "AAPL_٢٠٢٤-08.csv", [row("2024-08-02 09:30:00")])
    with pytest.raises(FileNotFoundError):
        symbol_bar_files(tmp_path, "AAPL")


def test_single_bar_month_feed_and_repeated_runs_agree(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2024, 8), [row("2024-08-02 09:30:00")])
    first = CsvBarFeed(tmp_path, ["AAPL"], warmup_days=0)
    second = CsvBarFeed(tmp_path, ["AAPL"], warmup_days=0)
    assert list(first) == list(second)
    assert_frame_equal(first.warmup("AAPL"), second.warmup("AAPL"))
    assert read_symbol_bars(tmp_path, "AAPL").shape == (1, len(COLUMNS))


def test_reader_does_not_mutate_between_calls(tmp_path: Path) -> None:
    put(month_file_path(tmp_path, "AAPL", 2024, 8), [row("2024-08-02 09:30:00"), row("2024-08-02 09:31:00")])
    first = read_symbol_bars(tmp_path, "AAPL")
    first.loc[0, "close"] = -1
    assert read_symbol_bars(tmp_path, "AAPL")["close"].iloc[0] != -1


# ---------------------------------------------------------------- legacy equals month layout

def test_committed_files_split_by_month_give_identical_frames(tmp_path: Path) -> None:
    for symbol in ("AAPL", "MSFT", "TSLA"):
        legacy = read_offline_bars(offline_data_path(REPO_ROOT / "OfflineData", symbol))
        for (year, month), part in legacy.groupby([legacy["date"].dt.year, legacy["date"].dt.month]):
            write_month_file(month_file_path(tmp_path, symbol, int(year), int(month)),
                             part.reset_index(drop=True))
        assert_frame_equal(read_symbol_bars(tmp_path, symbol), legacy)
        assert_frame_equal(read_symbol_bars(REPO_ROOT / "OfflineData", symbol), legacy)


# ---------------------------------------------------------------- write_month_file

def test_write_month_file_exact_bytes(tmp_path: Path) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    frame = pd.DataFrame(
        {"date": pd.to_datetime(["2024-08-16 06:30:00", "2024-08-16 06:31:00"]),
         "time": [1723815000, 1723815060],
         "open": [224.80000000000001, 100.0], "high": [225.5, 101.25], "low": [224.1, 99.0],
         "close": [224.19, 100.5], "volume": [2650721, 143526]},
        columns=list(COLUMNS),
    )
    write_month_file(path, frame)
    assert path.read_bytes() == (
        b"date,time,open,high,low,close,volume\n"
        b"2024-08-16 06:30:00,1723815000,224.8,225.5,224.1,224.19,2650721\n"
        b"2024-08-16 06:31:00,1723815060,100.0,101.25,99.0,100.5,143526\n"
    )
    assert_frame_equal(read_symbol_bars(tmp_path, "AAPL"), frame)


def test_write_month_file_does_not_modify_the_caller_frame(tmp_path: Path) -> None:
    frame = frame_of("2024-08-02 09:30:00", "2024-08-02 09:31:00")
    before = frame.copy()
    write_month_file(month_file_path(tmp_path, "AAPL", 2024, 8), frame)
    assert_frame_equal(frame, before)


def test_write_month_file_accepts_string_dates_and_matches_datetime_output(tmp_path: Path) -> None:
    frame = frame_of("2024-08-02 09:30:00", "2024-08-02 09:31:00")
    strings = frame.assign(date=frame["date"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    write_month_file(tmp_path / "a" / "AAPL_2024-08.csv", frame)
    write_month_file(tmp_path / "b" / "AAPL_2024-08.csv", strings)
    first, second = tmp_path / "a" / "AAPL_2024-08.csv", tmp_path / "b" / "AAPL_2024-08.csv"
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    "columns",
    [["time", "date", "open", "high", "low", "close", "volume"],
     ["date", "time", "open", "high", "low", "close", "volume", "extra"],
     ["date", "time", "open", "high", "low", "close"]],
)
def test_write_month_file_refuses_wrong_or_reordered_columns(tmp_path: Path, columns: list[str]) -> None:
    frame = frame_of("2024-08-02 09:30:00")
    frame = frame.reindex(columns=columns)
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    with pytest.raises(ValueError, match="columns"):
        write_month_file(path, frame)
    assert not path.parent.exists() or list(path.parent.iterdir()) == []


@pytest.mark.parametrize("stamp", ["2024-07-31 23:59:00", "2024-09-01 00:00:00", "2023-08-02 09:30:00"])
def test_write_month_file_refuses_outside_rows_and_keeps_old_file(tmp_path: Path, stamp: str) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    write_month_file(path, frame_of("2024-08-02 09:30:00"))
    old = path.read_bytes()
    with pytest.raises(ValueError, match="outside the month"):
        write_month_file(path, frame_of("2024-08-02 09:30:00", stamp))
    assert path.read_bytes() == old
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_write_month_file_failure_while_writing_leaves_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    write_month_file(path, frame_of("2024-08-02 09:30:00"))
    old = path.read_bytes()

    def boom(self: pd.DataFrame, handle: object, *args: object, **kwargs: object) -> None:
        handle.write("date,time,op")  # type: ignore[attr-defined]
        raise RuntimeError("killed halfway")

    monkeypatch.setattr(pd.DataFrame, "to_csv", boom)
    with pytest.raises(RuntimeError, match="killed halfway"):
        write_month_file(path, frame_of("2024-08-02 09:30:00", "2024-08-02 09:31:00"))
    assert path.read_bytes() == old
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_write_month_file_overwrites_an_existing_month_and_creates_the_folder(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "AAPL" / "AAPL_2024-08.csv"
    write_month_file(path, frame_of("2024-08-02 09:30:00"))
    write_month_file(path, frame_of("2024-08-03 09:30:00", "2024-08-03 09:31:00"))
    assert len(read_offline_bars(path)) == 2


def test_write_month_file_non_month_name_is_not_range_checked_but_still_atomic(tmp_path: Path) -> None:
    path = tmp_path / "AAPL" / "anything.csv"
    write_month_file(path, frame_of("2024-08-02 09:30:00"))
    assert [p.name for p in path.parent.iterdir()] == ["anything.csv"]


def test_write_month_file_legacy_dtypes_round_trip(tmp_path: Path) -> None:
    legacy = read_offline_bars(offline_data_path(REPO_ROOT / "OfflineData", "AAPL"))
    path = month_file_path(tmp_path, "AAPL", 2024, 8)
    write_month_file(path, legacy)
    back = read_offline_bars(path)
    assert_frame_equal(back, legacy)
    assert back.dtypes.to_dict() == legacy.dtypes.to_dict()


def test_concurrent_writers_of_different_months_do_not_collide(tmp_path: Path) -> None:
    errors: list[BaseException] = []

    def work(month: int) -> None:
        try:
            for _ in range(5):
                write_month_file(month_file_path(tmp_path, "AAPL", 2024, month),
                                 frame_of(f"2024-{month:02d}-02 09:30:00", f"2024-{month:02d}-02 09:31:00"))
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(m,), daemon=True) for m in range(1, 7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len(symbol_bar_files(tmp_path, "AAPL")) == 6
    assert len(list((tmp_path / "AAPL").iterdir())) == 6
    assert len(read_symbol_bars(tmp_path, "AAPL")) == 12


# ---------------------------------------------------------------- fingerprint

def month_set(directory: Path, months: tuple[int, ...] = (6, 7, 8)) -> list[Path]:
    return [
        write_month_file(p := month_file_path(directory, "AAPL", 2024, m),
                         frame_of(f"2024-{m:02d}-02 09:30:00", f"2024-{m:02d}-02 09:31:00")) or p
        for m in months
    ]


def test_fingerprint_changes_on_edit_removal_and_rename_but_not_on_listing_order(tmp_path: Path) -> None:
    paths = month_set(tmp_path)
    base = symbol_fingerprint(tmp_path, "AAPL")
    assert base == symbol_fingerprint(tmp_path, "AAPL")
    # same bytes under another month name: the name is part of the fingerprint
    # (a renamed file also changes the month check, so compare the digests only)
    other = tmp_path / "other"
    month_set(other, (6, 7, 9))
    assert symbol_fingerprint(other, "AAPL") != base
    # removal
    paths[0].rename(paths[0].with_suffix(".bak"))
    assert symbol_fingerprint(tmp_path, "AAPL") != base
    paths[0].with_suffix(".bak").rename(paths[0])
    assert symbol_fingerprint(tmp_path, "AAPL") == base
    # one byte edited
    raw = paths[1].read_bytes()
    paths[1].write_bytes(raw.replace(b"100.0", b"100.5", 1))
    assert symbol_fingerprint(tmp_path, "AAPL")[0] != base[0]
    assert symbol_fingerprint(tmp_path, "AAPL")[1] == base[1]  # same size, different sha
    paths[1].write_bytes(raw)
    assert symbol_fingerprint(tmp_path, "AAPL") == base


def test_fingerprint_is_not_the_concatenation_hash_and_ignores_extras(tmp_path: Path) -> None:
    paths = month_set(tmp_path)
    base = symbol_fingerprint(tmp_path, "AAPL")
    blob = b"".join(p.read_bytes() for p in paths)
    assert base[0] != hashlib.sha256(blob).hexdigest()
    assert base[1] == len(blob)
    (tmp_path / "AAPL" / "manifest.json").write_text('{"x": 1}')
    (tmp_path / "AAPL" / "AAPL_2024-08.csv.abc.tmp").write_text("partial")
    assert symbol_fingerprint(tmp_path, "AAPL") == base


def test_fingerprint_of_twelve_months_is_cheap(tmp_path: Path) -> None:
    import time

    month_set(tmp_path, tuple(range(1, 13)))
    start = time.perf_counter()
    for _ in range(20):
        symbol_fingerprint(tmp_path, "AAPL")
    assert (time.perf_counter() - start) / 20 < 0.5


def test_fingerprint_hasher_seam_is_called_once_per_month_file(tmp_path: Path) -> None:
    paths = month_set(tmp_path)
    seen: list[str] = []

    def spy(path: Path) -> tuple[str, int]:
        seen.append(path.name)
        return file_fingerprint(path)

    assert symbol_fingerprint(tmp_path, "AAPL", spy) == symbol_fingerprint(tmp_path, "AAPL")
    assert seen == [p.name for p in paths]


# ---------------------------------------------------------------- CLI

def test_backtest_on_a_symbol_with_a_bad_month_file_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.chdir(tmp_path)
    put(month_file_path(tmp_path / "d", "AAPL", 2024, 8), [row("2024-08-02 09:30:00")])
    put(month_file_path(tmp_path / "d", "AAPL", 2024, 9), [row("2024-08-02 09:31:00")])
    algos = tmp_path / "algos.json"
    algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [
        {"idname": "x", "ticker": "AAPL", "short": "10", "long": "30"}]}]))
    code = main(["backtest", "--algos", str(algos), "--data", str(tmp_path / "d"),
                 "--out", str(tmp_path / "out"), "--quiet"], env={})
    assert code != 0
    err = capsys.readouterr().err
    assert "AAPL_2024-09.csv" in err and "Traceback" not in err


def test_fingerprint_changes_when_identical_bytes_are_renamed_to_another_month(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    write_month_file(month_file_path(one, "AAPL", 2024, 8), frame_of("2024-08-02 09:30:00"))
    (two / "AAPL").mkdir(parents=True)
    (two / "AAPL" / "AAPL_2024-09.csv").write_bytes(month_file_path(one, "AAPL", 2024, 8).read_bytes())
    assert symbol_fingerprint(one, "AAPL")[1] == symbol_fingerprint(two, "AAPL")[1]
    assert symbol_fingerprint(one, "AAPL")[0] != symbol_fingerprint(two, "AAPL")[0]


def test_listing_order_does_not_matter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    month_set(tmp_path, (6, 7, 8, 9))
    expected_files = symbol_bar_files(tmp_path, "AAPL")
    expected = symbol_fingerprint(tmp_path, "AAPL")
    frame = read_symbol_bars(tmp_path, "AAPL")
    real = Path.iterdir

    def backwards(self: Path):  # noqa: ANN202 - test double
        return iter(sorted(real(self), reverse=True))

    monkeypatch.setattr(Path, "iterdir", backwards)
    assert symbol_bar_files(tmp_path, "AAPL") == expected_files
    assert symbol_fingerprint(tmp_path, "AAPL") == expected
    assert_frame_equal(read_symbol_bars(tmp_path, "AAPL"), frame)
