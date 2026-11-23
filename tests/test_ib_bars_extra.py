"""Extra tests for `ib.bars`: the converted bars are what a backtest reads from disk.

Written by the tester of task T6 (issue 119). The converter is checked here without the
helpers of `test_ib_bars.py`: every row of the three committed `OfflineData` files is turned
into an IB-style bar (epoch string from the `time` column, `Decimal` volume), converted, and
compared with `engine.feed.read_offline_bars` in values and dtypes; month files written from
the converted bars must read back identically and drive a backtest to the same trades and
ledger as the committed files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.cli import main
from MainStoinker.engine.feed import (
    COLUMNS,
    Bar,
    month_file_path,
    read_offline_bars,
    read_symbol_bars,
    write_month_file,
)
from MainStoinker.ib.bars import BadBar, bar_from_ib, data_time
from MainStoinker.ib.events import HistoricalBar

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = ROOT / "OfflineData"
SYMBOLS = ("AAPL", "MSFT", "TSLA")


def ib_row(date: object, o: float, h: float, low: float, c: float, volume: object) -> HistoricalBar:
    return HistoricalBar(7, date, o, h, low, c, volume, Decimal(0), 0)  # type: ignore[arg-type]


def converted(symbol: str) -> tuple[list[Bar], pd.DataFrame]:
    """The committed file of `symbol` as converted IB bars, and the engine's own reading of it."""
    path = OFFLINE / f"_{symbol}_offlinedata_"
    reference = read_offline_bars(path)
    text = pd.read_csv(path, usecols=list(COLUMNS), dtype=str)  # the file's own text
    bars = [
        bar_from_ib(ib_row(r.time, float(r.open), float(r.high), float(r.low), float(r.close),
                           Decimal(r.volume)))
        for r in text.itertuples(index=False)
    ]
    return bars, reference


def frame_of(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame.from_records([b.as_record() for b in bars], columns=list(COLUMNS))


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_converted_bars_equal_the_offline_reader_in_values_and_dtypes(symbol: str) -> None:
    bars, reference = converted(symbol)
    frame = frame_of(bars)
    assert len(frame) == len(reference) > 1000
    assert list(frame.dtypes) == list(reference.dtypes)
    pd.testing.assert_frame_equal(frame, reference, check_exact=True)
    # one row at a time through the engine's own bar type
    for bar, row in zip(bars, reference.itertuples(index=False), strict=True):
        assert bar == Bar(*row)
        assert type(bar.time) is int and type(bar.volume) is int
        assert bar.date.tzinfo is None


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_prices_are_passed_through_exactly_as_the_file_text_parses(symbol: str) -> None:
    path = OFFLINE / f"_{symbol}_offlinedata_"
    text = pd.read_csv(path, usecols=list(COLUMNS), dtype=str)
    bars, _ = converted(symbol)
    for name in ("open", "high", "low", "close"):
        assert [repr(getattr(b, name)) for b in bars] == [repr(float(s)) for s in text[name]]


def test_binary_noise_in_a_price_is_not_rounded() -> None:
    noisy = 0.1 + 0.2  # 0.30000000000000004
    bar = bar_from_ib(ib_row("1723815000", noisy, 224.80000000000001, noisy, noisy, Decimal(1)))
    assert bar.open == noisy and repr(bar.open) == "0.30000000000000004"
    assert bar.high == 224.8  # the same float the file's text "224.8" parses to


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_month_files_from_converted_bars_read_back_as_the_committed_data(
    symbol: str, tmp_path: Path
) -> None:
    bars, reference = converted(symbol)
    frame = frame_of(bars)
    stamp = frame["date"]
    for (year, month), part in frame.groupby([stamp.dt.year, stamp.dt.month]):
        write_month_file(month_file_path(tmp_path, symbol, int(year), int(month)),
                         part.reset_index(drop=True))
    back = read_symbol_bars(tmp_path, symbol)
    assert list(back.dtypes) == list(reference.dtypes)
    pd.testing.assert_frame_equal(back, reference, check_exact=True)
    # and the text of every price, volume and time cell is the committed file's text
    original = pd.read_csv(OFFLINE / f"_{symbol}_offlinedata_", usecols=list(COLUMNS), dtype=str)
    written = pd.concat(
        [pd.read_csv(p, dtype=str) for p in sorted((tmp_path / symbol).glob("*.csv"))],
        ignore_index=True,
    )
    pd.testing.assert_frame_equal(written, original)


def algos_json(path: Path) -> None:
    ema = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}
    fvg = {"idname": "AAPL-fvg", "ticker": "AAPL", "RRRatio": 3}
    path.write_text(json.dumps([{"ID": "EMACrossing", "data": [ema]},
                                {"ID": "FVG", "data": [fvg]}]), encoding="utf-8")


