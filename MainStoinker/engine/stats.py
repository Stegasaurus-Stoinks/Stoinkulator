"""The one definition of the results summary of a run.

Pure functions over `Decimal`s and round-trip objects: no pandas, no I/O, no
imports from the rest of the engine. `ledger.json`, the paper goldens in
`tests/golden_paper/` and (later) the dashboard all build their numbers here,
so a number means the same thing everywhere.

Money, prices and quantities are strings with exactly 4 decimals (never
floats). A round trip is any object with the attributes of
`ledger.RoundTrip` (`side`, `quantity`, `entry_time`, `exit_time`,
`entry_price`, `exit_price`, `gross`, `commission`, `net`, `exit_reason`,
`synthetic`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

_CENT4 = Decimal("0.0001")
_ZERO4 = Decimal("0.0000")


def fmt4(value: Decimal) -> str:
    """A `Decimal` as a plain fixed-point string with 4 decimals."""
    return format(value.quantize(_CENT4), "f")


def round_trip_record(trip: Any) -> dict[str, Any]:
    """One round trip as plain data: 4-decimal strings, times as `str`, `synthetic` a bool."""
    return {
        "side": trip.side,
        "quantity": fmt4(trip.quantity),
        "entry_time": str(trip.entry_time),
        "exit_time": str(trip.exit_time),
        "entry_price": fmt4(trip.entry_price),
        "exit_price": fmt4(trip.exit_price),
        "gross": fmt4(trip.gross),
        "commission": fmt4(trip.commission),
        "net": fmt4(trip.net),
        "exit_reason": trip.exit_reason,
        "synthetic": bool(trip.synthetic),
    }


def max_drawdown(equities: Iterable[Decimal], capital: Decimal) -> Decimal:
    """Largest peak-to-trough fall of an equity series; zero or positive.

    The starting `capital` counts as the first peak, so a series that only
    ever falls below it measures the fall from the start.
    """
    peak = capital
    worst = _ZERO4
    for value in equities:
        if value > peak:
            peak = value
        worst = max(worst, peak - value)
    return worst


def account_summary(
    round_trips: Iterable[Any],
    equity: Decimal,
    capital: Decimal,
    *,
    equity_series: Iterable[Decimal] = (),
    ignored_events: Mapping[str, int] | None = None,
    cancelled_entries: int = 0,
) -> dict[str, Any]:
    """Summary of one account (or of all of them together).

    `equity` is the final equity, `capital` the starting capital (the first
    peak of the drawdown) and `equity_series` the equity after each bar.
    Keys: `trade_count`, `wins` (net above zero), `commission`, `net`,
    `final_equity`, `max_drawdown`, `ignored_events` (kind to count, as
    given), `cancelled_entries`, `synthetic_closes`.
    """
    trips = list(round_trips)
    return {
        "trade_count": len(trips),
        "wins": sum(1 for t in trips if t.net > 0),
        "commission": fmt4(sum((t.commission for t in trips), _ZERO4)),
        "net": fmt4(sum((t.net for t in trips), _ZERO4)),
        "final_equity": fmt4(equity),
        "max_drawdown": fmt4(max_drawdown(equity_series, capital)),
        "ignored_events": dict(ignored_events or {}),
        "cancelled_entries": cancelled_entries,
        "synthetic_closes": sum(1 for t in trips if t.synthetic),
    }
