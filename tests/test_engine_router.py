"""Engine broker routing, broker bar/end hooks and ledger marks (in-memory feed, no legacy code)."""
from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pandas as pd
import pytest

from MainStoinker.engine.broker import Broker, SimBroker
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import COLUMNS, Bar
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.router import (
    BarAwareBroker,
    BrokerRouter,
    MappingRouter,
    SingleBrokerRouter,
)
from MainStoinker.engine.sink import NullSink

T0 = pd.Timestamp("2024-01-03 09:30:00")
STEP = pd.Timedelta(minutes=1)


def make_bar(minute: int, close: float) -> Bar:
    stamp = T0 + minute * STEP
    return Bar(stamp, int(stamp.timestamp()), close, close + 1, close - 1, close, 100)


class FakeFeed:
    """Warm-up of `warmup_rows` bars per symbol, then `rows` replay batches."""

    def __init__(self, symbols: list[str], rows: int = 3, warmup_rows: int = 2) -> None:
        self._symbols = symbols
        self._rows = rows
        self._warmup_rows = warmup_rows
        self.warmup_bars = {
            s: [make_bar(-warmup_rows + i, 50.0 + i) for i in range(warmup_rows)] for s in symbols
        }

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def warmup(self, symbol: str) -> pd.DataFrame:
        records = [b.as_record() for b in self.warmup_bars[symbol]]
        frame = pd.DataFrame.from_records(records, columns=list(COLUMNS))
        return frame.astype({"time": "int64", "volume": "int64"})

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        for i in range(self._rows):
            yield T0 + i * STEP, {s: make_bar(i, 100.0 + 10 * j + i) for j, s in enumerate(self._symbols)}


class FakeAlgo:
    def __init__(self, name: str, ticker: str, log: list | None = None) -> None:
        self.name = name
        self.ticker = ticker
        self.log = log if log is not None else []
        self.frames: list[int] = []

    def update(self, frame: pd.DataFrame) -> None:
        self.frames.append(len(frame))
        self.log.append(("update", self.name, len(frame)))

    def update_frontend(self) -> dict:
        return {}


class HookBroker(SimBroker):
    """SimBroker that logs the bar-aware hooks into a shared log."""

    def __init__(self, name: str, log: list) -> None:
        super().__init__()
        self.name = name
        self.log = log

    def on_bar(self, symbol: str, bar: Bar) -> None:
        self.log.append(("on_bar", self.name, symbol, bar.close))

    def on_end(self, last_bars) -> None:
        self.log.append(("on_end", self.name, dict(last_bars)))


def build(feed, broker, algos, **kwargs) -> Engine:
    return Engine(EngineConfig(), feed, broker, NullSink(), algos, **kwargs)


def test_single_broker_still_injected_into_every_algo() -> None:
    broker = SimBroker()
    algos = [FakeAlgo("a", "AAPL"), FakeAlgo("b", "MSFT")]
    build(FakeFeed(["AAPL", "MSFT"]), broker, algos)
    assert all(algo.broker is broker for algo in algos)
    # the router form of the same thing
    algos2 = [FakeAlgo("a", "AAPL"), FakeAlgo("b", "MSFT")]
    build(FakeFeed(["AAPL", "MSFT"]), SingleBrokerRouter(broker), algos2)
    assert all(algo.broker is broker for algo in algos2)


def test_mapping_router_gives_each_algo_its_broker() -> None:
    one, two = SimBroker(), SimBroker()
    a, b = FakeAlgo("a", "AAPL"), FakeAlgo("b", "AAPL")
    build(FakeFeed(["AAPL"]), MappingRouter({"a": one, "b": two}), [a, b])
    assert a.broker is one
    assert b.broker is two


def test_mapping_router_unknown_algo_raises_before_touching_algos() -> None:
    a, b = FakeAlgo("a", "AAPL"), FakeAlgo("b", "AAPL")
    with pytest.raises(KeyError, match="'b'"):
        build(FakeFeed(["AAPL"]), MappingRouter({"a": SimBroker()}), [a, b])
    assert not hasattr(a, "broker")


def test_router_returning_non_broker_raises_before_touching_algos() -> None:
    a, b = FakeAlgo("a", "AAPL"), FakeAlgo("b", "AAPL")
    router = MappingRouter({"a": SimBroker(), "b": object()})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="not a Broker"):
        build(FakeFeed(["AAPL"]), router, [a, b])
    assert not hasattr(a, "broker")
    assert not hasattr(b, "broker")


