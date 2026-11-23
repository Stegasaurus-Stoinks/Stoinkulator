"""Run only by `test_ib_subprocess_block.py`, under `-n 2 --dist each`, in a subprocess whose
environment does not contain `STOINK_BROKER_BLOCKED`.

Not collected normally (the name does not start with `test_`). Every test here runs once in each
xdist worker, so a worker whose `pytest_configure` did not set the variable fails it.
"""

from __future__ import annotations

import os


def test_this_is_an_xdist_worker() -> None:
    assert os.environ.get("PYTEST_XDIST_WORKER", "").startswith("gw")


def test_the_worker_has_the_broker_sentinel() -> None:
    assert os.environ.get("STOINK_BROKER_BLOCKED") == "1"
