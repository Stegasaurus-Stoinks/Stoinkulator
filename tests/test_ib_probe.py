"""`probe-data` (`MainStoinker/ib/probe.py`, wired in `MainStoinker/ib/cli.py`), driven in-process.

Every client is `FakeIbClient` behind a real `IbConnection` and `IbMarketData`; nothing opens a
socket, nothing really sleeps, the terminal and the environment are fakes passed in, and every file
is under `tmp_path`. The only child processes check that the offline commands never import
`MainStoinker.ib`.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.common import TickAttrib
from ibapi.contract import Contract, ContractDetails

from MainStoinker.ib import cli, probe
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import DATA_REQUESTS, ConnState, IbConnection
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.probe import closed_session_day
from MainStoinker.ib.recorder import (
    LONG_RUN_REASON,
    SEPARATED_RUN_REASON,
    Finding,
    SessionRecorder,
    scan_recording,
)
from tests.fake_ib import FARM_MESSAGES, ConnectBehavior, FakeIbClient, make_bar
from tests.home_guard import no_writes_to_real_home

SERVER = datetime(2024, 11, 22, 22, 0, tzinfo=UTC)  # Friday 17:00 New York, session closed
OPEN_EPOCH = int(datetime(2024, 11, 22, 14, 30, tzinfo=UTC).timestamp())  # 09:30 New York
ACCOUNT_TEXT = "Account DU1234567 is not subscribed"
REPO = Path(__file__).resolve().parent.parent
PROBE_SOURCE = REPO / "MainStoinker" / "ib" / "probe.py"


# -- the real home folder is never touched (the same guard as tests/test_ib_cli.py) ---------------


def _real_home() -> Path | None:
    """The real profile folder, read once when this module is imported (before any test runs)."""
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value)
    return None


REAL_HOME = _real_home()


@pytest.fixture(autouse=True)
def real_home_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The process environment is pointed at `tmp_path` for the test, and any write inside the real
    `Stoinkulator` folder by this process fails the test (tests/home_guard.py)."""
    fake_home = tmp_path / "process-home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    with no_writes_to_real_home(REAL_HOME):
        yield


# -- the rig -------------------------------------------------------------------------------------


class TickClock:
    """Real time sped up by `scale`, plus the time "slept": 8 s of waiting costs ~0.16 s."""

    def __init__(self, scale: float = 50.0) -> None:
        self.scale = scale
        self.start = time.monotonic()
        self.slept = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return (time.monotonic() - self.start) * self.scale + self.slept

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept += seconds


