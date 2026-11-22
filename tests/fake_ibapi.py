"""Recording fake of the module-level ``IBapi`` instance, for order tests.

Subclasses ``IBapi`` and overrides only what would talk to the broker. The real
``addStoploss`` and ``addTP`` are kept so the orders they build are exercised.
Every call is appended, in order, to ``self.calls``.

Contracts and orders are recorded as the attributes that differ from a freshly
constructed ``Contract()`` / ``Order()``, copied at call time (``Trade`` mutates
and re-sends the same objects).
"""
from __future__ import annotations

import copy
from typing import Any

import pandas as pd
from ibapi.contract import Contract
from ibapi.order import Order

from MainStoinker.DataCollection.apiApi import IBapi

FIRST_ORDER_ID = 100
_SCALARS = (int, float, str, bool, type(None))


def _same_object(a: Any, b: Any) -> bool:
    if hasattr(a, "__dict__") and hasattr(b, "__dict__"):
        return type(a) is type(b) and vars(a) == vars(b)
    return bool(repr(a) == repr(b))


def non_default_fields(obj: Any, fresh: Any) -> dict[str, Any]:
    """Attributes of ``obj`` that differ from ``fresh`` or do not exist on it."""
    fresh_vars = vars(fresh)
    out: dict[str, Any] = {}
    for key, value in vars(obj).items():
        if key in fresh_vars:
            default = fresh_vars[key]
            if isinstance(value, _SCALARS):
                if value == default and type(value) is type(default):
                    continue
            elif _same_object(value, default):
                continue
        out[key] = value if isinstance(value, _SCALARS) else copy.deepcopy(value)
    return out


class RecordingIBapi(IBapi):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, ...]] = []
        self._calls_made = 0
        self._timeouts_left = 0
        self._timeouts_skip = 0
        self._return_offset: int | None = None
        self._raise_on_stp: str | None = None  # "before" or "after" recording
        self.nextValidOrderId = FIRST_ORDER_ID - 1

    # -- knobs -----------------------------------------------------------------
    def timeout_next(self, n: int = 1, after: int = 0) -> None:
        """After ``after`` normal calls, the next ``n`` getNextOrderID calls are
        recorded, return None and leave ``nextValidOrderId`` untouched (like
        the real timeout path)."""
        self._timeouts_left = n
        self._timeouts_skip = after

    def decouple_return_value(self, offset: int = 400) -> None:
        """getNextOrderID returns ``offset + k`` for its k-th call (k from 0)
        while ``nextValidOrderId`` advances 100, 101, ... independently."""
        self._return_offset = offset

    def raise_on_next_stp(self, when: str) -> None:
        """The next STP ``placeOrder`` raises, before or after it is recorded."""
        assert when in ("before", "after")
        self._raise_on_stp = when

    # -- broker-facing methods, replaced ---------------------------------------
    def getNextOrderID(self) -> int | None:
        self.calls.append(("getNextOrderID",))
        if self._timeouts_skip > 0:
            self._timeouts_skip -= 1
        elif self._timeouts_left > 0:
            self._timeouts_left -= 1
            return None
        k = self._calls_made
        self._calls_made += 1
        self.nextValidOrderId = FIRST_ORDER_ID + k
        if self._return_offset is not None:
            return self._return_offset + k
        return self.nextValidOrderId

    def placeOrder(self, orderId: int | None, contract: Contract, order: Order) -> None:
        raising = self._raise_on_stp if order.orderType == "STP" else None
        if raising == "before":
            self._raise_on_stp = None
            raise RuntimeError("fake placeOrder failure before reaching the broker")
        self.calls.append(
            (
                "placeOrder",
                orderId,
                non_default_fields(contract, Contract()),
                non_default_fields(order, Order()),
            )
        )
        if raising == "after":
            self._raise_on_stp = None
            raise RuntimeError("fake placeOrder failure after reaching the broker")

    def cancelOrder(self, orderId: int, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("cancelOrder", orderId))

    def reqIds(self, numIds: int) -> None:
        self.calls.append(("reqIds", numIds))

    def reqAllOpenOrders(self) -> None:
        self.calls.append(("reqAllOpenOrders",))

    def readOrders(self) -> None:
        self.calls.append(("readOrders",))

    # -- test controls ----------------------------------------------------------
    def set_open_order_state(self, order_id: int, state: str) -> None:
        """List ``order_id`` in all_openorders with the given OrderState."""
        self.all_openorders = pd.DataFrame(
            {"OrderState": [state]}, index=pd.Index([order_id])
        )

    def clear_open_orders(self) -> None:
        self.all_openorders = pd.DataFrame(
            {"OrderState": []}, index=pd.Index([], dtype=int)
        )
