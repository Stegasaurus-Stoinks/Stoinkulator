# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""The gap rule (issue 175): no new position on a bar that follows a gap.

Owner decision, 2026-11-24. Exits still work; every refusal and every bar that
follows a gap is counted and recorded with the run. The rule is the engine's
(`engine/gaprule.py`), so these tests drive a scripted algo through the real
`Engine` with both `SimBroker` and `PaperBroker` and never touch an algo.

Synthetic one-minute bars of one session, 2024-01-04, after three warm-up bars
on 2024-01-02 (see `write_ohlc`). The session's normal bar is price 10.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
import MainStoinker.engine.gaprule as gaprule
from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.cli import main
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import COLUMNS, Bar, CsvBarFeed
from MainStoinker.engine.gaprule import (
    GapGuardBroker,
    GapRule,
    GapRuleReport,
    RefusedEntry,
    SuppressedEntries,
    follows_gap,
)
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.report import trade_records
from MainStoinker.engine.router import MappingRouter
from MainStoinker.engine.sink import NullSink
from MainStoinker.engine.store import (
    DataMeta,
    NullStore,
    RunMeta,
    SqliteStore,
    gap_rule_from_json,
    gap_rule_to_json,
    open_store,
)
from tests.api_support import NO_ENV, make_client
from tests.run_support import Env
from tests.test_algos_minute_spacing import ema_rows, fvg_rows
from tests.test_api_static import FAKE_DOM, run_node
from tests.test_api_static import node_workdir as node_workdir  # noqa: F401  (fixture)
from tests.test_opening_range_algos import ALGO_IDS, D1, D2, day, first_candle, quiet

FREE = CostModel(Decimal(0), Decimal(0), Decimal(0))
CAPITAL = 10_000
DAY = "2024-01-04"
VERSION = "0123456789abcdef0123456789abcdef01234567"
KINDS = ["sim", "paper"]
Row = tuple[pd.Timestamp, float, float, float, float]


def at(hhmm: str, day_: str = DAY) -> pd.Timestamp:
    return pd.Timestamp(f"{day_} {hhmm}")


def normal(hhmm: str, day_: str = DAY) -> Row:
    return (at(hhmm, day_), 10.0, 10.05, 9.95, 10.0)


def quiet_rows(*hhmm: str, day_: str = DAY) -> list[Row]:
    return [normal(t, day_) for t in hhmm]


def write_ohlc(directory: Path, symbol: str, rows: Sequence[Row]) -> None:
    """A symbol file: three warm-up bars on 2024-01-02, then `rows` (replay bars)."""
    warm = [(pd.Timestamp("2024-01-02 09:30") + pd.Timedelta(minutes=i), 10.0, 10.5, 9.5, 10.0)
            for i in range(3)]
    lines = ["x,date,time,open,high,low,close,volume"]
    for i, (stamp, o, h, low, c) in enumerate([*warm, *rows]):
        lines.append(f"{i},{stamp},{int(stamp.timestamp())},{o},{h},{low},{c},100")
    (directory / f"_{symbol}_offlinedata_").write_text("\n".join(lines) + "\n")


class Probe:
    """A scripted algo: `plan` maps the update number (1 = first replay bar) to a function."""

    def __init__(self, name: str, ticker: str,
                 plan: dict[int, Callable[[Probe, pd.Series], None]]) -> None:
        self.name, self.ticker, self.plan = name, ticker, plan
        self.n = 0
        self.broker: object = None
        self.refs: list = []
        self.times: list[pd.Timestamp] = []

    def update(self, frame: pd.DataFrame) -> None:
        self.n += 1
        row = frame.iloc[-1]
        self.times.append(row["date"])
        step = self.plan.get(self.n)
        if step is not None:
            step(self, row)

    def update_frontend(self) -> dict:
        return {}

    def open(self, row: pd.Series, *, direction: int = 1, stop: float | None = None) -> object:
        close = float(row["close"])
        ref = self.broker.open_position(  # type: ignore[attr-defined]
            symbol=self.ticker, volume=10, direction=direction, price=close, time=row["date"],
            limit=False, stop_price=close - 50 if stop is None else stop, oca_group="g")
        self.refs.append(ref)
        return ref

    def close(self, ref: object, row: pd.Series) -> None:
        self.broker.close_position(ref, price=float(row["close"]), time=row["date"])  # type: ignore[attr-defined]


