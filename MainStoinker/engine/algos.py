"""Loading algos by id from the legacy `MainStoinker.Algos` packages.

The import is done with `importlib` on a computed module name so that the
engine package has no static dependency on legacy modules. Importing an algo
module still pulls legacy modules in transitively; that is accepted in Phase A.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence


def load_algo(algo_id: str, cfg: Mapping) -> object:
    """Construct `MainStoinker.Algos.<algo_id>.<algo_id>_Algo.Algo(cfg)`.

    `algo_id` must be a Python identifier (checked before anything is
    imported). A missing module or a module without an `Algo` class raises
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
    return algo_class(cfg)


def load_algos(algo_config: Sequence[Mapping]) -> list:
    """Build algos from the parsed `Algo_config.json` shape.

    `[{"ID": <algo_id>, "data": [<cfg>, ...]}, ...]`; returns the algos in
    file order (entries in order, each entry's `data` in order). Reads no files.
    """
    algos = []
    for entry in algo_config:
        algo_id = entry["ID"]
        for cfg in entry["data"]:
            algos.append(load_algo(algo_id, cfg))
    return algos
