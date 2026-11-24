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
    DEFAULT_MAX_CLOCK_AHEAD,
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
    assert al.bar("B", mk(4)) == ACCEPTED  # before its decided point: no longer refused (#205)
    assert al.refused_counts() == {"A": 2, "B": 0}
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


# ---------------------------------------------------------------------------
# #205: call order, clock guard, date/time agreement
# ---------------------------------------------------------------------------

def counters_sum(al: LiveBarAligner, symbol: str) -> int:
    return (
        al.fed_counts()[symbol]
        + al.late_counts()[symbol]
        + al.late_after_decided_counts()[symbol]
        + al.refused_counts()[symbol]
    )


@pytest.mark.parametrize("decided_first", [True, False], ids=["decided_then_bar", "bar_then_decided"])
def test_bar_and_decided_for_the_same_minute_in_either_order(decided_first: bool) -> None:
    al = LiveBarAligner(["A", "B"])
    a0, b0 = mk(0), mk(0, 20)
    for symbol, bar in (("A", a0), ("B", b0)):
        if decided_first:
            al.decided(symbol, minute(0))
            assert al.bar(symbol, bar) == ACCEPTED
        else:
            assert al.bar(symbol, bar) == ACCEPTED
            al.decided(symbol, minute(0))
    assert al.poll(end(0)) == [(a0.date, {"A": a0, "B": b0})]
    assert al.refused_counts() == {"A": 0, "B": 0}
    assert al.fed_counts() == {"A": 1, "B": 1}


def test_decided_with_the_minute_end_does_not_lose_the_next_bar() -> None:
    al = LiveBarAligner(["A"])
    a0, a1 = mk(0), mk(1)
    assert al.bar("A", a0) == ACCEPTED
    al.decided("A", end(0))  # the end of minute 0 is the start of minute 1
    assert al.bar("A", a1) == ACCEPTED  # was refused before #205
    assert al.poll(end(1)) == [(a0.date, {"A": a0}), (a1.date, {"A": a1})]
    assert al.refused_counts() == {"A": 0}


def test_bar_behind_decided_point_is_late_with_its_own_counter_once_released() -> None:
    al = LiveBarAligner(["A", "B"])
    al.decided("A", minute(2))
    al.decided("B", minute(2))
    assert al.poll(end(2)) == []  # nothing pending, but minutes 0 to 2 are released
    assert al.bar("A", mk(1)) == LATE  # released, and at or before A's decided point
    assert al.late_after_decided_counts() == {"A": 1, "B": 0}
    assert al.late_counts() == {"A": 0, "B": 0}
    assert al.bar("A", mk(1)) == REFUSED  # now a duplicate
    a3 = mk(3)
    assert al.bar("A", a3) == ACCEPTED
    assert al.fed_counts() == {"A": 0, "B": 0}
    assert counters_sum(al, "A") == 2  # given 3 bars, one pending


def test_bar_behind_decided_point_but_pending_is_fed() -> None:
    al = LiveBarAligner(["A", "B"])
    b1 = mk(1, 20)
    assert al.bar("B", b1) == ACCEPTED
    al.decided("A", minute(1))
    a0 = mk(0)
    assert al.bar("A", a0) == ACCEPTED  # at or before decided, minute 0 not released yet
    out = al.poll(0)
    assert out == [(a0.date, {"A": a0}), (b1.date, {"B": b1})]


def test_duplicate_and_backwards_still_refused_after_decided() -> None:
    al = LiveBarAligner(["A"])
    assert al.bar("A", mk(3)) == ACCEPTED
    al.decided("A", minute(9))
    assert al.bar("A", mk(3, 99)) == REFUSED
    assert al.bar("A", mk(2)) == REFUSED
    assert al.refused_counts() == {"A": 2}
    assert al.late_after_decided_counts() == {"A": 0}


# --- clock guard -----------------------------------------------------------

def test_default_clock_bound_is_600_seconds() -> None:
    assert DEFAULT_MAX_CLOCK_AHEAD == 600


