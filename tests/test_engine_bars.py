"""BarStore: frame-identical to the legacy Ticker history, and no aliasing."""
from __future__ import annotations

import time
import warnings
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from MainStoinker.engine.bars import BarStore
from MainStoinker.engine.feed import COLUMNS, Bar, CsvBarFeed
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


# --- linear-time storage: equivalence with the old concat implementation ---

EXPECTED_DTYPES = ["datetime64[us]", "int64", "float64", "float64", "float64", "float64", "int64"]


class ConcatReference:
    """The pre-A7 BarStore: copy the warm-up, extend with concat, shallow-copy views."""

    def __init__(self, warmup: pd.DataFrame) -> None:
        self._data = warmup.copy()

    def append(self, bar: Bar) -> None:
        record = pd.DataFrame.from_records([bar.as_record()])
        self._data = pd.concat([self._data, record], ignore_index=True)

    def view(self) -> pd.DataFrame:
        return self._data.copy(deep=False)

    def __len__(self) -> int:
        return len(self._data)


def assert_identical(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    assert_frame_equal(
        actual, expected, check_dtype=True, check_index_type=True, check_column_type=True,
        check_exact=True,
    )
    assert type(actual.index) is pd.RangeIndex
    assert actual.index.equals(pd.RangeIndex(len(actual)))
    assert actual.columns.dtype == expected.columns.dtype
    assert list(actual.columns) == list(COLUMNS)
    assert actual.attrs == expected.attrs
    assert actual.flags.allows_duplicate_labels == expected.flags.allows_duplicate_labels
    assert actual.shape == expected.shape
    if len(actual):
        a, e = actual.iloc[-1], expected.iloc[-1]
        assert a.dtype == e.dtype
        assert [type(x) for x in a] == [type(x) for x in e]
        assert a.tolist() == e.tolist()
        assert str(a["date"]) == str(e["date"])
        a_close, e_close = actual["close"].values, expected["close"].values
        assert a_close.dtype == e_close.dtype
        assert a_close.flags["C_CONTIGUOUS"] == e_close.flags["C_CONTIGUOUS"]
        assert a_close.flags["WRITEABLE"] == e_close.flags["WRITEABLE"]
    assert actual["time"].dtype == expected["time"].dtype


def synthetic_bars(count: int, seed: int = 7) -> list[Bar]:
    rng = np.random.default_rng(seed)
    price = 190.0
    bars = []
    for i in range(count):
        open_ = price
        close = round(open_ + float(rng.normal(0, 0.15)), 2)
        high = round(max(open_, close) + float(rng.uniform(0, 0.1)), 2)
        low = round(min(open_, close) - float(rng.uniform(0, 0.1)), 2)
        bars.append(Bar(pd.Timestamp("2031-03-03 09:30:00").as_unit("us") + pd.Timedelta(minutes=i),
                        1_950_000_000 + 60 * i, open_, high, low, close, int(rng.integers(1_000, 90_000))))
        price = close
    return bars


def test_view_equals_concat_reference_every_step(feed: CsvBarFeed) -> None:
    warmup = feed.warmup("AAPL")
    store, reference = BarStore(warmup), ConcatReference(warmup)
    assert_identical(store.view(), reference.view())
    for bar in synthetic_bars(1000):
        store.append(bar)
        reference.append(bar)
        assert_identical(store.view(), reference.view())
        assert len(store) == len(reference)


def test_view_equals_legacy_ticker_on_real_data_across_a_reallocation(feed: CsvBarFeed) -> None:
    tickers, ibapi = legacy_start(["AAPL"])
    ticker = tickers[0]
    sim = ibapi.simulatedDatadict[0]
    store = BarStore(feed.warmup("AAPL"), _initial_capacity=400)
    start = len(store)
    steps = 1300  # capacity 400 -> 800 -> 1600: at least two growths
    bars = [batch[1]["AAPL"] for _, batch in zip(range(steps), feed, strict=False)]
    assert len(bars) == steps
    for k, bar in enumerate(bars):
        ticker.append(sim.iloc[start + k])
        store.append(bar)
        assert_frame_equal(store.view(), ticker.data, check_dtype=True, check_index_type=True,
                           check_exact=True)
    assert store._growths >= 2
    assert_identical(store.view(), ticker.data)


def test_reallocations_are_logarithmic(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"))
    for bar in synthetic_bars(100_000):
        store.append(bar)
    assert len(store) == 100_000 + 390
    assert 1 <= store._growths <= 20


def test_previously_returned_view_survives_growth_unchanged(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"), _initial_capacity=400)
    held: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    for i, bar in enumerate(synthetic_bars(3000)):
        if i % 250 == 0:
            view = store.view()
            held.append((view, view.copy()))
        store.append(bar)
    assert store._growths >= 3
    for view, snapshot in held:
        assert len(view) == len(snapshot)
        assert_identical(view, snapshot)


def _op_set_column(df: pd.DataFrame) -> None:
    df["close"] = -1.0


def _op_iloc(df: pd.DataFrame) -> None:
    df.iloc[0, 3] = -1.0


def _op_at(df: pd.DataFrame) -> None:
    df.at[0, "close"] = -1.0


def _op_values_item(df: pd.DataFrame) -> None:
    df["close"].values[0] = -1.0


def _op_to_numpy(df: pd.DataFrame) -> None:
    df.to_numpy()[0, 0] = 0


def _op_append_row(df: pd.DataFrame) -> None:
    df.loc[len(df)] = df.iloc[0]


def _op_drop_inplace(df: pd.DataFrame) -> None:
    df.drop(columns=["open"], inplace=True)


def _op_columns(df: pd.DataFrame) -> None:
    df.columns = list("abcdefg")


def _op_new_column(df: pd.DataFrame) -> None:
    df["extra"] = 1


def _op_loc_cell(df: pd.DataFrame) -> None:
    df.loc[0, "open"] = -2.0


def _op_iloc_date(df: pd.DataFrame) -> None:
    df.iloc[0, 0] = pd.Timestamp("1999-01-01")


def _op_df_values(df: pd.DataFrame) -> None:
    df.values[0, 0] = 1


def _op_series_to_numpy(df: pd.DataFrame) -> None:
    df["close"].to_numpy()[0] = -1.0


def _op_drop_rows_inplace(df: pd.DataFrame) -> None:
    df.drop(index=[0], inplace=True)


def _op_loc_augmented(df: pd.DataFrame) -> None:
    df.loc[:, "close"] *= 2


def _op_set_index(df: pd.DataFrame) -> None:
    df.index = df.index + 5


MUTATIONS: list[Callable[[pd.DataFrame], None]] = [
    _op_set_column, _op_iloc, _op_at, _op_values_item, _op_to_numpy, _op_append_row,
    _op_drop_inplace, _op_columns, _op_new_column, _op_loc_cell, _op_iloc_date, _op_df_values,
    _op_series_to_numpy, _op_drop_rows_inplace, _op_loc_augmented, _op_set_index,
]


def _outcome(op: Callable[[pd.DataFrame], None], frame: pd.DataFrame) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            op(frame)
    except Exception as exc:  # noqa: BLE001 - the outcome class is what is compared
        return f"raise {type(exc).__name__}"
    return "ok"


@pytest.mark.parametrize("op", MUTATIONS, ids=lambda f: f.__name__)
def test_views_do_not_share_writable_memory_with_the_store(
    op: Callable[[pd.DataFrame], None], feed: CsvBarFeed
) -> None:
    warmup = feed.warmup("AAPL")
    bars = synthetic_bars(3)
    store, reference = BarStore(warmup), ConcatReference(warmup)
    for s in (store, reference):
        s.append(bars[0])
    pristine = reference.view().copy()
    bystander = store.view()  # a second returned frame must not see the writes either
    victim, ref_victim = store.view(), reference.view()
    assert _outcome(op, victim) == _outcome(op, ref_victim)
    assert_identical(store.view(), pristine)
    assert_identical(bystander, pristine)
    for s in (store, reference):
        s.append(bars[1])
    assert_identical(store.view(), reference.view())


def test_close_values_are_contiguous_float64_and_talib_accepts_them(feed: CsvBarFeed) -> None:
    talib = pytest.importorskip("talib")
    store, reference = BarStore(feed.warmup("AAPL")), ConcatReference(feed.warmup("AAPL"))
    for bar in synthetic_bars(30):
        store.append(bar)
        reference.append(bar)
    values = store.view()["close"].values
    assert values.dtype == np.float64
    assert values.flags["C_CONTIGUOUS"]
    assert not values.flags["WRITEABLE"]
    np.testing.assert_array_equal(talib.EMA(values, 10), talib.EMA(reference.view()["close"].values, 10))


def _time_appends(rows: int, appends: int) -> float:
    bar = synthetic_bars(1)[0]
    seed = pd.DataFrame({
        "date": pd.Series(pd.date_range("2020-01-01", periods=rows, freq="min")).astype("datetime64[us]"),
        "time": np.arange(rows, dtype="int64"),
        "open": np.full(rows, 100.0), "high": np.full(rows, 101.0), "low": np.full(rows, 99.0),
        "close": np.full(rows, 100.5), "volume": np.full(rows, 5, dtype="int64"),
    })
    store = BarStore(seed)
    started = time.perf_counter()
    for _ in range(appends):
        store.append(bar)
        store.view()
    return time.perf_counter() - started


def test_append_cost_is_flat_in_history_length() -> None:
    small, large = _time_appends(1_000, 20_000), _time_appends(200_000, 20_000)
    # the legacy concat append is >10x slower at 200k rows than at 1k; linear storage is ~1x
    assert large / small < 3


def test_store_dtypes_after_warmup_and_appends(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"))
    for count in range(4):
        view = store.view()
        assert list(view.dtypes.astype(str)) == EXPECTED_DTYPES
        assert list(view.columns) == list(COLUMNS)
        assert type(view.index) is pd.RangeIndex
        assert view["date"].dt.tz is None
        assert view["close"].values.flags["C_CONTIGUOUS"]
        last = view.iloc[-1]
        assert last.dtype == object
        assert [type(x) for x in last] == [
            pd.Timestamp, np.int64, np.float64, np.float64, np.float64, np.float64, np.int64,
        ]
        store.append(make_bar(count))


def test_date_unit_follows_the_finest_unit_seen_like_concat(feed: CsvBarFeed) -> None:
    store, reference = BarStore(feed.warmup("AAPL")), ConcatReference(feed.warmup("AAPL"))
    for i, unit in enumerate(["s", "ms", "us", "ns", "s", "us"]):
        bar = Bar(pd.Timestamp("2032-01-01 09:30:00").as_unit(unit) + pd.Timedelta(minutes=i),
                  i, 1.0, 2.0, 0.5, 1.5, 3)
        store.append(bar)
        reference.append(bar)
        assert_identical(store.view(), reference.view())


def test_timezone_aware_bar_date_is_rejected(feed: CsvBarFeed) -> None:
    store = BarStore(feed.warmup("AAPL"))
    bar = Bar(pd.Timestamp("2032-01-01 09:30:00", tz="UTC"), 1, 1.0, 2.0, 0.5, 1.5, 3)
    with pytest.raises(ValueError, match="timezone-naive"):
        store.append(bar)
    assert len(store) == 390


@pytest.mark.parametrize("kind", ["untyped_empty", "extra_column", "int32_volume"])
def test_non_feed_shaped_warmup_keeps_concat_behaviour(kind: str) -> None:
    if kind == "untyped_empty":
        warmup = pd.DataFrame(columns=list(COLUMNS))
    elif kind == "extra_column":
        warmup = pd.DataFrame({**{c: [] for c in COLUMNS}, "x": []})
    else:
        warmup = pd.DataFrame({
            "date": pd.to_datetime(["2029-12-31 09:30"]).as_unit("us"), "time": [1],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "volume": np.array([1], dtype="int32"),
        })
    store, reference = BarStore(warmup), ConcatReference(warmup)
    assert_frame_equal(store.view(), reference.view(), check_dtype=True, check_exact=True)
    for bar in synthetic_bars(3):
        store.append(bar)
        reference.append(bar)
        assert_frame_equal(store.view(), reference.view(), check_dtype=True, check_exact=True)
        assert len(store) == len(reference)
