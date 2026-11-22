"""The API package depends on the engine's stored-run side only, never on the legacy runtime.

AST-based like `test_engine_import_boundary` (same checker). The API may import
the engine's store and records, but not the algo loader, the CLI or the modes
(they import algos, which pull in the legacy runtime), and the engine must not
import the API.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.test_engine_import_boundary import ENGINE_DIR, FORBIDDEN_PREFIXES, find_violations

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "MainStoinker" / "api"
API_PACKAGE = "MainStoinker.api"
API_FORBIDDEN = (
    *(p for p in FORBIDDEN_PREFIXES if p != API_PACKAGE),
    "MainStoinker.engine.algos",
    "MainStoinker.engine.cli",
    "MainStoinker.engine.modes",
)


def _scan(directory: Path, package: str, forbidden: tuple[str, ...]) -> list[str]:
    files = sorted(directory.rglob("*.py"))
    assert files, f"no files found under {directory}"
    found: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT)
        parts = rel.with_suffix("").parts
        pkg = ".".join(parts if path.name == "__init__.py" else parts[:-1])
        found += find_violations(
            path.read_text(encoding="utf-8"), rel.as_posix(), pkg, forbidden, package
        )
    return found


def test_api_has_no_forbidden_imports() -> None:
    violations = _scan(API_DIR, API_PACKAGE, API_FORBIDDEN)
    assert not violations, "\n".join(violations)


def test_api_checker_catches_violations() -> None:
    def bad(src: str) -> list[str]:
        return find_violations(src, "x.py", API_PACKAGE, API_FORBIDDEN, API_PACKAGE)

    assert bad("from MainStoinker.engine.algos import load_algo\n")
    assert bad("import MainStoinker.engine.cli\n")
    assert bad("from MainStoinker.engine import modes\n")
    assert bad("from MainStoinker.TradeTools.trade import Trade\n")
    assert bad("import ibapi\n")
    assert bad("def f():\n    from ..engine import cli\n")
    assert not bad("from MainStoinker.engine.store import open_store_readonly\n")
    assert not bad("from .errors import NotFound\n")


def test_engine_does_not_import_api() -> None:
    violations = _scan(ENGINE_DIR, "MainStoinker.engine", (API_PACKAGE,))
    assert not violations, "\n".join(violations)


def test_importing_api_loads_no_legacy_module() -> None:
    code = (
        "import sys\n"
        "import MainStoinker.api.app\n"
        "import MainStoinker.api.serve\n"
        f"banned = {API_FORBIDDEN!r}\n"
        "bad = sorted(m for m in sys.modules\n"
        "             if any(m == p or m.startswith(p + '.') for p in banned))\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
        cwd=ROOT,  # the tests run in a scratch directory; import this checkout
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
