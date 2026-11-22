"""Repository-level pytest fixtures (no project imports here)."""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Remove `STOINK_DB` for the whole session, before anything is collected.

    Session-scoped helpers (such as the stored runs built by `tests/api_support.py`)
    start before any per-test fixture, so the fixture below is not enough alone.
    """
    os.environ.pop("STOINK_DB", None)


@pytest.fixture(autouse=True)
def _no_stoink_db_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove `STOINK_DB` for every test.

    The CLI uses `STOINK_DB` as the default for `--db`. On the owner's machine
    it points at the real run database, so any test calling `main()` without
    `--db` (or without an injected `env`) would write test runs into it.
    Subprocess tests build their environment from `os.environ`, so removing it
    here covers them too. A test that wants it passes it explicitly.
    """
    monkeypatch.delenv("STOINK_DB", raising=False)