@pytest.mark.parametrize("bad", [None, object(), "broker", 3])
def test_non_broker_non_router_still_raises_the_old_type_error(bad) -> None:
    with pytest.raises(TypeError, match="Engine requires a Broker"):
        build(FakeFeed(["AAPL"]), bad, [FakeAlgo("a", "AAPL")])


def test_object_that_is_both_broker_and_router_is_treated_as_a_broker() -> None:
    other = SimBroker()

    class Both(SimBroker):
        def broker_for(self, algo: object) -> Broker:
            return other

    both = Both()
    assert isinstance(both, Broker) and isinstance(both, BrokerRouter)
    algo = FakeAlgo("a", "AAPL")
    build(FakeFeed(["AAPL"]), both, [algo])
    assert algo.broker is both


def test_on_bar_called_for_all_symbols_before_any_algo_update() -> None:
    log: list = []
    broker = HookBroker("x", log)
    algos = [FakeAlgo("a", "AAPL", log), FakeAlgo("b", "MSFT", log)]
    build(FakeFeed(["AAPL", "MSFT"], rows=2), broker, algos).run_backtest()
    kinds = [e[0] for e in log if e[0] != "on_end"]
    assert kinds == ["on_bar", "on_bar", "update", "update"] * 2
    first = [e for e in log[:2]]
    assert [e[2] for e in first] == ["AAPL", "MSFT"]


def test_on_bar_called_once_per_distinct_broker() -> None:
    log: list = []
    shared = HookBroker("shared", log)
    solo = HookBroker("solo", log)
    algos = [
        FakeAlgo("a", "AAPL", log),
        FakeAlgo("b", "AAPL", log),
        FakeAlgo("c", "AAPL", log),
    ]
    router = MappingRouter({"a": shared, "b": shared, "c": solo})
    build(FakeFeed(["AAPL"], rows=2), router, algos).run_backtest()
    on_bars = [e for e in log if e[0] == "on_bar"]
    assert [(e[1]) for e in on_bars] == ["shared", "solo", "shared", "solo"]
    assert len([e for e in log if e[0] == "on_end"]) == 2


def test_on_end_called_once_with_last_bars() -> None:
    log: list = []
    broker = HookBroker("x", log)
    feed = FakeFeed(["AAPL", "MSFT"], rows=3)
    build(feed, broker, [FakeAlgo("a", "AAPL", log)]).run_backtest()
    ends = [e for e in log if e[0] == "on_end"]
    assert len(ends) == 1
    assert log[-1] is ends[0]  # after the last update
    last = list(feed)[-1][1]
    assert ends[0][2] == last
    assert list(ends[0][2]) == ["AAPL", "MSFT"]


def test_ledger_marked_after_algos_each_timestamp() -> None:
    ledger = Ledger()
    ledger.open_account("a", "AAPL", 1000)
    seen: list[int] = []

    class Spy(FakeAlgo):
        def update(self, frame: pd.DataFrame) -> None:
            seen.append(len(ledger.equity_points))  # points stored before this bar's mark
            super().update(frame)

    feed = FakeFeed(["AAPL"], rows=3)
    build(feed, SimBroker(), [Spy("a", "AAPL")], ledger=ledger).run_backtest()
    # 2 points (account + total) per mark; each update saw the previous marks only
    assert seen == [0, 2, 4]
    times = [p.time for p in ledger.equity_points]
    assert times == [T0, T0, T0 + STEP, T0 + STEP, T0 + 2 * STEP, T0 + 2 * STEP]


def test_ledger_finish_closes_open_position_at_last_close_with_commission() -> None:
    ledger = Ledger()
    recorder = ledger.open_account("a", "AAPL", 10_000)
    recorder.fill("r", side="buy", quantity=10, price=100, commission=0, time=T0, reason="entry")
    charges: list[tuple[str, Decimal]] = []

    def commission(idname: str, quantity: Decimal) -> Decimal:
        charges.append((idname, quantity))
        return Decimal("1.5")

    feed = FakeFeed(["AAPL"], rows=3)
    build(feed, SimBroker(), [FakeAlgo("a", "AAPL")], ledger=ledger,
          end_commission=commission).run_backtest()
    last_time = T0 + 2 * STEP
    (trip,) = ledger.round_trips
    assert trip.synthetic and trip.exit_reason == "end_of_data"
    assert trip.exit_price == Decimal("102.0000")
    assert charges == [("a", Decimal("10"))]
    assert trip.net == Decimal("18.5000")  # 10 * (102 - 100) - 1.5
    final = [p for p in ledger.equity_points if p.time == last_time]
    assert len(final) == 2  # replaced, not duplicated: account + total
    assert final[-1].equity == ledger.equity()


