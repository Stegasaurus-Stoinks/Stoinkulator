"""Gaps in `tests/test_ib_cli.py`, found by mutating `MainStoinker/ib/cli.py` and probing the command.

Same rules as there: every client is `FakeIbClient`, time is injected, the environment is a mapping
passed in, every file is under `tmp_path`, and nothing opens a socket. Nothing here runs the
`fetch-bars` command in a child process.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.ib import cli
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import ConnState, IbConnection
from MainStoinker.ib.recorder import SessionRecorder, scan_recording
from tests.fake_ib import ConnectBehavior, FakeIbClient
from tests.ib_replay import handshake_kwargs, install, load_recording
from tests.test_ib_cli import DAYS, LOADED, REPO, Session, Tty, child
from tests.test_ib_fetch import SERVER_NOW, error_behaviour

OPT_IN = "STOINK_ALLOW_LIVE_PORT"


@pytest.fixture
def sess(tmp_path: Path) -> Iterator[Session]:
    s = Session(tmp_path)
    yield s
    for conn in s.connections:
        conn.close()
    for recorder in s.recorders:
        recorder.close()
    for fake in s.fakes:
        fake.close()


def argv_for(s: Session, port: str = "7497", symbols: str = "AAPL", *extra: str) -> list[str]:
    return ["--symbols", symbols, *DAYS, "--port", port, "--out", str(s.tmp / "bars"), *extra]


def untouched(s: Session) -> None:
    """Nothing was built, asked or created: not the bars folder, not the home folder."""
    assert not s.connections and not s.fakes and not s.recorders
    assert not (s.tmp / "bars").exists()
    assert not (Path(s.env["USERPROFILE"]) / "Stoinkulator").exists()


# -- the exit codes are the documented numbers --------------------------------------------------


def test_the_exit_code_constants_are_the_documented_numbers() -> None:
    assert (cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_REFUSED, cli.EXIT_NO_CONNECTION, cli.EXIT_STOPPED,
            cli.EXIT_INTERRUPTED) == (0, 1, 2, 3, 4, 130)


def test_a_refusal_returns_the_plain_number_2(sess: Session) -> None:
    assert sess.main(*argv_for(sess, "7496")) == 2
    assert sess.fetch(tty=False) == 2


# -- the gate -----------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["74 97", "7 497", "07497", "٧٤٩٧", "7497;", "-7497", "+7497"])
def test_a_near_miss_of_the_port_is_refused_and_creates_nothing(sess: Session, typed: str) -> None:
    sess.typed = typed
    assert sess.main(*argv_for(sess)) == 2
    assert len(sess.prompts) == 1
    untouched(sess)


@pytest.mark.parametrize("typed", ["7497\n", "\t7497\r\n", "  7497  "])
def test_whitespace_around_the_port_is_ignored(sess: Session, typed: str) -> None:
    sess.typed = typed
    assert sess.main(*argv_for(sess)) == 0


def test_ctrl_c_at_the_prompt_is_a_refusal_that_builds_and_creates_nothing(sess: Session) -> None:
    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    sess.input_fn = interrupt  # type: ignore[method-assign]
    assert sess.main(*argv_for(sess)) == 2
    assert "Nothing was started" in sess.text
    untouched(sess)


def test_every_gate_refusal_creates_no_folder(sess: Session) -> None:
    assert sess.main(*argv_for(sess), tty=False) == 2
    untouched(sess)
    sess.typed = ""
    assert sess.main(*argv_for(sess)) == 2
    untouched(sess)


def test_a_stdin_that_cannot_say_whether_it_is_a_terminal_is_refused(sess: Session) -> None:
    code = cli.main(argv_for(sess), env=sess.env, stdin=object(), input_fn=sess.input_fn,  # type: ignore[arg-type]
                    out=sess.out, connection_factory=sess.connection_factory,
                    market_data_factory=sess.market_data_factory, recorder_factory=sess.recorder_factory,
                    clock=sess.clock, sleep=sess.sleep, today=date(2024, 8, 21))
    assert code == 2 and not sess.prompts
    untouched(sess)


# -- the port guard -------------------------------------------------------------------------------


@pytest.mark.parametrize("port", ["7496", "4001", "5000", "1"])
def test_a_port_that_is_not_paper_is_refused_before_the_gate_with_the_powershell_line(sess: Session,
                                                                                      port: str) -> None:
    assert sess.main(*argv_for(sess, port)) == 2
    assert f'$env:{OPT_IN} = "1"' in sess.text
    assert not sess.prompts
    untouched(sess)


@pytest.mark.parametrize("value", ["0", "", " 1", "1 ", "true", "yes", "01", "1\n"])
def test_only_the_exact_text_1_opts_in_to_a_live_port(sess: Session, value: str) -> None:
    sess.env[OPT_IN] = value
    assert sess.main(*argv_for(sess, "7496")) == 2
    assert not sess.prompts
    untouched(sess)


@pytest.mark.parametrize("port", ["7496", "4001", "5000"])
def test_the_exact_opt_in_1_reaches_the_gate_and_then_the_session(sess: Session, port: str) -> None:
    sess.env[OPT_IN] = "1"
    sess.typed = port

    def live_connection(args: Any, observers: list[Any]) -> IbConnection:
        # the connection checks the port again, with the same mapping `main` was given
        conn = IbConnection(args.host, args.port, args.client_id, env=sess.env,
                            client_factory=sess.client_factory,
                            clock=sess.clock, sleep=sess.clock.sleep, poll_interval=0.002, attempts=2,
                            retry_delay=3, observers=observers)
        sess.connections.append(conn)
        return conn

    sess.connection_factory = live_connection  # type: ignore[method-assign]
    assert sess.main(*argv_for(sess, port)) == 0
    assert len(sess.prompts) == 1 and f"({port})" in sess.prompts[0]
    assert sess.connections and sess.fakes


@pytest.mark.parametrize("port", ["7497", "4002"])
def test_paper_ports_need_no_opt_in(sess: Session, port: str) -> None:
    sess.typed = port
    assert sess.main(*argv_for(sess, port)) == 0


def test_the_command_never_puts_the_opt_in_into_the_mapping_it_was_given(sess: Session) -> None:
    before = dict(sess.env)
    sess.main(*argv_for(sess, "7496"))
    sess.main(*argv_for(sess))
    assert sess.env == before


# -- paths ----------------------------------------------------------------------------------------


def test_check_path_refuses_the_repository_without_help_from_the_onedrive_rule(tmp_path: Path) -> None:
    """The repository of the owner is inside OneDrive, which hides a broken repository rule."""
    repo = tmp_path / "repo"
    for inside in (repo, repo / "bars", repo / "a" / ".." / "b", repo / "a" / "b" / "c.jsonl"):
        with pytest.raises(cli.PathRefused, match="repository"):
            cli.check_path(inside, {}, "the bars folder", repo=repo)
    for outside in (tmp_path / "repo2" / "bars", tmp_path / "bars", tmp_path / "repo" / ".." / "bars"):
        cli.check_path(outside, {}, "the bars folder", repo=repo)


def test_an_existing_recording_is_refused_before_the_gate(sess: Session) -> None:
    record = sess.tmp / "rec" / "mine.jsonl"
    record.parent.mkdir()
    record.write_text("keep me", encoding="utf-8")
    assert sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(record))) == 2
    assert not sess.prompts and "exists" in sess.text
    assert record.read_text(encoding="utf-8") == "keep me"
    assert not (sess.tmp / "bars").exists()


def test_a_recording_path_that_is_a_folder_is_refused_before_the_gate(sess: Session) -> None:
    assert sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(sess.tmp))) == 2
    assert not sess.prompts
    untouched(sess)


def test_a_recording_that_cannot_be_created_exits_2_with_nothing_connected(sess: Session) -> None:
    blocker = sess.tmp / "blocker"
    blocker.write_text("a file where a folder is needed", encoding="utf-8")
    code = sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(blocker / "r.jsonl")))
    assert code == 2
    assert "Cannot start the session recording" in sess.text
    assert "Result: refused" in sess.text
    assert not sess.connections and not sess.fakes


def test_relative_paths_are_taken_from_the_current_folder(sess: Session) -> None:
    code = sess.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--out", "relative_bars", "--record",
                     "relative_rec.jsonl")
    assert code == 0
    assert (Path.cwd() / "relative_bars" / "AAPL").is_dir() and (Path.cwd() / "relative_rec.jsonl").is_file()


@pytest.mark.xfail(strict=True, reason="defect: --out naming a file passes the gate, connects to TWS, "
                                       "then exits 4; it should be refused (exit 2) before the gate")
@pytest.mark.parametrize("shape", ["file", "under_file"])
def test_an_output_path_that_is_a_file_is_refused_before_the_gate(sess: Session, shape: str) -> None:
    blocker = sess.tmp / "afile"
    blocker.write_text("x", encoding="utf-8")
    out = blocker if shape == "file" else blocker / "sub"
    code = sess.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(out))
    assert code == 2 and not sess.prompts and not sess.connections


# -- arguments --------------------------------------------------------------------------------------


@pytest.mark.parametrize("symbols, expected", [
    ("aapl,msft", ["AAPL", "MSFT"]),
    ("AAPL MSFT", ["AAPL", "MSFT"]),
    ("AAPL, MSFT ,tsla", ["AAPL", "MSFT", "TSLA"]),
    ("AAPL,AAPL,aapl", ["AAPL"]),
    ("AAPL,,MSFT", ["AAPL", "MSFT"]),
])
def test_symbols_are_split_upper_cased_and_deduplicated(symbols: str, expected: list[str]) -> None:
    args = cli.parse_args(["--symbols", *symbols.split(" "), *DAYS, "--port", "7497"])
    assert args.symbols == expected


@pytest.mark.parametrize("symbols", ["A!", "AA PL,", ",,", "A/B", "../x", "AAPL;MSFT", "АAPL"])
def test_a_bad_symbol_is_a_usage_error(symbols: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.parse_args(["--symbols", symbols, *DAYS, "--port", "7497"])
    assert info.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize("flag, value", [
    ("--start", "2024-8-12"), ("--start", "20240812"), ("--start", "2024-02-30"),
    ("--start", "2024-08-12T09:30"),
    ("--start", "tomorrow"), ("--end", "2024-13-01"), ("--end", ""), ("--end", " 2024-08-14"),
    ("--client-id", "-1"), ("--client-id", "0"), ("--client-id", "123"), ("--client-id", "x"),
    ("--client-id", "2147483648"), ("--connect-timeout", "0"), ("--connect-timeout", "-5"),
    ("--connect-timeout", "nan"), ("--connect-timeout", "inf"), ("--connect-timeout", "1e9"),
    ("--port", "0"), ("--port", "65536"), ("--port", "7497.0"), ("--port", ""),
])
def test_bad_option_values_are_usage_errors(flag: str, value: str,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    options = {"--start": "2024-08-12", "--end": "2024-08-14", "--port": "7497"}
    options[flag] = value
    argv = ["--symbols", "AAPL"] + [part for pair in options.items() for part in pair]
    with pytest.raises(SystemExit) as info:
        cli.parse_args(argv)
    assert info.value.code == 2
    capsys.readouterr()


def test_there_is_no_option_that_skips_the_gate() -> None:
    options = {o for action in cli.build_parser()._actions for o in action.option_strings}
    assert options == {"-h", "--help", "--symbols", "--start", "--end", "--port", "--host", "--client-id",
                       "--out", "--record", "--no-record", "--connect-timeout"}


@pytest.mark.parametrize("flag", ["--yes", "-y", "--force", "--no-gate", "--skip-gate", "--assume-yes",
                                  "--confirm", "--non-interactive", "--batch", "--unattended"])
def test_a_gate_skipping_option_is_a_usage_error(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.parse_args(["--symbols", "AAPL", *DAYS, "--port", "7497", flag])
    assert info.value.code == 2
    capsys.readouterr()


def test_help_says_the_command_connects_and_only_the_owner_runs_it(
        capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.parse_args(["--help"])
    assert info.value.code == 0
    shown = " ".join(capsys.readouterr().out.split()).lower()
    assert "owner" in shown and "tws" in shown and "port number" in shown
    for line in ("0 complete", "1 finished", "2 refused", "3 could not connect", "4 stopped",
                 "130 interrupted"):
        assert line in shown, line


@pytest.mark.parametrize("start, end", [("2024-08-14", "2024-08-14"), ("2024-08-12", "2024-08-12")])
def test_a_single_day_is_a_valid_range(sess: Session, start: str, end: str) -> None:
    code = sess.main("--symbols", "AAPL", "--start", start, "--end", end, "--port", "7497", "--out",
                     str(sess.tmp / "bars"))
    assert code == 0 and "1 ok" in sess.text


def test_a_range_wholly_in_the_future_writes_nothing_and_says_why(sess: Session) -> None:
    code = sess.main("--symbols", "AAPL", "--start", "2030-01-07", "--end", "2030-01-11", "--port", "7497",
                     "--out", str(sess.tmp / "bars"))
    assert "not closed" in sess.text and "Result:" in sess.text
    assert code in (2, 4)
    assert not list((sess.tmp / "bars").rglob("*.csv"))
    assert sess.connections[0].state is ConnState.CLOSED


# -- what is printed ---------------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="defect: VOLUME_NOTE still says the unit is not settled; the owner's "
                                       "fact is that volumes are shares with the lots setting unticked")
def test_the_volume_note_states_the_settled_fact() -> None:
    note = cli.VOLUME_NOTE.lower()
    assert "not settled" not in note
    assert "as ib sends" in note or "as ib sent" in note
    assert "unticked" in note or "not ticked" in note
    assert "tens of thousands" in note


@pytest.mark.xfail(strict=True, reason="defect: a failure without an IB code prints 'failed, None: ...'")
def test_a_failure_without_a_code_does_not_print_none(sess: Session) -> None:
    def lose(req_id: int, day: date, symbol: str) -> list[Any]:
        sess.fakes[-1].drop("connectionClosed")
        return []

    sess.rig.script(date(2024, 8, 14), lose)
    sess.fetch(symbols="AAPL")
    assert "None" not in sess.text


def test_a_nonzero_scan_result_is_printed_not_hidden(sess: Session) -> None:
    """The scan after the run must be the real one: a recorder that was not given the private words
    leaves the user name in the file, and the command must say so."""
    user = "zq" + "uux"
    sess.env["USERNAME"] = user

    def bare_recorder(args: Any) -> SessionRecorder:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        recorder = SessionRecorder(args.record, clock=sess.clock, command=["fetch-bars"], port=args.port)
        sess.recorders.append(recorder)
        return recorder

    sess.rig.script(date(2024, 8, 13), error_behaviour(321, f"bad request from {user}"))
    code = cli.main(
        argv_for(sess), env=sess.env, stdin=Tty(True), input_fn=sess.input_fn, out=sess.out,
        connection_factory=sess.connection_factory, market_data_factory=sess.market_data_factory,
        recorder_factory=bare_recorder, clock=sess.clock, sleep=sess.sleep, today=date(2024, 8, 21))
    assert code == 1
    assert "Recording check: 0 finding(s)" not in sess.text
    assert "finding(s)" in sess.text and "line " in sess.text


def test_the_real_recorder_gets_the_private_words_and_the_header_holds_none(
        sess: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` without a recorder factory builds the real one: names, the home path and the paths on the
    command line must not reach the file, including the header's `command`."""
    user = "zq" + "uux"
    machine = "BOX-" + "ZQ9"
    home = sess.tmp / f"{user}-home"
    sess.env.update({"USERNAME": user, "COMPUTERNAME": machine, "USERPROFILE": str(home)})
    out = sess.tmp / user / "bars"
    text = f"bad request from {user} on {machine} in {home}"
    sess.rig.script(date(2024, 8, 13), error_behaviour(321, text))
    seen: dict[str, Any] = {}

    def spy_connection(host: str, port: int, client_id: int, **kw: Any) -> IbConnection:
        seen.update(kw, host=host, port=port, client_id=client_id)
        return sess.connection_factory(type("A", (), {"host": host, "port": port, "client_id": client_id}),
                                       kw["observers"])

    monkeypatch.setattr(cli, "IbConnection", spy_connection)
    code = cli.main(
        ["--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(out)], env=sess.env,
        stdin=Tty(True), input_fn=sess.input_fn, out=sess.out,
        market_data_factory=sess.market_data_factory, clock=sess.clock, sleep=sess.sleep,
        today=date(2024, 8, 21))
    assert code == 1
    assert seen["env"] is sess.env and seen["attempts"] == 2 and seen["connect_timeout"] == 30.0
    assert (seen["host"], seen["port"], seen["client_id"]) == ("127.0.0.1", 7497, 21)
    assert len(seen["observers"]) == 1 and isinstance(seen["observers"][0], SessionRecorder)
    recordings = sorted((home / "Stoinkulator" / "recordings").glob("*.jsonl"))
    assert [p.name for p in recordings] == ["fetch-bars-2024-08-21-001.jsonl"]
    raw = recordings[0].read_bytes().decode("utf-8")
    header = json.loads(raw.splitlines()[0])
    assert header["command"].startswith("fetch-bars")
    for word in (user, machine, str(home), str(out)):
        assert word.lower() not in raw.lower(), word
    assert "Recording check:" in sess.text


@pytest.mark.xfail(strict=True, reason="defect: the header command holds --start/--end dates, which the "
                                       "recording scan reports as 2 findings, so every real run ends with a "
                                       "false 'Recording check: 2 finding(s)'")
def test_a_clean_run_with_the_real_recorder_reports_no_findings(sess: Session,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    def connection(host: str, port: int, client_id: int, **kw: Any) -> IbConnection:
        return sess.connection_factory(type("A", (), {"host": host, "port": port, "client_id": client_id}),
                                       kw["observers"])

    monkeypatch.setattr(cli, "IbConnection", connection)
    code = cli.main(
        argv_for(sess), env=sess.env, stdin=Tty(True), input_fn=sess.input_fn, out=sess.out,
        market_data_factory=sess.market_data_factory, clock=sess.clock, sleep=sess.sleep,
        today=date(2024, 8, 21))
    assert code == 0
    assert "Recording check: 0 finding(s)" in sess.text


# -- sessions that end badly ----------------------------------------------------------------------------


def parseable(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert path.read_bytes().endswith(b"\n")
    return [json.loads(line) for line in lines]


def closed_and_complete(s: Session) -> None:
    assert s.connections and all(c.state is ConnState.CLOSED for c in s.connections)
    recorder = s.recorders[0]
    recorder.on_raw("probe", ())
    assert recorder.dropped == 1
    assert parseable(s.recording)[0]["format"] == "stoinkulator-ib-session"


def test_ctrl_c_during_a_pacing_wait_keeps_the_finished_days_and_a_complete_recording(sess: Session) -> None:
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        if seconds >= 5:
            waits.append(seconds)
            if len(waits) == 2:
                raise KeyboardInterrupt
        sess.clock.sleep(seconds)

    sess.sleep = sleep
    assert sess.fetch(symbols="AAPL") == 130
    assert "Interrupted (Ctrl-C)" in sess.text and "(exit 130)" in sess.text
    closed_and_complete(sess)
    from MainStoinker.engine.feed import read_symbol_bars

    assert len(read_symbol_bars(sess.tmp / "bars", "AAPL")) == 2 * 390


def test_ctrl_c_while_waiting_for_an_answer_exits_130_with_everything_closed(sess: Session) -> None:
    def interrupt(req_id: int, day: date, symbol: str) -> list[Any]:
        raise KeyboardInterrupt

    sess.rig.script(date(2024, 8, 13), interrupt)
    assert sess.fetch(symbols="AAPL") == 130
    closed_and_complete(sess)
    from MainStoinker.engine.feed import read_symbol_bars

    assert len(read_symbol_bars(sess.tmp / "bars", "AAPL")) == 390


def test_an_exception_inside_the_fetch_is_a_stop_not_a_traceback(sess: Session) -> None:
    secret = "DU" + "7654321"

    def boom(req_id: int, day: date, symbol: str) -> list[Any]:
        raise RuntimeError(f"boom for {secret}")

    sess.rig.script(date(2024, 8, 13), boom)
    assert sess.fetch(symbols="AAPL") == 4
    assert "Unexpected RuntimeError: boom for ACCOUNT" in sess.text
    assert "Traceback" not in sess.text and secret not in sess.text and "7654321" not in sess.text
    closed_and_complete(sess)


def test_a_client_id_already_in_use_is_exit_3_with_the_advice_and_no_files(sess: Session) -> None:
    sess.behavior = ConnectBehavior.CLIENT_ID_IN_USE
    assert sess.fetch() == 3
    assert "client id 21 is already in use" in sess.text and "use another id" in sess.text
    assert not (sess.tmp / "bars").exists()
    assert sess.connections[0].state is ConnState.CLOSED
    assert parseable(sess.recording)  # the attempt is recorded, and parses


def test_connection_lost_keeps_the_manifest_of_the_finished_days(sess: Session) -> None:
    def lose(req_id: int, day: date, symbol: str) -> list[Any]:
        sess.fakes[-1].drop("connectionClosed")
        return []

    sess.rig.script(date(2024, 8, 14), lose)
    assert sess.fetch(symbols="AAPL") == 4
    manifest = json.loads((sess.tmp / "bars" / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert "2024-08-12" in json.dumps(manifest) and "2024-08-13" in json.dumps(manifest)
    closed_and_complete(sess)
    # and the same command resumes: only the lost day is asked for again
    sess.rig.requests.clear()
    sess.rig.plans.clear()
    assert sess.fetch(symbols="AAPL") == 0
    assert [d for _c, d, _t in sess.rig.requests] == [date(2024, 8, 14)]


def test_account_user_and_home_never_reach_the_recording_and_the_account_never_reaches_stdout(
        sess: Session) -> None:
    user = "zq" + "uux"
    account = "DU" + "7654321"
    home = sess.tmp / f"{user}-home"
    sess.env.update({"USERNAME": user, "USERPROFILE": str(home)})
    sess.rig.script(date(2024, 8, 13), error_behaviour(321, f"bad {account} by {user} in {home}\\docs"))
    assert sess.fetch(symbols="AAPL") == 1
    raw = next((home / "Stoinkulator" / "recordings").glob("*.jsonl")).read_bytes().decode("utf-8").lower()
    for word in (account, "7654321", user, str(home)):
        assert word.lower() not in raw, word
    assert account not in sess.text and "7654321" not in sess.text
    assert "bad ACCOUNT by" in sess.text


# -- a recording of a clean run ---------------------------------------------------------------------------


class ReplaySession(Session):
    """The fake answers from a recording instead of from the scripted rig."""

    def __init__(self, tmp_path: Path, recording: Any) -> None:
        super().__init__(tmp_path)
        self.recorded = recording

    def client_factory(self, *, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger), connect_behavior=self.behavior,
                            **handshake_kwargs(self.recorded))
        install(fake, self.recorded)
        # the recording stores the server time zeroed (it is a clock reading); the test supplies it
        fake.respond("reqCurrentTime", [("currentTime", int(SERVER_NOW.timestamp()))])
        self.fakes.append(fake)
        return fake


def test_a_clean_run_is_recorded_scans_clean_and_replays_to_the_same_month_files(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first = Session(first_dir)
    user = "zq" + "uux"
    first.env["USERNAME"] = user
    assert first.fetch(symbols="AAPL,MSFT") == 0
    try:
        path = first.recording
        assert scan_recording(path, [user, str(first_dir), "Paul"]) == []
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert user not in json.dumps(header) and str(first_dir) not in json.dumps(header)
        recording = load_recording(path)
        assert {r.method for r in recording.requests} >= {"reqContractDetails", "reqHistoricalData"}
        second = ReplaySession(second_dir, recording)
        try:
            assert second.fetch(symbols="AAPL,MSFT") == 0
            for symbol in ("AAPL", "MSFT"):
                name = f"{symbol}_2024-08.csv"
                assert ((second_dir / "bars" / symbol / name).read_bytes()
                        == (first_dir / "bars" / symbol / name).read_bytes())
        finally:
            for conn in second.connections:
                conn.close()
            for recorder in second.recorders:
                recorder.close()
            for fake in second.fakes:
                fake.close()
    finally:
        for conn in first.connections:
            conn.close()
        for recorder in first.recorders:
            recorder.close()
        for fake in first.fakes:
            fake.close()


# -- no state is left behind --------------------------------------------------------------------------------


def test_runs_in_one_process_leave_no_thread_and_do_not_disturb_each_other(tmp_path: Path) -> None:
    before = {t.ident for t in threading.enumerate()}
    results = []
    behaviours = [ConnectBehavior.ACCEPT, ConnectBehavior.REFUSE, ConnectBehavior.ACCEPT]
    for number, behaviour in enumerate(behaviours):
        s = Session(tmp_path / str(number), behaviour)
        try:
            results.append((s.fetch(symbols="AAPL"), s.text.rstrip().splitlines()[-1].split("(")[0]))
        finally:
            for conn in s.connections:
                conn.close()
            for recorder in s.recorders:
                recorder.close()
            for fake in s.fakes:
                fake.close()
    assert [code for code, _ in results] == [0, 3, 0]
    assert results[0][1] == results[2][1]
    leaked = [t.name for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert not leaked, leaked


# -- dispatch -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["runs", "report", "backtest"])
def test_the_offline_commands_load_neither_ibapi_nor_the_ib_package(command: str) -> None:
    code = ("import sys, runpy\nsys.argv = ['MainStoinker', %r, '--help']\n"
            "try:\n    runpy.run_module('MainStoinker', run_name='__main__')\n"
            "except SystemExit:\n    pass\n") % command + LOADED
    done = child(code)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout


def test_an_unknown_subcommand_still_gets_the_old_error_and_loads_no_ib_code() -> None:
    code = ("import sys, runpy\nsys.argv = ['MainStoinker', 'frobnicate']\n"
            "try:\n    runpy.run_module('MainStoinker', run_name='__main__')\n"
            "except SystemExit as exc:\n    print('EXIT', exc.code)\n"
            + LOADED)
    done = child(code)
    assert "EXIT 2" in done.stdout and done.stdout.strip().splitlines()[-1] == "[]"
    assert "frobnicate" in done.stderr and "invalid choice" in done.stderr


def test_the_repository_path_the_command_refuses_is_this_repository() -> None:
    assert cli.REPO_ROOT == REPO
