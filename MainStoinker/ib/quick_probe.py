"""Owner-run quick probe: one read-only data session against TWS or IB Gateway.

Run it yourself, in a terminal, with TWS or IB Gateway open:

    python -m MainStoinker.ib.quick_probe --port 7497

It is an interim tool ahead of the proper `fetch-bars` / `probe-data` commands. It is
built only on `IbConnection` and `IbMarketData` (data-only client underneath: it cannot
place, modify or cancel anything at the broker). Agents never run it.

What it does, in numbered steps (each ends with an OK or FAILED line):

1. connect (time taken, attempts, every failed attempt with IB's code and text);
2. data farms: market and historical;
3. server time, in UTC and New York, and the difference from this machine's clock;
4. contract details per symbol;
5. per symbol: quotes, live (type 1) first, delayed (type 3) if no price tick comes in 8 s;
   the answer is "LIVE data", "DELAYED data (15 min)" or "NO data";
6. per symbol: one day of 1-minute regular-hours bars of the latest closed session;
7. a short summary to paste back.

Safety: the console gate (a terminal is required and you type the port number) runs
before any client is built. The port guard decides which ports are allowed: paper ports
always; live ports only if YOU set `STOINK_ALLOW_LIVE_PORT=1` in your terminal; this
script never sets it. The environment is read in `main()` only.

Privacy: nothing that carries an account id is printed or logged, and every printed line
passes `scrub` (one or two letters followed by five or more digits become `ACCOUNT`).
Read the output once before you share it anyway.

Exit codes: 0 all steps done (delayed data counts as success); 1 connected but a step
failed; 2 refused (gate, port guard) or bad arguments; 3 could not connect; 130 Ctrl-C.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.ib import events
from MainStoinker.ib.bars import bar_from_ib
from MainStoinker.ib.connection import (
    MAX_CLIENT_ID,
    REFUSED_CLIENT_IDS,
    BrokerConnectionRefusedHere,
    ClientIdRefused,
    ConnectFailed,
    IbConnection,
)
from MainStoinker.ib.history import check_symbol, session_close_utc
from MainStoinker.ib.market_data import (
    CLOSED,
    DATA_LOST,
    END,
    TIMEOUT,
    DataLost,
    IbMarketData,
    Outcome,
    ResolveFailed,
)
from MainStoinker.Util.broker_guard import ALLOW_LIVE_ENV, BrokerPortRefused, check_broker_port

NY = ZoneInfo("America/New_York")
MAX_SYMBOLS = 3
MAX_QUOTE_SECONDS = 120
MAX_CONNECT_TIMEOUT = 300
FIRST_TICK_WAIT = 8.0
HISTORY_WAIT = 60.0
HISTORY_HARD_CAP = 180.0
FARM_WAIT = 10.0
TIME_WAIT = 10.0
EXIT_OK, EXIT_STEP_FAILED, EXIT_REFUSED, EXIT_NO_CONNECTION, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

_ACCOUNT_SHAPE = re.compile(r"[A-Za-z]{1,2}[0-9]{5,}")
_TICK_NAMES = {1: "bid", 2: "ask", 4: "last", 66: "bid (delayed)", 67: "ask (delayed)", 68: "last (delayed)"}
_TYPE_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed frozen"}
_HEADLINES = {
    1: "LIVE data",
    2: "LIVE data (frozen)",
    3: "DELAYED data (15 min)",
    4: "DELAYED data (15 min, frozen)",
}

ConnectionFactory = Callable[[Any], IbConnection]
MarketDataFactory = Callable[[IbConnection], IbMarketData]


def scrub(text: str) -> str:
    """Replace anything shaped like an account id (1-2 letters then 5+ digits) with ACCOUNT."""
    return _ACCOUNT_SHAPE.sub("ACCOUNT", text)


class _Say:
    """Prints scrubbed lines; nothing reaches the stream unscrubbed."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __call__(self, text: str = "") -> None:
        for line in (text.splitlines() or [""]):
            print(scrub(line), file=self._stream)
        self._stream.flush()


@dataclass
class _Symbol:
    symbol: str
    info: Any = None
    verdict: str = "not tested"
    ticks: int = 0
    history: str = "not run"
    bars: int = 0
    first_time: str = "-"
    last_time: str = "-"
    first_volume: str = "-"
    ok: bool = True


@dataclass
class _Quotes:
    requested: int
    reported: int | None = None
    ticks: Counter[int] = field(default_factory=Counter)
    price_ticks: int = 0
    first: tuple[float, int, float] | None = None  # price, tick type, seconds since request
    last: tuple[float, int, float] | None = None
    errors: list[events.IbError] = field(default_factory=list)
    stopped: str = ""