def test_ledger_finish_without_commission_is_free() -> None:
    ledger = Ledger()
    recorder = ledger.open_account("a", "AAPL", 10_000)
    recorder.fill("r", side="buy", quantity=10, price=100, commission=0, time=T0, reason="entry")
    build(FakeFeed(["AAPL"]), SimBroker(), [FakeAlgo("a", "AAPL")], ledger=ledger).run_backtest()
    assert ledger.round_trips[0].commission == Decimal("0.0000")


def test_warmup_bars_not_sent_to_brokers() -> None:
    log: list = []
    ledger = Ledger()
    ledger.open_account("a", "AAPL", 1000)
    feed = FakeFeed(["AAPL"], rows=2, warmup_rows=4)
    algo = FakeAlgo("a", "AAPL", log)
    build(feed, HookBroker("x", log), [algo], ledger=ledger).run_backtest()
    closes = [e[3] for e in log if e[0] == "on_bar"]
    assert closes == [100.0, 101.0]  # no warm-up close (50..53)
    assert len(ledger.equity_points) == 4  # 2 timestamps, account + total
    assert algo.frames == [5, 6]  # algos still see the warm-up as history


def test_plain_simbroker_has_no_bar_hook_and_runs_unchanged() -> None:
    broker = SimBroker()
    assert not isinstance(broker, BarAwareBroker)
    algo = FakeAlgo("a", "AAPL")
    engine = build(FakeFeed(["AAPL"], rows=3), broker, [algo])
    engine.run_backtest()
    assert algo.frames == [3, 4, 5]
    assert algo.broker is broker
    assert broker.calls == ()


def test_zero_post_warmup_bars_does_nothing_and_does_not_crash() -> None:
    log: list = []
    ledger = Ledger()
    ledger.open_account("a", "AAPL", 1000)
    build(FakeFeed(["AAPL"], rows=0), HookBroker("x", log), [FakeAlgo("a", "AAPL", log)],
          ledger=ledger).run_backtest()
    assert log == []
    assert ledger.equity_points == ()


def test_finish_with_open_position_and_no_close_is_not_reachable_through_the_engine() -> None:
    # The engine always passes the last close of every feed symbol, so a position
    # on a feed symbol is closed; a position on a symbol outside the feed is a
    # wiring bug and Ledger.finish says so.
    ledger = Ledger()
    recorder = ledger.open_account("a", "ZZZ", 1000)
    recorder.fill("r", side="buy", quantity=1, price=10, commission=0, time=T0, reason="entry")
    engine = build(FakeFeed(["AAPL"]), SimBroker(), [FakeAlgo("a", "AAPL")], ledger=ledger)
    with pytest.raises(ValueError, match="no close"):
        engine.run_backtest()


# --- real offline data through CsvBarFeed + SimBroker + Ledger ----------------

