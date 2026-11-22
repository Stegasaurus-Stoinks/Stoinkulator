"""`python -m MainStoinker serve --db SPEC [--host 127.0.0.1] [--port 8750] [--allow-remote]`.

Serves the read-only API and the dashboard for one run database. Exit codes:
0 the server ran and stopped; 2 a usage or input error (one line on stderr):
no `--db` (and no `STOINK_DB`), a host that is not this machine without
`--allow-remote`, or a database that cannot be opened read-only (missing, not a
run database, newer schema, inside a OneDrive folder). The database is checked
once here with the same read-only open every request uses; it is never created.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

import uvicorn

from MainStoinker.api.app import create_app
from MainStoinker.engine.store import StoreError, open_store_readonly

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8750
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one line, exit 2
        self.exit(2, f"error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python -m MainStoinker serve",
                description="Serve the read-only API and dashboard for a run database.")
    p.add_argument("--db", help="database file or sqlite:///path; defaults to STOINK_DB")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port to listen on (default {DEFAULT_PORT})")
    p.add_argument("--allow-remote", action="store_true",
                   help="allow a host other than this machine (there is no login)")
    return p


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the server; `env` (default `os.environ`) supplies `STOINK_DB` and the cloud check."""
    args = _parser().parse_args(argv)
    environ = os.environ if env is None else env
    if not 1 <= args.port <= 65535:
        return _fail(f"port must be between 1 and 65535, not {args.port}")
    if args.host.lower() not in LOCAL_HOSTS and not args.allow_remote:
        return _fail(f"refusing to listen on {args.host}: the API has no login, so it is only "
                     "served on this machine; pass --allow-remote to override")
    spec = (args.db or environ.get("STOINK_DB", "")).strip()
    if not spec:
        return _fail("no database: give --db or set STOINK_DB")
    try:
        open_store_readonly(spec, env=environ).close()
    except StoreError as exc:
        return _fail(str(exc))
    uvicorn.run(create_app(spec, environ), host=args.host, port=args.port)
    return 0
