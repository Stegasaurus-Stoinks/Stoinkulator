"""Tester additions for the session recorder (T5, issue 118).

Leak hunt on the written bytes, failure behaviour, replay edge cases. No real account
number appears in this file: every id is built at run time by `make_id`, and
`test_extra_files_hold_no_account_shaped_text` scans this file, the recorder, the replay
helper and the committed recordings with a stricter pattern than the recorder's own.

Known gaps are `xfail(strict=True)`: when one is fixed the marker has to go.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import errno
import inspect
import json
import random
import re
import threading
import types
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.contract import Contract, ContractDetails
from ibapi.tag_value import TagValue

from MainStoinker.ib import events
from MainStoinker.ib import recorder as rmod
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.recorder import Redactor, SessionRecorder, scan_recording
from tests.fake_ib import FakeIbClient, make_bar
from tests.ib_replay import (
    SYNTHETIC_CURRENT_TIME,
    decode_callback,
    handshake_kwargs,
    install,
    load_recording,
    parse_recording,
)
from tests.test_ib_recorder import (
    LETTERS,
    LONG_RUN,
    RECORDINGS,
    T0,
    WAIT,
    A,
    Clock,
    collect,
    committed_recorder_files,
    contract,
    ev,
    normalise_ids,
    read_lines,
    reasons,
    recorder,
    session,
    settle,
    write_recording,
)


def make_id(prefix: str, seed: int, width: int = 7) -> str:
    """An account-shaped id built at run time from a seeded random source."""
    rng = random.Random(seed)
    digits = "".join(rng.choice("0123456789") for _ in range(width - 1)) + str(rng.randrange(1, 10))
    return prefix + digits


def digits_of(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def written(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def folded(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")


def survivors(text: str, *needles: str) -> list[str]:
    low = (text + "\n" + folded(text)).lower()
    return [n for n in needles if n and n.lower() in low]


# every id shape IB uses: individual, paper, advisor, paper advisor, institution, paper institution
FORMS = [
    ("U", 7), ("DU", 7), ("F", 7), ("DF", 7), ("I", 7), ("DI", 7), ("U", 8), ("I", 9),
]
IDS = [make_id(p, 100 + i, w) for i, (p, w) in enumerate(FORMS)]
OTHER = make_id("U", 999)


def fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)


class _Obj:
    pass


@dataclasses.dataclass
class _DC:
    note: Any
    n: int = 1


@dataclasses.dataclass(slots=True)
class _DCS:
    note: Any


class _Slotted:
    __slots__ = ("note",)

    def __init__(self, note: Any) -> None:
        self.note = note


def _ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


# (label, callback, args builder). Every builder takes the id and returns the callback's arguments.
CARRIERS: list[tuple[str, str, Any]] = [
    ("error text", "error", lambda a: (-1, 1700000000000, 2104, f"Account {a} rejected", "")),
    ("error lower", "error", lambda a: (-1, 1, 1, f"x {a.lower()} y", "")),
    ("error json", "error", lambda a: (-1, 1, 1, "x", json.dumps({"orders": [{"acct": a}]}))),
    ("error json twice", "error", lambda a: (-1, 1, 1, json.dumps(json.dumps({"acct": a})), "")),
    ("error xml", "error", lambda a: (-1, 1, 1, f'<group name="{a}"><account>{a}</account></group>', "")),
    ("error sub-account", "error", lambda a: (-1, 1, 1, f"{a}.1 / {a}.USD / {a}.Model", "")),
    ("error glued", "error", lambda a: (-1, 1, 1, f"abc{a}def and 77{a}88", "")),
    ("error full width", "error", lambda a: (-1, 1, 1, fullwidth(a), "")),
    ("error zero width", "error", lambda a: (-1, 1, 1, "\u200b".join(a), "")),
    ("error escapes", "error", lambda a: (-1, 1, 1, f"line\\n{a}\\t{a}\\\\{a}", "")),
    ("bytes", "custom", lambda a: (a.encode(), bytearray(a.encode()), memoryview(a.encode()))),
    ("bytes utf-16", "custom", lambda a: (a.encode("utf-16"), a.encode("utf-16-be"), a.encode("utf-32"))),
    ("dict key", "custom", lambda a: ({a: 1, "x": {a: [a]}},)),
    ("tuple set frozenset deque", "custom",
     lambda a: ((a,), {a}, frozenset([a]), collections.deque([a]))),
    ("dataclasses", "custom", lambda a: (_DC(a), _DCS(a), _Slotted(a))),
    ("exceptions", "custom", lambda a: (ValueError(a), RuntimeError("x", {"k": [a]}), KeyError(a))),
    ("attribute name", "custom", lambda a: (_ns(**{a: 1}),)),
    ("object attrs", "custom", lambda a: (_ns(owner=_ns(note=f"is {a}")),)),
    ("split strings", "custom", lambda a: (a[:3], a[3:])),
    ("split str int", "custom", lambda a: ("".join(c for c in a if c.isalpha()), int(digits_of(a)))),
    ("split list", "custom", lambda a: (["".join(c for c in a if c.isalpha()), digits_of(a)],)),
    ("position", "position", lambda a: (a, Contract(), 1, 2.0)),
    ("updatePortfolio", "updatePortfolio", lambda a: (Contract(), 1, 2, 3, 4, 5, 6, a)),
    ("execDetails", "execDetails", lambda a: (1, Contract(), _ns(acctNumber=a, execId="x"))),
    ("openOrder", "openOrder",
     lambda a: (1, Contract(), _ns(account=a, clearingAccount=a, faGroup=a, modelCode=a), _Obj())),
    ("familyCodes", "familyCodes", lambda a: ([_ns(accountID=a, familyCodeStr="x")],)),
    ("accountUpdateMulti", "accountUpdateMulti", lambda a: (1, a, a, "k", "v", "USD")),
    ("advanced reject json", "error", lambda a: (-1, 1, 1, "x", json.dumps({"account": a}))),
]


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(("label", "name", "build"), CARRIERS, ids=[c[0] for c in CARRIERS])
def test_every_id_shape_in_every_carrier_leaves_no_trace_in_the_bytes(
    tmp_path: Path, label: str, name: str, build: Any, warm: bool
) -> None:
    for n, account in enumerate(IDS):
        rec = SessionRecorder(tmp_path / f"r{n}.jsonl", clock=Clock())
        if warm:
            rec.on_raw("managedAccounts", (f"{account},{OTHER}",))
        args = build(account)
        rec.on_raw(name, args)
        rec.on_request(name, args)
        rec.close()
        text = written(tmp_path / f"r{n}.jsonl")
        assert not survivors(text, account, digits_of(account)), (label, n)
        for line in text.splitlines():
            json.loads(line)


@pytest.mark.parametrize(
    "sep",
    [" ", ".", "/", ":", "\u00a0", "\t", "\n", ",", "=", "|", "#", "'", "\u3000", "\u2009", "\U0001f600"],
)
def test_once_managed_accounts_is_seen_a_separator_cannot_hide_the_digits(tmp_path: Path, sep: str) -> None:
    for n, account in enumerate(IDS):
        rec = SessionRecorder(tmp_path / f"s{n}.jsonl", clock=Clock())
        rec.on_raw("managedAccounts", (account,))
        letters = "".join(c for c in account if c.isalpha())
        rec.on_raw("error", (-1, 1, 1, f"acct {letters}{sep}{digits_of(account)} refused", ""))
        rec.close()
        assert not survivors(written(tmp_path / f"s{n}.jsonl"), digits_of(account)), (n, sep)


def test_look_alike_letters_cannot_hide_the_digits_once_managed_accounts_is_seen(tmp_path: Path) -> None:
    """Cyrillic, Greek and Armenian look-alikes are not folded (documented); the learned digits go anyway."""
    account = make_id("U", 5)
    for n, lookalike in enumerate(("\u0423", "\u0555", "\u03a5", "\u0399", "\u0405\u0423")):
        rec = SessionRecorder(tmp_path / f"c{n}.jsonl", clock=Clock())
        rec.on_raw("managedAccounts", (account,))
        rec.on_raw("error", (-1, 1, 1, f"{lookalike}{digits_of(account)}", ""))
        rec.close()
        assert not survivors(written(tmp_path / f"c{n}.jsonl"), digits_of(account))


def test_managed_accounts_teaches_the_recorder_ids_that_have_no_account_shape(tmp_path: Path) -> None:
    """An all-digit id and a short one are caught only because managedAccounts named them."""
    all_digits = digits_of(make_id("U", 11, 9))
    short = "ZQX" + digits_of(make_id("U", 12))[:3]
    rec = recorder(tmp_path)
    # not known yet: kept (four separators are more than the pattern looks across)
    rec.on_raw("error", (-1, 1, 1, f"cold:    {all_digits} and {short}", ""))
    rec.on_raw("managedAccounts", (f"{all_digits},{short}",))
    rec.on_raw("error", (-1, 2, 1, f"warm {all_digits} and {short.lower()}", ""))
    rec.on_raw("custom", ({short: [all_digits]}, int(all_digits)))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert all_digits in json.dumps(lines[1])  # the cold gap is real: nothing could know
    later = json.dumps(lines[3:]) + json.dumps(lines[2])
    assert not survivors(later, all_digits, short)


def test_numbers_are_blanked_only_when_equal_to_the_learned_digits_strings_when_they_contain_them(
    tmp_path: Path,
) -> None:
    """Item 6: an int, float or Decimal that merely contains the digit run is a coincidence and stays;
    one that equals it (also as a float, a Decimal, with a sign) goes; a string that contains it goes."""
    account = make_id("DU", 21)
    run = digits_of(account)
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    around = (int("2" + run + "1"), float("2" + run + "1"), Decimal("1" + run + "5"))
    equal = (int(run), float(run), Decimal(run), Decimal(run + ".00"), -int(run))
    rec.on_raw("custom", (*around, *equal, "3" + run, 123456, "1704205800"))
    rec.close()
    args = read_lines(tmp_path / "rec.jsonl")[2]["args"]
    assert args[0] == int("2" + run + "1") and args[1] == float("2" + run + "1")
    assert args[2] == "1" + run + "5"  # a Decimal is written as text, untouched
    assert args[3:8] == ["ACCOUNT-1"] * 5
    assert args[8] == "3ACCOUNT-1"  # a string keeps the substring rule
    assert args[9] == 123456 and args[10] == "1704205800"
    assert rec.failures == 0  # the last check does not mistake a coincidence for a leak


def test_a_number_under_an_account_like_name_is_replaced_whole(tmp_path: Path) -> None:
    account = make_id("DU", 23)
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    rec.on_raw("custom", ({"acctCode": int("2" + digits_of(account))},))
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert not survivors(text, digits_of(account))
    assert "ACCOUNT-" in read_lines(tmp_path / "rec.jsonl")[2]["args"][0]["acctCode"]


def test_replay_survives_a_volume_that_contains_the_digits_of_an_account(tmp_path: Path) -> None:
    account = make_id("DU", 22)
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    volume = int("1" + digits_of(account))
    rec.on_request("reqHistoricalData", (7, contract(), "", "1 D", "1 min", "TRADES", 1, 2, False, []))
    rec.on_raw("historicalData", (7, make_bar(T0, 1.0, 2.0, 0.5, 1.5, volume)))
    rec.close()
    recording = load_recording(tmp_path / "rec.jsonl")
    assert recording.problems == []
    event = next(e for e in recording.raw_events() if e.name == "historicalData")
    assert decode_callback(event)[1].volume == Decimal(volume)  # the coincidence is kept


# -- other private data -----------------------------------------------------------------------


def test_private_values_are_removed_case_insensitively_everywhere(tmp_path: Path) -> None:
    rec = recorder(tmp_path, command="fetch --user Zed.Quux --as ZEDQUUX", port=7496,
                   private_values=["zedquux", "Zed.Quux", "BOX-ZQ9"])
    rec.on_raw("error", (-1, 1, 502, "Couldn't connect as ZEDQUUX on box-zq9 (zed.quux)", ""))
    rec.on_raw("custom", ({"owner zedquux": ["Box-ZQ9"]}, b"zedquux"))
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert not survivors(text, "zedquux", "zed.quux", "box-zq9")


def test_farm_names_and_error_codes_survive_redaction(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    for code, msg in (
        (2104, "Market data farm connection is OK:usfarm"),
        (2106, "HMDS data farm connection is OK:ushmds"),
        (2158, "Sec-def data farm connection is OK:secdefil"),
        (162, "Historical Market Data Service error message:Historical data request pacing violation"),
        (2107, "HMDS data farm connection is inactive but should be available upon demand.ushmds"),
    ):
        rec.on_raw("error", (-1, 1700000000000, code, msg, ""))
    rec.close()
    for line, (code, msg) in zip(read_lines(tmp_path / "rec.jsonl")[1:], (
        (2104, "usfarm"), (2106, "ushmds"), (2158, "secdefil"), (162, "pacing violation"), (2107, "ushmds"),
    ), strict=True):
        assert line["args"][2] == code and msg in line["args"][3]


def test_no_wall_clock_is_written_except_what_ib_sends_and_the_callers_start_utc(tmp_path: Path) -> None:
    rec = recorder(tmp_path, command="x", port=7497)
    rec.on_request("reqCurrentTime", ())
    rec.on_raw("currentTime", (1700000123,))
    rec.on_raw("error", (-1, 1700000123456, 2104, "ok", ""))  # IB's own errorTime: epoch milliseconds
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[0]["start_utc"] is None and lines[0]["error_time"] == "zeroed"
    assert lines[0]["current_time"] == "zeroed"
    assert [ln["t"] for ln in lines[1:]] == [1.0, 2.0, 3.0]  # monotonic and relative, from the injected clock
    # IB's errorTime and server time would say when the session ran: both are zeroed.
    assert lines[3]["args"] == [-1, 0, 2104, "ok", ""]
    assert lines[2]["args"] == [0]
    assert "1700000123" not in written(tmp_path / "rec.jsonl")


def test_a_windows_home_path_with_a_space_is_redacted_whole(tmp_path: Path) -> None:
    path = "\\".join(("C:", "Users", "Zed Quux", "OneDrive - Acme Widgets", "bars"))
    rec = recorder(tmp_path, command=f"fetch --out {path}")
    rec.on_raw("error", (-1, 1, 1, f'File "{path}\\x.py", line 5', ""))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), "quux", "acme", "widgets", "zed")


def test_an_email_address_without_a_dot_in_the_domain_is_redacted(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 1, "mail zed.quux@mailbox now", ""))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), "zed.quux", "mailbox")


def test_an_ipv4_mapped_ipv6_address_is_redacted_whole(tmp_path: Path) -> None:
    ip = ".".join(("198", "51", "100", "77"))
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 1, f"connect ::ffff:{ip} failed", ""))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), "51.100.77", ip)


def test_a_posix_style_windows_path_is_redacted(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 1, "cwd " + "/".join(("", "c", "Users", "zedquux", "bars")), ""))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), "zedquux")


@pytest.mark.parametrize("sep", [" ", ".", ":", "/", " "])
def test_cold_separated_id_is_redacted(tmp_path: Path, sep: str) -> None:
    account = make_id("DU", 31)
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 1, f"DU{sep}{digits_of(account)}", ""))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), digits_of(account))


# -- exotic values -------------------------------------------------------------------------------


def test_numpy_and_pandas_values_never_carry_an_id(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    account = make_id("DU", 41)
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    rec.on_raw("custom", (
        np.str_(account), np.array([account]), np.float64(1.5), np.int64(5),
        pd.DataFrame({account: [account], "x": [1]}), pd.Series([account], name=account),
    ))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), account, digits_of(account))
    assert rec.failures == 0


def test_hostile_objects_never_reach_the_file_through_repr_str_or_class_name(tmp_path: Path) -> None:
    account = make_id("DU", 42)

    class Repr:
        def __repr__(self) -> str:
            return account

        __str__ = __repr__

    class StrSub(str):
        __slots__ = ()

        def __str__(self) -> str:
            return "harmless"

    class IntSub(int):
        def __str__(self) -> str:
            return "harmless"

    Named = type(account, (), {})
    NamedError = type(account, (Exception,), {})
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    rec.on_raw("custom", (Repr(), StrSub(account), IntSub(int(digits_of(account))), Named(), NamedError("m")))
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), account, digits_of(account))


def test_very_deep_cyclic_and_huge_values_are_bounded(tmp_path: Path) -> None:
    account = make_id("DU", 43)
    ring: dict[str, Any] = {"k": account}
    ring["self"] = ring
    deep: Any = account
    for _ in range(50):
        deep = {"d": deep}
    rec = recorder(tmp_path)
    big = [[1]] * 6000 + [account]
    rec.on_raw("custom", (ring, deep, big, "word " * 100_000 + account, "\ud800" + account))
    rec.on_raw("custom", ("after",))
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert not survivors(text, account, digits_of(account))
    assert read_lines(tmp_path / "rec.jsonl")[-1]["args"] == ["after"]


# -- failure behaviour ----------------------------------------------------------------------------


class _FullDisk:
    """A file that accepts three writes, then fails the way a full disk does."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.writes = 0

    def write(self, s: str) -> int:
        self.writes += 1
        if self.writes > 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real.write(s)

    def flush(self) -> None:
        self.real.flush()

    def close(self) -> None:
        self.real.close()


