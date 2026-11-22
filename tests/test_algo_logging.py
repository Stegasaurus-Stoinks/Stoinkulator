"""Algo logger ownership: legacy path unchanged, engine path opens no file in cwd."""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

from MainStoinker.engine.algos import close_algo_logs, load_algo, load_algos
from MainStoinker.MainStuff import main_utils as utils
from MainStoinker.MainStuff.Globals import config
from tests import harness
from tests.test_algos_load import CASES

FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _cfg(algo_id: str, extra: dict, idname: str | None = None) -> dict:
    return {"idname": idname or f"test-{algo_id}", "ticker": "AAPL", **extra}


def _describe(logger: logging.Logger) -> dict:
    handlers = []
    for h in logger.handlers:
        entry = {
            "type": type(h),
            "level": h.level,
            "format": h.formatter._fmt if h.formatter else None,
        }
        if isinstance(h, logging.FileHandler):
            entry["path"] = Path(h.baseFilename)
        handlers.append(entry)
    return {
        "level": logger.level,
        "propagate": logger.propagate,
        "handlers": handlers,
    }


def _algo_class(algo_id: str):
    return importlib.import_module(f"MainStoinker.Algos.{algo_id}.{algo_id}_Algo").Algo


# ---- legacy path ---------------------------------------------------------


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_algo_starter_logger_identical_to_before(algo_id, extra):
    cfg = _cfg(algo_id, extra)
    algo = utils.algo_starter(algo_id, cfg)
    logger = algo.logger
    assert logger is logging.getLogger(f"algo:{cfg['idname']}:AAPL")
    assert logger.name == f"algo:test-{algo_id}:AAPL"

    got = _describe(logger)
    expected_path = Path.cwd() / "loggy.log"
    assert got["level"] == config.log_level
    assert got["propagate"] is True
    assert got["handlers"] == [
        {"type": logging.FileHandler, "level": config.log_level, "format": FORMAT,
         "path": expected_path.resolve()},
        {"type": logging.StreamHandler, "level": config.log_level, "format": FORMAT},
    ]

    # Direct reference: what create_logger itself produces for a fresh name.
    ref = _describe(utils.create_logger("algo:ref:AAPL"))
    assert got == ref


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_algo_starter_creates_loggy_log_in_cwd(algo_id, extra):
    assert not (Path.cwd() / "loggy.log").exists()
    utils.algo_starter(algo_id, _cfg(algo_id, extra))
    assert (Path.cwd() / "loggy.log").exists()


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_no_algo_logs_during_construction(algo_id, extra):
    """Handlers are attached after __init__ on the legacy path; this makes that unobservable."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    capture = Capture(level=logging.NOTSET)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.NOTSET)
    # Make every level reach the root handler regardless of config.log_level.
    name = f"algo:test-{algo_id}:AAPL"
    logger = logging.getLogger(name)
    logger.setLevel(logging.NOTSET)
    try:
        _algo_class(algo_id)(_cfg(algo_id, extra))
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
    assert [r for r in records if r.name.startswith("algo:")] == []


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_constructing_algo_class_directly_adds_no_handlers(algo_id, extra):
    algo = _algo_class(algo_id)(_cfg(algo_id, extra))
    assert isinstance(algo.logger, logging.Logger)
    assert algo.logger.handlers == []
    assert not (Path.cwd() / "loggy.log").exists()


# ---- engine path ---------------------------------------------------------


@pytest.mark.parametrize("algo_id, extra", CASES)
def test_engine_loader_creates_no_loggy_log_in_cwd(algo_id, extra):
    before = set(Path.cwd().iterdir())
    algo = load_algo(algo_id, _cfg(algo_id, extra))
    assert set(Path.cwd().iterdir()) == before
    assert not (Path.cwd() / "loggy.log").exists()
    assert algo.logger.handlers == []


def test_engine_loader_with_log_path_writes_there(tmp_path):
    log = tmp_path / "logs" / "run.log"
    log.parent.mkdir()
    cwd_before = set(Path.cwd().iterdir())
    algo = load_algo("FunctionTest", _cfg("FunctionTest", {"RRRatio": 3}), log_path=log)
    try:
        assert len(algo.logger.handlers) == 1
        (handler,) = algo.logger.handlers
        assert type(handler).__name__ == "_EngineFileHandler"
        assert handler.formatter._fmt == FORMAT
        algo.logger.info("hello engine")
        handler.flush()
        line = log.read_text().strip()
        assert line.endswith(f"- {algo.logger.name} - INFO - hello engine")
        assert set(Path.cwd().iterdir()) == cwd_before
    finally:
        close_algo_logs([algo])


def test_same_logger_name_twice_does_not_duplicate_file_handlers(tmp_path):
    log = tmp_path / "run.log"
    cfg = _cfg("FunctionTest", {"RRRatio": 3})
    a = load_algo("FunctionTest", cfg, log_path=log)
    b = load_algo("FunctionTest", cfg, log_path=log)
    algos = load_algos([{"ID": "FunctionTest", "data": [cfg]}], log_path=log)
    try:
        assert a.logger is b.logger
        assert len(a.logger.handlers) == 1
        assert len(algos[0].logger.handlers) == 1
    finally:
        close_algo_logs([a, b, *algos])
    assert a.logger.handlers == []


def test_close_algo_logs_removes_and_closes_only_loader_handlers(tmp_path):
    log = tmp_path / "run.log"
    algo = load_algo("FunctionTest", _cfg("FunctionTest", {"RRRatio": 3}), log_path=log)
    other = logging.NullHandler()
    algo.logger.addHandler(other)
    try:
        assert len(algo.logger.handlers) == 2
        close_algo_logs([algo])
        assert algo.logger.handlers == [other]
        close_algo_logs([algo])  # harmless twice
        assert algo.logger.handlers == [other]
        log.unlink()  # fails on Windows if the handler still holds the file
        assert not log.exists()
    finally:
        algo.logger.removeHandler(other)
        close_algo_logs([algo])


def test_close_algo_logs_leaves_legacy_handlers_alone():
    algo = utils.algo_starter("FunctionTest", _cfg("FunctionTest", {"RRRatio": 3}))
    before = list(algo.logger.handlers)
    close_algo_logs([algo])
    assert algo.logger.handlers == before


def test_log_level_argument_is_applied(tmp_path):
    cfg = _cfg("FunctionTest", {"RRRatio": 3})
    default = load_algo("FunctionTest", cfg, log_path=tmp_path / "a.log")
    try:
        assert default.logger.level == logging.INFO
    finally:
        close_algo_logs([default])
    debug = load_algo(
        "FunctionTest", cfg, log_path=tmp_path / "b.log", log_level=logging.DEBUG
    )
    try:
        assert debug.logger.level == logging.DEBUG
        debug.logger.debug("dbg")
        debug.logger.handlers[0].flush()
        assert "DEBUG - dbg" in (tmp_path / "b.log").read_text()
    finally:
        close_algo_logs([debug])


def test_full_backtest_through_harness_leaves_no_loggy_log():
    cfg = {"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3}
    harness.stage_offline_data(Path.cwd(), ["AAPL"])
    harness.run_offline_backtest("OpeningRangeCandle", [cfg])
    assert not (Path.cwd() / "loggy.log").exists()
