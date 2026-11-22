"""Routing an algo to its broker, and the optional bar hooks of a broker.

`Broker` stays the minimal order interface. A broker that wants to see every
bar (to fill pending orders) or the end of the run (to close what is left)
additionally satisfies `BarAwareBroker`; that is a separate protocol because a
runtime-checkable `Protocol` cannot express an optional method.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from .broker import Broker
from .feed import Bar


@runtime_checkable
class BrokerRouter(Protocol):
    """Maps an algo to the broker that handles its orders."""

    def broker_for(self, algo: object) -> Broker:
        """The broker for `algo`. Called once per algo, at Engine construction."""
        ...


@runtime_checkable
class BarAwareBroker(Protocol):
    """A broker that wants the engine's bar and end-of-run notifications."""

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """A new closed bar for `symbol`, before any algo sees it."""
        ...

    def on_end(self, last_bars: Mapping[str, Bar]) -> None:
        """The feed is exhausted; `last_bars` holds the final bar of each symbol."""
        ...


class SingleBrokerRouter:
    """Every algo gets the same broker (the engine's behaviour before routers)."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def broker_for(self, algo: object) -> Broker:
        return self._broker


class MappingRouter:
    """One broker per algo, keyed by `algo.name` (the algo's unique idname).

    An algo whose name is not in the mapping raises `KeyError` naming it. The
    mapping is copied, so later changes to the caller's dict have no effect.
    """

    def __init__(self, brokers: Mapping[str, Broker]) -> None:
        self._brokers: dict[str, Broker] = dict(brokers)

    def broker_for(self, algo: object) -> Broker:
        name = algo.name  # type: ignore[attr-defined]
        try:
            return self._brokers[name]
        except KeyError:
            raise KeyError(f"no broker routed for algo {name!r}") from None
