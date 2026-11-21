import json
from pathlib import Path

import pytest
from ibapi import connection as ibapi_connection

from tests import harness

GOLDEN_DIR = Path(__file__).parent / "golden"


class BrokerConnectionBlockedUnderPytest(RuntimeError):
    """Raised when anything tries to open a broker socket during a test run.

    Deliberately not an OSError: ibapi's EClient.connect catches socket.error
    (an alias of OSError) and would swallow it.
    """


_BLOCK_PATCH = pytest.StashKey[pytest.MonkeyPatch]()


def _blocked_connect(self, *args, **kwargs):
    host = getattr(self, "host", "?")
    port = getattr(self, "port", "?")
    raise BrokerConnectionBlockedUnderPytest(
        f"Refusing to connect to a broker at {host}:{port}: tests must never "
        "connect to TWS / IB Gateway on any port."
    )


def pytest_configure(config):
    # Patch the method on the class (not the class name), so every reference
    # to Connection, including the one inside ibapi.client, is covered.
    # Done here rather than in a fixture so it is active during collection
    # and for every test without opt-in.
    mp = pytest.MonkeyPatch()
    mp.setattr(ibapi_connection.Connection, "connect", _blocked_connect)
    config.stash[_BLOCK_PATCH] = mp


def pytest_unconfigure(config):
    mp = config.stash.get(_BLOCK_PATCH, None)
    if mp is not None:
        mp.undo()
        del config.stash[_BLOCK_PATCH]


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
