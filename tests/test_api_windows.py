"""Unit tests of the shared window rules in `MainStoinker.api.windows`."""

from __future__ import annotations

from datetime import datetime

import pytest

from MainStoinker.api.windows import MAX_WINDOW_DAYS, WindowError, format_time, parse_window

DEFAULT = datetime(2024, 1, 10, 15, 35, 12)


def test_default_is_whole_calendar_day() -> None:
    lo, hi = parse_window(None, None, default_day=DEFAULT)
    assert lo == datetime(2024, 1, 10, 0, 0, 0)
    assert hi == datetime(2024, 1, 10, 23, 59, 59, 999999)


def test_default_at_midnight_and_end_of_day_keeps_the_day() -> None:
    for day in (datetime(2024, 1, 10), datetime(2024, 1, 10, 23, 59, 59, 999999)):
        assert parse_window(None, None, default_day=day) == (
            datetime(2024, 1, 10), datetime(2024, 1, 10, 23, 59, 59, 999999))


def test_default_without_day_is_error() -> None:
    with pytest.raises(WindowError):
        parse_window(None, None, default_day=None)


@pytest.mark.parametrize("pair", [("2024-01-02T09:30", None), (None, "2024-01-02T09:30")])
def test_exactly_one_bound_is_error(pair: tuple[str | None, str | None]) -> None:
    with pytest.raises(WindowError):
        parse_window(*pair, default_day=DEFAULT)


@pytest.mark.parametrize("sep", ["T", " "])
@pytest.mark.parametrize("text,expected", [
    ("2024-01-02{s}09:30", datetime(2024, 1, 2, 9, 30)),
    ("2024-01-02{s}09:30:15", datetime(2024, 1, 2, 9, 30, 15)),
    ("2024-01-02{s}09:30:15.5", datetime(2024, 1, 2, 9, 30, 15, 500000)),
    ("2024-01-02{s}09:30:15.123456", datetime(2024, 1, 2, 9, 30, 15, 123456)),
])
def test_accepted_forms(sep: str, text: str, expected: datetime) -> None:
    lo, hi = parse_window(text.format(s=sep), "2024-01-03T00:00", default_day=None)
    assert lo == expected and hi == datetime(2024, 1, 3)
    lo, hi = parse_window("2024-01-01T00:00", text.format(s=sep), default_day=None)
    assert lo == datetime(2024, 1, 1) and hi == expected


BAD = ["2024-01-02T09:30:00Z", "2024-01-02T09:30:00+01:00", "2024-01-02T09:30:00-05:00",
       "2024-01-02", "20240102", "", " ", "garbage", "2024-13-45T09:30:00",
       "2024-01-02T25:00:00", "2024-01-02T09:30:00.1234567", "2024-01-02T09", " 2024-01-02T09:30"]


@pytest.mark.parametrize("bad", BAD)
def test_bad_bound_is_error_without_echo(bad: str) -> None:
    good = "2024-01-03T09:30:00"
    for start, end, name in ((bad, good, "start"), (good, bad, "end")):
        with pytest.raises(WindowError) as info:
            parse_window(start, end, default_day=DEFAULT)
        message = str(info.value)
        assert name in message
        if bad.strip():
            assert bad not in message
        assert "\n" not in message


def test_end_before_start_is_error() -> None:
    with pytest.raises(WindowError) as info:
        parse_window("2024-01-03T09:30", "2024-01-03T09:29:59", default_day=None)
    assert "2024" not in str(info.value)
    lo, hi = parse_window("2024-01-03T09:30", "2024-01-03T09:30", default_day=None)
    assert lo == hi


def test_span_limit_boundary() -> None:
    assert MAX_WINDOW_DAYS == 7
    lo, hi = parse_window("2024-01-02T23:59:59", "2024-01-08T00:00:00", default_day=None)
    assert (hi.date() - lo.date()).days == 6  # 6 calendar days apart passes
    with pytest.raises(WindowError) as info:
        parse_window("2024-01-02T00:00", "2024-01-09T00:00", default_day=None)  # 7 apart fails
    assert "2024" not in str(info.value)


def test_span_limit_counts_dates_not_elapsed_time() -> None:
    parse_window("2024-01-02T00:00:00", "2024-01-08T23:59:59.999999", default_day=None)
    with pytest.raises(WindowError):
        parse_window("2024-01-02T23:59:59", "2024-01-09T00:00:00", default_day=None)


def test_span_limit_ignores_weekdays() -> None:
    # a Saturday-to-Friday window and a Monday-to-Sunday window are both allowed
    parse_window("2024-01-06T00:00", "2024-01-12T00:00", default_day=None)
    parse_window("2024-01-01T00:00", "2024-01-07T00:00", default_day=None)


def test_format_time() -> None:
    assert format_time(datetime(2024, 1, 2, 9, 30)) == "2024-01-02 09:30:00"
    assert format_time(datetime(2024, 1, 2, 9, 30, 1, 250000)) == "2024-01-02 09:30:01.250000"