REAL_CASES = [
    ("EMACrossing", {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"},
     "emacrossing_aapl"),
    ("FridayFrenzy", {"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3}, "fridayfrenzy_tsla"),
]


class RecordingBroker(SimBroker):
    """Bar-aware SimBroker that records hook calls into a shared event list."""

    def __init__(self, events: list) -> None:
        super().__init__()
        self.events = events
        self.n_end = 0

    def on_bar(self, symbol: str, bar: Bar) -> None:
        self.events.append(("on_bar", symbol, bar.date))

    def on_end(self, last_bars) -> None:
        self.n_end += 1
        self.events.append(("on_end", dict(last_bars)))


def _real_engine(specs, broker, ledger=None, max_rows=None, **kwargs):
    from pathlib import Path

    from MainStoinker.engine.algos import load_algo
    from MainStoinker.engine.feed import CsvBarFeed
    from tests import harness

    symbols = list(dict.fromkeys(cfg["ticker"] for _, cfg in specs))
    harness.stage_offline_data(Path.cwd(), symbols, max_rows=max_rows)
    algos = [load_algo(a, cfg) for a, cfg in specs]
    feed = CsvBarFeed(Path.cwd() / "OfflineData", symbols, 2)
    engine = Engine(EngineConfig(), feed, broker, NullSink(), algos, ledger=ledger, **kwargs)
    return feed, algos, engine


REAL_ROWS = 900  # 390 warm-up rows and 510 replayed bars: enough for the marks, a fraction of the cost


@pytest.mark.slow
@pytest.mark.parametrize("algo_id, cfg, golden_id", REAL_CASES)
def test_real_data_trades_equal_golden_through_the_engine_and_ledger(algo_id, cfg, golden_id) -> None:
    """Full data through `Engine` + `CsvBarFeed` + `SimBroker` + `Ledger`: the golden trades.

    Slow because the same trades are already pinned by the fast `test_golden_backtests.py`
    (engine, ledger attached) and the slice test below checks the ledger marks.
    """
    import json

    from tests import harness
    from tests.conftest import GOLDEN_DIR

    ledger = Ledger()
    ledger.open_account(cfg["idname"], cfg["ticker"], 10_000)
    _feed, algos, engine = _real_engine([(algo_id, cfg)], SimBroker(), ledger)
    engine.run_backtest()
    expected = json.loads((GOLDEN_DIR / f"{golden_id}.json").read_text())
    assert harness.trade_records(algos[0]) == expected


@pytest.mark.parametrize("algo_id, cfg, golden_id", REAL_CASES)
def test_real_data_ledger_marks(algo_id, cfg, golden_id) -> None:
    ledger = Ledger()
    ledger.open_account(cfg["idname"], cfg["ticker"], 10_000)
    feed, algos, engine = _real_engine([(algo_id, cfg)], SimBroker(), ledger, max_rows=REAL_ROWS)
    engine.run_backtest()
    batches = list(feed)
    assert len(batches) > 100
    points = ledger.equity_points
    assert len(points) == 2 * len(batches)  # account + total per post-warm-up timestamp
    assert [p.time for p in points[::2]] == [t for t, _ in batches]
    assert points[-1].time == batches[-1][0]
    # SimBroker does not report to the ledger yet: flat at capital throughout
    assert {p.equity for p in points} == {Decimal("10000.0000")}
    warm_last = feed.warmup(cfg["ticker"])["date"].iloc[-1]
    assert all(p.time > warm_last for p in points)


def test_real_data_hooks_order_multi_symbol_and_end_bars() -> None:
    events: list = []
    broker = RecordingBroker(events)
    specs = [
        ("EMACrossing", {"idname": "AAPL-e", "ticker": "AAPL", "short": "10", "long": "30"}),
        ("FridayFrenzy", {"idname": "MSFT-ff", "ticker": "MSFT", "RRRatio": 3}),
    ]
    from pathlib import Path

    from MainStoinker.engine.algos import load_algo
    from MainStoinker.engine.feed import CsvBarFeed
    from tests import harness

    harness.stage_offline_data(Path.cwd(), ["AAPL", "MSFT"], max_rows=REAL_ROWS)
    algos = [load_algo(a, c) for a, c in specs]
    for algo in algos:
        real = algo.update

        def spy(frame, _real=real, _algo=algo):
            events.append(("update", _algo.ticker, frame["date"].iloc[-1]))
            return _real(frame)

        algo.update = spy
    feed = CsvBarFeed(Path.cwd() / "OfflineData", ["AAPL", "MSFT"], 2)
    Engine(EngineConfig(), feed, broker, NullSink(), algos).run_backtest()
    batches = list(feed)
    on_bars = [e for e in events if e[0] == "on_bar"]
    assert len(on_bars) == 2 * len(batches)
    assert broker.n_end == 1
    assert events[-1][0] == "on_end"
    assert events[-1][1] == batches[-1][1]
    warm_last = max(feed.warmup(s)["date"].iloc[-1] for s in ("AAPL", "MSFT"))
    assert all(e[2] > warm_last for e in events if e[0] in ("on_bar", "update"))
    # per timestamp: both on_bars (feed order) strictly before that timestamp's updates
    chunk = 4
    body = events[:-1]
    assert len(body) == chunk * len(batches)
    for i, (stamp, _) in enumerate(batches):
        group = body[i * chunk:(i + 1) * chunk]
        assert [(e[0], e[1]) for e in group] == [
            ("on_bar", "AAPL"), ("on_bar", "MSFT"), ("update", "AAPL"), ("update", "MSFT")]
        assert all(e[2] == stamp for e in group)


def test_two_engine_runs_in_one_process_do_not_leak_state() -> None:
    results = []
    for _ in range(2):
        ledger = Ledger()
        ledger.open_account("a", "AAPL", 1000)
        build(FakeFeed(["AAPL"]), RecordingBroker([]), [FakeAlgo("a", "AAPL")],
              ledger=ledger).run_backtest()
        results.append(len(ledger.equity_points))
    assert results == [6, 6]


# --- edge cases ------------------------------------------------------------------


def test_missing_name_in_mapping_router_names_the_algo() -> None:
    with pytest.raises(KeyError, match="no broker routed for algo 'zz'"):
        build(FakeFeed(["AAPL"]), MappingRouter({}), [FakeAlgo("zz", "AAPL")])


def test_non_broker_from_router_injects_nothing_into_any_algo() -> None:
    algos = [FakeAlgo("a", "AAPL"), FakeAlgo("b", "AAPL"), FakeAlgo("c", "AAPL")]
    router = MappingRouter({"a": SimBroker(), "b": SimBroker(), "c": None})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        build(FakeFeed(["AAPL"]), router, algos)
    assert not any(hasattr(a, "broker") for a in algos)


def test_two_algos_sharing_one_bar_aware_broker_get_one_hook_stream() -> None:
    events: list = []
    shared = RecordingBroker(events)
    algos = [FakeAlgo("a", "AAPL"), FakeAlgo("b", "AAPL")]
    build(FakeFeed(["AAPL"], rows=3), shared, algos).run_backtest()
    assert len([e for e in events if e[0] == "on_bar"]) == 3
    assert shared.n_end == 1


def test_ledger_finish_error_propagates_not_swallowed() -> None:
    class Boom(Ledger):
        def finish(self, *args, **kwargs):
            raise RuntimeError("finish boom")

    with pytest.raises(RuntimeError, match="finish boom"):
        build(FakeFeed(["AAPL"]), SimBroker(), [FakeAlgo("a", "AAPL")], ledger=Boom()).run_backtest()


def test_end_commission_and_closes_reach_finish_for_a_short() -> None:
    seen: dict = {}

    class Spy(Ledger):
        def finish(self, time, closes, commission=None):
            seen.update(time=time, closes=dict(closes), commission=commission)
            return super().finish(time, closes, commission)

    ledger = Spy()
    recorder = ledger.open_account("a", "AAPL", 10_000)
    recorder.fill("r", side="sell", quantity=5, price=110, commission=0, time=T0, reason="entry")

    def commission(idname: str, quantity: Decimal) -> Decimal:
        return Decimal("2")

    feed = FakeFeed(["AAPL", "MSFT"], rows=3)
    build(feed, SimBroker(), [FakeAlgo("a", "AAPL")], ledger=ledger,
          end_commission=commission).run_backtest()
    assert seen["commission"] is commission
    assert seen["time"] == T0 + 2 * STEP
    assert seen["closes"] == {"AAPL": 102.0, "MSFT": 112.0}
    (trip,) = ledger.round_trips
    assert trip.side == "short" and trip.net == Decimal("38.0000")  # 5 * (110 - 102) - 2


def test_exception_in_on_bar_propagates_and_stops_the_run() -> None:
    class Bad(RecordingBroker):
        def on_bar(self, symbol: str, bar: Bar) -> None:
            raise RuntimeError("on_bar boom")

    algo = FakeAlgo("a", "AAPL")
    with pytest.raises(RuntimeError, match="on_bar boom"):
        build(FakeFeed(["AAPL"]), Bad([]), [algo]).run_backtest()
    assert algo.frames == []


def test_single_post_warmup_bar_marks_once_and_finishes_once() -> None:
    events: list = []
    ledger = Ledger()
    ledger.open_account("a", "AAPL", 1000)
    broker = RecordingBroker(events)
    build(FakeFeed(["AAPL"], rows=1), broker, [FakeAlgo("a", "AAPL")], ledger=ledger).run_backtest()
    assert len(ledger.equity_points) == 2
    assert [e[0] for e in events] == ["on_bar", "on_end"]
