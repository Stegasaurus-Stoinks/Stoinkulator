"""Shared names of live runs: run kinds, heartbeat, and the `run_counts` key format.

The live writer builds count keys with `count_key` and the run documents read
them with `parse_count_key`; neither types the format by hand. Imports nothing
else from the package (no store internals), so both sides can import it.

A count is stored in `run_counts` under scope `COUNT_SCOPE_SYMBOL` with the key
`SYMBOL:fed`, `SYMBOL:late` or `SYMBOL:dropped:REASON`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NamedTuple

LIVE_KINDS: tuple[str, ...] = ("paper-live", "live")
HEARTBEAT_INTERVAL = timedelta(seconds=30)  # a live writer touches its run at least this often
# A running run whose last write is older than this is "stale" (its writer is probably dead).
STALE_AFTER = 6 * HEARTBEAT_INTERVAL

COUNT_SCOPE_SYMBOL = "symbol"
COUNT_FED, COUNT_LATE, COUNT_DROPPED = "fed", "late", "dropped"
COUNT_KINDS: tuple[str, ...] = (COUNT_FED, COUNT_LATE, COUNT_DROPPED)
COUNT_SEPARATOR = ":"

# Why the watcher dropped a minute (the `reason` of a dropped count), and the
# watcher counter (`MainStoinker/ib/watch_bars.py`, not imported) each stands for:
#
#   identifier        watcher counter        meaning
#   snapshot_only     snapshot_only          seen only in the backfill snapshot
#   connection_lost   lost_not_written       overlapped a connection loss
#   farm_break        farm_not_written       overlapped a data farm break
#   stalled           stale_not_written      closed by time after a stalled stream
#   rerequest_gap     gap_not_written        dropped by a re-requested backfill
#   previous_session  previous_session       backfill bar of an earlier date
DROP_REASONS: tuple[str, ...] = (
    "snapshot_only", "connection_lost", "farm_break", "stalled", "rerequest_gap",
    "previous_session")
DROP_REASON_LABELS: dict[str, str] = {
    "snapshot_only": "seen only in the backfill",
    "connection_lost": "connection lost",
    "farm_break": "data farm break",
    "stalled": "stalled stream",
    "rerequest_gap": "re-request gap",
    "previous_session": "previous session",
}


class CountKey(NamedTuple):
    symbol: str
    kind: str
    reason: str | None = None


def count_key(symbol: str, kind: str, reason: str | None = None) -> str:
    """The `run_counts` key of a count; `ValueError` for anything `parse_count_key` would refuse."""
    if kind not in COUNT_KINDS:
        raise ValueError(f"unknown count kind {kind!r}; use one of {COUNT_KINDS}")
    if (reason is None) != (kind != COUNT_DROPPED):
        raise ValueError("a reason is required for dropped counts and refused for others")
    if reason is not None and reason not in DROP_REASONS:
        raise ValueError(f"unknown drop reason {reason!r}; use one of {DROP_REASONS}")
    parts = [symbol, kind] + ([] if reason is None else [reason])
    for part in (symbol, *parts[2:]):
        if not part or COUNT_SEPARATOR in part:
            raise ValueError(f"{part!r} must be non-empty and not contain {COUNT_SEPARATOR!r}")
    return COUNT_SEPARATOR.join(parts)


def parse_count_key(scope: str, key: str) -> CountKey | None:
    """Inverse of `count_key` for scope `COUNT_SCOPE_SYMBOL`; None for any other shape."""
    if scope != COUNT_SCOPE_SYMBOL:
        return None
    parts = key.split(COUNT_SEPARATOR)
    if not all(parts):
        return None
    if len(parts) == 2 and parts[1] in (COUNT_FED, COUNT_LATE):
        return CountKey(parts[0], parts[1])
    if len(parts) == 3 and parts[1] == COUNT_DROPPED:
        return CountKey(parts[0], COUNT_DROPPED, parts[2])
    return None