class Tty(io.StringIO):
    def __init__(self, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def error_played(req_id: int, code: int, text: str) -> tuple[Any, ...]:
    return ("error", req_id, 1_700_000_000_000, code, text, "")


def details(symbol: str = "AAPL") -> ContractDetails:
    d = ContractDetails()
    d.contract = Contract()
    d.contract.conId = 265598
    d.contract.symbol = symbol
    d.contract.secType = "STK"
    d.contract.exchange = "SMART"
    d.contract.primaryExchange = "NASDAQ"
    d.contract.currency = "USD"
    d.minTick = 0.01
    d.timeZoneId = "US/Eastern"
    return d


@dataclass
class Script:
    live_ticks: bool = True
    delayed_ticks: bool = True
    quote_errors: dict[int, tuple[int, str]] = field(default_factory=dict)  # type -> (code, text)
    history_error: tuple[int, str] | None = None
    bars: int = 390
    first_volume: int = 1234  # the first bar's volume; the others are first_volume + i
    tick_strings: bool = False  # the last-trade-time stamp that the recording scan flags
    set_types: list[int] = field(default_factory=list)
    connect: ConnectBehavior = ConnectBehavior.ACCEPT

    def apply(self, fake: FakeIbClient) -> None:
        fake.respond("reqCurrentTime", [("currentTime", int(SERVER.timestamp()))])
        fake.respond("reqContractDetails", lambda rid, c: [
            ("contractDetails", rid, details(c.symbol)), ("contractDetailsEnd", rid),
        ])
        fake.respond("reqMarketDataType", lambda t: self.set_types.append(t))

        def quotes(rid: int, *rest: Any) -> list[tuple[Any, ...]]:
            mdt = self.set_types[-1]
            played: list[tuple[Any, ...]] = []
            if mdt in self.quote_errors:
                played.append(error_played(rid, *self.quote_errors[mdt]))
            if (mdt == 1 and self.live_ticks) or (mdt == 3 and self.delayed_ticks):
                played.append(("marketDataType", rid, mdt))
                played.append(("tickPrice", rid, 4 if mdt == 1 else 68, 190.5, TickAttrib()))
                played.append(("tickSize", rid, 5, Decimal(300)))
                if self.tick_strings:
                    played.append(("tickString", rid, 45, "1732290000"))
                    played.append(("tickString", rid, 45, "1732290001"))
            return played

        fake.respond("reqMktData", quotes)

        def history(rid: int, *rest: Any) -> list[tuple[Any, ...]]:
            if self.history_error is not None:
                return [error_played(rid, *self.history_error)]
            played: list[tuple[Any, ...]] = [
                ("historicalData", rid,
                 make_bar(OPEN_EPOCH + 60 * i, 10, 11, 9, 10.5, self.first_volume + i))
                for i in range(self.bars)
            ]
            played.append(("historicalDataEnd", rid, "s", "e"))
            return played

        fake.respond("reqHistoricalData", history)


@dataclass
class Rig:
    tmp: Path
    script: Script = field(default_factory=Script)
    clock: TickClock = field(default_factory=TickClock)
    fakes: list[FakeIbClient] = field(default_factory=list)
    conns: list[IbConnection] = field(default_factory=list)
    recorders: list[SessionRecorder] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    open_error: Exception | None = None

    @property
    def home(self) -> Path:
        return self.tmp / "home"

    def connection_factory(self, args: Any, observers: list[Any]) -> IbConnection:
        def client(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
            fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger),
                                connect_behavior=self.script.connect, farm_messages=FARM_MESSAGES)
            self.script.apply(fake)
            self.fakes.append(fake)
            return fake

        conn = IbConnection(
            args.host, args.port, args.client_id, env=self.env, client_factory=client,
            clock=self.clock, sleep=self.clock.sleep, poll_interval=0.002, attempts=2, retry_delay=3,
            observers=observers,
        )
        if self.open_error is not None:
            error = self.open_error

            def refuse() -> None:
                raise error

            conn.open = refuse  # type: ignore[method-assign]
        self.conns.append(conn)
        return conn

    def market_data_factory(self, conn: IbConnection) -> IbMarketData:
        return IbMarketData(conn, clock=self.clock, poll_interval=0.01)

    def recorder_factory(self, args: Any) -> SessionRecorder | None:
        if args.record is None:
            return None
        args.record.parent.mkdir(parents=True, exist_ok=True)
        recorder = SessionRecorder(args.record, clock=self.clock, command=["probe-data", *args.symbols],
                                   port=args.port, private_values=args.private)
        self.recorders.append(recorder)
        return recorder

    def main(
        self, *argv: str, port: str = "7497", tty: bool = True, typed: str | None = None,
        sleep: Callable[[float], None] | None = None, factories: bool = True,
    ) -> tuple[int, str]:
        out = io.StringIO()
        self.env.setdefault("USERPROFILE", str(self.home))
        self.env.setdefault("HOME", str(self.home))

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            return port if typed is None else typed

        extra: dict[str, Any] = {}
        if factories:
            extra = {"connection_factory": self.connection_factory,
                     "market_data_factory": self.market_data_factory,
                     "recorder_factory": self.recorder_factory}
        code = cli.probe_data_main(
            ["--port", port, *argv], env=self.env, stdin=Tty(tty), input_fn=ask, out=out,
            clock=self.clock, sleep=sleep or self.clock.sleep, today=date(2024, 11, 22),
            wall=lambda: SERVER - timedelta(seconds=2), **extra)
        return code, out.getvalue()

    def run(self, *extra: str, **kw: Any) -> tuple[int, str]:
        return self.main("--quote-seconds", "10", *extra, **kw)

    @property
    def recording(self) -> Path:
        found = sorted((self.home / "Stoinkulator" / "recordings").glob("*.jsonl"))
        assert len(found) == 1, found
        return found[0]

    def nothing_built(self) -> None:
        assert not self.conns and not self.fakes and not self.recorders


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    for conn in r.conns:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


def boom(*_: Any, **__: Any) -> Any:
    raise AssertionError("a client or connection was built")


# -- the gate, the guard, the arguments ------------------------------------------------------------


def test_gate_refuses_without_a_terminal(rig: Rig) -> None:
    code, text = rig.run(tty=False)
    assert code == 2 and "only the owner runs it" in text
    assert not rig.prompts
    rig.nothing_built()


@pytest.mark.parametrize("typed", ["", "7496", "yes", "7497x", "port"])
def test_gate_refuses_when_the_typed_text_is_not_the_port(rig: Rig, typed: str) -> None:
    code, text = rig.run(typed=typed)
    assert code == 2
    assert len(rig.prompts) == 1 and "(7497)" in rig.prompts[0]
    assert "data requests only" in text and "AAPL" in text and "Nothing was started" in text
    rig.nothing_built()
    assert not (rig.home / "Stoinkulator").exists()  # the recording folder this run made is gone again


def test_eof_at_the_prompt_is_a_refusal_that_builds_nothing(rig: Rig) -> None:
    out = io.StringIO()

    def eof(prompt: str) -> str:
        raise EOFError

    code = cli.probe_data_main(["--port", "7497"], env={"USERPROFILE": str(rig.home)}, stdin=Tty(),
                               input_fn=eof, out=out, connection_factory=boom)
    assert code == 2
    assert not (rig.home / "Stoinkulator").exists()


def test_live_port_without_opt_in_exits_2_with_the_powershell_line_and_builds_nothing(rig: Rig) -> None:
    code, text = rig.run(port="7496")
    assert code == 2
    assert '$env:STOINK_ALLOW_LIVE_PORT = "1"' in text and "never sets it" in text
    assert not rig.prompts
    rig.nothing_built()
    assert not (rig.home / "Stoinkulator").exists()


