"""The indicators package imports only the standard library, numpy and pandas.

Nothing from the rest of `MainStoinker` (legacy runtime, engine, api, ib, algos)
and not `ibapi`, so an algo, the engine or a notebook can all use it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.test_engine_import_boundary import find_violations

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "MainStoinker" / "indicators"
PACKAGE = "MainStoinker.indicators"

# Everything under MainStoinker is refused except the indicators package itself.
FORBIDDEN = tuple(
    f"MainStoinker.{name}"
    for name in (
        "MainStuff", "DataCollection", "TradeTools", "Algos", "Util", "NeatTools", "api", "ib", "engine",
    )
) + ("ibapi",)


def test_indicators_have_no_forbidden_imports():
    files = sorted(PKG_DIR.rglob("*.py"))
    assert files, f"no files under {PKG_DIR}"
    violations: list[str] = []
    for path in files:
        parts = path.relative_to(ROOT).with_suffix("").parts
        package = ".".join(parts if path.name == "__init__.py" else parts[:-1])
        violations += find_violations(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix(), package,
            forbidden=FORBIDDEN, own_package=PACKAGE,
        )
    assert not violations, "\n".join(violations)


def test_checker_catches_violations_for_this_package():
    def bad(src: str) -> list[str]:
        return find_violations(src, "x.py", PACKAGE, forbidden=FORBIDDEN, own_package=PACKAGE)

    assert bad("from MainStoinker.engine import feed\n")
    assert bad("import MainStoinker.ib.client\n")
    assert bad("from MainStoinker import api\n")
    assert bad("def f():\n    import MainStoinker.Algos.FVG\n")
    assert bad("from ..engine import feed\n")
    assert bad("import ibapi\n")
    assert not bad("import pandas\nfrom .fvg import FvgConfig\nfrom MainStoinker.indicators import fvg\n")


def test_importing_the_detector_loads_no_other_project_module():
    code = (
        "import sys\n"
        "import MainStoinker.indicators.fvg\n"
        "bad = sorted(m for m in sys.modules if m.startswith('MainStoinker.') and not\n"
        "             m.startswith('MainStoinker.indicators'))\n"
        "bad += [m for m in ('ibapi',) if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code], capture_output=True, text=True, check=True, timeout=60,
        cwd=ROOT,  # the tests run in a scratch directory; import this checkout
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.stdout.strip() == ""
