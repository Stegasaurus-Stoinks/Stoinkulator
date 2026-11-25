"""The incremental run writer of a live session: the only thing that writes a live run.

`LiveRunWriter` is the `on_step` observer of `Engine.start_live` and stores,
step by step, what a backtest stores at its end, so a dashboard reading the
database sees the run grow and a later comparison finds the same records.

Call sequence of a driver (no clock, thread or sleep in here: "now" is stamped by
the store, and the driver owns the pace):

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
        outcome = writer.finish(status, reason, counts, data_meta)

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

Marker delay (decision of issue 192): every indicator series of an algo is held back
by the tracker's `settle` bars (its `order`: 10 for Elliot, 2 for FunctionTest), so
stop and trade markers of such an algo reach the store that many bars late although
they are never revised. Overwriting and deleting stored points is NOT done here; the
tracker only returns new points. Accepted for the first sessions.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import sys
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from .feed import Bar
from .ledger import EquityPoint, Ledger
from .live_counts import COUNT_SCOPE_SYMBOL, LIVE_KINDS, count_key
from .series import SeriesTracker
from .store import (
    AlgoMeta,
    DataMeta,
    LiveStep,
    RunMeta,
    SeriesInfo,
    SeriesPoint,
    Store,
    StoredBar,
)

RUN_KIND = LIVE_KINDS[0]  # "paper-live"
EndStatus = Literal["completed", "failed"]
CountSpec = tuple[str, str, str | None]  # (symbol, kind, reason) as `count_key` takes them


class LiveWriterError(Exception):
    """The writer was used wrongly (no `fed` before a step, a mismatch, a second `begin`)."""


@dataclass(frozen=True)
class FinishOutcome:
    """What `LiveRunWriter.finish` achieved."""

    status: str | None  # the status the run was ended with; None when it could not be ended
    reason: str | None
    errors: tuple[str, ...]  # one line per problem, the first error ever seen first

    @property
    def ok(self) -> bool:
        return not self.errors and self.status is not None


def _naive(value: Any) -> datetime:
    return value.to_pydatetime() if isinstance(value, pd.Timestamp) else value


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class LiveRunWriter:
    """Writes one live run to a store, step by step. See the module text.

    `ledger` is the engine's ledger (the writer only reads it) and `algos` the
    engine's algos (`engine.algos`), whose indicator series are tracked.
    """

    def __init__(self, store: Store, ledger: Ledger, algos: Sequence[object]) -> None:
        self._store = store
        self._ledger = ledger
        self._algos = tuple(algos)
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
        """
        if self._run_id is not None:
            raise LiveWriterError("begin() was already called")
        stored = dict(config)
        if warmup_description is not None:
            stored["warmup"] = warmup_description
        self._history = {symbol: list(frame["date"].tolist()) for symbol, frame in warmup.items()}
        run_id = self._store.begin_run(RunMeta(kind=RUN_KIND, fills=fills,
                                               code_version=code_version, config=stored,
                                               data=data))
        self._run_id = run_id
        try:
            self._store.write_algos(run_id, list(algos))
            self._store.touch(run_id)  # a new run has no last_write_at
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

    def heartbeat(self) -> None:
        """Touch the run without data (a driver calls it at least every
        `HEARTBEAT_INTERVAL`, also during an outage)."""
        run_id = self._require_run()
        try:
            self._store.touch(run_id)
        except BaseException as exc:
            self._fail(exc)
            raise

    # --- internals -----------------------------------------------------------

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

    def _write(self, run_id: int, *, final: bool = False) -> None:
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
        self._store.write_live_step(run_id, step)
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
    ) -> FinishOutcome:
        """Close the run; call it after `engine.end_live()` on every exit path.

        `counts` maps `(symbol, kind, reason)` to a number, the arguments of
        `live_counts.count_key` (which builds every stored key); `data_meta` is the
        FULL `DataMeta` of the run (it replaces every field stored at `begin` except
        the gap rule, which is stored only if `data_meta.gap_rule` is set: pass
        `engine.gap_report`). `status`/`reason` are as for `Store.end_run`.

        Never raises for a store problem. If anything before the end fails, the run
        is ended as `failed` with reason `error` (when the caller said `completed`)
        and the problems are in the outcome; the first one is also `self.error`. A
        second call returns the first outcome.
        """
        if self._finished is not None:
            return self._finished
        errors: list[str] = []
        if self._error is not None:
            errors.append(_describe(self._error))
        if self._run_id is None:
            self._finished = FinishOutcome(None, None, tuple(errors) + (
                "begin() was never called; there is no run to end",))
            return self._finished
        run_id = self._run_id
        # Each part is tried on its own: a failed tail must not stop the run being ended.
        try:
            self._collect_series(None, final=True)
            self._write(run_id, final=True)
        except Exception as exc:
            self._fail(exc)
            errors.append(f"final write: {_describe(exc)}")
        try:
            rows = {count_key(*spec): int(value) for spec, value in counts.items()}
            self._store.set_counts(run_id, {COUNT_SCOPE_SYMBOL: rows} if rows else {})
        except Exception as exc:
            self._fail(exc)
            errors.append(f"counts: {_describe(exc)}")
        try:
            self._store.update_run_data(run_id, data_meta)
        except Exception as exc:
            self._fail(exc)
            errors.append(f"data: {_describe(exc)}")
        end_status: str = status
        end_reason = reason
        if errors and status == "completed":
            end_status, end_reason = "failed", "error"
        ended: str | None = None
        try:
            self._store.end_run(run_id, end_status, end_reason)  # type: ignore[arg-type]
            ended = end_status
        except Exception as exc:
            self._fail(exc)
            errors.append(f"end_run: {_describe(exc)}")
            if (end_status, end_reason) != ("failed", "error"):
                # the caller's reason may be what the store refused: still try to mark it failed
                try:
                    self._store.end_run(run_id, "failed", "error")
                    ended, end_reason = "failed", "error"
                except Exception as again:
                    errors.append(f"end_run (failed): {_describe(again)}")
        self._finished = FinishOutcome(ended, end_reason if ended else None, tuple(errors))
        return self._finished


__all__ = ["RUN_KIND", "FinishOutcome", "LiveRunWriter", "LiveWriterError"]
