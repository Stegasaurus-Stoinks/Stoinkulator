"""Turning a finished backtest into plain, comparable data.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from typing import Any


def trade_records(algo: Any) -> list[dict[str, Any]]:
    """Plain, comparable record of each trade an algo made.

    One dict per entry of `algo.trades`, in order, with the keys `id`,
    `symbol`, `volume`, `direction` (int), `openTime` (str), `openPrice`
    (rounded to 4 places), `closeTime` (str; "None" while open), `closePrice`
    (rounded, or None while open) and `status` (None when the trade has none).
    This is the exact shape stored in `tests/golden/*.json`.
    """
    records = []
    for trade in algo.trades:
        records.append(
            {
                "id": trade.tradeID,
                "symbol": trade.symbol,
                "volume": trade.volume,
                "direction": int(trade.direction),
                "openTime": str(trade.openTime),
                "openPrice": round(float(trade.openPrice), 4),
                "closeTime": str(getattr(trade, "closeTime", None)),
                "closePrice": (
                    round(float(trade.closePrice), 4) if hasattr(trade, "closePrice") else None
                ),
                "status": getattr(trade, "status", None),
            }
        )
    return records
