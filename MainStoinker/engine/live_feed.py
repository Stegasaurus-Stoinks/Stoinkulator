"""Cross-symbol alignment of live closed bars into one batch per minute.

`LiveBarAligner` is pure: no clock, no threads, no I/O. The caller feeds it the
closed bars of each symbol, tells it how far each symbol is decided, and polls
it with the current server time; it returns the minute batches the engine
consumes, in the shape `CsvBarFeed` yields: `(Timestamp, {symbol: Bar})`, keys
in symbol order.

Vocabulary: a minute is identified by its start, `Bar.time` (epoch seconds);
its end is `start + 60`. A symbol is *decided through* minute M when it has
either given its bar for every minute up to M that it has, or it will never
give one (the minute was dropped). `bar()` implies decided through that bar's
minute.

Release rule (exact): at `poll(now)` every pending minute M is released, in
time order, when
  - every active (not stopped) symbol is decided through M, or
  - `now >= M + 60 + align_wait`, or
  - no symbol is active any more.
The batch holds the bars given for M; symbols without one are skipped (counted
in `released_without_counts`). A minute with no bar at all produces no batch.
Once a minute is released, every minute before it is released too, so batches
are strictly increasing in time and never empty.

Acceptance of a bar does not depend on the order of `bar()` and `decided()`
calls: a bar is refused only if it is a duplicate or backwards (its minute is
not after the symbol's last bar), the symbol is stopped, or its `date` and
`time` disagree. Otherwise it is accepted while its minute is still pending
and late once its minute was released.

Clock. `now` must be IB server time in seconds. `poll(now)` raises
`ValueError`, changing nothing, only for a `now` that cannot be a time in
seconds: not finite, negative or above `MAX_PLAUSIBLE_EPOCH` (milliseconds or
worse). It never raises for anything else, so a long outage, the time after the
close or the morning after a backfill is an ordinary poll.

Time-based release uses a clamped clock: `min(now, newest_bar + max_clock_ahead)`,
where `newest_bar` is the start of the newest minute of any bar given (not a
decided point: a bogus `decided` cannot loosen the clamp). A poll whose clock
was clamped (only counted while some symbol is active) increments
`clock_clamped_count()` and raises `max_clock_excess()` to the largest excess
seen. Before the first bar has arrived there is no reference: `poll` returns
nothing and changes nothing, so a wrong first poll cannot poison the run.

What the clamp means. A correct clock is at most about 150 s past the start of
the newest minute in normal running (60 s, plus a bar closed by time up to 90 s
after its end), and the newest pending minute needs `60 + align_wait` s to be
released by time, so `max_clock_ahead` must be at least `60 + align_wait`
(enforced). The default is that plus 20 s for 1 s clock resolution and skew:
`DEFAULT_MAX_CLOCK_AHEAD` = 180 s, as small as that allows. The clamp only ever
lowers the clock, so it never makes a bar of a correct clock late. A
moderately wrong clock (fast by minutes or hours, for example a time-zone
error) now behaves so: the effective clock runs `max_clock_ahead` ahead of the
newest bar, so minutes starting up to `max_clock_ahead - 60 - align_wait` (20 s
by default: the newest minute only) past the newest bar are released by time
at once, the first poll after a minute's first bar releases it, and the other
symbols' bars for that minute arrive late and are counted late. A wrong clock
therefore shows up as late bars together with a non-zero clamp counter, and the
driver must treat both as a fault. A long outage also clamps (the counter
rises) but loses nothing: bars after it are accepted and fed.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd

from MainStoinker.engine.feed import Bar

DEFAULT_ALIGN_WAIT = 100.0
"""Seconds after a minute's end at which it is released without slow symbols
(approved decision D6; more than the watcher's 90 s settle time)."""

DEFAULT_MAX_CLOCK_AHEAD = 60.0 + DEFAULT_ALIGN_WAIT + 20.0
"""How far past the start of the newest bar's minute the clock used for
time-based release may run, in seconds: the 60 s of the minute, the align wait,
and 20 s for clock resolution and skew (see the module doc for why this is the
smallest sensible value)."""

MAX_PLAUSIBLE_EPOCH = 1e11
"""`poll` refuses a `now` above this (year 5138): epoch milliseconds are 1.7e12."""

_NEW_YORK = "America/New_York"

ACCEPTED = "accepted"
LATE = "late"
REFUSED = "refused"


class LiveBarAligner:
    """Turns per-symbol closed bars into one batch per minute (see module doc)."""

    def __init__(
        self,
        symbols: Sequence[str],
        align_wait: float = DEFAULT_ALIGN_WAIT,
        max_clock_ahead: float | None = None,
    ) -> None:
        self._symbols: list[str] = list(dict.fromkeys(symbols))
        if not self._symbols:
            raise ValueError("LiveBarAligner needs at least one symbol")
        if align_wait < 0:
            raise ValueError("align_wait must not be negative")
        if max_clock_ahead is None:
            max_clock_ahead = 60.0 + align_wait + (DEFAULT_MAX_CLOCK_AHEAD - 60.0 - DEFAULT_ALIGN_WAIT)
        if not max_clock_ahead >= 60.0 + align_wait:
            raise ValueError("max_clock_ahead must be at least 60 + align_wait")
        self._wait = float(align_wait)
        self._max_ahead = float(max_clock_ahead)
        self._newest = float("-inf")  # start of the newest minute of any bar given
        self._clamped = 0
        self._excess = 0.0
        self._late_decided = dict.fromkeys(self._symbols, 0)
        self._order = {s: i for i, s in enumerate(self._symbols)}
        self._active: set[str] = set(self._symbols)
        self._decided: dict[str, float] = {s: float("-inf") for s in self._symbols}
        self._last_bar: dict[str, float] = {s: float("-inf") for s in self._symbols}
        self._pending: dict[int, dict[str, Bar]] = {}
        self._released_through = float("-inf")
        self._fed = dict.fromkeys(self._symbols, 0)
        self._late = dict.fromkeys(self._symbols, 0)
        self._without = dict.fromkeys(self._symbols, 0)
        self._refused = dict.fromkeys(self._symbols, 0)

    def symbols(self) -> list[str]:
        """The symbols, in the order batches use (a fresh list)."""
        return list(self._symbols)

    def _check(self, symbol: str) -> None:
        if symbol not in self._order:
            raise KeyError(f"unknown symbol {symbol!r}; aligner has {self._symbols}")

    def bar(self, symbol: str, bar: Bar) -> str:
        """Give a closed bar. Returns `"accepted"`, `"late"` or `"refused"`.

        Refused (counted in `refused_counts`, never fed, no other state
        changes): the symbol is stopped, or the bar's minute is not after the
        symbol's last bar (duplicate or backwards), or its `date` is not the
        naive New York time of its epoch `time`. The symbol's decided point
        does not matter, so `decided` and `bar` may come in either order.
        Late (never fed): the bar's minute was already released; counted in
        `late_counts`, or in `late_after_decided_counts` when the minute was
        also at or before the symbol's decided point (a driver that tells
        `decided` too early or too eagerly shows up there). Otherwise accepted,
        and the symbol is decided through the bar's minute.
        """
        self._check(symbol)
        minute = int(bar.time)
        if symbol not in self._active or minute <= self._last_bar[symbol] or not _date_matches(bar):
            self._refused[symbol] += 1
            return REFUSED
        behind_decided = minute <= self._decided[symbol]
        self._last_bar[symbol] = minute
        self._decided[symbol] = max(self._decided[symbol], minute)
        self._newest = max(self._newest, minute)
        if minute <= self._released_through:
            (self._late_decided if behind_decided else self._late)[symbol] += 1
            return LATE
        self._pending.setdefault(minute, {})[symbol] = bar
        return ACCEPTED

    def decided(self, symbol: str, through_epoch: int) -> None:
        """Symbol has no more bars for any minute starting at or before `through_epoch`.

        Never moves a symbol's decided point back; a stopped symbol is ignored.
        """
        self._check(symbol)
        if symbol in self._active and through_epoch > self._decided[symbol]:
            self._decided[symbol] = through_epoch

    def stopped(self, symbol: str) -> None:
        """Symbol will give nothing more; it no longer holds minutes back."""
        self._check(symbol)
        self._active.discard(symbol)

    def poll(self, server_now_epoch: float) -> list[tuple[pd.Timestamp, dict[str, Bar]]]:
        """Release every minute that is due at `server_now_epoch`; oldest first.

        Raises `ValueError`, with no state changed, only when `server_now_epoch`
        cannot be a time in seconds: not finite, negative or above
        `MAX_PLAUSIBLE_EPOCH`. Otherwise time-based release uses the clock
        clamped to `max_clock_ahead` past the newest bar (see the module doc).
        Before the first bar this returns nothing and changes nothing.
        """
        if not math.isfinite(server_now_epoch) or not 0 <= server_now_epoch <= MAX_PLAUSIBLE_EPOCH:
            raise ValueError(
                f"poll time {server_now_epoch!r} cannot be an epoch time in seconds "
                f"(finite, 0 to {MAX_PLAUSIBLE_EPOCH:g}): is it IB server time in seconds?"
            )
        if self._newest == float("-inf"):
            return []
        clock = server_now_epoch
        if self._active:
            cap = self._newest + self._max_ahead
            if clock > cap:
                self._clamped += 1
                self._excess = max(self._excess, clock - cap)
                clock = cap
            through = max(
                min(self._decided[s] for s in self._active),
                clock - 60 - self._wait,
            )
        else:
            through = float("inf")
        out: list[tuple[pd.Timestamp, dict[str, Bar]]] = []
        for minute in sorted(m for m in self._pending if m <= through):
            bars = self._pending.pop(minute)
            batch = {s: bars[s] for s in self._symbols if s in bars}
            for symbol in self._symbols:
                if symbol in batch:
                    self._fed[symbol] += 1
                elif symbol in self._active:
                    self._without[symbol] += 1
            out.append((next(iter(batch.values())).date, batch))
        if through != float("inf"):
            self._released_through = max(self._released_through, through)
        elif out:
            self._released_through = max(self._released_through, _minute_of(out))
        return out

    def pending_minutes(self) -> int:
        """How many minutes hold bars not yet released."""
        return len(self._pending)

    def fed_counts(self) -> dict[str, int]:
        """Per symbol: bars released in a batch."""
        return dict(self._fed)

    def late_counts(self) -> dict[str, int]:
        """Per symbol: bars that arrived after their minute was released (not fed)."""
        return dict(self._late)

    def clock_clamped_count(self) -> int:
        """Polls (with an active symbol) whose clock ran past `max_clock_ahead` and was clamped.

        Non-zero means a long quiet stretch or outage, or a wrong clock; if bars
        also come back late, treat it as a fault.
        """
        return self._clamped

    def max_clock_excess(self) -> float:
        """The largest number of seconds by which a polled clock exceeded the clamp."""
        return self._excess

    def late_after_decided_counts(self) -> dict[str, int]:
        """Per symbol: late bars whose minute was also at or before the symbol's decided point."""
        return dict(self._late_decided)

    def released_without_counts(self) -> dict[str, int]:
        """Per symbol: released batches without it while it was still active."""
        return dict(self._without)

    def refused_counts(self) -> dict[str, int]:
        """Per symbol: bars refused as duplicate, backwards, after stop or with date and time disagreeing."""
        return dict(self._refused)


def _date_matches(bar: Bar) -> bool:
    """True when `bar.date` is the naive New York wall-clock time of epoch `bar.time`."""
    try:
        expected = pd.Timestamp(int(bar.time), unit="s", tz="UTC").tz_convert(_NEW_YORK).tz_localize(None)
        return bool(bar.date == expected)
    except (TypeError, ValueError, OverflowError):
        return False


def _minute_of(batches: list[tuple[pd.Timestamp, dict[str, Bar]]]) -> int:
    """Epoch start of the last released batch."""
    return int(next(iter(batches[-1][1].values())).time)
