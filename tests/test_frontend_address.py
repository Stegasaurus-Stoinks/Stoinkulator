"""The front-end address comes from STOINK_FRONTEND_ADDR, not from the source."""
import re
import subprocess
from pathlib import Path

import pytest

from MainStoinker.MainStuff.Globals import Config

ENV = "STOINK_FRONTEND_ADDR"
REPO = Path(__file__).resolve().parent.parent


def test_default_is_localhost_3000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert Config().FrontEndPort == "localhost:3000"


def test_env_variable_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "dashboard.example:8080")
    assert Config().FrontEndPort == "dashboard.example:8080"


def test_empty_variable_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "")
    assert Config().FrontEndPort == "localhost:3000"


def test_no_private_address_in_tracked_python() -> None:
    out = subprocess.run(
        ["git", "grep", "-nE", r"192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+", "--", "*.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    # Ignore this file: it names the pattern it searches for.
    hits = [
        line for line in out.stdout.splitlines()
        if not re.match(r"tests[/\\]test_frontend_address\.py:", line)
    ]
    assert hits == []
