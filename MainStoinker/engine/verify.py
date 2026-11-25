"""Exact parity check of a stored live run (`report RUN --verify`, issue 200).

A live run (kind `paper-live`) stores, step by step, what the engine did with the
bars it was given: the bars themselves (`run_bars`), the orders, fills, round trips
and equity of the ledger, the gap-rule counts and the description of its warm-up.
`verify_run` proves that this record is what the engine does with those inputs:

1. the warm-up is rebuilt from the month files the stored description names (only
   the recorded days) and its digest compared with the stored `warmup_sha256`;
2. warm-up plus the stored bars are replayed through `Engine.start_live` / `step` /
   `end_live` (the API the session used: nothing is closed synthetically) with the
   stored algo config, fill model and costs (the replay is built by the caller,
   `cli.replay_stored_run`, from the pieces `backtest` uses);
3. the replay is compared with the stored run.

What "the same" means (decision of issue 187, correction 9): the same fills, order
events, round trips and ignored events; the same open positions after `end_live`
(account, symbol, side, quantity, entry price and time, and for a cleanly ended run
the stop and target); the same equity at every mark before the last bar time; the
same FINAL equity (the ledger's state after the session end, per algo and in total,
against the last stored equity group); the same gap-rule counts; the stored `algos`
table against the config the replay was built from; for a completed run the stored
bars against the fed counts, `replayed_minutes` and `last_bar` (a completed run with
no fed counts is UNVERIFIABLE: "bar counts not recorded"). It is measured on the
bars the run consumed (`run_bars`), not on any file. Volumes come back from the store
as floats: they go back into integers (they were integers); quantities and prices
are compared as the ledger's own 4-decimal values.

Three outcomes, each its own exit code (`EXIT_*`):

- VERIFIED (0): everything compared equal;
- MISMATCH (1): at least one difference; the first few per record kind are listed;
- UNVERIFIABLE (5): the replay could not be built or its input is no longer the run's:
  a warm-up month file is missing or its digest differs from the stored one (the bars
  on disk changed since the run, for example a provisional day was refetched), the run
  stores no warm-up description, or its config cannot be rebuilt. This says nothing
  about the engine.

A run that is not a live run with stored bars is refused (`VerifyRefused`, exit 4). A
missing bars directory is a usage error (`VerifyUsageError`, exit 2).

A run that did not complete (`failed`, or still `running` after a crash) is replayed up
to its stored bars. The step that failed stored no bars, so ledger records stamped after
the last stored bar time are left out on both sides (and counted in a note); the verdict
says the run ended early and why (`end_reason`).

Read-only: the store is only read. Nothing here opens a file for writing, a socket, or
imports anything outside `MainStoinker.engine`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from .engine import LivePosition
from .feed import COLUMNS, Bar, BarFeed, read_offline_bars
from .gaprule import GapRuleReport
from .ledger import EquityPoint, Ledger
from .live_counts import COUNT_FED, COUNT_SCOPE_SYMBOL, parse_count_key
from .run_documents import is_live_run, open_positions
from .stats import fmt4
from .store import AlgoMeta, DataMeta, RunData, Store, StoredBar, expand_equity
from .warmup import _MONTH_FILE, warmup_digest

EXIT_VERIFIED = 0
EXIT_MISMATCH = 1
EXIT_REFUSED = 4
EXIT_UNVERIFIABLE = 5
SHOWN = 5  # differences listed per record kind

Outcome = Literal["verified", "mismatch", "unverifiable"]
_NEW_YORK = "America/New_York"


class VerifyRefused(Exception):
    """The run is not a live run with stored bars: there is nothing to verify (exit 4)."""


class VerifyUsageError(Exception):
    """The command line is wrong (no bars directory, not a directory); exit 2."""


class VerifyUnavailable(Exception):
    """Raised by a replayer when the stored run cannot be rebuilt (becomes UNVERIFIABLE)."""


@dataclass(frozen=True)
class Replayed:
    """What a replay produced: its ledger and the gap rule's report."""

    ledger: Ledger
    gap_report: GapRuleReport
    positions: tuple[LivePosition, ...]  # what the session end left open
    algos: tuple[AlgoMeta, ...]  # the algo records the replay's config implies


