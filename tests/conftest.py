import json
import os
from pathlib import Path

import pytest
from ibapi import connection as ibapi_connection

from tests import harness

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_PAPER_DIR = Path(__file__).parent / "golden_paper"


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


# Second guard: a session-wide variable that child processes inherit (see
# MainStoinker/ib/connection.py, `_broker_blocked_here`). Separate from the block above.
BROKER_BLOCKED_VARIABLE = "STOINK_BROKER_BLOCKED"
_BLOCKED_VARIABLE_BEFORE = pytest.StashKey[str | None]()


def _set_broker_blocked_variable(config):
    config.stash[_BLOCKED_VARIABLE_BEFORE] = os.environ.get(BROKER_BLOCKED_VARIABLE)
    os.environ[BROKER_BLOCKED_VARIABLE] = "1"


def _restore_broker_blocked_variable(config):
    if _BLOCKED_VARIABLE_BEFORE not in config.stash:
        return
    before = config.stash[_BLOCKED_VARIABLE_BEFORE]
    del config.stash[_BLOCKED_VARIABLE_BEFORE]
    if before is None:
        os.environ.pop(BROKER_BLOCKED_VARIABLE, None)
    else:
        os.environ[BROKER_BLOCKED_VARIABLE] = before


# A module cannot define `pytest_configure` twice, and the existing one must stay as it is,
# so the hooks below implement the same hooks under other names (pluggy `specname`).
@pytest.hookimpl(specname="pytest_configure")
def pytest_configure_broker_blocked_variable(config):
    _set_broker_blocked_variable(config)


@pytest.hookimpl(specname="pytest_unconfigure")
def pytest_unconfigure_broker_blocked_variable(config):
    _restore_broker_blocked_variable(config)


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


@pytest.fixture
def golden_paper(request):
    """Compare `actual` with tests/golden_paper/<name>.json, or rewrite it.

    The file is written as sorted-key JSON with LF newlines on every platform,
    so regenerating on a clean tree gives a byte-identical file.
    """

    def check(name: str, actual):
        path = GOLDEN_PAPER_DIR / f"{name}.json"
        text = json.dumps(actual, indent=1, sort_keys=True) + "\n"
        if request.config.getoption("--update-golden"):
            GOLDEN_PAPER_DIR.mkdir(exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            return
        assert path.exists(), f"No golden file {path.name}; run pytest --update-golden"
        assert actual == json.loads(path.read_text(encoding="utf-8"))

    return check
