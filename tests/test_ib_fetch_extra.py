"""Tester's extra checks for `MainStoinker/ib/fetch.py` (T12), over the same fake-client rig.

Every scenario here is also checked against an INDEPENDENT pacing audit (`assert_paced`) of
the request times on the fake clock, against the files on disk (byte-equal to an
uninterrupted run, no stray files) and against the manifest. Nothing sleeps for real and
nothing is asserted about the wall clock.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from MainStoinker.engine import feed as feed_module
from MainStoinker.engine.cli import main as cli_main
from MainStoinker.engine.feed import month_file_path, read_symbol_bars, write_month_file
from MainStoinker.ib import fetch as fetch_module
from MainStoinker.ib.connection import DATA_REQUESTS
from MainStoinker.ib.fetch import classify_162
from MainStoinker.ib.history import manifest_path
from tests.fake_ib import make_bar
from tests.test_ib_fetch import (
    CON_IDS,
    NO_DATA_TEXT,
    NY,
    PACING_TEXT,
    Played,
    SimClock,
    bars_played,
    day_rows,
    empty_end,
    end_played,
    error_behaviour,
    error_played,
    full_day,
    holiday_behaviour,
    make_rig,
    manifest_days,
    month_bytes,
    weekdays,
)

ALLOWED_FILES = re.compile(
    r"(AAPL|MSFT|TSLA)_[0-9]{4}-[0-9]{2}\.csv|manifest\.json|gaps\.csv|gaps_summary\.txt")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "bars"


# -- an independent audit of the request times -----------------------------------------------


def assert_paced(requests: list[tuple[int, date, float]], *, min_gap: float = 10.0) -> None:
    """IB's documented limits, checked on the times the fake saw (not through `Pacer`)."""
    eps = 1e-6
    sent = sorted(requests, key=lambda r: r[2])
    times = [t for _, _, t in sent]
    for a, b in zip(times, times[1:], strict=False):
        assert b - a >= min_gap - eps, f"two requests {b - a:.3f} s apart"
    for t in times:  # at most 60 in any 600 s window ending at a request
        assert sum(1 for u in times if t - 600.0 < u <= t) <= 60
    by_contract: dict[int, list[float]] = defaultdict(list)
    by_request: dict[tuple[int, date], list[float]] = defaultdict(list)
    for con_id, day, t in sent:
        by_contract[con_id].append(t)
        by_request[(con_id, day)].append(t)
    for stamps in by_contract.values():  # fewer than 6 per contract in 2 s
        for t in stamps:
            assert sum(1 for u in stamps if t - 2.0 < u <= t) < 6
    for stamps in by_request.values():  # no identical request within 15 s
        for a, b in zip(stamps, stamps[1:], strict=False):
            assert b - a >= 15.0 - eps, f"identical request again after {b - a:.3f} s"


def assert_no_stray_files(out: Path) -> None:
    for path in out.rglob("*"):
        if path.is_file():
            assert ALLOWED_FILES.fullmatch(path.name), f"unexpected file {path}"


def assert_ok_days_on_disk(out: Path, symbol: str) -> None:
    """Every day the manifest calls `ok` has exactly its recorded number of bars in the files."""
    path = manifest_path(out, symbol)
    if not path.is_file():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        frame = read_symbol_bars(out, symbol)
        per_day = frame["date"].dt.date.value_counts().to_dict()
    except FileNotFoundError:
        per_day = {}
    for key, record in raw["days"].items():
        if record["status"] == "ok":
            assert per_day.get(date.fromisoformat(key), 0) == record["bars"], key
        else:
            assert date.fromisoformat(key) not in per_day or record["status"] == "failed", key


def short_day(minutes: int = 20) -> Any:
    def behaviour(req_id: int, day: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, day_rows(day, minutes, symbol)), end_played(req_id)]

    return behaviour


def script_range(rig: Any, start: date, end: date, behaviour: Any, *, skip: tuple[date, ...] = ()) -> None:
    for day in weekdays(start, end):
        if day not in skip:
            rig.script(day, behaviour)


