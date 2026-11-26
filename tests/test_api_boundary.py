"""The API package may import only an allowlist from the rest of `MainStoinker`.

From `MainStoinker` the API may import `MainStoinker.api.*` and the engine
modules named in `ALLOWED_ENGINE_MODULES`: the ones that read stored runs,
compute statistics and read bars, and cannot build a broker or run a backtest.
Everything else under `MainStoinker` is refused, including the legacy runtime
(`Util`, `MainStuff`, `TradeTools`, `DataCollection`, `Algos`) and the engine
parts that build brokers, load algos or run backtests (`cli`, `engine`,
`algos`, `modes`, `paper_broker`, `broker`, `router`). `ibapi` is refused too.
A new module is therefore forbidden until it is added here in a reviewed edit.

`MainStoinker.indicators` (issue 276: the FVG zones of the dashboard) is allowed as a whole
package: it is pure (standard library, numpy, pandas; its own boundary test) and
`test_the_indicators_the_api_may_import_stay_pure` re-checks that from this side.

The allowed list is what the API needs now (`store`) and what issues 83 to 87
need (`stats`, `run_documents`, `report`, `feed` for the bar reader, `ledger`
record types). `run_documents` does not exist yet; naming it here only reserves
the name. `feed` imports pandas at import time, which is accepted. `report`
imports `paper_broker` (the cost model) and through it `broker`, so those two
load transitively once a route uses `report`; the runtime check below tolerates
exactly that and nothing else.

The engine side keeps its own test (`test_engine_import_boundary`): the engine
must not import the API.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from tests.test_engine_import_boundary import ENGINE_DIR, _resolve_relative, find_violations

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "MainStoinker" / "api"
API_PACKAGE = "MainStoinker.api"
ROOT_PACKAGE = "MainStoinker"
ENGINE_PACKAGE = "MainStoinker.engine"

ALLOWED_ENGINE_MODULES = frozenset({
    "MainStoinker.engine.store",
    "MainStoinker.engine.stats",
    "MainStoinker.engine.run_documents",
    "MainStoinker.engine.report",
    "MainStoinker.engine.feed",
    "MainStoinker.engine.ledger",
})
# Whole packages outside `engine` that the API may import (issue 276).
ALLOWED_PACKAGES = ("MainStoinker.indicators",)
# Packages whose members are named one by one: `from MainStoinker.engine import store`.
PARENT_PACKAGES = frozenset({ROOT_PACKAGE, ENGINE_PACKAGE})
# Named explicitly in the issue, and checked at runtime as well.
MUST_BE_FORBIDDEN = (
    "MainStoinker.Util", "MainStoinker.MainStuff", "MainStoinker.TradeTools",
    "MainStoinker.DataCollection", "MainStoinker.Algos",
    "MainStoinker.engine.cli", "MainStoinker.engine.engine", "MainStoinker.engine.algos",
    "MainStoinker.engine.modes", "MainStoinker.engine.paper_broker",
    "MainStoinker.engine.broker", "MainStoinker.engine.router", "MainStoinker.ib", "ibapi",
)
# Loaded transitively by an allowed module (`report`), not importable by the API directly.
TRANSITIVE_OK = frozenset({"MainStoinker.engine.paper_broker", "MainStoinker.engine.broker"})


def _is_under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def is_allowed(module: str) -> bool:
    if not (_is_under(module, ROOT_PACKAGE) or _is_under(module, "ibapi")):
        return True  # stdlib and third party are not this test's business
    if _is_under(module, "ibapi"):
        return False
    if _is_under(module, API_PACKAGE):
        return True
    return any(_is_under(module, allowed)
               for allowed in (*ALLOWED_ENGINE_MODULES, *ALLOWED_PACKAGES))


def imported_modules(source: str, filename: str, package: str) -> list[tuple[int, str]]:
    """Every module a file imports, at any depth; `from <package> import x` names `<package>.x`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = (_resolve_relative(package, node.level, node.module) if node.level
                    else node.module or "")
            if base in PARENT_PACKAGES:
                found += [(node.lineno, f"{base}.{a.name}") for a in node.names]
            else:
                found.append((node.lineno, base))
    return found


def violations(source: str, filename: str, package: str) -> list[str]:
    return [f"{filename}:{line}: {name} is not on the API's import allowlist"
            for line, name in imported_modules(source, filename, package)
            if not is_allowed(name)]


