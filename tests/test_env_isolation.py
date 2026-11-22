"""The repository-level conftest keeps the owner's STOINK_DB out of every test."""

from __future__ import annotations

import os


def test_stoink_db_is_removed_for_every_test() -> None:
    assert "STOINK_DB" not in os.environ
