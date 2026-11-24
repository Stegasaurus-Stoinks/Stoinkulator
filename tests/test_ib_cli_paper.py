"""`fetch-bars --paper` and `probe-data --paper`, driven in-process on `tests/fake_ib.py`.

Nothing opens a socket; time is injected; every file is under `tmp_path`. The environment is
always a mapping passed in (the live-port opt-in appears only inside such a mapping). Every
account-shaped value is assembled at run time. As in `test_ib_cli.py`, the real
`Stoinkulator` folders in the user's profile must be unchanged after each test.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.ib import cli
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import ConnState, IbConnection, NotPaperSession
from MainStoinker.Util.broker_guard import ALLOW_LIVE_ENV
from tests.fake_ib import FARM_MESSAGES, REQUEST_ARGS, FakeIbClient
from tests.test_ib_cli import DAYS, REAL_HOME, REAL_LISTING_AT_START, Session, Tty, _listing
from tests.test_ib_fetch import SERVER_NOW
from tests.test_ib_probe import SERVER, Script, TickClock

DIGITS = "4817203"
PAPER_ID = "DU" + DIGITS
LIVE_ID = "U" + DIGITS
FETCH = "fetch-bars"
PROBE = "probe-data"
NEW_CODE = {FETCH: cli.EXIT_NOT_PAPER_FETCH, PROBE: cli.EXIT_NOT_PAPER_PROBE}


@pytest.fixture(autouse=True)
def real_home_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    fake_home = tmp_path / "process-home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv(ALLOW_LIVE_ENV, raising=False)
    yield
    after = _listing(REAL_HOME)
    assert after == REAL_LISTING_AT_START, "A TEST WROTE INTO THE REAL HOME FOLDER"


class PaperSession(Session):
    """`Session` whose fakes report `accounts` and whose connections are built as the real
    factory builds them under `--paper` (`require_paper=self.require_paper`)."""

    def __init__(self, tmp_path: Path, accounts: str = PAPER_ID) -> None:
        super().__init__(tmp_path)
        self.accounts = accounts
        self.require_paper = True
        self.opened = 0
        self.open_error: Exception | None = None
        self.mid_list: str | None = None  # a managedAccounts list that arrives with the first clock answer

    def client_factory(self, *, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = super().client_factory(env=env, on_event=on_event, on_raw=on_raw, logger=logger)
        fake.managed_accounts = self.accounts
        if self.mid_list is not None:
            fake.respond("reqCurrentTime", [("managedAccounts", self.mid_list),
                                            ("currentTime", int(SERVER_NOW.timestamp()))])
        return fake

    def connection_factory(self, args: Any, observers: list[Any]) -> IbConnection:
        conn = IbConnection(args.host, args.port, args.client_id, env={}, client_factory=self.client_factory,
                            clock=self.clock, sleep=self.clock.sleep, poll_interval=0.002, attempts=2,
                            retry_delay=3, observers=observers, require_paper=self.require_paper)
        real_open = conn.open

        def counted_open() -> None:
            self.opened += 1
            if self.open_error is not None:
                raise self.open_error
            real_open()

        conn.open = counted_open  # type: ignore[method-assign]
        self.connections.append(conn)
        return conn

    def run(self, command: str, *argv: str, env: dict[str, str] | None = None) -> int:
        if env is not None:
            self.env.update(env)
        common: dict[str, Any] = {
            "env": self.env, "stdin": Tty(True), "input_fn": self.input_fn, "out": self.out,
            "clock": self.clock, "sleep": self.sleep, "today": date(2024, 8, 21),
            "connection_factory": self.connection_factory,
            "market_data_factory": self.market_data_factory,
            "recorder_factory": self.recorder_factory,
        }
        assert command == FETCH
        return cli.main(["--symbols", "AAPL", *DAYS, "--out", str(self.tmp / "bars"), *argv], **common)

    def requests(self) -> list[str]:
        return [n for f in self.fakes for n in f.call_names if n in REQUEST_ARGS]

    def every_text(self) -> str:
        parts = [self.text]
        for path in self.tmp.rglob("*.jsonl"):
            parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)


class PaperProbeSession(PaperSession):
    """The same, for `probe-data`: the fake and the clock are those of `tests/test_ib_probe.py`."""

    def __init__(self, tmp_path: Path, accounts: str = PAPER_ID) -> None:
        super().__init__(tmp_path, accounts)
        self.clock = TickClock()  # type: ignore[assignment]
        self.sleep = self.clock.sleep

    def client_factory(self, *, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger), managed_accounts=self.accounts,
                            farm_messages=FARM_MESSAGES)
        Script().apply(fake)
        if self.mid_list is not None:
            fake.respond("reqCurrentTime", [("managedAccounts", self.mid_list),
                                            ("currentTime", int(SERVER.timestamp()))])
        self.fakes.append(fake)
        return fake

    def run(self, command: str, *argv: str, env: dict[str, str] | None = None) -> int:
        assert command == PROBE
        if env is not None:
            self.env.update(env)
        return cli.probe_data_main(
            ["--symbols", "AAPL", "--no-history", "--quote-seconds", "10", *argv],
            env=self.env, stdin=Tty(True), input_fn=self.input_fn, out=self.out, clock=self.clock,
            sleep=self.sleep, today=date(2024, 11, 22), wall=lambda: SERVER - timedelta(seconds=2),
            connection_factory=self.connection_factory, market_data_factory=self.market_data_factory,
            recorder_factory=self.recorder_factory)


@pytest.fixture
def ps(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[PaperSession]:
    command = getattr(getattr(request.node, "callspec", None), "params", {}).get("command", FETCH)
    s = (PaperProbeSession if command == PROBE else PaperSession)(tmp_path)
    yield s
    for conn in s.connections:
        conn.close()
    for recorder in s.recorders:
        recorder.close()
    for fake in s.fakes:
        fake.close()


def nothing_built(s: PaperSession) -> None:
    assert not s.connections and not s.fakes and not s.recorders
    assert s.prompts == []  # the console gate was not even reached


def no_account_text(s: PaperSession, caplog: pytest.LogCaptureFixture) -> None:
    everything = s.every_text() + caplog.text
    assert DIGITS not in everything and LIVE_ID not in everything and PAPER_ID not in everything


# -- 1. the gate, before any connection object is built -----------------------------------------


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("port", ["7496", "4002", "4001", "7498", "1"])
@pytest.mark.parametrize("opt_in", [None, "1", ""])
def test_wrong_port_is_refused_even_with_the_opt_in(ps: PaperSession, command: str, port: str,
                                                    opt_in: str | None) -> None:
    env = {} if opt_in is None else {ALLOW_LIVE_ENV: opt_in}
    assert ps.run(command, "--paper", "--port", port, env=env) == cli.EXIT_REFUSED
    assert "Refused (--paper)" in ps.text and f"port {cli.PAPER_PORT}" in ps.text
    assert "paper mode:" not in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.2", "0.0.0.0", "::1", "127.0.0.1 ",
                                  "example.invalid", " 127.0.0.1"])
def test_wrong_host_is_refused(ps: PaperSession, command: str, host: str) -> None:
    assert ps.run(command, "--paper", "--port", "7497", "--host", host) == cli.EXIT_REFUSED
    assert "Refused (--paper)" in ps.text and "127.0.0.1" in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("value", ["", "0", "1", "true", "no"])
def test_live_opt_in_in_the_environment_is_refused_whatever_its_value(ps: PaperSession, command: str,
                                                                      value: str) -> None:
    assert ps.run(command, "--paper", env={ALLOW_LIVE_ENV: value}) == cli.EXIT_REFUSED
    assert ALLOW_LIVE_ENV in ps.text and "unset it" in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_every_failed_condition_is_named(ps: PaperSession, command: str) -> None:
    code = ps.run(command, "--paper", "--port", "7496", "--host", "localhost", env={ALLOW_LIVE_ENV: "1"})
    assert code == cli.EXIT_REFUSED
    assert "7497" in ps.text and "127.0.0.1" in ps.text and "unset it" in ps.text
    nothing_built(ps)


# -- 2. a paper list passes --------------------------------------------------------------------


@pytest.mark.parametrize("accounts", [PAPER_ID, "DUX123456"])
@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_paper_list_passes_and_the_command_runs(ps: PaperSession, caplog: pytest.LogCaptureFixture,
                                                command: str, accounts: str) -> None:
    caplog.set_level(logging.DEBUG)
    ps.accounts = accounts
    code = ps.run(command, "--paper")  # no --port: the paper port is the default
    lines = ps.text.splitlines()
    assert lines[0] == "paper mode: port 7497, account check on"
    assert code in (cli.EXIT_OK, cli.EXIT_FAILED) if command == PROBE else code == cli.EXIT_OK
    assert ps.connections[0].port == 7497
    assert "reqCurrentTime" in ps.requests()
    assert ps.prompts == ["To proceed, type the port number (7497) and press Enter: "]  # the gate stays
    result = [line for line in lines if line.startswith("Result:")]
    assert len(result) == 1 and result[0].endswith("; paper account verified")
    no_account_text(ps, caplog)


def test_paper_fetch_result_matches_the_run_without_the_flag(tmp_path: Path) -> None:
    results = []
    for flag in ([], ["--paper"]):
        s = PaperSession(tmp_path / ("p" if flag else "n"))
        s.require_paper = bool(flag)
        try:
            code = s.run(FETCH, "--port", "7497", *flag)
            body = [ln for ln in s.text.splitlines() if "paper" not in ln and "Recording" not in ln
                    and "recording" not in ln and not ln.startswith(("Result:", "  Output", "  Records"))]
            results.append((code, body, s.requests()))
        finally:
            for conn in s.connections:
                conn.close()
            for fake in s.fakes:
                fake.close()
            for rec in s.recorders:
                rec.close()
    assert results[0][0] == results[1][0] == cli.EXIT_OK
    assert results[0][2] == results[1][2]


def test_without_the_flag_there_is_no_paper_text_and_the_old_requirements(ps: PaperSession) -> None:
    ps.require_paper = False
    ps.accounts = LIVE_ID  # a live-looking list is not looked at without --paper
    assert ps.run(FETCH, "--port", "7497") == cli.EXIT_OK
    assert "paper" not in ps.text.lower().replace("paper account", "")
    assert ps.run(FETCH, "--port", "7497", env={ALLOW_LIVE_ENV: "1"}) in (cli.EXIT_OK, cli.EXIT_FAILED)
    with pytest.raises(SystemExit) as exc:
        ps.run(FETCH)  # no --port and no --paper: still an argument error
    assert exc.value.code == 2


# -- 3. a session that is not a paper session ---------------------------------------------------


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("accounts", [LIVE_ID, "", f"{PAPER_ID},{LIVE_ID}", "D" + DIGITS])
def test_a_non_paper_list_stops_with_the_new_exit_code(ps: PaperSession, caplog: pytest.LogCaptureFixture,
                                                       command: str, accounts: str) -> None:
    caplog.set_level(logging.DEBUG)
    ps.accounts = accounts
    code = ps.run(command, "--paper")
    assert code == NEW_CODE[command]
    assert code not in (cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_REFUSED, cli.EXIT_NO_CONNECTION,
                        cli.EXIT_INTERRUPTED)
    assert ps.requests() == []
    assert len(ps.fakes) == 1  # not retried
    assert ps.text.count(cli.NOT_PAPER_LINE) == 1
    assert "paper account verified" not in ps.text
    assert f"(exit {code})" in ps.text
    no_account_text(ps, caplog)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_paper_verified_false_after_open_sends_nothing(ps: PaperSession, caplog: pytest.LogCaptureFixture,
                                                       command: str) -> None:
    caplog.set_level(logging.DEBUG)
    ps.require_paper = False  # a connection that opens fine but cannot vouch for the account
    ps.accounts = LIVE_ID
    code = ps.run(command, "--paper")
    assert code == NEW_CODE[command]
    assert ps.connections[0].paper_verified is False
    assert ps.requests() == []
    assert ps.connections[0].state is ConnState.CLOSED
    assert "paper account verified" not in ps.text
    no_account_text(ps, caplog)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_a_failing_list_after_ready_stops_the_run(ps: PaperSession, caplog: pytest.LogCaptureFixture,
                                                  command: str) -> None:
    caplog.set_level(logging.DEBUG)
    ps.mid_list = LIVE_ID
    code = ps.run(command, "--paper")
    assert ps.requests() == ["reqCurrentTime"]  # the first request only; nothing after the list
    assert ps.connections[0].state is ConnState.CLOSED
    assert code == NEW_CODE[command]  # not "stopped, run again" and not "a step failed"
    assert ps.connections[0].paper_verified is False
    assert ps.text.count(cli.NOT_PAPER_LINE) == 1
    assert "paper account verified" not in ps.text
    assert "run the same command again" not in ps.text and "What to do" not in ps.text
    assert f"(exit {code})" in ps.text and cli.NOT_PAPER_LABEL in ps.text
    no_account_text(ps, caplog)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_check_broker_port_still_runs_in_paper_mode(ps: PaperSession, monkeypatch: pytest.MonkeyPatch,
                                                    command: str) -> None:
    calls: list[int] = []
    real = cli.check_broker_port

    def spy(port: int, env: Any) -> None:
        calls.append(port)
        real(port, env)

    monkeypatch.setattr(cli, "check_broker_port", spy)
    ps.run(command, "--paper")
    assert calls == [7497]


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("flag", [["--pape"], ["--pap", "--port", "7497"], ["--por", "7497"]])
def test_abbreviated_options_are_usage_errors(ps: PaperSession, capsys: pytest.CaptureFixture[str],
                                              command: str, flag: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        ps.run(command, *flag)
    assert info.value.code == 2
    capsys.readouterr()
    nothing_built(ps)


@pytest.mark.parametrize("flag, expected", [(["--paper"], True), ([], False)])
def test_the_real_connection_is_built_with_require_paper_only_in_paper_mode(
        ps: PaperSession, monkeypatch: pytest.MonkeyPatch, flag: list[str], expected: bool) -> None:
    seen: list[dict[str, Any]] = []

    def spy(*args: Any, **kw: Any) -> IbConnection:
        seen.append(dict(kw))
        kw.update(client_factory=ps.client_factory, clock=ps.clock, sleep=ps.clock.sleep, poll_interval=0.002)
        conn = IbConnection(*args, **kw)
        ps.connections.append(conn)
        return conn

    monkeypatch.setattr(cli, "IbConnection", spy)
    ps.accounts = LIVE_ID  # stops at the account check: nothing is requested
    code = cli.main(["--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(ps.tmp / "bars"),
                     "--no-record", *flag], env=ps.env, stdin=Tty(True), input_fn=ps.input_fn, out=ps.out,
                    clock=ps.clock, sleep=ps.sleep, today=date(2024, 8, 21))
    assert seen[0]["require_paper"] is expected
    assert ps.requests() == [] or not expected
    assert (code == cli.EXIT_NOT_PAPER_FETCH) is expected


# -- 3b. nothing else from open() is retried in paper mode ---------------------------------------


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_other_open_errors_are_fatal_in_paper_mode(ps: PaperSession, command: str) -> None:
    ps.open_error = RuntimeError("something unexpected")
    code = ps.run(command, "--paper")
    assert ps.opened == 1 and ps.requests() == []
    assert "Unexpected RuntimeError" in ps.text
    assert code == (cli.EXIT_STOPPED if command == FETCH else cli.EXIT_FAILED)
    assert "What to do" not in ps.text  # no "check the port and try again" advice
    result = [ln for ln in ps.text.splitlines() if ln.startswith("Result:")]
    assert result == [f"Result: {cli.OPEN_FAILED_LABEL} (exit {code})"]
    assert "connected" not in result[0].lower().replace("connection attempt", "")
    assert "resume" not in ps.text and "run again" not in ps.text
    assert "paper account verified" not in ps.text


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_not_paper_session_without_the_flag_is_an_unexpected_error(ps: PaperSession, command: str) -> None:
    ps.open_error = NotPaperSession("x")
    argv = ["--port", "7497"]
    code = ps.run(command, *argv)  # must not raise KeyError
    assert code == (cli.EXIT_STOPPED if command == FETCH else cli.EXIT_FAILED)
    assert "Unexpected NotPaperSession" in ps.text and ps.requests() == []


def test_the_same_error_without_the_flag_is_still_a_connect_failure(ps: PaperSession) -> None:
    ps.open_error = RuntimeError("something unexpected")
    ps.require_paper = False
    assert ps.run(FETCH, "--port", "7497") == cli.EXIT_NO_CONNECTION


# -- help and documentation ---------------------------------------------------------------------


@pytest.mark.parametrize("parser, text, code", [
    (cli.build_parser, cli.EXIT_CODES_TEXT, cli.EXIT_NOT_PAPER_FETCH),
    (cli.build_probe_parser, cli.PROBE_EXIT_CODES_TEXT, cli.EXIT_NOT_PAPER_PROBE),
])
def test_help_documents_the_flag_and_the_new_code(parser: Any, text: str, code: int) -> None:
    shown = parser().format_help()
    assert "--paper" in shown and f"  {code}    --paper only" in shown
    assert f"  {code}  " in text
    listed = [int(line.split()[0]) for line in text.splitlines()[1:] if line[:3].strip().isdigit()]
    assert listed.count(code) == 1


def test_new_codes_are_distinct_from_the_existing_ones() -> None:
    fetch_codes = [cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_REFUSED, cli.EXIT_NO_CONNECTION, cli.EXIT_STOPPED,
                   cli.EXIT_INTERRUPTED]
    probe_codes = [cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_REFUSED, cli.EXIT_NO_CONNECTION,
                   cli.EXIT_INTERRUPTED]
    assert cli.EXIT_NOT_PAPER_FETCH not in fetch_codes
    assert cli.EXIT_NOT_PAPER_PROBE not in probe_codes


def test_the_helpers_take_their_inputs_as_parameters() -> None:
    assert cli.paper_refusal("127.0.0.1", 7497, {}) is None
    assert cli.paper_refusal("127.0.0.1", 7497, {ALLOW_LIVE_ENV: ""}) is not None
    out = io.StringIO()

    class Stub:
        paper_verified = True

    assert cli.paper_session_ok(Stub(), cli._Say(out)) is True  # type: ignore[arg-type]
    Stub.paper_verified = False
    assert cli.paper_session_ok(Stub(), cli._Say(out)) is False  # type: ignore[arg-type]
    assert out.getvalue().strip() == cli.NOT_PAPER_LINE


# -- 4. --no-prompt: the console gate is skipped, and only together with --paper ----------------


class NoStdin(io.StringIO):
    """A stdin that fails the test on any access: no tty check, no read."""

    def isatty(self) -> bool:
        raise AssertionError("stdin was asked whether it is a terminal")

    def read(self, *args: Any) -> str:
        raise AssertionError("stdin was read")

    def readline(self, *args: Any) -> str:
        raise AssertionError("stdin was read")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("stdin was read")


def run_no_prompt(s: PaperSession, command: str, *argv: str, env: dict[str, str] | None = None,
                  stdin: Any = None) -> int:
    """`PaperSession.run`/`PaperProbeSession.run`, but with a stdin that must never be touched."""
    if env is not None:
        s.env.update(env)

    def no_input(prompt: str) -> str:
        raise AssertionError("the prompt was shown")

    kwargs: dict[str, Any] = {
        "env": s.env, "stdin": NoStdin() if stdin is None else stdin, "input_fn": no_input, "out": s.out,
        "clock": s.clock, "sleep": s.sleep, "connection_factory": s.connection_factory,
        "market_data_factory": s.market_data_factory, "recorder_factory": s.recorder_factory,
    }
    if command == PROBE:
        return cli.probe_data_main(
            ["--symbols", "AAPL", "--no-history", "--quote-seconds", "10", *argv], today=date(2024, 11, 22),
            wall=lambda: SERVER - timedelta(seconds=2), **kwargs)
    return cli.main(["--symbols", "AAPL", *DAYS, "--out", str(s.tmp / "bars"), *argv],
                    today=date(2024, 8, 21), **kwargs)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("argv", [["--no-prompt"], ["--no-prompt", "--port", "7497"],
                                  ["--port", "7497", "--no-prompt"], ["--no-prompt", "--port", "7496"]])
def test_no_prompt_without_paper_is_a_usage_error(ps: PaperSession, capsys: pytest.CaptureFixture[str],
                                                  command: str, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_no_prompt(ps, command, *argv)
    assert info.value.code == 2
    capsys.readouterr()
    nothing_built(ps)
    assert ps.text == ""  # not even the paper line


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_no_prompt_without_paper_names_the_flag(ps: PaperSession, capsys: pytest.CaptureFixture[str],
                                                command: str) -> None:
    with pytest.raises(SystemExit):
        run_no_prompt(ps, command, "--port", "7497", "--no-prompt")
    assert "--no-prompt is valid only together with --paper" in capsys.readouterr().err


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_paper_and_no_prompt_need_no_terminal_and_read_nothing(ps: PaperSession,
                                                               caplog: pytest.LogCaptureFixture,
                                                               command: str) -> None:
    caplog.set_level(logging.DEBUG)
    code = run_no_prompt(ps, command, "--paper", "--no-prompt")  # NoStdin raises on any access
    lines = ps.text.splitlines()
    assert lines[0] == "paper mode: port 7497, account check on, no prompt"
    assert code in (cli.EXIT_OK, cli.EXIT_FAILED) if command == PROBE else code == cli.EXIT_OK
    assert ps.prompts == []
    assert ps.connections[0].port == 7497 and ps.connections[0].host == "127.0.0.1"
    assert "reqCurrentTime" in ps.requests()
    result = [line for line in lines if line.startswith("Result:")]
    assert len(result) == 1 and result[0].endswith("; paper account verified")
    no_account_text(ps, caplog)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_a_stdin_that_is_no_terminal_does_not_matter_with_no_prompt(ps: PaperSession, command: str) -> None:
    assert run_no_prompt(ps, command, "--paper", "--no-prompt", stdin=Tty(False)) in (
        cli.EXIT_OK, cli.EXIT_FAILED)
    assert "Refusing to run" not in ps.text and ps.requests()


def test_without_no_prompt_the_gate_still_needs_a_terminal(ps: PaperSession) -> None:
    code = cli.main(["--symbols", "AAPL", *DAYS, "--out", str(ps.tmp / "bars"), "--paper"], env=ps.env,
                    stdin=Tty(False), input_fn=ps.input_fn, out=ps.out, clock=ps.clock, sleep=ps.sleep,
                    today=date(2024, 8, 21), connection_factory=ps.connection_factory,
                    market_data_factory=ps.market_data_factory, recorder_factory=ps.recorder_factory)
    assert code == cli.EXIT_REFUSED and "Refusing to run" in ps.text
    assert ps.requests() == [] and ps.prompts == []


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("port", ["7496", "4002", "4001", "7498", "1"])
@pytest.mark.parametrize("opt_in", [None, "1", ""])
def test_no_prompt_keeps_the_port_refusal(ps: PaperSession, command: str, port: str,
                                          opt_in: str | None) -> None:
    env = {} if opt_in is None else {ALLOW_LIVE_ENV: opt_in}
    assert run_no_prompt(ps, command, "--paper", "--no-prompt", "--port", port, env=env) == cli.EXIT_REFUSED
    assert "Refused (--paper)" in ps.text and "paper mode:" not in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "0.0.0.0", "::1", " 127.0.0.1"])
def test_no_prompt_keeps_the_host_refusal(ps: PaperSession, command: str, host: str) -> None:
    code = run_no_prompt(ps, command, "--paper", "--no-prompt", "--port", "7497", "--host", host)
    assert code == cli.EXIT_REFUSED and "Refused (--paper)" in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("value", ["", "0", "1"])
def test_no_prompt_keeps_the_live_opt_in_refusal(ps: PaperSession, command: str, value: str) -> None:
    code = run_no_prompt(ps, command, "--paper", "--no-prompt", env={ALLOW_LIVE_ENV: value})
    assert code == cli.EXIT_REFUSED
    assert "unset it" in ps.text
    nothing_built(ps)


@pytest.mark.parametrize("command", [FETCH, PROBE])
@pytest.mark.parametrize("accounts", [LIVE_ID, "", f"{PAPER_ID},{LIVE_ID}"])
def test_no_prompt_still_stops_a_non_paper_account_with_zero_requests(
        ps: PaperSession, caplog: pytest.LogCaptureFixture, command: str, accounts: str) -> None:
    caplog.set_level(logging.DEBUG)
    ps.accounts = accounts
    code = run_no_prompt(ps, command, "--paper", "--no-prompt")
    assert code == NEW_CODE[command]
    assert ps.requests() == [] and len(ps.fakes) == 1
    assert ps.text.count(cli.NOT_PAPER_LINE) == 1 and "paper account verified" not in ps.text
    assert ps.prompts == []
    no_account_text(ps, caplog)


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_no_prompt_still_stops_when_the_list_fails_after_ready(ps: PaperSession, command: str) -> None:
    ps.mid_list = LIVE_ID
    code = run_no_prompt(ps, command, "--paper", "--no-prompt")
    assert code == NEW_CODE[command] and ps.requests() == ["reqCurrentTime"]


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_no_prompt_still_runs_the_ordinary_port_guard(ps: PaperSession, monkeypatch: pytest.MonkeyPatch,
                                                      command: str) -> None:
    calls: list[int] = []
    real = cli.check_broker_port

    def spy(port: int, env: Any) -> None:
        calls.append(port)
        real(port, env)

    monkeypatch.setattr(cli, "check_broker_port", spy)
    run_no_prompt(ps, command, "--paper", "--no-prompt")
    assert calls == [7497]


@pytest.mark.parametrize("command", [FETCH, PROBE])
def test_no_prompt_with_the_port_and_host_given_is_fine(ps: PaperSession, command: str) -> None:
    code = run_no_prompt(ps, command, "--no-prompt", "--paper", "--port", "7497", "--host", "127.0.0.1")
    assert code in (cli.EXIT_OK, cli.EXIT_FAILED)


def test_the_real_wiring_passes_require_paper_with_no_prompt(ps: PaperSession,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def spy(*args: Any, **kw: Any) -> IbConnection:
        seen.append(dict(kw))
        kw.update(client_factory=ps.client_factory, clock=ps.clock, sleep=ps.clock.sleep, poll_interval=0.002)
        conn = IbConnection(*args, **kw)
        ps.connections.append(conn)
        return conn

    monkeypatch.setattr(cli, "IbConnection", spy)
    ps.accounts = LIVE_ID  # stops at the account check: nothing is requested
    code = cli.main(["--symbols", "AAPL", *DAYS, "--out", str(ps.tmp / "bars"), "--no-record", "--paper",
                     "--no-prompt"], env=ps.env, stdin=NoStdin(), input_fn=ps.input_fn, out=ps.out,
                    clock=ps.clock, sleep=ps.sleep, today=date(2024, 8, 21))
    assert seen[0]["require_paper"] is True
    assert code == cli.EXIT_NOT_PAPER_FETCH and ps.requests() == [] and ps.prompts == []


@pytest.mark.parametrize("parser", [cli.build_parser, cli.build_probe_parser])
def test_help_documents_no_prompt(parser: Any) -> None:
    shown = " ".join(parser().format_help().split())
    assert "--no-prompt" in shown and "only with --paper" in shown
