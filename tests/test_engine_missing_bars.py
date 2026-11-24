"""A symbol with no bar at a minute is skipped for that minute (spec decision 6).

Synthetic one-minute bars: every symbol has three warm-up bars on 2024-01-02
and replay bars from 2024-01-04 09:30, minute `k` being `START + k minutes`.
A symbol's price at minute `k` is `base + k`, so a fill price names the bar it
filled on.
"""
from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import Bar, CsvBarFeed
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.router import MappingRouter
from MainStoinker.engine.sink import NullSink, RecordingSink
from MainStoinker.engine.store import open_store
from tests.test_cli_modes import EMA, EMA_MSFT, Env

WARM_START = pd.Timestamp("2024-01-02 09:30:00")
START = pd.Timestamp("2024-01-04 09:30:00")
FREE = CostModel(Decimal(0), Decimal(0), Decimal(0))
CAPITAL = 10_000


def minute(k: int) -> pd.Timestamp:
    return START + pd.Timedelta(minutes=k)


def write_symbol(
    directory: Path, symbol: str, ks: Sequence[int], base: float, warm: int = 3,
    slope: float = 1.0,
) -> None:
    """Write `symbol`'s offline file: `warm` warm-up bars, then replay minutes `ks`."""
    stamps = [WARM_START + pd.Timedelta(minutes=i) for i in range(warm)]
    stamps += [minute(k) for k in ks]
    rows = ["x,date,time,open,high,low,close,volume"]
    for i, stamp in enumerate(stamps):
        k = int((stamp - START).total_seconds() // 60) if stamp >= START else -1
        price = base + slope * k
        rows.append(f"{i},{stamp},{int(stamp.timestamp())},{price},{price + 1},{price - 1},"
                    f"{price + 0.5},{100 + i}")
    (directory / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")


def make_feed(tmp_path: Path, **ks_by_symbol: Sequence[int]) -> CsvBarFeed:
    for i, (symbol, ks) in enumerate(ks_by_symbol.items()):
        write_symbol(tmp_path, symbol, ks, 100.0 * (i + 1))
    return CsvBarFeed(tmp_path, list(ks_by_symbol), warmup_days=1)


ALL = list(range(10))


def without(*missing: int) -> list[int]:
    return [k for k in ALL if k not in missing]


# ------------------------------------------------------------------ the feed


def test_missing_minute_skips_only_that_symbol(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3))
    batches = list(feed)
    assert [stamp for stamp, _ in batches] == [minute(k) for k in ALL]
    for k, (stamp, bars) in zip(ALL, batches, strict=True):
        assert list(bars) == (["AAA"] if k == 3 else ["AAA", "BBB"])
        assert all(bar.date == stamp for bar in bars.values())
    # no bar is invented: BBB's bars are the real ones, with their own prices
    assert [bars["BBB"].open for _, bars in batches if "BBB" in bars] == [
        200.0 + k for k in without(3)
    ]


def test_batches_keep_symbols_order_not_sorted_order(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, ZZZ=ALL, AAA=without(0), MMM=without(1))
    assert [list(bars) for _, bars in list(feed)[:3]] == [
        ["ZZZ", "MMM"], ["ZZZ", "AAA"], ["ZZZ", "AAA", "MMM"]]


def test_feed_never_yields_an_empty_batch(tmp_path: Path) -> None:
    # every minute is missing from one of the two symbols; the union has them all
    feed = make_feed(tmp_path, AAA=[0, 2, 4, 6, 8], BBB=[1, 3, 5, 7, 9])
    batches = list(feed)
    assert [stamp for stamp, _ in batches] == [minute(k) for k in ALL]
    assert all(bars for _, bars in batches)
    assert all(len(bars) == 1 for _, bars in batches)


def test_a_repeat_in_one_symbol_is_replayed_in_file_order_whatever_the_others_do(
    tmp_path: Path,
) -> None:
    # BBB repeats minute 2; AAA has it once and misses minute 3
    write_symbol(tmp_path, "AAA", [0, 1, 2, 4], 100.0)
    write_symbol(tmp_path, "BBB", [0, 1, 2, 2, 3, 4], 200.0)
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    batches = list(feed)
    assert [(stamp, list(bars)) for stamp, bars in batches] == [
        (minute(0), ["AAA", "BBB"]),
        (minute(1), ["AAA", "BBB"]),
        (minute(2), ["AAA", "BBB"]),
        (minute(2), ["BBB"]),  # AAA has no second bar at minute 2
        (minute(3), ["BBB"]),
        (minute(4), ["AAA", "BBB"]),
    ]
    assert feed.missing_counts() == {"AAA": 2, "BBB": 0}


def test_the_kth_repeat_of_each_symbol_joins_the_kth_batch(tmp_path: Path) -> None:
    write_symbol(tmp_path, "AAA", [0, 1, 1, 1, 2], 100.0)
    write_symbol(tmp_path, "BBB", [0, 1, 1, 2], 200.0)
    batches = list(CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1))
    at_1 = [bars for stamp, bars in batches if stamp == minute(1)]
    assert [list(bars) for bars in at_1] == [["AAA", "BBB"], ["AAA", "BBB"], ["AAA"]]
    # file order: the volume of a row is 100 + its row number (3 warm-up rows come first)
    assert [bars["AAA"].volume for bars in at_1] == [104, 105, 106]
    assert [bars["BBB"].volume for bars in at_1[:2]] == [104, 105]


