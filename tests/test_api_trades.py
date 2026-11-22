"""`GET /api/runs/{id}/trades`, `/orders` and `/equity` against the stored realistic run."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.engine.stats import run_statistics
from MainStoinker.engine.store import RunData
from tests.api_support import make_client, stored_run_database
from tests.test_cli_store import FAST_CASES, GOLDEN_PAPER, stored


@pytest.fixture(scope="module")
def full_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stored_run_database(tmp_path_factory, "realistic")


@pytest.fixture(scope="module")
def run(full_db: Path) -> RunData:
    return stored(full_db)


def get(db: Path, path: str, **params: Any) -> Any:
    return make_client(db).get(path, params=params)


@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_trades_equal_golden_paper(full_db: Path, case: str) -> None:
    _algo, cfg, symbol = FAST_CASES[case]
    golden = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())
    response = get(full_db, "/api/runs/1/trades", algo=cfg["idname"])
    assert response.status_code == 200
    trades = response.json()["trades"]
    assert trades and all(t["account"] == cfg["idname"] and t["symbol"] == symbol for t in trades)
    plain = [{k: v for k, v in t.items() if k not in ("account", "symbol")} for t in trades]
    assert plain == golden["trades"]


def test_trades_filtered_by_algo(full_db: Path, run: RunData) -> None:
    everything = get(full_db, "/api/runs/1/trades").json()
    assert everything["algo"] is None and len(everything["trades"]) == len(run.round_trips)
    seen = 0
    for algo in run.algos:
        mine = get(full_db, "/api/runs/1/trades", algo=algo.idname).json()
        assert mine["algo"] == algo.idname
        assert {t["account"] for t in mine["trades"]} <= {algo.idname}
        seen += len(mine["trades"])
    assert seen == len(everything["trades"])


@pytest.mark.parametrize("path", ["trades", "orders", "equity"])
def test_unknown_algo_404(full_db: Path, path: str) -> None:
    response = get(full_db, f"/api/runs/1/{path}", algo="NOPE")
    assert response.status_code == 404
    assert response.json() == {"detail": "no algo NOPE in run 1"}
    assert str(full_db.parent) not in response.text


@pytest.mark.parametrize("path", ["trades", "orders", "equity"])
def test_unknown_run_404(full_db: Path, path: str) -> None:
    response = get(full_db, f"/api/runs/99/{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": "no run 99"}


def test_orders_and_fills_counts_match_store(full_db: Path, run: RunData) -> None:
    body = get(full_db, "/api/runs/1/orders").json()
    assert len(body["order_events"]) == len(run.order_events) > 0
    assert len(body["fills"]) == len(run.fills) > 0
    first = body["order_events"][0]
    assert isinstance(first["broker_ref"], str) and isinstance(first["time"], str)
    assert "T" not in first["time"] and "+" not in first["time"]
    fill = body["fills"][0]
    assert fill["price"] == f"{run.fills[0].price:.4f}" and fill["synthetic"] is False
    for algo in run.algos:
        mine = get(full_db, "/api/runs/1/orders", algo=algo.idname).json()
        assert len(mine["order_events"]) == sum(1 for e in run.order_events
                                                if e.account == algo.idname)
        assert len(mine["fills"]) == sum(1 for f in run.fills if f.account == algo.idname)


def test_equity_account_and_algo_series_equal_store(full_db: Path, run: RunData) -> None:
    for algo in [None, *(a.idname for a in run.algos)]:
        params = {} if algo is None else {"algo": algo}
        body = get(full_db, "/api/runs/1/equity", max_points=100000, **params).json()
        stored_points = run.equity_series(algo)
        assert body["step"] is True and body["algo"] == algo
        assert body["stored_points"] == len(stored_points)
        assert body["points"] == [
            {"time": str(p.time), "cash": f"{p.cash:.4f}",
             "position_value": f"{p.position_value:.4f}", "realised": f"{p.realised:.4f}",
             "unrealised": f"{p.unrealised:.4f}", "equity": f"{p.equity:.4f}"}
            for p in stored_points]


def test_equity_final_value_equals_report_final_equity(full_db: Path, run: RunData) -> None:
    stats = run_statistics(run)
    total = get(full_db, "/api/runs/1/equity", max_points=10).json()
    assert total["points"][-1]["equity"] == f"{stats['total']['final_equity']:.4f}"
    for algo in run.algos:
        body = get(full_db, "/api/runs/1/equity", algo=algo.idname, max_points=10).json()
        assert body["points"][-1]["equity"] == f"{stats['accounts'][algo.idname]['final_equity']:.4f}"


def test_equity_thinning_bounds_size_and_keeps_drawdown(full_db: Path, run: RunData) -> None:
    algo = run.algos[0].idname
    full = get(full_db, "/api/runs/1/equity", algo=algo, max_points=100000).json()
    assert len(full["points"]) > 40
    thinned = get(full_db, "/api/runs/1/equity", algo=algo, max_points=40).json()
    assert len(thinned["points"]) <= 40 and thinned["step"] is True
    assert thinned["points"][0] == full["points"][0] and thinned["points"][-1] == full["points"][-1]

    def drawdown(points: list[dict[str, str]]) -> Decimal:
        peak, worst = Decimal(full["capital"]), Decimal(0)
        for p in points:
            peak = max(peak, Decimal(p["equity"]))
            worst = max(worst, peak - Decimal(p["equity"]))
        return worst

    assert drawdown(thinned["points"]) == drawdown(full["points"])


@pytest.mark.parametrize("value", ["9", "100001", "0", "-5", "abc"])
def test_equity_max_points_bounds_422(full_db: Path, value: str) -> None:
    response = get(full_db, "/api/runs/1/equity", max_points=value)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.max_points"}


@pytest.mark.parametrize("value", ["10", "100000"])
def test_equity_max_points_bounds_accepted(full_db: Path, value: str) -> None:
    assert get(full_db, "/api/runs/1/equity", max_points=value).status_code == 200
