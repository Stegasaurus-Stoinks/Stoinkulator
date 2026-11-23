"""`MainStoinker/ib/quick_probe.py`, in-process, over a real `IbConnection` + `IbMarketData`
and the scripted `FakeIbClient`. Nothing opens a socket and nothing really sleeps."""

from __future__ import annotations

import ast
import io
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.common import TickAttrib
from ibapi.contract import Contract, ContractDetails

from MainStoinker.ib import quick_probe
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import DATA_REQUESTS, ConnState, IbConnection
from MainStoinker.ib.market_data import IbMarketData
from MainStoinker.ib.quick_probe import closed_session_day, main, scrub
from tests.fake_ib import FakeIbClient, make_bar

SERVER = datetime(2024, 11, 22, 22, 0, tzinfo=UTC)  # Friday 17:00 New York, session closed
OPEN_EPOCH = int(datetime(2024, 11, 22, 14, 30, tzinfo=UTC).timestamp())  # 09:30 New York
ACCOUNT_TEXT = "Account DU1234567 is not subscribed"


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


def details() -> ContractDetails:
    d = ContractDetails()
    d.contract = Contract()
    d.contract.conId = 265598
    d.contract.symbol = "AAPL"
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
    set_types: list[int] = field(default_factory=list)

    def apply(self, fake: FakeIbClient) -> None:
        fake.respond("reqCurrentTime", [("currentTime", int(SERVER.timestamp()))])
        fake.respond("reqContractDetails", lambda rid, c: [
            ("contractDetails", rid, details()), ("contractDetailsEnd", rid),
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
            return played

        fake.respond("reqMktData", quotes)

        def history(rid: int, *rest: Any) -> list[tuple[Any, ...]]:
            if self.history_error is not None:
                return [error_played(rid, *self.history_error)]
            played: list[tuple[Any, ...]] = [
                ("historicalData", rid, make_bar(OPEN_EPOCH + 60 * i, 10, 11, 9, 10.5, 1234 + i))
                for i in range(self.bars)
            ]
            played.append(("historicalDataEnd", rid, "s", "e"))
            return played

        fake.respond("reqHistoricalData", history)


@dataclass
class Rig:
    script: Script
    clock: TickClock
    fakes: list[FakeIbClient] = field(default_factory=list)
    conns: list[IbConnection] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def connection_factory(self, args: Any) -> IbConnection:
        def client(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
            fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger))
            self.script.apply(fake)
            self.fakes.append(fake)
            return fake

        conn = IbConnection(
            args.host, args.port, args.client_id, env=self.env, client_factory=client,
            clock=self.clock, sleep=self.clock.sleep, poll_interval=0.002,
        )
        self.conns.append(conn)
        return conn

    def market_data_factory(self, conn: IbConnection) -> IbMarketData:
        return IbMarketData(conn, clock=self.clock, poll_interval=0.01)

    def run(
        self, *extra: str, port: str = "7497", sleep: Callable[[float], None] | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        out = io.StringIO()
        self.env = {} if env is None else env
        code = main(
            ["--port", port, "--quote-seconds", "10", *extra],
            env=self.env, stdin=Tty(), input_fn=lambda prompt: port, out=out,
            connection_factory=self.connection_factory,
            market_data_factory=self.market_data_factory,
            clock=self.clock, sleep=sleep or self.clock.sleep,
            wall=lambda: SERVER - timedelta(seconds=2),
        )
        return code, out.getvalue()


@pytest.fixture
def rig() -> Rig:
    return Rig(Script(), TickClock())


def boom(*_: Any) -> Any:
    raise AssertionError("a client or connection was built")


# -- the gate -----------------------------------------------------------------------------


def test_gate_refuses_without_a_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    out = io.StringIO()
    code = main(["--port", "7497"], env={}, stdin=Tty(False), out=out, connection_factory=boom)
    assert code == 2
    assert "interactive terminal" in out.getvalue()


@pytest.mark.parametrize("typed", ["", "7496", "yes", "7497x"])
def test_gate_refuses_when_the_typed_text_is_not_the_port(typed: str) -> None:
    out = io.StringIO()
    code = main(
        ["--port", "7497"], env={}, stdin=Tty(), input_fn=lambda p: typed, out=out,
        connection_factory=boom,
    )
    assert code == 2
    assert "Read-only data session" in out.getvalue()
    assert "Places no orders" in out.getvalue()


def test_live_port_without_opt_in_prints_the_powershell_line_and_builds_nothing() -> None:
    out = io.StringIO()

    def no_prompt(_: str) -> str:
        raise AssertionError("prompted")

    code = main(
        ["--port", "7496"], env={}, stdin=Tty(), input_fn=no_prompt, out=out, connection_factory=boom
    )
    assert code == 2
    assert '$env:STOINK_ALLOW_LIVE_PORT = "1"' in out.getvalue()
    assert "Refusing to connect" in out.getvalue()


def test_live_port_with_the_opt_in_set_by_the_owner_passes_the_guard(rig: Rig) -> None:
    code, _ = rig.run(port="7496", env={"STOINK_ALLOW_LIVE_PORT": "1"})
    assert code == 0


def test_help_and_argument_errors_exit_before_any_client(capsys: pytest.CaptureFixture[str]) -> None:
    for argv, expected in (
        (["--help"], 0), ([], 2), (["--port", "7497", "--symbol", "A", "--symbol", "B",
                                    "--symbol", "C", "--symbol", "D"], 2),
        (["--port", "7497", "--quote-seconds", "121"], 2), (["--port", "x"], 2),
        (["--port", "7497", "--client-id", "123"], 2),
    ):
        with pytest.raises(SystemExit) as info:
            main(argv, connection_factory=boom, market_data_factory=boom)
        assert info.value.code == expected, argv
    capsys.readouterr()


# -- the steps ----------------------------------------------------------------------------


def test_happy_path_ends_with_exit_0_and_a_summary(rig: Rig) -> None:
    code, text = rig.run()
    assert code == 0, text
    assert "connected in" in text and "1 attempt(s)" in text
    assert "market up, historical up" in text
    assert "IB minus machine +2.0 s" in text
    assert "New York:   2024-11-22 17:00:00 EST" in text
    assert "conId 265598, primary exchange NASDAQ, currency USD, time zone US/Eastern" in text
    assert "LIVE data" in text
    assert "raw date string of the first bar, as IB sent it: '%d'" % OPEN_EPOCH in text
    assert "first bar is 09:30: yes; last bar is 15:59: yes" in text
    summary = text.split("7. Summary")[1]
    assert "AAPL: LIVE data" in summary and "history end, 390 bars" in summary
    assert "first 09:30 last 15:59" in summary and "first bar volume 1234" in summary
    assert "2104" in summary
    assert rig.script.set_types == [1]
    assert rig.conns[0].state is ConnState.CLOSED
    assert "no account number by design" in text


def test_delayed_path_reports_delayed_and_succeeds(rig: Rig) -> None:
    rig.script.live_ticks = False
    rig.script.quote_errors = {1: (10089, "Requested market data requires additional subscription")}
    code, text = rig.run("--no-history")
    assert code == 0, text
    assert rig.script.set_types == [1, 3]
    assert "AAPL: DELAYED data (15 min)" in text
    assert "IB error 10089: Requested market data requires additional subscription" in text
    assert "6. History: skipped" in text


def test_no_ticks_at_all_reports_no_data_and_lists_the_error(rig: Rig) -> None:
    rig.script.live_ticks = rig.script.delayed_ticks = False
    rig.script.quote_errors = {
        1: (354, "Not subscribed"), 3: (10197, "No market data during competing session"),
    }
    code, text = rig.run("--no-history")
    assert code == 1
    assert "AAPL: NO data" in text
    assert "IB error 354: Not subscribed" in text
    assert "10197: No market data during competing session" in text.split("7. Summary")[1]


def test_history_error_162_is_printed_verbatim(rig: Rig) -> None:
    msg = "Historical Market Data Service error message:HMDS query returned no data: AAPL@SMART Trades"
    rig.script.history_error = (162, msg)
    code, text = rig.run()
    assert code == 1
    assert f"IB error 162: {msg}" in text
    assert "outcome: error" in text


def test_account_shaped_text_and_managed_accounts_never_appear(rig: Rig) -> None:
    rig.script.live_ticks = rig.script.delayed_ticks = False
    rig.script.quote_errors = {1: (10167, ACCOUNT_TEXT), 3: (10167, ACCOUNT_TEXT)}
    code, text = rig.run("--no-history")
    assert code == 1
    assert "DU1234567" not in text and "1234567" not in text
    assert "Account ACCOUNT is not subscribed" in text
    assert "DU0000000" not in text and "managed" not in text.lower()


def test_ctrl_c_closes_the_connection_and_exits_non_zero(rig: Rig) -> None:
    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    code, text = rig.run(sleep=interrupt)
    assert code == 130
    assert "Interrupted" in text
    assert rig.conns[0].state is ConnState.CLOSED
    assert any(c.name == "disconnect" for c in rig.fakes[0].calls)


def test_only_data_requests_reach_the_fake(rig: Rig) -> None:
    rig.script.live_ticks = False
    code, _ = rig.run()
    assert code == 0
    names = {c.name for c in rig.fakes[0].calls} - {"connect", "disconnect", "run"}
    assert names <= DATA_REQUESTS
    assert {"reqCurrentTime", "reqContractDetails", "reqMarketDataType", "reqMktData",
            "cancelMktData", "reqHistoricalData"} <= names


def test_cannot_connect_exits_3() -> None:
    from tests.fake_ib import ConnectBehavior

    rig = Rig(Script(), TickClock())
    script = rig.script.apply

    def refuse(fake: FakeIbClient) -> None:
        script(fake)
        fake.script_connect(ConnectBehavior.REFUSE)

    rig.script.apply = refuse  # type: ignore[method-assign]
    code, text = rig.run()
    assert code == 3
    assert "attempt 1: IB error 502" in text


# -- helpers and source ---------------------------------------------------------------------


def test_scrub_replaces_account_shapes() -> None:
    assert scrub("x DU1234567 y U12345 z AB123456") == "x ACCOUNT y ACCOUNT z ACCOUNT"
    assert scrub("conId 265598, AAPL, ab1234") == "conId 265598, AAPL, ab1234"


def test_closed_session_day() -> None:
    def at(*a: int) -> datetime:
        return datetime(*a, tzinfo=UTC)

    assert str(closed_session_day(at(2024, 11, 22, 22, 0))) == "2024-11-22"  # Fri after close
    assert str(closed_session_day(at(2024, 11, 22, 18, 0))) == "2024-11-21"  # Fri 13:00 NY
    assert str(closed_session_day(at(2024, 11, 25, 15, 0))) == "2024-11-22"  # Mon 10:00 NY
    assert str(closed_session_day(at(2024, 11, 24, 15, 0))) == "2024-11-22"  # Sunday


def test_source_uses_no_order_names_and_reads_the_environment_only_in_main() -> None:
    source = Path(quick_probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)}
    assert not [n for n in names if "order" in n.lower() or "position" in n.lower()]
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name != "main":
            assert "environ" not in ast.unparse(fn), fn.name
    assert "putenv" not in source and "environ[" not in source and "environ.update" not in source
