"""The `runs` and `report` commands: read stored runs and print them.

    python -m MainStoinker runs --db SPEC [--json]
    python -m MainStoinker report RUN --db SPEC [--json]

Both only read. They go through `store.open_store_readonly`, `Store.list_runs` and
`Store.load_run` (no SQL here) and compute with `stats`; they need no algos,
no data directory, no ledger, no git and never create a database: a missing
file is an error. Every problem is a `ReportError` (the caller prints one line
and exits 2), including any `StoreError` or database error while reading. A
database that another process is writing can be read (WAL).

`runs`: one line per run, oldest first. Times are UTC. A run still marked
`running` is shown as `incomplete` (a killed process, or a failure that could
not be recorded). The stored absolute data directory is never printed. `runs`
loads every run to count its algos and trades. With `--json`:
`{"runs": [{"run_id", "started_at", "ended_at", "duration_seconds", "status",
"incomplete", "kind", "fills", "code_version", "symbols", "algo_count",
"trade_count", "net"}]}`, `status` as stored, money as 4-decimal strings.
A live run (`run_documents.is_live_run`) also has `liveness`, `end_reason` and
`last_write_at`; no other run has a new key. Live runs show their liveness
(`running`, `stale`) in the status column instead of `incomplete`.

`report RUN`: a header (run, status with a note unless `completed`, kind,
fills, times, code version, symbols, bar range, capital and costs for
realistic fills, the data directory relative to the current directory if it is
inside it, else its last component only) and a fixed-width table with one row
per algo and a TOTAL row. Money in the table has 2 decimals (half up). Win
rate has 1 decimal. With `--json`: `{"run": {...}, "accounts": {idname:
{"symbol", "algo", "mode", ...statistics}}, "total": {...statistics}}`, the
statistics being `stats.statistics_record` (4-decimal strings, `win_rate` a
fraction). Definitions of every figure are in `stats.py`.

For a live run the report adds, only where there is something to say: the
liveness (a run still `running` is `running` or, when nothing was written for
`STALE_AFTER`, `stale`), the end reason, the live counts (minutes fed, dropped
per reason, late and not fed) and the open positions with their unrealised
result at the last stored bar. In `--json` they are the keys described in
`run_documents`. The current time is read here, at the edge, and passed down
(`now`); tests inject it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .gaprule import GapRuleReport
from .ledger import IGNORED_KINDS
from .run_documents import (
    STALE_AFTER,
    is_live_run,
    live_counts_record,
    liveness,
    open_positions,
    report_document,
    run_header,
    run_net,
    run_summary_record,
)
from .stats import fmt4, run_statistics
from .store import RunData, RunSummary, Store, StoredBar, StoreError, open_store_readonly

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_RUN_ID = re.compile(r"[0-9]+")
_INT64_MAX = 2**63 - 1
_TWO = Decimal("0.01")
_ONE = Decimal("0.1")
_LEGACY_NOTE = "legacy model (bar-close fills, no commission)"


class ReportError(Exception):
    """A problem with the arguments or the database; one line, exit code 2."""


# --- opening ---------------------------------------------------------------------


def _open_existing(spec: str | None, env: Mapping[str, str] | None) -> Store:
    """Open an existing database read-only; never creates or changes a file."""
    if spec is None:
        raise ReportError("--db is required: give the database file (or sqlite:///path)")
    path = spec[len("sqlite:///"):] if spec.startswith("sqlite:///") else spec
    if path == ":memory:":
        raise ReportError("--db :memory: holds nothing to read; give a database file")
    if not path.strip():
        raise ReportError("--db is empty")
    scheme = _SCHEME.match(path)
    if not (scheme and len(scheme.group(1)) > 1):  # a single letter is a Windows drive
        file = Path(path).expanduser()
        try:
            exists, is_file = file.exists(), file.is_file()
            size = file.stat().st_size if is_file else 0
        except OSError as exc:
            raise ReportError(f"cannot read database {path}: {exc}") from exc
        if not exists:
            raise ReportError(f"database {path} does not exist")
        if not is_file:
            raise ReportError(f"database {path} is not a file")
        if size == 0:
            raise ReportError(f"database {path} is empty (not a database)")
    try:
        return open_store_readonly(spec, env=os.environ if env is None else env)
    except StoreError as exc:
        raise ReportError(str(exc)) from exc


def _parse_run_id(text: str) -> int:
    if not _RUN_ID.fullmatch(text):
        raise ReportError(f"run id must be a whole number, got {ascii(text)}")
    return int(text)


# --- formatting ------------------------------------------------------------------


def _money2(value: Decimal) -> str:
    out = format(value.quantize(_TWO, rounding=ROUND_HALF_UP), "f")
    return out[1:] if out.startswith("-") and not any(c in "123456789" for c in out) else out


def _opt_money2(value: Decimal | None) -> str:
    return "-" if value is None else _money2(value)


def _percent(rate: Decimal | None) -> str:
    return "-" if rate is None else format((rate * 100).quantize(_ONE, rounding=ROUND_HALF_UP), "f")


def _utc(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _duration(start: datetime, end: datetime | None) -> str:
    if end is None:
        return "-"
    seconds = max((end - start).total_seconds(), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def _short_version(code_version: str) -> str:
    if not code_version:
        return "-"
    short = code_version[:10]
    return short + "+dirty" if "-dirty" in code_version else short


def _status_shown(status: str) -> str:
    return "incomplete" if status == "running" else status


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           right: Collection[int]) -> list[str]:
    """Fixed-width columns; the columns in `right` are right-aligned, the others left-aligned."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        parts = [c.rjust(w) if i in right else c.ljust(w)
                 for i, (c, w) in enumerate(zip(cells, widths, strict=True))]
        return "  ".join(parts).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)]


