"""`MainStoinker/ib/cli.py`: the `fetch-bars` command, driven in-process.

The client behind every connection is `FakeIbClient`; nothing opens a socket. Time is injected
(`SimClock`), the terminal is a fake, the environment is a mapping passed in. The only child
processes are `--help` and a check that the offline commands never import `MainStoinker.ib`.
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.engine.feed import offline_data_path, read_symbol_bars
from MainStoinker.ib import cli
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import DATA_REQUESTS, ConnState, IbConnection
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.recorder import SessionRecorder
from tests.fake_ib import FARM_MESSAGES, ConnectBehavior, FakeIbClient
from tests.test_ib_fetch import (
    SERVER_NOW,
    Rig,
    SimClock,
    details,
    error_behaviour,
)

REPO = Path(__file__).resolve().parent.parent
CLI_SOURCE = REPO / "MainStoinker" / "ib" / "cli.py"
DAYS = ["--start", "2024-08-12", "--end", "2024-08-14"]  # Monday to Wednesday, closed at SERVER_NOW


# -- the real home folder is never touched -------------------------------------------------------


def _real_home() -> Path | None:
    """The real profile folder, read once when this module is imported (before any test runs)."""
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value)
    return None


def _listing(home: Path | None) -> dict[str, tuple[bool, int]]:
    """Name and modification time of every entry of the real `Stoinkulator/bars` and `recordings`."""
    seen: dict[str, tuple[bool, int]] = {}
    if home is None:
        return seen
    for name in ("bars", "recordings"):
        folder = home / "Stoinkulator" / name
        try:
            seen[name] = (folder.is_dir(), folder.stat().st_mtime_ns)
        except OSError:
            seen[name] = (False, 0)
        if folder.is_dir():
            for entry in folder.rglob("*"):
                try:
                    seen[str(entry)] = (entry.is_dir(), entry.stat().st_mtime_ns)
                except OSError:
                    seen[str(entry)] = (False, 0)
    return seen


REAL_HOME = _real_home()
REAL_LISTING_AT_START = _listing(REAL_HOME)


@pytest.fixture(autouse=True)
def real_home_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """No test of this command may reach the real profile: the process environment is pointed at
    `tmp_path` for the test, and afterwards the real `Stoinkulator` folders must be as at the start."""
    fake_home = tmp_path / "process-home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    yield
    after = _listing(REAL_HOME)
    assert after == REAL_LISTING_AT_START, (
        "A TEST WROTE INTO THE REAL HOME FOLDER: "
        f"changed {sorted(set(after.items()) ^ set(REAL_LISTING_AT_START.items()))}")


class Tty(io.StringIO):
    def __init__(self, interactive: bool = True) -> None:
        super().__init__()
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


class Session:
    """What `main` is given instead of the real classes, and what the test looks at afterwards."""

    def __init__(self, tmp_path: Path, behavior: ConnectBehavior = ConnectBehavior.ACCEPT) -> None:
        self.tmp = tmp_path
        self.behavior = behavior
        self.farm = True  # False: the historical data farm never reports ready
        self.clock = SimClock()
        self.rig = Rig(None, None, None, self.clock)  # type: ignore[arg-type]
        self.fakes: list[FakeIbClient] = []
        self.connections: list[IbConnection] = []
        self.recorders: list[SessionRecorder] = []
        self.prompts: list[str] = []
        self.out = io.StringIO()
        self.env = {"USERPROFILE": str(tmp_path / "home"), "HOME": str(tmp_path / "home")}
        self.sleep: Callable[[float], None] = self.clock.sleep

    # -- factories ---------------------------------------------------------------

    def client_factory(self, *, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger), connect_behavior=self.behavior,
                            farm_messages=FARM_MESSAGES if self.farm else ())
        fake.respond("reqContractDetails", lambda req_id, contract: [
            ("contractDetails", req_id, details(contract.symbol)), ("contractDetailsEnd", req_id)])
        fake.respond("reqHistoricalData", self.rig.handler)
        fake.respond("reqCurrentTime", [("currentTime", int(SERVER_NOW.timestamp()))])
        self.fakes.append(fake)
        return fake

    def connection_factory(self, args: Any, observers: list[Any]) -> IbConnection:
        conn = IbConnection(args.host, args.port, args.client_id, env={}, client_factory=self.client_factory,
                            clock=self.clock, sleep=self.clock.sleep, poll_interval=0.002, attempts=2,
                            retry_delay=3, observers=observers)
        self.connections.append(conn)
        return conn

    def market_data_factory(self, conn: IbConnection) -> IbMarketData:
        return IbMarketData(conn, clock=self.clock, poll_interval=0.01)

    def recorder_factory(self, args: Any) -> SessionRecorder | None:
        if args.record is None:
            return None
        args.record.parent.mkdir(parents=True, exist_ok=True)
        recorder = SessionRecorder(args.record, clock=self.clock, command=["fetch-bars"], port=args.port,
                                   private_values=args.private)
        self.recorders.append(recorder)
        return recorder

    # -- running ---------------------------------------------------------------------

    def input_fn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.typed

    typed = "7497"

    def main(self, *argv: str, tty: bool = True, with_factories: bool = True) -> int:
        extra: dict[str, Any] = {}
        if with_factories:
            extra = {"connection_factory": self.connection_factory,
                     "market_data_factory": self.market_data_factory,
                     "recorder_factory": self.recorder_factory}
        return cli.main(
            list(argv), env=self.env, stdin=Tty(tty), input_fn=self.input_fn, out=self.out,
            clock=self.clock, sleep=self.sleep, today=date(2024, 8, 21), **extra)

    def fetch(self, *extra: str, symbols: str = "AAPL,MSFT", **kw: Any) -> int:
        return self.main("--symbols", symbols, *DAYS, "--port", "7497", "--out", str(self.tmp / "bars"),
                         *extra, **kw)

    @property
    def text(self) -> str:
        return self.out.getvalue()

    @property
    def recording(self) -> Path:
        found = sorted((self.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
        assert len(found) == 1, found
        return found[0]


@pytest.fixture
def session(tmp_path: Path) -> Any:
    s = Session(tmp_path)
    yield s
    for conn in s.connections:
        conn.close()
    for recorder in s.recorders:
        recorder.close()
    for fake in s.fakes:
        fake.close()


def nothing_built(s: Session) -> None:
    assert not s.connections and not s.fakes and not s.recorders


# -- arguments, help, guards, gate ----------------------------------------------------------


def test_help_exits_zero_before_any_client_lists_exit_codes(session: Session,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        session.main("--help")
    assert info.value.code == 0
    shown = capsys.readouterr().out
    for code in ("0 ", "1 ", "2 ", "3 ", "4 ", "130"):
        assert f"  {code}" in shown
    assert "--port" in shown and "--yes" not in shown
    nothing_built(session)


@pytest.mark.parametrize("argv", [
    ["--symbols", "AAPL", *DAYS],  # no --port
    ["--symbols", "AAPL", "--start", "2024-8-12", "--end", "2024-08-14", "--port", "7497"],
    ["--symbols", "AAPL", "--start", "2024-08-14", "--end", "2024-08-12", "--port", "7497"],
    ["--symbols", "AAPL", *DAYS, "--port", "7497", "--client-id", "123"],
    ["--symbols", "AAPL", *DAYS, "--port", "7497", "--client-id", "0"],
    ["--symbols", "AA PL", *DAYS, "--port", "7497"],
    ["--symbols", "AAPL", *DAYS, "--port", "0"],
    ["--symbols", "AAPL", *DAYS, "--port", "7497", "--record", "x.jsonl", "--no-record"],
    ["--symbols", "AAPL", *DAYS, "--port", "7497", "--connect-timeout", "0"],
    ["--symbols", "AAPL", *DAYS, "--port", "7497", "--yes-i-typed-the-port"],
])
def test_bad_arguments_exit_2_before_any_client(session: Session, argv: list[str],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        session.main(*argv)
    assert info.value.code == 2
    assert not session.prompts
    nothing_built(session)
    capsys.readouterr()


def test_a_live_port_without_the_opt_in_exits_2_before_the_gate(session: Session) -> None:
    code = session.main("--symbols", "AAPL", *DAYS, "--port", "7496", "--out", str(session.tmp / "bars"))
    assert code == cli.EXIT_REFUSED
    assert "STOINK_ALLOW_LIVE_PORT" in session.text
    assert '$env:STOINK_ALLOW_LIVE_PORT = "1"' in session.text
    assert "never sets it" in session.text
    assert not session.prompts
    nothing_built(session)


def test_a_live_port_with_the_opt_in_reaches_the_gate_and_stops_there_on_wrong_text(session: Session) -> None:
    session.env["STOINK_ALLOW_LIVE_PORT"] = "1"
    session.typed = "no"
    code = session.main("--symbols", "AAPL", *DAYS, "--port", "7496", "--out", str(session.tmp / "bars"))
    assert code == cli.EXIT_REFUSED and len(session.prompts) == 1
    nothing_built(session)


def test_the_gate_refuses_without_a_terminal(session: Session) -> None:
    assert session.fetch(tty=False) == cli.EXIT_REFUSED
    assert "only the owner runs it" in session.text
    assert not session.prompts
    nothing_built(session)


@pytest.mark.parametrize("typed", ["", "y", "yes", "7496", "7497 ", "port"])
def test_the_gate_needs_the_port_typed(session: Session, typed: str) -> None:
    session.typed = typed
    code = session.fetch()
    if typed.strip() == "7497":
        assert code == cli.EXIT_OK  # surrounding spaces are ignored
        return
    assert code == cli.EXIT_REFUSED and len(session.prompts) == 1
    assert "(7497)" in session.prompts[0]
    nothing_built(session)


def test_the_gate_describes_the_session_with_requests_and_minimum_duration(session: Session) -> None:
    session.typed = "nope"
    session.fetch()
    for word in ("6 requests", "at least", "AAPL, MSFT", "data requests only", "2024-08-12",
                 "Recording file"):
        assert word in session.text, word


def test_eof_at_the_prompt_is_a_refusal(session: Session) -> None:
    def eof(prompt: str) -> str:
        raise EOFError

    session.input_fn = eof  # type: ignore[method-assign]
    assert session.fetch() == cli.EXIT_REFUSED
    nothing_built(session)


PLACES = ["onedrive_out", "repo_out", "onedrive_record", "repo_record", "env_onedrive"]


@pytest.mark.parametrize("where", PLACES)
def test_paths_inside_onedrive_or_the_repository_are_refused(session: Session, where: str) -> None:
    out, record = session.tmp / "bars", None
    if where == "onedrive_out":
        out = session.tmp / "OneDrive - Org" / "bars"
    elif where == "repo_out":
        out = REPO / "fetched_bars"
    elif where == "onedrive_record":
        record = session.tmp / "OneDrive" / "rec.jsonl"
    elif where == "repo_record":
        record = REPO / "rec.jsonl"
    else:
        session.env["OneDrive"] = str(session.tmp / "synced")
        out = session.tmp / "synced" / "bars"
    argv = ["--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(out)]
    if record is not None:
        argv += ["--record", str(record)]
    assert session.main(*argv) == cli.EXIT_REFUSED
    assert "Refused" in session.text
    assert not session.prompts
    nothing_built(session)
    assert not out.exists() and (record is None or not record.exists())


def test_a_default_under_a_onedrive_home_is_refused(session: Session) -> None:
    session.env["USERPROFILE"] = str(session.tmp / "OneDrive - Org" / "me")
    assert session.main("--symbols", "AAPL", *DAYS, "--port", "7497") == cli.EXIT_REFUSED
    assert "OneDrive" in session.text
    nothing_built(session)


def test_a_mapping_without_a_home_folder_is_refused_before_the_gate_and_creates_nothing(
        session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_home(*args: Any, **kw: Any) -> Path:
        raise AssertionError("the machine's own home folder was asked for")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.setattr(os.path, "expanduser", no_home)
    session.env.clear()
    before = sorted(session.tmp.rglob("*"))
    for argv in (["--symbols", "AAPL", *DAYS, "--port", "7497"],  # every default
                 ["--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(session.tmp / "bars"),
                  "--no-record"]):  # even with both paths given
        assert session.main(*argv) == cli.EXIT_REFUSED
    assert "USERPROFILE" in session.text and "HOME" in session.text
    assert not session.prompts
    nothing_built(session)
    assert sorted(session.tmp.rglob("*")) == before and not (session.tmp / "bars").exists()


def test_a_blank_home_variable_counts_as_missing(session: Session) -> None:
    session.env.update({"USERPROFILE": "  ", "HOME": ""})
    assert session.main("--symbols", "AAPL", *DAYS, "--port", "7497") == cli.EXIT_REFUSED
    assert "neither USERPROFILE nor HOME" in session.text
    nothing_built(session)


def test_home_is_used_when_userprofile_is_missing(session: Session) -> None:
    del session.env["USERPROFILE"]
    assert session.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--no-record") == cli.EXIT_OK
    assert (session.tmp / "home" / "Stoinkulator" / "bars" / "AAPL").is_dir()


def test_userprofile_wins_over_home(session: Session) -> None:
    session.env["HOME"] = str(session.tmp / "other")
    assert session.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--no-record") == cli.EXIT_OK
    assert (session.tmp / "home" / "Stoinkulator" / "bars").is_dir() and not (session.tmp / "other").exists()


def test_the_process_environment_is_pointed_into_tmp_for_every_test(tmp_path: Path) -> None:
    assert Path(os.environ["USERPROFILE"]).is_relative_to(tmp_path)
    assert Path(os.environ["HOME"]).is_relative_to(tmp_path)
    assert Path.home().is_relative_to(tmp_path)


def test_the_guard_compares_the_real_folder_with_its_state_at_the_start() -> None:
    """The listing helper sees a change (so the autouse check after each test would fail loudly)."""
    assert REAL_HOME is not None
    assert not Path(os.environ["USERPROFILE"]).samefile(REAL_HOME)
    assert _listing(REAL_HOME) == REAL_LISTING_AT_START


def test_an_existing_recording_file_is_never_overwritten(session: Session) -> None:
    record = session.tmp / "rec" / "mine.jsonl"
    record.parent.mkdir()
    record.write_text("keep me", encoding="utf-8")
    assert session.fetch("--record", str(record)) == cli.EXIT_REFUSED
    assert record.read_text(encoding="utf-8") == "keep me"
    nothing_built(session)


def test_a_range_without_a_weekday_is_refused(session: Session) -> None:
    code = session.main("--symbols", "AAPL", "--start", "2024-08-17", "--end", "2024-08-18", "--port", "7497",
                        "--out", str(session.tmp / "bars"))
    assert code == cli.EXIT_REFUSED and not session.prompts
    nothing_built(session)


def test_a_legacy_single_file_folder_is_refused_before_the_gate(session: Session) -> None:
    out = session.tmp / "bars"
    legacy = offline_data_path(out, "AAPL")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("date,open,high,low,close,volume\n", encoding="utf-8")
    assert session.fetch(symbols="AAPL") == cli.EXIT_REFUSED
    assert "single file" in session.text and not session.prompts
    nothing_built(session)


# -- sessions -----------------------------------------------------------------------------


def test_happy_path_two_symbols_three_days(session: Session) -> None:
    assert session.fetch() == cli.EXIT_OK
    bars = session.tmp / "bars"
    for symbol in ("AAPL", "MSFT"):
        assert [p.name for p in (bars / symbol).glob(f"{symbol}_*.csv")] == [f"{symbol}_2024-08.csv"]
        assert len(read_symbol_bars(bars, symbol)) == 3 * 390
    text = session.text
    assert "Estimate (from IB's server time)" in text and "6 requests" in text
    assert text.count(": ok (390 bars)") == 6
    assert "AAPL: 3 ok, 0 no data, 0 failed, 0 already done, 0 not closed" in text
    assert "MSFT: 3 ok" in text
    for symbol in ("AAPL", "MSFT"):
        assert (f"{symbol} 2024-08-12: ok; first bar 09:30, last bar 15:59, 390 bars, first bar volume 1000, "
                "day total volume 617565") in text
    assert "stored exactly as IB sent them" in text and "UNTICKED" in text and "not settled" not in text
    assert "gaps:" in text and "gaps.csv" in text
    assert f"Recording: {session.recording}" in text
    assert "Recording check: 0 finding(s). Read the file yourself before it is ever committed." in text
    assert text.rstrip().splitlines()[-2] == "Result: complete (exit 0)"
    assert text.rstrip().splitlines()[-1] == cli.PATHS_NOTE
    assert session.recording.name == "fetch-bars-2024-08-21-001.jsonl"
    assert session.recording.read_text(encoding="utf-8").count("\n") > 10
    assert session.connections[0].state is ConnState.CLOSED


def test_a_second_run_resumes_and_the_recording_gets_a_new_name(session: Session) -> None:
    assert session.fetch() == cli.EXIT_OK
    requests_first = len(session.rig.requests)
    assert session.fetch() == cli.EXIT_OK
    assert len(session.rig.requests) == requests_first  # everything was already done
    assert "3 already done" in session.text
    names = sorted(p.name for p in (session.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
    assert names == ["fetch-bars-2024-08-21-001.jsonl", "fetch-bars-2024-08-21-002.jsonl"]


def test_the_default_bars_folder_is_under_the_home_folder(session: Session) -> None:
    code = session.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--no-record")
    assert code == cli.EXIT_OK
    assert len(read_symbol_bars(session.tmp / "home" / "Stoinkulator" / "bars", "AAPL")) == 3 * 390
    assert not (session.tmp / "home" / "Stoinkulator" / "recordings").exists()
    assert "Recording check" not in session.text and "none (--no-record)" in session.text


def test_a_failed_day_gives_exit_1_and_is_listed(session: Session) -> None:
    session.rig.script(date(2024, 8, 13), error_behaviour(200, "No security definition has been found"))
    assert session.fetch(symbols="AAPL") == cli.EXIT_FAILED
    text = session.text
    assert "2 ok, 0 no data, 1 failed" in text
    assert "2024-08-13: failed, IB 200: No security definition has been found" in text
    assert "None" not in text
    assert "IB error 200: No security definition has been found" in text
    assert "Result: finished with failed days" in text and "(exit 1)" in text


def test_a_symbol_that_cannot_be_fetched_gives_exit_1(session: Session) -> None:
    out = session.tmp / "bars" / "AAPL"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text("{ not json", encoding="utf-8")
    assert session.fetch(symbols="AAPL,MSFT") == cli.EXIT_FAILED
    assert "[manifest]" in session.text and "MSFT: 3 ok" in session.text


def test_connection_lost_gives_exit_4_with_the_finished_days_on_disk(session: Session) -> None:
    def lose(req_id: int, day: date, symbol: str) -> list[Any]:
        session.fakes[-1].drop("connectionClosed")
        return []

    session.rig.script(date(2024, 8, 14), lose)
    assert session.fetch(symbols="AAPL") == cli.EXIT_STOPPED
    assert len(read_symbol_bars(session.tmp / "bars", "AAPL")) == 2 * 390
    assert "STOPPED" in session.text and "Result: stopped early" in session.text
    assert "(exit 4)" in session.text
    assert session.connections[0].state is ConnState.CLOSED


def test_no_server_time_is_a_stop_with_exit_4(session: Session) -> None:
    def deaf(*args: Any, **kw: Any) -> None:
        return None

    original = session.client_factory

    def silent_time(**kw: Any) -> FakeIbClient:
        fake = original(**kw)
        fake.respond("reqCurrentTime", [])
        return fake

    session.client_factory = silent_time  # type: ignore[method-assign]
    session.clock.tick = SimClock.FAST  # nothing is coming: the 10 s timeout passes in a few polls
    assert session.fetch(symbols="AAPL") == cli.EXIT_STOPPED
    assert "did not answer the server time" in session.text
    assert not session.rig.requests


def test_cannot_connect_gives_exit_3_with_each_attempt(session: Session) -> None:
    session.behavior = ConnectBehavior.REFUSE
    assert session.fetch() == cli.EXIT_NO_CONNECTION
    assert "attempt 1:" in session.text and "attempt 2:" in session.text
    assert "502" in session.text
    assert "Result: could not connect (exit 3)" in session.text
    assert session.connections[0].state is ConnState.CLOSED
    assert not (session.tmp / "bars").exists()


def test_ctrl_c_closes_the_connection_and_the_recorder_and_exits_130(session: Session) -> None:
    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    session.sleep = interrupt
    assert session.fetch() == cli.EXIT_INTERRUPTED
    assert "Interrupted (Ctrl-C)" in session.text and "(exit 130)" in session.text
    assert session.connections[0].state is ConnState.CLOSED
    recorder = session.recorders[0]
    recorder.on_raw("probe", ())
    assert recorder.dropped == 1  # closed: later events are dropped, not written
    assert len(read_symbol_bars(session.tmp / "bars", "AAPL")) == 390  # the first day was kept


def test_a_second_pacing_violation_stops_with_exit_4(session: Session) -> None:
    text = "Historical Market Data Service error message:Historical data request pacing violation"
    session.rig.script(date(2024, 8, 13), error_behaviour(162, text))
    code = session.fetch(symbols="AAPL")
    assert code == cli.EXIT_STOPPED
    assert "pacing violation" in session.text


def test_an_account_shaped_string_never_reaches_the_output_or_the_recording(session: Session) -> None:
    secret = "DU7654321"
    text = f"Error validating request: account {secret} invalid"
    session.rig.script(date(2024, 8, 13), error_behaviour(321, text))
    assert session.fetch(symbols="AAPL") == cli.EXIT_FAILED
    assert "IB error 321: Error validating request: account ACCOUNT invalid" in session.text
    recording = session.recording.read_text(encoding="utf-8")
    for text in (session.text, recording):
        assert secret not in text and "7654321" not in text and "DU0000000" not in text
    assert "ManagedAccounts" not in session.text and "managedAccounts" not in session.text
    assert "0000000" not in recording


def test_the_user_and_machine_names_are_removed_from_the_recording(session: Session) -> None:
    session.env["USERNAME"] = "zedquux"
    session.env["COMPUTERNAME"] = "BOX-ZQ9"
    session.rig.script(date(2024, 8, 13), error_behaviour(321, "bad request from zedquux on BOX-ZQ9"))
    session.fetch(symbols="AAPL")
    recording = session.recording.read_text(encoding="utf-8").lower()
    assert "zedquux" not in recording and "box-zq9" not in recording
    assert "Recording check: 0 finding(s)" in session.text


def test_only_data_requests_reach_the_client(session: Session) -> None:
    assert session.fetch() == cli.EXIT_OK
    names = {call.name for fake in session.fakes for call in fake.calls}
    allowed = set(DATA_REQUESTS) | {"connect", "disconnect", "run"}  # run: the reader loop
    assert names and names <= allowed, names - allowed
    assert {"reqHistoricalData", "reqContractDetails", "reqCurrentTime"} <= names


def test_the_ibapi_logger_is_not_below_warning(session: Session) -> None:
    import logging

    logging.getLogger("ibapi").setLevel(logging.DEBUG)
    session.fetch(symbols="AAPL")
    assert logging.getLogger("ibapi").level >= logging.WARNING


# -- source checks -------------------------------------------------------------------------


def test_the_environment_is_read_only_in_main() -> None:
    tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    reads: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function.append(node.name)
            self.generic_visit(node)
            self.function.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr in {"environ", "getenv", "putenv", "environb"}:
                reads.append(self.function[0] if self.function else "<module>")
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            if node.id in {"environ", "getenv", "putenv"}:
                reads.append(self.function[0] if self.function else "<module>")

    Visitor().visit(tree)
    assert reads == ["main"], reads


def test_the_source_names_no_order_method_and_never_sets_the_opt_in() -> None:
    source = CLI_SOURCE.read_text(encoding="utf-8")
    for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "reqAllOpenOrders", "reqOpenOrders",
                 "reqAutoOpenOrders", "exerciseOptions", "whatIf", "Order(", "openOrder"):
        assert word not in source, word
    assert "os.environ[" not in source and "setdefault" not in source and "putenv" not in source


# -- dispatch ------------------------------------------------------------------------------


def child(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-P", "-c", code], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)


LOADED = ("print(sorted(m for m in sys.modules\n"
          "             if m == 'ibapi' or m.startswith(('ibapi.', 'MainStoinker.ib'))))")


def test_the_offline_commands_never_import_the_ib_package() -> None:
    done = child("import sys, runpy\nsys.argv = ['MainStoinker', 'backtest', '--help']\n"
                 "try:\n    runpy.run_module('MainStoinker', run_name='__main__')\n"
                 "except SystemExit:\n    pass\n"
                 + LOADED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout


def test_importing_the_package_does_not_import_the_ib_package() -> None:
    done = child("import sys, MainStoinker\n" + LOADED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_fetch_bars_help_exits_0_in_a_child_process() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    command = [sys.executable, "-P", "-m", "MainStoinker", "fetch-bars", "--help"]
    done = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "exit codes" in done.stdout and "--port" in done.stdout and "130" in done.stdout


def test_a_child_process_with_bad_arguments_exits_2_before_any_client() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    command = [sys.executable, "-P", "-m", "MainStoinker", "fetch-bars", "--symbols", "AAPL"]
    done = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 2
