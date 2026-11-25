"""The seams of `MainStoinker/ib/watch_bars.py` for an engine (#196): a bar sink, `decided` and
`stopped` notifications, a hook per pass and wider limits.

Everything here drives the real watcher through the `Rig` of `tests/test_ib_watch_bars.py`
(imported, not copied: a scripted `FakeIbClient` behind a real `IbConnection`; no socket). The main
property: `decided(symbol, M)` is never followed by a row (or a `write` call) for that symbol at a
minute at or before `M`, over many seeded random sessions with outages, farm breaks, 1101 re-requests
whose backfills arrive late, snapshot-only minutes, stalled streams, late revisions, delayed streams,
a start before the open and the end of the session. The same sessions run end to end against the
real `LiveBarAligner`, polled from the pass hook.

No name from the new API is used when this module is imported (only inside functions), so the
byte-identity scenarios at the bottom can also be run against a checkout without the seams.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from MainStoinker.engine.feed import Bar
from MainStoinker.engine.live_feed import LiveBarAligner
from MainStoinker.ib import watch_bars
from MainStoinker.ib.connection import ConnState
from tests.fake_ib import make_bar
from tests.test_ib_watch_bars import (  # noqa: F401  (`real_home_untouched` is an autouse fixture)
    AAPL_BACKFILL,
    DATA_CALLS,
    FARM_CASES,
    Rig,
    delayed_rig,
    deliver_backfill,
    ep,
    farm_rig,
    pending_rerequest,
    quiet_minute_rig,
    real_home_untouched,
    stall_rig,
)

NO_BAR = -1  # a minute for the events that carry none


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    """The fixture of `tests/test_ib_watch_bars.py`, written out again (an imported fixture would be
    redefined by every test that names it)."""
    r = Rig(tmp_path)
    yield r
    for conn in r.connections:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


REAL_RUN_WATCH = watch_bars.run_watch  # the originals, so that a test may patch twice in one run
REAL_FILES_WRITE = watch_bars.LiveFiles.write


# -- a record of everything the watcher does, in one sequence --------------------------------------


@dataclass
class Event:
    kind: str  # "file" (a LiveFiles.write call), "sink" (a bar at a tee sink), "decided", "stopped", "pass"
    symbol: str
    minute: int  # the minute start (decided: the point), NO_BAR for stopped and pass
    at: float  # server time when it happened (the rig's scripted clock)
    how: str = ""


class Harness:
    """Records the watcher's calls and, with `align`, feeds them to a real `LiveBarAligner`.

    It is the sink (`write`, `close`, `written`), the listener (`decided`, `stopped`) and the pass
    hook (`on_pass`) at once, so one list holds the true sequence of calls."""

    def __init__(self, rig: Rig, symbols: Sequence[str], *, align: bool = True) -> None:
        self.rig = rig
        self.symbols = list(symbols)
        self.events: list[Event] = []
        self.written: dict[str, list[Any]] = {}
        self.closed = 0
        self.passes = 0
        self.watcher: Any = None
        self.aligner = LiveBarAligner(symbols) if align else None
        self.batches: list[Any] = []
        # symbol, minute, the aligner's answer, the newest clock polled so far, how the bar was closed
        self.results: list[tuple[str, int, str, float, str]] = []
        self.polled_max = -math.inf
        self.clocks: list[Any] = []  # every StreamClock the hook got
        self.states: list[dict[str, Any]] = []  # Watcher.stream_states() at each of those passes
        self.release: dict[int, float] = {}  # minute -> server time of the poll that released it
        self.poll_with_server_time = False  # the mistake the stream clock exists to avoid
        self.rows: list[tuple[str, Bar, str]] = []

    def now(self) -> float:
        return self.rig.server_epoch + self.rig.t

    def note(self, kind: str, symbol: str, minute: int, how: str = "") -> None:
        self.events.append(Event(kind, symbol, minute, self.now(), how))

    # the sink
    def write(self, symbol: str, bar: Bar, how: str) -> bool:
        self.note("sink", symbol, bar.time, how)
        self.rows.append((symbol, bar, how))
        if self.aligner is not None:
            answer = self.aligner.bar(symbol, bar)
            self.results.append((symbol, bar.time, answer, self.polled_max, how))
        return True

    def close(self) -> None:
        self.closed += 1

    # the listener
    def decided(self, symbol: str, through_epoch: int) -> None:
        self.note("decided", symbol, through_epoch)
        if self.aligner is not None:
            self.aligner.decided(symbol, through_epoch)

    def stopped(self, symbol: str) -> None:
        self.note("stopped", symbol, NO_BAR)
        if self.aligner is not None:
            self.aligner.stopped(symbol)

    # the pass hook
    def on_pass(self, clock: Any) -> None:
        self.passes += 1
        self.clocks.append(clock)
        self.states.append(self.watcher.stream_states() if self.watcher is not None else {})
        self.note("pass", "", NO_BAR)
        polled = clock.server_now if self.poll_with_server_time else clock.stream_now
        if (self.aligner is not None and polled is not None and self.watcher is not None
                and self.watcher.first_backfills_over()):
            self.polled_max = max(self.polled_max, polled)
            self.take(self.aligner.poll(polled), clock.server_now)

    def take(self, batches: list[Any], at: float) -> None:
        for batch in batches:
            self.release[int(batch[1][next(iter(batch[1]))].time)] = at
        self.batches.extend(batches)

    def seams(self, **more: Any) -> Any:
        return watch_bars.WatchSeams(extra_sinks=(self,), listener=self, on_pass=self.on_pass,
                                     on_watcher=self.set_watcher, **more)

    def set_watcher(self, watcher: Any) -> None:
        self.watcher = watcher

    def finish(self) -> None:
        """The last poll, after the run returned (every symbol was told `stopped`)."""
        if self.aligner is not None:
            self.take(self.aligner.poll(self.now()), self.now())


def run_with(rig: Rig, monkeypatch: pytest.MonkeyPatch, seams: Any, *, symbols: str | None = None,
             minutes: int = 5, log: Harness | None = None, **kw: Any) -> int:
    """`rig.watch`, with `run_watch` given `seams`; with `log`, every `LiveFiles.write` call is noted."""
    def with_seams(args: Any, **given: Any) -> int:
        return REAL_RUN_WATCH(args, **given, seams=seams)

    monkeypatch.setattr(watch_bars, "run_watch", with_seams)
    if log is not None:

        def tapped(self: Any, symbol: str, bar: Any) -> bool:  # the two-argument form the watcher uses
            log.note("file", symbol, bar.time)
            return REAL_FILES_WRITE(self, symbol, bar)

        monkeypatch.setattr(watch_bars.LiveFiles, "write", tapped)
    return rig.watch(symbols=symbols, minutes=minutes, **kw)


def file_minutes(rig: Rig, symbol: str) -> list[int]:
    path = rig.csv(symbol)
    if not path.exists():
        return []
    return [int(r[1]) for r in rig.rows(symbol)]


def minute_label(epoch: int) -> str:
    return watch_bars._hm(epoch)  # noqa: SLF001


# -- random sessions ----------------------------------------------------------------------------


def _m(base: int, k: int) -> int:
    return base + 60 * k


Ohlcv = tuple[float, float, float, float, int]


def _partial(final: Ohlcv, frac: float) -> Ohlcv:
    """What the bar looked like `frac` of the way through its minute (valid: low <= open, close <= high)."""
    o, h, lo, c, v = final
    frac = min(1.0, max(0.05, frac))
    pc = round(o + (c - o) * frac, 2)
    ph = round(max(o, pc) + (h - max(o, c)) * frac, 2)
    pl = round(min(o, pc) - (min(o, c) - lo) * frac, 2)
    return (o, ph, pl, pc, max(1, int(v * frac)))


@dataclass
class Plan:
    seed: int
    variant: str
    symbols: tuple[str, ...]
    minutes: int
    notes: list[str] = field(default_factory=list)


@dataclass
class _Upd:
    symbol: str
    k: int
    at: float
    ohlcv: tuple[float, float, float, float, int]


FARM_TEXT = {
    2103: "Market data farm connection is broken:usfarm",
    2105: "HMDS data farm connection is broken:ushmds",
    2104: "Market data farm connection is OK:usfarm",
    2106: "HMDS data farm connection is OK:ushmds",
}


def _at(rig: Rig, when: float, fn: Callable[[], None], ready: Callable[[], bool] | None = None) -> None:
    rig.at(when - rig.server_epoch, fn, ready)


def _update_at(rig: Rig, symbol: str, bar_epoch: int, when: float, ohlcv: Ohlcv) -> None:
    """An update of the bar starting at `bar_epoch`, delivered at server time `when` (as `upd_at`)."""
    o, h, lo, c, v = ohlcv

    def push() -> None:
        rig.expected += 1
        rig.fakes[-1].push("historicalDataUpdate", rig.req_ids[symbol], make_bar(bar_epoch, o, h, lo, c, v))
        rig.await_delivery(symbol)

    _at(rig, when, push)


def _hold_requests(rig: Rig, held: set[tuple[str, int]]) -> None:
    """The (symbol, ordinal) requests (1 based, per symbol) answer nothing: the test delivers them."""
    original = rig._history  # noqa: SLF001

    def hist(req_id: int, contract: Any, *rest: Any) -> list[tuple[Any, ...]]:
        symbol = contract.symbol
        ordinal = len([r for r in rig.requests if r[0] == symbol]) + 1
        if (symbol, ordinal) in held:
            rig.req_ids[symbol] = req_id
            rig.requests.append((symbol, req_id, rest[1], rest[6]))
            return []
        return original(req_id, contract, *rest)

    rig._history = hist  # type: ignore[method-assign]  # noqa: SLF001


def build_session(rig: Rig, seed: int) -> Plan:
    """Script a random session on `rig` (the same seed always scripts the same one)."""
    rng = random.Random(seed)
    variant = rng.choices(["normal", "pre_open", "delayed", "session_end"], [8, 1, 2, 2])[0]
    symbols = ("AAPL", "MSFT") if rng.random() < 0.8 else ("AAPL",)
    notes = [variant]
    lag = 0
    if variant == "pre_open":
        base, live_from, last_k = ep(9, 30), 0, rng.randint(5, 7)
        start = ep(9, 28) + rng.choice([0, 30])
    elif variant == "session_end":
        base, live_from, last_k = ep(15, 55), 3, 4
        start = _m(base, 3) + rng.choice([2, 30])
    elif variant == "delayed":
        base, live_from, last_k = ep(9, 30), 3, 3 + rng.randint(4, 6)
        lag = rng.choice([200, 360, 900])
        start = _m(base, 3) + lag + rng.choice([5, 25])
        notes.append(f"lag {lag}")
    else:
        base, live_from, last_k = ep(9, 30), 3, 3 + rng.randint(5, 8)
        start = _m(base, 3) + rng.choice([1, 20, 50])
    rig.server_epoch = start

    # the bars as IB finally has them; a live minute may be missing (nothing traded)
    finals: dict[str, dict[int, tuple[float, float, float, float, int]]] = {}
    for symbol in symbols:
        px = 100.0 if symbol == "AAPL" else 400.0
        own: dict[int, tuple[float, float, float, float, int]] = {}
        for k in range(last_k + 1):
            if k >= live_from and rng.random() < (0.05 if variant == "session_end" else 0.14):
                continue
            o = px
            c = round(o + rng.uniform(-0.5, 0.5), 2)
            h = round(max(o, c) + rng.uniform(0, 0.4), 2)
            lo = round(min(o, c) - rng.uniform(0, 0.4), 2)
            own[k] = (o, h, lo, c, rng.randint(100, 5000))
            px = c
        finals[symbol] = own

    def snapshot(symbol: str, t_req: float, perturb: float) -> list[tuple[Any, ...]]:
        """IB's answer to a request sent at `t_req`: the last 600 s of the stream's data clock."""
        data_now = t_req - lag
        out: list[tuple[Any, ...]] = []
        for k, final in sorted(finals[symbol].items()):
            first = _m(base, k)
            if first > data_now or first < data_now - 600:
                continue
            if data_now < first + 60:
                out.append((first, *_partial(final, (data_now - first) / 60)))
            else:
                v = final[4] + (rng.randint(1, 30) if rng.random() < perturb else 0)
                out.append((first, *final[:4], v))
        return out

    first_back = {s: snapshot(s, start, 0.0) for s in symbols}
    if variant == "pre_open" and rng.random() < 0.5:
        yesterday = [(ep(9, 30) - 86400 - 60 * j, 99.0, 99.5, 98.8, 99.2, 100) for j in (3, 2, 1)]
        first_back = {s: list(yesterday) for s in symbols}

    # the live updates
    updates: list[_Upd] = []
    for symbol in symbols:
        for k in range(live_from, last_k + 1):
            final = finals[symbol].get(k)
            if final is None:
                continue
            snapshot_only = any(b[0] == _m(base, k) for b in first_back[symbol]) and rng.random() < 0.4
            if snapshot_only:
                notes.append(f"{symbol} {k} snapshot only")
                continue
            n = rng.randint(1, 4)
            first = rng.uniform(0.5, 8.0) if rng.random() < 0.7 else rng.uniform(8.0, 50.0)
            offsets = sorted([first] + [rng.uniform(first, 59.5) for _ in range(n - 1)])
            if rng.random() < 0.2:
                offsets = [min(o, 40.0) for o in offsets]  # the last update is early: a stalled bar
                notes.append(f"{symbol} {k} stalls")
            for i, off in enumerate(offsets):
                shown = final if i == n - 1 else _partial(final, (i + 1) / n)
                updates.append(_Upd(symbol, k, _m(base, k) + round(off, 1) + lag, shown))
            if k > live_from and rng.random() < 0.12:  # IB revises an earlier minute
                j = k - 1 if rng.random() < 0.7 or k - 2 < live_from else k - 2
                rev = finals[symbol].get(j)
                if rev is not None:
                    notes.append(f"{symbol} {k} revises {j}")
                    updates.append(_Upd(symbol, j, _m(base, k) + round(rng.uniform(3, 40), 1) + lag,
                                        (*rev[:4], rev[4] + rng.randint(1, 50))))
    updates = [u for u in updates if u.at > start + 1.5]

    # outages, farm breaks and 1101 data loss with their backfills
    lo_t, hi_t = _m(base, live_from) + lag + 15, _m(base, last_k) + lag + 40
    disruptive: list[tuple[float, float]] = []
    held: set[tuple[str, int]] = set()
    scripts: dict[str, list[list[tuple[Any, ...]]]] = {s: [first_back[s]] for s in symbols}
    deliveries: list[tuple[str, int, float, list[tuple[Any, ...]]]] = []
    blackouts: list[tuple[float, float, set[str]]] = []
    reinsert: list[tuple[float, float]] = []  # windows whose updates arrive in a burst after the restore
    losses = 0
    horizon = _m(base, last_k) + lag + 60 + 90 + 45
    if variant == "session_end":
        horizon = max(horizon, ep(16, 22))
    events = sorted(
        (rng.uniform(lo_t, hi_t), rng.choice(["loss", "loss_lost", "lost", "farm", "farm"]))
        for _ in range(rng.choices([0, 1, 2, 3], [3, 4, 3, 1])[0]))

    def lost_data(at: float) -> None:
        """A 1101 at `at`: both symbols ask again; a held answer arrives later; blackout around it."""
        _at(rig, at, lambda: rig.fakes[-1].drop(1101))
        names = set(symbols)
        for symbol in symbols:
            ordinal = len(scripts[symbol]) + 1
            scripts[symbol].append(snapshot(symbol, at + 1.0 + (1.0 if symbol != symbols[0] else 0.0), 0.25))
            wait = 8.0
            if rng.random() < 0.5:
                delay = rng.choice([5.0, 20.0, 60.0])
                held.add((symbol, ordinal))
                deliveries.append((symbol, ordinal, at + delay, scripts[symbol][-1]))
                wait = delay + 3.0
                notes.append(f"{symbol} backfill {ordinal} held {delay:g} s")
            blackouts.append((at - 1.0, at + wait, {symbol}))
        blackouts.append((at - 1.0, at + 8.0, names))
        asked = {s: len(scripts[s]) - 1 for s in symbols}  # re-requests of each symbol up to this one
        _at(rig, at + 0.01, lambda: None,
            ready=lambda: all(rig.text.count(f"requesting {s} again") >= asked[s] for s in symbols))

    for when, kind in events:
        if kind == "farm":
            code, ok = rng.choice([(2103, 2104), (2105, 2106)])
            _at(rig, when, lambda code=code: rig.fakes[-1].drop(code, FARM_TEXT[code]))
            if rng.random() < 0.7:
                _at(rig, when + rng.uniform(20, 240), lambda ok=ok: rig.fakes[-1].drop(ok, FARM_TEXT[ok]))
            notes.append(f"farm {code} at {when - start:.0f}")
            continue
        end = when + (rng.uniform(20, 200) if kind != "lost" else 12.0)
        if any(when < b + 75 and a - 75 < end for a, b in disruptive):
            continue
        disruptive.append((when, end))
        notes.append(f"{kind} at {when - start:.0f} to {end - start:.0f}")
        if kind == "lost":
            lost_data(when)
            continue
        losses += 1
        n_lost = losses
        _at(rig, when, lambda: rig.fakes[-1].drop(1100))
        _at(rig, when + 0.01, lambda: None, ready=lambda n=n_lost: rig.text.count("connectivity LOST") >= n)
        if kind == "loss":
            _at(rig, end, lambda: rig.fakes[-1].drop(1102),
                ready=lambda n=n_lost: rig.text.count("connectivity LOST") >= n)
            blackouts.append((when - 0.5, end + 0.5, set(symbols)))
            if rng.random() < 0.5:
                reinsert.append((when - 0.5, end + 0.5))
        else:
            lost_data(end)
            blackouts.append((when - 0.5, end, set(symbols)))
        _at(rig, end + 0.02, lambda: None,
            ready=lambda n=n_lost: rig.text.count("connectivity restored") >= n)

    # blackouts remove updates (the data that never came); a window may be replayed after a restore
    kept: list[_Upd] = []
    for u in updates:
        gone = any(a <= u.at <= b and u.symbol in names for a, b, names in blackouts)
        if not gone:
            kept.append(u)
            continue
        for a, b in reinsert:
            if a <= u.at <= b:
                kept.append(_Upd(u.symbol, u.k, b + 2.0 + 0.3 * len(kept) % 3, u.ohlcv))
                break
    for u in kept:
        _update_at(rig, u.symbol, _m(base, u.k), u.at, u.ohlcv)
    for symbol, ordinal, at, bars in deliveries:
        deliver_backfill(rig, symbol, bars, at - rig.server_epoch, after_request=ordinal)
        horizon = max(horizon, at + 100)
    _hold_requests(rig, held)
    rig.backfills = {s: scripts[s] for s in symbols}

    for _, end in disruptive:
        horizon = max(horizon, end + 200)
    pass_at = start + 3.0
    while pass_at < horizon:
        _at(rig, pass_at, lambda: None)
        pass_at += rng.uniform(10, 25)
    minutes = min(420, math.ceil((horizon - start) / 60))
    rig.end_at(minutes, symbols=len(symbols))
    return Plan(seed, variant, symbols, minutes, notes)


def run_session(rig: Rig, monkeypatch: pytest.MonkeyPatch, seed: int) -> tuple[Plan, Harness, int]:
    plan = build_session(rig, seed)
    harness = Harness(rig, plan.symbols)
    code = run_with(rig, monkeypatch, harness.seams(), symbols=",".join(plan.symbols),
                    minutes=plan.minutes, log=harness)
    harness.finish()
    return plan, harness, code


# Twelve sessions in the fast suite, picked from a sweep of 80 to cover between them: a delayed stream
# with late bars (0), 1101 with a held backfill (1, 10), a snapshot-only minute at the session end (2),
# a start before the open (5), 1100 then 1101 (12, 19, 23, 27, 41, 47), a plain loss (15) and farm breaks.
# The other 48 seeds are the same test in the slow suite (about 2.5 s a session).
FAST_SEEDS = (0, 1, 2, 5, 10, 12, 15, 19, 23, 27, 41, 47)
SEEDS = [*FAST_SEEDS, *(pytest.param(s, marks=pytest.mark.slow) for s in range(60) if s not in FAST_SEEDS)]
_RESULTS: dict[int, dict[str, Any]] = {}


def session_facts(rig: Rig, monkeypatch: pytest.MonkeyPatch, seed: int) -> dict[str, Any]:
    """Run one random session once per process and keep what the tests need (not the rig)."""
    if seed not in _RESULTS:
        plan, harness, code = run_session(rig, monkeypatch, seed)
        assert harness.aligner is not None
        aligner = harness.aligner
        _RESULTS[seed] = {
            "plan": plan, "events": harness.events, "batches": harness.batches, "results": harness.results,
            "code": code, "rows": {s: file_minutes(rig, s) for s in plan.symbols},
            "refused": aligner.refused_counts(), "late_after": aligner.late_after_decided_counts(),
            "late": aligner.late_counts(), "fed": aligner.fed_counts(),
            "text": rig.text, "closed": harness.closed,
        }
    return _RESULTS[seed]


def describe(facts: dict[str, Any]) -> str:
    plan: Plan = facts["plan"]
    return f"seed {plan.seed} ({'; '.join(plan.notes)})\n{facts['text'][-3000:]}"


@pytest.mark.parametrize("seed", SEEDS)
def test_decided_never_precedes_a_possible_write(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    facts = session_facts(rig, monkeypatch, seed)
    plan: Plan = facts["plan"]
    events: list[Event] = facts["events"]
    decided_through: dict[str, int] = {}
    stopped: dict[str, int] = {}
    for i, e in enumerate(events):
        if e.kind in ("file", "sink"):
            assert e.minute > decided_through.get(e.symbol, -math.inf), (
                f"{e.kind} {e.symbol} {minute_label(e.minute)} after decided through "
                f"{minute_label(decided_through[e.symbol])}\n{describe(facts)}")
            assert e.symbol not in stopped, f"a {e.kind} after stopped\n{describe(facts)}"
        elif e.kind == "decided":
            assert e.minute % 60 == 0, "a decided point is a minute start"
            assert e.minute > decided_through.get(e.symbol, -math.inf), "decided only moves forward"
            assert e.symbol not in stopped, f"decided after stopped\n{describe(facts)}"
            decided_through[e.symbol] = e.minute
        elif e.kind == "stopped":
            assert e.symbol not in stopped, "stopped twice"
            stopped[e.symbol] = i
    assert set(stopped) == set(plan.symbols), "every symbol is told stopped, once, when the watch ends"
    # every file call reached the sink, and the files hold exactly those rows
    for symbol in plan.symbols:
        files = [e.minute for e in events if e.kind == "file" and e.symbol == symbol]
        sinks = [e.minute for e in events if e.kind == "sink" and e.symbol == symbol]
        assert files == sinks == facts["rows"][symbol], describe(facts)
        assert files == sorted(set(files)), "a symbol's rows are strictly increasing"
    assert facts["closed"] == 1


@pytest.mark.parametrize("seed", SEEDS)
def test_end_to_end_with_the_real_aligner(rig: Rig, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    facts = session_facts(rig, monkeypatch, seed)
    plan: Plan = facts["plan"]
    assert set(facts["refused"].values()) == {0}, describe(facts)
    assert set(facts["late_after"].values()) == {0}, describe(facts)
    stamps = [b[0] for b in facts["batches"]]
    assert stamps == sorted(set(stamps)), "batches are strictly increasing"
    assert all(b[1] and list(b[1]) == [s for s in plan.symbols if s in b[1]] for b in facts["batches"])
    for symbol in plan.symbols:
        fed = {int(batch[symbol].time) for _, batch in facts["batches"] if symbol in batch}
        late = {m for s, m, answer, _, _ in facts["results"] if s == symbol and answer == "late"}
        accepted = {m for s, m, answer, _, _ in facts["results"] if s == symbol and answer == "accepted"}
        assert not fed & late
        assert fed | late == set(facts["rows"][symbol]), describe(facts)
        assert accepted == fed, "every accepted bar was released by the end (stopped releases the rest)"
        assert facts["fed"][symbol] == len(fed) and facts["late"][symbol] == len(late)
        outage = any(n.split()[0] in ("loss", "loss_lost", "lost", "farm") for n in plan.notes[1:])
        for s, m, answer, polled, how in facts["results"]:
            if s == symbol and answer == "late":
                # polled with the stream clock, a bar the watcher writes in time is never late. A late
                # bar needs an outage or a 1101 in the session (nothing closes while it lasts, and a
                # re-requested backfill then writes minutes the aligner released by time), and its
                # minute must have been released by time before it arrived
                assert outage, f"{symbol} {minute_label(m)} ({how}) late in a session without an outage"
                assert polled >= m + 60 + 100, f"{symbol} {minute_label(m)} late without a poll to release it"


# -- the sink: a tee feeds exactly the file rows ---------------------------------------------------


def test_tee_receives_exactly_the_file_rows(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.standard()
    log = Harness(rig, ("AAPL", "MSFT"), align=False)
    code = run_with(rig, monkeypatch, watch_bars.WatchSeams(extra_sinks=(log,)), minutes=5, log=log)
    assert code == watch_bars.EXIT_OK, rig.text
    for symbol in ("AAPL", "MSFT"):
        got = [(bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume)
               for s, bar, _ in log.rows if s == symbol]
        on_disk = [(int(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[6]))
                   for r in rig.rows(symbol)]
        assert got == on_disk and len(got) == 6
    hows = {how for _, _, how in log.rows}
    assert hows == {"backfill", "later"}
    assert [e.minute for e in log.events if e.kind == "file" and e.symbol == "AAPL"] == [
        e.minute for e in log.events if e.kind == "sink" and e.symbol == "AAPL"]
    assert log.closed == 1


def test_a_second_run_hands_the_sink_the_rows_the_file_already_has(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first sink (the files) says False for a minute it holds; later sinks still get the bar."""
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.end_at(1, symbols=1)
    first = Harness(rig, ("AAPL",), align=False)
    seams = watch_bars.WatchSeams(extra_sinks=(first,))
    assert run_with(rig, monkeypatch, seams, symbols="AAPL", minutes=1) == 0
    assert len(rig.rows("AAPL")) == 2 and len(first.rows) == 2
    rig2 = Rig(rig.tmp, ("AAPL",))
    rig2.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig2.end_at(1, symbols=1)
    second = Harness(rig2, ("AAPL",), align=False)
    try:
        assert run_with(rig2, monkeypatch, watch_bars.WatchSeams(extra_sinks=(second,)), symbols="AAPL",
                        minutes=1) == 0
    finally:
        for conn in rig2.connections:
            conn.close()
        for fake in rig2.fakes:
            fake.close()
    assert len(rig2.rows("AAPL")) == 2  # not appended twice
    assert [bar.time for _, bar, _ in second.rows] == [bar.time for _, bar, _ in first.rows]


