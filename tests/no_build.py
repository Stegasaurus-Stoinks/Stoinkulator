"""Make a "usage error, exit 2 before anything is built" test fail fast instead of hanging.

A test that expects the argument parser to stop a run drives `main` with fake factories. If the
parser regressed and accepted the arguments, those factories would start a real (fake-broker)
watch or fetch that waits for scripted time, and the test would hang instead of failing.
`forbid_building` replaces every factory of a rig/session with one that raises at once.
"""

from __future__ import annotations

from typing import Any

FACTORY_NAMES = ("connection_factory", "market_data_factory", "recorder_factory", "client_factory")


class BuiltDespiteUsageError(AssertionError):
    pass


def forbid_building(owner: Any) -> None:
    """Every factory attribute `owner` has now raises when called (instance attributes shadow methods)."""

    def make(name: str) -> Any:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise BuiltDespiteUsageError(f"{name} was called: the arguments should have been refused first")

        return refuse

    for name in FACTORY_NAMES:
        if hasattr(owner, name):
            setattr(owner, name, make(name))
