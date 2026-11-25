"""`report RUN --verify`: the exact parity check of a stored live run (issue 200).

Every run here is a REAL live run: `Engine.start_live` / `step` / `end_live` driven with a
`LiveRunWriter`, over month files written from a slice of `OfflineData`, with the warm-up
from `live_warmup` (so the stored description, digests included, is the real one). The
verifier then rebuilds the warm-up from the month files, replays the stored bars through
`run_backtest` and compares. Sessions are built once per module and copied before a test
changes anything; every database and bar folder lives under a temporary folder.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from MainStoinker.engine.algos import close_algo_logs
from MainStoinker.engine.cli import _maybe_quiet, build_engine, main
from MainStoinker.engine.feed import COLUMNS, Bar, read_offline_bars, write_month_file
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.live_store import LiveRunWriter, describe_today_bars
from MainStoinker.engine.modes import build_router
from MainStoinker.engine.paper_broker import CostModel
from MainStoinker.engine.stats import fmt4
from MainStoinker.engine.store import AlgoMeta, DataMeta, SqliteStore, open_store_readonly
from MainStoinker.engine.verify import (
    EXIT_MISMATCH,
    EXIT_REFUSED,
    EXIT_UNVERIFIABLE,
    EXIT_VERIFIED,
    StoredBarsFeed,
)
from MainStoinker.engine.warmup import live_warmup
from tests import harness
from tests.run_support import Env

OFFLINE = Path(__file__).resolve().parent.parent / "OfflineData"
SYMBOLS = ["AAPL", "MSFT", "TSLA"]
ROWS = 900  # bars per symbol replayed after the warm-up
EMA = {"idname": "{s}-10/30EMA", "ticker": "{s}", "short": "10", "long": "30"}
COSTS = CostModel(Decimal("0.005"), Decimal("1.00"), Decimal("0"))
CAPITAL = Decimal("10000")
NO_ENV: dict[str, str] = {}


def ema_entries(symbols: list[str]) -> list[dict[str, Any]]:
    return [{"ID": "EMACrossing", "data": [
        {k: v.format(s=s) if isinstance(v, str) else v for k, v in EMA.items()}
        for s in symbols]}]


# ------------------------------------------------------------------ building a live run


@dataclass
class Session:
    """A finished live session: its database, its bar folder and what the engine left open."""

    root: Path
    db: Path
    bars: Path
    positions: tuple = ()
    steps: int = 0
    gap_bars: int = 0
    failed: bool = False
    fills: str = "legacy"
    symbols: list[str] = field(default_factory=list)

    def copy(self, to: Path) -> Session:
        shutil.copytree(self.root, to)
        rel = lambda p: to / p.relative_to(self.root)  # noqa: E731
        return Session(to, rel(self.db), rel(self.bars), self.positions, self.steps,
                       self.gap_bars, self.failed, self.fills, self.symbols)


def write_bars(bars_dir: Path, symbols: list[str]) -> tuple[dict[str, pd.DataFrame], Any]:
    """Month files of the offline slices; the full frames and the replay day (the third day)."""
    data = {s: read_offline_bars(OFFLINE / f"_{s}_offlinedata_") for s in symbols}
    for symbol, frame in data.items():
        for (year, month), part in frame.groupby([frame["date"].dt.year, frame["date"].dt.month]):
            write_month_file(bars_dir / symbol / f"{symbol}_{year:04d}-{month:02d}.csv",
                             part.reset_index(drop=True)[list(COLUMNS)])
    days = sorted(data[symbols[0]]["date"].dt.normalize().unique())
    return data, pd.Timestamp(days[2]).date()


def build_session(
    root: Path, symbols: list[str], fills: str, *, rows: int = ROWS,
    drop: dict[str, range] | None = None, fail_at: int | None = None,
    stop_after: int | None = None, record_bars_dir: bool = True,
    kill_after: int | None = None, reverse_algos: bool = False, backfill: int = 0,
    heartbeat_every: int | None = None,
) -> Session:
    """Drive a live session to its end (or its failure) and return what it stored.

    `drop` removes bars (by position among the replay bars of a symbol) to make gaps;
    `fail_at` makes the first algo raise at that step; `stop_after` ends the session early
    as `failed` without an error; `kill_after` stops after that many steps without
    `end_live` or `finish` (a killed process: the run stays `running`); `reverse_algos`
    lists the algos in the opposite order of the feed's symbols; `backfill` makes the session
    start after the open: that many of today's first bars are warm-up (stored in the
    description with their digest, appended to the frames), the rest are stepped;
    `heartbeat_every` makes the writer store provisional counts and data (as the live driver's
    heartbeat does) after every that many steps.
    """
    harness.reset_runtime_state()
    root.mkdir(parents=True)
    bars_dir, db, out = root / "bars", root / "live.db", root / "live-out"
    out.mkdir()
    data, today = write_bars(bars_dir, symbols)
    frames, description, _warnings = live_warmup(bars_dir, symbols, today, 2)
    per_symbol: dict[str, list[Bar]] = {}
    warm_today: dict[str, list[Bar]] = {}
    for symbol in symbols:
        after = data[symbol][data[symbol]["date"] >= pd.Timestamp(today)].head(rows)
        gone = set((drop or {}).get(symbol, ()))
        per_symbol[symbol] = [
            Bar(*row) for i, row in enumerate(
                zip(*(after[c].tolist() for c in COLUMNS), strict=True)) if i not in gone]
        warm_today[symbol], per_symbol[symbol] = (
            per_symbol[symbol][:backfill], per_symbol[symbol][backfill:])
        if backfill:
            frames[symbol] = pd.concat(
                [frames[symbol], after.head(backfill)[list(COLUMNS)]], ignore_index=True)
    if backfill:
        description["today_bars"], description["today_sha256"] = describe_today_bars(warm_today)
    batches: dict[Any, dict[str, Bar]] = {}
    for symbol in symbols:
        for bar in per_symbol[symbol]:
            batches.setdefault(bar.date, {})[symbol] = bar
    ordered = [(stamp, {s: batches[stamp][s] for s in symbols if s in batches[stamp]})
               for stamp in sorted(batches)]
    entries = ema_entries(symbols)
    if reverse_algos:
        entries[0]["data"] = list(reversed(entries[0]["data"]))
    ledger = Ledger()
    router = build_router(entries, fills=fills, costs=COSTS, capital=CAPITAL, ledger=ledger)
    algos: list = []
    store = SqliteStore(db, env=NO_ENV)
    writer: LiveRunWriter | None = None
    positions: tuple = ()
    failed = False
    with contextlib.chdir(out), _maybe_quiet(True):
        engine = build_engine(entries, StoredBarsFeed(frames, []), router, ledger, algos)
        if fail_at is not None:
            real, calls = algos[0].update, [0]

            def update(frame: Any) -> Any:
                calls[0] += 1
                if calls[0] == fail_at:
                    raise RuntimeError("algo failure (test)")
                return real(frame)

            algos[0].update = update
        writer = LiveRunWriter(store, ledger, engine.algos)
        metas = [AlgoMeta(cfg["idname"], entry["ID"], cfg["ticker"], router.modes[cfg["idname"]],
                          CAPITAL, cfg) for entry in entries for cfg in entry["data"]]
        config = {
            "entries": entries, "warmup_days": 2, "capital": fmt4(CAPITAL),
            "costs": {"commission_per_share": fmt4(COSTS.commission_per_share),
                      "commission_minimum": fmt4(COSTS.commission_minimum),
                      "slippage_per_share": fmt4(COSTS.slippage_per_share)}
            if fills == "realistic" else None}
        writer.begin(fills=fills, code_version="test", config=config,
                     data=DataMeta(source="live", directory=str(bars_dir),
                                   symbols=tuple(symbols)),
                     algos=metas, warmup=frames, warmup_description=description)
        engine.start_live(frames, on_step=writer.on_step)
        done = 0
        try:
            for stamp, bars in ordered:
                if (stop_after is not None and done >= stop_after) or (
                        kill_after is not None and done >= kill_after):
                    break
                writer.fed(stamp, bars)
                engine.step(stamp, bars)
                done += 1
                if heartbeat_every and done % heartbeat_every == 0:
                    writer.heartbeat(
                        {(s, "fed", None): sum(1 for _, b in ordered[:done] if s in b)
                         for s in symbols},
                        DataMeta(source="live", directory=str(bars_dir), symbols=tuple(symbols),
                                 first_bar=ordered[0][0], last_bar=stamp, replayed_minutes=done,
                                 missing_minutes=(), uncovered_minutes=0, warnings=(),
                                 gap_rule=engine.gap_report))
        except RuntimeError:
            failed = True
        if kill_after is not None:  # the process dies here: no end_live, no finish
            close_algo_logs(algos)
            store.close()
            return Session(root, db, bars_dir, (), done, 0, False, fills, list(symbols))
        positions = engine.end_live()
        data_meta = DataMeta(source="live", directory=str(bars_dir) if record_bars_dir else "",
                             symbols=tuple(symbols), first_bar=ordered[0][0],
                             last_bar=ordered[done - 1][0], replayed_minutes=done,
                             missing_minutes=(), uncovered_minutes=0, warnings=())
        status = "failed" if failed or stop_after is not None else "completed"
        reason = ("error" if failed else "interrupted") if status == "failed" else "session_end"
        outcome = writer.finish(status, reason,  # type: ignore[arg-type]
                                {(s, "fed", None): sum(1 for _, b in ordered[:done] if s in b)
                                 for s in symbols}, data_meta, engine.gap_report)
        assert outcome.ok, outcome
        gap_bars = engine.gap_report.total_bars_after_gap
    close_algo_logs(algos)
    store.close()
    if not record_bars_dir:  # `begin` refuses an empty directory: an old run is made by hand
        data_json = json.loads(_data_json(db))
        data_json["directory"] = ""
        sql(db, "UPDATE runs SET data_json=? WHERE id=1", json.dumps(data_json))
    return Session(root, db, bars_dir, positions, done, gap_bars, failed, fills, list(symbols))


@pytest.fixture(scope="module")
def sessions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Session]:
    """The sessions the tests share (read-only: a test that changes one copies it first)."""
    base = tmp_path_factory.mktemp("verify-sessions")
    return {
        "one-legacy": build_session(base / "one-legacy", ["AAPL"], "legacy"),
        "one-realistic": build_session(base / "one-realistic", ["AAPL"], "realistic"),
        "three-legacy": build_session(base / "three-legacy", SYMBOLS, "legacy"),
        "three-realistic": build_session(base / "three-realistic", SYMBOLS, "realistic"),
        "gaps": build_session(
            base / "gaps", ["AAPL", "MSFT"], "realistic",
            drop={"AAPL": range(100, 104), "MSFT": range(300, 302)}),
        "failed-algo": build_session(base / "failed-algo", ["AAPL", "MSFT"], "realistic",
                                     fail_at=500),
        "stopped": build_session(base / "stopped", ["AAPL"], "realistic", stop_after=400),
        "killed": build_session(base / "killed", ["AAPL", "MSFT"], "realistic", kill_after=450),
        "reordered": build_session(base / "reordered", SYMBOLS, "realistic", rows=600,
                                   reverse_algos=True),
        "no-dir": build_session(base / "no-dir", ["AAPL"], "legacy", rows=300,
                                record_bars_dir=False),
        "provisional": build_session(
            base / "provisional", ["AAPL", "MSFT"], "realistic", rows=700, heartbeat_every=40,
            drop={"AAPL": range(100, 104), "MSFT": range(300, 302)}),
        "killed-provisional": build_session(
            base / "killed-provisional", ["AAPL", "MSFT"], "realistic", kill_after=450,
            heartbeat_every=40, drop={"AAPL": range(100, 104)}),
        "mid-session": build_session(base / "mid-session", ["AAPL", "MSFT"], "realistic",
                                     rows=700, backfill=60),
    }


# ------------------------------------------------------------------ running the command


def verify(session: Session, capsys: pytest.CaptureFixture[str], *extra: str,
           bars: bool = True, run: str = "1") -> tuple[int, str, str]:
    harness.reset_runtime_state()  # the legacy algos keep module-level state
    args = ["report", run, "--db", str(session.db), "--verify", *extra]
    if bars:
        args += ["--bars", str(session.bars)]
    capsys.readouterr()
    code = main(args, env=NO_ENV)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def sql(db: Path, statement: str, *params: Any) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(statement, params)
        con.commit()
    finally:
        con.close()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stored_run(db: Path) -> Any:
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        return store.load_run(1)
    finally:
        store.close()


# ------------------------------------------------------------------ the runs verify


@pytest.mark.parametrize("name", ["one-legacy", "one-realistic", "three-legacy",
                                  "three-realistic"])
def test_verify_passes_on_t12_synthetic_session_run(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    session = sessions[name]
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
    assert "verify: VERIFIED" in out
    run = stored_run(session.db)
    assert len(run.fills) > 4 and run.round_trips  # the data does trade
    assert f"{len(run.fills)} fills, {len(run.round_trips)} round trips" in out
    assert f"{len(session.positions)} open positions compared" in out


def test_verify_compares_a_position_still_open_at_the_end(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    open_sessions = [s for s in sessions.values() if s.positions and not s.failed]
    assert open_sessions  # at least one session ends with a position open
    for session in open_sessions:
        code, out, _ = verify(session, capsys, "--json")
        assert code == EXIT_VERIFIED
        document = json.loads(out)
        assert document["verify"]["compared"]["open_positions"] == len(session.positions) > 0
        assert document["open_positions"]  # the report lists the same positions


def test_verify_on_bars_with_gaps_compares_the_gap_rule_counts(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    session = sessions["gaps"]
    assert session.gap_bars >= 2  # the dropped minutes made bars that follow a gap
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
    # a gap-rule count that differs in the stored run is a mismatch
    changed = session.copy(tmp_path / "gaps-copy")
    con = sqlite3.connect(changed.db)
    try:
        (raw,) = con.execute("SELECT data_json FROM runs WHERE id=1").fetchone()
        data = json.loads(raw)
        first = next(iter(data["gap_rule"]["bars_after_gap"]))
        data["gap_rule"]["bars_after_gap"][first] += 1
        con.execute("UPDATE runs SET data_json=? WHERE id=1", (json.dumps(data),))
        con.commit()
    finally:
        con.close()
    code, out, _ = verify(changed, capsys)
    assert code == EXIT_MISMATCH
    assert "gap_rule" in out and f"bars_after_gap[{first}]" in out


@pytest.mark.parametrize("name", ["failed-algo", "stopped"])
def test_verify_replays_a_run_that_ended_early_up_to_its_bars(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    session = sessions[name]
    run = stored_run(session.db)
    assert run.summary.status == "failed"
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
    assert "the run ended early" in out and "status failed" in out
    assert f"end_reason {run.summary.end_reason}" in out
    document = json.loads(verify(session, capsys, "--json")[1])
    assert document["verify"]["run_ended_early"] is True
    assert document["verify"]["end_reason"] == run.summary.end_reason


def test_verify_reads_the_directory_recorded_with_the_run(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = verify(sessions["one-legacy"], capsys, bars=False)
    assert code == EXIT_VERIFIED, out + err


def test_verify_without_a_known_bars_directory_is_a_usage_error(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = verify(sessions["no-dir"], capsys, bars=False)
    assert code == 2 and out == ""
    assert "--bars" in err


def test_bars_without_verify_and_a_bars_path_that_is_no_directory_are_usage_errors(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    session = sessions["one-legacy"]
    capsys.readouterr()
    assert main(["report", "1", "--db", str(session.db), "--bars", str(session.bars)],
                env=NO_ENV) == 2
    assert "--bars only applies with --verify" in capsys.readouterr().err
    code, _, err = verify(session, capsys, "--bars", str(tmp_path / "missing"), bars=False)
    assert code == 2 and "not a directory" in err


# ------------------------------------------------------------------ mismatches


def test_verify_detects_altered_fill(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    fills = stored_run(base.db).fills
    assert len(fills) > 4
    target = fills[2]
    changes = {
        "price": ("UPDATE fills SET price_e4=price_e4+1000 WHERE run_id=1 AND fill_id=?",
                  (target.fill_id,)),
        "quantity": ("UPDATE fills SET quantity_e4=quantity_e4+10000 WHERE run_id=1 AND fill_id=?",
                     (target.fill_id,)),
        "time": ("UPDATE fills SET time=? WHERE run_id=1 AND fill_id=?",
                 ("2024-08-20T11:11:00", target.fill_id)),
    }
    for field_name, (statement, params) in changes.items():
        altered = base.copy(tmp_path / f"altered-{field_name}")
        sql(altered.db, statement, *params)
        code, out, _ = verify(altered, capsys)
        assert code == EXIT_MISMATCH, field_name
        assert "verify: MISMATCH" in out
        assert "fills[2] at " in out
        assert f" {field_name}: stored " in out, out
    # the same verdict in the json document, with the index, time, field and both values
    altered = base.copy(tmp_path / "altered-json")
    sql(altered.db, *changes["price"][:1], *changes["price"][1])
    code, out, _ = verify(altered, capsys, "--json")
    assert code == EXIT_MISMATCH
    block = json.loads(out)["verify"]
    assert block["outcome"] == "mismatch" and block["exit_code"] == EXIT_MISMATCH
    (fills_diff,) = [d for d in block["differences"] if d["kind"] == "fills"]
    first = fills_diff["first"][0]
    assert first["index"] == 2 and first["field"] == "price"
    assert first["time"] == target.time.isoformat(sep=" ")
    assert first["stored"] == fmt4(target.price + Decimal("0.1"))
    assert first["replayed"] == fmt4(target.price)


def test_verify_detects_an_altered_stored_bar(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    run = stored_run(base.db)
    fill = run.fills[2]
    altered = base.copy(tmp_path / "altered-bar")
    sql(altered.db, "UPDATE run_bars SET open=open*1.01, high=high*1.01, low=low*1.01,"
        " close=close*1.01 WHERE run_id=1 AND symbol=? AND time=?",
        fill.symbol, fill.time.isoformat())
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH
    assert "verify: MISMATCH" in out
    assert "fills[" in out or "equity[" in out or "round_trips[" in out


def test_verify_detects_a_missing_stored_bar_by_the_fed_count(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["one-legacy"].copy(tmp_path / "missing-bar")
    sql(altered.db, "DELETE FROM run_bars WHERE run_id=1 AND symbol='AAPL' AND time="
        "(SELECT MAX(time) FROM run_bars WHERE run_id=1)")
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH
    assert "bars" in out and "fed" in out


def test_verify_detects_a_changed_order_event_and_a_changed_ignored_event(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    altered = base.copy(tmp_path / "altered-order")
    sql(altered.db, "UPDATE order_events SET price_e4=price_e4+500 WHERE run_id=1 AND seq="
        "(SELECT MIN(seq) FROM order_events WHERE run_id=1 AND price_e4 IS NOT NULL)")
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH and "order_events[" in out


def test_verify_detects_a_changed_round_trip_and_changed_equity(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    altered = base.copy(tmp_path / "altered-trip")
    sql(altered.db, "UPDATE trades SET net_e4=net_e4+1 WHERE run_id=1 AND seq="
        "(SELECT MIN(seq) FROM trades WHERE run_id=1)")
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH and "round_trips[0]" in out and " net: " in out
    altered = base.copy(tmp_path / "altered-equity")
    sql(altered.db, "UPDATE equity SET equity_e4=equity_e4+7 WHERE run_id=1 AND seq="
        "(SELECT MIN(seq) FROM equity WHERE run_id=1 AND seq>5 AND algo IS NULL)")
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH and "equity[" in out


def test_verify_lists_at_most_a_handful_of_differences_per_kind(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["three-realistic"].copy(tmp_path / "many")
    sql(altered.db, "UPDATE fills SET price_e4=price_e4+10 WHERE run_id=1")
    code, out, _ = verify(altered, capsys, "--json")
    assert code == EXIT_MISMATCH
    (diff,) = [d for d in json.loads(out)["verify"]["differences"] if d["kind"] == "fills"]
    assert diff["count"] > 5 and len(diff["first"]) == 5


# ------------------------------------------------------------------ unverifiable


def test_verify_reports_changed_warmup_files(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["three-realistic"].copy(tmp_path / "changed-warmup")
    path = altered.bars / "MSFT" / "MSFT_2024-08.csv"
    frame = read_offline_bars(path)
    frame.loc[3, "close"] = frame.loc[3, "close"] + 0.37  # a warm-up day (the first two days)
    write_month_file(path, frame)
    expected = json.loads(_config(altered.db))["warmup"]["symbols"]["MSFT"]["warmup_sha256"]
    code, out, err = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE, out + err
    assert "verify: UNVERIFIABLE" in out
    assert "MSFT" in out and "MSFT_2024-08.csv" in out
    assert f"expected {expected}" in out and "actual " in out
    assert out.count("digest differs") == 1  # AAPL and TSLA are untouched
    block = json.loads(verify(altered, capsys, "--json")[1])["verify"]
    assert block["outcome"] == "unverifiable"
    (problem,) = block["unverifiable"]
    assert problem["symbol"] == "MSFT" and problem["file"] == "MSFT_2024-08.csv"
    assert problem["expected"] == expected and problem["actual"] not in (None, expected)


def test_a_change_after_the_warmup_days_does_not_make_a_run_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["one-realistic"].copy(tmp_path / "later-days")
    path = altered.bars / "AAPL" / "AAPL_2024-08.csv"
    frame = read_offline_bars(path)
    frame.loc[len(frame) - 1, "close"] += 1.0  # the last day of the month file: not a warm-up day
    write_month_file(path, frame)
    code, out, err = verify(altered, capsys)
    assert code == EXIT_VERIFIED, out + err


def test_verify_reports_a_missing_month_file(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["three-legacy"].copy(tmp_path / "missing-warmup")
    (altered.bars / "TSLA" / "TSLA_2024-08.csv").unlink()
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE
    assert "TSLA" in out and "TSLA_2024-08.csv" in out and "missing" in out
    block = json.loads(verify(altered, capsys, "--json")[1])["verify"]
    assert block["unverifiable"][0]["actual"] is None


def test_verify_without_a_warmup_description_is_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["one-legacy"].copy(tmp_path / "no-description")
    config = json.loads(_config(altered.db))
    del config["warmup"]
    sql(altered.db, "UPDATE runs SET config_json=? WHERE id=1", json.dumps(config))
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE and "no warm-up description" in out


def test_verify_of_a_realistic_run_without_stored_costs_is_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["one-realistic"].copy(tmp_path / "no-costs")
    config = json.loads(_config(altered.db))
    config["costs"] = None
    sql(altered.db, "UPDATE runs SET config_json=? WHERE id=1", json.dumps(config))
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE and "stores no costs" in out


def _data_json(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT data_json FROM runs WHERE id=1").fetchone()[0]
    finally:
        con.close()


def _config(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT config_json FROM runs WHERE id=1").fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------------ refusal, read-only, json


def test_verify_refuses_a_backtest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path / "env", monkeypatch)
    env.setup([{"ID": "EMACrossing", "data": [
        {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}]}],
        ["AAPL"], 400)
    db = tmp_path / "bt.db"
    assert env.run("--db", str(db)) == 0
    before = file_hash(db)
    capsys.readouterr()
    code = main(["report", "1", "--db", str(db), "--verify", "--bars", str(tmp_path)], env=NO_ENV)
    captured = capsys.readouterr()
    assert code == EXIT_REFUSED == 4
    assert captured.out == ""  # no report, no verdict
    assert "backtest run" in captured.err and "nothing to verify" in captured.err
    assert file_hash(db) == before
    # and without --verify the report is what it always was
    assert main(["report", "1", "--db", str(db)], env=NO_ENV) == 0


def test_verify_never_writes_db(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for name in ("three-realistic", "failed-algo"):
        session = sessions[name].copy(tmp_path / f"readonly-{name}")
        before = file_hash(session.db)
        for extra in ((), ("--json",)):
            code, _, _ = verify(session, capsys, *extra)
            assert code == EXIT_VERIFIED
        assert file_hash(session.db) == before
    # a mismatch and an unverifiable run leave the file alone too
    altered = sessions["three-realistic"].copy(tmp_path / "readonly-altered")
    sql(altered.db, "UPDATE fills SET price_e4=price_e4+1000 WHERE run_id=1 AND fill_id=1")
    before = file_hash(altered.db)
    assert verify(altered, capsys)[0] == EXIT_MISMATCH
    (altered.bars / "AAPL" / "AAPL_2024-08.csv").unlink()
    assert verify(altered, capsys)[0] == EXIT_UNVERIFIABLE
    assert file_hash(altered.db) == before


def test_report_json_with_verify_carries_a_verify_object(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["three-realistic"]
    code, out, _ = verify(session, capsys, "--json")
    assert code == EXIT_VERIFIED
    document = json.loads(out)
    assert {"run", "accounts", "total", "verify"} <= set(document)
    block = document["verify"]
    assert block["outcome"] == "verified" and block["exit_code"] == 0
    assert block["run_ended_early"] is False and block["differences"] == []
    assert block["unverifiable"] == []
    run = stored_run(session.db)
    assert block["compared"]["fills"] == len(run.fills)
    assert block["compared"]["round_trips"] == len(run.round_trips)
    assert block["compared"]["bars"] == len(_bar_rows(session.db)) == session.steps * 3
    # without --verify the document has no such key
    harness.reset_runtime_state()
    capsys.readouterr()
    assert main(["report", "1", "--db", str(session.db), "--json"], env=NO_ENV) == 0
    assert "verify" not in json.loads(capsys.readouterr().out)


def _bar_rows(db: Path) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT 1 FROM run_bars WHERE run_id=1").fetchall()
    finally:
        con.close()


def test_the_replay_leaves_no_file_in_the_starting_directory(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = tmp_path / "start-here"
    start.mkdir()
    monkeypatch.chdir(start)
    assert verify(sessions["one-realistic"], capsys)[0] == EXIT_VERIFIED
    assert list(start.iterdir()) == []


# ------------------------------------------------------------------ early-ended runs


def test_verify_compares_a_run_left_running_by_a_killed_process(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["killed"]
    assert stored_run(session.db).summary.status == "running"
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
    assert "the run ended early (status running" in out
    assert "final equity was not compared" in out


@pytest.mark.parametrize("name", ["failed-algo", "killed", "stopped"])
def test_verify_detects_an_altered_fill_in_an_early_ended_run(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path, name: str
) -> None:
    altered = sessions[name].copy(tmp_path / f"early-{name}")
    fill = stored_run(altered.db).fills[1]
    sql(altered.db, "UPDATE fills SET price_e4=price_e4+1000 WHERE run_id=1 AND fill_id=?",
        fill.fill_id)
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH, out
    assert "the run ended early" in out
    assert "fills[1] at " in out and " price: stored " in out


def test_records_after_the_last_stored_bar_are_left_out_of_an_early_ended_run_only(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    insert = ("INSERT INTO order_events(run_id, seq, order_id, algo, broker_ref, kind, time, "
              "side, quantity_e4, order_type, price_e4) SELECT run_id, seq+100000, order_id, algo,"
              " broker_ref, kind, '2024-08-30T10:00:00', side, quantity_e4, order_type, price_e4 "
              "FROM order_events WHERE run_id=1 AND seq=(SELECT MAX(seq) FROM order_events "
              "WHERE run_id=1)")
    early = sessions["stopped"].copy(tmp_path / "cut-early")
    sql(early.db, insert)
    code, out, _ = verify(early, capsys)
    assert code == EXIT_VERIFIED, out
    assert "1 stored record(s) after the last stored bar" in out  # left out, and said so
    # the same record in a COMPLETED run is compared, and is a difference
    done = sessions["one-realistic"].copy(tmp_path / "cut-done")
    sql(done.db, insert)
    code, out, _ = verify(done, capsys)
    assert code == EXIT_MISMATCH and "order_events[" in out
    # a record at or before the last stored bar is always compared
    inside = sessions["stopped"].copy(tmp_path / "cut-inside")
    sql(inside.db, insert.replace("'2024-08-30T10:00:00'", "'2024-08-20T10:00:00'"))
    assert verify(inside, capsys)[0] == EXIT_MISMATCH


# ------------------------------------------------------------------ final equity, cross-checks


@pytest.mark.parametrize("name", ["three-realistic", "one-legacy", "stopped"])
def test_verify_detects_an_altered_final_equity_group(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path, name: str
) -> None:
    session = sessions[name]
    assert verify(session, capsys)[0] == EXIT_VERIFIED
    altered = session.copy(tmp_path / f"final-{name}")
    sql(altered.db, "UPDATE equity SET equity_e4=equity_e4+10000 WHERE run_id=1 AND seq="
        "(SELECT MAX(seq) FROM equity WHERE run_id=1 AND algo IS NULL)")
    code, out, _ = verify(altered, capsys)
    assert code == EXIT_MISMATCH
    assert "final_equity" in out and " equity: stored " in out
    document = json.loads(verify(session, capsys, "--json")[1])
    assert document["verify"]["compared"]["final_equity"] == len(stored_run(session.db).algos) + 1


def test_verify_detects_a_changed_algo_parameter_fill_mode_and_capital(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    config = json.loads(_config(base.db))
    # an algo parameter in the config the replay uses
    config["entries"][0]["data"][0]["short"] = "8"
    changed = base.copy(tmp_path / "param-config")
    sql(changed.db, "UPDATE runs SET config_json=? WHERE id=1", json.dumps(config))
    code, out, _ = verify(changed, capsys)
    assert code == EXIT_MISMATCH and "algos[0]" in out and " params: stored " in out
    # the same parameter changed only in the algos table (what the dashboard shows)
    shown = base.copy(tmp_path / "param-algos")
    sql(shown.db, "UPDATE algos SET params_json=REPLACE(params_json, '\"10\"', '\"9\"') "
        "WHERE run_id=1 AND rowid=(SELECT MIN(rowid) FROM algos WHERE run_id=1)")
    code, out, _ = verify(shown, capsys)
    assert code == EXIT_MISMATCH and " params: stored " in out
    # the fill mode
    mode = base.copy(tmp_path / "mode")
    sql(mode.db, "UPDATE runs SET fills='legacy' WHERE id=1")
    assert verify(mode, capsys)[0] == EXIT_MISMATCH
    # the capital, in the config and in the algos table
    capital = base.copy(tmp_path / "capital-config")
    config = json.loads(_config(base.db))
    config["capital"] = "20000.0000"
    sql(capital.db, "UPDATE runs SET config_json=? WHERE id=1", json.dumps(config))
    code, out, _ = verify(capital, capsys)
    assert code == EXIT_MISMATCH and " capital: stored " in out
    table = base.copy(tmp_path / "capital-table")
    sql(table.db, "UPDATE algos SET capital_e4=capital_e4+10000 WHERE run_id=1")
    code, out, _ = verify(table, capsys)
    assert code == EXIT_MISMATCH and " capital: stored " in out


def test_verify_detects_swapped_bars_and_a_deleted_last_exit_fill(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    fill = stored_run(base.db).fills[2]
    swapped = base.copy(tmp_path / "swapped")
    con = sqlite3.connect(swapped.db)
    try:
        rows = con.execute(
            "SELECT time, open, high, low, close FROM run_bars WHERE run_id=1 AND symbol=? "
            "AND time <= ? ORDER BY time DESC LIMIT 2", (fill.symbol, fill.time.isoformat())
        ).fetchall()
        (t1, *v1), (t2, *v2) = rows
        for t, v in ((t1, v2), (t2, v1)):
            con.execute("UPDATE run_bars SET open=?, high=?, low=?, close=? WHERE run_id=1 AND "
                        "symbol=? AND time=?", (*v, fill.symbol, t))
        con.commit()
    finally:
        con.close()
    assert verify(swapped, capsys)[0] == EXIT_MISMATCH
    deleted = base.copy(tmp_path / "no-last-exit")
    sql(deleted.db, "DELETE FROM fills WHERE run_id=1 AND fill_id="
        "(SELECT MAX(fill_id) FROM fills WHERE run_id=1 AND reason != 'entry')")
    code, out, _ = verify(deleted, capsys)
    assert code == EXIT_MISMATCH and "fills" in out


def test_verify_cross_checks_the_stored_bars_against_the_run_record(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base = sessions["three-realistic"]
    counts = base.copy(tmp_path / "counts")
    sql(counts.db, "UPDATE run_counts SET value=value+1 WHERE run_id=1")
    code, out, _ = verify(counts, capsys)
    assert code == EXIT_MISMATCH and "bars: " in out and " fed: stored " in out
    # the minutes and the last bar the run says it replayed
    for field_name, new in (("replayed_minutes", lambda v: v + 1),
                            ("last_bar", lambda v: "2024-08-20T09:31:00")):
        data_copy = base.copy(tmp_path / f"data-{field_name}")
        con = sqlite3.connect(data_copy.db)
        try:
            data = json.loads(con.execute("SELECT data_json FROM runs WHERE id=1").fetchone()[0])
            data[field_name] = new(data[field_name])
            con.execute("UPDATE runs SET data_json=? WHERE id=1", (json.dumps(data),))
            con.commit()
        finally:
            con.close()
        code, out, _ = verify(data_copy, capsys)
        assert code == EXIT_MISMATCH and f" {field_name}: stored " in out, field_name
    # no fed counts at all on a completed run: stated, and not verified
    none = base.copy(tmp_path / "no-counts")
    sql(none.db, "DELETE FROM run_counts WHERE run_id=1")
    code, out, _ = verify(none, capsys)
    assert code == EXIT_UNVERIFIABLE and "bar counts not recorded" in out


def test_verify_in_a_fresh_process_prints_only_the_report(
    sessions: dict[str, Session], tmp_path: Path
) -> None:
    """The legacy algo modules print when they are first imported; a fresh process (unlike
    this one, where they are loaded already) must not let that reach stdout."""
    session = sessions["one-legacy"]
    work = tmp_path / "fresh-cwd"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    base = [sys.executable, "-P", "-m", "MainStoinker", "report", "1", "--db", str(session.db),
            "--verify", "--bars", str(session.bars)]
    as_json = subprocess.run([*base, "--json"], cwd=work, env=env, capture_output=True,
                             text=True, timeout=300)
    assert as_json.returncode == EXIT_VERIFIED, as_json.stdout + as_json.stderr
    assert json.loads(as_json.stdout)["verify"]["outcome"] == "verified"
    as_text = subprocess.run(base, cwd=work, env=env, capture_output=True, text=True,
                             timeout=300)
    assert as_text.returncode == EXIT_VERIFIED, as_text.stdout + as_text.stderr
    lines = as_text.stdout.splitlines()
    assert lines[0] == "run 1"
    assert lines[-1].startswith("verify: VERIFIED")
    assert list(work.iterdir()) == []


def test_the_replay_uses_the_feed_order_of_the_stored_symbols_not_the_algos_order(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["reordered"]
    run = stored_run(session.db)
    assert [a.ticker for a in run.algos] == ["TSLA", "MSFT", "AAPL"]  # algos listed backwards
    assert list(run.summary.data.symbols) == ["AAPL", "MSFT", "TSLA"]  # the feed's order
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err


# ------------------------------------------------------------------ a session started after the open


def _set_description(db: Path, change: Any) -> None:
    """Rewrite the stored warm-up description of run 1 with `change(description)`."""
    config = json.loads(_config(db))
    change(config["warmup"])
    sql(db, "UPDATE runs SET config_json=? WHERE id=1", json.dumps(config))


def test_a_session_started_after_the_open_stores_and_verifies_todays_bars(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["mid-session"]
    description = json.loads(_config(session.db))["warmup"]
    assert set(description["today_bars"]) == {"AAPL", "MSFT"}
    assert all(len(rows) == 60 for rows in description["today_bars"].values())
    assert set(description["today_sha256"]) == {"AAPL", "MSFT"}
    assert all(row[0].startswith(description["today"]) for rows in description["today_bars"].values()
               for row in rows)
    run = stored_run(session.db)
    assert run.fills, "the mid-session run does trade"
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
    assert "verify: VERIFIED" in out and "today's" not in out


def test_the_replay_of_a_mid_session_run_starts_from_the_month_files_plus_todays_bars(
    sessions: dict[str, Session]
) -> None:
    from MainStoinker.engine.verify import add_today_bars, rebuild_warmup

    session = sessions["mid-session"]
    description = json.loads(_config(session.db))["warmup"]
    month, problems = rebuild_warmup(session.bars, description, ["AAPL", "MSFT"])
    assert not problems
    both, problems = add_today_bars(month, description, ["AAPL", "MSFT"])
    assert not problems
    for symbol in ("AAPL", "MSFT"):
        assert len(both[symbol]) == len(month[symbol]) + 60
        assert both[symbol]["date"].iloc[-1].strftime("%H:%M") == "10:29"  # 60 bars after 09:30
        assert both[symbol].iloc[:len(month[symbol])].equals(month[symbol])


def test_a_changed_stored_today_bar_makes_the_run_unverifiable_not_a_mismatch(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The bars in the description no longer hash to the digest recorded with them: the record
    changed, nothing says which of the two is the one the session used, so the verdict is
    UNVERIFIABLE (it says nothing about the engine) and names the symbol and both digests."""
    altered = sessions["mid-session"].copy(tmp_path / "tampered")
    expected = json.loads(_config(altered.db))["warmup"]["today_sha256"]["MSFT"]

    def change(description: dict[str, Any]) -> None:
        description["today_bars"]["MSFT"][10][4] += 0.5  # a close

    _set_description(altered.db, change)
    code, out, err = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE, out + err
    assert "verify: UNVERIFIABLE" in out and "today's backfill bars digest differs" in out
    assert "MSFT" in out and "AAPL:" not in out
    assert f"expected {expected}" in out
    (problem,) = json.loads(verify(altered, capsys, "--json")[1])["verify"]["unverifiable"]
    assert problem["symbol"] == "MSFT" and problem["expected"] == expected
    assert problem["actual"] not in (None, expected)