def test_live_port_with_the_opt_in_set_by_the_owner_passes_the_guard(rig: Rig) -> None:
    rig.env["STOINK_ALLOW_LIVE_PORT"] = "1"
    code, _ = rig.run(port="7496")
    assert code == 0


def test_the_command_never_sets_the_opt_in(rig: Rig) -> None:
    rig.run()
    assert "STOINK_ALLOW_LIVE_PORT" not in rig.env
    assert "STOINK_ALLOW_LIVE_PORT" not in os.environ


def test_a_recording_inside_onedrive_or_the_repository_is_refused_before_the_gate(rig: Rig) -> None:
    for record in (rig.tmp / "OneDrive - Org" / "rec.jsonl", REPO / "probe-rec.jsonl"):
        code, text = rig.run("--record", str(record))
        assert code == 2 and "Refused" in text
        assert not record.exists()
    assert not rig.prompts
    rig.nothing_built()


def test_an_existing_recording_is_never_overwritten(rig: Rig) -> None:
    record = rig.tmp / "rec" / "mine.jsonl"
    record.parent.mkdir()
    record.write_text("keep me", encoding="utf-8")
    code, _ = rig.run("--record", str(record))
    assert code == 2 and record.read_text(encoding="utf-8") == "keep me"
    rig.nothing_built()


