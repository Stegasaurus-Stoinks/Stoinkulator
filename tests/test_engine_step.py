"""The engine's per-timestamp step was extracted from `run_backtest` (stage 2, T1).

The refactor must not change behaviour: the first test pins what a realistic
run on an OfflineData slice produced BEFORE the extraction (the frozen values
below were computed on the code before it). The rest pin the pieces' contracts
that the live API will rely on.
"""
from __future__ import annotations

import contextlib
import hashlib

import pandas as pd
import pytest

from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.sink import NullSink
from tests import harness
from tests.test_engine import ListFeed, SpyAlgo

ROWS = 1500
EMA = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}

# Frozen from the pre-refactor engine (origin/revival at the gap-rule merge).
FROZEN_COUNTS = {"fills": 32, "round_trips": 16, "equity_points": 2220}
FROZEN_SHA256 = "323c10af4d08f34eb23ed1a91d708b7fd3a9c806b543f73c3deb4ec29702a534"


def _digest(ledger) -> str:
    parts = [*ledger.fills, *ledger.round_trips, *ledger.equity_points]
    return hashlib.sha256("\n".join(repr(p) for p in parts).encode()).hexdigest()


def test_backtest_identical_before_and_after_refactor(tmp_path) -> None:
    harness.stage_offline_data(tmp_path, ["AAPL"], max_rows=ROWS)
    with contextlib.chdir(tmp_path):
        try:
            run = harness.run_paper_backtest("EMACrossing", EMA)
        finally:
            harness.reset_runtime_state()
    ledger = run.ledger
    counts = {
        "fills": len(ledger.fills),
        "round_trips": len(ledger.round_trips),
        "equity_points": len(ledger.equity_points),
    }
    assert counts == FROZEN_COUNTS
    assert _digest(ledger) == FROZEN_SHA256


def test_run_backtest_still_single_use() -> None:
    log: list = []
    algo = SpyAlgo("a", "AAA", log)
    engine = Engine(EngineConfig(), ListFeed({"AAA": 1}, 2), _broker(), NullSink(), [algo])
    engine.run_backtest()
    seen = len(log)
    assert seen == 2
    with pytest.raises(RuntimeError, match="once"):
        engine.run_backtest()
    assert len(log) == seen


def _broker():
    from MainStoinker.engine.broker import SimBroker

    return SimBroker()


def test_step_with_no_known_symbol_does_nothing() -> None:
    log: list = []
    algo = SpyAlgo("a", "AAA", log)
    feed = ListFeed({"AAA": 1}, 2)
    engine = Engine(EngineConfig(), feed, _broker(), NullSink(), [algo])
    engine._begin({"AAA": feed.warmup("AAA")})
    assert engine._step(pd.Timestamp("2030-01-01 09:31"), {}) == []
    assert engine._step(pd.Timestamp("2030-01-01 09:32"), {"ZZZ": ListFeed._bar(2)}) == []
    assert log == []
    # nothing was processed, so the end handling is a no-op too
    engine._end_bars()
    engine._finish_ledger()


def test_pieces_driven_by_hand_equal_run_backtest() -> None:
    """`_begin`, one `_step` per batch, `_end_bars`, `_finish_ledger` do what `run_backtest` does."""
    run_log: list = []
    engine = Engine(EngineConfig(), ListFeed({"AAA": 2, "BBB": 1}, 3), _broker(), NullSink(),
                    [SpyAlgo("a", "AAA", run_log), SpyAlgo("b", "BBB", run_log)])
    engine.run_backtest()

    hand_log: list = []
    feed = ListFeed({"AAA": 2, "BBB": 1}, 3)
    hand = Engine(EngineConfig(), feed, _broker(), NullSink(),
                  [SpyAlgo("a", "AAA", hand_log), SpyAlgo("b", "BBB", hand_log)])
    hand._begin({s: feed.warmup(s) for s in feed.symbols()})
    present = [hand._step(timestamp, bars) for timestamp, bars in feed]
    hand._end_bars()
    hand._finish_ledger()
    assert present == [["AAA", "BBB"]] * 3
    assert hand_log == run_log
    assert len(run_log) == 6