def test_the_same_repeated_minute_in_every_symbol_still_replays(tmp_path: Path) -> None:
    write_symbol(tmp_path, "AAA", [0, 1, 1, 2], 100.0)
    write_symbol(tmp_path, "BBB", [0, 1, 1, 2], 200.0)
    batches = list(CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1))
    assert [stamp for stamp, _ in batches] == [minute(k) for k in (0, 1, 1, 2)]
    assert all(list(bars) == ["AAA", "BBB"] for _, bars in batches)
    write_symbol(tmp_path, "CCC", [0, 1, 1, 2], 300.0)
    assert len(list(CsvBarFeed(tmp_path, ["CCC"], warmup_days=1))) == 4


def test_a_timestamp_going_backwards_is_refused_with_symbol_file_and_row(tmp_path: Path) -> None:
    write_symbol(tmp_path, "AAA", ALL, 100.0)
    write_symbol(tmp_path, "BBB", [0, 1, 3, 2, 4], 200.0)
    with pytest.raises(ValueError, match="BBB") as info:
        CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    message = str(info.value)
    assert "_BBB_offlinedata_" in message and "not sorted" in message
    assert "data row 7 " in message  # 3 warm-up rows, then minutes 0, 1, 3, 2: the 7th row
    assert str(minute(2)) in message and "\n" not in message


def test_a_shuffled_single_symbol_file_is_refused(tmp_path: Path) -> None:
    write_symbol(tmp_path, "AAA", [5, 4, 3, 2, 1, 0], 100.0)
    with pytest.raises(ValueError, match="not sorted"):
        CsvBarFeed(tmp_path, ["AAA"], warmup_days=1)


def test_missing_counts(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3, 4), CCC=without(0))
    assert feed.missing_counts() == {"AAA": 0, "BBB": 2, "CCC": 1}
    assert list(feed.missing_counts()) == ["AAA", "BBB", "CCC"]
    assert feed.missing_counts() is not feed.missing_counts()  # a copy


def test_missing_counts_of_an_aligned_feed_are_all_zero(tmp_path: Path) -> None:
    assert make_feed(tmp_path, AAA=ALL, BBB=ALL).missing_counts() == {"AAA": 0, "BBB": 0}


@pytest.mark.parametrize(
    ("name", "ks", "missing"),
    [
        ("first_replay_minute", without(0), 1),
        ("last_minute", without(9), 1),
        ("first_and_last", without(0, 9), 2),
        ("whole_middle", [0, 1, 8, 9], 6),
        ("one_bar_only", [5], 9),
    ],
)
def test_gap_positions(tmp_path: Path, name: str, ks: list[int], missing: int) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=ks)
    batches = list(feed)
    assert [stamp for stamp, _ in batches] == [minute(k) for k in ALL], name
    assert [k for k, (_, bars) in zip(ALL, batches, strict=True) if "BBB" in bars] == ks
    assert feed.missing_counts() == {"AAA": 0, "BBB": missing}


def test_a_minute_missing_from_every_symbol_is_not_replayed_or_counted(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(4), BBB=without(4, 5))
    assert [stamp for stamp, _ in feed] == [minute(k) for k in without(4)]  # minute 4 is nowhere
    assert feed.missing_counts() == {"AAA": 0, "BBB": 1}  # only minute 5; minute 4 is not counted


