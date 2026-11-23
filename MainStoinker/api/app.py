"""The FastAPI application: read-only, GET only, no CORS, local use."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.resources import files

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.types import Scope

from MainStoinker.api import API_VERSION
from MainStoinker.api.errors import install_error_handlers
from MainStoinker.api.routes_bars import router as bars_router
from MainStoinker.api.routes_compare import router as compare_router
from MainStoinker.api.routes_health import router as health_router
from MainStoinker.api.routes_runs import router as runs_router
from MainStoinker.api.routes_series import router as series_router
from MainStoinker.api.routes_trades import router as trades_router

PathLike = str | os.PathLike[str]
LOCAL_HOST_HEADERS = ("127.0.0.1", "localhost", "[::1]")  # any port; others are 400 (DNS rebinding)


# Starlette takes the type from `mimetypes`, which on Windows reads the registry: a machine that maps
# `.js` to text/plain makes browsers refuse the ES modules. These are pinned, whatever the registry says.
PINNED_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}


class _StaticFiles(StaticFiles):
    """Static files where a name the OS rejects (too long, illegal characters) is a plain 404.

    Responses carry pinned content types for the dashboard's file kinds and `nosniff`.
    """

    def file_response(self, full_path: PathLike, stat_result: os.stat_result, scope: Scope,
                      status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        pinned = PINNED_CONTENT_TYPES.get(os.path.splitext(str(full_path))[1].lower())
        if pinned is not None and "content-type" in response.headers:
            response.headers["content-type"] = pinned
        response.headers["x-content-type-options"] = "nosniff"
        # Revalidate on every use (cheap: the ETag / Last-Modified answer is a 304), so an update is
        # never masked by a heuristically cached older module.
        response.headers["cache-control"] = "no-cache"
        return response

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
