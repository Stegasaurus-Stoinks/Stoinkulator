"""Stored indicator series hold the right values at the right bars.

Independent of `engine.series`: the expected points are recomputed here from the
bar file (dates in file order, which is warm-up then replay) and from the
algo's final `AlgoData`, and compared point by point with what `load_series`
returns. Every database lives under pytest's `tmp_path`.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import MainStoinker.engine.cli as cli
from MainStoinker.engine.cli import main
from MainStoinker.engine.store import open_store
from tests import harness
from tests.test_cli_modes import EMA, EMA_MSFT, Env, ema_config
from tests.test_golden_backtests import CASES

VERSION = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "current_code_version", lambda: VERSION)
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def frames(monkeypatch) -> dict[str, tuple[pd.DataFrame, list, list]]:
    """Each algo's final frame, names and draw types as seen at capture time."""
    seen: dict[str, tuple[pd.DataFrame, list, list]] = {}
    real = cli.extract_series

    def spy(algo, bar_times):
        seen[algo.name] = (algo.AlgoData.copy(deep=True), list(algo.FrontEndDataStruct),
                           list(algo.FrontEndDataType))
        return real(algo, bar_times)

    monkeypatch.setattr(cli, "extract_series", spy)
    return seen


def backtest(env: Env, db: Path, *extra: str) -> int:
    return main(["backtest", "--algos", str(env.algos), "--data", str(env.data),
                 "--out", str(env.out), "--quiet", "--db", str(db), *extra])


def load(db: Path, algo: str):
    store = open_store(str(db), env=os.environ)
    try:
        return store.load_run(1), store.load_series(1, algo)
    finally:
        store.close()


def bar_dates(env: Env, symbol: str) -> list:
    """Dates of one bar file in file order (warm-up bars, then the replayed ones)."""
    frame = pd.read_csv(env.data / f"_{symbol}_offlinedata_", index_col=0)
    dates = pd.to_datetime(frame["date"])
    assert dates.is_monotonic_increasing and dates.is_unique
    return [d.to_pydatetime() for d in dates]


def expected_points(frame: pd.DataFrame, names: list, types: list, dates: list) -> dict:
    """{name: (draw_type, position, [(time, value), ...])} straight from the frame."""
    assert len(frame) == len(dates)
    out: dict = {}
    for position, (name, draw_type) in enumerate(zip(names, types, strict=True)):
        if name in out or name not in frame.columns:
            continue
        points = []
        if frame[name].dtype != bool:
            values = pd.to_numeric(frame[name], errors="coerce").to_numpy(
                dtype="float64", na_value=np.nan)
            points = [(dates[i], float(v)) for i, v in enumerate(values) if math.isfinite(v)]
        out[name] = (draw_type, position, points)
    return out


def check_against_frame(db: Path, idname: str, dates: list, seen) -> dict:
    frame, names, types = seen[idname]
    expected = expected_points(frame, names, types, dates)
    data, points = load(db, idname)
    infos = {s.name: s for s in data.series if s.account == idname}
    assert list(infos) == sorted(infos, key=lambda n: infos[n].position)
    assert set(infos) == set(expected)
    for name, (draw_type, position, wanted) in expected.items():
        assert (infos[name].draw_type, infos[name].position) == (draw_type, position)
        assert [(p.time, p.value) for p in points if p.name == name] == wanted, name
    assert len(points) == sum(len(w) for _, _, w in expected.values())
    return expected


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """TA-Lib style EMA: the first value is the mean of the first `period` closes."""
    out = np.full(len(closes), np.nan)
    out[period - 1] = closes[:period].mean()
    factor = 2 / (period + 1)
    for i in range(period, len(closes)):
        out[i] = closes[i] * factor + out[i - 1] * (1 - factor)
    return out


def test_ema_series_equal_an_ema_computed_from_the_csv(env, tmp_path, frames):
    env.setup(ema_config(), ["AAPL"], rows=700)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db) == 0
    _, points = load(db, EMA["idname"])
    raw = pd.read_csv(env.data / "_AAPL_offlinedata_", index_col=0)
    dates = bar_dates(env, "AAPL")
    closes = raw["close"].to_numpy(float)
    for name, period in (("MA20", int(EMA["short"])), ("MA50", int(EMA["long"]))):
        wanted = ema(closes, period)
        stored = {p.time: p.value for p in points if p.name == name}
        assert len(stored) == int(np.isfinite(wanted).sum()) == 700 - period + 1
        for i in (period - 1, period, period + 1, 100, 480, 699):  # first value, then later bars
            assert stored[dates[i]] == pytest.approx(wanted[i], rel=1e-12)
        assert dates[period - 2] not in stored  # nothing before the first computed value
    # the first series point is at a warm-up bar, before the first replayed bar
    assert min(stored) < load(db, EMA["idname"])[0].summary.data.first_bar


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_series_match_algodata_for_two_algos_and_symbols_with_different_warmup(
        env, tmp_path, frames, fills):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=1500)
    # Drop MSFT's first 200 rows: the replay bars stay the same, its warm-up is shorter.
    path = env.data / "_MSFT_offlinedata_"
    pd.read_csv(path, index_col=0).iloc[200:].reset_index(drop=True).to_csv(path)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db, "--fills", fills) == 0
    aapl = check_against_frame(db, EMA["idname"], bar_dates(env, "AAPL"), frames)
    msft = check_against_frame(db, EMA_MSFT["idname"], bar_dates(env, "MSFT"), frames)
    assert len(bar_dates(env, "AAPL")) - len(bar_dates(env, "MSFT")) == 200
    assert aapl["MA20"][2][0][0] != msft["MA20"][2][0][0]  # each starts at its own bar 9


