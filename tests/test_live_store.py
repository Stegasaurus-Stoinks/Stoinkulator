"""The incremental live run writer (`MainStoinker.engine.live_store`, issue 192).

The key property is equivalence: a stepped run through `Engine.start_live` / `step` /
`end_live` with the writer attached stores the same records as `backtest --db` stores for
the same bars, except the synthetic end-of-data closes, the status/kind and the live-only
fields. The rest pins the writer's contracts: the bars hand-over, the tail flush after a
failed step, the final equity point, the heartbeat, a failing store, the series, and that
the stored bars replay to the stored fills. Every database lives under `tmp_path`.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from MainStoinker.engine.algos import close_algo_logs
from MainStoinker.engine.cli import (
    _load_algos,
    _maybe_quiet,
    build_parser,
    config_symbols,
    parse_fill_options,
    read_algo_config,
)
from MainStoinker.engine.engine import Engine, EngineConfig, LivePosition
from MainStoinker.engine.feed import Bar, CsvBarFeed
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.live_counts import HEARTBEAT_INTERVAL, LIVE_KINDS, count_key
from MainStoinker.engine.live_store import (
    BUSY_PAUSE,
    BUSY_RETRIES,
    FinishOutcome,
    LiveRunWriter,
    LiveWriterError,
    describe_today_bars,
    today_frame,
)
from MainStoinker.engine.modes import build_router
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.router import MappingRouter
from MainStoinker.engine.run_documents import liveness, open_positions
from MainStoinker.engine.series import extract_series
from MainStoinker.engine.sink import NullSink
from MainStoinker.engine.stats import fmt4
from MainStoinker.engine.store import (
    AlgoMeta,
    DataMeta,
    RunMeta,
    SqliteStore,
    StoreBusyError,
    StoreError,
    open_store_readonly,
)
from tests import harness
from tests.run_support import Env
from tests.test_engine_live import Script, bar, warm

NO_ENV: dict[str, str] = {}
EMA = {"idname": "{s}-10/30EMA", "ticker": "{s}", "short": "10", "long": "30"}
ROWS = 900
SYMBOLS = ["AAPL", "MSFT", "TSLA"]
T0 = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)


def ema_entries(symbols: list[str]) -> list[dict[str, Any]]:
    return [{"ID": "EMACrossing", "data": [
        {k: v.format(s=s) if isinstance(v, str) else v for k, v in EMA.items()}
        for s in symbols]}]


class Clock:
    """An injected store clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


# ------------------------------------------------------------------ the driver


def run_config(entries: list, costs: CostModel, capital: Decimal) -> dict[str, Any]:
    """What a driver passes as `config`: everything `report --verify` needs."""
    return {"entries": entries, "capital": fmt4(capital),
            "costs": {"commission_per_share": fmt4(costs.commission_per_share),
                      "commission_minimum": fmt4(costs.commission_minimum),
                      "slippage_per_share": fmt4(costs.slippage_per_share)}}


def description_of(symbols: Any) -> dict[str, Any]:
    """A warm-up description that covers `symbols` (the digests are not rebuilt here)."""
    return {"today": "2025-01-02", "trading_days": 1, "symbols": {
        s: {"files": [f"{s}_2025-01.csv"], "days": ["2025-01-01"], "warmup_sha256": "0" * 64}
        for s in symbols}}


