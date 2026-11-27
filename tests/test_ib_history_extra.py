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


def violations(events: list[tuple[float, int, str]], min_gap: float = 10.0) -> list[str]:
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
    per_contract: dict[int, list[float]] = defaultdict(list)
    identical: dict[tuple[int, str], list[float]] = defaultdict(list)
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


def punctual_send(pacer: Pacer, now: list[float], contract: int, request: str) -> None:
    reservation = pacer.reserve(contract, request)
    now[0] += reservation.wait
    pacer.sent(reservation)


def drive(count: int, seed: int, pattern: str, **kwargs: float) -> list[tuple[float, int, str]]:
    """Punctual caller: idles at random, reserves, waits exactly the answer, sends."""
    rnd = random.Random(seed)
    now = [1000.0]
    pacer = Pacer(lambda: now[0], **kwargs)
    symbols = [1, 2, 3, 4, 5]
    events = []
    for i in range(count):
        if pattern == "same":
            contract, request = 1, "r"
        elif pattern == "one_contract":
            contract, request = 1, f"r{i}"
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
        reservation = pacer.reserve(contract, request)
        assert reservation.wait >= 0
        now[0] += reservation.wait
        pacer.sent(reservation)
        events.append((now[0], contract, request))
    return events


def drive_late(count: int, seed: int, pattern: str, **kwargs: float) -> list[tuple[float, int, str]]:
    """Sloppy caller: 5% of sends up to 30 s late, one 20 minute suspend, ~3% of reservations cancelled.

    Returns the ACTUAL send times (the clock when `sent` was called).
    """
    rnd = random.Random(seed)
    now = [1000.0]
    pacer = Pacer(lambda: now[0], **kwargs)
    symbols = [1, 2, 3, 4, 5]
    events = []
    for i in range(count):
        if pattern == "same":
            contract, request = 1, "r"
        elif pattern == "one_contract":
            contract, request = 1, f"r{i}"
        elif pattern == "alternate":
            contract, request = symbols[i % 2], f"r{i % 2}"
        else:
            contract, request = rnd.choice(symbols), f"r{rnd.randrange(3)}"
        if rnd.random() < 0.1:
            now[0] += rnd.uniform(0, 5)
        reservation = pacer.reserve(contract, request)
        if rnd.random() < 0.03:
            pacer.cancel(reservation)
            continue
        now[0] += reservation.wait
        if rnd.random() < 0.05:
            now[0] += rnd.uniform(0, 30)
        if i == count // 2:
            now[0] += 20 * 60  # the machine was suspended
        pacer.sent(reservation)
        events.append((now[0], contract, request))
    return events


@pytest.mark.parametrize("pattern", ["same", "one_contract", "alternate", "random_repeats"])
@pytest.mark.parametrize("kwargs", [{}, {"min_gap": 0.0}, {"min_gap": 1.0}])
def test_pacer_late_sends_cancels_and_a_suspend_never_break_a_limit(
        pattern: str, kwargs: dict[str, float]) -> None:
    events = drive_late(3000, 17, pattern, **kwargs)
    assert len(events) > 2500
    assert violations(events, min_gap=kwargs.get("min_gap", 10.0)) == []


def test_pacer_late_send_delays_the_next_request() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    first = pacer.reserve(1, "a")
    assert first.wait == 0.0
    now[0] += 25.0  # the send is 25 s late
    pacer.sent(first)
    assert pacer.reserve(2, "b").wait == pytest.approx(10.0)  # 10 s after the REAL send


def test_pacer_second_reserve_raises_until_sent_or_cancelled() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    first = pacer.reserve(1, "a")
    with pytest.raises(history.ReservationOutstanding):
        pacer.reserve(2, "b")
    assert issubclass(history.ReservationOutstanding, RuntimeError)
    pacer.sent(first)
    now[0] += 10.0
    second = pacer.reserve(2, "b")  # works after sent()
    with pytest.raises(history.ReservationOutstanding):
        pacer.reserve(2, "c")
    pacer.cancel(second)
    third = pacer.reserve(2, "c")  # works after cancel()
    assert third.wait == 0.0
    pacer.cancel(third)