def _write(lines: Sequence[str]) -> None:
    """Print lines; characters the console cannot show become escapes, never a traceback."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    for line in lines:
        print(line.encode(encoding, "backslashreplace").decode(encoding))


def _data_directory(directory: str) -> str:
    """The stored data directory relative to the current directory if inside it, else its name."""
    if not directory:
        return "-"
    stored = Path(directory)
    try:
        return str(stored.relative_to(Path.cwd().resolve()) or ".")
    except (ValueError, OSError):
        return stored.name or "-"


# --- reading with errors converted ------------------------------------------------


def _read(action: Any) -> Any:
    try:
        return action()
    except StoreError as exc:
        raise ReportError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise ReportError(f"database error: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ReportError(f"stored data cannot be read: {type(exc).__name__}: {exc}") from exc


# --- runs ------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    """The edge's clock: the injected time, else the wall clock (UTC)."""
    return datetime.now(timezone.utc) if now is None else now


def run_runs_command(spec: str | None, *, as_json: bool,
                     env: Mapping[str, str] | None = None, now: datetime | None = None,
                     stale_after: timedelta = STALE_AFTER) -> int:
    store = _open_existing(spec, env)
    try:
        entries: list[tuple[RunSummary, RunData]] = _read(
            lambda: [(s, store.load_run(s.run_id)) for s in store.list_runs()])
    finally:
        store.close()
    now = _now(now)
    if as_json:
        records = [run_summary_record(summary, run, now=now, stale_after=stale_after)
                   for summary, run in entries]
        _write([json.dumps({"runs": records}, indent=1)])
        return 0
    if not entries:
        _write(["no runs stored"])
        return 0
    rows = [[str(s.run_id), _utc(s.started_at), _duration(s.started_at, s.ended_at),
             liveness(s, now, stale_after) if is_live_run(s) and s.status == "running"
             else _status_shown(s.status), s.kind, s.fills, str(len(r.algos)),
             str(len(r.round_trips)), _money2(run_net(r)), _short_version(s.code_version)]
            for s, r in entries]
    headers = ["id", "started (UTC)", "duration", "status", "kind", "fills", "algos", "trades",
               "net", "code"]
    _write(_table(headers, rows, right={0, 2, 6, 7, 8}))
    return 0