def after(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def summarise(out: Path, symbols: list[str]) -> dict[str, Any]:
    """Everything that a resumed run must equal: month files, gap files, manifest statuses."""
    summary: dict[str, Any] = {}
    for symbol in symbols:
        raw = json.loads((out / symbol / "manifest.json").read_text(encoding="utf-8"))
        summary[symbol] = {
            "files": month_bytes(out, symbol),
            "gaps": (out / symbol / "gaps.csv").read_bytes(),
            "days": {k: (v["status"], v["bars"]) for k, v in raw["days"].items()},
        }
    return summary


# -- clean multi-symbol, multi-month run across a year end -----------------------------------

YE_START, YE_END = date(2024, 11, 18), date(2025, 1, 17)
YE_HOLIDAYS = {date(2024, 11, 28): date(2024, 11, 27), date(2024, 12, 25): date(2024, 12, 24),
               date(2025, 1, 1): date(2024, 12, 31), date(2025, 1, 9): date(2025, 1, 8)}


def test_clean_run_three_symbols_three_months_across_a_year_end(out: Path) -> None:
    symbols = ["AAPL", "MSFT", "TSLA"]
    with make_rig(server_now=after(date(2025, 1, 21))) as rig:
        script_range(rig, YE_START, YE_END, short_day(), skip=tuple(YE_HOLIDAYS))
        for holiday, previous in YE_HOLIDAYS.items():
            rig.script(holiday, holiday_behaviour(previous))
        result = rig.run(symbols, YE_START, YE_END, out)
    days = weekdays(YE_START, YE_END)
    assert result.complete and result.stop_reason is None and result.fatal is None
    assert result.estimate is not None and result.estimate.requests == 3 * len(days)
    assert len(rig.requests) == 3 * len(days)
    assert_paced(rig.requests)
    for symbol in symbols:
        sym = result.symbols[symbol]
        assert (sym.ok, sym.no_data, sym.failed) == (len(days) - 4, 4, 0)
        names = sorted(p.name for p in (out / symbol).glob(f"{symbol}_*.csv"))
        assert names == [f"{symbol}_2024-11.csv", f"{symbol}_2024-12.csv", f"{symbol}_2025-01.csv"]
        frame = read_symbol_bars(out, symbol)  # the engine's own reader accepts what was written
        assert frame["date"].is_monotonic_increasing and not frame["date"].duplicated().any()
        assert len(frame) == (len(days) - 4) * 20
        stored_days = set(frame["date"].dt.date)
        assert not stored_days & set(YE_HOLIDAYS)  # IB's previous-session answer never stored twice
        manifest = manifest_days(out, symbol)
        assert [manifest[d.isoformat()] for d in sorted(YE_HOLIDAYS)] == ["no_data"] * 4
    assert_no_stray_files(out)
    sent = {c.name for c in rig.fake.calls} - {"connect", "disconnect", "run"}
    assert sent <= DATA_REQUESTS, sent - DATA_REQUESTS
    assert len(rig.fake.calls_named("cancelHistoricalData")) <= len(rig.requests)
    # progress is enough to see where the run is: one `day` event per request, in order
    day_events = [e for e in rig.events if e.kind == "day"]
    assert len(day_events) == 3 * len(days)
    assert [e.symbol for e in day_events[: len(days)]] == ["AAPL"] * len(days)


# -- resume after every kind of stop ---------------------------------------------------------

RS_SYMBOLS = ["AAPL", "MSFT"]
RS_START, RS_END = date(2024, 8, 28), date(2024, 9, 6)
RS_HOLIDAY = date(2024, 9, 2)
RS_SERVER = after(date(2024, 9, 10))


def scripted_rig_run(rig: Any, out: Path, **kw: Any) -> Any:
    script_range(rig, RS_START, RS_END, short_day(), skip=(RS_HOLIDAY,))
    rig.script(RS_HOLIDAY, holiday_behaviour(date(2024, 8, 30)))
    return rig.run(RS_SYMBOLS, RS_START, RS_END, out, **kw)


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("uninterrupted") / "bars"
    with make_rig(server_now=RS_SERVER) as rig:
        result = scripted_rig_run(rig, out)
    assert result.complete
    assert_paced(rig.requests)
    return summarise(out, RS_SYMBOLS)


def done_days(out: Path, symbol: str) -> set[date]:
    path = manifest_path(out, symbol)
    if not path.is_file():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {date.fromisoformat(k) for k, v in raw["days"].items() if v["status"] in ("ok", "no_data")}


KINDS = ["closed", "lost_1100_timeout", "interrupt_in_pacing_sleep", "interrupt_in_wait",
         "crash_in_write_month_file", "crash_in_save_manifest", "silent_timeout", "partial_then_silence"]
TARGET_DAY = date(2024, 9, 3)  # the day (of AAPL) the first run goes wrong on; MSFT never started


@pytest.mark.parametrize("kind", KINDS)
def test_resume_after_each_kind_of_stop_equals_an_uninterrupted_run(
    kind: str, out: Path, reference: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    raised: type[BaseException] | None = None
    armed = {"on": False}
    with make_rig(server_now=RS_SERVER) as rig:
        if kind == "closed":
            def breaker(req_id: int, day: date, symbol: str) -> list[Played]:
                rig.fake.drop("connectionClosed")
                return []
            rig.script(TARGET_DAY, breaker, con_id=CON_IDS["AAPL"])
        elif kind == "lost_1100_timeout":
            def breaker(req_id: int, day: date, symbol: str) -> list[Played]:
                rig.fake.drop(1100)
                return []
            rig.script(TARGET_DAY, breaker, con_id=CON_IDS["AAPL"])
        elif kind == "silent_timeout":
            rig.script(TARGET_DAY, lambda r, d, s: [], con_id=CON_IDS["AAPL"])
        elif kind == "partial_then_silence":
            rig.script(TARGET_DAY, lambda r, d, s: bars_played(r, day_rows(d, 20, s)[:7]),
                       con_id=CON_IDS["AAPL"])
        elif kind == "interrupt_in_pacing_sleep":
            raised = KeyboardInterrupt
            slept = {"n": 0}

            def sleep(seconds: float) -> None:
                slept["n"] += 1
                if slept["n"] == 4:
                    raise KeyboardInterrupt
                rig.clock.sleep(seconds)
            monkeypatch.setattr(rig.clock, "sleep", sleep)
        elif kind == "interrupt_in_wait":
            raised = KeyboardInterrupt

            def arm(req_id: int, day: date, symbol: str) -> list[Played]:
                armed["on"] = True
                return [*bars_played(req_id, day_rows(day, 20, symbol)), end_played(req_id)]
            rig.script(TARGET_DAY, arm, con_id=CON_IDS["AAPL"])
            original = SimClock.__call__

            def clock_call(self: SimClock) -> float:
                if armed["on"]:
                    armed["on"] = False
                    raise KeyboardInterrupt
                return original(self)
            monkeypatch.setattr(SimClock, "__call__", clock_call)
        elif kind == "crash_in_write_month_file":
            raised = RuntimeError
            real_write = fetch_module.write_month_file
            count = {"n": 0}

            def write(path: Path, frame: pd.DataFrame, **k: Any) -> None:
                count["n"] += 1
                if count["n"] == 4:
                    raise RuntimeError("disk fell over")
                real_write(path, frame, **k)
            monkeypatch.setattr(fetch_module, "write_month_file", write)
        elif kind == "crash_in_save_manifest":
            raised = RuntimeError
            real_save = fetch_module.save_manifest
            count = {"n": 0}

            def save(path: Path, manifest: Any, **k: Any) -> None:
                count["n"] += 1
                if count["n"] == 4:
                    raise RuntimeError("power failure")
                real_save(path, manifest, **k)
            monkeypatch.setattr(fetch_module, "save_manifest", save)
        if raised is not None:
            with pytest.raises(raised):
                scripted_rig_run(rig, out)
            first = None
        else:
            first = scripted_rig_run(rig, out)
        first_requests = list(rig.requests)
    monkeypatch.undo()
    assert_paced(first_requests)
    if first is not None:
        assert not first.complete
        if kind in ("closed", "lost_1100_timeout"):
            assert first.connection_lost and first.symbols["MSFT"].state == "not_started"
        else:  # a timeout fails the day, the run goes on
            assert not first.connection_lost and first.symbols["AAPL"].failed == 1
            assert first.symbols["MSFT"].state == "done"
        assert manifest_days(out)[TARGET_DAY.isoformat()] == "failed"
        record = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
        assert record["days"][TARGET_DAY.isoformat()]["bars"] == 0
    for symbol in RS_SYMBOLS:  # whatever the manifest calls ok is on disk, bar for bar
        assert_ok_days_on_disk(out, symbol)
    assert_no_stray_files(out)
    left = {s: done_days(out, s) for s in RS_SYMBOLS}

    with make_rig(server_now=RS_SERVER) as rig2:
        second = scripted_rig_run(rig2, out)
    assert second.complete, second.stop_reason
    assert_paced(rig2.requests)
    planned = weekdays(RS_START, RS_END)
    for symbol in RS_SYMBOLS:  # only what was missing is asked for, each day once
        asked = [d for c, d, _ in rig2.requests if c == CON_IDS[symbol]]
        assert sorted(asked) == sorted(set(planned) - left[symbol])
    assert summarise(out, RS_SYMBOLS) == reference
    assert_no_stray_files(out)


# -- single scenarios on one week -------------------------------------------------------------

WEEK = weekdays(date(2024, 8, 12), date(2024, 8, 16))


def test_wire_arguments_and_end_times_across_both_dst_changes(out: Path) -> None:
    """What reached the (fake) wire, written down by hand: UTC end times follow New York's DST."""
    expected_end = {
        date(2024, 3, 8): "20240308-21:00:00",   # EST, UTC-5
        date(2024, 3, 11): "20240311-20:00:00",  # EDT after the change of Sunday 10 March
        date(2024, 11, 1): "20241101-20:00:00",  # EDT
        date(2024, 11, 4): "20241104-21:00:00",  # EST after the change of Sunday 3 November
    }
    with make_rig(server_now=after(date(2024, 11, 20))) as rig:
        rig.script(date(2024, 3, 8), short_day())
        rig.script(date(2024, 3, 11), short_day())
        rig.script(date(2024, 11, 1), short_day())
        rig.script(date(2024, 11, 4), short_day())
        for first, last in ((date(2024, 3, 8), date(2024, 3, 11)), (date(2024, 11, 1), date(2024, 11, 4))):
            assert rig.run(["AAPL"], first, last, out).complete
    calls = rig.fake.calls_named("reqHistoricalData")
    assert [c.args[2] for c in calls] == list(expected_end.values())
    for call in calls:
        _, contract, _, duration, bar_size, what, use_rth, fmt, keep, options = call.args
        assert (duration, bar_size, what, use_rth, fmt, keep, list(options or [])) == (
            "1 D", "1 min", "TRADES", 1, 2, False, [])
        assert (contract.secType, contract.symbol, contract.currency, contract.conId) == (
            "STK", "AAPL", "USD", CON_IDS["AAPL"])
    frame = read_symbol_bars(out, "AAPL")
    first_bars = frame.groupby(frame["date"].dt.date)["date"].min().dt.time.astype(str).unique().tolist()
    assert first_bars == ["09:30:00"]  # every day starts at the open, whatever the UTC offset
    assert len(frame) == 4 * 20


def test_two_runs_in_one_process_share_no_state(tmp_path: Path) -> None:
    """A pacing violation counts per run: the second run may have its own first one and retry."""
    day = date(2024, 8, 13)
    for name in ("one", "two"):
        with make_rig() as rig:
            rig.script(day, error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))
            result = rig.run(["AAPL"], WEEK[0], WEEK[2], tmp_path / name)
        assert result.complete and not result.pacing_stopped, name
        assert rig.days_requested().count(day) == 2
        assert_paced(rig.requests)


@pytest.mark.parametrize("which", ["first_minute", "last_minute"])
def test_a_single_bar_day_is_stored_and_reported_partial(out: Path, which: str) -> None:
    day = date(2024, 8, 14)
    rows = day_rows(day, 390)
    one = [rows[0] if which == "first_minute" else rows[-1]]

    def single(req_id: int, d: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, one), end_played(req_id)]

    with make_rig() as rig:
        rig.script(day, single)
        result = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert result.complete and result.symbols["AAPL"].ok == 4  # `complete` does not look at the gap report
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"][day.isoformat()]["bars"] == 1
    status = {r.day: r.status for r in result.symbols["AAPL"].gaps}
    assert status[day] == "partial"  # reported by the gap report, though the manifest says ok
    assert_ok_days_on_disk(out, "AAPL")


