"""The gap rule: no new position on a bar that follows a gap (issue 175).

Owner decision, 2026-11-24: a bar that follows a gap carries no new signal.
Exits still work. Every occurrence is counted and recorded.

A bar "follows a gap" for a symbol when the previous bar of that symbol is in
the same session (same calendar date of the naive America/New_York data time)
and is not exactly one minute earlier, or when it is the first bar of its
session and is not 09:30. The previous bar of the first replayed bar is the
last warm-up bar of the symbol. A repeated timestamp is not one minute later
and so counts as following a gap. Half days need no special case: only the
start and the spacing are judged, never the close.

What "opens a new position" means here. The engine's order model has one
entry point, `Broker.open_position`: every call creates a new position (a new
opaque ref) with its own protective stop, whether the algo was flat, already
long or short (an add, or a second concurrent position), or is reversing (the
close of the old position plus the entry of the new one). Every such call on a
bar that follows a gap is refused. `close_position`, `modify_stop` and
`set_take_profit` of positions that exist are never refused; a reversal's exit
leg goes through and only its entry leg is refused, whatever their order.

Where it is enforced: `GapGuardBroker` wraps the broker of each algo while
`Engine.run_backtest` runs, so it holds for every algo and every broker, and no
algo changes. The algo still gets every bar: `update` runs, its indicators stay
in step; only the entry is refused.

How the algo learns of the refusal: the same way it learns that a position's
stop is gone, which is the existing unfilled-order path. `open_position`
returns a `RefusedEntry` ref instead of a broker ref. For that ref
`stop_is_working` is False, so `Trade.check_stoploss` returns 0 on the algo's
next bar, and the algo leaves its trade (`inTrade = False`) exactly as it does
when the broker cancelled an entry at a session end. Calls on the refused ref
(`modify_stop`, `set_take_profit`, `close_position`) are ignored: there is no
position. Between the refusal and the algo's next bar the algo's own flag still
says "in a trade" (as it does for an entry the broker cancels during the bar).
The refused `Trade` object stays in `algo.trades`; `report.trade_records` skips
it because its ref is a `RefusedEntry`.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .broker import Broker

if TYPE_CHECKING:
    import pandas as pd

MAX_TIMES = 200
"""Timestamps kept per (algo, symbol); the count is always complete."""

_ONE_MINUTE = timedelta(minutes=1)


def follows_gap(previous: datetime | None, current: datetime) -> bool:
    """Whether a bar at `current` follows a gap, given the symbol's previous bar time.

    `previous` None means the symbol has no earlier bar at all.
    """
    if previous is None or previous.date() != current.date():
        return (current.hour, current.minute) != (9, 30)
    return current - previous != _ONE_MINUTE


@dataclass(frozen=True)
class RefusedEntry:
    """The ref `GapGuardBroker.open_position` returns for a refused entry."""

    algo: str
    symbol: str
    time: datetime
    number: int


@dataclass(frozen=True)
class SuppressedEntries:
    """Refused entries of one algo on one symbol.

    `count` is the total; `times` holds the first `MAX_TIMES` of them, in order.
    """

    algo: str
    symbol: str
    count: int
    times: tuple[datetime, ...]


@dataclass(frozen=True)
class GapRuleReport:
    """What the gap rule did in a run.

    `bars_after_gap` has every replayed symbol (feed order), 0 when none of its
    bars follows a gap. `suppressed` lists (algo, symbol) pairs that had a
    refusal, in order of first refusal.
    """

    bars_after_gap: tuple[tuple[str, int], ...] = ()
    suppressed: tuple[SuppressedEntries, ...] = ()

    @property
    def total_bars_after_gap(self) -> int:
        return sum(n for _, n in self.bars_after_gap)

    @property
    def total_suppressed(self) -> int:
        return sum(s.count for s in self.suppressed)

    @property
    def empty(self) -> bool:
        return self.total_bars_after_gap == 0 and self.total_suppressed == 0


class GapRule:
    """Per-run state of the gap rule: the gap test and the record of what it did.

    The engine calls `on_bar` when a symbol's bar arrives and `end_bar` after
    that symbol's algos were updated; `GapGuardBroker` asks `blocked` and calls
    `refuse`.
    """

    def __init__(self, last_times: Mapping[str, datetime | None]) -> None:
        self._previous: dict[str, datetime | None] = dict(last_times)
        self._counts: dict[str, int] = {symbol: 0 for symbol in last_times}
        self._current: dict[str, datetime] = {}
        self._suppressed: dict[tuple[str, str], list[datetime]] = {}
        self._totals: dict[tuple[str, str], int] = {}
        self._refusals = 0

    def start(self, last_times: Mapping[str, datetime | None]) -> None:
        """Begin a replay: the last warm-up bar time of each symbol (None: no warm-up)."""
        self._previous = dict(last_times)
        self._counts = {symbol: 0 for symbol in last_times}

    def on_bar(self, symbol: str, time: datetime) -> bool:
        """Register the symbol's new bar; True when it follows a gap (entries blocked)."""
        gap = follows_gap(self._previous.get(symbol), time)
        self._previous[symbol] = time
        if gap:
            self._counts[symbol] = self._counts.get(symbol, 0) + 1
            self._current[symbol] = time
        return gap

    def end_bar(self, symbol: str) -> None:
        """The symbol's algos have been updated: entries are open again."""
        self._current.pop(symbol, None)

    def blocked(self, symbol: str) -> bool:
        return symbol in self._current

    def refuse(self, algo: str, symbol: str) -> RefusedEntry:
        """Record a refused entry (at the current bar of `symbol`) and return its ref."""
        time = self._current[symbol]
        key = (algo, symbol)
        times = self._suppressed.setdefault(key, [])
        self._totals[key] = self._totals.get(key, 0) + 1
        if len(times) < MAX_TIMES:
            times.append(time)
        self._refusals += 1
        return RefusedEntry(algo, symbol, time, self._refusals)

    def report(self) -> GapRuleReport:
        return GapRuleReport(
            tuple(self._counts.items()),
            tuple(SuppressedEntries(a, s, self._totals[(a, s)], tuple(t))
                  for (a, s), t in self._suppressed.items()),
        )


