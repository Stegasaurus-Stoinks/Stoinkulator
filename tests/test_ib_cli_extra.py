"""Gaps in `tests/test_ib_cli.py`, found by mutating `MainStoinker/ib/cli.py` and probing the command.

Same rules as there: every client is `FakeIbClient`, time is injected, the environment is a mapping
passed in, every file is under `tmp_path`, and nothing opens a socket. Nothing here runs the
`fetch-bars` command in a child process.
"""

from __future__ import annotations

import json
import shutil
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
from tests.test_ib_cli import real_home_untouched as real_home_untouched  # noqa: F401  (autouse here too)
from tests.test_ib_fetch import SERVER_NOW, SimClock, details, error_behaviour

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


def test_a_recording_under_a_file_exits_2_before_the_gate_with_nothing_connected(sess: Session) -> None:
    blocker = sess.tmp / "blocker"
    blocker.write_text("a file where a folder is needed", encoding="utf-8")
    code = sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(blocker / "r.jsonl")))
    assert code == 2
    assert "is a file" in sess.text and "recording folder" in sess.text
    assert not sess.prompts and not sess.connections and not sess.fakes
    assert not (sess.tmp / "bars").exists()


def test_a_recording_folder_that_cannot_be_made_is_refused_before_the_gate_and_removes_what_was_made(
        sess: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check is by file type; this is the other kind: the folder cannot be created at all."""
    real_mkdir = Path.mkdir
    target = sess.tmp / "rec" / "deep"

    def mkdir(self: Path, *a: Any, **kw: Any) -> None:
        if self == target:
            raise PermissionError("denied")
        real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    code = sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(target / "r.jsonl")))
    assert code == 2 and not sess.prompts and not sess.connections
    assert "cannot create the folder" in sess.text
    assert not (sess.tmp / "bars").exists() and not (sess.tmp / "rec").exists()


def test_folders_made_before_a_refused_gate_are_removed_when_empty(sess: Session) -> None:
    sess.typed = "no"
    record = sess.tmp / "r1" / "r2" / "x.jsonl"
    assert sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(record))) == 2
    assert not (sess.tmp / "bars").exists() and not (sess.tmp / "r1").exists()


def test_a_folder_that_held_something_before_is_never_removed(sess: Session) -> None:
    (sess.tmp / "bars").mkdir()
    (sess.tmp / "bars" / "keep.txt").write_text("x", encoding="utf-8")
    sess.typed = "no"
    assert sess.main(*argv_for(sess)) == 2
    assert (sess.tmp / "bars" / "keep.txt").is_file()
    (sess.tmp / "bars" / "keep.txt").unlink()
    assert sess.main(*argv_for(sess)) == 2
    assert (sess.tmp / "bars").is_dir()  # it was there before: it stays
    assert not (sess.tmp / "home" / "Stoinkulator").exists()  # made by this run, empty: removed


def test_a_recording_parent_that_is_a_file_is_refused_before_the_port_guard(sess: Session) -> None:
    blocker = sess.tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    code = sess.main(*argv_for(sess, "7496", "AAPL", "--record", str(blocker / "r.jsonl")))
    assert code == 2 and "is a file" in sess.text
    assert "STOINK_ALLOW_LIVE_PORT" not in sess.text  # refused on the path, before the port guard
    untouched(sess)


def test_a_recording_folder_under_a_file_says_so(sess: Session) -> None:
    blocker = sess.tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    code = sess.main(*argv_for(sess, "7497", "AAPL", "--record", str(blocker / "sub" / "r.jsonl")))
    assert code == 2 and "lies under the file" in sess.text
    untouched(sess)


def test_relative_paths_are_taken_from_the_current_folder(sess: Session) -> None:
    code = sess.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--out", "relative_bars", "--record",
                     "relative_rec.jsonl")
    assert code == 0
    assert (Path.cwd() / "relative_bars" / "AAPL").is_dir() and (Path.cwd() / "relative_rec.jsonl").is_file()


@pytest.mark.parametrize("shape", ["file", "under_file"])
@pytest.mark.parametrize("port", ["7497", "7496"])
def test_an_output_path_that_is_a_file_is_refused_before_the_port_guard_and_the_gate(
        sess: Session, shape: str, port: str) -> None:
    blocker = sess.tmp / "afile"
    blocker.write_text("x", encoding="utf-8")
    out = blocker if shape == "file" else blocker / "sub"
    code = sess.main("--symbols", "AAPL", *DAYS, "--port", port, "--out", str(out))
    assert code == 2 and not sess.prompts and not sess.connections and not sess.fakes
    assert "STOINK_ALLOW_LIVE_PORT" not in sess.text and "bars folder" in sess.text
    assert blocker.read_text(encoding="utf-8") == "x"


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
                       "--out", "--record", "--no-record", "--connect-timeout", "--settle-hours"}


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


def test_a_range_wholly_in_the_future_is_refused_before_connecting_as_a_pre_check(sess: Session) -> None:
    code = sess.main("--symbols", "AAPL", "--start", "2030-01-07", "--end", "2030-01-11", "--port", "7497",
                     "--out", str(sess.tmp / "bars"))
    assert code == 2
    assert "pre-check" in sess.text and "after today in New York (2024-08-21)" in sess.text
    assert not sess.prompts and not sess.connections and not sess.fakes
    assert not (sess.tmp / "bars").exists()


def test_a_range_starting_today_in_new_york_is_not_refused_by_the_pre_check(sess: Session) -> None:
    """Only a start after today is refused from the clock; the authoritative check is IB's server time."""
    code = sess.main("--symbols", "AAPL", "--start", "2024-08-21", "--end", "2024-08-21", "--port", "7497",
                     "--out", str(sess.tmp / "bars"))
    assert "pre-check" not in sess.text and sess.connections
    assert code in (0, 1, 4)


def test_a_range_that_only_ends_in_the_future_is_not_refused_by_the_pre_check(sess: Session) -> None:
    sess.main("--symbols", "AAPL", "--start", "2024-08-12", "--end", "2030-01-11", "--port", "7497",
              "--out", str(sess.tmp / "bars"))
    assert "pre-check" not in sess.text and sess.connections


def test_a_default_recording_folder_under_a_file_is_refused_before_the_gate(sess: Session) -> None:
    home = Path(sess.env["USERPROFILE"])
    home.mkdir(parents=True)
    (home / "Stoinkulator").write_text("a file", encoding="utf-8")
    assert sess.main(*argv_for(sess)) == 2
    assert "recording folder" in sess.text and "lies under the file" in sess.text
    assert not sess.prompts and not sess.connections
    assert not (sess.tmp / "bars").exists()


def test_a_connect_that_raises_something_unexpected_advises_checking_the_port(sess: Session) -> None:
    class Broken:
        def open(self) -> None:
            raise RuntimeError("boom")

        def close(self) -> None:
            pass

    code = cli.main(argv_for(sess), env=sess.env, stdin=Tty(True), input_fn=sess.input_fn, out=sess.out,
                    connection_factory=lambda a, o: Broken(),  # type: ignore[arg-type,return-value]
                    market_data_factory=sess.market_data_factory, recorder_factory=sess.recorder_factory,
                    clock=sess.clock, sleep=sess.sleep, today=date(2024, 8, 21))
    assert code == cli.EXIT_NO_CONNECTION
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_CONNECT}"]


