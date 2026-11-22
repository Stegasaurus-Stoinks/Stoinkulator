"""`python -m MainStoinker serve --db SPEC [--host 127.0.0.1] [--port 8750] [--allow-remote]`.

Serves the read-only API and the dashboard for one run database. Exit codes:
0 the server ran and stopped; 2 a usage or input error (one line on stderr):
no `--db` (and no `STOINK_DB`), a bad port, a host that is not this machine
without `--allow-remote`, a port that is already in use (checked by binding and
releasing it before the server starts), or a database that cannot be opened
read-only (missing, not a run database, newer schema, inside a OneDrive
folder). The database is checked once here with the same read-only open every
request uses; it is never created. Any other exit status comes from uvicorn
itself (for example a bind that fails after the check, in the race window).
Unless `--allow-remote`, requests whose Host header is not this machine are
refused with 400 (see `app.create_app`). One worker process is always used.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from collections.abc import Mapping, Sequence

import uvicorn

from MainStoinker.api.app import create_app
from MainStoinker.api.db import API_BUSY_TIMEOUT
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


def _port_free(host: str, port: int) -> bool:
    """True if `host:port` can be bound right now (the socket is released again at once)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError:
        return False
    family, kind, proto, _name, address = infos[0]
    probe = socket.socket(family, kind, proto)
    try:
        if sys.platform != "win32":  # as uvicorn does; on Windows it would allow stealing a port
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(address)
    except OSError:
        return False
    finally:
        probe.close()
    return True


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
        open_store_readonly(spec, env=environ, busy_timeout=API_BUSY_TIMEOUT).close()
    except StoreError as exc:
        return _fail(str(exc))
    if not _port_free(args.host, args.port):
        return _fail(f"port {args.port} on {args.host} is already in use or cannot be bound")
    app = create_app(spec, environ, allow_remote=args.allow_remote)
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0
