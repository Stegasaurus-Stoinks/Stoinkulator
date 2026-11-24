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

Clock guard: `poll(now)` raises `ValueError`, changing nothing, when `now` is
not finite or is more than `max_clock_ahead` seconds after the start of the
newest minute seen (newest bar, or newest decided point). `now` must be IB
server time in seconds, never milliseconds or a local clock. See
`DEFAULT_MAX_CLOCK_AHEAD`. Before any bar or decided point there is nothing to
compare with and any finite `now` passes (so does any finite `now` once every
symbol is stopped, as the clock then decides nothing), so deliver startup backfill before
the first poll: a poll releases everything up to `now - 60 - align_wait`.

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

DEFAULT_MAX_CLOCK_AHEAD = 600.0
"""Largest accepted lead of `poll`'s `now` over the start of the newest minute
seen, in seconds. A real poll is at most about 150 s after that start (the
minute's 60 s, plus a bar closed by time up to 90 s after its end), so 600 s is
four times the legitimate maximum and tolerates a quiet stretch of several
minutes in which no new bar or decided point arrives. It still refuses what
goes wrong in practice: milliseconds (1000 times too big), a local clock in the
wrong time zone (hours off) or a clock fast by 10 minutes or more. A clock fast
by less than that is not caught here and turns bars late, which is counted."""

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
        max_clock_ahead: float = DEFAULT_MAX_CLOCK_AHEAD,
    ) -> None:
        self._symbols: list[str] = list(dict.fromkeys(symbols))
        if not self._symbols:
            raise ValueError("LiveBarAligner needs at least one symbol")
        if align_wait < 0:
            raise ValueError("align_wait must not be negative")
        if not max_clock_ahead >= 0:
            raise ValueError("max_clock_ahead must not be negative")
        self._wait = float(align_wait)
        self._max_ahead = float(max_clock_ahead)
        self._newest = float("-inf")  # start of the newest minute seen (bar or decided point)
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
            self._newest = max(self._newest, through_epoch)

    def stopped(self, symbol: str) -> None:
        """Symbol will give nothing more; it no longer holds minutes back."""
        self._check(symbol)
        self._active.discard(symbol)

    def poll(self, server_now_epoch: float) -> list[tuple[pd.Timestamp, dict[str, Bar]]]:
        """Release every minute that is due at `server_now_epoch`; oldest first.

        Raises `ValueError`, with no state changed, when `server_now_epoch` is
        not finite or is more than `max_clock_ahead` seconds after the start of
        the newest minute seen (see the module doc).
        """
        if not math.isfinite(server_now_epoch):
            raise ValueError(f"poll time must be finite, got {server_now_epoch!r}")
        ahead = server_now_epoch - self._newest
        if self._active and self._newest != float("-inf") and ahead > self._max_ahead:
            raise ValueError(
                f"poll time {server_now_epoch!r} is {ahead:.0f} s after the newest "
                f"minute seen ({self._newest!r}), more than max_clock_ahead={self._max_ahead:g} s: "
                "is it IB server time in seconds?"
            )
        if self._active:
            through = max(
                min(self._decided[s] for s in self._active),
                server_now_epoch - 60 - self._wait,
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