@dataclass
class _Run:
    say: _Say
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    wall: Callable[[], datetime]
    seen: dict[int, str] = field(default_factory=dict)  # distinct IB codes -> first text
    failed: bool = False
    farms: str = "not checked"
    clock_note: str = "no server time"

    def note(self, code: int | None, text: str) -> None:
        if code is not None:
            self.seen.setdefault(code, scrub(text))

    def ok(self, text: str) -> None:
        self.say(f"   OK: {text}")

    def fail(self, text: str) -> None:
        self.failed = True
        self.say(f"   FAILED: {text}")


def closed_session_day(server_now: datetime) -> date:
    """The latest trading weekday whose regular session (16:00 New York) has closed.

    Today if it is a weekday and 16:00 New York has passed, else the previous weekday.
    Holidays are not known here (an IB "no data" answer shows them).
    """
    today = server_now.astimezone(NY).date()
    if today.weekday() < 5 and session_close_utc(today) <= server_now:
        return today
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _tick_summary(ticks: Counter[int]) -> str:
    if not ticks:
        return "none"
    parts = [f"{t}{f' {_TICK_NAMES[t]}' if t in _TICK_NAMES else ''}={n}" for t, n in sorted(ticks.items())]
    return ", ".join(parts) + f" ({sum(ticks.values())} total)"


# -- steps ---------------------------------------------------------------------------------


def _step_farms(run: _Run, conn: IbConnection) -> None:
    run.say("2. Data farms")
    up = {kind: conn.wait_for_data_farm(FARM_WAIT, farm=kind) for kind in ("market", "historical")}
    text = ", ".join(f"{kind} {'up' if value else 'NOT seen'}" for kind, value in up.items())
    run.farms = text
    if all(up.values()):
        run.ok(text)
    else:
        run.fail(text)


def _step_time(run: _Run, md: IbMarketData) -> datetime | None:
    run.say("3. Server time")
    md.request_server_time()
    try:
        server = md.wait_server_time(TIME_WAIT)
    except DataLost:
        server = None
    if server is None:
        run.fail(f"no answer from IB within {TIME_WAIT:g} s")
        return None
    diff = (server - run.wall()).total_seconds()
    run.say(f"   UTC:        {server:%Y-%m-%d %H:%M:%S}")
    run.say(f"   New York:   {server.astimezone(NY):%Y-%m-%d %H:%M:%S %Z}")
    run.clock_note = f"IB UTC {server:%Y-%m-%d %H:%M:%S}, IB minus machine {diff:+.1f} s"
    run.ok(f"IB clock minus this machine's clock = {diff:+.1f} s (IB time has 1 s resolution)")
    return server


def _step_contract(run: _Run, md: IbMarketData, sym: _Symbol) -> None:
    try:
        info = md.resolve_stock(sym.symbol, 10.0)
    except ResolveFailed as exc:
        run.note(getattr(exc, "code", None), str(exc))
        sym.ok = False
        sym.verdict = "contract not found"
        run.say(f"   {sym.symbol}:")
        run.fail(str(exc))
        return
    sym.info = info
    run.say(
        f"   {sym.symbol}: conId {info.con_id}, primary exchange {info.primary_exchange}, "
        f"currency {info.currency}, time zone {info.time_zone_id}"
    )
    run.ok(sym.symbol)


def _read_quotes(
    run: _Run, md: IbMarketData, contract: Any, mdt: int, window: float, early_stop: float | None
) -> _Quotes:
    """One quote stream of market data type `mdt`; always cancelled."""
    q = _Quotes(mdt)
    md.set_market_data_type(mdt)
    run.sleep(0.5)
    started = run.clock()
    req_id = md.request_quotes(contract)
    try:
        while True:
            elapsed = run.clock() - started
            limit = early_stop if (early_stop is not None and not q.price_ticks) else window
            if elapsed >= limit:
                break
            event = md.poll_event(req_id, min(1.0, limit - elapsed))
            if isinstance(event, Outcome):
                if event is TIMEOUT:
                    continue
                if event in (CLOSED, DATA_LOST):
                    q.stopped = event.value
                    break
                continue
            if isinstance(event, events.MarketDataType):
                q.reported = event.market_data_type
            elif isinstance(event, events.TickPrice):
                q.ticks[event.tick_type] += 1
                if event.price > 0:
                    q.price_ticks += 1
                    seen = (event.price, event.tick_type, run.clock() - started)
                    q.first = q.first or seen
                    q.last = seen
            elif isinstance(event, events.TickSize | events.TickString):
                q.ticks[event.tick_type] += 1
            elif isinstance(event, events.IbError):
                q.errors.append(event)
    finally:
        md.cancel_quotes(req_id)
    run.sleep(0.5)
    return q