def test_a_full_disk_is_counted_never_raised_and_stops_further_writes(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec._file = _FullDisk(rec._file)
    for i in range(10):
        rec.on_raw("currentTime", (i,))
    rec.close()
    rec.close()  # idempotent
    assert rec.write_failures == 1 and rec.dropped == 6 and rec.lines_written == 4
    lines = read_lines(tmp_path / "rec.jsonl")  # the header and three events, each whole
    assert [ln.get("seq") for ln in lines[1:]] == [1, 2, 3]


def test_four_threads_ten_thousand_events_every_line_parses(tmp_path: Path) -> None:
    account = make_id("DU", 51)
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (account,))
    barrier = threading.Barrier(4)

    def work(k: int) -> None:
        barrier.wait(WAIT)
        for i in range(2500):
            rec.on_raw("error", (-1, i, 2104, f"thread {k} sees {account}", ""))

    threads = [threading.Thread(target=work, args=(k,), daemon=True) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 12)
        assert not t.is_alive()
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    lines = text.splitlines()
    assert len(lines) == 10_002
    assert [json.loads(ln)["seq"] for ln in lines[1:]] == list(range(1, 10_002))
    assert not survivors(text, account, digits_of(account))
    assert rec.failures == 0


def test_a_redaction_failure_through_a_connection_is_a_marker_not_an_observer_failure(tmp_path: Path) -> None:
    class Boom:
        @property
        def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
            raise RuntimeError("never shown")

    rec = recorder(tmp_path)
    with session(observers=(rec,)) as (conn, fake):
        fake.push("tickString", 1, 45, Boom())
        fake.push("tickString", 2, 45, "next")
        for _ in range(400):  # a bound on polls, each a few ms at most
            conn.next_event(0.005)
            if written(tmp_path / "rec.jsonl").count("tickString") >= 2:
                break
        assert conn.observer_failures == []
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert "<redaction failed: RuntimeError>" in text and "never shown" not in text
    assert rec.failures >= 1