def opener(**kwargs: float) -> Callable[[Probe, pd.Series], None]:
    return lambda algo, row: algo.open(row, **kwargs)  # type: ignore[arg-type]


@dataclass
class Result:
    engine: Engine
    ledger: Ledger
    brokers: dict[str, object]

    @property
    def report(self) -> GapRuleReport:
        return self.engine.gap_report

    @property
    def entry_requests(self) -> list[pd.Timestamp]:
        """Times of the entry orders that reached the ledger (what the broker accepted)."""
        return [o.time for o in self.ledger.orders
                if o.kind == "placed" and o.order_type in ("market", "entry")]


def run(tmp_path: Path, bars: dict[str, Sequence[Row]], algos: Sequence[object],
        kind: str = "paper") -> Result:
    for symbol, rows in bars.items():
        write_ohlc(tmp_path, symbol, rows)
    feed = CsvBarFeed(tmp_path, list(bars), warmup_days=1)
    ledger = Ledger()
    brokers: dict[str, object] = {}
    for algo in algos:
        account = ledger.open_account(algo.name, algo.ticker, CAPITAL)  # type: ignore[attr-defined]
        brokers[algo.name] = (  # type: ignore[attr-defined]
            PaperBroker(account, FREE) if kind == "paper" else SimBroker(account=account))
    engine = Engine(EngineConfig(), feed, MappingRouter(brokers), NullSink(), list(algos),  # type: ignore[arg-type]
                    ledger=ledger, end_commission=lambda idname, quantity: 0)
    engine.run_backtest()
    return Result(engine, ledger, brokers)


# 09:33 is missing: 09:34 follows a gap (update 4 of the algo)
GAPPED = ("09:30", "09:31", "09:32", "09:34", "09:35", "09:36")


# --------------------------------------------------------------- the gap test


@pytest.mark.parametrize("previous, current, expected", [
    (None, "2024-01-04 09:30", False),               # first bar of the data, at the open
    (None, "2024-01-04 09:31", True),                 # first bar of the data, not at the open
    ("2024-01-04 09:30", "2024-01-04 09:31", False),  # exactly one minute
    ("2024-01-04 09:30", "2024-01-04 09:32", True),   # one minute missing
    ("2024-01-04 09:30", "2024-01-04 09:30", True),   # a repeated timestamp is not one minute later
    ("2024-01-04 09:30", "2024-01-04 09:29", True),   # out of order
    ("2024-01-04 15:59", "2024-01-05 09:30", False),  # a new session opening at 09:30
    ("2024-01-04 15:59", "2024-01-05 09:31", True),   # a new session not opening at 09:30
    ("2024-01-05 15:59", "2024-01-08 09:30", False),  # over a weekend
    ("2024-01-05 15:59", "2024-01-08 09:45", True),
    ("2024-01-04 12:58", "2024-01-04 12:59", False),  # a half day needs no special case
    ("2024-11-29 12:59", "2024-12-02 09:30", False),
    ("2024-01-04 09:59", "2024-01-04 10:00", False),  # across the hour
    ("2024-01-04 09:30:00", "2024-01-04 09:31:30", True),
])
def test_follows_gap_cases(previous: str | None, current: str, expected: bool) -> None:
    prev = None if previous is None else datetime.fromisoformat(previous)
    assert follows_gap(prev, datetime.fromisoformat(current)) is expected


def test_the_last_warmup_bar_is_the_previous_bar_of_the_first_replay_bar() -> None:
    rule = GapRule({"A": None})
    rule.start({"A": datetime(2024, 1, 4, 9, 29), "B": None})
    assert rule.on_bar("A", datetime(2024, 1, 4, 9, 30)) is False  # one minute after the warm-up
    assert rule.on_bar("B", datetime(2024, 1, 4, 9, 30)) is False
    rule.start({"A": datetime(2024, 1, 4, 9, 28)})
    assert rule.on_bar("A", datetime(2024, 1, 4, 9, 30)) is True  # a minute missing after it
    assert rule.report().bars_after_gap == (("A", 1),)  # a new start resets the count