@pytest.mark.parametrize("how", ["closed", "lost"])
def test_connection_loss_on_the_very_last_request_still_stops_the_run(out: Path, how: str) -> None:
    last = WEEK[1]

    def breaker(req_id: int, day: date, symbol: str) -> list[Played]:
        rig.fake.drop("connectionClosed" if how == "closed" else 1100)
        return []

    with make_rig() as rig:
        rig.script(last, breaker)
        result = rig.run(["AAPL"], WEEK[0], last, out)
    assert result.connection_lost and result.stop_reason is not None and not result.complete
    assert result.symbols["AAPL"].state == "stopped" and result.symbols["AAPL"].failed == 1
    assert manifest_days(out) == {WEEK[0].isoformat(): "ok", last.isoformat(): "failed"}
    assert any(e.kind == "stopped" for e in rig.events)  # a person watching sees where it stopped


@pytest.mark.parametrize("how", ["closed", "lost"])
def test_connection_lost_between_two_requests_sends_nothing_more(out: Path, how: str) -> None:
    """The answer to day 2 is complete, then the link goes: day 3 must not be sent into the void."""
    def answer_then_break(req_id: int, day: date, symbol: str) -> list[Played]:
        played = full_day(req_id, day, symbol)
        rig.fake.drop("connectionClosed" if how == "closed" else 1100)
        return played

    with make_rig() as rig:
        rig.script(WEEK[1], answer_then_break)
        result = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert result.connection_lost and not result.complete
    assert rig.days_requested() == [WEEK[0], WEEK[1]]
    assert manifest_days(out)[WEEK[1].isoformat()] in ("ok", "failed")
    assert_ok_days_on_disk(out, "AAPL")


def test_early_close_day_is_ok_with_210_bars_and_reported_short(out: Path) -> None:
    half = date(2024, 11, 29)

    def early(req_id: int, day: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, day_rows(day, 210, symbol)), end_played(req_id)]

    with make_rig(server_now=after(date(2024, 12, 3))) as rig:
        rig.script(date(2024, 11, 28), holiday_behaviour(date(2024, 11, 27)))
        rig.script(half, early)
        result = rig.run(["AAPL"], date(2024, 11, 27), half, out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (2, 1, 0)
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"][half.isoformat()]["bars"] == 210
    assert [r.status for r in sym.gaps] == ["complete", "no_data", "short"]
    assert "AAPL,2024-11-29,short,390,210,180,13:00-15:59" in (out / "AAPL" / "gaps.csv").read_text(
        encoding="utf-8")
    assert_paced(rig.requests)


@pytest.mark.parametrize("text, expected", [
    (NO_DATA_TEXT, "failed"),
    ("Historical Market Data Service error message:HMDS query returned no data: AAPL@SMART Trades",
     "failed"),
])
def test_bars_then_error_162_is_failed_and_stores_nothing(out: Path, text: str, expected: str) -> None:
    day = date(2024, 8, 13)

    def bars_then_error(req_id: int, d: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, day_rows(d, 100, symbol)), error_played(req_id, 162, text)]

    with make_rig() as rig:
        rig.script(day, bars_then_error)
        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert result.symbols["AAPL"].failed == 1 and result.symbols["AAPL"].ok == 2
    assert manifest_days(out)[day.isoformat()] == expected
    frame = read_symbol_bars(out, "AAPL")
    assert day not in set(frame["date"].dt.date)  # none of the 100 bars leaked into the file
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"][day.isoformat()]["error_code"] == 162
    assert raw["days"][day.isoformat()]["error_text"] == text
    assert_paced(rig.requests)


