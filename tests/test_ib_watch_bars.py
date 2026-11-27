"""`MainStoinker/ib/watch_bars.py`: the owner-run live bar watcher, driven in-process.

The client behind every connection is `FakeIbClient` (a real `IbConnection` and `IbMarketData`
over it); nothing opens a socket. Time is scripted: the watcher's own clock (`Rig.t`) is frozen
and moves only through the injected `sleep`, which is also where the scripted stream is fed in,
one step at a time, and only after the previous step's event was read. Delays printed by the
watcher are therefore exact. The plumbing clock of the connection and `IbMarketData` is a
separate, ticking one so that their polls end.
"""

from __future__ import annotations

import ast
import io
import os
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ibapi.common import BarData
from ibapi.contract import Contract, ContractDetails

from MainStoinker.engine.feed import read_offline_bars
from MainStoinker.ib import events, watch_bars
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import ConnState, IbConnection
from MainStoinker.ib.market_data import IbMarketData, Outcome
from MainStoinker.ib.recorder import SessionRecorder, scan_recording
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.fake_ib import _WAKE, FARM_MESSAGES, ConnectBehavior, FakeIbClient, make_bar
from tests.home_guard import no_writes_to_real_home
from tests.no_build import forbid_building
from tests.not_paper_text import assert_one_not_paper_line

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "MainStoinker" / "ib" / "watch_bars.py"
NY = ZoneInfo("America/New_York")
HEADER = "date,time,open,high,low,close,volume"
DATA_CALLS = {"connect", "run", "disconnect", "reqCurrentTime", "reqContractDetails",
              "reqHistoricalData", "cancelHistoricalData"}


# -- the real home folder is never touched --------------------------------------------------


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
    """No test of this command may reach the real profile: the process environment is pointed at
    `tmp_path` for the test, and any write inside the real `Stoinkulator` folder, by this process,
    fails the test (tests/home_guard.py; the real folder is not looked at, so what else writes it,
    such as an owner's real session, cannot matter)."""
    fake_home = tmp_path / "process-home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    with no_writes_to_real_home(REAL_HOME):
        yield


# -- scripted time and stream ---------------------------------------------------------------


def ep(hh: int, mm: int, ss: int = 0) -> int:
    """Epoch seconds of a New York wall-clock time on the scripted day."""
    return int(datetime(2024, 8, 16, hh, mm, ss, tzinfo=NY).timestamp())


E0 = ep(9, 33, 25)  # IB's server time when the watcher asks (script time t = 0)


def T(mm: int, ss: float = 0.0, hh: int = 9) -> float:
    """Script time of the New York time hh:mm:ss (server time = E0 + t)."""
    return ep(hh, mm) + ss - E0


class TickClock:
    """The plumbing clock: moves by `tick` on every read and by the slept time on every sleep."""

    def __init__(self, tick: float = 0.05) -> None:
        self.now = 0.0
        self.tick = tick
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            value = self.now
            self.now += self.tick
            return value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class FeederStuck(BaseException):
    """The script waited for something that never happened (a bug in a test or in the watcher)."""


class CountingMd(IbMarketData):
    """`IbMarketData` that counts the events its streams handed out (so the feeder can wait)."""

    taken = 0

    def poll_event(self, req_id: int, timeout: float) -> Any:
        result = super().poll_event(req_id, timeout)
        if not isinstance(result, Outcome):
            self.taken += 1
        return result


class Tty(io.StringIO):
    def __init__(self, interactive: bool = True) -> None:
        super().__init__()
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def details(symbol: str) -> ContractDetails:
    d = ContractDetails()
    d.contract = Contract()
    d.contract.conId = {"AAPL": 265598, "MSFT": 272093, "TSLA": 76792991}.get(symbol, 1000)
    d.contract.symbol = symbol
    d.contract.secType = "STK"
    d.contract.exchange = "SMART"
    d.contract.primaryExchange = "NASDAQ"
    d.contract.currency = "USD"
    d.minTick = 0.01
    d.timeZoneId = "US/Eastern"
    return d


Bar6 = tuple[float, float, float, float, int]
AAPL_BACKFILL = [(ep(9, 30), 99.0, 99.5, 98.8, 99.2, 5000), (ep(9, 31), 99.2, 99.9, 99.1, 99.8, 4000),
                 (ep(9, 32), 99.8, 100.1, 99.7, 100.0, 3500)]
MSFT_BACKFILL = [(ep(9, 30), 400.0, 400.5, 399.5, 400.2, 7000), (ep(9, 31), 400.2, 400.9, 400.1, 400.8, 6000),
                 (ep(9, 32), 400.8, 401.1, 400.7, 401.0, 5500)]


class Step:
    def __init__(self, t: float, fn: Callable[[], None], ready: Callable[[], bool] | None) -> None:
        self.t, self.fn, self.ready = t, fn, ready


class Rig:
    """What `watch_bars.main` is given instead of the real classes, and the scripted stream."""

    LIMIT = 4000  # idle sleeps without progress before the feeder gives up

    def __init__(self, tmp_path: Path, symbols: tuple[str, ...] = ("AAPL", "MSFT")) -> None:
        self.tmp = tmp_path
        self.symbols = symbols
        self.run_symbols: list[str] = list(symbols)
        self.t = 0.0
        self.plumb = TickClock()
        self.out = io.StringIO()
        self.env = {"USERPROFILE": str(tmp_path / "home"), "HOME": str(tmp_path / "home")}
        self.fakes: list[FakeIbClient] = []
        self.connections: list[IbConnection] = []
        self.recorders: list[SessionRecorder] = []
        self.mds: list[CountingMd] = []
        self.prompts: list[str] = []
        self.typed = "7497"
        self.behavior = ConnectBehavior.ACCEPT
        self.farm = True
        self.server_epoch = E0
        self.backfills: dict[str, list[list[tuple[Any, ...]]]] = {
            "AAPL": [AAPL_BACKFILL], "MSFT": [MSFT_BACKFILL]}
        self.history_error: dict[str, tuple[int, str]] = {}
        self.unknown: set[str] = set()
        self.req_ids: dict[str, int] = {}
        self.requests: list[tuple[str, int, str, bool]] = []
        self.steps: list[Step] = []
        self.expected = 0
        self.steps_run = 0
        self.stuck = 0
        self.sorted = False
        self.armed_count: int | None = None
        self.interrupt_after: int | None = None
        self.wall_offset = -0.4  # this computer's clock is 0.4 s behind IB
        self.require_paper_override: bool | None = None  # build the connection otherwise than asked
        self.accounts = "DU0000000"  # the list IB sends with nextValidId
        self.mid_list: str | None = None  # a managedAccounts list that comes with the first clock answer

    # -- the watcher's clock -------------------------------------------------------------

    def __call__(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return datetime.fromtimestamp(self.server_epoch + self.wall_offset + self.t, tz=UTC)

    def armed(self) -> bool:
        need = self.armed_count
        if need is None:
            need = len([s for s in self.run_symbols if s not in self.unknown and s not in self.history_error])
        return self.out.getvalue().count(": backfill:") >= need

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        if not self.armed():
            return
        if self.interrupt_after is not None and self.steps_run >= self.interrupt_after:
            raise KeyboardInterrupt
        if not self.sorted:
            self.steps.sort(key=lambda s: s.t)
            self.sorted = True
        step = self.steps[0] if self.steps else None
        if step is None or self.mds[-1].taken < self.expected or (step.ready and not step.ready()):
            self.stuck += 1
            if self.stuck > self.LIMIT:
                raise FeederStuck(f"stuck: {len(self.steps)} steps left, taken {self.mds[-1].taken} "
                                  f"of {self.expected}\n{self.out.getvalue()[-1500:]}")
            return
        self.steps.pop(0)
        self.stuck = 0
        self.t = max(self.t, step.t)
        step.fn()
        self.steps_run += 1

    # -- scripting --------------------------------------------------------------------------

    def at(self, t: float, fn: Callable[[], None], ready: Callable[[], bool] | None = None) -> None:
        self.steps.append(Step(t, fn, ready))

    def upd(self, symbol: str, bar_mm: int, when: tuple[int, float], ohlcv: Bar6, *,
            hh: int = 9) -> None:
        """An update of the bar that starts at `hh:bar_mm`, delivered at New York time `when`."""
        o, h, lo, c, v = ohlcv

        def push() -> None:
            self.expected += 1
            self.fakes[-1].push("historicalDataUpdate", self.req_ids[symbol],
                                make_bar(ep(hh, bar_mm), o, h, lo, c, v))
            self.await_delivery(symbol)

        self.at(self.tt(when[0], when[1], hh), push)

    def await_delivery(self, symbol: str) -> None:
        """Return only when what was just pushed has been delivered by the fake's reader thread.
        The watcher's next poll runs on a ticking clock, so a slow thread made that poll time out
        first; the watcher then slept (moving script time on) and read the update later than
        scripted. A wake item queued behind the push is taken only after the push was delivered
        (one reader, in order). Real time, bounded; a miss is reported by the feeder's own check."""
        fake = self.fakes[-1]
        fake._q.put((_WAKE, ()))
        end = time.monotonic() + 5.0
        while not fake._q.empty() and time.monotonic() < end:
            time.sleep(0.0005)

    def raw_update(self, symbol: str, when: tuple[int, float], bar: BarData) -> None:
        def push() -> None:
            self.expected += 1
            self.fakes[-1].push("historicalDataUpdate", self.req_ids[symbol], bar)
            self.await_delivery(symbol)

        self.at(self.tt(when[0], when[1]), push)

    def error(self, symbol: str, when: tuple[int, float], code: int, text: str) -> None:
        def push() -> None:
            self.fakes[-1].push("error", self.req_ids[symbol], 1_700_000_000_000, code, text, "")

        self.at(self.tt(when[0], when[1]), push)
        # the stream reports an error as an outcome, not as an event: wait for the printed line
        self.at(self.tt(when[0], when[1]) + 0.001, lambda: None,
                ready=lambda: f"IB error {code} for {symbol}" in self.text)

    def tt(self, mm: int, ss: float = 0.0, hh: int = 9) -> float:
        """Script time of the New York time hh:mm:ss, for this rig's server time at t = 0."""
        return ep(hh, mm) + ss - self.server_epoch

    def drop(self, when: tuple[int, float], code: int | str,
             ready: Callable[[], bool] | None = None) -> None:
        self.at(self.tt(when[0], when[1]), lambda: self.fakes[-1].drop(code), ready)

    def end_at(self, minutes: int, symbols: int | None = None) -> None:
        """A last step just past the watch time (the loop starts after the request spacing)."""
        count = symbols if symbols is not None else len(self.symbols) - len(self.unknown)
        self.at((count - 1) * watch_bars.REQUEST_SPACING + minutes * 60 + 1.0, lambda: None)

    def standard(self, *, end: bool = True, minutes: int = 5) -> None:
        """Both symbols: 09:33 to 09:36 close, 09:37 is still forming at the end of the watch."""
        aapl: dict[int, list[tuple[float, float, Bar6]]] = {
            33: [(30.0, (100.0, 100.5, 99.9, 100.2, 1000)), (40.0, (100.0, 100.6, 99.9, 100.4, 1500)),
                 (50.0, (100.0, 100.6, 99.9, 100.3, 1800))],
            34: [(1.5, (100.3, 100.4, 100.2, 100.3, 100)), (30.0, (100.3, 100.9, 100.2, 100.8, 2200))],
            35: [(0.5, (100.8, 100.8, 100.8, 100.8, 50)), (20.0, (100.8, 101.0, 100.7, 100.9, 700)),
                 (40.0, (100.8, 101.1, 100.7, 101.0, 1200)), (55.0, (100.8, 101.2, 100.7, 101.1, 1900))],
            36: [(2.0, (101.1, 101.1, 101.0, 101.0, 80)), (30.0, (101.1, 101.3, 101.0, 101.2, 900))],
            37: [(1.0, (101.2, 101.2, 101.2, 101.2, 10))],
        }
        for bar_mm, items in aapl.items():
            for secs, ohlcv in items:
                # the update that opens a minute is stamped with that minute
                self.upd("AAPL", bar_mm, (bar_mm, secs), ohlcv)
        for k in range(33, 38):
            o = 400.0 + (k - 33)
            first = (k, 35.0) if k == 33 else (k, 3.0)
            self.upd("MSFT", k, first, (o, o + 0.5, o - 0.5, o + 0.2, 100 * k))
            if k < 37:
                self.upd("MSFT", k, (k, 50.0 if k == 33 else 30.0), (o, o + 0.5, o - 0.5, o + 0.4, 200 * k))
        if end:
            self.end_at(minutes)

    # -- factories ------------------------------------------------------------------------

    def client_factory(self, *, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger), connect_behavior=self.behavior,
                            farm_messages=FARM_MESSAGES if self.farm else (), managed_accounts=self.accounts)
        fake.respond("reqContractDetails", self._contract)
        fake.respond("reqHistoricalData", self._history)
        fake.respond("reqCurrentTime", lambda: [
            *([("managedAccounts", self.mid_list)] if self.mid_list else []),
            ("currentTime", self.server_epoch)])
        self.fakes.append(fake)
        return fake

    def _contract(self, req_id: int, contract: Contract) -> list[tuple[Any, ...]]:
        if contract.symbol in self.unknown:
            return [("error", req_id, 1_700_000_000_000, 200,
                     "No security definition has been found for the request", "")]
        return [("contractDetails", req_id, details(contract.symbol)), ("contractDetailsEnd", req_id)]

    def _history(self, req_id: int, contract: Contract, end: str, duration: str, size: str, what: str,
                 rth: int, fmt: int, keep: bool, opts: Any) -> list[tuple[Any, ...]]:
        symbol = contract.symbol
        self.req_ids[symbol] = req_id
        self.requests.append((symbol, req_id, duration, keep))
        if symbol in self.history_error:
            code, text = self.history_error[symbol]
            return [("error", req_id, 1_700_000_000_000, code, text, "")]
        asked = len([r for r in self.requests if r[0] == symbol])
        scripts = self.backfills.get(symbol, [[]])
        bars = scripts[min(asked, len(scripts)) - 1]
        played: list[tuple[Any, ...]] = [("historicalData", req_id, make_bar(*b)) for b in bars]
        played.append(("historicalDataEnd", req_id, "start", "end"))
        return played

    def connection_factory(self, args: Any, observers: list[Any]) -> IbConnection:
        conn = IbConnection(args.host, args.port, args.client_id, env={}, client_factory=self.client_factory,
                            clock=self.plumb, sleep=self.plumb.sleep, poll_interval=0.002, attempts=2,
                            retry_delay=3, observers=observers,
                            require_paper=args.paper if self.require_paper_override is None
                            else self.require_paper_override)
        self.connections.append(conn)
        return conn

    def market_data_factory(self, conn: IbConnection) -> IbMarketData:
        md = CountingMd(conn, clock=self.plumb, poll_interval=0.01)
        self.mds.append(md)
        return md

    def recorder_factory(self, args: Any) -> SessionRecorder | None:
        if args.record is None:
            return None
        args.record.parent.mkdir(parents=True, exist_ok=True)
        recorder = SessionRecorder(args.record, clock=self.plumb, command=["watch-bars", *args.symbols],
                                   port=args.port, private_values=args.private)
        self.recorders.append(recorder)
        return recorder

    # -- running --------------------------------------------------------------------------

    def input_fn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.typed

    def main(self, *argv: str, tty: bool = True, with_factories: bool = True,
             recorder: bool = True, stdin: Any = None) -> int:
        if "--symbols" in argv:
            self.run_symbols = argv[argv.index("--symbols") + 1].split(",")
        extra: dict[str, Any] = {}
        if with_factories:
            extra = {"connection_factory": self.connection_factory,
                     "market_data_factory": self.market_data_factory}
            if recorder:
                extra["recorder_factory"] = self.recorder_factory
        return watch_bars.main(
            list(argv), env=self.env, stdin=Tty(tty) if stdin is None else stdin,
            input_fn=self.input_fn, out=self.out,
            clock=self, sleep=self.sleep, wall=self.wall, today=date(2024, 8, 16), **extra)

    def watch(self, *extra: str, symbols: str | None = None, minutes: int = 5, record: bool = False,
              port: str = "7497", paper: bool = False, **kw: Any) -> int:
        names = symbols if symbols is not None else ",".join(self.symbols)
        argv = ["--paper"] if paper else ["--port", port]
        argv += ["--symbols", names, "--minutes", str(minutes),
                "--out", str(self.tmp / "live")]
        if not record:
            argv.append("--no-record")
        return self.main(*argv, *extra, **kw)

    @property
    def text(self) -> str:
        return self.out.getvalue()

    @property
    def live(self) -> Path:
        return self.tmp / "live"

    def csv(self, symbol: str, day: str = "2024-08-16") -> Path:
        return self.live / symbol / f"{symbol}_live_{day}.csv"

    def rows(self, symbol: str) -> list[list[str]]:
        lines = self.csv(symbol).read_text(encoding="utf-8").splitlines()
        assert lines[0] == HEADER
        return [line.split(",") for line in lines[1:]]

    def closed_lines(self, symbol: str) -> list[str]:
        return [ln for ln in self.text.splitlines()
                if re.match(rf"^\d\d:\d\d  {symbol}  O ", ln)]


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    r = Rig(tmp_path)
    yield r
    for conn in r.connections:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