def test_pacer_the_reviewers_four_traces_cannot_be_expressed() -> None:
    # Late first send, identical key, out of order and 61 at once all need two open reservations.
    pacer = Pacer(lambda: 0.0)
    pacer.reserve(1, "k")
    for contract, request in ((2, "k2"), (1, "k"), (1, "other")):
        with pytest.raises(history.ReservationOutstanding):
            pacer.reserve(contract, request)
    # Taken one at a time and sent when due, 61 requests stay within the limits, whatever the lateness.
    now = [0.0]
    one = Pacer(lambda: now[0], min_gap=1.0)
    events = []
    for i in range(61):
        reservation = one.reserve(1 + i % 2, "k" if i % 3 == 0 else f"k{i}")
        now[0] += reservation.wait + (14.0 if i in (0, 7) else 0.0)  # two late sends
        one.sent(reservation)
        events.append((now[0], 1 + i % 2, "k" if i % 3 == 0 else f"k{i}"))
    assert violations(events, min_gap=1.0) == []


def test_pacer_cancel_releases_and_tokens_are_single_use() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    held = pacer.reserve(1, "a")
    pacer.cancel(held)
    again = pacer.reserve(1, "a")
    assert again.wait == 0.0  # the cancelled one left no trace
    with pytest.raises(ValueError):
        pacer.cancel(held)
    with pytest.raises(ValueError):
        pacer.sent(held)
    pacer.sent(again)
    with pytest.raises(ValueError):
        pacer.sent(again)


def test_pacer_early_send_is_recorded_then_raises_and_the_next_wait_counts_it() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    pacer.sent(pacer.reserve(1, "a"))  # at 0
    now[0] = 4.0
    second = pacer.reserve(2, "b")
    assert second.wait == pytest.approx(6.0)
    now[0] = 5.0  # sent 5 s early
    with pytest.raises(history.SentTooEarly, match="5.000") as excinfo:
        pacer.sent(second)
    assert excinfo.value.early_by == pytest.approx(5.0)
    assert isinstance(excinfo.value, RuntimeError)
    # History is truthful: the early send at 5 is recorded, so the next wait is 10 s after it.
    third = pacer.reserve(3, "c")
    assert third.planned == pytest.approx(15.0)
    pacer.cancel(third)
    # The reservation was settled: no second sent() and a new reserve is possible.
    with pytest.raises(ValueError):
        pacer.sent(second)


def test_pacer_on_time_send_does_not_raise_and_float_noise_is_tolerated() -> None:
    now = [0.1]
    pacer = Pacer(lambda: now[0])
    pacer.sent(pacer.reserve(1, "a"))
    second = pacer.reserve(2, "b")
    now[0] += second.wait  # float sums may land an ulp below planned
    pacer.sent(second)


def test_pacer_per_contract_window_is_two_seconds_not_one_and_a_half() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0], min_gap=0.0, margin=0.0)
    for i in range(5):
        pacer.sent(pacer.reserve(1, f"r{i}"))
    sixth = pacer.reserve(1, "r5")
    assert sixth.wait == pytest.approx(2.0)  # fails if the window is 1.5 s
    pacer.cancel(sixth)


@pytest.mark.parametrize("contract, request_key", [
    ("AAPL", "k"), (1.0, "k"), (True, "k"), (None, "k"), (object(), "k"),
    (1, 5), (1, None), (1, b"k"), (1, ("a", "b")),
])
def test_pacer_accepts_only_int_conid_and_str_request(contract: object, request_key: object) -> None:
    pacer = Pacer(lambda: 0.0)
    with pytest.raises(TypeError):
        pacer.reserve(contract, request_key)  # type: ignore[arg-type]
    pacer.cancel(pacer.reserve(1, "k"))  # nothing was left outstanding by the refused calls