def test_a_weekend_only_range_is_refused_before_connecting_and_says_so(sess: Session) -> None:
    code = sess.main("--symbols", "AAPL", "--start", "2024-08-17", "--end", "2024-08-18", "--port", "7497",
                     "--out", str(sess.tmp / "bars"))
    assert code == 2 and "no weekday" in sess.text and "pre-check" in sess.text
    untouched(sess)


def test_a_range_that_starts_in_the_future_by_server_time_is_still_stopped_after_connecting(
        sess: Session) -> None:
    """The authoritative check stays: the clock of the machine says a day has started, IB's says not."""
    code = sess.main("--symbols", "AAPL", "--start", "2024-08-20", "--end", "2024-08-20", "--port", "7497",
                     "--out", str(sess.tmp / "bars"))
    assert code == 4  # IB's server time (SERVER_NOW: the morning of 2024-08-20) says it is not closed
    assert "not closed" in sess.text and "pre-check" not in sess.text
    assert not list((sess.tmp / "bars").rglob("*.csv"))
    assert sess.connections[0].state is ConnState.CLOSED


# -- what is printed ---------------------------------------------------------------------------------


def test_the_volume_note_states_the_settled_fact() -> None:
    note = cli.VOLUME_NOTE.lower()
    assert len(note.splitlines()) == 3
    assert "not settled" not in note
    assert "as ib sent" in note
    assert "unticked" in note and "global configuration, api, settings" in note
    assert "cannot be converted back" in note and "fetch that data again" in note
    assert "hundreds of thousands to millions" in note and "tens of thousands" in note


