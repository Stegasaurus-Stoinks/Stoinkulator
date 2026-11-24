"""`tests/home_guard.py`, tried on a FAKE home under `tmp_path` (never the real profile)."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from tests.home_guard import RealHomeWrite, no_writes_to_real_home


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "fake-home"
    (folder / "Stoinkulator" / "recordings").mkdir(parents=True)
    (folder / "Stoinkulator" / "recordings" / "older.jsonl").write_text("x")
    return folder


def test_every_kind_of_write_into_the_guarded_folder_is_refused_and_recorded(home: Path) -> None:
    target = home / "Stoinkulator" / "recordings"
    outside = home / "elsewhere"
    outside.mkdir()
    with pytest.raises(AssertionError, match="writes into the real home folder"):  # at the end
        with no_writes_to_real_home(home) as attempts:
            for attempt in (
                lambda: (target / "a.jsonl").write_text("y"),
                lambda: open(target / "b.jsonl", "ab"),  # noqa: SIM115
                lambda: (target / "sub").mkdir(),
                lambda: os.makedirs(target / "deep" / "er"),
                lambda: (target / "older.jsonl").replace(target / "moved.jsonl"),
                lambda: os.rename(target / "older.jsonl", target / "renamed.jsonl"),
                lambda: (target / "older.jsonl").unlink(),
                lambda: os.replace(outside / "none", target / "in.jsonl"),
                lambda: shutil.copyfile(__file__, target / "copy.py"),
            ):
                with pytest.raises(RealHomeWrite, match="REAL HOME FOLDER"):
                    attempt()
            assert len(attempts) == 9
    assert sorted(p.name for p in target.iterdir()) == ["older.jsonl"]  # nothing happened


def test_a_swallowed_refusal_still_fails_the_test_at_the_end(home: Path) -> None:
    with pytest.raises(AssertionError, match="attempted"), no_writes_to_real_home(home):
        try:
            (home / "Stoinkulator" / "x.txt").write_text("y")
        except OSError:  # the code under test catches only OSError; the guard's error is not one
            pass
        except AssertionError:
            pass  # swallowed by a broad handler


def test_reads_and_writes_elsewhere_are_fine(home: Path, tmp_path: Path) -> None:
    with no_writes_to_real_home(home) as attempts:
        assert (home / "Stoinkulator" / "recordings" / "older.jsonl").read_text() == "x"
        assert list((home / "Stoinkulator").rglob("*"))
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "f.txt").write_text("ok")
        (home / "Stoinkulator-other").mkdir()  # a sibling whose name only starts the same
    assert attempts == []


def test_what_else_does_to_the_guarded_folder_does_not_matter(home: Path) -> None:
    """Another process (a real session of the owner) writing the folder meanwhile: simulated by a
    thread that bypasses the wrappers through the unwrapped calls. The guard has no opinion."""
    real_open = open
    stop = threading.Event()

    def other_process() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            with real_open(home / "Stoinkulator" / "recordings" / f"r{n}.jsonl", "w") as handle:
                handle.write("x")
            time.sleep(0.002)

    with no_writes_to_real_home(home) as attempts:
        # the thread is started before the wrappers matter for it: it calls the saved original
        thread = threading.Thread(target=other_process)
        thread.start()
        try:
            time.sleep(0.05)
        finally:
            stop.set()
            thread.join(10)
    assert attempts == []


def test_the_wrappers_are_removed_afterwards(home: Path) -> None:
    before = (open, os.mkdir, os.replace)
    with no_writes_to_real_home(home):
        assert open is not before[0]
    assert (open, os.mkdir, os.replace) == before
    (home / "Stoinkulator" / "recordings" / "fine-now.jsonl").write_text("y")


def test_no_home_means_nothing_is_guarded() -> None:
    with no_writes_to_real_home(None) as attempts:
        pass
    assert attempts == []