def test_pacing_request_key_includes_the_conid_and_the_per_contract_rules_hold() -> None:
    request = day_request(date(2024, 3, 11))
    assert history.pacing_request_key(265598, request) == f"265598|{request.key()}"
    assert history.pacing_request_key(265598, request) != history.pacing_request_key(272093, request)
    with pytest.raises(TypeError):
        history.pacing_request_key("265598", request)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        history.pacing_request_key(True, request)  # type: ignore[arg-type]
    # The same day for two contracts is not identical; the same day twice for one is.
    now = [0.0]
    pacer = Pacer(lambda: now[0], min_gap=0.0)
    for con_id in (265598, 272093):
        reservation = pacer.reserve(con_id, history.pacing_request_key(con_id, request))
        assert reservation.wait == 0.0
        pacer.sent(reservation)
    again = pacer.reserve(265598, history.pacing_request_key(265598, request))
    assert again.wait == pytest.approx(16.0)
    pacer.cancel(again)


def test_pacer_memory_stays_bounded_over_100000_requests() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0], min_gap=0.0)
    for i in range(100_000):
        reservation = pacer.reserve(i, f"r{i}")  # a new contract and request every time
        now[0] += reservation.wait
        pacer.sent(reservation)
    assert len(pacer._all) <= pacer.max_in_window
    assert len(pacer._per_contract) < 200
    assert len(pacer._identical) < 200


@pytest.mark.parametrize("kwargs", [
    {"min_gap": -1.0}, {"margin": -0.5}, {"window": 0.0}, {"window": -600.0},
    {"contract_window": 0.0}, {"identical_gap": -1.0}, {"max_in_window": 0},
    {"max_per_contract": 0}, {"min_gap": float("nan")}, {"window": float("inf")},
    {"max_in_window": 1.5}, {"min_gap": True},
])
def test_pacer_rejects_nonsense_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Pacer(lambda: 0.0, **kwargs)  # type: ignore[arg-type]


def test_pacer_accepts_zero_gap_and_margin() -> None:
    Pacer(lambda: 0.0, min_gap=0.0, margin=0.0, identical_gap=0.0)


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
        punctual_send(pacer, now, [1, 2, 3][i % 3], f"day{i}")
    assert 7500 <= now[0] <= 7600  # 756 requests, 10 s apart plus the 1 s window margin


def test_pacer_long_idle_forgets_the_past() -> None:
    now = [0.0]
    pacer = Pacer(lambda: now[0])
    for i in range(60):
        punctual_send(pacer, now, 1, f"r{i}")
    now[0] += 100_000
    first = pacer.reserve(1, "r0")
    assert first.wait == 0.0
    pacer.cancel(first)
    again = pacer.reserve(1, "r0")
    assert again.wait == 0.0  # nothing of the past remains
    pacer.sent(again)
    assert pacer.reserve(2, "r0").wait == pytest.approx(10.0)  # only the global gap


def test_pacer_backwards_clock_never_shortens_a_wait() -> None:
    now = [100.0]
    pacer = Pacer(lambda: now[0])
    first = pacer.reserve(1, "r1")
    assert first.wait == 0.0
    pacer.sent(first)
    now[0] = 50.0  # clock jumps back 50 s
    assert 50.0 + pacer.reserve(1, "r2").wait >= 110.0  # still 10 s after the send at 100.0


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
        date(2023, 12, 28), date(2023, 12, 29), date(2024, 1, 2), date(2024, 1, 3)]  # no New Year's Day
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
    bars = frame(expected_minutes(date(2024, 3, 8)) + expected_minutes(day))
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


