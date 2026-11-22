"""`GET /api/runs/{id}/trades`, `/orders` and `/equity` against the stored realistic run."""

from __future__ import annotations

import json
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
    plain = [{k: v for k, v in t.items() if k not in ("account", "symbol", "broker_ref")}
             for t in trades]
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


@pytest.mark.parametrize("value", ["9", "100001", "0", "-5", "abc"])
def test_equity_max_points_bounds_422(full_db: Path, value: str) -> None:
    response = get(full_db, "/api/runs/1/equity", max_points=value)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid parameter: query.max_points"}


@pytest.mark.parametrize("value", ["10", "100000"])
def test_equity_max_points_bounds_accepted(full_db: Path, value: str) -> None:
    assert get(full_db, "/api/runs/1/equity", max_points=value).status_code == 200


def test_trades_carry_broker_ref_matching_orders_and_fills(full_db: Path, run: RunData) -> None:
    trades = get(full_db, "/api/runs/1/trades").json()["trades"]
    assert [t["broker_ref"] for t in trades] == [json.dumps(t.broker_ref) for t in run.round_trips]
    body = get(full_db, "/api/runs/1/orders").json()
    refs = {(e["account"], e["broker_ref"]) for e in body["order_events"]}
    refs |= {(f["account"], f["broker_ref"]) for f in body["fills"]}
    assert all((t["account"], t["broker_ref"]) in refs for t in trades)


def test_modified_and_cancelled_events_carry_side_and_order_type(
    full_db: Path, run: RunData
) -> None:
    """The ledger stores null for these on a modify or cancel; the route fills them in."""
    stored_placed = {(e.account, e.order_id): e for e in run.order_events if e.kind == "placed"}
    events = get(full_db, "/api/runs/1/orders").json()["order_events"]
    later = [e for e in events if e["kind"] in ("modified", "cancelled")]
    assert {e["kind"] for e in later} == {"modified", "cancelled"}
    assert any(e.side is None and e.order_type is None
               for e in run.order_events if e.kind in ("modified", "cancelled"))
    for e in later:
        origin = stored_placed[(e["account"], e["order_id"])]
        assert e["side"] == origin.side is not None
        assert e["order_type"] == origin.order_type is not None
    for e in events:
        if e["kind"] == "placed":
            assert e["side"] is not None and e["order_type"] is not None


@pytest.mark.parametrize("path", ["trades", "orders", "equity"])
def test_empty_algo_is_absent(full_db: Path, path: str) -> None:
    plain = get(full_db, f"/api/runs/1/{path}")
    empty = get(full_db, f"/api/runs/1/{path}", algo="")
    assert empty.status_code == 200
    assert empty.json() == plain.json() and empty.json()["algo"] is None


@pytest.mark.parametrize("path", ["trades", "orders", "equity"])
def test_unknown_algo_echo_is_capped(full_db: Path, path: str) -> None:
    response = get(full_db, f"/api/runs/1/{path}", algo="x" * 5000)
    assert response.status_code == 404
    assert response.json() == {"detail": f"no algo {'x' * 64}... in run 1"}
    assert len(response.text) < 200
    exact = "y" * 64
    assert get(full_db, f"/api/runs/1/{path}", algo=exact).json() == {
        "detail": f"no algo {exact} in run 1"}
