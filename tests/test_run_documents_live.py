"""Live runs in the run documents and in `report` (issue 193).

Everything is computed from stored data with "now" passed in. Every database
lives under `tmp_path`; nothing reads a clock.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from MainStoinker.engine.cli import main
from MainStoinker.engine.cli_report import run_report_command, run_runs_command
from MainStoinker.engine.engine import Engine
from MainStoinker.engine.feed import Bar
from MainStoinker.engine.gaprule import GapRuleReport
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.live_counts import (
    COUNT_DROPPED,
    COUNT_FED,
    COUNT_LATE,
    COUNT_SCOPE_SYMBOL,
    DROP_REASON_LABELS,
    DROP_REASONS,
    HEARTBEAT_INTERVAL,
    LIVE_KINDS,
    count_key,
    parse_count_key,
)
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from MainStoinker.engine.run_documents import (
    STALE_AFTER,
    is_live_run,
    live_counts_record,
    liveness,
    open_positions,
    report_document,
    run_summary_record,
)
from MainStoinker.engine.stats import fmt4
from MainStoinker.engine.store import (
    RunData,
    SqliteStore,
    StoredBar,
    open_store_readonly,
)
from tests.test_store import NO_ENV, T0, algos, build_ledger, meta, stored_run

D = Decimal
SNAPSHOTS = Path(__file__).parent / "snapshots"
START = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)
COUNTS = {"symbol": {"AAPL:fed": 3, "MSFT:fed": 2, "AAPL:dropped:stalled": 1,
                     "AAPL:dropped:snapshot_only": 2, "MSFT:late": 1},
          "algo": {"a1": 5}}


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def minute(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def live_ledger() -> Ledger:
    """a1 holds a long that was scaled in and partly sold; a2 a short with no target.

    Each account also has a closed position and a cancelled target.
    """
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 10000)
    a2 = led.open_account("a2", "MSFT", D("2500"))
    # a closed round trip first
    o = a1.order_placed("r0", time=minute(0), side="buy", quantity=3, order_type="market")
    a1.fill("r0", side="buy", quantity=3, price=D("98"), commission=1, time=minute(0),
            reason="entry", order_id=o)
    s0 = a1.order_placed("r0", time=minute(0), side="sell", quantity=3, order_type="stop",
                         price=D("97"))
    a1.fill("r0", side="sell", quantity=3, price=D("97"), commission=1, time=minute(1),
            reason="stop", order_id=s0)
    # the open long
    o = a1.order_placed("r1", time=minute(1), side="buy", quantity=10, order_type="market")
    a1.fill("r1", side="buy", quantity=10, price=D("100"), commission=1, time=minute(1),
            reason="entry", order_id=o)
    stop = a1.order_placed("r1", time=minute(1), side="sell", quantity=10, order_type="stop",
                           price=D("99.5"))
    target = a1.order_placed("r1", time=minute(1), side="sell", quantity=10, order_type="target",
                             price=D("105"))
    a1.order_modified(stop, time=minute(2), price=D("99.75"))
    a1.order_modified(stop, time=minute(3), price=D("100.5"))
    a1.order_modified(target, time=minute(3), price=D("106"))
    a1.fill("r1", side="buy", quantity=10, price=D("102"), commission=1, time=minute(3),
            reason="entry")
    a1.fill("r1", side="sell", quantity=5, price=D("103"), commission=1, time=minute(4),
            reason="algo_exit")
    # the open short; its target was cancelled
    o = a2.order_placed("s1", time=minute(1), side="sell", quantity=4, order_type="market")
    a2.fill("s1", side="sell", quantity=4, price=D("50"), commission=D("1.5"), time=minute(1),
            reason="entry", order_id=o)
    a2.order_placed("s1", time=minute(1), side="buy", quantity=4, order_type="stop",
                    price=D("51"))
    tp = a2.order_placed("s1", time=minute(1), side="buy", quantity=4, order_type="target",
                         price=D("45"))
    a2.order_cancelled(tp, time=minute(2))
    led.mark(minute(5), {"AAPL": D("104.5"), "MSFT": D("49.25")})
    return led


def store_live_run(db: Path, *, clock: Clock | None = None, touch_at: datetime | None = None,
                   led: Ledger | None = None, counts: bool = True,
                   end: tuple[str, str | None] | None = None) -> int:
    clock = clock or Clock()
    led = led or live_ledger()
    with SqliteStore(db, clock=clock) as store:
        run_id = store.begin_run(replace(meta("paper-live"), config={}))
        store.write_algos(run_id, algos())
        store.write_results(run_id, order_events=led.orders, fills=led.fills,
                            round_trips=led.round_trips, equity=led.equity_points)
        store.write_bars(run_id, [
            StoredBar("AAPL", minute(4), 1.0, 1.0, 1.0, 103.0, 10.0),
            StoredBar("AAPL", minute(5), 1.0, 1.0, 1.0, 104.5, 10.0),
            StoredBar("MSFT", minute(5), 1.0, 1.0, 1.0, 49.25, 10.0)])
        if counts:
            store.set_counts(run_id, COUNTS)
            store.update_run_data(run_id, replace(
                meta("paper-live").data,
                gap_rule=GapRuleReport((("AAPL", 2), ("MSFT", 0)), ())))
        if touch_at is not None:
            clock.now = touch_at
            store.touch(run_id)
        if end is not None:
            store.end_run(run_id, end[0], end[1])  # type: ignore[arg-type]
    return run_id


def load(db: Path, run_id: int = 1) -> tuple[RunData, dict[str, StoredBar]]:
    store = open_store_readonly(str(db), env=NO_ENV)
    try:
        return store.load_run(run_id), {b.symbol: b for b in store.load_last_bars(run_id)}
    finally:
        store.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


# --- open positions ----------------------------------------------------------------


def test_open_positions_from_fills_and_modifications(db):
    led = live_ledger()
    store_live_run(db, led=led)
    run, bars = load(db)
    positions = open_positions(run, bars)
    assert positions == [
        {"account": "a1", "symbol": "AAPL", "side": "long", "quantity": "15.0000",
         "entry_price": "101.0000", "entry_time": minute(1).isoformat(),
         "stop": "100.5000", "target": "106.0000",
         "last_close": "104.5000", "unrealised": fmt4(led.unrealised_pnl("a1"))},
        {"account": "a2", "symbol": "MSFT", "side": "short", "quantity": "4.0000",
         "entry_price": "50.0000", "entry_time": minute(1).isoformat(),
         "stop": "51.0000", "target": None,
         "last_close": "49.2500", "unrealised": fmt4(led.unrealised_pnl("a2"))},
    ]
    # the ledger's own view of the same positions
    for shown, held in zip(positions, led.open_positions(), strict=True):
        assert (shown["account"], shown["symbol"], shown["side"]) == (
            held.account, held.symbol, held.side)
        assert shown["quantity"] == fmt4(held.quantity)
        assert shown["entry_price"] == fmt4(held.average_price)
    # 15 shares at 104.5 against a cost of 1515, less 2.25 commission still pending
    assert D(positions[0]["unrealised"]) == D("50.2500")


def test_open_positions_omit_the_mark_without_a_bar(db):
    store_live_run(db)
    run, _bars = load(db)
    only_aapl = {"AAPL": StoredBar("AAPL", minute(5), 1.0, 1.0, 1.0, 104.5, 10.0)}
    for given in (None, {}):
        for position in open_positions(run, given):
            assert "last_close" not in position and "unrealised" not in position
    marked, unmarked = open_positions(run, only_aapl)
    assert "unrealised" in marked and "unrealised" not in unmarked


def test_open_positions_follow_the_last_stop_and_a_cancelled_stop(db):
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 1000)
    o = a1.order_placed("x", time=minute(0), side="buy", quantity=1, order_type="market")
    a1.fill("x", side="buy", quantity=1, price=D("10"), commission=0, time=minute(0),
            reason="entry", order_id=o)
    stop = a1.order_placed("x", time=minute(0), side="sell", quantity=1, order_type="stop",
                           price=D("9"))
    a1.order_modified(stop, time=minute(1), price=D("9.5"))
    store_live_run(db, led=led, counts=False)
    run, _ = load(db)
    assert open_positions(run)[0]["stop"] == "9.5000"
    a1.order_cancelled(stop, time=minute(2))
    other = db.with_name("other.db")
    store_live_run(other, led=led, counts=False)
    run, _ = load(other)
    assert open_positions(run)[0]["stop"] is None


def test_a_backtest_closed_by_synthetic_fills_has_no_open_positions(db):
    led = build_ledger()
    assert any(f.synthetic for f in led.fills) and not led.open_positions()
    with SqliteStore(db) as store:
        stored_run(store, led)
    run, _ = load(db)
    assert open_positions(run) == []


def test_a_fully_closed_position_leaves_the_list_and_a_reentry_is_new(db):
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 1000)
    for ref, t in (("x", 0), ("y", 5)):
        a1.fill(ref, side="buy", quantity=2, price=D("10"), commission=0, time=minute(t),
                reason="entry")
        if ref == "x":
            a1.fill(ref, side="sell", quantity=2, price=D("11"), commission=0,
                    time=minute(1), reason="target")
    store_live_run(db, led=led, counts=False)
    run, _ = load(db)
    (position,) = open_positions(run)
    assert position["entry_time"] == minute(5).isoformat()


# --- liveness ----------------------------------------------------------------------


def test_stale_after_threshold_running_before(db):
    last = START + timedelta(minutes=5)
    store_live_run(db, touch_at=last)
    run, _ = load(db)
    summary = run.summary
    assert summary.status == "running" and summary.last_write_at == last
    assert liveness(summary, last) == "running"
    assert liveness(summary, last + STALE_AFTER - timedelta(microseconds=1)) == "running"
    assert liveness(summary, last + STALE_AFTER) == "running"  # not older than the threshold
    assert liveness(summary, last + STALE_AFTER + timedelta(microseconds=1)) == "stale"
    assert liveness(summary, last - timedelta(hours=1)) == "running"  # a clock behind the writer
    assert liveness(summary, last + timedelta(seconds=30), timedelta(seconds=10)) == "stale"
    assert liveness(summary, last + timedelta(seconds=30), timedelta(minutes=5)) == "running"
    with pytest.raises(ValueError):
        liveness(summary, datetime(2025, 3, 3, 16, 0))  # naive "now"
    after = last + STALE_AFTER + timedelta(seconds=1)
    assert run_summary_record(summary, run, now=last)["liveness"] == "running"
    assert run_summary_record(summary, run, now=after)["liveness"] == "stale"
    assert run_summary_record(summary, run, now=after, stale_after=timedelta(hours=1)
                              )["liveness"] == "running"


def test_a_running_run_without_a_last_write_falls_back_to_its_start(db):
    with SqliteStore(db, clock=Clock()) as store:
        store.begin_run(meta("paper-live"))
    run, _ = load(db)
    assert run.summary.last_write_at is None
    assert liveness(run.summary, START + STALE_AFTER) == "running"
    assert liveness(run.summary, START + STALE_AFTER + timedelta(seconds=1)) == "stale"
    assert run_summary_record(run.summary, run, now=START)["last_write_at"] is None


@pytest.mark.parametrize(("status", "reason"), [("completed", "session_end"),
                                                ("failed", "connection_closed"),
                                                ("failed", None)])
def test_ended_runs_are_completed_or_failed_with_their_end_reason(db, status, reason):
    store_live_run(db, end=(status, reason))
    run, _ = load(db)
    far = START + timedelta(days=30)
    record = run_summary_record(run.summary, run, now=far)
    assert record["liveness"] == status and record["end_reason"] == reason
    assert report_document(run, data_directory="x", now=far)["run"]["end_reason"] == reason


def test_liveness_keys_need_now_but_end_reason_does_not(db):
    store_live_run(db, end=("failed", "error"))
    run, _ = load(db)
    record = run_summary_record(run.summary, run)
    assert "liveness" not in record and record["end_reason"] == "error"


# --- backtest documents unchanged -------------------------------------------------


def capture(*argv: str) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert main(list(argv)) == 0
    return buffer.getvalue()


def test_backtest_documents_unchanged(db):
    """`runs --json` and `report --json` of a stored backtest equal files made by the code
    before issue 193 (tests/snapshots), byte for byte: no key was added to a backtest."""
    times = iter([START, START + timedelta(seconds=2)])
    with SqliteStore(db, clock=lambda: next(times)) as store:
        stored_run(store)
    expected_runs = (SNAPSHOTS / "backtest_runs.json").read_text(encoding="utf-8")
    expected_report = (SNAPSHOTS / "backtest_report.json").read_text(encoding="utf-8")
    assert capture("runs", "--db", str(db), "--json") == expected_runs
    assert capture("report", "1", "--db", str(db), "--json") == expected_report
    # passing "now", counts and bars adds nothing to a backtest either
    run, bars = load(db)
    assert not is_live_run(run.summary)
    far = START + timedelta(days=1)
    document = report_document(run, data_directory="OfflineData", now=far,
                               counts={"symbol": {"AAPL:fed": 1}}, last_bars=bars)
    assert set(document) == {"run", "accounts", "total"}
    assert json.dumps(document, indent=1) + "\n" == expected_report
    assert json.dumps({"runs": [run_summary_record(run.summary, run, now=far)]},
                      indent=1) + "\n" == expected_runs


# --- report ------------------------------------------------------------------------


def report_text(db: Path, now: datetime, capsys, *, as_json: bool = False) -> str:
    capsys.readouterr()
    assert run_report_command(str(db), "1", as_json=as_json, env=NO_ENV, now=now) == 0
    return capsys.readouterr().out


def test_report_shows_live_counts_and_open_positions(db, capsys):
    last = START + timedelta(minutes=5)
    led = live_ledger()
    store_live_run(db, touch_at=last, led=led)
    text = report_text(db, last + timedelta(minutes=1), capsys)
    lines = text.splitlines()
    assert any(ln.startswith("liveness: running (last write 2025-03-03 15:05:00 UTC)")
               for ln in lines)
    assert "minutes fed: AAPL 3, MSFT 2" in lines
    assert "minutes dropped by the watcher: AAPL 3 (seen only in the backfill 2, stalled stream 1)" in lines
    assert "late, not fed: MSFT 1" in lines
    assert any(ln.startswith("bars that follow a gap") and "AAPL 2" in ln for ln in lines)
    assert "open positions:" in lines
    row = next(ln for ln in lines if ln.startswith("a1") and "long" in ln).split()
    assert row[:9] == ["a1", "AAPL", "long", "15.0000", "101.0000", minute(1).isoformat(),
                       "100.5000", "106.0000", "104.5000"]
    assert row[9] == fmt4(led.unrealised_pnl("a1"))
    short = next(ln for ln in lines if ln.startswith("a2") and "short" in ln).split()
    assert short[6:8] == ["51.0000", "-"]

    stale = report_text(db, last + STALE_AFTER + timedelta(seconds=61), capsys)
    assert ("liveness: stale (last write 2025-03-03 15:05:00 UTC, 4m01s ago; "
            "the process may have died)") in stale
    assert "process was killed" not in stale and "process was killed" not in text

    document = json.loads(report_text(db, last, capsys, as_json=True))
    assert document["run"]["liveness"] == "running"
    assert document["run"]["end_reason"] is None
    assert document["run"]["last_write_at"] == last.isoformat()
    assert [p["account"] for p in document["open_positions"]] == ["a1", "a2"]
    assert document["live_counts"] == {
        "fed": {"AAPL": 3, "MSFT": 2},
        "dropped": {"AAPL": {"stalled": 1, "snapshot_only": 2}},
        "late": {"MSFT": 1},
        "gap_rule": {"bars_after_gap": {"AAPL": 2, "MSFT": 0}, "suppressed_entries": 0},
        "other": {"algo": {"a1": 5}},
    }


def test_report_of_an_ended_live_run_shows_the_end_reason_and_no_liveness(db, capsys):
    store_live_run(db, end=("completed", "session_end"))
    text = report_text(db, START + timedelta(days=1), capsys)
    assert "end reason: session_end" in text
    assert "liveness:" not in text


def test_report_is_quiet_when_a_live_run_has_nothing_to_say(db, capsys):
    store_live_run(db, led=Ledger(), counts=False, end=("completed", None))
    text = report_text(db, START, capsys)
    for word in ("liveness", "end reason", "open positions", "minutes fed", "minutes dropped",
                 "late, not fed"):
        assert word not in text
    document = json.loads(report_text(db, START, capsys, as_json=True))
    assert document["open_positions"] == []
    assert document["live_counts"] == {"fed": {}, "dropped": {}, "late": {}, "gap_rule": None}


def test_report_of_a_backtest_prints_no_live_lines(db, capsys):
    with SqliteStore(db) as store:
        stored_run(store)
    text = report_text(db, START, capsys)
    for word in ("liveness", "end reason", "open positions", "minutes fed"):
        assert word not in text


def test_runs_shows_liveness_for_live_runs_only(db, capsys):
    last = START + timedelta(minutes=5)
    store_live_run(db, touch_at=last)
    capsys.readouterr()
    assert run_runs_command(str(db), as_json=False, env=NO_ENV,
                            now=last + STALE_AFTER + timedelta(seconds=1)) == 0
    assert "stale" in capsys.readouterr().out
    assert run_runs_command(str(db), as_json=True, env=NO_ENV, now=last) == 0
    (record,) = json.loads(capsys.readouterr().out)["runs"]
    assert record["liveness"] == "running" and record["incomplete"] is True
    assert record["end_reason"] is None and record["last_write_at"] == last.isoformat()


def test_live_counts_record_keeps_keys_it_does_not_know():
    wrong = {"weird": 1, "AAPL:dropped:a:b": 1, "AAPL:fed:x": 2, "AAPL:dropped": 3,
             "AAPL:other": 4, ":fed": 5, "AAPL:dropped:": 6, "AAPL:late:x": 7}
    record = live_counts_record(
        {"symbol": {"AAPL:fed": 4, **wrong}, "run": {"x": 2}}, None)
    assert record["fed"] == {"AAPL": 4}
    assert record["dropped"] == {} and record["late"] == {}
    assert record["other"] == {"symbol": wrong, "run": {"x": 2}}  # nothing is lost
    assert record["gap_rule"] is None


def test_count_keys_round_trip_through_the_store(db):
    keys = [count_key("AAPL", COUNT_FED), count_key("BRK.B", COUNT_FED),
            count_key("BRK.B", COUNT_LATE), count_key("AAPL", COUNT_DROPPED, "stalled"),
            count_key("BRK.B", COUNT_DROPPED, "snapshot_only")]
    assert keys[1] == "BRK.B:fed" and keys[4] == "BRK.B:dropped:snapshot_only"
    for key in keys:
        parsed = parse_count_key(COUNT_SCOPE_SYMBOL, key)
        assert parsed is not None and count_key(*parsed) == key
    with SqliteStore(db) as store:
        run_id = store.begin_run(meta("paper-live"))
        store.write_algos(run_id, algos())
        store.set_counts(run_id, {COUNT_SCOPE_SYMBOL: {k: i + 1 for i, k in enumerate(keys)}})
        loaded = store.load_counts(run_id)
    record = live_counts_record(loaded, None)
    assert record["fed"] == {"AAPL": 1, "BRK.B": 2}
    assert record["late"] == {"BRK.B": 3}
    assert record["dropped"] == {"AAPL": {"stalled": 4}, "BRK.B": {"snapshot_only": 5}}
    assert "other" not in record


def test_count_key_refuses_what_it_cannot_parse():
    for args in (("A:B", COUNT_FED), ("", COUNT_FED), ("A", "bogus"), ("A", COUNT_DROPPED),
                 ("A", COUNT_FED, "r"), ("A", COUNT_DROPPED, "a:b"), ("A", COUNT_DROPPED, "")):
        with pytest.raises(ValueError):
            count_key(*args)
    assert parse_count_key("algo", "a1") is None
    assert parse_count_key("algo", "AAPL:fed") is None


def test_stale_after_is_a_multiple_of_the_heartbeat():
    assert STALE_AFTER >= 3 * HEARTBEAT_INTERVAL
    assert STALE_AFTER == 6 * HEARTBEAT_INTERVAL == timedelta(seconds=180)
    assert set(LIVE_KINDS) == {"paper-live", "live"}


def test_a_run_of_another_kind_is_live_when_a_live_column_is_set(db):
    for name, end_reason in (("a.db", None), ("b.db", "session_end")):
        path = db.with_name(name)
        with SqliteStore(path, clock=Clock()) as store:
            run_id = store.begin_run(meta("backtest"))
            store.write_algos(run_id, algos())
            if end_reason is None:
                store.touch(run_id)
            else:
                store.end_run(run_id, "completed", end_reason)
        run, _ = load(path)
        assert run.summary.kind == "backtest" and is_live_run(run.summary)
        assert "liveness" in run_summary_record(run.summary, run, now=START)
    plain = db.with_name("c.db")
    with SqliteStore(plain, clock=Clock()) as store:
        store.begin_run(meta("backtest"))
    run, _ = load(plain)
    assert not is_live_run(run.summary)
    live = db.with_name("d.db")
    with SqliteStore(live, clock=Clock()) as store:
        store.begin_run(meta("live"))
    run, _ = load(live)
    assert is_live_run(run.summary)


def test_a_reused_broker_reference_starts_afresh(db):
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 1000)
    o = a1.order_placed("x", time=minute(0), side="buy", quantity=1, order_type="market")
    a1.fill("x", side="buy", quantity=1, price=D("10"), commission=0, time=minute(0),
            reason="entry", order_id=o)
    stop = a1.order_placed("x", time=minute(0), side="sell", quantity=1, order_type="stop",
                           price=D("9"))
    a1.fill("x", side="sell", quantity=1, price=D("9"), commission=0, time=minute(1),
            reason="stop", order_id=stop)
    a1.fill("x", side="buy", quantity=1, price=D("10"), commission=0, time=minute(5),
            reason="entry")
    store_live_run(db, led=led, counts=False)
    run, _ = load(db)
    (position,) = open_positions(run)
    assert position["entry_time"] == minute(5).isoformat()
    assert position["stop"] is None and position["target"] is None


# --- protective orders of a real PaperBroker session ---------------------------------


def pb_bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    stamp = pd.Timestamp(minute(i))
    return Bar(stamp, int(stamp.timestamp()), o, h, low, c, 1000)


def pb_time(i: int) -> pd.Timestamp:
    return pd.Timestamp(minute(i))


def check_against_end_live(tmp_path: Path, led: Ledger, name: str) -> list[dict]:
    """Store the ledger through the real writers, read it back, compare with `end_live`'s logic."""
    path = tmp_path / f"{name}.db"
    store_live_run(path, led=led, counts=False)
    run, _ = load(path)
    shown = open_positions(run)
    held = Engine._open_positions(SimpleNamespace(_ledger=led))  # what end_live returns
    assert len(shown) == len(held)
    for doc, pos in zip(shown, held, strict=True):
        assert (doc["account"], doc["symbol"], doc["side"], doc["quantity"], doc["entry_price"],
                doc["entry_time"]) == (pos.account, pos.symbol, pos.side, fmt4(pos.quantity),
                                       fmt4(pos.entry_price), pos.entry_time.isoformat())
        assert doc["stop"] == (None if pos.stop is None else fmt4(pos.stop))
        assert doc["target"] == (None if pos.target is None else fmt4(pos.target))
    return shown