# -- replay -----------------------------------------------------------------------------------------


def _script_with_pacing_error(fake: FakeIbClient) -> None:
    def details(req_id: int, c: Contract) -> list[tuple[Any, ...]]:
        from tests.test_ib_recorder import _details

        return [("contractDetails", req_id, _details()), ("contractDetailsEnd", req_id)]

    def history(req_id: int, c: Contract, *_: Any) -> list[tuple[Any, ...]]:
        if c.symbol == "SLOW":
            text = "Historical Market Data Service error message:Historical data request pacing violation"
            return [("error", req_id, 1700000004000, 162, text, "")]
        bars = [
            make_bar(T0 + 60 * i, 187.0 + i, 187.5 + i, 186.5 + i, 187.2 + i, 100 + i)
            for i in range(3)
        ]
        return [("historicalData", req_id, b) for b in bars] + [
            ("historicalDataEnd", req_id, "20240102-14:30:00", "20240102-21:00:00")
        ]

    fake.respond("reqContractDetails", details)
    fake.respond("reqHeadTimeStamp", lambda req_id, *_: [("headTimestamp", req_id, "345479400")])
    fake.respond("reqHistoricalData", history)


def _ask(conn: Any, skip: int) -> list[Any]:
    head = settle(conn)
    for _ in range(skip):
        conn.next_request_id()
    c = contract()
    when = ("20240102-21:00:00", "1 D", "1 min", "TRADES", 1, 2, False, [])
    conn.request("reqContractDetails", conn.next_request_id(), c)
    conn.request("reqHeadTimeStamp", conn.next_request_id(), c, "TRADES", 1, 2)
    conn.request("reqHistoricalData", conn.next_request_id(), c, *when)
    conn.request("reqHistoricalData", conn.next_request_id(), contract("SLOW"), *when)
    return collect(conn, lambda evs: (
        any(isinstance(e, events.HistoricalDataEnd) for e in evs)
        and any(isinstance(e, events.IbError) and e.code == 162 for e in evs)
        and any(isinstance(e, events.ContractDetailsEnd) for e in evs)
        and any(isinstance(e, events.HeadTimestamp) for e in evs)
    ), head)


