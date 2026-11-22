"""Run only by `test_env_isolation.py`, in a subprocess that starts with STOINK_DB set.

Not collected normally (the name does not start with `test_`). The module-level
line runs at collection, before any fixture: only a session-wide removal in the
root `conftest.py` can make it pass.
"""

from __future__ import annotations

import os

SEEN_AT_COLLECTION = os.environ.get("STOINK_DB")


def test_stoink_db_absent_from_session_start() -> None:
    assert SEEN_AT_COLLECTION is None
    assert "STOINK_DB" not in os.environ
