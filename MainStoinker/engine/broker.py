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

from .ledger import AccountRecorder, FillReason, Side

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


class _Tracked:
    """What a SimBroker with an account remembers about one ref."""

    __slots__ = ("symbol", "volume", "long", "stop", "target", "last_time", "closed")

    def __init__(self, symbol: str, volume: float, long: bool, stop: float,
                 time: pd.Timestamp) -> None:
        self.symbol = symbol
        self.volume = volume
        self.long = long
        self.stop = stop
        self.target: float | None = None
        self.last_time = time
        self.closed = False


class SimBroker:
    """Simulated broker for backtests.

    Always keeps a call log and a ref counter. Never raises for unknown or
    already-closed refs, because existing algos close twice and modify stops
    after a close.

    With `account=` (an `AccountRecorder` from the ledger) it also reports to
    the ledger, still modelling nothing itself: it fills where the caller says.
    `open_position` records an entry order and an entry fill at the given price
    and time (long is a buy, short a sell; the order type is "limit" when
    `limit`, else "market"). The first `close_position` on a ref records the
    exit fill at the price and time passed, whole position, with reason "stop"
    if the price equals the last stop it was told, else "target" if it equals
    the take-profit, else "algo_exit". Later closes, and `modify_stop` or
    `set_take_profit` after the close, are recorded as ignored requests.
    Commission is always 0. Without an account the behaviour is exactly the
    record-only behaviour.

    `modify_stop` and `set_take_profit` carry no time, so their ignored events
    use the last time the broker saw for that ref (its entry or close time). A
    ref this broker never opened has no time of its own: it falls back to the
    latest time any call gave, and is not recorded at all if there is none.
    """

    def __init__(self, *, account: AccountRecorder | None = None) -> None:
        self._calls: list[BrokerCall] = []
        self._next_ref = 1
        self._account = account
        self._tracked: dict[Hashable, _Tracked] = {}
        self._latest_time: pd.Timestamp | None = None

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
        account = self._account
        if account is not None:
            long = bool(direction)
            side: Side = "buy" if long else "sell"
            order_id = account.order_placed(
                ref, time=time, side=side, quantity=volume,
                order_type="limit" if limit else "market", price=price)
            account.fill(ref, side=side, quantity=volume, price=price, commission=0,
                         time=time, reason="entry", order_id=order_id)
            self._tracked[ref] = _Tracked(symbol, volume, long, stop_price, time)
            self._latest_time = time
        return ref

    def _time_for(self, tracked: _Tracked | None) -> pd.Timestamp | None:
        return tracked.last_time if tracked is not None else self._latest_time

    def modify_stop(self, ref: Hashable, stop_price: float) -> None:
        self._record("modify_stop", ref, stop_price=stop_price)
        if self._account is None:
            return
        tracked = self._tracked.get(ref)
        if tracked is not None and not tracked.closed:
            tracked.stop = stop_price
            return
        time = self._time_for(tracked)
        if time is not None:
            self._account.ignored(ref, time=time, kind="modify_stop")

    def set_take_profit(self, ref: Hashable, price: float, quantity: float) -> None:
        self._record("set_take_profit", ref, price=price, quantity=quantity)
        if self._account is None:
            return
        tracked = self._tracked.get(ref)
        if tracked is not None and not tracked.closed:
            tracked.target = price
            return
        time = self._time_for(tracked)
        if time is not None:
            self._account.ignored(ref, time=time, kind="take_profit")

    def close_position(self, ref: Hashable, *, price: float, time: pd.Timestamp) -> None:
        self._record("close_position", ref, price=price, time=time)
        if self._account is None:
            return
        self._latest_time = time
        tracked = self._tracked.get(ref)
        if tracked is None or tracked.closed:
            if tracked is not None:
                tracked.last_time = time
            self._account.ignored(ref, time=time, kind="close")
            return
        if price == tracked.stop:
            reason: FillReason = "stop"
        elif tracked.target is not None and price == tracked.target:
            reason = "target"
        else:
            reason = "algo_exit"
        self._account.fill(ref, side="sell" if tracked.long else "buy", quantity=tracked.volume,
                           price=price, commission=0, time=time, reason=reason)
        tracked.closed = True
        tracked.last_time = time

    def stop_is_working(self, ref: Hashable) -> bool:
        return True