def test_a_paper_broker_stop_placed_with_the_entry_order_is_shown(tmp_path):
    led = Ledger()
    b1 = PaperBroker(led.open_account("a1", "AAPL", 10000), CostModel())
    b2 = PaperBroker(led.open_account("a2", "MSFT", 10000), CostModel())
    r1 = b1.open_position(symbol="AAPL", volume=10, direction=1, price=100, time=pb_time(0),
                          limit=False, stop_price=99.0, oca_group="g")
    r2 = b2.open_position(symbol="MSFT", volume=7, direction=0, price=50, time=pb_time(0),
                          limit=False, stop_price=51.0, oca_group="g")
    assert check_against_end_live(tmp_path, led, "pending") == []  # not filled yet
    b1.on_bar("AAPL", pb_bar(1, 100.1, 100.5, 99.9, 100.3))
    b2.on_bar("MSFT", pb_bar(1, 50.0, 50.2, 49.8, 49.9))
    first = check_against_end_live(tmp_path, led, "entered")
    assert [(p["stop"], p["target"]) for p in first] == [("99.0000", None), ("51.0000", None)]

    b1.modify_stop(r1, 99.8)
    second = check_against_end_live(tmp_path, led, "modified")
    assert second[0]["stop"] == "99.8000"

    b1.set_take_profit(r1, 103.0, 10)
    b2.set_take_profit(r2, 48.0, 7)
    third = check_against_end_live(tmp_path, led, "target")
    assert [p["target"] for p in third] == ["103.0000", "48.0000"]

    b2.close_position(r2, price=49.9, time=pb_time(2))  # cancels the stop and the target
    fourth = check_against_end_live(tmp_path, led, "closing")
    assert (fourth[1]["stop"], fourth[1]["target"]) == (None, None)
    assert (fourth[0]["stop"], fourth[0]["target"]) == ("99.8000", "103.0000")