class GapGuardBroker:
    """A broker for one algo that refuses entries on bars that follow a gap.

    Satisfies `Broker`; everything but a refused entry is passed to `inner`.
    """

    def __init__(self, inner: Broker, algo: str, rule: GapRule) -> None:
        self._inner = inner
        self._algo = algo
        self._rule = rule

    @property
    def inner(self) -> Broker:
        return self._inner

    def open_position(
        self,
        *,
        symbol: str,
        volume: float,
        direction: int,
        price: float,
        time: pd.Timestamp,
        limit: bool,
        stop_price: float,
        oca_group: str,
    ) -> Hashable:
        if self._rule.blocked(symbol):
            return self._rule.refuse(self._algo, symbol)
        return self._inner.open_position(
            symbol=symbol, volume=volume, direction=direction, price=price, time=time,
            limit=limit, stop_price=stop_price, oca_group=oca_group)

    def modify_stop(self, ref: Hashable, stop_price: float) -> None:
        if not isinstance(ref, RefusedEntry):
            self._inner.modify_stop(ref, stop_price)

    def set_take_profit(self, ref: Hashable, price: float, quantity: float) -> None:
        if not isinstance(ref, RefusedEntry):
            self._inner.set_take_profit(ref, price, quantity)

    def close_position(self, ref: Hashable, *, price: float, time: pd.Timestamp) -> None:
        if not isinstance(ref, RefusedEntry):
            self._inner.close_position(ref, price=price, time=time)

    def stop_is_working(self, ref: Hashable) -> bool:
        if isinstance(ref, RefusedEntry):
            return False
        return self._inner.stop_is_working(ref)
