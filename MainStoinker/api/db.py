"""Per-request read-only database access."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request

from MainStoinker.engine.store import Store, open_store_readonly


async def get_store(request: Request) -> AsyncIterator[Store]:
    """Open the database read-only for one request and close it afterwards.

    An `async` dependency runs on the event loop thread, so a route that uses
    it must be `async def` too: a `SqliteStore` belongs to the thread that
    made it. A `StoreError` here becomes a 503 (see `errors`).
    """
    store = open_store_readonly(request.app.state.db_spec, env=request.app.state.env)
    try:
        yield store
    finally:
        store.close()