def test_a_reused_reference_with_its_stop_placed_before_the_new_entry_fill(db):
    led = Ledger()
    a1 = led.open_account("a1", "AAPL", 1000)
    a1.order_placed("x", time=minute(0), side="buy", quantity=1, order_type="entry")
    stop = a1.order_placed("x", time=minute(0), side="sell", quantity=1, order_type="stop",
                           price=D("9"))
    a1.fill("x", side="buy", quantity=1, price=D("10"), commission=0, time=minute(1),
            reason="entry")
    a1.fill("x", side="sell", quantity=1, price=D("9"), commission=0, time=minute(2),
            reason="stop", order_id=stop)
    # the algo reopens on the bar the stop was hit: its stop is recorded at that time,
    # one bar before the new fill
    new = a1.order_placed("x", time=minute(2), side="sell", quantity=1, order_type="stop",
                          price=D("8"))
    a1.fill("x", side="buy", quantity=1, price=D("11"), commission=0, time=minute(3),
            reason="entry")
    a1.order_modified(new, time=minute(4), price=D("8.5"))
    a1.order_modified(stop, time=minute(4), price=D("1"))  # an old order: ignored
    store_live_run(db, led=led, counts=False)
    run, _ = load(db)
    (position,) = open_positions(run)
    assert position["entry_time"] == minute(3).isoformat()
    assert position["stop"] == "8.5000"