def test_a_mapping_without_a_home_folder_is_refused_and_creates_nothing(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_home(*args: Any, **kw: Any) -> Path:
        raise AssertionError("the machine's own home folder was asked for")

    monkeypatch.setattr(Path, "home", no_home)
    out = io.StringIO()
    before = sorted(rig.tmp.rglob("*"))
    code = cli.probe_data_main(["--port", "7497"], env={}, stdin=Tty(), out=out, connection_factory=boom)
    assert code == 2 and "neither USERPROFILE nor HOME" in out.getvalue()
    assert sorted(rig.tmp.rglob("*")) == before


def test_the_default_recording_goes_under_the_home_folder_with_the_probe_prefix(rig: Rig) -> None:
    code, text = rig.run()
    assert code == 0, text
    assert rig.recording.name == "probe-data-2024-11-22-001.jsonl"
    assert f"Recording: {rig.recording}" in text
    header = json.loads(rig.recording.read_text(encoding="utf-8").splitlines()[0])
    assert header["command"] == "probe-data AAPL"
    assert not (rig.home / "Stoinkulator" / "bars").exists()


def test_no_record_writes_no_recording(rig: Rig) -> None:
    code, text = rig.run("--no-record")
    assert code == 0
    assert "Recording check" not in text and "Recording file:   none (--no-record)" in text
    assert not (rig.home / "Stoinkulator").exists()


def test_help_and_argument_errors_exit_before_any_client(
        rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    cases: list[tuple[list[str], int]] = [
        (["--help"], 0),
        ([], 2),
        (["--port", "7497", "--symbols", "A,B,C,D,E,F"], 2),
        (["--port", "7497", "--symbols", "AA PL"], 2),
        (["--port", "7497", "--quote-seconds", "121"], 2),
        (["--port", "7497", "--quote-seconds", "0.5"], 2),
        (["--port", "x"], 2),
        (["--port", "0"], 2),
        (["--port", "7497", "--client-id", "123"], 2),
        (["--port", "7497", "--client-id", "0"], 2),
        (["--port", "7497", "--connect-timeout", "0"], 2),
        (["--port", "7497", "--record", "x.jsonl", "--no-record"], 2),
        (["--port", "7497", "--yes"], 2),
    ]
    for argv, expected in cases:
        with pytest.raises(SystemExit) as info:
            cli.probe_data_main(argv, env={}, connection_factory=boom, market_data_factory=boom,
                                recorder_factory=boom)
        assert info.value.code == expected, argv
    shown = capsys.readouterr().out
    assert "exit codes" in shown and "--no-history" in shown and "--quote-seconds" in shown
    assert "130" in shown


def test_five_symbols_are_accepted_and_are_split_upper_cased_and_deduplicated(rig: Rig) -> None:
    code, text = rig.run("--symbols", "aapl, msft,AAPL,tsla,nvda,amd", "--no-history")
    assert code == 0, text
    assert "AAPL, MSFT, TSLA, NVDA, AMD" in text


# -- the steps ---------------------------------------------------------------------------------


def test_happy_path_ends_with_exit_0_and_the_facts(rig: Rig) -> None:
    code, text = rig.run()
    assert code == 0, text
    assert "connected after 1 attempt(s)" in text
    assert "market up, historical up" in text
    assert "farms IB reported: usfarm (market), ushmds (historical), secdefil (security definitions)" in text
    assert "IB minus machine +2.0 s" in text
    assert "New York:   2024-11-22 17:00:00 EST" in text
    assert "conId 265598, primary exchange NASDAQ, currency USD, time zone US/Eastern" in text
    assert "LIVE data" in text
    assert "raw date string of the first bar, as IB sent it: '%d'" % OPEN_EPOCH in text
    assert "first bar is 09:30: yes; last bar is 15:59: yes" in text
    volumes = [1234 + i for i in range(390)]
    assert f"day total volume: {sum(volumes)}; median per-minute volume: {1234 + 194}" in text  # 194.5 -> 194
    summary = text.split("7. Summary")[1]
    assert "AAPL: LIVE data" in summary and "history end, 390 bars" in summary
    assert "first 09:30 last 15:59" in summary and "first bar volume 1234" in summary
    assert f"day total volume {sum(volumes)}, median per-minute volume 1428" in summary
    assert "2104" in summary
    assert rig.script.set_types == [1]
    assert rig.conns[0].state is ConnState.CLOSED
    assert text.rstrip().splitlines()[-2] == "Result: all steps done (exit 0)"
    assert text.rstrip().splitlines()[-1] == cli.PATHS_NOTE
    assert ("Recording check: 0 finding(s): 0 from last-trade-time and volume ticks (expected), "
            "0 other") in text


def test_the_median_is_of_the_per_minute_volumes(rig: Rig) -> None:
    rig.script.first_volume = 500_000
    rig.script.bars = 3
    code, text = rig.run()
    assert code == 0, text
    assert "day total volume: 1500003; median per-minute volume: 500001" in text


def test_the_small_volume_hint_appears_for_88328_and_not_for_3533124(rig: Rig) -> None:
    rig.script.first_volume = 88_328
    code, text = rig.run()
    assert code == 0  # a hint, not a failure
    assert "volume looks too small for shares: check that the TWS API setting that sends US stock " \
           "sizes in lots is UNTICKED" in text
    rig.script.first_volume = 3_533_124
    _, text = rig.run()
    assert "volume looks too small" not in text
    assert "is plausible for shares" in text
    assert probe.SMALL_VOLUME_SHARES == 100_000


def test_delayed_path_reports_delayed_and_succeeds(rig: Rig) -> None:
    rig.script.live_ticks = False
    rig.script.quote_errors = {1: (10089, "Requested market data requires additional subscription")}
    code, text = rig.run("--no-history")
    assert code == 0, text
    assert rig.script.set_types == [1, 3]
    assert "AAPL: DELAYED data (15 min)" in text
    assert "IB error 10089: Requested market data requires additional subscription" in text
    assert "6. History: skipped" in text
    assert "68 last (delayed)" in text


def test_no_ticks_at_all_reports_no_data_and_lists_the_errors(rig: Rig) -> None:
    rig.script.live_ticks = rig.script.delayed_ticks = False
    rig.script.quote_errors = {
        1: (354, "Not subscribed"), 3: (10197, "No market data during competing session"),
    }
    code, text = rig.run("--no-history")
    assert code == 1
    assert "AAPL: NO data" in text
    assert "IB error 354: Not subscribed" in text
    assert "10197: No market data during competing session" in text.split("7. Summary")[1]
    assert "What to do: NO data for a symbol" in text
    assert text.rstrip().splitlines()[-2] == "Result: connected, but a step failed (exit 1)"


def test_history_error_162_is_printed_verbatim(rig: Rig) -> None:
    msg = "Historical Market Data Service error message:HMDS query returned no data: AAPL@SMART Trades"
    rig.script.history_error = (162, msg)
    code, text = rig.run()
    assert code == 1
    assert f"IB error 162: {msg}" in text
    assert "outcome: error" in text


def test_several_symbols_are_each_probed(rig: Rig) -> None:
    code, text = rig.run("--symbols", "AAPL,MSFT", "--no-history")
    assert code == 0, text
    assert text.count("LIVE data") >= 2


def test_an_account_shaped_string_never_appears_in_the_output_or_the_recording(rig: Rig) -> None:
    rig.script.live_ticks = rig.script.delayed_ticks = False
    rig.script.quote_errors = {1: (10167, ACCOUNT_TEXT), 3: (10167, ACCOUNT_TEXT)}
    code, text = rig.run("--no-history")
    assert code == 1
    assert "Account ACCOUNT is not subscribed" in text
    recording = rig.recording.read_text(encoding="utf-8")
    for shown in (text, recording):
        assert "DU1234567" not in shown and "1234567" not in shown
        assert "DU0000000" not in shown and "0000000" not in shown
    assert "managedaccounts" not in text.lower()


def test_the_user_and_machine_names_are_removed_from_the_recording(rig: Rig) -> None:
    rig.env["USERNAME"] = "zedquux"
    rig.env["COMPUTERNAME"] = "BOX-ZQ9"
    rig.script.live_ticks = rig.script.delayed_ticks = False
    rig.script.quote_errors = {1: (321, "bad request from zedquux on BOX-ZQ9")}
    code, text = rig.run("--no-history")
    assert code == 1
    recording = rig.recording.read_text(encoding="utf-8").lower()
    assert "zedquux" not in recording and "box-zq9" not in recording


def test_ctrl_c_exits_130_with_the_connection_and_the_recorder_closed(rig: Rig) -> None:
    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    code, text = rig.run(sleep=interrupt)
    assert code == 130
    assert "Interrupted (Ctrl-C)" in text and "(exit 130)" in text
    assert rig.conns[0].state is ConnState.CLOSED
    assert any(c.name == "disconnect" for c in rig.fakes[0].calls)
    rig.recorders[0].on_raw("probe", ())
    assert rig.recorders[0].dropped == 1  # closed: later events are dropped, not written


def test_cannot_connect_exits_3_lists_each_attempt_and_advises(rig: Rig) -> None:
    rig.script.connect = ConnectBehavior.REFUSE
    code, text = rig.run()
    assert code == 3
    assert "attempt 1:" in text and "attempt 2:" in text and "502" in text
    assert "What to do: Cannot connect" in text
    assert "Result: could not connect (exit 3)" in text
    assert rig.conns[0].state is ConnState.CLOSED


def test_cannot_connect_with_nothing_recorded_leaves_no_recording_file(rig: Rig) -> None:
    rig.open_error = OSError("socket closed")
    code, text = rig.run()
    assert code == 3 and "Could not connect: OSError" in text
    assert not (rig.home / "Stoinkulator").exists()  # and the empty folders are gone too
    assert "Recording:" not in text


def test_a_connection_refused_here_exits_2_and_leaves_no_recording_file(rig: Rig) -> None:
    from MainStoinker.ib.connection import BrokerConnectionRefusedHere

    rig.open_error = BrokerConnectionRefusedHere("refused here")
    code, text = rig.run()
    assert code == 2 and "REFUSED: refused here" in text
    assert not (rig.home / "Stoinkulator").exists()


def test_a_recording_that_holds_events_is_kept_after_a_failed_connect(rig: Rig) -> None:
    rig.script.connect = ConnectBehavior.REFUSE
    code, text = rig.run()
    assert code == 3
    assert rig.recording.exists() and "Recording check" in text


def test_only_data_requests_reach_the_fake_and_every_subscription_is_cancelled(rig: Rig) -> None:
    rig.script.live_ticks = False
    code, _ = rig.run()
    assert code == 0
    calls = rig.fakes[0].calls
    names = {c.name for c in calls} - {"connect", "disconnect", "run"}
    assert names <= DATA_REQUESTS
    assert {"reqCurrentTime", "reqContractDetails", "reqMarketDataType", "reqMktData",
            "cancelMktData", "reqHistoricalData"} <= names
    assert "reqMktData" in names
    assert [c.name for c in calls].count("reqMktData") == [c.name for c in calls].count("cancelMktData") == 2
    snapshots = [c for c in calls if c.name == "reqMktData" and len(c.args) > 3 and c.args[3]]
    assert not snapshots  # regulatorySnapshot (and snapshot) never


def test_the_scan_summary_splits_tick_findings_from_the_others(rig: Rig) -> None:
    rig.script.tick_strings = True
    out = io.StringIO()
    say = cli._Say(out)
    record = rig.tmp / "r.jsonl"
    record.write_text("\n".join([
        '{"format": "x"}',
        '{"seq": 1, "kind": "raw", "name": "tickString", "args": [3, 45, "1732290000"]}',
        '{"seq": 2, "kind": "raw", "name": "tickString", "args": [3, 48, "1732290001"]}',
        '{"seq": 3, "kind": "raw", "name": "error", "args": [3, "x"]}',
    ]) + "\n", encoding="utf-8")
    run_reason = LONG_RUN_REASON
    findings = [Finding(2, run_reason, ""), Finding(3, run_reason, ""), Finding(4, "other reason", "")]
    probe.report_probe_findings(say, record, findings)
    text = out.getvalue()
    assert "3 finding(s): 2 from last-trade-time and volume ticks (expected), 1 other" in text
    assert "line 4: other reason" in text and "line 2" not in text and "line 3:" not in text


def test_a_real_run_with_tick_strings_counts_them_as_expected(rig: Rig) -> None:
    rig.script.tick_strings = True
    code, text = rig.run("--no-history")
    assert code == 0
    line = next(x for x in text.splitlines() if x.startswith("Recording check:"))
    assert "0 other" in line, line


def test_closed_session_day() -> None:
    def at(*a: int) -> datetime:
        return datetime(*a, tzinfo=UTC)

    assert str(closed_session_day(at(2024, 11, 22, 22, 0))) == "2024-11-22"  # Fri after close
    assert str(closed_session_day(at(2024, 11, 22, 18, 0))) == "2024-11-21"  # Fri 13:00 NY
    assert str(closed_session_day(at(2024, 11, 25, 15, 0))) == "2024-11-22"  # Mon 10:00 NY
    assert str(closed_session_day(at(2024, 11, 24, 15, 0))) == "2024-11-22"  # Sunday


def test_closed_session_day_follows_the_market_calendar() -> None:
    """Issue 285: a holiday is skipped, and a half day has closed at 13:00 New York."""

    def at(*a: int) -> datetime:
        return datetime(*a, tzinfo=UTC)

    assert str(closed_session_day(at(2024, 11, 28, 20, 0))) == "2024-11-27"  # Thanksgiving, after 16:00
    assert str(closed_session_day(at(2024, 11, 29, 15, 0))) == "2024-11-27"  # day after, 10:00 NY
    assert str(closed_session_day(at(2024, 11, 29, 18, 30))) == "2024-11-29"  # 13:30 NY: half day closed
    assert str(closed_session_day(at(2024, 11, 29, 17, 30))) == "2024-11-27"  # 12:30 NY: still open
    assert str(closed_session_day(at(2024, 9, 3, 14, 0))) == "2024-08-30"  # the day after Labor Day, 10:00


# -- the fixes in the shared fetch-bars code ---------------------------------------------------------


def test_the_volume_note_no_longer_says_a_rerun_fetches_again() -> None:
    note = cli.VOLUME_NOTE
    assert "fetch that data again" not in note
    assert "delete the symbol's folder under the output folder" in note
    assert "new --out" in note and "skips days already on disk" in note


def test_the_estimate_is_singular_and_never_says_at_least_zero() -> None:
    one = "up to 1 request (1 symbol x 1 day), at least 10 s at the pacing limits"
    assert cli.describe_estimate(1, 1, 1, 10) == one
    six = "up to 6 requests (2 symbols x 3 days), at least 1 min 00 s at the pacing limits"
    assert cli.describe_estimate(6, 2, 3, 60) == six
    for seconds in (0, 0.4):
        text = cli.describe_estimate(1, 1, 1, seconds)
        assert "at least" not in text and "0 s" not in text


def test_the_gap_line_counts_a_failed_day_apart_from_no_data() -> None:
    from MainStoinker.ib.history import GapRow

    def row(day: int, status: str) -> GapRow:
        return GapRow(date(2024, 8, day), status, 390, 0, ())

    @dataclass
    class Failure:
        day: date

    @dataclass
    class Result:
        gaps: list[GapRow]
        failures: list[Failure]

    res = Result([row(12, "complete"), row(13, "complete"), row(14, "no_data")], [])
    assert cli.gap_counts_text(res) == "2 complete, 1 no data"
    res = Result([row(12, "complete"), row(13, "complete"), row(14, "no_data")], [Failure(date(2024, 8, 14))])
    assert cli.gap_counts_text(res) == "2 complete, 1 failed, 0 no data"
    res = Result([row(13, "no_data"), row(14, "no_data")], [Failure(date(2024, 8, 14))])
    assert cli.gap_counts_text(res) == "1 failed, 1 no data"


def _fetch_session(tmp_path: Path) -> Any:
    from tests.test_ib_cli import Session

    return Session(tmp_path)


def test_fetch_bars_shows_a_failed_day_apart_from_no_data_in_the_gap_line(tmp_path: Path) -> None:
    from tests.test_ib_cli import DAYS
    from tests.test_ib_fetch import error_behaviour

    s = _fetch_session(tmp_path)
    s.rig.script(date(2024, 8, 13), error_behaviour(200, "No security definition has been found"))
    try:
        code = s.main("--symbols", "AAPL", *DAYS, "--port", "7497", "--out", str(tmp_path / "bars"))
        assert code == 1
        assert "AAPL: gaps: 2 complete, 1 failed, 0 no data" in s.text
        assert "1 no_data" not in s.text
    finally:
        for conn in s.connections:
            conn.close()
        for recorder in s.recorders:
            recorder.close()
        for fake in s.fakes:
            fake.close()


def test_fetch_bars_singular_estimate_and_no_recording_left_when_the_connection_is_refused(
        tmp_path: Path) -> None:
    from MainStoinker.ib.connection import BrokerConnectionRefusedHere

    s = _fetch_session(tmp_path)
    s.typed = "nope"
    s.main("--symbols", "AAPL", "--start", "2024-08-12", "--end", "2024-08-12", "--port", "7497",
           "--out", str(tmp_path / "bars"))
    assert "up to 1 request (1 symbol x 1 day)" in s.text and "at least 0 s" not in s.text
    s.typed = "7497"
    original = s.connection_factory

    def refusing(args: Any, observers: list[Any]) -> IbConnection:
        conn = original(args, observers)

        def refuse() -> None:
            raise BrokerConnectionRefusedHere("refused here")

        conn.open = refuse  # type: ignore[method-assign]
        return conn

    s.connection_factory = refusing  # type: ignore[method-assign]
    s.out.truncate(0)
    s.out.seek(0)
    try:
        code = s.main("--symbols", "AAPL", "--start", "2024-08-12", "--end", "2024-08-12", "--port", "7497",
                      "--out", str(tmp_path / "bars"))
        assert code == 2 and "REFUSED: refused here" in s.text
        assert not (tmp_path / "home" / "Stoinkulator").exists()
    finally:
        for conn in s.connections:
            conn.close()
        for recorder in s.recorders:
            recorder.close()


# -- the source and the dispatch ---------------------------------------------------------------------


def test_the_source_uses_no_order_names_and_reads_the_environment_nowhere() -> None:
    source = PROBE_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)}
    names |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.args}
    bad = [n for n in names if "order" in n.lower().replace("recorder", "") or "position" in n.lower()]
    assert not bad, bad
    for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "regulatorySnapshot", "snapshot"):
        assert word not in source, word
    assert "environ" not in source and "getenv" not in source and "putenv" not in source
    assert "STOINK_ALLOW_LIVE_PORT" not in source