@pytest.mark.parametrize(
    "bad_now",
    [
        (BASE + 60) * 1000.0,  # milliseconds
        BASE + 5 * 3600.0,  # local time five hours ahead
        BASE + 10**9,
        float("nan"),
        float("inf"),
        -float("inf"),
    ],
    ids=["milliseconds", "five_hours", "far_future", "nan", "inf", "minus_inf"],
)
def test_poll_refuses_a_wrong_clock_and_changes_nothing(bad_now: float) -> None:
    al, twin = LiveBarAligner(["A", "B"]), LiveBarAligner(["A", "B"])
    for each in (al, twin):
        each.bar("A", mk(0))
        each.bar("B", mk(0, 20))
        each.bar("A", mk(1))
    with pytest.raises(ValueError, match="poll time"):
        al.poll(bad_now)
    # the refused call left no trace: the same calls from here give the twin's results
    assert al.pending_minutes() == twin.pending_minutes()
    for step in (end(0), end(1), end(1) + 100, end(1) + 200):
        assert al.poll(step) == twin.poll(step)
    assert al.bar("B", mk(1, 20)) == twin.bar("B", mk(1, 20)) == LATE
    assert al.bar("B", mk(5, 20)) == twin.bar("B", mk(5, 20)) == ACCEPTED
    for name in (
        "fed_counts",
        "late_counts",
        "refused_counts",
        "released_without_counts",
        "late_after_decided_counts",
    ):
        assert getattr(al, name)() == getattr(twin, name)()


def test_poll_after_a_refused_clock_behaves_as_if_it_never_happened() -> None:
    al = LiveBarAligner(["A", "B"])
    a0 = mk(0)
    al.bar("A", a0)
    with pytest.raises(ValueError):
        al.poll((BASE + 60) * 1000)  # the mistake that used to make every later bar late
    assert al.poll(end(0) + 99) == []
    b0 = mk(0, 20)
    assert al.bar("B", b0) == ACCEPTED  # not late
    assert al.poll(end(0)) == [(a0.date, {"A": a0, "B": b0})]


def test_clock_bound_is_measured_from_the_newest_bar_or_decided_point() -> None:
    al = LiveBarAligner(["A", "B"], max_clock_ahead=300)
    al.bar("A", mk(2))
    al.poll(minute(2) + 300)  # exactly at the bound: fine
    with pytest.raises(ValueError):
        al.poll(minute(2) + 300.5)
    al.decided("B", minute(6))  # a newer decided point moves the reference
    al.poll(minute(6) + 300)
    with pytest.raises(ValueError):
        al.poll(minute(6) + 301)
    al.poll(minute(2))  # a clock behind the data is not refused


def test_clock_bound_covers_every_legitimate_poll() -> None:
    # A bar of minute M is closed by time up to 90 s after M + 60, the aligner waits 100 s.
    assert 60 + 90 + 1 < DEFAULT_MAX_CLOCK_AHEAD
    assert 60 + DEFAULT_ALIGN_WAIT + 1 < DEFAULT_MAX_CLOCK_AHEAD
    al = LiveBarAligner(["A"])
    al.bar("A", mk(0))
    al.poll(end(0) + 90 + 1)


def test_clock_guard_is_off_without_a_reference_and_when_all_stopped() -> None:
    al = LiveBarAligner(["A"])
    assert al.poll(BASE * 1000.0) == []  # nothing seen yet: nothing to compare with
    al = LiveBarAligner(["A"])
    a0 = mk(0)
    al.bar("A", a0)
    al.stopped("A")
    assert al.poll(BASE * 1000.0) == [(a0.date, {"A": a0})]  # the clock decides nothing any more


def test_clock_bound_validation() -> None:
    with pytest.raises(ValueError):
        LiveBarAligner(["A"], max_clock_ahead=-1)
    with pytest.raises(ValueError):
        LiveBarAligner(["A"], max_clock_ahead=float("nan"))
    al = LiveBarAligner(["A"], max_clock_ahead=0)
    al.bar("A", mk(0))
    al.poll(minute(0))
    with pytest.raises(ValueError):
        al.poll(minute(0) + 1)


# --- date and time must agree ----------------------------------------------