# --- report ----------------------------------------------------------------------


def run_report_command(spec: str | None, run_text: str, *, as_json: bool,
                       env: Mapping[str, str] | None = None, now: datetime | None = None,
                       stale_after: timedelta = STALE_AFTER) -> int:
    run_id = _parse_run_id(run_text)
    store = _open_existing(spec, env)
    counts: dict[str, dict[str, int]] | None = None
    last_bars: dict[str, StoredBar] | None = None
    try:
        if run_id > _INT64_MAX:
            raise ReportError(f"no run {run_id}")
        run: RunData = _read(lambda: store.load_run(run_id))
        if is_live_run(run.summary):
            counts = _read(lambda: store.load_counts(run_id))
            last_bars = _read(lambda: {b.symbol: b for b in store.load_last_bars(run_id)})
    finally:
        store.close()
    now = _now(now)
    data_directory = _data_directory(run.summary.data.directory)
    if as_json:
        _write([json.dumps(report_document(run, data_directory=data_directory, now=now,
                                           stale_after=stale_after, counts=counts,
                                           last_bars=last_bars), indent=1)])
        return 0

    stats = run_statistics(run, IGNORED_KINDS)
    header = run_header(run, data_directory=data_directory)
    s = run.summary
    live = is_live_run(s)
    lines = [f"run {s.run_id}"]
    if s.status == "completed":
        lines.append("status: completed")
    elif s.status == "running":
        lines.append("status: incomplete (still marked running: the run is in progress, or its "
                     "process was killed). Figures cover only what was stored.")
    else:
        lines.append("status: failed. The run did not finish; figures cover only what was stored.")
    if live:
        lines.extend(_liveness_lines(s, now, stale_after))
    lines.append(f"kind: {s.kind}")
    lines.append(f"fills: {s.fills}")
    lines.append(f"started: {_utc(s.started_at)} UTC")
    lines.append(f"ended: {_utc(s.ended_at)} UTC  (duration {_duration(s.started_at, s.ended_at)})")
    lines.append(f"code version: {_short_version(s.code_version)}")
    lines.append(f"symbols: {', '.join(s.data.symbols) or '-'}")
    first, last = s.data.first_bar, s.data.last_bar
    lines.append(f"bars: {first if first else '-'} to {last if last else '-'}")
    lines.append(f"data directory: {header['data_directory']}")
    missing = header["missing_minutes"]
    if missing and any(missing.values()):
        lines.append("missing bars: " + ", ".join(
            f"{symbol} {count} of {header['replayed_minutes']} minutes"
            for symbol, count in missing.items() if count))
    if header["uncovered_minutes"]:
        lines.append(f"minutes with no bar for any symbol: {header['uncovered_minutes']}")
    lines.extend(gap_rule_lines(s.data.gap_rule))
    lines.extend(f"data warning: {text}" for text in header["data_warnings"])
    if live and counts is not None:
        lines.extend(live_count_lines(live_counts_record(counts, s.data.gap_rule)))
    if s.fills == "realistic":
        capital = header["capital"]
        lines.append("capital per algo: " + (_opt_str_money(capital) or "-"))
        costs = header["costs"]
        if costs:
            lines.append("costs: commission {}/share, minimum {} per order, slippage {}/share"
                         .format(*(_opt_str_money(costs.get(k)) or "-" for k in (
                             "commission_per_share", "commission_minimum", "slippage_per_share"))))
    else:
        lines.append(f"fill model: {_LEGACY_NOTE}")
    lines.append("")
    headers = ["algo", "trades", "wins", "win %", "net", "commission", "avg win", "avg loss",
               "max drawdown", "final equity"]
    rows = [_stats_row(idname, st) for idname, st in stats["accounts"].items()]
    rows.append(_stats_row("TOTAL", stats["total"]))
    lines.extend(_table(headers, rows, right=range(1, len(headers))))
    total = stats["total"]
    lines.append("")
    ignored = sum(total["ignored_events"].values())
    lines.append(f"TOTAL: {total['synthetic_closes']} synthetic end-of-data closes (counted in the "
                 f"trades), {ignored} ignored events, {total['cancelled_entries']} cancelled entries")
    if live:
        lines.extend(open_position_lines(open_positions(run, last_bars)))
    _write(lines)
    return 0