def test_manifest_second_unusable_file_does_not_destroy_the_first(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    for text in ("FIRST BAD", "SECOND BAD", "THIRD BAD"):
        path.write_text(text, encoding="utf-8")
        load_manifest(path, Manifest("AAPL", 1))
    kept = {p.name: p.read_text(encoding="utf-8") for p in path.parent.iterdir()}
    assert kept == {"manifest.json.unusable": "FIRST BAD", "manifest.json.unusable.1": "SECOND BAD",
                    "manifest.json.unusable.2": "THIRD BAD"}


def test_manifest_record_rejects_what_the_loader_rejects(tmp_path: Path) -> None:
    manifest = Manifest("AAPL", 1)
    day = date(2024, 3, 12)
    for kwargs in ({"error_code": "162"}, {"error_code": 1.5}, {"error_code": True},
                   {"error_text": 5}, {"error_text": None}, {"bars": 1.0}, {"bars": True}):
        with pytest.raises(TypeError):
            manifest.record(day, "failed", fetched_utc=LATE, **kwargs)  # type: ignore[arg-type]
    for status, bars in (("failed", -1), ("ok", 0), ("no_data", 3), ("bogus", 0)):
        with pytest.raises(ValueError):
            manifest.record(day, status, bars=bars, fetched_utc=LATE, complete_answer=True)
    with pytest.raises(TypeError):
        manifest.record(datetime(2024, 3, 12), "ok", bars=1, fetched_utc=LATE)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        manifest.record(day, "ok", bars=1, fetched_utc=datetime(2030, 1, 1))
    assert manifest.days == {}


def test_manifest_anything_record_accepts_loads_back_equal(tmp_path: Path) -> None:
    """Seeded random property test (no hypothesis in the dev requirements)."""
    rnd = random.Random(2024)
    bars_pool: list[object] = [0, 0, 1, 390, 2**70, -1, 1.5, True, "5", None]
    code_pool: list[object] = [None, None, 162, -1, 2**70, 0, "162", 1.5, True, b"x"]
    text_pool: list[object] = ["", "pacing", "x\nä\u0000\ud800", "é" * 50, 5, None, b"x"]
    path = history.manifest_path(tmp_path, "AAPL")
    accepted = 0
    for round_ in range(100):
        manifest = Manifest("AAPL", 1)
        for i in range(25):
            status = rnd.choice(["ok", "no_data", "failed", "failed", "failed", "bad", None])
            kwargs = {"bars": rnd.choice(bars_pool), "error_code": rnd.choice(code_pool),
                      "error_text": rnd.choice(text_pool)}
            moment = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=rnd.randrange(10**8))
            try:
                manifest.record(date(2024, 1, 1) + timedelta(days=i), status,  # type: ignore[arg-type]
                                fetched_utc=moment, complete_answer=True,
                                **kwargs)  # type: ignore[arg-type]
                accepted += 1
            except (TypeError, ValueError):
                pass
        save_manifest(path, manifest)
        loaded, problem = load_manifest(path, Manifest("AAPL", 1))
        assert problem is None, (round_, problem)
        assert loaded.days == manifest.days
    assert accepted > 150


def test_manifest_loader_uses_the_same_rules_as_record(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    for record in ({"status": "ok", "bars": 0}, {"status": "no_data", "bars": 4},
                   {"status": "failed", "error_code": "162"}, {"status": "failed", "error_text": 5},
                   {"status": "failed", "bars": -1}):
        path.write_text(_good(days={"2024-03-11": record}), encoding="utf-8")
        loaded, problem = load_manifest(path, Manifest("AAPL", 265598))
        assert problem and loaded.days == {}, record
    assert len(list(path.parent.iterdir())) == 5  # each one set aside, none overwritten


def test_manifest_loader_normalises_basic_iso_day_keys(tmp_path: Path) -> None:
    path = history.manifest_path(tmp_path, "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text(_good(days={"20240311": {"status": "ok", "bars": 390}}), encoding="utf-8")
    loaded, problem = load_manifest(path, Manifest("AAPL", 265598))
    assert problem is None and list(loaded.days) == ["2024-03-11"]
    assert loaded.pending([date(2024, 3, 11)]) == []
    # The same day twice, in both spellings, is not guessed at.
    path.write_text(_good(days={"20240311": {"status": "ok", "bars": 1},
                                "2024-03-11": {"status": "ok", "bars": 2}}), encoding="utf-8")
    loaded, problem = load_manifest(path, Manifest("AAPL", 265598))
    assert problem and loaded.days == {}


def test_manifest_mark_pending_forces_a_refetch() -> None:
    days = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)]
    manifest = Manifest("AAPL", 1)
    manifest.record(days[0], "ok", bars=390, fetched_utc=LATE)
    manifest.record(days[1], "no_data", fetched_utc=LATE, complete_answer=True)
    manifest.record(days[2], "ok", bars=100, fetched_utc=LATE)
    assert manifest.pending(days) == [days[3]]
    assert manifest.mark_pending([days[1], days[3]]) == [days[1]]  # days[3] had no record
    assert manifest.pending(days) == [days[1], days[3]]
    rows = [history.GapRow(days[0], GAP_COMPLETE, 390, 390, ()),
            history.GapRow(days[1], "no_data", 390, 0, ()),
            history.GapRow(days[2], "partial", 390, 100, ())]
    assert manifest.mark_gaps_pending(rows) == [days[2]]  # days[1] was already pending
    assert manifest.pending(days) == days[1:]
    assert days[0].isoformat() in manifest.days  # the complete day keeps its record