def test_pacing_error_session_replays_to_the_same_events_with_new_request_ids(tmp_path: Path) -> None:
    account = make_id("DU", 61)
    rec = recorder(tmp_path, command="history", port=7497)
    fake_kw = {"managed_accounts": account}
    with session(observers=(rec,), fake_kw=fake_kw, setup=_script_with_pacing_error) as (c, _):
        first = _ask(c, 0)
    rec.close()
    assert not survivors(written(tmp_path / "rec.jsonl"), account, digits_of(account))
    recording = load_recording(tmp_path / "rec.jsonl")
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["replay"] = install(fake, recording)

    with session(fake_kw=handshake_kwargs(recording), setup=setup) as (c, _):
        second = _ask(c, 77)
    holder["replay"].assert_all_used()

    def comparable(evs: list[Any]) -> list[Any]:
        return normalise_ids([e for e in evs if not isinstance(e, events.ManagedAccounts)])

    assert comparable(second) == comparable(first)
    errors = [e for e in second if isinstance(e, events.IbError) and e.code == 162]
    assert len(errors) == 1 and errors[0].req_id > 77


def test_replay_skips_a_marker_line_or_a_torn_last_line_and_reports_it() -> None:
    """Item 5 (was pinned as a refusal): the file stays readable, the request is reported."""
    text = (RECORDINGS / "synthetic_history.jsonl").read_text(encoding="utf-8")
    lines = text.splitlines()
    marked = lines[:6] + [json.dumps({"seq": 7, "t": 0.5, "kind": "raw", "name": "custom",
                                      "args": "<redaction failed: RuntimeError>"})] + lines[6:]
    rec = parse_recording("\n".join(marked))
    assert len(rec.problems) == 1 and "redaction" in rec.problems[0].reason
    torn = parse_recording(text[:-20])
    assert [p.reason for p in torn.problems] == ["torn last line"]
    assert len(parse_recording("\n".join(lines[:12])).requests) == 2  # a file cut between lines is fine


def test_replay_refuses_another_version_and_a_header_only_file_is_empty() -> None:
    lines = (RECORDINGS / "synthetic_history.jsonl").read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["version"] = 2
    with pytest.raises(ValueError):
        parse_recording("\n".join([json.dumps(header), *lines[1:]]))
    empty = parse_recording(lines[0])
    assert empty.events == [] and handshake_kwargs(empty) == {}


# -- committed files ---------------------------------------------------------------------------------

# one or two letters, at most one separator of any kind, then five or more digits
_STRICT = re.compile(r"[A-Za-z]{1,2}[ ._:/\\-]?\d{5,}")
_OCC_TEXT = re.compile(r"[A-Za-z]{1,6} *\d{6}[CcPp]\d{8}")
_KNOWN_LONG_NUMBERS = {
    "1704205800", "1704205860", "1704205920", "20240102", "265598", "345479400",
}


def test_extra_files_hold_no_account_shaped_text() -> None:
    for p in committed_recorder_files():
        text = p.read_text(encoding="utf-8")
        masked = _OCC_TEXT.sub(lambda m: " " * len(m.group(0)), text)
        found = [m.group(0) for m in _STRICT.finditer(masked)]
        assert not found, f"{p}: account-shaped text {found}"
    assert _STRICT.search("DU" + "1234567") and _STRICT.search("U " + "1234567")  # the scan does flag them


def test_synthetic_recording_holds_only_known_long_numbers_and_no_private_data() -> None:
    text = (RECORDINGS / "synthetic_history.jsonl").read_text(encoding="utf-8")
    assert set(re.findall(r"\d{6,}", text)) == _KNOWN_LONG_NUMBERS
    for needle in ("@", "\\\\", "/Users", "/home", "C:", "tws", "localhost"):
        assert needle not in text, needle
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)  # no IPv4 address
    header = json.loads(text.splitlines()[0])
    assert header["start_utc"] is None and "host" not in header


# -- fix round 2 (review of PR 149, head df9c299) -------------------------------------------------------
#
# Every account-shaped or date-shaped input below is built at run time from parts.

_THURSDAY = datetime.date(year=2024, month=1, day=11)
_SATURDAY = _THURSDAY + datetime.timedelta(days=2)
_HOLIDAY = _THURSDAY + datetime.timedelta(days=4)  # the Monday after the weekend
_AFTER_HOLIDAY = _HOLIDAY + datetime.timedelta(days=1)


def ymd(day: datetime.date) -> str:
    return f"{day:%Y%m%d}"


def week_hours(open_from: str, open_to: str) -> str:
    """A week of IB trading or liquid hours: closed days as `<date>:CLOSED`, a weekend, a holiday."""
    parts = []
    for k in range(7):
        day = _THURSDAY + datetime.timedelta(days=k)
        if day.weekday() >= 5 or day == _HOLIDAY:
            parts.append(f"{ymd(day)}:CLOSED")
        else:
            parts.append(f"{ymd(day)}:{open_from}-{ymd(day)}:{open_to}")
    return ";".join(parts)


