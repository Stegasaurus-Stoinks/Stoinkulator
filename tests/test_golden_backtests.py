"""Golden backtests: an algo replayed over the committed offline bars must
produce exactly the trades recorded in tests/golden/.

These pin *current* behaviour, including the simulated-fill model (fills at
bar close, no slippage or commission). A diff here means trading behaviour
changed. If the change is intended, regenerate with `pytest --update-golden`
(add `-m slow` for the slow ones) and explain the diff in the PR.
"""
from pathlib import Path

import pytest

from tests import harness

CASES = [
    pytest.param(
        "EMACrossing",
        {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"},
        id="emacrossing_aapl",
    ),
    pytest.param(
        "FunctionTest",
        {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3},
        id="functiontest_msft",
    ),
    pytest.param(
        "OpeningRangeCandle",
        {"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3},
        id="openingrangecandle_aapl",
    ),
    pytest.param(
        "FridayFrenzy",
        {"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3},
        id="fridayfrenzy_tsla",
    ),
    pytest.param(
        "FVG",
        {"idname": "AAPL-fvg", "ticker": "AAPL", "RRRatio": 3},
        id="fvg_aapl",
    ),
    pytest.param(
        "Elliot",
        {"idname": "elliot-msft", "ticker": "MSFT", "order": 10, "trailingStopDelta": 0.10},
        id="elliot_msft",
    ),
    pytest.param(
        "Elliot",
        {"idname": "elliot-aapl", "ticker": "AAPL", "order": 10, "trailingStopDelta": 0.10},
        id="elliot_aapl",
    ),
    pytest.param(
        "Elliot",
        {"idname": "elliot-tsla", "ticker": "TSLA", "order": 10, "trailingStopDelta": 0.10},
        id="elliot_tsla",
    ),
]


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_trades_match_golden(algo_id, cfg, golden, request):
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    (algo,) = harness.run_offline_backtest(algo_id, [cfg])
    golden(request.node.callspec.id, harness.trade_records(algo))
