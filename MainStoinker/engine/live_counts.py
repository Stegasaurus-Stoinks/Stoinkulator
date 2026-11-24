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