def realistic_details() -> ContractDetails:
    """`ContractDetails` as TWS sends it for a US stock: ibapi defaults plus typical values."""
    d = ContractDetails()
    c = d.contract
    c.conId, c.symbol, c.secType, c.exchange = 265598, "AAPL", "STK", "SMART"
    c.primaryExchange, c.currency, c.localSymbol, c.tradingClass = "NASDAQ", "USD", "AAPL", "NMS"
    d.marketName, d.minTick, d.priceMagnifier, d.longName = "NMS", 0.01, 1, "APPLE INC"
    d.orderTypes = "ACTIVETIM,AD,ADJUST,ALERT,ALGO,ALLOC,AON,AVGCOST,BASKET,COND,DAY,GTC,LMT,MKT,STP,TRAIL"
    d.validExchanges = "SMART,AMEX,NYSE,CBOE,PHLX,ISE,CHX,ARCA,ISLAND,DRCTEDGE,BEX,BATS,IEX,EDGX,PSX"
    d.industry, d.category, d.subcategory = "Technology", "Computers", "Computers"
    d.timeZoneId = "US/Eastern"
    d.tradingHours = week_hours("0400", "2000")
    d.liquidHours = week_hours("0930", "1600")
    d.aggGroup = 1
    d.marketRuleIds = ",".join(["26"] * 15)
    d.secIdList = [TagValue("ISIN", "US" + "0378331005")]
    d.stockType = "COMMON"
    d.minSize = d.sizeIncrement = Decimal("0.0001")
    d.suggestedSizeIncrement = Decimal("100")
    return d


def _bars() -> list[Any]:
    rng = random.Random(4242)
    out = []
    for i in range(390):  # a regular session of one-minute bars
        p = 185 + rng.random() * 2
        out.append(make_bar(T0 + 60 * i, round(p, 2), round(p + 0.3, 2), round(p - 0.3, 2),
                            round(p + 0.1, 2), rng.randrange(100, 900000)))
    return out


def _history_script() -> Any:
    details, bars = realistic_details(), _bars()
    no_data = (
        "Historical Market Data Service error message:HMDS query returned no data: AAPL@SMART Trades"
    )

    def setup(fake: FakeIbClient) -> None:
        fake.respond("reqContractDetails",
                     lambda rid, c: [("contractDetails", rid, details), ("contractDetailsEnd", rid)])
        fake.respond("reqHeadTimeStamp", lambda rid, *a: [("headTimestamp", rid, "345479400")])

        def history(rid: int, c: Contract, end: str, *a: Any) -> list[tuple[Any, ...]]:
            if end.startswith(ymd(_SATURDAY)):
                return [("error", rid, 1700000003000, 162, no_data, "")]
            return [("historicalData", rid, b) for b in bars] + [
                ("historicalDataEnd", rid, f"{ymd(_AFTER_HOLIDAY)}-14:30:00",
                 f"{ymd(_AFTER_HOLIDAY)}-21:00:00"),
            ]

        fake.respond("reqHistoricalData", history)
        fake.respond("reqCurrentTime", lambda: [("currentTime", T0 + 12345)])

    return setup


def _history_requests(conn: Any, skip: int, with_error: bool) -> list[Any]:
    head = settle(conn)
    for _ in range(skip):
        conn.next_request_id()
    c = contract()
    conn.request("reqContractDetails", conn.next_request_id(), c)
    conn.request("reqHeadTimeStamp", conn.next_request_id(), c, "TRADES", 1, 2)
    # a fetch made after the contract details, for the first day after the closed days
    conn.request("reqHistoricalData", conn.next_request_id(), c, f"{ymd(_AFTER_HOLIDAY)}-21:00:00",
                 "1 D", "1 min", "TRADES", 1, 2, False, [])
    if with_error:
        conn.request("reqHistoricalData", conn.next_request_id(), c, f"{ymd(_SATURDAY)}-21:00:00",
                     "1 D", "1 min", "TRADES", 1, 2, False, [])
    return collect(conn, lambda evs: (
        any(isinstance(e, events.HistoricalDataEnd) for e in evs)
        and any(isinstance(e, events.ContractDetailsEnd) for e in evs)
        and any(isinstance(e, events.HeadTimestamp) for e in evs)
        and (not with_error or any(isinstance(e, events.IbError) and e.code == 162 for e in evs))
    ), head)


def _record_and_replay(
    tmp_path: Path, *, private_values: tuple[str, ...] = (), with_error: bool = True,
    name: str = "rec.jsonl",
) -> tuple[list[Any], list[Any], Any, SessionRecorder]:
    """Record a scripted history session through a real `IbConnection`, replay the file in a
    second one. Returns the first events, the replayed events, the parsed recording, the recorder."""
    rec = recorder(tmp_path, name=name, command="fetch", port=7497, private_values=private_values)
    with session(observers=(rec,), fake_kw={"managed_accounts": A},
                 setup=_history_script()) as (conn, _):
        first = _history_requests(conn, 0, with_error)
    rec.close()
    parsed = load_recording(tmp_path / name)
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["replay"] = install(fake, parsed)

    with session(fake_kw=handshake_kwargs(parsed), setup=setup) as (conn, _):
        second = _history_requests(conn, 40, with_error)
    holder["replay"].assert_all_used()

    def comparable(evs: list[Any]) -> list[Any]:
        return normalise_ids([e for e in evs if not isinstance(e, events.ManagedAccounts)])

    return comparable(first), comparable(second), parsed, rec


def test_a_realistic_week_of_hours_and_a_later_fetch_replay_to_equal_events(tmp_path: Path) -> None:
    """Review item 1: `<date>:CLOSED;<date>` was taken for an id, the date learned, and then removed
    from the end times of every later fetch; the replayed events silently differed."""
    first, second, parsed, rec = _record_and_replay(tmp_path)
    assert second == first
    assert parsed.problems == [] and rec.failures == 0
    details = next(e for e in second if isinstance(e, events.ContractDetailsEvent))
    assert details.trading_hours == week_hours("0400", "2000")
    assert details.liquid_hours == week_hours("0930", "1600")
    assert f"{ymd(_HOLIDAY)}:CLOSED;{ymd(_AFTER_HOLIDAY)}:" in details.trading_hours
    end = next(e for e in second if isinstance(e, events.HistoricalDataEnd))
    assert end.start == f"{ymd(_AFTER_HOLIDAY)}-14:30:00" and end.end == f"{ymd(_AFTER_HOLIDAY)}-21:00:00"
    file_text = written(tmp_path / "rec.jsonl")
    assert f"{ymd(_AFTER_HOLIDAY)}-21:00:00" in file_text  # the request's end time is still there
    recorded = next(ln for ln in read_lines(tmp_path / "rec.jsonl")[1:] if ln["name"] == "contractDetails")
    assert recorded["args"][1]["tradingHours"] == week_hours("0400", "2000")
    assert recorded["args"][1]["liquidHours"] == week_hours("0930", "1600")
    assert not survivors(file_text, A, digits_of(A))
    assert scan_recording(tmp_path / "rec.jsonl", words=[A]) == []
    lines = read_lines(tmp_path / "rec.jsonl")[1:]
    sent = next(ln for ln in lines if ln["name"] == "error" and ln["args"][2] == 162)
    assert "AAPL@SMART" in sent["args"][3]  # item 6: an exchange token is not an e-mail address


