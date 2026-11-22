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
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timezone
from decimal import Decimal
from typing import Any

from .ledger import IGNORED_KINDS
from .stats import fmt4, run_statistics, statistics_record
from .store import RunData, RunSummary


def run_net(run: RunData) -> Decimal:
    """Net of every round trip of the run."""
    return sum((t.net for t in run.round_trips), Decimal("0.0000"))


def run_summary_record(summary: RunSummary, run: RunData) -> dict[str, Any]:
    """One entry of `runs --json`; `run` is the loaded run of `summary`."""
    duration = (None if summary.ended_at is None
                else round(max((summary.ended_at - summary.started_at).total_seconds(), 0.0), 3))
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


def run_header(run: RunData, *, data_directory: str) -> dict[str, Any]:
    """The `run` part of the report document."""
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
        "capital": config.get("capital") if s.fills == "realistic" else None,
        "costs": costs if isinstance(costs, Mapping) else None,
    }


def report_document(run: RunData, *, data_directory: str) -> dict[str, Any]:
    """The `{"run", "accounts", "total"}` document of `report --json`."""
    stats = run_statistics(run, IGNORED_KINDS)
    accounts = {
        algo.idname: {"symbol": algo.ticker, "algo": algo.algo_id, "mode": algo.mode,
                      **statistics_record(stats["accounts"][algo.idname])}
        for algo in run.algos
    }
    return {"run": run_header(run, data_directory=data_directory), "accounts": accounts,
            "total": statistics_record(stats["total"])}
