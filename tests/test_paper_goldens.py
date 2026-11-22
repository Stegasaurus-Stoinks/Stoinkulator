"""Goldens for the realistic broker (`PaperBroker`), and per-bar consistency checks.

The same eight cases as `tests/test_golden_backtests.py`, run through
`harness.run_paper_backtest` (Engine + PaperBroker + Ledger, default costs:
commission 0.005/share with a 1.00 minimum, no slippage, capital 10,000).
`tests/golden_paper/<case>.json` pins what the ledger reports. The legacy
goldens in `tests/golden/` are untouched; they pin the optimistic SimBroker
fills, these pin the realistic ones, so trade counts differ on purpose.

Each golden holds:

- `trades`: the ledger's round trips in exit order. Money, prices and
  quantities are 4-decimal strings (never floats), times are `str(Timestamp)`.
- `summary`: trade count, wins (net > 0), commission, net, final equity, max
  drawdown, ignored events by kind (all kinds, zeros included), cancelled
  entries, synthetic end-of-data closes.
- `equity_sha256`: sha256 of the per-bar account equity series, serialised by
  `equity_series_bytes` below.

Regenerate with `pytest --update-golden tests/test_paper_goldens.py` (add
`-m slow` for the slow cases) and explain the diff in the PR. The slow cases
are FunctionTest and the three Elliot runs.
"""
from __future__ import annotations

import contextlib
import hashlib
from decimal import Decimal

import pytest

from MainStoinker.engine.ledger import IGNORED_KINDS
from MainStoinker.engine.report import ledger_trade_records
from tests import harness
from tests.test_golden_backtests import CASES as LEGACY_CASES

SLOW_IDS = {"functiontest_msft", "elliot_msft", "elliot_aapl", "elliot_tsla"}

CASES = [
    pytest.param(
        *p.values,
        id=p.id,
        marks=[pytest.mark.slow] if p.id in SLOW_IDS else [],
    )
    for p in LEGACY_CASES
]

_RUNS: dict[str, harness.PaperRun] = {}


def _fmt(value: Decimal) -> str:
    """A Decimal as a plain fixed-point string (the ledger already quantizes to 4 places)."""
    return format(value, "f")


def equity_series_bytes(points) -> bytes:
    """Canonical serialisation of an account's equity series, for the golden's sha256.

    One line per point, in series order: `str(time)`, a comma, the equity as a
    4-decimal fixed-point string, then "\\n". UTF-8, no header, no trailing
    blank line.
    """
    return "".join(f"{p.time},{_fmt(p.equity)}\n" for p in points).encode("utf-8")


def max_drawdown(equities) -> Decimal:
    """Largest peak-to-trough fall in a series of equities (zero or positive)."""
    peak = None
    worst = Decimal("0.0000")
    for value in equities:
        if peak is None or value > peak:
            peak = value
        worst = max(worst, peak - value)
    return worst


def paper_golden(run: harness.PaperRun, algo_id: str) -> dict:
    ledger, algo = run.ledger, run.algo
    idname = algo.name
    trips = [t for t in ledger.round_trips if t.account == idname]
    points = [p for p in ledger.equity_points if p.account == idname]
    ignored = {kind: ledger.ignored_count(kind, idname) for kind in IGNORED_KINDS}
    return {
        "algo": algo_id,
        "idname": idname,
        "ticker": algo.ticker,
        "capital": _fmt(run.capital.quantize(Decimal("0.0001"))),
        "costs": {
            "commission_per_share": _fmt(run.broker.costs.commission_per_share),
            "commission_minimum": _fmt(run.broker.costs.commission_minimum),
            "slippage_per_share": _fmt(run.broker.costs.slippage_per_share),
        },
        "trades": [
            {
                "side": t.side,
                "quantity": _fmt(t.quantity),
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "entry_price": _fmt(t.entry_price),
                "exit_price": _fmt(t.exit_price),
                "gross": _fmt(t.gross),
                "commission": _fmt(t.commission),
                "net": _fmt(t.net),
                "exit_reason": t.exit_reason,
                "synthetic": t.synthetic,
            }
            for t in trips
        ],
        "summary": {
            "trade_count": len(trips),
            "wins": sum(1 for t in trips if t.net > 0),
            "commission": _fmt(sum((t.commission for t in trips), Decimal("0.0000"))),
            "net": _fmt(sum((t.net for t in trips), Decimal("0.0000"))),
            "final_equity": _fmt(ledger.equity(idname)),
            "max_drawdown": _fmt(max_drawdown(p.equity for p in points)),
            "ignored_events": ignored,
            "cancelled_entries": run.broker.cancelled_entries,
            "synthetic_closes": sum(1 for t in trips if t.synthetic),
        },
        "equity_points": len(points),
        "equity_sha256": hashlib.sha256(equity_series_bytes(points)).hexdigest(),
    }


@pytest.fixture(scope="module")
def paper_run(tmp_path_factory):
    """A function giving the (cached) paper run of a case: both tests share one run."""

    def get(case_id: str, algo_id: str, cfg: dict) -> harness.PaperRun:
        if case_id not in _RUNS:
            workdir = tmp_path_factory.mktemp(f"paper_{case_id}")
            harness.stage_offline_data(workdir, [cfg["ticker"]])
            with contextlib.chdir(workdir):
                try:
                    _RUNS[case_id] = harness.run_paper_backtest(algo_id, cfg)
                finally:
                    harness.reset_runtime_state()
        return _RUNS[case_id]

    return get


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_paper_trades_match_golden(algo_id, cfg, paper_run, golden_paper, request):
    run = paper_run(request.node.callspec.id, algo_id, cfg)
    golden_paper(request.node.callspec.id, paper_golden(run, algo_id))


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_algo_view_matches_broker_every_bar(algo_id, cfg, paper_run, request):
    """During the run (not after `ledger.finish`), `algo.inTrade` is True exactly
    when the broker's state for the algo's current trade is pending or open, and
    the algo never has more than one live (pending, open or exiting) position."""
    run = paper_run(request.node.callspec.id, algo_id, cfg)
    assert run.bars_checked > 0
    assert run.violations == []
    assert run.max_live <= 1


def test_ledger_is_sole_report_source(paper_run):
    """With realistic fills the legacy `Trade` objects go stale: the broker
    closes positions without telling them, so `trade_records` cannot be used
    for reporting. The ledger is the only source."""
    algo_id, cfg = next(p.values for p in CASES if p.id == "fvg_aapl")
    run = paper_run("fvg_aapl", algo_id, cfg)
    legacy = harness.trade_records(run.algo)
    from_ledger = ledger_trade_records(run.ledger)

    stale = [r for r in legacy if r["closePrice"] is None]
    assert stale, "expected Trade objects without close details in realistic mode"
    closed_in_ledger = [r for r in from_ledger if r["closePrice"] is not None]
    assert len(closed_in_ledger) > len(legacy) - len(stale)
    assert legacy != [{k: v for k, v in r.items() if k != "status"} for r in from_ledger]
    assert len(run.ledger.round_trips) > 0
