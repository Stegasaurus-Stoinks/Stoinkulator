"""`python -m MainStoinker`: the command line (see `MainStoinker.engine.cli`).

`serve` (the read-only web API, see `MainStoinker.api.serve`) is dispatched
here with a lazy import, so every other command works without the web packages.
"""

import sys
from importlib.util import find_spec


def _serve(argv: list[str]) -> int:
    missing = [name for name in ("fastapi", "uvicorn") if find_spec(name) is None]
    if missing:
        print(f"error: serve needs the web packages ({', '.join(missing)} missing): "
              "install requirements-web.txt", file=sys.stderr)
        return 2
    try:
        from MainStoinker.api.serve import main as serve_main
    except ImportError as exc:  # a broken install (starlette, pydantic, ...): not a traceback
        print(f"error: serve could not load the web packages ({type(exc).__name__}): "
              "reinstall requirements-web.txt", file=sys.stderr)
        return 2
    return serve_main(argv)


def _fetch_bars(argv: list[str]) -> int:
    # Lazy, like `serve`: the four offline commands never import MainStoinker.ib (or ibapi).
    try:
        from MainStoinker.ib.cli import main as fetch_main
    except ImportError as exc:
        print(f"error: fetch-bars could not load the IB client ({type(exc).__name__}): "
              "install requirements.txt", file=sys.stderr)
        return 2
    return fetch_main(argv)


def _probe_data(argv: list[str]) -> int:
    # Lazy, like `fetch-bars`: the offline commands never import MainStoinker.ib (or ibapi).
    try:
        from MainStoinker.ib.cli import probe_data_main
    except ImportError as exc:
        print(f"error: probe-data could not load the IB client ({type(exc).__name__}): "
              "install requirements.txt", file=sys.stderr)
        return 2
    return probe_data_main(argv)


def _run_session(argv: list[str]) -> int:
    # Lazy, like `fetch-bars`: the offline commands never import MainStoinker.ib (or ibapi).
    try:
        from MainStoinker.ib.run_cli import main as run_main
    except ImportError as exc:
        print(f"error: run could not load the IB client ({type(exc).__name__}): "
              "install requirements.txt", file=sys.stderr)
        return 2
    return run_main(argv)


def _run(argv: list[str]) -> int:
    if argv[:1] == ["run"]:
        return _run_session(argv[1:])
    if argv[:1] == ["serve"]:
        return _serve(argv[1:])
    if argv[:1] == ["fetch-bars"]:
        return _fetch_bars(argv[1:])
    if argv[:1] == ["probe-data"]:
        return _probe_data(argv[1:])
    from MainStoinker.engine.cli import main

    return main(argv)


raise SystemExit(_run(sys.argv[1:]))
