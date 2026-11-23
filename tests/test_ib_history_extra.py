"""Extra checks for `MainStoinker/ib/history.py`: independent pacing checker, edges, known defects."""

from __future__ import annotations

import ast
import json
import random
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from MainStoinker.ib import history
from MainStoinker.ib.history import (
    GAP_COMPLETE,
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
REPO = Path(__file__).resolve().parent.parent


def frame(stamps: list[datetime]) -> pd.DataFrame:
    return pd.DataFrame({"date": stamps, "time": 0, "open": 1.0, "high": 1.0,
                         "low": 1.0, "close": 1.0, "volume": 1})


# --- pacing: an independent checker over many requests -----------------------


def violations(events: list[tuple[float, str, str]], min_gap: float = 10.0) -> list[str]:
    """Rule breaks in (send time, contract, request) events; every window is closed at both ends."""
    ordered = sorted(events)
    times = [e[0] for e in ordered]
    found = []
    for i in range(len(times) - 60):
        if times[i + 60] - times[i] <= 600:
            found.append(f"61 requests within 600 s at {times[i]}")
    for i in range(1, len(times)):
        if times[i] - times[i - 1] < min_gap - 1e-6:  # float noise from summing waits
            found.append(f"gap {times[i] - times[i - 1]} at {times[i]}")
    per_contract: dict[str, list[float]] = defaultdict(list)
    identical: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t, contract, request in ordered:
        per_contract[contract].append(t)
        identical[(contract, request)].append(t)
    for contract, seen in per_contract.items():
        for i in range(len(seen) - 5):
            if seen[i + 5] - seen[i] <= 2:
                found.append(f"6 requests for {contract} within 2 s at {seen[i]}")
    for key, seen in identical.items():
        for a, b in zip(seen, seen[1:], strict=False):
            if b - a <= 15:
                found.append(f"identical request {key} again after {b - a} s")
    return found


def drive(count: int, seed: int, pattern: str, **kwargs: float) -> list[tuple[float, str, str]]:
    """Punctual caller: idles at random, reserves, waits exactly the answer, sends."""
    rnd = random.Random(seed)
    now = [1000.0]
    pacer = Pacer(lambda: now[0], **kwargs)
    symbols = ["AAPL", "MSFT", "TSLA", "X", "Y"]
    events = []
    for i in range(count):
        if pattern == "same":
            contract, request = "AAPL", "r"
        elif pattern == "one_contract":
            contract, request = "AAPL", f"r{i}"
        elif pattern == "alternate":
            contract, request = symbols[i % 2], f"r{i % 2}"
        elif pattern == "random_repeats":
            contract, request = rnd.choice(symbols), f"r{rnd.randrange(3)}"
        else:
            contract, request = rnd.choice(symbols), f"r{i}"
        roll = rnd.random()
        if roll < 0.1:
            now[0] += rnd.uniform(0, 5)
        elif roll < 0.12:
            now[0] += rnd.uniform(100, 5000)
        wait = pacer.reserve(contract, request)
        assert wait >= 0
        now[0] += wait
        events.append((now[0], contract, request))
    return events


@pytest.mark.parametrize("pattern", ["same", "one_contract", "alternate", "random_repeats", "unique"])
@pytest.mark.parametrize("kwargs", [{}, {"min_gap": 0.0}, {"min_gap": 1.0}])
def test_pacer_random_load_never_breaks_a_limit(pattern: str, kwargs: dict[str, float]) -> None:
    events = drive(3000, 11, pattern, **kwargs)
    assert violations(events, min_gap=kwargs.get("min_gap", 10.0)) == []


def test_pacer_is_deterministic() -> None:
    assert drive(500, 3, "random_repeats") == drive(500, 3, "random_repeats")


def test_pacer_year_of_three_symbols_takes_about_two_hours() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    for i in range(252 * 3):
        now[0] += pacer.reserve(["AAPL", "MSFT", "TSLA"][i % 3], f"day{i}")
    assert 7500 <= now[0] <= 7600  # 756 requests, 10 s apart plus the 1 s window margin


def test_pacer_long_idle_forgets_the_past() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    for i in range(60):
        now[0] += pacer.reserve("c", f"r{i}")
    now[0] += 100_000
    assert pacer.reserve("c", "r0") == 0.0
    assert pacer.reserve("d", "r0") == pytest.approx(10.0)  # only the global gap remains


def test_pacer_backwards_clock_never_shortens_a_wait() -> None:
    now = [100.0]
    pacer = Pacer(lambda: now[0])
    assert pacer.reserve("c", "r1") == 0.0
    now[0] = 50.0  # clock jumps back 50 s
    assert 50.0 + pacer.reserve("c", "r2") >= 110.0  # still 10 s after the booked 100.0


# --- day planning edges -----------------------------------------------------


@pytest.mark.parametrize("zone, hour, expected", [
    ("Pacific/Honolulu", 10, True), ("Pacific/Honolulu", 9, False),
    ("America/Phoenix", 13, True), ("America/Phoenix", 12, False),
    ("UTC", 20, True), ("UTC", 19, False),
    ("America/New_York", 16, True), ("America/New_York", 15, False),
])
def test_plan_close_boundary_in_other_zones(zone: str, hour: int, expected: bool) -> None:
    day = date(2024, 3, 11)  # EDT, closes 20:00 UTC
    exact = datetime(2024, 3, 11, hour, 0, 0, tzinfo=ZoneInfo(zone))
    if expected:
        assert plan_days(day, day, exact) == [day]
        exact -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="not closed"):
        plan_days(day, day, exact)


