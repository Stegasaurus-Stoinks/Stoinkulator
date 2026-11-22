"""Verification additions for the trades, orders and equity routes and `thinning.thin`."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.api.thinning import thin
from MainStoinker.engine.stats import max_drawdown, run_statistics
from MainStoinker.engine.store import RunData
from tests.api_support import make_client, stored_run_database
from tests.test_cli_store import stored

PATHS = ["trades", "orders", "equity"]


@dataclass(frozen=True)
class Pt:
    index: int
    equity: Decimal


def series(kind: str, count: int, seed: int) -> list[Pt]:
    rng = random.Random(seed)
    value = Decimal(100)
    points = []
    for i in range(count):
        if kind == "constant":
            pass
        elif kind == "up":
            value += 1
        elif kind == "down":
            value -= 1
        else:
            value += Decimal(rng.randint(-300, 290)) / 100
        points.append(Pt(i, value))
    return points


@pytest.mark.parametrize("kind", ["constant", "up", "down", "walk"])
@pytest.mark.parametrize("max_points", [10, 11, 12, 13, 25, 100])
def test_thin_properties_over_many_series(kind: str, max_points: int) -> None:
    for seed in range(15):
        for count in (0, 1, 2, max_points - 1, max_points, max_points + 1, max_points + 2,
                      3 * max_points, 1000):
            points = series(kind, count, seed)
            before = list(points)
            for capital in (Decimal(50), Decimal(100), Decimal(150)):
                out = thin(points, max_points, capital)
                assert points == before
                assert len(out) <= max_points
                if count:
                    assert out[0] is points[0] and out[-1] is points[-1]
                indices = [p.index for p in out]
                assert indices == sorted(set(indices))
                assert max_drawdown([p.equity for p in out], capital) == max_drawdown(
                    [p.equity for p in points], capital)


def test_thin_without_capital_uses_the_first_point() -> None:
    points = series("walk", 500, 1)
    out = thin(points, 12)
    first = points[0].equity
    assert max_drawdown([p.equity for p in out], first) == max_drawdown(
        [p.equity for p in points], first)


@pytest.fixture(scope="module")
def realistic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stored_run_database(tmp_path_factory, "realistic")


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stored_run_database(tmp_path_factory, "legacy")


def get(db: Path, path: str, **params: Any) -> Any:
    return make_client(db).get(path, params=params)


def drawdown(points: list[dict[str, str]], capital: str) -> Decimal:
    return max_drawdown([Decimal(p["equity"]) for p in points], Decimal(capital))


@pytest.mark.parametrize("max_points", [10, 11, 12, 37])
def test_thinned_equity_through_the_route_keeps_the_report_drawdown(
    realistic_db: Path, max_points: int
) -> None:
    run: RunData = stored(realistic_db)
    stats = run_statistics(run)
    for algo in [None, *(a.idname for a in run.algos)]:
        params = {} if algo is None else {"algo": algo}
        body = get(realistic_db, "/api/runs/1/equity", max_points=max_points, **params).json()
        points = body["points"]
        assert len(points) <= max_points < body["stored_points"]
        keys = [(p["time"], p["equity"], p["cash"]) for p in points]
        assert len(set(keys)) == len(keys)
        assert [p["time"] for p in points] == sorted(p["time"] for p in points)
        report = stats["total"] if algo is None else stats["accounts"][algo]
        assert f"{drawdown(points, body['capital']):.4f}" == f"{report['max_drawdown']:.4f}"
        assert points[-1]["equity"] == f"{report['final_equity']:.4f}"


def test_legacy_fill_run_routes_equal_the_store(legacy_db: Path) -> None:
    run: RunData = stored(legacy_db)
    for algo in run.algos:
        idname = algo.idname
        trades = get(legacy_db, "/api/runs/1/trades", algo=idname).json()["trades"]
        assert len(trades) == sum(1 for t in run.round_trips if t.account == idname)
        orders = get(legacy_db, "/api/runs/1/orders", algo=idname).json()
        assert len(orders["fills"]) == sum(1 for f in run.fills if f.account == idname)
        assert all(f["commission"] == "0.0000" for f in orders["fills"])
        body = get(legacy_db, "/api/runs/1/equity", algo=idname, max_points=100000).json()
        assert len(body["points"]) == len(run.equity_series(idname))
    account = get(legacy_db, "/api/runs/1/equity", max_points=100000).json()
    assert len(account["points"]) == len(run.equity_series(None))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("run_id", ["0", "-1", "99999999999999999999999"])
def test_unknown_run_ids_are_404(realistic_db: Path, path: str, run_id: str) -> None:
    response = get(realistic_db, f"/api/runs/{run_id}/{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": f"no run {run_id}"}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("run_id", ["abc", "1.5"])
def test_non_integer_run_id_is_422(realistic_db: Path, path: str, run_id: str) -> None:
    response = get(realistic_db, f"/api/runs/{run_id}/{path}")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: path.run_id"}


@pytest.mark.parametrize("path", PATHS)
def test_only_get_is_allowed(realistic_db: Path, path: str) -> None:
    client = make_client(realistic_db)
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)(f"/api/runs/1/{path}").status_code == 405


@pytest.mark.parametrize("path", PATHS)
def test_responses_carry_no_paths_and_leave_the_database_unchanged(
    realistic_db: Path, path: str
) -> None:
    before = realistic_db.read_bytes()
    text = get(realistic_db, f"/api/runs/1/{path}").text
    unknown = get(realistic_db, f"/api/runs/1/{path}", algo="no/such algo").text
    for body in (text, unknown):
        assert str(realistic_db.parent) not in body
        assert str(Path.home()) not in body
        assert "\\\\" not in body
    assert realistic_db.read_bytes() == before


@pytest.mark.parametrize("path", PATHS)
def test_database_removed_after_start_is_the_fixed_503(tmp_path: Path, path: str) -> None:
    client = make_client(tmp_path / "missing.db")
    response = client.get(f"/api/runs/1/{path}")
    assert response.status_code == 503
    assert response.json() == {"detail": "the run database is unavailable"}
    assert str(tmp_path) not in response.text


def _opt4(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.4f}"


@pytest.mark.parametrize("fills", ["realistic", "legacy"])
def test_order_events_and_fills_equal_the_store_field_by_field(
    realistic_db: Path, legacy_db: Path, fills: str
) -> None:
    db = realistic_db if fills == "realistic" else legacy_db
    run: RunData = stored(db)
    body = get(db, "/api/runs/1/orders").json()
    assert body["order_events"] == [
        {"order_id": e.order_id, "account": e.account, "broker_ref": str(e.broker_ref),
         "kind": e.kind, "time": str(e.time), "side": e.side, "quantity": _opt4(e.quantity),
         "order_type": e.order_type, "price": _opt4(e.price)} for e in run.order_events]
    assert body["fills"] == [
        {"fill_id": f.fill_id, "order_id": f.order_id, "account": f.account,
         "broker_ref": str(f.broker_ref), "symbol": f.symbol, "side": f.side,
         "quantity": f"{f.quantity:.4f}", "price": f"{f.price:.4f}",
         "commission": f"{f.commission:.4f}", "time": str(f.time), "reason": f.reason,
         "synthetic": bool(f.synthetic)} for f in run.fills]


def test_repeated_requests_give_identical_bodies(realistic_db: Path) -> None:
    client = make_client(realistic_db)
    for path in PATHS:
        first = client.get(f"/api/runs/1/{path}").content
        assert client.get(f"/api/runs/1/{path}").content == first