def test_a_dropped_stored_today_bar_makes_the_run_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    altered = sessions["mid-session"].copy(tmp_path / "dropped")
    _set_description(altered.db, lambda d: d["today_bars"]["AAPL"].pop(20))
    code, out, err = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE, out + err
    assert "today's backfill bars digest differs" in out and "AAPL" in out


@pytest.mark.parametrize("how,words", [
    ("no-bars", "missing from the stored warm-up description"),
    ("no-key", "missing from the stored warm-up description"),
    ("no-key-no-digest", "has no `today_bars` for this symbol"),
    ("no-digest", "no recorded digest"),
])
def test_missing_today_bars_or_digest_are_named_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path,
    how: str, words: str
) -> None:
    altered = sessions["mid-session"].copy(tmp_path / how)

    def change(description: dict[str, Any]) -> None:
        if how == "no-bars":
            description["today_bars"]["AAPL"] = []
        elif how == "no-key":
            del description["today_bars"]["AAPL"]
        elif how == "no-key-no-digest":
            del description["today_bars"]["AAPL"]
            del description["today_sha256"]["AAPL"]
        else:
            del description["today_sha256"]["AAPL"]

    _set_description(altered.db, change)
    code, out, err = verify(altered, capsys)
    assert code == EXIT_UNVERIFIABLE, out + err
    assert words in out and "AAPL" in out


