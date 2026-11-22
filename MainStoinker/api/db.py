"""Read-only database access for the API: one helper, `with_store`.

Every data route must go through `with_store`. It opens the database
read-only, calls `fn(store)` and closes the store, all inside ONE worker
thread (`anyio.to_thread.run_sync`): a SQLite connection only works on the
thread that opened it, and a query on the event loop thread would stall every
other request (static files included) behind a slow or locked database.

`fn` must return plain data (dicts, lists, numbers, strings, dataclasses of
those) built inside the call. Nothing lazy that touches the connection later
(an iterator, a cursor, a generator over the store) may escape: the store is
closed by the time the route sees the result.

A `StoreError` or `sqlite3.Error` raised by the open or by `fn` becomes the
fixed 503 (see `errors`); the API waits at most `API_BUSY_TIMEOUT` seconds for
a locked database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

import anyio.to_thread
from fastapi import Request

from MainStoinker.engine.store import Store, open_store_readonly

T = TypeVar("T")

API_BUSY_TIMEOUT = 2.0  # seconds a request waits for a locked database before the 503


def _open_run_close(spec: str, env: Mapping[str, str], fn: Callable[[Store], T]) -> T:
    store = open_store_readonly(spec, env=env, busy_timeout=API_BUSY_TIMEOUT)
    try:
        return fn(store)
    finally:
        store.close()


async def with_store(request: Request, fn: Callable[[Store], T]) -> T:
    """Run `fn(store)` on a worker thread against a fresh read-only store; see the module docstring."""
    state = request.app.state
    return await anyio.to_thread.run_sync(_open_run_close, state.db_spec, state.env, fn)