@pytest.mark.slow
@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_a_backtest_on_converted_month_files_equals_one_on_the_committed_file(
    fills: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / "committed"
    committed.mkdir()
    (committed / "_AAPL_offlinedata_").write_bytes((OFFLINE / "_AAPL_offlinedata_").read_bytes())
    fetched = tmp_path / "fetched"
    bars, _ = converted("AAPL")
    frame = frame_of(bars)
    for (year, month), part in frame.groupby([frame["date"].dt.year, frame["date"].dt.month]):
        write_month_file(month_file_path(fetched, "AAPL", int(year), int(month)),
                         part.reset_index(drop=True))
    algos = tmp_path / "algos.json"
    algos_json(algos)
    outs = {}
    for name, data in (("committed", committed), ("fetched", fetched)):
        out = tmp_path / f"out-{name}"
        code = main(["backtest", "--algos", str(algos), "--data", str(data), "--out", str(out),
                     "--quiet", "--fills", fills])
        assert code == 0
        outs[name] = out
    trades = (outs["committed"] / "trades.json").read_bytes()
    assert trades == (outs["fetched"] / "trades.json").read_bytes()
    by_algo = json.loads(trades)
    assert len(by_algo) == 2 and sum(len(v) for v in by_algo.values()) > 0
    if fills == "realistic":
        ledger = (outs["committed"] / "ledger.json").read_bytes()
        assert ledger == (outs["fetched"] / "ledger.json").read_bytes()


def utc_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # 2024 spring change, Sunday 10 March: 07:00 UTC is 02:00 EST becoming 03:00 EDT
        (utc_epoch(2024, 3, 10, 6, 59), "2024-03-10 01:59:00"),
        (utc_epoch(2024, 3, 10, 7, 0), "2024-03-10 03:00:00"),
        # 2025 spring change, Sunday 9 March
        (utc_epoch(2025, 3, 9, 6, 59), "2025-03-09 01:59:00"),
        (utc_epoch(2025, 3, 9, 7, 0), "2025-03-09 03:00:00"),
        # 2024 autumn change, Sunday 3 November: 06:00 UTC is 02:00 EDT becoming 01:00 EST
        (utc_epoch(2024, 11, 3, 5, 59), "2024-11-03 01:59:00"),
        (utc_epoch(2024, 11, 3, 6, 0), "2024-11-03 01:00:00"),
        (utc_epoch(2024, 11, 3, 7, 0), "2024-11-03 02:00:00"),
        # 2025 autumn change, Sunday 2 November
        (utc_epoch(2025, 11, 2, 5, 59), "2025-11-02 01:59:00"),
        (utc_epoch(2025, 11, 2, 6, 0), "2025-11-02 01:00:00"),
        (utc_epoch(2025, 11, 2, 7, 0), "2025-11-02 02:00:00"),
        # the sessions around the changes
        (utc_epoch(2025, 3, 7, 14, 30), "2025-03-07 09:30:00"),
        (utc_epoch(2025, 3, 10, 13, 30), "2025-03-10 09:30:00"),
        (utc_epoch(2025, 10, 31, 13, 30), "2025-10-31 09:30:00"),
        (utc_epoch(2025, 11, 3, 14, 30), "2025-11-03 09:30:00"),
    ],
)
def test_data_time_around_both_years_of_clock_changes(utc: int, expected: str) -> None:
    assert data_time(utc) == pd.Timestamp(expected)


