"""CsvBarFeed: parity with the legacy offline loader and strict alignment."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from MainStoinker.engine.feed import (
    COLUMNS,
    Bar,
    BarFeed,
    CsvBarFeed,
    bars_between,
    file_fingerprint,
    offline_data_path,
    read_offline_bars,
)
from tests import harness

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "OfflineData"
SYMBOLS = ["AAPL", "MSFT", "TSLA"]
WARMUP_ROWS = 390
REPLAY_ROWS = 2340


@pytest.fixture(scope="module")
def feed_all() -> CsvBarFeed:
    return CsvBarFeed(DATA_DIR, SYMBOLS)


@pytest.fixture(scope="module")
def batches_all(feed_all: CsvBarFeed) -> list:
    return list(feed_all)


def legacy_start(symbols: list[str], warmup_days: int = 2):
    """Run the legacy start-up (offline load + warm-up split) without replaying.

    Returns (tickers by index, ibapi). Needs OfflineData in the cwd.
    """
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.MainStuff import Globals
    from MainStoinker.TradeTools.ticker import Ticker

    harness.stage_offline_data(Path.cwd(), symbols)
    harness.apply_safe_config()
    harness.reset_runtime_state()
    tickers = {i: Ticker(s, i) for i, s in enumerate(symbols)}
    Globals.tickers = tickers
    ibapi.startData(tickers, [], warmup_days, {0: threading.Event()}, 1)
    return tickers, ibapi


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_warmup_matches_legacy(symbol: str, feed_all: CsvBarFeed) -> None:
    tickers, _ = legacy_start([symbol])
    legacy = tickers[0].data
    ours = feed_all.warmup(symbol)
    assert len(ours) == WARMUP_ROWS
    assert isinstance(ours.index, pd.RangeIndex)
    assert list(ours.columns) == list(COLUMNS)
    assert_frame_equal(ours, legacy, check_dtype=True, check_index_type=True)


def test_replay_bars_match_legacy_rows() -> None:
    tickers, ibapi = legacy_start(["AAPL"])
    sim = ibapi.simulatedDatadict[0]
    warm = tickers[0].data.shape[0]
    batches = list(CsvBarFeed(DATA_DIR, ["AAPL"]))
    assert len(batches) == REPLAY_ROWS
    previous = None
    for k, (timestamp, bars) in enumerate(batches):
        row = sim.iloc[warm + k]
        bar = bars["AAPL"]
        assert timestamp == bar.date == row["date"]
        assert [getattr(bar, c) for c in COLUMNS] == [row[c] for c in COLUMNS]
        if previous is not None:
            assert timestamp > previous
        previous = timestamp


def test_bar_types() -> None:
    bar = next(iter(CsvBarFeed(DATA_DIR, ["AAPL"])))[1]["AAPL"]
    assert isinstance(bar, Bar)
    assert isinstance(bar.date, pd.Timestamp)
    assert type(bar.time) is int
    assert type(bar.volume) is int
    assert all(type(getattr(bar, c)) is float for c in ("open", "high", "low", "close"))
    assert list(bar.as_record()) == list(COLUMNS)


def test_appended_bars_reproduce_legacy_ticker_frame(feed_all: CsvBarFeed, batches_all: list) -> None:
    """Appending Bars to the warm-up frame gives exactly what Ticker.append builds."""
    tickers, ibapi = legacy_start(["AAPL"])
    ticker = tickers[0]
    sim = ibapi.simulatedDatadict[0]
    ours = feed_all.warmup("AAPL")
    start = len(ours)
    for k in range(5):
        ticker.append(sim.iloc[start + k])
        bar = batches_all[k][1]["AAPL"]
        ours = pd.concat([ours, pd.DataFrame.from_records([bar.as_record()])], ignore_index=True)
        assert_frame_equal(ours, ticker.data, check_dtype=True, check_index_type=True)


def test_three_symbols_batches_in_caller_order(batches_all: list) -> None:
    assert len(batches_all) == REPLAY_ROWS
    for timestamp, bars in batches_all:
        assert list(bars) == SYMBOLS
        assert {bar.date for bar in bars.values()} == {timestamp}


def test_caller_order_is_kept_not_sorted() -> None:
    feed = CsvBarFeed(DATA_DIR, ["TSLA", "AAPL"])
    assert feed.symbols() == ["TSLA", "AAPL"]
    assert list(next(iter(feed))[1]) == ["TSLA", "AAPL"]


def test_duplicate_symbols_are_deduplicated_in_order() -> None:
    feed = CsvBarFeed(DATA_DIR, ["MSFT", "AAPL", "MSFT", "AAPL", "TSLA"])
    assert feed.symbols() == ["MSFT", "AAPL", "TSLA"]
    assert list(next(iter(feed))[1]) == ["MSFT", "AAPL", "TSLA"]


def test_empty_symbols_rejected() -> None:
    with pytest.raises(ValueError, match="at least one symbol"):
        CsvBarFeed(DATA_DIR, [])


def write_csv(directory: Path, symbol: str, stamps: list[str]) -> None:
    rows = ["x,date,time,open,high,low,close,volume"]
    for i, stamp in enumerate(stamps):
        epoch = int(pd.Timestamp(stamp).timestamp())
        rows.append(f"{i},{stamp},{epoch},1.0,2.0,0.5,1.5,{100 + i}")
    (directory / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")


STAMPS = [str(t) for t in pd.date_range("2024-01-01 00:00:00", periods=12, freq="12h")]
# Warm-up (2 days) takes the first 4 rows; replay is the remaining 8.


def test_synthetic_aligned_files_work(tmp_path: Path) -> None:
    write_csv(tmp_path, "AAA", STAMPS)
    write_csv(tmp_path, "BBB", STAMPS)
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"])
    assert len(feed.warmup("AAA")) == 4
    assert len(list(feed)) == 8


@pytest.mark.parametrize(
    ("name", "bad_stamps", "needle"),
    [
        ("gap", STAMPS[:8] + STAMPS[9:], "2024-01-05 00:00:00"),
        ("shorter", STAMPS[:-1], "2024-01-06 12:00:00"),
        ("shifted_warmup", STAMPS[1:], "2024-01-03"),
    ],
)
def test_misaligned_symbols_raise(
    tmp_path: Path, name: str, bad_stamps: list[str], needle: str
) -> None:
    write_csv(tmp_path, "GOOD", STAMPS)
    write_csv(tmp_path, "BAD", bad_stamps)
    with pytest.raises(ValueError, match="BAD") as info:
        CsvBarFeed(tmp_path, ["GOOD", "BAD"])
    assert needle in str(info.value), name


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    write_csv(tmp_path, "AAA", STAMPS)
    with pytest.raises(FileNotFoundError, match="_NOPE_offlinedata_"):
        CsvBarFeed(tmp_path, ["AAA", "NOPE"])


def test_missing_column_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "_AAA_offlinedata_").write_text(
        "x,date,time,open,high,low,close\n0,2024-01-01 00:00:00,1,1,2,0.5,1.5\n"
    )
    with pytest.raises(ValueError, match="_AAA_offlinedata_"):
        CsvBarFeed(tmp_path, ["AAA"])


def test_unparseable_file_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "_AAA_offlinedata_").write_text(
        "x,date,time,open,high,low,close,volume\n0,not-a-date,1,1,2,0.5,1.5,10\n"
    )
    with pytest.raises(ValueError, match="_AAA_offlinedata_"):
        CsvBarFeed(tmp_path, ["AAA"])


def test_feed_reads_data_dir_not_cwd(tmp_path: Path) -> None:
    # The autouse fixture put us in an empty temp dir; an absolute data_dir works.
    assert not (Path.cwd() / "OfflineData").exists()
    assert len(CsvBarFeed(DATA_DIR, ["AAPL"]).warmup("AAPL")) == WARMUP_ROWS

    # A decoy OfflineData in the cwd must be ignored when another data_dir is given.
    decoy = Path.cwd() / "OfflineData"
    decoy.mkdir()
    write_csv(decoy, "AAPL", STAMPS[:2])
    other = tmp_path / "other"
    other.mkdir()
    write_csv(other, "AAPL", STAMPS)
    feed = CsvBarFeed(other, ["AAPL"])
    assert len(feed.warmup("AAPL")) == 4
    assert len(list(feed)) == 8
    # And a relative OfflineData is not a fallback for a missing file.
    with pytest.raises(FileNotFoundError):
        CsvBarFeed(other, ["MSFT"])


def test_feed_can_be_iterated_twice(feed_all: CsvBarFeed, batches_all: list) -> None:
    again = list(feed_all)
    assert again == batches_all
    assert len(again) == REPLAY_ROWS


def test_warmup_returns_independent_copy(feed_all: CsvBarFeed) -> None:
    first = feed_all.warmup("AAPL")
    expected = first.copy()
    first.loc[0, "close"] = -1.0
    first["extra"] = 1
    first.loc[:, "volume"] = 0
    assert_frame_equal(feed_all.warmup("AAPL"), expected)


def test_warmup_unknown_symbol(feed_all: CsvBarFeed) -> None:
    with pytest.raises(KeyError, match="NOPE"):
        feed_all.warmup("NOPE")


# warmup_days counts calendar days from the first bar, like the legacy loader:
# the data starts on a Friday, so 1 to 3 days all cover just that session.
@pytest.mark.parametrize(
    ("days", "warm_rows"), [(1, 390), (2, 390), (3, 390), (4, 780), (5, 1170)]
)
def test_warmup_days_changes_split(days: int, warm_rows: int) -> None:
    feed = CsvBarFeed(DATA_DIR, ["AAPL"], warmup_days=days)
    full = pd.read_csv(DATA_DIR / "_AAPL_offlinedata_", usecols=list(COLUMNS))
    full["date"] = pd.to_datetime(full["date"])
    warm = feed.warmup("AAPL")
    replay = [bar["AAPL"] for _, bar in feed]
    assert len(warm) == warm_rows
    assert len(warm) + len(replay) == len(full)
    seen = list(warm["date"]) + [b.date for b in replay]
    assert seen == list(full["date"])


def test_csv_feed_satisfies_protocol(feed_all: CsvBarFeed) -> None:
    assert isinstance(feed_all, BarFeed)


def test_non_feed_fails_protocol() -> None:
    assert not isinstance(object(), BarFeed)


def test_read_offline_bars_equals_feed_parsing(feed_all: CsvBarFeed, batches_all: list) -> None:
    ours = read_offline_bars(offline_data_path(DATA_DIR, "AAPL"))
    replay = pd.DataFrame.from_records([bars["AAPL"].as_record() for _, bars in batches_all])
    held = pd.concat([feed_all.warmup("AAPL"), replay], ignore_index=True)
    assert_frame_equal(ours, held, check_dtype=True, check_index_type=True)


def test_bars_between_inclusive_and_empty() -> None:
    frame = read_offline_bars(offline_data_path(DATA_DIR, "AAPL"))
    start, end = frame["date"].iloc[10], frame["date"].iloc[19]
    window = bars_between(frame, start.to_pydatetime(), end.to_pydatetime())
    assert len(window) == 10
    assert window["date"].iloc[0] == start and window["date"].iloc[-1] == end
    assert isinstance(window.index, pd.RangeIndex)
    assert list(window.columns) == list(COLUMNS)
    last = frame["date"].iloc[-1]
    assert bars_between(frame, last + pd.Timedelta(days=1), last + pd.Timedelta(days=2)).empty
    assert bars_between(frame, end, start).empty


def test_file_fingerprint_stable_and_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc" * 1_000_000)
    first = file_fingerprint(path)
    assert first == file_fingerprint(path)
    assert first[1] == 3_000_000 and len(first[0]) == 64
    assert first[0] == hashlib.sha256(b"abc" * 1_000_000).hexdigest()
    path.write_bytes(b"abd" * 1_000_000)
    assert file_fingerprint(path)[0] != first[0]


@pytest.mark.parametrize(
    "symbol", ["", "..", "../AAPL", "A/B", "A\\B", ".AAPL", "AAPL\n", "A B", "/etc"]
)
def test_offline_data_path_rejects_bad_symbol(symbol: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid symbol"):
        offline_data_path(tmp_path, symbol)


def test_offline_data_path_good_symbol(tmp_path: Path) -> None:
    assert offline_data_path(tmp_path, "BRK.B") == tmp_path / "_BRK.B_offlinedata_"
