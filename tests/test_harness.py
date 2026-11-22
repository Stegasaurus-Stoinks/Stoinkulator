"""The harness itself: which path each runner takes, and what it shares."""
import ast
import json
import re
from pathlib import Path

import pytest

from MainStoinker.engine import feed as feed_module
from MainStoinker.engine.broker import SimBroker
from tests import harness
from tests.test_golden_backtests import CASES

TESTS_DIR = Path(__file__).parent
GOLDEN_DIR = TESTS_DIR / "golden"

EMA_AAPL = (
    "EMACrossing",
    {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"},
)
EMA_MSFT = (
    "EMACrossing",
    {"idname": "MSFT-10/30EMA", "ticker": "MSFT", "short": "10", "long": "30"},
)


def _forbidden(*args: object, **kwargs: object) -> None:
    raise AssertionError("legacy path touched")


def test_golden_path_is_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.TradeTools.ticker import Ticker

    case = next(c for c in CASES if c.id == "openingrangecandle_aapl")
    algo_id, cfg = case.values
    harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
    monkeypatch.setattr(ibapi, "startData", _forbidden)
    monkeypatch.setattr(Ticker, "__init__", _forbidden)
    (algo,) = harness.run_offline_backtest(algo_id, [cfg])
    expected = json.loads((GOLDEN_DIR / "openingrangecandle_aapl.json").read_text())
    assert expected, "golden should contain trades, or the test proves little"
    assert harness.trade_records(algo) == expected


def test_legacy_runner_still_uses_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from MainStoinker.DataCollection.apiApi import ibapi

    calls: list[object] = []
    original = ibapi.startData

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return original(*args, **kwargs)

    harness.stage_offline_data(Path.cwd(), ["AAPL"], max_rows=420)
    monkeypatch.setattr(ibapi, "startData", spy)
    harness.run_legacy_offline_backtest(EMA_AAPL[0], [EMA_AAPL[1]])
    assert len(calls) == 1


def test_default_broker_is_a_fresh_simbroker_per_run() -> None:
    harness.stage_offline_data(Path.cwd(), ["AAPL"], max_rows=1000)
    runs = [harness.run_offline_backtest(EMA_AAPL[0], [EMA_AAPL[1]]) for _ in range(2)]
    brokers = [algo.broker for (algo,) in runs]
    assert all(isinstance(b, SimBroker) for b in brokers)
    assert brokers[0] is not brokers[1]
    first_refs = []
    for broker in brokers:
        opens = [c for c in broker.calls if c.method == "open_position"]
        assert opens, "expected a trade, or the test proves little"
        first_refs.append(opens[0].ref)
    assert first_refs == [1, 1]
    assert len(brokers[0].calls) == len(brokers[1].calls)


def test_harness_symbols_follow_first_appearance_order(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real = feed_module.CsvBarFeed

    def spy(data_dir: Path, symbols: list[str], warmup_days: int = 2) -> object:
        seen.append(list(symbols))
        return real(data_dir, symbols, warmup_days)

    harness.stage_offline_data(Path.cwd(), ["AAPL", "MSFT"], max_rows=420)
    monkeypatch.setattr(feed_module, "CsvBarFeed", spy)
    harness.run_offline_backtest(
        "EMACrossing",
        [EMA_MSFT[1], EMA_AAPL[1], {**EMA_MSFT[1], "idname": "MSFT-other"}],
    )
    assert seen == [["MSFT", "AAPL"]]


def test_only_test_golden_backtests_uses_the_golden_fixture() -> None:
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == "test_golden_backtests.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args
                names = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
                if "golden" in names:
                    offenders.append(f"{path.name}:{node.lineno} requests golden")
        if re.search(r"usefixtures\([^)]*['\"]golden['\"]", source):
            offenders.append(f"{path.name} usefixtures golden")
        if re.search(r"getfixturevalue\(\s*['\"]golden['\"]", source):
            offenders.append(f"{path.name} getfixturevalue golden")
    assert offenders == []