def test_regular_session_bars_never_share_a_naive_time_across_a_year() -> None:
    seen: dict[pd.Timestamp, int] = {}
    # every minute of regular hours (13:30..21:00 UTC covers both offsets) on the change days
    for day in ((2024, 11, 1), (2024, 11, 4), (2025, 10, 31), (2025, 11, 3)):
        for minute in range(13 * 60 + 30, 21 * 60):
            stamp = data_time(utc_epoch(*day, minute // 60, minute % 60))
            assert stamp not in seen
            seen[stamp] = minute


@pytest.mark.xfail(
    strict=True,
    reason="T6 defect: the repeated autumn hour (01:00-02:00 New York) maps two epochs to one "
    "naive time and bar_from_ib accepts both",
)
def test_the_repeated_autumn_hour_is_not_silently_ambiguous() -> None:
    first, second = utc_epoch(2024, 11, 3, 5, 30), utc_epoch(2024, 11, 3, 6, 30)
    stamps = []
    for epoch in (first, second):
        try:
            stamps.append(bar_from_ib(ib_row(str(epoch), 10, 11, 9, 10.5, Decimal(1))).date)
        except BadBar:
            pass
    assert len(stamps) < 2 or stamps[0] != stamps[1]


@pytest.mark.xfail(
    strict=True,
    reason="T6 defect: a volume above int64 is accepted and would not fit an engine frame",
)
def test_a_volume_beyond_int64_is_refused() -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_row("1723815000", 10, 11, 9, 10.5, Decimal(10**30)))


@pytest.mark.parametrize(
    "date",
    [" 1723815000", "1723815000 ", "1723815000\n", "+1723815000", "1723815000.0", "-60", "1e9",
     "١٧٢٣٨١٥٠٠٠", "20240816  09:30:00",
     "20240816 09:30:00 US/Eastern", "20240816", "", 1723815000, 1723815000.0, None, b"1723815000"],
)
def test_only_plain_digit_strings_are_epochs(date: object) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_row(date, 10, 11, 9, 10.5, Decimal(1)))


@pytest.mark.parametrize("epoch", ["0", "00", "946684799", "4102444860", "17238150000",
                                   "1723815001", "1723815059"])
def test_epochs_out_of_range_or_off_the_minute_are_refused(epoch: str) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_row(epoch, 10, 11, 9, 10.5, Decimal(1)))


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(Decimal(0), 0), (Decimal(12), 12), (Decimal("12.0"), 12), (Decimal("1E+3"), 1000),
     (Decimal("-0"), 0), (7, 7)],
)
def test_whole_volumes_in_any_decimal_spelling(volume: object, expected: int) -> None:
    got = bar_from_ib(ib_row("1723815000", 10, 11, 9, 10.5, volume))
    assert got.volume == expected and type(got.volume) is int


@pytest.mark.parametrize(
    "volume",
    [Decimal("0.5"), Decimal(-1), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
     Decimal("sNaN"), 12.0, "12", True, None],
)
def test_other_volumes_are_refused(volume: object) -> None:
    with pytest.raises(BadBar):
        bar_from_ib(ib_row("1723815000", 10, 11, 9, 10.5, volume))


def test_no_data_bars_and_unusable_prices_are_refused() -> None:
    for price in (-1.0, 0.0, float("nan"), float("inf"), True, "10", None):
        with pytest.raises(BadBar):
            bar_from_ib(ib_row("1723815000", price, 11, 9, 10.5, Decimal(1)))  # type: ignore[arg-type]
    with pytest.raises(BadBar):  # IB's all -1 "no data" bar
        bar_from_ib(ib_row("1723815000", -1, -1, -1, -1, Decimal(-1)))


def test_wap_and_bar_count_are_ignored_even_when_they_are_nonsense() -> None:
    row = HistoricalBar(7, "1723815000", 10.0, 11.0, 9.0, 10.5, Decimal(5), Decimal("NaN"), -9)
    assert bar_from_ib(row).volume == 5


def test_the_machine_zone_really_changes_in_the_subprocess_of_the_zone_test() -> None:
    """The `TZ` values `test_bar_time_ignores_the_machine_time_zone` uses must bite here.

    `Pacific/Honolulu` and `America/Phoenix` are ignored by Windows' C runtime, but the POSIX
    forms (`HST10`) are not: a naive local conversion would differ in the subprocess, so the
    test is not vacuous. Skipped on a platform where the local clock ignores `TZ` altogether.
    """
    code = "import datetime;print(datetime.datetime.fromtimestamp(1723815000))"
    outputs = set()
    for tz in ("HST10", "MST7", "JST-9", "UTC"):
        env = {**os.environ, "TZ": tz}
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                                text=True, timeout=120, env=env)
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())
    if len(outputs) < 2:
        pytest.skip("this platform's local clock ignores the TZ variable")
    assert len(outputs) == 4
