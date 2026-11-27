"""The history a live paper session needs before the open (issue 287).

`run` works out how many trading sessions of history its algos need (the largest
`warmup_sessions()`, at least `DEFAULT_WARMUP_DAYS`), checks that the month files hold those sessions
(`check_coverage`: present, complete by the market calendar, final) and, when something is missing,
fetches exactly that before it subscribes to live bars (`HistoryStep`), through the same `fetch`
driver as `fetch-bars`: the same pacing, manifest, gap report and session recording. Data requests
only: this module sends nothing but what `fetch` sends (`reqHistoricalData`, `reqContractDetails`,
`reqCurrentTime`), so the client's allowlist of outgoing messages is unchanged.

A day of a symbol needs history in two cases:

* `missing`: no usable bars. A day with bars but holes (IB sends no bar for a minute without a trade)
  counts as present when the manifest records it as `ok` and final: IB's own final answer, a refetch
  would give the same. Without such a record (a file copied in by hand, an interrupted write) it is
  fetched. A half day's 210 bars are complete (the market calendar), and so are 210 bars of a day the
  calendar calls full (an early close it did not know: the data is kept as it came).
* `provisional`: complete, but fetched before the day's history is final
  (`history.DAY_FINAL_AT`, 06:00 New York next day). A provisional day is fetched again once it is
  final; before that no fetch can improve it (`blocked`): yesterday's bars are never taken as final
  before 06:00 New York of the session day.

The time budget: everything here must be done before 09:30 New York. `plan_fetch` says, from this
computer's clock and the pacing limits, whether the fetch can finish by then; `HistoryStep` stops the
fetch at the open (checked at every progress event of the driver, so within one request). A session
never starts trading without the history its algos need, unless the owner says `--allow-cold`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from MainStoinker.engine import market_calendar as calendar
from MainStoinker.engine.feed import (
    month_file_path,
    offline_data_path,
    read_offline_bars,
    read_symbol_bars,
    symbol_bar_files,
)
from MainStoinker.engine.warmup import first_session_needed
from MainStoinker.ib import cli
from MainStoinker.ib.fetch import (
    DEFAULT_PACING_BACKOFF,
    FetchResult,
    FetchSetupError,
    ProgressEvent,
    estimate_duration,
    fetch,
)
from MainStoinker.ib.history import (
    GAP_COMPLETE,
    GAP_PARTIAL,
    GAP_SHORT,
    STATUS_OK,
    DayRecord,
    Manifest,
    Pacer,
    day_final_utc,
    gap_report,
    manifest_path,
)

DEFAULT_WARMUP_DAYS = 2  # the warm-up of a session whose algos declare less, in trading sessions
OPEN_HOUR, OPEN_MINUTE = 9, 30
SHOWN_DAYS = 6  # days named in one line before "and N more"


class HistoryError(Exception):
    """The month files cannot be judged (unreadable, both layouts): the message is one line."""


# -- what is needed ---------------------------------------------------------------------------------


def needed_days(today: date, sessions: int) -> list[date]:
    """The `sessions` trading days before `today` (market calendar), oldest first."""
    if sessions < 1:
        raise ValueError(f"sessions must be 1 or more, got {sessions}")
    first = first_session_needed(today, sessions)
    return list(calendar.trading_days(first, calendar.previous_trading_day(today)))


def warmup_sessions_for(needs: Sequence[tuple[str, int]], explicit: int | None) -> tuple[int, str]:
    """`(sessions, why)`: the explicit `--warmup-days`, else the largest declared need, at least
    `DEFAULT_WARMUP_DAYS`. `why` is the words after "Warm-up: N sessions" in the console line."""
    need = max((n for _, n in needs), default=0)
    who = ", ".join(name for name, n in needs if n == need and n > 0)
    if explicit is not None:
        return explicit, f"--warmup-days; the algos declare {need}"
    if need > DEFAULT_WARMUP_DAYS:
        return need, f"the most any algo declares: {who}"
    return DEFAULT_WARMUP_DAYS, f"the default; the algos declare {need}"


def day_list_text(days: Sequence[date], shown: int = SHOWN_DAYS) -> str:
    names = [d.isoformat() for d in days]
    if len(names) <= shown:
        return ", ".join(names)
    return ", ".join(names[:shown]) + f" and {len(names) - shown} more"


# -- the coverage check -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolHistory:
    """What one symbol's month files lack of the needed days."""

    symbol: str
    missing: tuple[date, ...] = ()
    provisional: tuple[date, ...] = ()  # present and complete, but fetched before the day is final
    blocked: tuple[date, ...] = ()  # the provisional days that are not final yet: no fetch helps
    partial: tuple[tuple[date, int], ...] = ()  # days kept with holes (IB's final answer): (day, bars)

    @property
    def todo(self) -> list[date]:
        """The days to fetch: missing ones and provisional ones that are final by now."""
        return sorted(set(self.missing) | (set(self.provisional) - set(self.blocked)))