Replayer = Callable[[RunData, BarFeed], Replayed]


@dataclass(frozen=True)
class Difference:
    """One field of one record that differs. `index` is the record's place in its list."""

    index: int | None
    time: str | None
    field: str
    stored: str
    replayed: str

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "time": self.time, "field": self.field,
                "stored": self.stored, "replayed": self.replayed}


@dataclass(frozen=True)
class KindDifferences:
    """Everything that differs in one kind of record: how many records, and the first few."""

    kind: str
    count: int
    first: tuple[Difference, ...]


@dataclass(frozen=True)
class Problem:
    """Why a run is unverifiable. `expected` / `actual` are digests when it is about one."""

    reason: str
    symbol: str | None = None
    file: str | None = None
    expected: str | None = None
    actual: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "file": self.file, "reason": self.reason,
                "expected": self.expected, "actual": self.actual}

    def line(self) -> str:
        where = " ".join(part for part in (self.symbol, self.file) if part)
        text = f"{where}: {self.reason}" if where else self.reason
        if self.expected is not None or self.actual is not None:
            text += f" (expected {self.expected}, actual {self.actual})"
        return text


@dataclass(frozen=True)
class VerifyResult:
    """The verdict. `compared` counts what was compared (empty when unverifiable)."""

    outcome: Outcome
    run_id: int
    status: str
    end_reason: str | None
    compared: Mapping[str, int]
    differences: tuple[KindDifferences, ...] = ()
    problems: tuple[Problem, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return {"verified": EXIT_VERIFIED, "mismatch": EXIT_MISMATCH,
                "unverifiable": EXIT_UNVERIFIABLE}[self.outcome]

    @property
    def ended_early(self) -> bool:
        return self.status != "completed"

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "run_ended_early": self.ended_early,
            "status": self.status,
            "end_reason": self.end_reason,
            "compared": dict(self.compared),
            "differences": [
                {"kind": d.kind, "count": d.count, "first": [x.to_json() for x in d.first]}
                for d in self.differences],
            "unverifiable": [p.to_json() for p in self.problems],
            "notes": list(self.notes),
        }

    def lines(self) -> list[str]:
        """The verdict as text lines (the `report` command prints them under its table)."""
        c = self.compared
        if self.outcome == "verified":
            head = (f"verify: VERIFIED ({c['fills']} fills, {c['round_trips']} round trips, "
                    f"{c['open_positions']} open positions compared; {c['bars']} bars replayed)")
        elif self.outcome == "mismatch":
            head = (f"verify: MISMATCH in {len(self.differences)} kind(s) of record "
                    f"({c['fills']} fills, {c['round_trips']} round trips, "
                    f"{c['open_positions']} open positions compared; {c['bars']} bars replayed)")
        else:
            head = ("verify: UNVERIFIABLE (the run can be neither proven nor disproven: the bars "
                    "on disk changed since the run, or the run's record is incomplete; this "
                    "says nothing about the engine)")
        out = [head]
        if self.ended_early:
            reason = self.end_reason or "no reason stored"
            out.append(f"  the run ended early (status {self.status}, end_reason {reason}); "
                       f"replayed up to its stored bars")
        for problem in self.problems:
            out.append(f"  {problem.line()}")
        for diff in self.differences:
            out.append(f"  {diff.kind}: {diff.count} differ" + (
                f" (first {len(diff.first)} shown)" if diff.count > len(diff.first) else ""))
            for x in diff.first:
                where = "" if x.index is None else f"[{x.index}]"
                when = "" if x.time is None else f" at {x.time}"
                out.append(f"    {diff.kind}{where}{when} {x.field}: stored {x.stored}, "
                           f"replayed {x.replayed}")
        out.extend(f"  note: {note}" for note in self.notes)
        return out


# --- rebuilding the warm-up ----------------------------------------------------------


def _month_paths(bars_dir: Path, symbol: str, names: Sequence[str]) -> list[Path]:
    """Path of each named file: a month file is in the symbol's folder, the legacy file is not."""
    return [bars_dir / symbol / name if _MONTH_FILE.fullmatch(name) else bars_dir / name
            for name in names]