def test_whole_day_missing_for_one_symbol(tmp_path: Path) -> None:
    day2 = pd.Timedelta(days=1)
    for symbol, with_day2 in (("AAA", True), ("BBB", False)):
        stamps = [WARM_START + pd.Timedelta(minutes=i) for i in range(3)]
        stamps += [minute(k) for k in range(3)]
        if with_day2:
            stamps += [minute(k) + day2 for k in range(3)]
        rows = ["x,date,time,open,high,low,close,volume"]
        rows += [f"{i},{s},{int(s.timestamp())},1,2,0.5,1.5,{i + 1}" for i, s in enumerate(stamps)]
        (tmp_path / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    batches = list(feed)
    assert len(batches) == 6
    assert [list(bars) for _, bars in batches] == [["AAA", "BBB"]] * 3 + [["AAA"]] * 3
    assert feed.missing_counts() == {"AAA": 0, "BBB": 3}


def test_symbol_with_no_replay_bars_is_skipped_everywhere(tmp_path: Path) -> None:
    # BBB has warm-up bars only: every replay minute is missing for it. The feed allows
    # that (replay_counts shows the 0); the backtest command refuses it.
    feed = make_feed(tmp_path, AAA=ALL, BBB=[])
    assert feed.replay_counts() == {"AAA": 10, "BBB": 0}
    assert len(feed.warmup("BBB")) == 3
    assert [list(bars) for _, bars in feed] == [["AAA"]] * 10
    assert feed.missing_counts() == {"AAA": 0, "BBB": 10}


def test_no_replay_bars_in_any_symbol_is_an_empty_feed(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=[], BBB=[])
    assert list(feed) == []
    assert feed.missing_counts() == {"AAA": 0, "BBB": 0}
    assert feed.replay_counts() == {"AAA": 0, "BBB": 0}


def test_warmup_stays_per_symbol(tmp_path: Path) -> None:
    write_symbol(tmp_path, "AAA", ALL, 100.0, warm=3)
    write_symbol(tmp_path, "BBB", without(4), 200.0, warm=5)
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    assert (len(feed.warmup("AAA")), len(feed.warmup("BBB"))) == (3, 5)
    assert feed.missing_counts() == {"AAA": 0, "BBB": 1}
    assert len(list(feed)) == 10


def test_feed_is_deterministic_and_repeatable(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(2, 5), BBB=without(3), CCC=[0, 9])
    first = list(feed)
    assert list(feed) == first
    again = CsvBarFeed(tmp_path, ["AAA", "BBB", "CCC"], warmup_days=1)
    assert list(again) == first
    stamps = [stamp for stamp, _ in first]
    assert stamps == sorted(set(stamps))


# ------------------------------------------------------------------ the engine


class SpyAlgo:
    """Records every update (with a copy of its frame) and front-end call."""

    def __init__(self, name: str, ticker: str, log: list | None = None) -> None:
        self.name = name
        self.ticker = ticker
        self.broker: object | None = None
        self.frames: list[pd.DataFrame] = []
        self.frontend_calls = 0
        self.log = log if log is not None else []

    def update(self, frame: pd.DataFrame) -> None:
        self.frames.append(frame.copy())
        self.log.append(("update", self.name, frame["date"].iloc[-1]))

    def update_frontend(self) -> dict:
        self.frontend_calls += 1
        return {"idname": self.name}


class HookBroker(SimBroker):
    """A bar-aware SimBroker that logs what the engine tells it."""

    def __init__(self) -> None:
        super().__init__()
        self.bars: list[tuple[str, pd.Timestamp]] = []
        self.ended: dict[str, Bar] | None = None
        self.end_calls = 0

    def on_bar(self, symbol: str, bar: Bar) -> None:
        self.bars.append((symbol, bar.date))

    def on_end(self, last_bars: Mapping[str, Bar]) -> None:
        self.end_calls += 1
        self.ended = dict(last_bars)


def run_engine(
    feed: CsvBarFeed,
    algos: Sequence[object],
    broker: object | None = None,
    sink: object | None = None,
    **kwargs: object,
) -> None:
    Engine(EngineConfig(), feed, broker or SimBroker(), sink or NullSink(), algos,  # type: ignore[arg-type]
           **kwargs).run_backtest()  # type: ignore[arg-type]


def test_algo_on_missing_symbol_not_updated(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3, 4))
    a, b = SpyAlgo("a", "AAA"), SpyAlgo("b", "BBB")
    sink = RecordingSink(wants_algo_data=True)
    run_engine(feed, [a, b], sink=sink)
    assert len(a.frames) == 10 and a.frontend_calls == 10
    assert len(b.frames) == 8 and b.frontend_calls == 8
    # the sink heard of BBB only for the minutes it has a bar
    bbb_events = [e for e in sink.events if e.symbol == "BBB"]
    assert [e.bar.date for e in bbb_events] == [minute(k) for k in without(3, 4)]
    assert all(e.algo_data == [{"idname": "b"}] for e in bbb_events)
    assert len([e for e in sink.events if e.symbol == "AAA"]) == 10