def _print_quotes(run: _Run, q: _Quotes) -> None:
    reported = "none reported" if q.reported is None else f"{q.reported} ({_TYPE_NAMES.get(q.reported, '?')})"
    run.say(f"   requested type {q.requested}; IB reports market data type: {reported}")
    run.say(f"   ticks by type: {_tick_summary(q.ticks)}; price ticks: {q.price_ticks}")
    for label, tick in (("first", q.first), ("last", q.last)):
        if tick is not None:
            run.say(f"   {label} price tick: {tick[0]} (tick type {tick[1]}) at +{tick[2]:.1f} s")
    if q.stopped:
        run.say(f"   stream ended early: {q.stopped}")
    for err in q.errors:
        run.note(err.code, err.message)
        run.say(f"   IB error {err.code}: {err.message}")


def _step_quotes(run: _Run, md: IbMarketData, sym: _Symbol, seconds: float) -> None:
    first_wait = min(FIRST_TICK_WAIT, seconds)
    contract = sym.info.contract()
    live = _read_quotes(run, md, contract, 1, seconds, first_wait)
    _print_quotes(run, live)
    final, ticks = live, sum(live.ticks.values())
    if not live.price_ticks:
        run.say(f"   no price tick within {first_wait:g} s of live data; trying delayed (type 3)")
        final = _read_quotes(run, md, contract, 3, max(seconds - first_wait, FIRST_TICK_WAIT), None)
        _print_quotes(run, final)
        ticks += sum(final.ticks.values())
    sym.ticks = ticks
    if final.price_ticks:
        shown = final.reported if final.reported is not None else final.requested
        sym.verdict = _HEADLINES.get(shown, "LIVE data" if final.requested == 1 else "DELAYED data (15 min)")
        run.say(f"   {sym.symbol}: {sym.verdict}")
        run.ok(sym.symbol)
    else:
        sym.verdict = "NO data"
        sym.ok = False
        run.say(f"   {sym.symbol}: NO data")
        run.fail("no price tick with live or delayed data (see the IB errors above)")


def _step_history(run: _Run, md: IbMarketData, sym: _Symbol, day: date) -> None:
    end_utc = session_close_utc(day)
    run.say(
        f"   {sym.symbol}: 1 D of 1-minute bars (regular hours) ending "
        f"{end_utc:%Y-%m-%d %H:%M} UTC ({day})"
    )
    req_id = md.request_history(sym.info.contract(), end_utc, "1 D", use_rth=True)
    try:
        started = run.clock()
        seen_bars = -1
        while True:
            result = md.wait_history(req_id, 10.0)
            elapsed = run.clock() - started
            growing = len(result.bars) > seen_bars >= 0
            seen_bars = len(result.bars)
            if result.outcome is not TIMEOUT or elapsed >= HISTORY_HARD_CAP:
                break
            if elapsed >= HISTORY_WAIT and not growing:
                break
    finally:
        md.cancel_history(req_id)
    sym.history = result.outcome.value
    sym.bars = len(result.bars)
    run.say(f"   outcome: {result.outcome.value}; bars: {sym.bars}; late bars: {result.late_bars}")
    for err in result.errors:
        run.note(err.code, err.message)
        run.say(f"   IB error {err.code}: {err.message}")
    if result.bars:
        run.say(f"   raw date string of the first bar, as IB sent it: {result.bars[0].date!r}")
    bad = None
    if result.outcome is END and result.bars:
        try:
            first, last = bar_from_ib(result.bars[0]), bar_from_ib(result.bars[-1])
        except ValueError as exc:
            bad = f"a bar was refused by bar_from_ib: {exc}"
        else:
            sym.first_volume = str(first.volume)
            sym.first_time = f"{first.date:%H:%M}"
            sym.last_time = f"{last.date:%H:%M}"
            for label, bar in (("first", first), ("last", last)):
                run.say(
                    f"   {label} bar: {bar.date:%Y-%m-%d %H:%M} New York, open {bar.open}, high {bar.high}, "
                    f"low {bar.low}, close {bar.close}, volume {bar.volume}"
                )
            starts = (first.date.hour, first.date.minute) == (9, 30)
            ends = (last.date.hour, last.date.minute) == (15, 59)
            run.say(
                f"   first bar is 09:30: {'yes' if starts else 'NO'}; "
                f"last bar is 15:59: {'yes' if ends else 'NO'}"
            )
    if bad is not None:
        run.fail(bad)
    elif result.outcome is END and result.bars:
        run.ok(sym.symbol)
    else:
        sym.ok = False
        run.fail(f"{sym.symbol}: outcome {result.outcome.value} with {sym.bars} bars")


def _guarded(run: _Run, what: str, step: Callable[[], Any]) -> Any:
    """Run one step; an unexpected error fails the step and the probe goes on."""
    try:
        return step()
    except Exception as exc:
        run.fail(f"{what}: unexpected {type(exc).__name__}: {exc}")
        return None


def _summary(run: _Run, md: IbMarketData | None, header: list[str], symbols: list[_Symbol]) -> None:
    run.say()
    run.say("7. Summary (paste this back)")
    for line in header:
        run.say(f"   {line}")
    for sym in symbols:
        run.say(
            f"   {sym.symbol}: {sym.verdict}; ticks {sym.ticks}; history {sym.history}, {sym.bars} bars, "
            f"first {sym.first_time} last {sym.last_time} New York, first bar volume {sym.first_volume}"
        )
    if md is not None:
        for event in md.general_events:
            if isinstance(event, events.IbError):
                run.note(event.code, event.message)
    codes = "; ".join(f"{code}: {text}" for code, text in sorted(run.seen.items())) or "none"
    run.say(f"   IB codes seen: {codes}")


def run_probe(
    connection_factory: ConnectionFactory,
    market_data_factory: MarketDataFactory,
    args: Any,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime] | None = None,
) -> int:
    """Run the numbered steps over a connection built by `connection_factory(args)`.

    `clock` is monotonic seconds, `sleep` an interruptible sleep, `wall` the machine's clock
    as an aware datetime (default: now in UTC). Always closes the connection.
    """
    run = _Run(_Say(out), clock, sleep, wall or (lambda: datetime.now(UTC)))
    say = run.say
    try:
        return _run(run, connection_factory, market_data_factory, args)
    except KeyboardInterrupt:
        say()
        say("Interrupted (Ctrl-C). The connection was closed.")
        return EXIT_INTERRUPTED


def _run(
    run: _Run, connection_factory: ConnectionFactory, market_data_factory: MarketDataFactory, args: Any
) -> int:
    say = run.say
    conn = connection_factory(args)
    say("1. Connect")
    started = run.clock()
    try:
        conn.open()
    except (BrokerPortRefused, ClientIdRefused, BrokerConnectionRefusedHere) as exc:
        say(f"   REFUSED: {exc}")
        return EXIT_REFUSED
    except ConnectFailed as exc:
        for failure in exc.failures:
            run.note(failure.code, failure.message)
            say(f"   {failure.describe()}")
        run.fail(f"could not connect after {conn.attempts_made} attempt(s): {exc}")
        return EXIT_NO_CONNECTION
    except Exception as exc:
        run.fail(f"could not connect: {type(exc).__name__}: {exc}")
        return EXIT_NO_CONNECTION
    took = run.clock() - started
    with conn:
        md = market_data_factory(conn)
        connected = f"connected in {took:.1f} s, {conn.attempts_made} attempt(s)"
        run.ok(connected)
        symbols = [_Symbol(s) for s in args.symbol]
        _guarded(run, "data farms", lambda: _step_farms(run, conn))
        server = _guarded(run, "server time", lambda: _step_time(run, md))
        say("4. Contract details")
        for sym in symbols:
            _guarded(run, sym.symbol, lambda sym=sym: _step_contract(run, md, sym))
        found = [s for s in symbols if s.info is not None]
        say("5. Market data type and quotes")
        for sym in found:
            say(f"   {sym.symbol}")
            _guarded(run, sym.symbol, lambda sym=sym: _step_quotes(run, md, sym, args.quote_seconds))
        if args.no_history:
            say("6. History: skipped (--no-history)")
            for sym in found:
                sym.history = "skipped"
        else:
            say("6. History")
            if server is None:
                run.fail("no server time, so the last closed session cannot be derived; skipped")
            else:
                day = closed_session_day(server)
                for sym in found:
                    _guarded(run, sym.symbol, lambda sym=sym: _step_history(run, md, sym, day))
        header = [f"Connected: yes, {connected}", f"Farms: {run.farms}", f"Clock: {run.clock_note}"]
        _summary(run, md, header, symbols)
        say(f"   Result: {'a step FAILED' if run.failed else 'all steps completed'}")
    say("The output above contains no account number by design, but read it before you share it.")
    return EXIT_STEP_FAILED if run.failed else EXIT_OK