def test_every_algo_in_the_repository_stores_what_it_declares(env, tmp_path, frames):
    """Each shipped algo: declared columns stored with their draw type; others are skipped."""
    configs = [
        {"ID": "TrendLine", "data": [{"idname": "AAPL-trend", "ticker": "AAPL", "short": "10",
                                      "long": "30"}]},
        {"ID": "FunctionTest", "data": [{"idname": "MSFT-ft", "ticker": "MSFT", "RRRatio": 3}]},
        {"ID": "OpeningRangeCandle", "data": [{"idname": "AAPL-orc", "ticker": "AAPL",
                                               "RRRatio": 3}]},
    ]
    # Few rows and a one-day warm-up: TrendLine prints its whole frame on every bar.
    env.setup(configs, ["AAPL", "MSFT"], rows=700)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db, "--warmup-days", "1") == 0
    trend = check_against_frame(db, "AAPL-trend", bar_dates(env, "AAPL"), frames)
    assert trend["random"][0] == "marker-dot" and trend["random"][2]  # unknown type kept as text
    func = check_against_frame(db, "MSFT-ft", bar_dates(env, "MSFT"), frames)
    assert "tp" not in func  # FunctionTest writes a `tp` column it never declares
    assert "tp" in frames["MSFT-ft"][0].columns
    orc = check_against_frame(db, "AAPL-orc", bar_dates(env, "AAPL"), frames)
    assert orc["tp"][0] == "segment"


# Every golden case with legacy fills; realistic fills only differ in the
# StopPrice / Trade columns, so two cases cover them.
REALISTIC = {"emacrossing_aapl", "elliot_msft"}
RUNS = [
    pytest.param(*case.values, fills, id=f"{case.id}-{fills}")
    for fills in ("legacy", "realistic")
    for case in CASES
    if fills == "legacy" or case.id in REALISTIC
]


@pytest.mark.slow
@pytest.mark.parametrize("algo_id, cfg, fills", RUNS)
def test_golden_cases_store_the_algodata_columns_point_for_point(
        algo_id, cfg, fills, env, tmp_path, frames):
    env.setup([{"ID": algo_id, "data": [cfg]}], [cfg["ticker"]], rows=None)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db, "--fills", fills) == 0
    expected = check_against_frame(db, cfg["idname"], bar_dates(env, cfg["ticker"]), frames)
    assert expected  # every algo has at least one series
    if fills == "legacy":
        # The same backtest through the harness leaves the same columns behind.
        harness.stage_offline_data(Path.cwd(), [cfg["ticker"]])
        (algo,) = harness.run_offline_backtest(algo_id, [cfg])
        again = expected_points(algo.AlgoData, list(algo.FrontEndDataStruct),
                                list(algo.FrontEndDataType), bar_dates(env, cfg["ticker"]))
        assert again == expected


def test_one_algo_with_a_short_frame_is_skipped_and_the_others_are_stored(
        env, tmp_path, monkeypatch, capsys):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=900)
    real = cli.extract_series

    def damage(algo, bar_times):
        if algo.name == EMA["idname"]:
            algo.AlgoData = algo.AlgoData.iloc[:-1]
        return real(algo, bar_times)

    monkeypatch.setattr(cli, "extract_series", damage)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db) == 0
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith(f"warning: {EMA['idname']}:")
    data, damaged = load(db, EMA["idname"])
    _, healthy = load(db, EMA_MSFT["idname"])
    assert damaged == () and healthy
    assert {s.account for s in data.series} == {EMA_MSFT["idname"]}
    assert data.summary.status == "completed"


def test_unreadable_data_file_for_the_fingerprint_is_exit_2_and_nothing_is_stored(
        env, tmp_path, monkeypatch, capsys):
    env.setup(ema_config(), ["AAPL"], rows=300)

    def refuse(path):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "file_fingerprint", refuse)
    db = tmp_path / "s" / "r.db"
    assert backtest(env, db) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: cannot read data file") and len(err.splitlines()) == 1
    assert not db.exists() and not env.out.exists()


def test_two_runs_in_one_process_store_identical_series(env, tmp_path, frames):
    env.setup(ema_config(), ["AAPL"], rows=900)
    first, second = tmp_path / "a" / "r.db", tmp_path / "b" / "r.db"
    assert backtest(env, first) == 0
    assert backtest(env, second) == 0
    assert load(first, EMA["idname"])[1] == load(second, EMA["idname"])[1]
    assert load(first, EMA["idname"])[1]