def _liveness_lines(s: RunSummary, now: datetime, stale_after: timedelta) -> list[str]:
    """Liveness (only for a run still marked running) and the end reason (when stored)."""
    lines = []
    state = liveness(s, now, stale_after)
    if state in ("running", "stale"):
        last = s.last_write_at if s.last_write_at is not None else s.started_at
        text = f"liveness: {state} (last write {_utc(last)} UTC"
        if state == "stale":
            text += f", {_duration(last, now)} ago"
        lines.append(text + ")")
    if s.end_reason is not None:
        lines.append(f"end reason: {s.end_reason}")
    return lines


def _counts_line(label: str, counts: Mapping[str, int]) -> str | None:
    shown = [f"{symbol} {n}" for symbol, n in counts.items() if n]
    return f"{label}: " + ", ".join(shown) if shown else None


def live_count_lines(record: Mapping[str, Any]) -> list[str]:
    """Lines for the live counts of a run; nothing when every count is zero or absent."""
    dropped = [f"{symbol} {sum(by.values())} ("
               + ", ".join(f"{reason} {n}" for reason, n in by.items() if n) + ")"
               for symbol, by in record["dropped"].items() if sum(by.values())]
    lines = [line for line in (
        _counts_line("minutes fed", record["fed"]),
        "minutes dropped by the watcher: " + ", ".join(dropped) if dropped else None,
        _counts_line("late, not fed", record["late"])) if line]
    for scope, keyed in record.get("other", {}).items():
        shown = [f"{key} {n}" for key, n in keyed.items() if n]
        if shown:
            lines.append(f"{scope} counts: " + ", ".join(shown))
    return lines


def open_position_lines(positions: Sequence[Mapping[str, Any]]) -> list[str]:
    """The open positions as a table; nothing when there are none."""
    if not positions:
        return []
    headers = ["algo", "symbol", "side", "quantity", "entry", "entry time", "stop", "target",
               "last close", "unrealised"]
    rows = [[p["account"], p["symbol"], p["side"], p["quantity"], p["entry_price"],
             p["entry_time"] or "-", p["stop"] or "-", p["target"] or "-",
             p.get("last_close", "-"), p.get("unrealised", "-")] for p in positions]
    return ["", "open positions:", *_table(headers, rows, right={3, 4, 6, 7, 8, 9})]


def gap_rule_lines(report: GapRuleReport | None) -> list[str]:
    """The lines about the gap rule (issue 175); empty when it counted and refused nothing."""
    if report is None or report.empty:
        return []
    lines = []
    after = [f"{symbol} {count}" for symbol, count in report.bars_after_gap if count]
    if after:
        lines.append("bars that follow a gap (entries signalled on them were refused): " + ", ".join(after))
    if report.total_suppressed:
        lines.append(f"entries refused on bars that follow a gap: {report.total_suppressed}")
        for e in report.suppressed:
            shown = ", ".join(t.isoformat(sep=" ") for t in e.times[:5])
            more = f", ... ({e.count - 5} more)" if e.count > 5 else ""
            lines.append(f"  {e.algo} {e.symbol}: {e.count} at {shown}{more}")
    return lines


def _opt_str_money(text: Any) -> str | None:
    if text is None:
        return None
    try:
        return fmt4(Decimal(str(text)))
    except ArithmeticError:
        return str(text)


def _stats_row(name: str, st: Mapping[str, Any]) -> list[str]:
    return [name, str(st["trade_count"]), str(st["wins"]), _percent(st["win_rate"]),
            _money2(st["net"]), _money2(st["commission"]), _opt_money2(st["avg_win"]),
            _opt_money2(st["avg_loss"]), _money2(st["max_drawdown"]), _money2(st["final_equity"])]

