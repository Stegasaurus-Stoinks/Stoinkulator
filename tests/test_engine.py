"""Engine.run_backtest: same trades and same frames as the legacy path."""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from MainStoinker.engine import engine as engine_module
from MainStoinker.engine.bars import BarStore
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import COLUMNS, Bar, CsvBarFeed
from MainStoinker.engine.sink import NullSink, RecordingSink
from tests import harness
from tests.test_golden_backtests import CASES

GOLDEN_DIR = Path(__file__).parent / "golden"

Spec = tuple[str, dict]
MULTI_ROWS = 1000
TWO_ALGO_ROWS = 900


def _is_slow(case: object) -> bool:
    return any(mark.name == "slow" for mark in case.marks)  # type: ignore[attr-defined]


FAST_CASES = [c for c in CASES if not _is_slow(c)]
# The two cheapest golden cases stay in the fast suite for checks that re-run a
# full backtest; the rest of those re-runs go to the slow suite.
_CHEAP_IDS = {"openingrangecandle_aapl", "fridayfrenzy_tsla"}
SUPPLIED_BROKER_CASES = [
    c if c.id in _CHEAP_IDS else pytest.param(*c.values, id=c.id, marks=pytest.mark.slow)
    for c in FAST_CASES
]

EMA_AAPL: Spec = (
    "EMACrossing",
    {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"},
)
FUNC_MSFT: Spec = ("FunctionTest", {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3})
EMA_A: Spec = ("EMACrossing", {"idname": "AAPL-10/30", "ticker": "AAPL", "short": "10", "long": "30"})
EMA_B: Spec = ("EMACrossing", {"idname": "AAPL-20/40", "ticker": "AAPL", "short": "20", "long": "40"})


# ---------------------------------------------------------------- run helpers


@contextmanager
def scratch_runtime(path: Path) -> Iterator[None]:
    """chdir into `path` with safe config and fresh legacy state, restoring cwd after."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        harness.apply_safe_config()
        harness.reset_runtime_state()
        yield
    finally:
        harness.reset_runtime_state()
        os.chdir(previous)


def symbols_of(specs: Sequence[Spec]) -> list[str]:
    return list(dict.fromkeys(cfg["ticker"] for _, cfg in specs))


def both_runs(
    specs: Sequence[Spec], workdir: Path, max_rows: int | None = None
) -> tuple[list[list[dict]], list[list[dict]]]:
    """(legacy records per algo, engine records per algo) on data staged in workdir."""
    harness.stage_offline_data(workdir, symbols_of(specs), max_rows=max_rows)
    with scratch_runtime(workdir):
        legacy = [harness.trade_records(a) for a in harness.run_mixed_legacy_offline_backtest(specs)]
    with scratch_runtime(workdir):
        engine = [harness.trade_records(a) for a in harness.run_mixed_offline_backtest(specs)]
    return legacy, engine


# ------------------------------------------------------------ synthetic fakes


class ListFeed:
    """In-memory BarFeed: fixed warm-up frames and batches."""

    def __init__(self, warmup_rows: dict[str, int], n_batches: int) -> None:
        self._symbols = list(warmup_rows)
        self._warmup_rows = warmup_rows
        self._n_batches = n_batches
        self.warmup_calls = 0

    @staticmethod
    def _bar(i: int) -> Bar:
        return Bar(
            pd.Timestamp("2030-01-01 09:30:00") + pd.Timedelta(minutes=i),
            1_900_000_000 + 60 * i,
            1.0 + i,
            2.0 + i,
            0.5 + i,
            1.5 + i,
            100 + i,
        )

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def warmup(self, symbol: str) -> pd.DataFrame:
        self.warmup_calls += 1
        n = self._warmup_rows[symbol]
        return pd.DataFrame.from_records([self._bar(-i - 1).as_record() for i in reversed(range(n))])

    def __iter__(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar]]]:
        for i in range(self._n_batches):
            bar = self._bar(i)
            yield bar.date, {s: bar for s in self._symbols}


class SpyAlgo:
    """Records every engine interaction into a shared log."""

    def __init__(self, name: str, ticker: str, log: list) -> None:
        self.name = name
        self.ticker = ticker
        self.broker: object | None = None
        self.trades: list = []
        self._log = log

    def update(self, frame: pd.DataFrame) -> None:
        self._log.append(("update", self.name, self.ticker, len(frame)))

    def update_frontend(self) -> dict:
        self._log.append(("frontend", self.name, self.ticker))
        return {"idname": self.name}


class LoggingSink:
    def __init__(self, log: list, wants_algo_data: bool = False) -> None:
        self.wants_algo_data = wants_algo_data
        self._log = log

    def on_bar(self, symbol: str, bar: Bar, algo_data: list | None) -> None:
        self._log.append(("bar", symbol, bar.time, algo_data))

    def on_intrabar(self, symbol: str, bar: Bar) -> None:
        raise AssertionError("backtest must not emit intrabar events")


@pytest.fixture
def logged_store(monkeypatch: pytest.MonkeyPatch):
    """Make the engine's BarStore write its appends into a shared log."""
    log: list = []

    class LoggedStore(BarStore):
        def __init__(self, warmup: pd.DataFrame) -> None:
            super().__init__(warmup)
            self.symbol = "?"

        def append(self, bar: Bar) -> None:
            log.append(("append", bar.time))
            super().append(bar)

    monkeypatch.setattr(engine_module, "BarStore", LoggedStore)
    return log


# --------------------------------------------------------------- unit tests


def test_update_order_append_all_then_update_all(logged_store: list) -> None:
    log = logged_store
    feed = ListFeed({"AAA": 3, "BBB": 5}, n_batches=2)
    # registration order interleaves symbols on purpose
    algos = [
        SpyAlgo("b1", "BBB", log),
        SpyAlgo("a1", "AAA", log),
        SpyAlgo("b2", "BBB", log),
        SpyAlgo("a2", "AAA", log),
    ]
    sink = LoggingSink(log)
    Engine(EngineConfig(), feed, SimBroker(), sink, algos).run_backtest()
    t0, t1 = ListFeed._bar(0).time, ListFeed._bar(1).time
    one_batch = lambda t, n_a, n_b: [  # noqa: E731
        ("append", t),
        ("append", t),
        ("update", "a1", "AAA", n_a),
        ("update", "a2", "AAA", n_a),
        ("bar", "AAA", t, None),
        ("update", "b1", "BBB", n_b),
        ("update", "b2", "BBB", n_b),
        ("bar", "BBB", t, None),
    ]
    assert log == one_batch(t0, 4, 6) + one_batch(t1, 5, 7)


def test_update_and_frontend_are_interleaved_per_algo(logged_store: list) -> None:
    log = logged_store
    feed = ListFeed({"AAA": 2}, n_batches=1)
    algos = [SpyAlgo("a1", "AAA", log), SpyAlgo("a2", "AAA", log)]
    sink = LoggingSink(log, wants_algo_data=True)
    Engine(EngineConfig(), feed, SimBroker(), sink, algos).run_backtest()
    t0 = ListFeed._bar(0).time
    assert log == [
        ("append", t0),
        ("update", "a1", "AAA", 3),
        ("frontend", "a1", "AAA"),
        ("update", "a2", "AAA", 3),
        ("frontend", "a2", "AAA"),
        ("bar", "AAA", t0, [{"idname": "a1"}, {"idname": "a2"}]),
    ]


def test_no_update_during_warmup() -> None:
    log: list = []
    feed = ListFeed({"AAA": 50}, n_batches=3)
    algo = SpyAlgo("a", "AAA", log)
    Engine(EngineConfig(), feed, SimBroker(), NullSink(), [algo]).run_backtest()
    # exactly one update per replay bar, the first already sees warm-up + 1 rows
    assert [entry[3] for entry in log] == [51, 52, 53]


def test_broker_injected_on_registration() -> None:
    log: list = []
    broker = SimBroker()
    algos = [SpyAlgo("a", "AAA", log), SpyAlgo("b", "BBB", log)]
    assert all(a.broker is None for a in algos)
    engine = Engine(EngineConfig(), ListFeed({"AAA": 1, "BBB": 1}, 1), broker, NullSink(), algos)
    assert all(a.broker is broker for a in algos)
    assert engine.algos == tuple(algos)
    assert log == []  # nothing ran yet


def test_algo_for_unknown_symbol_raises() -> None:
    algo = SpyAlgo("a", "ZZZ", [])
    with pytest.raises(ValueError, match="ZZZ"):
        Engine(EngineConfig(), ListFeed({"AAA": 1}, 1), SimBroker(), NullSink(), [algo])
    assert algo.broker is None  # a failed construction does not touch the algo


@pytest.mark.parametrize("not_a_broker", [None, object(), True])
def test_engine_refuses_to_run_without_a_broker(not_a_broker: object) -> None:
    # Without a broker, Trade would fall back to the legacy live-trading flag.
    algo = SpyAlgo("a", "AAA", [])
    with pytest.raises(TypeError, match="requires a Broker"):
        Engine(EngineConfig(), ListFeed({"AAA": 1}, 1), not_a_broker, NullSink(), [algo])  # type: ignore[arg-type]
    assert algo.broker is None


def test_same_algo_twice_raises() -> None:
    algo = SpyAlgo("a", "AAA", [])
    with pytest.raises(ValueError, match="twice"):
        Engine(EngineConfig(), ListFeed({"AAA": 1}, 1), SimBroker(), NullSink(), [algo, algo])


def test_symbol_without_algo_is_stored_and_published() -> None:
    log: list = []
    feed = ListFeed({"AAA": 2, "BBB": 2}, n_batches=2)
    algo = SpyAlgo("a", "AAA", log)
    Engine(EngineConfig(), feed, SimBroker(), LoggingSink(log, wants_algo_data=True), [algo]).run_backtest()
    bars = [e for e in log if e[0] == "bar"]
    assert [(e[1], e[3]) for e in bars] == [
        ("AAA", [{"idname": "a"}]),
        ("BBB", []),
        ("AAA", [{"idname": "a"}]),
        ("BBB", []),
    ]
    assert [e[0] for e in log if e[0] != "bar"] == ["update", "frontend"] * 2
    log.clear()
    feed = ListFeed({"AAA": 2, "BBB": 2}, 1)
    Engine(EngineConfig(), feed, SimBroker(), LoggingSink(log), [algo]).run_backtest()
    assert [e[3] for e in log if e[0] == "bar"] == [None, None]


def test_engine_with_no_algos_runs_and_publishes() -> None:
    sink = RecordingSink()
    Engine(EngineConfig(), ListFeed({"AAA": 1}, 3), SimBroker(), sink, []).run_backtest()
    assert [e.kind for e in sink.events] == ["bar"] * 3


def test_run_twice_raises_and_does_not_replay() -> None:
    log: list = []
    algo = SpyAlgo("a", "AAA", log)
    engine = Engine(EngineConfig(), ListFeed({"AAA": 1}, 2), SimBroker(), NullSink(), [algo])
    engine.run_backtest()
    assert len(log) == 2
    with pytest.raises(RuntimeError, match="once"):
        engine.run_backtest()
    assert len(log) == 2


def test_sink_receives_one_bar_event_per_symbol_per_batch_in_order() -> None:
    sink = RecordingSink()
    feed = ListFeed({"BBB": 2, "AAA": 4}, n_batches=3)
    Engine(EngineConfig(), feed, SimBroker(), sink, [SpyAlgo("a", "AAA", [])]).run_backtest()
    events = sink.events
    assert all(e.kind == "bar" for e in events)
    assert [e.symbol for e in events] == ["BBB", "AAA"] * 3
    assert [e.bar.time for e in events] == [ListFeed._bar(i).time for i in (0, 0, 1, 1, 2, 2)]
    assert all(e.algo_data is None for e in events)


def test_each_symbol_warmup_requested_once() -> None:
    feed = ListFeed({"AAA": 1, "BBB": 1}, 2)
    Engine(EngineConfig(), feed, SimBroker(), NullSink(), []).run_backtest()
    assert feed.warmup_calls == 2


def test_update_frontend_only_when_sink_wants_it() -> None:
    def frontend_calls(sink: object) -> tuple[int, list]:
        log: list = []
        feed = ListFeed({"AAA": 1}, 2)
        algo = SpyAlgo("a", "AAA", log)
        Engine(EngineConfig(), feed, SimBroker(), sink, [algo]).run_backtest()  # type: ignore[arg-type]
        return sum(1 for e in log if e[0] == "frontend"), log

    assert frontend_calls(NullSink())[0] == 0
    off = RecordingSink(wants_algo_data=False)
    assert frontend_calls(off)[0] == 0
    assert [e.algo_data for e in off.events] == [None, None]
    on = RecordingSink(wants_algo_data=True)
    assert frontend_calls(on)[0] == 2
    assert [e.algo_data for e in on.events] == [[{"idname": "a"}]] * 2


def test_no_sleep_no_threads_no_globals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.MainStuff import Globals

    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=420)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden call during engine run")

    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(ibapi, "startData", forbidden)
    threads_before = threading.active_count()
    algos = harness.run_mixed_offline_backtest([EMA_AAPL])
    assert threading.active_count() == threads_before
    assert Globals.tickers == {}
    assert Globals.algos == []
    assert len(algos[0].AlgoData) == 420  # it really ran (warm-up 390 rows + 30 bars)