def test_plan_year_boundary_and_leap_day() -> None:
    assert plan_days(date(2023, 12, 28), date(2024, 1, 3), LATE) == [
        date(2023, 12, 28), date(2023, 12, 29), date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
    assert plan_days(date(2024, 2, 28), date(2024, 3, 1), LATE) == [
        date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1)]
    one = date(2024, 3, 11)
    assert plan_days(one, one, LATE) == [one]
    assert plan_days(date(2024, 3, 9), date(2024, 3, 10), LATE) == []


@pytest.mark.parametrize("day, end", [
    (date(2024, 3, 8), "20240308-21:00:00"), (date(2024, 3, 11), "20240311-20:00:00"),
    (date(2024, 11, 1), "20241101-20:00:00"), (date(2024, 11, 4), "20241104-21:00:00"),
    (date(2025, 3, 7), "20250307-21:00:00"), (date(2025, 3, 10), "20250310-20:00:00"),
    (date(2025, 10, 31), "20251031-20:00:00"), (date(2025, 11, 3), "20251103-21:00:00"),
    (date(2024, 2, 29), "20240229-21:00:00"), (date(2023, 12, 29), "20231229-21:00:00"),
])
def test_day_request_around_both_dst_changes(day: date, end: str) -> None:
    request = day_request(day)
    assert request.end_date_time == end
    # IB's UTC form is yyyymmdd-hh:mm:ss, and it must read back as 16:00 New York on `day`.
    back = datetime.strptime(end, "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC).astimezone(NY)
    assert (back.date(), back.hour, back.minute, back.second) == (day, 16, 0, 0)
    assert (request.duration, request.bar_size, request.use_rth) == ("1 D", "1 min", 1)


# --- filter_day -------------------------------------------------------------


def test_filter_day_does_not_change_its_input() -> None:
    day = date(2024, 3, 11)
    bars = frame(expected_minutes(day) + expected_minutes(date(2024, 3, 8)))
    before = bars.copy()
    kept = filter_day(bars, day, LATE)
    assert len(kept) == 390
    pd.testing.assert_frame_equal(bars, before)
    kept.loc[0, "close"] = 99.0
    pd.testing.assert_frame_equal(bars, before)


def test_filter_day_empty_response_and_exact_minute_end() -> None:
    day = date(2024, 3, 11)
    assert filter_day(frame([]), day, LATE).empty
    last = [datetime(2024, 3, 11, 15, 59)]
    assert len(filter_day(frame(last), day, datetime(2024, 3, 11, 16, 0, tzinfo=NY))) == 1
    assert filter_day(frame(last), day, datetime(2024, 3, 11, 15, 59, 59, tzinfo=NY)).empty


def test_epoch_to_new_york_matches_the_committed_bars() -> None:
    """The `time` column is epoch seconds; New York wall time of it is exactly 09:30 to 15:59 per day."""
    raw = pd.read_csv(REPO / "OfflineData" / "_AAPL_offlinedata_")
    stamps = [datetime.fromtimestamp(int(t), NY).replace(tzinfo=None) for t in raw["time"]]
    days = sorted({s.date() for s in stamps})
    assert len(days) >= 5
    for row in gap_report(days, pd.DataFrame({"date": stamps})):
        assert (row.status, row.present) == (GAP_COMPLETE, 390), row.day
    for one in days:
        assert len(filter_day(frame(stamps), one, LATE)) == 390


# --- manifest ---------------------------------------------------------------


def _good(**changes: object) -> str:
    raw: dict[str, object] = {
        "version": 1, "symbol": "AAPL", "conId": 265598, "data_zone": "America/New_York",
        "use_rth": 1, "what_to_show": "TRADES", "bar_size": "1 min",
        "days": {"2024-03-11": {"status": "ok", "bars": 390}}}
    raw.update(changes)
    return json.dumps(raw)


def test_manifest_matching_file_is_accepted_and_foreign_fields_are_ignored(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text(_good(note="hand edited"), encoding="utf-8")
    loaded, problem = load_manifest(path, Manifest("AAPL", 265598))
    assert problem is None and loaded.days["2024-03-11"].bars == 390


@pytest.mark.parametrize("changes", [
    {"conId": 1}, {"data_zone": "America/Los_Angeles"}, {"bar_size": "5 mins"},
    {"what_to_show": "MIDPOINT"}, {"use_rth": 0}, {"symbol": "aapl"}, {"version": 2},
])
def test_manifest_for_other_identity_is_set_aside_with_its_content(
        tmp_path: Path, changes: dict[str, object]) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text(_good(**changes), encoding="utf-8")
    loaded, problem = load_manifest(path, Manifest("AAPL", 265598))
    assert problem and loaded.days == {}
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json.unusable"]
    assert json.loads((path.parent / "manifest.json.unusable").read_text(encoding="utf-8"))["days"]


@pytest.mark.parametrize("symbol", ["..", "../x", "a/b", "a\\b", "", " ", "AAPL\n", "-x", ".x", "a:b"])
def test_manifest_path_rejects_unsafe_symbols(tmp_path: Path, symbol: str) -> None:
    with pytest.raises(ValueError):
        history.manifest_path(tmp_path, symbol)


def test_manifest_save_leaves_only_the_manifest_and_reloads_equal(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "BRK.B")
    manifest = Manifest("BRK.B", 72063691)
    manifest.record(date(2024, 3, 11), "ok", bars=390, fetched_utc=LATE)
    manifest.record(date(2024, 3, 12), "failed", error_code=162, error_text="HMDS: no data\nline 2",
                    fetched_utc=LATE)
    save_manifest(path, manifest)
    save_manifest(path, manifest)  # second write over the first
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json"]
    loaded, problem = load_manifest(path, Manifest("BRK.B", 72063691))
    assert problem is None and loaded.days == manifest.days


def test_manifest_save_failing_while_writing_keeps_old_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    manifest = Manifest("AAPL", 1)
    manifest.record(date(2024, 3, 11), "ok", bars=1, fetched_utc=LATE)
    save_manifest(path, manifest)
    before = path.read_bytes()
    manifest.record(date(2024, 3, 12), "ok", bars=2, fetched_utc=LATE)

    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(history.os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        save_manifest(path, manifest)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json"]


@pytest.mark.xfail(strict=True, reason="a second unusable manifest overwrites the first .unusable file")
def test_manifest_second_unusable_file_does_not_destroy_the_first(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text("FIRST BAD", encoding="utf-8")
    load_manifest(path, Manifest("AAPL", 1))
    path.write_text("SECOND BAD", encoding="utf-8")
    load_manifest(path, Manifest("AAPL", 1))
    kept = [p.read_text(encoding="utf-8") for p in path.parent.iterdir()]
    assert "FIRST BAD" in kept and "SECOND BAD" in kept


@pytest.mark.xfail(strict=True, reason="record() accepts an error_code that the loader then rejects")
def test_manifest_written_by_the_module_always_loads(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    manifest = Manifest("AAPL", 1)
    manifest.record(date(2024, 3, 11), "ok", bars=390, fetched_utc=LATE)
    try:
        manifest.record(date(2024, 3, 12), "failed", error_code="162", fetched_utc=LATE)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return  # rejecting it at record() time is the right fix too
    save_manifest(path, manifest)
    loaded, problem = load_manifest(path, Manifest("AAPL", 1))
    assert problem is None and "2024-03-11" in loaded.days


# --- gap report -------------------------------------------------------------


def test_gap_report_counts_duplicate_minutes_once_and_ignores_unsorted_input() -> None:
    day = date(2024, 3, 11)
    minutes = expected_minutes(day)
    shuffled = minutes + minutes[:10]
    random.Random(5).shuffle(shuffled)
    [row] = gap_report([day], frame(shuffled))
    assert (row.status, row.present, row.missing) == (GAP_COMPLETE, 390, ())


def test_gap_report_day_missing_only_its_last_minute_and_a_one_bar_day() -> None:
    day = date(2024, 3, 11)
    minutes = expected_minutes(day)
    [last] = gap_report([day], frame(minutes[:-1]))
    assert last.missing == ((minutes[-1], minutes[-1]),) and last.missing_count == 1
    [one] = gap_report([day], frame(minutes[100:101]))
    assert one.present == 1 and one.missing_count == 389
    assert [one.missing[0][0], one.missing[0][1], one.missing[1][0], one.missing[1][1]] == [
        minutes[0], minutes[99], minutes[101], minutes[-1]]


def test_gap_files_have_exact_columns_and_unix_line_endings(tmp_path: Path) -> None:
    day = date(2024, 3, 11)
    minutes = expected_minutes(day)
    rows = gap_report([day, date(2024, 3, 12)], frame(minutes[:100] + minutes[103:]))
    csv_path, text_path = write_gap_files(tmp_path, rows, "AAPL")
    assert csv_path.read_bytes() == (
        b"day,status,expected,present,missing,ranges\n"
        b"2024-03-11,partial,390,387,3,11:10-11:12\n"
        b"2024-03-12,no_data,390,0,390,09:30-15:59\n")
    summary = text_path.read_text(encoding="utf-8")
    assert summary.startswith("Gap report for AAPL: 2 days\n")
    assert b"\r" not in text_path.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gaps.csv", "gaps_summary.txt"]


def test_gap_report_in_other_data_zone_across_dst() -> None:
    for day in (date(2024, 3, 8), date(2024, 3, 11), date(2024, 11, 1), date(2024, 11, 4)):
        zone = "America/Los_Angeles"
        minutes = expected_minutes(day, zone)
        assert (minutes[0].hour, minutes[0].minute) == (6, 30) and len(minutes) == 390
        [row] = gap_report([day], frame(minutes), data_zone=zone)
        assert row.status == GAP_COMPLETE
    utc_minutes = expected_minutes(date(2024, 3, 8), "UTC")
    assert utc_minutes[0] == datetime(2024, 3, 8, 14, 30)  # EST
    assert expected_minutes(date(2024, 3, 11), "UTC")[0] == datetime(2024, 3, 11, 13, 30)  # EDT


# --- purity -----------------------------------------------------------------


def test_history_module_reads_no_clock_and_opens_nothing() -> None:
    tree = ast.parse((REPO / "MainStoinker" / "ib" / "history.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "utcnow", "today", "time", "sleep", "monotonic"}, (
                f"line {node.lineno} calls a clock or sleeps")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            assert not any(n.split(".")[0] in {"ibapi", "socket", "requests", "urllib"} for n in names)
