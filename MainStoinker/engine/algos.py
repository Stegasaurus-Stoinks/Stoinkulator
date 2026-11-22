"""Loading algos by id from the legacy `MainStoinker.Algos` packages.

The import is done with `importlib` on a computed module name so that the
engine package has no static dependency on legacy modules. Importing an algo
module still pulls legacy modules in transitively; that is accepted in Phase A.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

# Same format string as the legacy `main_utils.create_logger` (copied, not
# imported: the engine package has no static dependency on legacy modules).
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class _EngineFileHandler(logging.FileHandler):
    """Marker type: a file handler attached by this module, removable by it."""


def _attach_file_log(algo: object, log_path: Path, log_level: int) -> None:
    logger = getattr(algo, "logger", None)
    if not isinstance(logger, logging.Logger):
        return
    target = Path(log_path).resolve()
    for handler in logger.handlers:
        if isinstance(handler, _EngineFileHandler) and Path(handler.baseFilename) == target:
            break  # already attached for this path: no duplicates
    else:
        handler = _EngineFileHandler(log_path)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.setLevel(log_level)
        logger.addHandler(handler)
    logger.setLevel(log_level)


def close_algo_logs(algos: Iterable[object]) -> None:
    """Remove and close every file handler this module attached to the algos' loggers.

    Handlers attached by anyone else (e.g. the legacy `create_logger`) are left
    alone. Safe to call more than once. Algos without a `logger` are skipped.
    """
    for algo in algos:
        logger = getattr(algo, "logger", None)
        if not isinstance(logger, logging.Logger):
            continue
        for handler in list(logger.handlers):
            if isinstance(handler, _EngineFileHandler):
                logger.removeHandler(handler)
                handler.close()


def load_algo(
    algo_id: str,
    cfg: Mapping,
    *,
    log_path: Path | None = None,
    log_level: int = logging.INFO,
) -> object:
    """Construct `MainStoinker.Algos.<algo_id>.<algo_id>_Algo.Algo(cfg)`.

    `algo_id` must be a Python identifier (checked before anything is
    imported). With `log_path=None` (default) no handler is added and no file
    is created: the algo's logger is plain `logging.getLogger(name)`. With a
    `log_path`, exactly one file handler for that path (legacy format) is
    attached to the algo's logger and its level is set to `log_level`
    (default INFO; the engine does not read the legacy config). Pair with
    `close_algo_logs` when the run ends. A missing module or a module without an `Algo` class raises
    `ValueError` naming the id, with the original exception chained.
    """
    if not isinstance(algo_id, str) or not algo_id.isidentifier() or algo_id.startswith("_"):
        raise ValueError(
            f"invalid algo id {algo_id!r}: must be a Python identifier not starting with '_'"
        )
    module_name = f"MainStoinker.Algos.{algo_id}.{algo_id}_Algo"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # The algo itself is missing only if the module that could not be found
        # is this module or one of its parent packages (whole dotted components).
        missing = exc.name
        is_own_path = missing is not None and (
            module_name == missing or module_name.startswith(missing + ".")
        )
        if missing is not None and not is_own_path:
            raise  # the algo exists but one of its own dependencies is missing
        raise ValueError(f"unknown algo {algo_id!r}: cannot import {module_name}") from exc
    try:
        algo_class = module.Algo
    except AttributeError as exc:
        raise ValueError(f"algo {algo_id!r}: {module_name} has no class 'Algo'") from exc
    algo = algo_class(cfg)
    if log_path is not None:
        _attach_file_log(algo, log_path, log_level)
    return algo


def load_algos(
    algo_config: Sequence[Mapping],
    *,
    log_path: Path | None = None,
    log_level: int = logging.INFO,
) -> list:
    """Build algos from the parsed `Algo_config.json` shape.

    `[{"ID": <algo_id>, "data": [<cfg>, ...]}, ...]`; returns the algos in
    file order (entries in order, each entry's `data` in order). Reads no files.
    `log_path` / `log_level` are passed to `load_algo` for every algo.
    """
    algos = []
    for entry in algo_config:
        algo_id = entry["ID"]
        for cfg in entry["data"]:
            algos.append(load_algo(algo_id, cfg, log_path=log_path, log_level=log_level))
    return algos