def test_partial_bars_then_pacing_violation_then_success_stores_the_day_once(out: Path) -> None:
    day = date(2024, 8, 13)

    def partial_then_pacing(req_id: int, d: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, day_rows(d, 50, symbol)), error_played(req_id, 162, PACING_TEXT)]

    with make_rig() as rig:
        rig.script(day, partial_then_pacing, lambda r, d, s: full_day(r, d, s))
        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert result.complete and not result.pacing_stopped
    frame = read_symbol_bars(out, "AAPL")
    assert (frame["date"].dt.date == day).sum() == 390  # the 50 early bars were discarded, not merged
    first, second = [t for _, d, t in rig.requests if d == day]
    assert second - first >= 600.0
    assert_paced(rig.requests)


@pytest.mark.parametrize("text", ["", "Something IB never said before", "Historical Market Data Service "
                                  "error message:No historical market data for AAPL/STK@SMART Last 1min"])
def test_unknown_162_text_is_failed_never_no_data_and_the_run_goes_on(out: Path, text: str) -> None:
    day = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(day, error_behaviour(162, text))
        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (2, 0, 1) and not result.pacing_stopped
    assert manifest_days(out)[day.isoformat()] == "failed"
    assert rig.days_requested().count(day) == 1


@pytest.mark.parametrize("text, kind", [
    ("Historical Market Data Service error message:Historical data request pacing violation", "pacing"),
    ("HISTORICAL DATA REQUEST PACING VIOLATION", "pacing"),
    ("Historical Market Data Service query message:HMDS query returned no data: AAPL@SMART Trades",
     "no_data"),
    ("hmds query RETURNED NO DATA", "no_data"),
    ("", "other"),
    ("No historical market data for AAPL/STK@SMART Last 1min", "other"),
    ("Max number of simultaneous historical data requests exceeded", "other"),
    ("Historical Market Data Service error message:No market data permissions for NASDAQ STK", "other"),
    ("Historical Market Data Service error message:Trading TWS session is connected from a "
     "different IP address", "other"),
])
def test_classify_162_documented_texts(text: str, kind: str) -> None:
    assert classify_162(text) == kind


def test_a_stale_or_general_error_does_not_fail_the_day_in_progress(out: Path) -> None:
    day = date(2024, 8, 13)

    def noisy(req_id: int, d: date, symbol: str) -> list[Played]:
        return [error_played(-1, 2104, "Market data farm connection is OK:usfarm"),
                error_played(987654, 354, "an answer for an id nobody asked about"),
                *full_day(req_id, d, symbol)]

    with make_rig() as rig:
        rig.script(day, noisy)
        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert result.complete and result.symbols["AAPL"].ok == 3
    assert not result.symbols["AAPL"].failures


@pytest.mark.parametrize("what", ["minus_one", "fractional_volume", "off_the_minute", "zero_price",
                                  "high_below_low"])
def test_bad_bar_in_the_middle_fails_the_day_and_a_rerun_repairs_it(out: Path, what: str) -> None:
    day = date(2024, 8, 14)

    def broken(req_id: int, d: date, symbol: str) -> list[Played]:
        rows = day_rows(d, 390, symbol)
        bars = bars_played(req_id, rows)
        t, o, h, low, c, v = rows[200]
        replaced = {
            "minus_one": make_bar(t, -1, -1, -1, -1, -1),
            "fractional_volume": make_bar(t, o, h, low, c, Decimal("12.5")),
            "off_the_minute": make_bar(t + 30, o, h, low, c, v),
            "zero_price": make_bar(t, 0.0, h, low, c, v),
            "high_below_low": make_bar(t, o, low - 5, h + 5, c, v),
        }[what]
        bars[200] = ("historicalData", req_id, replaced)
        return [*bars, end_played(req_id)]

    with make_rig() as rig:
        rig.script(day, broken)
        first = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    sym = first.symbols["AAPL"]
    assert (sym.ok, sym.failed) == (3, 1) and sym.failures[0].reason == "bad_bar"
    assert day not in set(read_symbol_bars(out, "AAPL")["date"].dt.date)  # no prefix of the day stored
    assert manifest_days(out)[day.isoformat()] == "failed"
    with make_rig() as rig2:
        second = rig2.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert second.complete and rig2.days_requested() == [day]
    assert len(read_symbol_bars(out, "AAPL")) == 4 * 390
    assert_no_stray_files(out)


@pytest.mark.parametrize("what", ["duplicate", "unsorted"])
def test_duplicate_or_unsorted_bars_fail_the_day_and_store_nothing(out: Path, what: str) -> None:
    day = date(2024, 8, 14)

    def odd(req_id: int, d: date, symbol: str) -> list[Played]:
        rows = day_rows(d, 390, symbol)
        rows = rows[:100] + [rows[99]] + rows[100:] if what == "duplicate" else (
            rows[:100] + [rows[101], rows[100]] + rows[102:])
        return [*bars_played(req_id, rows), end_played(req_id)]

    with make_rig() as rig:
        rig.script(day, odd)
        result = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert result.symbols["AAPL"].failed == 1 and result.symbols["AAPL"].ok == 3
    assert day not in set(read_symbol_bars(out, "AAPL")["date"].dt.date)
    assert manifest_days(out)[day.isoformat()] == "failed"


def test_bars_of_another_day_mixed_in_are_dropped(out: Path) -> None:
    day = date(2024, 8, 14)

    def mixed(req_id: int, d: date, symbol: str) -> list[Played]:
        before = day_rows(date(2024, 8, 13), 390, symbol)[-5:]
        return [*bars_played(req_id, before), *full_day(req_id, d, symbol)]

    with make_rig() as rig:
        rig.script(day, mixed)
        result = rig.run(["AAPL"], day, day, out)
    assert result.complete and result.symbols["AAPL"].ok == 1
    frame = read_symbol_bars(out, "AAPL")
    assert set(frame["date"].dt.date) == {day} and len(frame) == 390