def _glued(day: str, word: str = "CLOSED") -> str:
    return f"{word};{day}:0400-{day}:2000"


@pytest.mark.parametrize("day", [
    ymd(datetime.date(year=1990, month=1, day=1)),
    ymd(datetime.date(year=2100, month=12, day=31)),
    ymd(datetime.date(year=2024, month=2, day=29)),
])
def test_a_date_after_a_word_is_left_alone_and_never_learned(day: str) -> None:
    red = Redactor()
    text = _glued(day)
    assert red.sanitise("raw", "error", (-1, 0, 1, text, ""))[3] == text
    assert red.known_accounts == 0
    assert not red.leaks(text)
    # not learned: the bare date elsewhere is untouched too
    assert red.sanitise("raw", "historicalDataEnd", (5, f"{day}-14:30:00", f"{day}-21:00:00")) == [
        5, f"{day}-14:30:00", f"{day}-21:00:00",
    ]


@pytest.mark.parametrize("digits", ["19891231", "21010101", "20240230", "20231301", "20240100", "12345678"])
def test_eight_digits_that_are_not_a_valid_date_after_a_word_are_still_an_id(digits: str) -> None:
    red = Redactor()
    out = red.sanitise("raw", "error", (-1, 0, 1, f"CLOSED;{digits}", ""))[3]
    assert digits not in out and "ACCOUNT-1" in out


def test_a_date_after_a_word_is_not_exempt_when_more_than_a_time_follows() -> None:
    day = ymd(_THURSDAY)
    for tail in (" 12345", ".5678", "-12345"):
        out = Redactor().sanitise("raw", "error", (-1, 0, 1, f"CLOSED;{day}{tail}", ""))[3]
        assert day not in out, tail


def test_an_id_whose_digits_are_a_date_but_is_not_glued_to_a_letter_is_redacted_cold() -> None:
    day = ymd(_THURSDAY)
    for before in (" ", ":", "=", "("):
        red = Redactor()
        out = red.sanitise("raw", "error", (-1, 0, 1, f"account{before}U{day} rejected", ""))[3]
        assert day not in out and "ACCOUNT-1" in out, before
        # its digits are not learned alone: a date elsewhere stays, the id as written is caught
        assert red.sanitise("raw", "error", (-1, 0, 1, _glued(day), ""))[3] == _glued(day)
        assert "ACCOUNT-1" in red.sanitise("raw", "error", (-1, 0, 1, f"again U{day}", ""))[3]


def test_the_glued_form_of_such_an_id_is_redacted_warm_and_kept_cold_by_design() -> None:
    """The one narrow loosening, pinned: cold, an 8-digit id that is a valid date AND glued to a
    word of four or more letters is not caught. Warm it is, because `managedAccounts` taught the
    id. (Issue 156 tightened it: a prefix of up to three letters after a non-letter, `xxU<date>`
    standing alone, is now redacted cold as well.)"""
    day = ymd(_THURSDAY)
    standalone = "xx" + f"U{day}"
    assert day not in Redactor().sanitise("raw", "error", (-1, 0, 1, f"see {standalone} now", ""))[3]
    glued = "ab" + standalone
    cold = Redactor().sanitise("raw", "error", (-1, 0, 1, f"see {glued} now", ""))[3]
    assert cold == f"see {glued} now"  # documented loosening
    warm = Redactor()
    warm.sanitise("raw", "managedAccounts", (f"U{day}",))
    out = warm.sanitise("raw", "error", (-1, 0, 1, f"see {glued} now", ""))[3]
    assert day not in out and "ACCOUNT-1" in out
    # and its digits alone, also with a separator inside, are caught warm
    for text in (f"bare {day}", f"bare {day[:4]}:{day[4:]}", f"bare {day[:4]}  {day[4:]}"):
        assert day not in warm.sanitise("raw", "error", (-1, 0, 1, text, ""))[3]


def test_iso_time_of_start_utc_survives_and_other_t_prefixed_digits_do_not() -> None:
    red = Redactor()
    assert red.scrub("2026-01-02T03:04:05Z") == "2026-01-02T03:04:05Z"
    assert red.scrub("2026-01-02T03:04:05.123456+01:00") == "2026-01-02T03:04:05.123456+01:00"
    assert "ACCOUNT" in red.scrub("T" + "03:04:05")  # not after a digit: not a time stamp of ours
    assert "ACCOUNT" in red.scrub("1T" + "03:04:5678")  # not hh:mm:ss


# -- currentTime ------------------------------------------------------------------------------


def test_current_time_is_zeroed_noted_in_the_header_and_replayed_as_a_fixed_synthetic_time(
    tmp_path: Path,
) -> None:
    """Review item 2: `currentTime` is TWS's wall clock, exactly what `errorTime` zeroing hides."""
    real = T0 + 12345
    rec = recorder(tmp_path, command="probe", port=7497)
    with session(observers=(rec,), setup=_history_script()) as (conn, _):
        conn.request("reqCurrentTime")
        first = collect(conn, lambda evs: any(isinstance(e, events.CurrentTime) for e in evs))
    rec.close()
    assert next(e for e in first if isinstance(e, events.CurrentTime)).time == real
    text = written(tmp_path / "rec.jsonl")
    assert str(real) not in text
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[0]["current_time"] == "zeroed" and lines[0]["error_time"] == "zeroed"
    cur = next(ln for ln in lines[1:] if ln["name"] == "currentTime")
    assert cur["args"] == [0]
    parsed = load_recording(tmp_path / "rec.jsonl")
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["replay"] = install(fake, parsed)

    with session(fake_kw=handshake_kwargs(parsed), setup=setup) as (conn, _):
        conn.request("reqCurrentTime")
        again = collect(conn, lambda evs: any(isinstance(e, events.CurrentTime) for e in evs))
    assert next(e for e in again if isinstance(e, events.CurrentTime)).time == SYNTHETIC_CURRENT_TIME
    assert SYNTHETIC_CURRENT_TIME != 0 and SYNTHETIC_CURRENT_TIME < 1_000_000_000_000
    assert decode_callback(
        parse_recording(
            "\n".join([
                json.dumps(lines[0]),
                json.dumps({"seq": 1, "t": 0.0, "kind": "raw", "name": "currentTime",
                            "args": [real]}),  # an old file that kept the real time
            ])
        ).events[0]
    ) == (SYNTHETIC_CURRENT_TIME,)


def test_every_callback_the_data_only_client_forwards_is_discussed_in_the_recorder_docstring() -> None:
    names = sorted(
        n for n, v in vars(DataOnlyClient).items()
        if not n.startswith("_") and inspect.isfunction(v) and hasattr(rmod.EWrapper, n)
    )
    assert len(names) == 16
    missing = [n for n in names if n not in (rmod.__doc__ or "")]
    assert missing == [], f"a forwarded callback is not covered by the wall-clock search: {missing}"


