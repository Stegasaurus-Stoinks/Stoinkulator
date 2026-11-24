"""LiveBarAligner: minute batches from per-symbol closed bars (pure, time passed in)."""
from __future__ import annotations

import random
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.feed import COLUMNS, Bar, CsvBarFeed, read_symbol_bars
from MainStoinker.engine.live_feed import (
    ACCEPTED,
    DEFAULT_ALIGN_WAIT,
    LATE,
    REFUSED,
    LiveBarAligner,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "OfflineData"
BASE = 1_723_815_000  # 2024-08-16 09:30 New York, the first bar of the offline data
START = pd.Timestamp("2024-08-16 09:30:00")


def minute(i: int) -> int:
    """Epoch start of minute number `i` (0 = 09:30)."""
    return BASE + 60 * i


def mk(i: int, price: float = 10.0) -> Bar:
    return Bar(START + pd.Timedelta(minutes=i), minute(i), price, price + 1, price - 1, price, 100 + i)


def end(i: int) -> int:
    return minute(i) + 60


def test_default_wait_is_100_seconds() -> None:
    assert DEFAULT_ALIGN_WAIT == 100


def test_release_when_all_decided() -> None:
    al = LiveBarAligner(["A", "B"])
    a0, b0 = mk(0), mk(0, 20)
    assert al.bar("A", a0) == ACCEPTED
    assert al.poll(end(0)) == []  # B undecided, wait not over
    assert al.bar("B", b0) == ACCEPTED
    out = al.poll(end(0))
    assert out == [(a0.date, {"A": a0, "B": b0})]
    assert al.poll(end(0)) == []  # released once
    assert al.fed_counts() == {"A": 1, "B": 1}


def test_release_after_align_wait_without_slow_symbol() -> None:
    al = LiveBarAligner(["A", "B"])
    a0 = mk(0)
    al.bar("A", a0)
    assert al.poll(end(0) + 99.9) == []
    out = al.poll(end(0) + 100)
    assert out == [(a0.date, {"A": a0})]
    assert al.released_without_counts() == {"A": 0, "B": 1}
    assert al.fed_counts() == {"A": 1, "B": 0}


def test_align_wait_is_configurable() -> None:
    al = LiveBarAligner(["A", "B"], align_wait=10)
    al.bar("A", mk(0))
    assert al.poll(end(0) + 9) == []
    assert len(al.poll(end(0) + 10)) == 1


def test_late_bar_counted_not_fed() -> None:
    al = LiveBarAligner(["A", "B"])
    al.bar("A", mk(0))
    assert len(al.poll(end(0) + 100)) == 1
    assert al.bar("B", mk(0, 20)) == LATE
    assert al.late_counts() == {"A": 0, "B": 1}
    assert al.poll(end(0) + 100) == []
    assert al.fed_counts() == {"A": 1, "B": 0}
    # the late bar still decided B through minute 0: B's next bar is accepted
    assert al.bar("B", mk(1, 20)) == ACCEPTED


def test_late_when_time_passed_even_without_any_bar_for_the_minute() -> None:
    al = LiveBarAligner(["A", "B"])
    assert al.poll(end(0) + 100) == []  # nothing pending, but minute 0 is now released
    assert al.bar("A", mk(0)) == LATE
    assert al.poll(end(0) + 200) == []


def test_duplicate_and_backwards_bars_refused() -> None:
    al = LiveBarAligner(["A", "B"])
    first = mk(3)
    assert al.bar("A", first) == ACCEPTED
    assert al.bar("A", mk(3, 99)) == REFUSED  # duplicate
    assert al.bar("A", mk(2)) == REFUSED  # backwards
    al.decided("B", minute(5))
    assert al.bar("B", mk(4)) == REFUSED  # at or before its decided point
    assert al.refused_counts() == {"A": 2, "B": 1}
    out = al.poll(end(3))
    assert out == [(first.date, {"A": first})]  # the original bar, not the duplicate


def test_unknown_symbol_raises() -> None:
    al = LiveBarAligner(["A"])
    with pytest.raises(KeyError):
        al.bar("Z", mk(0))
    with pytest.raises(KeyError):
        al.decided("Z", 1)
    with pytest.raises(KeyError):
        al.stopped("Z")


def test_stopped_symbol_no_longer_holds_minutes() -> None:
    al = LiveBarAligner(["A", "B"])
    a0 = mk(0)
    al.bar("A", a0)
    assert al.poll(end(0)) == []
    al.stopped("B")
    assert al.poll(end(0)) == [(a0.date, {"A": a0})]
    assert al.released_without_counts() == {"A": 0, "B": 0}  # stopped: not counted
    assert al.bar("B", mk(1)) == REFUSED  # nothing more is taken from it
    a1 = mk(1)
    al.bar("A", a1)
    assert al.poll(end(1)) == [(a1.date, {"A": a1})]


def test_all_stopped_flushes_everything() -> None:
    al = LiveBarAligner(["A", "B"])
    al.bar("A", mk(0))
    al.bar("A", mk(1))
    al.stopped("A")
    al.stopped("B")
    out = al.poll(0)
    assert [stamp for stamp, _ in out] == [mk(0).date, mk(1).date]
    assert al.bar("A", mk(2)) == REFUSED


def test_dropped_minute_decided_by_later_update_releases() -> None:
    al = LiveBarAligner(["A", "B"])
    a = [mk(0), mk(1), mk(2)]
    for bar in a:
        al.bar("A", bar)
    b0 = mk(0, 20)
    al.bar("B", b0)
    assert al.poll(end(2)) == [(b0.date, {"A": a[0], "B": b0})]  # B undecided from minute 1 on
    al.decided("B", minute(1))  # B's minute 1 was dropped
    assert al.poll(end(2)) == [(a[1].date, {"A": a[1]})]
    assert al.released_without_counts()["B"] == 1
    b2 = mk(2, 20)
    al.bar("B", b2)
    assert al.poll(end(2)) == [(b2.date, {"A": a[2], "B": b2})]


def test_decided_never_goes_backwards() -> None:
    al = LiveBarAligner(["A", "B"])
    al.bar("A", mk(1))
    al.decided("B", minute(1))
    al.decided("B", minute(0))  # ignored
    assert len(al.poll(0)) == 1


def test_keys_in_symbol_order_not_arrival_order() -> None:
    al = LiveBarAligner(["A", "B", "C"])
    for name in ("C", "A", "B"):
        al.bar(name, mk(0))
    ((_, batch),) = al.poll(0)
    assert list(batch) == ["A", "B", "C"]


def test_batches_strictly_increasing_and_never_empty() -> None:
    al = LiveBarAligner(["A", "B"])
    got: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
    for i in range(6):
        if i % 2 == 0:
            al.bar("A", mk(i))
        al.bar("B", mk(i, 20)) if i != 3 else al.decided("B", minute(3))
        got += al.poll(end(i) + (150 if i == 4 else 0))
    al.stopped("A")
    al.stopped("B")
    got += al.poll(0)
    stamps = [stamp for stamp, _ in got]
    assert stamps == sorted(set(stamps))
    assert all(batch for _, batch in got)
    assert stamps  # something was released


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        LiveBarAligner([])
    with pytest.raises(ValueError):
        LiveBarAligner(["A"], align_wait=-1)
    assert LiveBarAligner(["A", "B", "A"]).symbols() == ["A", "B"]


def test_equivalent_to_csv_feed_on_complete_data() -> None:
    symbols = ["AAPL", "MSFT", "TSLA"]
    feed = CsvBarFeed(DATA_DIR, symbols)
    expected = list(feed)
    assert expected
    al = LiveBarAligner(symbols)
    got: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
    for symbol in symbols:  # symbol-major: the worst arrival order for alignment
        frame = read_symbol_bars(DATA_DIR, symbol).iloc[len(feed.warmup(symbol)):]
        for row in zip(*(frame[c].tolist() for c in COLUMNS), strict=True):
            assert al.bar(symbol, Bar(*row)) == ACCEPTED
            got += al.poll(0)
        al.stopped(symbol)
        got += al.poll(0)
    assert got == expected
    assert al.fed_counts() == feed.replay_counts()
    assert al.late_counts() == dict.fromkeys(symbols, 0)


def test_equivalent_to_csv_feed_with_minute_wise_arrival() -> None:
    symbols = ["AAPL", "MSFT", "TSLA"]
    feed = CsvBarFeed(DATA_DIR, symbols)
    al = LiveBarAligner(symbols)
    got: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
    for _stamp, batch in feed:
        for symbol in symbols:
            if symbol in batch:
                al.bar(symbol, batch[symbol])
            else:
                al.decided(symbol, int(batch[next(iter(batch))].time))
        got += al.poll(0)
    assert got == list(feed)
    assert al.released_without_counts() == feed.missing_counts()


def test_random_interleavings_hold_the_invariants() -> None:
    symbols = ["A", "B", "C"]
    slow = "C"
    n_minutes = 40
    for seed in range(30):
        rng = random.Random(seed)
        # known schedule: per symbol, which minutes have a bar (the rest are dropped)
        schedule = {
            s: {i for i in range(n_minutes) if rng.random() > (0.3 if s != slow else 0.5)}
            for s in symbols
        }
        # per symbol its call script: ("bar", i) / ("decided", i) in minute order, then stop
        scripts: dict[str, list[tuple[str, int]]] = {}
        for s in symbols:
            script: list[tuple[str, int]] = []
            for i in range(n_minutes):
                if i in schedule[s]:
                    script.append(("bar", i))
                elif rng.random() < 0.5:
                    script.append(("decided", i))
            script.append(("stopped", n_minutes))
            scripts[s] = script
        al = LiveBarAligner(symbols)
        given: dict[tuple[str, int], Bar] = {}
        accepted: set[tuple[str, int]] = set()
        batches: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
        pos = dict.fromkeys(symbols, 0)
        now = float(BASE)
        stopped: set[str] = set()
        while len(stopped) < len(symbols):
            action = rng.random()
            if action < 0.25:
                now += rng.choice([1, 10, 60, 150])
                batches += al.poll(now)
                continue
            candidates = [s for s in symbols if s not in stopped]
            # the slow symbol is picked rarely
            weights = [1 if s == slow else 6 for s in candidates]
            symbol = rng.choices(candidates, weights)[0]
            kind, i = scripts[symbol][pos[symbol]]
            pos[symbol] += 1
            if kind == "bar":
                bar = mk(i, rng.random() * 100)
                given[(symbol, i)] = bar
                if al.bar(symbol, bar) in (ACCEPTED, LATE):
                    accepted.add((symbol, i))
            elif kind == "decided":
                al.decided(symbol, minute(i))
            else:
                al.stopped(symbol)
                stopped.add(symbol)
            now += rng.choice([0, 0, 5])
        batches += al.poll(now)

        stamps = [stamp for stamp, _ in batches]
        assert all(a < b for a, b in zip(stamps, stamps[1:], strict=False))
        assert all(batch for _, batch in batches)
        seen: set[tuple[str, int]] = set()
        for stamp, batch in batches:
            assert list(batch) == [s for s in symbols if s in batch]
            for symbol, bar in batch.items():
                i = int((stamp - START) / pd.Timedelta(minutes=1))
                key = (symbol, i)
                assert given.get(key) is bar  # only bars that were given, unchanged
                assert key not in seen  # at most once
                seen.add(key)
        fed = al.fed_counts()
        late = al.late_counts()
        assert sum(fed.values()) == len(seen)
        # every bar given is fed or counted late: nothing vanishes
        assert sum(fed.values()) + sum(late.values()) == len(accepted)
        assert al.pending_minutes() == 0