def _port_refusal_text(exc: Exception) -> str:
    return (
        f"{exc}\n"
        "To allow it for this terminal only, YOU would run this in PowerShell, then start the probe again:\n"
        f'    $env:{ALLOW_LIVE_ENV} = "1"\n'
        "This script never sets it."
    )


def _describe(args: Any, seconds: float | None = None) -> str:
    history = (
        "and one day of 1-minute bars" if not args.no_history else "and no historical bars (--no-history)"
    )
    return (
        f"Read-only data session. Connects to TWS at {args.host}:{args.port} with client id "
        f"{args.client_id}. Requests server time, contract details, about {args.quote_seconds:g} "
        f"seconds of quotes {history} for {', '.join(args.symbol)}. "
        "Places no orders; the client cannot send order messages."
    )


def _gate(args: Any, stdin: TextIO, input_fn: Callable[[str], str], out: TextIO) -> bool:
    say = _Say(out)
    isatty = getattr(stdin, "isatty", None)
    if isatty is None or not isatty():
        say("Refusing to run: this needs an interactive terminal (stdin is not one).")
        return False
    say(_describe(args))
    try:
        typed = input_fn(f"To proceed, type the port number ({args.port}) and press Enter: ")
    except (EOFError, KeyboardInterrupt):
        say("No confirmation given.")
        return False
    if typed.strip() != str(args.port):
        say("The text typed is not the port number. Nothing was started.")
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MainStoinker.ib.quick_probe",
        description="Owner-run read-only TWS data probe. Places no orders.",
    )
    parser.add_argument("--port", type=int, required=True, help="TWS or Gateway API port (no default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=21)
    parser.add_argument("--symbol", action="append", help=f"repeatable, at most {MAX_SYMBOLS}; default AAPL")
    parser.add_argument("--quote-seconds", type=float, default=20, help=f"1 to {MAX_QUOTE_SECONDS}")
    parser.add_argument("--connect-timeout", type=float, default=30, help="seconds; TWS may show a dialog")
    parser.add_argument("--no-history", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    symbols: list[str] = []
    for raw in args.symbol or ["AAPL"]:
        try:
            symbol = check_symbol(raw.strip().upper())
        except ValueError as exc:
            parser.error(f"bad --symbol {raw!r}: {exc}")
        if symbol not in symbols:
            symbols.append(symbol)
    if len(symbols) > MAX_SYMBOLS:
        parser.error(f"at most {MAX_SYMBOLS} symbols")
    args.symbol = symbols
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1 to 65535")
    if not 1 <= args.client_id <= MAX_CLIENT_ID or args.client_id in REFUSED_CLIENT_IDS:
        parser.error(f"--client-id must be 1 to {MAX_CLIENT_ID} and not {sorted(REFUSED_CLIENT_IDS)}")
    if not 1 <= args.quote_seconds <= MAX_QUOTE_SECONDS:
        parser.error(f"--quote-seconds must be 1 to {MAX_QUOTE_SECONDS}")
    if not 1 <= args.connect_timeout <= MAX_CONNECT_TIMEOUT:
        parser.error(f"--connect-timeout must be 1 to {MAX_CONNECT_TIMEOUT}")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
    out: TextIO | None = None,
    connection_factory: ConnectionFactory | None = None,
    market_data_factory: MarketDataFactory | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wall: Callable[[], datetime] | None = None,
) -> int:
    """Parse arguments, run the port check and the console gate, then `run_probe`.

    The keyword arguments are for tests; the defaults wire the real classes. This is the
    only place the process environment is read.
    """
    args = parse_args(argv)  # --help and argument errors exit here, before any client exists
    variables = os.environ if env is None else env
    stream = sys.stdout if out is None else out
    say = _Say(stream)
    try:
        check_broker_port(args.port, variables)
    except BrokerPortRefused as exc:
        say(_port_refusal_text(exc))
        return EXIT_REFUSED
    tty = sys.stdin if stdin is None else stdin
    ask = input if input_fn is None else input_fn
    if not _gate(args, tty, ask, stream):
        return EXIT_REFUSED
    logging.getLogger("ibapi").setLevel(logging.WARNING)

    def real_connection(a: Any) -> IbConnection:
        return IbConnection(
            a.host, a.port, a.client_id, env=variables, attempts=2, retry_delay=3,
            connect_timeout=a.connect_timeout, ready_timeout=15,
        )

    return run_probe(
        connection_factory or real_connection,
        market_data_factory or IbMarketData,
        args,
        stream,
        clock or time.monotonic,
        sleep or time.sleep,
        wall,
    )


if __name__ == "__main__":
    sys.exit(main())
