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
    from MainStoinker.api.serve import main as serve_main

    return serve_main(argv)


def _run(argv: list[str]) -> int:
    if argv[:1] == ["serve"]:
        return _serve(argv[1:])
    from MainStoinker.engine.cli import main

    return main(argv)


raise SystemExit(_run(sys.argv[1:]))
