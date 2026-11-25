"""The live paper session driver (`MainStoinker/ib/live_run.py`, issue 197), on the fake.

Everything here is real except the broker connection: `IbConnection` and `IbMarketData` over
`FakeIbClient` (no socket), the real `Watcher` driven through the `Rig` of
`tests/test_ib_watch_bars.py` (scripted server time and stream, injected clock and sleep, no real
sleep), the real `LiveBarAligner`, `Engine` with the real EMACrossing algo, `PaperBroker`,
`LiveRunWriter` and a `SqliteStore` under `tmp_path`. A session starts at 09:30:10 on 2024-08-16
unless a test says otherwise; the month files hold the two sessions before it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from MainStoinker.engine.algos import close_algo_logs
from MainStoinker.engine.cli import build_engine, build_router
from MainStoinker.engine.cli import main as engine_main
from MainStoinker.engine.engine import Engine
from MainStoinker.engine.feed import COLUMNS, Bar, write_month_file
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.live_counts import HEARTBEAT_INTERVAL, parse_count_key
from MainStoinker.engine.live_feed import LiveBarAligner
from MainStoinker.engine.paper_broker import CostModel
from MainStoinker.engine.store import RunData, RunMeta, SqliteStore
from MainStoinker.engine.verify import StoredBarsFeed
from MainStoinker.engine.warmup import live_warmup
from MainStoinker.ib import live_run
from MainStoinker.ib.connection import ConnState
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.watch_bars import StreamClock, StreamState
from tests import harness
from tests.fake_ib import ConnectBehavior
from tests.not_paper_text import assert_one_not_paper_line
from tests.test_ib_import_boundary import imported_modules
from tests.test_ib_watch_bars import (  # noqa: F401  (`real_home_untouched` is an autouse fixture)
    DATA_CALLS,
    LIVE_LOOKING,
    Rig,
    ep,
    real_home_untouched,
    upd_at,
)

SYMBOLS = ("AAPL", "MSFT", "TSLA")
COSTS = CostModel(Decimal("0.005"), Decimal("1.00"), Decimal("0"))
FIRST = ep(9, 30)  # the first minute of the scripted session
WARMUP_DAYS = 1
LIVE_RUN = Path(live_run.__file__)


# -- the warm-up month files and the scripted session -------------------------


def _base(symbol: str) -> float:
    return 100.0 + 10.0 * SYMBOLS.index(symbol)


def _close_of(symbol: str, k: int) -> float:
    """Close of minute k of the scripted day (k = 0 is 09:30): flat, up (a golden cross), then down."""
    base = _base(symbol)
    if k < 8:
        return round(base, 2)
    if k <= 21:
        return round(base + 0.4 * (k - 7), 2)
    return round(max(base + 5.6 - 0.8 * (k - 21), base - 3.0), 2)


def write_warmup_files(bars_dir: Path, symbols: tuple[str, ...] = SYMBOLS) -> None:
    """Two full sessions (2024-08-14 and 15, 390 bars) that fall, so the EMAs start below."""
    for symbol in symbols:
        rows = []
        n = 0
        for day in (14, 15):
            for minute in range(390):
                stamp = pd.Timestamp(2024, 8, day, 9, 30) + pd.Timedelta(minutes=minute)
                close = round(_base(symbol) + 5.0 - 5.0 * n / 780.0, 4)
                prev = round(_base(symbol) + 5.0 - 5.0 * max(n - 1, 0) / 780.0, 4)
                epoch = int(stamp.tz_localize("America/New_York").timestamp())
                rows.append((stamp, epoch, prev, max(prev, close) + 0.05, min(prev, close) - 0.05,
                             close, 1000 + minute))
                n += 1
        write_month_file(bars_dir / symbol / f"{symbol}_2024-08.csv",
                         pd.DataFrame(rows, columns=list(COLUMNS)))


def minute_ohlcv(symbol: str, k: int) -> tuple[float, float, float, float, int]:
    o = _close_of(symbol, k - 1) if k else _base(symbol)
    c = _close_of(symbol, k)
    return (o, round(max(o, c) + 0.05, 2), round(min(o, c) - 0.05, 2), c, 1000 + 10 * k)


def _partial(final: tuple[float, float, float, float, int]) -> tuple[float, float, float, float, int]:
    o, _h, _lo, _c, v = final
    return (o, o, o, o, max(1, v // 10))


def script_minutes(rig: Rig, symbols: tuple[str, ...], first: int, last: int, *,
                   delay: dict[str, int] | None = None, quiet: tuple[int, int] | None = None,
                   skip_symbol_from: tuple[str, int] | None = None) -> None:
    """Two updates per minute and symbol: one 4 s into the minute, the final one at 50 s. The bar of
    minute k closes when the first update of minute k + 1 arrives. `delay` shifts a symbol's updates
    (seconds), `quiet` (server epochs) drops every update inside the window, `skip_symbol_from`
    stops one symbol's updates from a minute on."""
    for k in range(first, last + 1):
        for symbol in symbols:
            if skip_symbol_from is not None and symbol == skip_symbol_from[0] and k >= skip_symbol_from[1]:
                continue
            shift = (delay or {}).get(symbol, 0)
            final = minute_ohlcv(symbol, k)
            for secs, ohlcv in ((4, _partial(final)), (50, final)):
                when = FIRST + 60 * k + secs + shift
                if quiet is not None and quiet[0] <= when <= quiet[1]:
                    continue
                upd_at(rig, symbol, FIRST + 60 * k, when, ohlcv)


def set_backfills(rig: Rig, symbols: tuple[str, ...] = SYMBOLS, last_closed: int = -1) -> None:
    """IB's snapshot at the request: the minutes up to `last_closed` closed and the next one forming."""
    for symbol in symbols:
        closed = [(FIRST + 60 * k, *minute_ohlcv(symbol, k)) for k in range(last_closed + 1)]
        forming = (FIRST + 60 * (last_closed + 1), *_partial(minute_ohlcv(symbol, last_closed + 1)))
        rig.backfills[symbol] = [[*closed, forming]]


def ticks(rig: Rig, first: int, last: int, every: int = 10) -> None:
    """No-op steps so that the watcher takes a look at these server times (epochs)."""
    for when in range(first, last + 1, every):
        rig.at(when - rig.server_epoch, lambda: None)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path, SYMBOLS)
    r.server_epoch = ep(9, 30, 10)
    r.backfills = {}
    set_backfills(r)
    write_warmup_files(tmp_path / "bars")
    yield r
    for conn in r.connections:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