# ------------------------------------------------------ the rule, both brokers


@pytest.mark.parametrize("kind", KINDS)
def test_entry_when_flat_on_a_gap_bar_is_refused(tmp_path: Path, kind: str) -> None:
    algo = Probe("a", "AAA", {4: opener()})
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], kind)
    assert result.entry_requests == []
    assert len(result.ledger.fills) == 0
    assert [type(ref) for ref in algo.refs] == [RefusedEntry]
    assert result.report.bars_after_gap == (("AAA", 1),)
    assert result.report.suppressed == (
        SuppressedEntries("a", "AAA", 1, (at("09:34"),)),)
    if kind == "sim":
        assert [c.method for c in result.brokers["a"].calls] == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", KINDS)
def test_the_bar_after_the_gap_bar_is_normal_again(tmp_path: Path, kind: str) -> None:
    algo = Probe("a", "AAA", {5: opener()})  # 09:35, one minute after 09:34
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], kind)
    assert result.entry_requests == [at("09:35")]
    assert not isinstance(algo.refs[0], RefusedEntry)
    assert result.report.total_suppressed == 0
    assert result.report.bars_after_gap == (("AAA", 1),)  # counted whether or not an entry came


@pytest.mark.parametrize("kind", KINDS)
def test_no_gap_no_effect_and_no_record(tmp_path: Path, kind: str) -> None:
    algo = Probe("a", "AAA", {1: opener(), 3: lambda a, r: a.close(a.refs[0], r)})
    result = run(tmp_path, {"AAA": quiet_rows("09:30", "09:31", "09:32", "09:33")}, [algo], kind)
    assert result.entry_requests == [at("09:30")]
    assert result.report == GapRuleReport((("AAA", 0),), ())
    assert result.report.empty


@pytest.mark.parametrize("kind", KINDS)
def test_first_bar_of_the_session_at_0931_is_refused_and_at_0930_is_allowed(
    tmp_path: Path, kind: str
) -> None:
    (tmp_path / "late").mkdir()
    late = run(tmp_path / "late", {"AAA": quiet_rows("09:31", "09:32")},
               [Probe("a", "AAA", {1: opener()})], kind)
    assert late.entry_requests == []
    assert late.report.bars_after_gap == (("AAA", 1),)
    assert late.report.suppressed == (SuppressedEntries("a", "AAA", 1, (at("09:31"),)),)

    (tmp_path / "open").mkdir()
    opening = run(tmp_path / "open", {"AAA": quiet_rows("09:30", "09:31")}, [Probe("a", "AAA", {1: opener()})], kind)
    assert opening.entry_requests == [at("09:30")]
    assert opening.report == GapRuleReport((("AAA", 0),), ())


@pytest.mark.parametrize("kind", KINDS)
def test_each_session_is_judged_on_its_own_first_bar(tmp_path: Path, kind: str) -> None:
    rows = [*quiet_rows("15:58", "15:59"), *quiet_rows("09:31", "09:32", day_="2024-01-05")]
    # day one's first bar (15:58 is not 09:30) and day two's (09:31) both follow a gap
    algo = Probe("a", "AAA", {1: opener(), 2: opener(), 3: opener(), 4: opener()})
    result = run(tmp_path, {"AAA": rows}, [algo], kind)
    assert result.report.bars_after_gap == (("AAA", 2),)
    assert result.report.suppressed[0].times == (at("15:58"), at("09:31", "2024-01-05"))
    assert result.entry_requests == [at("15:59"), at("09:32", "2024-01-05")]


@pytest.mark.parametrize("kind", KINDS)
def test_an_exit_on_a_gap_bar_still_works(tmp_path: Path, kind: str) -> None:
    algo = Probe("a", "AAA", {2: opener(), 4: lambda a, r: a.close(a.refs[0], r)})
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], kind)
    assert result.entry_requests == [at("09:31")]
    (trip,) = result.ledger.round_trips
    assert trip.exit_reason == "algo_exit" and not trip.synthetic
    if kind == "sim":
        assert trip.exit_time == at("09:34")
    assert result.report.total_suppressed == 0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("exit_first", [True, False])