def test_a_description_with_empty_today_bars_or_none_verifies_as_before(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A session started before the open (nothing backfilled), as the first driver stored it
    (`today_bars` all empty, no digest), and a run stored before `today_bars` existed."""
    empty = sessions["three-realistic"].copy(tmp_path / "empty")
    _set_description(empty.db, lambda d: d.update(today_bars={s: [] for s in SYMBOLS}))
    code, out, err = verify(empty, capsys)
    assert code == EXIT_VERIFIED, out + err
    with_digests = sessions["three-realistic"].copy(tmp_path / "empty-digests")
    _set_description(with_digests.db, lambda d: d.update(
        today_bars={s: [] for s in SYMBOLS}, today_sha256={}))
    code, out, err = verify(with_digests, capsys)
    assert code == EXIT_VERIFIED, out + err
    code, out, err = verify(sessions["three-realistic"], capsys)  # no key at all
    assert code == EXIT_VERIFIED, out + err
    assert "today_bars" not in _config(sessions["three-realistic"].db)


def test_a_run_stored_by_the_first_driver_with_todays_bars_and_no_digest_is_unverifiable(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    old = sessions["mid-session"].copy(tmp_path / "first-driver")
    _set_description(old.db, lambda d: d.pop("today_sha256"))
    code, out, _ = verify(old, capsys)
    assert code == EXIT_UNVERIFIABLE and "no recorded digest" in out


def test_verify_passes_on_a_run_that_stored_provisional_counts_and_data_midway(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["provisional"]
    run = stored_run(session.db)
    assert run.summary.status == "completed" and run.summary.data.gap_rule is not None
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err


def test_verify_does_not_compare_the_provisional_counts_of_a_run_left_running(
    sessions: dict[str, Session], capsys: pytest.CaptureFixture[str]
) -> None:
    session = sessions["killed-provisional"]
    run = stored_run(session.db)
    assert run.summary.status == "running"
    assert run.summary.data.last_bar is not None  # the heartbeat stored a provisional data record
    assert run.summary.data.gap_rule is not None
    code, out, err = verify(session, capsys)
    assert code == EXIT_VERIFIED, out + err