def ema_entries(symbols: tuple[str, ...] = SYMBOLS, mode: str = "paper") -> list[dict[str, Any]]:
    return [{"ID": "EMACrossing", "data": [
        {"idname": f"{s}-10/30EMA", "ticker": s, "short": "10", "long": "30", "mode": mode}
        for s in symbols]}]


def live_args(tmp: Path, **over: Any) -> argparse.Namespace:
    values: dict[str, Any] = dict(
        paper=True, host="127.0.0.1", port=7497, client_id=20, connect_timeout=30.0,
        entries=ema_entries(), bars=tmp / "bars", warmup_days=WARMUP_DAYS, capital=Decimal("10000"),
        costs=COSTS, fills="realistic", out=tmp / "live", record=None, private=(), minutes=32,
        run_out=tmp / "run-out", stop_file=tmp / "STOP", code_version="test", quiet=True)
    values.update(over)
    return argparse.Namespace(**values)


@dataclass
class Result:
    code: int
    store: SqliteStore
    db: Path
    args: argparse.Namespace

    def run(self, number: int = 1) -> RunData:
        return self.store.load_run(number)

    def counts(self, number: int = 1) -> dict[tuple[str, str, str | None], int]:
        found: dict[tuple[str, str, str | None], int] = {}
        for scope, rows in self.store.load_counts(number).items():
            for key, value in rows.items():
                parsed = parse_count_key(scope, key)
                assert parsed is not None, (scope, key)
                found[(parsed.symbol, parsed.kind, parsed.reason)] = value
        return found

    def times(self, symbol: str, number: int = 1) -> list[str]:
        return [b.time.strftime("%H:%M") for b in self.store.load_bars(number, symbol)]


def run_session(rig: Rig, tmp: Path, *, db: Path | None = None, store: SqliteStore | None = None,
                store_clock: Callable[[], datetime] | None = None,
                **over: Any) -> Result:
    """Run `run_live_session` on the rig; the store stamps with the rig's wall clock."""
    path = db or tmp / "runs.db"
    opened = store or SqliteStore(path, clock=store_clock or rig.wall, env={})
    args = live_args(tmp, **over)
    code = live_run.run_live_session(
        args, connection_factory=rig.connection_factory, market_data_factory=rig.market_data_factory,
        recorder_factory=rig.recorder_factory, store=opened, clock=rig, sleep=rig.sleep,
        wall=rig.wall, out=rig.out)
    return Result(code, opened, path, args)


@pytest.fixture
def stores() -> Iterator[list[SqliteStore]]:
    opened: list[SqliteStore] = []
    yield opened
    for store in opened:
        store.close()


def session(rig: Rig, tmp: Path, stores: list[SqliteStore], **over: Any) -> Result:
    result = run_session(rig, tmp, **over)
    stores.append(result.store)
    return result


def standard_script(rig: Rig, last: int = 29, minutes: int = 32) -> None:
    script_minutes(rig, SYMBOLS, 0, last + 1)  # the update that opens minute last + 1 closes `last`
    rig.end_at(minutes, symbols=3)


def verify_stored(result: Result, tmp: Path, number: int = 1) -> int:
    """`report RUN --verify`, the command (read-only on the same database)."""
    return engine_main(["report", str(number), "--db", str(result.db), "--verify", "--bars",
                        str(tmp / "bars")], env={})


def backtest_of_fed_bars(result: Result, tmp: Path, number: int = 1) -> Ledger:
    """The stored bars through `run_backtest` (the engine's pull mode) with the stored config."""
    run = result.run(number)
    today = pd.Timestamp(run.summary.data.first_bar).date()
    frames, _description, _warnings = live_warmup(tmp / "bars", list(SYMBOLS), today, WARMUP_DAYS)
    stored = sorted((b for s in SYMBOLS for b in result.store.load_bars(number, s)),
                    key=lambda b: (b.time, b.symbol))
    ledger = Ledger()
    router = build_router(run.config["entries"], fills="realistic", costs=COSTS,
                          capital=Decimal("10000"), ledger=ledger)
    algos: list[Any] = []
    harness.reset_runtime_state()
    with contextlib.chdir(tmp / "run-out"), contextlib.redirect_stdout(io.StringIO()):
        engine = build_engine(run.config["entries"], StoredBarsFeed(frames, stored), router, ledger, algos)
        engine.run_backtest()
    close_algo_logs(algos)
    return ledger


def _fills(source: Any, *, synthetic: bool = False) -> list[tuple[Any, ...]]:
    return [(f.time, f.symbol, f.side, f.quantity, f.price) for f in source.fills
            if f.synthetic is synthetic]


def stop_at(rig: Rig, tmp: Path, when: tuple[int, float]) -> None:
    rig.at(rig.tt(when[0], when[1]), lambda: (tmp / "STOP").write_text("stop", encoding="utf-8"))


# -- (a) a scripted three-symbol session --------------------------------------


def test_synthetic_session_trades_equal_backtest_of_fed_bars(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig)
    result = session(rig, tmp_path, stores)
    text = rig.text
    assert result.code == 0, text
    summary = result.store.list_runs()[0]
    assert (summary.kind, summary.fills, summary.status, summary.end_reason) == (
        "paper-live", "realistic", "completed", "minutes_elapsed")
    run = result.run()
    # every closed minute of every symbol was fed, one batch per minute
    assert result.times("AAPL") == [f"09:{m}" for m in range(30, 60)]
    assert result.counts()[("AAPL", "fed", None)] == 30
    assert run.summary.data.replayed_minutes == 30
    assert run.fills and run.round_trips, "the scripted golden cross, then the cross down, must trade"
    assert all(f.synthetic is False for f in run.fills)
    # the same fills and round trips as the engine's backtest of the stored bars
    ledger = backtest_of_fed_bars(result, tmp_path)
    assert _fills(run) == _fills(ledger)
    assert [(t.symbol, t.entry_time, t.exit_time, t.net) for t in run.round_trips if not t.synthetic] == [
        (t.symbol, t.entry_time, t.exit_time, t.net) for t in ledger.round_trips if not t.synthetic]
    # the stored run replays exactly: report --verify (the command) is VERIFIED
    assert "verify: VERIFIED" in text
    assert verify_stored(result, tmp_path) == 0
    assert "Live session: completed, end reason minutes_elapsed (exit 0)" in text


