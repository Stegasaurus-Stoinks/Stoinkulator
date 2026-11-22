"""`engine/codeversion.py`: the git revision stored with a run.

Most tests answer through a fake git runner; the ones marked `needs_git` build
a real repository under pytest's `tmp_path` (never inside this checkout).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import MainStoinker.engine.codeversion as codeversion
from MainStoinker.engine.codeversion import current_code_version

VERSION = "0123456789abcdef0123456789abcdef01234567"
GIT = "/usr/bin/git"  # never executed: the fake runner answers


class Done:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout, self.returncode = stdout, returncode


def names_in(tree: bytes) -> bytes:
    """The index a clean checkout of `tree` would have: the same names."""
    names = (record.partition(b"\t")[2] for record in tree.split(b"\0") if record)
    return b"".join(name + b"\0" for name in names)


def fake_git(sha: bytes, tree: bytes = b"", others: bytes = b"", top: str = "/nowhere",
             calls: list | None = None, cached: bytes | None = None):
    def runner(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        assert args[:3] == [GIT, "-c", "core.fsmonitor=false"], args
        words = args[3:]
        if words == ["rev-parse", "HEAD"]:
            return Done(sha + b"\n")
        if words == ["rev-parse", "--show-toplevel"]:
            return Done(top.encode() + b"\n")
        if words[0] == "ls-tree":
            return Done(tree)
        if words[0] == "ls-files" and "--cached" in words:
            return Done(names_in(tree) if cached is None else cached)
        if words[0] == "ls-files" and "--others" in words:
            return Done(others)
        raise AssertionError(args)

    return runner


def version_of(start, **kwargs) -> str:
    budget = {k: kwargs.pop(k) for k in ("max_hashed", "budget", "clock") if k in kwargs}
    return current_code_version(start, run=fake_git(VERSION.encode(), **kwargs), git=GIT,
                                **budget)


def blob_of(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tracked(name: str, committed: bytes) -> bytes:
    """One `git ls-tree -r -l -z` record for a file whose HEAD content is `committed`."""
    return f"100644 blob {blob_of(committed)} {len(committed):>7}\t{name}\0".encode()


def test_code_version_clean(tmp_path):
    assert version_of(tmp_path, top=str(tmp_path)) == VERSION


def test_code_version_dirty_suffix_and_the_git_commands_asked(tmp_path):
    (tmp_path / "x.py").write_bytes(b"changed\n")
    committed = tracked("x.py", b"original\n")
    calls: list = []
    version = version_of(tmp_path, tree=committed, top=str(tmp_path), calls=calls)
    assert version.startswith(VERSION + "-dirty:") and len(version.split("-dirty:")[1]) == 12
    # read-only plumbing only (never diff / status / diff-index, which can refresh the index and
    # run filters), no shell, with a timeout, with fsmonitor off and optional locks (index
    # refresh writes) off
    assert [c[0][3:5] for c in calls] == [["rev-parse", "HEAD"], ["rev-parse", "--show-toplevel"],
                                          ["ls-tree", "-r"], ["ls-files", "--cached"],
                                          ["ls-files", "--others"]]
    assert "--exclude-standard" in calls[4][0]
    for args, kwargs in calls:
        assert isinstance(args, list) and not kwargs.get("shell")
        assert kwargs["cwd"] == str(tmp_path) and 0 < kwargs["timeout"] <= 60
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert not {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR"} & set(
            kwargs["env"])
        assert not {"diff", "diff-index", "diff-files", "status", "--refresh"} & set(args)
    (tmp_path / "x.py").write_bytes(b"changed again\n")  # different content, different suffix
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)) != version
    (tmp_path / "x.py").write_bytes(b"original\n")  # back to HEAD's content: clean
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)) == VERSION
    (tmp_path / "x.py").write_bytes(b"original\r\n")  # a CRLF checkout of the same text: clean
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)) == VERSION


def test_code_version_defaults_to_the_package_directory():
    calls: list = []
    current_code_version(run=fake_git(VERSION.encode(), calls=calls), git=GIT)
    assert calls[0][1]["cwd"] == str(Path(codeversion.__file__).resolve().parent)


def test_code_version_resolves_git_once_with_which(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(codeversion.shutil, "which", lambda name: seen.append(name) or "/x/git")
    calls: list = []

    def runner(args, **kwargs):
        calls.append(args)
        return Done(b"", 1)

    current_code_version(tmp_path, run=runner)
    assert seen == ["git"] and calls and all(c[0] == "/x/git" for c in calls)
    monkeypatch.setattr(codeversion.shutil, "which", lambda name: None)
    assert current_code_version(tmp_path, run=lambda *a, **k: 1 / 0) == "unknown"


def test_code_version_dirty_for_an_untracked_file_by_name_and_content(tmp_path):
    (tmp_path / "new_algo.py").write_text("one\n")
    names = b"new_algo.py\0"
    first = version_of(tmp_path, others=names, top=str(tmp_path))
    assert first.startswith(VERSION + "-dirty:")
    (tmp_path / "new_algo.py").write_text("two\n")  # content counts
    second = version_of(tmp_path, others=names, top=str(tmp_path))
    assert second.startswith(VERSION + "-dirty:") and second != first
    (tmp_path / "other.py").write_text("two\n")  # and so does the name
    assert version_of(tmp_path, others=b"other.py\0", top=str(tmp_path)) != second


def test_the_full_hash_limit_is_64_megabytes():
    assert codeversion.MAX_HASHED_FILE == 64 * 1024 * 1024


def test_code_version_large_file_counts_by_name_and_size_only(tmp_path):
    big = tmp_path / "big.bin"
    names = b"big.bin\0"
    big.write_bytes(b"a" * 20)
    first = version_of(tmp_path, others=names, top=str(tmp_path), max_hashed=8)
    big.write_bytes(b"b" * 20)  # same size: not read
    assert version_of(tmp_path, others=names, top=str(tmp_path), max_hashed=8) == first
    big.write_bytes(b"b" * 21)  # new size
    assert version_of(tmp_path, others=names, top=str(tmp_path), max_hashed=8) != first


def test_code_version_deleted_tracked_file_is_dirty(tmp_path):
    gone = tracked("gone.py", b"x\n")
    assert version_of(tmp_path, tree=gone, top=str(tmp_path)).startswith(VERSION + "-dirty:")


def test_code_version_large_tracked_file_is_compared_by_size_only(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * 20)
    committed = tracked("big.bin", b"a" * 20)
    assert version_of(tmp_path, tree=committed, top=str(tmp_path), max_hashed=8) == VERSION
    big.write_bytes(b"b" * 20)  # same size, over the bound: not looked at
    assert version_of(tmp_path, tree=committed, top=str(tmp_path), max_hashed=8) == VERSION
    big.write_bytes(b"b" * 21)
    assert version_of(tmp_path, tree=committed, top=str(tmp_path), max_hashed=8).startswith(
        VERSION + "-dirty:")


@pytest.mark.parametrize("failure", [
    FileNotFoundError("git"), PermissionError("no"), OSError("broken"),
    subprocess.TimeoutExpired("git", 15), subprocess.SubprocessError("x"),
])
def test_code_version_unknown_when_git_is_unavailable(tmp_path, failure):
    def runner(args, **kwargs):
        raise failure

    assert current_code_version(tmp_path, run=runner, git=GIT) == "unknown"


@pytest.mark.parametrize("sha, code", [(b"", 128), (b"fatal", 128), (b"abc123", 0), (b"", 0),
                                       (b"zz" * 20, 0)])
def test_code_version_unknown_for_a_non_repository_or_odd_output(tmp_path, sha, code):
    assert current_code_version(
        tmp_path, run=lambda args, **kw: Done(sha, code), git=GIT) == "unknown"


@pytest.mark.parametrize("failing", ["--show-toplevel", "ls-tree", "--cached", "--others"])
def test_code_version_unknown_when_a_listing_fails(tmp_path, failing):
    def runner(args, **kwargs):
        if failing in args:
            return Done(b"", 1)
        return Done(VERSION.encode(), 0) if args[3] == "rev-parse" else Done(b"", 0)

    assert current_code_version(tmp_path, run=runner, git=GIT) == "unknown"


# ---- untracked files anywhere in the repository -------------------------------------


def test_listings_run_from_the_top_level_and_find_an_untracked_file_elsewhere(tmp_path):
    """The start folder is the package (MainStoinker/engine); a new algo lives next to it."""
    package = tmp_path / "MainStoinker" / "engine"
    algo = tmp_path / "MainStoinker" / "Algos" / "New" / "New_Algo.py"
    package.mkdir(parents=True)
    algo.parent.mkdir(parents=True)
    algo.write_text("class Algo: pass\n")
    calls: list = []
    version = version_of(package, others=b"MainStoinker/Algos/New/New_Algo.py\0",
                         top=str(tmp_path), calls=calls)
    assert version.startswith(VERSION + "-dirty:")
    by_command = {tuple(args[3:5]): kwargs["cwd"] for args, kwargs in calls}
    assert by_command[("rev-parse", "HEAD")] == str(package)
    for command in (("rev-parse", "--show-toplevel"),):
        assert by_command[command] == str(package)
    for command in (("ls-tree", "-r"), ("ls-files", "--cached"), ("ls-files", "--others")):
        assert by_command[command] == str(tmp_path)


# ---- the index ----------------------------------------------------------------------


def test_a_staged_new_file_is_dirty_even_though_it_is_not_in_head(tmp_path):
    (tmp_path / "staged.py").write_text("one\n")
    kept = tracked("kept.py", b"x\n")
    (tmp_path / "kept.py").write_bytes(b"x\n")
    index = names_in(kept) + b"staged.py\0"
    first = version_of(tmp_path, tree=kept, cached=index, top=str(tmp_path))
    assert first.startswith(VERSION + "-dirty:")
    (tmp_path / "staged.py").write_text("two\n")  # its content is part of the hash
    assert version_of(tmp_path, tree=kept, cached=index, top=str(tmp_path)) != first


def test_a_path_in_head_removed_from_the_index_is_dirty(tmp_path):
    (tmp_path / "gone.py").write_bytes(b"x\n")  # still in the working tree
    committed = tracked("gone.py", b"x\n")
    assert version_of(tmp_path, tree=committed, cached=b"", top=str(tmp_path)).startswith(
        VERSION + "-dirty:")
    # the index agreeing with HEAD is clean
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)) == VERSION


# ---- unexpected failures never raise ------------------------------------------------


def test_an_undecodable_path_counts_as_changed_not_a_traceback(tmp_path):
    version = version_of(tmp_path, others=b"caf\xe9\xff.py\0", top=str(tmp_path))
    assert version.startswith(VERSION + "-dirty:")


def test_an_unreadable_file_counts_as_changed(tmp_path, monkeypatch):
    (tmp_path / "x.py").write_bytes(b"changed\n")
    committed = tracked("x.py", b"original\n")

    def refuse(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(codeversion, "_blob_ids", refuse)
    monkeypatch.setattr(codeversion, "_feed", refuse)
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)).startswith(VERSION + "-dirty:")
    # a file that cannot be compared with HEAD is changed even if it is identical
    (tmp_path / "x.py").write_bytes(b"original\n")
    assert version_of(tmp_path, tree=committed, top=str(tmp_path)).startswith(VERSION + "-dirty:")


def test_any_unexpected_error_is_unknown(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("surprise")

    monkeypatch.setattr(codeversion, "_changed_paths", explode)
    assert version_of(tmp_path, top=str(tmp_path)) == "unknown"
    monkeypatch.undo()
    # a toplevel that cannot be decoded (raises on Windows, decodes on POSIX)
    runner = fake_git(VERSION.encode())

    def odd_top(args, **kwargs):
        if args[3:] == ["rev-parse", "--show-toplevel"]:
            return Done(b"\xff\xfe\n")
        return runner(args, **kwargs)

    assert current_code_version(tmp_path, run=odd_top, git=GIT) in ("unknown", VERSION)


# ---- the time budget ----------------------------------------------------------------


def test_hashing_has_a_time_budget_and_gives_dirty_unknown_when_it_runs_out(tmp_path):
    (tmp_path / "new.py").write_text("x\n")
    ticks = iter([0.0] + [1000.0] * 50)  # the deadline is set at 0 + 20; the next look is late
    version = version_of(tmp_path, others=b"new.py\0", top=str(tmp_path), budget=20.0,
                         clock=lambda: next(ticks))
    assert version == VERSION + "-dirty:unknown"
    # inside the budget the answer is a real hash
    assert version_of(tmp_path, others=b"new.py\0", top=str(tmp_path), budget=20.0,
                      clock=lambda: 0.0).count(":") == 1
    assert "unknown" not in version_of(tmp_path, others=b"new.py\0", top=str(tmp_path))
    assert codeversion.HASH_BUDGET == 20.0


def test_the_budget_also_covers_reading_a_big_file(tmp_path, monkeypatch):
    monkeypatch.setattr(codeversion, "_CHUNK", 4)
    (tmp_path / "big.txt").write_bytes(b"x" * 64)
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0  # every look at the clock costs a second
        return now[0]

    version = version_of(tmp_path, others=b"big.txt\0", top=str(tmp_path), budget=5.0, clock=clock)
    assert version == VERSION + "-dirty:unknown"


# ---- a real repository --------------------------------------------------------------


def real_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*words: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *words],
            cwd=repo, capture_output=True, text=True, check=True, timeout=60)
        return done.stdout.strip()

    git("init", "-q")
    return repo, git


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@needs_git
def test_code_version_with_a_real_repository(tmp_path):
    repo, git = real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git("add", "a.txt", ".gitignore")
    git("commit", "-q", "-m", "first")
    head = git("rev-parse", "HEAD")
    assert current_code_version(repo) == head
    (repo / "sub").mkdir()
    assert current_code_version(repo / "sub") == head  # any folder inside the repository
    (repo / "ignored.txt").write_text("ignored\n")  # ignored files do not count
    assert current_code_version(repo) == head
    (repo / "a.txt").write_text("two\n")
    dirty = current_code_version(repo)
    assert dirty.startswith(head + "-dirty:") and len(dirty) == len(head) + 7 + 12
    (repo / "a.txt").write_text("three\n")
    assert current_code_version(repo) not in (head, dirty)
    git("checkout", "--", "a.txt")
    assert current_code_version(repo) == head
    # a new, uncommitted file is not a clean commit; its content and name are part of the hash
    (repo / "new_algo.py").write_text("print(1)\n")
    untracked = current_code_version(repo)
    assert untracked.startswith(head + "-dirty:")
    (repo / "new_algo.py").write_text("print(2)\n")
    assert current_code_version(repo) not in (head, untracked)
    (repo / "new_algo.py").unlink()
    assert current_code_version(repo) == head
    (repo / "a.txt").unlink()  # a deleted tracked file
    assert current_code_version(repo).startswith(head + "-dirty:")


@needs_git
def test_an_untracked_file_outside_the_start_folder_counts(tmp_path):
    repo, git = real_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "engine.py").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    head = git("rev-parse", "HEAD")
    assert current_code_version(repo / "pkg") == head
    (repo / "Algos" / "New").mkdir(parents=True)
    (repo / "Algos" / "New" / "New_Algo.py").write_text("class Algo: pass\n")
    first = current_code_version(repo / "pkg")
    assert first.startswith(head + "-dirty:")
    assert current_code_version(repo) == first  # the same from any start folder
    (repo / "Algos" / "New" / "New_Algo.py").write_text("class Algo: x = 1\n")
    assert current_code_version(repo / "pkg") not in (head, first)


@needs_git
def test_a_staged_new_file_is_dirty_and_the_index_is_not_written(tmp_path):
    repo, git = real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    head = git("rev-parse", "HEAD")
    (repo / "staged.py").write_text("print(1)\n")
    git("add", "staged.py")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    first = current_code_version(repo)
    assert first.startswith(head + "-dirty:")
    (repo / "staged.py").write_text("print(2)\n")  # staged name, new content: another hash
    second = current_code_version(repo)
    assert second.startswith(head + "-dirty:") and second != first
    assert index.read_bytes() == before and not (repo / ".git" / "index.lock").exists()
    git("rm", "-q", "-f", "--cached", "staged.py")
    (repo / "staged.py").unlink()
    assert current_code_version(repo) == head  # no longer anywhere


@needs_git
def test_a_path_removed_from_the_index_is_dirty(tmp_path):
    repo, git = real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    head = git("rev-parse", "HEAD")
    git("rm", "-q", "--cached", "a.txt")  # still on disk, but the next commit would drop it
    index = repo / ".git" / "index"
    before = index.read_bytes()
    assert current_code_version(repo).startswith(head + "-dirty:")
    assert index.read_bytes() == before


def run_git_marked(repo: Path, tmp_path: Path):
    """A repository whose *.txt files go through a `clean` filter that leaves a marker file."""
    marker = tmp_path / "filter-ran"
    script = tmp_path / "clean.py"
    script.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\n"
                      "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}"'
    return marker, command


@needs_git
def test_code_version_leaves_the_index_alone_and_runs_no_filter(tmp_path):
    repo, git = real_repo(tmp_path)
    marker, command = run_git_marked(repo, tmp_path)
    (repo / ".gitattributes").write_text("*.txt filter=probe\n")
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    git("config", "filter.probe.clean", command)  # only now: committing needs no filter
    head = git("rev-parse", "HEAD")
    index = repo / ".git" / "index"

    # stat-dirty but unchanged: same bytes, new mtime (the case where `git diff` would run the
    # filter to compare content, and rewrite the refreshed index)
    stamp = (repo / "a.txt").stat().st_mtime_ns + 5_000_000_000
    os.utime(repo / "a.txt", ns=(stamp, stamp))
    before = index.read_bytes()
    assert current_code_version(repo) == head  # unchanged content is not dirty
    assert index.read_bytes() == before and not marker.exists()
    assert not (repo / ".git" / "index.lock").exists()

    # really modified, plus an untracked file
    (repo / "b.txt").write_text("changed\n")
    (repo / "c.txt").write_text("brand new\n")
    dirty = current_code_version(repo)
    assert dirty.startswith(head + "-dirty:")
    assert index.read_bytes() == before and not marker.exists()
    assert not (repo / ".git" / "index.lock").exists()

    # control: the filter really would have run (and the index rewritten) under `git diff HEAD`
    subprocess.run(["git", "diff", "HEAD"], cwd=repo, capture_output=True, check=True,
                   timeout=60)
    assert marker.exists(), "the probe filter never ran: this test would prove nothing"


@needs_git
def test_code_version_touched_crlf_checkout_is_still_clean(tmp_path):
    repo, git = real_repo(tmp_path)
    git("config", "core.autocrlf", "true")
    (repo / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    stamp = (repo / "a.txt").stat().st_mtime_ns + 5_000_000_000
    os.utime(repo / "a.txt", ns=(stamp, stamp))
    assert current_code_version(repo) == git("rev-parse", "HEAD")
    (repo / "a.txt").write_bytes(b"one\r\nthree\r\n")
    assert "-dirty:" in current_code_version(repo)


@needs_git
def test_a_commit_can_run_while_the_version_is_computed(tmp_path):
    """The version takes no index lock, so it cannot collide with a concurrent commit."""
    repo, git = real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    (repo / "a.txt").write_text("two\n")
    (repo / ".git" / "index.lock").write_bytes(b"")  # another git process holds the lock
    version = current_code_version(repo)
    assert version.startswith(git("rev-parse", "HEAD") + "-dirty:")
    assert (repo / ".git" / "index.lock").exists()  # and ours did not remove it


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_code_version_unknown_outside_a_repository(tmp_path):
    probe = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                           timeout=60)
    if probe.returncode == 0:
        pytest.skip("the temporary directory is inside a repository on this machine")
    assert current_code_version(tmp_path) == "unknown"


def test_importing_the_module_does_not_run_git():
    code = ("import subprocess\n"
            "def no(*a, **k):\n    raise SystemExit('git was run at import time')\n"
            "subprocess.run = no\nsubprocess.Popen = no\n"
            "import MainStoinker.engine.codeversion\n")
    repo = Path(__file__).resolve().parent.parent
    done = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