def test_a_reversal_on_a_gap_bar_exits_but_does_not_enter(
    tmp_path: Path, kind: str, exit_first: bool
) -> None:
    def reverse(algo: Probe, row: pd.Series) -> None:
        if exit_first:
            algo.close(algo.refs[0], row)
            algo.open(row, direction=0)
        else:
            algo.open(row, direction=0)
            algo.close(algo.refs[0], row)

    algo = Probe("a", "AAA", {2: opener(), 4: reverse})
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], kind)
    assert result.entry_requests == [at("09:31")]  # the short never reached the broker
    (trip,) = result.ledger.round_trips
    assert trip.exit_reason == "algo_exit" and not trip.synthetic  # the exit leg went through
    assert result.report.suppressed == (SuppressedEntries("a", "AAA", 1, (at("09:34"),)),)


@pytest.mark.parametrize("kind", KINDS)
def test_an_add_to_an_open_position_on_a_gap_bar_is_refused(tmp_path: Path, kind: str) -> None:
    algo = Probe("a", "AAA", {2: opener(), 4: opener()})
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], kind)
    assert result.entry_requests == [at("09:31")]
    assert isinstance(algo.refs[1], RefusedEntry) and not isinstance(algo.refs[0], RefusedEntry)
    # the first position is untouched: still open until the end-of-data close
    (trip,) = result.ledger.round_trips
    assert trip.synthetic
    assert result.report.total_suppressed == 1


def test_stop_and_take_profit_changes_on_a_gap_bar_reach_the_broker(tmp_path: Path) -> None:
    def adjust(algo: Probe, row: pd.Series) -> None:
        algo.broker.modify_stop(algo.refs[0], 9.0)  # type: ignore[attr-defined]
        algo.broker.set_take_profit(algo.refs[0], 12.0, 10)  # type: ignore[attr-defined]

    algo = Probe("a", "AAA", {2: opener(), 4: adjust})
    result = run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], "sim")
    assert [c.method for c in result.brokers["a"].calls] == [  # type: ignore[attr-defined]
        "open_position", "modify_stop", "set_take_profit"]
    paper = Probe("p", "AAA", {2: opener(), 4: adjust})
    (tmp_path / "paper").mkdir()
    presult = run(tmp_path / "paper", {"AAA": quiet_rows(*GAPPED)}, [paper], "paper")
    gap_bar = [(o.kind, o.order_type) for o in presult.ledger.orders if o.time == at("09:34")]
    assert ("modified", "stop") in gap_bar or any(kind == "modified" for kind, _ in gap_bar)
    assert ("placed", "target") in gap_bar
    assert presult.report.total_suppressed == 0


@pytest.mark.parametrize("which", ["stop", "target"])
def test_a_stop_or_take_profit_on_a_gap_bar_still_fills(tmp_path: Path, which: str) -> None:
    rows = [*quiet_rows("09:30", "09:31", "09:32"),
            (at("09:34"), 10.0, 12.0 if which == "target" else 10.05,
             8.0 if which == "stop" else 9.95, 10.0),
            *quiet_rows("09:35")]

    def enter(algo: Probe, row: pd.Series) -> None:
        ref = algo.open(row, stop=9.0)
        algo.broker.set_take_profit(ref, 11.0, 10)  # type: ignore[attr-defined]

    algo = Probe("a", "AAA", {2: enter})
    result = run(tmp_path, {"AAA": rows}, [algo], "paper")
    (trip,) = result.ledger.round_trips
    assert trip.exit_time == at("09:34")  # the gap bar itself
    assert trip.exit_reason == which
    assert trip.exit_price == (Decimal("9.0") if which == "stop" else Decimal("11.0"))


# ------------------------------------------- per symbol, per algo, the record


