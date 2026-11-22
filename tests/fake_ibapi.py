"""Recording fake of the module-level ``IBapi`` instance, for order tests.

Subclasses ``IBapi`` and overrides only what would talk to the broker. The real
``addStoploss`` and ``addTP`` are kept so the orders they build are exercised.
Every call is appended, in order, to ``self.calls``.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from MainStoinker.DataCollection.apiApi import IBapi

CONTRACT_FIELDS = ("symbol", "secType", "exchange", "currency", "primaryExchange")
ORDER_FIELDS = (
    "orderId",
    "action",
    "orderType",
    "totalQuantity",
    "lmtPrice",
    "auxPrice",
    "tif",
    "goodTillDate",
    "ocaGroup",
    "ocaType",
    "transmit",
    "parentId",
)
FIRST_ORDER_ID = 100


class RecordingIBapi(IBapi):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, ...]] = []
        self._highest_id_handed_out = FIRST_ORDER_ID - 1
        self.nextValidOrderId = None

    # -- broker-facing methods, replaced -------------------------------------
    def getNextOrderID(self) -> int:
        self.calls.append(("getNextOrderID",))
        self._highest_id_handed_out += 1
        self.nextValidOrderId = self._highest_id_handed_out
        return self.nextValidOrderId

    def placeOrder(self, orderId, contract, order) -> None:
        # Snapshot values now: Trade mutates and re-sends the same Order object.
        self.calls.append(
            (
                "placeOrder",
                orderId,
                {k: getattr(contract, k) for k in CONTRACT_FIELDS},
                {k: getattr(order, k) for k in ORDER_FIELDS},
            )
        )

    def cancelOrder(self, orderId, *args, **kwargs) -> None:
        self.calls.append(("cancelOrder", orderId))

    def reqIds(self, numIds) -> None:
        self.calls.append(("reqIds", numIds))

    def reqAllOpenOrders(self) -> None:
        self.calls.append(("reqAllOpenOrders",))

    def readOrders(self) -> None:
        self.calls.append(("readOrders",))

    # -- test controls --------------------------------------------------------
    def set_open_order_state(self, order_id: int, state: str) -> None:
        """List ``order_id`` in all_openorders with the given OrderState."""
        self.all_openorders = pd.DataFrame(
            {"OrderState": [state]}, index=pd.Index([order_id])
        )

    def clear_open_orders(self) -> None:
        self.all_openorders = pd.DataFrame({"OrderState": []}, index=pd.Index([], dtype=int))

    def placed(self) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == "placeOrder"]
