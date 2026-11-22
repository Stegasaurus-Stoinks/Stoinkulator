"""Error responses: short, one line, and never carrying a path."""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from MainStoinker.engine.store import StoreError

UNAVAILABLE = "the run database is unavailable"


class NotFound(Exception):
    """An unknown run, algo or symbol. The message is returned as the one-line detail."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


async def _not_found(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": _one_line(str(exc)) or "not found"}, status_code=404)


async def _unavailable(_request: Request, _exc: Exception) -> JSONResponse:
    # The exception text carries database paths: it is never returned.
    return JSONResponse({"detail": UNAVAILABLE}, status_code=503)


async def _invalid(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    names = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
    return JSONResponse({"detail": "invalid parameter: " + ", ".join(names)}, status_code=422)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(StoreError, _unavailable)
    app.add_exception_handler(sqlite3.Error, _unavailable)
    app.add_exception_handler(RequestValidationError, _invalid)