# -- names that come from code ----------------------------------------------------------------------


def _private_words() -> tuple[str, ...]:
    # words that occur inside ibapi method and attribute names (built from parts)
    return ("Zed " + "Data", "Trading" + " Co", "Ti" + "m")


def test_private_words_do_not_rename_events_requests_or_ibapi_attributes(tmp_path: Path) -> None:
    """Review item 3: a private word inside a callback or attribute name renamed it; replay then
    failed or set another attribute, and nothing was reported."""
    plain_first, plain_second, plain, _ = _record_and_replay(
        tmp_path, with_error=False, name="plain.jsonl")
    first, second, parsed, rec = _record_and_replay(
        tmp_path, private_values=_private_words(), with_error=False, name="private.jsonl")
    def no_texts(evs: list[Any]) -> list[Any]:  # farm messages carry the word "data": redacted, rightly
        return [e for e in evs if not isinstance(e, events.IbError)]

    assert no_texts(second) == no_texts(first) == no_texts(plain_first) == no_texts(plain_second)
    assert parsed.problems == [] and rec.failures == 0
    private_lines = read_lines(tmp_path / "private.jsonl")[1:]
    names = [ln["name"] for ln in private_lines]
    # the reader thread's farm messages interleave with the requests: compare as a multiset
    assert collections.Counter(names) == collections.Counter(
        ln["name"] for ln in read_lines(tmp_path / "plain.jsonl")[1:]
    )
    assert "historicalData" in names and "reqHeadTimeStamp" in names
    cd = next(ln for ln in private_lines if ln["name"] == "contractDetails")
    assert {"tradingHours", "liquidHours", "timeZoneId"} <= set(cd["args"][1])
    assert {"tradingClass"} <= set(cd["args"][1]["contract"])
    assert next(e for e in second if isinstance(e, events.ContractDetailsEvent)).time_zone_id == "US/Eastern"
    assert scan_recording(tmp_path / "private.jsonl", words=[_private_words()[0]]) == []


def test_private_words_still_redact_data_keys_values_and_unknown_names() -> None:
    red = Redactor(private_values=("Zed " + "Data",))
    assert red.event_name("raw", "historicalData") == "historicalData"
    assert red.event_name("request", "reqHistoricalData") == "reqHistoricalData"
    assert red.event_name("raw", "customData") == "custom" + "REDACTED"  # not an EWrapper method
    assert red.event_name("request", "historicalData") != "historicalData"  # a callback is not a request
    out = red.sanitise("raw", "custom", ({"my" + "Data": "x"}, "Data here", _NS(Data=1)))
    assert out[0] == {"myREDACTED": "x"}
    assert out[1] == "REDACTED here"
    assert out[2] == {"__class__": "_NS", "REDACTED": 1}  # an object of ours, not of ibapi: redacted


class _NS:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


# -- wider separators ------------------------------------------------------------------------------


@pytest.mark.parametrize("sep", [":", "; ", " = ", ", \"", "\\\"", "  \\\"", "|\t "])
def test_up_to_three_separators_between_letters_and_digits_are_caught_cold(sep: str) -> None:
    digits = digits_of(make_id("U", 301))
    out = Redactor().sanitise("raw", "error", (-1, 0, 1, f"id{sep}{digits} x", ""))[3]
    assert digits not in out and "ACCOUNT-1" in out


def test_four_separators_between_letters_and_digits_remain_a_cold_gap() -> None:
    digits = digits_of(make_id("U", 302))
    assert digits in Redactor().sanitise("raw", "error", (-1, 0, 1, f"id{' ' * 4}{digits}", ""))[3]


@pytest.mark.parametrize("depth", [1, 2])
def test_a_json_escaped_quote_between_letters_and_digits_is_caught_cold(depth: int) -> None:
    digits = digits_of(make_id("U", 303))
    text = json.dumps({"note": f'id"{digits}'})  # the quote is now backslash, quote
    for _ in range(depth - 1):
        text = json.dumps(text)  # and again: JSON inside a JSON string
    out = Redactor().sanitise("raw", "error", (-1, 0, 1, text, ""))[3]
    assert digits not in out


@pytest.mark.parametrize("sep", [":", "/", "\t", "  ", ". ", "--", "\\\""])
def test_two_separators_of_any_kind_inside_the_digits_are_caught_cold_and_warm(sep: str) -> None:
    account = make_id("DU", 304)
    run = digits_of(account)
    split = f"{run[:3]}{sep}{run[3:]}"
    cold = Redactor().sanitise("raw", "error", (-1, 0, 1, f"id {account[:2]}{split} x", ""))[3]
    assert run not in cold and run[3:] not in cold
    warm = Redactor()
    warm.sanitise("raw", "managedAccounts", (account,))
    for text in (f"bare {split} x", f"id {account[:2]}{split}", f"{split}"):
        out = warm.sanitise("raw", "error", (-1, 0, 1, text, ""))[3]
        assert run[3:] not in out and run[:3] not in out.replace("ACCOUNT-1", ""), text


def test_three_separators_inside_the_digits_remain_a_gap() -> None:
    account = make_id("DU", 305)
    run = digits_of(account)
    warm = Redactor()
    warm.sanitise("raw", "managedAccounts", (account,))
    text = f"bare {run[:3]} - {run[3:]}"
    assert run[3:] in warm.sanitise("raw", "error", (-1, 0, 1, text, ""))[3]


@pytest.mark.parametrize("value_kind", ["str", "int"])
def test_an_id_split_over_a_dict_key_and_its_value_is_caught_cold(tmp_path: Path, value_kind: str) -> None:
    account = make_id("U", 306)
    digits = digits_of(account)
    value: Any = digits if value_kind == "str" else int(digits)
    rec = recorder(tmp_path)
    rec.on_raw("custom", ({"U": value, "other": 1},))
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert digits not in text and rec.failures == 0
    args = read_lines(tmp_path / "rec.jsonl")[1]["args"]
    assert args[0]["other"] == 1  # the rest of the dict is kept


def test_an_id_split_inside_a_dict_key_and_over_its_value_loses_the_key_too(tmp_path: Path) -> None:
    digits = digits_of(make_id("U", 307))
    key = "U" + digits[:2]
    rec = recorder(tmp_path)
    rec.on_raw("custom", ({key: digits[2:], "other": 1},))
    rec.close()
    text = written(tmp_path / "rec.jsonl")
    assert key not in text and digits[2:] not in text and rec.failures == 0
    assert read_lines(tmp_path / "rec.jsonl")[1]["args"][0]["other"] == 1


