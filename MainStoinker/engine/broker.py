"""Broker interface and a record-only simulated broker.

The engine talks to a broker only through the `Broker` protocol. Order ids
never appear in this interface: `open_position` returns an opaque reference
that the caller hands back on later calls. A real-broker implementation is a
later phase; today only `SimBroker` exists, and it records calls without
modelling fills.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd


@runtime_checkable
class Broker(Protocol):
    """What the engine needs from a broker."""

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
        """Open the entry together with its initial protective stop.

        The entry is a market order, or a limit order at `price` when `limit`
        is true. The protective stop is placed at `stop_price`. `direction` is
        truthy for long and falsy for short (the legacy code uses 1 / 0).
        Returns an opaque reference the caller passes back on later calls.
        """
        ...

    def modify_stop(self, ref: Hashable, stop_price: float) -> None:
        """Move the protective stop of the position `ref` to `stop_price`."""
        ...

    def set_take_profit(self, ref: Hashable, price: float, quantity: float) -> None:
        """Place the take-profit on first call for `ref`; modify it on later calls."""
        ...

    def close_position(self, ref: Hashable, *, price: float, time: pd.Timestamp) -> None:
        """Close the whole position `ref`.

        `price` and `time` are the caller's view of the close. A simulated
        broker fills there; a real broker may ignore them.
        """
        ...

    def stop_is_working(self, ref: Hashable) -> bool:
        """Whether the protective stop is still live at the broker.

        False once it has filled or disappeared.
        """
        ...


@dataclass(frozen=True)
class BrokerCall:
    """One recorded broker call.

    `method` is the Broker method name, `ref` the reference it applied to (for
    `open_position`, the ref it returned), and `args` the remaining arguments
    by keyword name, as a read-only mapping.
    """

    method: str
    ref: Hashable | None
    args: Mapping[str, object]


class SimBroker:
    """Record-only broker for backtests.

    Keeps a call log and a ref counter, nothing else: no fills, positions or
    prices. Never raises for unknown or already-closed refs, because existing
    algos close twice and modify stops after a close.
    """

    def __init__(self) -> None:
        self._calls: list[BrokerCall] = []
        self._next_ref = 1

    @property
    def calls(self) -> tuple[BrokerCall, ...]:
        """Snapshot of the recorded calls, in order. Mutating it does not affect the log."""
        return tuple(self._calls)

    def _record(self, method: str, ref: Hashable | None, **args: object) -> None:
        self._calls.append(BrokerCall(method, ref, MappingProxyType(dict(args))))

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
        ref = self._next_ref
        self._next_ref += 1
        self._record(
            "open_position",
            ref,
            symbol=symbol,
            volume=volume,
            direction=direction,
            price=price,
            time=time,
            limit=limit,
            stop_price=stop_price,
            oca_group=oca_group,
        )
        return ref

    def modify_stop(self, ref: Hashable, stop_price: float) -> None:
        self._record("modify_stop", ref, stop_price=stop_price)

    def set_take_profit(self, ref: Hashable, price: float, quantity: float) -> None:
        self._record("set_take_profit", ref, price=price, quantity=quantity)

    def close_position(self, ref: Hashable, *, price: float, time: pd.Timestamp) -> None:
        self._record("close_position", ref, price=price, time=time)

    def stop_is_working(self, ref: Hashable) -> bool:
        return True
