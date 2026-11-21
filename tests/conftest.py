import json
from pathlib import Path

import pytest

from tests import harness

GOLDEN_DIR = Path(__file__).parent / "golden"


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="Rewrite golden files from current behaviour instead of comparing.",
    )


@pytest.fixture(autouse=True)
def safe_runtime(tmp_path, monkeypatch):
    """Every test runs offline, non-live, in a scratch directory.

    The runtime writes loggy.log, tradey.txt and algo.csv to the current
    directory, so tests must never run from the repo root.
    """
    monkeypatch.chdir(tmp_path)
    harness.apply_safe_config()
    harness.reset_runtime_state()
    yield
    harness.reset_runtime_state()


@pytest.fixture
def golden(request):
    """Compare `actual` with tests/golden/<name>.json, or rewrite it."""

    def check(name: str, actual):
        path = GOLDEN_DIR / f"{name}.json"
        if request.config.getoption("--update-golden"):
            GOLDEN_DIR.mkdir(exist_ok=True)
            path.write_text(json.dumps(actual, indent=1) + "\n")
            return
        assert path.exists(), f"No golden file {path.name}; run pytest --update-golden"
        expected = json.loads(path.read_text())
        assert actual == expected

    return check
