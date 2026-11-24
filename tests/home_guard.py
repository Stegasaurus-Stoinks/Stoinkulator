"""The "no test wrote into the real home folder" guard of the `tests/test_ib_*` files.

An earlier version compared a listing (names and modification times) of the real
`~/Stoinkulator/{live,bars,recordings}` before and after each test. The owner or an agent running a
real fetch or watch while a suite runs writes exactly those folders, so unrelated tests failed in
teardown (101 errors in one file in one run). A listing cannot tell that writer from the test.

This guard does not look at the folder at all. While a test runs, the calls that write or change
the file system (`open` for writing, `mkdir`, `replace`, `rename`, `remove`, `rmdir`, ...) are
wrapped, and one aimed at a path inside the real `Stoinkulator` folder raises at once and is
recorded; the test fails at teardown if anything was recorded, even when the code under test
swallowed the error. Only this process is watched, so what else runs on the machine cannot matter.
Reads are not restricted. The real folder is never touched by the guard itself.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


class RealHomeWrite(AssertionError):
    """A test (or the code under test) tried to write into the real `Stoinkulator` folder."""


WRITE_MODE_CHARS = frozenset("wax+")


def _inside(path: Any, root: str) -> bool:
    try:
        text = os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return False  # a file descriptor or something unusable as a path
    return text == root or text.startswith(root + os.sep)


@contextlib.contextmanager
def no_writes_to_real_home(real_home: Path | None) -> Iterator[list[str]]:
    """Fail on any write inside `real_home/Stoinkulator`; yields the list of attempts recorded."""
    attempts: list[str] = []
    if real_home is None:
        yield attempts
        return
    root = os.path.normcase(os.path.abspath(real_home / "Stoinkulator"))
    patch = pytest.MonkeyPatch()

    def refuse(what: str, path: Any) -> None:
        message = f"{what} {os.fspath(path)!r}"
        attempts.append(message)
        raise RealHomeWrite(f"A TEST WROTE INTO THE REAL HOME FOLDER: {message}")

    def wrap_open(real: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if WRITE_MODE_CHARS & set(mode) and _inside(file, root):
                refuse(f"open({mode!r})", file)
            return real(file, mode, *args, **kwargs)

        return guarded

    def wrap_path_call(name: str, real: Callable[..., Any], count: int) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            for path in [*args[:count], *[v for k, v in kwargs.items() if k in ("src", "dst", "path")]]:
                if _inside(path, root):
                    refuse(f"os.{name}", path)
            return real(*args, **kwargs)

        return guarded

    patch.setattr(builtins, "open", wrap_open(builtins.open))
    patch.setattr(io, "open", wrap_open(io.open))
    for name, count in (("mkdir", 1), ("makedirs", 1), ("replace", 2), ("rename", 2), ("remove", 1),
                        ("unlink", 1), ("rmdir", 1)):
        patch.setattr(os, name, wrap_path_call(name, getattr(os, name), count))
    try:
        yield attempts
    finally:
        patch.undo()
    assert not attempts, f"writes into the real home folder were attempted: {attempts}"
