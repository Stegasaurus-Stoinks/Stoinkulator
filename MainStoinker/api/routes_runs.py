"""`GET /api/runs` and `GET /api/runs/{run_id}`.

`/api/runs` is `{"runs": [...]}`, one `run_summary_record` per stored run, newest
first (`runs --json` lists oldest first). `/api/runs/{run_id}` is the document of
`report --json` (`report_document`); an unknown id is a 404. A run still marked
`running` carries `incomplete: true`, as in the CLI. The stored data directory
is shown as its last folder name only (`-` if it has none), never as a path.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any

from fastapi import APIRouter, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.engine.run_documents import report_document, run_summary_record
from MainStoinker.engine.store import Store

router = APIRouter()


def last_folder_name(directory: str) -> str:
    """The last folder of a stored directory; `-` for an empty or root-only one.

    Read as a Windows path, which also splits on `/`, so a path stored on either
    system is reduced the same way.
    """
    if not directory:
        return "-"
    return PureWindowsPath(directory).name or "-"


def _runs(store: Store) -> dict[str, Any]:
    records = [run_summary_record(s, store.load_run(s.run_id)) for s in store.list_runs()]
    records.reverse()
    return {"runs": records}


def _run_detail(store: Store, run_id: int) -> dict[str, Any]:
    if run_id not in {s.run_id for s in store.list_runs()}:
        raise NotFound(f"no run {run_id}")
    run = store.load_run(run_id)
    return report_document(run, data_directory=last_folder_name(run.summary.data.directory))


@router.get("/api/runs")
async def runs(request: Request) -> dict[str, Any]:
    return await with_store(request, _runs)


@router.get("/api/runs/{run_id}")
async def run_detail(run_id: int, request: Request) -> dict[str, Any]:
    return await with_store(request, lambda store: _run_detail(store, run_id))
