"""The engine package must not depend on legacy modules or ibapi.

AST-based: scans every *.py under MainStoinker/engine, including imports
inside functions and classes. Dynamic imports through importlib with a
computed string are invisible to this check by design (the future algo loader
uses one).
"""

import ast
import subprocess
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent / "MainStoinker" / "engine"
ENGINE_PACKAGE = "MainStoinker.engine"

FORBIDDEN_PREFIXES = (
    "MainStoinker.MainStuff",
    "MainStoinker.DataCollection",
    "MainStoinker.TradeTools",
    "MainStoinker.Algos",
    "MainStoinker.Util.SocketIO_Client",
    "MainStoinker.NeatTools",
    "ibapi",
)

# Imports inside a function or class body are checked exactly like module-level
# ones. A future exception must be added here as "<filename>:<module>" in a
# reviewed edit; module-level imports can never be allow-listed.
ALLOWED_FUNCTION_LEVEL_IMPORTS: set[str] = set()


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def _is_outside_engine(module: str) -> bool:
    return module.startswith("MainStoinker") and not (
        module == ENGINE_PACKAGE or module.startswith(ENGINE_PACKAGE + ".")
    )


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, module] if module else base)


def find_violations(source: str, filename: str, package: str = ENGINE_PACKAGE) -> list[str]:
    """Return 'filename:line: message' for each forbidden import in source.

    `package` is the dotted package the file lives in, used to resolve
    relative imports.
    """
    tree = ast.parse(source, filename=filename)
    found: list[str] = []

    def visit(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            names: list[str] = []
            relative = False
            if isinstance(child, ast.Import):
                names = [a.name for a in child.names]
            elif isinstance(child, ast.ImportFrom):
                relative = child.level > 0
                if relative:
                    base = _resolve_relative(package, child.level, child.module)
                else:
                    base = child.module or ""
                names = [base]
                # `from MainStoinker import MainStuff` imports a submodule.
                names += [f"{base}.{a.name}" for a in child.names if a.name != "*"]
            for name in names:
                bad = _is_forbidden(name) or (relative and _is_outside_engine(name))
                allowed = depth > 0 and f"{filename}:{name}" in ALLOWED_FUNCTION_LEVEL_IMPORTS
                if bad and not allowed:
                    found.append(f"{filename}:{child.lineno}: forbidden import of {name}")
                    break
            nested = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            visit(child, depth + 1 if nested else depth)

    visit(tree, 0)
    return found


def test_engine_has_no_forbidden_imports():
    files = sorted(ENGINE_DIR.rglob("*.py"))
    assert files, f"no engine files found under {ENGINE_DIR}"
    violations: list[str] = []
    for path in files:
        rel = path.relative_to(ENGINE_DIR.parent.parent).as_posix()
        pkg_parts = path.relative_to(ENGINE_DIR.parent.parent).with_suffix("").parts
        package = ".".join(pkg_parts if path.name == "__init__.py" else pkg_parts[:-1])
        violations += find_violations(path.read_text(encoding="utf-8"), rel, package)
    assert not violations, "\n".join(violations)


def test_checker_catches_violations():
    src = "import os\nfrom MainStoinker.TradeTools.trade import Trade\n"
    assert find_violations(src, "x.py") == [
        "x.py:2: forbidden import of MainStoinker.TradeTools.trade"
    ]
    assert find_violations("import ibapi.client\n", "x.py")
    assert find_violations("from MainStoinker import MainStuff\n", "x.py")
    assert find_violations("def f():\n    import MainStoinker.Algos.Foo\n", "x.py")
    assert find_violations(
        "class C:\n    def m(self):\n        from ibapi import wrapper\n", "x.py"
    )
    assert find_violations("from ..Util import SocketIO_Client\n", "x.py")
    assert find_violations("from .. import TradeTools\n", "x.py")
    assert find_violations("from ..Util.broker_guard import x\n", "x.py")
    # allowed: stdlib, third party, and engine-internal imports
    ok = "import pandas\nfrom .broker import Broker\nfrom MainStoinker.engine import x\n"
    assert not find_violations(ok, "x.py")


def test_importing_engine_does_not_import_legacy_modules():
    code = (
        "import sys\n"
        "import MainStoinker.engine.broker\n"
        "bad = [m for m in ('MainStoinker.DataCollection.apiApi',\n"
        "                   'MainStoinker.Util.SocketIO_Client',\n"
        "                   'MainStoinker.TradeTools.trade', 'ibapi') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == ""