def test_the_run_stores_what_backtest_stores_so_that_it_can_be_verified(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    begun: list[Any] = []
    from MainStoinker.engine.live_store import LiveRunWriter
    real_begin = LiveRunWriter.begin

    def spy(self: Any, **kw: Any) -> int:
        begun.append(kw)
        return real_begin(self, **kw)

    monkeypatch.setattr(LiveRunWriter, "begin", spy)
    standard_script(rig, last=5, minutes=8)
    result = session(rig, tmp_path, stores, minutes=8)
    assert result.code == 0, rig.text
    (given,) = begun
    data = given["data"]
    assert data.last_bar is None and data.replayed_minutes is None  # nothing is known at the start
    assert data.source == "ib-live" and data.symbols == SYMBOLS and data.directory == str(tmp_path / "bars")
    assert all("\\" not in w and "/" not in w for w in data.warnings)  # no path in a warning
    assert given["fills"] == "realistic"
    config = given["config"]
    assert config["capital"] == "10000.0000" and set(config["costs"]) == {
        "commission_per_share", "commission_minimum", "slippage_per_share"}
    assert config["entries"] == ema_entries()
    assert set(given["warmup_description"]["symbols"]) == set(SYMBOLS)
    run = result.run()
    assert run.config["warmup"]["trading_days"] == WARMUP_DAYS
    final = run.summary.data  # the full DataMeta, stored at the end
    assert final.last_bar is not None and final.replayed_minutes == 6
    assert final.gap_rule is not None and final.directory == str(tmp_path / "bars")
    assert dict(final.missing_minutes or ()) == dict.fromkeys(SYMBOLS, 0)
    assert [a.mode for a in run.algos] == ["paper"] * 3 and run.summary.fills == "realistic"


# -- (b) a connection closed mid-run --------------------------------------------


def test_connection_closed_mid_run_ends_with_reason_and_open_positions(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    script_minutes(rig, SYMBOLS, 0, 20)
    rig.drop((45, 20.0), "connectionClosed")
    rig.at(rig.tt(45, 40.0), lambda: None)
    result = session(rig, tmp_path, stores, minutes=20)
    text = rig.text
    assert result.code == 4, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "connection_closed")
    assert "STOPPED: the connection to TWS closed." in text
    assert text.count("OPEN POSITION (left open)") == 3 and "3 position(s) left OPEN" in text
    assert result.run().fills and not result.run().round_trips  # entered at 09:40, never closed
    assert all(c.state is ConnState.CLOSED for c in rig.connections)
    # still verifiable, up to its bars
    assert "verify: VERIFIED" in text and "the run ended early" in text
    assert verify_stored(result, tmp_path) == 0
    assert result.times("AAPL")[-1] == "09:44"  # the bar of 09:45 was still forming


# -- (c) an outage (code 1100) ----------------------------------------------------


def outage(rig: Rig, down: tuple[int, float] = (38, 40.0), up: tuple[int, float] = (42, 40.0),
           last: int = 29) -> None:
    """Code 1100 at `down`, 1102 at `up`: no update arrives in between; the watcher looks every 10 s."""
    start, end = rig.tt(down[0], down[1]) + rig.server_epoch, rig.tt(up[0], up[1]) + rig.server_epoch
    script_minutes(rig, SYMBOLS, 0, last + 1, quiet=(int(start), int(end)))
    rig.drop(down, 1100)
    rig.at(rig.tt(down[0], down[1] + 5), lambda: None, ready=lambda: "connectivity LOST" in rig.text)
    ticks(rig, int(start) + 10, int(end) - 10, 10)
    rig.drop(up, 1102)
    rig.at(rig.tt(up[0], up[1] + 2), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.end_at(last + 3, symbols=3)


def test_1100_outage_minutes_dropped_and_next_bar_follows_gap(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    outage(rig)
    result = session(rig, tmp_path, stores)
    text = rig.text
    assert result.code == 0, text
    run = result.run()
    assert run.summary.status == "completed"
    # minutes 08 and 12 overlapped the outage (dropped), 09 to 11 never existed: 25 bars were fed
    expected = [f"09:{m:02d}" for m in [*range(30, 38), *range(43, 60)]]
    for symbol in SYMBOLS:
        assert result.times(symbol) == expected
        assert result.counts()[(symbol, "fed", None)] == 25
        assert result.counts()[(symbol, "dropped", "connection_lost")] == 2
        assert result.counts()[(symbol, "late", None)] == 0
    assert dict(run.summary.data.missing_minutes or ()) == dict.fromkeys(SYMBOLS, 0)
    assert run.summary.data.uncovered_minutes == 5
    # the bar after the gap: the golden cross shows on it, and the gap rule refuses the entry
    gap = run.summary.data.gap_rule
    assert gap is not None and dict(gap.bars_after_gap) == dict.fromkeys(SYMBOLS, 1)
    assert gap.total_suppressed == 3
    assert not run.fills, "no entry on the bar that follows a gap"
    assert "verify: VERIFIED" in text and verify_stored(result, tmp_path) == 0


# -- (d) not a paper account ----------------------------------------------------


def test_not_paper_mid_run_exit_5_run_failed_reason_not_paper(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    script_minutes(rig, SYMBOLS, 0, 14)
    rig.at(rig.tt(40, 10.0), lambda: rig.fakes[-1].push("managedAccounts", LIVE_LOOKING))
    result = session(rig, tmp_path, stores, minutes=14)
    text = rig.text
    assert result.code == 5, text
    assert_one_not_paper_line(text, "mid")
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "not_paper")
    assert "U7654321" not in text and "DU0000000" not in text
    assert "Live session: failed, end reason not_paper (exit 5)" in text
    assert all(c.state is ConnState.CLOSED for c in rig.connections)


def test_not_paper_at_the_start_exit_5_nothing_requested_nothing_stored(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    rig.accounts = LIVE_LOOKING
    result = session(rig, tmp_path, stores)
    assert result.code == 5, rig.text
    assert_one_not_paper_line(rig.text, "open")
    assert result.store.list_runs() == ()
    names = {c.name for f in rig.fakes for c in f.calls}
    assert not names & {"reqHistoricalData", "reqCurrentTime", "reqContractDetails"}, names
    assert "no run was stored" in rig.text


def test_a_connection_that_cannot_be_made_exits_3_and_stores_no_run(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    rig.behavior = ConnectBehavior.REFUSE
    result = session(rig, tmp_path, stores)
    assert result.code == 3, rig.text
    assert result.store.list_runs() == ()


# -- (e) the stop file -------------------------------------------------------------


def test_stop_file_clean_stop(rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    script_minutes(rig, SYMBOLS, 0, 20)
    stop_at(rig, tmp_path, (45, 5.0))
    rig.end_at(20, symbols=3)
    result = session(rig, tmp_path, stores, minutes=20)
    text = rig.text
    assert result.code == 0, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("completed", "stop_file")
    assert "Stopped on the driver's request (stop_file)." in text
    assert all(c.state is ConnState.CLOSED for c in rig.connections)
    cancelled = [c for f in rig.fakes for c in f.calls if c.name == "cancelHistoricalData"]
    assert len(cancelled) == 3  # every request was cancelled
    assert result.times("AAPL")[-1] == "09:44" and "verify: VERIFIED" in text
    assert text.count("OPEN POSITION (left open)") == 3  # entered at 09:40, listed, never closed


def test_an_existing_stop_file_refuses_the_session_before_anything_is_built(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    (tmp_path / "STOP").write_text("old", encoding="utf-8")
    result = session(rig, tmp_path, stores)
    assert result.code == 2 and "stop file exists already" in rig.text
    assert not rig.connections and result.store.list_runs() == ()


# -- (f) only data requests -------------------------------------------------------


def test_only_data_requests_reach_the_fake(rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig, last=6, minutes=9)
    result = session(rig, tmp_path, stores, minutes=9)
    assert result.code == 0, rig.text
    names = {c.name for f in rig.fakes for c in f.calls}
    assert names <= DATA_CALLS, names
    assert {"reqCurrentTime", "reqContractDetails", "reqHistoricalData", "cancelHistoricalData"} <= names
    # the driver can import nothing that can send an order, and no socket
    allowed = ("MainStoinker.ib.", "MainStoinker.engine.")
    forbidden_roots = ("ibapi", "socket", "ssl", "MainStoinker.DataCollection", "MainStoinker.TradeTools",
                       "MainStoinker.Util", "MainStoinker.MainStuff", "MainStoinker.Algos")
    found = imported_modules(LIVE_RUN.read_text(encoding="utf-8"), str(LIVE_RUN), "MainStoinker.ib")
    assert found
    for line, module in found:
        assert not module.startswith(forbidden_roots), f"line {line}: {module}"
        if module.startswith("MainStoinker"):
            assert module.startswith(allowed), f"line {line}: {module}"
    # it holds the data-only market data object and nothing with an order method
    md = rig.mds[0]
    assert isinstance(md, IbMarketData)
    assert not [n for n in dir(md) if "order" in n.lower() and not n.startswith("_")]


# -- (g) warm-up, backfill, live ----------------------------------------------------


def test_warmup_and_backfill_no_duplicate_or_missing_minute(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """Started at 09:45:00 with 15 closed minutes in IB's snapshot: they are warm-up, the forming one
    is the first bar stepped, and the golden cross inside the warm-up is not traded (D1)."""
    rig.server_epoch = ep(9, 45, 0)
    set_backfills(rig, last_closed=14)
    script_minutes(rig, SYMBOLS, 15, 31)
    rig.end_at(16, symbols=3)
    result = session(rig, tmp_path, stores, minutes=16)
    text = rig.text
    assert result.code == 0, text
    run = result.run()
    for symbol in SYMBOLS:
        stepped = result.times(symbol)
        assert stepped == [f"09:{m}" for m in range(45, 60)], stepped  # none twice, none missing
    today = run.config["warmup"]["today_bars"]
    for symbol in SYMBOLS:
        warm = [row[0][11:16] for row in today[symbol]]
        assert warm == [f"09:{m:02d}" for m in range(30, 45)]
        assert not set(warm) & {t for t in result.times(symbol)}
        assert warm + result.times(symbol) == [f"09:{m}" for m in range(30, 60)]  # the whole session
    assert not run.fills and not run.round_trips, "nothing trades on today's earlier bars (D1)"
    assert run.summary.data.replayed_minutes == 15
    assert "today's closed backfill bars {'AAPL': 15, 'MSFT': 15, 'TSLA': 15}" in text
    # a session started after the open is verified too: the month files plus today's stored bars
    assert "verify: VERIFIED" in text and "not run" not in text
    assert verify_stored(result, tmp_path) == 0
    assert set(run.config["warmup"]["today_sha256"]) == set(SYMBOLS)
    assert result.counts()[("AAPL", "fed", None)] == 15
    gap = run.summary.data.gap_rule
    assert gap is not None and gap.total_bars_after_gap == 0  # the first bar follows the warm-up directly


def test_late_start_today_bars_are_warmup_only(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's frame holds the warm-up and today's bars, but no algo is updated on them."""
    rig.server_epoch = ep(9, 45, 0)
    set_backfills(rig, last_closed=14)
    script_minutes(rig, SYMBOLS, 15, 17)
    rig.end_at(4, symbols=3)
    seen: list[tuple[int, int]] = []
    real_start = Engine.start_live

    def spy(self: Engine, warmup: Any, **kw: Any) -> None:
        seen.append((len(warmup["AAPL"]), int(warmup["AAPL"]["time"].iloc[-1])))
        real_start(self, warmup, **kw)

    monkeypatch.setattr(Engine, "start_live", spy)
    updates: list[int] = []
    real_step = Engine.step

    def counting(self: Engine, ts: Any, bars: Any) -> None:
        if bars:
            algo = next(a for a in self.algos if a.ticker == "AAPL")
            updates.append(len(algo.AlgoData))
        real_step(self, ts, bars)

    monkeypatch.setattr(Engine, "step", counting)
    result = session(rig, tmp_path, stores, minutes=4)
    assert result.code == 0, rig.text
    # one warm-up day plus today's 15 closed bars: the session's own start, then the replay of the
    # end-of-session check (which rebuilds the same warm-up from the month files and the stored bars)
    assert seen == [(390 + 15, FIRST + 60 * 14)] * 2
    first_rows = updates[0]
    assert first_rows == 0  # no algo update happened on the 15 warm-up bars: its data starts empty
    assert result.times("AAPL")[0] == "09:45" and not result.run().fills


def test_a_symbol_that_starts_later_than_the_others_is_not_missing_the_minutes_of_its_warmup(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """TSLA's snapshot was taken a moment later than the others' (the requests are spaced): the
    minute 09:44 closed in between, so it is in TSLA's warm-up and the first live bar of the
    others. The stamp 09:44 has no TSLA bar, but TSLA is not missing it: `missing_minutes` counts
    from a symbol's first fed minute."""
    rig.server_epoch = ep(9, 45, 0)
    set_backfills(rig, ("AAPL", "MSFT"), last_closed=13)
    set_backfills(rig, ("TSLA",), last_closed=14)
    script_minutes(rig, ("AAPL", "MSFT"), 14, 24)
    script_minutes(rig, ("TSLA",), 15, 24)
    rig.end_at(8, symbols=3)
    result = session(rig, tmp_path, stores, minutes=8)
    text = rig.text
    assert result.code == 0, text
    assert result.times("AAPL")[0] == "09:44" and result.times("TSLA")[0] == "09:45", text
    fed = {s: result.counts()[(s, "fed", None)] for s in SYMBOLS}
    assert fed["AAPL"] == fed["MSFT"] == fed["TSLA"] + 1
    assert dict(result.run().summary.data.missing_minutes or ()) == dict.fromkeys(SYMBOLS, 0)
    assert "verify: VERIFIED" in text


class LockingStore(SqliteStore):
    """A store whose database is taken by another connection from its n-th step write on: for
    `release_after` attempts (a dashboard looking at it for a moment), or for good (None)."""

    def __init__(self, *args: Any, lock_at: int, release_after: int | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock_at, self.release_after = lock_at, release_after
        self.writes = 0
        self.attempts = 0
        self.other: Any = None

    def write_live_step(self, run_id: int, step: Any) -> None:
        import sqlite3

        self.writes += 1
        if self.writes == self.lock_at:
            self.other = sqlite3.connect(self._path, isolation_level=None)
            self.other.execute("BEGIN IMMEDIATE")
        if self.other is not None:
            self.attempts += 1
            if self.release_after is not None and self.attempts > self.release_after:
                self.unlock()
        super().write_live_step(run_id, step)

    def unlock(self) -> None:
        if self.other is not None:
            self.other.execute("ROLLBACK")
            self.other.close()
            self.other = None


def test_a_database_held_for_a_moment_does_not_lose_or_double_anything_in_a_session(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig, last=9, minutes=12)
    store = LockingStore(tmp_path / "runs.db", clock=rig.wall, env={}, busy_timeout=0.05,
                         lock_at=4, release_after=2)
    result = session(rig, tmp_path, stores, store=store, minutes=12)
    text = rig.text
    assert result.code == 0, text
    assert store.other is None and store.attempts >= 3  # it was busy, and was written anyway
    assert result.times("AAPL") == [f"09:{m}" for m in range(30, 40)]
    assert result.counts()[("AAPL", "fed", None)] == 10
    assert "Live session: completed, end reason minutes_elapsed (exit 0)" in text
    assert "verify: VERIFIED" in text and verify_stored(result, tmp_path) == 0


def test_a_database_that_stays_held_ends_the_session_and_the_last_line_says_what_is_stored(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig, last=9, minutes=12)
    store = LockingStore(tmp_path / "runs.db", clock=rig.wall, env={}, busy_timeout=0.05,
                         lock_at=4, release_after=None)
    try:
        result = session(rig, tmp_path, stores, store=store, minutes=12)
        text = rig.text
        lines = text.splitlines()
        assert result.code == 4, text
        assert lines[-1].startswith("Live session: the run could not be closed: still marked "
                                    "running in the database"), lines[-3:]
        assert "the database stayed busy" in lines[-1] and "(exit 4)" in lines[-1]
        assert "Live session: failed, end reason" not in text  # never claims what was not stored
        assert "ERROR StoreBusyError" in text or "ERROR StoreError" in text
    finally:
        store.unlock()
    summary = store.list_runs()[0]
    assert summary.status == "running", summary  # what the last line said
    assert len(result.times("AAPL")) == 3  # the three steps before the lock were stored


def test_a_live_bar_not_after_the_symbols_newest_minute_is_not_given_on(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    write_warmup_files(tmp_path / "bars")
    unit = UnitSession(tmp_path, stores)
    bars = [Bar(pd.Timestamp(2024, 8, 16, 9, 30 + k), FIRST + 60 * k, 100.0, 100.5, 99.5, 100.2, 10)
            for k in range(4)]
    unit.session.on_bar("AAPL", bars[0], "backfill")
    unit.session.on_bar("AAPL", bars[1], "backfill")
    unit.session.on_bar("AAPL", bars[1], "later")  # the minute the backfill ended with, again
    unit.session.on_bar("AAPL", bars[0], "later")  # an earlier one
    unit.session.on_bar("AAPL", bars[2], "later")
    assert unit.session.counts.overlap["AAPL"] == 2
    assert unit.session.aligner.refused_counts()["AAPL"] == 0  # the aligner never saw them
    assert [b.time for b in unit.session.warm_today["AAPL"]] == [FIRST, FIRST + 60]
    assert unit.session.aligner.pending_minutes() == 1
    assert sum("not given on" in line for line in unit.lines) == 2


class UnitSession:
    """A `_Session` without a watcher, for the rules that do not need one."""

    def __init__(self, tmp: Path, stores: list[SqliteStore], **over: Any) -> None:
        plan = live_run._plan(live_args(tmp, **over), date(2024, 8, 16))  # noqa: SLF001
        store = SqliteStore(tmp / "unit.db", env={})
        stores.append(store)
        self.lines: list[str] = []
        self.devnull = open(os.devnull, "w")  # noqa: SIM115
        self.session = live_run._Session(  # noqa: SLF001
            plan, store, lambda: 0.0, lambda: datetime(2024, 8, 16, 13, 30, tzinfo=UTC), self.lines.append,
            self.devnull)

    def __del__(self) -> None:
        self.devnull.close()


# -- (h) late bars ---------------------------------------------------------------------


def test_late_bar_written_to_file_not_fed(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock that runs ahead (here 120 s: a wrong time zone or a skewed machine) releases a minute
    when the first symbol's bar is in, so the other symbols' bars of that minute come late. Their
    rows are in the live files, the bars are not fed, they are counted, and the session ends with a
    data fault: a late bar of this kind is never normal."""
    real_poll = LiveBarAligner.poll
    monkeypatch.setattr(LiveBarAligner, "poll", lambda self, now: real_poll(self, now + 120))
    script_minutes(rig, SYMBOLS, 0, 8)
    rig.end_at(10, symbols=3)
    result = session(rig, tmp_path, stores, minutes=10)
    text = rig.text
    assert result.code == 7, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "data_fault")
    assert "late bars that no re-requested backfill explains" in text
    never_fed = 0
    for symbol in ("MSFT", "TSLA"):
        path = tmp_path / "live" / symbol / f"{symbol}_live_2024-08-16.csv"
        if not path.exists():
            continue  # the run ended before this symbol closed a bar
        rows = {int(line.split(",")[1]) for line in path.read_text("utf-8").splitlines()[1:]}
        fed = {int(pd.Timestamp(f"2024-08-16 {t}").tz_localize("America/New_York").timestamp())
               for t in result.times(symbol)}
        late = rows - fed
        never_fed += len(late)  # written to the file, not fed
        assert result.counts()[(symbol, "late", None)] >= len(late)  # counted
        assert result.counts()[(symbol, "fed", None)] == len(fed)
    assert never_fed >= 1


def test_a_late_bar_of_a_requested_backfill_is_counted_and_not_a_fault(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    """The allowance, exactly: late and `backfilled` (a bar a re-requested backfill wrote) is allowed;
    late and `later` or `time` is a fault; late behind the decided point is always a fault."""
    write_warmup_files(tmp_path / "bars")
    unit = UnitSession(tmp_path, stores)
    given = unit.session

    def bar(k: int) -> Bar:
        return Bar(pd.Timestamp(2024, 8, 16, 9, 30 + k), FIRST + 60 * k, 1.0, 1.5, 0.5, 1.2, 1)

    for symbol in SYMBOLS:
        given.on_bar(symbol, bar(0), "later")
    given.on_bar("MSFT", bar(1), "later")  # AAPL has no bar for 09:31, TSLA none for 09:31 and 09:32
    given.on_bar("TSLA", bar(1), "later")
    given.on_bar("TSLA", bar(2), "later")
    released = given.aligner.poll(FIRST + 300)  # the clock is far on: 09:30 to 09:32 go without the others
    assert [m.minute for m, _ in released] == [30, 31, 32]
    # AAPL's 09:31 comes from a re-requested backfill: late, counted, allowed
    given.on_bar("AAPL", bar(1), "backfilled")
    assert given.counts.late_allowed["AAPL"] == 1 and not any(given.counts.late_other.values())
    assert given.aligner.late_counts()["AAPL"] == 1
    # MSFT's 09:32 arrives as an ordinary closed bar after its minute went: a fault
    given.on_bar("MSFT", bar(2), "time")
    assert given.counts.late_other["MSFT"] == 1 and given.counts.late_allowed["MSFT"] == 0
    assert given.aligner.late_after_decided_counts() == dict.fromkeys(SYMBOLS, 0)


def test_decided_before_the_bar_and_the_bar_before_decided_are_both_accepted(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    """The aligner takes the two call orders alike (review of #202/#207); the driver sees no refusal."""
    write_warmup_files(tmp_path / "bars")
    given = UnitSession(tmp_path, stores).session
    first = Bar(pd.Timestamp(2024, 8, 16, 9, 30), FIRST, 1.0, 1.5, 0.5, 1.2, 1)
    second = Bar(pd.Timestamp(2024, 8, 16, 9, 31), FIRST + 60, 1.0, 1.5, 0.5, 1.2, 1)
    given.decided("AAPL", FIRST)  # decided first, then the bar of that minute
    given.on_bar("AAPL", first, "later")
    given.on_bar("AAPL", second, "later")  # the bar first, then decided
    given.decided("AAPL", FIRST + 60)
    assert given.aligner.refused_counts()["AAPL"] == 0
    assert given.aligner.late_counts()["AAPL"] == 0 == given.aligner.late_after_decided_counts()["AAPL"]
    assert given.aligner.pending_minutes() == 2


def test_a_bar_the_aligner_refuses_is_a_fault(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate that reaches the aligner (the driver's own overlap check bypassed) is refused there;
    any refusal ends the session with a data fault."""
    real = live_run._Session.on_bar  # noqa: SLF001

    def twice(self: Any, symbol: str, bar: Bar, how: str) -> None:
        real(self, symbol, bar, how)
        if symbol == "MSFT" and bar.time == FIRST + 180 and how == "later":
            self.aligner.bar(symbol, bar)

    monkeypatch.setattr(live_run._Session, "on_bar", twice)  # noqa: SLF001
    standard_script(rig, last=9, minutes=12)
    result = session(rig, tmp_path, stores, minutes=12)
    assert result.code == 7, rig.text
    assert result.store.list_runs()[0].end_reason == "data_fault"
    assert "the aligner refused bars: {'MSFT': 1}" in rig.text


# -- (i) delayed data --------------------------------------------------------------------


def test_a_delayed_stream_ends_the_run_with_delayed_data(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """At 10:00:05 MSFT's stream is 15 minutes behind, AAPL and TSLA are live."""
    rig.server_epoch = FIRST + 1800 + 5
    set_backfills(rig, ("AAPL", "TSLA"), last_closed=29)
    rig.backfills["MSFT"] = [[(FIRST + 60 * k, *minute_ohlcv("MSFT", k)) for k in range(15)]
                             + [(FIRST + 60 * 15, *_partial(minute_ohlcv("MSFT", 15)))]]
    script_minutes(rig, ("AAPL", "TSLA"), 30, 36)
    script_minutes(rig, ("MSFT",), 15, 21, delay={"MSFT": 900})
    rig.end_at(6, symbols=3)
    result = session(rig, tmp_path, stores, minutes=6)
    text = rig.text
    assert result.code == 7, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "delayed_data"), text
    assert "labelled delayed" in text and "real-time data" in text
    assert "Live session: failed, end reason delayed_data (exit 7)" in text
    # nothing was traded 15 minutes late: the live symbols' minutes were held back for MSFT
    assert result.counts()[("AAPL", "fed", None)] == 0 and not result.run().fills


# -- (j) F1: an unmeasured symbol holds the stream clock --------------------------------


def test_f1_a_symbol_with_no_measured_lag_does_not_let_minutes_go_without_it(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """MSFT's backfill holds only the previous session and its first update comes 15 minutes late:
    its lag is unmeasured. AAPL and TSLA are live. Minutes must not be released without MSFT (they
    would arrive late). A symbol that shows no bar at all is a silent symbol after SILENT_LIMIT
    (600 s), which is before STREAM_WAIT_LIMIT (900 s): the session ends with a data fault."""
    rig.backfills["MSFT"] = [[(ep(9, 30) - 86400 + 60 * k, 99.0, 99.5, 98.8, 99.2, 10) for k in range(5)]]
    script_minutes(rig, ("AAPL", "TSLA"), 0, 12)
    script_minutes(rig, ("MSFT",), 0, 3, delay={"MSFT": 900})
    ticks(rig, FIRST + 20, FIRST + 700, 10)
    rig.end_at(12, symbols=3)
    result = session(rig, tmp_path, stores, minutes=12)
    text = rig.text
    assert result.code == 7, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "data_fault")
    assert all(result.counts()[(s, "fed", None)] == 0 for s in SYMBOLS)  # held, never released alone
    assert all(result.counts()[(s, "late", None)] == 0 for s in SYMBOLS)
    assert "MSFT has shown no bar and no decision" in text


def test_the_stream_clock_may_stay_unmeasured_for_900_seconds_and_not_longer(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    """`no_data` after STREAM_WAIT_LIMIT s with `stream_now` unmeasured while every symbol is alive
    (a symbol that shows nothing is a silent symbol, a data fault, after 600 s)."""
    assert live_run.STREAM_WAIT_LIMIT == 900.0
    write_warmup_files(tmp_path / "bars")
    given = UnitSession(tmp_path, stores).session

    class Quiet:
        def is_down(self) -> bool:
            return False

        def request_stop(self, reason: str) -> None:
            pass

    given.watcher = Quiet()  # type: ignore[assignment]
    states = {s: StreamState(None, False, True) for s in SYMBOLS}
    start = FIRST + 10

    def look(elapsed: float) -> None:
        for symbol in SYMBOLS:
            given.alive_at[symbol] = start + elapsed - 30.0  # alive: not silent
        given._check_faults(StreamClock(start + elapsed, None), states)  # noqa: SLF001

    for elapsed in (0.0, 300.0, 301.0, 600.0, 899.0, 900.0):
        look(elapsed)
        assert given.fault is None, elapsed
    look(901.0)
    assert given.fault is not None and given.fault.reason == "no_data"
    assert given.fault.code == 6 and "for 900 s of the session" in given.fault.message


def test_the_aligner_is_polled_with_the_stream_clock_not_with_server_time(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """Every stream runs about 100 s behind IB's clock (below the delayed-data threshold), and the
    last bar of each symbol is closed by time. The aligner must release a minute only when the
    STREAM has passed it: polled with server time it would release minutes whose bars have not
    arrived yet, and those bars would come late (a data fault)."""
    script_minutes(rig, SYMBOLS, 0, 9, delay=dict.fromkeys(SYMBOLS, 100))
    ticks(rig, FIRST + 700, FIRST + 900, 10)
    rig.end_at(14, symbols=3)
    result = session(rig, tmp_path, stores, minutes=14)
    text = rig.text
    assert result.code == 0, text
    assert result.store.list_runs()[0].end_reason == "minutes_elapsed"
    for symbol in SYMBOLS:
        assert result.times(symbol) == [f"09:{m}" for m in range(30, 40)], text  # the last by time
        assert result.counts()[(symbol, "late", None)] == 0
    assert "late bars" not in text and "came late" not in text


def test_a_halt_of_several_minutes_at_the_open_does_not_end_a_healthy_session(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """No symbol shows a bar of today for 8 minutes (a delayed opening): `stream_now` is unmeasured
    all that time, which is more than the old 300 s limit and less than the present one, and
    the session goes on once the bars flow."""
    for symbol in SYMBOLS:
        rig.backfills[symbol] = [[(ep(9, 30) - 86400 + 60 * k, 99.0, 99.5, 98.8, 99.2, 10)
                                  for k in range(5)]]
    script_minutes(rig, SYMBOLS, 8, 16)
    ticks(rig, FIRST + 20, FIRST + 700, 10)
    rig.end_at(14, symbols=3)
    result = session(rig, tmp_path, stores, minutes=14)
    text = rig.text
    assert result.code == 0, text
    assert result.store.list_runs()[0].end_reason == "minutes_elapsed"
    assert "no lag measured" not in text
    assert result.counts()[("AAPL", "fed", None)] > 0


# -- (k) heartbeat -----------------------------------------------------------------------


def test_heartbeat_during_an_outage_keeps_the_run_from_going_stale(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    stamps: list[datetime] = []

    def stamping() -> datetime:
        now = rig.wall()
        stamps.append(now)
        return now

    outage(rig, down=(38, 40.0), up=(42, 40.0))
    result = session(rig, tmp_path, stores, store_clock=stamping)
    assert result.code == 0, rig.text
    during = [s for s in stamps if ep(9, 38, 40) <= s.timestamp() <= ep(9, 42, 40)]
    assert len(during) >= 8  # touched while nothing arrived: the watcher looked every 10 s
    gaps = [(b - a).total_seconds() for a, b in zip(during, during[1:], strict=False)]
    assert max(gaps) <= HEARTBEAT_INTERVAL.total_seconds(), max(gaps)
    first, last = during[0].timestamp(), during[-1].timestamp()
    assert first - ep(9, 38, 40) <= 30 and ep(9, 42, 40) - last <= 30  # also at both ends of the window


# -- (l) abandoned runs ----------------------------------------------------------------------


def old_run(db: Path, *, kind: str, touched: datetime) -> None:
    store = SqliteStore(db, clock=lambda: touched, env={})
    run = store.begin_run(RunMeta(kind=kind, fills="realistic", code_version="old"))
    store.touch(run)
    store.close()


def test_an_abandoned_earlier_run_is_closed_at_start(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    db = tmp_path / "runs.db"
    old_run(db, kind="paper-live", touched=datetime(2024, 8, 16, 12, 0, tzinfo=UTC))  # stale
    old_run(db, kind="backtest", touched=datetime(2024, 8, 16, 12, 0, tzinfo=UTC))  # not a live run
    old_run(db, kind="paper-live", touched=datetime(2024, 8, 16, 13, 29, 30, tzinfo=UTC))  # recent
    standard_script(rig, last=4, minutes=7)
    result = session(rig, tmp_path, stores, db=db, minutes=7)
    assert result.code == 0, rig.text
    runs = {r.run_id: r for r in result.store.list_runs()}
    assert (runs[1].status, runs[1].end_reason) == ("failed", "abandoned")
    assert runs[2].status == "running" and runs[2].kind == "backtest"
    assert runs[3].status == "running" and runs[3].kind == "paper-live"  # written 40 s ago: left alone
    assert (runs[4].status, runs[4].end_reason) == ("completed", "minutes_elapsed")  # a NEW run (D2)
    assert "run 1 was still marked running but its last write is stale" in rig.text
    assert "run 3 is marked running and was written recently: left alone" in rig.text


# -- (m) a step the engine refuses -----------------------------------------------------------


def test_a_step_refused_by_the_engine_does_not_end_the_session(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    """The aligner is made to give a batch twice: the engine refuses the second (a timestamp not after
    the previous one, a real `ValueError`); the session goes on and is VERIFIED."""
    real_poll = LiveBarAligner.poll
    state = {"duplicated": False}

    def poll(self: LiveBarAligner, now: float) -> Any:
        out = real_poll(self, now)
        if out and not state["duplicated"] and out[-1][0] >= pd.Timestamp(2024, 8, 16, 9, 35):
            state["duplicated"] = True
            return [*out, out[-1]]
        return out

    monkeypatch.setattr(LiveBarAligner, "poll", poll)
    standard_script(rig, last=11, minutes=14)
    result = session(rig, tmp_path, stores, minutes=14)
    text = rig.text
    assert state["duplicated"] and result.code == 0, text
    assert text.count("the engine refused the batch") == 1 and "the session goes on" in text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("completed", "minutes_elapsed")
    assert result.counts()[("AAPL", "fed", None)] == len(result.times("AAPL")) == 12
    assert any("refused by the engine" in w for w in result.run().summary.data.warnings)
    assert "verify: VERIFIED" in text


def test_an_algo_or_engine_error_ends_the_run_failed_and_end_live_still_runs(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    real_step = Engine.step
    calls: list[Any] = []

    def failing(self: Engine, ts: Any, bars: Any) -> None:
        if bars:
            calls.append(ts)
            if len(calls) == 6:
                raise RuntimeError("the engine fell over (test)")
        real_step(self, ts, bars)

    monkeypatch.setattr(Engine, "step", failing)
    standard_script(rig, last=12, minutes=15)
    result = session(rig, tmp_path, stores, minutes=15)
    text = rig.text
    # never retried: the failing batch was given once (the later calls are the end-of-session verify's)
    assert result.code == 4 and calls.count(calls[5]) == 1, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "error")
    assert "ERROR RuntimeError: the engine fell over (test)" in text
    assert len(result.times("AAPL")) == 5


def test_ctrl_c_during_a_step_ends_the_run_interrupted_and_the_connection_is_closed(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    real_step = Engine.step
    calls = {"n": 0}

    def interrupted(self: Engine, ts: Any, bars: Any) -> None:
        if bars:
            calls["n"] += 1
            if calls["n"] == 4:
                raise KeyboardInterrupt
        real_step(self, ts, bars)

    monkeypatch.setattr(Engine, "step", interrupted)
    standard_script(rig, last=12, minutes=15)
    result = session(rig, tmp_path, stores, minutes=15)
    assert result.code == 130, rig.text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("failed", "interrupted")
    assert all(c.state is ConnState.CLOSED for c in rig.connections)
    assert "Live session: failed, end reason interrupted (exit 130)" in rig.text


def test_ctrl_c_in_the_loop_ends_the_run_interrupted(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig, last=12, minutes=15)
    rig.interrupt_after = 40
    result = session(rig, tmp_path, stores, minutes=15)
    assert result.code == 130, rig.text
    assert result.store.list_runs()[0].end_reason == "interrupted"


# -- steps that take too long --------------------------------------------------------------------


@pytest.mark.parametrize("seconds,code,reason", [(25.0, 0, "minutes_elapsed"), (70.0, 7, "step_too_slow")])
def test_a_slow_step_is_recorded_and_a_very_slow_one_ends_the_run(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch,
        seconds: float, code: int, reason: str) -> None:
    real_step = Engine.step
    done = {"slow": False}

    def slow(self: Engine, ts: Any, bars: Any) -> None:
        if bars and not done["slow"]:
            done["slow"] = True
            rig.t += seconds  # the step takes that long on the injected clock
        real_step(self, ts, bars)

    monkeypatch.setattr(Engine, "step", slow)
    standard_script(rig, last=6, minutes=9)
    result = session(rig, tmp_path, stores, minutes=9)
    assert result.code == code, rig.text
    summary = result.store.list_runs()[0]
    assert summary.end_reason == reason
    assert any("took more than 20 s" in w for w in result.run().summary.data.warnings)
    assert f"the step of 09:30 took {seconds:.1f} s" in rig.text
    if reason == "step_too_slow":
        assert "FAULT (step_too_slow)" in rig.text


# -- a symbol falls silent --------------------------------------------------------------------------


def test_a_symbol_silent_for_ten_session_minutes_ends_the_run_with_a_data_fault(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    script_minutes(rig, SYMBOLS, 0, 25, skip_symbol_from=("MSFT", 3))
    ticks(rig, FIRST + 200, FIRST + 1500, 20)
    rig.end_at(25, symbols=3)
    result = session(rig, tmp_path, stores, minutes=25)
    assert result.code == 7, rig.text
    assert result.store.list_runs()[0].end_reason == "data_fault"
    assert "MSFT has shown no bar and no decision" in rig.text


# -- the session's end --------------------------------------------------------------------------------


def test_the_session_ends_when_the_last_bar_is_decided_at_16_20(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    rig.server_epoch = ep(15, 57, 0)
    set_backfills(rig, last_closed=386)  # 15:56 closed, 15:57 forming
    script_minutes(rig, SYMBOLS, 387, 389)
    ticks(rig, ep(16, 0, 10), ep(16, 25, 0), 30)
    rig.end_at(30, symbols=3)
    result = session(rig, tmp_path, stores, minutes=30)
    text = rig.text
    assert result.code == 0, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("completed", "session_end")
    assert result.times("AAPL") == ["15:57", "15:58", "15:59"], text  # the last two arrive about 16:20
    assert "ending the session (session_end)" in text
    assert "Stopped on the driver's request (session_end)." in text


# -- refusals before anything is built ------------------------------------------------------------------


@pytest.mark.parametrize("over,words", [
    (dict(paper=False), "paper only"),
    (dict(fills="legacy"), "realistic fills only"),
    (dict(entries=ema_entries(mode="backtest")), "mode 'paper' only"),
    (dict(entries=[{"ID": "EMACrossing", "data": [{"idname": "x", "ticker": "AAPL", "short": "10",
                                                    "long": "30"}]}]), "mode 'paper' only"),
    (dict(minutes=0), "`minutes` must be"),
    (dict(minutes=601), "`minutes` must be"),
    (dict(bars=None), "folder of the warm-up month files"),
    (dict(warmup_days=0), "warmup_days"),
    (dict(capital="x"), "not a number"),
    (dict(entries=[]), "algo config"),
    (dict(symbols=["AAPL"]), "differ from the algos' symbols"),
])
def test_unusable_options_are_refused_with_exit_2_and_nothing_is_built(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore], over: dict[str, Any], words: str) -> None:
    result = session(rig, tmp_path, stores, **over)
    assert result.code == 2 and words in rig.text, rig.text
    assert not rig.connections and not rig.fakes and result.store.list_runs() == ()


def test_a_missing_warmup_file_is_refused_before_the_connection(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    (tmp_path / "bars" / "TSLA" / "TSLA_2024-08.csv").unlink()
    result = session(rig, tmp_path, stores)
    assert result.code == 2 and "no warm-up can be built" in rig.text
    assert not rig.connections


def test_the_recording_is_on_through_the_injected_factory(
        rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    standard_script(rig, last=3, minutes=6)
    result = session(rig, tmp_path, stores, minutes=6, record=tmp_path / "rec" / "live.jsonl")
    assert result.code == 0, rig.text
    assert len(rig.recorders) == 1 and (tmp_path / "rec" / "live.jsonl").stat().st_size > 0
    assert "Recording check:" in rig.text
