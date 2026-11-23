"""Import boundaries of `MainStoinker/ib` (rules in that package's docstring).

1. `ibapi` is imported only under `MainStoinker/ib` and by a frozen allowlist of
   legacy files; the allowlist can only shrink.
2. `MainStoinker/ib` imports from `MainStoinker` only `MainStoinker.ib.*`,
   `MainStoinker.engine.*` and `MainStoinker.Util.broker_guard`.
3. `MainStoinker/ib` imports no raw network modules (`socket`, `ssl`, `asyncio`,
   `http`, ...), so every connection goes through `ibapi.connection.Connection`.
4. Importing the engine CLI and the API app does not load `MainStoinker.ib`.

AST-based over every `*.py` and `*.pyw` under `MainStoinker/`. Whether a file
is "inside `MainStoinker/ib`" is decided by its path, so odd file or folder
names (`ib.evil.py`, `ib.x/y.py`) cannot escape the scan. Seen: `import x`,
`import x.y as z`, `from x import y`, relative imports, imports inside
functions, classes and `if TYPE_CHECKING`, and `importlib.import_module("lit")`
/ `import_module(name="lit")` / `__import__("lit")` with a string literal
(positional or keyword; a relative literal is resolved against a literal
`package=` argument, else against the file's own package). In `MainStoinker/ib`
`from <any module> import socket|ssl|_socket` is also refused.

Not seen: extensionless scripts and any other file type; legacy modules
imported by a bare top-level name after a `sys.path` change (`from Util import
IBKRHelper`); an aliased import function (`imp = importlib.import_module;
imp("ibapi")`); dynamic imports with a computed name (a variable, f-string or
concatenation, or a computed `package=`); `exec`/`eval`; `importlib.util` spec
loading; and modules reached as attributes of an already imported object other
than the `from m import socket|ssl` form above.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "MainStoinker"
IB_DIR = PACKAGE_DIR / "ib"
IB_PACKAGE = "MainStoinker.ib"
ROOT_PACKAGE = "MainStoinker"

# The six legacy files that import ibapi today. Frozen: remove an entry when the
# file stops importing ibapi or goes away; never add one.
ORIGINAL_LEGACY_IBAPI_FILES = frozenset({
    "MainStoinker/DataCollection/apiApi.py",
    "MainStoinker/Util/IBKRHelper.py",
    "MainStoinker/MainStuff/this is how we do it dum dum.py",
    "MainStoinker/DataCollection/Dumb dumb/IBAPI_TESTING.py",
    "MainStoinker/testy/testy.py",
    "MainStoinker/testy/testy_advanced.py",
})

LEGACY_IBAPI_ALLOWLIST = (
    "MainStoinker/DataCollection/apiApi.py",
    "MainStoinker/Util/IBKRHelper.py",
    "MainStoinker/MainStuff/this is how we do it dum dum.py",
    "MainStoinker/DataCollection/Dumb dumb/IBAPI_TESTING.py",
    "MainStoinker/testy/testy.py",
    "MainStoinker/testy/testy_advanced.py",
)

# Where MainStoinker/ib may import from within MainStoinker. broker_guard is
# pure and stdlib only; it is named here whether or not it exists yet.
IB_ALLOWED_INTERNAL = (
    "MainStoinker.ib",
    "MainStoinker.engine",
    "MainStoinker.Util.broker_guard",
)
# `from <parent> import name` names `<parent>.name`; the parent alone is not judged.
PARENT_PACKAGES = frozenset({"MainStoinker", "MainStoinker.Util"})

# Raw network access is refused; connections go through ibapi.connection.Connection.
RAW_NETWORK_MODULES = (
    "socket", "_socket", "ssl", "asyncio", "http", "urllib", "socketserver",
    "selectors", "ftplib", "smtplib", "multiprocessing.connection", "xmlrpc",
    "imaplib", "poplib", "wsgiref",
)
# Names that must not be pulled out of any module (`from ibapi.connection import socket`).
RAW_NETWORK_NAMES = frozenset({"socket", "_socket", "ssl"})
SOURCE_SUFFIXES = (".py", ".pyw")


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    parts = package.split(".")
    base = parts[: max(len(parts) - (level - 1), 0)]
    return ".".join([*base, module] if module else base)


def _package_of(path: Path, root: Path = ROOT) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts if path.name == "__init__.py" else parts[:-1])


def imported_modules(source: str, filename: str, package: str) -> list[tuple[int, str]]:
    """(line, dotted module) for every import in source, at any depth.

    `from MainStoinker import x` yields `MainStoinker.x` (the parent alone is
    not yielded); other `from a.b import c` yields `a.b`. Relative imports are
    resolved against `package`. Literal-string dynamic imports are included.
    """
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
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name not in ("import_module", "__import__"):
                continue
            keywords = {k.arg: k.value for k in node.keywords if k.arg}
            target = node.args[0] if node.args else keywords.get("name")
            if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                continue
            literal = target.value
            if literal.startswith("."):
                anchor = package
                if name == "import_module":
                    given = node.args[1] if len(node.args) > 1 else keywords.get("package")
                    if isinstance(given, ast.Constant) and isinstance(given.value, str):
                        anchor = given.value
                stripped = literal.lstrip(".")
                level = len(literal) - len(stripped)
                literal = _resolve_relative(anchor, level, stripped or None)
            found.append((node.lineno, literal))
    return found


def ibapi_violations(source: str, filename: str, package: str) -> list[str]:
    """Imports of ibapi in a file that is not allowed to have any."""
    return [f"{filename}:{line}: imports {name}; only MainStoinker/ib and the frozen legacy "
            "allowlist may import ibapi"
            for line, name in imported_modules(source, filename, package)
            if _under(name, "ibapi")]


def ib_violations(source: str, filename: str, package: str = IB_PACKAGE) -> list[str]:
    """Breaches of rules 2 and 3 in a file of MainStoinker/ib."""
    found: list[str] = []
    for line, name in imported_modules(source, filename, package):
        if _under(name, ROOT_PACKAGE):
            if name != ROOT_PACKAGE and not any(_under(name, a) for a in IB_ALLOWED_INTERNAL):
                found.append(f"{filename}:{line}: {name} is not importable from MainStoinker/ib "
                             f"(allowed: {', '.join(IB_ALLOWED_INTERNAL)})")
        elif any(_under(name, m) for m in RAW_NETWORK_MODULES):
            found.append(f"{filename}:{line}: raw network module {name}; go through "
                         "ibapi.connection.Connection")
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.ImportFrom):
            found += [f"{filename}:{node.lineno}: raw network module name {a.name} imported "
                      "from another module; go through ibapi.connection.Connection"
                      for a in node.names if a.name in RAW_NETWORK_NAMES]
    return found


def allowlist_problems(
    allowlist: tuple[str, ...], original: frozenset[str] = ORIGINAL_LEGACY_IBAPI_FILES,
) -> list[str]:
    """Why the legacy allowlist is not a duplicate-free subset of the original six."""
    problems = [f"{p} is not one of the original legacy files; the allowlist can only shrink"
                for p in allowlist if p not in original]
    if len(set(allowlist)) != len(allowlist):
        problems.append("duplicate entries in the allowlist")
    return problems


def source_files(root: Path, sub: str | None = None) -> list[Path]:
    base = root / "MainStoinker" / sub if sub else root / "MainStoinker"
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES)


def ibapi_scan(root: Path, allowlist: tuple[str, ...] = LEGACY_IBAPI_ALLOWLIST) -> list[str]:
    """ibapi imports under root/MainStoinker outside root/MainStoinker/ib and the allowlist."""
    allowed = set(allowlist)
    ib_dir = root / "MainStoinker" / "ib"
    found: list[str] = []
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in allowed or path.is_relative_to(ib_dir):
            continue
        found += ibapi_violations(path.read_text(encoding="utf-8"), rel, _package_of(path, root))
    return found


def _ib_files() -> list[Path]:
    files = source_files(ROOT, "ib")
    assert files, f"no files under {IB_DIR}"
    return files


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_only_ib_and_frozen_legacy_import_ibapi() -> None:
    assert source_files(ROOT), f"no files under {PACKAGE_DIR}"
    found = ibapi_scan(ROOT)
    assert not found, "\n".join(found)


def test_legacy_ibapi_allowlist_is_frozen_and_files_exist() -> None:
    assert not allowlist_problems(LEGACY_IBAPI_ALLOWLIST)
    assert len(ORIGINAL_LEGACY_IBAPI_FILES) == 6
    missing = [p for p in LEGACY_IBAPI_ALLOWLIST if not (ROOT / p).is_file()]
    assert not missing, f"remove deleted files from the allowlist: {missing}"


def test_allowlist_checker_refuses_new_or_swapped_entries() -> None:
    original = tuple(sorted(ORIGINAL_LEGACY_IBAPI_FILES))
    assert not allowlist_problems(original)
    assert not allowlist_problems(original[:-1])  # shrinking is fine
    assert allowlist_problems((*original, "MainStoinker/Util/zz_new.py"))
    assert allowlist_problems((*original[:-1], "MainStoinker/Util/zz_new.py"))  # swap
    assert allowlist_problems((*original, original[0]))  # duplicate


def test_ibapi_scan_is_decided_by_path_not_dotted_name(tmp_path: Path) -> None:
    pkg = tmp_path / "MainStoinker"
    (pkg / "ib").mkdir(parents=True)
    (pkg / "ib" / "ok.py").write_text("import ibapi\n", encoding="utf-8")
    assert ibapi_scan(tmp_path, ()) == []
    (pkg / "ib.evil.py").write_text("import ibapi\n", encoding="utf-8")
    (pkg / "ib.x").mkdir()
    (pkg / "ib.x" / "y.py").write_text("import ibapi\n", encoding="utf-8")
    (pkg / "other.pyw").write_text("import ibapi\n", encoding="utf-8")
    found = ibapi_scan(tmp_path, ())
    assert any("ib.evil.py" in f for f in found), found
    assert any("ib.x/y.py" in f for f in found), found
    assert any("other.pyw" in f for f in found), found
    assert not any("ib/ok.py" in f for f in found), found


def test_ib_imports_no_legacy_modules() -> None:
    found: list[str] = []
    for path in _ib_files():
        found += [v for v in ib_violations(path.read_text(encoding="utf-8"), _rel(path),
                                           _package_of(path))
                  if "raw network" not in v]
    assert not found, "\n".join(found)


def test_ib_opens_no_raw_sockets() -> None:
    found: list[str] = []
    for path in _ib_files():
        found += [v for v in ib_violations(path.read_text(encoding="utf-8"), _rel(path),
                                           _package_of(path))
                  if "raw network" in v]
    assert not found, "\n".join(found)


def test_ib_checker_catches_violations() -> None:
    def ibapi_bad(src: str, package: str = "MainStoinker.Foo") -> list[str]:
        return ibapi_violations(src, "x.py", package)

    def ib_bad(src: str) -> list[str]:
        return ib_violations(src, "x.py", IB_PACKAGE)

    # Rule 1: every spelling of an ibapi import, at any depth.
    for src in (
        "import ibapi\n",
        "import ibapi.client\n",
        "import ibapi.client as c\n",
        "from ibapi import client\n",
        "from ibapi.client import EClient\n",
        "def f():\n    import ibapi\n",
        "class C:\n    def m(self):\n        from ibapi.wrapper import EWrapper\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ibapi.contract import Contract\n",
        "import importlib\nimportlib.import_module('ibapi.client')\n",
        "from importlib import import_module\nimport_module('ibapi')\n",
        "__import__('ibapi.wrapper')\n",
        "import importlib\nimportlib.import_module(name='ibapi')\n",
        "import importlib\nimportlib.import_module('ibapi', package='x')\n",
        "__import__(name='ibapi')\n",
        "import importlib\nimportlib.import_module('.client', package='ibapi')\n",
    ):
        assert ibapi_bad(src), src
    assert not ibapi_bad("import os\nfrom MainStoinker.engine import store\nimport ibapi_like\n")
    # A computed name is invisible by design (documented in the module docstring).
    assert not ibapi_bad("import importlib\nname = 'ibapi'\nimportlib.import_module(name)\n")

    # Rule 2: legacy modules, including relative and nested forms.
    for src in (
        "import MainStoinker.DataCollection.apiApi\n",
        "from MainStoinker.TradeTools.trade import Trade\n",
        "from MainStoinker import MainStuff\n",
        "from MainStoinker import Util\n",
        "from MainStoinker.Util import IBKRHelper\n",
        "from MainStoinker.Util.SocketIO_Client import x\n",
        "from MainStoinker.Algos.Foo import y\n",
        "from MainStoinker import *\n",
        "from .. import TradeTools\n",
        "from ..Util import IBKRHelper\n",
        "from ..Util.IBKRHelper import x\n",
        "from ..api import app\n",
        "def f():\n    import MainStoinker.MainStuff.Globals\n",
        "class C:\n    def m(self):\n        from MainStoinker.DataCollection import apiApi\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..TradeTools import trade\n",
        "import importlib\nimportlib.import_module('MainStoinker.TradeTools.trade')\n",
        "__import__('MainStoinker.Util.IBKRHelper')\n",
        "import importlib\nimportlib.import_module('..MainStuff.Globals')\n",
        "import importlib\nimportlib.import_module(name='MainStoinker.TradeTools')\n",
        "import importlib\nimportlib.import_module('.TradeTools', package='MainStoinker')\n",
        "import importlib\nimportlib.import_module(name='.TradeTools', package='MainStoinker')\n",
    ):
        assert ib_bad(src), src
    assert not ib_bad(
        "from MainStoinker.engine.broker import Broker\n"
        "from MainStoinker.engine import ledger\n"
        "from MainStoinker.Util.broker_guard import guard\n"
        "from MainStoinker.Util import broker_guard\n"
        "from MainStoinker.ib.client import C\n"
        "from . import client\n"
        "from .client import C\n"
        "from ..engine import ledger\n"
        "from ..Util.broker_guard import guard\n"
        "import importlib\n"
        "importlib.import_module('.client', package='MainStoinker.ib')\n"
        "import MainStoinker\n"
        "import ibapi.client\nimport threading, json\n"
    )

    # Rule 3: raw network modules.
    for src in (
        "import socket\n",
        "import socket as s\n",
        "from socket import socket\n",
        "import ssl\n",
        "from ssl import SSLContext\n",
        "import asyncio\n",
        "from asyncio import open_connection\n",
        "import http.client\n",
        "from http import client\n",
        "import urllib.request\n",
        "def f():\n    import socket\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import ssl\n",
        "import importlib\nimportlib.import_module('socket')\n",
        "__import__('asyncio')\n",
        "import multiprocessing.connection\n",
        "from multiprocessing.connection import Client\n",
        "import xmlrpc.client\n",
        "import imaplib\n",
        "import poplib\n",
        "from wsgiref import simple_server\n",
        "from ibapi.connection import socket\n",
        "from ibapi.connection import ssl as s\n",
        "from . import socket\n",
    ):
        assert ib_bad(src), src
    assert not ib_bad("import sockets_like\nimport sslx\nimport httpx_like\n"
                      "import multiprocessing\nfrom ibapi.connection import Connection\n")


def test_importing_engine_and_api_does_not_load_ib() -> None:
    code = (
        "import sys\n"
        "import MainStoinker.engine.cli\n"
        "import MainStoinker.api.app\n"
        "import MainStoinker.api.serve\n"
        "bad = sorted(m for m in sys.modules\n"
        "             if m == 'MainStoinker.ib' or m.startswith('MainStoinker.ib.'))\n"
        "print(','.join(bad))\n"
    )
    # The tests run in a scratch directory and the venv has an editable install of
    # another checkout: point the child at this one.
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
        cwd=ROOT, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