def test_algo_frame_after_a_gap_holds_only_real_bars(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3, 4))
    b = SpyAlgo("b", "BBB")
    run_engine(feed, [SpyAlgo("a", "AAA"), b])
    assert [len(f) for f in b.frames] == [3 + 1 + i for i in range(8)]
    after_gap = b.frames[3]  # the update at minute 5, first bar after the gap
    assert list(after_gap["date"].iloc[-3:]) == [minute(1), minute(2), minute(5)]
    assert after_gap["date"].iloc[-1] - after_gap["date"].iloc[-2] == pd.Timedelta(minutes=3)
    expected = list(feed.warmup("BBB")["date"]) + [minute(k) for k in without(3, 4)]
    assert list(b.frames[-1]["date"]) == expected  # in order, nothing fabricated
    assert list(b.frames[-1]["open"].iloc[3:]) == [200.0 + k for k in without(3, 4)]
    assert list(b.frames[-1].index) == list(range(len(expected)))


def test_algo_at_first_and_last_missing_minutes(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(0, 9))
    b = SpyAlgo("b", "BBB")
    run_engine(feed, [SpyAlgo("a", "AAA"), b])
    assert [f["date"].iloc[-1] for f in b.frames] == [minute(k) for k in range(1, 9)]
    # the first bar BBB sees follows its own warm-up, two days earlier
    assert b.frames[0]["date"].iloc[-1] - b.frames[0]["date"].iloc[-2] > pd.Timedelta(days=1)


def test_update_order_is_deterministic_when_symbols_gap(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(1), BBB=without(0), CCC=ALL)
    log: list = []
    algos = [SpyAlgo("b", "BBB", log), SpyAlgo("a", "AAA", log), SpyAlgo("c", "CCC", log)]
    run_engine(feed, algos)
    first = [entry[1] for entry in log if entry[2] == minute(0)]
    second = [entry[1] for entry in log if entry[2] == minute(1)]
    third = [entry[1] for entry in log if entry[2] == minute(2)]
    assert (first, second, third) == (["a", "c"], ["b", "c"], ["a", "b", "c"])
    again: list = []
    run_engine(make_feed(tmp_path, AAA=without(1), BBB=without(0), CCC=ALL),
               [SpyAlgo("b", "BBB", again), SpyAlgo("a", "AAA", again), SpyAlgo("c", "CCC", again)])
    assert again == log


def test_brokers_not_told_about_missing_symbol(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(3))
    broker = HookBroker()
    run_engine(feed, [SpyAlgo("a", "AAA"), SpyAlgo("b", "BBB")], broker=broker)
    assert ("BBB", minute(3)) not in broker.bars
    assert ("AAA", minute(3)) in broker.bars
    assert len(broker.bars) == 19
    # per minute, symbols in feed order
    assert broker.bars[:2] == [("AAA", minute(0)), ("BBB", minute(0))]


def test_on_end_gets_each_symbols_last_bar(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(8, 9), CCC=[])
    broker = HookBroker()
    run_engine(feed, [SpyAlgo("a", "AAA"), SpyAlgo("b", "BBB"), SpyAlgo("c", "CCC")], broker=broker)
    assert broker.end_calls == 1
    assert broker.ended is not None
    assert list(broker.ended) == ["AAA", "BBB"]  # CCC never had a bar; feed order
    assert broker.ended["AAA"].date == minute(9)
    assert broker.ended["BBB"].date == minute(7)
    assert broker.ended["BBB"].open == 207.0


