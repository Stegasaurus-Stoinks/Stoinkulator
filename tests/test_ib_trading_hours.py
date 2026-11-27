"""`MainStoinker/ib/trading_hours.py` (issue 285): why a day is closed, and IB's hours for a day."""

from __future__ import annotations

from datetime import date

import pytest

from MainStoinker.ib import trading_hours as th

THURSDAY = date(2024, 11, 28)  # Thanksgiving
FRIDAY = date(2024, 11, 29)  # the day after: closes 13:00
NORMAL = date(2024, 11, 27)
HOURS = ("20241127:0930-20241127:1600;20241128:CLOSED;20241129:0930-20241129:1300;"
         "20241130:CLOSED;20241201:CLOSED")


@pytest.mark.parametrize("day,words", [
    (date(2024, 11, 30), "a weekend (Saturday)"),
    (date(2024, 12, 1), "a weekend (Sunday)"),
    (THURSDAY, "a market holiday (Thanksgiving)"),
    (date(2024, 9, 2), "a market holiday (Labor Day)"),
    (date(2025, 1, 9), "a one-off closure"),
])
def test_closed_reason_names_the_cause(day: date, words: str) -> None:
    assert words in th.closed_reason(day)
    text = th.not_a_trading_day_text(day)
    assert text.startswith(f"{day.isoformat()} is not a trading day") and words in text


def test_the_callers_table_is_named_as_such() -> None:
    assert "caller's calendar table" in th.closed_reason(NORMAL, {NORMAL: None})


@pytest.mark.parametrize("day,expected", [
    (NORMAL, th.IbHours(th.OPEN, 16 * 60)),
    (FRIDAY, th.IbHours(th.OPEN, 13 * 60)),
    (THURSDAY, th.IbHours(th.CLOSED)),
    (date(2024, 11, 30), th.IbHours(th.CLOSED)),
])
def test_ib_hours_for_reads_a_day(day: date, expected: th.IbHours) -> None:
    assert th.ib_hours_for(HOURS, "US/Eastern", day) == expected


def test_several_sessions_of_one_day_use_the_outer_ends() -> None:
    text = "20241127:0930-20241127:1130,20241127:1200-20241127:1600"
    assert th.ib_hours_for(text, "America/New_York", NORMAL) == th.IbHours(th.OPEN, 16 * 60)


@pytest.mark.parametrize("text,zone,why", [
    ("", "US/Eastern", "no liquid hours"),
    ("   ", "US/Eastern", "no liquid hours"),
    (HOURS, "Europe/London", "time zone"),
    (HOURS, "", "time zone"),
    ("20241126:0930-20241126:1600", "US/Eastern", "does not mention the day"),
    ("20241127:0400-20241127:2000", "US/Eastern", "starts at 04:00"),
    ("20241127:0930-20241127:2000", "US/Eastern", "outside 09:31 to 16:00"),
    ("20241127:0930-20241128:0100", "US/Eastern", "crosses midnight"),
    ("20241127:0930-20241127:0930", "US/Eastern", "unreadable"),
    ("20241127:0930-20241127:2575", "US/Eastern", "unreadable"),
    ("20241127:CLOSED;20241127:0930-20241127:1600", "US/Eastern", "both closed and open"),
    ("garbage;;", "US/Eastern", "does not mention the day"),
])
def test_anything_unusual_is_unknown_never_a_guess(text: str, zone: str, why: str) -> None:
    answer = th.ib_hours_for(text, zone, NORMAL)
    assert answer.kind == th.UNKNOWN and why in answer.why, answer


def test_a_non_text_field_is_unknown() -> None:
    assert th.ib_hours_for(None, "US/Eastern", NORMAL).kind == th.UNKNOWN  # type: ignore[arg-type]