def test_bar_whose_date_and_time_disagree_is_refused_without_effect() -> None:
    al = LiveBarAligner(["A", "B"])
    good = mk(0)
    wrong_minute = Bar(good.date + pd.Timedelta(minutes=1), good.time, 1, 1, 1, 1, 1)
    utc_naive = Bar(good.date + pd.Timedelta(hours=4), good.time, 1, 1, 1, 1, 1)  # UTC, not New York
    aware = Bar(good.date.tz_localize("America/New_York"), good.time, 1, 1, 1, 1, 1)
    nat = Bar(pd.NaT, good.time, 1, 1, 1, 1, 1)
    for bad in (wrong_minute, utc_naive, aware, nat):
        assert al.bar("A", bad) == REFUSED
    assert al.refused_counts() == {"A": 4, "B": 0}
    assert al.pending_minutes() == 0
    assert al.poll(end(0) + 100) == []
    # nothing was recorded: the symbol's last bar is unchanged, so the right bar is not a duplicate
    # (it is late here only because the poll above passed the deadline)
    assert al.bar("A", good) == LATE
    assert counters_sum(al, "A") == 5


def test_date_time_agreement_holds_across_daylight_saving_time() -> None:
    winter = pd.Timestamp("2024-01-16 09:30:00")
    epoch = int(winter.tz_localize("America/New_York").timestamp())
    al = LiveBarAligner(["A"])
    assert al.bar("A", Bar(winter, epoch, 1, 1, 1, 1, 1)) == ACCEPTED
    assert al.bar("A", Bar(winter + pd.Timedelta(minutes=1), epoch + 60, 1, 1, 1, 1, 1)) == ACCEPTED
    summer_epoch = int(pd.Timestamp("2024-07-16 09:30:00").tz_localize("America/New_York").timestamp())
    assert al.bar("A", Bar(pd.Timestamp("2024-07-16 08:30:00"), summer_epoch, 1, 1, 1, 1, 1)) == REFUSED


# --- model-based property tests, both call orders ---------------------------

class Model:
    """Wraps an aligner with an independent model and checks every call and poll."""

    def __init__(self, symbols: list[str], wait: float) -> None:
        self.symbols = symbols
        self.wait = wait
        self.al = LiveBarAligner(symbols, wait)
        self.active = set(symbols)
        self.last_bar = dict.fromkeys(symbols, float("-inf"))
        self.dec = dict.fromkeys(symbols, float("-inf"))
        self.newest = float("-inf")
        self.released = float("-inf")  # everything at or before this minute is released
        self.pending: dict[int, set[str]] = {}
        self.given = dict.fromkeys(symbols, 0)
        self.bars: dict[int, tuple[str, str, Bar]] = {}  # id(bar) -> (symbol, status, bar)
        self.fed: set[int] = set()
        self.batches: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
        self.refused_polls = 0

    def bar(self, symbol: str, i: int, price: float = 10.0) -> str:
        bar = mk(i, price)
        self.given[symbol] += 1
        got = self.al.bar(symbol, bar)
        m = minute(i)
        if symbol not in self.active or m <= self.last_bar[symbol]:
            expected = REFUSED
        else:
            self.last_bar[symbol] = m
            self.dec[symbol] = max(self.dec[symbol], m)
            self.newest = max(self.newest, m)
            if m <= self.released:
                expected = LATE
            else:
                expected = ACCEPTED
                self.pending.setdefault(m, set()).add(symbol)
        assert got == expected, (symbol, i, got, expected)
        self.bars[id(bar)] = (symbol, got, bar)
        return got

    def decided(self, symbol: str, through: int) -> None:
        self.al.decided(symbol, through)
        if symbol in self.active and through > self.dec[symbol]:
            self.dec[symbol] = through
            self.newest = max(self.newest, through)

    def stopped(self, symbol: str) -> None:
        self.al.stopped(symbol)
        self.active.discard(symbol)

    def poll(self, now: float) -> None:
        if self.active and self.newest != float("-inf") and now - self.newest > DEFAULT_MAX_CLOCK_AHEAD:
            before = (self.al.fed_counts(), self.al.late_counts(), self.al.pending_minutes())
            with pytest.raises(ValueError):
                self.al.poll(now)
            assert before == (self.al.fed_counts(), self.al.late_counts(), self.al.pending_minutes())
            self.refused_polls += 1
            return
        if self.active:
            through = max(min(self.dec[s] for s in self.active), now - 60 - self.wait)
        else:
            through = float("inf")
        expected = sorted(m for m in self.pending if m <= through)
        before = self.released
        out = self.al.poll(now)
        stamps = [stamp for stamp, _ in out]
        assert stamps == sorted(set(stamps)), "batches not strictly increasing"
        assert [int(next(iter(b.values())).time) for _, b in out] == expected, (now, through)
        for stamp, batch in out:
            assert batch, "empty batch"
            assert list(batch) == [s for s in self.symbols if s in batch]
            minutes = {int(b.time) for b in batch.values()}
            assert len(minutes) == 1, "mixed minutes"
            (m,) = minutes
            assert all(b.date == stamp for b in batch.values())
            if self.active:
                assert m > before, "fed for a released minute"
            assert set(batch) == self.pending.pop(m)
            for symbol, bar in batch.items():
                owner, status, _ = self.bars[id(bar)]
                assert owner == symbol and status == ACCEPTED, "fed a bar that was not accepted"
                assert id(bar) not in self.fed, "fed twice"
                self.fed.add(id(bar))
        if self.active:
            self.released = max(self.released, through)
        elif out:
            self.released = max(self.released, int(next(iter(out[-1][1].values())).time))
        self.batches += out

    def finish(self) -> None:
        """Liveness: with the clock past every deadline, everything accepted is fed."""
        if self.active:
            self.poll(max(self.newest, BASE) + 60 + self.wait)
            self.poll(max(self.newest, BASE) + 60 + self.wait + 1)
        else:
            self.poll(0)
        assert self.al.pending_minutes() == 0
        assert not self.pending
        accepted = {i for i, (_, status, _) in self.bars.items() if status == ACCEPTED}
        assert accepted == self.fed, "an accepted bar was never fed"
        al = self.al
        for symbol in self.symbols:
            counted = {
                status: sum(1 for s, st, _ in self.bars.values() if s == symbol and st == status)
                for status in (ACCEPTED, LATE, REFUSED)
            }
            assert al.fed_counts()[symbol] == counted[ACCEPTED]
            assert al.late_counts()[symbol] + al.late_after_decided_counts()[symbol] == counted[LATE]
            assert al.refused_counts()[symbol] == counted[REFUSED]
            assert counters_sum(al, symbol) == self.given[symbol]