class _Sink:
    def __init__(self, name: str, log: list[str], answer: bool = True, fail: Exception | None = None) -> None:
        self.name, self.log, self.answer, self.fail = name, log, answer, fail
        self.written: dict[str, list[Any]] = {}

    def write(self, symbol: str, bar: Any, how: str) -> bool:
        self.log.append(f"{self.name}:{symbol}:{how}")
        if self.fail is not None:
            raise self.fail
        return self.answer

    def close(self) -> None:
        self.log.append(f"{self.name}:close")
        if self.fail is not None:
            raise self.fail


def _bar() -> Bar:
    return Bar(pd.Timestamp("2024-08-16 09:33:00"), ep(9, 33), 1.0, 2.0, 0.5, 1.5, 10)


def test_the_tee_feeds_every_sink_in_sequence_and_answers_as_the_first() -> None:
    log: list[str] = []
    tee = watch_bars.TeeBars(_Sink("a", log, answer=False), _Sink("b", log), _Sink("c", log))
    assert tee.write("AAPL", _bar(), "later") is False  # the first sink's answer; the others still get it
    assert log == ["a:AAPL:later", "b:AAPL:later", "c:AAPL:later"]
    tee.close()
    assert log[3:] == ["a:close", "b:close", "c:close"]


def test_the_tee_does_not_pass_a_bar_on_when_the_first_sink_raises_and_closes_every_sink() -> None:
    log: list[str] = []
    boom = watch_bars.LiveFileError("cannot open", locked=True)
    tee = watch_bars.TeeBars(_Sink("a", log, fail=boom), _Sink("b", log))
    with pytest.raises(watch_bars.LiveFileError):
        tee.write("AAPL", _bar(), "time")
    assert log == ["a:AAPL:time"]
    with pytest.raises(watch_bars.LiveFileError):
        tee.close()
    assert log[1:] == ["a:close", "b:close"]  # the failure did not skip the second sink