def test_frames_identical_to_legacy_each_step(tmp_path: Path) -> None:
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=700)

    def recorder(frames: list) -> Callable[[object], None]:
        def wrap(algo: object) -> None:
            original = algo.update  # type: ignore[attr-defined]

            def update(frame: pd.DataFrame) -> None:
                frames.append(frame.copy())
                original(frame)

            algo.update = update  # type: ignore[attr-defined]

        return wrap

    legacy_frames: list[pd.DataFrame] = []
    engine_frames: list[pd.DataFrame] = []
    with scratch_runtime(tmp_path):
        harness.run_mixed_legacy_offline_backtest([EMA_AAPL], wrap=recorder(legacy_frames))
    with scratch_runtime(tmp_path):
        harness.run_mixed_offline_backtest([EMA_AAPL], wrap=recorder(engine_frames))
    assert len(legacy_frames) == len(engine_frames) == 700 - 390
    for step, (legacy, ours) in enumerate(zip(legacy_frames, engine_frames, strict=True)):
        try:
            assert_frame_equal(ours, legacy, check_dtype=True, check_index_type=True)
        except AssertionError as exc:  # name the step
            raise AssertionError(f"frame differs at step {step}: {exc}") from exc
        assert list(ours.columns) == list(COLUMNS)


def test_frame_passed_to_algo_is_isolated_from_the_store(tmp_path: Path) -> None:
    """An algo that scribbles on its frame cannot change what later algos or steps see."""
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=420)
    seen: list[pd.DataFrame] = []

    class Scribbler(SpyAlgo):
        def update(self, frame: pd.DataFrame) -> None:
            seen.append(frame.copy())
            frame["close"] = -1.0

    algo = Scribbler("s", "AAPL", [])
    feed = CsvBarFeed(tmp_path / "OfflineData", ["AAPL"])
    Engine(EngineConfig(), feed, SimBroker(), NullSink(), [algo]).run_backtest()
    closes = [f["close"].iloc[-1] for f in seen]
    assert all(c > 0 for c in closes)
    assert all((f["close"] > 0).all() for f in seen)