def test_a_gap_in_one_symbol_only_blocks_that_symbol(tmp_path: Path) -> None:
    both = quiet_rows("09:30", "09:31", "09:32", "09:33", "09:34", "09:35")
    only_a = [r for r in both if r[0] != at("09:33")]  # BBB has no bar at 09:33
    algos = [Probe("a", "AAA", {5: opener()}), Probe("b1", "BBB", {4: opener()}),
             Probe("b2", "BBB", {4: opener(), 5: opener()})]  # 09:34 follows a gap for BBB only
    result = run(tmp_path, {"AAA": both, "BBB": only_a}, algos)
    assert result.report.bars_after_gap == (("AAA", 0), ("BBB", 1))
    assert result.report.suppressed == (
        SuppressedEntries("b1", "BBB", 1, (at("09:34"),)),
        SuppressedEntries("b2", "BBB", 1, (at("09:34"),)),
    )
    placed = sorted((o.account, o.time) for o in result.ledger.orders
                    if o.kind == "placed" and o.order_type == "entry")
    assert placed == [("a", at("09:34")), ("b2", at("09:35"))]


def test_every_refusal_is_counted_and_the_timestamps_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gaprule, "MAX_TIMES", 3)
    rule = GapRule({"A": datetime(2024, 1, 4, 9, 30)})
    for minute in range(35, 45, 2):  # five bars, each two minutes after the last
        assert rule.on_bar("A", datetime(2024, 1, 4, 9, minute))
        rule.refuse("x", "A")
        rule.end_bar("A")
    (entry,) = rule.report().suppressed
    assert entry.count == 5
    assert entry.times == (datetime(2024, 1, 4, 9, 35), datetime(2024, 1, 4, 9, 37),
                           datetime(2024, 1, 4, 9, 39))
    assert rule.report().total_suppressed == 5
    assert gap_rule_to_json(rule.report())["suppressed_entries"]["by_algo"][0]["times_truncated"] is True


def test_entries_outside_a_bar_update_are_not_blocked() -> None:
    rule = GapRule({"A": None})
    inner = SimBroker()
    guard = GapGuardBroker(inner, "a", rule)
    assert rule.on_bar("A", datetime(2024, 1, 4, 9, 31))
    rule.end_bar("A")
    ref = guard.open_position(symbol="A", volume=1, direction=1, price=1.0,
                              time=pd.Timestamp("2024-01-04 09:31"), limit=False,
                              stop_price=0.5, oca_group="g")
    assert not isinstance(ref, RefusedEntry)
    assert [c.method for c in inner.calls] == ["open_position"]


def test_the_algos_own_broker_is_installed_only_while_the_replay_runs(tmp_path: Path) -> None:
    seen: list[object] = []
    algo = Probe("a", "AAA", {1: lambda a, r: seen.append(a.broker)})
    write_ohlc(tmp_path, "AAA", quiet_rows("09:30", "09:31"))
    broker = SimBroker()
    engine = Engine(EngineConfig(), CsvBarFeed(tmp_path, ["AAA"], 1), broker, NullSink(), [algo])
    assert algo.broker is broker
    engine.run_backtest()
    assert isinstance(seen[0], GapGuardBroker) and seen[0].inner is broker
    assert algo.broker is broker
    assert engine.gap_report == GapRuleReport((("AAA", 0),), ())


# ------------------------------------------- how an algo learns of a refusal


class TradeProbe:
    """Enters the way `ParentAlgo.open_trade` does (through the legacy `Trade`) and checks its
    stop each bar the way the real algos do, so a refusal must show through that path."""

    def __init__(self, name: str, ticker: str, open_at: set[int]) -> None:
        self.name, self.ticker, self.open_at = name, ticker, open_at
        self.n = 0
        self.broker: object = None
        self.inTrade = False
        self.trades: list = []
        self.trade: object = None
        self.history: list[bool] = []

    def update(self, frame: pd.DataFrame) -> None:
        from MainStoinker.TradeTools.trade import Trade
        self.n += 1
        row = frame.iloc[-1]
        if self.inTrade and not self.trade.check_stoploss(row):  # type: ignore[attr-defined]
            self.inTrade = False
        if self.n in self.open_at and not self.inTrade:
            self.trade = Trade(self.ticker, 10, f"{self.name}{len(self.trades)}",
                               float(row["close"]), row["date"], 1, logging.getLogger("probe"),
                               broker=self.broker)
            self.trades.append(self.trade)
            self.inTrade = True
        self.history.append(self.inTrade)

    def update_frontend(self) -> dict:
        return {}


