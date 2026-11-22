"""The repository-level conftest keeps the owner's STOINK_DB out of every test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_stoink_db_is_removed_for_every_test() -> None:
    assert "STOINK_DB" not in os.environ


def test_stoink_db_is_removed_even_when_the_environment_sets_it(tmp_path: Path) -> None:
    """The check above passes trivially on a machine without STOINK_DB (CI). Here a real
    pytest session starts with it set, and a module-level read at collection (before any
    fixture) must not see it."""
    probe = REPO / "tests" / "env_isolation_probe.py"
    env = {**os.environ, "STOINK_DB": str(tmp_path / "must-not-be-used.db")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(probe)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout
