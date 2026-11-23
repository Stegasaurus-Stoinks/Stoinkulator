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
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ibapi.common import BarData
from ibapi.contract import Contract, ContractDetails

from MainStoinker.engine.feed import read_offline_bars
from MainStoinker.ib import watch_bars
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import ConnState, IbConnection
from MainStoinker.ib.market_data import IbMarketData, Outcome
from MainStoinker.ib.recorder import SessionRecorder, scan_recording
from MainStoinker.Util.broker_guard import BrokerPortRefused
from tests.fake_ib import FARM_MESSAGES, ConnectBehavior, FakeIbClient, make_bar

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


def _listing(home: Path | None) -> dict[str, tuple[bool, int]]:
    """Name and modification time of every entry of the real `Stoinkulator/live`, `bars`, `recordings`."""
    seen: dict[str, tuple[bool, int]] = {}
    if home is None:
        return seen
    for name in ("live", "bars", "recordings"):
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

        self.at(self.tt(when[0], when[1], hh), push)

    def raw_update(self, symbol: str, when: tuple[int, float], bar: BarData) -> None:
        def push() -> None:
            self.expected += 1
            self.fakes[-1].push("historicalDataUpdate", self.req_ids[symbol], bar)

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
                            farm_messages=FARM_MESSAGES if self.farm else ())
        fake.respond("reqContractDetails", self._contract)
        fake.respond("reqHistoricalData", self._history)
        fake.respond("reqCurrentTime", lambda: [("currentTime", self.server_epoch)])
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
                            retry_delay=3, observers=observers)
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
             recorder: bool = True) -> int:
        if "--symbols" in argv:
            self.run_symbols = argv[argv.index("--symbols") + 1].split(",")
        extra: dict[str, Any] = {}
        if with_factories:
            extra = {"connection_factory": self.connection_factory,
                     "market_data_factory": self.market_data_factory}
            if recorder:
                extra["recorder_factory"] = self.recorder_factory
        return watch_bars.main(
            list(argv), env=self.env, stdin=Tty(tty), input_fn=self.input_fn, out=self.out,
            clock=self, sleep=self.sleep, wall=self.wall, today=date(2024, 8, 16), **extra)

    def watch(self, *extra: str, symbols: str | None = None, minutes: int = 5, record: bool = False,
              port: str = "7497", **kw: Any) -> int:
        names = symbols if symbols is not None else ",".join(self.symbols)
        argv = ["--port", port, "--symbols", names, "--minutes", str(minutes),
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
    assert lines[4] == f"2024-08-16 09:33:00,{ep(9, 33)},100.0,100.6,99.9,100.3,1800"
    assert lines[-1] == f"2024-08-16 09:36:00,{ep(9, 36)},101.1,101.3,101.0,101.2,900"
    assert [row[0][11:16] for row in rig.rows("AAPL")] == ["09:30", "09:31", "09:32", "09:33", "09:34",
                                                           "09:35", "09:36"]  # 09:37 is still forming
    assert "101.2" not in {row[2] for row in rig.rows("AAPL")}  # the forming bar's open is not there
    assert len(rig.rows("MSFT")) == 7
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
    assert "closed bars emitted live: 4" in aapl and "backfill bars written: 3" in aapl
    assert "missing minutes inside the span: none" in aapl
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
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33", "09:35"]
    assert rows[3][2:] == ["100.0", "100.6", "99.9", "100.3", "1800"]
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
    assert "missing minutes inside the span (2): 09:34, 09:35" in rig.text
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
    assert [r[0][11:16] for r in rig.rows("AAPL")] == ["09:30", "09:31", "09:32", "09:33", "09:34"]
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
    assert "AAPL_live_2024-08-16.csv exists with 5 rows, last 09:34; appending only later bars" in second.text
    rows = rig.rows("AAPL")
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35", "09:36"]
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
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35", "09:36"]
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
    assert rig.text.index("LOST") < rig.text.index("restored.") < rig.text.index("09:33  AAPL  O")
    assert len(rig.requests) == 1  # data kept: no new request
    assert [r[0][11:16] for r in rig.rows("AAPL")][-1] == "09:33"


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
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33"]
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
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.6, 99.9, 100.3, 1800))
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
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35"]
    assert rows[5][2:] == ["100.8", "101.2", "100.7", "101.1", "1900"]
    assert "700" not in rig.csv("AAPL").read_text(encoding="utf-8")


@pytest.mark.parametrize("seconds_after_the_minute,closed", [
    (watch_bars.CLOSE_GRACE, True),  # the minute ended exactly the grace period before the request
    (watch_bars.CLOSE_GRACE - 1, False),  # one second less: the bar may still be forming
])
def test_the_grace_period_decides_whether_the_last_backfill_bar_is_closed(
        rig: Rig, seconds_after_the_minute: int, closed: bool) -> None:
    rig.server_epoch = ep(9, 33, seconds_after_the_minute)  # the 09:32 bar ended at 09:33:00
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.end_at(1, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=1) == watch_bars.EXIT_OK, rig.text
    if closed:
        assert "last 09:32; 3 new closed bars written" in rig.text
        assert "still forming" not in rig.text.split("Summary")[0]
    else:
        assert "last 09:32; 2 new closed bars written; the 09:32 bar is still forming" in rig.text


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