def test_a_failure_without_a_code_does_not_print_none(sess: Session) -> None:
    def lose(req_id: int, day: date, symbol: str) -> list[Any]:
        sess.fakes[-1].drop("connectionClosed")
        return []

    sess.rig.script(date(2024, 8, 14), lose)
    sess.fetch(symbols="AAPL")
    assert "None" not in sess.text
    assert "2024-08-14: failed: " in sess.text and "failed, IB" not in sess.text.split("Summary")[0]


def test_failure_text_with_and_without_a_code() -> None:
    assert cli.failure_text(None, "the connection closed") == "failed: the connection closed"
    assert cli.failure_text(162, "pacing") == "failed, IB 162: pacing"
    assert cli.failure_text(0, "zero is a code") == "failed, IB 0: zero is a code"
    assert cli.failure_text(None, "x", "no_data_yet") == f"failed: {cli.NO_DATA_YET_TEXT}"
    damaged = cli.failure_text(None, "month file AAPL_2024-08.csv is damaged: bad", "damaged_file")
    assert "left untouched" in damaged and "AAPL_2024-08.csv" in damaged


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


def real_recorder_main(sess: Session, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """`main` with the REAL recorder (no recorder factory) and the fake connection."""
    def connection(host: str, port: int, client_id: int, **kw: Any) -> IbConnection:
        return sess.connection_factory(type("A", (), {"host": host, "port": port, "client_id": client_id}),
                                       kw["observers"])

    monkeypatch.setattr(cli, "IbConnection", connection)
    return cli.main(
        list(argv) or argv_for(sess), env=sess.env, stdin=Tty(True), input_fn=sess.input_fn, out=sess.out,
        market_data_factory=sess.market_data_factory, clock=sess.clock, sleep=sess.sleep,
        today=date(2024, 8, 21))


def test_a_clean_run_with_the_real_recorder_reports_no_findings(sess: Session,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    assert real_recorder_main(sess, monkeypatch) == 0
    assert "Recording check: 0 finding(s)" in sess.text
    assert "Where to look" not in sess.text


def test_the_recording_header_holds_the_subcommand_and_the_symbols_only(
        sess: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["--symbols", "AAPL,MSFT", *DAYS, "--port", "7497", "--host", "127.0.0.1", "--client-id", "33",
            "--out", str(sess.tmp / "bars")]
    assert real_recorder_main(sess, monkeypatch, *argv) == 0
    header = json.loads(next((sess.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
                        .read_text(encoding="utf-8").splitlines()[0])
    assert header["command"] == "fetch-bars AAPL MSFT"


def test_findings_are_listed_by_line_and_reason_never_by_the_matched_text(sess: Session) -> None:
    user = "zq" + "uux"
    sess.env["USERNAME"] = user

    def bare_recorder(args: Any) -> SessionRecorder:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        recorder = SessionRecorder(args.record, clock=sess.clock, command=["fetch-bars"], port=args.port)
        sess.recorders.append(recorder)
        return recorder

    for day in (12, 13, 14, 15, 16):
        sess.rig.script(date(2024, 8, day), error_behaviour(321, f"bad request from {user}"))
    code = cli.main(
        ["--symbols", "AAPL,MSFT", "--start", "2024-08-12", "--end", "2024-08-16", "--port", "7497",
         "--out", str(sess.tmp / "bars")], env=sess.env, stdin=Tty(True), input_fn=sess.input_fn,
        out=sess.out, connection_factory=sess.connection_factory,
        market_data_factory=sess.market_data_factory, recorder_factory=bare_recorder,
        clock=sess.clock, sleep=sess.sleep, today=date(2024, 8, 21))
    assert code == 1
    shown = [line for line in sess.text.splitlines() if line.startswith("  line ")]
    found = scan_recording(sess.recording, [user])
    assert len(found) > cli.SHOWN_FINDINGS  # more than are shown
    assert shown == [f"  line {f.line}: {f.reason}" for f in found[:cli.SHOWN_FINDINGS]]  # nothing else
    assert f"  ... and {len(found) - cli.SHOWN_FINDINGS} more" in sess.text
    assert "Where to look" in sess.text


# -- fix round: the new driver states, advice, the symbol cap, the settle option ---------------------------


def test_the_help_states_the_estimate_rule_the_cap_and_the_settle_option(
        capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "about 10 seconds per symbol-day" in shown
    assert "At most 20 symbols" in shown and "--settle-hours" in shown


def test_at_most_twenty_symbols(capsys: pytest.CaptureFixture[str]) -> None:
    twenty = ",".join(f"S{n:02d}" for n in range(20))
    assert len(cli.parse_args(["--symbols", twenty, *DAYS, "--port", "7497"]).symbols) == 20
    with pytest.raises(SystemExit) as info:
        cli.parse_args(["--symbols", twenty + ",EXTRA", *DAYS, "--port", "7497"])
    assert info.value.code == 2
    assert "at most 20" in capsys.readouterr().err
    # duplicates do not count twice
    again = cli.parse_args(["--symbols", twenty + ",s00,S01", *DAYS, "--port", "7497"])
    assert len(again.symbols) == 20


def test_too_many_symbols_never_reach_the_gate(sess: Session, capsys: pytest.CaptureFixture[str]) -> None:
    many = ",".join(f"S{n:02d}" for n in range(21))
    with pytest.raises(SystemExit) as info:
        sess.main("--symbols", many, *DAYS, "--port", "7497")
    assert info.value.code == 2 and not sess.prompts
    untouched(sess)
    capsys.readouterr()


@pytest.mark.parametrize("value, ok", [("0", True), ("3", True), ("24", True), ("0.5", True), ("-0.1", False),
                                       ("24.01", False), ("nan", False), ("inf", False), ("x", False)])
def test_settle_hours_runs_from_0_to_24(value: str, ok: bool, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--symbols", "AAPL", *DAYS, "--port", "7497", "--settle-hours", value]
    if ok:
        assert cli.parse_args(argv).settle_hours == float(value)
    else:
        with pytest.raises(SystemExit) as info:
            cli.parse_args(argv)
        assert info.value.code == 2
    capsys.readouterr()
    assert cli.parse_args(["--symbols", "AAPL", *DAYS, "--port", "7497"]).settle_hours == 3.0


def test_settle_hours_reaches_the_driver_as_seconds(sess: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real = cli.fetch

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append(kw["settle_seconds"])
        return real(*a, **kw)

    monkeypatch.setattr(cli, "fetch", spy)
    assert sess.fetch("--settle-hours", "1.5", symbols="AAPL") == 0
    assert sess.fetch(symbols="AAPL") == 0
    assert seen == [5400.0, 10800.0]


def test_a_day_without_data_is_retried_when_it_closed_less_than_settle_hours_ago(sess: Session) -> None:
    """2024-08-19 closed 16 hours before the fake's server time: a holiday at 3 h, 'not yet' at 24 h."""
    from tests.test_ib_fetch import empty_end

    sess.rig.script(date(2024, 8, 19), empty_end)
    argv = ["--symbols", "AAPL", "--start", "2024-08-19", "--end", "2024-08-19", "--port", "7497", "--out",
            str(sess.tmp / "bars")]
    assert sess.main(*argv) == 0 and "1 no data" in sess.text
    sess.out.truncate(0)
    sess.out.seek(0)
    shutil.rmtree(sess.tmp / "bars")
    assert sess.main(*argv, "--settle-hours", "24") == 1
    assert "failed: IB had no data for this day yet; it will be retried next run" in sess.text
    assert "None" not in sess.text and "no data yet (session closed" not in sess.text


def test_short_and_partial_days_and_the_run_total_are_listed(sess: Session) -> None:
    from tests.test_ib_fetch import bars_played, day_rows, end_played

    def short(req_id: int, day: date, symbol: str) -> list[Any]:
        rows = day_rows(day, minutes=210, symbol=symbol)
        return [*bars_played(req_id, rows), end_played(req_id)]

    def partial(req_id: int, day: date, symbol: str) -> list[Any]:
        return [*bars_played(req_id, day_rows(day, minutes=1, symbol=symbol)), end_played(req_id)]

    sess.rig.script(date(2024, 8, 13), short)
    sess.rig.script(date(2024, 8, 14), partial)
    assert sess.fetch(symbols="AAPL") == 0  # missing minutes are data, not a failure
    assert "Days with missing minutes (short or partial, bars kept): 2" in sess.text
    assert "AAPL: 2024-08-13 210 bars (short), 2024-08-14 1 bars (partial)" in sess.text


def test_a_clean_run_says_zero_days_with_missing_minutes(sess: Session) -> None:
    assert sess.fetch() == 0
    assert "Days with missing minutes (short or partial, bars kept): 0" in sess.text


def test_a_damaged_month_file_names_the_file_says_untouched_gives_exit_1_and_the_other_symbol_goes_on(
        sess: Session) -> None:
    from MainStoinker.engine.feed import month_file_path

    path = month_file_path(sess.tmp / "bars", "AAPL", 2024, 8)
    path.parent.mkdir(parents=True)
    bad = ("date,time,open,high,low,close,volume\n2024-08-13 09:31:00,1,1,2,0.5,1,1\n"
           "2024-08-13 09:30:00,1,1,2,0.5,1,1\n")
    path.write_text(bad, encoding="utf-8")
    assert sess.fetch(symbols="AAPL,MSFT") == cli.EXIT_FAILED
    text = sess.text
    assert "AAPL_2024-08.csv" in text and "left untouched" in text
    assert "[stopped]" in text and "MSFT: 3 ok" in text
    assert path.read_text(encoding="utf-8") == bad
    assert "(exit 1)" in text and "What to do:" not in text


@pytest.mark.parametrize("state", ["stopped", "manifest", "resolve_failed"])
def test_a_symbol_in_a_bad_state_is_exit_1_unless_the_run_stopped(state: str) -> None:
    from MainStoinker.ib.fetch import FetchResult, SymbolResult

    result = FetchResult(symbols={"AAPL": SymbolResult("AAPL", state="done"),
                                  "MSFT": SymbolResult("MSFT", state=state)})
    assert cli.exit_code_for(result) == cli.EXIT_FAILED
    result.stop_reason = "connection lost"
    assert cli.exit_code_for(result) == cli.EXIT_STOPPED
    result.stop_reason = None
    result.fatal = "disk"
    assert cli.exit_code_for(result) == cli.EXIT_STOPPED
    ok = FetchResult(symbols={"AAPL": SymbolResult("AAPL", state="done")})
    assert cli.exit_code_for(ok) == cli.EXIT_OK


def test_a_resolve_failure_is_exit_1_and_the_other_symbol_goes_on(sess: Session) -> None:
    original = sess.client_factory

    def refuse_msft(**kw: Any) -> FakeIbClient:
        fake = original(**kw)
        fake.respond("reqContractDetails", lambda req_id, contract: (
            [("error", req_id, 1_700_000_000_000, 200, "No security definition has been found", "")]
            if contract.symbol == "MSFT" else
            [("contractDetails", req_id, details(contract.symbol)), ("contractDetailsEnd", req_id)]))
        return fake

    sess.client_factory = refuse_msft  # type: ignore[method-assign]
    assert sess.fetch(symbols="MSFT,AAPL") == cli.EXIT_FAILED
    assert "[resolve_failed]" in sess.text and "AAPL: 3 ok" in sess.text


def lose_connection(sess: Session, day: int = 14) -> None:
    def lose(req_id: int, d: date, symbol: str) -> list[Any]:
        sess.fakes[-1].drop("connectionClosed")
        return []

    sess.rig.script(date(2024, 8, day), lose)


def advice_lines(sess: Session) -> list[str]:
    return [line for line in sess.text.splitlines() if line.startswith("What to do:")]


def test_connection_lost_gives_one_advice_line(sess: Session) -> None:
    lose_connection(sess)
    assert sess.fetch(symbols="AAPL,MSFT") == cli.EXIT_STOPPED
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_LOST}"]
    assert "TWS is running and logged in" in sess.text and "run the same command again to resume" in sess.text


def test_a_second_pacing_violation_advises_waiting_ten_minutes(sess: Session) -> None:
    text = "Historical Market Data Service error message:Historical data request pacing violation"
    sess.rig.script(date(2024, 8, 13), error_behaviour(162, text))
    assert sess.fetch(symbols="AAPL") == cli.EXIT_STOPPED
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_PACING}"]
    assert "at least 10 minutes" in sess.text


def test_a_client_id_in_use_advises_closing_the_other_client(sess: Session) -> None:
    sess.behavior = ConnectBehavior.CLIENT_ID_IN_USE
    assert sess.fetch() == cli.EXIT_NO_CONNECTION
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_CLIENT_ID}"]
    assert "--client-id" in sess.text


def test_a_refused_connection_advises_checking_the_port_and_the_api(sess: Session) -> None:
    sess.behavior = ConnectBehavior.REFUSE
    assert sess.fetch() == cli.EXIT_NO_CONNECTION
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_CONNECT}"]
    assert "check the port and that the API is enabled in TWS" in sess.text


def test_no_historical_farm_advises_waiting_a_minute(sess: Session) -> None:
    sess.farm = False
    sess.clock.tick = SimClock.FAST
    assert sess.fetch(symbols="AAPL") == cli.EXIT_STOPPED
    assert advice_lines(sess) == [f"What to do: {cli.ADVICE_FARM}"]
    assert "history service" in sess.text and not sess.rig.requests


def test_advice_lines_are_distinct_when_causes_repeat(sess: Session) -> None:
    from MainStoinker.ib.fetch import FetchResult

    result = FetchResult(connection_lost=True, pacing_stopped=True, stop_reason="no data farm")
    assert cli.advice_for(result) == [cli.ADVICE_LOST, cli.ADVICE_PACING, cli.ADVICE_FARM]
    assert cli.advice_for(FetchResult()) == []


def test_a_clean_run_has_no_advice_and_ends_with_the_paths_note(sess: Session) -> None:
    assert sess.fetch() == 0
    assert advice_lines(sess) == []
    assert sess.text.rstrip().splitlines()[-1] == (
        "Note: this output shows local paths. Read it before sharing it.")


@pytest.mark.parametrize("code", [cli.EXIT_FAILED, cli.EXIT_STOPPED, cli.EXIT_NO_CONNECTION])
def test_the_paths_note_is_the_last_line_after_a_failure_or_stop_too(sess: Session, code: int) -> None:
    if code == cli.EXIT_FAILED:
        sess.rig.script(date(2024, 8, 13), error_behaviour(200, "nope"))
    elif code == cli.EXIT_STOPPED:
        lose_connection(sess)
    else:
        sess.behavior = ConnectBehavior.REFUSE
    assert sess.fetch(symbols="AAPL") == code
    assert sess.text.rstrip().splitlines()[-1] == cli.PATHS_NOTE
    assert sess.text.rstrip().splitlines()[-2].startswith("Result:")


def test_the_gate_text_is_one_fact_per_short_line(sess: Session) -> None:
    sess.typed = "no"
    sess.main(*argv_for(sess, "7497", "AAPL,MSFT"))
    lines = [line.strip() for line in sess.text.splitlines()]
    for start in ("Connects to:", "Client id:", "Symbols:", "Dates:", "Output folder:", "Recording file:",
                  "Estimate:"):
        assert sum(line.startswith(start) for line in lines) == 1, start
    assert "Connects to:      127.0.0.1:7497" in sess.text
    assert "It sends data requests only" in sess.text
    assert max(len(line) for line in sess.text.splitlines() if "folder" not in line.lower()
               and "file" not in line.lower()) < 100


@pytest.mark.parametrize("seconds, text", [(0, "0 s"), (50, "50 s"), (59.9, "59 s"), (60, "1 min 00 s"),
                                           (425, "7 min 05 s"), (3599, "59 min 59 s"), (3600, "1 h 00 min"),
                                           (5400, "1 h 30 min")])
def test_durations_under_a_minute_are_shown_in_seconds(seconds: float, text: str) -> None:
    assert cli.format_duration(seconds) == text


def test_the_gate_and_the_progress_estimate_never_show_a_zero_hour_duration(sess: Session) -> None:
    assert sess.fetch() == 0
    assert "0 h" not in sess.text and "at least 50 s" in sess.text  # 6 requests: five 10 s gaps


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
            results.append((s.fetch(symbols="AAPL"), s.text.rstrip().splitlines()[-2].split("(")[0]))
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