def test_the_tee_lists_the_files_of_all_sinks_once() -> None:
    first, second = _Sink("a", []), _Sink("b", [])
    p, q = Path("x.csv"), Path("y.csv")
    first.written = {"AAPL": [p]}
    second.written = {"AAPL": [p, q], "MSFT": [q]}
    assert watch_bars.TeeBars(first, second).written == {"AAPL": [p, q], "MSFT": [q]}
    assert first.written == {"AAPL": [p]}  # nothing of a sink was changed


def test_with_no_extra_sink_the_files_alone_are_the_sink(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = watch_bars.Watcher.__init__

    def spy(self: Any, args: Any, conn: Any, md: Any, say: Any, files: Any, *rest: Any, **kw: Any) -> None:
        seen.append(files)
        real(self, args, conn, md, say, files, *rest, **kw)

    monkeypatch.setattr(watch_bars.Watcher, "__init__", spy)
    rig.standard()
    assert rig.watch() == 0
    assert type(seen[0]) is watch_bars.LiveFiles


# -- the pass hook and the limits ------------------------------------------------------------------


def test_on_pass_called_each_loop(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    passes: list[float] = []
    started: list[Any] = []
    counted = {"states": 0}
    real_check = watch_bars.Watcher._check_state  # noqa: SLF001

    def counting(self: Any) -> None:
        counted["states"] += 1
        real_check(self)

    monkeypatch.setattr(watch_bars.Watcher, "_check_state", counting)

    def hook(clock: Any) -> None:
        w = started[0]
        server_now = clock.server_now
        assert server_now == w.server_now() and isinstance(server_now, float)
        assert server_now == rig.server_epoch + (rig.t - w.clock0)
        assert clock == w.stream_clock() and isinstance(clock, watch_bars.StreamClock)
        passes.append(server_now)

    rig.standard()
    seams = watch_bars.WatchSeams(on_pass=hook, on_watcher=started.append)
    assert run_with(rig, monkeypatch, seams, minutes=5) == watch_bars.EXIT_OK, rig.text
    assert len(passes) == counted["states"] > 20  # one call per pass of the reading loop
    assert passes == sorted(passes)
    assert passes[0] >= rig.server_epoch and passes[-1] < rig.server_epoch + 5 * 60 + 10
    assert "Watch time over (5 minutes)" in rig.text


def test_first_backfills_over_is_false_until_every_symbol_has_its_backfill(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    states: list[tuple[bool, str]] = []
    seen: list[Any] = []
    rig.standard()

    def hook(now: float) -> None:
        states.append((seen[0].first_backfills_over(), rig.text))

    seams = watch_bars.WatchSeams(on_watcher=seen.append, on_pass=hook)
    assert run_with(rig, monkeypatch, seams, minutes=5) == 0
    assert states[-1][0] is True
    for over, text in states:
        assert over == (text.count(": backfill:") >= 2)  # AAPL and MSFT


def test_the_run_deadline_is_a_parameter_and_the_command_line_keeps_its_limits(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.at(3 * 60 + 5.0, lambda: None)
    rig.end_at(4, symbols=1)
    assert run_with(rig, monkeypatch, watch_bars.WatchSeams(minutes=4), symbols="AAPL", minutes=1) == 0
    assert "Watch time over (4 minutes)." in rig.text  # not the one minute of the command line
    with pytest.raises(SystemExit):
        watch_bars.parse_args(["--port", "7497", "--minutes", "421"])
    with pytest.raises(SystemExit):
        watch_bars.parse_args(["--port", "7497", "--backfill-minutes", "121"])
    assert watch_bars.MAX_MINUTES == 420 and watch_bars.MAX_BACKFILL_MINUTES == 120


@pytest.mark.parametrize("kw", [{"minutes": 0}, {"minutes": 601}, {"backfill_minutes": 0},
                                {"backfill_minutes": 391},
                                {"backfill_since_open": True, "backfill_minutes": 10}])
def test_driver_limits_are_checked_before_anything_is_built(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        watch_bars.WatchSeams(**kw)
    assert watch_bars.WatchSeams(minutes=watch_bars.MAX_DRIVER_MINUTES, backfill_minutes=390).minutes == 600


def test_backfill_since_open_duration_string() -> None:
    at = lambda hh, mm, ss=0: float(ep(hh, mm, ss))  # noqa: E731
    assert watch_bars.since_open_duration(at(9, 33, 25)) == "300 S"  # 205 s since the open: 4 minutes + 1
    assert watch_bars.since_open_duration(at(9, 30, 0)) == "60 S"
    assert watch_bars.since_open_duration(at(9, 30, 5)) == "120 S"
    assert watch_bars.since_open_duration(at(9, 31, 0)) == "120 S"
    assert watch_bars.since_open_duration(at(9, 29, 59)) == "60 S"  # before the open: the shortest
    assert watch_bars.since_open_duration(at(9, 0, 0)) == "60 S"
    assert watch_bars.since_open_duration(at(10, 30, 30)) == f"{(61 + 1) * 60} S"  # 3630 s: 61 minutes + 1
    assert watch_bars.since_open_duration(at(15, 59, 59)) == "23460 S"  # 390 minutes + 1
    # after the close the request still reaches back to 09:30 of that session: 390 + the time since 16:00
    assert watch_bars.since_open_duration(at(16, 0)) == "23460 S"
    assert watch_bars.since_open_duration(at(16, 30)) == f"{(421) * 60} S"
    assert watch_bars.since_open_duration(at(20, 0)) == f"{(631) * 60} S"
    for hh, mm in ((16, 0), (16, 30), (20, 0)):
        asked = int(watch_bars.since_open_duration(at(hh, mm)).split()[0])
        assert at(hh, mm) - asked <= ep(9, 30)  # reaches the open
    assert watch_bars.since_open_minutes(at(12, 0)) == 151
    assert watch_bars.MAX_SINCE_OPEN_MINUTES == 390 == (16 - 9) * 60 - 30


def _durations(rig: Rig) -> list[tuple[str, str]]:
    """(symbol, duration) of every history request the fake got."""
    symbol_of = {r[1]: r[0] for r in rig.requests}
    return [(symbol_of[c.args[0]], c.args[3]) for f in rig.fakes for c in f.calls_named("reqHistoricalData")]


def test_the_request_asks_from_the_open_when_the_driver_says_so(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server_epoch = ep(9, 33, 25)
    rig.backfills = {"AAPL": [AAPL_BACKFILL, AAPL_BACKFILL]}
    rig.drop((36, 10.0), 1101)  # the re-request asks again, from the open again
    rig.at(rig.tt(36, 40.0), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.end_at(4, symbols=1)
    seams = watch_bars.WatchSeams(backfill_since_open=True)
    assert run_with(rig, monkeypatch, seams, symbols="AAPL", minutes=4) == 0, rig.text
    calls = [d for _, d in _durations(rig)]
    # the first request at 09:33:25; the second at about 09:36:10, over 6 minutes after the open: 7 + 1
    assert calls == ["300 S", "480 S"]
    assert "AAPL: requested live bars with the backfill since the open (300 S)" in rig.text


def test_a_driver_can_ask_for_the_backfill_length_and_the_command_line_default_is_unchanged(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.end_at(1, symbols=1)
    seams = watch_bars.WatchSeams(backfill_minutes=390)
    assert run_with(rig, monkeypatch, seams, symbols="AAPL", minutes=1) == 0
    assert _durations(rig) == [("AAPL", "23400 S")]
    assert "requested live bars with 390 minutes of backfill" in rig.text


def test_the_command_line_default_backfill_duration_is_unchanged(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.end_at(1, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=1) == 0
    assert _durations(rig) == [("AAPL", "600 S")]
    assert "requested live bars with 10 minutes of backfill" in rig.text


def test_the_watcher_has_a_public_name_and_sends_only_the_requests_it_always_did(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert watch_bars.Watcher is watch_bars._Watcher  # noqa: SLF001
    log = Harness(rig, ("AAPL", "MSFT"))
    rig.standard()
    assert run_with(rig, monkeypatch, log.seams(backfill_since_open=True), minutes=5) == 0
    names = {c.name for f in rig.fakes for c in f.calls}
    assert names <= DATA_CALLS and "reqHistoricalData" in names, names


def test_drop_counts_are_the_counts_of_the_summary(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    rig.server_epoch = ep(9, 33, 30)
    rig.backfills = {"AAPL": [AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)]]}
    rig.at(rig.tt(36, 1.0), lambda: None)
    rig.end_at(3, symbols=1)
    assert run_with(rig, monkeypatch, watch_bars.WatchSeams(on_watcher=seen.append), symbols="AAPL",
                    minutes=3) == 0
    counts = seen[0].drop_counts()["AAPL"]
    assert counts.snapshot_only == 1 and counts.connection_loss == 0 and counts.rerequests == 0
    assert "snapshot only 1" in rig.text


# -- `decided` and `stopped`, case by case ----------------------------------------------------------


def _events(log: Harness, kind: str, symbol: str) -> list[Event]:
    return [e for e in log.events if e.kind == kind and e.symbol == symbol]


def test_decided_follows_each_row_and_comes_after_the_bar(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.standard()
    log = Harness(rig, ("AAPL", "MSFT"))
    assert run_with(rig, monkeypatch, log.seams(), minutes=5, log=log) == 0
    aapl = [(e.kind, e.minute) for e in log.events if e.symbol == "AAPL" and e.kind in ("sink", "decided")]
    assert aapl and aapl[-1][0] == "decided"
    for i, (kind, minute) in enumerate(aapl):
        if kind == "sink":
            assert aapl[i + 1] == ("decided", minute)  # a bar, then the point at that bar's minute
    assert log.aligner.refused_counts() == {"AAPL": 0, "MSFT": 0}
    assert log.aligner.late_after_decided_counts() == {"AAPL": 0, "MSFT": 0}
    # the engine would get 09:33 as soon as the 09:34 update of the slower symbol closed it
    first_33 = next(b for b in log.batches if int(b[1]["AAPL"].time) == ep(9, 33))
    assert set(first_33[1]) == {"AAPL", "MSFT"}


def test_a_dropped_minute_is_decided_at_once_so_the_aligner_does_not_wait(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """09:33 is only in the snapshot: not written, and `decided` through it is told right after the line."""
    rig.server_epoch = ep(9, 33, 59)
    rig.armed_count = 0
    rig.backfills = {"AAPL": [AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)]]}
    rig.upd("AAPL", 34, (34, 5.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.upd("AAPL", 35, (35, 2.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.end_at(2, symbols=1)
    log = Harness(rig, ("AAPL",), align=False)
    assert run_with(rig, monkeypatch, log.seams(), symbols="AAPL", minutes=2, log=log) == 0, rig.text
    assert "AAPL 09:33 not written: seen only in the backfill snapshot" in rig.text
    points = [e.minute for e in _events(log, "decided", "AAPL")]
    assert ep(9, 33) in points
    assert not [e for e in _events(log, "sink", "AAPL") if e.minute == ep(9, 33)]
    assert points == sorted(set(points))


def test_a_minute_skipped_by_a_later_update_is_not_decided_because_a_rerequested_backfill_may_write_it(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """09:33 closes, then the stream shows 09:35 (09:34 never came). `decided` stays at 09:33. A 1101 and
    its backfill then write 09:34: a rule "an update for a later minute decides every minute before it"
    would have broken the promise."""
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL, AAPL_BACKFILL[1:] + [
        (ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800), (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 2200),
        (ep(9, 35), 100.8, 100.9, 100.8, 100.9, 3), (ep(9, 36), 100.9, 101.0, 100.9, 101.0, 2)]]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 35, (35, 5.0), (100.8, 100.8, 100.8, 100.8, 3))  # closes 09:33; 09:34 skipped
    rig.upd("AAPL", 35, (35, 20.0), (100.8, 100.9, 100.8, 100.9, 3))
    rig.drop((35, 40.0), 1101)
    rig.at(rig.tt(35, 50.0), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.end_at(4, symbols=1)
    log = Harness(rig, ("AAPL",))
    assert run_with(rig, monkeypatch, log.seams(), symbols="AAPL", minutes=4, log=log) == 0, rig.text
    rows = [e.minute for e in _events(log, "sink", "AAPL")]
    assert ep(9, 34) in rows, rig.text  # written, by the backfill
    at_write = next(i for i, e in enumerate(log.events) if e.kind == "sink" and e.minute == ep(9, 34))
    before = [e.minute for e in log.events[:at_write] if e.kind == "decided"]
    assert max(before) == ep(9, 33)  # the point had not passed the skipped minute
    assert log.aligner.late_after_decided_counts() == {"AAPL": 0}


def test_nothing_is_decided_while_a_rerequested_backfill_is_outstanding(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    pending_rerequest(rig, closes_first=False)  # 1101 at 09:34:30; the backfill arrives at 09:35:50
    log = Harness(rig, ("AAPL",))
    assert run_with(rig, monkeypatch, log.seams(), symbols="AAPL", minutes=4, log=log) == 0, rig.text
    drop_at, arrives = ep(9, 34, 30), ep(9, 35, 50)
    inside = [e for e in log.events if e.kind in ("decided", "sink") and drop_at <= e.at < arrives]
    assert inside == [], inside
    assert ep(9, 33) in [e.minute for e in _events(log, "sink", "AAPL")]  # written by the backfill, after
    assert log.aligner.late_after_decided_counts() == {"AAPL": 0}


def standard_with_msft_failing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The standard stream, but MSFT's file cannot be written when 09:33 closes (09:34:03) and MSFT
    sends nothing after that (a stopped symbol's unread updates would hold the scripted feeder)."""
    real = REAL_FILES_WRITE

    def failing(self: Any, symbol: str, bar: Any) -> bool:
        if symbol == "MSFT" and bar.time == ep(9, 33):
            raise watch_bars.LiveFileError("cannot open or append to X: PermissionError: denied", locked=True)
        return real(self, symbol, bar)

    monkeypatch.setattr(watch_bars.LiveFiles, "write", failing)
    original = rig.upd

    def upd(symbol: str, bar_mm: int, when: tuple[int, float], ohlcv: Any, *, hh: int = 9) -> None:
        if not (symbol == "MSFT" and when > (34, 5.0)):
            original(symbol, bar_mm, when, ohlcv, hh=hh)

    rig.upd = upd  # type: ignore[method-assign]
    rig.standard()


def test_stopped_is_sent_when_a_symbol_stops_and_for_the_rest_at_the_end(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_with_msft_failing(rig, monkeypatch)
    log = Harness(rig, ("AAPL", "MSFT"))
    assert run_with(rig, monkeypatch, log.seams(), minutes=5) == watch_bars.EXIT_FAILED, rig.text
    stops = [(e.symbol, e.at) for e in log.events if e.kind == "stopped"]
    assert [s for s, _ in stops] == ["MSFT", "AAPL"]  # MSFT when it stopped, AAPL when the watch ended
    msft_stop = next(i for i, e in enumerate(log.events) if e.kind == "stopped" and e.symbol == "MSFT")
    assert not [e for e in log.events[msft_stop:] if e.symbol == "MSFT" and e.kind != "stopped"]
    assert stops[0][1] < stops[1][1]
    aapl_stop = next(i for i, e in enumerate(log.events) if e.kind == "stopped" and e.symbol == "AAPL")
    assert not [e for e in log.events[aapl_stop:] if e.kind in ("sink", "decided")]
    assert log.aligner.refused_counts() == {"AAPL": 0, "MSFT": 0}


def test_a_symbol_that_cannot_be_resolved_is_told_stopped(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.unknown = {"MSFT"}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.end_at(2)
    log = Harness(rig, ("AAPL", "MSFT"))
    run_with(rig, monkeypatch, log.seams(), minutes=2)
    assert [e.symbol for e in log.events if e.kind == "stopped"][0] == "MSFT"
    assert log.passes > 0


def test_the_last_bars_of_the_session_are_decided_when_16_20_closes_them(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server_epoch = ep(15, 58, 30)
    rig.backfills = {"AAPL": [[(ep(15, 56), 100.0, 101.0, 99.0, 100.5, 10),
                               (ep(15, 57), 100.5, 101.0, 100.0, 100.8, 10)]]}
    rig.upd("AAPL", 58, (58, 40.0), (100.8, 101.0, 100.7, 100.9, 50), hh=15)
    rig.upd("AAPL", 59, (59, 1.0), (100.9, 100.9, 100.9, 100.9, 5), hh=15)
    rig.upd("AAPL", 59, (59, 59.0), (100.9, 101.5, 100.5, 101.4, 90000), hh=15)
    for t in range(5, 24):
        rig.at(rig.tt(t, 30.0, hh=16), lambda: None)
    rig.end_at(26, symbols=1)
    log = Harness(rig, ("AAPL",))
    assert run_with(rig, monkeypatch, log.seams(), symbols="AAPL", minutes=26, log=log) == 0, rig.text
    last = [e for e in _events(log, "sink", "AAPL") if e.minute == ep(15, 59)]
    assert len(last) == 1 and last[0].at >= ep(16, 20)
    assert max(e.minute for e in _events(log, "decided", "AAPL")) == ep(15, 59)
    assert [e.minute for e in _events(log, "decided", "AAPL") if e.at < ep(16, 20)][-1] == ep(15, 58)


# -- no listener, default sink: the same output as origin/revival -------------------------------------


def _outage(rig: Rig, mp: pytest.MonkeyPatch) -> int:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.at(rig.tt(35, 35.0), lambda: None, ready=lambda: "connectivity LOST" in rig.text)
    rig.drop((35, 40.0), 1102)
    rig.at(rig.tt(35, 42.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.upd("AAPL", 33, (35, 45.0), (100.0, 100.6, 99.9, 100.4, 1700))
    rig.upd("AAPL", 35, (35, 50.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.upd("AAPL", 36, (36, 1.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.end_at(4, symbols=1)
    return rig.watch(symbols="AAPL", minutes=4)


def _session_end(rig: Rig, mp: pytest.MonkeyPatch) -> int:
    rig.server_epoch = ep(15, 58, 30)
    rig.backfills = {"AAPL": [[(ep(15, 56), 100.0, 101.0, 99.0, 100.5, 10),
                               (ep(15, 57), 100.5, 101.0, 100.0, 100.8, 10)]]}
    rig.upd("AAPL", 58, (58, 40.0), (100.8, 101.0, 100.7, 100.9, 50), hh=15)
    rig.upd("AAPL", 59, (59, 1.0), (100.9, 100.9, 100.9, 100.9, 5), hh=15)
    rig.upd("AAPL", 59, (59, 59.0), (100.9, 101.5, 100.5, 101.4, 90000), hh=15)
    rig.at(rig.tt(0, 30.0, hh=16), lambda: None)
    rig.at(rig.tt(2, 0.0, hh=16), lambda: None)
    rig.end_at(24, symbols=1)
    return rig.watch(symbols="AAPL", minutes=24)


def _file_error(rig: Rig, mp: pytest.MonkeyPatch) -> int:
    standard_with_msft_failing(rig, mp)
    return rig.watch()


def _no_data(rig: Rig, mp: pytest.MonkeyPatch) -> int:
    rig.backfills = {"AAPL": [[]]}
    rig.end_at(4, symbols=1)
    return rig.watch(symbols="AAPL", minutes=4)


def _a_second_run(rig: Rig, mp: pytest.MonkeyPatch) -> int:
    path = rig.csv("AAPL")
    path.parent.mkdir(parents=True)
    path.write_text("date,time,open,high,low,close,volume\n"
                    f"2024-08-16 09:30:00,{ep(9, 30)},99.0,99.5,98.8,99.2,5000\n", encoding="utf-8")
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    return rig.watch(symbols="AAPL", minutes=2, record=True)


SCENARIOS: dict[str, Callable[[Rig, pytest.MonkeyPatch], int]] = {
    "standard": lambda rig, mp: (rig.standard(), rig.watch())[1],
    "standard_recorded": lambda rig, mp: (rig.standard(), rig.watch(record=True))[1],
    "delayed": lambda rig, mp: delayed_rig(rig, 5),
    "rerequest": lambda rig, mp: (pending_rerequest(rig, closes_first=True),
                                  rig.watch(symbols="AAPL", minutes=4))[1],
    "farm_break": lambda rig, mp: (farm_rig(rig, *FARM_CASES[0]), rig.watch(symbols="AAPL", minutes=5))[1],
    "stalled": lambda rig, mp: (stall_rig(rig, 45.0), rig.watch(symbols="AAPL", minutes=4))[1],
    "revised": lambda rig, mp: (quiet_minute_rig(rig, (100.0, 100.6, 99.9, 100.4, 1700)),
                                rig.watch(symbols="AAPL", minutes=4))[1],
    "outage": _outage,
    "session_end": _session_end,
    "file_error": _file_error,
    "no_data": _no_data,
    "second_run": _a_second_run,
}


def run_scenario(rig: Rig, monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    """The exit code and the output of one scenario with the tmp folder written as `<tmp>`."""
    code = SCENARIOS[name](rig, monkeypatch)
    return f"exit {code}\n" + rig.text.replace(str(rig.tmp), "<tmp>")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# The digests (sha256, first 16 hex digits) of the output of `origin/revival` at 48e8d86, the watcher
# before these seams, for the same scenarios; made by running `run_scenario` on that checkout.
PINNED: dict[str, str] = {
    "delayed": "5f0731dc7ab22e7c",
    "farm_break": "6f73a5b3e3eeb1bc",
    "file_error": "4ad960a0afc7305a",
    "no_data": "224dddd5f2ba0b79",
    "outage": "3e50c1bb682ce112",
    "rerequest": "4876df5b803f51bd",
    "revised": "372506026f5f116d",
    "second_run": "5358fa8c71ffa691",
    "session_end": "98ec471e4a049090",
    "stalled": "2727a6a9a11a1645",
    "standard": "288b1bac057b7edc",
    "standard_recorded": "5f21fabbbec8841e",
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_output_without_seams_is_byte_identical_to_origin_revival(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    text = run_scenario(rig, monkeypatch, name)
    assert digest(text) == PINNED[name], (
        f"the watcher's output for {name!r} changed. If that is intended, repin; this pin exists so that "
        "the seams of #196 could not change what the watcher says.\n" + text[-1500:])


def test_a_listener_and_a_sink_do_not_change_the_output(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """With every seam attached the output is the same text (nothing the watcher prints depends on them)."""
    plain = run_scenario(rig, monkeypatch, "standard")
    rig2 = Rig(rig.tmp / "second", ("AAPL", "MSFT"))
    rig2.tmp.mkdir()
    log = Harness(rig2, ("AAPL", "MSFT"))
    try:
        rig2.standard()
        assert run_with(rig2, monkeypatch, log.seams(), minutes=5) == 0
        with_seams = f"exit 0\n{rig2.text}".replace(str(rig2.tmp), "<tmp>")
    finally:
        for conn in rig2.connections:
            conn.close()
        for fake in rig2.fakes:
            fake.close()
    assert with_seams == plain


# -- the stream clock: delayed streams, mixed streams, stopped streams -----------------------------------


def _delayed_updates(rig: Rig, symbol: str, minutes: range, offsets: tuple[int, ...], lag: int,
                     px: float) -> None:
    for m in minutes:
        for i, s in enumerate(offsets):
            shown = (px, px + 0.5, px - 0.5, px + 0.1, 10 + 100 * i)
            _update_at(rig, symbol, ep(9, m), ep(9, m) + lag + s, shown)


def _backfill_of(first: int, last: int, px: float) -> list[tuple[Any, ...]]:
    return [(ep(9, m), px, px + 0.5, px - 0.5, px + 0.2, 100) for m in range(first, last + 1)]


def both_delayed(rig: Rig) -> None:
    """The reviewer's e5: IB's clock says 10:00:25 and both streams are 15 minutes behind."""
    rig.server_epoch = ep(10, 0, 25)
    rig.backfills = {"AAPL": [_backfill_of(36, 45, 99.0)], "MSFT": [_backfill_of(36, 45, 400.0)]}
    _delayed_updates(rig, "AAPL", range(45, 50), (35, 45, 58), 900, 99.0)
    _delayed_updates(rig, "MSFT", range(45, 50), (38, 50, 59), 900, 400.0)
    rig.end_at(6, symbols=2)


def mixed(rig: Rig, *, msft_stops: bool = False) -> None:
    """AAPL is live, MSFT is 15 minutes behind. MSFT's minutes 09:45 to 09:49 arrive at 10:00 to 10:05; AAPL
    has them in its backfill, and goes on with 10:00 to 10:04 as they happen."""
    rig.server_epoch = ep(10, 0, 25)
    rig.backfills = {"AAPL": [_backfill_of(36, 59, 99.0) + [(ep(10, 0), 99.0, 99.5, 98.8, 99.2, 50)]],
                     "MSFT": [_backfill_of(36, 45, 400.0)]}
    for m in range(1, 5):
        for s in (3, 30, 55):
            _update_at(rig, "AAPL", ep(10, m), ep(10, m) + s, (99.0, 99.5, 98.8, 99.2, 10 + s))
    for s in (30, 55):
        _update_at(rig, "AAPL", ep(10, 0), ep(10, 0) + s, (99.0, 99.5, 98.8, 99.2, 10 + s))
    # MSFT stops on 09:46's row: it sends nothing after the first update of 09:47
    _delayed_updates(rig, "MSFT", range(45, 47 if msft_stops else 50), (38, 50, 59), 900, 400.0)
    if msft_stops:
        _update_at(rig, "MSFT", ep(9, 47), ep(9, 47) + 900 + 3, (400.0, 400.5, 399.5, 400.1, 10))
    rig.end_at(6, symbols=2)


def assert_nothing_lost(h: Harness) -> None:
    assert h.aligner is not None
    zeros = {s: 0 for s in h.symbols}
    assert h.aligner.late_counts() == zeros
    assert h.aligner.released_without_counts() == zeros
    assert h.aligner.refused_counts() == zeros
    assert h.aligner.late_after_decided_counts() == zeros
    stamps = [b[0] for b in h.batches]
    assert stamps and stamps == sorted(set(stamps))


def test_two_delayed_symbols_lose_no_bar_when_polled_with_the_stream_clock(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    both_delayed(rig)
    h = Harness(rig, ("AAPL", "MSFT"))
    assert run_with(rig, monkeypatch, h.seams(), minutes=6, log=h) == 0, rig.text
    h.finish()
    assert_nothing_lost(h)
    assert {int(b[1]["AAPL"].time) for b in h.batches} >= {ep(9, m) for m in range(36, 49)}
    assert all(set(b[1]) == {"AAPL", "MSFT"} for b in h.batches)
    for m in (46, 47, 48):  # released when the slower of the two has closed it, 15 minutes after it ended
        assert 900 <= h.release[ep(9, m)] - (ep(9, m) + 60) < 960
    # the clocks: the stream clock is the stream's, about 15 minutes behind IB's
    measured = [c for c in h.clocks if c.stream_now is not None and c.server_now > ep(10, 1)]
    assert measured and all(880 < c.server_now - c.stream_now < 960 for c in measured)


def test_polled_with_server_time_the_same_session_loses_bars(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mistake the stream clock exists to avoid: polling the aligner with IB's server time."""
    both_delayed(rig)
    h = Harness(rig, ("AAPL", "MSFT"))
    h.poll_with_server_time = True
    run_with(rig, monkeypatch, h.seams(), minutes=6, log=h)
    h.finish()
    assert h.aligner is not None and sum(h.aligner.late_counts().values()) > 0


def test_a_live_and_a_delayed_symbol_hold_the_live_bars_until_the_delayed_minute_is_decided(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    mixed(rig)
    h = Harness(rig, ("AAPL", "MSFT"))
    assert run_with(rig, monkeypatch, h.seams(), minutes=6, log=h) == 0, rig.text
    h.finish()
    assert_nothing_lost(h)
    for m in (46, 47, 48):
        batch = next(b for b in h.batches if int(b[1]["AAPL"].time) == ep(9, m))
        assert set(batch[1]) == {"AAPL", "MSFT"}
        held = h.release[ep(9, m)] - (ep(9, m) + 60)  # AAPL had its bar within seconds
        assert 900 <= held < 960, held  # measured: the live symbol waits for the delayed one
    assert 880 < max(c.server_now - c.stream_now for c in h.clocks if c.stream_now is not None) < 960
    states = h.watcher.stream_states()
    assert states["AAPL"].lag_bound is not None and states["AAPL"].lag_bound < 10
    assert states["MSFT"].lag_bound is not None and 890 < states["MSFT"].lag_bound < 930
    assert states["MSFT"].delayed and not states["AAPL"].delayed


def test_when_the_delayed_symbol_stops_the_stream_clock_moves_to_the_remaining_symbols(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(self: Any, symbol: str, bar: Any) -> bool:
        if symbol == "MSFT" and bar.time == ep(9, 46):
            raise watch_bars.LiveFileError("cannot open or append to X: PermissionError: denied", locked=True)
        return REAL_FILES_WRITE(self, symbol, bar)

    mixed(rig, msft_stops=True)
    h = Harness(rig, ("AAPL", "MSFT"))
    seams = h.seams()
    monkeypatch.setattr(watch_bars.LiveFiles, "write", failing)
    assert run_with(rig, monkeypatch, seams, minutes=6) == watch_bars.EXIT_FAILED, rig.text
    h.finish()
    before = [c for c, s in zip(h.clocks, h.states, strict=True) if s["MSFT"].running and c.stream_now]
    after = [c for c, s in zip(h.clocks, h.states, strict=True) if not s["MSFT"].running and c.stream_now]
    assert before and after
    assert all(c.server_now - c.stream_now > 880 for c in before[-5:])
    assert all(c.server_now - c.stream_now < 60 for c in after)  # now AAPL's clock
    assert h.aligner is not None and h.aligner.late_counts() == {"AAPL": 0, "MSFT": 0}
    assert h.aligner.refused_counts() == {"AAPL": 0, "MSFT": 0}


def test_stream_now_is_none_until_a_lag_is_measured_and_server_time_when_nothing_runs(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server_epoch = ep(9, 28, 0)
    rig.backfills = {"AAPL": [[]]}
    rig.upd("AAPL", 30, (30, 5.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 31, (31, 3.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(4, symbols=1)
    h = Harness(rig, ("AAPL",), align=False)
    assert run_with(rig, monkeypatch, h.seams(), symbols="AAPL", minutes=4) == 0, rig.text
    assert h.clocks[0].stream_now is None and h.states[0]["AAPL"].lag_bound is None
    first = next(i for i, c in enumerate(h.clocks) if c.stream_now is not None)
    assert first > 0 and all(c.stream_now is None for c in h.clocks[:first])
    assert all(c.stream_now is not None for c in h.clocks[first:])
    assert h.states[first]["AAPL"].lag_bound == pytest.approx(5.0)
    assert h.clocks[first].stream_now == pytest.approx(h.clocks[first].server_now - 5.0)
    # no symbol running: the server time
    for s in h.watcher.syms:
        s.phase = "stopped"
    final = h.watcher.stream_clock()
    assert final.stream_now == final.server_now
    assert not h.watcher.stream_states()["AAPL"].running


def test_a_bar_closed_by_time_with_a_lag_bound_of_30_seconds_is_not_late(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher closes 09:33 by time at 09:35:30 on the stream clock (09:36:00 by IB's), the aligner
    releases at 09:35:40 on the clock it is polled with: with the stream clock the bar comes first."""
    rig.server_epoch = ep(9, 33, 20)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    _update_at(rig, "AAPL", ep(9, 33), ep(9, 33, 30), (100.0, 100.5, 99.9, 100.2, 1000))  # lag bound 30
    _update_at(rig, "AAPL", ep(9, 33), ep(9, 34, 25), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.backfills["MSFT"] = [[(ep(9, m), 400.0, 400.5, 399.5, 400.2, 100) for m in (30, 31, 32)]]
    for m in range(33, 38):  # MSFT is current and keeps the aligner's newest minute moving
        for s in (4, 40):
            _update_at(rig, "MSFT", ep(9, m), ep(9, m, s), (400.0, 400.5, 399.5, 400.2, 10 + s))
    for when in range(ep(9, 34, 40), ep(9, 38, 0), 10):  # a look every 10 s
        _at(rig, when, lambda: None)
    rig.end_at(5, symbols=2)
    h = Harness(rig, ("AAPL", "MSFT"))
    assert run_with(rig, monkeypatch, h.seams(), minutes=5, log=h) == 0, rig.text
    h.finish()
    assert "closed by time" in rig.text and ep(9, 33) in file_minutes(rig, "AAPL")
    assert h.watcher.stream_states()["AAPL"].lag_bound == pytest.approx(30.0)
    assert h.aligner is not None and h.aligner.late_counts() == {"AAPL": 0, "MSFT": 0}
    batch = next(b for b in h.batches if int(b[1]["MSFT"].time) == ep(9, 33))
    assert set(batch[1]) == {"AAPL", "MSFT"}  # the minute waited for the thin symbol's bar


class _BadClose:
    written: dict[str, list[Any]] = {}

    def write(self, symbol: str, bar: Any, how: str) -> bool:
        return True

    def close(self) -> None:
        raise RuntimeError("close boom")


def test_a_sink_that_raises_in_close_is_one_line_and_the_connection_is_still_closed(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.standard()
    seams = watch_bars.WatchSeams(extra_sinks=(_BadClose(),))
    assert run_with(rig, monkeypatch, seams, minutes=5) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("Closing the bar sinks raised RuntimeError: close boom") == 1
    assert "Traceback" not in rig.text and "Result: " in rig.text
    assert all(c.state is ConnState.CLOSED for c in rig.connections)


def test_a_sink_that_raises_in_close_after_a_recorder_failure_is_one_line_too(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(args: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(rig, "recorder_factory", refuse)
    seams = watch_bars.WatchSeams(extra_sinks=(_BadClose(),))
    assert run_with(rig, monkeypatch, seams, record=True) == watch_bars.EXIT_REFUSED, rig.text
    assert "Cannot start the session recording: OSError: disk full" in rig.text
    assert rig.text.count("Closing the bar sinks raised RuntimeError: close boom") == 1
    assert "Traceback" not in rig.text
    assert not rig.connections


# -- F1, the early stop and the end cause (issue 197) ---------------------------------------------------


def test_f1_stream_now_stays_none_while_a_running_symbol_has_no_measured_lag(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """AAPL is live from the open; MSFT's backfill holds only the previous session and its first bar of
    today shows 15 minutes late. Its lag is unmeasured until then: the stream clock must wait (None), or
    minutes would be released without MSFT and its bars would arrive late."""
    rig.server_epoch = ep(9, 30, 10)
    rig.backfills = {
        "AAPL": [[(ep(9, 30), 99.0, 99.5, 98.8, 99.2, 100)]],
        "MSFT": [[(ep(9, 30) - 86400 + 60 * k, 400.0, 400.5, 399.5, 400.2, 100) for k in range(3)]]}
    for m in range(30, 38):
        _update_at(rig, "AAPL", ep(9, m), ep(9, m, 4), (99.0, 99.5, 98.8, 99.2, 100 + m))
    _update_at(rig, "MSFT", ep(9, 30), ep(9, 45, 4), (400.0, 400.5, 399.5, 400.2, 100))
    _update_at(rig, "MSFT", ep(9, 31), ep(9, 46, 4), (400.2, 400.7, 400.1, 400.5, 100))
    for when in range(ep(9, 31), ep(9, 47), 30):  # the watcher looks every 30 s
        _at(rig, when, lambda: None)
    rig.end_at(17, symbols=2)
    h = Harness(rig, ("AAPL", "MSFT"))
    run_with(rig, monkeypatch, h.seams(), minutes=17, log=h)
    h.finish()
    unmeasured = [c for c in h.clocks if c.server_now < ep(9, 45, 4)]
    assert len(unmeasured) > 20 and all(c.stream_now is None for c in unmeasured)
    assert any(c.stream_now is not None for c in h.clocks if c.server_now > ep(9, 45, 6))
    assert h.aligner is not None
    zeros = {"AAPL": 0, "MSFT": 0}
    assert h.aligner.late_counts() == zeros and h.aligner.refused_counts() == zeros
    assert h.aligner.released_without_counts() == zeros
    assert h.release and min(h.release.values()) >= ep(9, 45)  # nothing was released before MSFT was heard


def test_request_stop_from_the_hook_ends_the_watch_at_once_and_closes_everything_once(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.standard()
    h = Harness(rig, ("AAPL", "MSFT"), align=False)

    def hook(clock: Any) -> None:
        h.on_pass(clock)
        if clock.server_now >= ep(9, 35):
            h.watcher.request_stop("enough")
            h.watcher.request_stop("again")  # the first reason stays

    seams = watch_bars.WatchSeams(extra_sinks=(h,), listener=h, on_pass=hook, on_watcher=h.set_watcher)
    code = run_with(rig, monkeypatch, seams, minutes=5, log=h)
    text = rig.text
    assert code == watch_bars.EXIT_OK, text
    assert "Stopped on the driver's request (enough)." in text and "(again)" not in text
    assert "Watch time over" not in text and "No symbol is streaming" not in text
    assert h.watcher.stop_requested and h.watcher.end_cause == "stop_requested"
    assert h.closed == 1  # the sink is closed once
    assert all(c.state is ConnState.CLOSED for c in rig.connections)
    cancelled = [c for f in rig.fakes for c in f.calls if c.name == "cancelHistoricalData"]
    assert len(cancelled) == 2  # both requests were cancelled
    assert rig.t < rig.tt(36, 0.0)  # prompt: it did not wait for the watch time (5 minutes)
    assert "Summary" in text and "Result: " in text
    assert [r[0][11:16] for r in rig.rows("AAPL")][:2] == ["09:30", "09:31"]  # what was closed stays


def _end_cause(rig: Rig, mp: pytest.MonkeyPatch, symbols: str, minutes: int, **kw: Any) -> tuple[int, str]:
    h = Harness(rig, tuple(symbols.split(",")), align=False)
    code = run_with(rig, mp, h.seams(), symbols=symbols, minutes=minutes, **kw)
    return code, h.watcher.end_cause


def _one_symbol(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))


def test_end_cause_says_why_the_watch_ended(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    causes: dict[str, str] = {}
    rigs: list[Rig] = []

    def fresh(name: str) -> Rig:
        r = Rig(tmp_path / name, ("AAPL", "MSFT"))
        r.tmp.mkdir()
        rigs.append(r)
        return r

    try:
        r = fresh("deadline")
        r.standard()
        causes["deadline"] = _end_cause(r, monkeypatch, "AAPL,MSFT", 5)[1]
        r = fresh("closed")
        _one_symbol(r)
        r.drop((34, 20.0), "connectionClosed")
        r.at(r.tt(34, 40.0), lambda: None)
        code, causes["closed"] = _end_cause(r, monkeypatch, "AAPL", 5)
        assert code == watch_bars.EXIT_STOPPED
        r = fresh("lost")
        _one_symbol(r)
        r.drop((33, 40.0), 1100)
        r.end_at(2, symbols=1)
        code, causes["lost"] = _end_cause(r, monkeypatch, "AAPL", 2)
        assert code == watch_bars.EXIT_STOPPED
        r = fresh("not_paper")
        _one_symbol(r)
        r.at(r.tt(34, 10.0), lambda: r.fakes[-1].push("managedAccounts", "U7654321"))
        r.expected = 0
        r.end_at(4, symbols=1)
        code, causes["not_paper"] = _end_cause(r, monkeypatch, "AAPL", 4, paper=True)
        assert code == watch_bars.EXIT_NOT_PAPER
        r = fresh("interrupted")
        r.standard()
        r.interrupt_after = 3
        code, causes["interrupted"] = _end_cause(r, monkeypatch, "AAPL,MSFT", 5)
        assert code == watch_bars.EXIT_INTERRUPTED
        r = fresh("idle")
        r.history_error = {"AAPL": (162, "Historical Market Data Service error message: pacing")}
        r.end_at(2, symbols=1)
        code, causes["idle"] = _end_cause(r, monkeypatch, "AAPL", 2)
        assert code == watch_bars.EXIT_FAILED
    finally:
        for r in rigs:
            for conn in r.connections:
                conn.close()
            for fake in r.fakes:
                fake.close()
    assert causes == {"deadline": "deadline", "closed": "closed", "lost": "lost", "not_paper": "not_paper",
                      "interrupted": "interrupted", "idle": "idle"}


def test_is_down_is_true_while_the_connection_is_lost(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.at(rig.tt(35, 35.0), lambda: None, ready=lambda: "connectivity LOST" in rig.text)
    rig.drop((35, 40.0), 1102)
    rig.at(rig.tt(35, 42.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.end_at(4, symbols=1)
    h = Harness(rig, ("AAPL",), align=False)
    seen: list[tuple[float, bool]] = []

    def hook(clock: Any) -> None:
        h.on_pass(clock)
        seen.append((clock.server_now, h.watcher.is_down()))

    seams = watch_bars.WatchSeams(extra_sinks=(h,), listener=h, on_pass=hook, on_watcher=h.set_watcher)
    run_with(rig, monkeypatch, seams, symbols="AAPL", minutes=4, log=h)
    assert not any(down for at, down in seen if at < ep(9, 33, 40))
    assert any(down for at, down in seen if ep(9, 33, 40) <= at < ep(9, 35, 30))
    assert not any(down for at, down in seen if at > ep(9, 35, 45))