@pytest.mark.parametrize("seed", range(300))
def test_adversarial_calls_in_both_orders_hold_the_invariants(seed: int) -> None:
    rng = random.Random(seed)
    symbols = [f"S{k}" for k in range(rng.randint(1, 4))]
    model = Model(symbols, rng.choice([100.0, 100.0, 0.0, 30.0]))
    n_minutes = 40
    nxt = dict.fromkeys(symbols, 0)
    now = float(BASE)
    for _ in range(rng.randint(50, 300)):
        roll = rng.random()
        symbol = rng.choice(symbols)
        if roll < 0.45:
            i = nxt[symbol] + (rng.randint(0, 3) if rng.random() < 0.3 else 0)
            if rng.random() < 0.05:
                i = max(0, i - rng.randint(1, 3))  # backwards or duplicate
            if i >= n_minutes:
                continue
            # the call order under test: decided before the bar, after it, or not at all;
            # the decided point is the minute's start, its end, or something older
            through = minute(i) + rng.choice([0, 0, 60, -60, 600])
            order = rng.choice(["decided_first", "bar_first", "bar_only"])
            if order == "decided_first":
                model.decided(symbol, through)
            model.bar(symbol, i, rng.random() * 100)
            if order == "bar_first":
                model.decided(symbol, through)
            nxt[symbol] = i + 1
        elif roll < 0.6:
            j = nxt[symbol] + rng.choice([0, 0, 1, 5, 30])  # decided, sometimes far ahead
            model.decided(symbol, minute(j))
            if rng.random() < 0.5:
                nxt[symbol] = j + 1
        elif roll < 0.63:
            model.stopped(symbol)
        else:
            now += rng.choice([0, 1, 5, 5, 60, 100, 161, 600, 1200, -3, -60])  # jumps and skew
            if rng.random() < 0.6:
                model.poll(now)
    model.finish()


