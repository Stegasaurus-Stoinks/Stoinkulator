"""Golden backtests: an algo replayed over the committed offline bars must
produce exactly the trades recorded in tests/golden/.

These pin *current* behaviour, including the simulated-fill model (fills at
bar close, no slippage or commission). A diff here means trading behaviour
changed. If the change is intended, regenerate with `pytest --update-golden`
(add `-m slow` for the slow ones) and explain the diff in the PR.
"""
from pathlib import Path

import pytest

from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.report import ledger_trade_records
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
    # FVGZone (issue 277): defaults on each symbol, and the 1-minute zones without trend filter.
    pytest.param(
        "FVGZone",
        {"idname": "AAPL-fvgzone", "ticker": "AAPL"},
        id="fvgzone_aapl",
    ),
    pytest.param(
        "FVGZone",
        {"idname": "MSFT-fvgzone", "ticker": "MSFT"},
        id="fvgzone_msft",
    ),
    pytest.param(
        "FVGZone",
        {"idname": "TSLA-fvgzone", "ticker": "TSLA"},
        id="fvgzone_tsla",
    ),
    pytest.param(
        "FVGZone",
        {"idname": "TSLA-fvgzone-1m", "ticker": "TSLA", "zone_minutes": 1, "trend_filter": False},
        id="fvgzone_tsla_1m_nofilter",
    ),
]


# The cheap cases (the original four and the four fvgzone ones, the same as the realistic-fill
# goldens in test_paper_goldens.py use) stay in the fast suite; FunctionTest and the three
# Elliot runs, each 7 to 14 s on its own, run
# with `pytest -m slow`. Their behaviour is also exercised by fast tests on shorter slices:
# FunctionTest by test_engine (multi-symbol, engine against the legacy path) and the CLI/store
# tests, Elliot by test_elliot_state_golden and test_elliot_incremental (MSFT).
SLOW_IDS = {"functiontest_msft", "elliot_msft", "elliot_aapl", "elliot_tsla"}
CASES = [
    pytest.param(*p.values, id=p.id, marks=[pytest.mark.slow] if p.id in SLOW_IDS else [])
    for p in CASES
]


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_trades_match_golden(algo_id, cfg, golden, request):
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    ledger = Ledger()
    (algo,) = harness.run_offline_backtest(algo_id, [cfg], ledger=ledger)
    records = harness.trade_records(algo)
    golden(request.node.callspec.id, records)

    # The ledger built in the same run must reproduce the golden trades.
    # `status` is a legacy Trade attribute the ledger has no notion of.
    expected = [{k: v for k, v in r.items() if k != "status"} for r in records]
    assert ledger_trade_records(ledger) == expected
