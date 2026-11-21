"""Every algo package must import and construct from a minimal config."""
import pytest

from MainStoinker.MainStuff import main_utils as utils

CASES = [
    pytest.param("EMACrossing", {"short": "10", "long": "30"}, id="EMACrossing"),
    pytest.param("TrendLine", {"short": "10", "long": "30"}, id="TrendLine"),
    pytest.param("Elliot", {"order": 10, "trailingStopDelta": 0.10}, id="Elliot"),
    pytest.param("FunctionTest", {"RRRatio": 3}, id="FunctionTest"),
    pytest.param("OpeningRangeCandle", {"RRRatio": 3}, id="OpeningRangeCandle"),
    pytest.param("FridayFrenzy", {"RRRatio": 3}, id="FridayFrenzy"),
    pytest.param("FVG", {"RRRatio": 3}, id="FVG"),
]


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_algo_constructs(algo_id, extra):
    cfg = {"idname": f"test-{algo_id}", "ticker": "AAPL", **extra}
    algo = utils.algo_starter(algo_id, cfg)
    assert algo.name == cfg["idname"]
    assert algo.ticker == "AAPL"
    assert algo.inTrade is False
