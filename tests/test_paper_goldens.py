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

Regenerate with `pytest tests/test_paper_goldens.py --update-golden`; to
regenerate all eight goldens add `-m "slow or not slow"` (the slow cases are
FunctionTest and the three Elliot runs, which a bare run skips). Explain the
diff in the PR.
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


def max_drawdown(equities, capital: Decimal) -> Decimal:
    """Peak to trough on the account equity series, starting capital counts as
    the first peak. Zero or positive."""
    peak = capital
    worst = Decimal("0.0000")
    for value in equities:
        if value > peak:
            peak = value
        worst = max(worst, peak - value)
    return worst


def paper_golden(run: harness.PaperRun, algo_id: str) -> dict:
    ledger, algo = run.ledger, run.algo
    idname = algo.name
    cent4 = Decimal("0.0001")
    trips = [t for t in ledger.round_trips if t.account == idname]
    points = [p for p in ledger.equity_points if p.account == idname]
    ignored = {kind: ledger.ignored_count(kind, idname) for kind in IGNORED_KINDS}
    return {
        "algo": algo_id,
        "idname": idname,
        "ticker": algo.ticker,
        "capital": _fmt(run.capital.quantize(Decimal("0.0001"))),
        "costs": {
            "commission_per_share": _fmt(run.broker.costs.commission_per_share.quantize(cent4)),
            "commission_minimum": _fmt(run.broker.costs.commission_minimum.quantize(cent4)),
            "slippage_per_share": _fmt(run.broker.costs.slippage_per_share.quantize(cent4)),
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
            "max_drawdown": _fmt(max_drawdown((p.equity for p in points), run.capital)),
            "ignored_events": ignored,
            "cancelled_entries": run.broker.cancelled_entries,
            "synthetic_closes": sum(1 for t in trips if t.synthetic),
        },
        "equity_points": len(points),
        "equity_sha256": hashlib.sha256(equity_series_bytes(points)).hexdigest(),
    }


@pytest.fixture(scope="module")
def paper_run(tmp_path_factory):
    """A function giving the (cached) paper run of a case: tests share one run."""
    runs: dict[str, harness.PaperRun] = {}

    def get(case_id: str, algo_id: str, cfg: dict) -> harness.PaperRun:
        if case_id not in runs:
            workdir = tmp_path_factory.mktemp(f"paper_{case_id}")
            harness.stage_offline_data(workdir, [cfg["ticker"]])
            with contextlib.chdir(workdir):
                try:
                    runs[case_id] = harness.run_paper_backtest(algo_id, cfg)
                finally:
                    harness.reset_runtime_state()
        return runs[case_id]

    return get


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_paper_trades_match_golden(algo_id, cfg, paper_run, golden_paper, request):
    run = paper_run(request.node.callspec.id, algo_id, cfg)
    golden_paper(request.node.callspec.id, paper_golden(run, algo_id))


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_algo_view_matches_broker_every_bar(algo_id, cfg, paper_run, request):
    """During the run (not after `ledger.finish`), `algo.inTrade` is True exactly
    when the broker's state for the algo's current trade is pending or open, and
    the algo never has more than one live (pending or open) position. The check
    runs once per post-warm-up bar, i.e. once per equity point of the account."""
    run = paper_run(request.node.callspec.id, algo_id, cfg)
    points = [p for p in run.ledger.equity_points if p.account == run.algo.name]
    assert run.bars_checked == len(points)
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


def _walk(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)
    else:
        yield node


@pytest.mark.parametrize("algo_id, cfg", CASES)
def test_golden_file_is_self_consistent(algo_id, cfg, request):
    """The committed golden agrees with itself: no floats, money as 4-decimal
    strings, and the summary equals what the trade list adds up to."""
    import json
    import re

    from tests.conftest import GOLDEN_PAPER_DIR

    golden = json.loads((GOLDEN_PAPER_DIR / f"{request.node.callspec.id}.json").read_text(encoding="utf-8"))
    assert not [v for v in _walk(golden) if isinstance(v, float)]
    four = re.compile(r"^-?\d+\.\d{4}$")
    trades, summary = golden["trades"], golden["summary"]
    for trade in trades:
        for key in ("quantity", "entry_price", "exit_price", "gross", "commission", "net"):
            assert four.match(trade[key]), (key, trade[key])
    for key in ("commission", "net", "final_equity", "max_drawdown"):
        assert four.match(summary[key]), (key, summary[key])
    assert summary["trade_count"] == len(trades)
    assert summary["wins"] == sum(1 for t in trades if Decimal(t["net"]) > 0)
    assert Decimal(summary["net"]) == sum((Decimal(t["net"]) for t in trades), Decimal(0))
    assert Decimal(summary["commission"]) == sum((Decimal(t["commission"]) for t in trades), Decimal(0))
    assert Decimal(summary["final_equity"]) == Decimal(golden["capital"]) + Decimal(summary["net"])
    assert summary["synthetic_closes"] == sum(1 for t in trades if t["synthetic"])
    for trade in trades:
        gross = (Decimal(trade["exit_price"]) - Decimal(trade["entry_price"])) * Decimal(trade["quantity"])
        if trade["side"] == "short":
            gross = -gross
        assert Decimal(trade["gross"]) == gross
        assert Decimal(trade["net"]) == gross - Decimal(trade["commission"])


def test_paper_backtest_twice_in_one_process_is_identical(paper_run, tmp_path):
    """No state leaks between runs: a second, fresh run of a case gives the same
    golden as the first (the first is the module's cached run of that case)."""
    algo_id, cfg = next(p.values for p in CASES if p.id == "fridayfrenzy_tsla")
    first = paper_golden(paper_run("fridayfrenzy_tsla", algo_id, cfg), algo_id)
    harness.stage_offline_data(tmp_path, [cfg["ticker"]])
    with contextlib.chdir(tmp_path):
        try:
            second = paper_golden(harness.run_paper_backtest(algo_id, cfg), algo_id)
        finally:
            harness.reset_runtime_state()
    assert first == second
    assert first["summary"]["trade_count"] > 0


def test_view_problem_rules():
    """The per-bar checker's two rules, on hand-made states."""
    # flat view and flat broker: fine; exited trades do not count as live
    assert harness.view_problem(False, "closed", []) == (None, 0)
    assert harness.view_problem(False, None, []) == (None, 0)
    # in a trade, pending or open: fine
    assert harness.view_problem(True, "open", ["open"]) == (None, 1)
    assert harness.view_problem(True, "pending", ["pending"]) == (None, 1)
    # an exiting position next to a new pending one is legitimate
    assert harness.view_problem(True, "pending", ["exiting", "pending"]) == (None, 1)
    # view and broker disagree, either way round
    assert harness.view_problem(True, "closed", [])[0] is not None
    assert harness.view_problem(False, "open", ["open"])[0] is not None
    assert harness.view_problem(True, None, [])[0] is not None
    # two live positions
    problem, live = harness.view_problem(True, "open", ["open", "pending"])
    assert problem is not None
    assert live == 2