class FlakyStore(SqliteStore):
    """A store whose `write_live_step` (and `end_run`) fail on demand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_steps_from: int | None = None  # fail every call number >= this one
        self.fail_steps_count = 0
        self.fail_end_run = False
        self.calls = 0

    def write_live_step(self, run_id: int, step: Any) -> None:
        self.calls += 1
        if self.fail_steps_from is not None and self.calls >= self.fail_steps_from \
                and self.fail_steps_count != 0:
            self.fail_steps_count -= 1
            raise StoreError("disk full (test)")
        super().write_live_step(run_id, step)

    def end_run(self, run_id: int, status: Any, reason: str | None = None) -> None:
        if self.fail_end_run:
            raise StoreError("cannot end (test)")
        super().end_run(run_id, status, reason)


@dataclass
class Driver:
    """A live session the way a driver runs it, over the CSV bars of an `Env`."""

    engine: Engine
    ledger: Ledger
    writer: LiveRunWriter
    store: SqliteStore
    feed: CsvBarFeed
    algos: list
    batches: list
    warmup: dict
    metas: list
    out: Path
    position: int = 0
    run_id: int = 0
    positions: tuple[LivePosition, ...] = ()
    outcome: FinishOutcome | None = None
    fed_times: list = field(default_factory=list)
    fills: str = "legacy"
    entries: list = field(default_factory=list)
    costs: CostModel | None = None
    capital: Decimal = Decimal("10000")

    def start(self) -> int:
        assert self.costs is not None
        self.run_id = self.writer.begin(
            fills=self.fills, code_version="test",
            config=run_config(self.entries, self.costs, self.capital),
            data=DataMeta(source="live", directory=str(self.out),
                          symbols=tuple(self.feed.symbols())),
            algos=self.metas, warmup=self.warmup,
            warmup_description=description_of(self.feed.symbols()))
        self.engine.start_live(self.warmup, on_step=self.writer.on_step)
        return self.run_id

    def step(self, count: int = 1) -> None:
        for _ in range(count):
            timestamp, bars = self.batches[self.position]
            self.position += 1
            self.writer.fed(timestamp, bars)
            self.engine.step(timestamp, bars)
            self.fed_times.append(timestamp)

    def end(self, status: str = "completed", reason: str | None = "session_end",
            counts: dict | None = None) -> FinishOutcome:
        with contextlib.chdir(self.out):
            self.positions = self.engine.end_live()
        first = self.batches[0][0] if self.batches else None
        last = self.fed_times[-1] if self.fed_times else None
        self.outcome = self.writer.finish(
            status, reason,  # type: ignore[arg-type]
            counts if counts is not None else {
                (s, "fed", None): self.position for s in self.feed.symbols()},
            DataMeta(source="live", directory=str(self.out), symbols=tuple(self.feed.symbols()),
                     first_bar=first, last_bar=last, replayed_minutes=self.position,
                     missing_minutes=(), uncovered_minutes=0, warnings=()),
            self.engine.gap_report)
        close_algo_logs(self.algos)
        return self.outcome


def make_driver(env: Env, db: Path, fills: str, *, entries: list | None = None,
                store: SqliteStore | None = None, writer_algos: bool = True) -> Driver:
    harness.reset_runtime_state()
    args = build_parser().parse_args([
        "backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
        "--fills", fills])
    costs, capital = parse_fill_options(args)
    entries = entries or read_algo_config(env.algos)
    feed = CsvBarFeed(env.data, config_symbols(entries), 2)
    ledger = Ledger()
    router = build_router(entries, fills=fills, costs=costs, capital=capital, ledger=ledger)
    out = env.out.parent / "live-out"
    out.mkdir(exist_ok=True)
    algos: list = []
    with contextlib.chdir(out), _maybe_quiet(True):
        _load_algos(entries, None, algos)
    engine = Engine(EngineConfig(), feed, router, NullSink(), algos, ledger=ledger,
                    end_commission=router.end_commission)
    store = store or SqliteStore(db, env=NO_ENV)
    metas = [AlgoMeta(cfg["idname"], entry["ID"], cfg["ticker"], router.modes[cfg["idname"]],
                      capital, cfg) for entry in entries for cfg in entry["data"]]
    return Driver(engine, ledger, LiveRunWriter(store, ledger, engine.algos), store, feed,
                  algos, list(feed), {s: feed.warmup(s) for s in feed.symbols()}, metas, out,
                  fills=fills, entries=entries, costs=costs, capital=capital)


def run_live(env: Env, db: Path, fills: str, **kwargs: Any) -> Driver:
    driver = make_driver(env, db, fills, **kwargs)
    driver.start()
    with contextlib.chdir(driver.out), _maybe_quiet(True):
        driver.step(len(driver.batches))
        driver.end()
    return driver


def setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list, symbols: list[str],
              rows: int = ROWS) -> Env:
    env = Env(tmp_path / "env", monkeypatch)
    env.setup(entries, symbols, rows)
    return env


def backtest(env: Env, db: Path, fills: str) -> None:
    assert env.run("--db", str(db), "--fills", fills) == 0


# ------------------------------------------------------------------ comparison


def stored(db: Path) -> Any:
    store = open_store_readonly(str(db), env=NO_ENV)
    return store, store.load_run(1)


def bars_table(db: Path, run_id: int = 1) -> list[tuple]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT symbol, time, open, high, low, close, volume FROM run_bars WHERE run_id=?"
            " ORDER BY time, symbol", (run_id,)).fetchall()
    finally:
        con.close()


def series_of(store: Any, run: Any) -> dict:
    infos = sorted((i.account, i.name, i.draw_type, i.position) for i in run.series)
    points = {a.idname: sorted((p.name, p.time, p.value) for p in store.load_series(1, a.idname))
              for a in run.algos}
    return {"infos": infos, "points": points}


def equity_rows(run: Any, last: datetime) -> list:
    return [p for p in run.equity if p.time != last]


def assert_same_as_backtest(bt_db: Path, live_db: Path, driver: Driver) -> None:
    bt_store, bt = stored(bt_db)
    lv_store, lv = stored(live_db)
    try:
        assert lv.summary.kind == LIVE_KINDS[0] and bt.summary.kind == "backtest"
        assert lv.summary.status == "completed" and lv.summary.end_reason == "session_end"
        assert lv.algos == bt.algos
        assert len(bt.fills) > 4  # the data does trade
        assert lv.order_events == bt.order_events
        assert lv.fills == tuple(f for f in bt.fills if not f.synthetic)
        assert lv.round_trips == tuple(r for r in bt.round_trips if not r.synthetic)
        assert not any(f.synthetic for f in lv.fills)
        assert lv.ignored_events == bt.ignored_events
        last = driver.ledger.equity_points[-1].time
        assert equity_rows(lv, last) == equity_rows(bt, last)
        assert series_of(lv_store, lv) == series_of(bt_store, bt)
        # the stored bars are exactly the fed bars
        fed = sorted((s, b.date.isoformat(), b.open, b.high, b.low, b.close, float(b.volume))
                     for _, bars in driver.batches for s, b in bars.items())
        assert sorted(((s, t, o, h, lo, c, v) for s, t, o, h, lo, c, v in bars_table(live_db)),
                      key=lambda r: (r[0], r[1])) == sorted(fed, key=lambda r: (r[0], r[1]))
        assert bars_table(bt_db) == []
        # the final equity: one point per series at the last bar time, the ledger's own
        ledger = driver.ledger
        for account in [a.idname for a in ledger.accounts] + [None]:
            mine = [p for p in lv.equity_series(account)]
            assert [p.time for p in mine].count(last) <= 1
            assert mine[-1].time == last and mine[-1].equity == ledger.equity(account)
            assert mine[-1].cash == ledger.cash(account)
            assert len({p.time for p in mine}) == len(mine)  # never a duplicate time
        # the open positions the documents derive equal the engine's
        last_bars = {b.symbol: b for b in lv_store.load_last_bars(1)}
        documents = open_positions(lv, last_bars)
        assert [(d["account"], d["symbol"], d["side"], d["quantity"], d["entry_price"],
                 d["entry_time"], d["stop"], d["target"]) for d in documents] == \
            [(p.account, p.symbol, p.side, fmt4(p.quantity), fmt4(p.entry_price),
              None if p.entry_time is None else p.entry_time.isoformat(),
              None if p.stop is None else fmt4(p.stop),
              None if p.target is None else fmt4(p.target)) for p in driver.positions]
        # and equal the backtest's synthetic closes
        synthetic = [r for r in bt.round_trips if r.synthetic]
        assert sorted((p.account, p.symbol, p.side, p.quantity) for p in driver.positions) == \
            sorted((r.account, r.symbol, r.side, r.quantity) for r in synthetic)
    finally:
        bt_store.close()
        lv_store.close()


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
@pytest.mark.parametrize("symbols", [["AAPL"], SYMBOLS], ids=["one-symbol", "three-symbols"])
def test_live_run_stores_what_backtest_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbols: list[str], fills: str
) -> None:
    env = setup_env(tmp_path, monkeypatch, ema_entries(symbols), symbols)
    backtest(env, tmp_path / "bt.db", fills)
    driver = run_live(env, tmp_path / "live.db", fills)
    assert driver.outcome is not None and driver.outcome.ok
    assert_same_as_backtest(tmp_path / "bt.db", tmp_path / "live.db", driver)
    if fills == "realistic" and len(symbols) == 3:
        assert driver.positions  # a position is open at the end: the final point is exercised


# ------------------------------------------------------------------ mid-run reader, heartbeat


def test_a_reader_mid_run_sees_the_run_up_to_the_last_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = setup_env(tmp_path, monkeypatch, ema_entries(["AAPL"]), ["AAPL"])
    clock = Clock()
    db = tmp_path / "live.db"
    driver = make_driver(env, db, "realistic", store=SqliteStore(db, env=NO_ENV, clock=clock))
    driver.start()
    reader = open_store_readonly(str(db), env=NO_ENV)
    try:
        (summary,) = reader.list_runs()
        assert summary.status == "running" and summary.kind == LIVE_KINDS[0]
        assert summary.last_write_at == T0  # touched by begin
        assert liveness(summary, T0 + timedelta(seconds=5)) == "running"
        with contextlib.chdir(driver.out), _maybe_quiet(True):
            for target in (30, 70, len(driver.batches) - 3):
                clock.now += timedelta(minutes=1)
                driver.step(target - driver.position)
                run = reader.load_run(1)
                assert run.summary.status == "running" and run.summary.ended_at is None
                assert run.summary.last_write_at == clock.now
                assert run.fills == driver.ledger.fills
                assert run.order_events == driver.ledger.orders
                assert run.round_trips == driver.ledger.round_trips
                assert len(bars_table(db)) == driver.position
                (last,) = reader.load_last_bars(1)
                assert last.time == driver.fed_times[-1]
                assert run.equity and run.equity[-1].time <= driver.fed_times[-1]
            assert driver.ledger.fills  # the reader had something to see
            driver.end()
        done = reader.load_run(1)
        assert done.summary.status == "completed"
        assert liveness(done.summary, clock.now) == "completed"
    finally:
        reader.close()


def test_heartbeat_without_bars_updates_the_last_write(tmp_path: Path) -> None:
    clock = Clock()
    ledger = Ledger()
    store = SqliteStore(tmp_path / "hb.db", env=NO_ENV, clock=clock)
    writer = LiveRunWriter(store, ledger, [])
    with pytest.raises(LiveWriterError, match="begin"):
        writer.heartbeat()
    writer.begin(**synth_begin(algos=[]))
    assert store.list_runs()[0].last_write_at == T0
    for _ in range(3):
        clock.now += HEARTBEAT_INTERVAL
        writer.heartbeat()
        summary = store.list_runs()[0]
        assert summary.last_write_at == clock.now and summary.status == "running"
        assert liveness(summary, clock.now + timedelta(seconds=1)) == "running"
    assert bars_table(tmp_path / "hb.db") == []
    clock.now += 7 * HEARTBEAT_INTERVAL
    assert liveness(store.list_runs()[0], clock.now) == "stale"
    writer.heartbeat()
    assert liveness(store.list_runs()[0], clock.now) == "running"
    store.close()


# ------------------------------------------------------------------ synthetic sessions

COSTS = CostModel(Decimal("0.01"), Decimal("1"), Decimal(0))


@dataclass
class Synth:
    engine: Engine
    algo: Script
    ledger: Ledger
    writer: LiveRunWriter
    store: SqliteStore
    db: Path

    def step(self, index: int) -> None:
        self.writer.fed(bar(index).date, {"AAA": bar(index)})
        self.engine.step(bar(index).date, {"AAA": bar(index)})


def synth(tmp_path: Path, actions: dict | None = None, store: SqliteStore | None = None,
          sleep: Callable[[float], None] | None = None) -> Synth:
    from tests.test_engine import ListFeed

    ledger = Ledger()
    algo = Script("a-AAA", "AAA", actions)
    broker = PaperBroker(ledger.open_account("a-AAA", "AAA", 10_000), COSTS)
    engine = Engine(EngineConfig(), ListFeed({"AAA": 1}, 0), MappingRouter({"a-AAA": broker}),
                    NullSink(), [algo], ledger=ledger)
    db = tmp_path / "synth.db"
    store = store or SqliteStore(db, env=NO_ENV)
    writer = LiveRunWriter(store, ledger, engine.algos, sleep=sleep or (lambda _s: None))
    writer.begin(**synth_begin())
    engine.start_live(warm("AAA"), on_step=writer.on_step)
    return Synth(engine, algo, ledger, writer, store, db)


SYNTH_ENTRIES = [{"ID": "Script", "data": [{"idname": "a-AAA", "ticker": "AAA"}]}]
SYNTH_FED = {("AAA", "fed", None): 0}


def synth_begin(**over: Any) -> dict[str, Any]:
    """The arguments of a valid `begin` of the synthetic one-algo session."""
    given: dict[str, Any] = dict(
        fills="realistic", code_version="t",
        config=run_config(SYNTH_ENTRIES, COSTS, Decimal(10_000)),
        data=DataMeta(source="live", directory="bars", symbols=("AAA",)),
        algos=[AlgoMeta("a-AAA", "Script", "AAA", "paper", Decimal(10_000), {})],
        warmup=warm("AAA"), warmup_description=description_of(["AAA"]))
    given.update(over)
    return given


def finish(s: Synth, status: str = "completed", reason: str | None = "session_end",
           counts: dict | None = None) -> FinishOutcome:
    s.engine.end_live()
    return s.writer.finish(status, reason, SYNTH_FED if counts is None else counts,  # type: ignore[arg-type]
                           DataMeta(source="live"), s.engine.gap_report)


def test_the_final_equity_point_includes_fills_made_by_end_live(tmp_path: Path) -> None:
    def close_it(algo: Script, time: pd.Timestamp) -> None:
        algo.broker.close_position(algo.refs[0], price=10.0, time=time)  # type: ignore[attr-defined]

    s = synth(tmp_path, {1: lambda a, t: a.enter(t), 3: close_it})
    for i in range(3):
        s.step(i)
    last_mark = s.ledger.equity_points[-1]
    assert last_mark.time == bar(2).date
    outcome = finish(s)
    assert outcome.ok and outcome.status == "completed"
    # the exit was filled by on_end, after the last mark
    assert [f.reason for f in s.ledger.fills] == ["entry", "algo_exit"]
    assert s.ledger.equity("a-AAA") != last_mark.equity
    run = s.store.load_run(1)
    assert run.fills == s.ledger.fills and run.round_trips == s.ledger.round_trips
    for account in ("a-AAA", None):
        points = run.equity_series(account)
        assert points[-1].time == bar(2).date
        assert points[-1].equity == s.ledger.equity(account)
        assert points[-1].realised == s.ledger.realised_pnl(account)
        assert points[-1].position_value == Decimal("0.0000")
        assert [p.time for p in points].count(bar(2).date) == 1  # replaced, not duplicated
        assert len({p.time for p in points}) == len(points)
    # nothing is stored twice in the raw table either
    con = sqlite3.connect(s.db)
    try:
        assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT algo || time) FROM equity"
                           " WHERE algo IS NOT NULL").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM equity WHERE algo IS NULL AND time=?",
                           (bar(2).date.isoformat(),)).fetchone() == (1,)
    finally:
        con.close()


def test_the_final_point_is_not_added_when_nothing_changed_after_the_last_mark(
    tmp_path: Path
) -> None:
    s = synth(tmp_path)
    for i in range(3):
        s.step(i)
    marks = s.ledger.equity_points
    assert finish(s).ok
    run = s.store.load_run(1)
    assert run.equity_series("a-AAA")[-1].time == bar(2).date
    assert run.equity_series("a-AAA")[-1].equity == marks[-1].equity


def test_finish_flushes_the_tail_after_a_step_the_observer_never_saw(tmp_path: Path) -> None:
    def boom(algo: Script, time: pd.Timestamp) -> None:
        raise RuntimeError("algo boom")

    s = synth(tmp_path, {1: lambda a, t: a.enter(t), 2: boom})
    s.step(0)
    with pytest.raises(RuntimeError, match="algo boom"):
        s.step(1)  # the entry filled in on_bar, then the algo raised: on_step was not called
    assert [f.reason for f in s.ledger.fills] == ["entry"]
    assert s.store.load_run(1).fills == ()  # nothing of the failed step is stored yet
    outcome = finish(s, "failed", "error")
    assert outcome.status == "failed" and outcome.reason == "error" and outcome.errors == ()
    run = s.store.load_run(1)
    assert run.summary.status == "failed" and run.summary.end_reason == "error"
    assert run.fills == s.ledger.fills and len(run.fills) == 1
    assert run.order_events == s.ledger.orders
    assert [p.time for p in run.equity_series(None)][-1] == bar(0).date  # the last mark
    # the open position of the failed run is listed
    assert [d["symbol"] for d in open_positions(run)] == ["AAA"]
    # the failed step's bar was never consumed by a completed step: not stored
    assert [row[1] for row in bars_table(s.db)] == [bar(0).date.isoformat()]


# ------------------------------------------------------------------ the bars hand-over


def test_on_step_without_fed_fails_the_engine_and_stores_nothing(tmp_path: Path) -> None:
    s = synth(tmp_path)
    with pytest.raises(LiveWriterError, match="fed"):
        s.engine.step(bar(0).date, {"AAA": bar(0)})  # the driver forgot writer.fed
    with pytest.raises(RuntimeError, match="failed"):
        s.step(1)
    assert bars_table(s.db) == []
    assert isinstance(s.writer.error, LiveWriterError)
    outcome = finish(s)
    assert outcome.status == "failed" and outcome.reason == "error"
    assert "LiveWriterError" in outcome.errors[0]


@pytest.mark.parametrize("wrong", ["time", "symbols"])
def test_on_step_refuses_bars_that_do_not_match_the_step(tmp_path: Path, wrong: str) -> None:
    s = synth(tmp_path)
    if wrong == "time":
        s.writer.fed(bar(7).date, {"AAA": bar(7)})
    else:
        s.writer.fed(bar(0).date, {"AAA": bar(0), "ZZZ": bar(0)})
    with pytest.raises(LiveWriterError, match="fed|symbols"):
        s.engine.step(bar(0).date, {"AAA": bar(0)})
    assert bars_table(s.db) == []


def test_a_batch_the_engine_refused_is_replaced_by_the_next_fed(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.writer.fed(bar(0).date, {"AAA": bar(0)})
    with pytest.raises(ValueError, match="differs"):
        s.engine.step(bar(0).date, {"AAA": bar(1)})  # refused: nothing applied
    s.step(0)  # the driver tries again with the right batch
    assert [row[1] for row in bars_table(s.db)] == [bar(0).date.isoformat()]
    assert finish(s).ok


# ------------------------------------------------------------------ a failing store


def test_a_store_error_mid_run_ends_the_run_failed_with_everything_written(
    tmp_path: Path
) -> None:
    store = FlakyStore(tmp_path / "synth.db", env=NO_ENV)
    s = synth(tmp_path, {1: lambda a, t: a.enter(t)}, store=store)
    s.step(0)
    store.fail_steps_from, store.fail_steps_count = store.calls + 1, 1
    with pytest.raises(StoreError, match="disk full"):
        s.step(1)  # the writer raises: the engine goes to its failed state
    with pytest.raises(RuntimeError, match="failed"):
        s.step(2)
    original = s.writer.error
    assert isinstance(original, StoreError)
    # the failed transaction stored nothing of that step
    assert [row[1] for row in bars_table(s.db)] == [bar(0).date.isoformat()]
    outcome = finish(s, "completed", "session_end")  # the driver's own verdict is overridden
    assert outcome.status == "failed" and outcome.reason == "error"
    assert "disk full" in outcome.errors[0] and s.writer.error is original
    run = store.load_run(1)
    assert run.summary.status == "failed" and run.summary.end_reason == "error"
    # the tail flush stored what the failed write had not, bars included, once
    assert run.fills == s.ledger.fills and run.order_events == s.ledger.orders
    assert [row[1] for row in bars_table(s.db)] == [
        bar(0).date.isoformat(), bar(1).date.isoformat()]
    assert run.equity_series(None)[-1].time == bar(1).date


def test_finish_never_raises_for_a_dead_store_and_reports_the_original_error(
    tmp_path: Path
) -> None:
    store = FlakyStore(tmp_path / "synth.db", env=NO_ENV)
    s = synth(tmp_path, store=store)
    s.step(0)
    store.fail_steps_from, store.fail_steps_count, store.fail_end_run = store.calls + 1, -1, True
    with pytest.raises(StoreError, match="disk full"):
        s.step(1)
    outcome = finish(s)
    assert not outcome.ok and outcome.status is None
    assert "disk full" in outcome.errors[0]
    assert any("end_run" in e for e in outcome.errors)
    assert isinstance(s.writer.error, StoreError) and "disk full" in str(s.writer.error)
    assert store.load_run(1).summary.status == "running"  # could not be ended: stale by heartbeat
    assert s.writer.finish("completed", None, {}, DataMeta(), s.engine.gap_report) is outcome  # inert


def test_a_refused_end_reason_still_ends_the_run_failed(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    outcome = finish(s, "completed", "no-such-reason")
    assert outcome.status == "failed" and outcome.reason == "error"
    assert s.store.load_run(1).summary.end_reason == "error"
    assert any("end_run" in e for e in outcome.errors)


# ------------------------------------------------------------------ misc contracts


def test_begin_twice_is_refused_and_a_finished_writer_takes_no_more(tmp_path: Path) -> None:
    s = synth(tmp_path)
    with pytest.raises(LiveWriterError, match="already"):
        s.writer.begin(**synth_begin())
    assert finish(s).ok
    with pytest.raises(LiveWriterError, match="finished"):
        s.writer.heartbeat()
    assert s.store.load_run(1).summary.kind == "paper-live"


def test_finish_stores_counts_through_count_key_and_the_full_data_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = setup_env(tmp_path, monkeypatch, ema_entries(["AAPL"]), ["AAPL"])
    driver = make_driver(env, tmp_path / "live.db", "realistic")
    driver.start()
    with contextlib.chdir(driver.out), _maybe_quiet(True):
        driver.step(60)
        driver.end("completed", "minutes_elapsed", {
            ("AAPL", "fed", None): 60, ("AAPL", "late", None): 2,
            ("AAPL", "dropped", "stalled"): 1})
    assert driver.outcome is not None and driver.outcome.ok
    store, run = stored(tmp_path / "live.db")
    try:
        assert store.load_counts(1) == {"symbol": {
            count_key("AAPL", "fed"): 60, count_key("AAPL", "late"): 2,
            count_key("AAPL", "dropped", "stalled"): 1}}
        data = run.summary.data
        assert data.first_bar == driver.batches[0][0] and data.last_bar == driver.batches[59][0]
        assert data.replayed_minutes == 60 and data.symbols == ("AAPL",)
        assert data.gap_rule == driver.engine.gap_report
        assert run.summary.end_reason == "minutes_elapsed"
        assert run.config["warmup"] == description_of(["AAPL"])
        assert run.config["entries"] == driver.entries
    finally:
        store.close()


def test_a_bad_count_key_ends_the_run_failed(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    outcome = finish(s, counts={("AAA", "fed", None): 1, ("AAA", "dropped", "bogus"): 1})
    assert outcome.status == "failed" and outcome.reason == "error"
    assert "counts" in outcome.errors[0]
    assert s.store.load_counts(1) == {}


# ------------------------------------------------------------------ series


def test_incremental_series_equal_the_end_extraction_for_every_configured_algo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = json.loads((Path(__file__).parent.parent / "MainStoinker" / "MainStuff"
                         / "Algo_config.json").read_text(encoding="utf-8"))
    assert {e["ID"] for e in config} == {"FunctionTest", "Elliot"}
    env = setup_env(tmp_path, monkeypatch, config, ["MSFT"], rows=1300)
    backtest(env, tmp_path / "bt.db", "realistic")
    driver = make_driver(env, tmp_path / "live.db", "realistic")
    driver.start()
    delayed: dict[str, int] = {}
    with contextlib.chdir(driver.out), _maybe_quiet(True):
        driver.step(len(driver.batches) - 30)
        mid = open_store_readonly(str(tmp_path / "live.db"), env=NO_ENV)
        try:
            # marker delay (issue 192): each series is held back by the algo's `order` bars
            for algo in driver.algos:
                order = getattr(algo, "order", 0)
                points = mid.load_series(1, algo.name)
                assert points, algo.name
                times = [*driver.warmup["MSFT"]["date"].tolist(), *driver.fed_times]
                assert max(p.time for p in points) <= times[-1 - order]
                delayed[algo.name] = order
        finally:
            mid.close()
        driver.step(30)
        driver.end()
    assert sorted(delayed.values()) == [2, 10]
    bt_store, bt = stored(tmp_path / "bt.db")
    lv_store, lv = stored(tmp_path / "live.db")
    try:
        assert lv.series  # the algos draw
        assert series_of(lv_store, lv) == series_of(bt_store, bt)
        history = [*driver.warmup["MSFT"]["date"].tolist(), *driver.fed_times]
        for algo in driver.algos:
            infos, points = extract_series(algo, history)
            stored_points = lv_store.load_series(1, algo.name)
            assert sorted((p.name, p.time, p.value) for p in points) == sorted(
                (p.name, p.time, p.value) for p in stored_points)
            assert sorted((i.name, i.draw_type, i.position) for i in infos) == sorted(
                (i.name, i.draw_type, i.position) for i in lv.series if i.account == algo.name)
    finally:
        bt_store.close()
        lv_store.close()


# ------------------------------------------------------------------ replaying the stored bars


class StoredBarsFeed:
    """A feed of the bars a live run stored (volumes read back as floats)."""

    def __init__(self, warmups: dict, rows: list[tuple]) -> None:
        self._warmups = warmups
        self._symbols = list(warmups)
        self._rows = rows

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def warmup(self, symbol: str) -> pd.DataFrame:
        return self._warmups[symbol].copy()

    def __iter__(self):
        times: dict[str, dict[str, Bar]] = {}
        for symbol, text, o, h, lo, c, v in self._rows:
            date = pd.Timestamp(text)
            epoch = int(date.tz_localize("America/New_York").timestamp())
            times.setdefault(text, {})[symbol] = Bar(date, epoch, o, h, lo, c, int(round(v)))
        for text in sorted(times):
            batch = times[text]
            yield pd.Timestamp(text), {s: batch[s] for s in self._symbols if s in batch}


def test_the_stored_bars_replay_to_the_stored_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = setup_env(tmp_path, monkeypatch, ema_entries(SYMBOLS), SYMBOLS)
    driver = run_live(env, tmp_path / "live.db", "realistic")
    store, run = stored(tmp_path / "live.db")
    store.close()
    rows = bars_table(tmp_path / "live.db")
    assert len(rows) == sum(len(b) for _, b in driver.batches)
    assert all(isinstance(r[6], float) for r in rows)  # volumes come back as floats
    # replay them through run_backtest with fresh algos
    harness.reset_runtime_state()
    entries = driver.entries
    args = build_parser().parse_args(["backtest", "--algos", str(env.algos), "--data",
                                      str(env.data), "--out", str(env.out), "--fills", "realistic"])
    costs, capital = parse_fill_options(args)
    ledger = Ledger()
    router = build_router(entries, fills="realistic", costs=costs, capital=capital, ledger=ledger)
    algos: list = []
    out = tmp_path / "replay-out"
    out.mkdir()
    with contextlib.chdir(out), _maybe_quiet(True):
        _load_algos(entries, None, algos)
        feed = StoredBarsFeed({s: driver.feed.warmup(s) for s in driver.feed.symbols()}, rows)
        Engine(EngineConfig(), feed, router, NullSink(), algos, ledger=ledger,
               end_commission=router.end_commission).run_backtest()
    close_algo_logs(algos)
    assert run.fills and run.fills == tuple(f for f in ledger.fills if not f.synthetic)
    assert run.round_trips == tuple(r for r in ledger.round_trips if not r.synthetic)
    assert driver.positions  # the replay's synthetic closes are the live open positions
    assert sorted((r.account, r.quantity) for r in ledger.round_trips if r.synthetic) == sorted(
        (p.account, p.quantity) for p in driver.positions)


# ------------------------------------------------------------------ the guards of begin (issue 217)


def bare_writer(tmp_path: Path) -> tuple[LiveRunWriter, SqliteStore]:
    store = SqliteStore(tmp_path / "guards.db", env=NO_ENV)
    return LiveRunWriter(store, Ledger(), []), store


def test_begin_accepts_the_valid_run_and_a_legacy_run_without_costs(tmp_path: Path) -> None:
    writer, store = bare_writer(tmp_path)
    config = run_config(SYNTH_ENTRIES, COSTS, Decimal(10_000))
    config["costs"] = None
    assert writer.begin(**synth_begin(fills="legacy", config=config)) == 1
    assert store.list_runs()[0].kind == "paper-live"
    store.close()


@pytest.mark.parametrize("name,over,words", [
    ("last_bar", dict(data=DataMeta(source="live", directory="bars", symbols=("AAA",),
                                    last_bar=datetime(2025, 1, 2, 9, 31))), "last_bar is set"),
    ("windows path", dict(data=DataMeta(source="live", directory="bars", symbols=("AAA",),
                                        warnings=(r"file C:\Users\x\bars\AAA.csv is odd",))),
     "a warning contains a path"),
    ("posix path", dict(data=DataMeta(source="live", directory="bars", symbols=("AAA",),
                                      warnings=("read /home/x/bars/AAA.csv",))),
     "a warning contains a path"),
    ("no entries", dict(config={"capital": "1.0000", "costs": run_config(
        SYNTH_ENTRIES, COSTS, Decimal(1))["costs"]}), "no `entries`"),
    ("empty entries", dict(config=run_config([], COSTS, Decimal(1))), "no `entries`"),
    ("no capital", dict(config={"entries": SYNTH_ENTRIES, "costs": run_config(
        SYNTH_ENTRIES, COSTS, Decimal(1))["costs"]}, algos=[]), "no capital"),
    ("no costs", dict(config={"entries": SYNTH_ENTRIES, "capital": "1.0000"}), "no `costs`"),
    ("partial costs", dict(config={"entries": SYNTH_ENTRIES, "capital": "1.0000",
                                   "costs": {"commission_per_share": "0.0050"}}), "no `costs`"),
    ("no description", dict(warmup_description=None), "no warm-up description"),
    ("no symbols in description", dict(warmup_description={"today": "2025-01-02"}),
     "has no `symbols`"),
    ("symbol not described", dict(warmup_description=description_of(["ZZZ"])),
     "does not cover AAA"),
    ("description without digest", dict(warmup_description={"symbols": {"AAA": {
        "files": ["a"], "days": ["2025-01-01"]}}}), "does not cover AAA"),
    ("no directory", dict(data=DataMeta(source="live", symbols=("AAA",))), "data.directory"),
    ("no symbols", dict(data=DataMeta(source="live", directory="bars")), "data.symbols is empty"),
    ("other symbols", dict(data=DataMeta(source="live", directory="bars", symbols=("BBB",)),
                           warmup_description=description_of(["BBB"])), "not the algos' tickers"),
])
def test_begin_refuses_a_run_that_could_not_be_verified_or_holds_what_it_must_not(
    tmp_path: Path, name: str, over: dict[str, Any], words: str
) -> None:
    writer, store = bare_writer(tmp_path)
    with pytest.raises(LiveWriterError, match="begin\\(\\) refused") as caught:
        writer.begin(**synth_begin(**over))
    assert words in str(caught.value), (name, str(caught.value))
    assert store.list_runs() == () and writer.run_id is None  # nothing was stored
    store.close()


def test_begin_names_every_problem_at_once(tmp_path: Path) -> None:
    writer, store = bare_writer(tmp_path)
    with pytest.raises(LiveWriterError) as caught:
        writer.begin(**synth_begin(
            config={}, data=DataMeta(source="live", last_bar=datetime(2025, 1, 2)),
            warmup_description=None))
    for words in ("last_bar", "no `entries`", "no `costs`", "data.directory", "data.symbols",
                  "no warm-up description"):
        assert words in str(caught.value)
    store.close()


def today_rows(count: int) -> list[list[Any]]:
    return [[f"2025-01-02 09:{30 + i}:00", 10.0 + i, 10.5 + i, 9.5 + i, 10.25 + i, 100 + i]
            for i in range(count)]


def test_begin_checks_the_today_bars_against_their_digest_and_the_warmup_frames(
    tmp_path: Path
) -> None:
    rows = today_rows(3)
    bars = {"AAA": [Bar(pd.Timestamp(r[0]), 0, *r[1:5], r[5]) for r in rows]}
    described, digests = describe_today_bars(bars)
    assert described == {"AAA": rows} and set(digests) == {"AAA"}
    frames = {"AAA": pd.concat([warm("AAA")["AAA"], today_frame(rows)], ignore_index=True)}
    good = dict(description_of(["AAA"]), today_bars=described, today_sha256=digests)
    writer, store = bare_writer(tmp_path)
    assert writer.begin(**synth_begin(warmup=frames, warmup_description=good)) == 1
    stored_description = store.load_run(1).config["warmup"]
    assert stored_description["today_bars"] == described
    assert stored_description["today_sha256"] == digests
    store.close()
    for number, (bad, words) in enumerate((
        (dict(good, today_sha256={}), "no matching `today_sha256`"),
        (dict(good, today_sha256={"AAA": "0" * 64}), "no matching `today_sha256`"),
        (dict(good, today_bars={"AAA": today_rows(2)}), "no matching `today_sha256`"),
        (dict(good, today_bars="x"), "not a mapping"),
    )):
        writer, store = bare_writer(tmp_path / f"bad{number}")
        with pytest.raises(LiveWriterError, match=words):
            writer.begin(**synth_begin(warmup=frames, warmup_description=bad))
        store.close()
    # the engine's frames must end with exactly those bars
    writer, store = bare_writer(tmp_path / "frames")
    with pytest.raises(LiveWriterError, match="does not end with the 3 today_bars"):
        writer.begin(**synth_begin(warmup=warm("AAA"), warmup_description=good))
    store.close()


# ------------------------------------------------------------------ the guards of finish (issue 217)


def test_finish_takes_the_gap_report_as_a_required_argument(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.step(0)
    s.engine.end_live()
    with pytest.raises(TypeError, match="gap_rule"):
        s.writer.finish("completed", "session_end", SYNTH_FED, DataMeta())  # type: ignore[call-arg]
    with pytest.raises(LiveWriterError, match="gap report"):
        s.writer.finish("completed", "session_end", SYNTH_FED, DataMeta(), None)  # type: ignore[arg-type]
    assert s.store.load_run(1).summary.status == "running"  # nothing was written, nothing lost
    outcome = s.writer.finish("completed", "session_end", SYNTH_FED, DataMeta(),
                              s.engine.gap_report)
    assert outcome.ok
    assert s.store.load_run(1).summary.data.gap_rule == s.engine.gap_report


def test_finish_stores_the_gap_report_whatever_the_data_meta_says(tmp_path: Path) -> None:
    from MainStoinker.engine.gaprule import GapRuleReport

    s = synth(tmp_path)
    s.step(0)
    s.engine.end_live()
    outcome = s.writer.finish("completed", "session_end", SYNTH_FED,
                              DataMeta(source="live", gap_rule=GapRuleReport()), s.engine.gap_report)
    assert outcome.ok
    assert s.store.load_run(1).summary.data.gap_rule == s.engine.gap_report


def test_finish_flags_missing_fed_counts_and_a_completed_run_is_ended_failed(
    tmp_path: Path
) -> None:
    s = synth(tmp_path)
    s.step(0)
    outcome = finish(s, "completed", "session_end", counts={("AAA", "late", None): 0})
    assert not outcome.ok and outcome.status == "failed" and outcome.reason == "error"
    assert "no fed count for AAA" in outcome.errors[0]
    run = s.store.load_run(1)
    assert (run.summary.status, run.summary.end_reason) == ("failed", "error")
    assert s.store.load_counts(1) == {"symbol": {count_key("AAA", "late"): 0}}


def test_finish_flags_missing_fed_counts_of_a_failed_run_without_changing_its_reason(
    tmp_path: Path
) -> None:
    s = synth(tmp_path)
    s.step(0)
    outcome = finish(s, "failed", "interrupted", counts={})
    assert not outcome.ok and outcome.status == "failed" and outcome.reason == "interrupted"
    assert "no fed count for AAA" in outcome.errors[0]


# ------------------------------------------------------------------ a busy database (issue 217)


class Holder:
    """A second real connection that holds the database's write lock until `release()`."""

    def __init__(self, db: Path) -> None:
        self._con = sqlite3.connect(db, isolation_level=None)
        self.held = False

    def hold(self) -> None:
        self._con.execute("BEGIN IMMEDIATE")
        self.held = True

    def release(self) -> None:
        if self.held:
            self._con.execute("ROLLBACK")
            self.held = False

    def close(self) -> None:
        self.release()
        self._con.close()


