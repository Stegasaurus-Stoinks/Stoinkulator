"""Parity guard: the legacy runtime path still produces the pinned golden trades.

The golden backtests themselves run through the engine
(tests/test_golden_backtests.py). This file runs the legacy path
(`harness.run_legacy_offline_backtest`) over the same cases and compares with
the same golden files, READ-ONLY: it never uses the `golden` fixture and never
writes, even under --update-golden. Everything here is slow; the legacy path
is removed in Phase B, together with this file.
"""
import json
from pathlib import Path

import pytest

from tests import harness
from tests.test_golden_backtests import CASES

GOLDEN_DIR = Path(__file__).parent / "golden"

SLOW_CASES = [
    pytest.param(*case.values, id=case.id, marks=[*case.marks, pytest.mark.slow])
    for case in CASES
]


@pytest.mark.slow
@pytest.mark.parametrize("algo_id, cfg", SLOW_CASES)
def test_legacy_path_matches_golden(algo_id, cfg, request):
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    (algo,) = harness.run_legacy_offline_backtest(algo_id, [cfg])
    expected = json.loads((GOLDEN_DIR / f"{request.node.callspec.id}.json").read_text())
    assert harness.trade_records(algo) == expected


@pytest.mark.slow
def test_legacy_and_engine_agree_on_a_multi_symbol_run():
    specs = [
        ("EMACrossing", {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}),
        ("FunctionTest", {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3}),
    ]
    harness.stage_offline_data(Path.cwd(), ["AAPL", "MSFT"])
    legacy = [
        harness.trade_records(a) for a in harness.run_mixed_legacy_offline_backtest(specs)
    ]
    engine = [harness.trade_records(a) for a in harness.run_mixed_offline_backtest(specs)]
    assert len(legacy) == 2
    assert all(records for records in legacy), "both algos should trade, or the test proves little"
    assert engine == legacy
