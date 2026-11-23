"""The session recorder (MainStoinker/ib/recorder.py) and its replay helper (tests/ib_replay.py).

Nothing here opens a socket or touches TWS. Sessions run through `IbConnection` with
the scripted `FakeIbClient`. No real account number appears in this file: every
account id is built at run time (`acct`), and `test_no_account_pattern_in_committed_files`
checks that this file, the recorder, the replay helper and the committed recordings
hold nothing account-shaped.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import random
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.contract import Contract

from MainStoinker.ib import events
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import (
    ConnectionLost,
    ConnectionRestored,
    IbConnection,
)
from MainStoinker.ib.recorder import (
    FORMAT_NAME,
    FORMAT_VERSION,
    REDACTION_VERSION,
    Redactor,
    SessionRecorder,
)
from tests.fake_ib import FakeIbClient, make_bar
from tests.ib_replay import (
    ReplayExhausted,
    handshake_kwargs,
    install,
    load_recording,
    parse_recording,
    push_unsolicited,
)

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "tests" / "recordings"
WAIT = 5.0


def acct(prefix: str = "DU", number: int = 7654321) -> str:
    """An account-shaped id, built at run time so it is never in the source."""
    return f"{prefix}{number:07d}"


A = acct("DU", 7654321)
B = acct("U", 1357913)


class Clock:
    """A monotonic clock that moves by one second each time it is read."""

    def __init__(self) -> None:
        self.now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += 1.0
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def recorder(tmp_path: Path, name: str = "rec.jsonl", **kw: Any) -> SessionRecorder:
    return SessionRecorder(tmp_path / name, clock=Clock(), **kw)


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def forbidden_in(text: str, *secrets: str) -> list[str]:
    """The secrets still readable in `text`, also after folding full-width letters and
    dropping zero-width characters (a file that kept them would still leak)."""
    folded = "".join(
        c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf"
    )
    low = (text + "\n" + folded).lower()
    return [s for s in secrets if s.lower() in low]


def digits_of(account: str) -> str:
    return "".join(c for c in account if c.isdigit())


# -- the scripted session rig ------------------------------------------------------------------


@contextmanager
def session(
    *,
    observers: tuple[Any, ...] = (),
    fake_kw: dict[str, Any] | None = None,
    setup: Callable[[FakeIbClient], None] | None = None,
) -> Iterator[tuple[IbConnection, FakeIbClient]]:
    fakes: list[FakeIbClient] = []

    def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger), **(fake_kw or {}))
        if setup is not None:
            setup(fake)
        fakes.append(fake)
        return fake

    clock = Clock()
    conn = IbConnection(
        "tws.invalid", 7497, 21, env={}, client_factory=factory, clock=clock,
        sleep=clock.sleep, poll_interval=0.002, observers=observers,
    )
    try:
        conn.open()
        yield conn, fakes[-1]
    finally:
        conn.close()


def collect(conn: IbConnection, done: Callable[[list[Any]], bool]) -> list[Any]:
    got: list[Any] = []
    for _ in range(2000):  # a bound on polls, each at most a few ms; never a sleep
        ev = conn.next_event(0.01)
        if ev is not None:
            got.append(ev)
        if done(got):
            return got
    raise AssertionError(f"events never completed: {got!r}")


def normalise_ids(evs: list[Any]) -> list[Any]:
    """Replace each request id by the order in which ids first appear."""
    order: dict[int, int] = {}
    out = []
    for ev in evs:
        rid = getattr(ev, "req_id", None)
        if rid is not None and rid >= 0:
            ev = dataclasses.replace(ev, req_id=order.setdefault(rid, len(order) + 1))
        out.append(ev)
    return out


def contract(symbol: str = "AAPL") -> Contract:
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = symbol, "STK", "SMART", "USD"
    return c


def run_requests(conn: IbConnection, first_id_skip: int = 0) -> list[Any]:
    """The same four requests, with request ids that depend on `first_id_skip`."""
    for _ in range(first_id_skip):
        conn.next_request_id()
    c = contract()
    conn.request("reqContractDetails", conn.next_request_id(), c)
    conn.request("reqHeadTimeStamp", conn.next_request_id(), c, "TRADES", 1, 2)
    conn.request(
        "reqHistoricalData", conn.next_request_id(), c, "20240102-21:00:00", "1 D", "1 min",
        "TRADES", 1, 2, False, [],
    )
    conn.request(
        "reqHistoricalData", conn.next_request_id(), contract("NOSUCH"), "20240102-21:00:00",
        "1 D", "1 min", "TRADES", 1, 2, False, [],
    )

    def done(evs: list[Any]) -> bool:
        return (
            any(isinstance(e, events.HistoricalDataEnd) for e in evs)
            and any(isinstance(e, events.HeadTimestamp) for e in evs)
            and any(isinstance(e, events.ContractDetailsEnd) for e in evs)
            and any(isinstance(e, events.IbError) and e.code == 200 for e in evs)
        )

    return collect(conn, done)


def script_ib(fake: FakeIbClient) -> None:
    """What TWS says to those four requests (a script for session one of the round trip)."""
    details = _details()

    def contract_details(req_id: int, c: Contract) -> list[tuple[Any, ...]]:
        return [("contractDetails", req_id, details), ("contractDetailsEnd", req_id)]

    def head(req_id: int, *_: Any) -> list[tuple[Any, ...]]:
        return [("headTimestamp", req_id, "345479400")]

    def history(req_id: int, c: Contract, *_: Any) -> list[tuple[Any, ...]]:
        if c.symbol == "NOSUCH":
            return [("error", req_id, 1700000003000, 200, "No security definition has been found", "")]
        bars = [make_bar(1704205800 + 60 * i, 187.0 + i, 187.5 + i, 186.5 + i, 187.2 + i, 100 + i)
                for i in range(3)]
        return [("historicalData", req_id, b) for b in bars] + [
            ("historicalDataEnd", req_id, "20240102-14:30:00", "20240102-21:00:00")
        ]

    fake.respond("reqContractDetails", contract_details)
    fake.respond("reqHeadTimeStamp", head)
    fake.respond("reqHistoricalData", history)


def _details() -> Any:
    from ibapi.contract import ContractDetails

    d = ContractDetails()
    d.contract = contract()
    d.contract.conId = 265598
    d.contract.localSymbol = "AAPL"
    d.longName = "APPLE INC"
    d.minTick = 0.01
    d.timeZoneId = "US/Eastern"
    d.tradingHours = "20240102:0400-20240102:2000"
    d.liquidHours = "20240102:0930-20240102:1600"
    return d


# -- the acceptance criteria -------------------------------------------------------------------


def test_managed_accounts_redacted(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (f"{A},{B}",))
    rec.on_raw("error", (-1, 1, 2104, f"Farm OK for {A.lower()} and {B}", ""))
    rec.close()
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert not forbidden_in(text, A, B, digits_of(A), digits_of(B))
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"] == ["ACCOUNT-1,ACCOUNT-2"]
    assert lines[2]["args"][3] == "Farm OK for ACCOUNT-1 and ACCOUNT-2"


def test_account_in_error_text_redacted(tmp_path: Path) -> None:
    """Before managedAccounts is seen the pattern catches it; after, the learned id does."""
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 321, f"Error validating request: account {A} is not valid", ""))
    rec.on_raw("managedAccounts", (A,))
    rec.on_raw("error", (-1, 2, 321, f"order for {A}.1 refused; also {digits_of(A)}", ""))
    rec.close()
    path = tmp_path / "rec.jsonl"
    assert not forbidden_in(path.read_text(encoding="utf-8"), A, digits_of(A))
    lines = read_lines(path)
    assert lines[1]["args"][3] == "Error validating request: account ACCOUNT-1 is not valid"
    assert lines[3]["args"][3] == "order for ACCOUNT-1.1 refused; also ACCOUNT-1"


class _Exec:
    pass


def test_account_fields_redacted_by_name(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    e = _Exec()
    e.acctNumber = "any text at all"  # type: ignore[attr-defined]
    e.accountName = "Pat Example"  # type: ignore[attr-defined]
    e.shares = 100  # type: ignore[attr-defined]
    rec.on_raw("execDetails", (1, contract(), e))
    rec.on_raw("updateAccountValue", ("NetLiquidation", "12.5", "USD", "Pat Example IRA"))
    rec.on_raw("accountSummary", (3, "Pat Example IRA", "TotalCashValue", "5", "USD"))
    rec.on_raw("custom", ({"accountsList": ["x1", "y2"], "acctCode": 5, "n": 1},))
    rec.on_request("reqAccountUpdates", (True, "Pat Example IRA"))
    rec.close()
    path = tmp_path / "rec.jsonl"
    text = path.read_text(encoding="utf-8")
    assert not forbidden_in(text, "any text at all", "Pat Example", "x1", "y2")
    lines = read_lines(path)
    ex = lines[1]["args"][2]
    assert ex["acctNumber"] == "ACCOUNT-1" and ex["accountName"].startswith("ACCOUNT-")
    assert ex["shares"] == 100
    assert lines[2]["args"][3].startswith("ACCOUNT-")
    assert lines[3]["args"][1].startswith("ACCOUNT-")
    assert lines[4]["args"][0]["accountsList"][0].startswith("ACCOUNT-")
    assert lines[4]["args"][0]["acctCode"].startswith("ACCOUNT-")
    assert lines[4]["args"][0]["n"] == 1
    assert lines[5]["args"][1].startswith("ACCOUNT-")
    assert lines[5]["args"][0] is True


def test_option_local_symbol_not_mangled(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    c = contract("AAPL")
    c.localSymbol = "AAPL  240816C00220000"
    c.description = "AAPL240816C00220000 and 240816P00150000"
    rec.on_raw("managedAccounts", (A,))
    rec.on_request("reqContractDetails", (1, c))
    rec.close()
    got = read_lines(tmp_path / "rec.jsonl")[2]["args"][1]
    assert got["localSymbol"] == "AAPL  240816C00220000"
    assert got["description"] == "AAPL240816C00220000 and 240816P00150000"


def test_host_not_recorded(tmp_path: Path) -> None:
    rec = recorder(tmp_path, command="fetch-bars AAPL", port=7497)
    with session(observers=(rec,), setup=script_ib) as (conn, _fake):
        run_requests(conn)
    rec.close()
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert "tws.invalid" not in text
    header = json.loads(text.splitlines()[0])
    assert header["port"] == 7497
    assert "host" not in header and "tws.invalid" not in json.dumps(header)


def test_host_and_paths_in_text_are_scrubbed(tmp_path: Path) -> None:
    ip = ".".join(("192", "0", "2", "55"))
    rec = recorder(
        tmp_path, command=r"fetch --out C:\Users\someone\bars --host tws-box.invalid",
        private_values=["someone", "tws-box"],
    )
    rec.on_raw("error", (-1, 1, 502, f"Couldn't connect to {ip}:7497 as someone on TWS-BOX", ""))
    rec.on_raw("error", (-1, 2, 1, r"wrote C:\Users\x\f.txt and /home/someone/y and ::1 and fe80::1", ""))
    rec.on_raw("error", (-1, 3, 1, "mail me@example.org", ""))
    rec.close()
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    for secret in (ip, "someone", "tws-box", "Users", "/home", "fe80", "example.org", "me@"):
        assert secret.lower() not in text.lower(), secret


def test_each_line_flushed(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    path = tmp_path / "rec.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # the header, already on disk
    rec.on_request("reqCurrentTime", ())
    text = path.read_text(encoding="utf-8")  # still open
    assert text.endswith("\n") and len(text.splitlines()) == 2
    rec.on_raw("currentTime", (1700000000,))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    rec.close()
    rec.on_raw("currentTime", (1,))  # after close: dropped, never raised
    assert rec.dropped == 1 and len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_recording_round_trips_through_replay(tmp_path: Path) -> None:
    """A scripted session is recorded, replayed with fresh request ids, and gives the same events."""
    rec = recorder(tmp_path, command="round trip", port=7497)
    with session(observers=(rec,), fake_kw={"managed_accounts": A}, setup=script_ib) as (conn, _f):
        first = run_requests(conn)
    rec.close()
    path = tmp_path / "rec.jsonl"
    assert not forbidden_in(path.read_text(encoding="utf-8"), A, digits_of(A))

    recording = load_recording(path)
    assert [r.method for r in recording.requests] == [
        "reqContractDetails", "reqHeadTimeStamp", "reqHistoricalData", "reqHistoricalData",
    ]
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["replay"] = install(fake, recording)

    with session(fake_kw=handshake_kwargs(recording), setup=setup) as (conn, _f):
        second = run_requests(conn, first_id_skip=40)
    holder["replay"].assert_all_used()

    def comparable(evs: list[Any]) -> list[Any]:
        return normalise_ids([e for e in evs if not isinstance(e, events.ManagedAccounts)])

    assert comparable(second) == comparable(first)
    assert [e.accounts for e in second if isinstance(e, events.ManagedAccounts)] == ["ACCOUNT-1"]
    first_ids = {e.req_id for e in first if hasattr(e, "req_id") and e.req_id > 0}
    second_ids = {e.req_id for e in second if hasattr(e, "req_id") and e.req_id > 0}
    assert first_ids.isdisjoint(second_ids)  # the ids really were rewritten


def test_no_account_pattern_in_committed_files() -> None:
    files = [p for p in RECORDINGS.rglob("*") if p.is_file()]
    assert any(p.name == "synthetic_history.jsonl" for p in files)
    files += [
        ROOT / "MainStoinker" / "ib" / "recorder.py",
        ROOT / "tests" / "ib_replay.py",
        Path(__file__),
    ]
    red = Redactor()
    for p in files:
        text = p.read_text(encoding="utf-8")
        found = [text[a:b] for a, b in red.leaks_in_text(text)]
        assert not found, f"{p}: account-shaped text {found}"
    assert red.leaks_in_text(f"x {A} y")  # the scan does flag one


# -- replay -------------------------------------------------------------------------------------


def test_synthetic_recording_replays_by_request_id() -> None:
    rec = load_recording(RECORDINGS / "synthetic_history.jsonl")
    assert rec.header["format"] == FORMAT_NAME and rec.header["version"] == FORMAT_VERSION
    assert [h.name for h in rec.handshake] == [
        "connectAck", "nextValidId", "managedAccounts", "error", "error", "error",
    ]
    assert [u.args[2] for u in rec.unsolicited] == [1100, 1102]
    kw = handshake_kwargs(rec)
    assert kw["managed_accounts"] == "ACCOUNT-1" and kw["next_valid_id"] == 1
    assert [c for c, _ in kw["farm_messages"]] == [2104, 2106, 2158]
    holder: dict[str, Any] = {}

    def setup(fake: FakeIbClient) -> None:
        holder["replay"] = install(fake, rec)
        holder["fake"] = fake

    with session(fake_kw=kw, setup=setup) as (conn, fake):
        for _ in range(9):
            conn.next_request_id()
        c = contract()
        r_det, r_head, r_hist, r_bad, r_tick = (conn.next_request_id() for _ in range(5))
        conn.request("reqContractDetails", r_det, c)
        conn.request("reqHeadTimeStamp", r_head, c, "TRADES", 1, 2)
        conn.request("reqHistoricalData", r_hist, c, "20240102-21:00:00", "1 D", "1 min",
                     "TRADES", 1, 2, False, [])
        conn.request("reqHistoricalData", r_bad, contract("NOSUCH"), "20240102-21:00:00", "1 D",
                     "1 min", "TRADES", 1, 2, False, [])
        conn.request("reqMktData", r_tick, c, "", False, False, [])
        assert push_unsolicited(fake, rec) == 2
        evs = collect(conn, lambda e: any(isinstance(x, ConnectionRestored) for x in e))
    holder["replay"].assert_all_used()
    by = {type(e).__name__: e for e in evs}
    assert by["ContractDetailsEvent"].req_id == r_det and by["ContractDetailsEvent"].con_id == 265598
    assert by["ContractDetailsEnd"].req_id == r_det
    assert by["HeadTimestamp"].req_id == r_head and by["HeadTimestamp"].timestamp == "345479400"
    bars = [e for e in evs if isinstance(e, events.HistoricalBar)]
    assert [b.req_id for b in bars] == [r_hist] * 3
    assert [b.volume for b in bars] == [Decimal(12345), Decimal(9876), Decimal(7654)]
    assert by["HistoricalDataEnd"].req_id == r_hist
    errors = [e for e in evs if isinstance(e, events.IbError) and e.code == 200]
    assert [e.req_id for e in errors] == [r_bad]
    assert by["MarketDataType"].req_id == r_tick
    assert by["TickPrice"].price == 187.2 and by["TickSize"].size == Decimal(300)
    assert any(isinstance(e, ConnectionLost) for e in evs)


def test_replay_raises_when_the_recording_has_no_answer_left() -> None:
    rec = load_recording(RECORDINGS / "synthetic_history.jsonl")
    with session(fake_kw=handshake_kwargs(rec), setup=lambda f: install(f, rec)) as (conn, _f):
        conn.request("reqContractDetails", conn.next_request_id(), contract())
        with pytest.raises(ReplayExhausted):
            conn.request("reqContractDetails", conn.next_request_id(), contract())


def test_replay_prefers_the_recorded_request_with_equal_arguments() -> None:
    rec = load_recording(RECORDINGS / "synthetic_history.jsonl")
    with session(fake_kw=handshake_kwargs(rec), setup=lambda f: install(f, rec)) as (conn, _f):
        bad = conn.next_request_id()
        conn.request("reqHistoricalData", bad, contract("NOSUCH"), "20240102-21:00:00", "1 D",
                     "1 min", "TRADES", 1, 2, False, [])  # asked first, but recorded second
        good = conn.next_request_id()
        conn.request("reqHistoricalData", good, contract(), "20240102-21:00:00", "1 D",
                     "1 min", "TRADES", 1, 2, False, [])
        evs = collect(conn, lambda e: any(isinstance(x, events.HistoricalDataEnd) for x in e))
    assert [e.req_id for e in evs if isinstance(e, events.IbError) and e.code == 200] == [bad]
    assert {e.req_id for e in evs if isinstance(e, events.HistoricalBar)} == {good}


def test_parse_recording_refuses_other_formats() -> None:
    with pytest.raises(ValueError):
        parse_recording('{"format":"other","version":1}\n')
    with pytest.raises(ValueError):
        parse_recording("")


# -- redaction corpus ---------------------------------------------------------------------------


def _contract_with(**attrs: str) -> Contract:
    c = contract()
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


def _exec(**attrs: Any) -> _Exec:
    e = _Exec()
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


def fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)


def corpus() -> list[tuple[str, str, tuple[Any, ...], bool]]:
    """(label, callback, args, needs_warm). `needs_warm` cases need the id learned first."""
    dig = digits_of(A)
    e = lambda text: ("error", (-1, 1700000000000, 2104, text, ""))  # noqa: E731
    cases: list[tuple[str, str, tuple[Any, ...], bool]] = []

    def add(label: str, name: str, args: tuple[Any, ...], warm: bool = False) -> None:
        cases.append((label, name, args, warm))

    add("managed single", "managedAccounts", (A,))
    add("managed pair", "managedAccounts", (f"{A},{B}",))
    add("managed padded", "managedAccounts", (f" {A} , {B} ;",))
    add("managed lower", "managedAccounts", (A.lower(),))
    add("managed bytes", "managedAccounts", (A.encode(),))
    add("managed list", "managedAccounts", ([A, B],))
    for label, text in (
        ("middle", f"Account {A} is not valid"),
        ("start", f"{A} rejected"),
        ("end", f"rejected for {A}"),
        ("parens", f"({A})"),
        ("brackets", f"[{A}]"),
        ("quotes", f"'{A}' and \"{B}\""),
        ("trailing dot", f"{A}."),
        ("sub account", f"{A}.1 and {B}.USD"),
        ("slash", f"{A}/{B}"),
        ("colon", f"{A}:{B}"),
        ("json in string", f'{{"account":"{A}"}}'),
        ("json list", f'{{"orders":[{{"acct":"{A}","qty":5}}]}}'),
        ("farm 2104", f"Market data farm connection is OK:usfarm acct={A}"),
        ("farm text", f"Account: {A}; Farm: usfarm"),
        ("underscore prefix", f"acct_{A}"),
        ("glued letters", f"xx{A}yy"),
        ("glued digits", f"20240102{B}"),
        ("adjacent", f"{A}{B}"),
        ("dash joined", f"{A}-{B}"),
        ("dash inside", f"{A[:2]}-{A[2:]}"),
        ("underscore inside", f"{A[:2]}_{A[2:]}"),
        ("mixed case", A[0].lower() + A[1:]),
        ("trailing text", f"{A}USD"),
        ("newlines", f"line1\n{A}\nline2"),
        ("full width", fullwidth(A)),
        ("zero width", "\u200b".join(A)),
    ):
        add(f"error text: {label}", *e(text))
    add("error json arg", "error", (-1, 1, 200, "x", f'{{"acct":"{A}"}}'))
    add("list", "custom", ([A, B],))
    add("nested", "custom", ({"a": {"b": [{"c": A}]}},))
    add("dict key", "custom", ({A: 1, B: {A: 2}},))
    add("tuple", "custom", ((A,),))
    add("set", "custom", ({A, B},))
    add("bytearray", "custom", (bytearray(A.encode()),))
    add("utf-16 bytes", "custom", (A.encode("utf-16-le"),))
    add("contract local symbol", "contractDetails", (1, _contract_with(localSymbol=A)))
    add("contract description", "contractDetails", (1, _contract_with(description=f"of {A}")))
    add("object acctNumber", "execDetails", (1, contract(), _exec(acctNumber=A, shares=1)))
    add("object nested", "custom", ([_exec(owner=_exec(note=f"is {B}"))],))
    add("exception", "custom", (ValueError(f"bad {A}"),))
    add("split strings", "custom", (A[:3], A[3:]))
    add("split list", "custom", ([A[:4], A[4:]],))
    add("split str and int", "custom", (A[:2], int(dig)))
    add("split short prefix", "custom", ("U", int(digits_of(B))))
    add("split dict values", "custom", ({"p": A[:2], "q": int(dig)},))
    add("only the digits", "error", (-1, 1, 1, f"account number {dig}", ""), True)
    add("digits as int", "custom", ("x", int(dig)), True)
    add("digits as float", "custom", (float(dig) + 0.5,), True)
    add("digits as Decimal", "custom", (Decimal(dig),), True)
    add("digits inside number", "custom", (int("9" + dig + "9"),), True)
    return cases


def test_corpus_has_at_least_forty_cases() -> None:
    assert len(corpus()) >= 40


def _corpus_params() -> list[Any]:
    """Every case warm (the id learned from managedAccounts first) and, unless the case
    is a bare digit run that only a known id can explain, cold."""
    return [
        pytest.param(c, warm, id=f"{c[0]} [{'warm' if warm else 'cold'}]")
        for c in corpus() for warm in (False, True) if warm or not c[3]
    ]


@pytest.mark.parametrize(("case", "warm"), _corpus_params())
def test_corpus_no_account_survives(
    tmp_path: Path, case: tuple[str, str, tuple[Any, ...], bool], warm: bool
) -> None:
    label, name, args, _needs_warm = case
    rec = recorder(tmp_path)
    if warm:
        rec.on_raw("managedAccounts", (f"{A},{B}",))
    rec.on_raw(name, args)
    rec.on_request(name, args)
    rec.close()
    text = (tmp_path / "rec.jsonl").read_bytes().decode("utf-8")
    left = forbidden_in(text, A, B, digits_of(A), digits_of(B))
    assert not left, f"{label}: {left} survived in {text!r}"
    assert rec.failures == 0
    for line in text.splitlines():
        json.loads(line)  # every line is whole JSON


def test_corpus_cases_are_not_vacuous(tmp_path: Path) -> None:
    """The cases do put something in the file (they are not all swallowed as failures)."""
    rec = recorder(tmp_path)
    rec.on_raw("error", (-1, 1, 2104, "Market data farm connection is OK:usfarm", ""))
    rec.on_raw("custom", ({"keep": "this", "n": 5},))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"][3] == "Market data farm connection is OK:usfarm"
    assert lines[2]["args"] == [{"keep": "this", "n": 5}]


def test_split_account_blanks_the_event_but_not_its_ids(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("custom", (77, "U", int(digits_of(B)), "keep"))
    rec.close()
    args = read_lines(tmp_path / "rec.jsonl")[1]["args"]
    assert args == [77, "<redacted>", "<redacted>", "<redacted>"]


# -- property style -----------------------------------------------------------------------------

_WORDS = ("hello", "farm", "ok", "error", "Account", "id", "usfarm", "x", "", " ", ",", ".", ":",
          "-", "_", "/", "(", ")", "[", "]", '"', "'", "\n", "=", ";", "AAPL", "TRADES")
_PREFIXES = ("U", "DU", "F", "DF", "I", "DI")


def _random_id(rng: random.Random) -> str:
    return rng.choice(_PREFIXES) + "".join(rng.choice("0123456789") for _ in range(rng.randrange(6, 10)))


def _random_text(rng: random.Random, ids: list[str]) -> str:
    parts = []
    for _ in range(rng.randrange(1, 7)):
        if rng.random() < 0.4:
            i = rng.choice(ids)
            parts.append(rng.choice((i, i.lower(), i.upper(), i.title())))
        else:
            parts.append(rng.choice(_WORDS))
    return "".join(parts)


def _random_value(rng: random.Random, ids: list[str], depth: int = 0) -> Any:
    kind = rng.randrange(9)
    if depth >= 3 or kind < 3:
        return _random_text(rng, ids)
    if kind == 3:
        return [_random_value(rng, ids, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 4:
        return tuple(_random_value(rng, ids, depth + 1) for _ in range(rng.randrange(4)))
    if kind == 5:
        return {_random_text(rng, ids): _random_value(rng, ids, depth + 1)
                for _ in range(rng.randrange(4))}
    if kind == 6:
        return _contract_with(localSymbol=_random_text(rng, ids), description=_random_text(rng, ids))
    if kind == 7:
        return rng.choice((_random_text(rng, ids).encode(), bytearray(_random_text(rng, ids).encode())))
    return rng.randrange(10**5)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_random_structures_leave_no_account_in_the_written_bytes(tmp_path: Path, warm: bool) -> None:
    rng = random.Random(20241123 + warm)
    for n in range(150):
        ids = [_random_id(rng) for _ in range(rng.randrange(1, 4))]
        rec = SessionRecorder(tmp_path / f"r{n}.jsonl", clock=Clock())
        if warm:
            rec.on_raw("managedAccounts", (",".join(ids),))
        for _ in range(rng.randrange(1, 5)):
            args = [_random_value(rng, ids) for _ in range(rng.randrange(1, 5))]
            if rng.random() < 0.3:  # an id split over two neighbouring arguments
                i = rng.choice(ids)
                cut = rng.randrange(1, 3)
                at = rng.randrange(len(args) + 1)
                args[at:at] = [i[:cut], i[cut:]]
            rec.on_raw("custom", tuple(args))
        rec.close()
        raw = (tmp_path / f"r{n}.jsonl").read_bytes().lower()
        for i in ids:
            assert i.lower().encode() not in raw, (n, i)
            assert digits_of(i).encode() not in raw, (n, i)


# -- the recorder as an observer ----------------------------------------------------------------


def test_header_and_line_format(tmp_path: Path) -> None:
    rec = recorder(tmp_path, command="probe AAPL", port=7496, start_utc="2026-01-02T03:04:05Z")
    rec.on_request("reqCurrentTime", ())
    rec.on_raw("currentTime", (1700000000,))
    rec.close()
    header, req, raw = read_lines(tmp_path / "rec.jsonl")
    assert header == {
        "format": FORMAT_NAME, "version": 1, "redaction": REDACTION_VERSION, "ibapi": "10.37.2",
        "command": "probe AAPL", "port": 7496, "start_utc": "2026-01-02T03:04:05Z",
    }
    assert req == {"seq": 1, "t": 1.0, "kind": "request", "name": "reqCurrentTime", "args": []}
    assert raw == {"seq": 2, "t": 2.0, "kind": "raw", "name": "currentTime", "args": [1700000000]}


def test_objects_and_decimals_are_serialised(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("historicalData", (3, make_bar(1704205800, 1.5, 2.5, 1.0, 2.0, 42)))
    rec.close()
    bar = read_lines(tmp_path / "rec.jsonl")[1]["args"][1]
    assert bar["__class__"] == "BarData" and bar["volume"] == "42" and bar["open"] == 1.5


def test_unserialisable_values_name_their_type_and_never_use_repr(tmp_path: Path) -> None:
    class Sneaky:
        __slots__ = ()

        def __repr__(self) -> str:
            return f"Sneaky({A})"

    rec = recorder(tmp_path)
    rec.on_raw("custom", (Sneaky(), object(), lambda: A, iter([A]), threading.Lock()))
    rec.close()
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert not forbidden_in(text, A, digits_of(A))
    args = read_lines(tmp_path / "rec.jsonl")[1]["args"]
    assert args[0] == {"__class__": "Sneaky"} or args[0] == "<unserialisable Sneaky>"
    assert args[1] == "<unserialisable object>"
    assert args[2] == "<unserialisable function>"
    assert args[3] == "<unserialisable list_iterator>"
    assert str(args[4]).startswith("<unserialisable")


def test_cycles_and_depth_are_bounded(tmp_path: Path) -> None:
    loop: list[Any] = []
    loop.append(loop)
    deep: Any = "x"
    for _ in range(40):
        deep = [deep]
    rec = recorder(tmp_path)
    rec.on_raw("custom", (loop, deep))
    rec.close()
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert "<cycle>" in text and "<truncated>" in text and rec.failures == 0


def test_redaction_failure_writes_a_marker_and_never_raises(tmp_path: Path) -> None:
    class Boom:
        @property
        def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
            raise RuntimeError(f"explodes with {A}")

    rec = recorder(tmp_path)
    rec.on_raw("custom", (Boom(),))
    rec.on_raw("custom", ("fine",))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"] == "<redaction failed: RuntimeError>"
    assert lines[2]["args"] == ["fine"]
    assert rec.failures == 1
    assert A not in (tmp_path / "rec.jsonl").read_text(encoding="utf-8")


def test_clock_failure_and_write_failure_never_raise(tmp_path: Path) -> None:
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("clock broke")
        return 0.0

    rec = SessionRecorder(tmp_path / "a.jsonl", clock=clock)
    rec.on_raw("currentTime", (1,))
    rec.close()
    assert read_lines(tmp_path / "a.jsonl")[1]["t"] is None and rec.failures == 1

    rec2 = recorder(tmp_path, "b.jsonl")
    rec2._file.close()  # the disk went away under it
    rec2.on_raw("currentTime", (1,))
    rec2.on_raw("currentTime", (2,))
    assert rec2.write_failures == 1 and rec2.dropped == 1
    rec2.close()


def test_leak_check_replaces_arguments_it_cannot_prove_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = recorder(tmp_path)
    monkeypatch.setattr(Redactor, "sanitise", lambda self, kind, name, args: [A])
    rec.on_raw("custom", ("anything",))
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    assert lines[1]["args"] == "<redaction check failed>" and rec.failures == 1
    assert A not in (tmp_path / "rec.jsonl").read_text(encoding="utf-8")


def test_output_is_deterministic(tmp_path: Path) -> None:
    def make(name: str) -> bytes:
        rec = recorder(tmp_path, name, command="x", port=7497)
        rec.on_raw("managedAccounts", (f"{B},{A}",))
        rec.on_raw("error", (-1, 1, 2, f"{A} {B} {A}", ""))
        rec.on_request("reqHistoricalData", (4, contract(), "", "1 D", "1 min", "TRADES", 1, 2, False, []))
        rec.close()
        return (tmp_path / name).read_bytes()

    assert make("one.jsonl") == make("two.jsonl")


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "keep.jsonl"
    path.write_text("precious", encoding="utf-8")
    with pytest.raises(FileExistsError):
        SessionRecorder(path, clock=Clock())
    assert path.read_text(encoding="utf-8") == "precious"


def test_same_account_gets_the_same_placeholder_in_any_spelling(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (f"{A}",))
    rec.on_raw("custom", (A, A.lower(), f"{B}", f"{B.lower()}", f"{A}.1"))
    rec.close()
    assert read_lines(tmp_path / "rec.jsonl")[2]["args"] == [
        "ACCOUNT-1", "ACCOUNT-1", "ACCOUNT-2", "ACCOUNT-2", "ACCOUNT-1.1",
    ]


def test_threads_write_whole_lines_in_sequence(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec.on_raw("managedAccounts", (A,))
    per_thread = 150
    barrier = threading.Barrier(8)

    def work(k: int) -> None:
        barrier.wait(WAIT)
        for i in range(per_thread):
            if (i + k) % 2:
                rec.on_raw("error", (-1, i, 2104, f"thread {k} sees {A}", ""))
            else:
                rec.on_request("reqMktData", (k, contract(), "", False, False, []))

    threads = [threading.Thread(target=work, args=(k,), daemon=True) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 4)
        assert not t.is_alive()
    rec.close()
    lines = read_lines(tmp_path / "rec.jsonl")
    seqs = [d["seq"] for d in lines[1:]]
    assert seqs == list(range(1, 8 * per_thread + 2))
    assert rec.failures == 0 and rec.lines_written == len(lines)
    assert A not in (tmp_path / "rec.jsonl").read_text(encoding="utf-8")


def test_a_failing_recorder_does_not_break_the_connection(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    rec._file.close()
    with session(observers=(rec,), setup=script_ib) as (conn, _fake):
        run_requests(conn)
        assert conn.observer_failures == []  # the recorder counted its own trouble
    assert rec.write_failures >= 1


def test_recorder_never_calls_back_into_the_connection(tmp_path: Path) -> None:
    """A full session with the recorder installed raises no RuntimeError from request()."""
    rec = recorder(tmp_path)
    with session(observers=(rec,), setup=script_ib) as (conn, _fake):
        run_requests(conn)
        assert conn.observer_failures == []
    assert rec.failures == 0 and rec.lines_written > 10


def test_module_reads_no_environment_and_opens_no_socket() -> None:
    tree = ast.parse((ROOT / "MainStoinker" / "ib" / "recorder.py").read_text(encoding="utf-8"))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"os", "socket", "ssl", "getpass", "platform", "subprocess", "sys"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "environ" not in attrs and "getenv" not in attrs


def test_normalisation_defeats_look_alike_characters() -> None:
    red = Redactor()
    for variant in (fullwidth(A), "\u200b".join(A), unicodedata.normalize("NFKD", A), A.swapcase()):
        assert A.lower() not in red.scrub(f"id {variant}").lower()
        assert digits_of(A) not in red.scrub(f"id {variant}")