@pytest.mark.parametrize("symbol", [
    "CON", "con", "Prn", "AUX", "NUL", "COM1", "com9", "LPT1", "lpt9", "NUL.A", "AAPL.", "AAPL ", "A.B.",
])
def test_symbol_rejects_windows_reserved_names_and_trailing_dot_or_space(
        tmp_path: Path, symbol: str) -> None:
    with pytest.raises(ValueError):
        history.manifest_path(tmp_path, symbol)
    with pytest.raises(ValueError):
        history.symbol_folder(tmp_path, symbol)


@pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "COM", "COM10", "LPT0", "CONX", "NULL", "A-B"])
def test_symbol_accepts_ordinary_tickers(tmp_path: Path, symbol: str) -> None:
    assert history.manifest_path(tmp_path, symbol).parent == tmp_path / symbol


def test_end_date_time_string_has_one_home() -> None:
    moment = datetime(2024, 3, 11, 16, 0, tzinfo=NY)
    assert history.format_end_date_time(moment) == "20240311-20:00:00"
    assert history.format_end_date_time(moment.astimezone(UTC)) == "20240311-20:00:00"
    assert day_request(date(2024, 3, 11)).end_date_time == history.format_end_date_time(moment)
    with pytest.raises(ValueError):
        history.format_end_date_time(datetime(2024, 3, 11, 16, 0))
    assert "yyyymmdd-HH:mm:ss" in (history.format_end_date_time.__doc__ or "")


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
        b"symbol,day,status,expected,present,missing,ranges\n"
        b"AAPL,2024-03-11,partial,390,387,3,11:10-11:12\n"
        b"AAPL,2024-03-12,no_data,390,0,390,09:30-15:59\n")
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


def test_gap_files_are_written_per_symbol_folder_with_a_symbol_column(tmp_path: Path) -> None:
    day = date(2024, 3, 11)
    rows = gap_report([day], frame(expected_minutes(day)))
    folders = {s: history.symbol_folder(tmp_path, s) for s in ("AAPL", "MSFT")}
    for symbol, folder in folders.items():
        write_gap_files(folder, rows, symbol)
    assert folders["AAPL"] != folders["MSFT"]
    for symbol, folder in folders.items():
        lines = (folder / "gaps.csv").read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("symbol,day,") and lines[1].startswith(f"{symbol},2024-03-11,")
    with pytest.raises(ValueError):
        write_gap_files(tmp_path, rows, "CON")


def _stamped(stamps: list[datetime], tz: str | None = None) -> pd.DataFrame:
    frame_ = frame(stamps)
    if tz:
        frame_["date"] = pd.to_datetime(frame_["date"]).dt.tz_localize(tz)
    return frame_


