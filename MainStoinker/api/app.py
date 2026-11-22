"""The FastAPI application: read-only, GET only, no CORS, local use."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.resources import files

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from MainStoinker.api import API_VERSION
from MainStoinker.api.errors import install_error_handlers
from MainStoinker.api.routes_bars import router as bars_router
from MainStoinker.api.routes_compare import router as compare_router
from MainStoinker.api.routes_health import router as health_router
from MainStoinker.api.routes_runs import router as runs_router
from MainStoinker.api.routes_series import router as series_router
from MainStoinker.api.routes_trades import router as trades_router

LOCAL_HOST_HEADERS = ("127.0.0.1", "localhost", "[::1]")  # any port; others are 400 (DNS rebinding)


class _StaticFiles(StaticFiles):
    """Static files where a name the OS rejects (too long, illegal characters) is a plain 404."""

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        try:
            return super().lookup_path(path)
        except (OSError, ValueError):
            return "", None


def create_app(db_spec: str, env: Mapping[str, str], *, allow_remote: bool = False) -> FastAPI:
    """Build the app for the database `db_spec`; `env` is what the OneDrive refusal judges by.

    Nothing is opened here: each request opens the database read-only itself.
    No CORS middleware is installed, and the interactive docs are off. Unless
    `allow_remote`, a request whose Host header is not this machine (any port)
    is refused with a bare 400, so a web page cannot reach the API through a
    domain rebound to 127.0.0.1.
    """
    middleware = [] if allow_remote else [
        Middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOST_HEADERS),
                   www_redirect=False)]
    app = FastAPI(title="Stoinkulator", version=API_VERSION, middleware=middleware,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_spec = db_spec
    app.state.env = env
    install_error_handlers(app)
    for router in (health_router, runs_router, trades_router, bars_router,
                   series_router, compare_router):
        app.include_router(router)
    # Last, so it never shadows an API route.
    app.mount("/", _StaticFiles(directory=str(files("MainStoinker.api") / "static"), html=True),
              name="static")
    return app