# --- drop reasons ------------------------------------------------------------------


def test_drop_reasons_are_a_fixed_vocabulary_with_labels():
    assert DROP_REASONS == ("snapshot_only", "connection_lost", "farm_break", "stalled",
                            "rerequest_gap", "previous_session")
    assert set(DROP_REASON_LABELS) == set(DROP_REASONS)
    for reason in DROP_REASONS:
        assert parse_count_key(COUNT_SCOPE_SYMBOL, count_key("AAPL", COUNT_DROPPED, reason)
                               ) == ("AAPL", COUNT_DROPPED, reason)
    for bad in ("backfill_only", "cannot resolve", "STALLED", "lost_not_written"):
        with pytest.raises(ValueError):
            count_key("AAPL", COUNT_DROPPED, bad)


def test_report_prints_a_label_for_every_drop_reason(db, capsys):
    counts = {COUNT_SCOPE_SYMBOL: {count_key("AAPL", COUNT_DROPPED, r): i + 1
                                   for i, r in enumerate(DROP_REASONS)}}
    with SqliteStore(db, clock=Clock()) as store:
        run_id = store.begin_run(meta("paper-live"))
        store.write_algos(run_id, algos())
        store.set_counts(run_id, counts)
    text = report_text(db, START, capsys)
    line = next(ln for ln in text.splitlines() if ln.startswith("minutes dropped"))
    for i, reason in enumerate(DROP_REASONS):
        assert f"{DROP_REASON_LABELS[reason]} {i + 1}" in line
    assert "_" not in line