# ------------------------------------------- engine versus legacy, real algos


def golden_records(case_id: str) -> list[dict]:
    """The pinned trades for a golden case (read-only; this file never writes goldens)."""
    return json.loads((GOLDEN_DIR / f"{case_id}.json").read_text())


@pytest.mark.parametrize("algo_id, cfg", SUPPLIED_BROKER_CASES)
def test_supplied_simbroker_gives_golden_trades_and_records_the_orders(
    algo_id: str, cfg: dict, request: pytest.FixtureRequest
) -> None:
    """An explicitly supplied SimBroker changes nothing, and sees every trade."""
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    broker = SimBroker()
    (algo,) = harness.run_offline_backtest(algo_id, [cfg], broker=broker)
    records = harness.trade_records(algo)
    assert records == golden_records(request.node.callspec.id)
    methods = [c.method for c in broker.calls]
    assert methods.count("open_position") == len(records)
    closed = sum(1 for r in records if r["status"] == "Closed")
    assert methods.count("close_position") >= closed


@pytest.fixture(scope="module")
def multi_symbol_runs(tmp_path_factory: pytest.TempPathFactory):
    workdir = tmp_path_factory.mktemp("multi")
    specs = [EMA_AAPL, FUNC_MSFT]
    legacy, engine = both_runs(specs, workdir, max_rows=MULTI_ROWS)
    return specs, workdir, legacy, engine