def test_plain_dict_keys_and_values_that_do_not_join_into_an_id_are_kept() -> None:
    out = Redactor().sanitise("raw", "custom", ({"conId": "x", "size": 7, "id": "abc"},))
    assert out == [{"conId": "x", "size": 7, "id": "abc"}]


# -- exchange tokens and e-mail addresses -------------------------------------------------------------


@pytest.mark.parametrize("token", ["AAPL@SMART", "BRK@NYSE", "ES@GLOBEX", "A1B2@ISLAND"])
def test_a_symbol_at_exchange_token_is_not_an_email_address(token: str) -> None:
    text = f"HMDS query returned no data: {token} Trades"
    assert Redactor().sanitise("raw", "error", (1, 0, 162, text, ""))[3] == text


@pytest.mark.parametrize("address", [
    "pat@" + "example", "Pat@" + "Host", "pat@" + "HOST", "PAT@" + "host", "AAPL@" + "SMART.US",
    "AVERYLONG@" + "SMART", "AAPL@" + "SMART@x", "a.b@" + "example.org", "AAPL1_x@" + "SMART",
])
def test_user_at_host_is_still_an_email_address(address: str) -> None:
    out = Redactor().sanitise("raw", "error", (1, 0, 1, f"mail {address} now", ""))[3]
    assert out == "mail EMAIL-REDACTED now"


# -- scan_recording --------------------------------------------------------------------------------


def _digits7(seed: int) -> str:
    return digits_of(make_id("U", seed))


@pytest.mark.parametrize("sep", [":", "/", "\t", "  ", ". "])
def test_scan_flags_separators_inside_a_digit_run(tmp_path: Path, sep: str) -> None:
    d = _digits7(401)
    path = write_recording(tmp_path, ev(1, "custom", f"{d[:3]}{sep}{d[3:]}"))
    assert reasons(scan_recording(path)) == [rmod.SEPARATED_RUN_REASON]
    assert d[3:] not in "".join(f.shown for f in scan_recording(path))


def test_scan_does_not_flag_short_or_known_separated_digits(tmp_path: Path) -> None:
    hours = week_hours("0400", "2000")
    path = write_recording(
        tmp_path,
        ev(1, "custom", "12:34 5"),  # five digits only
        ev(2, "contractDetails", 1, {"__class__": "ContractDetails", "tradingHours": hours,
                                      "marketRuleIds": ",".join(["26"] * 15),
                                      "liquidHours": week_hours("0930", "1600")}),
        ev(3, "historicalDataEnd", 1, f"{ymd(_THURSDAY)}-14:30:00", f"{ymd(_THURSDAY)}-21:00:00"),
        ev(4, "historicalData", 1, {"__class__": "BarData", "date": f"{ymd(_THURSDAY)} 09:30:00",
                                    "volume": "123456", "wap": "186.1234"}),
    )
    assert scan_recording(path) == []


def test_scan_checks_that_a_date_position_holds_a_valid_date_or_epoch(tmp_path: Path) -> None:
    bad = ("20241345", "20240230", "19891231", "99999999")
    path = write_recording(
        tmp_path,
        *[ev(i + 1, "historicalDataEnd", 1, f"{b}-14:30:00", f"{ymd(_THURSDAY)}-21:00:00")
          for i, b in enumerate(bad)],
        ev(9, "headTimestamp", 1, "9" * 10),  # ten digits but past the year 2100
        ev(10, "headTimestamp", 1, "345479400"),  # a real head timestamp
        ev(11, "headTimestamp", 1, "1704205800"),
    )
    found = scan_recording(path)
    assert [f.line for f in found] == [2, 3, 4, 5, 6]
    assert set(reasons(found)[:4]) == {rmod.INVALID_DATE_REASON} and reasons(found)[4] == LONG_RUN


def test_scan_flags_digit_only_dict_keys(tmp_path: Path) -> None:
    path = write_recording(tmp_path, ev(1, "custom", {_digits7(402): "x"}), ev(2, "custom", {"1234": "x"}))
    found = scan_recording(path)
    assert [(f.line, f.reason) for f in found] == [(2, rmod.DIGIT_KEY_REASON)]  # line 1 is the header


def test_scan_flags_long_digits_under_numeric_names_when_the_value_is_not_a_number(tmp_path: Path) -> None:
    d = _digits7(403)
    path = write_recording(
        tmp_path,
        ev(1, "custom", {"__class__": "Contract", "conId": 265598}),  # a number: fine
        ev(2, "custom", {"__class__": "Contract", "conId": d}),  # text under a numeric name
        ev(3, "tickPrice", 1, 1, [d], {}),  # a list under `price`
        ev(4, "custom", {"__class__": "BarData", "volume": "1234567", "wap": "186.1234", "barCount": 42}),
        ev(5, "custom", {"__class__": "BarData", "volume": "12345678x"}),
        ev(6, "custom", {"volume": [d]}),  # a list under a Decimal-text name is not a Decimal
    )
    found = scan_recording(path)
    assert [(f.line, f.reason) for f in found] == [
        (3, LONG_RUN), (4, LONG_RUN), (6, LONG_RUN), (7, LONG_RUN),
    ]


def test_scan_does_not_flag_an_exchange_token_or_a_date_after_a_word(tmp_path: Path) -> None:
    path = write_recording(
        tmp_path,
        ev(1, "error", -1, 0, 162, "no data: AAPL@SMART Trades", ""),
        ev(2, "error", -1, 0, 1, "write to pat@" + "example now", ""),
        ev(3, "custom", {"tradingHours": _glued(ymd(_THURSDAY))}),
        ev(4, "custom", {"note": _glued(ymd(_THURSDAY))}),
    )
    by_line: dict[int, list[str]] = collections.defaultdict(list)
    for f in scan_recording(path):
        by_line[f.line].append(f.reason)
    assert 2 not in by_line and 4 not in by_line  # the exchange token; the date in a date-named field
    assert by_line[3] == ["e-mail address"]
    assert LETTERS in by_line[5]  # the same text under another name is flagged


def test_scan_does_not_hold_a_caller_word_against_a_method_name(tmp_path: Path) -> None:
    path = write_recording(tmp_path, ev(1, "historicalDataEnd", 1, "x", "y"), ev(2, "custom", "my Data"))
    assert [f.line for f in scan_recording(path, words=["Data"])] == [3]  # only the value


def test_docstring_carries_the_owner_checklist_and_the_limits() -> None:
    doc = rmod.__doc__ or ""
    assert "Before committing a real recording" in doc
    for needle in ("one or two letters", "relative paths", "base64", "by hand", "display names"):
        assert needle in doc.replace("\n", " ").replace("  ", " "), needle
    scan_doc = " ".join((rmod.scan_recording.__doc__ or "").split())
    assert "cannot see" in scan_doc and "base64" in scan_doc and "by hand" in scan_doc
