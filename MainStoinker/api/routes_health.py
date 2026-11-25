"""`GET /api/health`.

`incomplete_runs` lists every run still marked `running`, as before. When the
database holds a live run (`is_live_run`), the response also says which of those
are really running and which are stale (their writer has not written for longer
than `STALE_AFTER`): `running_runs`, `stale_runs` (run ids) and `live_state`
(`running` if any live run is running, else `stale` if any is stale, else
`idle`). A database without live runs answers exactly the original four keys.
"Now" is read once per request, at the route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request

from MainStoinker.api import API_VERSION
from MainStoinker.api.db import with_store
from MainStoinker.api.routes_runs import edge_now
from MainStoinker.engine.run_documents import is_live_run, liveness
from MainStoinker.engine.store import SqliteStore, Store

router = APIRouter()


def _health(store: Store, now: datetime) -> dict[str, Any]:
    runs = store.list_runs()
    version = store.schema_version if isinstance(store, SqliteStore) else None
    body: dict[str, Any] = {
        "api_version": API_VERSION,
        "schema_version": version,
        "run_count": len(runs),
        "incomplete_runs": [r.run_id for r in runs if r.status == "running"],
    }
    live = [r for r in runs if is_live_run(r)]
    if live:
        states = {r.run_id: liveness(r, now) for r in live}
        running = [i for i, state in states.items() if state == "running"]
        stale = [i for i, state in states.items() if state == "stale"]
        body["running_runs"] = running
        body["stale_runs"] = stale
        body["live_state"] = "running" if running else "stale" if stale else "idle"
    return body


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    now = edge_now(request)
    return await with_store(request, lambda store: _health(store, now))