class MarkSpy(Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[pd.Timestamp, dict]] = []
        self.finishes: list[tuple[pd.Timestamp, dict]] = []

    def mark(self, time, closes_by_symbol):  # type: ignore[no-untyped-def]
        self.marks.append((time, dict(closes_by_symbol)))
        return super().mark(time, closes_by_symbol)

    def finish(self, time, closes_by_symbol, commission=None, **kwargs):  # type: ignore[no-untyped-def]
        self.finishes.append((time, dict(closes_by_symbol)))
        return super().finish(time, closes_by_symbol, commission, **kwargs)


class Scripted:
    """Opens at update number `open_at`, asks to close at `close_at` (both optional)."""

    def __init__(self, name: str, ticker: str, *, open_at: int | None = 1,
                 close_at: int | None = None, stop_offset: float = 50.0) -> None:
        self.name, self.ticker = name, ticker
        self.open_at, self.close_at, self.stop_offset = open_at, close_at, stop_offset
        self.n = 0
        self.ref: Hashable | None = None
        self.broker: PaperBroker
        self.seen_fills: list[int] = []
        self.ledger: Ledger | None = None

    def update(self, frame: pd.DataFrame) -> None:
        self.n += 1
        row = frame.iloc[-1]
        if self.ledger is not None:
            self.seen_fills.append(len(self.ledger.fills))
        if self.n == self.open_at:
            self.ref = self.broker.open_position(
                symbol=self.ticker, volume=10, direction=1, price=float(row["close"]),
                time=row["date"], limit=False, stop_price=float(row["close"]) - self.stop_offset,
                oca_group="g")
        if self.n == self.close_at and self.ref is not None:
            self.broker.close_position(self.ref, price=float(row["close"]), time=row["date"])

    def update_frontend(self) -> dict:
        return {}


def paper_engine(
    feed: CsvBarFeed, algos: Sequence[Scripted], ledger: Ledger
) -> dict[str, PaperBroker]:
    brokers = {a.name: PaperBroker(ledger.open_account(a.name, a.ticker, CAPITAL), FREE)
               for a in algos}
    for algo in algos:
        algo.ledger = ledger
    router = MappingRouter(brokers)
    Engine(EngineConfig(), feed, router, NullSink(), algos, ledger=ledger,
           end_commission=lambda idname, qty: brokers[idname].costs.commission(qty)
           ).run_backtest()
    return brokers


def test_pending_paper_entry_fills_at_next_bar_of_its_symbol(tmp_path: Path) -> None:
    # AAA opens at its update on minute 1; its next bars are minutes 4 (2, 3 missing)
    feed = make_feed(tmp_path, AAA=without(2, 3), BBB=ALL)
    ledger = Ledger()
    algo = Scripted("a", "AAA", open_at=2)
    brokers = paper_engine(feed, [algo, Scripted("b", "BBB", open_at=None)], ledger)
    entry = next(f for f in ledger.fills if f.reason == "entry")
    assert entry.time == minute(4)
    assert entry.price == Decimal("104")  # AAA's open at minute 4, not BBB's or a stale one
    assert algo.ref is not None and brokers["a"].position_state(algo.ref) == "open"
    # nothing filled while AAA had no bar (the update at minute 4 is its third)
    assert algo.seen_fills[2] == 1 and algo.seen_fills[1] == 0


def test_pending_paper_entry_across_a_missing_day_start_is_cancelled(tmp_path: Path) -> None:
    # An entry requested at the end of one session still cancels at AAA's next bar when
    # that bar is on another day, however many minutes are missing in between.
    day2 = pd.Timedelta(days=1)
    for symbol, with_day2 in (("AAA", True), ("BBB", True)):
        stamps = [WARM_START + pd.Timedelta(minutes=i) for i in range(3)]
        stamps += [minute(0), minute(1)]
        if with_day2:
            stamps += [minute(k) + day2 for k in range(3)]
        if symbol == "AAA":
            stamps.remove(minute(0) + day2)  # AAA's first bar of day 2 is missing
        rows = ["x,date,time,open,high,low,close,volume"]
        rows += [f"{i},{s},{int(s.timestamp())},100,101,99,100.5,{i + 1}"
                 for i, s in enumerate(stamps)]
        (tmp_path / f"_{symbol}_offlinedata_").write_text("\n".join(rows) + "\n")
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    ledger = Ledger()
    algo = Scripted("a", "AAA", open_at=2)  # requested on the last bar of day 1
    brokers = paper_engine(feed, [algo, Scripted("b", "BBB", open_at=None)], ledger)
    assert algo.ref is not None
    assert brokers["a"].position_state(algo.ref) == "cancelled"
    assert ledger.fills == ()
    cancelled = [e for e in ledger.orders if e.kind == "cancelled"]
    assert cancelled and all(e.time == minute(1) + day2 for e in cancelled)


