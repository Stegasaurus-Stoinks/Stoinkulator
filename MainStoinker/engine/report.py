"""Turning a finished backtest into plain, comparable data.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from .gaprule import RefusedEntry
from .ledger import IGNORED_KINDS, Ledger
from .paper_broker import CostModel
from .stats import account_summary, fmt4, round_trip_record


def ledger_document(
    ledger: Ledger,
    idnames: Sequence[str],
    *,
    fills: str,
    costs: CostModel,
    capital: Decimal,
    cancelled_entries: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The `ledger.json` content: fill model, costs, capital, total and per-algo results.

    Every number comes from `stats`. Per algo: `symbol`, `summary` (the shape
    of `stats.account_summary`) and `round_trips` (`stats.round_trip_record`,
    in the order the ledger closed them). The top-level `summary` is the same
    shape over all accounts, with the total equity series and the summed
    starting capital. `cancelled_entries` maps idname to the broker's count
    (missing means 0). Algos appear in `idnames` order.
    """
    cancelled = cancelled_entries or {}
    accounts: dict[str, Any] = {}
    total_capital = Decimal("0.0000")
    for idname in idnames:
        info = next((a for a in ledger.accounts if a.idname == idname), None)
        if info is None:
            raise ValueError(f"unknown idname {idname!r}: the ledger has no such account")
        total_capital += info.capital
        trips = [t for t in ledger.round_trips if t.account == idname]
        accounts[idname] = {
            "symbol": info.symbol,
            "summary": account_summary(
                trips,
                ledger.equity(idname),
                info.capital,
                equity_series=[p.equity for p in ledger.equity_points if p.account == idname],
                ignored_events={k: ledger.ignored_count(k, idname) for k in IGNORED_KINDS},
                cancelled_entries=cancelled.get(idname, 0),
            ),
            "round_trips": [round_trip_record(t) for t in trips],
        }
    return {
        "fills": fills,
        "costs": {
            "commission_per_share": fmt4(costs.commission_per_share),
            "commission_minimum": fmt4(costs.commission_minimum),
            "slippage_per_share": fmt4(costs.slippage_per_share),
        },
        "capital": fmt4(capital),
        "summary": account_summary(
            ledger.round_trips,
            ledger.equity(),
            total_capital,
            equity_series=[p.equity for p in ledger.equity_points if p.account is None],
            ignored_events={k: ledger.ignored_count(k) for k in IGNORED_KINDS},
            cancelled_entries=sum(cancelled.get(i, 0) for i in idnames),
        ),
        "accounts": accounts,
    }


def trade_records(algo: Any) -> list[dict[str, Any]]:
    """Plain, comparable record of each trade an algo made.

    One dict per entry of `algo.trades`, in order, with the keys `id`,
    `symbol`, `volume`, `direction` (int), `openTime` (str), `openPrice`
    (rounded to 4 places), `closeTime` (str; "None" while open), `closePrice`
    (rounded, or None while open) and `status` (None when the trade has none).
    This is the exact shape stored in `tests/golden/*.json`. An entry the gap
    rule refused (its `brokerRef` is a `RefusedEntry`) never became a position
    and is left out; `GapRuleReport` has it.
    """
    records = []
    for trade in algo.trades:
        if isinstance(getattr(trade, "brokerRef", None), RefusedEntry):
            continue
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
