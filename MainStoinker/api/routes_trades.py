"""`GET /api/runs/{id}/trades`, `/orders` and `/equity`.

Money, prices and quantities are 4-decimal strings; market times are returned
exactly as stored (naive, no zone). Each route reads one run inside
`with_store`. An unknown run or algo is a 404.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.errors import NotFound
from MainStoinker.api.thinning import MAX_POINTS, MIN_POINTS, thin
from MainStoinker.engine.ledger import EquityPoint, Fill, OrderEvent
from MainStoinker.engine.stats import fmt4, round_trip_record
from MainStoinker.engine.store import RunData, Store

router = APIRouter()

DEFAULT_MAX_POINTS = 5000


def _load(store: Store, run_id: int, algo: str | None) -> RunData:
    if run_id not in {r.run_id for r in store.list_runs()}:
        raise NotFound(f"no run {run_id}")
    run = store.load_run(run_id)
    if algo is not None and algo not in {a.idname for a in run.algos}:
        raise NotFound(f"no algo {algo} in run {run_id}")
    return run


def _opt4(value: Decimal | None) -> str | None:
    return None if value is None else fmt4(value)


def _event(e: OrderEvent) -> dict[str, Any]:
    return {"order_id": e.order_id, "account": e.account, "broker_ref": str(e.broker_ref),
            "kind": e.kind, "time": str(e.time), "side": e.side,
            "quantity": _opt4(e.quantity), "order_type": e.order_type, "price": _opt4(e.price)}


def _fill(f: Fill) -> dict[str, Any]:
    return {"fill_id": f.fill_id, "order_id": f.order_id, "account": f.account,
            "broker_ref": str(f.broker_ref), "symbol": f.symbol, "side": f.side,
            "quantity": fmt4(f.quantity), "price": fmt4(f.price),
            "commission": fmt4(f.commission), "time": str(f.time), "reason": f.reason,
            "synthetic": bool(f.synthetic)}


def _point(p: EquityPoint) -> dict[str, Any]:
    return {"time": str(p.time), "cash": fmt4(p.cash), "position_value": fmt4(p.position_value),
            "realised": fmt4(p.realised), "unrealised": fmt4(p.unrealised),
            "equity": fmt4(p.equity)}


def _trades(store: Store, run_id: int, algo: str | None) -> dict[str, Any]:
    run = _load(store, run_id, algo)
    trades = [{**round_trip_record(t), "account": t.account, "symbol": t.symbol}
              for t in run.round_trips if algo is None or t.account == algo]
    return {"run_id": run_id, "algo": algo, "trades": trades}


def _orders(store: Store, run_id: int, algo: str | None) -> dict[str, Any]:
    run = _load(store, run_id, algo)
    return {
        "run_id": run_id, "algo": algo,
        "order_events": [_event(e) for e in run.order_events if algo is None or e.account == algo],
        "fills": [_fill(f) for f in run.fills if algo is None or f.account == algo],
    }


def _equity(store: Store, run_id: int, algo: str | None, max_points: int) -> dict[str, Any]:
    run = _load(store, run_id, algo)
    series = run.equity_series(algo)
    capital = sum((a.capital for a in run.algos if algo is None or a.idname == algo),
                  Decimal("0.0000"))
    points = thin(series, max_points, capital)
    return {"run_id": run_id, "algo": algo, "step": True, "capital": fmt4(capital),
            "stored_points": len(series), "points": [_point(p) for p in points]}


@router.get("/api/runs/{run_id}/trades")
async def trades(request: Request, run_id: int, algo: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _trades(store, run_id, algo))


@router.get("/api/runs/{run_id}/orders")
async def orders(request: Request, run_id: int, algo: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _orders(store, run_id, algo))


@router.get("/api/runs/{run_id}/equity")
async def equity(
    request: Request, run_id: int, algo: str | None = None,
    max_points: int = Query(DEFAULT_MAX_POINTS, ge=MIN_POINTS, le=MAX_POINTS),
) -> dict[str, Any]:
    return await with_store(request, lambda store: _equity(store, run_id, algo, max_points))
