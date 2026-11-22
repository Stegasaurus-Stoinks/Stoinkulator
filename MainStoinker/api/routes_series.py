"""`GET /api/runs/{run_id}/series`: the indicator series one algo drew, for a time window.

The window rules are `MainStoinker.api.windows`' (the same as the bars route's;
default: the calendar day of the run's last bar). A bad window is a 422 whose
detail is the rule's message; an unknown run or algo is a 404 first. Times are
naive "data time", formatted by `format_time` (`YYYY-MM-DD HH:MM:SS`).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.api.windows import WindowError, format_time, parse_window
from MainStoinker.engine.store import Store

router = APIRouter()

DECIMAL_PLACES = 6
MAX_ECHO = 64


def _echo(value: str) -> str:
    """At most `MAX_ECHO` characters of a client-supplied value, cut with an ellipsis."""
    return value if len(value) <= MAX_ECHO else value[:MAX_ECHO] + "..."


def _round(value: float) -> float:
    return round(value, DECIMAL_PLACES)


def _series(store: Store, run_id: int, algo: str,
            start: str | None, end: str | None) -> dict[str, Any]:
    if run_id not in {r.run_id for r in store.list_runs()}:
        raise NotFound(f"no run {run_id}")
    run = store.load_run(run_id)
    if algo not in {a.idname for a in run.algos}:
        raise NotFound(f"no algo {_echo(algo)} in run {run_id}")
    try:
        lo, hi = parse_window(start, end, default_day=run.summary.data.last_bar)
    except WindowError as error:
        raise HTTPException(422, str(error)) from None
    status = run.summary.status
    infos = sorted((s for s in run.series if s.account == algo),
                   key=lambda s: (s.position, s.name))
    if not infos:
        return {"algo": algo, "run_status": status, "available": False, "series": []}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in store.load_series(run_id, algo, lo, hi):
        by_name[p.name].append({"time": format_time(p.time), "value": _round(p.value)})
    return {"algo": algo, "run_status": status, "available": True,
            "series": [{"name": s.name, "draw_type": s.draw_type, "position": s.position,
                        "points": by_name.get(s.name, [])} for s in infos]}


@router.get("/api/runs/{run_id}/series")
async def series(request: Request, run_id: int, algo: str = Query(..., min_length=1),
                 start: str | None = None, end: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _series(store, run_id, algo, start, end))
