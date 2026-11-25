"""`GET /api/compare?runs=1,2`: the statistics of 2 to 5 stored runs side by side.

Every run is loaded inside ONE `with_store` callback (one connection) and run
through `report_document`, so each statistics block is exactly the `accounts[...]`
or `total` of that run's report. Only the last folder name of the stored data
directory is returned (`-` when there is none); no absolute path is ever sent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.api.routes_runs import last_folder_name, scrub_document
from MainStoinker.engine.run_documents import report_document
from MainStoinker.engine.store import Store

router = APIRouter()

MIN_RUNS = 2
MAX_RUNS = 5
MAX_DIGITS = 18  # a longer part is never a run id (and `int()` refuses thousands of digits)


def _invalid() -> RequestValidationError:
    return RequestValidationError([{"loc": ("query", "runs"), "msg": "invalid", "type": "value_error"}])


def parse_run_ids(text: str) -> list[int]:
    """`"1,2"` to `[1, 2]`: 2 to 5 distinct plain integers, else a validation error (422)."""
    parts = text.split(",")
    if not all(p.isascii() and p.isdigit() and len(p) <= MAX_DIGITS for p in parts):
        raise _invalid()
    ids = [int(p) for p in parts]
    if not MIN_RUNS <= len(ids) <= MAX_RUNS or len(set(ids)) != len(ids):
        raise _invalid()
    return ids


def _compare(store: Store, ids: list[int]) -> dict[str, Any]:
    known = {r.run_id for r in store.list_runs()}
    for run_id in ids:
        if run_id not in known:
            raise NotFound(f"no run {run_id}")
    docs = [scrub_document(report_document(
                run, data_directory=last_folder_name(run.summary.data.directory)))
            for run in (store.load_run(run_id) for run_id in ids)]
    algos: dict[str, dict[str, Any]] = {}
    for doc in docs:
        for idname in doc["accounts"]:
            algos.setdefault(idname, {})
    for idname, row in algos.items():
        for run_id, doc in zip(ids, docs, strict=True):
            row[str(run_id)] = doc["accounts"].get(idname)
    return {
        "runs": [doc["run"] for doc in docs],
        "algos": algos,
        "total": {str(run_id): doc["total"] for run_id, doc in zip(ids, docs, strict=True)},
    }


@router.get("/api/compare")
async def compare(request: Request, runs: str = Query(...)) -> dict[str, Any]:
    if len(request.query_params.getlist("runs")) != 1:
        raise _invalid()  # a repeated parameter is ambiguous: no "last one wins"
    ids = parse_run_ids(runs)
    return await with_store(request, lambda store: _compare(store, ids))