def test_open_trade_is_not_touched_on_a_minute_without_its_bar(tmp_path: Path) -> None:
    # AAA's bar at minute 3 is missing: the broker is not stepped for AAA then, so no stop,
    # target or session-end handling happens; the trade is held until the end of the data.
    feed = make_feed(tmp_path, AAA=without(3), BBB=ALL)
    ledger = MarkSpy()
    algo = Scripted("a", "AAA", open_at=1, stop_offset=1.5)
    brokers = paper_engine(feed, [algo, Scripted("b", "BBB", open_at=None)], ledger)
    assert algo.ref is not None
    assert brokers["a"].position_state(algo.ref) == "open"
    assert [f.reason for f in ledger.fills] == ["entry", "end_of_data"]
    # one fill (the entry) seen at every update from the one after the entry on
    assert algo.seen_fills == [0, 1, 1, 1, 1, 1, 1, 1, 1]


@pytest.mark.parametrize(
    ("missing", "stop_minute", "price"), [([], 3, "96.5"), ([3], 4, "96")]
)
def test_stop_is_checked_only_against_real_bars(
    tmp_path: Path, missing: list[int], stop_minute: int, price: str
) -> None:
    """A falling AAA with a stop at 96.5: bar 3 (open 97, low 96) stops it at 96.5.

    Without bar 3 the stop fills on bar 4 (open 96): at that bar's time, at min(open, stop).
    """
    write_symbol(tmp_path, "AAA", [k for k in ALL if k not in missing], 100.0, slope=-1.0)
    write_symbol(tmp_path, "BBB", ALL, 200.0)
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    ledger = Ledger()
    algo = Scripted("a", "AAA", open_at=1, stop_offset=4.0)
    paper_engine(feed, [algo, Scripted("b", "BBB", open_at=None)], ledger)
    stop_fill = next(f for f in ledger.fills if f.reason == "stop")
    assert stop_fill.time == minute(stop_minute)
    assert stop_fill.price == Decimal(price)


def test_exiting_trade_fills_at_the_next_bar_of_its_symbol(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(3, 4), BBB=ALL)
    ledger = Ledger()
    algo = Scripted("a", "AAA", open_at=1, close_at=4)  # close asked at minute 2's update... see below
    paper_engine(feed, [algo, Scripted("b", "BBB", open_at=None)], ledger)
    # updates of AAA are minutes 0,1,2,5,6,...: update 4 is minute 5, so the exit is asked
    # there and fills at the open of minute 6
    exit_fill = next(f for f in ledger.fills if f.reason == "algo_exit")
    assert exit_fill.time == minute(6)
    assert exit_fill.price == Decimal("106")


def test_ledger_keeps_last_close_for_missing_symbol(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(3), BBB=ALL)
    ledger = MarkSpy()
    paper_engine(feed, [Scripted("a", "AAA", open_at=1), Scripted("b", "BBB", open_at=1)], ledger)
    marks = dict(ledger.marks)
    assert set(marks[minute(3)]) == {"BBB"}
    assert set(marks[minute(2)]) == {"AAA", "BBB"}
    assert len(ledger.marks) == 10
    # the gap minute leaves AAA's mark at its last close: its equity does not move
    def equity(account: str, when: pd.Timestamp) -> Decimal:
        (point,) = [p for p in ledger.equity_points if p.account == account and p.time == when]
        return point.equity
    assert equity("a", minute(3)) == equity("a", minute(2))
    assert equity("b", minute(3)) != equity("b", minute(2))
    # and the next AAA bar moves it again
    assert equity("a", minute(4)) != equity("a", minute(3))