def test_multi_symbol_trades_match_legacy(multi_symbol_runs) -> None:
    _, _, legacy, engine = multi_symbol_runs
    assert len(legacy) == 2
    assert all(records for records in legacy), "both algos should trade, or the test proves little"
    assert engine == legacy


@pytest.mark.slow
def test_engine_run_with_recording_sink_requesting_algo_data_gives_same_trades(multi_symbol_runs) -> None:
    specs, workdir, legacy, _ = multi_symbol_runs
    sink = RecordingSink(wants_algo_data=True)
    with scratch_runtime(workdir):
        algos = harness.run_mixed_offline_backtest(specs, sink=sink)
        records = [harness.trade_records(a) for a in algos]
    assert records == legacy
    bar_events = [e for e in sink.events if e.kind == "bar"]
    assert len(bar_events) == 2 * (MULTI_ROWS - 390)
    assert all(e.algo_data is not None and len(e.algo_data) == 1 for e in bar_events)


def test_two_algos_on_one_symbol_match_legacy(tmp_path: Path) -> None:
    legacy, engine = both_runs([EMA_A, EMA_B], tmp_path, max_rows=TWO_ALGO_ROWS)
    assert legacy[0] != legacy[1]
    assert all(records for records in legacy)
    assert engine == legacy


def test_algo_data_matches_legacy_frontend_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The algo_data the sink gets equals the `algodata` the legacy update_algos emits."""
    from MainStoinker.MainStuff.Globals import config
    from MainStoinker.Util.SocketIO_Client import frontend_client

    specs = [EMA_A, EMA_B]
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=520)
    payloads: list = []
    monkeypatch.setattr(frontend_client, "emit_data", lambda event, payload: payloads.append(payload))
    def enable_frontend(_algo: object) -> None:
        # The runner applies the safe config first, so switch it on afterwards.
        object.__setattr__(config, "FrontEndDisplay", True)

    with scratch_runtime(tmp_path):
        try:
            harness.run_mixed_legacy_offline_backtest(specs, wrap=enable_frontend)
        finally:
            object.__setattr__(config, "FrontEndDisplay", False)
    legacy_algodata = [p["algodata"] for p in payloads if "algodata" in p]
    assert len(legacy_algodata) == 520 - 390
    assert all(len(entry) == 2 for entry in legacy_algodata)

    sink = RecordingSink(wants_algo_data=True)
    with scratch_runtime(tmp_path):
        harness.run_mixed_offline_backtest(specs, sink=sink)
    assert [e.algo_data for e in sink.events] == legacy_algodata
