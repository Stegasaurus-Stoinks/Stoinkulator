"""Shared builders for tests that replay the offline bars through the CLI.

`Env` is the staged inputs and outputs of one CLI run. `shared_env` / `full_store_run` build the
expensive full-data runs once per test SESSION, not once per pytest-xdist worker: the first worker
that needs one builds it under a lock file, the others wait for it and then only read it. Nothing
that is shared may be modified; a test that needs to change a stored run copies the database first
(`copy_database`).

Everything lives under pytest's temporary folders. With `-n 0` (or without xdist) the "shared"
folder is simply the one session folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from MainStoinker.engine.cli import main
from tests import harness

EMA = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}
FVG = {"idname": "AAPL-fvg", "ticker": "AAPL", "RRRatio": 3}
ORC = {"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3}
FF = {"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3}
ROWS = 600
SMALL_ROWS = 700  # a stored run that is only read (the API and report tests), not compared with a golden

# the four fast golden cases: golden file name -> (algo id, config, symbol)
FAST_CASES = {
    "emacrossing_aapl": ("EMACrossing", EMA, "AAPL"),
    "openingrangecandle_aapl": ("OpeningRangeCandle", ORC, "AAPL"),
    "fridayfrenzy_tsla": ("FridayFrenzy", FF, "TSLA"),
    "fvg_aapl": ("FVG", FVG, "AAPL"),
}


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> None:
        self.inputs = root / "inputs"
        self.start = root / "start"
        self.out = root / "out"
        self.inputs.mkdir(parents=True)
        self.start.mkdir()
        self.algos = self.inputs / "algos.json"
        self.data = self.inputs / "OfflineData"
        if monkeypatch is not None:
            monkeypatch.chdir(self.start)

    @classmethod
    def existing(cls, root: Path) -> Env:
        """The `Env` of a folder that `Env(root)` and a run already filled (nothing is created)."""
        env = object.__new__(cls)
        env.inputs = root / "inputs"
        env.start = root / "start"
        env.out = root / "out"
        env.algos = env.inputs / "algos.json"
        env.data = env.inputs / "OfflineData"
        return env

    def setup(self, algos: object, symbols: list[str], rows: int | None = ROWS) -> None:
        harness.stage_offline_data(self.inputs, symbols, max_rows=rows)
        self.algos.write_text(json.dumps(algos), encoding="utf-8")

    def run(self, *extra: str) -> int:
        return main(["backtest", "--algos", str(self.algos), "--data", str(self.data),
                     "--out", str(self.out), "--quiet", *extra])

    def trades_bytes(self) -> bytes:
        return (self.out / "trades.json").read_bytes()

    def trades(self) -> dict:
        return json.loads(self.trades_bytes())

    def ledger(self) -> dict:
        return json.loads((self.out / "ledger.json").read_text(encoding="utf-8"))


# ---- builds shared by every worker of one session ---------------------------------------


_built: dict[str, Path] = {}
_BUILD_TIMEOUT = 900.0  # seconds a worker waits for another worker's build, or for its lock


def _shared_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.getbasetemp()
    # under xdist `base` is this worker's folder `<session>/popen-gwN`; the session folder is common
    root = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    shared = root / "shared-builds"
    shared.mkdir(exist_ok=True)
    return shared


def build_once(
    tmp_path_factory: pytest.TempPathFactory, name: str, build: Callable[[Path], None]
) -> Path:
    """The folder `build` filled, built once per session by whichever process asks first."""
    if name in _built:
        return _built[name]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:40] + "-" + hashlib.sha1(name.encode()).hexdigest()[:8]
    shared = _shared_root(tmp_path_factory)
    target, done, lock = shared / safe, shared / f"{safe}.done", shared / f"{safe}.lock"
    began = time.monotonic()
    while not done.exists():
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:  # another process is building it
            if time.monotonic() - began > _BUILD_TIMEOUT:
                raise TimeoutError(f"waited {_BUILD_TIMEOUT:.0f} s for the shared build {name!r}") from None
            time.sleep(0.2)
            continue
        try:
            if not done.exists():
                shutil.rmtree(target, ignore_errors=True)  # a failed earlier attempt
                build(target)
                done.write_text("ok")
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)
    _built[name] = target
    return target


def shared_env(
    tmp_path_factory: pytest.TempPathFactory, name: str, build: Callable[[Env], None]
) -> Env:
    """An `Env` whose run `build(env)` made once for the whole session (read it, never change it)."""
    return Env.existing(build_once(tmp_path_factory, name, lambda root: build(Env(root))))


def full_run(
    tmp_path_factory: pytest.TempPathFactory, key: str, algos: list[dict], symbols: list[str],
    *extra: str, rows: int | None = None,
) -> Env:
    """One CLI run of `algos` (full data unless `rows`), shared by every test of the session.

    `key` names the run: use a different key for a different config, options or `rows`.
    """

    def build(env: Env) -> None:
        env.setup(algos, symbols, rows=rows)
        assert env.run(*extra) == 0

    return shared_env(tmp_path_factory, "run-" + key, build)


def _stored_four_algo_run(
    tmp_path_factory: pytest.TempPathFactory, fills: str, rows: int | None
) -> tuple[Env, Path]:
    def build(env: Env) -> None:
        env.setup([{"ID": a, "data": [c]} for a, c, _ in FAST_CASES.values()],
                  ["AAPL", "TSLA"], rows=rows)
        assert main(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                     "--out", str(env.out), "--quiet", "--fills", fills,
                     "--db", str(env.inputs.parent / "runs.db")]) == 0

    env = shared_env(tmp_path_factory, f"store-{rows}-{fills}", build)
    return env, env.inputs.parent / "runs.db"


def full_store_run(tmp_path_factory: pytest.TempPathFactory, fills: str) -> tuple[Env, Path]:
    """All fast golden cases in one config, full data, one database (shared per fill model).

    For tests that compare with the golden files. Tests that only need "a stored run" use
    `small_store_run`, which costs a fraction of this.
    """
    return _stored_four_algo_run(tmp_path_factory, fills, None)


def small_store_run(tmp_path_factory: pytest.TempPathFactory, fills: str) -> tuple[Env, Path]:
    """The same four algos and config on the first `SMALL_ROWS` bars of each symbol."""
    return _stored_four_algo_run(tmp_path_factory, fills, SMALL_ROWS)
