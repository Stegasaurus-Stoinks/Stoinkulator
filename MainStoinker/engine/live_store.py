"""The incremental run writer of a live session: the only thing that writes a live run.

`LiveRunWriter` is the `on_step` observer of `Engine.start_live` and stores,
step by step, what a backtest stores at its end, so a dashboard reading the
database sees the run grow and a later comparison finds the same records.

Call sequence of a driver (no clock or thread in here: "now" is stamped by the store, and
the driver owns the pace; the only sleep is the injected pause between busy retries):

    writer = LiveRunWriter(store, ledger, engine.algos)
    run_id = writer.begin(fills=..., code_version=..., config=..., data=DataMeta(...),
                          algos=[AlgoMeta, ...], warmup=warmup_frames,
                          warmup_description=description)
    engine.start_live(warmup_frames, on_step=writer.on_step)
    try:
        for timestamp, bars in released_batches:      # the aligner's batches
            writer.fed(timestamp, bars)               # BEFORE engine.step: see below
            engine.step(timestamp, bars)              # on_step -> one transaction
            ...                                       # writer.heartbeat() at least every
                                                      # HEARTBEAT_INTERVAL while idle
    except BaseException:
        ...                                           # remember why
    finally:
        positions = engine.end_live()                 # also after a failed step
        outcome = writer.finish(status, reason, counts, data_meta, engine.gap_report)

A store call that fails because the database is busy (another WRITER holds it: in WAL mode
readers, the dashboard and `report`, never block a write) is repeated `BUSY_RETRIES` times
with `BUSY_PAUSE` seconds of the injected `sleep` before each repeat, inside the step; each
call is one transaction, so a
repeat can neither lose nor duplicate a record. After that the call fails as before.

The bars hand-over: `on_step` receives only the timestamp and the present symbols,
so the driver announces each batch with `fed(timestamp, bars)` before `engine.step`.
`on_step` raises (the engine then fails, as for any observer error) when no `fed`
call precedes it, or when it does not match the step (other time or other symbols),
so a driver that forgets cannot get a run without its bars. A `fed` batch whose step
the engine refused (`ValueError`, nothing applied) is simply replaced by the next
`fed`. The bars of a step that failed inside the engine (no `on_step`) are not
stored: the engine did not complete that step.

Per step, in one transaction: the new order events, fills, round trips, equity points
and ignored events (by index into the ledger's tuples, so nothing is sent twice), the
new points of every indicator series (one `SeriesTracker` per algo), the bars of the
step, and a `last_write_at` stamp. What a failed transaction did not store stays
buffered (series points, series declarations, bars) or unindexed (ledger records) and
goes out with the next write or in `finish`.

`finish` runs after `end_live()` on every exit path: it writes the tail past the last
index sent (a failed step may have produced records without calling the observer, and
`end_live` adds more), flushes the series trackers with `final=True`, replaces the
last bar time's equity points with the ledger's final ones (fills made by `on_end`
are in `ledger.equity()` but not in the last mark; `Ledger.finish` does the same in a
backtest and is not called in a live run), stores the counts and the full `DataMeta`,
and ends the run. It is best effort after an earlier store error: it tries to end
the run as `failed`, never raises for a store problem, and reports the problems in
the returned `FinishOutcome` (the first error ever seen is also `writer.error`).

Provisional counts: `heartbeat(counts, data_meta)` also stores the counts and the data record as
they stand (first and last bar, minute counts, warnings, gap rule), so a dashboard shows them while
the run is running. A busy database skips that part until the next heartbeat (`provisional_skipped`
counts them) and never fails a step or the session. `finish` overwrites both with the final values;
a run that completed holds exactly what it would hold without any provisional write.

Marker delay (decision of issue 192): every indicator series of an algo is held back
by the tracker's `settle` bars (its `order`: 10 for Elliot, 2 for FunctionTest), so
stop and trade markers of such an algo reach the store that many bars late although
they are never revised. Overwriting and deleting stored points is NOT done here; the
tracker only returns new points. Accepted for the first sessions.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, TypeVar

import pandas as pd

from .feed import COLUMNS, Bar
from .gaprule import GapRuleReport
from .ledger import EquityPoint, Ledger
from .live_counts import COUNT_FED, COUNT_SCOPE_SYMBOL, LIVE_KINDS, count_key
from .series import SeriesTracker
from .store import (
    AlgoMeta,
    DataMeta,
    LiveStep,
    RunMeta,
    SeriesInfo,
    SeriesPoint,
    Store,
    StoreBusyError,
    StoredBar,
)
from .warmup import warmup_digest

RUN_KIND = LIVE_KINDS[0]  # "paper-live"
EndStatus = Literal["completed", "failed"]
CountSpec = tuple[str, str, str | None]  # (symbol, kind, reason) as `count_key` takes them
T = TypeVar("T")

# A store call that fails because another writer holds the database (in WAL mode readers, such
# as the dashboard or a `report`, never block a write) is repeated this many times after the
# first attempt, with BUSY_PAUSE seconds
# (the injected sleep) before each repeat: 5 * 0.6 = 3 s of waiting added to the store's own
# busy timeout of every attempt. A write that still fails then fails as it did before.
BUSY_RETRIES = 5
BUSY_PAUSE = 0.6

_NEW_YORK = "America/New_York"
_STAMP = "%Y-%m-%d %H:%M:%S"
_PATH_IN_TEXT = re.compile(r"[\\/]")  # a path or a URL: both have a separator


class LiveWriterError(Exception):
    """The writer was used wrongly (no `fed` before a step, a mismatch, a second `begin`, a run
    that could never be verified, a `finish` without its gap report)."""


@dataclass(frozen=True)
class FinishOutcome:
    """What `LiveRunWriter.finish` achieved."""

    status: str | None  # the status the run was ended with; None when it could not be ended
    reason: str | None
    errors: tuple[str, ...]  # one line per problem, the first error ever seen first
    busy: bool = False  # some store call kept failing because the database stayed busy

    @property
    def ok(self) -> bool:
        return not self.errors and self.status is not None


# --- today's backfill bars: the part of the warm-up the month files do not hold ------------------


def today_frame(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """The frame of today's backfill bars as the session description stores them.

    A row is `[stamp "YYYY-MM-DD HH:MM:SS" (New York wall clock), open, high, low, close,
    volume]`; the epoch `time` column is derived from the stamp (the bars of a live session
    carry exactly that epoch). Raises `ValueError` for a row that cannot be read.
    """
    try:
        stamps = [str(row[0]) for row in rows]
        dates = pd.to_datetime(stamps, format=_STAMP)
        epochs = [int(pd.Timestamp(d).tz_localize(_NEW_YORK).timestamp()) for d in dates]
        frame = pd.DataFrame({
            "date": dates, "time": epochs,
            "open": [float(row[1]) for row in rows], "high": [float(row[2]) for row in rows],
            "low": [float(row[3]) for row in rows], "close": [float(row[4]) for row in rows],
            "volume": [int(row[5]) for row in rows],
        })
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"today's bars cannot be read: {exc}") from exc
    return frame.astype({"time": "int64", "volume": "int64", "open": "float64",
                         "high": "float64", "low": "float64", "close": "float64"})[list(COLUMNS)]


def describe_today_bars(bars: Mapping[str, Sequence[Bar]]
                        ) -> tuple[dict[str, list[list[Any]]], dict[str, str]]:
    """`(rows, digests)` of today's backfill bars per symbol, for the warm-up description.

    `rows` goes under `"today_bars"` and `digests` (`warmup_digest` of `today_frame(rows)`,
    only for a symbol that has bars) under `"today_sha256"`.
    """
    rows = {symbol: [[b.date.strftime(_STAMP), float(b.open), float(b.high), float(b.low),
                      float(b.close), int(b.volume)] for b in sorted(items, key=lambda b: b.time)]
            for symbol, items in bars.items()}
    return rows, {symbol: warmup_digest(today_frame(r)) for symbol, r in rows.items() if r}


def _naive(value: Any) -> datetime:
    return value.to_pydatetime() if isinstance(value, pd.Timestamp) else value


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class LiveRunWriter:
    """Writes one live run to a store, step by step. See the module text.

    `ledger` is the engine's ledger (the writer only reads it) and `algos` the
    engine's algos (`engine.algos`), whose indicator series are tracked.
    """

    def __init__(self, store: Store, ledger: Ledger, algos: Sequence[object], *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._store = store
        self._ledger = ledger
        self._algos = tuple(algos)
        self._sleep = sleep  # the pause between busy retries (BUSY_PAUSE)
        self._symbols: tuple[str, ...] = ()  # data.symbols given to `begin`
        self._run_id: int | None = None
        self._finished: FinishOutcome | None = None
        self._error: BaseException | None = None
        # How much of each ledger tuple is stored.
        self._sent = {"orders": 0, "fills": 0, "round_trips": 0, "equity": 0, "ignored": 0}
        self._trackers: dict[int, SeriesTracker] = {}
        self._broken_trackers: set[int] = set()
        self._history: dict[str, list[Any]] = {}
        # Written with the next transaction: kept until one commits.
        self._buffer_infos: list[SeriesInfo] = []
        self._buffer_points: list[SeriesPoint] = []
        self._buffer_bars: list[StoredBar] = []
        self._pending: tuple[Hashable, dict[str, Bar]] | None = None
        self._last_equity_time: datetime | None = None
        self._provisional_skipped = 0

    # --- state -------------------------------------------------------------

    @property
    def run_id(self) -> int | None:
        return self._run_id

    @property
    def error(self) -> BaseException | None:
        """The first error the writer met (a failed step write, a failed heartbeat, ...)."""
        return self._error

    def _fail(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc

    # --- begin -------------------------------------------------------------

    def begin(
        self,
        *,
        fills: str,
        code_version: str,
        config: Mapping[str, Any],
        data: DataMeta,
        algos: Sequence[AlgoMeta],
        warmup: Mapping[str, pd.DataFrame],
        warmup_description: Mapping[str, Any] | None = None,
    ) -> int:
        """Create the run (kind `paper-live`), store its algos, and `touch` it at once.

        `config` and `data` are what a backtest would pass (`DataMeta` is also what
        `finish` replaces at the end). `warmup_description` (the description
        `live_warmup` returns) is kept in the stored config under `"warmup"`.
        `warmup` holds the frames given to `Engine.start_live`: their dates are the
        algos' history before the first step.

        Refuses (`LiveWriterError`, nothing stored, every problem in one message) a run
        that could never be verified or that holds something it must not: a `DataMeta`
        with `last_bar` set (nothing is known at the start), a warning containing a path,
        no `config["entries"]`, no capital (neither `config["capital"]` nor an algo), no
        `config["costs"]` for realistic fills, no warm-up description, no `data.directory`
        or `data.symbols` (or symbols that are not the algos' tickers), and a description
        whose `today_bars` do not match their digests or the end of the `warmup` frames.
        """
        if self._run_id is not None:
            raise LiveWriterError("begin() was already called")
        problems = _begin_problems(fills, config, data, algos, warmup, warmup_description)
        if problems:
            raise LiveWriterError("begin() refused: " + "; ".join(problems))
        stored = dict(config)
        stored["warmup"] = warmup_description
        self._symbols = tuple(data.symbols)
        self._history = {symbol: list(frame["date"].tolist()) for symbol, frame in warmup.items()}
        meta = RunMeta(kind=RUN_KIND, fills=fills, code_version=code_version, config=stored,
                       data=data)
        run_id = self._retried(lambda: self._store.begin_run(meta))
        self._run_id = run_id
        try:
            self._retried(lambda: self._store.write_algos(run_id, list(algos)))
            self._retried(lambda: self._store.touch(run_id))  # a new run has no last_write_at
        except BaseException as exc:
            self._fail(exc)
            raise
        return run_id

    # --- the step observer ---------------------------------------------------

    def fed(self, timestamp: Any, bars: Mapping[str, Bar]) -> None:
        """Announce the batch the driver is about to give `Engine.step`.

        Call it right before `engine.step(timestamp, bars)`; the next `on_step`
        stores these bars. A second call before an `on_step` replaces the first
        (the engine refused that step).
        """
        self._pending = (timestamp, dict(bars))

    def on_step(self, timestamp: Any, present: tuple[str, ...]) -> None:
        """The `on_step` observer: store the step in one transaction.

        Raises `LiveWriterError` when there is no matching `fed`, and `StoreError` (or
        whatever the store raised) when the write fails; either makes the engine fail.
        """
        run_id = self._require_run()
        if self._error is not None:
            raise LiveWriterError("the writer already failed; the run cannot be continued"
                                  ) from self._error
        pending, self._pending = self._pending, None
        if pending is None or pending[0] != timestamp:
            problem = LiveWriterError(
                f"on_step({timestamp}) without a matching fed(): call writer.fed(timestamp, "
                f"bars) before engine.step"
                + ("" if pending is None else f" (fed was {pending[0]})"))
            self._fail(problem)
            raise problem
        bars = pending[1]
        if set(bars) != set(present):
            problem = LiveWriterError(
                f"the step at {timestamp} had the symbols {sorted(present)} but fed() gave "
                f"{sorted(bars)}")
            self._fail(problem)
            raise problem
        try:
            for symbol in present:
                self._history.setdefault(symbol, []).append(timestamp)
            self._buffer_bars.extend(
                StoredBar(symbol, _naive(bar.date), float(bar.open), float(bar.high),
                          float(bar.low), float(bar.close), float(bar.volume))
                for symbol, bar in bars.items())
            self._collect_series(set(present), final=False)
            self._write(run_id)
        except BaseException as exc:
            self._fail(exc)
            raise

    def heartbeat(self, counts: Mapping[CountSpec, int] | None = None,
                  data_meta: DataMeta | None = None) -> None:
        """Touch the run without data (a driver calls it at least every
        `HEARTBEAT_INTERVAL`, also during an outage).

        With `counts` (as for `finish`) and/or `data_meta` (the full `DataMeta` as it stands,
        gap report included) the run's counts and data record are also refreshed, so a
        dashboard sees them while the run is still running. These are PROVISIONAL: `finish`
        writes the final values over them, and `report --verify` ignores them for a run that
        did not complete. A provisional write is best effort: when the database is busy (or the
        write fails otherwise) it is skipped, counted in `provisional_skipped`, and tried again
        with the next heartbeat; it never fails the writer or the session. The plain touch
        keeps its busy retries and its failure behaviour.
        """
        run_id = self._require_run()
        stamped = self._write_provisional(run_id, counts, data_meta)
        if stamped:
            return  # each provisional write stamps `last_write_at` itself
        try:
            self._retried(lambda: self._store.touch(run_id))
        except BaseException as exc:
            self._fail(exc)
            raise

    @property
    def provisional_skipped(self) -> int:
        """How many provisional writes (counts or data record) a heartbeat gave up on."""
        return self._provisional_skipped

    def _write_provisional(self, run_id: int, counts: Mapping[CountSpec, int] | None,
                           data_meta: DataMeta | None) -> bool:
        """Best-effort provisional counts and data record; True when every part asked for
        was stored (and so the run was stamped). Single attempt: no pause inside a heartbeat."""
        stored = False
        complete = True
        if counts is not None:
            try:
                rows = {count_key(*spec): int(value) for spec, value in counts.items()}
                if rows:
                    self._store.set_counts(run_id, {COUNT_SCOPE_SYMBOL: rows})
                    stored = True
            except Exception:  # noqa: BLE001 - provisional data never fails the run
                self._provisional_skipped += 1
                complete = False
        if data_meta is not None:
            try:
                self._store.update_run_data(run_id, data_meta)
                stored = True
            except Exception:  # noqa: BLE001
                self._provisional_skipped += 1
                complete = False
        return stored and complete

    # --- internals -----------------------------------------------------------

    def _retried(self, call: Callable[[], T], *, retries: int = BUSY_RETRIES) -> T:
        """`call()`, repeated up to `retries` times (pausing `BUSY_PAUSE`) while the store
        reports a busy database; any other error, and the last busy one, propagate."""
        attempt = 0
        while True:
            try:
                return call()
            except StoreBusyError:
                if attempt >= retries:
                    raise
                attempt += 1
                self._sleep(BUSY_PAUSE)

    def _require_run(self) -> int:
        if self._run_id is None:
            raise LiveWriterError("begin() has not been called")
        if self._finished is not None:
            raise LiveWriterError("the run was finished")
        return self._run_id

    def _tracker(self, algo: object) -> SeriesTracker | None:
        if id(algo) in self._broken_trackers:
            return None
        if id(algo) not in self._trackers:
            self._trackers[id(algo)] = SeriesTracker(algo)
        return self._trackers[id(algo)]

    def _collect_series(self, symbols: set[str] | None, *, final: bool) -> None:
        """Move the trackers' new declarations and points into the buffer.

        `symbols` limits it to the algos on those symbols (None: every algo).
        Series are secondary data: an algo whose tracker raises is dropped with a
        warning and never fails the run.
        """
        for algo in self._algos:
            ticker = getattr(algo, "ticker", None)
            if symbols is not None and ticker not in symbols:
                continue
            tracker = self._tracker(algo)
            if tracker is None:
                continue
            try:
                history = self._history_for(algo, ticker) if final else self._history.get(
                    str(ticker), [])
                self._buffer_infos.extend(tracker.declare())
                self._buffer_points.extend(tracker.new_points(history, final=final))
            except Exception as exc:
                self._broken_trackers.add(id(algo))
                print(f"warning: {getattr(algo, 'name', '?')}: indicator series not stored "
                      f"({type(exc).__name__})", file=sys.stderr)

    def _history_for(self, algo: object, ticker: Any) -> list[Any]:
        """The bar times an algo saw. After a step that failed inside the engine its
        algo may have taken the bar already: then the fed bar's time belongs to it."""
        history = self._history.get(str(ticker), [])
        frame = getattr(algo, "AlgoData", None)
        if (self._pending is not None and ticker in self._pending[1]
                and isinstance(frame, pd.DataFrame) and len(frame) == len(history) + 1):
            return [*history, self._pending[0]]
        return history

    def _write(self, run_id: int, *, final: bool = False, retries: int = BUSY_RETRIES) -> None:
        """Send everything not yet stored in one transaction; advance the indexes after."""
        ledger = self._ledger
        orders, fills = ledger.orders, ledger.fills
        trips, equity = ledger.round_trips, ledger.equity_points
        ignored = ledger.ignored_events
        sent = self._sent
        new_equity = equity[sent["equity"]:]
        last_time = new_equity[-1].time if new_equity else self._last_equity_time
        final_points: Sequence[EquityPoint] = ()
        if final and last_time is not None:
            final_points = self._final_points(last_time)
        step = LiveStep(
            order_events=orders[sent["orders"]:], fills=fills[sent["fills"]:],
            round_trips=trips[sent["round_trips"]:], equity=new_equity,
            ignored_events=ignored[sent["ignored"]:],
            series=tuple(self._buffer_infos), series_points=tuple(self._buffer_points),
            bars=tuple(self._buffer_bars), final_equity=final_points)
        # A failed call stored nothing (one transaction) and left the indexes and buffers as
        # they were, so repeating it cannot lose or duplicate a record.
        self._retried(lambda: self._store.write_live_step(run_id, step), retries=retries)
        sent.update(orders=len(orders), fills=len(fills), round_trips=len(trips),
                    equity=len(equity), ignored=len(ignored))
        self._last_equity_time = last_time
        self._buffer_infos, self._buffer_points, self._buffer_bars = [], [], []

    def _final_points(self, time: datetime) -> list[EquityPoint]:
        """One point per account plus the total at `time`, from the ledger as it is now."""
        ledger = self._ledger
        accounts: list[str | None] = [a.idname for a in ledger.accounts]
        accounts.append(None)
        points = []
        for account in accounts:
            cash, equity = ledger.cash(account), ledger.equity(account)
            points.append(EquityPoint(time, account, cash, equity - cash,
                                      ledger.realised_pnl(account),
                                      ledger.unrealised_pnl(account), equity))
        return points

    # --- finish ----------------------------------------------------------------

    def finish(
        self,
        status: EndStatus,
        reason: str | None,
        counts: Mapping[CountSpec, int],
        data_meta: DataMeta,
        gap_rule: GapRuleReport,
    ) -> FinishOutcome:
        """Close the run; call it after `engine.end_live()` on every exit path.

        `counts` maps `(symbol, kind, reason)` to a number, the arguments of
        `live_counts.count_key` (which builds every stored key); it must hold the fed
        count of every symbol of the run (`(symbol, "fed", None)`: a run without them
        cannot be verified, so a missing one is an error of the outcome). `data_meta` is
        the FULL `DataMeta` of the run (it replaces every field stored at `begin`).
        `gap_rule` is the engine's gap report (`engine.gap_report`), REQUIRED: it is
        stored as the run's gap rule whatever `data_meta.gap_rule` says, and a missing
        one raises `LiveWriterError` before anything is written (a forgotten gap rule
        can no longer be lost silently). `status`/`reason` are as for `Store.end_run`.

        Never raises for a store problem. If anything before the end fails, the run
        is ended as `failed` with reason `error` (when the caller said `completed`)
        and the problems are in the outcome; the first one is also `self.error`. A
        second call returns the first outcome.

        Every store call is repeated while the database is busy (`BUSY_RETRIES`,
        `BUSY_PAUSE`). When one part has used up its repeats, the parts after it are
        tried once only, except `end_run`, which gets its full repeats. When `end_run`
        itself cannot write, the outcome has `status` None (the run is still `running`
        in the database), `busy` True, and the error says so.
        """
        if self._finished is not None:
            return self._finished
        if not isinstance(gap_rule, GapRuleReport):
            raise LiveWriterError(
                f"finish() needs the engine's gap report (engine.gap_report), got "
                f"{type(gap_rule).__name__}; without it the run's gap rule would be lost")
        errors: list[str] = []
        if self._error is not None:
            errors.append(_describe(self._error))
        if self._run_id is None:
            self._finished = FinishOutcome(None, None, tuple(errors) + (
                "begin() was never called; there is no run to end",))
            return self._finished
        run_id = self._run_id
        busy = False

        def retries(*, last: bool = False) -> int:
            return BUSY_RETRIES if last or not busy else 0

        absent = [s for s in self._symbols if (s, COUNT_FED, None) not in counts]
        if absent:
            errors.append(f"counts: no fed count for {', '.join(absent)}: the run cannot be "
                          "verified without them")
        # Each part is tried on its own: a failed tail must not stop the run being ended.
        try:
            self._collect_series(None, final=True)
            self._write(run_id, final=True, retries=retries())
        except Exception as exc:
            busy = busy or isinstance(exc, StoreBusyError)
            self._fail(exc)
            errors.append(f"final write: {_describe(exc)}")
        try:
            rows = {count_key(*spec): int(value) for spec, value in counts.items()}
            self._retried(lambda: self._store.set_counts(
                run_id, {COUNT_SCOPE_SYMBOL: rows} if rows else {}), retries=retries())
        except Exception as exc:
            busy = busy or isinstance(exc, StoreBusyError)
            self._fail(exc)
            errors.append(f"counts: {_describe(exc)}")
        try:
            full = replace(data_meta, gap_rule=gap_rule)
            self._retried(lambda: self._store.update_run_data(run_id, full), retries=retries())
        except Exception as exc:
            busy = busy or isinstance(exc, StoreBusyError)
            self._fail(exc)
            errors.append(f"data: {_describe(exc)}")
        end_status: str = status
        end_reason = reason
        if errors and status == "completed":
            end_status, end_reason = "failed", "error"
        ended: str | None = None
        try:
            self._retried(lambda: self._store.end_run(
                run_id, end_status, end_reason), retries=retries(last=True))  # type: ignore[arg-type]
            ended = end_status
        except Exception as exc:
            self._fail(exc)
            if isinstance(exc, StoreBusyError):
                busy = True
                errors.append("end_run: the run could not be closed: the database stayed busy "
                              f"({_describe(exc)}); it is still marked running")
            else:
                errors.append(f"end_run: {_describe(exc)}")
            if not isinstance(exc, StoreBusyError) and (end_status, end_reason) != (
                    "failed", "error"):
                # the caller's reason may be what the store refused: still try to mark it failed
                try:
                    self._retried(lambda: self._store.end_run(run_id, "failed", "error"))
                    ended, end_reason = "failed", "error"
                except Exception as again:
                    busy = busy or isinstance(again, StoreBusyError)
                    errors.append(f"end_run (failed): {_describe(again)}")
        self._finished = FinishOutcome(ended, end_reason if ended else None, tuple(errors), busy)
        return self._finished


# --- the guards of `begin` -----------------------------------------------------------------------


def _begin_problems(
    fills: str,
    config: Mapping[str, Any],
    data: DataMeta,
    algos: Sequence[AlgoMeta],
    warmup: Mapping[str, pd.DataFrame],
    description: Mapping[str, Any] | None,
) -> list[str]:
    """Everything that makes a run unverifiable or wrong at its start, one line each."""
    problems: list[str] = []
    if data.last_bar is not None:
        problems.append("data.last_bar is set (nothing is known at the start: `finish` stores "
                        "the last bar)")
    for warning in data.warnings:
        if _PATH_IN_TEXT.search(warning):
            problems.append(f"a warning contains a path: {warning[:60]!r}")
    entries = config.get("entries")
    if not isinstance(entries, list) or not entries:
        problems.append("config has no `entries` (the algo config the replay is built from)")
    if config.get("capital") is None and not algos:
        problems.append("no capital: config has no `capital` and no algo is given")
    if fills == "realistic":
        costs = config.get("costs")
        needed = {"commission_per_share", "commission_minimum", "slippage_per_share"}
        if not isinstance(costs, Mapping) or not needed <= set(costs):
            problems.append("realistic fills but config has no `costs` "
                            "(commission_per_share, commission_minimum, slippage_per_share)")
    if not data.directory:
        problems.append("data.directory is empty (`report --verify` cannot find the warm-up "
                        "month files without it)")
    if not data.symbols:
        problems.append("data.symbols is empty")
    elif algos and set(data.symbols) != {a.ticker for a in algos}:
        problems.append(f"data.symbols {list(data.symbols)} are not the algos' tickers "
                        f"{sorted({a.ticker for a in algos})}")
    if description is None:
        problems.append("no warm-up description (`live_warmup` returns it): the warm-up "
                        "cannot be rebuilt")
        return problems
    described = description.get("symbols")
    if not isinstance(described, Mapping):
        problems.append("the warm-up description has no `symbols`")
    else:
        for symbol in data.symbols:
            info = described.get(symbol)
            if not isinstance(info, Mapping) or not {"files", "days", "warmup_sha256"} <= set(info):
                problems.append(f"the warm-up description does not cover {symbol} (needs files, "
                                "days and warmup_sha256)")
    problems.extend(_today_bars_problems(description, warmup))
    return problems


def _today_bars_problems(description: Mapping[str, Any],
                         warmup: Mapping[str, pd.DataFrame]) -> list[str]:
    """The `today_bars` of a description must carry their digests and be the end of the
    frames the engine starts with (what the engine saw is what `report --verify` rebuilds)."""
    today = description.get("today_bars")
    if today is None:
        return []
    if not isinstance(today, Mapping):
        return ["`today_bars` of the warm-up description is not a mapping of symbol to rows"]
    stored = description.get("today_sha256")
    digests: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    problems: list[str] = []
    for symbol, rows in today.items():
        if not rows:
            continue
        try:
            digest = warmup_digest(today_frame(rows))
        except ValueError as exc:
            problems.append(f"today_bars of {symbol}: {exc}")
            continue
        if digests.get(symbol) != digest:
            problems.append(f"today_bars of {symbol} have no matching `today_sha256` "
                            "(build both with `describe_today_bars`)")
            continue
        frame = warmup.get(symbol)
        tail = None if frame is None or len(frame) < len(rows) else frame.tail(len(rows))
        if tail is None or warmup_digest(tail.reset_index(drop=True)) != digest:
            problems.append(f"the warm-up frame of {symbol} does not end with the {len(rows)} "
                            "today_bars of its description")
    return problems


__all__ = ["BUSY_PAUSE", "BUSY_RETRIES", "RUN_KIND", "FinishOutcome", "LiveRunWriter",
           "LiveWriterError", "describe_today_bars", "today_frame"]