def test_the_close_boundary_on_the_servers_clock(out: Path, tmp_path: Path) -> None:
    day = date(2024, 8, 20)
    just_closed = datetime(2024, 8, 20, 20, 0, 0, tzinfo=UTC)  # 16:00:00 New York
    with make_rig(server_now=just_closed) as rig:
        result = rig.run(["AAPL"], day, day, out)
    assert result.complete and result.symbols["AAPL"].ok == 1
    assert len(read_symbol_bars(out, "AAPL")) == 390  # the 15:59 bar had ended: it is kept
    before = datetime(2024, 8, 20, 19, 59, 59, tzinfo=UTC)
    with make_rig(server_now=before) as rig2:
        result2 = rig2.run(["AAPL"], date(2024, 8, 19), day, tmp_path / "early")
    assert result2.not_closed == [day] and rig2.days_requested() == [date(2024, 8, 19)]
    assert not result2.complete or result2.not_closed  # reported, never silent
    kinds = [e.kind for e in rig2.events]
    assert "not_closed" in kinds


def test_a_range_of_only_a_weekend_requests_nothing(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL"], date(2024, 8, 17), date(2024, 8, 18), out)
    assert rig.requests == [] and result.fatal is None
    assert result.estimate is not None and result.estimate.requests == 0


def test_truncated_manifest_stops_that_symbol_and_is_set_aside_not_deleted(out: Path) -> None:
    with make_rig() as rig:
        assert rig.run(["AAPL", "MSFT"], WEEK[0], WEEK[1], out).complete
    path = manifest_path(out, "AAPL")
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: len(whole) // 2], encoding="utf-8")
    with make_rig() as rig2:
        result = rig2.run(["AAPL", "MSFT"], WEEK[0], WEEK[2], out)
    assert result.symbols["AAPL"].state == "manifest" and result.symbols["AAPL"].message
    assert result.symbols["MSFT"].state == "done" and result.symbols["MSFT"].ok == 1
    assert rig2.days_requested(CON_IDS["AAPL"]) == []
    assert any(p.name.startswith("manifest.json") and p.name.endswith(".unusable")
               for p in (out / "AAPL").iterdir())  # kept for the owner to look at
    assert len(read_symbol_bars(out, "AAPL")) == 2 * 390  # its bars are untouched


@pytest.mark.parametrize("damage", ["garbage", "empty", "header_only", "duplicate_rows", "out_of_order"])
def test_a_hand_damaged_month_file_fails_the_first_day_stops_the_symbol_and_is_left_alone(
        out: Path, damage: str) -> None:
    with make_rig() as rig:
        assert rig.run(["AAPL"], WEEK[0], WEEK[0], out).complete
    path = month_file_path(out, "AAPL", 2024, 8)
    good = path.read_text(encoding="utf-8")
    lines = good.splitlines()
    bad = {
        "garbage": "this is not a csv\x00\x01 at all\n",
        "empty": "",
        "header_only": lines[0] + "\n",
        "duplicate_rows": "\n".join([*lines, lines[-1]]) + "\n",
        "out_of_order": "\n".join([lines[0], lines[2], lines[1], *lines[3:]]) + "\n",
    }[damage]
    path.write_text(bad, encoding="utf-8", newline="")
    with make_rig() as rig2:
        result = rig2.run(["AAPL"], WEEK[0], WEEK[2], out)  # does not raise
    sym = result.symbols["AAPL"]
    # adapted: the damage is found before the first request, so no request is sent at all
    # (it was one request, and before that both remaining days failed)
    assert (sym.ok, sym.failed, sym.state) == (0, 1, "stopped")
    assert {f.reason for f in sym.failures} == {"damaged_file"}
    assert rig2.days_requested() == []
    assert path.read_text(encoding="utf-8") == bad  # left exactly as the owner's hand left it
    assert manifest_days(out) == {WEEK[0].isoformat(): "ok", WEEK[1].isoformat(): "failed"}
    assert_no_stray_files(out)
    assert_paced(rig2.requests)


def test_days_outside_the_range_in_an_existing_month_file_survive(out: Path) -> None:
    other = [day_rows(d, 390) for d in (date(2024, 8, 1), date(2024, 8, 2))]
    stamps = [datetime.fromtimestamp(r[0], NY).replace(tzinfo=None) for rows in other for r in rows]
    frame = pd.DataFrame({
        "date": pd.to_datetime(stamps),
        "time": [r[0] for rows in other for r in rows],
        "open": [r[1] for rows in other for r in rows], "high": [r[2] for rows in other for r in rows],
        "low": [r[3] for rows in other for r in rows], "close": [r[4] for rows in other for r in rows],
        "volume": [r[5] for rows in other for r in rows],
    })[list(feed_module.COLUMNS)]
    path = month_file_path(out, "AAPL", 2024, 8)
    write_month_file(path, frame)
    before = path.read_text(encoding="utf-8").splitlines()
    with make_rig() as rig:
        result = rig.run(["AAPL"], WEEK[0], WEEK[1], out)
    assert result.complete
    after_lines = path.read_text(encoding="utf-8").splitlines()
    assert after_lines[: len(before)] == before  # the earlier days are byte-for-byte as they were
    assert len(after_lines) == len(before) + 2 * 390
    assert read_symbol_bars(out, "AAPL")["date"].is_monotonic_increasing


@pytest.mark.parametrize("text", ["Fehler: Währung ü 日本語 \U0001f600\nzweite Zeile\t\"quoted\" \\ end",
                                  "x" * 200_000, "\x00\x07 control characters"])
def test_odd_ib_error_text_survives_the_manifest_round_trip(out: Path, text: str) -> None:
    day = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(day, error_behaviour(200, text))
        first = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert first.symbols["AAPL"].failed == 1 and first.symbols["AAPL"].failures[0].text == text
    with make_rig() as rig2:  # the manifest loads back: it was not set aside as unusable
        second = rig2.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert second.complete and rig2.days_requested() == [day]
    assert not list((out / "AAPL").glob("*.unusable"))


def test_account_shaped_text_in_an_ib_error_reaches_the_events_verbatim(out: Path) -> None:
    """Documents that the driver does not scrub IB's texts: the command layer must (see report)."""
    text = "Error with account DU1234567: something"
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(200, text))
        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert any("DU1234567" in e.message for e in rig.events)
    assert any(r.text == text for r in result.ib_errors)