def test_ledger_finish_closes_a_symbol_at_its_own_last_bar(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=without(8, 9))
    ledger = MarkSpy()
    paper_engine(feed, [Scripted("a", "AAA", open_at=1), Scripted("b", "BBB", open_at=1)], ledger)
    (stamp, closes) = ledger.finishes[0]
    assert stamp == minute(9)
    assert closes == {"AAA": 109.5, "BBB": 207.5}  # BBB's last bar is minute 7
    trips = {t.account: t for t in ledger.round_trips}
    # stamped at BBB's own last bar (minute 7), not the run's last timestamp (issue 144)
    assert trips["b"].exit_reason == "end_of_data" and trips["b"].exit_time == minute(7)
    assert trips["b"].exit_price == Decimal("207.5")


def test_symbol_with_no_bars_at_all_does_not_stop_the_run(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=ALL, BBB=[])
    a, b = SpyAlgo("a", "AAA"), SpyAlgo("b", "BBB")
    broker = HookBroker()
    run_engine(feed, [a, b], broker=broker)
    assert len(a.frames) == 10 and b.frames == []
    assert all(symbol == "AAA" for symbol, _ in broker.bars)
    assert broker.ended is not None and list(broker.ended) == ["AAA"]


def test_simulated_broker_is_not_touched_by_a_gap(tmp_path: Path) -> None:
    """A plain SimBroker has no bar hooks: an open trade just stays open through a gap."""
    feed = make_feed(tmp_path, AAA=without(3, 4), BBB=ALL)
    ledger = Ledger()
    sim = SimBroker(account=ledger.open_account("a", "AAA", CAPITAL))
    algo = Scripted("a", "AAA", open_at=1)
    algo.broker = sim  # type: ignore[assignment]
    run_engine(feed, [algo, SpyAlgo("b", "BBB")], broker=sim, ledger=ledger)
    assert [c.method for c in sim.calls] == ["open_position"]
    assert [f.reason for f in ledger.fills if f.reason != "end_of_data"] == ["entry"]


# ------------------------------------------------------------------- the CLI


VERSION = "0123456789abcdef0123456789abcdef01234567"
CONFIG = [{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}]
DROPPED = (390, 400, 401, 500, 699)  # MSFT rows removed: the first replay bar, the last, three more


@pytest.fixture
def gapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    env = Env(tmp_path, monkeypatch)
    env.setup(CONFIG, ["AAPL", "MSFT"], rows=700)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[list(DROPPED)]).to_csv(path)
    return env


def test_cli_series_history_matches_bars_each_algo_saw(
    gapped: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, list] = {}
    real = cli.extract_series

    def spy(algo, bar_times):  # type: ignore[no-untyped-def]
        seen[algo.ticker] = list(bar_times)
        return real(algo, bar_times)

    monkeypatch.setattr(cli, "extract_series", spy)
    db = tmp_path / "store" / "runs.db"
    assert gapped.run("--db", str(db)) == 0
    feed = CsvBarFeed(gapped.data, ["AAPL", "MSFT"], 2)
    replay = list(feed)
    for symbol in ("AAPL", "MSFT"):
        expected = [*feed.warmup(symbol)["date"].tolist(),
                    *[stamp for stamp, bars in replay if symbol in bars]]
        assert seen[symbol] == expected
    assert len(seen["AAPL"]) - len(seen["MSFT"]) == len(DROPPED)
    store = open_store(str(db), env={})
    try:
        stored = {info.account for info in store.load_run(1).series}
    finally:
        store.close()
    assert stored == {EMA["idname"], EMA_MSFT["idname"]}  # both algos' series were stored


def test_cli_prints_missing_minutes(gapped: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert gapped.run() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "replayed 310 bars for AAPL, MSFT"
    assert lines[1] == "MSFT: no bar at 5 of 310 minutes (skipped for those minutes)"
    assert not any(line.startswith("AAPL:") and "no bar" in line for line in lines)
    assert lines[2].startswith("AAPL-10/30EMA: ")
    # the run's output files are untouched by the extra line
    import json
    assert set(json.loads(gapped.trades_bytes())) == {EMA["idname"], EMA_MSFT["idname"]}


def test_cli_prints_nothing_extra_for_aligned_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.setup(CONFIG, ["AAPL", "MSFT"], rows=700)
    assert env.run() == 0
    out = capsys.readouterr().out
    assert "no bar at" not in out
    assert out.splitlines()[0] == "replayed 310 bars for AAPL, MSFT"
