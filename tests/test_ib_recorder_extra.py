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
import errno
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
from ibapi.contract import Contract

from MainStoinker.ib import events
from MainStoinker.ib.recorder import SessionRecorder
from tests.fake_ib import FakeIbClient, make_bar
from tests.ib_replay import (
    decode_callback,
    handshake_kwargs,
    install,
    load_recording,
    parse_recording,
)
from tests.test_ib_recorder import (
    RECORDINGS,
    T0,
    WAIT,
    Clock,
    collect,
    committed_recorder_files,
    contract,
    normalise_ids,
    read_lines,
    recorder,
    session,
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
    rec.on_raw("error", (-1, 1, 1, f"cold: {all_digits} and {short}", ""))  # not known yet: kept
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
    assert [ln["t"] for ln in lines[1:]] == [1.0, 2.0, 3.0]  # monotonic and relative, from the injected clock
    # Item 7: IB's errorTime would say when the session ran: it is zeroed (market times are kept).
    assert lines[3]["args"] == [-1, 0, 2104, "ok", ""]
    assert lines[2]["args"] == [1700000123]
    assert "1700000123456" not in written(tmp_path / "rec.jsonl")


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
    ))


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