def test_month_files_are_read_at_most_once_per_day_and_never_across_months(
    out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-day cost must not grow with the days already fetched (only that month is read)."""
    reads: list[Path] = []
    real = fetch_module.read_offline_bars

    def spy(path: Path) -> Any:
        reads.append(Path(path))
        return real(path)

    monkeypatch.setattr(fetch_module, "read_offline_bars", spy)
    symbol_reads: list[str] = []
    real_all = fetch_module.read_symbol_bars

    def spy_all(data_dir: Path, symbol: str, *a: Any, **k: Any) -> Any:
        symbol_reads.append(symbol)
        return real_all(data_dir, symbol, *a, **k)

    monkeypatch.setattr(fetch_module, "read_symbol_bars", spy_all)
    start, end = date(2024, 7, 1), date(2024, 8, 30)
    with make_rig(server_now=after(date(2024, 9, 3))) as rig:
        script_range(rig, start, end, short_day(5))
        result = rig.run(["AAPL"], start, end, out)
    assert result.complete
    days = weekdays(start, end)
    assert len(reads) <= len(days)
    by_name = defaultdict(int)
    for p in reads:
        by_name[p.name] += 1
    assert set(by_name) <= {"AAPL_2024-07.csv", "AAPL_2024-08.csv"}
    assert by_name["AAPL_2024-07.csv"] <= 23 and by_name["AAPL_2024-08.csv"] <= 22
    assert symbol_reads == ["AAPL"]  # the whole symbol is read once, for the gap report


# -- the committed data through the fake -----------------------------------------------------

REPO = Path(feed_module.__file__).resolve().parents[2]


@pytest.mark.slow
def test_all_committed_days_fetched_through_the_fake_equal_the_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    symbols = ["AAPL", "MSFT", "TSLA"]
    committed: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        committed[symbol] = pd.read_csv(REPO / "OfflineData" / f"_{symbol}_offlinedata_", index_col=0)
    days = sorted({date.fromisoformat(d[:10]) for d in committed["AAPL"]["date"]})
    assert len(days) == 7

    def from_committed(req_id: int, day: date, symbol: str) -> list[Played]:
        frame = committed[symbol]
        rows = frame[frame["date"].str.startswith(day.isoformat())]
        return [*(("historicalData", req_id, make_bar(int(r.time), r.open, r.high, r.low, r.close,
                                                      Decimal(int(r.volume)))) for r in rows.itertuples()),
                end_played(req_id)]

    fetched = tmp_path / "fetched"
    with make_rig(server_now=after(date(2024, 8, 28))) as rig:
        for day in days:
            rig.script(day, from_committed)
        result = rig.run(symbols, days[0], days[-1], fetched)
    assert result.complete and all(result.symbols[s].ok == 7 for s in symbols)
    assert_paced(rig.requests)
    for symbol in symbols:
        files = sorted((fetched / symbol).glob(f"{symbol}_*.csv"))
        assert [f.name for f in files] == [f"{symbol}_2024-08.csv"]
        got = files[0].read_text(encoding="utf-8").splitlines()
        want = (REPO / "OfflineData" / f"_{symbol}_offlinedata_").read_text(encoding="utf-8").splitlines()
        assert got[0] == "date,time,open,high,low,close,volume"
        assert len(got) == len(want)
        for line_got, line_want in zip(got[1:], want[1:], strict=True):
            # the text of every row, not only the values
            assert line_got == line_want.split(",", 1)[1], (symbol, line_got, line_want)
    assert_no_stray_files(fetched)

    committed_dir = tmp_path / "committed"
    committed_dir.mkdir()
    for symbol in symbols:
        name = f"_{symbol}_offlinedata_"
        shutil.copy(REPO / "OfflineData" / name, committed_dir / name)
    algos = tmp_path / "algos.json"
    algos.write_text(json.dumps([
        {"ID": "EMACrossing", "data": [{"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10",
                                        "long": "30"}]},
        {"ID": "OpeningRangeCandle", "data": [{"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3}]},
        {"ID": "FridayFrenzy", "data": [{"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3}]},
    ]), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for fills in ("legacy", "realistic"):
        outs = {}
        for name, data in (("fetched", fetched), ("committed", committed_dir)):
            target = tmp_path / f"out_{fills}_{name}"
            extra = ["--fills", "realistic"] if fills == "realistic" else []
            assert cli_main(["backtest", "--algos", str(algos), "--data", str(data), "--out", str(target),
                             "--quiet", *extra]) == 0
            outs[name] = target
        assert (outs["fetched"] / "trades.json").read_bytes() == (
            outs["committed"] / "trades.json").read_bytes()
        assert json.loads((outs["fetched"] / "trades.json").read_text(encoding="utf-8"))
        if fills == "realistic":
            assert (outs["fetched"] / "ledger.json").read_bytes() == (
                outs["committed"] / "ledger.json").read_bytes()


# -- T12 fix round: settle time, short days, damaged month file -------------------------------


def minutes_after_close(day: date, seconds: float) -> datetime:
    """A server time `seconds` after the 16:00 New York close of `day`."""
    close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY).astimezone(UTC)
    return close + timedelta(seconds=seconds)


@pytest.mark.parametrize("how", ["empty_end", "previous_session", "no_data_162"])
def test_no_data_right_after_the_close_is_failed_and_retried_next_run(out: Path, how: str) -> None:
    day = date(2024, 8, 20)
    behaviour = {"empty_end": empty_end, "previous_session": holiday_behaviour(date(2024, 8, 19)),
                 "no_data_162": error_behaviour(162, NO_DATA_TEXT)}[how]
    with make_rig(server_now=minutes_after_close(day, 600)) as rig:  # closed 10 minutes ago
        rig.script(day, behaviour)
        first = rig.run(["AAPL"], day, day, out)
    sym = first.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (0, 0, 1) and not first.complete
    assert "no data yet (the day's bars are not final until 06:00 New York time" in sym.failures[0].text
    assert manifest_days(out) == {day.isoformat(): "failed"}
    with make_rig(server_now=minutes_after_close(day, 14 * 3600)) as rig2:  # 06:00 next day: asked again
        rig2.script(day, behaviour)
        second = rig2.run(["AAPL"], day, day, out)
    assert rig2.days_requested() == [day]  # the failed day is retried
    assert second.symbols["AAPL"].no_data == 1 and second.complete
    assert manifest_days(out) == {day.isoformat(): "no_data"}


def test_no_data_at_the_final_time_is_no_data_and_three_hours_after_the_close_is_not(out: Path) -> None:
    day = date(2024, 8, 20)
    with make_rig(server_now=minutes_after_close(day, 3 * 3600 + 1)) as early:  # the old 3 h rule
        early.script(day, empty_end)
        assert early.run(["AAPL"], day, day, out).symbols["AAPL"].failed == 1
    with make_rig(server_now=minutes_after_close(day, 14 * 3600)) as rig:  # 06:00 New York next day
        rig.script(day, empty_end)
        result = rig.run(["AAPL"], day, day, out)
    assert result.symbols["AAPL"].no_data == 1 and result.complete
    assert manifest_days(out) == {day.isoformat(): "no_data"}


def test_a_holiday_two_days_ago_is_no_data(out: Path) -> None:
    holiday = date(2024, 9, 2)  # Labor Day
    with make_rig(server_now=minutes_after_close(date(2024, 9, 4), 0)) as rig:
        rig.script(holiday, holiday_behaviour(date(2024, 8, 30)))
        result = rig.run(["AAPL"], holiday, holiday, out)
    assert result.symbols["AAPL"].no_data == 1 and result.complete


def test_the_settle_seconds_argument_is_gone(out: Path) -> None:
    with make_rig() as rig:
        with pytest.raises(TypeError, match="settle_seconds"):
            rig.run(["AAPL"], WEEK[0], WEEK[0], out, settle_seconds=0)
    assert rig.requests == []


def test_early_close_day_is_listed_in_short_days_and_stays_ok(out: Path) -> None:
    half = date(2024, 11, 29)

    def early(req_id: int, day: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, day_rows(day, 210, symbol)), end_played(req_id)]

    with make_rig(server_now=after(date(2024, 12, 3))) as rig:
        rig.script(date(2024, 11, 28), holiday_behaviour(date(2024, 11, 27)))
        rig.script(half, early)
        result = rig.run(["AAPL"], date(2024, 11, 27), half, out)
    sym = result.symbols["AAPL"]
    assert sym.short_days == [(half, 210, "short")]  # the holiday (no bars) is not in the list
    assert result.days_with_gaps == 1 and result.complete and sym.ok == 2
    event = next(e for e in rig.events if e.kind == "gaps")
    assert event.detail["short_days"] == [(half.isoformat(), 210, "short")]
    assert event.detail["days_with_gaps"] == 1


def test_a_one_bar_day_is_listed_in_short_days_as_partial(out: Path) -> None:
    day = date(2024, 8, 14)
    one = [day_rows(day, 390)[0]]

    def single(req_id: int, d: date, symbol: str) -> list[Played]:
        return [*bars_played(req_id, one), end_played(req_id)]

    with make_rig() as rig:
        rig.script(day, single)
        result = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert result.symbols["AAPL"].short_days == [(day, 1, "partial")]
    assert result.days_with_gaps == 1 and result.complete


def test_a_clean_run_has_no_short_days(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL"], WEEK[0], WEEK[3], out)
    assert result.symbols["AAPL"].short_days == [] and result.days_with_gaps == 0
    event = next(e for e in rig.events if e.kind == "gaps")
    assert event.detail["short_days"] == [] and event.detail["days_with_gaps"] == 0


def test_already_present_short_days_are_listed_too(out: Path) -> None:
    day = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(day, lambda r, d, s: [*bars_played(r, day_rows(d, 100, s)), end_played(r)])
        assert rig.run(["AAPL"], WEEK[0], WEEK[2], out).complete
    with make_rig() as rig2:
        again = rig2.run(["AAPL"], WEEK[0], WEEK[2], out)
    assert rig2.requests == [] and again.symbols["AAPL"].short_days == [(day, 100, "partial")]
    assert again.days_with_gaps == 1


def test_a_damaged_month_file_stops_that_symbol_before_any_request_and_the_others_go_on(
        out: Path) -> None:
    with make_rig() as rig:
        assert rig.run(["AAPL"], WEEK[0], WEEK[0], out).complete
    path = month_file_path(out, "AAPL", 2024, 8)
    bad = b"this is not a csv\x00\n" + path.read_bytes()
    path.write_bytes(bad)
    with make_rig() as rig2:
        result = rig2.run(["AAPL", "MSFT"], WEEK[0], WEEK[4], out)
    aapl, msft = result.symbols["AAPL"], result.symbols["MSFT"]
    # adapted: was exactly one request after the damage; now none (the file is checked first)
    assert rig2.days_requested(CON_IDS["AAPL"]) == []
    assert (aapl.state, aapl.ok, aapl.failed) == ("stopped", 0, 1)
    assert path.name in aapl.message and "untouched" in aapl.message
    assert path.read_bytes() == bad
    assert (msft.state, msft.ok, msft.failed) == ("done", 5, 0)
    assert rig2.days_requested(CON_IDS["MSFT"]) == weekdays(WEEK[0], WEEK[4])
    assert result.stop_reason is None and not result.complete  # AAPL did not finish
    assert any(e.kind == "symbol_stopped" and e.symbol == "AAPL" for e in rig2.events)
    assert_paced(rig2.requests)


# -- T12 fix round 2: the month file is checked before a request is spent ---------------------


def damage_august(out: Path, kind: str) -> tuple[Path, bytes]:
    """Fetch AAPL 2024-08-12, then damage the August file in the way `kind` says."""
    with make_rig() as rig:
        assert rig.run(["AAPL"], WEEK[0], WEEK[0], out).complete
    path = month_file_path(out, "AAPL", 2024, 8)
    lines = path.read_text(encoding="utf-8").splitlines()
    if kind == "other_month":  # a row of July in the August file
        stray = ",".join(["2024-07-31 15:59:00", *lines[1].split(",")[1:]])
        new = [lines[0], stray, *lines[1:]]
    elif kind == "zone_aware":  # every date carries a UTC offset
        new = [lines[0], *(ln.split(",", 1)[0] + "-04:00," + ln.split(",", 1)[1] for ln in lines[1:])]
    else:
        new = ["this is not a csv\x00\x01 at all"]
    path.write_text("\n".join(new) + "\n", encoding="utf-8", newline="")
    return path, path.read_bytes()


@pytest.mark.parametrize("kind", ["other_month", "zone_aware"])
def test_a_month_file_with_a_stray_row_or_zone_aware_dates_stops_only_that_symbol(
        out: Path, kind: str) -> None:
    path, bad = damage_august(out, kind)
    with make_rig() as rig:
        result = rig.run(["AAPL", "MSFT"], WEEK[0], WEEK[2], out)  # must not raise
    aapl, msft = result.symbols["AAPL"], result.symbols["MSFT"]
    assert rig.days_requested(CON_IDS["AAPL"]) == []  # nothing was sent for the damaged symbol
    assert (aapl.state, aapl.ok, aapl.failed) == ("stopped", 0, 1)
    assert [(f.day, f.reason) for f in aapl.failures] == [(WEEK[1], "damaged_file")]
    assert path.name in aapl.message and "untouched" in aapl.message
    assert path.read_bytes() == bad
    assert manifest_days(out) == {WEEK[0].isoformat(): "ok", WEEK[1].isoformat(): "failed"}
    assert (msft.state, msft.ok, msft.failed) == ("done", 3, 0)
    assert rig.days_requested(CON_IDS["MSFT"]) == weekdays(WEEK[0], WEEK[2])
    assert result.stop_reason is None and not result.complete


def test_a_garbage_month_file_costs_no_request_even_for_days_that_would_fail(out: Path) -> None:
    path, bad = damage_august(out, "garbage")
    with make_rig() as rig:
        for day in WEEK[1:4]:
            rig.script(day, error_behaviour(200, "No security definition has been found"))
        result = rig.run(["AAPL"], WEEK[0], WEEK[4], out)
    assert rig.requests == []  # it used to be four requests
    sym = result.symbols["AAPL"]
    assert (sym.state, sym.failed, sym.planned) == ("stopped", 1, 4)
    assert [f.reason for f in sym.failures] == ["damaged_file"] and sym.remaining == 3
    assert path.read_bytes() == bad


@pytest.mark.parametrize("kind", ["other_month", "zone_aware"])
def test_the_merge_time_catch_is_a_second_line_behind_the_check(
        out: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the up-front check switched off, the same files still cannot make `fetch` raise."""
    path, bad = damage_august(out, kind)
    monkeypatch.setattr(fetch_module, "check_month_file", lambda *a, **k: None)
    with make_rig() as rig:
        result = rig.run(["AAPL", "MSFT"], WEEK[0], WEEK[2], out)
    aapl = result.symbols["AAPL"]
    assert rig.days_requested(CON_IDS["AAPL"]) == [WEEK[1]]  # one request, then it stops
    assert (aapl.state, aapl.failed) == ("stopped", 1)
    assert [f.reason for f in aapl.failures] == ["damaged_file"]
    assert path.read_bytes() == bad
    assert result.symbols["MSFT"].state == "done"


def test_a_healthy_month_file_is_read_once_per_symbol_month_for_the_check(
        out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start, end = date(2024, 7, 29), date(2024, 8, 2)
    later = after(date(2024, 9, 3))
    with make_rig(server_now=later) as rig:
        script_range(rig, start, end, short_day(5))
        assert rig.run(["AAPL", "MSFT"], date(2024, 7, 31), date(2024, 8, 1), out).complete
    checked: list[str] = []
    real = fetch_module.check_month_file

    def spy(path: Path, year: int, month: int) -> None:
        checked.append(path.name)
        real(path, year, month)

    monkeypatch.setattr(fetch_module, "check_month_file", spy)
    with make_rig(server_now=later) as rig2:
        script_range(rig2, start, end, short_day(5))
        result = rig2.run(["AAPL", "MSFT"], start, end, out)
    assert result.complete
    assert sorted(checked) == ["AAPL_2024-07.csv", "AAPL_2024-08.csv",
                               "MSFT_2024-07.csv", "MSFT_2024-08.csv"]  # many days, one check each


# -- a 1100 in the last slice of a pacing wait ------------------------------------------------


def test_a_1100_in_the_last_slice_of_a_pacing_wait_sends_nothing(out: Path) -> None:
    from tests.test_ib_fetch import SpyPacer

    state = {"waited": 0.0, "dropped": False}

    with make_rig() as rig:
        pacer = SpyPacer(rig.clock)

        def sleep(seconds: float) -> None:
            rig.clock.sleep(seconds)
            waits = [e for e in rig.events if e.kind == "pacing_wait"]
            if not waits or state["dropped"]:
                return
            state["waited"] += seconds
            if state["waited"] >= float(waits[-1].detail["seconds"]) - 1e-9:  # the last slice is over
                state["dropped"] = True
                rig.fake.drop(1100)
                for _ in range(2000):
                    if rig.conn.state.name == "LOST":
                        break
                    time.sleep(0.001)

        result = rig.run(["AAPL"], WEEK[0], WEEK[2], out, sleep=sleep, pacer=pacer)
    assert state["dropped"]
    assert rig.days_requested() == [WEEK[0]]  # the second request was never sent
    assert [n for n, _ in pacer.log] == ["reserve", "sent", "reserve", "cancel"]
    assert result.connection_lost and result.stop_reason == "connection lost"
    assert manifest_days(out) == {WEEK[0].isoformat(): "ok"}
    assert result.symbols["AAPL"].remaining == 2


# -- IB empty for a day that is on disk; no data yet keeps IB's code --------------------------


def manifest_record(out: Path, day: date) -> dict[str, Any]:
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    return raw["days"][day.isoformat()]  # type: ignore[no-any-return]


@pytest.mark.parametrize("how", ["empty_end", "no_data_162"])
def test_an_empty_answer_for_a_day_with_bars_on_disk_is_failed_and_the_bars_stay(
        out: Path, how: str) -> None:
    with make_rig() as rig:
        assert rig.run(["AAPL"], WEEK[0], WEEK[1], out).complete
    (out / "AAPL" / "manifest.json").unlink()
    before = month_bytes(out)
    behaviour = empty_end if how == "empty_end" else error_behaviour(162, NO_DATA_TEXT)
    with make_rig() as rig2:
        rig2.script(WEEK[1], behaviour)
        result = rig2.run(["AAPL"], WEEK[0], WEEK[1], out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (1, 0, 1) and not result.complete
    failure = sym.failures[0]
    assert (failure.day, failure.reason) == (WEEK[1], "no_data_but_bars_on_disk")
    assert "kept" in failure.text and "390 bar" in failure.text
    if how == "no_data_162":
        assert failure.code == 162 and NO_DATA_TEXT in failure.text
    assert month_bytes(out) == before  # the bars stay, byte for byte
    assert manifest_days(out) == {WEEK[0].isoformat(): "ok", WEEK[1].isoformat(): "failed"}


def test_no_data_for_a_day_without_bars_on_disk_is_still_no_data(out: Path) -> None:
    with make_rig() as rig:
        assert rig.run(["AAPL"], WEEK[0], WEEK[0], out).complete
        rig.script(WEEK[1], empty_end)
        result = rig.run(["AAPL"], WEEK[0], WEEK[1], out)
    assert result.symbols["AAPL"].no_data == 1 and result.complete


def test_no_data_yet_keeps_ib_code_and_text(out: Path) -> None:
    day = date(2024, 8, 20)
    with make_rig(server_now=minutes_after_close(day, 600)) as rig:
        rig.script(day, error_behaviour(162, NO_DATA_TEXT))
        result = rig.run(["AAPL"], day, day, out)
    failure = result.symbols["AAPL"].failures[0]
    assert failure.reason == "no_data_yet" and failure.code == 162
    assert failure.text.startswith(NO_DATA_TEXT) and "no data yet" in failure.text
    record = manifest_record(out, day)
    assert record["status"] == "failed" and record["error_code"] == 162
    assert NO_DATA_TEXT in record["error_text"]
    with make_rig(server_now=minutes_after_close(day, 600)) as rig2:  # an empty end has no IB code
        rig2.script(day, empty_end)
        again = rig2.run(["AAPL"], day, day, out)
    assert again.symbols["AAPL"].failures[0].code is None
