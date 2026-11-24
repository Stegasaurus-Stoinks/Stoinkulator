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

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from MainStoinker.engine.feed import Bar

DEFAULT_ALIGN_WAIT = 100.0
"""Seconds after a minute's end at which it is released without slow symbols
(approved decision D6; more than the watcher's 90 s settle time)."""

ACCEPTED = "accepted"
LATE = "late"
REFUSED = "refused"


class LiveBarAligner:
    """Turns per-symbol closed bars into one batch per minute (see module doc)."""

    def __init__(self, symbols: Sequence[str], align_wait: float = DEFAULT_ALIGN_WAIT) -> None:
        self._symbols: list[str] = list(dict.fromkeys(symbols))
        if not self._symbols:
            raise ValueError("LiveBarAligner needs at least one symbol")
        if align_wait < 0:
            raise ValueError("align_wait must not be negative")
        self._wait = float(align_wait)
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

        Refused (counted in `refused_counts`, never fed): the symbol is
        stopped, or the bar is not after the symbol's last bar or its decided
        point (duplicate or backwards). Late (counted `late_not_fed`, never
        fed): the bar's minute was already released. Otherwise accepted, and
        the symbol is decided through the bar's minute.
        """
        self._check(symbol)
        minute = int(bar.time)
        if symbol not in self._active or minute <= self._last_bar[symbol] or minute <= self._decided[symbol]:
            self._refused[symbol] += 1
            return REFUSED
        self._last_bar[symbol] = minute
        self._decided[symbol] = minute
        if minute <= self._released_through:
            self._late[symbol] += 1
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
        """Release every minute that is due at `server_now_epoch`; oldest first."""
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

    def released_without_counts(self) -> dict[str, int]:
        """Per symbol: released batches without it while it was still active."""
        return dict(self._without)

    def refused_counts(self) -> dict[str, int]:
        """Per symbol: bars refused as duplicate, backwards or after stop."""
        return dict(self._refused)


def _minute_of(batches: list[tuple[pd.Timestamp, dict[str, Bar]]]) -> int:
    """Epoch start of the last released batch."""
    return int(next(iter(batches[-1][1].values())).time)
