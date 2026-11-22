"""Event sinks: where the engine reports what it saw.

A sink receives each closed bar together with the per-algo front-end data for
that symbol in a single `on_bar` call, because the existing front end takes
both in one message (`update_send` with `tickerdata` and `algodata`); keeping
them together lets a socket sink emit exactly that message. Algo data is only
computed when `wants_algo_data` is true, since building it is not free.

A socket.io sink is deferred to the live phase. This module must not import
anything from `MainStoinker.*` outside `MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from MainStoinker.engine.feed import Bar


@runtime_checkable
class EventSink(Protocol):
    """What the engine needs from an event consumer."""

    wants_algo_data: bool

    def on_bar(self, symbol: str, bar: Bar, algo_data: list | None) -> None:
        """One call per symbol per closed bar.

        `algo_data` is the list of per-algo front-end dicts for `symbol` when
        `wants_algo_data` is true, else `None`.
        """
        ...

    def on_intrabar(self, symbol: str, bar: Bar) -> None:
        """A still-forming bar for `symbol` (live feeds only)."""
        ...


class NullSink:
    """Discards every event and never asks for algo data."""

    wants_algo_data: bool = False

    def on_bar(self, symbol: str, bar: Bar, algo_data: list | None) -> None:
        return None

    def on_intrabar(self, symbol: str, bar: Bar) -> None:
        return None


@dataclass(frozen=True)
class SinkEvent:
    """One recorded sink event.

    `kind` is "bar" or "intrabar". `algo_data` is only meaningful for "bar"
    events (it is `None` for "intrabar").
    """

    kind: Literal["bar", "intrabar"]
    symbol: str
    bar: Bar
    algo_data: list | None = None


class RecordingSink:
    """Records every event in order, for tests."""

    def __init__(self, wants_algo_data: bool = False) -> None:
        self.wants_algo_data: bool = wants_algo_data
        self._events: list[SinkEvent] = []

    @property
    def events(self) -> tuple[SinkEvent, ...]:
        """Snapshot of the recorded events, in order. Mutating it does not affect the log."""
        return tuple(self._events)

    def on_bar(self, symbol: str, bar: Bar, algo_data: list | None) -> None:
        self._events.append(SinkEvent("bar", symbol, bar, algo_data))

    def on_intrabar(self, symbol: str, bar: Bar) -> None:
        self._events.append(SinkEvent("intrabar", symbol, bar))
