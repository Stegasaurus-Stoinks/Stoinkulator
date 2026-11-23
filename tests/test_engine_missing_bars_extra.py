"""Further probes of decision 6 (a symbol with no bar at a minute is skipped for that minute).

`tests/test_engine_missing_bars.py` pins the rule on synthetic bars. These tests add what it
leaves out: a gapped symbol sees exactly the frames it would see run alone and the other symbols
are unaffected; real algos on gapped copies of the committed bars keep the per-bar agreement
between each algo and its paper broker; the equity series across a gap; duplicate-timestamp
rules; a symbol whose data starts days later; month files with gaps; repeated runs in one
process; the summary line.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

import MainStoinker.engine.cli as cli
from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.engine import Engine, EngineConfig
from MainStoinker.engine.feed import CsvBarFeed, read_offline_bars
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.router import MappingRouter
from MainStoinker.engine.sink import NullSink
from tests import harness
from tests.test_cli_modes import EMA, EMA_MSFT, Env
from tests.test_csv_feed_monthly import shifted_aapl, write_legacy, write_months
from tests.test_engine_missing_bars import (
    ALL,
    CAPITAL,
    Scripted,
    SpyAlgo,
    make_feed,
    minute,
    paper_engine,
    run_engine,
    without,
    write_symbol,
)

# --------------------------------------------------------- a gapped symbol equals itself alone


def test_gapped_symbol_sees_the_frames_it_would_see_alone(tmp_path: Path) -> None:
    gapped_ks = without(0, 3, 4, 9)
    for folder in ("joint", "alone", "whole"):
        (tmp_path / folder).mkdir()
    joint = make_feed(tmp_path / "joint", AAA=ALL, BBB=gapped_ks)
    a_joint, b_joint = SpyAlgo("a", "AAA"), SpyAlgo("b", "BBB")
    run_engine(joint, [a_joint, b_joint])
    # BBB alone, on the very same file
    alone_dir = tmp_path / "alone"
    write_symbol(alone_dir, "BBB", gapped_ks, 200.0)
    b_alone = SpyAlgo("b", "BBB")
    run_engine(CsvBarFeed(alone_dir, ["BBB"], warmup_days=1), [b_alone])
    assert len(b_joint.frames) == len(b_alone.frames) == len(gapped_ks)
    for got, want in zip(b_joint.frames, b_alone.frames, strict=True):
        assert_frame_equal(got, want)
    # and AAA, whose data is complete, is told nothing of BBB's gap
    whole = make_feed(tmp_path / "whole", AAA=ALL, BBB=ALL)
    a_whole = SpyAlgo("a", "AAA")
    run_engine(whole, [a_whole, SpyAlgo("b", "BBB")])
    assert len(a_joint.frames) == len(a_whole.frames) == 10
    for got, want in zip(a_joint.frames, a_whole.frames, strict=True):
        assert_frame_equal(got, want)


def test_paper_fills_of_other_symbols_do_not_depend_on_a_gap(tmp_path: Path) -> None:
    def fills(missing: Sequence[int], folder: str) -> dict[str, list[tuple]]:
        (tmp_path / folder).mkdir()
        feed = make_feed(tmp_path / folder, AAA=without(*missing), BBB=ALL)
        assert feed.missing_counts()["AAA"] == len(missing)
        ledger = Ledger()
        paper_engine(feed, [Scripted("a", "AAA", open_at=1, close_at=5),
                            Scripted("b", "BBB", open_at=2, close_at=6)], ledger)
        result: dict[str, list[tuple]] = {"a": [], "b": []}
        for f in ledger.fills:
            result[f.account].append((f.time, f.side, f.price, f.reason))
        return result

    whole, gapped = fills([], "whole"), fills([0, 3, 9], "gapped")
    assert gapped["b"] == whole["b"]  # BBB, whose bars are all there, trades exactly as before
    assert gapped["a"] != whole["a"]  # AAA's own fills do move: its bars are not the same


# ------------------------------------------------ equity and ledger across a gap


def test_equity_has_one_point_per_timestamp_and_never_zero_across_gaps(tmp_path: Path) -> None:
    feed = make_feed(tmp_path, AAA=without(3, 9), BBB=without(0, 8))
    ledger = Ledger()
    brokers = paper_engine(feed, [Scripted("a", "AAA", open_at=1), Scripted("b", "BBB", open_at=1)],
                           ledger)
    assert set(brokers) == {"a", "b"}
    for account in ("a", "b", None):
        points = [p for p in ledger.equity_points if p.account == account]
        assert [p.time for p in points] == [minute(k) for k in ALL]  # one per timestamp, in order
        assert all(p.equity > 0 for p in points)
    # the final point is the ledger's own number: cash plus what the open positions are worth
    for account in ("a", "b"):
        last = [p for p in ledger.equity_points if p.account == account][-1]
        assert last.equity == ledger.equity(account) == ledger.cash(account)
    # AAA's last bar is minute 8, but its synthetic close is stamped at the run's last minute
    # (the spec's wording: finish runs at the last timestamp); the price is AAA's own last close.
    (aaa,) = [t for t in ledger.round_trips if t.account == "a"]
    assert (aaa.exit_reason, aaa.exit_time, aaa.exit_price) == (
        "end_of_data", minute(9), Decimal("108.5"))


def test_a_gap_minute_leaves_every_open_account_at_its_last_close(tmp_path: Path) -> None:
    # the gap is at AAA's minute 4 and BBB's minute 5: each account is flat through its own gap
    feed = make_feed(tmp_path, AAA=without(4), BBB=without(5))
    ledger = Ledger()
    paper_engine(feed, [Scripted("a", "AAA", open_at=1), Scripted("b", "BBB", open_at=1)], ledger)

    def equity(account: str, k: int) -> Decimal:
        (point,) = [p for p in ledger.equity_points if p.account == account and p.time == minute(k)]
        return point.equity

    assert equity("a", 4) == equity("a", 3) and equity("a", 5) != equity("a", 4)
    assert equity("b", 5) == equity("b", 4) and equity("b", 6) != equity("b", 5)
    total = [p for p in ledger.equity_points if p.account is None]
    assert len(total) == 10 and all(p.equity > 0 for p in total)


# ----------------------------------------------------------- duplicates


@pytest.mark.parametrize(
    ("a_ks", "b_ks"),
    [
        ([0, 1, 1, 2], [0, 1, 2]),  # only A repeats, B has the minute once
        ([0, 1, 2], [0, 1, 1, 2]),  # only B repeats
        ([0, 1, 1, 2, 3], [0, 1, 2, 2, 3]),  # each repeats a different minute
        ([0, 1, 1, 1, 2], [0, 1, 1, 2]),  # both repeat the same minute, a different number of times
    ],
)
def test_a_repeat_that_is_not_in_every_symbol_raises(
    tmp_path: Path, a_ks: list[int], b_ks: list[int]
) -> None:
    write_symbol(tmp_path, "AAA", a_ks, 100.0)
    write_symbol(tmp_path, "BBB", b_ks, 200.0)
    with pytest.raises(ValueError, match="more than one bar"):
        CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)


def test_a_repeated_minute_in_every_symbol_reaches_the_algos_twice(tmp_path: Path) -> None:
    """The kept exception: both bars of the repeated minute are replayed, in file order."""
    write_symbol(tmp_path, "AAA", [0, 1, 1, 2], 100.0)
    write_symbol(tmp_path, "BBB", [0, 1, 1, 2], 200.0)
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    a = SpyAlgo("a", "AAA")
    run_engine(feed, [a, SpyAlgo("b", "BBB")])
    assert [f["date"].iloc[-1] for f in a.frames] == [minute(k) for k in (0, 1, 1, 2)]
    assert feed.missing_counts() == {"AAA": 0, "BBB": 0}


# ----------------------------------------------------- start dates and warm-up


def test_a_symbol_whose_data_starts_days_later_keeps_its_own_full_warmup(tmp_path: Path) -> None:
    day = pd.Timedelta(days=1)
    write_symbol(tmp_path, "AAA", ALL, 100.0)
    # BBB's file starts a day after AAA's and one warm-up bar later than AAA's own warm-up:
    # its warm-up is measured from its own first row, so it replays only from there on.
    stamps = [minute(k) + day * 2 for k in range(3)] + [minute(k) + day * 4 for k in range(4)]
    rows = ["x,date,time,open,high,low,close,volume"]
    rows += [f"{i},{s},{int(s.timestamp())},50,51,49,50.5,{i + 1}" for i, s in enumerate(stamps)]
    (tmp_path / "_BBB_offlinedata_").write_text("\n".join(rows) + "\n")
    feed = CsvBarFeed(tmp_path, ["AAA", "BBB"], warmup_days=1)
    assert len(feed.warmup("BBB")) == 3  # the first three rows, not fewer
    assert feed.warmup("BBB")["date"].iloc[0] == minute(0) + day * 2
    batches = list(feed)
    first_bbb = next(stamp for stamp, bars in batches if "BBB" in bars)
    assert first_bbb == minute(0) + day * 4
    assert feed.missing_counts() == {"AAA": 4, "BBB": 10}
    b = SpyAlgo("b", "BBB")
    run_engine(feed, [SpyAlgo("a", "AAA"), b])
    assert len(b.frames) == 4
    assert len(b.frames[0]) == 3 + 1  # its own three warm-up bars plus the first bar


# ---------------------------------------------- real algos on gapped committed bars


def _gapped_frame(symbol: str, drops: Sequence[str]) -> pd.DataFrame:
    frame = read_offline_bars(harness.OFFLINE_DATA / f"_{symbol}_offlinedata_").head(1170)
    stamps = [pd.Timestamp(s) for s in drops if not s.startswith("day:")]
    days = [pd.Timestamp(s[4:]).date() for s in drops if s.startswith("day:")]
    return frame[~(frame["date"].isin(stamps) | frame["date"].dt.date.isin(days))]


def _write(directory: Path, symbol: str, frame: pd.DataFrame) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(directory / f"_{symbol}_offlinedata_")


ALGO_CFGS = {
    "AAPL": ("EMACrossing", {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}),
    "MSFT": ("FunctionTest", {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3}),
}


def _paper_run(data: Path, symbols: Sequence[str]) -> tuple[dict, list[str], Ledger]:
    """Engine + one PaperBroker per algo over `data`; the per-bar agreement check of
    `harness.run_paper_backtest` (the algo's `inTrade` against the broker) for every algo."""
    harness.apply_safe_config()
    harness.reset_runtime_state()
    ledger = Ledger()
    algos, brokers = [], {}
    for symbol in symbols:
        algo_id, cfg = ALGO_CFGS[symbol]
        algo = load_algo(algo_id, dict(cfg))
        algos.append(algo)
        brokers[algo.name] = PaperBroker(ledger.open_account(algo.name, algo.ticker, CAPITAL),
                                         CostModel(Decimal("0.005"), Decimal(1), Decimal(0)))
    violations: list[str] = []
    for algo in algos:
        seen: list = []
        unsettled: list = []
        original = algo.update

        def checked(frame, algo=algo, original=original, seen=seen, unsettled=unsettled):  # type: ignore[no-untyped-def]
            result = original(frame)
            trades = algo.trades
            unsettled.extend(t.brokerRef for t in trades[len(seen):])
            seen[:] = trades
            states = {ref: brokers[algo.name].position_state(ref) for ref in unsettled}
            unsettled[:] = [r for r, s in states.items() if s not in ("closed", "cancelled")]
            current = brokers[algo.name].position_state(trades[-1].brokerRef) if trades else None
            problem, _ = harness.view_problem(algo.inTrade, current, list(states.values()))
            if problem:
                violations.append(f"{algo.name} {frame['date'].iloc[-1]}: {problem}")
            return result

        algo.update = checked
    feed = CsvBarFeed(data, [a.ticker for a in algos], 2)
    Engine(EngineConfig(), feed, MappingRouter(brokers), NullSink(), algos, ledger=ledger,
           end_commission=lambda idname, qty: brokers[idname].costs.commission(qty)).run_backtest()
    result = {}
    for algo in algos:
        result[algo.name] = [
            (t.entry_time, t.entry_price, None if t.exit_reason == "end_of_data" else t.exit_time,
             t.exit_price, t.exit_reason, t.side)
            for t in ledger.round_trips if t.account == algo.name
        ]
    return result, violations, ledger


GAPS = {
    "mid_session": {"AAPL": ["2024-08-19 09:45:00", "2024-08-19 09:46:00"],
                    "MSFT": ["2024-08-20 10:10:00"]},
    "session_tail": {"AAPL": [f"2024-08-19 12:{m}:00" for m in range(56, 60)]},
    "first_replay_and_last_of_data": {"AAPL": ["2024-08-19 06:30:00", "2024-08-20 12:59:00"],
                                      "MSFT": ["2024-08-19 06:30:00", "2024-08-20 12:59:00"]},
    "whole_day": {"MSFT": ["day:2024-08-19"]},
}


@pytest.fixture(scope="module")
def ungapped_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    data = tmp_path_factory.mktemp("ungapped")
    for symbol in ALGO_CFGS:
        _write(data, symbol, _gapped_frame(symbol, []))
    result, violations, _ = _paper_run(data, list(ALGO_CFGS))
    assert violations == []
    return result


@pytest.mark.parametrize("name", list(GAPS))
def test_real_algos_on_gapped_bars_agree_with_their_brokers_and_with_running_alone(
    name: str, tmp_path: Path, ungapped_run: dict
) -> None:
    gaps = GAPS[name]
    joint = tmp_path / "joint"
    for symbol in ALGO_CFGS:
        _write(joint, symbol, _gapped_frame(symbol, gaps.get(symbol, [])))
    result, violations, ledger = _paper_run(joint, list(ALGO_CFGS))
    assert violations == []  # per bar, each algo and its broker agree on whether a trade is open
    assert all(p.equity > 0 for p in ledger.equity_points)
    assert ledger.equity() == ledger.cash()  # finish closed everything: equity is cash
    for symbol, (_, cfg) in ALGO_CFGS.items():
        idname = cfg["idname"]
        if symbol not in gaps:
            # a symbol whose bars are all there: exactly the trades of the ungapped run
            assert result[idname] == ungapped_run[idname], (name, symbol)
        alone = tmp_path / f"alone_{symbol}"
        _write(alone, symbol, _gapped_frame(symbol, gaps.get(symbol, [])))
        alone_result, alone_violations, _ = _paper_run(alone, [symbol])
        assert alone_violations == []
        # the same trades as when it runs by itself (a synthetic end-of-data close differs only
        # in its stamp, which is the run's last timestamp)
        assert result[idname] == alone_result[idname], (name, symbol)


# ------------------------------------------------------------- month files


def test_gapped_month_files_replay_like_the_same_gapped_single_file(tmp_path: Path) -> None:
    frame = shifted_aapl(1500)
    other = frame.copy()
    other["close"] = other["close"] + 1.0
    gapped = other.drop(index=other.index[[0, 450, 451, 800, len(other) - 1]]).reset_index(
        drop=True)
    for kind, writer in (("single", write_legacy), ("months", write_months)):
        writer(tmp_path / kind, frame, "AAPL")
        writer(tmp_path / kind, gapped, "MSFT")
    single = CsvBarFeed(tmp_path / "single", ["AAPL", "MSFT"])
    months = CsvBarFeed(tmp_path / "months", ["AAPL", "MSFT"])
    assert list(single) == list(months)
    assert single.missing_counts() == months.missing_counts()
    assert months.missing_counts()["AAPL"] == 0
    assert months.missing_counts()["MSFT"] > 0
    assert all(bars for _, bars in months)


# ---------------------------------------------------------------- the command


def test_cli_repeated_gapped_runs_in_one_process_are_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=700)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[[395, 396, 500]]).to_csv(path)
    outputs = []
    for run in ("one", "two", "three"):
        env.out = tmp_path / run
        assert env.run() == 0
        captured = capsys.readouterr()
        outputs.append((json.loads((env.out / "trades.json").read_text(encoding="utf-8")),
                        captured.out.replace(str(env.out), "OUT")))
        # the summary goes to stdout only
        assert "no bar at" not in captured.err
    assert outputs[0] == outputs[1] == outputs[2]
    assert "MSFT: no bar at 3 of 310 minutes (skipped for those minutes)" in outputs[0][1]


def test_cli_missing_line_is_not_in_the_stored_run_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`runs` and `report --json` of a gapped run stay machine-readable (the line is
    printed by `backtest` only, and the count is not stored with the run)."""
    monkeypatch.setattr(cli, "current_code_version", lambda: "0" * 40)
    env = Env(tmp_path, monkeypatch)
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=700)
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[[395]]).to_csv(path)
    db = tmp_path / "store" / "runs.db"
    assert env.run("--db", str(db)) == 0
    capsys.readouterr()
    assert cli.main(["runs", "--db", str(db), "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert cli.main(["report", "1", "--db", str(db), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert "no bar" not in json.dumps(document)