@pytest.mark.parametrize("kind", KINDS)
def test_the_algo_learns_of_a_refusal_on_its_next_bar_and_can_enter_again(
    tmp_path: Path, kind: str
) -> None:
    algo = TradeProbe("p", "AAA", {4, 6})
    rows = quiet_rows(*GAPPED, "09:37")
    result = run(tmp_path, {"AAA": rows}, [algo], kind)
    # update 4 (the gap bar): the algo believes it is in a trade; update 5: the stop is gone
    assert algo.history == [False, False, False, True, False, True, True]
    assert result.entry_requests == [at("09:36")]
    assert result.report.suppressed == (SuppressedEntries("p", "AAA", 1, (at("09:34"),)),)
    # the refused Trade object is not a trade in the records
    assert [r["openTime"] for r in trade_records(algo)] == [str(at("09:36"))]


def test_a_refused_trade_object_is_left_out_of_the_legacy_trade_records(tmp_path: Path) -> None:
    algo = TradeProbe("p", "AAA", {4})
    run(tmp_path, {"AAA": quiet_rows(*GAPPED)}, [algo], "sim")
    assert len(algo.trades) == 1 and isinstance(algo.trades[0].brokerRef, RefusedEntry)
    assert trade_records(algo) == []


# ---------------------------------------- what the rule does not fix (issue 144)
# The four strict xfails in tests/test_algos_minute_spacing.py feed the algo directly. Here the
# same scenarios run through the engine with the rule on: each still fails, so the markers stay.


class RowsFeed:
    """A one-symbol feed over explicit rows; the first `warm` rows are the warm-up."""

    def __init__(self, symbol: str, rows: Sequence[tuple], warm: int = 2) -> None:
        frame = pd.DataFrame(
            [(pd.Timestamp(ts), int(pd.Timestamp(ts).timestamp()), o, h, low, c, 100)
             for ts, o, h, low, c in rows], columns=list(COLUMNS))
        frame["date"] = frame["date"].astype("datetime64[us]")
        frame = frame.astype({"time": "int64", "volume": "int64"})
        self._symbol, self._warm, self._frame = symbol, frame.iloc[:warm], frame.iloc[warm:]

    def symbols(self) -> list[str]:
        return [self._symbol]

    def warmup(self, symbol: str) -> pd.DataFrame:
        return self._warm.reset_index(drop=True).copy()

    def __iter__(self):  # type: ignore[no-untyped-def]
        for rec in self._frame.itertuples(index=False):
            bar = Bar(*rec)
            yield bar.date, {self._symbol: bar}


def engine_run(algo_id: str, cfg: dict, rows: Sequence[tuple], warm: int = 2) -> object:
    algo = load_algo(algo_id, cfg)
    engine = Engine(EngineConfig(), RowsFeed(cfg["ticker"], rows, warm), SimBroker(),
                    NullSink(), [algo])
    engine.run_backtest()
    return algo


@pytest.mark.xfail(strict=True, reason="the gap rule only refuses entries on a bar that follows a "
                   "gap; FVG still records a gap formed across missing minutes (#175 does not fix it)")
def test_fvg_through_the_engine_is_still_not_protected_from_missing_minutes() -> None:
    algo = engine_run("FVG", {"idname": "T", "ticker": "TST", "RRRatio": 3}, fvg_rows("10:07"))
    assert algo.FVGs == []  # type: ignore[attr-defined]


@pytest.mark.xfail(strict=True, reason="the first bar of the day still sets the opening range: "
                   "the rule refuses entries on the 09:31 bar but not the range taken from it")
@pytest.mark.parametrize("algo_id", ALGO_IDS)
def test_opening_range_through_the_engine_is_still_taken_from_a_later_minute(algo_id: str) -> None:
    bars = [*day(D1, [first_candle(100), quiet(100)]), *day(D2, [first_candle(200)], "09:31")]
    rows = [(pd.Timestamp(ts), 90, 91, 89, 90) for ts in ("2024-08-16 15:58", "2024-08-16 15:59")]
    rows += [(b.ts, b.open, b.high, b.low, b.close) for b in bars]
    algo = engine_run(algo_id, {"idname": "T", "ticker": "TST", "RRRatio": 3}, rows)
    assert (algo.upperbound, algo.lowerbound) != (202, 198)  # type: ignore[attr-defined]


