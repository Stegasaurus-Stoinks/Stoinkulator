"""The golden backtests again, with every trade reporting to a SimBroker.

Read-only on purpose: this file never writes goldens, even under
--update-golden. The no-broker path is covered by test_golden_backtests.py.
"""
import json
from pathlib import Path

import pytest

from MainStoinker.engine.broker import SimBroker
from tests import harness
from tests.test_golden_backtests import CASES

GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_trades_match_golden_via_simbroker(algo_id, cfg, request):
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    broker = SimBroker()
    (algo,) = harness.run_offline_backtest(algo_id, [cfg], broker=broker)

    records = harness.trade_records(algo)
    expected = json.loads((GOLDEN_DIR / f"{request.node.callspec.id}.json").read_text())
    assert records == expected

    methods = [c.method for c in broker.calls]
    assert methods.count("open_position") == len(records)
    closed = sum(1 for r in records if r["status"] == "Closed")
    assert methods.count("close_position") >= closed
