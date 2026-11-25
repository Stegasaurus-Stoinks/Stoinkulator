"""`GET /api/runs` and `GET /api/runs/{run_id}`.

`/api/runs` is `{"runs": [...]}`, one `run_summary_record` per stored run, newest
first (`runs --json` lists oldest first). `/api/runs/{run_id}` is the document of
`report --json` (`report_document`); an unknown id is a 404. A run still marked
`running` carries `incomplete: true`, as in the CLI. The stored data directory
is shown as its last folder name only (`-` if it has none), never as a path.

Live runs (kind `paper-live` / `live`, or written by a live writer) also carry
`liveness`, `end_reason` and `last_write_at`; the detail document adds
`open_positions` (marked at the last stored bars) and `live_counts`. The `live_counts` of a
running or stale run (and of one whose data record is marked provisional) has `provisional: true`: the writer
refreshes the counts with its heartbeats, so they are the counts so far.

Two shapes of the refused-entries count, both kept as stored: `run.gap_rule.suppressed_entries`
is an object (`{"count": N, "by_algo": [...]}`), `live_counts.gap_rule.suppressed_entries` is a
plain number (the total). The dashboard reads both.

"Now" is read once per request, here at the route; `run_documents` has no clock. In the
list, running live runs come first, then stale ones, then the rest, each group
newest first. A backtest's response is exactly what it was before.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Any

from fastapi import APIRouter, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.engine.run_documents import is_live_run, report_document, run_summary_record
from MainStoinker.engine.store import Store

router = APIRouter()


_START = r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?=[^\s/]))"  # drive, UNC, `~`, `/`-rooted
_QUOTED_PATH = re.compile(rf"(?P<q>[\"']){_START}.*?(?P=q)")
_WORD = r"[^\\/\s\"',]+"
# A bare path starts where no word, `.`, `:` or separator precedes it (so `scheme://host/x` and
# `3/4` are not paths). Its folders may hold single spaces ("OneDrive - Corp\..."), but not
# ", ", " and ", " to " or the start of a following path; the last component has no space.
_BARE_PATH = re.compile(
    rf"(?<![\w.:/\\]){_START}"
    rf"(?:{_WORD}(?: (?!(?:and|to)\b)(?!{_START}){_WORD})*[\\/])*[^\\/\s\"',]*")
_TRAILING = ".,;:!?)]}"


def _last_component(path: str) -> str:
    tail = path.rstrip(_TRAILING)
    punct = path[len(tail):]
    name = re.split(r"[\\/]+", tail.rstrip("\\/"))[-1]
    return (name or "-") + punct


def scrub_warning(text: str) -> str:
    """A warning with every path in it (drive, UNC, `/`-rooted, `~`) reduced to its last component."""
    text = _QUOTED_PATH.sub(
        lambda m: m["q"] + _last_component(m[0][1:-1]) + m["q"], text)
    return _BARE_PATH.sub(lambda m: _last_component(m[0]), text)


def scrub_document(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with the paths of `run.data_warnings` removed (the API never shows a path)."""
    header = document["run"]
    warnings = header.get("data_warnings")
    if warnings:
        header["data_warnings"] = [scrub_warning(w) for w in warnings]
    return document


def last_folder_name(directory: str) -> str:
    """The last folder of a stored directory; `-` for an empty or root-only one.

    Read as a Windows path, which also splits on `/`, so a path stored on either
    system is reduced the same way.
    """
    if not directory:
        return "-"
    return PureWindowsPath(directory).name or "-"


def edge_now(request: Request) -> datetime:
    """The clock, read once per request at the route (injectable through `create_app`)."""
    return request.app.state.clock()  # type: ignore[no-any-return]


_LIVENESS_RANK = {"running": 0, "stale": 1}


def _runs(store: Store, now: datetime) -> dict[str, Any]:
    records = [run_summary_record(s, store.load_run(s.run_id), now=now) for s in store.list_runs()]
    records.reverse()
    # Running live runs first, then stale ones, then the rest, each group newest first.
    records.sort(key=lambda r: _LIVENESS_RANK.get(r.get("liveness", ""), 2))
    return {"runs": records}


def _run_detail(store: Store, run_id: int, now: datetime) -> dict[str, Any]:
    if run_id not in {s.run_id for s in store.list_runs()}:
        raise NotFound(f"no run {run_id}")
    run = store.load_run(run_id)
    directory = last_folder_name(run.summary.data.directory)
    if not is_live_run(run.summary):
        return scrub_document(report_document(run, data_directory=directory, now=now))
    return scrub_document(report_document(
        run, data_directory=directory, now=now, counts=store.load_counts(run_id),
        last_bars={b.symbol: b for b in store.load_last_bars(run_id)}))


@router.get("/api/runs")
async def runs(request: Request) -> dict[str, Any]:
    now = edge_now(request)
    return await with_store(request, lambda store: _runs(store, now))


@router.get("/api/runs/{run_id}")
async def run_detail(run_id: int, request: Request) -> dict[str, Any]:
    now = edge_now(request)
    return await with_store(request, lambda store: _run_detail(store, run_id, now))