@dataclass(frozen=True)
class Coverage:
    days: tuple[date, ...]
    symbols: tuple[SymbolHistory, ...]

    def todo(self) -> dict[str, list[date]]:
        return {s.symbol: s.todo for s in self.symbols if s.todo}

    def blocked(self) -> dict[str, list[date]]:
        return {s.symbol: list(s.blocked) for s in self.symbols if s.blocked}

    @property
    def requests(self) -> int:
        return sum(len(days) for days in self.todo().values())

    @property
    def clean(self) -> bool:
        return not self.todo() and not self.blocked()

    def describe_gaps(self) -> str:
        parts = []
        for s in self.symbols:
            if s.missing:
                parts.append(f"{s.symbol} missing {day_list_text(s.missing)}")
            late = [d for d in s.provisional if d not in s.blocked]
            if late:
                parts.append(f"{s.symbol} provisional (to fetch again) {day_list_text(late)}")
            if s.blocked:
                parts.append(f"{s.symbol} provisional until 06:00 New York next day "
                             f"{day_list_text(s.blocked)}")
        return "; ".join(parts)


def _manifest(bars_dir: Path, symbol: str) -> Manifest | None:
    """The records of `manifest.json`, read as they stand (no identity check, nothing moved or
    written); None when there is no usable file. `fetch` is what judges the file's identity."""
    try:
        raw = json.loads(manifest_path(bars_dir, symbol).read_text(encoding="utf-8"))
        records = raw["days"]
        manifest = Manifest(symbol, int(raw.get("conId", 0)))
        for key, rec in records.items():
            manifest.days[date.fromisoformat(key).isoformat()] = DayRecord(
                str(rec["status"]), int(rec.get("bars", 0)), None, "", str(rec.get("fetched_utc", "")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return manifest


def _bars_of(bars_dir: Path, symbol: str, days: Sequence[date]) -> pd.DataFrame | None:
    """The rows of the months that hold `days` (all rows of a single legacy file); None when the
    symbol has no bar file at all."""
    try:
        files = symbol_bar_files(bars_dir, symbol)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise HistoryError(f"{symbol}: {exc}") from exc
    try:
        if files[0] == offline_data_path(bars_dir, symbol):
            return read_symbol_bars(bars_dir, symbol)
        parts = []
        for year, month in sorted({(d.year, d.month) for d in days}):
            path = month_file_path(bars_dir, symbol, year, month)
            if path.is_file():
                parts.append(read_offline_bars(path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HistoryError(f"{symbol}: a bar file cannot be read: {exc}") from exc
    return pd.concat(parts, ignore_index=True) if parts else None


def check_coverage(bars_dir: Path, symbols: Sequence[str], days: Sequence[date],
                   now: datetime) -> Coverage:
    """Judge the month files of `symbols` for `days` (offline: nothing is requested or written).

    `now` is an aware datetime (this computer's clock): a provisional day is `blocked` while `now` is
    before `history.day_final_utc(day)`. `HistoryError` for a file that cannot be read."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    found: list[SymbolHistory] = []
    for symbol in symbols:
        frame = _bars_of(bars_dir, symbol, days)
        manifest = _manifest(bars_dir, symbol)
        rows = {r.day: r for r in gap_report(days, frame)} if frame is not None and len(frame) else {}
        missing: list[date] = []
        provisional: list[date] = []
        partial: list[tuple[date, int]] = []
        for day in days:
            row = rows.get(day)
            if row is None or row.present == 0:
                missing.append(day)
                continue
            record = None if manifest is None else manifest.days.get(day.isoformat())
            if manifest is not None and manifest.is_provisional(day):
                provisional.append(day)
            elif row.status in (GAP_COMPLETE, GAP_SHORT):
                continue
            elif row.status == GAP_PARTIAL and record is not None and record.status == STATUS_OK:
                partial.append((day, row.present))  # IB's own final answer, kept as it came
            else:
                missing.append(day)  # holes and no record of a complete answer: ask IB
        blocked = [d for d in provisional if now.astimezone(UTC) < day_final_utc(d)]
        found.append(SymbolHistory(symbol, tuple(missing), tuple(provisional), tuple(blocked),
                                   tuple(partial)))
    return Coverage(tuple(days), tuple(found))


# -- plan and commands ------------------------------------------------------------------------------


def open_epoch(day: date) -> float:
    """Epoch seconds of 09:30 New York on `day`."""
    return datetime(day.year, day.month, day.day, OPEN_HOUR, OPEN_MINUTE,
                    tzinfo=ZoneInfo("America/New_York")).timestamp()


def fetch_command(bars_dir: Path, todo: Mapping[str, Sequence[date]]) -> str:
    """The `fetch-bars` command that fetches `todo` into `bars_dir` (owner form, paper)."""
    days = [d for ds in todo.values() for d in ds]
    return ("python -m MainStoinker fetch-bars --symbols " + ",".join(todo)
            + f" --start {min(days).isoformat()} --end {max(days).isoformat()}"
            + f' --out "{bars_dir}" --paper --no-prompt')


def estimate_seconds(requests: int) -> float:
    """The fewest seconds `requests` history requests take at IB's pacing limits."""
    return estimate_duration(requests)


def describe_requests(coverage: Coverage, seconds: float) -> str:
    todo = coverage.todo()
    most = max((len(d) for d in todo.values()), default=0)
    total = sum(len(d) for d in todo.values())
    text = (f"up to {total} request{'' if total == 1 else 's'} for {len(todo)} "
            f"symbol{'' if len(todo) == 1 else 's'} (at most {most} day{'' if most == 1 else 's'} each)")
    if seconds >= 1:
        text += f", at least {format_seconds(seconds)} at the pacing limits"
    return text


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min {total % 60:02d} s"
    return f"{total // 3600} h {total % 3600 // 60:02d} min"


# -- the fetch step ----------------------------------------------------------------------------------


class OpenReached(Exception):
    """The clock reached the open (or a wait would have passed it) while the fetch was running."""


@dataclass
class HistoryOutcome:
    """What the step did, for the driver."""

    elapsed: float = 0.0  # seconds of the session's clock the step took
    stop: tuple[int, str] | None = None  # (exit code, message) when the session must not go on
    cold: str | None = None  # a start with less history than needed, and why


@dataclass
class HistoryStep:
    """The step between the paper check and the subscription: fetch what is missing, or say so.

    `rebuild` is called after a fetch (it builds the warm-up from the month files again; it may
    raise `ValueError`, `FileNotFoundError` or `TypeError`, which stop the session). `exit_stop` is
    the exit code for a history that is incomplete (7), `exit_lost` the one for a lost connection
    (4). The step never raises for what IB does; Ctrl-C passes through."""

    bars_dir: Path
    symbols: tuple[str, ...]
    days: tuple[date, ...]
    today: date
    coverage: Coverage
    allow_cold: bool
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    wall: Callable[[], datetime]
    exit_stop: int
    exit_lost: int
    rebuild: Callable[[], None]
    outcome: HistoryOutcome = field(default_factory=HistoryOutcome)

    # -- the step ---------------------------------------------------------------------------------

    def __call__(self, conn: Any, md: Any, say: Callable[[str], None]) -> int | None:
        started = self.clock()
        try:
            return self._run(conn, md, say)
        finally:
            self.outcome.elapsed = max(0.0, self.clock() - started)

    def _run(self, conn: Any, md: Any, say: Callable[[str], None]) -> int | None:
        coverage = self.coverage
        todo = coverage.todo()
        if not todo:
            return None
        requests = coverage.requests
        seconds = estimate_seconds(requests)
        now = self.wall().timestamp()
        opening = open_epoch(self.today)
        say(f"History: estimate: {describe_requests(coverage, seconds)}; the open (09:30 New York) "
            f"is in {format_seconds(max(0.0, opening - now))}")
        if now >= opening or now + seconds > opening:
            why = ("the open has passed" if now >= opening else
                   f"the fetch cannot finish before the open (at least {format_seconds(seconds)} "
                   f"needed, {format_seconds(opening - now)} left)")
            return self._cannot(say, coverage, why)
        say(f"History: fetching {requests} symbol-day(s) before the open, data only")
        hit_open = False
        results: list[FetchResult] = []
        try:
            results = self._fetch_groups(conn, md, say, todo, opening)
        except OpenReached:
            hit_open = True
        except FetchSetupError as exc:
            return self._stop(say, f"the fetch was refused before anything was requested: {exc}")
        if hit_open:
            done = "what was fetched is on disk and a rerun resumes"
            if self.allow_cold:
                say(f"History: the open was reached before the fetch finished ({done}); --allow-cold: "
                    "the session goes on with the history it has")
                self.outcome.cold = "the fetch was cut at the open"
                return self._rebuild(say)
            return self._stop(say, f"the fetch had not finished by the open, 09:30 New York ({done})")
        lost = any(r.connection_lost for r in results)
        return self._settle(say, results, lost)

    def _cannot(self, say: Callable[[str], None], coverage: Coverage, why: str) -> int | None:
        if self.allow_cold:
            say(f"History: {why}; --allow-cold: no fetch, the session starts with the history it has")
            self.outcome.cold = why
            return self._rebuild(say)
        return self._stop(say, f"{why}; missing: {coverage.describe_gaps()}")

    def _fetch_groups(self, conn: Any, md: Any, say: Callable[[str], None],
                      todo: Mapping[str, Sequence[date]], opening: float) -> list[FetchResult]:
        groups: dict[tuple[date, date], list[str]] = {}
        for symbol, days in todo.items():
            groups.setdefault((days[0], days[-1]), []).append(symbol)
        show = _progress(say)
        pacer = Pacer(self.clock)  # ONE pacer for every group: the pacing limits are per session

        def on_progress(event: ProgressEvent) -> None:
            wait = 0.0
            if event.kind == "pacing_wait":
                wait = float(event.detail.get("seconds", 0.0))  # type: ignore[arg-type]
            elif event.kind == "pacing_backoff":
                wait = DEFAULT_PACING_BACKOFF
            if event.kind in ("day", "pacing_wait", "pacing_backoff") \
                    and self.wall().timestamp() + wait > opening:
                raise OpenReached
            show(event)

        results = []
        for (start, end), names in groups.items():
            results.append(fetch(md, names, start, end, self.bars_dir, connection=conn,
                                 clock=self.clock, sleep=self.sleep, on_progress=on_progress,
                                 pacer=pacer))
            result = results[-1]
            if result.stop_reason is not None:
                break  # a stopped run (lost connection, pacing, no farm): the rest would only repeat it
        return results

    # -- after the fetch --------------------------------------------------------------------------

    def _settle(self, say: Callable[[str], None], results: Sequence[FetchResult], lost: bool) -> int | None:
        for result in results:
            for name, res in result.symbols.items():
                if res.state != "done" or res.failed:
                    for failure in res.failures:
                        say(f"History: {name} {failure.day}: failed ({failure.text})")
                    if res.message:
                        say(f"History: {name}: {res.message}")
        try:
            after = check_coverage(self.bars_dir, self.symbols, self.days, self.wall())
        except HistoryError as exc:
            return self._stop(say, f"the month files cannot be read after the fetch: {exc}")
        stops = [r.stop_reason for r in results if r.stop_reason is not None]
        if after.clean:
            say(f"History: complete: the month files hold the {len(self.days)} session(s) "
                f"{self.days[0]} to {self.days[-1]} for {', '.join(self.symbols)}")
            return self._rebuild(say)
        text = (f"the history is still incomplete after the fetch: {after.describe_gaps()}"
                + (f" (the fetch stopped: {'; '.join(stops)})" if stops else ""))
        if self.allow_cold:
            say(f"History: {text}; --allow-cold: the session goes on with the history it has")
            self.outcome.cold = text
            return self._rebuild(say)
        if lost:
            self.outcome.stop = (self.exit_lost, text)
            say(f"History: STOPPED: {text}. Nothing was traded and no run was stored; start "
                "again once TWS is back (the fetch resumes where it stopped).")
            return self.exit_lost
        return self._stop(say, text)

    def _rebuild(self, say: Callable[[str], None]) -> int | None:
        try:
            self.rebuild()
        except (FileNotFoundError, ValueError, TypeError) as exc:
            return self._stop(say, f"no warm-up can be built from the month files: {exc}")
        return None

    def _stop(self, say: Callable[[str], None], text: str) -> int:
        self.outcome.stop = (self.exit_stop, text)
        say(f"History: STOPPED: {text}. The session does not start trading without its history: "
            "nothing was traded and no run was stored. Run it again (a fetch resumes where it stopped), "
            "or use --allow-cold to start with what is stored.")
        return self.exit_stop


def _progress(say: Callable[[str], None]) -> Callable[[ProgressEvent], None]:
    """The progress lines of `fetch-bars`, under a `History:` prefix (`cli._progress`)."""
    return cli._progress(lambda text="": say(f"History:{text}" if text.startswith("  ")  # noqa: SLF001
                                             else f"History: {text}"))


__all__ = [
    "DEFAULT_WARMUP_DAYS", "Coverage", "HistoryError", "HistoryOutcome", "HistoryStep",
    "OpenReached", "SymbolHistory", "check_coverage", "day_list_text", "fetch_command",
    "needed_days", "open_epoch", "warmup_sessions_for",
]