def nothing_built(r: Rig) -> None:
    assert not r.connections and not r.fakes and not r.recorders and not r.mds


def closed_line(symbol: str, mm: int, o: float, h: float, lo: float, c: float, volume: int,
                updates: int, first: float, closed: float) -> str:
    return (f"09:{mm:02d}  {symbol}  O {o!r} H {h!r} L {lo!r} C {c!r}  volume {volume}  "
            f"(updates {updates}, first seen +{first:.1f} s after the minute started, "
            f"closed +{closed:.1f} s after the minute ended)")


# -- arguments, help, guards, gate ----------------------------------------------------------


def test_help_exits_zero_before_any_client_and_says_what_it_is(rig: Rig,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        rig.main("--help")
    assert info.value.code == 0
    shown = capsys.readouterr().out
    for code in ("0 ", "1 ", "2 ", "3 ", "4 ", "130"):
        assert f"  {code}" in shown
    assert "--port" in shown and "--quiet" in shown and "--no-record" in shown
    assert "DATA REQUESTS ONLY" in shown and "Only the owner runs it" in shown
    assert "places no trade" in shown and "connects to TWS" in shown
    nothing_built(rig)


@pytest.mark.parametrize("argv", [
    [],  # no --port
    ["--port", "0"],
    ["--port", "7497", "--client-id", "0"],
    ["--port", "7497", "--client-id", "123"],
    ["--port", "7497", "--symbols", "AAPL,MSFT,TSLA,AMD,NVDA,META"],
    ["--port", "7497", "--symbols", "AA PL"],
    ["--port", "7497", "--symbols", ","],
    ["--port", "7497", "--minutes", "0"],
    ["--port", "7497", "--minutes", "421"],
    ["--port", "7497", "--backfill-minutes", "0"],
    ["--port", "7497", "--backfill-minutes", "121"],
    ["--port", "7497", "--connect-timeout", "0"],
    ["--port", "7497", "--record", "x.jsonl", "--no-record"],
    ["--port", "7497", "--order"],
])
def test_bad_arguments_exit_2_before_any_client(rig: Rig, argv: list[str],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    forbid_building(rig)  # a wrongly accepted argument must fail here, not start a watch that waits
    with pytest.raises(SystemExit) as info:
        rig.main(*argv)
    assert info.value.code == 2
    assert not rig.prompts
    nothing_built(rig)
    assert not (rig.tmp / "home").exists() and not (rig.tmp / "live").exists()
    capsys.readouterr()


def test_the_defaults_and_the_limits_are_the_documented_ones() -> None:
    args = watch_bars.parse_args(["--port", "7497"])
    assert (args.client_id, args.symbols, args.minutes, args.backfill_minutes) == (22, ["AAPL"], 30, 10)
    assert args.connect_timeout == 30 and args.quiet is False and args.host == "127.0.0.1"
    assert watch_bars.parse_args(["--port", "7497", "--symbols", "aapl", "msft,tsla", "AAPL"]).symbols == [
        "AAPL", "MSFT", "TSLA"]
    top = watch_bars.parse_args(["--port", "7497", "--minutes", "420", "--backfill-minutes", "120"])
    assert (top.minutes, top.backfill_minutes) == (420, 120)


def test_a_live_port_without_the_opt_in_exits_2_with_the_powershell_line(rig: Rig) -> None:
    code = rig.watch(port="7496")
    assert code == watch_bars.EXIT_REFUSED
    assert "STOINK_ALLOW_LIVE_PORT" in rig.text and '$env:STOINK_ALLOW_LIVE_PORT = "1"' in rig.text
    assert "never sets it" in rig.text
    assert not rig.prompts
    nothing_built(rig)
    assert not (rig.tmp / "live").exists()


def test_a_live_port_with_the_opt_in_reaches_the_gate_and_stops_there_on_wrong_text(rig: Rig) -> None:
    rig.env["STOINK_ALLOW_LIVE_PORT"] = "1"
    rig.typed = "no"
    assert rig.watch(port="7496") == watch_bars.EXIT_REFUSED
    assert len(rig.prompts) == 1 and "(7496)" in rig.prompts[0]
    nothing_built(rig)
    assert not (rig.tmp / "live").exists()


def test_the_gate_refuses_without_a_terminal_and_leaves_no_folder(rig: Rig) -> None:
    assert rig.watch(tty=False) == watch_bars.EXIT_REFUSED
    assert "only the owner runs it" in rig.text
    assert not rig.prompts
    nothing_built(rig)
    assert not (rig.tmp / "live").exists() and not (rig.tmp / "home" / "Stoinkulator").exists()


@pytest.mark.parametrize("typed", ["", "y", "yes", "7496", "port", "75"])
def test_the_gate_needs_the_port_typed_and_leaves_no_folder(rig: Rig, typed: str) -> None:
    rig.typed = typed
    assert rig.watch(record=True) == watch_bars.EXIT_REFUSED
    assert len(rig.prompts) == 1 and "(7497)" in rig.prompts[0]
    nothing_built(rig)
    assert not (rig.tmp / "live").exists() and not (rig.tmp / "home" / "Stoinkulator").exists()


def test_the_gate_describes_what_will_happen(rig: Rig) -> None:
    rig.typed = "nope"
    rig.watch("--backfill-minutes", "15", symbols="AAPL,MSFT", minutes=12)
    for word in ("AAPL, MSFT", "12 minutes", "backfill 15 minutes", "data requests only",
                 "Live files", "Recording file", "Only the owner runs it", "places no trade"):
        assert word in rig.text, word


def test_eof_at_the_prompt_is_a_refusal(rig: Rig) -> None:
    def eof(prompt: str) -> str:
        raise EOFError

    rig.input_fn = eof  # type: ignore[method-assign]
    assert rig.watch() == watch_bars.EXIT_REFUSED
    nothing_built(rig)
    assert not (rig.tmp / "live").exists()


@pytest.mark.parametrize("where", ["onedrive_out", "repo_out", "onedrive_record", "repo_record",
                                   "env_onedrive", "out_is_file", "out_under_file", "symbol_is_file",
                                   "record_exists"])
def test_unusable_paths_are_refused_before_the_gate(rig: Rig, where: str) -> None:
    out, record = rig.tmp / "live", None
    if where == "onedrive_out":
        out = rig.tmp / "OneDrive - Org" / "live"
    elif where == "repo_out":
        out = REPO / "live_bars"
    elif where == "onedrive_record":
        record = rig.tmp / "OneDrive" / "rec.jsonl"
    elif where == "repo_record":
        record = REPO / "rec.jsonl"
    elif where == "env_onedrive":
        rig.env["OneDrive"] = str(rig.tmp / "synced")
        out = rig.tmp / "synced" / "live"
    elif where == "out_is_file":
        out.write_text("x", encoding="utf-8")
    elif where == "out_under_file":
        (rig.tmp / "afile").write_text("x", encoding="utf-8")
        out = rig.tmp / "afile" / "live"
    elif where == "symbol_is_file":
        out.mkdir()
        (out / "AAPL").write_text("x", encoding="utf-8")
    else:
        record = rig.tmp / "rec" / "mine.jsonl"
        record.parent.mkdir()
        record.write_text("keep me", encoding="utf-8")
    argv = ["--port", "7497", "--symbols", "AAPL", "--out", str(out)]
    if record is not None:
        argv += ["--record", str(record)]
    else:
        argv.append("--no-record")
    assert rig.main(*argv) == watch_bars.EXIT_REFUSED
    assert "Refused" in rig.text
    assert not rig.prompts
    nothing_built(rig)
    if where in ("onedrive_out", "repo_out", "env_onedrive"):
        assert not out.exists()
    if where == "record_exists":
        assert record is not None and record.read_text(encoding="utf-8") == "keep me"


def test_a_default_under_a_onedrive_home_is_refused(rig: Rig) -> None:
    rig.env["USERPROFILE"] = str(rig.tmp / "OneDrive - Org" / "me")
    assert rig.main("--port", "7497") == watch_bars.EXIT_REFUSED
    assert "OneDrive" in rig.text
    nothing_built(rig)


def test_a_mapping_without_a_home_folder_is_refused_before_the_gate_and_creates_nothing(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_home(*args: Any, **kw: Any) -> Path:
        raise AssertionError("the machine's own home folder was asked for")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.setattr(os.path, "expanduser", no_home)
    rig.env.clear()
    before = sorted(rig.tmp.rglob("*"))
    assert rig.main("--port", "7497") == watch_bars.EXIT_REFUSED  # every default
    given = ["--port", "7497", "--out", str(rig.tmp / "live"), "--no-record"]  # even with both paths given
    assert rig.main(*given) == watch_bars.EXIT_REFUSED
    assert "USERPROFILE" in rig.text and "HOME" in rig.text
    assert not rig.prompts
    nothing_built(rig)
    assert sorted(rig.tmp.rglob("*")) == before


def test_home_is_used_when_userprofile_is_missing_and_the_defaults_are_under_it(rig: Rig) -> None:
    del rig.env["USERPROFILE"]
    rig.end_at(1, symbols=1)
    assert rig.main("--port", "7497", "--symbols", "AAPL", "--minutes", "1", "--no-record") == 0
    assert (rig.tmp / "home" / "Stoinkulator" / "live" / "AAPL").is_dir()


def test_the_default_recording_is_a_watch_bars_file_in_the_recordings_folder(rig: Rig) -> None:
    rig.end_at(1, symbols=1)
    assert rig.main("--port", "7497", "--symbols", "AAPL", "--minutes", "1", "--out", str(rig.tmp / "live"),
                    recorder=False) == 0
    found = sorted((rig.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
    assert [p.name for p in found] == ["watch-bars-2024-08-16-001.jsonl"]
    assert "watch-bars" in found[0].read_text(encoding="utf-8").splitlines()[0]
    assert "Recording check: 0 finding(s)" in rig.text


def test_a_refused_connection_exits_3_and_closes_the_recording(rig: Rig) -> None:
    rig.behavior = ConnectBehavior.REFUSE
    assert rig.watch(record=True) == watch_bars.EXIT_NO_CONNECTION
    assert "Could not connect" in rig.text and "What to do" in rig.text
    assert all(c.state is ConnState.CLOSED for c in rig.connections)
    assert not any(c.name == "reqHistoricalData" for f in rig.fakes for c in f.calls)


# -- the scripted stream --------------------------------------------------------------------


def test_the_stream_emits_exactly_the_closed_bars_each_once_with_delays_from_the_script(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK, rig.text
    assert rig.closed_lines("AAPL") == [
        closed_line("AAPL", 33, 100.0, 100.6, 99.9, 100.3, 1800, 3, 30.0, 1.5),
        closed_line("AAPL", 34, 100.3, 100.9, 100.2, 100.8, 2200, 2, 1.5, 0.5),
        closed_line("AAPL", 35, 100.8, 101.2, 100.7, 101.1, 1900, 4, 0.5, 2.0),
        closed_line("AAPL", 36, 101.1, 101.3, 101.0, 101.2, 900, 2, 2.0, 1.0),
    ]
    assert rig.closed_lines("MSFT") == [
        closed_line("MSFT", 33, 400.0, 400.5, 399.5, 400.4, 6600, 2, 35.0, 3.0),
        closed_line("MSFT", 34, 401.0, 401.5, 400.5, 401.4, 6800, 2, 3.0, 3.0),
        closed_line("MSFT", 35, 402.0, 402.5, 401.5, 402.4, 7000, 2, 3.0, 3.0),
        closed_line("MSFT", 36, 403.0, 403.5, 402.5, 403.4, 7200, 2, 3.0, 3.0),
    ]
    # the order of the lines in the output follows the order in which the bars closed
    times = [ln[:5] for ln in rig.text.splitlines() if re.match(r"^\d\d:\d\d  (AAPL|MSFT)  O ", ln)]
    assert times == sorted(times)
    assert "IB minus this computer's clock = +0.4 s" in rig.text
    assert "AAPL: backfill: 3 bars, first 09:30, last 09:32" in rig.text


def test_the_files_hold_the_closed_bars_only_in_the_month_file_formats(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK
    assert sorted(p.relative_to(rig.live).as_posix() for p in rig.live.rglob("*") if p.is_file()) == [
        "AAPL/AAPL_live_2024-08-16.csv", "MSFT/MSFT_live_2024-08-16.csv"]
    raw = rig.csv("AAPL").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == HEADER
    assert lines[1] == f"2024-08-16 09:30:00,{ep(9, 30)},99.0,99.5,98.8,99.2,5000"
    assert lines[3] == f"2024-08-16 09:33:00,{ep(9, 33)},100.0,100.6,99.9,100.3,1800"
    assert lines[-1] == f"2024-08-16 09:36:00,{ep(9, 36)},101.1,101.3,101.0,101.2,900"
    assert [row[0][11:16] for row in rig.rows("AAPL")] == ["09:30", "09:31", "09:33", "09:34",
                                                           "09:35", "09:36"]  # 09:37 is still forming
    assert "101.2" not in {row[2] for row in rig.rows("AAPL")}  # the forming bar's open is not there
    assert len(rig.rows("MSFT")) == 6
    assert "Still forming" not in rig.text and "still forming, not written: 09:37" in rig.text


def test_the_files_read_back_as_engine_bars(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK
    for symbol in ("AAPL", "MSFT"):
        frame = read_offline_bars(rig.csv(symbol))
        assert list(frame.columns) == ["date", "time", "open", "high", "low", "close", "volume"]
        assert str(frame["date"].dtype).startswith("datetime64")
        assert frame["date"].dt.tz is None
        assert str(frame["time"].dtype) == "int64" and str(frame["volume"].dtype) == "int64"
        for name in ("open", "high", "low", "close"):
            assert str(frame[name].dtype) == "float64"
        assert frame["date"].is_monotonic_increasing and not frame["date"].duplicated().any()
        assert int(frame["time"].iloc[0]) == ep(9, 30)
        assert frame["date"].iloc[0].strftime("%Y-%m-%d %H:%M:%S") == "2024-08-16 09:30:00"


def test_the_summary_has_the_statistics_the_first_run_needs(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK
    text = rig.text
    summary = text[text.index("Summary"):]
    aapl = summary[summary.index("AAPL:"):summary.index("MSFT:")]
    assert "closed bars emitted live: 4" in aapl and "backfill bars written: 2" in aapl
    assert "missing minutes inside the span (1): 09:32" in aapl  # 09:32 was seen only in the snapshot
    assert "late revisions: 0; rows that may be stale: 0; bad bars skipped: 0" in aapl
    assert "stream: LIVE" in aapl and "DELAYED" not in aapl
    counts, firsts, closes = [3, 2, 4, 2], [30.0, 1.5, 0.5, 2.0], [1.5, 0.5, 2.0, 1.0]

    def mmm(values: list[float], spec: str) -> str:
        return " / ".join(format(v, spec) for v in (min(values), statistics.median(values), max(values)))

    assert f"updates per bar (min / median / max): {mmm(counts, 'g')}" in aapl
    assert f"first update after the minute started, s (min / median / max): {mmm(firsts, '.1f')}" in aapl
    assert f"closed after the minute ended, s (min / median / max): {mmm(closes, '.1f')}" in aapl
    assert "last closed bar: 09:36  O 101.1 H 101.3 L 101.0 C 101.2  volume 900" in aapl
    assert "file: " in aapl and "AAPL_live_2024-08-16.csv" in aapl
    assert "run fetch-bars for today after 19:00 New York and compare with the file(s) above" in text
    assert "server time" in text and "1 s resolution" in text


def test_forming_lines_are_throttled_and_quiet_removes_them(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK
    forming = [ln for ln in rig.text.splitlines() if "forming 09:" in ln]
    assert forming and all("(update " in ln for ln in forming)
    per_symbol = [ln for ln in forming if "  AAPL  " in ln]
    assert 1 <= len(per_symbol) <= 25  # at most one every 10 s of the 5 minutes, not one per update
    assert len(per_symbol) < 14  # AAPL sent 14 updates
    quiet = Rig(rig.tmp / "q")
    quiet.tmp.mkdir()
    quiet.standard()
    try:
        assert quiet.watch("--quiet") == watch_bars.EXIT_OK
        assert not [ln for ln in quiet.text.splitlines() if "forming 09:" in ln]
        assert len(quiet.closed_lines("AAPL")) == 4
    finally:
        for conn in quiet.connections:
            conn.close()
        for fake in quiet.fakes:
            fake.close()


def test_a_late_revision_is_counted_and_nothing_emitted_is_rewritten(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 33, (33, 50.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 35, (35, 1.0), (101.0, 101.0, 101.0, 101.0, 10))  # 09:34 never traded: 33 closes
    rig.upd("AAPL", 33, (35, 5.0), (100.0, 150.0, 99.9, 149.0, 9999))  # a revision of a bar already emitted
    rig.upd("AAPL", 34, (35, 6.0), (100.3, 100.4, 100.2, 100.3, 7))  # older than the forming minute
    rig.upd("AAPL", 35, (35, 30.0), (101.0, 101.2, 101.0, 101.1, 40))
    rig.upd("AAPL", 36, (36, 1.0), (101.1, 101.1, 101.1, 101.1, 3))
    rig.end_at(4)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("late revision for 09:33") == 1
    assert "late revision for 09:34 (the forming minute is 09:35)" in rig.text
    assert "nothing already emitted is rewritten" in rig.text
    assert "late revisions: 2" in rig.text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:33", "09:35"]
    assert rows[2][2:] == ["100.0", "100.6", "99.9", "100.3", "1800"]
    assert "150.0" not in rig.csv("AAPL").read_text(encoding="utf-8")
    assert "9999" not in rig.csv("AAPL").read_text(encoding="utf-8")
    assert len(rig.closed_lines("AAPL")) == 2  # 09:33 and 09:35


def test_a_missing_minute_is_listed_in_the_summary(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 36, (36, 2.0), (101.0, 101.0, 101.0, 101.0, 10))  # 09:34 and 09:35 have no bar
    rig.upd("AAPL", 37, (37, 2.0), (101.0, 101.0, 101.0, 101.0, 10))
    rig.end_at(4)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK
    assert "missing minutes inside the span (3): 09:32, 09:34, 09:35" in rig.text
    assert "09:34" not in "".join(r[0] for r in rig.rows("AAPL"))


def test_a_second_run_the_same_day_appends_only_later_bars(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 5.0), (100.2, 100.3, 100.2, 100.3, 10))
    rig.upd("AAPL", 34, (34, 30.0), (100.2, 100.9, 100.1, 100.8, 2200))
    rig.upd("AAPL", 35, (35, 5.0), (100.8, 100.8, 100.8, 100.8, 1))
    rig.end_at(3)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK
    first = rig.csv("AAPL").read_text(encoding="utf-8")
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:33", "09:34"]
    # the second run starts at 09:36:25 and its backfill repeats everything up to 09:35
    second = Rig(rig.tmp)
    second.server_epoch = ep(9, 36, 25)
    second.backfills = {"AAPL": [AAPL_BACKFILL[1:] + [
        (ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800), (ep(9, 34), 100.2, 100.9, 100.1, 100.8, 2200),
        (ep(9, 35), 100.8, 101.0, 100.7, 100.9, 500)]]}
    second.upd("AAPL", 36, (36, 30.0), (101.0, 101.2, 101.0, 101.1, 40))
    second.upd("AAPL", 37, (37, 1.0), (101.1, 101.1, 101.1, 101.1, 3))
    second.end_at(3, symbols=1)
    try:
        assert second.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, second.text
    finally:
        for conn in second.connections:
            conn.close()
        for fake in second.fakes:
            fake.close()
    assert "AAPL_live_2024-08-16.csv exists with 4 rows, last 09:34; appending only later bars" in second.text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:33", "09:34", "09:36"]
    assert len({r[1] for r in rows}) == len(rows)
    assert rig.csv("AAPL").read_text(encoding="utf-8").startswith(first)  # appended, nothing rewritten
    assert rig.csv("AAPL").read_text(encoding="utf-8").count(HEADER) == 1


def test_data_lost_requests_again_once_and_emits_no_duplicate(rig: Rig) -> None:
    rig.backfills = {"AAPL": [
        AAPL_BACKFILL,
        # the second backfill (at 09:36:10) repeats 09:31 to 09:33, adds 09:34 and 09:35 that closed during
        # the outage, and holds 09:36 as the bar still forming
        AAPL_BACKFILL[1:] + [(ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800),
                             (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 2200),
                             (ep(9, 35), 100.8, 101.2, 100.7, 101.1, 1900),
                             (ep(9, 36), 101.1, 101.1, 101.0, 101.05, 30)]]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 5.0), (100.3, 100.3, 100.3, 100.3, 5))  # 09:33 closes
    # the re-request goes out at 09:36:10: IB's snapshot cannot hold a closed 09:35 bar before then
    rig.drop((36, 10.0), 1101)
    rig.upd("AAPL", 36, (36, 20.0), (101.1, 101.3, 101.0, 101.2, 900))  # on the new request id
    rig.upd("AAPL", 37, (37, 1.0), (101.2, 101.2, 101.2, 101.2, 10))
    rig.end_at(4)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("requesting AAPL again") == 1
    requests = [r for r in rig.requests if r[0] == "AAPL"]
    assert len(requests) == 2 and requests[0][1] != requests[1][1] and all(r[3] for r in requests)
    assert "(backfilled)" in rig.text
    backfilled = [ln for ln in rig.text.splitlines() if ln.endswith("(backfilled)")]
    assert [ln[:5] for ln in backfilled] == ["09:34", "09:35"]
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:33", "09:34", "09:35", "09:36"]
    assert len({r[1] for r in rows}) == len(rows)  # no minute twice
    emitted = [ln[:5] for ln in rig.text.splitlines() if re.match(r"^\d\d:\d\d  AAPL  O ", ln)]
    assert len(emitted) == len(set(emitted)) == 4  # 09:33, 09:34 and 09:35 once, 09:36 once
    cancels = rig.fakes[-1].calls_named("cancelHistoricalData")
    assert [c.args[0] for c in cancels] == [requests[0][1], requests[1][1]]
    assert "re-requests after data loss: 1" in rig.text


def test_lost_then_restored_continues_and_a_later_bar_still_closes(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.drop((33, 50.0), 1102, ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(33, 55.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(3)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert "IB connectivity LOST (code 1100)" in rig.text and "IB connectivity restored." in rig.text
    lost_line = "AAPL 09:33 not written: spanned a connection loss"
    assert rig.text.index("LOST") < rig.text.index("restored.") < rig.text.index(lost_line)
    assert len(rig.requests) == 1  # data kept: no new request
    assert [r[0][11:16] for r in rig.rows("AAPL")][-1] == "09:31"  # 09:33 spanned the loss: not written


def test_still_lost_at_the_end_of_the_watch_exits_4(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.end_at(2)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_STOPPED
    assert "still lost" in rig.text


def test_a_closed_connection_stops_with_exit_4_and_the_files_stay_intact(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.drop((34, 20.0), "connectionClosed")
    rig.at(rig.tt(34, 40.0), lambda: None)
    assert rig.watch(symbols="AAPL", minutes=5) == watch_bars.EXIT_STOPPED
    assert "STOPPED: the connection to TWS closed" in rig.text
    assert rig.connections[0].state is ConnState.CLOSED
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:33"]
    assert "Summary" in rig.text and "exit 4" in rig.text


def test_an_ib_error_stops_that_symbol_only_and_the_exit_code_is_1(rig: Rig) -> None:
    rig.standard(end=False)
    rig.steps = [s for s in rig.steps if s.t < T(34, 10)]  # AAPL and MSFT stream up to 09:34:10 ...
    rig.error("MSFT", (34, 20.0), 10197, "No market data during competing live session")
    rig.upd("AAPL", 34, (34, 30.0), (100.3, 100.9, 100.2, 100.8, 2200))
    rig.upd("AAPL", 35, (35, 0.5), (100.8, 100.8, 100.8, 100.8, 50))
    rig.end_at(3)
    assert rig.watch(minutes=3) == watch_bars.EXIT_FAILED, rig.text
    assert "IB error 10197 for MSFT: No market data during competing live session" in rig.text
    assert "MSFT: stopped watching (IB error 10197); the other symbols continue" in rig.text
    assert [ln[:5] for ln in rig.closed_lines("AAPL")] == ["09:33", "09:34"]
    assert [ln[:5] for ln in rig.closed_lines("MSFT")] == ["09:33"]  # closed before the error, 09:34 never
    assert "MSFT: stopped: IB error 10197" in rig.text
    cancels = rig.fakes[-1].calls_named("cancelHistoricalData")
    assert [c.args[0] for c in cancels] == [rig.req_ids["AAPL"]]  # the dead request needs no cancel


def test_an_error_before_the_end_marker_stops_that_symbol(rig: Rig) -> None:
    rig.history_error["MSFT"] = (
        162, "Historical Market Data Service error message:No market data permissions")
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(2)
    assert rig.watch(minutes=2) == watch_bars.EXIT_FAILED
    assert "IB error 162 for MSFT: Historical Market Data Service error message" in rig.text
    assert [ln[:5] for ln in rig.closed_lines("AAPL")] == ["09:33"]


def test_an_unknown_symbol_is_reported_and_the_others_stream(rig: Rig) -> None:
    rig.unknown = {"MSFT"}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(2)
    assert rig.watch(minutes=2) == watch_bars.EXIT_FAILED
    assert "MSFT: stopped watching (cannot resolve" in rig.text
    assert [r[0] for r in rig.requests] == ["AAPL"]
    assert len(rig.closed_lines("AAPL")) == 1


def test_a_symbol_with_no_bars_at_all_makes_the_exit_code_1(rig: Rig) -> None:
    rig.backfills = {"AAPL": [[]]}
    rig.armed_count = 1
    rig.end_at(1, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=1) == watch_bars.EXIT_FAILED
    assert "AAPL: backfill: 0 bars" in rig.text


def test_a_bad_update_is_reported_and_skipped_without_killing_the_stream(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    inconsistent = make_bar(ep(9, 33), 100.0, 99.0, 98.0, 100.2, 5)  # high below open and close
    text_date = BarData()
    text_date.date = "20240816  09:33:00"  # a formatDate=1 string, not epoch seconds
    text_date.open = text_date.high = text_date.low = text_date.close = 100.0
    text_date.volume = 1  # type: ignore[assignment]
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.raw_update("AAPL", (33, 35.0), inconsistent)
    rig.raw_update("AAPL", (33, 36.0), text_date)
    rig.upd("AAPL", 33, (33, 50.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(2)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("AAPL: bad bar skipped") == 2
    assert "high 99.0 is below open" in rig.text and "not epoch seconds" in rig.text
    assert rig.closed_lines("AAPL") == [
        closed_line("AAPL", 33, 100.0, 100.6, 99.9, 100.3, 1800, 2, 30.0, 2.0)]
    assert "bad bars skipped: 2" in rig.text


def test_a_bar_without_a_later_update_is_closed_by_time_and_marked(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 0)  # the first update comes 1 s into the minute: the lag bound is 1 s
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 33, (33, 50.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.at(rig.tt(36, 0.0), lambda: None)  # 09:33 ended at 09:34:00; 90 s later nothing came
    rig.end_at(4)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK
    line = [ln for ln in rig.text.splitlines() if ln.startswith("09:33  AAPL")][0]
    assert "closed by time: no later update arrived within 90 s" in line
    assert rig.rows("AAPL")[-1][0][11:16] == "09:33"
    assert "closed by time 1" in rig.text


def test_an_interrupt_exits_130_with_requests_cancelled_and_everything_closed(rig: Rig) -> None:
    rig.standard()
    rig.interrupt_after = 6
    assert rig.watch(record=True) == watch_bars.EXIT_INTERRUPTED
    assert "Interrupted (Ctrl-C)" in rig.text and "exit 130" in rig.text and "Summary" in rig.text
    cancels = rig.fakes[-1].calls_named("cancelHistoricalData")
    assert sorted(c.args[0] for c in cancels) == sorted(rig.req_ids.values()) and len(cancels) == 2
    assert rig.connections[0].state is ConnState.CLOSED
    assert rig.recorders and all(r._closed for r in rig.recorders)  # noqa: SLF001
    assert rig.csv("AAPL").exists()  # what was emitted before the interrupt is on disk
    assert not rig.fakes[-1].run_errors


def test_an_unexpected_error_exits_4_with_a_one_line_message(rig: Rig) -> None:
    rig.standard()

    def boom(seconds: float) -> None:
        raise RuntimeError("something odd")

    rig.sleep = boom  # type: ignore[method-assign]
    assert rig.watch() == watch_bars.EXIT_STOPPED
    assert "Unexpected RuntimeError: something odd" in rig.text
    assert "Traceback" not in rig.text
    assert len(rig.fakes[-1].calls_named("cancelHistoricalData")) <= 2
    assert rig.connections[0].state is ConnState.CLOSED


def test_requests_are_spaced_by_the_injected_sleep_and_are_keep_up_to_date_history(rig: Rig) -> None:
    rig.standard(minutes=1)
    slept: list[float] = []
    original = rig.sleep

    def spy(seconds: float) -> None:
        slept.append(seconds)
        original(seconds)

    rig.sleep = spy  # type: ignore[method-assign]
    rig.watch("--backfill-minutes", "15", minutes=1)
    assert slept[0] >= 1.0  # between the first and the second request
    calls = rig.fakes[-1].calls_named("reqHistoricalData")
    assert len(calls) == 2 and [c.args[0] for c in calls] == [r[1] for r in rig.requests]
    for call in calls:
        assert call.args[2] == "" and call.args[3] == "900 S" and call.args[4] == "1 min"
        assert call.args[6] == 1 and call.args[7] == 2 and call.args[8] is True


def test_only_data_requests_reach_the_fake(rig: Rig) -> None:
    rig.standard()
    assert rig.watch(record=True) == watch_bars.EXIT_OK
    names = {c.name for f in rig.fakes for c in f.calls}
    assert names <= DATA_CALLS, names
    assert {"reqCurrentTime", "reqContractDetails", "reqHistoricalData", "cancelHistoricalData"} <= names


def test_an_account_shaped_string_never_reaches_the_output_the_files_or_the_recording(rig: Rig) -> None:
    rig.standard(end=False)
    rig.steps = [s for s in rig.steps if s.t < T(34, 10)]
    rig.error("MSFT", (34, 20.0), 10197, "Account DU1234567 has no permission; also U7654321")
    rig.end_at(3)
    assert rig.watch(minutes=3, record=True) == watch_bars.EXIT_FAILED
    assert "IB error 10197 for MSFT: Account ACCOUNT has no permission; also ACCOUNT" in rig.text
    assert "DU1234567" not in rig.text and "U7654321" not in rig.text
    assert "DU0000000" not in rig.text  # the managed account list is never printed
    for path in rig.live.rglob("*.csv"):
        assert "DU1234567" not in path.read_text(encoding="utf-8")
    recording = next((rig.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
    body = recording.read_text(encoding="utf-8")
    assert "DU1234567" not in body and "DU0000000" not in body and "U7654321" not in body
    assert scan_recording(recording, []) == []
    assert "Recording check: 0 finding(s)" in rig.text


# -- review round: finished bars only -------------------------------------------------------


def upd_at(rig: Rig, symbol: str, bar_epoch: int, when: int, ohlcv: Bar6) -> None:
    """An update of the bar starting at `bar_epoch`, delivered at server time `when` (epoch seconds)."""
    o, h, lo, c, v = ohlcv

    def push() -> None:
        rig.expected += 1
        rig.fakes[-1].push("historicalDataUpdate", rig.req_ids[symbol], make_bar(bar_epoch, o, h, lo, c, v))
        rig.await_delivery(symbol)

    rig.at(when - rig.server_epoch, push)


def hold_history(rig: Rig, *, from_request: int) -> list[tuple[str, float]]:
    """Make requests number `from_request` on (per run, 1 based) answer nothing: the test delivers the
    backfill itself, later. Returns the list that gets (symbol, script time) of every request sent."""
    sent: list[tuple[str, float]] = []
    original = rig._history  # noqa: SLF001

    def hist(req_id: int, contract: Contract, *rest: Any) -> list[tuple[Any, ...]]:
        sent.append((contract.symbol, rig.t))
        if len(sent) >= from_request:
            rig.req_ids[contract.symbol] = req_id
            rig.requests.append((contract.symbol, req_id, rest[1], rest[6]))
            return []
        return original(req_id, contract, *rest)

    rig._history = hist  # type: ignore[method-assign]  # noqa: SLF001
    return sent


def deliver_backfill(rig: Rig, symbol: str, bars: list[tuple[Any, ...]], at: float,
                     after_request: int = 1) -> None:
    """At script time `at`, once `after_request` requests of `symbol` are out, IB sends `bars` and the end."""

    def deliver() -> None:
        req_id = rig.req_ids[symbol]
        for b in bars:
            rig.fakes[-1].push("historicalData", req_id, make_bar(*b))
        rig.fakes[-1].push("historicalDataEnd", req_id, "start", "end")

    rig.at(at, deliver, ready=lambda: len([r for r in rig.requests if r[0] == symbol]) >= after_request)


def test_a_backfill_that_arrives_late_never_writes_the_forming_bar_as_closed(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 59)  # the request goes out 1 s before 09:33 ends ...
    rig.armed_count = 0
    hold_history(rig, from_request=1)
    partial = (ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)
    deliver_backfill(rig, "AAPL", AAPL_BACKFILL + [partial], at=4.0)  # ... and the answer arrives after it
    rig.at(4.5, lambda: None, ready=lambda: ": backfill:" in rig.text)
    rig.upd("AAPL", 33, (34, 4.0), (100.0, 100.9, 99.9, 100.8, 2500))
    rig.upd("AAPL", 34, (34, 5.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "the 09:33 bar is still forming" in rig.text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33"]
    assert rows[3][2:] == ["100.0", "100.9", "99.9", "100.8", "2500"]
    assert ",1000\n" not in rig.csv("AAPL").read_text(encoding="utf-8").split(f",{ep(9, 33)},")[1]
    assert "late revision for" not in rig.text


def test_a_minute_known_only_from_a_late_backfill_snapshot_is_not_written(rig: Rig) -> None:
    """The same late backfill, but IB's next update is already the next minute: 09:33 was seen only in
    the snapshot taken before it ended, so nothing about it is known to be final."""
    rig.server_epoch = ep(9, 33, 59)
    rig.armed_count = 0
    hold_history(rig, from_request=1)
    deliver_backfill(rig, "AAPL", AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)], at=4.0)
    rig.at(4.5, lambda: None, ready=lambda: ": backfill:" in rig.text)
    rig.upd("AAPL", 34, (34, 5.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.upd("AAPL", 35, (35, 2.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:33 not written: seen only in the backfill snapshot (no live update)" in rig.text
    assert rig.text.count("09:33 not written") == 1
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:32", "09:34"]
    assert "minutes not written: snapshot only 1, connection loss 0" in rig.text
    assert "first update not seen" not in rig.text


def test_a_snapshot_only_minute_closed_by_time_is_not_written_either(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 30)
    rig.backfills = {"AAPL": [AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)]]}
    rig.at(rig.tt(36, 1.0), lambda: None)  # past 09:33 + 60 s + 90 s: closed by time
    rig.end_at(3, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:33 not written: seen only in the backfill snapshot (no live update)" in rig.text
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:32"]
    assert "closed by time: no later" not in rig.text


def test_a_minute_with_one_live_update_after_the_snapshot_is_written(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 30)
    rig.backfills = {"AAPL": [AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.5, 99.9, 100.2, 1000)]]}
    rig.upd("AAPL", 33, (33, 45.0), (100.0, 100.8, 99.9, 100.7, 1400))
    rig.upd("AAPL", 34, (34, 2.0), (100.7, 100.7, 100.7, 100.7, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert rig.rows("AAPL")[3][2:] == ["100.0", "100.8", "99.9", "100.7", "1400"]
    assert not re.search(r"\d\d:\d\d not written", rig.text)


def test_a_backfill_after_data_loss_is_judged_at_the_time_its_request_was_sent(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 5.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.upd("AAPL", 35, (35, 5.0), (100.8, 100.8, 100.8, 100.8, 10))
    rig.drop((35, 58.0), 1101)  # the re-request goes out at 09:35:58, 2 s before 09:35 ends
    hold_history(rig, from_request=2)
    second = AAPL_BACKFILL[1:] + [(ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800),
                                  (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 2200),
                                  (ep(9, 35), 100.8, 101.0, 100.7, 100.9, 700)]
    deliver_backfill(rig, "AAPL", second, at=rig.tt(36, 3.0), after_request=2)  # arrives after 09:36:00
    rig.at(rig.tt(36, 3.5), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.upd("AAPL", 35, (36, 4.0), (100.8, 101.2, 100.7, 101.1, 1900))
    rig.upd("AAPL", 36, (36, 5.0), (101.1, 101.1, 101.1, 101.1, 5))
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:33", "09:34", "09:35"]
    assert rows[4][2:] == ["100.8", "101.2", "100.7", "101.1", "1900"]
    assert "700" not in rig.csv("AAPL").read_text(encoding="utf-8")


def test_a_late_backfill_after_data_loss_without_a_final_update_writes_no_partial_row(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 5.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.upd("AAPL", 35, (35, 5.0), (100.8, 100.8, 100.8, 100.8, 10))  # live update of 09:35 before the loss
    rig.drop((35, 58.0), 1101)
    hold_history(rig, from_request=2)
    second = AAPL_BACKFILL[1:] + [(ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800),
                                  (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 2200),
                                  (ep(9, 35), 100.8, 101.0, 100.7, 100.9, 700)]
    deliver_backfill(rig, "AAPL", second, at=rig.tt(36, 3.0), after_request=2)
    rig.at(rig.tt(36, 3.5), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.upd("AAPL", 36, (36, 5.0), (101.1, 101.1, 101.1, 101.1, 5))  # no final update of 09:35
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:35 not written: seen only in the backfill snapshot (no live update)" in rig.text
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:33", "09:34"]
    assert "700" not in rig.csv("AAPL").read_text(encoding="utf-8")


@pytest.mark.parametrize("seconds_after_the_minute,closed", [
    (watch_bars.CLOSE_GRACE, True),  # the minute ended exactly the grace period before the request
    (watch_bars.CLOSE_GRACE - 1, False),  # one second less: the bar may still be forming
])
def test_the_grace_period_decides_whether_the_last_backfill_bar_is_closed(
        rig: Rig, seconds_after_the_minute: int, closed: bool) -> None:
    """After a data loss, with a lag bound of 0 s measured from live updates, the re-requested backfill's
    last bar (09:34, no trade in 09:35) is closed at the request only if its minute ended `CLOSE_GRACE`
    s before it was sent."""
    rig.backfills = {"AAPL": [AAPL_BACKFILL, AAPL_BACKFILL[1:] + [
        (ep(9, 33), 100.0, 100.6, 99.9, 100.3, 1800), (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 2200)]]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 0.0), (100.3, 100.4, 100.3, 100.3, 100))  # first seen at 0.0 s: bound 0
    rig.drop((35, float(seconds_after_the_minute)), 1101)  # the re-request is sent at 09:35:0x
    rig.at(rig.tt(35, 10.0), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    if closed:
        assert "last 09:34; 1 new closed bars written" in rig.text
        assert "09:34  AAPL  O 100.3 H 100.9 L 100.2 C 100.8  volume 2200  (backfilled)" in rig.text
        assert [r[0][11:16] for r in rig.rows("AAPL")][-1] == "09:34"
    else:
        assert "last 09:34; 0 new closed bars written; the 09:34 bar is still forming" in rig.text
        assert "(backfilled)" not in rig.text


def test_data_lost_requests_are_capped_and_spaced_at_least_15_seconds_apart(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    sent = []
    original = rig._history  # noqa: SLF001

    def hist(*a: Any) -> list[tuple[Any, ...]]:
        sent.append(rig.t)
        return original(*a)

    rig._history = hist  # type: ignore[method-assign]  # noqa: SLF001
    for k in range(watch_bars.MAX_REREQUESTS + 1):  # one more loss than the cap allows
        rig.drop((34, float(k)), 1101,
                 ready=(lambda k=k: rig.text.count("re-requested backfill") == k))
    rig.end_at(10, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=10) == watch_bars.EXIT_FAILED, rig.text
    assert len(sent) == watch_bars.MAX_REREQUESTS + 1  # the first request and ten more, no eleventh
    assert all(b - a >= watch_bars.IDENTICAL_REQUEST_SPACING for a, b in zip(sent, sent[1:], strict=False))
    assert f"data lost {watch_bars.MAX_REREQUESTS + 1} times" in rig.text
    assert f"re-requests after data loss: {watch_bars.MAX_REREQUESTS}" in rig.text


def test_all_history_requests_of_all_symbols_are_spaced_at_least_a_second(rig: Rig) -> None:
    sent = hold_history(rig, from_request=10_000)  # only records the requests
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("MSFT", 33, (33, 35.0), (400.0, 400.5, 399.9, 400.2, 1000))
    rig.drop((35, 0.0), 1101)  # both streams lose their data at once
    rig.upd("AAPL", 36, (36, 5.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.upd("MSFT", 36, (36, 10.0), (400.6, 400.6, 400.6, 400.6, 1))
    rig.end_at(4)
    assert rig.watch(minutes=4) == watch_bars.EXIT_OK, rig.text
    assert [s for s, _ in sent].count("AAPL") == 2 and [s for s, _ in sent].count("MSFT") == 2
    times = sorted(t for _, t in sent)
    assert all(b - a >= watch_bars.REQUEST_SPACING for a, b in zip(times, times[1:], strict=False)), sent
    for name in ("AAPL", "MSFT"):
        own = [t for s, t in sent if s == name]
        assert own[1] - own[0] >= watch_bars.IDENTICAL_REQUEST_SPACING


def quiet_minute_rig(rig: Rig, revised: tuple[float, float, float, float, int]) -> None:
    """09:33 has one live update (at 09:33:01, so the lag bound is 1 s), then no trade in 09:34: it is
    closed by time at 09:35:31; IB then sends a revision of it, and a bar of 09:35 and 09:36."""
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 33, (33, 50.0), (100.0, 100.5, 99.9, 100.2, 1000))  # 10 s before the end: not stale
    rig.at(rig.tt(35, 32.0), lambda: None)  # 09:33 is closed by time at 09:35:31
    rig.upd("AAPL", 33, (35, 40.0), revised)  # IB revises it afterwards
    rig.upd("AAPL", 35, (35, 45.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.upd("AAPL", 36, (36, 1.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.end_at(4, symbols=1)


def test_a_quiet_minute_closed_by_time_then_revised_says_the_file_keeps_the_earlier_values(rig: Rig) -> None:
    quiet_minute_rig(rig, (100.0, 100.7, 99.9, 100.6, 1300))
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert "09:33  AAPL" in rig.text and "closed by time" in rig.text
    assert rig.rows("AAPL")[2][2:] == ["100.0", "100.5", "99.9", "100.2", "1000"]
    late = [ln for ln in rig.text.splitlines() if "late revision for 09:33" in ln]
    assert len(late) == 1
    assert "the file keeps the earlier values" in late[0]
    assert "written O 100.0 H 100.5 L 99.9 C 100.2 volume 1000" in late[0]
    assert "newer O 100.0 H 100.7 L 99.9 C 100.6 volume 1300" in late[0]
    assert len([ln for ln in rig.closed_lines("AAPL") if ln.startswith("09:33")]) == 1  # emitted once
    assert "rows that may be stale: 1" in rig.text and "late revisions: 1" in rig.text


def test_a_revision_identical_to_the_written_row_is_not_called_stale(rig: Rig) -> None:
    quiet_minute_rig(rig, (100.0, 100.5, 99.9, 100.2, 1000))
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert "identical to the bar already emitted" in rig.text and "rows that may be stale: 0" in rig.text


# -- delayed data -----------------------------------------------------------------------------


def delayed_rig(rig: Rig, minutes: int) -> int:
    """The reviewer's scenario: IB's clock says 10:00:25 and the newest bar is 09:45 (15 min behind)."""
    rig.server_epoch = ep(10, 0, 25)
    rig.backfills = {"AAPL": [[(ep(9, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(36, 46)]]}
    upd_at(rig, "AAPL", ep(9, 45), ep(10, 0, 30), (99.0, 99.6, 98.8, 99.3, 150))
    upd_at(rig, "AAPL", ep(9, 46), ep(10, 1, 1), (99.3, 99.3, 99.3, 99.3, 5))
    upd_at(rig, "AAPL", ep(9, 46), ep(10, 1, 30), (99.3, 99.9, 99.2, 99.8, 800))
    upd_at(rig, "AAPL", ep(9, 47), ep(10, 2, 1), (99.8, 99.8, 99.8, 99.8, 5))
    upd_at(rig, "AAPL", ep(9, 47), ep(10, 2, 50), (99.8, 99.9, 99.7, 99.85, 600))
    rig.end_at(minutes, symbols=1)
    return rig.watch(symbols="AAPL", minutes=minutes)


def test_delayed_data_is_declared_once_and_no_partial_row_is_written(rig: Rig) -> None:
    assert delayed_rig(rig, 3) == watch_bars.EXIT_OK, rig.text
    text = rig.text
    declared = [ln for ln in text.splitlines() if "behind IB's clock: this looks like delayed" in ln]
    assert declared == ["AAPL: bars are about 15 minutes behind IB's clock: this looks like delayed "
                        "market data (the paper account may not share the live account's real-time data)"]
    assert "late revision for" not in text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == [f"09:{m}" for m in range(36, 47)]  # 09:47 is still forming
    assert rows[-2][2:] == ["99.0", "99.6", "98.8", "99.3", "150"]  # 09:45 as its last update left it
    assert rows[-1][2:] == ["99.3", "99.9", "99.2", "99.8", "800"]
    assert "still forming, not written: 09:47" in text
    printed = rig.closed_lines("AAPL")
    assert printed and all("(delayed about 15 min)" in ln for ln in printed), printed
    summary = text[text.index("Summary"):]
    assert "AAPL: streaming (delayed about 15 min)" in summary and "stream: DELAYED" in summary
    assert "LIVE" not in summary


def test_a_delayed_bar_with_no_later_update_is_closed_by_the_delayed_clock(rig: Rig) -> None:
    # 5 minutes: IB's clock reaches 10:05:25, the delayed clock about 09:50:24; 09:47 ended at 09:48:00
    assert delayed_rig(rig, 5) == watch_bars.EXIT_OK, rig.text
    line = [ln for ln in rig.text.splitlines() if ln.startswith("09:47  AAPL")]
    assert len(line) == 1 and "closed by time" in line[0] and "(delayed about 15 min)" in line[0]
    assert rig.rows("AAPL")[-1][2:] == ["99.8", "99.9", "99.7", "99.85", "600"]


def test_a_delayed_bar_is_not_closed_by_time_while_the_delayed_clock_has_not_reached_it(rig: Rig) -> None:
    # IB's clock is past 09:47 + 90 s by 15 minutes, but the delayed clock is not: 3 minutes, no row
    assert delayed_rig(rig, 3) == watch_bars.EXIT_OK, rig.text
    assert not [ln for ln in rig.closed_lines("AAPL") if ln.startswith("09:47")]
    assert "closed by time: no later update" not in rig.text and "closed by time 0" in rig.text
    assert rig.rows("AAPL")[-1][0][11:16] == "09:46"


def test_a_live_stream_is_not_called_delayed(rig: Rig) -> None:
    rig.standard()
    assert rig.watch() == watch_bars.EXIT_OK, rig.text
    assert "delayed" not in rig.text and "market closed" not in rig.text
    assert "stream: LIVE, bars at most" in rig.text


def test_a_start_before_the_open_says_market_closed_and_writes_the_previous_session_nowhere(rig: Rig) -> None:
    rig.server_epoch = ep(8, 0, 0)
    friday = [int(datetime(2024, 8, 15, 15, m, tzinfo=NY).timestamp()) for m in (57, 58, 59)]
    rig.backfills = {"AAPL": [[(e, 99.0, 99.5, 98.8, 99.2, 100) for e in friday]]}
    rig.end_at(1, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=1) == watch_bars.EXIT_OK, rig.text
    assert "AAPL: market closed: no new bars expected" in rig.text
    assert rig.text.count("market closed") == 1
    assert "AAPL: backfill holds 3 bars of the previous session: not written" in rig.text
    assert "looks like delayed" not in rig.text
    assert not list(rig.live.rglob("*.csv"))  # nothing, in particular no file of the previous day


def test_a_start_just_after_the_open_writes_only_todays_backfill_bars(rig: Rig) -> None:
    yesterday = [int(datetime(2024, 8, 15, 15, m, tzinfo=NY).timestamp()) for m in (58, 59)]
    rig.backfills = {"AAPL": [[(e, 99.0, 99.5, 98.8, 99.2, 100) for e in yesterday] + AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "AAPL: backfill holds 2 bars of the previous session: not written" in rig.text
    assert [r[0][:10] for r in rig.rows("AAPL")] == ["2024-08-16"] * 3
    assert not (rig.live / "AAPL" / "AAPL_live_2024-08-15.csv").exists()
    assert "stream: LIVE" in rig.text


def test_the_market_closing_during_the_watch_is_said_once_and_the_last_bar_still_closes(rig: Rig) -> None:
    rig.server_epoch = ep(15, 58, 30)
    rig.backfills = {"AAPL": [[(ep(15, 56), 100.0, 101.0, 99.0, 100.5, 10),
                               (ep(15, 57), 100.5, 101.0, 100.0, 100.8, 10)]]}
    rig.upd("AAPL", 58, (58, 40.0), (100.8, 101.0, 100.7, 100.9, 50), hh=15)
    rig.upd("AAPL", 59, (59, 1.0), (100.9, 100.9, 100.9, 100.9, 5), hh=15)
    rig.upd("AAPL", 59, (59, 59.0), (100.9, 101.5, 100.5, 101.4, 90000), hh=15)
    rig.at(rig.tt(0, 30.0, hh=16), lambda: None)  # a pass over the symbols after 16:00
    rig.at(rig.tt(1, 0.0, hh=16), lambda: None)  # and a second one: still said once
    rig.at(rig.tt(2, 0.0, hh=16), lambda: None)  # 16:02: past 15:59 + 60 s + 90 s ...
    seen: list[bool] = []
    rig.at(rig.tt(19, 59.0, hh=16), lambda: seen.append("15:59  AAPL" in rig.text))  # ... but not 16:20
    rig.end_at(24, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=24) == watch_bars.EXIT_OK, rig.text
    assert seen == [False]  # the last bars of a session wait for 16:20 server time
    assert rig.text.count("market closed: no new bars expected") == 1
    assert rig.rows("AAPL")[-1][2:] == ["100.9", "101.5", "100.5", "101.4", "90000"]
    assert "closed by time" in [ln for ln in rig.text.splitlines() if ln.startswith("15:59  AAPL")][0]
    assert "looks like delayed" not in rig.text


def test_a_bar_of_the_last_minutes_is_not_closed_by_time_before_16_20(rig: Rig) -> None:
    rig.server_epoch = ep(15, 58, 30)
    rig.backfills = {"AAPL": [[(ep(15, 56), 100.0, 101.0, 99.0, 100.5, 10)]]}
    rig.upd("AAPL", 59, (59, 1.0), (100.9, 100.9, 100.9, 100.9, 5), hh=15)
    rig.end_at(19, symbols=1)  # the watch ends at 16:17:30 server time
    assert rig.watch(symbols="AAPL", minutes=19) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 15:56 not written: seen only in the backfill snapshot" in rig.text
    assert "15:59  AAPL" not in rig.text and "still forming, not written: 15:59" in rig.text


# -- outages ----------------------------------------------------------------------------------


def test_nothing_is_closed_by_time_while_the_connection_is_lost_and_the_bar_is_not_written(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.at(rig.tt(35, 35.0), lambda: None, ready=lambda: "connectivity LOST" in rig.text)  # 09:33 is overdue
    rig.drop((35, 40.0), 1102)
    rig.at(rig.tt(35, 42.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.upd("AAPL", 33, (35, 45.0), (100.0, 100.6, 99.9, 100.4, 1700))  # what IB held back, after the restore
    rig.upd("AAPL", 35, (35, 50.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.upd("AAPL", 36, (36, 1.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    text = rig.text
    assert "closed by time" not in text.split("Summary")[0].replace("no bar is closed by time", "")
    assert "AAPL 09:33 not written: spanned a connection loss" in text
    assert "AAPL 09:35 not written: spanned a connection loss" in text  # 09:35 ran into the restore
    assert text.index("connectivity restored") < text.index("AAPL 09:33 not written")
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31"]
    assert "connection loss 2" in text


def test_a_bar_whose_last_update_came_before_an_outage_is_not_written_when_a_later_update_closes_it(
        rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.drop((34, 20.0), 1102, ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(34, 21.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.upd("AAPL", 34, (34, 25.0), (100.3, 100.3, 100.3, 100.3, 5))  # 09:33's trades after 33:40 are missing
    rig.upd("AAPL", 35, (35, 1.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(3, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:33 not written: spanned a connection loss" in rig.text
    assert "09:33  AAPL  O" not in rig.text
    assert "09:33" not in "".join(r[0] for r in rig.rows("AAPL"))


def test_a_spanned_bar_that_nothing_closes_is_not_written_by_time_either(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.drop((35, 40.0), 1102, ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(35, 42.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.at(rig.tt(37, 0.0), lambda: None)
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:33 not written: spanned a connection loss" in rig.text
    assert "closed by time: no later" not in rig.text
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31"]


def test_connection_codes_1100_to_1102_are_printed_as_notices_not_errors(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.drop((33, 50.0), 1102, ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(33, 55.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    rig.end_at(2, symbols=1)
    rig.watch(symbols="AAPL", minutes=2)
    assert re.search(r"IB notice 1100: ", rig.text) and re.search(r"IB notice 1102: ", rig.text)
    assert not re.search(r"IB error 110[012]", rig.text)


# -- statistics -------------------------------------------------------------------------------


def test_a_negative_offset_is_printed_with_a_single_sign(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (33, 59.7), (100.2, 100.2, 100.2, 100.2, 5))  # IB's clock, 1 s resolution, is behind
    rig.upd("AAPL", 35, (34, 59.5), (100.3, 100.3, 100.3, 100.3, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "first seen -0.3 s after the minute started" in rig.text
    assert "+-" not in rig.text and "-+" not in rig.text


def test_closes_after_a_skipped_minute_are_left_out_of_the_close_delay_statistics(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 36, (36, 2.0), (101.0, 101.0, 101.0, 101.0, 10))  # 09:34 and 09:35 have no bar
    rig.upd("AAPL", 37, (37, 3.0), (101.0, 101.0, 101.0, 101.0, 10))
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    line = [ln for ln in rig.text.splitlines() if ln.startswith("09:33  AAPL")][0]
    assert "closed by the 09:36 update, after 2 minutes without a bar" in line
    assert "closed after the minute ended, s (min / median / max): 3.0 / 3.0 / 3.0" in rig.text  # 09:36 only
    assert "re-request gap 0; closes left out of the close-delay statistics: skipped minute 1" in rig.text


# -- second review: a row is a finished bar with final values, or there is no row ---------------


def minutes_in(rig: Rig, symbol: str = "AAPL") -> dict[str, list[str]]:
    """minute -> [open, high, low, close, volume] of every row of the symbol's live file today."""
    path = rig.csv(symbol)
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == HEADER
    return {ln.split(",")[0][11:16]: ln.split(",")[2:] for ln in lines[1:]}


def delayed_stream(rig: Rig, *, server: int, snapshot_minutes: range, hh: int, lag: int, last_volume: int,
                   bars: tuple[int, ...], minutes: int) -> int:  # `bars`: epochs of the bars that update
    """A stream `lag` s behind IB's clock: `snapshot_minutes` of closed bars, then the forming bar
    `snapshot_minutes.stop` (volume `last_volume`), then three updates per bar of `bars` (volumes
    10, 300, 900) delivered `lag` s after the data time. Returns the exit code."""
    rig.server_epoch = server
    last = snapshot_minutes.stop
    rig.backfills = {"AAPL": [[(ep(hh, m), 99.0, 99.5, 98.8, 99.2, 100) for m in snapshot_minutes]
                              + [(ep(hh, last), 99.0, 99.5, 98.8, 99.2, last_volume)]]}
    for start in bars:
        for seconds, volume in ((5, 10), (30, 300), (55, 900)):
            upd_at(rig, "AAPL", start, start + seconds + lag, (99.0, 99.5, 98.8, 99.2, volume))
    rig.end_at(minutes, symbols=1)
    return rig.watch(symbols="AAPL", minutes=minutes)


def test_a_stream_100_seconds_behind_is_not_closed_by_time_while_its_bar_is_still_forming(rig: Rig) -> None:
    lag = 100
    rig.server_epoch = ep(10, 0, 40)  # the data clock reads 09:59:00
    rig.backfills = {"AAPL": [[(ep(9, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(50, 59)]
                              + [(ep(9, 59), 99.0, 99.5, 98.8, 99.2, 1)]]}
    upd_at(rig, "AAPL", ep(9, 59), ep(9, 59, 30) + lag, (99.0, 99.5, 98.8, 99.2, 300))
    upd_at(rig, "AAPL", ep(9, 59), ep(9, 59, 55) + lag, (99.0, 99.5, 98.8, 99.2, 900))
    for minute in (0, 1):
        start = ep(10, minute)
        upd_at(rig, "AAPL", start, start + 5 + lag, (99.0, 99.5, 98.8, 99.2, 10))
        rig.at(start + 151 - rig.server_epoch, lambda: None)  # a pass at server M+151: data M+51, forming
        upd_at(rig, "AAPL", start, start + 55 + lag, (99.0, 99.5, 98.8, 99.2, 900))
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert all(row[-1] == "900" for minute, row in minutes_in(rig).items() if minute >= "09:59"), rig.text


def test_a_delayed_stream_started_after_16_00_never_writes_a_partial_row(rig: Rig) -> None:
    """Server 16:05:20, data clock 15:50: the snapshot's 15:50 bar (volume 40) is forming at the request."""
    code = delayed_stream(rig, server=ep(16, 5, 20), snapshot_minutes=range(41, 50), hh=15, lag=920,
                          last_volume=40, bars=(ep(15, 50), ep(15, 51), ep(15, 52)), minutes=4)
    assert code == 0, rig.text
    rows = minutes_in(rig)
    assert rows.get("15:50", ["", "", "", "", "900"])[-1] == "900", rows
    assert rows.get("15:51", ["", "", "", "", "900"])[-1] == "900", rows
    assert "15:52" not in rows  # still forming at the end of the watch
    assert "late revision for" not in rig.text


def test_a_snapshot_only_minute_of_a_delayed_stream_is_not_written(rig: Rig) -> None:
    """Lag 125 s: the snapshot's 09:59 bar (volume 30) gets no live update, so it is not written."""
    code = delayed_stream(rig, server=ep(10, 1, 10), snapshot_minutes=range(50, 59), hh=9, lag=125,
                          last_volume=30, bars=(ep(10, 0), ep(10, 1), ep(10, 2)), minutes=5)
    assert code == 0, rig.text
    assert "AAPL 09:59 not written: seen only in the backfill snapshot (no live update)" in rig.text
    rows = minutes_in(rig)
    assert "09:59" not in rows
    assert all(row[-1] == "900" for minute, row in rows.items() if minute.startswith("10:")), rows
    assert "first update not seen" not in rig.text


# -- the DELAYED label ------------------------------------------------------------------------------


def label_stream(rig: Rig, *, server: int, snapshot_last: int, updates: list[tuple[int, int, int]]) -> None:
    """A snapshot of 09:50 to `snapshot_last` (forming) at `server`, then live updates (bar minute at 10:xx
    or 09:xx as an epoch, seconds after 10:00:00 it arrives, volume)."""
    rig.server_epoch = server
    rig.backfills = {"AAPL": [[(ep(9, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(50, snapshot_last)]
                              + [(ep(9, snapshot_last), 99.0, 99.5, 98.8, 99.2, 30)]]}
    for bar_epoch, when, volume in updates:
        upd_at(rig, "AAPL", bar_epoch, ep(10, 0) + when, (99.0, 99.5, 98.8, 99.2, volume))
    rig.end_at(3, symbols=1)


@pytest.mark.parametrize("live_arrival,declared", [(120, False), (121, True)])
def test_the_label_threshold_is_a_lag_of_more_than_120_seconds(rig: Rig, live_arrival: int,
                                                               declared: bool) -> None:
    # the snapshot at 10:01:10 holds 09:59 as its newest bar (130 s behind: over); the live update of
    # the 10:00 bar arrives 120 s or 121 s after that bar started
    label_stream(rig, server=ep(10, 1, 10), snapshot_last=59, updates=[(ep(10, 0), live_arrival, 50)])
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert ("looks like delayed" in rig.text) is declared


def test_one_live_update_confirms_a_backfill_that_showed_the_lag(rig: Rig) -> None:
    label_stream(rig, server=ep(10, 1, 10), snapshot_last=59, updates=[(ep(10, 0), 130, 50)])
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("looks like delayed") == 1


def test_a_backfill_alone_does_not_declare_delayed(rig: Rig) -> None:
    label_stream(rig, server=ep(10, 1, 10), snapshot_last=59, updates=[])
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert "looks like delayed" not in rig.text


@pytest.mark.parametrize("second_when,declared", [(80, True), (75, False)])
def test_two_live_updates_10_seconds_apart_declare_delayed_without_the_backfill(
        rig: Rig, second_when: int, declared: bool) -> None:
    # the snapshot at 10:00:25 shows 85 s (not over); the 09:59 bar's updates arrive at 10:01:10 (130 s
    # behind the bar) and then 10:01:20 (10 s later: declared) or 10:01:15 (5 s later: not yet)
    label_stream(rig, server=ep(10, 0, 25), snapshot_last=59, updates=[
        (ep(9, 59), 70, 40), (ep(9, 59), second_when, 60)])
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert ("looks like delayed" in rig.text) is declared


def test_a_quiet_symbol_is_not_called_delayed_and_then_current_again(rig: Rig) -> None:
    """The newest snapshot bar is 3 minutes old (the symbol did not trade), then its current bar arrives."""
    rig.server_epoch = ep(10, 5, 30)
    rig.backfills = {"AAPL": [[(ep(9, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(56, 60)]
                              + [(ep(10, 0), 99.0, 99.5, 98.8, 99.2, 100),
                                 (ep(10, 2), 99.0, 99.5, 98.8, 99.2, 100)]]}
    upd_at(rig, "AAPL", ep(10, 5), ep(10, 5, 40), (99.1, 99.1, 99.1, 99.1, 5))
    upd_at(rig, "AAPL", ep(10, 6), ep(10, 6, 2), (99.1, 99.1, 99.1, 99.1, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "delayed" not in rig.text.split("Summary")[0] and "current again" not in rig.text


def test_a_delayed_stream_that_becomes_current_says_so_and_stops_being_labelled(rig: Rig) -> None:
    rig.server_epoch = ep(10, 0, 25)
    rig.backfills = {"AAPL": [[(ep(9, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(36, 46)]]}
    upd_at(rig, "AAPL", ep(9, 45), ep(10, 0, 30), (99.0, 99.6, 98.8, 99.3, 150))  # 15 min behind: declared
    upd_at(rig, "AAPL", ep(10, 0), ep(10, 0, 40), (99.0, 99.6, 98.8, 99.3, 1))  # a current bar
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("looks like delayed") == 1
    current = "AAPL: bars are current again (at most 40 s behind IB's clock): treating the stream as live"
    assert current in rig.text
    assert "AAPL: streaming (delayed" not in rig.text and "stream: LIVE" in rig.text


# -- wording ---------------------------------------------------------------------------------------


def test_a_delayed_stream_says_the_session_ended_and_bars_continue(rig: Rig) -> None:
    rig.server_epoch = ep(15, 59, 20)  # the data clock reads 15:44:20
    rig.backfills = {"AAPL": [[(ep(15, m), 99.0, 99.5, 98.8, 99.2, 100) for m in range(35, 44)]
                              + [(ep(15, 44), 99.0, 99.5, 98.8, 99.2, 40)]]}
    upd_at(rig, "AAPL", ep(15, 44), ep(15, 59, 30), (99.0, 99.5, 98.8, 99.2, 150))
    upd_at(rig, "AAPL", ep(15, 45), ep(16, 0, 1), (99.0, 99.5, 98.8, 99.2, 10))
    upd_at(rig, "AAPL", ep(15, 45), ep(16, 0, 31), (99.0, 99.5, 98.8, 99.2, 900))
    upd_at(rig, "AAPL", ep(15, 46), ep(16, 1, 1), (99.0, 99.5, 98.8, 99.2, 10))
    rig.end_at(3, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_OK, rig.text
    assert "AAPL: regular session ended; delayed bars continue for about 15 minutes" in rig.text
    assert "market closed" not in rig.text
    rows = minutes_in(rig)
    assert rows["15:44"][-1] == "150" and rows["15:45"][-1] == "900"


def test_a_start_in_the_session_with_no_bar_of_today_yet_says_why(rig: Rig) -> None:
    rig.server_epoch = ep(9, 31, 0)
    yesterday = [int(datetime(2024, 8, 15, 15, m, tzinfo=NY).timestamp()) for m in range(50, 60)]
    rig.backfills = {"AAPL": [[(e, 99.0, 99.5, 98.8, 99.2, 100) for e in yesterday]]}
    upd_at(rig, "AAPL", yesterday[-1], ep(9, 31, 5), (99.0, 99.5, 98.8, 99.2, 100))  # an update of yesterday
    upd_at(rig, "AAPL", ep(9, 30), ep(9, 45, 3), (99.0, 99.5, 98.8, 99.2, 10))
    upd_at(rig, "AAPL", ep(9, 30), ep(9, 45, 50), (99.0, 99.5, 98.8, 99.2, 900))
    upd_at(rig, "AAPL", ep(9, 31), ep(9, 46, 3), (99.0, 99.5, 98.8, 99.2, 10))
    rig.end_at(16, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=16) == watch_bars.EXIT_OK, rig.text
    note = ("AAPL: no bars of today's session yet; with delayed data the first bars appear about "
            "15 minutes after the open")
    assert rig.text.count(note) == 1
    assert not rig.csv("AAPL", "2024-08-15").exists(), "an update of the previous session was written"
    assert "AAPL: update for 15:59 of the previous session ignored: not written" in rig.text
    assert minutes_in(rig)["09:30"][-1] == "900"
    assert "looks like delayed" in rig.text


def test_a_start_before_the_open_does_not_say_no_bars_yet(rig: Rig) -> None:
    rig.server_epoch = ep(8, 0, 0)
    rig.end_at(1, symbols=1)
    rig.watch(symbols="AAPL", minutes=1)
    assert "no bars of today's session yet" not in rig.text and "market closed" in rig.text


# -- a live file that cannot be used stops only that symbol --------------------------------------------


def msft_only(rig: Rig) -> None:
    """The standard stream of MSFT only (nothing is scripted for the symbol that is stopped)."""
    for k in range(33, 38):
        o = 400.0 + (k - 33)
        rig.upd("MSFT", k, (k, 35.0) if k == 33 else (k, 3.0), (o, o + 0.5, o - 0.5, o + 0.2, 100 * k))
        if k < 37:
            rig.upd("MSFT", k, (k, 50.0 if k == 33 else 30.0), (o, o + 0.5, o - 0.5, o + 0.4, 200 * k))
    rig.end_at(5)


def stopped_symbol_checks(rig: Rig, code: int, *, excel: bool = True) -> None:
    assert code == watch_bars.EXIT_FAILED, rig.text
    assert "Unexpected" not in rig.text and "Traceback" not in rig.text
    lines = [ln for ln in rig.text.splitlines() if "AAPL_live_2024-08-16.csv" in ln and "stopped" in ln]
    assert len(lines) == 1 and lines[0].startswith("AAPL: ")
    assert lines[0].count("AAPL_live_2024-08-16.csv") == 1  # the path once
    assert "run again" in lines[0]
    assert ("close it in other programs (for example Excel)" in lines[0]) is excel
    assert "AAPL: stopped: file not writable" in rig.text
    cancels = [c.args[0] for c in rig.fakes[-1].calls_named("cancelHistoricalData")]
    assert cancels.count(rig.req_ids["AAPL"]) == 1  # cancelled once, never polled again
    assert len(minutes_in(rig, "MSFT")) >= 6  # the other symbol kept streaming


def test_a_live_file_that_cannot_be_opened_stops_only_that_symbol(rig: Rig) -> None:
    (rig.live / "AAPL" / "AAPL_live_2024-08-16.csv").mkdir(parents=True)  # as if open in another program
    msft_only(rig)
    stopped_symbol_checks(rig, rig.watch())


@pytest.mark.parametrize("tail", ["2024-08-16 09:3", "2024-08-16 09:32:00," + str(ep(9, 32)) + ",99.8,100"])
def test_a_damaged_last_row_stops_only_that_symbol_and_leaves_the_file_alone(rig: Rig, tail: str) -> None:
    path = rig.csv("AAPL")
    path.parent.mkdir(parents=True)
    row = "2024-08-16 09:31:00," + str(ep(9, 31)) + ",99.2,99.9,99.1,99.8,4000"
    text = HEADER + "\n" + row + "\n" + tail
    path.write_text(text, encoding="utf-8", newline="\n")
    msft_only(rig)
    stopped_symbol_checks(rig, rig.watch(), excel=False)
    assert path.read_text(encoding="utf-8") == text  # untouched


# -- a re-requested backfill that is still on its way ---------------------------------------------------


def pending_rerequest(rig: Rig, *, closes_first: bool) -> None:
    """09:33 has a live update (volume 1000) at 09:33:01; 1101 at 09:34:30 (no 1100), and the re-requested
    backfill, which holds 09:33's final values (volume 2500), takes 80 s (inside the 90 s timeout)."""
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    if closes_first:
        rig.upd("AAPL", 34, (34, 1.0), (100.3, 100.3, 100.3, 100.3, 5))  # 09:33 is written with 1000
    rig.drop((34, 30.0), 1101)
    hold_history(rig, from_request=2)
    second = AAPL_BACKFILL + [(ep(9, 33), 100.0, 100.9, 99.9, 100.8, 2500),
                              (ep(9, 34), 100.3, 100.9, 100.2, 100.8, 300)]
    deliver_backfill(rig, "AAPL", second, at=rig.tt(35, 50.0), after_request=2)  # 09:33 was due at 09:35:31
    rig.at(rig.tt(35, 51.0), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.upd("AAPL", 34, (35, 55.0), (100.3, 100.9, 100.2, 100.8, 600))
    rig.upd("AAPL", 35, (36, 2.0), (100.8, 100.8, 100.8, 100.8, 5))
    rig.end_at(4, symbols=1)


def test_nothing_is_closed_by_time_while_a_rerequested_backfill_is_on_its_way(rig: Rig) -> None:
    pending_rerequest(rig, closes_first=False)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert minutes_in(rig)["09:33"] == ["100.0", "100.9", "99.9", "100.8", "2500"]  # the backfill's values
    assert "closed by time: no later update" not in rig.text


def test_a_rerequested_backfill_that_differs_from_a_written_row_says_so(rig: Rig) -> None:
    pending_rerequest(rig, closes_first=True)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert minutes_in(rig)["09:33"] == ["100.0", "100.5", "99.9", "100.2", "1000"]  # not rewritten
    line = [ln for ln in rig.text.splitlines() if "re-requested backfill differs for 09:33" in ln]
    assert len(line) == 1 and "the file keeps the earlier values" in line[0]
    assert "volume 1000" in line[0] and "volume 2500" in line[0]
    assert "rows that may be stale: 1" in rig.text


# -- the snapshot, the outage and the date rules ---------------------------------------------------------


def test_a_minute_closed_by_a_later_snapshot_bar_is_written_without_the_forming_label(rig: Rig) -> None:
    code = delayed_stream(rig, server=ep(10, 0, 25), snapshot_minutes=range(36, 45), hh=9, lag=900,
                          last_volume=40, bars=(ep(9, 45), ep(9, 46)), minutes=3)
    assert code == 0, rig.text
    assert minutes_in(rig)["09:44"][-1] == "100"  # a later bar exists in the snapshot: final
    assert "09:44  AAPL  O" not in rig.text and "(updates 0" not in rig.text  # closed at the request: silent


# -- the recording of a run that never connected -----------------------------------------------


def test_a_refused_run_leaves_no_header_only_recording_behind(rig: Rig) -> None:
    base = rig.connection_factory

    def refusing(args: Any, observers: list[Any]) -> IbConnection:
        conn = base(args, observers)

        def open_refused() -> None:
            raise BrokerPortRefused("refused by the test: nothing was sent")

        conn.open = open_refused  # type: ignore[method-assign]
        return conn

    rig.connection_factory = refusing  # type: ignore[method-assign]
    assert rig.watch(record=True) == watch_bars.EXIT_REFUSED
    assert rig.recorders  # a recording was started ...
    assert not list(rig.tmp.rglob("*.jsonl"))  # ... and, holding only its header, deleted again
    assert "Recording:" not in rig.text and "REFUSED" in rig.text


@pytest.mark.parametrize("behavior", [ConnectBehavior.REFUSE, ConnectBehavior.CLIENT_ID_IN_USE,
                                      ConnectBehavior.NO_NEXT_VALID_ID,
                                      ConnectBehavior.CLOSED_IN_HANDSHAKE])
def test_a_failed_connect_never_leaves_a_header_only_recording_behind(
        rig: Rig, behavior: ConnectBehavior) -> None:
    rig.behavior = behavior
    assert rig.watch(record=True) == watch_bars.EXIT_NO_CONNECTION
    for path in rig.tmp.rglob("*.jsonl"):  # what stays holds events: it is checked and reported
        assert watch_bars._header_only(path) is False  # noqa: SLF001
        assert "Recording check:" in rig.text
    assert "Recording:" in rig.text or not list(rig.tmp.rglob("*.jsonl"))


def test_header_only_means_the_session_header_and_no_event(tmp_path: Path) -> None:
    one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    one.write_text('{"header": 1}\n', encoding="utf-8")
    two.write_text('{"header": 1}\n{"event": 2}\n', encoding="utf-8")
    assert watch_bars._header_only(one) is True and watch_bars._header_only(two) is False  # noqa: SLF001
    assert watch_bars._header_only(tmp_path / "missing.jsonl") is False  # noqa: SLF001


def test_a_recording_with_events_is_kept_and_checked_after_a_run(rig: Rig) -> None:
    rig.standard()
    assert rig.watch(record=True) == watch_bars.EXIT_OK
    kept = list((rig.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
    assert len(kept) == 1 and "Recording check: 0 finding(s)" in rig.text


# -- the documentation names the paper port ---------------------------------------------------


def test_the_docstring_and_the_help_show_the_paper_port_and_no_live_example(
        capsys: pytest.CaptureFixture[str]) -> None:
    doc = watch_bars.__doc__ or ""
    assert "--paper --symbols AAPL,MSFT,TSLA" in doc and "7496" not in doc
    with pytest.raises(SystemExit):
        watch_bars.parse_args(["--help"])
    shown = capsys.readouterr().out
    assert "--paper --symbols AAPL,MSFT,TSLA" in shown and "7496" not in shown
    assert "own opt-in" in shown and "STOINK_ALLOW_LIVE_PORT" not in shown


# -- third review: file encoding, farm breaks, stalls, wording, paper mode ---------------------------


def test_a_live_file_that_is_not_utf8_stops_only_that_symbol_and_is_left_alone(rig: Rig) -> None:
    path = rig.csv("AAPL")
    path.parent.mkdir(parents=True)
    row = f"2024-08-16 09:31:00,{ep(9, 31)},99.2,99.9,99.1,99.8,4000\n".encode()
    content = HEADER.encode() + b"\n" + row + b"2024-08-16 09:32:00,x\xe9\xe8,1,2,3,4,5\n"
    path.write_bytes(content)
    msft_only(rig)
    stopped_symbol_checks(rig, rig.watch(), excel=False)
    assert "UnicodeDecodeError" in rig.text and "Unexpected" not in rig.text
    assert path.read_bytes() == content


def farm_rig(rig: Rig, broken: tuple[int, str], ok: int | None) -> None:
    """09:33 and 09:34 are updated (09:34 last at 09:34:15), the farm breaks at 09:34:20 and is
    reported OK again at 09:37:10 (or never). 09:34 is overdue for a time close from 09:35:31."""
    rig.LIMIT = 300  # a gate that never opens fails in seconds, not minutes
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 33, (33, 50.0), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 1.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.upd("AAPL", 34, (34, 15.0), (100.3, 100.9, 100.2, 100.5, 50))
    code, text = broken
    rig.at(rig.tt(34, 20.0), lambda: rig.fakes[-1].drop(code, text))
    seen: list[bool] = []
    rig.at(rig.tt(36, 0.0),
           lambda: seen.append("09:34  AAPL" in rig.text or "AAPL 09:34 not written" in rig.text),
           ready=lambda: f"data farm break ({code})" in rig.text)
    rig.farm_seen = seen  # type: ignore[attr-defined]
    if ok is not None:
        ok_text = {2104: "Market data farm connection is OK:usfarm",
                   2106: "HMDS data farm connection is OK:ushmds"}
        rig.at(rig.tt(37, 10.0), lambda: rig.fakes[-1].drop(ok, ok_text.get(ok)), ready=lambda: bool(seen))
        rig.at(rig.tt(37, 20.0), lambda: None, ready=lambda: "AAPL 09:34 not written" in rig.text)
    rig.end_at(5, symbols=1)


FARM_CASES = [((2105, "HMDS data farm connection is broken:ushmds"), 2106),
              ((2103, "Market data farm connection is broken:usfarm"), 2104),
              ((2105, "HMDS data farm connection is broken:ushmds"), 1102)]


@pytest.mark.parametrize("broken,ok", FARM_CASES)
def test_a_minute_that_overlapped_a_data_farm_break_is_not_written_and_nothing_closes_during_it(
        rig: Rig, broken: tuple[int, str], ok: int) -> None:
    farm_rig(rig, broken, ok)
    assert rig.watch(symbols="AAPL", minutes=5) == watch_bars.EXIT_OK, rig.text
    assert rig.farm_seen == [False]  # type: ignore[attr-defined]  # not closed by time during the break
    assert "AAPL 09:34 not written: spanned a data farm break" in rig.text
    assert "closed by time: no later update" not in rig.text
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:33"]
    assert "data farm break 1" in rig.text and "never reported OK again" not in rig.text


def test_a_data_farm_break_that_is_never_reported_ok_closes_nothing_by_time_and_the_summary_says_so(
        rig: Rig) -> None:
    farm_rig(rig, (2105, "HMDS data farm connection is broken:ushmds"), None)
    assert rig.watch(symbols="AAPL", minutes=5) == watch_bars.EXIT_OK, rig.text
    assert rig.farm_seen == [False]  # type: ignore[attr-defined]
    assert "09:34" not in "".join(r[0] for r in rig.rows("AAPL"))
    assert "closed by time: no later update" not in rig.text
    assert "A data farm break (historical) was never reported OK again" in rig.text
    assert "still forming, not written: 09:34" in rig.text


def stall_rig(rig: Rig, last_update: float) -> None:
    """09:33 is updated at 09:33:01 and at `last_update`, then nothing: only time can close it."""
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    if last_update > 1.0:
        rig.upd("AAPL", 33, (33, last_update), (100.0, 100.6, 99.9, 100.3, 1800))
    rig.at(rig.tt(36, 0.0), lambda: None)
    rig.end_at(4, symbols=1)


@pytest.mark.parametrize("last_update,written", [(46.0, True), (45.0, False)])
def test_a_bar_closed_by_time_needs_a_live_update_within_15_seconds_of_its_end(
        rig: Rig, last_update: float, written: bool) -> None:
    stall_rig(rig, last_update)  # the lag bound is 1 s: the stream's clock reads the update 1 s early
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    rows = [r[0][11:16] for r in rig.rows("AAPL")]
    if written:
        assert rows[-1] == "09:33" and "not written: last update came" not in rig.text
    else:
        assert "09:33" not in rows
        assert ("AAPL 09:33 not written: last update came 16 s before the minute ended "
                "(stream stalled?)") in rig.text
        assert "stalled stream 1" in rig.text


def test_a_silent_stall_writes_no_bar_closed_by_time(rig: Rig) -> None:
    stall_rig(rig, 1.0)  # one update at 09:33:01 and then silence: 59 s before the minute ended
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert "last update came 60 s before the minute ended (stream stalled?)" in rig.text
    assert "09:33" not in "".join(r[0] for r in rig.rows("AAPL"))


def test_a_thin_symbol_closed_by_a_later_update_is_not_affected_by_the_stall_guard(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))  # no trade for the rest of the minute
    rig.upd("AAPL", 34, (34, 40.0), (100.2, 100.2, 100.2, 100.2, 3))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert rig.rows("AAPL")[-1][0][11:16] == "09:33" and "stalled" not in rig.text.split("Summary")[0]


# -- counting and wording ----------------------------------------------------------------------


def test_a_minute_dropped_by_a_rerequested_backfill_is_counted(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL, [(ep(9, 34), 100.3, 100.4, 100.2, 100.3, 100)]]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((34, 30.0), 1101)
    rig.at(rig.tt(34, 40.0), lambda: None, ready=lambda: "re-requested backfill:" in rig.text)
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "AAPL 09:33 not written: its final values were not in the re-requested backfill" in rig.text
    assert "re-request gap 1" in rig.text


def test_a_rerequest_still_outstanding_at_the_end_is_said_and_is_not_every_symbol_streaming(rig: Rig) -> None:
    rig.server_epoch = ep(9, 33, 0)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 1.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((34, 30.0), 1101)
    hold_history(rig, from_request=2)  # the answer never comes
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_FAILED, rig.text
    assert "AAPL: NOT streaming at the end" in rig.text
    assert "every symbol streaming" not in rig.text
    assert "had no stream at the end" in rig.text


def test_a_stopped_symbol_prints_no_further_forming_line_and_no_still_forming(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = watch_bars.LiveFiles.write

    def failing_write(self: watch_bars.LiveFiles, symbol: str, bar: Any) -> bool:
        if bar.time == ep(9, 33):
            raise watch_bars.LiveFileError("cannot open or append to X: PermissionError: denied", locked=True)
        return real_write(self, symbol, bar)

    monkeypatch.setattr(watch_bars.LiveFiles, "write", failing_write)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))  # closes 09:33: the write fails here
    rig.upd("AAPL", 34, (34, 30.0), (100.2, 100.3, 100.2, 100.3, 9))
    rig.end_at(3, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_FAILED, rig.text
    after = rig.text.split("AAPL: cannot open or append")[1]
    assert "forming 09:34" not in after.split("Summary")[0]
    assert "still forming, not written" not in rig.text


# -- --paper -----------------------------------------------------------------------------------


PAPER_LINE = "paper mode: port 7497, account check on"
LIVE_LOOKING = "U7654321"


def paper_argv(rig: Rig, *extra: str) -> list[str]:
    return ["--paper", "--symbols", "AAPL", "--minutes", "2", "--out", str(rig.live), "--no-record", *extra]


@pytest.mark.parametrize("extra,env", [
    (["--port", "7496"], {}), (["--port", "4002"], {}), (["--port", "7497", "--host", "127.0.0.2"], {}),
    (["--host", "localhost"], {}), ([], {"STOINK_ALLOW_LIVE_PORT": "1"}),
    ([], {"STOINK_ALLOW_LIVE_PORT": ""}),
    (["--port", "7496"], {"STOINK_ALLOW_LIVE_PORT": "1"})])
def test_paper_mode_refusals_build_nothing(rig: Rig, extra: list[str], env: dict[str, str]) -> None:
    rig.env.update(env)
    assert rig.main(*paper_argv(rig, *extra)) == watch_bars.EXIT_REFUSED
    assert "Refused (--paper)" in rig.text and "Nothing was started." in rig.text
    assert not rig.prompts
    nothing_built(rig)
    assert not rig.live.exists()


@pytest.mark.parametrize("accounts", ["DU0000000", "DUX123456"])
def test_paper_mode_defaults_to_the_paper_port_and_says_so_first(rig: Rig, accounts: str) -> None:
    rig.accounts = accounts
    assert watch_bars.parse_args(["--paper"]).port == 7497
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2, paper=True) == watch_bars.EXIT_OK, rig.text
    assert rig.text.splitlines()[0] == PAPER_LINE
    assert len(rig.fakes) == 1 and len(rig.rows("AAPL")) >= 2
    assert rig.prompts and "(7497)" in rig.prompts[0]  # the console gate stays


def test_without_paper_there_is_no_paper_text_and_a_port_is_still_required(rig: Rig) -> None:
    rig.end_at(1, symbols=1)
    rig.watch(symbols="AAPL", minutes=1)
    assert "paper mode" not in rig.text
    with pytest.raises(SystemExit):
        watch_bars.parse_args(["--symbols", "AAPL"])


def test_paper_mode_keeps_the_console_gate(rig: Rig) -> None:
    rig.typed = "nope"
    assert rig.watch(symbols="AAPL", minutes=1, paper=True) == watch_bars.EXIT_REFUSED
    assert len(rig.prompts) == 1
    nothing_built(rig)


def no_data_request(rig: Rig) -> None:
    names = {c.name for f in rig.fakes for c in f.calls}
    assert not names & {"reqContractDetails", "reqHistoricalData", "cancelHistoricalData"}, names


@pytest.mark.parametrize("accounts", [LIVE_LOOKING, "", f"DU1234567,{LIVE_LOOKING}", "D1234567"])
def test_a_live_looking_account_list_ends_with_the_not_paper_code_and_requests_nothing(
        rig: Rig, accounts: str) -> None:
    rig.accounts = accounts
    code = rig.watch(symbols="AAPL", minutes=2, paper=True, record=True)
    assert code == watch_bars.EXIT_NOT_PAPER == 5
    assert len(rig.fakes) == 1  # not retried
    no_data_request(rig)
    assert "reqCurrentTime" not in {c.name for c in rig.fakes[0].calls}
    assert rig.text.count(watch_bars.NOT_PAPER_LINE) == 1
    assert "U7654321" not in rig.text and "1234567" not in rig.text
    assert f"(exit {code})" in rig.text and watch_bars.NOT_PAPER_LABEL in rig.text
    assert not rig.live.exists() or not list(rig.live.rglob("*.csv"))


def test_a_connection_that_cannot_vouch_for_the_account_sends_nothing_in_paper_mode(rig: Rig) -> None:
    rig.require_paper_override = False  # opens fine, but `paper_verified` stays False
    rig.accounts = LIVE_LOOKING
    code = rig.watch(symbols="AAPL", minutes=2, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert rig.connections[0].paper_verified is False
    no_data_request(rig)
    assert "reqCurrentTime" not in {c.name for c in rig.fakes[0].calls}
    assert rig.text.count(watch_bars.NOT_PAPER_LINE) == 1


def test_a_failing_list_with_the_first_clock_answer_requests_nothing_more(rig: Rig) -> None:
    rig.mid_list = LIVE_LOOKING
    code = rig.watch(symbols="AAPL", minutes=2, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    no_data_request(rig)
    assert_one_not_paper_line(rig.text, "mid")
    assert "U7654321" not in rig.text


def test_a_failing_list_in_the_middle_of_a_watch_ends_it_with_the_not_paper_code(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))  # 09:33 is written
    rig.at(rig.tt(34, 10.0), lambda: rig.fakes[-1].push("managedAccounts", LIVE_LOOKING))
    rig.upd("AAPL", 35, (35, 2.0), (100.3, 100.3, 100.3, 100.3, 5))  # never read
    rig.expected = 0
    rig.end_at(4, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=4, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert_one_not_paper_line(rig.text, "mid")
    assert "U7654321" not in rig.text and "Nothing was requested." not in rig.text
    assert "STOPPED: the connection to TWS closed" not in rig.text
    assert rig.rows("AAPL")[-1][0][11:16] == "09:33"  # what was written before stays
    assert "Summary" in rig.text and f"(exit {code})" in rig.text


def test_a_connection_closed_after_the_account_check_is_the_ordinary_stop_not_not_paper(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.upd("AAPL", 34, (34, 2.0), (100.3, 100.3, 100.3, 100.3, 5))
    rig.drop((34, 20.0), "connectionClosed")
    rig.at(rig.tt(34, 40.0), lambda: None)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_STOPPED == 4, rig.text
    assert "STOPPED: the connection to TWS closed" in rig.text
    assert watch_bars.NOT_PAPER_LINE not in rig.text and watch_bars.NOT_PAPER_LABEL not in rig.text
    assert "Nothing was requested" not in rig.text
    assert rig.connections[0].paper_verified is False and rig.connections[0].paper_rejected is False
    assert rig.connections[0].state is ConnState.CLOSED
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:33"]
    assert "Summary" in rig.text and "exit 4" in rig.text


def test_a_lost_then_closed_connection_in_paper_mode_is_the_ordinary_stop(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.drop((33, 40.0), 1100)
    rig.drop((34, 20.0), "connectionClosed", ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(34, 40.0), lambda: None)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_STOPPED, rig.text
    assert "connectivity LOST" in rig.text
    assert watch_bars.NOT_PAPER_LINE not in rig.text and watch_bars.NOT_PAPER_LABEL not in rig.text
    assert rig.connections[0].paper_rejected is False
    assert "Summary" in rig.text and f"(exit {code})" in rig.text


def test_a_failing_list_after_a_loss_in_paper_mode_is_still_not_paper(rig: Rig) -> None:
    """Fail-safe: the connection was lost, then a failing list arrived: the account is rejected."""
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.drop((33, 40.0), 1100)
    rig.at(rig.tt(34, 10.0), lambda: rig.fakes[-1].push("managedAccounts", LIVE_LOOKING),
           ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(34, 40.0), lambda: None)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert rig.connections[0].paper_rejected is True
    assert_one_not_paper_line(rig.text, "mid")
    assert "U7654321" not in rig.text


@pytest.mark.parametrize("rep", range(4))
def test_a_failing_list_behind_connection_closed_in_a_watch_is_still_not_paper(rig: Rig, rep: int) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))

    def both() -> None:
        fake = rig.fakes[-1]
        fake._deliver("connectionClosed", ())  # the EReader order of real ibapi
        fake.push("managedAccounts", LIVE_LOOKING)
        fake._peer_close()

    rig.at(rig.tt(34, 10.0), both)
    rig.at(rig.tt(34, 40.0), lambda: None)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert rig.connections[0].paper_rejected is True
    assert_one_not_paper_line(rig.text, "mid")
    assert "U7654321" not in rig.text and "verified at start" not in rig.text


def test_a_failing_list_delivered_just_before_close_in_a_watch_is_not_paper(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.drop((34, 20.0), "connectionClosed")
    rig.at(rig.tt(34, 40.0), lambda: None)
    real_close = IbConnection.close

    def close(self: IbConnection) -> None:
        att = self._att
        if att is not None and not att.dead and not self._close_started:
            self._on_event(att, events.ManagedAccounts(LIVE_LOOKING))
        real_close(self)

    monkeypatch.setattr(IbConnection, "close", close)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert rig.connections[0].paper_rejected is True
    assert_one_not_paper_line(rig.text, "mid")
    assert "verified at start" not in rig.text


def test_the_watcher_result_line_says_the_account_was_verified_at_start_after_a_loss(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.3, 1800))
    rig.drop((34, 20.0), "connectionClosed")
    rig.at(rig.tt(34, 40.0), lambda: None)
    code = rig.watch(symbols="AAPL", minutes=5, paper=True)
    assert code == watch_bars.EXIT_STOPPED
    assert f"(exit {code}); paper account verified at start" in rig.text


def test_the_real_connection_is_built_with_require_paper_only_in_paper_mode(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    original = watch_bars.IbConnection

    def spy(*args: Any, **kw: Any) -> IbConnection:
        seen.append(dict(kw))
        kw.update(client_factory=rig.client_factory, clock=rig.plumb, sleep=rig.plumb.sleep,
                  poll_interval=0.002)
        conn = original(*args, **kw)
        rig.connections.append(conn)
        return conn

    monkeypatch.setattr(watch_bars, "IbConnection", spy)
    rig.accounts = LIVE_LOOKING  # the paper run stops at the account check; the other cannot connect
    for paper in (True, False):
        rig.behavior = ConnectBehavior.ACCEPT if paper else ConnectBehavior.REFUSE
        rig.watch(symbols="AAPL", minutes=1, paper=paper, with_factories=False)
    assert [s["require_paper"] for s in seen] == [True, False]


# -- --paper --no-prompt (unattended, bounded) ---------------------------------------------------

NO_PROMPT_LINE = "paper mode: port 7497, account check on, no prompt"


class NoStdin:
    """A stdin that fails the test on any access: the unattended run never touches it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"stdin was touched: {name}")


def unattended(rig: Rig, *extra: str, symbols: str = "AAPL", minutes: int = 2, **kw: Any) -> int:
    """A `--paper --no-prompt` run whose stdin raises on any access and whose prompt must stay unused."""
    return rig.watch(*extra, "--no-prompt", symbols=symbols, minutes=minutes, paper=True,
                     stdin=NoStdin(), **kw)


@pytest.mark.parametrize("argv", [
    ["--no-prompt", "--port", "7497", "--symbols", "AAPL", "--minutes", "2"],  # no --paper
    ["--no-prompt", "--symbols", "AAPL", "--minutes", "2"],  # no --paper, no port
    ["--paper", "--no-prompt", "--symbols", "AAPL"],  # no --minutes
    ["--paper", "--no-prompt"],  # no --minutes
    ["--paper", "--no-prompt", "--minutes", "0"],
    ["--paper", "--no-prompt", "--minutes", "421"],
    ["--paper", "--no-prompt", "--minutes", "-1"],
    ["--paper", "--no-prompt", "--minutes", "x"],
    ["--paper", "--no-prompt", "--minutes="],
    ["--paper", "--no-prompt", "--minutes", "1000"],
    ["--paper", "--no-prom", "--minutes", "5"],  # no abbreviations
    ["--paper", "--no-prompt", "--minute", "5"],
    ["--paper", "--no-prompt", "--min", "5"],
    ["--pape", "--no-prompt", "--minutes", "5"],
    ["--paper", "--no-prompt=1", "--minutes", "5"],
])
def test_no_prompt_usage_errors_exit_2_before_anything_is_built(rig: Rig, argv: list[str]) -> None:
    forbid_building(rig)  # a wrongly accepted argument must fail here, not start a watch that waits
    with pytest.raises(SystemExit) as raised:
        rig.main(*argv, "--out", str(rig.live), "--no-record", stdin=NoStdin())
    assert raised.value.code == 2
    assert not rig.prompts
    nothing_built(rig)
    assert not rig.live.exists()


def test_accepted_arguments_with_forbidden_factories_fail_at_once_instead_of_waiting(rig: Rig) -> None:
    """What the usage-error tests rely on: if the parser let a run through, building stops it
    immediately (the run would otherwise wait for scripted time that nobody feeds)."""
    forbid_building(rig)
    began = time.monotonic()
    try:
        code = rig.main("--paper", "--no-prompt", "--minutes", "2", "--symbols", "AAPL",
                        "--out", str(rig.live), "--no-record", stdin=NoStdin())
    except AssertionError:
        code = None  # the factory's own error
    assert code != watch_bars.EXIT_OK
    assert time.monotonic() - began < 20


@pytest.mark.parametrize("argv,message", [
    (["--no-prompt", "--port", "7497", "--minutes", "5"], "--no-prompt is valid only together with --paper"),
    (["--paper", "--no-prompt"], "--minutes is required with --no-prompt"),
    (["--paper", "--no-prompt", "--symbols", "AAPL"], "--minutes is required with --no-prompt"),
    (["--paper", "--no-prompt", "--minutes", "0"], "--minutes must be 1 to 420"),
    (["--paper", "--no-prompt", "--minutes", "421"], "--minutes must be 1 to 420"),
])
def test_no_prompt_usage_errors_are_raised_by_the_parser(
        argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        watch_bars.parse_args(argv)
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_the_minutes_bounds_with_no_prompt_are_1_to_420() -> None:
    for good in ("1", "30", "420"):
        assert watch_bars.parse_args(["--paper", "--no-prompt", "--minutes", good]).minutes == int(good)
    assert watch_bars.parse_args(["--paper", "--no-prompt", "--minutes=390"]).minutes == 390
    assert watch_bars.parse_args(["--paper"]).minutes == watch_bars.DEFAULT_MINUTES == 30  # the default stays
    assert watch_bars.parse_args(["--port", "7497"]).minutes == 30


def test_no_prompt_skips_the_console_gate_and_never_reads_stdin(rig: Rig) -> None:
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    assert unattended(rig) == watch_bars.EXIT_OK, rig.text
    assert rig.text.splitlines()[0] == NO_PROMPT_LINE
    assert not rig.prompts  # nothing typed, nothing asked
    assert len(rig.fakes) == 1 and len(rig.rows("AAPL")) >= 2


def test_without_no_prompt_a_missing_terminal_still_refuses(rig: Rig) -> None:
    assert rig.watch(symbols="AAPL", minutes=2, paper=True, tty=False) == watch_bars.EXIT_REFUSED
    assert "stdin is not one" in rig.text
    nothing_built(rig)


@pytest.mark.parametrize("extra,env", [
    (["--port", "7496"], {}), (["--port", "4002"], {}), (["--port", "7497", "--host", "127.0.0.2"], {}),
    (["--host", "localhost"], {}), ([], {"STOINK_ALLOW_LIVE_PORT": "1"}),
    ([], {"STOINK_ALLOW_LIVE_PORT": ""}),
    (["--port", "7496"], {"STOINK_ALLOW_LIVE_PORT": "1"})])
def test_each_paper_refusal_still_applies_with_no_prompt(
        rig: Rig, extra: list[str], env: dict[str, str]) -> None:
    rig.env.update(env)
    assert rig.main(*paper_argv(rig, *extra), "--no-prompt", stdin=NoStdin()) == watch_bars.EXIT_REFUSED
    assert "Refused (--paper)" in rig.text and "Nothing was started." in rig.text
    assert "no prompt" not in rig.text  # the paper line comes only after the refusals pass
    assert not rig.prompts
    nothing_built(rig)
    assert not rig.live.exists()


@pytest.mark.parametrize("accounts", [LIVE_LOOKING, "", f"DU1234567,{LIVE_LOOKING}", "D1234567"])
def test_a_live_looking_account_list_with_no_prompt_ends_with_5_and_requests_nothing(
        rig: Rig, accounts: str) -> None:
    rig.accounts = accounts
    code = unattended(rig, record=True)
    assert code == watch_bars.EXIT_NOT_PAPER == 5
    assert len(rig.fakes) == 1  # not retried
    no_data_request(rig)
    assert "reqCurrentTime" not in {c.name for c in rig.fakes[0].calls}
    assert rig.text.count(watch_bars.NOT_PAPER_LINE) == 1 and rig.text.splitlines()[0] == NO_PROMPT_LINE
    assert "U7654321" not in rig.text and "1234567" not in rig.text
    assert not rig.prompts
    assert not rig.live.exists() or not list(rig.live.rglob("*.csv"))


def test_a_connection_that_cannot_vouch_for_the_account_sends_nothing_with_no_prompt(rig: Rig) -> None:
    rig.require_paper_override = False
    rig.accounts = LIVE_LOOKING
    assert unattended(rig) == watch_bars.EXIT_NOT_PAPER
    assert rig.connections[0].paper_verified is False
    no_data_request(rig)


def test_a_failing_list_in_the_middle_of_a_no_prompt_watch_ends_it_with_5(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.at(rig.tt(34, 10.0), lambda: rig.fakes[-1].push("managedAccounts", LIVE_LOOKING))
    rig.upd("AAPL", 35, (35, 2.0), (100.3, 100.3, 100.3, 100.3, 5))  # never read
    rig.expected = 0
    rig.end_at(4, symbols=1)
    code = unattended(rig, minutes=4)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert_one_not_paper_line(rig.text, "mid")
    assert "U7654321" not in rig.text
    assert rig.rows("AAPL")[-1][0][11:16] == "09:33"
    assert not rig.prompts


def test_the_real_connection_is_still_built_with_require_paper_with_no_prompt(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    original = watch_bars.IbConnection

    def spy(*args: Any, **kw: Any) -> IbConnection:
        seen.append(dict(kw))
        kw.update(client_factory=rig.client_factory, clock=rig.plumb, sleep=rig.plumb.sleep,
                  poll_interval=0.002)
        conn = original(*args, **kw)
        rig.connections.append(conn)
        return conn

    monkeypatch.setattr(watch_bars, "IbConnection", spy)
    rig.accounts = LIVE_LOOKING  # stops at the account check
    rig.behavior = ConnectBehavior.ACCEPT
    assert unattended(rig, minutes=1, with_factories=False) == watch_bars.EXIT_NOT_PAPER
    assert [s["require_paper"] for s in seen] == [True]
    assert not rig.prompts


def test_the_parser_turns_abbreviations_off_and_documents_no_prompt() -> None:
    parser = watch_bars.build_parser()
    assert parser.allow_abbrev is False
    shown = " ".join(parser.format_help().split())
    assert "--no-prompt" in shown and "required with --no-prompt" in shown


def test_the_no_prompt_line_is_the_one_of_cli() -> None:
    from MainStoinker.ib import cli

    assert watch_bars.PAPER_NO_PROMPT_LINE is cli.PAPER_NO_PROMPT_LINE
    assert cli.PAPER_NO_PROMPT_LINE == NO_PROMPT_LINE


def test_the_not_paper_code_is_new_documented_and_distinct() -> None:
    codes = [watch_bars.EXIT_OK, watch_bars.EXIT_FAILED, watch_bars.EXIT_REFUSED,
             watch_bars.EXIT_NO_CONNECTION, watch_bars.EXIT_STOPPED, watch_bars.EXIT_INTERRUPTED]
    assert watch_bars.EXIT_NOT_PAPER not in codes
    assert "  5    --paper only" in watch_bars.EXIT_CODES_TEXT


# -- "no data received": a stream that answers nothing in the session -----------------------------

NO_DATA_HINT = ("check the market data subscription of this account (quotes answering IB 354 / 10167 "
                "mean none; a paper account needs the live account's data shared with it); on a "
                "market holiday or with delayed data this is expected")


def epd(day: int, hh: int, mm: int, ss: int = 0) -> int:
    """Epoch seconds of 2024-08-<day> hh:mm:ss New York (the 16th is a Friday)."""
    return int(datetime(2024, 8, day, hh, mm, ss, tzinfo=NY).timestamp())


def earlier_session() -> list[tuple[Any, ...]]:
    """What a start outside the session gets from IB: bars of the previous session only."""
    return [(epd(15, 15, 58), 99.0, 99.5, 98.8, 99.2, 5000), (epd(15, 15, 59), 99.2, 99.9, 99.1, 99.8, 4000)]


def probe_text(rig: Rig, when: float, seen: dict[str, bool]) -> None:
    """At script time `when`, record whether the no-data line is out yet (the step runs before the pass)."""
    rig.at(when, lambda: seen.__setitem__(str(when), "no data received" in rig.text))


def no_data_lines(rig: Rig, symbol: str) -> list[str]:
    return [ln for ln in rig.text.splitlines() if ln.startswith(f"{symbol}: no data received")]


def test_no_data_is_said_after_two_minutes_of_the_session_and_ends_with_the_new_code(rig: Rig) -> None:
    rig.backfills = {"AAPL": [[]]}
    seen: dict[str, bool] = {}
    probe_text(rig, rig.tt(34, 50.0), seen)  # 85 s after the backfill: not yet
    probe_text(rig, rig.tt(35, 40.0), seen)  # the pass at 09:34:50 has run: still nothing said
    probe_text(rig, rig.tt(36, 30.0), seen)  # the pass at 09:35:40 (135 s) has run: said
    rig.end_at(4, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=4)
    assert code == watch_bars.EXIT_NO_DATA == 6, rig.text
    assert list(seen.values()) == [False, False, True]
    lines = no_data_lines(rig, "AAPL")
    assert len(lines) == 1 and NO_DATA_HINT in lines[0]  # once, with the hint
    assert "  AAPL: no data received" in rig.text and "  AAPL: streaming" not in rig.text
    assert "every symbol streaming" not in rig.text
    assert f"Result: {watch_bars._LABELS[6]} (exit 6)" in rig.text
    assert "no data received for at least one symbol" in rig.text


def test_no_data_with_only_the_end_of_the_watch_as_the_first_look(rig: Rig) -> None:
    rig.backfills = {"AAPL": [[]]}
    rig.end_at(3, symbols=1)  # nothing happens until 181 s: the end of the watch decides
    assert rig.watch(symbols="AAPL", minutes=3) == watch_bars.EXIT_NO_DATA, rig.text
    assert len(no_data_lines(rig, "AAPL")) == 1


def test_no_data_less_than_two_minutes_into_the_session_is_not_declared(rig: Rig) -> None:
    rig.backfills = {"AAPL": [[]]}
    rig.end_at(1, symbols=1)  # a one-minute watch: 61 s
    code = rig.watch(symbols="AAPL", minutes=1)
    assert "no data received" not in rig.text
    assert code != watch_bars.EXIT_NO_DATA


def test_one_symbol_without_data_is_named_and_the_other_keeps_streaming(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL], "MSFT": [[]]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(4)
    code = rig.watch(minutes=4)
    assert code == watch_bars.EXIT_NO_DATA, rig.text
    assert len(no_data_lines(rig, "MSFT")) == 1 and no_data_lines(rig, "AAPL") == []
    assert "  AAPL: streaming" in rig.text and "  MSFT: no data received" in rig.text
    assert "every symbol streaming" not in rig.text


def test_data_that_starts_later_makes_the_symbol_streaming_and_the_result_follows(rig: Rig) -> None:
    rig.backfills = {"AAPL": [[]]}
    rig.at(rig.tt(35, 40.0), lambda: None)  # a look at 135 s: nothing yet
    rig.upd("AAPL", 36, (36, 2.0), (100.0, 100.5, 99.9, 100.2, 1000))  # 155 s after the backfill
    rig.upd("AAPL", 36, (36, 20.0), (100.0, 100.6, 99.9, 100.3, 1200))
    rig.end_at(4, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=4)
    assert code == watch_bars.EXIT_OK, rig.text
    assert len(no_data_lines(rig, "AAPL")) == 1  # said once, when it was established
    assert "  AAPL: streaming" in rig.text and "  AAPL: no data received" not in rig.text
    assert f"Result: {watch_bars._LABELS[0]} (exit 0)" in rig.text


@pytest.mark.parametrize("server,minutes", [
    (epd(16, 16, 30), 4),  # after the close
    # a Saturday and a Sunday used to be watched ("market closed"); since issue 285 they are refused
    # (tests/test_ib_calendar_live.py)
    (epd(16, 9, 0), 4),    # before the open, and the watch ends before 09:30
])
def test_outside_the_session_nothing_changes(rig: Rig, server: int, minutes: int) -> None:
    rig.server_epoch = server
    rig.wall_offset = -0.4
    rig.backfills = {"AAPL": [earlier_session()]}
    rig.end_at(minutes, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=minutes)
    assert code == watch_bars.EXIT_OK, rig.text
    assert "no data received" not in rig.text
    assert "market closed: no new bars expected" in rig.text
    assert "  AAPL: streaming" in rig.text


def test_a_start_before_the_open_starts_the_clock_at_0930(rig: Rig) -> None:
    rig.server_epoch = epd(16, 9, 28)
    rig.backfills = {"AAPL": [earlier_session()]}
    seen: dict[str, bool] = {}
    probe_text(rig, rig.tt(31, 40.0), seen)  # 100 s after 09:30
    probe_text(rig, rig.tt(32, 20.0), seen)  # the pass at 09:31:40 has run: nothing said
    probe_text(rig, rig.tt(33, 5.0), seen)   # the pass at 09:32:20 (140 s) has run: said
    rig.end_at(6, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=6)
    assert code == watch_bars.EXIT_NO_DATA, rig.text
    assert list(seen.values()) == [False, False, True]
    assert len(no_data_lines(rig, "AAPL")) == 1


def test_the_no_data_code_is_new_documented_and_distinct() -> None:
    codes = [watch_bars.EXIT_OK, watch_bars.EXIT_FAILED, watch_bars.EXIT_REFUSED,
             watch_bars.EXIT_NO_CONNECTION, watch_bars.EXIT_STOPPED, watch_bars.EXIT_NOT_PAPER,
             watch_bars.EXIT_INTERRUPTED]
    assert watch_bars.EXIT_NO_DATA == 6 and watch_bars.EXIT_NO_DATA not in codes
    assert "  6    " in watch_bars.EXIT_CODES_TEXT and "no data" in watch_bars.EXIT_CODES_TEXT
    assert "exit code 6" in (watch_bars.__doc__ or "")
    shown = " ".join(watch_bars.build_parser().format_help().split())
    assert "6 finished" in shown and "market data subscription" in shown


def at_epoch(rig: Rig, when: int, fn: Callable[[], None], ready: Callable[[], bool] | None = None) -> None:
    rig.at(when - rig.server_epoch, fn, ready)


def passes(rig: Rig, first: int, last: int, every: int) -> None:
    """No-op steps, so that the watcher takes a look at these server times."""
    for when in range(first, last + 1, every):
        at_epoch(rig, when, lambda: None)


def update_of(rig: Rig, symbol: str, bar_epoch: int, when: int, volume: int = 10) -> None:
    def push() -> None:
        rig.expected += 1
        rig.fakes[-1].push("historicalDataUpdate", rig.req_ids[symbol],
                           make_bar(bar_epoch, 99.0, 99.5, 98.8, 99.2, volume))
        rig.await_delivery(symbol)

    at_epoch(rig, when, push)


PRE_OPEN = epd(16, 9, 28)
SUBSCRIPTION_WORDS = "check the market data subscription"


def test_a_lost_connection_that_is_restored_is_not_no_data(rig: Rig) -> None:
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    at_epoch(rig, epd(16, 9, 29), lambda: rig.fakes[-1].drop(1100))
    at_epoch(rig, epd(16, 9, 34), lambda: rig.fakes[-1].drop(1102),
             ready=lambda: "connectivity LOST" in rig.text)
    passes(rig, epd(16, 9, 29, 30), epd(16, 9, 33, 30), 30)  # 09:30 to 09:34 is an outage: no clock
    for secs in (10, 15, 20, 25, 30):
        update_of(rig, "AAPL", epd(16, 9, 34), epd(16, 9, 34, secs))
    update_of(rig, "AAPL", epd(16, 9, 35), epd(16, 9, 35, 4))
    rig.end_at(8, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=8)
    assert code == watch_bars.EXIT_OK, rig.text
    assert "no data received" not in rig.text and SUBSCRIPTION_WORDS not in rig.text
    assert "connectivity LOST" in rig.text and "connectivity restored" in rig.text


def test_the_clock_resumes_after_an_outage_where_it_stopped(rig: Rig) -> None:
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    at_epoch(rig, epd(16, 9, 29), lambda: rig.fakes[-1].drop(1100))
    at_epoch(rig, epd(16, 9, 31, 30), lambda: rig.fakes[-1].drop(1102),
             ready=lambda: "connectivity LOST" in rig.text)
    passes(rig, epd(16, 9, 29, 30), epd(16, 9, 31, 0), 30)
    at_epoch(rig, epd(16, 9, 31, 31), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    passes(rig, epd(16, 9, 31, 40), epd(16, 9, 32, 30), 30)
    seen: dict[str, bool] = {}
    when = epd(16, 9, 33, 0)  # 90 s after the restore: not yet (the 09:30 to 09:31:30 outage is not counted)
    at_epoch(rig, when, lambda: seen.__setitem__("early", "no data received" in rig.text))
    at_epoch(rig, epd(16, 9, 34, 0), lambda: None)
    at_epoch(rig, epd(16, 9, 34, 30), lambda: seen.__setitem__("late", "no data received" in rig.text))
    rig.end_at(8, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=8)
    assert seen == {"early": False, "late": True}, rig.text
    assert code == watch_bars.EXIT_NO_DATA


def test_a_connection_lost_until_the_end_is_the_outage_not_no_data(rig: Rig) -> None:
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    at_epoch(rig, epd(16, 9, 29), lambda: rig.fakes[-1].drop(1100))
    at_epoch(rig, epd(16, 9, 29, 10), lambda: None, ready=lambda: "connectivity LOST" in rig.text)
    passes(rig, epd(16, 9, 29, 30), epd(16, 9, 35, 0), 30)
    rig.end_at(8, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=8)
    assert code == watch_bars.EXIT_STOPPED, rig.text
    assert "no data received" not in rig.text and SUBSCRIPTION_WORDS not in rig.text
    assert "The connection to IB is still lost" in rig.text


def test_a_data_farm_break_that_is_never_restored_is_not_no_data(rig: Rig) -> None:
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    at_epoch(rig, epd(16, 9, 29),
             lambda: rig.fakes[-1].drop(2103, "Market data farm connection is broken:usfarm"))
    at_epoch(rig, epd(16, 9, 29, 10), lambda: None, ready=lambda: "data farm break (2103)" in rig.text)
    passes(rig, epd(16, 9, 29, 30), epd(16, 9, 35, 0), 30)
    rig.end_at(8, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=8)
    assert code == watch_bars.EXIT_OK, rig.text  # what the watcher answered before "no data" existed
    assert "no data received" not in rig.text and SUBSCRIPTION_WORDS not in rig.text
    assert "never reported OK again" in rig.text


def test_exit_1_wins_over_no_data(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = watch_bars.LiveFiles.write

    def failing(self: Any, symbol: str, bar: Any) -> bool:
        if symbol == "MSFT":
            raise watch_bars.LiveFileError("cannot open the file: PermissionError", locked=True)
        return real_write(self, symbol, bar)

    monkeypatch.setattr(watch_bars.LiveFiles, "write", failing)
    rig.backfills = {"AAPL": [[]], "MSFT": [[(ep(9, m), 1.0, 2.0, 0.5, 1.5, 10) for m in (30, 31, 32)]]}
    rig.end_at(4)
    code = rig.watch(minutes=4)
    assert len(no_data_lines(rig, "AAPL")) == 1 and "MSFT stopped, the other symbols continue" in rig.text
    assert code == watch_bars.EXIT_FAILED == 1, rig.text


def test_updates_of_the_previous_session_keep_the_symbol_alive_but_not_forever(rig: Rig) -> None:
    """Updates of an earlier date are no data of today, but while any update arrives the stream is alive;
    the 120 s run from the last one."""
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    passes(rig, epd(16, 9, 29), epd(16, 9, 36, 30), 30)
    for secs in range(0, 8 * 60, 30):  # an update every 30 s from 09:30 to 09:37:30
        update_of(rig, "AAPL", epd(15, 15, 59), PRE_OPEN + 120 + secs)
    seen: dict[str, bool] = {}
    at_epoch(rig, epd(16, 9, 36), lambda: seen.__setitem__("while", "no data received" in rig.text))
    rig.end_at(9, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=9)
    assert seen["while"] is False
    assert code == watch_bars.EXIT_OK, rig.text  # updates keep arriving until the end of the watch
    assert "no data received" not in rig.text


def test_a_stream_that_goes_silent_without_a_bar_of_today_is_no_data_after_120_s(rig: Rig) -> None:
    rig.server_epoch = PRE_OPEN
    rig.backfills = {"AAPL": [earlier_session()]}
    update_of(rig, "AAPL", epd(15, 15, 59), epd(16, 9, 30, 30))  # the last sign of life
    passes(rig, epd(16, 9, 31), epd(16, 9, 35, 0), 30)
    rig.end_at(8, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=8) == watch_bars.EXIT_NO_DATA, rig.text
    assert len(no_data_lines(rig, "AAPL")) == 1


def test_the_watcher_doubt_line_is_exactly_one_line_with_the_same_code(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from MainStoinker.ib import cli
    from MainStoinker.ib.connection import IbConnection as Connection

    monkeypatch.setattr(cli, "READER_WAIT", 0.01)
    monkeypatch.setattr(Connection, "reader_finished", lambda self, timeout=0.0: False)
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.at(rig.tt(34, 10.0), lambda: rig.fakes[-1].drop("connectionClosed"))
    rig.end_at(4, symbols=1)
    code = rig.watch(symbols="AAPL", minutes=4, paper=True)
    assert code == watch_bars.EXIT_NOT_PAPER, rig.text
    assert_one_not_paper_line(rig.text, "doubt")


# -- the first banner line --------------------------------------------------------------------


def test_an_unattended_paper_watch_does_not_say_only_the_owner_runs_it(rig: Rig) -> None:
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    unattended(rig)
    assert "Unattended paper run: data only, paper account only." in rig.text
    assert "Only the owner runs it" not in rig.text
    assert "places no trade" in rig.text


def test_an_attended_watch_keeps_the_owner_line(rig: Rig) -> None:
    rig.typed = "nope"
    rig.watch(symbols="AAPL", minutes=2, paper=True)
    assert "Only the owner runs it" in rig.text and "Unattended paper run" not in rig.text


def test_the_mid_run_line_is_not_the_at_open_line(rig: Rig) -> None:
    from MainStoinker.ib import cli

    assert watch_bars.NOT_PAPER_MID_LINE is cli.NOT_PAPER_MID_LINE
    assert "Nothing was requested" not in watch_bars.NOT_PAPER_MID_LINE


def test_the_paper_helpers_are_the_ones_of_cli() -> None:
    from MainStoinker.ib import cli

    assert watch_bars.paper_refusal is cli.paper_refusal
    assert watch_bars.paper_session_ok is cli.paper_session_ok
    assert watch_bars.NOT_PAPER_LINE is cli.NOT_PAPER_LINE


# -- source checks --------------------------------------------------------------------------


def test_the_environment_is_read_only_in_main() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
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
    source = SOURCE.read_text(encoding="utf-8")
    for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "reqAllOpenOrders", "reqOpenOrders",
                 "reqAutoOpenOrders", "exerciseOptions", "whatIf", "Order(", "openOrder",
                 "reqPositions", "reqAccount", "reqExecutions"):
        assert word not in source, word
    assert not re.search(r"(?i)\borders?\b", source)  # no order-related name at all (recorder is fine)
    assert "os.environ[" not in source and "setdefault" not in source and "putenv" not in source
    assert "STOINK_ALLOW_LIVE_PORT" not in source


def test_the_source_uses_only_public_methods_of_the_connection_and_market_data() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    private: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__")
                and isinstance(node.value, ast.Attribute) and node.value.attr in {"md", "conn"}):
            private.add(node.attr)
    assert not private, private


def test_the_module_has_a_main_guard_and_is_not_a_subcommand() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source and "sys.exit(main())" in source
    assert watch_bars.build_parser().prog == "python -m MainStoinker.ib.watch_bars"
