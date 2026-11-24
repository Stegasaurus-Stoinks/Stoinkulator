"""The JSON documents of a stored run, as plain dicts.

    run_summary_record(summary, run)             one entry of `runs --json`
    report_document(run, *, data_directory)      the `report --json` document

Pure: no I/O, no pandas, no clock, no current directory. Both the `runs` and
`report` commands and the API call these, so they return exactly the same
structures. The caller decides how the stored data directory is shown and passes
it in (the CLI: relative to the current directory if inside it, else the last
component; the API: the last component only). The stored absolute directory is
never put in a document.

Field names and values are those described in `cli_report.py`: money as
4-decimal strings, run times in UTC ISO-8601, data times as stored.

Live runs (issue 193) get extra keys, all pure additions computed from stored
data with "now" passed in (there is no clock here). A run is live when
`is_live_run` says so (kind in `LIVE_KINDS`, or a live writer ever set its
`last_write_at` or `end_reason`); every other run's documents are exactly what
they were before, with no new key:

- `runs --json` entry and `report --json` `run`: `liveness` (`running`, `stale`,
  `completed` or `failed`; only when `now` is given), `end_reason`,
  `last_write_at` (UTC ISO or null);
- `report --json` top level: `open_positions` (always for a live run) and
  `live_counts` (when the counts were passed in).

Stale: a `running` run whose last write (`last_write_at`, else `started_at`)
is more than `stale_after` (default `STALE_AFTER`) before `now`.

Open positions are derived from the stored fills and order events alone, by
the ledger's own rules (average cost method), and equal what
`Engine.end_live` reports. The live counts live in the store's `run_counts`
table under scope "symbol" with keys `SYMBOL:fed`, `SYMBOL:late` and
`SYMBOL:dropped:REASON` (see `live_counts_record`).
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .gaprule import GapRuleReport
from .ledger import IGNORED_KINDS, money
from .live_counts import (
    COUNT_FED,
    COUNT_LATE,
    LIVE_KINDS,
    STALE_AFTER,
    parse_count_key,
)
from .stats import fmt4, run_statistics, statistics_record
from .store import RunData, RunSummary, StoredBar, gap_rule_to_json

# STALE_AFTER, LIVE_KINDS and the count key format live in `live_counts`, which
# the live writer imports too; `STALE_AFTER` is re-exported from here.


def run_net(run: RunData) -> Decimal:
    """Net of every round trip of the run."""
    return sum((t.net for t in run.round_trips), Decimal("0.0000"))


def is_live_run(summary: RunSummary) -> bool:
    """Whether the run was written by the live writers (kind, or a live-only column set)."""
    return (summary.kind in LIVE_KINDS or summary.last_write_at is not None
            or summary.end_reason is not None)


def _utc_iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def liveness(summary: RunSummary, now: datetime, stale_after: timedelta = STALE_AFTER) -> str:
    """`running`, `stale`, `completed` or `failed`; `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if summary.status != "running":
        return summary.status
    last = summary.last_write_at if summary.last_write_at is not None else summary.started_at
    return "stale" if now - last > stale_after else "running"


def live_summary_fields(summary: RunSummary, now: datetime | None,
                        stale_after: timedelta = STALE_AFTER) -> dict[str, Any]:
    """The liveness keys of a live run's document; empty for any other run."""
    if not is_live_run(summary):
        return {}
    out: dict[str, Any] = {}
    if now is not None:
        out["liveness"] = liveness(summary, now, stale_after)
    out["end_reason"] = summary.end_reason
    out["last_write_at"] = _utc_iso(summary.last_write_at)
    return out


@dataclass
class _Pos:
    long: bool
    symbol: str
    qty: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    pending: Decimal = Decimal(0)
    entry_time: datetime | None = None  # of the current position's first entry fill


def open_positions(run: RunData, last_bars: Mapping[str, StoredBar] | None = None
                   ) -> list[dict[str, Any]]:
    """The positions still open after the stored fills, in the engine's order.

    Replays the fills with the ledger's rules (average cost; exits release the
    pending entry commission). `entry_time` is the time of the first entry fill of
    the current position (a reused broker reference starts afresh once flat).
    `stop` and `target` are the last price of the `stop` / `target` order placed
    for that position, after modifications, null when none was recorded or it
    was cancelled. When `last_bars` has the
    position's symbol, `last_close` and `unrealised` (the ledger's: marked
    against the remaining cost, less the commission still pending) are added.
    A run closed by synthetic end-of-data fills has none.
    """
    book: dict[str, dict[Hashable, _Pos]] = {a.idname: {} for a in run.algos}
    for fill in run.fills:
        held = book.setdefault(fill.account, {})
        pos = held.get(fill.broker_ref)
        if fill.reason == "entry":
            if pos is None:
                pos = held[fill.broker_ref] = _Pos(fill.side == "buy", fill.symbol,
                                                   entry_time=fill.time)
            pos.qty += fill.quantity
            pos.cost += fill.quantity * fill.price
            pos.pending += fill.commission
        elif pos is not None:
            full = fill.quantity >= pos.qty
            removed = pos.cost if full else money(pos.cost * fill.quantity / pos.qty)
            pos.pending += fill.commission
            release = pos.pending if full else money(pos.pending * fill.quantity / pos.qty)
            pos.qty -= fill.quantity
            pos.cost -= removed
            pos.pending -= release
            if pos.qty <= 0:
                del held[fill.broker_ref]
    levels: dict[tuple[str, Hashable], dict[str, Decimal | None]] = {}
    placed: dict[int, tuple[tuple[str, Hashable], str]] = {}
    for event in run.order_events:
        if event.kind == "placed" and event.order_type in ("stop", "target"):
            key = (event.account, event.broker_ref)
            current = book.get(event.account, {}).get(event.broker_ref)
            # an order placed before the current position's first entry belongs to an
            # earlier position of a reused reference
            if current is None or current.entry_time is None or event.time < current.entry_time:
                continue
            placed[event.order_id] = (key, event.order_type)
            levels.setdefault(key, {})[event.order_type] = event.price
        elif event.order_id in placed:
            key, kind = placed[event.order_id]
            if event.kind == "modified" and event.price is not None:
                levels[key][kind] = event.price
            elif event.kind == "cancelled":
                levels[key][kind] = None
    out: list[dict[str, Any]] = []
    for account, held in book.items():
        for ref, pos in held.items():
            key = (account, ref)
            lv = levels.get(key, {})
            average = money(pos.cost / pos.qty)
            entry = pos.entry_time
            record: dict[str, Any] = {
                "account": account, "symbol": pos.symbol,
                "side": "long" if pos.long else "short",
                "quantity": fmt4(pos.qty), "entry_price": fmt4(average),
                "entry_time": None if entry is None else entry.isoformat(),
                "stop": None if lv.get("stop") is None else fmt4(lv["stop"]),
                "target": None if lv.get("target") is None else fmt4(lv["target"]),
            }
            bar = None if last_bars is None else last_bars.get(pos.symbol)
            if bar is not None:
                close = money(bar.close)
                value = close * pos.qty
                gross = money(value - pos.cost if pos.long else pos.cost - value)
                record["last_close"] = fmt4(close)
                record["unrealised"] = fmt4(money(gross - pos.pending))
            out.append(record)
    return out


def live_counts_record(counts: Mapping[str, Mapping[str, int]],
                       gap_rule: GapRuleReport | None) -> dict[str, Any]:
    """The `live_counts` document: minutes fed, dropped per reason, late (not fed), gap rule.

    `counts` is the store's `load_counts`. Keys of scope "symbol" that do not
    follow the `SYMBOL:fed|late|dropped:REASON` scheme, and every other scope,
    are kept as they are under `other` (only present when there are any).
    """
    fed: dict[str, int] = {}
    late: dict[str, int] = {}
    dropped: dict[str, dict[str, int]] = {}
    other: dict[str, dict[str, int]] = {}
    for scope, keyed in counts.items():
        for key, value in keyed.items():
            parsed = parse_count_key(scope, key)
            if parsed is None:
                other.setdefault(scope, {})[key] = value
            elif parsed.kind == COUNT_FED:
                fed[parsed.symbol] = value
            elif parsed.kind == COUNT_LATE:
                late[parsed.symbol] = value
            elif parsed.reason is not None:
                dropped.setdefault(parsed.symbol, {})[parsed.reason] = value
            else:
                other.setdefault(scope, {})[key] = value
    record: dict[str, Any] = {
        "fed": fed, "dropped": dropped, "late": late,
        "gap_rule": None if gap_rule is None else {
            "bars_after_gap": {s: n for s, n in gap_rule.bars_after_gap},
            "suppressed_entries": gap_rule.total_suppressed},
    }
    if other:
        record["other"] = other
    return record


def run_summary_record(summary: RunSummary, run: RunData, *, now: datetime | None = None,
                       stale_after: timedelta = STALE_AFTER) -> dict[str, Any]:
    """One entry of `runs --json`; `run` is the loaded run of `summary`.

    A live run (see the module text) gets `liveness` (when `now` is given),
    `end_reason` and `last_write_at` at the end; no other run has a new key.
    """
    duration = (None if summary.ended_at is None
                else round(max((summary.ended_at - summary.started_at).total_seconds(), 0.0), 3))
    return {**_summary_base(summary, run, duration),
            **live_summary_fields(summary, now, stale_after)}


def _summary_base(summary: RunSummary, run: RunData, duration: float | None) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "started_at": summary.started_at.astimezone(timezone.utc).isoformat(),
        "ended_at": (None if summary.ended_at is None
                     else summary.ended_at.astimezone(timezone.utc).isoformat()),
        "duration_seconds": duration,
        "status": summary.status,
        "incomplete": summary.status == "running",
        "kind": summary.kind,
        "fills": summary.fills,
        "code_version": summary.code_version,
        "symbols": list(summary.data.symbols),
        "algo_count": len(run.algos),
        "trade_count": len(run.round_trips),
        "net": fmt4(run_net(run)),
    }


def run_header(run: RunData, *, data_directory: str, now: datetime | None = None,
               stale_after: timedelta = STALE_AFTER) -> dict[str, Any]:
    """The `run` part of the report document (a live run also has the liveness keys)."""
    return {**_header_base(run, data_directory),
            **live_summary_fields(run.summary, now, stale_after)}


def _header_base(run: RunData, data_directory: str) -> dict[str, Any]:
    s = run.summary
    config = run.config if isinstance(run.config, Mapping) else {}
    costs = config.get("costs") if s.fills == "realistic" else None
    return {
        "run_id": s.run_id,
        "status": s.status,
        "incomplete": s.status == "running",
        "kind": s.kind,
        "fills": s.fills,
        "started_at": s.started_at.astimezone(timezone.utc).isoformat(),
        "ended_at": None if s.ended_at is None else s.ended_at.astimezone(timezone.utc).isoformat(),
        "code_version": s.code_version,
        "symbols": list(s.data.symbols),
        "first_bar": None if s.data.first_bar is None else s.data.first_bar.isoformat(),
        "last_bar": None if s.data.last_bar is None else s.data.last_bar.isoformat(),
        "data_directory": data_directory,
        # None for a run stored before gaps were recorded
        "replayed_minutes": s.data.replayed_minutes,
        "missing_minutes": (None if s.data.missing_minutes is None
                            else {symbol: n for symbol, n in s.data.missing_minutes}),
        "uncovered_minutes": s.data.uncovered_minutes,
        # None for a run stored before the gap rule (issue 175)
        "gap_rule": None if s.data.gap_rule is None else gap_rule_to_json(s.data.gap_rule),
        "data_warnings": list(s.data.warnings),
        "capital": config.get("capital") if s.fills == "realistic" else None,
        "costs": costs if isinstance(costs, Mapping) else None,
    }


def report_document(run: RunData, *, data_directory: str, now: datetime | None = None,
                    stale_after: timedelta = STALE_AFTER,
                    counts: Mapping[str, Mapping[str, int]] | None = None,
                    last_bars: Mapping[str, StoredBar] | None = None) -> dict[str, Any]:
    """The `{"run", "accounts", "total"}` document of `report --json`.

    For a live run it also has `open_positions` (marked at `last_bars`, the
    store's latest bar per symbol, when given) and, when `counts` (the store's
    `load_counts`) is given, `live_counts`. Any other run gets neither.
    """
    stats = run_statistics(run, IGNORED_KINDS)
    accounts = {
        algo.idname: {"symbol": algo.ticker, "algo": algo.algo_id, "mode": algo.mode,
                      **statistics_record(stats["accounts"][algo.idname])}
        for algo in run.algos
    }
    document: dict[str, Any] = {"run": run_header(run, data_directory=data_directory, now=now,
                                  stale_after=stale_after),
                "accounts": accounts, "total": statistics_record(stats["total"])}
    if is_live_run(run.summary):
        document["open_positions"] = open_positions(run, last_bars)
        if counts is not None:
            document["live_counts"] = live_counts_record(counts, run.summary.data.gap_rule)
    return document
