"""Follow-ups from the T5 review of the session recorder (issue 156).

1. the committed-files check on a realistic recording, 2. `scan_recording` on market data,
3. docstring accuracy, 4. late farm messages in replay, 5. two cold-only exemptions, and the
three-letter paper account id (`DU`, one more upper-case letter, six digits) everywhere the
two-letter forms are caught.

No real account number appears in this file: every id is built at run time by `make_id`, and
`test_no_account_pattern_in_committed_files` scans this file too.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.ib import events
from MainStoinker.ib import recorder as rmod
from MainStoinker.ib.connection import ConnectionRestored
from MainStoinker.ib.recorder import Redactor, SessionRecorder, scan_recording
from tests.fake_ib import FakeIbClient
from tests.ib_replay import handshake_kwargs, install, load_recording, parse_recording, push_unsolicited
from tests.test_ib_recorder import (
    _COMMITTED_SHAPE,
    LETTERS,
    LONG_RUN,
    T0,
    Clock,
    collect,
    committed_recording_findings,
    committed_text_hits,
    ev,
    reasons,
    recorder,
    session,
    write_recording,
)
from tests.test_ib_recorder_extra import (
    CARRIERS,
    digits_of,
    make_id,
    realistic_details,
    survivors,
    written,
    ymd,
)

# the three-letter paper form: `DU`, one more upper-case letter, six digits
IDS3 = [make_id("DUX", 301, 6), make_id("DUA", 302, 6), make_id("DUZ", 303, 6)]
OTHER3 = make_id("DUQ", 399, 6)
A_DATE = "2024" + "0103"  # eight digits that are a valid date, built so no id-shaped text is in the source
NOT_A_DATE = "2024" + "1345"


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def clean(text: str) -> str:
    return " ".join(text.split())


# -- 1. the committed-files check on a realistic recording -------------------------------------------

_FIRST_DAY = datetime.date(2024, 8, 1)
_HOLIDAY = datetime.date(2024, 9, 2)


def two_weeks(open_from: str, open_to: str, first: datetime.date = _FIRST_DAY) -> str:
    """Fourteen days of IB hours: weekends and a holiday as `<date>:CLOSED`, each followed by a date."""
    parts = []
    for k in range(14):
        day = first + datetime.timedelta(days=k)
        if day.weekday() >= 5 or day == _HOLIDAY:
            parts.append(f"{ymd(day)}:CLOSED")
        else:
            parts.append(f"{ymd(day)}:{open_from}-{ymd(day)}:{open_to}")
    return ";".join(parts)


def realistic_recording(tmp_path: Path, name: str = "real.jsonl") -> Path:
    """What a real two-week history session writes: details with two weeks of hours, closed days
    followed by the next date, a head timestamp, bars, an end string, farm messages, ticks."""
    rec = SessionRecorder(tmp_path / name, clock=Clock(), command="fetch AAPL", port=7497,
                          start_utc="2024-08-01T14:30:00Z")
    d = realistic_details()
    d.tradingHours = two_weeks("0400", "2000", datetime.date(2024, 8, 17))  # starts on a closed day
    d.liquidHours = two_weeks("0930", "1600", datetime.date(2024, 8, 17))
    rec.on_raw("managedAccounts", (make_id("DU", 7),))
    rec.on_raw("error", (-1, 1700000000000, 2104, "Market data farm connection is OK:usfarm", ""))
    rec.on_request("reqContractDetails", (5, d.contract))
    rec.on_raw("contractDetails", (5, d))
    rec.on_raw("headTimestamp", (6, "345479400"))
    rec.on_raw("historicalDataEnd", (7, "20240830-14:30:00", "20240830-21:00:00"))
    rec.close()
    return tmp_path / name


def test_a_realistic_two_week_recording_passes_both_committed_file_checks(tmp_path: Path) -> None:
    path = realistic_recording(tmp_path)
    text = path.read_text(encoding="utf-8")
    raw = [m.group(0) for m in _COMMITTED_SHAPE.finditer(text)]
    # the bare shape takes the tail of CLOSED and the next date for an id
    assert len(raw) >= 8 and all(h.startswith("ED;") for h in raw)
    assert committed_text_hits(text, recording=True) == []
    assert committed_text_hits(text)  # any other kind of file is held to the bare shape
    assert committed_recording_findings(path) == []


def test_the_glued_date_exemption_of_the_committed_shape_is_the_recorders_and_no_wider() -> None:
    day = ymd(_FIRST_DAY)
    hours = f"{ymd(_HOLIDAY)}:CLOSED;{day}:0400-{day}:2000"
    assert committed_text_hits(hours, recording=True) == []
    assert committed_text_hits(hours)  # not for a source file
    # not a valid date, not preceded by a letter, or more than a time after it: still flagged
    assert committed_text_hits(f"CLOSED;{NOT_A_DATE}:0400", recording=True)
    assert committed_text_hits(f"note XY;{day} end", recording=True)  # no word tail
    assert committed_text_hits(f"x DU{day} y", recording=True)
    assert committed_text_hits(f"CLOSED;{day}9 end", recording=True)
    assert committed_text_hits("x " + make_id("DU", 8) + " y", recording=True)


def test_a_source_file_is_held_to_the_shape_as_strict_as_before_the_exemption() -> None:
    day = ymd(_FIRST_DAY)
    assert committed_text_hits(f"x = DU{day}")
    assert committed_text_hits("x = " + make_id("DUX", 9, 6))  # a three-letter id after an assignment
    assert committed_text_hits(f"x = 'CLOSED;{day}:0400'")
    assert committed_text_hits(f"x = DUX{day}")


def test_the_committed_files_check_flags_what_the_shape_alone_misses(tmp_path: Path) -> None:
    """Two separators, a JSON-escaped quote, two separators inside the digits, bare digits."""
    digits = digits_of(make_id("U", 51))
    for n, value in enumerate((
        f"DU: {digits}", f'DU"{digits}', f"{digits[:3]}: {digits[3:]}", digits,
        f"DU {digits[:2]}  {digits[2:]}",
    )):
        path = write_recording(tmp_path, ev(1, "error", -1, 0, 1, value, ""), name=f"m{n}.jsonl")
        assert committed_text_hits(path.read_text(encoding="utf-8")) == [], n  # the shape is blind to it
        assert committed_recording_findings(path), n  # scan_recording is not


# -- 2. scan_recording on market data ---------------------------------------------------------------


def rt_volume(i: int) -> str:
    flag = "true" if i % 2 else "false"
    return f"{187 + (i % 7) / 100:.2f};{100 + i};{T0 * 1000 + i * 137:013d};{1234567 + 311 * i};187.15;{flag}"


def test_a_thousand_ticks_of_market_data_leave_no_findings(tmp_path: Path) -> None:
    rec = SessionRecorder(tmp_path / "ticks.jsonl", clock=Clock())
    for i in range(250):
        rec.on_raw("tickPrice", (9, 4, 187.0 + i / 100, None))
        rec.on_raw("tickString", (9, 45, str(T0 + i)))
        rec.on_raw("tickString", (9, 48, rt_volume(i)))
        rec.on_raw("tickSize", (9, 5, 100 + i))
    rec.close()
    assert rec.failures == 0 and rec.lines_written == 1001
    assert scan_recording(tmp_path / "ticks.jsonl") == []


def test_known_tick_strings_are_kept_as_they_were_by_the_recorder(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("tickString", (9, 45, str(T0)))
    rec.on_raw("tickString", (9, 48, rt_volume(3)))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"] == [9, 45, str(T0)] and lines[2]["args"] == [9, 48, rt_volume(3)]


@pytest.mark.parametrize("args", [
    (9, 45, "9" * 10),  # an epoch after 2100
    (9, 45, "12345678"),  # eight digits are not an epoch
    (9, 45, str(T0) + "x"),
    (9, 46, str(T0)),  # another tick type
    (9, 48, "187.2;100;" + "9" * 13 + ";1234567;187.15;true"),  # a time after 2100
    (9, 48, "187.2;100;" + str(T0 * 1000) + ";1234567;187.15"),  # a field short
    (9, 48, "187.2;100;" + str(T0 * 1000) + ";1234567;187.15;yes"),
    (9, 48, "187.2;100;" + str(T0 * 1000) + ";1234567;187.15;true;" + "1234" + "56"),
    (9, 48, "187.2;" + "1" * 12 + ";" + str(T0 * 1000) + ";1234567;187.15;true"),  # a price field too long
    ("9", 45, str(T0)),  # not (int, int, str)
    (9, 45, 1704205800),
])
def test_other_shapes_of_a_tick_string_are_still_scanned(tmp_path: Path, args: tuple[Any, ...]) -> None:
    path = write_recording(tmp_path, ev(1, "tickString", *args))
    assert scan_recording(path)


def test_a_caller_word_or_a_letter_id_in_a_known_tick_string_is_still_found(tmp_path: Path) -> None:
    value = rt_volume(1)
    path = write_recording(tmp_path, ev(1, "tickString", 9, 48, value))
    assert scan_recording(path) == []
    assert reasons(scan_recording(path, words=["187.15"])) == ["caller-supplied word number 1"]
    prefixed = make_id("DUX", 5, 6) + value
    bad = write_recording(tmp_path, ev(1, "tickString", 9, 48, prefixed), name="bad.jsonl")
    assert LETTERS in reasons(scan_recording(bad))


# -- 3. docstring accuracy ----------------------------------------------------------------------------


def test_a_gap_of_three_characters_inside_the_digits_leaks_warm_as_well_as_cold(tmp_path: Path) -> None:
    account = make_id("DU", 61)
    digits = digits_of(account)
    cold = recorder(tmp_path, name="cold.jsonl")
    cold.on_raw("error", (-1, 1, 1, f"x {digits[:3]}   {digits[3:]} y", ""))
    cold.close()
    warm = recorder(tmp_path, name="warm.jsonl")
    warm.on_raw("managedAccounts", (account,))
    warm.on_raw("error", (-1, 1, 1, f"x {digits[:3]}   {digits[3:]} y", ""))
    warm.close()
    for name in ("cold.jsonl", "warm.jsonl"):
        assert f"{digits[:3]}   {digits[3:]}" in written(tmp_path / name), name  # the documented limit
    doc = clean(rmod.__doc__ or "")
    assert "more than two between digits (cold AND warm" in doc


def test_a_key_value_split_is_caught_only_when_the_key_ends_in_the_letters(tmp_path: Path) -> None:
    digits = digits_of(make_id("U", 62))
    red = Redactor()
    assert digits not in json.dumps(red.sanitise("raw", "custom", ({"DU": digits},)))
    assert digits not in json.dumps(Redactor().sanitise("raw", "custom", ({"acct DU": digits},)))
    assert digits in json.dumps(Redactor().sanitise("raw", "custom", ({"DU:": digits},)))  # the limit
    assert digits not in json.dumps(Redactor().sanitise("raw", "custom", ({"DU_": digits},)))
    assert "only when the key ends in the letters of the id" in clean(rmod.__doc__ or "")


# -- 4. replay and late farm messages -----------------------------------------------------------------


def late_farm_recording(tmp_path: Path) -> Path:
    return write_recording(
        tmp_path,
        ev(1, "connectAck"),
        ev(2, "nextValidId", 1),
        ev(3, "managedAccounts", "ACCOUNT-1"),
        ev(4, "error", -1, 0, 2104, "Market data farm connection is OK:usfarm", ""),
        ev(5, "reqCurrentTime", kind="request"),
        ev(6, "currentTime", 0),
        ev(7, "error", -1, 0, 2106, "HMDS data farm connection is OK:ushmds", ""),
        ev(8, "error", -1, 0, 1102, "Connectivity restored", ""),
        ev(9, "error", -1, 0, 2158, "Sec-def data farm connection is OK:secdefil", ""),
    )


def test_farm_messages_after_the_first_request_are_part_of_the_replay_handshake(tmp_path: Path) -> None:
    rec = load_recording(late_farm_recording(tmp_path))
    assert [u.args[2] for u in rec.unsolicited] == [2106, 1102, 2158]  # filed as unsolicited
    kw = handshake_kwargs(rec)
    assert [c for c, _ in kw["farm_messages"]] == [2104, 2106, 2158]  # in file order
    assert kw["managed_accounts"] == "ACCOUNT-1" and kw["next_valid_id"] == 1


def test_a_replayed_connection_sees_the_late_farm_messages_without_a_push(tmp_path: Path) -> None:
    rec = load_recording(late_farm_recording(tmp_path))
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["fake"] = fake
        install(fake, rec)

    with session(fake_kw=handshake_kwargs(rec), setup=setup) as (conn, fake):
        assert conn.wait_for_data_farm(5.0, farm="historical") is True  # code 2106, recorded late
        assert conn.wait_for_data_farm(5.0, farm="secdef") is True
        assert push_unsolicited(fake, rec) == 1  # only 1102: the farm messages are not delivered twice
        got = collect(conn, lambda e: any(isinstance(x, ConnectionRestored) for x in e))
    codes = [e.code for e in got if isinstance(e, events.IbError) and e.req_id == -1]
    assert codes.count(2106) == 1 and codes.count(2158) == 1  # once each, from the handshake


def test_only_farm_messages_with_request_id_minus_one_and_a_code_in_range_are_taken(tmp_path: Path) -> None:
    path = write_recording(
        tmp_path,
        ev(1, "nextValidId", 1),
        ev(2, "reqCurrentTime", kind="request"),
        ev(3, "error", 7, 0, 2104, "not id -1", ""),
        ev(4, "error", -1, 0, 2200, "out of range", ""),
        ev(5, "error", -1, 0, 2099, "out of range", ""),
        ev(6, "error", -1, 0, 2107, "in range", ""),
        ev(7, "error", -1, 0, 1100, "lost", ""),
    )
    rec = parse_recording(path.read_text(encoding="utf-8"))
    assert handshake_kwargs(rec)["farm_messages"] == ((2107, "in range"),)


# -- 5. the two cold-only exemptions ------------------------------------------------------------------


def test_a_non_ascii_letter_before_a_one_letter_prefix_does_not_make_a_date_shaped_id_a_word_tail() -> None:
    red = Redactor()
    out = red.sanitise("raw", "error", (-1, 0, 1, f"id \u00e9U{A_DATE} end", ""))[3]
    assert A_DATE not in out and "ACCOUNT-1" in out


def test_an_id_glued_after_an_ip_address_is_caught_cold() -> None:
    address = ".".join(("10", "1", "2", "3"))
    out = Redactor().sanitise("raw", "error", (-1, 0, 1, f"from {address}U{A_DATE} end", ""))[3]
    assert A_DATE not in out and "ACCOUNT-1" in out and address not in out


def test_the_trading_hours_glue_is_still_exempt() -> None:
    text = f"{'2024' + '0102'}:CLOSED;{A_DATE}:0400-{A_DATE}:2000"
    assert Redactor().sanitise("raw", "error", (-1, 0, 1, text, ""))[3] == text


# -- the three-letter account id -------------------------------------------------------------------------


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(("label", "name", "build"), CARRIERS, ids=[c[0] for c in CARRIERS])
def test_the_three_letter_id_in_every_carrier_leaves_no_trace_in_the_bytes(
    tmp_path: Path, label: str, name: str, build: Any, warm: bool
) -> None:
    for n, account in enumerate(IDS3):
        rec = SessionRecorder(tmp_path / f"r{n}.jsonl", clock=Clock())
        if warm:
            rec.on_raw("managedAccounts", (f"{account},{OTHER3}",))
        args = build(account)
        rec.on_raw(name, args)
        rec.on_request(name, args)
        rec.close()
        text = written(tmp_path / f"r{n}.jsonl")
        assert not survivors(text, account, digits_of(account)), (label, n)
        for line in text.splitlines():
            json.loads(line)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_the_three_letter_id_is_replaced_whole_leaving_no_letter_beside_the_placeholder(warm: bool) -> None:
    for account in IDS3:
        red = Redactor()
        if warm:
            red.sanitise("raw", "managedAccounts", (account,))
        text = f"Account {account} rejected; {account.lower()}.1 and {account}.USD"
        out = red.sanitise("raw", "error", (-1, 0, 1, text, ""))[3]
        # a sub-account suffix may make the lower-case spelling a second placeholder; nothing of it stays
        assert re.fullmatch(
            r"Account ACCOUNT-1 rejected; ACCOUNT-\d+(?:\.1)? and ACCOUNT-1\.USD", out
        ), (warm, out)


def test_the_three_letter_id_in_managed_accounts_is_learned_whole(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (f"{IDS3[0]},{IDS3[1]}",))
    rec.on_raw("error", (-1, 1, 2104, f"Farm OK for {IDS3[1].lower()} and {IDS3[0]}", ""))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"] == ["ACCOUNT-1,ACCOUNT-2"]
    assert lines[2]["args"][3] == "Farm OK for ACCOUNT-2 and ACCOUNT-1"
    assert rec.failures == 0


def test_the_three_letter_id_in_account_keyed_callbacks_is_replaced_whole() -> None:
    account = IDS3[0]
    red = Redactor()
    assert red.sanitise("raw", "position", (account, "x", 1, 2.0))[0] == "ACCOUNT-1"
    assert red.sanitise("raw", "accountSummary", (3, account, "NetLiquidation", "1", "USD"))[1] == "ACCOUNT-1"
    assert Redactor().sanitise("raw", "custom", ({"acctCode": account},)) == [{"acctCode": "ACCOUNT-1"}]


@pytest.mark.parametrize("template", [
    "C:\\Users\\{a}\\bars", "/home/{a}/x", "~/{a}/y", "file:///c:/{a}/z", "\\\\{a}\\share\\f",
])
def test_the_three_letter_id_in_a_path_is_gone(template: str) -> None:
    account = IDS3[0]
    out = Redactor().sanitise("raw", "error", (-1, 0, 1, "see " + template.format(a=account), ""))[3]
    assert not survivors(out, account, digits_of(account)) and out.startswith("see ")


def test_the_three_letter_id_in_a_relative_path_is_replaced_whole() -> None:
    account = IDS3[1]
    out = Redactor().sanitise("raw", "error", (-1, 0, 1, f"data/{account}/bars.csv", ""))[3]
    assert out == "data/ACCOUNT-1/bars.csv"


@pytest.mark.parametrize("parts", [
    lambda a: (a[:3], int(digits_of(a))),  # the three letters, the digits as an int
    lambda a: (a[:2], a[2:]),  # two letters, the third and the digits
    lambda a: (a[:1], a[1:3], int(digits_of(a))),  # one letter, two letters, the digits
    lambda a: (["DU", a[2:]],),
])
def test_the_three_letter_id_split_over_adjacent_arguments_is_wiped(parts: Any) -> None:
    account = IDS3[2]
    out = Redactor().sanitise("raw", "custom", parts(account))
    assert not survivors(json.dumps(out), digits_of(account), account[:3] + digits_of(account)[:2])
    assert digits_of(account)[:2] not in json.dumps(out)


def test_the_three_letter_id_is_learned_from_a_pattern_hit_and_found_elsewhere() -> None:
    account = IDS3[0]
    red = Redactor()
    assert red.sanitise("raw", "error", (-1, 0, 1, f"first {account}", ""))[3] == "first ACCOUNT-1"
    assert red.known_accounts == 1
    assert red.sanitise("raw", "custom", (digits_of(account), {"k": f"[{digits_of(account)}]"})) == [
        "ACCOUNT-1", {"k": "[ACCOUNT-1]"},
    ]


def test_the_three_letter_id_is_found_by_the_scanner_in_text_names_and_paths(tmp_path: Path) -> None:
    account = IDS3[0]
    digits = digits_of(account)
    path = write_recording(
        tmp_path,
        ev(1, "error", -1, 0, 1, f"note {account}", ""),
        ev(2, "managedAccounts", account),
        ev(3, "custom", {"owner": f"C:\\Users\\{account}\\x"}),
        ev(4, "custom", {account: "x"}),
        ev(5, "error", -1, 0, 1, f"{account[:3]} {digits}", ""),
    )
    found = scan_recording(path)
    by_line: dict[int, list[str]] = {}
    for f in found:
        by_line.setdefault(f.line, []).append(f.reason)
    assert LETTERS in by_line[2] and LONG_RUN in by_line[2]
    assert LETTERS in by_line[3] and LONG_RUN in by_line[3]
    assert "path" in by_line[4]
    assert 5 in by_line and 6 in by_line
    assert digits not in repr(found)


def test_the_three_letter_id_shape_is_in_the_committed_text_check() -> None:
    account = IDS3[1]
    assert committed_text_hits(f"x {account} y")
    assert Redactor().leaks_in_text(f"x {account} y")
    assert rmod.ACCOUNT_PATTERN.search(f"x {account} y").group(0) == account  # type: ignore[union-attr]


def test_the_id_shape_comment_in_the_docstring_names_the_three_letter_form() -> None:
    doc = clean(rmod.__doc__ or "")
    assert "one more upper-case letter and six digits" in doc and "one to three letters" in doc


def test_no_recording_checks_are_left_with_a_stale_two_letter_description() -> None:
    assert "one or two letters, then up to three separator" not in clean(rmod.__doc__ or "")


# -- fix round: three-letter prefix before a date-shaped number --------------------------------------------

_DAY = "2026" + "1124"  # a valid date, built so no id-shaped text is in the source
_NEXT = "2026" + "1125"
_DATE_FORMS = [
    ("alone", "{a}"),
    ("in a sentence", "Account {a} rejected"),
    ("in brackets", "[{a}]"),
    ("with a time", "{L}:{D}:0930"),
    ("after a word", "x {a} y"),
]
_DATE_IDS = [("DUX", _DAY), ("ABU", _DAY), ("abU", _DAY)]


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(("label", "form"), _DATE_FORMS, ids=[f[0] for f in _DATE_FORMS])
def test_a_three_letter_prefix_before_a_date_shaped_number_is_redacted_cold_and_warm(
    label: str, form: str, warm: bool
) -> None:
    for letters, day in _DATE_IDS:
        red = Redactor()
        if warm:
            red.sanitise("raw", "managedAccounts", (letters + day,))
        text = form.format(a=letters + day, L=letters, D=day)
        out = red.sanitise("raw", "error", (-1, 0, 1, text, ""))[3]
        # (a placeholder followed by `:0930` can be re-read as a hit and mangled; nothing leaks)
        assert day not in out and letters.lower() not in out.lower() and "ACCO" in out, (label, warm, out)


def test_real_trading_hours_and_closed_days_are_untouched_by_the_date_rule() -> None:
    texts = (
        two_weeks("0400", "2000") + f";{_DAY}:CLOSED;{_NEXT}:0400-{_NEXT}:2000",
        f"{_DAY}:0930-{_DAY}:1600;{_NEXT}:CLOSED;{_DAY}:0400-{_DAY}:2000",
    )
    for text in texts:
        red = Redactor()
        assert red.sanitise("raw", "error", (-1, 0, 1, text, ""))[3] == text
        assert red.known_accounts == 0 and not red.leaks(text)


# -- fix round: farm notices in replay -----------------------------------------------------------------------


def test_farm_broken_notices_stay_unsolicited_events_in_their_recorded_order(tmp_path: Path) -> None:
    path = write_recording(
        tmp_path,
        ev(1, "nextValidId", 1),
        ev(2, "error", -1, 0, 2104, "Market data farm connection is OK:usfarm", ""),
        ev(3, "error", -1, 0, 2106, "HMDS data farm connection is OK:ushmds", ""),
        ev(4, "reqCurrentTime", kind="request"),
        ev(5, "error", -1, 0, 2105, "HMDS data farm connection is broken:ushmds", ""),
        ev(6, "error", -1, 0, 1100, "Connectivity lost", ""),
        ev(7, "error", -1, 0, 2110, "Connectivity between TWS and server is broken", ""),
        ev(8, "error", -1, 0, 1102, "Connectivity restored", ""),
        ev(9, "error", -1, 0, 2106, "HMDS data farm connection is OK:ushmds", ""),
        ev(10, "error", -1, 0, 2105, "HMDS data farm connection is broken:ushmds", ""),
    )
    rec = parse_recording(path.read_text(encoding="utf-8"))
    assert [c for c, _ in handshake_kwargs(rec)["farm_messages"]] == [2104, 2106, 2106]
    pushed: list[Any] = []

    class _Sink:
        def push(self, name: str, *args: Any) -> None:
            pushed.append(args[2])

    assert push_unsolicited(_Sink(), rec) == 5  # type: ignore[arg-type]
    assert pushed == [2105, 1100, 2110, 1102, 2105]


def test_a_farm_broken_notice_in_the_handshake_itself_is_kept(tmp_path: Path) -> None:
    path = write_recording(
        tmp_path,
        ev(1, "nextValidId", 1),
        ev(2, "error", -1, 0, 2103, "Market data farm connection is broken:usfarm", ""),
        ev(3, "error", -1, 0, 2106, "HMDS data farm connection is OK:ushmds", ""),
    )
    rec = parse_recording(path.read_text(encoding="utf-8"))
    assert [c for c, _ in handshake_kwargs(rec)["farm_messages"]] == [2103, 2106]


# -- fix round: letters separated from the digits ------------------------------------------------------------

_SEPS = [" ", ": ", "-", "\\\"", " ", " - ", "_"]


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("sep", _SEPS, ids=["space", "colon", "dash", "escaped quote", "nbsp", "spaced dash",
                                            "underscore"])
@pytest.mark.parametrize("letters", ["DU", "DUX"])
def test_no_letter_of_an_id_prefix_stays_beside_the_placeholder_when_a_separator_splits_it(
    letters: str, sep: str, warm: bool
) -> None:
    digits = digits_of(make_id(letters, 77, 6 if len(letters) == 3 else 7))
    red = Redactor()
    if warm:
        red.sanitise("raw", "managedAccounts", (letters + digits,))
    out = red.sanitise("raw", "error", (-1, 0, 1, f"Account {letters}{sep}{digits} rejected", ""))[3]
    assert out == "Account ACCOUNT-1 rejected", out


def test_the_separator_rule_is_for_learned_ids_only_so_ordinary_words_are_not_widened() -> None:
    red = Redactor()
    red.sanitise("raw", "managedAccounts", (make_id("DUX", 78, 6),))
    assert red.sanitise("raw", "error", (-1, 0, 1, "see ZZZ words and no numbers", ""))[3] == (
        "see ZZZ words and no numbers"
    )
    assert red.known_accounts == 1


# -- fix round: the tick exemption --------------------------------------------------------------------------


def test_a_letter_prefixed_id_inside_a_type_48_value_is_still_flagged(tmp_path: Path) -> None:
    glued = make_id("DUX", 91, 6)
    fields = rt_volume(2).split(";")
    inside = ";".join([fields[0], glued, *fields[2:]])
    path = write_recording(tmp_path, ev(1, "tickString", 9, 48, inside))
    assert LETTERS in reasons(scan_recording(path))
    assert "is therefore NOT flagged" in clean(rmod.scan_recording.__doc__ or "")