class Napper:
    """The injected sleep: records every pause and releases the lock on the n-th one."""

    def __init__(self, holder: Holder | None = None, release_on: int | None = None) -> None:
        self.holder, self.release_on = holder, release_on
        self.pauses: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)
        if self.holder is not None and self.release_on == len(self.pauses):
            self.holder.release()


def busy_store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "synth.db", env=NO_ENV, busy_timeout=0.05)


def test_a_locked_database_raises_a_busy_error_and_other_failures_do_not(tmp_path: Path) -> None:
    store = busy_store(tmp_path)
    run_id = store.begin_run(RunMeta(kind="paper-live", fills="realistic", code_version="t"))
    holder = Holder(tmp_path / "synth.db")
    holder.hold()
    try:
        with pytest.raises(StoreBusyError) as caught:
            store.touch(run_id)
        assert isinstance(caught.value, StoreError)  # callers that catch StoreError still do
    finally:
        holder.close()
    with pytest.raises(StoreError) as other:
        store.touch(999)  # no such run: not a busy database
    assert not isinstance(other.value, StoreBusyError)
    store.touch(run_id)  # the connection is usable again
    store.close()


def test_a_step_write_is_retried_while_the_database_is_busy_and_nothing_is_lost_or_doubled(
    tmp_path: Path
) -> None:
    holder = Holder(tmp_path / "synth.db")
    store = busy_store(tmp_path)
    nap = Napper(holder, release_on=2)  # the lock goes on the second pause
    s = synth(tmp_path, {1: lambda a, t: a.enter(t), 3: lambda a, t: a.enter(t)}, store, sleep=nap)
    try:
        s.step(0)
        s.step(1)
        holder.hold()
        s.step(2)  # busy twice, then written, inside this one step
        assert nap.pauses == [BUSY_PAUSE, BUSY_PAUSE] and s.writer.error is None
        s.step(3)
        outcome = finish(s, counts={("AAA", "fed", None): 4})
    finally:
        holder.close()
    assert outcome.ok and nap.pauses == [BUSY_PAUSE, BUSY_PAUSE]
    run = s.store.load_run(1)
    assert run.fills == s.ledger.fills and run.order_events == s.ledger.orders
    assert len(run.fills) >= 1
    assert [r[1] for r in bars_table(s.db)] == [bar(i).date.isoformat() for i in range(4)]
    con = sqlite3.connect(s.db)
    try:  # no equity point twice
        assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT algo || time) FROM equity"
                           " WHERE algo IS NOT NULL").fetchone() == (0,)
    finally:
        con.close()
    store.close()


