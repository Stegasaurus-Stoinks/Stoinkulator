"""`GET /api/health`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from MainStoinker.api import API_VERSION
from MainStoinker.api.db import get_store
from MainStoinker.engine.store import SqliteStore, Store

router = APIRouter()


@router.get("/api/health")
async def health(store: Store = Depends(get_store)) -> dict[str, Any]:  # noqa: B008
    runs = store.list_runs()
    version = store.schema_version if isinstance(store, SqliteStore) else None
    return {
        "api_version": API_VERSION,
        "schema_version": version,
        "run_count": len(runs),
        "incomplete_runs": [r.run_id for r in runs if r.status == "running"],
    }
