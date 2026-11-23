"""The safety guards are active in every pytest-xdist worker, not only in the main process.

`pytest_configure` of `tests/conftest.py` (the broker-connection block) and of the root
`conftest.py` (`STOINK_DB` removal) run once per process; a worker is a process of its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ibapi import connection as ibapi_connection

REPO = Path(__file__).resolve().parent.parent


def test_broker_connect_is_blocked_in_this_process() -> None:
    """Runs in whichever process runs the tests (a worker with the default options)."""
    assert ibapi_connection.Connection.connect.__name__ == "_blocked_connect"
    assert "STOINK_DB" not in os.environ


def test_guards_are_active_in_every_xdist_worker(tmp_path: Path) -> None:
    """Two real workers each run the probe tests (`--dist each`), with STOINK_DB set outside."""
    probe = REPO / "tests" / "xdist_guard_probe.py"
    env = {**os.environ, "STOINK_DB": str(tmp_path / "must-not-be-used.db")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-n", "2", "--dist", "each",
         "-m", "slow or not slow", str(probe)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "6 passed" in done.stdout  # three probe tests in each of the two workers