def rebuild_warmup(
    bars_dir: Path, description: Mapping[str, Any], symbols: Sequence[str]
) -> tuple[dict[str, pd.DataFrame], list[Problem]]:
    """The warm-up frames of `symbols` from the stored description, and what is wrong with them.

    Per symbol: reads exactly the named files, keeps exactly the recorded days and compares
    `warmup_digest` with the stored `warmup_sha256`. A missing or unreadable file, a recorded
    day the files do not hold, a digest that differs: a `Problem` naming symbol and file (and
    the expected and actual digest). The returned frames are those of the symbols without a
    problem.
    """
    described = description.get("symbols") if isinstance(description, Mapping) else None
    if not isinstance(described, Mapping):
        return {}, [Problem("the stored warm-up description has no symbols")]
    frames: dict[str, pd.DataFrame] = {}
    problems: list[Problem] = []
    for symbol in symbols:
        info = described.get(symbol)
        if not isinstance(info, Mapping) or not {"files", "days", "warmup_sha256"} <= set(info):
            problems.append(Problem("the stored warm-up description does not cover this symbol "
                                    "(needs files, days and warmup_sha256)", symbol))
            continue
        expected = str(info["warmup_sha256"])
        names = [str(n) for n in info["files"]]
        paths = _month_paths(bars_dir, symbol, names)
        parts: list[pd.DataFrame] = []
        broken = False
        for name, path in zip(names, paths, strict=True):
            if not path.is_file():
                problems.append(Problem(f"warm-up file is missing in {bars_dir}", symbol, name,
                                        expected, None))
                broken = True
                continue
            try:
                parts.append(read_offline_bars(path))
            except (OSError, ValueError) as exc:
                problems.append(Problem(f"warm-up file cannot be read: {exc}", symbol, name,
                                        expected, None))
                broken = True
        if broken:
            continue
        if not parts:
            problems.append(Problem("the stored warm-up description names no file", symbol))
            continue
        data = pd.concat(parts, ignore_index=True)[list(COLUMNS)]
        try:
            wanted = pd.to_datetime(list(info["days"]))
        except (ValueError, TypeError) as exc:
            problems.append(Problem(f"the stored warm-up days cannot be read: {exc}", symbol))
            continue
        days = data["date"].dt.normalize()
        frame = data.loc[days.isin(wanted)].reset_index(drop=True)
        absent = sorted(set(wanted) - set(frame["date"].dt.normalize()))
        if absent:
            problems.append(Problem(
                "recorded warm-up day(s) not in the files: "
                + ", ".join(day.date().isoformat() for day in absent),
                symbol, ", ".join(names), expected, None))
            continue
        try:
            actual = warmup_digest(frame)
        except ValueError as exc:
            problems.append(Problem(f"the warm-up cannot be digested: {exc}", symbol,
                                    ", ".join(names), expected, None))
            continue
        if actual != expected:
            problems.append(Problem("warm-up digest differs: the bars on disk are not those the "
                                    "run used", symbol, ", ".join(names), expected, actual))
            continue
        frames[symbol] = frame
    return frames, problems


# --- replaying the stored bars ---------------------------------------------------------


class StoredBarsFeed:
    """A feed of the bars a live run stored, after a warm-up (a `BarFeed`).

    Volumes were integers and come back from the store as floats: they are rounded back.
    The bar's epoch `time` is derived from its New York wall-clock date.
    """

    def __init__(self, warmups: Mapping[str, pd.DataFrame], bars: Sequence[StoredBar]) -> None:
        self._warmups = dict(warmups)
        self._symbols = list(warmups)
        self._bars = list(bars)

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def warmup(self, symbol: str) -> pd.DataFrame:
        return self._warmups[symbol].copy()

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        by_time: dict[datetime, dict[str, Bar]] = {}
        for stored in self._bars:
            date = pd.Timestamp(stored.time)
            epoch = int(date.tz_localize(_NEW_YORK).timestamp())
            by_time.setdefault(stored.time, {})[stored.symbol] = Bar(
                date, epoch, stored.open, stored.high, stored.low, stored.close,
                int(round(stored.volume)))
        for stamp in sorted(by_time):
            batch = by_time[stamp]
            yield pd.Timestamp(stamp), {s: batch[s] for s in self._symbols if s in batch}


