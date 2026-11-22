"""Engine algo loader: same classes and names as the legacy algo_starter."""
from __future__ import annotations

import pytest

from MainStoinker.engine.algos import load_algo, load_algos
from MainStoinker.MainStuff import main_utils as utils
from tests.test_algos_load import CASES


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_load_algo_constructs_each_real_algo(algo_id: str, extra: dict) -> None:
    cfg = {"idname": f"test-{algo_id}", "ticker": "AAPL", **extra}
    algo = load_algo(algo_id, cfg)
    assert algo.name == cfg["idname"]
    assert algo.ticker == "AAPL"
    assert algo.inTrade is False
    assert algo.broker is None


@pytest.mark.parametrize("algo_id", ["../x", "a.b", "os; x", "", "a b", "1abc", "x/y", "..", "a-b"])
def test_load_algo_rejects_non_identifier(algo_id: str) -> None:
    with pytest.raises(ValueError, match="invalid algo id"):
        load_algo(algo_id, {"idname": "x", "ticker": "AAPL"})


def test_load_algo_unknown_id_raises_value_error() -> None:
    with pytest.raises(ValueError, match="NoSuchAlgo") as info:
        load_algo("NoSuchAlgo", {"idname": "x", "ticker": "AAPL"})
    assert isinstance(info.value.__cause__, ModuleNotFoundError)


def test_load_algo_module_without_algo_class_raises_value_error() -> None:
    # `ParentAlgo` is a real module under Algos but its package path has no
    # `ParentAlgo.ParentAlgo_Algo`, so it is unknown; a module that imports but
    # lacks `Algo` is covered by a stub below.
    with pytest.raises(ValueError, match="ParentAlgo"):
        load_algo("ParentAlgo", {})


def test_load_algo_module_missing_class_names_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import types

    stub = types.ModuleType("MainStoinker.Algos.Stub.Stub_Algo")
    real_import = importlib.import_module

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "MainStoinker.Algos.Stub.Stub_Algo":
            return stub
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(ValueError, match="Stub") as info:
        load_algo("Stub", {})
    assert isinstance(info.value.__cause__, AttributeError)


def test_load_algos_preserves_file_order_and_shape() -> None:
    config = [
        {
            "ID": "EMACrossing",
            "data": [
                {"idname": "e1", "ticker": "AAPL", "short": "10", "long": "30"},
                {"idname": "e2", "ticker": "MSFT", "short": "20", "long": "40"},
            ],
        },
        {
            "ID": "FunctionTest",
            "data": [
                {"idname": "f1", "ticker": "TSLA", "RRRatio": 3},
                {"idname": "f2", "ticker": "AAPL", "RRRatio": 2},
            ],
        },
    ]
    algos = load_algos(config)
    assert [a.name for a in algos] == ["e1", "e2", "f1", "f2"]
    assert [a.ticker for a in algos] == ["AAPL", "MSFT", "TSLA", "AAPL"]
    assert [type(a).__module__.split(".")[-2] for a in algos] == [
        "EMACrossing", "EMACrossing", "FunctionTest", "FunctionTest",
    ]
    assert load_algos([]) == []


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_loader_matches_legacy_algo_starter(algo_id: str, extra: dict) -> None:
    cfg = {"idname": f"test-{algo_id}", "ticker": "AAPL", **extra}
    ours = load_algo(algo_id, cfg)
    legacy = utils.algo_starter(algo_id, cfg)
    assert type(ours) is type(legacy)
    assert (ours.name, ours.ticker) == (legacy.name, legacy.ticker)
