"""Provisional days (issue 174): a day fetched before 06:00 New York next day is stored, marked
provisional and fetched again by a later run at or after that time.

Everything runs on the fakes of `tests/test_ib_fetch.py`; nothing opens a socket.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from MainStoinker.engine.feed import read_symbol_bars
from MainStoinker.ib import cli
from MainStoinker.ib.history import (
    DAY_FINAL_AT,
    GAP_PROVISIONAL,
    Manifest,
    day_final_utc,
    gap_report,
    gaps_summary_text,
    load_manifest,
    manifest_path,
)
from tests.test_ib_fetch import (
    PACING_TEXT,
    bars_played,
    day_rows,
    end_played,
    error_behaviour,
    make_rig,
    manifest_days,
    month_bytes,
)

NY = ZoneInfo("America/New_York")
DAY = date(2024, 8, 20)  # a Tuesday, daylight saving time (UTC-4)
EVENING = datetime(2024, 8, 20, 19, 30, tzinfo=NY).astimezone(UTC)  # 3.5 h after the close
BEFORE_FINAL = datetime(2024, 8, 21, 5, 59, tzinfo=NY).astimezone(UTC)
AT_FINAL = datetime(2024, 8, 21, 6, 0, tzinfo=NY).astimezone(UTC)
PROVISIONAL_LINE = ("AAPL 2024-08-20: ok (390 bars, provisional: fetched before the day's bars are "
                    "final; a run after 06:00 New York next day will refetch)")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "bars"


def manifest_record(out: Path, day: date) -> dict[str, Any]:
    return json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))["days"][day.isoformat()]


def day_lines(rig: Any) -> list[str]:
    return [e.message for e in rig.events if e.kind == "day"]


def changed_volume(rows_changed: int, minutes: int = 390):  # type: ignore[no-untyped-def]
    """A behaviour answering the day with `rows_changed` first minutes carrying other values."""
    def behave(req_id: int, day: date, symbol: str) -> list[Any]:
        rows = day_rows(day, minutes, symbol)
        rows = [(t, o, h + 0.05, lo, c, v + 5000) if i < rows_changed else (t, o, h, lo, c, v)
                for i, (t, o, h, lo, c, v) in enumerate(rows)]
        return [*bars_played(req_id, rows), end_played(req_id)]
    return behave


def fewer_bars(minutes: int):  # type: ignore[no-untyped-def]
    def behave(req_id: int, day: date, symbol: str) -> list[Any]:
        return [*bars_played(req_id, day_rows(day, minutes, symbol)), end_played(req_id)]
    return behave


def first_evening_run(out: Path) -> None:
    with make_rig(server_now=EVENING) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert result.complete


# -- the constant and the final time ------------------------------------------------------------------------


def test_the_final_time_is_06_00_new_york_next_calendar_day() -> None:
    assert DAY_FINAL_AT.hour == 6 and DAY_FINAL_AT.minute == 0
    assert day_final_utc(DAY) == datetime(2024, 8, 21, 10, 0, tzinfo=UTC)  # EDT: UTC-4
    assert day_final_utc(date(2024, 8, 23)) == datetime(2024, 8, 24, 10, 0, tzinfo=UTC)  # Friday: Saturday


@pytest.mark.parametrize("day, final", [
    (date(2024, 3, 8), datetime(2024, 3, 9, 11, 0, tzinfo=UTC)),    # Friday before the spring change: EST
    (date(2024, 3, 11), datetime(2024, 3, 12, 10, 0, tzinfo=UTC)),  # Monday after it: EDT
    (date(2024, 11, 1), datetime(2024, 11, 2, 10, 0, tzinfo=UTC)),  # Friday before the autumn change: EDT
    (date(2024, 11, 4), datetime(2024, 11, 5, 11, 0, tzinfo=UTC)),  # Monday after it: EST
    (date(2026, 11, 23), datetime(2026, 11, 24, 11, 0, tzinfo=UTC)),  # the observed day
])
def test_the_final_time_follows_daylight_saving(day: date, final: datetime) -> None:
    assert day_final_utc(day) == final


@pytest.mark.parametrize("day, run_at", [
    (date(2024, 11, 1), datetime(2024, 11, 2, 10, 0, tzinfo=UTC)),   # EDT: 06:00 is 10:00 UTC
    (date(2024, 11, 4), datetime(2024, 11, 5, 11, 0, tzinfo=UTC)),   # EST: 06:00 is 11:00 UTC
])
def test_the_refetch_waits_for_06_00_local_across_the_daylight_saving_change(
        out: Path, day: date, run_at: datetime) -> None:
    close = datetime(day.year, day.month, day.day, 17, 0, tzinfo=NY).astimezone(UTC)
    with make_rig(server_now=close) as rig:
        assert rig.run(["AAPL"], day, day, out).complete
    assert load_manifest(manifest_path(out, "AAPL"), Manifest("AAPL", 265598))[0].is_provisional(day)
    with make_rig(server_now=datetime.fromtimestamp(run_at.timestamp() - 1, UTC)) as rig2:
        assert rig2.run(["AAPL"], day, day, out).complete
    assert rig2.requests == []  # one second before 06:00 local: not final
    with make_rig(server_now=run_at) as rig3:
        assert rig3.run(["AAPL"], day, day, out).complete
    assert rig3.days_requested() == [day]


# -- the rule -----------------------------------------------------------------------------------------------


def test_a_day_fetched_in_the_evening_is_stored_and_provisional(out: Path) -> None:
    with make_rig(server_now=EVENING) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    sym = result.symbols["AAPL"]
    assert result.complete and (sym.ok, sym.failed) == (1, 0)
    assert len(read_symbol_bars(out, "AAPL")) == 390  # usable the same evening
    assert manifest_days(out) == {DAY.isoformat(): "ok"}
    assert sym.provisional_days == [DAY]
    assert day_lines(rig) == [PROVISIONAL_LINE]
    gaps = (out / "AAPL" / "gaps.csv").read_text(encoding="utf-8")
    assert f"AAPL,{DAY.isoformat()},provisional,390,390,0," in gaps
    assert "  provisional: 1" in (out / "AAPL" / "gaps_summary.txt").read_text(encoding="utf-8")
    assert "complete" not in gaps.splitlines()[1]


def test_a_day_fetched_at_the_final_time_is_complete_at_once(out: Path) -> None:
    with make_rig(server_now=AT_FINAL) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert result.symbols["AAPL"].provisional_days == []
    assert day_lines(rig) == ["AAPL 2024-08-20: ok (390 bars)"]
    assert ",complete," in (out / "AAPL" / "gaps.csv").read_text(encoding="utf-8")


def test_a_run_one_minute_before_final_does_not_refetch(out: Path) -> None:
    first_evening_run(out)
    before = month_bytes(out)
    with make_rig(server_now=BEFORE_FINAL) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert rig.requests == []  # refetching before final cannot give a better answer
    sym = result.symbols["AAPL"]
    assert sym.provisional_days == [DAY] and sym.already_done == 1 and result.complete
    assert month_bytes(out) == before


def test_a_run_at_the_final_time_refetches_replaces_and_completes(out: Path) -> None:
    first_evening_run(out)
    with make_rig(server_now=AT_FINAL) as rig:
        rig.script(DAY, changed_volume(3))
        result = rig.run(["AAPL"], DAY, DAY, out)
    sym = result.symbols["AAPL"]
    assert rig.days_requested() == [DAY]
    assert sym.refetched == {DAY: 3} and sym.provisional_days == [] and result.complete
    assert day_lines(rig) == ["AAPL 2024-08-20: ok (390 bars, refetched: 3 row(s) changed)"]
    frame = read_symbol_bars(out, "AAPL")
    assert len(frame) == 390 and int(frame["volume"].iloc[0]) == 1000 + 5000  # the new rows are on disk
    assert not load_manifest(manifest_path(out, "AAPL"), Manifest("AAPL", 265598))[0].is_provisional(DAY)
    assert ",complete," in (out / "AAPL" / "gaps.csv").read_text(encoding="utf-8")
    with make_rig(server_now=AT_FINAL) as rig2:  # complete now: skipped, no request at all
        again = rig2.run(["AAPL"], DAY, DAY, out)
    assert rig2.requests == [] and again.symbols["AAPL"].already_done == 1


def test_an_unchanged_refetch_reports_zero_changed_rows(out: Path) -> None:
    first_evening_run(out)
    before = month_bytes(out)
    with make_rig(server_now=AT_FINAL) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert result.symbols["AAPL"].refetched == {DAY: 0}
    assert day_lines(rig) == ["AAPL 2024-08-20: ok (390 bars, refetched: 0 row(s) changed)"]
    assert month_bytes(out) == before


def test_a_refetch_with_fewer_bars_keeps_the_stored_day(out: Path) -> None:
    first_evening_run(out)
    before = month_bytes(out)
    with make_rig(server_now=AT_FINAL) as rig:
        rig.script(DAY, fewer_bars(380))
        result = rig.run(["AAPL"], DAY, DAY, out)
    sym = result.symbols["AAPL"]
    assert month_bytes(out) == before and len(read_symbol_bars(out, "AAPL")) == 390
    assert sym.refetch_kept == {DAY: (390, 380)} and sym.ok == 1 and result.complete
    assert day_lines(rig) == ["AAPL 2024-08-20: ok (390 bars, stored day kept: the refetch returned "
                              "only 380 of the 390 stored bars)"]
    record = manifest_record(out, DAY)
    assert record["bars"] == 390 and record["refetch_fewer"] == 380
    with make_rig(server_now=AT_FINAL) as rig2:  # the day is final now: not asked for again
        rig2.run(["AAPL"], DAY, DAY, out)
    assert rig2.requests == []


def test_a_failed_refetch_keeps_the_stored_day_and_is_tried_again(out: Path) -> None:
    first_evening_run(out)
    before = month_bytes(out)
    with make_rig(server_now=AT_FINAL) as rig:
        rig.script(DAY, error_behaviour(321, "some IB error"))
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert result.symbols["AAPL"].failed == 1 and month_bytes(out) == before
    assert manifest_days(out) == {DAY.isoformat(): "ok"}  # the record was not replaced by `failed`
    with make_rig(server_now=AT_FINAL) as rig2:
        again = rig2.run(["AAPL"], DAY, DAY, out)
    assert rig2.days_requested() == [DAY] and again.complete


# -- old and unknown manifests ------------------------------------------------------------------------------


def write_manifest(out: Path, days: dict[str, dict[str, Any]], **extra: Any) -> None:
    path = out / "AAPL" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "symbol": "AAPL", "conId": 265598, "data_zone": "America/New_York",
            "use_rth": 1, "what_to_show": "TRADES", "bar_size": "1 min", "days": days, **extra}
    path.write_text(json.dumps(body), encoding="utf-8")


def old_record(stamp: str) -> dict[str, Any]:
    return {"status": "ok", "bars": 390, "error_code": None, "error_text": "", "fetched_utc": stamp}


def test_an_old_manifest_day_without_a_fetch_time_stays_complete(out: Path) -> None:
    write_manifest(out, {DAY.isoformat(): old_record("")})
    with make_rig(server_now=AT_FINAL) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert rig.requests == [] and result.symbols["AAPL"].provisional_days == []


def test_an_old_manifest_day_fetched_before_its_final_time_becomes_provisional(out: Path) -> None:
    stamps = {"2024-08-19": "2024-08-20T09:40:00Z",  # 05:40 New York: before 06:00 next day
              "2024-08-16": "2024-08-17T15:00:00Z"}  # after its final time: complete
    write_manifest(out, {k: old_record(v) for k, v in stamps.items()})
    with make_rig(server_now=BEFORE_FINAL) as rig:  # 2024-08-21 05:59: final for the 19th is passed
        result = rig.run(["AAPL"], date(2024, 8, 16), date(2024, 8, 19), out)
    assert rig.days_requested() == [date(2024, 8, 19)]  # only the one fetched before its final time
    assert result.symbols["AAPL"].provisional_days == []


def test_unknown_fields_in_a_manifest_do_not_crash_and_old_files_load(out: Path) -> None:
    record = old_record("2024-08-21T15:00:00Z") | {"future_field": [1, 2], "refetch_fewer": "x"}
    write_manifest(out, {DAY.isoformat(): record}, something_new={"a": 1})
    manifest, reason = load_manifest(manifest_path(out, "AAPL"), Manifest("AAPL", 265598))
    assert reason is None and manifest.days[DAY.isoformat()].refetch_fewer == 0
    assert not manifest.is_provisional(DAY)


def test_a_garbled_fetch_time_counts_as_complete() -> None:
    manifest = Manifest("AAPL", 265598)
    from MainStoinker.ib.history import DayRecord
    manifest.days[DAY.isoformat()] = DayRecord("ok", 390, None, "", "yesterday-ish")
    assert not manifest.is_provisional(DAY)


# -- resume -------------------------------------------------------------------------------------------------


def test_resume_after_a_stopped_run_still_fetches_the_days_not_fetched(out: Path) -> None:
    days = [date(2024, 8, 20), date(2024, 8, 21)]
    evening = datetime(2024, 8, 21, 19, 30, tzinfo=NY).astimezone(UTC)  # the 21st is provisional
    before_final = datetime(2024, 8, 22, 5, 59, tzinfo=NY).astimezone(UTC)
    with make_rig(server_now=evening) as rig0:
        assert rig0.run(["AAPL"], days[1], days[1], out).complete
    with make_rig(server_now=evening) as rig:
        rig.script(days[0], error_behaviour(162, PACING_TEXT))  # the second violation stops the run
        stopped = rig.run(["AAPL"], days[0], days[1], out)
    assert stopped.pacing_stopped
    rows = {r.day: r.status for r in stopped.symbols["AAPL"].gaps}
    assert rows == {days[0]: "no_data", days[1]: GAP_PROVISIONAL}
    with make_rig(server_now=before_final) as rig2:
        resumed = rig2.run(["AAPL"], days[0], days[1], out)
    assert rig2.days_requested() == [days[0]]  # the failed day is fetched; the provisional one is not
    assert resumed.complete
    assert resumed.symbols["AAPL"].provisional_days == [days[1]]


def test_a_stopped_run_lists_not_fetched_days_next_to_provisional_ones(out: Path) -> None:
    days = [date(2024, 8, 19), date(2024, 8, 20), date(2024, 8, 21)]
    with make_rig(server_now=datetime(2024, 8, 21, 22, 0, tzinfo=UTC)) as rig:
        rig.script(days[1], error_behaviour(162, PACING_TEXT))
        stopped = rig.run(["AAPL"], days[0], days[2], out)
    assert {r.day: r.status for r in stopped.symbols["AAPL"].gaps}[days[2]] == "not_fetched"
    with make_rig(server_now=datetime(2024, 8, 21, 22, 0, tzinfo=UTC)) as rig2:
        resumed = rig2.run(["AAPL"], days[0], days[2], out)
    assert rig2.days_requested() == [days[1], days[2]]  # the failed and the not_fetched day
    assert resumed.complete and resumed.symbols["AAPL"].provisional_days == [days[2]]


# -- gap report and the CLI summary -------------------------------------------------------------------------


def test_gap_report_marks_only_full_provisional_days() -> None:
    import pandas as pd

    from MainStoinker.ib.history import expected_minutes

    full = expected_minutes(DAY)
    other = date(2024, 8, 19)
    frame = pd.DataFrame({"date": full + expected_minutes(other)[:100]})
    rows = {r.day: r.status for r in gap_report([DAY, other], frame, provisional={DAY, other})}
    assert rows == {DAY: "provisional", other: "partial"}  # a day with holes keeps its stronger status
    assert "provisional: 1" in gaps_summary_text(gap_report([DAY], frame, provisional={DAY}))
    assert "provisional" not in gaps_summary_text(gap_report([DAY], frame))


def test_the_summary_counts_provisional_days_per_symbol(out: Path) -> None:
    with make_rig(server_now=EVENING) as rig:
        result = rig.run(["AAPL"], DAY, DAY, out)
    lines: list[str] = []

    class Say:
        def __call__(self, text: str = "") -> None:
            lines.append(text)

    cli._summary(Say(), result, out)  # type: ignore[arg-type]
    text = "\n".join(lines)
    assert "AAPL: 1 ok, 0 no data, 0 failed, 0 already done, 0 not closed, 1 provisional [done]" in text
    assert "1 provisional" in text and "a run after 06:00 New York next day will refetch" in text
    assert "gaps: 1 provisional" in text


def test_the_settle_rule_is_replaced_not_kept(out: Path) -> None:
    """Three hours after the close (19:00) is not final: no data then is `failed`, not `no_data`."""
    close_plus_3h = datetime(2024, 8, 20, 19, 0, 1, tzinfo=NY).astimezone(UTC)

    def empty(req_id: int, day: date, symbol: str) -> list[Any]:
        return [end_played(req_id)]

    with make_rig(server_now=close_plus_3h) as rig:
        rig.script(DAY, empty)
        result = rig.run(["AAPL"], DAY, DAY, out)
    assert result.symbols["AAPL"].failed == 1