@pytest.mark.xfail(strict=True, reason="EMACrossing's end-of-day close still needs a bar after "
                   "15:55 on the same date; exits are not what the gap rule changes")
def test_ema_through_the_engine_still_carries_a_position_overnight() -> None:
    algo = engine_run("EMACrossing", {"idname": "T", "ticker": "TST", "short": 2, "long": 4},
                      ema_rows("15:54"))
    assert algo.trades  # type: ignore[attr-defined]  # a trade was opened (after the refused first bar)
    assert algo.inTrade is False  # type: ignore[attr-defined]


def test_the_through_the_engine_scenarios_do_open_trades_and_refuse_the_first_bar() -> None:
    """The controls of the three xfails above: the data is driven the same way, so a pass of
    an xfail would be the rule's doing and not a broken rig."""
    algo = engine_run("EMACrossing", {"idname": "T", "ticker": "TST", "short": 2, "long": 4},
                      ema_rows("15:54"))
    assert len(algo.trades) >= 1  # type: ignore[attr-defined]
    fvg = engine_run("FVG", {"idname": "T", "ticker": "TST", "RRRatio": 3}, fvg_rows("10:02"))
    assert len(fvg.FVGs) == 1  # type: ignore[attr-defined]


# ------------------------------------------------------------------ the store


def sample_report() -> GapRuleReport:
    return GapRuleReport(
        (("AAA", 2), ("BBB", 0)),
        (SuppressedEntries("a", "AAA", 3, (datetime(2024, 1, 4, 9, 34), datetime(2024, 1, 4, 9, 40))),))


def test_json_form_round_trips_and_a_bad_value_reads_as_not_recorded() -> None:
    report = sample_report()
    assert gap_rule_from_json(gap_rule_to_json(report)) == report
    assert gap_rule_to_json(report)["suppressed_entries"]["count"] == 3
    assert gap_rule_to_json(report)["suppressed_entries"]["by_algo"][0]["times_truncated"] is True
    assert gap_rule_from_json(None) is None
    assert gap_rule_from_json({"bars_after_gap": 1}) is None
    assert gap_rule_from_json({"bars_after_gap": {}, "suppressed_entries": {"by_algo": [{}]}}) is None


def test_store_keeps_the_gap_rule_in_data_json_without_a_migration(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    store = SqliteStore(db, env=NO_ENV)
    try:
        version = store.schema_version
        old = store.begin_run(RunMeta("backtest", "legacy", "v", {}, DataMeta("csv", "d", ("AAA",))))
        new = store.begin_run(RunMeta("backtest", "legacy", "v", {}, DataMeta("csv", "d", ("AAA",))))
        store.write_gap_rule(new, sample_report())
        store.end_run(old, "completed")
        store.end_run(new, "completed")
        with pytest.raises(Exception, match="already completed"):
            store.write_gap_rule(new, sample_report())  # only a running run can be written
        assert store.load_run(old).summary.data.gap_rule is None  # never recorded
        assert store.load_run(new).summary.data.gap_rule == sample_report()
        assert store.schema_version == version
    finally:
        store.close()
    NullStore().write_gap_rule(1, sample_report())


# --------------------------------------------- the command, report, API, notes


def gapped_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamps: Sequence[str]) -> Env:
    """FunctionTest on AAA over `stamps`: it enters on its second update (and again on its 12th)."""
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    env = Env(tmp_path, monkeypatch)
    env.data.mkdir()
    write_ohlc(env.data, "AAA", quiet_rows(*stamps))
    env.algos.write_text(json.dumps(
        [{"ID": "FunctionTest", "data": [{"idname": "AAA-ft", "ticker": "AAA", "RRRatio": 3}]}]),
        encoding="utf-8")
    return env


MINUTES = [f"09:{m:02d}" for m in range(30, 45)]
WITH_GAP = [t for t in MINUTES if t != "09:31"]  # 09:32 is the second bar: it follows a gap
LINES = [
    "bars that follow a gap (no new position opened on them): AAA 1",
    "entries refused on bars that follow a gap: 1",
    "  AAA-ft AAA: 1 at 2024-01-04 09:32:00",
]