def test_a_database_that_stays_busy_fails_the_step_after_the_bounded_retries(
    tmp_path: Path
) -> None:
    holder = Holder(tmp_path / "synth.db")
    store = busy_store(tmp_path)
    nap = Napper()
    s = synth(tmp_path, store=store, sleep=nap)
    try:
        s.step(0)
        holder.hold()
        with pytest.raises(StoreBusyError):
            s.step(1)
        assert nap.pauses == [BUSY_PAUSE] * BUSY_RETRIES
        assert sum(nap.pauses) == pytest.approx(BUSY_RETRIES * BUSY_PAUSE) and sum(nap.pauses) <= 5.0
        assert isinstance(s.writer.error, StoreBusyError)
    finally:
        holder.close()
    store.close()


def test_finish_retries_every_part_and_succeeds_when_the_lock_goes(tmp_path: Path) -> None:
    holder = Holder(tmp_path / "synth.db")
    store = busy_store(tmp_path)
    nap = Napper(holder, release_on=1)
    s = synth(tmp_path, {1: lambda a, t: a.enter(t)}, store, sleep=nap)
    try:
        for i in range(3):
            s.step(i)
        holder.hold()
        outcome = finish(s, counts={("AAA", "fed", None): 3})
    finally:
        holder.close()
    assert outcome.ok and outcome.status == "completed" and not outcome.busy
    assert nap.pauses == [BUSY_PAUSE]
    run = s.store.load_run(1)
    assert run.summary.status == "completed" and run.fills == s.ledger.fills
    assert run.summary.data.gap_rule == s.engine.gap_report
    assert s.store.load_counts(1) == {"symbol": {count_key("AAA", "fed"): 3}}
    store.close()


