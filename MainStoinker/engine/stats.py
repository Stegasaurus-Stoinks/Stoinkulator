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


# --- run statistics (what `report` shows) -----------------------------------------
#
# Computed from stored data only (a `store.RunData`, duck-typed). The numbers
# shared with `account_summary` are the same by construction and are pinned
# against `ledger.json` and the paper goldens; the extra ones live here so those
# files do not change.
#
# Definitions:
# - a win is a round trip with net > 0, a loss one with net < 0; net == 0 is
#   neither (`break_even`). `trade_count == wins + losses + break_even`.
# - `win_rate` = wins / trade_count as a fraction (None without trades).
# - `gross_profit` is the sum of the net of the winning trips, `gross_loss` the
#   sum of the net of the losing trips (zero or negative); both are after
#   commission. `net` = gross_profit + gross_loss + break-even nets (zero).
# - `avg_win` = gross_profit / wins, `avg_loss` = gross_loss / losses (negative);
#   None when there are no wins / losses. Rounded to 4 places.
# - synthetic end-of-data closes are real round trips: counted in every total
#   and also reported separately as `synthetic_closes`.
# - `max_drawdown`: see `max_drawdown` (starting capital is the first peak).
# - `final_equity` is the last stored point of the series, the capital if the
#   series is empty.
# - `cancelled_entries`: distinct order ids (per algo) with a `placed` event of
#   `order_type == "entry"` and a `cancelled` event. Legacy fills have none.
# - the account total uses the summed capital, the stored account-total series
#   (`account` None) and every round trip, like `report.ledger_document`.


def cancelled_entry_counts(order_events: Iterable[Any]) -> dict[str, int]:
    """Cancelled entry orders per algo, derived from the order event log."""
    entries: set[tuple[str, int]] = set()
    cancelled: set[tuple[str, int]] = set()
    for event in order_events:
        key = (event.account, event.order_id)
        if event.kind == "placed" and event.order_type == "entry":
            entries.add(key)
        elif event.kind == "cancelled":
            cancelled.add(key)
    counts: dict[str, int] = {}
    for account, _order_id in entries & cancelled:
        counts[account] = counts.get(account, 0) + 1
    return counts


def trade_statistics(
    round_trips: Iterable[Any],
    capital: Decimal,
    *,
    equity_series: Iterable[Decimal] = (),
    ignored_events: Mapping[str, int] | None = None,
    cancelled_entries: int = 0,
) -> dict[str, Any]:
    """Statistics of one account (or of all together); money as `Decimal`, see above.

    Keys: `trade_count`, `wins`, `losses`, `break_even`, `win_rate`, `net`,
    `gross_profit`, `gross_loss`, `commission`, `avg_win`, `avg_loss`,
    `max_drawdown`, `final_equity`, `synthetic_closes`, `ignored_events`,
    `cancelled_entries`, `capital`.
    """
    trips = list(round_trips)
    series = list(equity_series)
    winners = [t.net for t in trips if t.net > 0]
    losers = [t.net for t in trips if t.net < 0]
    gross_profit = sum(winners, _ZERO4)
    gross_loss = sum(losers, _ZERO4)
    return {
        "trade_count": len(trips),
        "wins": len(winners),
        "losses": len(losers),
        "break_even": len(trips) - len(winners) - len(losers),
        "win_rate": Decimal(len(winners)) / len(trips) if trips else None,
        "net": sum((t.net for t in trips), _ZERO4),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "commission": sum((t.commission for t in trips), _ZERO4),
        "avg_win": gross_profit / len(winners) if winners else None,
        "avg_loss": gross_loss / len(losers) if losers else None,
        "max_drawdown": max_drawdown(series, capital),
        "final_equity": series[-1] if series else capital,
        "synthetic_closes": sum(1 for t in trips if t.synthetic),
        "ignored_events": dict(ignored_events or {}),
        "cancelled_entries": cancelled_entries,
        "capital": capital,
    }


def run_statistics(run: Any, ignored_kinds: Iterable[str] = ()) -> dict[str, Any]:
    """Statistics of a stored run (`store.RunData`): `{"accounts": {idname: stats}, "total": stats}`.

    `accounts` follows the order of the run's algos. `ignored_kinds` are the
    kinds listed (with a zero) for every account, so the result has the same
    shape for every run; kinds that occur are always counted.
    """
    kinds = list(ignored_kinds)
    cancelled = cancelled_entry_counts(run.order_events)

    def ignored(account: str | None) -> dict[str, int]:
        counts = {k: 0 for k in kinds}
        for event in run.ignored_events:
            if account is None or event.account == account:
                counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts

    accounts: dict[str, Any] = {}
    for algo in run.algos:
        points = run.equity_series(algo.idname)
        accounts[algo.idname] = trade_statistics(
            [t for t in run.round_trips if t.account == algo.idname],
            algo.capital,
            equity_series=[p.equity for p in points],
            ignored_events=ignored(algo.idname),
            cancelled_entries=cancelled.get(algo.idname, 0),
        )
    total_capital = sum((a.capital for a in run.algos), _ZERO4)
    names = {a.idname for a in run.algos}
    total = trade_statistics(
        [t for t in run.round_trips if t.account in names],
        total_capital,
        equity_series=[p.equity for p in run.equity_series(None)],
        ignored_events=ignored(None),
        cancelled_entries=sum(cancelled.get(n, 0) for n in names),
    )
    return {"accounts": accounts, "total": total}


def statistics_record(stats: Mapping[str, Any]) -> dict[str, Any]:
    """`trade_statistics` as plain JSON data: money and `win_rate` as 4-decimal strings.

    `win_rate` is the fraction (0.2286 for 22.86 %); None stays None.
    """
    money = ("net", "gross_profit", "gross_loss", "commission", "max_drawdown", "final_equity",
             "capital")
    out: dict[str, Any] = {}
    for key, value in stats.items():
        if key in money or key in ("win_rate", "avg_win", "avg_loss"):
            out[key] = None if value is None else fmt4(value)
        elif key == "ignored_events":
            out[key] = dict(value)
        else:
            out[key] = value
    return out