# --- comparing -----------------------------------------------------------------------------


def _show(value: Any) -> str:
    if isinstance(value, Decimal):
        return fmt4(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def _same(a: Any, b: Any) -> bool:
    return bool(a == b)


def _compare_records(
    kind: str, stored: Sequence[Any], replayed: Sequence[Any], when: Callable[[Any], Any],
    fields: Sequence[str] | None = None,
) -> KindDifferences | None:
    """Compare two lists of records index by index; None when they are equal."""
    differing = 0
    shown: list[Difference] = []

    def add(item: Difference) -> None:
        if len(shown) < SHOWN:
            shown.append(item)

    for index in range(max(len(stored), len(replayed))):
        left = stored[index] if index < len(stored) else None
        right = replayed[index] if index < len(replayed) else None
        if left is None or right is None:
            differing += 1
            record = right if left is None else left
            add(Difference(index, _show(when(record)), "record",
                           "(none)" if left is None else "present",
                           "(none)" if right is None else "present"))
            continue
        names = fields or [f.name for f in dataclasses.fields(left)]
        bad = [n for n in names if not _same(getattr(left, n), getattr(right, n))]
        if bad:
            differing += 1
            for name in bad:
                add(Difference(index, _show(when(left)), name, _show(getattr(left, name)),
                               _show(getattr(right, name))))
    return KindDifferences(kind, differing, tuple(shown)) if differing else None


def _position_record(account: str, symbol: str, side: str, quantity: Any, price: Any,
                     entry_time: Any, stop: Any = None, target: Any = None, *,
                     levels: bool = False) -> dict[str, str]:
    record = {"account": account, "symbol": symbol, "side": side, "quantity": _show(quantity),
              "entry_price": _show(price),
              "entry_time": "-" if entry_time is None else _show(entry_time)}
    if levels:
        record["stop"] = "-" if stop is None else _show(stop)
        record["target"] = "-" if target is None else _show(target)
    return record


def _compare_mappings(kind: str, stored: Sequence[Mapping[str, str]],
                      replayed: Sequence[Mapping[str, str]]) -> KindDifferences | None:
    differing = 0
    shown: list[Difference] = []
    for index in range(max(len(stored), len(replayed))):
        left = stored[index] if index < len(stored) else None
        right = replayed[index] if index < len(replayed) else None
        record = left or right or {}
        when = record.get("entry_time")
        if left is None or right is None:
            differing += 1
            if len(shown) < SHOWN:
                shown.append(Difference(index, when, "position",
                                        "(none)" if left is None else
                                        f"{left['account']} {left['symbol']} {left['side']} "
                                        f"{left['quantity']}",
                                        "(none)" if right is None else
                                        f"{right['account']} {right['symbol']} {right['side']} "
                                        f"{right['quantity']}"))
            continue
        bad = [k for k in left if left[k] != right[k]]
        if bad:
            differing += 1
            for name in bad:
                if len(shown) < SHOWN:
                    shown.append(Difference(index, when, name, left[name], right[name]))
    return KindDifferences(kind, differing, tuple(shown)) if differing else None


def _compare_equity(run: RunData, ledger: Ledger, cutoff: datetime
                    ) -> tuple[KindDifferences | None, int]:
    """Equity at every mark before `cutoff`, per account and in total; the number compared."""
    stored_all: list[Any] = []
    replayed_all: list[Any] = []
    accounts: list[str | None] = [a.idname for a in ledger.accounts]
    accounts.append(None)
    for account in accounts:
        mine = [p for p in ledger.equity_points if p.account == account and p.time < cutoff]
        times = [p.time for p in mine]
        stored_all.extend(expand_equity(run.equity_series(account), times))
        replayed_all.extend(mine)
    found = _compare_records("equity", stored_all, replayed_all, lambda p: p.time)
    return found, len(replayed_all)


def _compare_gap_rule(stored: GapRuleReport | None, replayed: GapRuleReport, *, completed: bool
                      ) -> tuple[KindDifferences | None, bool]:
    """Compare the gap-rule counts; the flag says whether they were compared."""
    if stored is None:
        if not completed:
            return None, False  # only written when the run finishes
        return KindDifferences("gap_rule", 1, (Difference(
            None, None, "record", "(not stored)", "stored by the run's end"),)), False
    shown: list[Difference] = []
    differing = 0

    def check(field: str, left: Any, right: Any) -> None:
        nonlocal differing
        if left != right:
            differing += 1
            if len(shown) < SHOWN:
                shown.append(Difference(None, None, field, str(left), str(right)))

    stored_after, replay_after = dict(stored.bars_after_gap), dict(replayed.bars_after_gap)
    for symbol in dict.fromkeys([*stored_after, *replay_after]):
        check(f"bars_after_gap[{symbol}]", stored_after.get(symbol), replay_after.get(symbol))
    stored_ref = {(e.algo, e.symbol): (e.count, e.times) for e in stored.suppressed}
    replay_ref = {(e.algo, e.symbol): (e.count, e.times) for e in replayed.suppressed}
    for key in dict.fromkeys([*stored_ref, *replay_ref]):
        check(f"suppressed_entries[{key[0]} {key[1]}] (count, times)", stored_ref.get(key),
              replay_ref.get(key))
    return (KindDifferences("gap_rule", differing, tuple(shown)) if differing else None), True


def _upto(items: Sequence[Any], when: Callable[[Any], Any], cutoff: datetime) -> list[Any]:
    return [i for i in items if when(i) <= cutoff]


def compare_runs(run: RunData, bars: Mapping[str, Sequence[StoredBar]],
                 counts: Mapping[str, Mapping[str, int]], replayed: Replayed
                 ) -> VerifyResult:
    """Compare a stored live run with its replay; see the module text for what counts."""
    summary = run.summary
    completed = summary.status == "completed"
    last_time = max(b.time for rows in bars.values() for b in rows)
    ledger = replayed.ledger
    notes: list[str] = []
    stored_fills, stored_orders = list(run.fills), list(run.order_events)
    stored_trips, stored_ignored = list(run.round_trips), list(run.ignored_events)
    if not completed:
        # The step that failed stored no bar: what the ledger holds after the last stored
        # bar belongs to it (or to the end of the session) and has nothing to replay from.
        cut = (len(stored_fills) - len(_upto(stored_fills, lambda f: f.time, last_time))
               + len(stored_orders) - len(_upto(stored_orders, lambda e: e.time, last_time))
               + len(stored_trips) - len(_upto(stored_trips, lambda t: t.exit_time, last_time))
               + len(stored_ignored) - len(_upto(stored_ignored, lambda e: e.time, last_time)))
        stored_fills = _upto(stored_fills, lambda f: f.time, last_time)
        stored_orders = _upto(stored_orders, lambda e: e.time, last_time)
        stored_trips = _upto(stored_trips, lambda t: t.exit_time, last_time)
        stored_ignored = _upto(stored_ignored, lambda e: e.time, last_time)
        if cut:
            notes.append(f"{cut} stored record(s) after the last stored bar ({last_time}) were "
                         f"not compared: they belong to the step that did not finish")
    replay_fills = _upto([f for f in ledger.fills if not f.synthetic], lambda f: f.time, last_time)
    replay_orders = _upto(list(ledger.orders), lambda e: e.time, last_time)
    replay_trips = _upto([t for t in ledger.round_trips if not t.synthetic],
                         lambda t: t.exit_time, last_time)
    replay_ignored = _upto(list(ledger.ignored_events), lambda e: e.time, last_time)

    found: list[KindDifferences | None] = [
        _compare_records("fills", stored_fills, replay_fills, lambda f: f.time),
        _compare_records("order_events", stored_orders, replay_orders, lambda e: e.time),
        _compare_records("round_trips", stored_trips, replay_trips, lambda t: t.exit_time),
        _compare_records("ignored_events", stored_ignored, replay_ignored, lambda e: e.time),
    ]
    # Open positions: what the replay's `end_live` leaves open against what the stored fills
    # leave open. The protective levels are compared only for a run that ended cleanly: an
    # early-ended run's session end ran on a bar the store never got.
    held = open_positions(dataclasses.replace(run, fills=tuple(stored_fills),
                                              order_events=tuple(stored_orders)))
    levels = completed
    stored_open = sorted(
        (_position_record(p["account"], p["symbol"], p["side"], p["quantity"], p["entry_price"],
                          None if p["entry_time"] is None else
                          datetime.fromisoformat(p["entry_time"]),
                          p["stop"], p["target"], levels=levels)
         for p in held),
        key=lambda r: (r["account"], r["symbol"], r["entry_time"], r["quantity"]))
    replay_open = sorted(
        (_position_record(p.account, p.symbol, p.side, p.quantity, p.entry_price, p.entry_time,
                          p.stop, p.target, levels=levels)
         for p in replayed.positions),
        key=lambda r: (r["account"], r["symbol"], r["entry_time"], r["quantity"]))
    found.append(_compare_mappings("open_positions", stored_open, replay_open))
    equity_found, equity_compared = _compare_equity(run, ledger, last_time)
    found.append(equity_found)
    # The headline result: the ledger's final state after the session end, against the last
    # equity group the run stored. Not compared when the run did not finish between steps.
    final_compared = 0
    if summary.status == "running" or (not completed and summary.end_reason == "error"):
        notes.append("the final equity was not compared: the run did not end between steps")
    else:
        stored_final, replay_final = _final_points(run, ledger, last_time)
        found.append(_compare_records("final_equity", stored_final, replay_final,
                                      lambda p: p.time))
        final_compared = len(replay_final)
    # The algos table (what the dashboard shows) against the config the replay was built from.
    found.append(_compare_records("algos", list(run.algos), list(replayed.algos),
                                  lambda a: a.idname))
    gap_found, gap_compared = _compare_gap_rule(summary.data.gap_rule, replayed.gap_report,
                                                completed=completed)
    found.append(gap_found)
    if not gap_compared and not gap_found:
        notes.append("the run stored no gap-rule counts yet (they are written when it finishes); "
                     "not compared")
    # The bars against what the run says about them.
    fed = {key.symbol: value for scope, rows in counts.items() for name, value in rows.items()
           if (key := parse_count_key(scope, name)) is not None and key.kind == COUNT_FED
           and scope == COUNT_SCOPE_SYMBOL}
    data = summary.data
    no_counts = False
    if completed:
        if fed:
            names = list(dict.fromkeys([*data.symbols, *fed]))
            found.append(_compare_records(
                "bars", [_Count(s, len(bars.get(s, ()))) for s in names],
                [_Count(s, fed.get(s)) for s in names], lambda c: last_time, ["fed"]))
        else:
            no_counts = True
        found.append(_compare_data(data, bars, last_time))
    else:
        notes.append("the fed counts and the stored bar range were not compared: the run did "
                     "not complete")
    differences = tuple(d for d in found if d is not None)
    compared = {
        "fills": len(stored_fills), "order_events": len(stored_orders),
        "round_trips": len(stored_trips), "ignored_events": len(stored_ignored),
        "open_positions": len(stored_open), "equity_points": equity_compared,
        "final_equity": final_compared,
        "bars": sum(len(rows) for rows in bars.values()),
    }
    if not differences and no_counts:
        # Decision: a completed run that stored no fed counts cannot show that its stored bars
        # are complete, so it is reported unverifiable (never skipped silently).
        problem = Problem("bar counts not recorded: the run stored no fed counts, so its stored "
                          "bars cannot be checked for completeness (the driver passed none to "
                          "`finish`)")
        return VerifyResult("unverifiable", summary.run_id, summary.status, summary.end_reason,
                            compared, (), (problem,), tuple(notes))
    return VerifyResult("mismatch" if differences else "verified", summary.run_id,
                        summary.status, summary.end_reason, compared, differences, (),
                        tuple(notes))


def _final_points(run: RunData, ledger: Ledger, time: datetime
                  ) -> tuple[list[EquityPoint], list[EquityPoint]]:
    """The last stored point of each account and of the total, and the replay's final state."""
    accounts: list[str | None] = [a.idname for a in ledger.accounts]
    accounts.append(None)
    stored: list[EquityPoint] = []
    replayed: list[EquityPoint] = []
    for account in accounts:
        series = run.equity_series(account)
        if series:
            stored.append(series[-1])
        cash, equity = ledger.cash(account), ledger.equity(account)
        replayed.append(EquityPoint(time, account, cash, equity - cash,
                                    ledger.realised_pnl(account),
                                    ledger.unrealised_pnl(account), equity))
    return stored, replayed


def _compare_data(data: DataMeta, bars: Mapping[str, Sequence[StoredBar]], last_time: datetime
                  ) -> KindDifferences | None:
    """`replayed_minutes` and `last_bar` of the stored data record against the stored bars."""
    shown: list[Difference] = []
    stamps = {b.time for rows in bars.values() for b in rows}
    for name, stored, actual in (("replayed_minutes", data.replayed_minutes, len(stamps)),
                                 ("last_bar", data.last_bar, last_time)):
        if stored is not None and stored != actual:
            shown.append(Difference(None, None, name, _show(stored), _show(actual)))
    return KindDifferences("data", len(shown), tuple(shown)) if shown else None


@dataclass(frozen=True)
class _Count:
    symbol: str
    fed: int | None


# --- the check ---------------------------------------------------------------------------


def _unverifiable(run: RunData, problems: Sequence[Problem]) -> VerifyResult:
    s = run.summary
    return VerifyResult("unverifiable", s.run_id, s.status, s.end_reason, {}, (), tuple(problems))


def verify_run(store: Store, run: RunData, *, bars_dir: Path | None,
               replayer: Replayer) -> VerifyResult:
    """Verify one stored live run; reads `store`, never writes.

    `bars_dir` is the directory the warm-up month files are read from (the caller passes
    the `--bars` option, else the directory recorded with the run, else None).
    `replayer(run, feed)` replays the feed and returns the ledger and gap report; it
    raises `VerifyUnavailable` when the stored config cannot be rebuilt.

    Raises `VerifyRefused` for a run that is not a live run or has no stored bars,
    `VerifyUsageError` when no bars directory is known.
    """
    summary = run.summary
    if not is_live_run(summary):
        raise VerifyRefused(f"run {summary.run_id} is a {summary.kind} run: it stores no bars, "
                            f"so there is nothing to verify (only live runs can be verified)")
    tickers = list(dict.fromkeys(a.ticker for a in run.algos))
    # The feed's symbol order is the run's own (`data.symbols`), not the algos' order.
    symbols = list(summary.data.symbols) or tickers
    if set(symbols) != set(tickers):
        return _unverifiable(run, [Problem(
            f"the stored symbols {symbols} are not the algos' symbols {tickers}")])
    bars = {s: store.load_bars(summary.run_id, s) for s in symbols}
    if not any(bars.values()):
        raise VerifyRefused(f"run {summary.run_id} has no stored bars (kind {summary.kind}, "
                            f"status {summary.status}): there is nothing to replay")
    if bars_dir is None:
        recorded = summary.data.directory
        if not recorded:
            raise VerifyUsageError("this run records no bars directory: give --bars DIR (the "
                                   "directory holding the warm-up month files)")
        bars_dir = Path(recorded)
    description = run.config.get("warmup")
    if not isinstance(description, Mapping):
        return _unverifiable(run, [Problem("the run stores no warm-up description "
                                           "(config \"warmup\"); its warm-up cannot be rebuilt")])
    frames, problems = rebuild_warmup(bars_dir, description, symbols)
    if problems:
        return _unverifiable(run, problems)
    feed = StoredBarsFeed({s: frames[s] for s in symbols},
                          sorted((b for rows in bars.values() for b in rows),
                                 key=lambda b: (b.time, b.symbol)))
    try:
        replayed = replayer(run, feed)
    except VerifyUnavailable as exc:
        return _unverifiable(run, [Problem(str(exc))])
    counts = store.load_counts(summary.run_id)
    return compare_runs(run, bars, counts, replayed)


__all__ = [
    "EXIT_MISMATCH", "EXIT_REFUSED", "EXIT_UNVERIFIABLE", "EXIT_VERIFIED", "SHOWN",
    "Difference", "KindDifferences", "Problem", "Replayed", "Replayer", "StoredBarsFeed",
    "VerifyRefused", "VerifyResult", "VerifyUnavailable", "VerifyUsageError", "compare_runs",
    "rebuild_warmup", "verify_run",
]