def test_backtest_summary_report_json_and_api_show_the_gap_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = gapped_env(tmp_path, monkeypatch, WITH_GAP)
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if "gap" in line] == LINES[:2]
    assert out[out.index(LINES[0]):][:3] == LINES
    # the refused entry is not a trade; the algo's later entry (12th update) is
    (records,) = env.trades().values()
    assert len(records) == 1 and records[0]["openTime"] != "2024-01-04 09:32:00"

    assert main(["report", "1", "--db", str(db)]) == 0
    text = capsys.readouterr().out.splitlines()
    assert all(line in text for line in LINES)

    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["run"]["gap_rule"] == {
        "bars_after_gap": {"AAA": 1},
        "suppressed_entries": {"count": 1, "by_algo": [{
            "algo": "AAA-ft", "symbol": "AAA", "count": 1,
            "times": ["2024-01-04T09:32:00"], "times_truncated": False}]},
    }
    response = make_client(db).get("/api/runs/1")
    assert response.status_code == 200
    assert response.json()["run"]["gap_rule"] == doc["run"]["gap_rule"]
    store = open_store(str(db), env={})
    try:
        assert store.load_run(1).summary.data.gap_rule is not None
    finally:
        store.close()


def test_nothing_is_printed_or_shown_when_there_was_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = gapped_env(tmp_path, monkeypatch, MINUTES)
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    assert "gap" not in capsys.readouterr().out
    assert main(["report", "1", "--db", str(db)]) == 0
    assert "gap" not in capsys.readouterr().out
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    gap_rule = json.loads(capsys.readouterr().out)["run"]["gap_rule"]
    assert gap_rule == {"bars_after_gap": {"AAA": 0},
                        "suppressed_entries": {"count": 0, "by_algo": []}}


def test_a_run_stored_before_the_gap_rule_has_no_gap_rule_in_its_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "runs.db"
    store = SqliteStore(db, env=NO_ENV)
    try:
        run_id = store.begin_run(RunMeta("backtest", "legacy", "v", {}, DataMeta("csv", "d", ("AAA",))))
        store.end_run(run_id, "completed")
    finally:
        store.close()
    assert main(["report", "1", "--db", str(db)]) == 0
    assert "gap" not in capsys.readouterr().out
    assert main(["report", "1", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["gap_rule"] is None


def test_overview_notes_show_the_gap_rule_only_when_it_counted_something(
    node_workdir: Path,  # noqa: F811
) -> None:
    out = run_node(node_workdir, "overview_gap_rule_notes", FAKE_DOM + """
const { runNotes } = await import("./js/views/overview.js");
const total = { synthetic_closes: 0, cancelled_entries: 0, ignored_events: {} };
const rule = { bars_after_gap: { AAA: 2, BBB: 0 }, suppressed_entries: { count: 3, by_algo: [{ algo: "a", symbol: "AAA", count: 3, times: [], times_truncated: false }] } };
console.log(JSON.stringify({
  none: runNotes({ run: {}, total }),
  nullRule: runNotes({ run: { gap_rule: null }, total }),
  zero: runNotes({ run: { gap_rule: { bars_after_gap: { AAA: 0 }, suppressed_entries: { count: 0, by_algo: [] } } }, total }),
  junk: runNotes({ run: { gap_rule: { bars_after_gap: "x", suppressed_entries: 5 } }, total }),
  shown: runNotes({ run: { gap_rule: rule }, total }),
  one: runNotes({ run: { gap_rule: { bars_after_gap: { AAA: 1 }, suppressed_entries: { count: 1, by_algo: [] } } }, total }),
}));
""")
    assert isinstance(out, dict)
    for name in ("none", "nullRule", "zero", "junk"):
        assert out[name] == [], name
    assert out["shown"] == [
        "Bars that follow a gap (no new position opened on them): AAA 2.",
        "3 entries refused on bars that follow a gap (a AAA: 3).",
    ]
    assert out["one"][1] == "1 entry refused on bars that follow a gap."
