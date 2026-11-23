"""Run only by `test_xdist_guards.py`, under `-n 2 --dist each`, in a subprocess that starts with
STOINK_DB set.

Not collected normally (the name does not start with `test_`). Every test here runs once in each
xdist worker, so a worker that did not get the safety guards fails it.
"""

from __future__ import annotations

import os

from ibapi import connection as ibapi_connection

SEEN_AT_COLLECTION = os.environ.get("STOINK_DB")


def test_this_is_an_xdist_worker() -> None:
    assert os.environ.get("PYTEST_XDIST_WORKER", "").startswith("gw")


def test_stoink_db_is_absent_in_the_worker() -> None:
    assert SEEN_AT_COLLECTION is None  # the worker's collection, before any fixture
    assert "STOINK_DB" not in os.environ


def test_broker_connect_is_blocked_in_the_worker() -> None:
    assert ibapi_connection.Connection.connect.__name__ == "_blocked_connect"
    assert ibapi_connection.Connection.connect.__module__ == "tests.conftest"
