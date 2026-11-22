"""`GET /api/runs/{run_id}/series`: the indicator series one algo drew, for a time window.

The window rules are the bars route's: `start` and `end` are naive ISO date-times
with a time part (a timezone offset or a date-only value is a 422, because the
store does not validate its bounds), giving only one of them is a 422, a window
of more than `MAX_TRADING_DAYS` weekdays is a 422, and with neither the window
is the calendar day of the run's last bar. Times are returned exactly as stored
(data time, naive).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.engine.store import Store

router = APIRouter()

MAX_TRADING_DAYS = 5
SIGNIFICANT_DIGITS = 6

# A naive date-time: date, a T or space, hours and minutes, optional seconds and fraction.
_NAIVE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?")


def _bound(name: str, text: str) -> datetime:
    if _NAIVE.fullmatch(text) is None:
        raise HTTPException(422, f"invalid parameter: {name} (naive date-time, no timezone)")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, f"invalid parameter: {name}") from None


def _weekdays(first: date, last: date) -> int:
    return sum(1 for i in range((last - first).days + 1)
               if (first + timedelta(days=i)).weekday() < 5)


def _window(start: str | None, end: str | None) -> tuple[datetime, datetime] | None:
    """The explicit window, or None for the default; raises 422 for a bad one."""
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise HTTPException(422, "invalid parameter: give both start and end, or neither")
    lo, hi = _bound("start", start), _bound("end", end)
    if hi < lo:
        raise HTTPException(422, "invalid parameter: end is before start")
    if _weekdays(lo.date(), hi.date()) > MAX_TRADING_DAYS:
        raise HTTPException(422, f"window is longer than {MAX_TRADING_DAYS} trading days")
    return lo, hi


def _round(value: float) -> float:
    return float(f"{value:.{SIGNIFICANT_DIGITS}g}")


def _series(store: Store, run_id: int, algo: str,
            window: tuple[datetime, datetime] | None) -> dict[str, Any]:
    if run_id not in {r.run_id for r in store.list_runs()}:
        raise NotFound(f"no run {run_id}")
    run = store.load_run(run_id)
    if algo not in {a.idname for a in run.algos}:
        raise NotFound(f"no algo {algo!r} in run {run_id}")
    infos = sorted((s for s in run.series if s.account == algo),
                   key=lambda s: (s.position, s.name))
    if not infos:
        return {"algo": algo, "available": False, "series": []}
    if window is None:
        last = run.summary.data.last_bar
        if last is not None:
            window = (datetime.combine(last.date(), time.min),
                      datetime.combine(last.date(), time.max))
    start, end = window if window is not None else (None, None)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in store.load_series(run_id, algo, start, end):
        by_name[p.name].append({"time": p.time.isoformat(), "value": _round(p.value)})
    return {"algo": algo, "available": True,
            "series": [{"name": s.name, "draw_type": s.draw_type, "position": s.position,
                        "points": by_name.get(s.name, [])} for s in infos]}


@router.get("/api/runs/{run_id}/series")
async def series(request: Request, run_id: int, algo: str = Query(...),
                 start: str | None = None, end: str | None = None) -> dict[str, Any]:
    window = _window(start, end)
    return await with_store(request, lambda store: _series(store, run_id, algo, window))