def test_finish_retries_end_run_when_the_lock_comes_back_just_before_it(tmp_path: Path) -> None:
    holder = Holder(tmp_path / "synth.db")
    nap = Napper(holder, release_on=2)

    class LockBeforeEnd(SqliteStore):
        def update_run_data(self, run_id: int, data: DataMeta) -> None:
            super().update_run_data(run_id, data)
            holder.hold()  # the dashboard grabs the database between two parts of finish

    store = LockBeforeEnd(tmp_path / "synth.db", env=NO_ENV, busy_timeout=0.05)
    s = synth(tmp_path, store=store, sleep=nap)
    try:
        s.step(0)
        outcome = finish(s, counts={("AAA", "fed", None): 1})
    finally:
        holder.close()
    assert outcome.ok and outcome.status == "completed"
    assert nap.pauses == [BUSY_PAUSE, BUSY_PAUSE]
    assert s.store.load_run(1).summary.status == "completed"
    store.close()


def test_finish_with_a_database_that_never_frees_is_bounded_and_says_the_run_is_still_running(
    tmp_path: Path
) -> None:
    holder = Holder(tmp_path / "synth.db")
    store = busy_store(tmp_path)
    nap = Napper()
    s = synth(tmp_path, store=store, sleep=nap)
    try:
        s.step(0)
        holder.hold()
        outcome = finish(s, counts={("AAA", "fed", None): 1})
        # the first part (the final write) and end_run use all their retries, counts and data one try
        assert len(nap.pauses) == 2 * BUSY_RETRIES and set(nap.pauses) == {BUSY_PAUSE}
    finally:
        holder.close()
    assert not outcome.ok and outcome.status is None and outcome.reason is None and outcome.busy
    assert any("the run could not be closed" in e and "still marked running" in e
               for e in outcome.errors)
    assert s.store.load_run(1).summary.status == "running"  # what the outcome says is what is stored
    assert s.writer.finish("completed", None, {}, DataMeta(), s.engine.gap_report) is outcome
    store.close()


def test_the_retry_constants_are_a_few_seconds_in_total() -> None:
    assert BUSY_RETRIES >= 3 and 0 < BUSY_PAUSE <= 2.0
    assert BUSY_RETRIES * BUSY_PAUSE <= 5.0