def child(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-P", "-c", code], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)


LOADED = ("print(sorted(m for m in sys.modules\n"
          "             if m == 'ibapi' or m.startswith(('ibapi.', 'MainStoinker.ib'))))")


@pytest.mark.parametrize("command", ["backtest", "runs", "report"])
def test_the_offline_commands_never_import_the_ib_package(command: str) -> None:
    done = child("import sys, runpy\nsys.argv = ['MainStoinker', '" + command + "', '--help']\n"
                 "try:\n    runpy.run_module('MainStoinker', run_name='__main__')\n"
                 "except SystemExit:\n    pass\n" + LOADED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout


def test_importing_the_package_does_not_import_the_ib_package() -> None:
    done = child("import sys, MainStoinker\n" + LOADED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


@pytest.mark.parametrize(("argv", "code"), [(["--help"], 0), (["--symbols", "AAPL"], 2)])
def test_the_main_module_dispatches_probe_data_in_process_and_stops_before_any_client(
        argv: list[str], code: int, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """In-process (never a child process that could reach a broker): `--help` and a missing
    `--port` end in the argument parser, before any client is built."""
    import runpy

    monkeypatch.setattr(sys, "argv", ["MainStoinker", "probe-data", *argv])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("MainStoinker", run_name="__main__")
    assert info.value.code == code
    shown = capsys.readouterr()
    assert ("--quote-seconds" in shown.out) if code == 0 else ("--port" in shown.err)


# -- fix round: the recording check, farm names, the clock step, IB codes ---------------------------


def _tick_line(number: int, tick_type: int, value: str) -> str:
    return json.dumps({"seq": number, "kind": "raw", "name": "tickString", "args": [3, tick_type, value]})


def _planted_values() -> dict[str, tuple[int, str]]:
    account = "".join(["D", "U"]) + str(7654321)  # built at run time: no account-shaped literal
    path = "C:" + "/" + "Users" + "/" + "someone" + "/" + "x"
    mail = "a.b" + "@" + "example." + "com"
    return {
        "account on a type 45 line": (45, account),
        "account on a type 48 line": (48, account),
        "account on a type 84 line": (84, "U" + str(7654321)),
        "path on a type 45 line": (45, path),
        "path on a type 32 line": (32, path),
        "e-mail on a type 32 line": (32, "mail me at " + mail),
    }


@pytest.mark.parametrize("what", sorted(_planted_values()))
def test_a_planted_token_on_a_tick_string_line_is_other(tmp_path: Path, what: str) -> None:
    tick_type, value = _planted_values()[what]
    record = tmp_path / "planted.jsonl"
    lines = ['{"format": "x"}', _tick_line(2, tick_type, value)]
    record.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = scan_recording(record, [])
    mine = [f for f in findings if f.line == 2]
    assert mine, findings
    expected, other = probe.split_findings(record, findings)
    assert expected == 0, (expected, other)
    assert [f for f in other if f.line == 2] == mine
    out = io.StringIO()
    probe.report_probe_findings(cli._Say(out), record, findings)
    assert "line 2:" in out.getvalue()


def test_genuine_epoch_and_rt_volume_lines_are_expected_and_a_wrong_reason_is_not(tmp_path: Path) -> None:
    record = tmp_path / "genuine.jsonl"
    record.write_text("\n".join([
        '{"format": "x"}',
        _tick_line(2, 45, "1732290000"),
        _tick_line(3, 48, "190.5;300;1732290000123;12345;190.4;true"),
        _tick_line(4, 48, "190.5;300;1732290000123;12345;190.4;false"),
    ]) + "\n", encoding="utf-8")
    findings = scan_recording(record, [])
    findings = [f for f in findings if f.line > 1]  # line 1 is the stand-in header of this file
    # issue 156 item 2: scan_recording itself treats genuine type 45 / 48 values as known shapes
    assert findings == [], findings
    # split_findings still handles such findings if they occur (an older scan, another tool)
    findings = [
        Finding(2, LONG_RUN_REASON, ""), Finding(3, LONG_RUN_REASON, ""),
        Finding(4, SEPARATED_RUN_REASON, ""),
    ]
    expected, other = probe.split_findings(record, findings)
    assert (expected, other) == (len(findings), [])
    # the same lines with another reason (an address, say) are not the expected kind
    expected, other = probe.split_findings(record, [Finding(2, "IPv4 address", "")])
    assert expected == 0 and len(other) == 1


def test_farm_names_keep_every_name_per_kind_in_order(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "FARM_MESSAGES", (
        (2104, "Market data farm connection is OK:usfarm.nj"),
        (2104, "Market data farm connection is OK:uscrypto"),
        (2104, "Market data farm connection is OK:usfarm.nj"),
        (2106, "HMDS data farm connection is OK:euhmds"),
        (2106, "HMDS data farm connection is OK:ushmds"),
        (2158, "Sec-def data farm connection is OK:secdefnj"),
    ))
    code, text = rig.run("--no-history")
    assert code == 0, text
    assert ("farms IB reported: usfarm.nj, uscrypto (market), euhmds, ushmds (historical), "
            "secdefnj (security definitions)") in text


def test_farm_names_are_parsed_from_the_inactive_but_available_form(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "FARM_MESSAGES", (
        (2108, "Market data farm connection is inactive but should be available upon demand.usfarm"),
        (2107, "HMDS data farm connection is inactive but should be available upon demand.ushmds"),
    ))
    code, text = rig.run("--no-history")
    assert code == 0, text
    assert "market up, historical up" in text
    assert "farms IB reported: usfarm (market), ushmds (historical)" in text


def test_a_farm_that_is_up_without_a_parsable_name_says_so_and_not_none(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "FARM_MESSAGES", (
        (2108, "Market data farm connection is inactive"),
        (2106, "HMDS data farm connection is OK:ushmds"),
    ))
    code, text = rig.run("--no-history")
    assert code == 0, text
    assert "farms IB reported: name not parsed (market), ushmds (historical)" in text
    assert "farms IB reported: none" not in text


def test_a_failing_farm_step_does_not_turn_into_no_answer_for_the_clock(rig: Rig) -> None:
    base = rig.connection_factory

    def factory(args: Any, observers: list[Any]) -> IbConnection:
        conn = base(args, observers)

        def boom(*a: Any, **k: Any) -> bool:
            raise RuntimeError("farm wait broke")

        conn.wait_for_data_farm = boom  # type: ignore[method-assign]
        return conn

    rig.connection_factory = factory  # type: ignore[method-assign]
    code, text = rig.run("--no-history")
    assert code == 1, text
    assert "data farms: unexpected RuntimeError: farm wait broke" in text
    assert "no answer from IB" not in text
    assert "IB clock minus this machine's clock = +2.0 s" in text
    assert [c.name for c in rig.fakes[0].calls].count("reqCurrentTime") == 1


def test_a_failing_clock_request_is_reported_as_that_failure(rig: Rig) -> None:
    def factory(conn: IbConnection) -> IbMarketData:
        md = IbMarketData(conn, clock=rig.clock, poll_interval=0.01)

        def boom() -> None:
            raise RuntimeError("clock request broke")

        md.request_server_time = boom  # type: ignore[method-assign]
        return md

    rig.market_data_factory = factory  # type: ignore[method-assign]
    code, text = rig.run("--no-history")
    assert code == 1, text
    assert "the server-time request failed: RuntimeError: clock request broke" in text
    assert "no answer from IB" not in text


def test_an_unknown_symbol_shows_its_ib_code_in_the_summary(rig: Rig) -> None:
    base = rig.script.apply

    def apply(fake: Any) -> None:
        base(fake)

        def lookup(rid: int, c: Any) -> list[tuple[Any, ...]]:
            if c.symbol == "ZZZZ":
                return [error_played(rid, 200, "No security definition has been found for the request")]
            return [("contractDetails", rid, details(c.symbol)), ("contractDetailsEnd", rid)]

        fake.respond("reqContractDetails", lookup)

    rig.script.apply = apply  # type: ignore[method-assign]
    code, text = rig.run("--symbols", "AAPL,ZZZZ", "--no-history")
    assert code == 1, text
    codes_line = next(x for x in text.splitlines() if "IB codes seen:" in x)
    assert "200: " in codes_line, codes_line


def test_the_real_recorder_header_names_probe_data_and_holds_nothing_private(rig: Rig) -> None:
    private = "zed" + "quux"
    rig.env["USERNAME"] = private
    out = io.StringIO()
    rig.env.setdefault("USERPROFILE", str(rig.home))
    code = cli.probe_data_main(
        ["--port", "7497", "--symbols", "aapl,msft", "--no-history", "--quote-seconds", "10"],
        env=rig.env, stdin=Tty(), input_fn=lambda prompt: "7497", out=out, clock=rig.clock,
        sleep=rig.clock.sleep, today=date(2024, 11, 22),
        connection_factory=rig.connection_factory, market_data_factory=rig.market_data_factory)
    assert code == 0, out.getvalue()
    first = rig.recording.read_text(encoding="utf-8").splitlines()[0]
    head = json.loads(first)
    assert head["command"] == "probe-data AAPL MSFT"
    assert "fetch-bars" not in first
    assert private not in first.lower() and str(rig.home) not in first and str(rig.tmp) not in first


def test_the_quotes_request_is_not_a_snapshot_and_not_a_regulatory_snapshot(rig: Rig) -> None:
    code, _ = rig.run("--no-history")
    assert code == 0
    requests = [c for c in rig.fakes[0].calls if c.name == "reqMktData"]
    assert requests
    for call in requests:
        assert call.args[3] is False
        assert call.args[4] is False
