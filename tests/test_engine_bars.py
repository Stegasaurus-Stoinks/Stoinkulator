"""BarStore: frame-identical to the legacy Ticker history, and no aliasing."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from MainStoinker.engine.bars import BarStore
from MainStoinker.engine.feed import Bar, CsvBarFeed
from tests.test_csv_feed import DATA_DIR, legacy_start


@pytest.fixture(scope="module")
def feed() -> CsvBarFeed:
    return CsvBarFeed(DATA_DIR, ["AAPL"])


def make_bar(i: int) -> Bar:
    return Bar(pd.Timestamp("2030-01-01 09:30:00") + pd.Timedelta(minutes=i), 1_900_000_000 + 60 * i,
               1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 100 + i)


def test_store_view_equals_legacy_ticker_at_every_step(feed: CsvBarFeed) -> None:
    tickers, ibapi = legacy_start(["AAPL"])
    ticker = tickers[0]
    sim = ibapi.simulatedDatadict[0]
    store = BarStore(feed.warmup("AAPL"))
    start = len(store)
    assert start == len(ticker.data)
    bars = [bar for _, batch in zip(range(50), feed, strict=False) for bar in [batch[1]["AAPL"]]]
    assert len(bars) == 50
    for k, bar in enumerate(bars):
        ticker.append(sim.iloc[start + k])
        store.append(bar)
        view = store.view()
        assert_frame_equal(view, ticker.data, check_dtype=True, check_index_type=True)
        assert list(view.dtypes.astype(str)) == [
            "datetime64[us]", "int64", "float64", "float64", "float64", "float64", "int64",
        ]
        assert list(view.columns) == ["date", "time", "open", "high", "low", "close", "volume"]
        assert isinstance(view.index, pd.RangeIndex)


def test_len(feed: CsvBarFeed) -> None:
    warmup = feed.warmup("AAPL")
    store = BarStore(warmup)
    assert len(store) == len(warmup)
    store.append(make_bar(0))
    store.append(make_bar(1))
    assert len(store) == len(warmup) + 2
    assert len(store.view()) == len(store)


def test_caller_mutating_view_does_not_change_store(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"))
    store.append(make_bar(0))
    before = store.view().copy()
    held = store.view()
    held["close"] = -1.0  # overwrite a column
    held.loc[0, "open"] = -2.0  # overwrite a cell
    held["extra"] = 1  # add a column
    held.loc[len(held)] = held.iloc[0]  # add a row
    held.iloc[0, 0] = pd.Timestamp("1999-01-01")
    assert len(store) == len(before)
    assert_frame_equal(store.view(), before, check_dtype=True, check_index_type=True)
    # a fresh view is a different object each time
    assert store.view() is not store.view()


def test_held_view_is_not_changed_by_later_appends(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"))
    held = store.view()
    snapshot = held.copy()
    store.append(make_bar(0))
    assert_frame_equal(held, snapshot, check_dtype=True, check_index_type=True)
    assert len(held) == len(store) - 1


def test_store_does_not_alias_feed_warmup(feed: CsvBarFeed) -> None:
    pristine = feed.warmup("AAPL")
    # the frame handed to the store is not kept
    seed = feed.warmup("AAPL")
    store = BarStore(seed)
    seed["close"] = -1.0
    assert_frame_equal(store.view(), pristine, check_dtype=True)
    # mutating the store, or a view of it, never changes what the feed returns next
    store.append(make_bar(0))
    view = store.view()
    view["close"] = -3.0
    view.loc[0, "open"] = -4.0
    assert_frame_equal(feed.warmup("AAPL"), pristine, check_dtype=True, check_index_type=True)


def test_data_dir_is_the_committed_offline_data() -> None:
    assert Path(DATA_DIR, "_AAPL_offlinedata_").is_file()
