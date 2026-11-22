"""Turning a finished backtest into plain, comparable data.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from typing import Any

from .ledger import Ledger


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


def ledger_trade_records(ledger: Ledger, account: str | None = None) -> list[dict[str, Any]]:
    """The golden-shaped trade list (without `status`) projected from a ledger.

    One dict per entry fill, in entry order, optionally only for `account`. Keys
    are `id` (account name plus that account's entry index, as `ParentAlgo.open_trade`
    builds it), `symbol`, `volume` (int), `direction` (1 long, 0 short), `openTime`
    (str), `openPrice` (float, 4 places), `closeTime` (str) and `closePrice`.
    A trade is paired by broker ref with its non-synthetic exit fill. One with
    none (still open, or closed only by the ledger's synthetic end-of-data
    fill) shows `closeTime` "None" and `closePrice` None, like the legacy view.
    """
    exits: dict[tuple[str, object], Any] = {}
    for fill in ledger.fills:
        if fill.reason != "entry" and not fill.synthetic:
            exits.setdefault((fill.account, fill.broker_ref), fill)
    counts: dict[str, int] = {}
    records = []
    for fill in ledger.fills:
        if fill.reason != "entry" or (account is not None and fill.account != account):
            continue
        index = counts.get(fill.account, 0)
        counts[fill.account] = index + 1
        out = exits.get((fill.account, fill.broker_ref))
        records.append(
            {
                "id": f"{fill.account}{index}",
                "symbol": fill.symbol,
                "volume": int(fill.quantity),
                "direction": 1 if fill.side == "buy" else 0,
                "openTime": str(fill.time),
                "openPrice": float(fill.price),
                "closeTime": "None" if out is None else str(out.time),
                "closePrice": None if out is None else float(out.price),
            }
        )
    return records