def test_the_property_test_exercises_the_clock_guard_and_late_bars() -> None:
    refused = late = 0
    for seed in range(300):
        rng = random.Random(seed)
        symbols = ["A", "B"]
        model = Model(symbols, 100.0)
        for _ in range(100):
            symbol = rng.choice(symbols)
            if rng.random() < 0.5:
                model.bar(symbol, rng.randint(0, 20), 1.0)
            else:
                model.poll(BASE + rng.choice([0, 100, 300, 1000, 5000, 100000]))
        refused += model.refused_polls
        late += sum(model.al.late_counts().values()) + sum(model.al.late_after_decided_counts().values())
    assert refused > 100
    assert late > 100


@pytest.mark.parametrize("order", ["decided_first", "bar_first", "bar_only"])
def test_realistic_arrival_never_late_and_always_fed(order: str) -> None:
    """Bars close 4 to 5 s after their minute, some by time up to 90 s after; clock off by 1 s."""
    for seed in range(60):
        rng = random.Random(seed)
        symbols = ["AAPL", "MSFT", "TSLA"][: rng.randint(1, 3)]
        model = Model(symbols, 100.0)
        events = []
        for symbol in symbols:
            previous = 0.0
            for i in range(60):
                if rng.random() < 0.1:
                    continue  # a dropped minute
                delay = rng.uniform(4, 5) if rng.random() < 0.9 else rng.uniform(5, 90)
                arrival = max(previous, end(i) + delay)  # per symbol, bars stay in order
                previous = arrival
                events.append((arrival, symbol, i))
        events.sort()
        for arrival, symbol, i in events:
            model.poll(int(arrival) + rng.choice([-1, 0, 1]))
            if order == "decided_first":
                model.decided(symbol, minute(i))
            assert model.bar(symbol, i) == ACCEPTED
            if order == "bar_first":
                model.decided(symbol, minute(i))
            model.poll(int(arrival) + rng.choice([-1, 0, 1]))
        model.finish()
        assert sum(model.al.late_counts().values()) + sum(model.al.late_after_decided_counts().values()) == 0
        assert sum(model.al.refused_counts().values()) == 0


@pytest.mark.parametrize("delay,expected", [(98, ACCEPTED), (99, ACCEPTED), (100, LATE)])
def test_release_by_time_is_exactly_at_the_deadline(delay: int, expected: str) -> None:
    al = LiveBarAligner(["A", "B"])
    al.bar("B", mk(0))
    al.poll(end(0) + delay)
    assert al.bar("A", mk(0)) == expected


def test_startup_backfill_before_the_first_poll_is_all_fed() -> None:
    symbols = ["A", "B", "C"]
    al = LiveBarAligner(symbols)
    for symbol in symbols:
        for i in range(20):
            assert al.bar(symbol, mk(i)) == ACCEPTED
    out = al.poll(end(19) + 5)
    assert [int(next(iter(b.values())).time) for _, b in out] == [minute(i) for i in range(20)]
    assert all(list(b) == symbols for _, b in out)


def test_last_minute_dropped_by_one_symbol() -> None:
    al = LiveBarAligner(["A", "B"])
    al.bar("A", mk(389))
    al.bar("B", mk(388))
    assert len(al.poll(end(389) + 99)) == 1  # minute 388, decided by both
    assert al.poll(end(389) + 99) == []
    out = al.poll(end(389) + 100)
    assert len(out) == 1 and list(out[0][1]) == ["A"]


@pytest.mark.parametrize("decided_first", [True, False], ids=["decided_then_bar", "bar_then_decided"])
def test_equivalent_to_csv_feed_minute_wise_in_either_call_order(decided_first: bool) -> None:
    symbols = ["AAPL", "MSFT", "TSLA"]
    feed = CsvBarFeed(DATA_DIR, symbols)
    al = LiveBarAligner(symbols)
    got: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
    for _stamp, batch in feed:
        epoch = int(next(iter(batch.values())).time)
        for symbol in symbols:
            if decided_first:
                al.decided(symbol, epoch)
            if symbol in batch:
                assert al.bar(symbol, batch[symbol]) == ACCEPTED
            if not decided_first:
                al.decided(symbol, epoch)
        got += al.poll(epoch + 65)
    assert got == list(feed)
    assert al.released_without_counts() == feed.missing_counts()
    assert al.refused_counts() == dict.fromkeys(symbols, 0)