def test_a_quiet_minute_closed_by_time_then_revised_says_the_file_keeps_the_earlier_values(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.at(rig.tt(35, 31.0), lambda: None)  # 09:33 is closed by time at 09:35:30
    rig.upd("AAPL", 33, (35, 40.0), (100.0, 100.7, 99.9, 100.6, 1300))  # IB revises it afterwards
    rig.upd("AAPL", 35, (35, 45.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.upd("AAPL", 36, (36, 1.0), (100.6, 100.6, 100.6, 100.6, 1))
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert rig.rows("AAPL")[3][2:] == ["100.0", "100.5", "99.9", "100.2", "1000"]
    late = [ln for ln in rig.text.splitlines() if "late revision for 09:33" in ln]
    assert len(late) == 1
    assert "the file keeps the earlier values" in late[0]
    assert "written O 100.0 H 100.5 L 99.9 C 100.2 volume 1000" in late[0]
    assert "newer O 100.0 H 100.7 L 99.9 C 100.6 volume 1300" in late[0]
    assert len([ln for ln in rig.closed_lines("AAPL") if ln.startswith("09:33")]) == 1  # emitted once
    assert "rows that may be stale: 1" in rig.text and "late revisions: 1" in rig.text


def test_a_revision_identical_to_the_written_row_is_not_called_stale(rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.at(rig.tt(35, 31.0), lambda: None)
    rig.upd("AAPL", 33, (35, 40.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.end_at(4, symbols=1)
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
    upd_at(rig, "AAPL", ep(9, 47), ep(10, 2, 30), (99.8, 99.9, 99.7, 99.85, 600))
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
    assert "delayed" not in rig.text
    assert not list(rig.live.rglob("*.csv"))  # nothing, in particular no file of the previous day


def test_a_start_just_after_the_open_writes_only_todays_backfill_bars(rig: Rig) -> None:
    yesterday = [int(datetime(2024, 8, 15, 15, m, tzinfo=NY).timestamp()) for m in (58, 59)]
    rig.backfills = {"AAPL": [[(e, 99.0, 99.5, 98.8, 99.2, 100) for e in yesterday] + AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.upd("AAPL", 34, (34, 2.0), (100.2, 100.2, 100.2, 100.2, 5))
    rig.end_at(2, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=2) == watch_bars.EXIT_OK, rig.text
    assert "AAPL: backfill holds 2 bars of the previous session: not written" in rig.text
    assert [r[0][:10] for r in rig.rows("AAPL")] == ["2024-08-16"] * 4
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
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert rig.text.count("market closed: no new bars expected") == 1
    assert rig.rows("AAPL")[-1][2:] == ["100.9", "101.5", "100.5", "101.4", "90000"]
    assert "delayed" not in rig.text


# -- outages ----------------------------------------------------------------------------------


def test_nothing_is_closed_by_time_while_the_connection_is_lost(rig: Rig) -> None:
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
    line = [ln for ln in text.splitlines() if ln.startswith("09:33  AAPL")]
    assert len(line) == 1 and "(spanned a connection loss)" in line[0]
    assert text.index("connectivity restored") < text.index(line[0])
    assert rig.rows("AAPL")[3][2:] == ["100.0", "100.6", "99.9", "100.4", "1700"]  # the final values
    assert "spanned a connection loss 2" in text  # 09:33 and 09:35, the minutes of the outage


def test_a_bar_that_spanned_an_outage_waits_90_seconds_after_the_restore_before_time_closes_it(
        rig: Rig) -> None:
    rig.backfills = {"AAPL": [AAPL_BACKFILL]}
    rig.upd("AAPL", 33, (33, 30.0), (100.0, 100.5, 99.9, 100.2, 1000))
    rig.drop((33, 40.0), 1100)
    rig.drop((35, 40.0), 1102, ready=lambda: "connectivity LOST" in rig.text)
    rig.at(rig.tt(35, 42.0), lambda: None, ready=lambda: "connectivity restored" in rig.text)
    seen: list[bool] = []
    rig.at(rig.tt(37, 0.0), lambda: seen.append("09:33  AAPL" in rig.text))  # 80 s after the restore
    rig.end_at(4, symbols=1)
    assert rig.watch(symbols="AAPL", minutes=4) == watch_bars.EXIT_OK, rig.text
    assert seen == [False]
    line = [ln for ln in rig.text.splitlines() if ln.startswith("09:33  AAPL")]
    assert len(line) == 1 and "closed by time" in line[0] and "(spanned a connection loss)" in line[0]


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
    assert "skipped minute 1, spanned a connection loss 0" in rig.text


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
    assert "--port 7497 --symbols AAPL,MSFT,TSLA" in doc and "7496" not in doc
    with pytest.raises(SystemExit):
        watch_bars.parse_args(["--help"])
    shown = capsys.readouterr().out
    assert "--port 7497 --symbols AAPL,MSFT,TSLA" in shown and "7496" not in shown
    assert "own opt-in" in shown and "STOINK_ALLOW_LIVE_PORT" not in shown


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
