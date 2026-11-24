"""The engine's live-session API: `start_live` / `step` / `end_live` (stage 2, T2).

The key property is equivalence: feeding a `CsvBarFeed`'s batches through the
live API gives what `run_backtest` gives on the same data, except for the
synthetic end-of-data closes, which a live session never makes (the positions
stay open and `end_live` returns them). The rest pins the session's contracts:
ordering, refusals, the failed state, one mode per engine.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.engine import Engine, EngineConfig, LivePosition
from MainStoinker.engine.feed import Bar, CsvBarFeed
from MainStoinker.engine.gaprule import GapGuardBroker
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.router import MappingRouter
from MainStoinker.engine.sink import NullSink
from tests import harness
from tests.test_engine import ListFeed

FREE = CostModel(Decimal(0), Decimal(0), Decimal(0))
ROWS = 900
SYMBOLS = ["AAPL", "MSFT", "TSLA"]


# ------------------------------------------------------------ equivalence


@dataclass
class Run:
    engine: Engine
    ledger: Ledger
    brokers: dict


def _build(directory: Path, symbols: list[str], kind: str) -> tuple[Run, CsvBarFeed]:
    harness.reset_runtime_state()
    feed = CsvBarFeed(directory / "OfflineData", symbols, 2)
    ledger = Ledger()
    algos = [load_algo("EMACrossing", {"idname": f"{s}-10/30EMA", "ticker": s,
                                       "short": "10", "long": "30"}) for s in symbols]
    brokers: dict = {}
    for algo in algos:
        account = ledger.open_account(algo.name, algo.ticker, 10_000)
        brokers[algo.name] = (PaperBroker(account) if kind == "paper"
                              else SimBroker(account=account))
    engine = Engine(
        EngineConfig(), feed, MappingRouter(brokers), NullSink(), algos, ledger=ledger,
        end_commission=lambda idname, quantity: (
            brokers[idname].costs.commission(quantity) if kind == "paper" else 0))
    return Run(engine, ledger, brokers), feed


def _stage(directory: Path, symbols: list[str], drop_every: int | None) -> None:
    harness.stage_offline_data(directory, symbols, max_rows=ROWS)
    if drop_every is None:
        return
    for symbol in symbols:
        path = directory / "OfflineData" / f"_{symbol}_offlinedata_"
        data = pd.read_csv(path, index_col=0)
        # leave the warm-up alone; punch holes in the replayed part
        data.drop(data.index[500::drop_every]).to_csv(path)


def _compare(tmp_path: Path, symbols: list[str], kind: str, drop_every: int | None = None
             ) -> tuple[Run, Run, tuple[LivePosition, ...]]:
    _stage(tmp_path, symbols, drop_every)
    with contextlib.chdir(tmp_path):
        try:
            backtest, _ = _build(tmp_path, symbols, kind)
            backtest.engine.run_backtest()
            live, feed = _build(tmp_path, symbols, kind)
            live.engine.start_live({s: feed.warmup(s) for s in symbols})
            steps = 0
            for timestamp, bars in feed:
                live.engine.step(timestamp, bars)
                steps += 1
            assert steps > 100
            open_positions = live.engine.end_live()
        finally:
            harness.reset_runtime_state()
    return backtest, live, open_positions


def _assert_equivalent(backtest: Run, live: Run, open_positions: tuple[LivePosition, ...]
                       ) -> None:
    bt, lv = backtest.ledger, live.ledger
    assert len(bt.fills) > 4  # the data does trade
    assert [f for f in bt.fills if not f.synthetic] == list(lv.fills)
    assert [r for r in bt.round_trips if not r.synthetic] == list(lv.round_trips)
    last = bt.equity_points[-1].time
    assert last == lv.equity_points[-1].time
    # finish() replaced the points at the last time; every earlier point is identical
    assert [p for p in bt.equity_points if p.time != last] == \
        [p for p in lv.equity_points if p.time != last]
    # the live run's last points are the last step's marks: nothing is closed
    assert not any(f.synthetic for f in lv.fills)
    assert live.engine.gap_report == backtest.engine.gap_report
    # the backtest's extra trades are the synthetic closes = the live open positions
    synthetic = [r for r in bt.round_trips if r.synthetic]
    assert all(r.exit_reason == "end_of_data" for r in synthetic)
    assert sorted((p.account, p.symbol, p.side, p.quantity) for p in open_positions) == \
        sorted((r.account, r.symbol, r.side, r.quantity) for r in synthetic)
    assert tuple(open_positions) == live.engine._open_positions()
    for position in open_positions:
        (trip,) = [r for r in synthetic if r.account == position.account]
        assert position.entry_price == trip.entry_price
        assert position.entry_time == trip.entry_time


@pytest.mark.parametrize("kind", ["paper", "sim"])
@pytest.mark.parametrize("symbols", [["AAPL"], SYMBOLS], ids=["one-symbol", "three-symbols"])
def test_live_steps_equal_backtest_without_synthetic_closes(
    tmp_path: Path, symbols: list[str], kind: str
) -> None:
    backtest, live, open_positions = _compare(tmp_path, symbols, kind)
    _assert_equivalent(backtest, live, open_positions)
    if kind == "paper" and len(symbols) == 3:
        assert open_positions  # the equivalence of open positions is not vacuous here


@pytest.mark.parametrize("kind", ["paper", "sim"])
def test_live_gap_rule_counts_equal_backtest(tmp_path: Path, kind: str) -> None:
    # every 37th replayed row is dropped: about eleven gap bars per symbol
    backtest, live, open_positions = _compare(tmp_path, SYMBOLS, kind, 37)
    counted = backtest.engine.gap_report.total_bars_after_gap
    assert counted > 20
    assert live.engine.gap_report.total_bars_after_gap == counted
    assert live.engine.gap_report == backtest.engine.gap_report
    _assert_equivalent(backtest, live, open_positions)


# ------------------------------------------------------- synthetic sessions


def bar(minute: int, price: float = 10.0) -> Bar:
    date = pd.Timestamp("2030-01-01 09:30:00") + pd.Timedelta(minutes=minute)
    return Bar(date, int(date.timestamp()), price, price + 0.05, price - 0.05, price, 100)


def warm(*symbols: str) -> dict[str, pd.DataFrame]:
    return {s: pd.DataFrame.from_records([bar(-1).as_record()]) for s in symbols}


class Script:
    """An algo that runs `actions[n]` on its n-th update (1-based)."""

    def __init__(self, name: str, ticker: str, actions: dict | None = None) -> None:
        self.name, self.ticker = name, ticker
        self.broker: object | None = None
        self.actions = actions or {}
        self.updates = 0
        self.refs: list = []

    def update(self, frame: pd.DataFrame) -> None:
        self.updates += 1
        action = self.actions.get(self.updates)
        if action is not None:
            action(self, frame["date"].iloc[-1])

    def update_frontend(self) -> dict:
        return {}

    def enter(self, time: pd.Timestamp, tp: float | None = 12.0):
        ref = self.broker.open_position(  # type: ignore[attr-defined]
            symbol=self.ticker, volume=10, direction=1, price=10.0, time=time, limit=False,
            stop_price=9.0, oca_group="g")
        if tp is not None:
            self.broker.set_take_profit(ref, tp, 10)  # type: ignore[attr-defined]
        self.refs.append(ref)


def boom(algo: Script, time: pd.Timestamp) -> None:
    raise RuntimeError("algo boom")


def make(actions: dict | None = None, kind: str = "paper", symbols: tuple[str, ...] = ("AAA",),
         broker=None):
    ledger = Ledger()
    algos = [Script(f"a-{s}", s, actions if i == 0 else None) for i, s in enumerate(symbols)]
    brokers = {}
    for algo in algos:
        account = ledger.open_account(algo.name, algo.ticker, 10_000)
        brokers[algo.name] = broker or (PaperBroker(account, FREE) if kind == "paper"
                                        else SimBroker(account=account))
    engine = Engine(EngineConfig(), ListFeed({s: 1 for s in symbols}, 0),
                    MappingRouter(brokers), NullSink(), algos, ledger=ledger)
    return engine, algos, ledger, brokers


def test_step_refuses_non_increasing_time() -> None:
    engine, algos, _, _ = make()
    engine.start_live(warm("AAA"))
    engine.step(bar(0).date, {"AAA": bar(0)})
    engine.step(bar(1).date, {"AAA": bar(1)})
    for stamp in (1, 0):  # the same time again, an earlier time
        with pytest.raises(ValueError, match="not after"):
            engine.step(bar(stamp).date, {"AAA": bar(stamp)})
    assert algos[0].updates == 2  # nothing was applied
    engine.step(bar(2).date, {"AAA": bar(2)})  # the engine is still usable
    assert algos[0].updates == 3


def test_step_refuses_unknown_symbols_and_mismatched_dates() -> None:
    engine, algos, _, _ = make()
    engine.start_live(warm("AAA"))
    with pytest.raises(ValueError, match="unknown symbol.*ZZZ"):
        engine.step(bar(0).date, {"AAA": bar(0), "ZZZ": bar(0)})
    with pytest.raises(ValueError, match="differs from the batch timestamp"):
        engine.step(bar(1).date, {"AAA": bar(0)})
    assert algos[0].updates == 0
    engine.step(bar(0).date, {"AAA": bar(0)})
    assert algos[0].updates == 1


def test_empty_batch_is_a_no_op() -> None:
    seen: list = []
    engine, algos, ledger, _ = make()
    engine.start_live(warm("AAA"), on_step=lambda t, s: seen.append((t, s)))
    engine.step(bar(5).date, {})
    assert seen == [] and algos[0].updates == 0 and ledger.equity_points == ()
    engine.step(bar(0).date, {"AAA": bar(0)})  # an empty batch did not set the time
    assert algos[0].updates == 1


def test_on_step_called_once_per_nonempty_batch() -> None:
    seen: list = []
    engine, algos, ledger, _ = make(symbols=("AAA", "BBB"))

    def observer(timestamp, present) -> None:
        # called after the step completed: the algos were updated and the ledger marked
        seen.append((timestamp, present, [a.updates for a in algos], len(ledger.equity_points)))

    engine.start_live(warm("AAA", "BBB"), on_step=observer)
    engine.step(bar(0).date, {"BBB": bar(0), "AAA": bar(0)})
    engine.step(bar(1).date, {})
    engine.step(bar(2).date, {"BBB": bar(2)})
    assert [(t, p) for t, p, _, _ in seen] == [(bar(0).date, ("AAA", "BBB")),
                                               (bar(2).date, ("BBB",))]
    assert seen[0][2] == [1, 1] and seen[1][2] == [1, 2]
    assert seen[0][3] == 3 and seen[1][3] == 6  # two accounts + total per mark


def test_end_live_cancels_pending_entries_and_leaves_positions_open() -> None:
    actions = {1: lambda a, t: a.enter(t), 3: lambda a, t: a.enter(t, tp=None)}
    engine, algos, ledger, brokers = make(actions)
    engine.start_live(warm("AAA"))
    for i in range(3):
        engine.step(bar(i).date, {"AAA": bar(i)})
    # the first entry filled at bar 1's open; the second is still pending
    assert [f.reason for f in ledger.fills] == ["entry"]
    marks = len(ledger.equity_points)
    opened = engine.end_live()
    assert brokers["a-AAA"].cancelled_entries == 1
    assert [f.reason for f in ledger.fills] == ["entry"]  # nothing synthetic
    assert ledger.round_trips == ()
    assert len(ledger.equity_points) == marks  # no finish(), no extra point
    assert ledger.equity_points[-1].time == bar(2).date
    assert opened == (LivePosition(
        "a-AAA", 1, "AAA", "long", Decimal(10), Decimal("10"), bar(1).date,
        Decimal("9"), Decimal("12")),)
    assert opened == tuple(
        LivePosition(p.account, p.broker_ref, p.symbol, p.side, p.quantity, p.average_price,
                     bar(1).date, Decimal("9"), Decimal("12")) for p in ledger.open_positions())


def test_end_live_fills_exiting_positions_and_reports_modified_levels() -> None:
    def exit_it(algo, time) -> None:
        algo.broker.close_position(algo.refs[0], price=10.0, time=time)

    def tighten(algo, time) -> None:
        algo.broker.modify_stop(algo.refs[1], 9.5)

    actions = {1: lambda a, t: a.enter(t), 2: lambda a, t: a.enter(t, tp=None),
               3: lambda a, t: (exit_it(a, t), tighten(a, t))}
    engine, _, ledger, _ = make(actions)
    engine.start_live(warm("AAA"))
    for i in range(3):
        engine.step(bar(i).date, {"AAA": bar(i)})
    opened = engine.end_live()
    assert [f.reason for f in ledger.fills] == ["entry", "entry", "algo_exit"]
    assert [(p.broker_ref, p.stop, p.target) for p in opened] == [(2, Decimal("9.5"), None)]


def test_sim_broker_positions_have_no_levels() -> None:
    engine, _, ledger, _ = make({1: lambda a, t: a.enter(t)}, kind="sim")
    engine.start_live(warm("AAA"))
    engine.step(bar(0).date, {"AAA": bar(0)})
    (position,) = engine.end_live()
    assert (position.symbol, position.quantity, position.entry_time) == \
        ("AAA", Decimal(10), bar(0).date)
    assert position.stop is None and position.target is None


def test_end_live_without_a_ledger_returns_nothing() -> None:
    algo = Script("a", "AAA")
    engine = Engine(EngineConfig(), ListFeed({"AAA": 1}, 0), SimBroker(), NullSink(), [algo])
    engine.start_live(warm("AAA"))
    engine.step(bar(0).date, {"AAA": bar(0)})
    assert engine.end_live() == ()


def test_end_live_twice_is_refused() -> None:
    engine, _, _, _ = make()
    engine.start_live(warm("AAA"))
    engine.end_live()
    with pytest.raises(RuntimeError, match="ended"):
        engine.end_live()
    with pytest.raises(RuntimeError, match="ended"):
        engine.step(bar(0).date, {"AAA": bar(0)})
    with pytest.raises(RuntimeError, match="once"):
        engine.start_live(warm("AAA"))


def test_live_calls_need_start_live() -> None:
    engine, _, _, _ = make()
    with pytest.raises(RuntimeError, match="start_live"):
        engine.step(bar(0).date, {"AAA": bar(0)})
    with pytest.raises(RuntimeError, match="start_live"):
        engine.end_live()


def test_cannot_mix_run_backtest_and_live() -> None:
    engine, _, _, _ = make()
    engine.start_live(warm("AAA"))
    with pytest.raises(RuntimeError, match="live mode"):
        engine.run_backtest()
    engine.end_live()
    with pytest.raises(RuntimeError, match="live mode"):
        engine.run_backtest()

    algo = Script("a", "AAA")
    feed = ListFeed({"AAA": 1}, 2)
    other = Engine(EngineConfig(), feed, SimBroker(), NullSink(), [algo])
    other.run_backtest()
    with pytest.raises(RuntimeError, match="backtest"):
        other.start_live(warm("AAA"))
    with pytest.raises(RuntimeError, match="start_live"):
        other.step(bar(0).date, {"AAA": bar(0)})


def test_start_live_validates_the_warmup_before_touching_anything() -> None:
    engine, algos, _, brokers = make(symbols=("AAA", "BBB"))
    before = engine.gap_report
    with pytest.raises(ValueError, match="BBB"):
        engine.start_live(warm("AAA"))
    with pytest.raises(ValueError, match="date"):
        engine.start_live({"AAA": warm("AAA")["AAA"], "BBB": pd.DataFrame({"x": [1]})})
    assert engine.gap_report == before
    assert all(a.broker is brokers[a.name] for a in algos)  # no guard installed
    engine.start_live(warm("AAA", "BBB"))  # still startable: nothing was consumed
    assert all(isinstance(a.broker, GapGuardBroker) for a in algos)
    engine.end_live()
    assert all(a.broker is brokers[a.name] for a in algos)


# ------------------------------------------------------------ failed state


class RaisingBroker(SimBroker):
    """A bar-aware broker whose `on_bar` raises."""

    def __init__(self) -> None:
        super().__init__()
        self.ended: list = []

    def on_bar(self, symbol, bar_) -> None:
        raise RuntimeError("broker boom")

    def on_end(self, last_bars) -> None:
        self.ended.append(dict(last_bars))


def _fail_with_algo():
    engine, algos, ledger, brokers = make({2: boom}, kind="paper")
    return engine, algos, ledger, brokers, None, "algo boom"


def _fail_with_broker():
    broker = RaisingBroker()
    engine, algos, ledger, brokers = make(broker=broker)
    return engine, algos, ledger, brokers, None, "broker boom"


def _fail_with_observer():
    engine, algos, ledger, brokers = make()

    def observer(timestamp, present) -> None:
        if timestamp == bar(1).date:
            raise RuntimeError("observer boom")

    return engine, algos, ledger, brokers, observer, "observer boom"


@pytest.mark.parametrize("scenario", [_fail_with_algo, _fail_with_broker, _fail_with_observer],
                         ids=["algo", "broker-on-bar", "observer"])
def test_a_failed_step_fails_the_engine_but_end_live_still_closes_cleanly(scenario) -> None:
    engine, algos, ledger, brokers, observer, message = scenario()
    engine.start_live(warm("AAA"), on_step=observer)
    broker = brokers["a-AAA"]
    with pytest.raises(RuntimeError, match=message):
        for i in range(2):  # the failure comes on the first (broker) or second step
            engine.step(bar(i).date, {"AAA": bar(i)})
    updates = algos[0].updates
    for _ in range(2):  # every later step raises, a retry of the same batch included
        with pytest.raises(RuntimeError, match="failed") as info:
            engine.step(bar(5).date, {"AAA": bar(5)})
        assert str(info.value.__cause__) == message
    with pytest.raises(RuntimeError, match="failed"):
        engine.step(bar(9).date, {})  # even an empty batch
    assert algos[0].updates == updates
    assert isinstance(algos[0].broker, GapGuardBroker)
    opened = engine.end_live()  # still closes the run
    assert algos[0].broker is broker
    assert opened == ()
    if isinstance(broker, RaisingBroker):
        assert broker.ended == [{"AAA": bar(0)}]  # on_end ran, with what had arrived
    with pytest.raises(RuntimeError, match="ended"):
        engine.end_live()


def test_end_live_after_a_failure_calls_on_end_on_the_brokers() -> None:
    actions = {1: lambda a, t: a.enter(t), 3: boom}
    engine, algos, ledger, brokers = make(actions)
    engine.start_live(warm("AAA"))
    engine.step(bar(0).date, {"AAA": bar(0)})
    engine.step(bar(1).date, {"AAA": bar(1)})
    with pytest.raises(RuntimeError, match="algo boom"):
        engine.step(bar(2).date, {"AAA": bar(2)})
    opened = engine.end_live()
    assert [(p.symbol, p.quantity) for p in opened] == [("AAA", Decimal(10))]
    assert [f.reason for f in ledger.fills] == ["entry"]


# ------------------------------------------------- warm-up, re-entrancy, extras


def test_first_step_must_be_after_the_warmup() -> None:
    engine, algos, _, _ = make()
    engine.start_live(warm("AAA"))  # the warm-up ends at 09:29
    for stale in (-1, -3):  # the warm-up's own minute, an earlier one
        with pytest.raises(ValueError, match="last stored bar"):
            engine.step(bar(stale).date, {"AAA": bar(stale)})
    assert algos[0].updates == 0
    engine.step(bar(0).date, {"AAA": bar(0)})  # still usable
    assert algos[0].updates == 1


def test_the_warmup_check_is_per_symbol() -> None:
    engine, algos, _, _ = make(symbols=("AAA", "BBB"))
    frames = {"AAA": pd.DataFrame.from_records([bar(-1).as_record()]),
              "BBB": pd.DataFrame.from_records([bar(-3).as_record(), bar(-2).as_record()])}
    engine.start_live(frames)
    with pytest.raises(ValueError, match="AAA"):
        engine.step(bar(-1).date, {"AAA": bar(-1)})
    engine.step(bar(-1).date, {"BBB": bar(-1)})  # fine for BBB, whose warm-up ends at 09:28
    assert [a.updates for a in algos] == [0, 1]
    engine.step(bar(0).date, {"AAA": bar(0), "BBB": bar(0)})
    assert [a.updates for a in algos] == [1, 2]


def test_warmup_must_be_strictly_increasing() -> None:
    engine, algos, _, brokers = make()
    for rows in ([bar(-1), bar(-2)], [bar(-2), bar(-2)]):
        with pytest.raises(ValueError, match="not sorted"):
            engine.start_live({"AAA": pd.DataFrame.from_records([b.as_record() for b in rows])})
    assert algos[0].broker is brokers["a-AAA"]
    engine.start_live(warm("AAA"))  # nothing was consumed


def test_start_live_refuses_warmups_of_unknown_symbols() -> None:
    engine, algos, _, brokers = make()
    with pytest.raises(ValueError, match="ZZZ"):
        engine.start_live({**warm("AAA"), "ZZZ": warm("ZZZ")["ZZZ"]})
    assert algos[0].broker is brokers["a-AAA"]
    engine.start_live(warm("AAA"))


def test_step_and_start_live_from_on_step_are_refused_but_end_live_is_allowed() -> None:
    engine, algos, _, _ = make()
    caught: list = []

    def observer(timestamp, present) -> None:
        for call in (lambda: engine.step(bar(1).date, {"AAA": bar(1)}),
                     lambda: engine.start_live(warm("AAA"))):
            with pytest.raises(RuntimeError, match="inside a step"):
                call()
        caught.append(timestamp)

    engine.start_live(warm("AAA"), on_step=observer)
    engine.step(bar(0).date, {"AAA": bar(0)})
    assert caught == [bar(0).date]
    engine.step(bar(1).date, {"AAA": bar(1)})  # the refusals did not fail the engine
    assert algos[0].updates == 2

    engine, algos, _, brokers = make()
    ended: list = []
    engine.start_live(warm("AAA"), on_step=lambda t, p: ended.append(engine.end_live()))
    engine.step(bar(0).date, {"AAA": bar(0)})
    assert ended == [()] and algos[0].broker is brokers["a-AAA"]
    with pytest.raises(RuntimeError, match="ended"):
        engine.step(bar(1).date, {"AAA": bar(1)})