def _scan() -> list[str]:
    files = sorted(API_DIR.rglob("*.py"))
    assert files, f"no files found under {API_DIR}"
    found: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT)
        parts = rel.with_suffix("").parts
        pkg = ".".join(parts if path.name == "__init__.py" else parts[:-1])
        found += violations(path.read_text(encoding="utf-8"), rel.as_posix(), pkg)
    return found


def test_api_imports_only_the_allowlist() -> None:
    found = _scan()
    assert not found, "\n".join(found)


def test_allowlist_names_nothing_it_must_forbid() -> None:
    for forbidden in MUST_BE_FORBIDDEN:
        assert not is_allowed(forbidden), forbidden
        assert not any(_is_under(forbidden, a) or _is_under(a, forbidden)
                       for a in (*ALLOWED_ENGINE_MODULES, *ALLOWED_PACKAGES)), forbidden


def test_allowlist_checker_catches_violations() -> None:
    def bad(src: str) -> list[str]:
        return violations(src, "x.py", API_PACKAGE)

    for forbidden in MUST_BE_FORBIDDEN:
        assert bad(f"import {forbidden}\n"), forbidden
        assert bad(f"from {forbidden} import thing\n"), forbidden
    assert bad("from MainStoinker.engine import cli\n")
    assert bad("from MainStoinker.engine import paper_broker, store\n")
    assert bad("from MainStoinker import Util\n")
    assert bad("from MainStoinker.Util.IBKRHelper import x\n")
    assert bad("from MainStoinker.engine.somethingnew import x\n")
    assert bad("def f():\n    from ..engine import cli\n")
    assert bad("class C:\n    def m(self):\n        import ibapi.client\n")
    assert bad("from .. import MainStuff\n")
    assert not bad("from MainStoinker.engine.store import open_store_readonly\n")
    assert not bad("from MainStoinker.engine import store, stats, feed\n")
    assert not bad("from MainStoinker.engine.ledger import Fill\n")
    assert not bad("from .errors import NotFound\n")
    assert not bad("from MainStoinker.api.db import with_store\n")
    assert not bad("from . import db\n")
    assert not bad("import fastapi\nimport sqlite3\n")
    assert not bad("from MainStoinker.indicators.fvg import FvgConfig\n")
    assert not bad("from MainStoinker.indicators import fvg\n")
    assert not bad("from MainStoinker import indicators\n")
    assert bad("from MainStoinker.indicatorsX import y\n")  # a prefix is not a package


def test_the_indicators_the_api_may_import_stay_pure() -> None:
    """The API loads `indicators.fvg`; no module of that package imports another project module or `ibapi`."""
    code = (
        "import sys\n"
        "import MainStoinker.api.routes_fvg\n"
        "own = sorted(m for m in sys.modules if m.startswith('MainStoinker.indicators'))\n"
        "assert 'MainStoinker.indicators.fvg' in own, own\n"
        "import ast, pathlib\n"
        "import MainStoinker.indicators as pkg\n"
        "seen = set()\n"
        "for path in pathlib.Path(pkg.__file__).parent.rglob('*.py'):\n"
        "    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):\n"
        "        if isinstance(node, ast.Import):\n"
        "            seen.update(a.name for a in node.names)\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            seen.add(('.' * node.level) + (node.module or ''))\n"
        "bad = sorted(n for n in seen if n.startswith('ibapi') or n.startswith('..') or (\n"
        "    n.startswith('MainStoinker') and not n.startswith('MainStoinker.indicators')))\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code], capture_output=True, text=True, timeout=120,
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_engine_does_not_import_api() -> None:
    found: list[str] = []
    for path in sorted(ENGINE_DIR.rglob("*.py")):
        rel = path.relative_to(ROOT)
        parts = rel.with_suffix("").parts
        pkg = ".".join(parts if path.name == "__init__.py" else parts[:-1])
        found += find_violations(path.read_text(encoding="utf-8"), rel.as_posix(), pkg,
                                 (API_PACKAGE,), ENGINE_PACKAGE)
    assert not found, "\n".join(found)


def test_importing_api_loads_no_legacy_module() -> None:
    banned = tuple(m for m in MUST_BE_FORBIDDEN if m not in TRANSITIVE_OK)
    code = (
        "import sys\n"
        "import MainStoinker.api.app\n"
        "import MainStoinker.api.serve\n"
        f"banned = {banned!r}\n"
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
