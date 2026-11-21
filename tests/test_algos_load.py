"""Every algo package must import and construct from a minimal config."""
import pytest

from MainStoinker.MainStuff import main_utils as utils

STATEMACHINE_BUG = (
    "Known defect: with the unpinned python-statemachine now installed, the "
    "AlgoLogic state machine is rejected at import (doneTradingState has no "
    "outgoing transition and is not marked final)."
)

CASES = [
    pytest.param("EMACrossing", {"short": "10", "long": "30"}, id="EMACrossing"),
    pytest.param("TrendLine", {"short": "10", "long": "30"}, id="TrendLine"),
    pytest.param("Elliot", {"order": 10, "trailingStopDelta": 0.10}, id="Elliot"),
    pytest.param("FunctionTest", {"RRRatio": 3}, id="FunctionTest"),
    pytest.param(
        "OpeningRangeCandle",
        {"RRRatio": 3},
        id="OpeningRangeCandle",
        marks=pytest.mark.xfail(strict=True, reason=STATEMACHINE_BUG),
    ),
    pytest.param(
        "FridayFrenzy",
        {"RRRatio": 3},
        id="FridayFrenzy",
        marks=pytest.mark.xfail(strict=True, reason=STATEMACHINE_BUG),
    ),
    pytest.param(
        "FVG",
        {"RRRatio": 3},
        id="FVG",
        marks=pytest.mark.xfail(strict=True, reason=STATEMACHINE_BUG),
    ),
]


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_algo_constructs(algo_id, extra):
    cfg = {"idname": f"test-{algo_id}", "ticker": "AAPL", **extra}
    algo = utils.algo_starter(algo_id, cfg)
    assert algo.name == cfg["idname"]
    assert algo.ticker == "AAPL"
    assert algo.inTrade is False