@pytest.mark.parametrize("call", [
    lambda b: gap_report([date(2024, 3, 11)], b),
    lambda b: filter_day(b, date(2024, 3, 11), LATE),
], ids=["gap_report", "filter_day"])
def test_bad_bar_input_raises_instead_of_silently_matching_nothing(call: object) -> None:
    minutes = expected_minutes(date(2024, 3, 11))[:5]
    with pytest.raises(ValueError, match="timezone-aware"):
        call(_stamped(minutes, "America/New_York"))  # type: ignore[operator]
    with pytest.raises(ValueError, match="whole minutes"):
        call(frame([minutes[0] + timedelta(seconds=30), minutes[1]]))  # type: ignore[operator]
    with pytest.raises(ValueError, match="whole minutes"):
        call(frame([minutes[0] + timedelta(microseconds=1), minutes[1]]))  # type: ignore[operator]


def test_filter_day_raises_on_bars_of_the_day_outside_the_session_but_drops_other_dates() -> None:
    day = date(2024, 3, 11)
    minutes = expected_minutes(day)
    for stray in (datetime(2024, 3, 11, 9, 29), datetime(2024, 3, 11, 16, 0),
                  datetime(2024, 3, 11, 20, 0), datetime(2024, 3, 11, 4, 0)):
        stamps = sorted([*minutes, stray])
        with pytest.raises(ValueError, match="regular-hours New York"):
            filter_day(frame(stamps), day, LATE)
    # Bars of other dates (the previous session, the next day) go quietly, whatever their time.
    other = [datetime(2024, 3, 8, 9, 29), datetime(2024, 3, 8, 15, 59), *minutes,
             datetime(2024, 3, 12, 9, 30), datetime(2024, 3, 12, 20, 0)]
    assert list(filter_day(frame(other), day, LATE)["date"]) == minutes
    with pytest.raises(ValueError, match="duplicate"):
        filter_day(frame([minutes[0], minutes[1], minutes[1]]), day, LATE)
    with pytest.raises(ValueError, match="not sorted"):
        filter_day(frame([minutes[1], minutes[0]]), day, LATE)
    # Nothing was sorted or de-duplicated behind the caller's back.
    assert filter_day(frame(minutes[:2]), day, LATE).shape[0] == 2


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


def test_filter_day_pacific_stamped_session_read_as_new_york_raises_not_a_half_day() -> None:
    day = date(2024, 3, 11)
    # A full session stamped in Pacific wall time: 06:30 to 12:59.
    pacific = [m - timedelta(hours=3) for m in expected_minutes(day)]
    with pytest.raises(ValueError, match="do not look like regular-hours New York times"):
        filter_day(frame(pacific), day, LATE)
    # Read as the zone it really is, the same bars are a full day.
    kept = filter_day(frame(pacific), day, LATE, data_zone="America/Los_Angeles")
    assert len(kept) == 390


def test_filter_day_in_other_data_zone_still_drops_other_sessions_quietly() -> None:
    day = date(2024, 3, 11)
    zone = "Asia/Tokyo"  # the session crosses local midnight
    today = expected_minutes(day, zone)
    friday = expected_minutes(date(2024, 3, 8), zone)
    assert list(filter_day(frame(friday + today), day, LATE, data_zone=zone)["date"]) == today


def test_manifest_no_data_needs_a_complete_answer() -> None:
    manifest = Manifest("AAPL", 1)
    day = date(2024, 3, 12)
    with pytest.raises(ValueError, match="complete_answer"):
        manifest.record(day, "no_data", fetched_utc=LATE)
    with pytest.raises(ValueError, match="complete_answer"):
        manifest.record(day, "no_data", fetched_utc=LATE, complete_answer=False)
    assert manifest.days == {}
    manifest.record(day, "no_data", fetched_utc=LATE, complete_answer=True)
    assert manifest.days["2024-03-12"].status == "no_data"
    # A pacing violation (error 162 with that text) is a failure and needs no such flag.
    manifest.record(day, "failed", error_code=162, error_text="pacing violation", fetched_utc=LATE)
    assert manifest.pending([day]) == [day]
