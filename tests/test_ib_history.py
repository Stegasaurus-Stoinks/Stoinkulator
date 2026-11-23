"""Fetch planning for historical bars (`MainStoinker/ib/history.py`): offline, injected clocks."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from MainStoinker.ib import history
from MainStoinker.ib.history import (
    GAP_COMPLETE,
    GAP_NO_DATA,
    GAP_PARTIAL,
    GAP_SHORT,
    Manifest,
    Pacer,
    day_request,
    expected_minutes,
    filter_day,
    gap_report,
    load_manifest,
    plan_days,
    save_manifest,
    write_gap_files,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
LATE = datetime(2030, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def send(self, pacer: Pacer, contract: int = 1, request: str = "r") -> float:
        """Reserve, wait the returned time on the fake clock, send on time, return the send time."""
        reservation = pacer.reserve(contract, request)
        self.now += reservation.wait
        pacer.sent(reservation)
        return self.now


def bars_for(day: date, minutes: list[datetime] | None = None) -> pd.DataFrame:
    stamps = minutes if minutes is not None else expected_minutes(day)
    return pd.DataFrame({"date": stamps, "time": 0, "open": 1.0, "high": 1.0,
                         "low": 1.0, "close": 1.0, "volume": 1})


def test_plan_skips_weekends() -> None:
    # Fri 2024-03-08 .. Tue 2024-03-12 contains a weekend.
    days = plan_days(date(2024, 3, 8), date(2024, 3, 12), LATE)
    assert days == [date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12)]
    assert plan_days(date(2024, 3, 9), date(2024, 3, 10), LATE) == []
    assert plan_days(date(2024, 3, 8), date(2024, 3, 12), LATE) == days  # deterministic
    with pytest.raises(ValueError):
        plan_days(date(2024, 3, 12), date(2024, 3, 8), LATE)


def test_plan_refuses_unclosed_session() -> None:
    # Rule: a range is refused only when it holds trading days and none has closed. A range
    # whose only trading day is unclosed is therefore still refused (this test's meaning); a
    # range with a closed trading day is planned (see test_plan_range_*).
    day = date(2024, 3, 11)  # EDT: 16:00 is 20:00 UTC
    with pytest.raises(ValueError, match="not closed"):
        plan_days(day, day, datetime(2024, 3, 11, 19, 59, 59, tzinfo=UTC))
    assert plan_days(day, day, datetime(2024, 3, 11, 20, 0, 0, tzinfo=UTC)) == [day]
    # A naive clock is refused, not guessed.
    with pytest.raises(ValueError, match="timezone-aware"):
        plan_days(day, day, datetime(2030, 1, 1))
    # Same instant expressed in another zone.
    other = datetime(2024, 3, 11, 13, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert plan_days(day, day, other) == [day]


def test_plan_range_plans_closed_days_and_reports_unclosed_ones() -> None:
    # Saturday noon: Friday has closed, so a range ending on the weekend is planned.
    saturday_noon = datetime(2024, 3, 9, 17, 0, tzinfo=UTC)
    plan = history.plan_range(date(2024, 3, 7), date(2024, 3, 10), saturday_noon)
    assert plan.days == [date(2024, 3, 7), date(2024, 3, 8)] and plan.not_closed == []
    assert plan_days(date(2024, 3, 7), date(2024, 3, 10), saturday_noon) == plan.days
    # Monday 15:00 New York: Monday is not closed and is reported, earlier days are planned.
    monday = datetime(2024, 3, 11, 15, 0, tzinfo=NY)
    plan = history.plan_range(date(2024, 3, 7), date(2024, 3, 12), monday)
    assert plan.days == [date(2024, 3, 7), date(2024, 3, 8)]
    assert plan.not_closed == [date(2024, 3, 11), date(2024, 3, 12)]
    # Only unclosed trading days (with a weekend in between): refused.
    with pytest.raises(ValueError, match="not closed"):
        history.plan_range(date(2024, 3, 11), date(2024, 3, 12), monday)
    # No trading day at all is not a refusal.
    assert history.plan_range(date(2024, 3, 9), date(2024, 3, 10), saturday_noon) == history.DayPlan([], [])


def test_day_request_end_is_1600_new_york_in_utc_across_dst() -> None:
    before = day_request(date(2024, 3, 8))  # EST
    after = day_request(date(2024, 3, 11))  # EDT, DST began on the 10th
    assert before.end_date_time == "20240308-21:00:00"
    assert after.end_date_time == "20240311-20:00:00"
    assert (before.duration, before.use_rth) == ("1 D", 1)
    assert day_request(date(2024, 11, 1)).end_date_time == "20241101-20:00:00"
    assert day_request(date(2024, 11, 4)).end_date_time == "20241104-21:00:00"
    assert before.bar_size == "1 min" and before.what_to_show == "TRADES"


def test_filter_day_drops_previous_session_returned_for_holiday() -> None:
    holiday = date(2024, 5, 27)  # Memorial Day: IB returns Friday 24th for "1 D"
    friday = date(2024, 5, 24)
    returned = bars_for(friday)
    assert filter_day(returned, holiday, LATE).empty
    kept = filter_day(pd.concat([returned, bars_for(holiday)]), holiday, LATE)
    assert len(kept) == 390
    assert set(kept["date"].dt.date) == {holiday}
    assert list(kept.index) == list(range(390))


def test_filter_day_drops_unfinished_minute() -> None:
    day = date(2024, 3, 11)
    # 10:00 New York (EDT) is 14:00 UTC. The 10:00 bar ends at 10:01.
    now = datetime(2024, 3, 11, 14, 1, 0, tzinfo=UTC)
    stamps = [datetime(2024, 3, 11, 9, 59), datetime(2024, 3, 11, 10, 0), datetime(2024, 3, 11, 10, 1)]
    kept = filter_day(bars_for(day, stamps), day, now)
    assert list(kept["date"]) == stamps[:2]
    early = datetime(2024, 3, 11, 14, 0, 59, tzinfo=UTC)
    assert list(filter_day(bars_for(day, stamps), day, early)["date"]) == stamps[:1]


def test_pacer_never_exceeds_60_per_10_minutes() -> None:
    for kwargs in ({}, {"min_gap": 1.0}):  # defaults, and a short gap so the window rule binds
        clock = FakeClock()
        pacer = Pacer(clock, **kwargs)
        times = [clock.send(pacer, 1, f"r{i}") for i in range(61)]
        for i, t in enumerate(times):
            in_window = [u for u in times if t <= u <= t + 600]
            assert len(in_window) <= 60, (kwargs, i)
        assert all(b - a >= kwargs.get("min_gap", 10.0) for a, b in zip(times, times[1:], strict=False))
    # The window rule alone (no gap) is what delays the 61st request.
    clock = FakeClock()
    pacer = Pacer(clock, min_gap=0.0, max_per_contract=1000)
    times = [clock.send(pacer, 1, f"r{i}") for i in range(61)]
    assert times[59] == times[0]  # 60 go out at once
    assert times[60] - times[0] > 600


def test_pacer_no_identical_request_within_15s() -> None:
    clock = FakeClock()
    pacer = Pacer(clock, min_gap=0.0)
    first = clock.send(pacer, 1, "same")
    other = clock.send(pacer, 1, "different")
    again = clock.send(pacer, 1, "same")
    assert other == first
    assert again - first > 15
    assert pacer.earliest(1, "same") - clock() > 14  # booked, so waiting again is needed


def test_pacer_contract_burst_limit() -> None:
    clock = FakeClock()
    pacer = Pacer(clock, min_gap=0.0)
    times = [clock.send(pacer, 265598, f"r{i}") for i in range(12)]
    for t in times:
        assert len([u for u in times if t <= u <= t + 2]) <= 5
    assert times[5] - times[0] > 2
    # Another contract is not held back by it.
    t0 = clock()
    assert pacer.reserve(272093, "x").wait == pytest.approx(0.0)
    assert clock() == t0


def _manifest() -> Manifest:
    return Manifest("AAPL", 265598)


def test_manifest_resume_skips_done_and_retries_failed(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    assert path == tmp_path / "AAPL" / "manifest.json"
    days = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)]
    manifest = _manifest()
    assert manifest.pending(days) == days
    manifest.record(days[0], "ok", bars=390, fetched_utc=LATE)
    manifest.record(days[1], "no_data", fetched_utc=LATE, complete_answer=True)
    manifest.record(days[2], "failed", error_code=162, error_text="pacing violation", fetched_utc=LATE)
    save_manifest(path, manifest)

    loaded, problem = load_manifest(path, _manifest())
    assert problem is None
    assert loaded.pending(days) == days[2:]  # failed retried, unseen still to do
    assert loaded.days["2024-03-12"].error_code == 162
    assert loaded.days["2024-03-12"].error_text == "pacing violation"
    assert loaded.days["2024-03-08"].fetched_utc == "2030-01-01T00:00:00Z"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert (raw["symbol"], raw["conId"], raw["data_zone"], raw["use_rth"]) == (
        "AAPL", 265598, "America/New_York", 1)
    assert (raw["what_to_show"], raw["bar_size"]) == ("TRADES", "1 min")
    # Fixing the failed day finishes the plan.
    loaded.record(days[2], "ok", bars=390, fetched_utc=LATE)
    assert loaded.pending(days) == days[3:]
    with pytest.raises(ValueError):
        loaded.record(days[3], "ok", bars=0, fetched_utc=LATE)
    with pytest.raises(ValueError):
        loaded.record(days[3], "no_data", bars=5, fetched_utc=LATE, complete_answer=True)
    with pytest.raises(ValueError):
        loaded.record(days[3], "maybe", fetched_utc=LATE)


@pytest.mark.parametrize("content", [
    '{"version": 1, "symbol": "AAPL", "conId": 265598, "days": {"2024-03-',  # truncated
    "[1, 2, 3]",  # foreign
    '{"hello": "world"}',
    "",
    '{"version": 1, "symbol": "MSFT", "conId": 1, "data_zone": "America/New_York", '
    '"use_rth": 1, "what_to_show": "TRADES", "bar_size": "1 min", "days": {}}',  # other symbol
    '{"version": 1, "symbol": "AAPL", "conId": 265598, "data_zone": "America/New_York", '
    '"use_rth": 1, "what_to_show": "TRADES", "bar_size": "1 min", '
    '"days": {"2024-03-08": {"status": "great"}}}',
])
def test_manifest_tolerates_truncated_or_foreign_file(tmp_path: Path, content: str) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    manifest, problem = load_manifest(path, _manifest())
    assert problem
    assert manifest.days == {} and manifest.symbol == "AAPL"
    assert not path.exists()
    assert path.with_name("manifest.json.unusable").read_text(encoding="utf-8") == content


def test_manifest_write_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    manifest = _manifest()
    manifest.record(date(2024, 3, 8), "ok", bars=390, fetched_utc=LATE)
    save_manifest(path, manifest)
    before = path.read_text(encoding="utf-8")
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json"]

    manifest.record(date(2024, 3, 11), "ok", bars=390, fetched_utc=LATE)

    def boom(src: str, dst: str) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        save_manifest(path, manifest)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before  # old file intact
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json"]  # no temp left

    # A reader holding the file (PermissionError on Windows) is retried with the injected sleep.
    real_replace = os.replace
    calls: list[int] = []

    def flaky(src: str, dst: str) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    sleeps: list[float] = []
    save_manifest(path, manifest, sleep=sleeps.append)
    assert len(sleeps) == 2
    assert "2024-03-11" in path.read_text(encoding="utf-8")


def test_manifest_rejects_bad_symbol(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        history.manifest_path(tmp_path, "../x")


def test_gap_report_minute_ranges() -> None:
    day = date(2024, 3, 11)
    minutes = expected_minutes(day)
    assert len(minutes) == 390
    assert minutes[0] == datetime(2024, 3, 11, 9, 30) and minutes[-1] == datetime(2024, 3, 11, 15, 59)
    present = [m for i, m in enumerate(minutes) if not (10 <= i <= 12 or i == 100 or i == 200)]
    present.append(datetime(2024, 3, 11, 16, 0))  # outside the session: ignored
    [row] = gap_report([day], bars_for(day, present))
    assert row.status == GAP_PARTIAL
    assert (row.expected, row.present, row.missing_count) == (390, 385, 5)
    assert row.missing == (
        (datetime(2024, 3, 11, 9, 40), datetime(2024, 3, 11, 9, 42)),
        (minutes[100], minutes[100]),
        (minutes[200], minutes[200]),
    )
    [full] = gap_report([day], bars_for(day))
    assert (full.status, full.missing) == (GAP_COMPLETE, ())


def test_gap_report_early_close_is_short() -> None:
    day = date(2024, 11, 29)  # day after Thanksgiving, closes 13:00
    minutes = expected_minutes(day)
    [row] = gap_report([day], bars_for(day, minutes[:210]))  # 09:30 to 12:59
    assert row.status == GAP_SHORT
    assert row.missing == ((datetime(2024, 11, 29, 13, 0), datetime(2024, 11, 29, 15, 59)),)
    # A hole before the end is partial, not short.
    holed = minutes[:100] + minutes[150:210]
    assert gap_report([day], bars_for(day, holed))[0].status == GAP_PARTIAL


def test_gap_report_short_means_exactly_a_13_00_close() -> None:
    day = date(2024, 11, 29)
    minutes = expected_minutes(day)
    assert minutes[210] == datetime(2024, 11, 29, 13, 0)  # the early close, first missing bar
    status = {name: gap_report([day], bars_for(day, mins))[0].status for name, mins in {
        "to_1259": minutes[:210],  # early close: short
        "to_1258": minutes[:209],  # tail missing but not from 13:00
        "to_1300": minutes[:211],
        "only_1559_missing": minutes[:-1],
        "one_bar": minutes[:1],
        "one_bar_late": minutes[100:101],
        "late_only": minutes[210:],
    }.items()}
    assert status == {"to_1259": GAP_SHORT, "to_1258": GAP_PARTIAL, "to_1300": GAP_PARTIAL,
                      "only_1559_missing": GAP_PARTIAL, "one_bar": GAP_PARTIAL,
                      "one_bar_late": GAP_PARTIAL, "late_only": GAP_PARTIAL}


def test_gap_report_missing_day(tmp_path: Path) -> None:
    days = [date(2024, 3, 11), date(2024, 3, 12)]
    rows = gap_report(days, bars_for(days[0]))
    assert [r.status for r in rows] == [GAP_COMPLETE, GAP_NO_DATA]
    assert rows[1].missing == ((datetime(2024, 3, 12, 9, 30), datetime(2024, 3, 12, 15, 59)),)
    empty = gap_report(days, pd.DataFrame({"date": pd.to_datetime([])}))
    assert [r.status for r in empty] == [GAP_NO_DATA, GAP_NO_DATA]

    csv_path, text_path = write_gap_files(tmp_path, rows, "AAPL")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "symbol,day,status,expected,present,missing,ranges"
    assert lines[1] == "AAPL,2024-03-11,complete,390,390,0,"
    assert lines[2] == "AAPL,2024-03-12,no_data,390,0,390,09:30-15:59"
    summary = text_path.read_text(encoding="utf-8")
    assert "no_data: 1" in summary and "complete: 1" in summary and "2024-03-12 no_data" in summary
    assert write_gap_files(tmp_path, rows, "AAPL") == (csv_path, text_path)  # rewrite is fine


def test_gap_report_converts_to_other_data_zone() -> None:
    minutes = expected_minutes(date(2024, 3, 11), "America/Los_Angeles")
    assert minutes[0] == datetime(2024, 3, 11, 6, 30) and minutes[-1] == datetime(2024, 3, 11, 12, 59)
    [row] = gap_report([date(2024, 3, 11)], bars_for(date(2024, 3, 11), minutes),
                       data_zone="America/Los_Angeles")
    assert row.status == GAP_COMPLETE
