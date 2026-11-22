"""The git revision of the running code, recorded with every stored run.

`current_code_version()` returns `<HEAD sha>` for a clean checkout and
`<HEAD sha>-dirty:<12 hex>` otherwise, or `"unknown"`. It is a pure function of
the repository and the files in it: no module state, nothing computed at import
time, git run only when it is called. The package imports nothing legacy (the
engine import boundary holds).

What makes a tree dirty, and what the suffix hashes. Every difference between
the working files and HEAD counts, whatever its state in the index:
- a tracked file whose bytes differ from HEAD's blob, or that is deleted;
- a file in the index that is not in HEAD's tree (`git add` of a new file);
- a path in HEAD's tree that is no longer in the index (`git rm --cached`);
- an untracked, non-ignored file anywhere in the repository (listing runs from
  the repository top level, so a start folder inside the repository sees them
  all).
The suffix is the first 12 hex digits of a sha256 over those paths sorted by
name: for each its name, why it counts, kind, size and (up to
`MAX_HASHED_FILE`, 64 MB, streamed) bytes. A larger file contributes its name
and size only, and a tracked file that large is compared with HEAD by size
only: modification times are not used, they are not reliable. (The offline
bars that decide a result are well under 1 MB.)

Reading the version must not change the checkout, so git is only asked
read-only questions that never use the index's stat data: `rev-parse HEAD`,
`rev-parse --show-toplevel`, `ls-tree -r -l -z --full-tree HEAD` (HEAD's blob
ids and sizes), `ls-files --cached -z` (the index: names only) and `ls-files
--others --exclude-standard -z`. Never `git diff` / `status` / `diff-index`
(those refresh and may rewrite `.git/index`, take `index.lock` and run `clean`
filters). Every call passes `-c core.fsmonitor=false` and runs with
`GIT_OPTIONAL_LOCKS=0`. Tracked files are compared with HEAD here: the hash git
would give the file's bytes, as they are and with CRLF read as LF (a checkout
under `core.autocrlf`), is looked up among HEAD's blobs. No other conversion is
attempted, so a file that a custom filter changes counts as modified, and a
file mode change alone is not noticed.

Environment: git is run directly (no shell), with an explicit working
directory, a timeout of 15 s per call and no stdin. `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_INDEX_FILE` and `GIT_COMMON_DIR` are removed from the environment so git
looks at the checkout it is run in; nothing else is changed. In particular git
configuration (system, global, repository, `GIT_CONFIG_*` variables) is trusted
as the user's own, apart from `core.fsmonitor`, which is forced off.

Failures: `"unknown"` when git is missing or too slow, the start folder is not
in a repository, a listing fails or is malformed, or anything unexpected goes
wrong (never a traceback). A path that cannot be decoded or a file that cannot
be read counts as changed. The Python side (reading and hashing files) has an
overall time budget (`HASH_BUDGET`, 20 s, in addition to git's own timeouts);
when it runs out the answer is `<HEAD sha>-dirty:unknown` rather than a hang.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 15
HASH_BUDGET = 20.0
MAX_HASHED_FILE = 64 * 1024 * 1024  # a larger file contributes its name and size only
_CHUNK = 1024 * 1024
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

_ADDED = b"A"  # in the index, not in HEAD
_REMOVED = b"R"  # in HEAD, no longer in the index
_CHANGED = b"T"  # tracked, bytes differ from HEAD
_DELETED = b"D"  # tracked, missing from the working tree
_UNTRACKED = b"U"


class _OutOfTime(Exception):
    """The hashing budget ran out."""


def current_code_version(
    start: Path | None = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    git: str | None = None,
    budget: float = HASH_BUDGET,
    max_hashed: int = MAX_HASHED_FILE,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """The git revision of the code: `<HEAD sha>`, with `-dirty:<12 hex>` when modified.

    `start` is a directory inside the repository (default: the folder of this
    package). `run` is the subprocess runner, `git` the executable (default:
    `shutil.which("git")`), `budget` the seconds the file reading and hashing
    may take, `max_hashed` the largest file read in full and `clock` the time
    source for the budget; all four exist for tests. See the module docstring.
    """
    try:
        return _compute(start, run, git, budget, max_hashed, clock)
    except Exception:
        return "unknown"


def _compute(start: Path | None, run: Callable[..., Any], git: str | None, budget: float,
             max_hashed: int, clock: Callable[[], float]) -> str:
    exe = git if git is not None else shutil.which("git")
    if not exe:
        return "unknown"
    where = Path(__file__).resolve().parent if start is None else Path(start)
    child_env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    child_env["GIT_OPTIONAL_LOCKS"] = "0"

    def ask(*words: str, cwd: Path) -> bytes | None:
        try:
            done = run([exe, "-c", "core.fsmonitor=false", *words], cwd=str(cwd),
                       env=child_env, capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=GIT_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    head = ask("rev-parse", "HEAD", cwd=where)
    sha = "" if head is None else head.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        return "unknown"
    top = ask("rev-parse", "--show-toplevel", cwd=where)
    if top is None:
        return "unknown"
    root = Path(os.fsdecode(top).rstrip("\r\n"))
    # Every listing runs from the top level: `ls-files` lists below its working
    # directory only, and a start folder is usually a package inside the repository.
    tree = ask("ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD", cwd=root)
    cached = ask("ls-files", "--cached", "-z", cwd=root)
    others = ask("ls-files", "--others", "--exclude-standard", "-z", cwd=root)
    if tree is None or cached is None or others is None:
        return "unknown"

    deadline = clock() + budget

    def tick() -> None:
        if clock() > deadline:
            raise _OutOfTime

    try:
        changed = _changed_paths(root, sha, tree, cached, others, max_hashed, tick)
        if changed is None:
            return "unknown"
        if not changed:
            return sha
        return f"{sha}-dirty:{_digest(root, changed, max_hashed, tick)}"
    except _OutOfTime:
        return f"{sha}-dirty:unknown"


def _changed_paths(root: Path, sha: str, tree: bytes, cached: bytes, others: bytes,
                   max_hashed: int, tick: Callable[[], None]) -> dict[bytes, bytes] | None:
    """Path (as git prints it) -> why it counts as changed; None if `tree` is malformed."""
    algorithm = "sha1" if len(sha) == 40 else "sha256"
    indexed = {name for name in cached.split(b"\0") if name}
    in_head: set[bytes] = set()
    changed: dict[bytes, bytes] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        tick()
        header, tab, name = record.partition(b"\t")
        parts = header.split()
        if not tab or len(parts) != 4:
            return None
        _mode, otype, blob, blob_size = parts
        in_head.add(name)
        if name not in indexed:
            changed[name] = _REMOVED
            continue
        if otype != b"blob":
            continue  # a submodule is not part of this checkout's files
        path = _path_of(root, name)
        kind, size = _kind(path)
        if kind == "missing":
            changed[name] = _DELETED
            continue
        try:
            if kind in ("file", "link") and max(size, int(blob_size)) <= max_hashed:
                assert path is not None
                same = blob.decode("ascii") in _blob_ids(algorithm, path, kind == "link")
            else:  # too big to hash (or unreadable kind): name and size are all that is compared
                same = kind == "file" and size == int(blob_size)
        except (OSError, ValueError):
            same = False
        if not same:
            changed[name] = _CHANGED
    for name in sorted(indexed - in_head):
        changed[name] = _ADDED
    for name in others.split(b"\0"):
        if name:
            changed.setdefault(name, _UNTRACKED)
    return changed


def _digest(root: Path, changed: dict[bytes, bytes], max_hashed: int,
            tick: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    for name in sorted(changed):
        tick()
        path = _path_of(root, name)
        kind, size = _kind(path)
        digest.update(changed[name] + b"\0" + name + b"\0" + kind.encode() + b"\0"
                      + b"%d" % size + b"\0")
        if kind in ("file", "link") and size <= max_hashed:
            assert path is not None
            try:
                _feed(digest, path, kind == "link", tick)
            except OSError:
                digest.update(b"unreadable")
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def _path_of(root: Path, name: bytes) -> Path | None:
    """The working-tree path of a name git printed, or None if it cannot be decoded."""
    try:
        return root / os.fsdecode(name)
    except (UnicodeError, ValueError):
        return None


def _blob_ids(algorithm: str, path: Path, link: bool) -> set[str]:
    """The git object ids the file's bytes would have as a blob, without running filters.

    Two candidates: the raw bytes, and the bytes with CRLF turned into LF (what
    `core.autocrlf` / `text` attributes store for a file checked out with CRLF).
    The caller has checked the file is at most `max_hashed`.
    """
    if link:
        data = os.fsencode(os.readlink(path))
    else:
        with open(path, "rb") as handle:
            data = handle.read()
    ids = set()
    for body in (data, data.replace(b"\r\n", b"\n")):
        digest = hashlib.new(algorithm)
        digest.update(b"blob %d\0" % len(body))
        digest.update(body)
        ids.add(digest.hexdigest())
    return ids


def _feed(digest: Any, path: Path, link: bool, tick: Callable[[], None]) -> None:
    """Stream a file's bytes (or a link's target) into `digest`."""
    if link:
        digest.update(os.fsencode(os.readlink(path)))
        return
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            tick()


def _kind(path: Path | None) -> tuple[str, int]:
    """('file' | 'link' | 'other' | 'missing' | 'undecodable', size), links not followed."""
    if path is None:
        return "undecodable", 0
    try:
        info = path.lstat()
    except (OSError, ValueError):
        return "missing", 0
    if stat.S_ISLNK(info.st_mode):
        try:
            return "link", len(os.fsencode(os.readlink(path)))
        except (OSError, ValueError):
            return "missing", 0
    if stat.S_ISREG(info.st_mode):
        return "file", info.st_size
    return "other", 0
