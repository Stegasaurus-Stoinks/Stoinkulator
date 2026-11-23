"""Helpers for the API tests: stored runs and a `TestClient`.

`stored_run_database` is `small_store_run` (the four fast golden algos on a short slice of the
data, one database per fill model, built once per session across all xdist workers; tests that
compare with the golden files use `full_store_run` instead). Every database lives
under pytest's temporary folders. Nothing here reads `STOINK_DB` (the root
`conftest.py` removes it for the whole session) and every call passes an
explicit `env`, so the owner's real database and OneDrive settings are never
consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from MainStoinker.api.app import create_app
from tests.run_support import small_store_run

NO_ENV: Mapping[str, str] = {}


def stored_run_database(
    tmp_path_factory: pytest.TempPathFactory, fills: Literal["realistic", "legacy"]
) -> Path:
    """The database of one stored backtest run of four algos (cached per session)."""
    return small_store_run(tmp_path_factory, fills)[1]


def make_client(db: str | Path, env: Mapping[str, str] | None = None) -> TestClient:
    """A client for an app over `db`; `env` defaults to an empty environment, never `os.environ`."""
    app = create_app(str(db), NO_ENV if env is None else env)
    return TestClient(app, base_url="http://127.0.0.1:8750")  # the Host the API accepts by default
