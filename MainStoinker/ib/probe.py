"""The `probe-data` command: what market data does this account get? Owner-run, data only.

    python -m MainStoinker probe-data --port 7496 --symbols AAPL,MSFT,TSLA

The command line, the checks before connecting (paths, port guard, console gate), the
scrubbed printer and the session recording are the ones of `fetch-bars` (`MainStoinker.ib.cli`);
this module holds the numbered steps only. It uses `IbConnection` and `IbMarketData` public
methods, which can send data requests and nothing else. Agents never run it.

Steps, each ending with an OK or FAILED line:

1. connected;
2. data farms (market, historical) and the farm names IB reported;
3. server time, in UTC and New York, and the difference from this machine's clock;
4. contract details per symbol;
5. per symbol: quotes with market data type 1 (live); if no price tick comes in 8 s, type 3
   (delayed). Verdict: LIVE data, DELAYED data (15 min) or NO data;
6. per symbol: one day of 1-minute regular-hours bars of the latest closed session (from IB's
   server time), with the day's total and median per-minute volume;
7. a summary to paste back.

Exit codes: see `PROBE_EXIT_CODES_TEXT` in `cli.py` (0 all done, delayed data counts as success;
1 connected but a step failed; 2 refused; 3 could not connect; 130 Ctrl-C).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from MainStoinker.ib import events
from MainStoinker.ib.bars import bar_from_ib
from MainStoinker.ib.cli import (
    ADVICE_FARM,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_NO_CONNECTION,
    EXIT_OK,
    EXIT_REFUSED,
    ConnectionFactory,
    MarketDataFactory,
    RecorderFactory,
    SessionKind,
    _Say,
    list_findings,
    run_session,
    scrub,
)
from MainStoinker.ib.connection import IbConnection
from MainStoinker.ib.history import session_close_utc
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
from MainStoinker.ib.recorder import LONG_RUN_REASON, SEPARATED_RUN_REASON, Finding

NY = ZoneInfo("America/New_York")
FIRST_TICK_WAIT = 8.0
HISTORY_WAIT = 60.0
HISTORY_HARD_CAP = 180.0
FARM_WAIT = 10.0
TIME_WAIT = 10.0
SMALL_VOLUME_SHARES = 100_000
"""A first bar below this many shares hints that IB sends US stock sizes in lots (a hint, not a failure)."""
SMALL_VOLUME_HINT = (
    "volume looks too small for shares: check that the TWS API setting that sends US stock sizes "
    "in lots is UNTICKED"
)
ADVICE_NO_DATA = ("NO data for a symbol: check the market data subscriptions of this account in the "
                  "Account Management site, and that no other TWS session is using the data (IB 10197).")
EXPECTED_TICK_EVENT = "tickString"
PROBE_LABELS = {
    EXIT_OK: "all steps done",
    EXIT_FAILED: "connected, but a step failed",
    EXIT_REFUSED: "refused, nothing was probed",
    EXIT_NO_CONNECTION: "could not connect",
    EXIT_INTERRUPTED: "interrupted with Ctrl-C",
}

_TICK_NAMES = {1: "bid", 2: "ask", 4: "last", 66: "bid (delayed)", 67: "ask (delayed)", 68: "last (delayed)"}
_TYPE_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed frozen"}
_HEADLINES = {
    1: "LIVE data",
    2: "LIVE data (frozen)",
    3: "DELAYED data (15 min)",
    4: "DELAYED data (15 min, frozen)",
}
_FARM_CODES = {
    2104: "market", 2108: "market",
    2106: "historical", 2107: "historical",
    2158: "security definitions",
}
_FARM_KINDS_SHOWN = ("market", "historical", "security definitions")
_DEMAND_MARK = "upon demand."
_DIGIT_RUN_REASONS = frozenset({LONG_RUN_REASON, SEPARATED_RUN_REASON})  # an epoch; RT volume numbers
_EPOCH_VALUE =re.compile(r"[0-9]+")
_RT_VOLUME_VALUE = re.compile(r"[0-9.;]+|[0-9.;]*;(?:true|false)")
_IB_ERROR_IN_TEXT =re.compile(r"IB error (\d+):")


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
    total_volume: str = "-"
    median_volume: str = "-"
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
    up: dict[str, bool] | None = None
    time_error: str = ""
    clock_note: str = "no server time"
    server: datetime | None = None

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


def farm_names(md: IbMarketData) -> dict[str, list[str]]:
    """The farm names IB reported, per kind, in arrival order without duplicates.

    Codes 2104 / 2106 / 2158 end with `:name`; 2107 / 2108 (inactive but available on
    demand) end with `.name`. A message whose name cannot be parsed adds nothing.
    """
    found: dict[str, list[str]] = {}
    for event in md.general_events:
        if not (isinstance(event, events.IbError) and event.code in _FARM_CODES):
            continue
        message = event.message
        if ":" in message:
            name = message.rsplit(":", 1)[-1].strip()
        elif _DEMAND_MARK in message:
            name = message.rsplit(_DEMAND_MARK, 1)[-1].strip()
        else:
            name = ""
        name = scrub(name) if name else ""
        if name and name not in found.setdefault(_FARM_CODES[event.code], []):
            found[_FARM_CODES[event.code]].append(name)
    return {kind: names for kind, names in found.items() if names}


def _farm_line(up: dict[str, bool], names: dict[str, list[str]]) -> str:
    parts = []
    for kind in _FARM_KINDS_SHOWN:
        if kind in names:
            parts.append(f"{', '.join(names[kind])} ({kind})")
        elif up.get(kind):
            parts.append(f"name not parsed ({kind})")
    return ", ".join(parts) if parts else "none"


def _step_farms(run: _Run, conn: IbConnection) -> None:
    run.say("2. Data farms")
    up = {kind: conn.wait_for_data_farm(FARM_WAIT, farm=kind) for kind in ("market", "historical")}
    run.up = up
    run.farms = ", ".join(f"{kind} {'up' if value else 'NOT seen'}" for kind, value in up.items())


def _ask_time(run: _Run, md: IbMarketData) -> None:
    """Ask for the clock now (step 3 prints it): IB's farm messages reach `general_events`
    only while the market-data object reads events, so this comes before naming the farms."""
    try:
        md.request_server_time()
        run.server = md.wait_server_time(TIME_WAIT)
    except DataLost:
        run.server = None
    except Exception as exc:  # noqa: BLE001 - reported by step 3, not as "no answer"
        run.time_error = f"{type(exc).__name__}: {exc}"


def _report_farms(run: _Run, md: IbMarketData) -> None:
    if run.up is None:
        return
    run.say(f"   farms IB reported: {_farm_line(run.up, farm_names(md))}")
    if all(run.up.values()):
        run.ok(run.farms)
    else:
        run.fail(run.farms)


def _step_time(run: _Run) -> datetime | None:
    """Step 3: print the clock that `_ask_time` asked for."""
    run.say("3. Server time")
    server = run.server
    if run.time_error:
        run.fail(f"the server-time request failed: {run.time_error}")
        return None
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
        code = getattr(exc, "code", None)
        if code is None:  # "not found" carries its IB code only in the text
            match = _IB_ERROR_IN_TEXT.search(str(exc))
            code = int(match.group(1)) if match else None
        run.note(code, str(exc))
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
            converted = [bar_from_ib(bar) for bar in result.bars]
        except ValueError as exc:
            bad = f"a bar was refused by bar_from_ib: {exc}"
        else:
            first, last = converted[0], converted[-1]
            volumes = [bar.volume for bar in converted]
            sym.first_volume = str(first.volume)
            sym.first_time = f"{first.date:%H:%M}"
            sym.last_time = f"{last.date:%H:%M}"
            sym.total_volume = f"{sum(volumes):.0f}"
            sym.median_volume = f"{statistics.median(volumes):.0f}"
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
            run.say(f"   day total volume: {sym.total_volume}; median per-minute volume: {sym.median_volume}")
            if first.volume < SMALL_VOLUME_SHARES:
                run.say(f"   volume check: first bar volume {first.volume}: {SMALL_VOLUME_HINT}")
            else:
                run.say(f"   volume check: first bar volume {first.volume} is plausible for shares")
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
    except Exception as exc:  # noqa: BLE001 - KeyboardInterrupt is not an Exception and passes
        run.fail(f"{what}: unexpected {type(exc).__name__}: {exc}")
        return None


def _summary(run: _Run, md: IbMarketData, header: list[str], symbols: list[_Symbol]) -> None:
    run.say()
    run.say("7. Summary (paste this back)")
    for line in header:
        run.say(f"   {line}")
    for sym in symbols:
        run.say(
            f"   {sym.symbol}: {sym.verdict}; ticks {sym.ticks}; history {sym.history}, {sym.bars} bars, "
            f"first {sym.first_time} last {sym.last_time} New York, first bar volume {sym.first_volume}, "
            f"day total volume {sym.total_volume}, median per-minute volume {sym.median_volume}"
        )
    for event in md.general_events:
        if isinstance(event, events.IbError):
            run.note(event.code, event.message)
    codes = "; ".join(f"{code}: {text}" for code, text in sorted(run.seen.items())) or "none"
    run.say(f"   IB codes seen: {codes}")


def run_steps(
    say: _Say,
    conn: IbConnection,
    md: IbMarketData,
    advice: list[str],
    args: argparse.Namespace,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime] | None = None,
) -> int:
    """The numbered steps over an open connection; returns 0, or 1 when a step failed."""
    run = _Run(say, clock, sleep, wall or (lambda: datetime.now(UTC)))
    connected = f"connected after {conn.attempts_made} attempt(s)"
    say("1. Connect")
    run.ok(connected)
    symbols = [_Symbol(s) for s in args.symbols]
    _guarded(run, "data farms", lambda: _step_farms(run, conn))
    _ask_time(run, md)
    _guarded(run, "data farms", lambda: _report_farms(run, md))
    if "historical NOT seen" in run.farms:
        advice.append(ADVICE_FARM)
    server = _guarded(run, "server time", lambda: _step_time(run))
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
    if any(s.verdict == "NO data" for s in symbols):
        advice.append(ADVICE_NO_DATA)
    say(f"   Result: {'a step FAILED' if run.failed else 'all steps completed'}")
    return EXIT_FAILED if run.failed else EXIT_OK


# -- the recording check ----------------------------------------------------------------------


def split_findings(record: Path, findings: list[Finding]) -> tuple[int, list[Finding]]:
    """(expected findings, all the others). Streamed quotes carry last-trade times (tick type
    45, an epoch) and RT volume (type 48, `price;size;ms-epoch;total;vwap;flag`) that the scan
    flags as a digit run (a known limitation, issue 156). A finding is expected only when its
    line is a `tickString` of type 45 or 48 whose value has no other characters than those
    formats allow, and its reason is the digit-run one."""
    allowed: set[int] = set()
    try:
        lines = record.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return 0, list(findings)
    for number, content in enumerate(lines, 1):
        try:
            parsed = json.loads(content)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("name") == EXPECTED_TICK_EVENT:
            args = parsed.get("args")
            if isinstance(args, list) and len(args) == 3 and _expected_tick(args[1], args[2]):
                allowed.add(number)
    def is_expected(f: Finding) -> bool:
        return f.line in allowed and f.reason in _DIGIT_RUN_REASONS

    expected = [f for f in findings if is_expected(f)]
    other = [f for f in findings if not is_expected(f)]
    return len(expected), other


def _expected_tick(tick_type: Any, value: Any) -> bool:
    if not isinstance(value, str) or isinstance(tick_type, bool):
        return False
    if tick_type == 45:
        return _EPOCH_VALUE.fullmatch(value) is not None
    if tick_type == 48:
        return _RT_VOLUME_VALUE.fullmatch(value) is not None
    return False


def report_probe_findings(say: _Say, record: Path, findings: list[Finding]) -> None:
    expected, other = split_findings(record, findings)
    say(f"Recording check: {len(findings)} finding(s): {expected} from last-trade-time and volume ticks "
        f"(expected), {len(other)} other. Read the file yourself before it is ever committed.")
    list_findings(say, other)


PROBE_KIND = SessionKind(
    labels=PROBE_LABELS,
    interrupted="Interrupted (Ctrl-C). The connection and the recording were closed.",
    unexpected=EXIT_FAILED,
    report_findings=report_probe_findings,
)


def run_probe_data(
    args: argparse.Namespace,
    *,
    connection_factory: ConnectionFactory,
    market_data_factory: MarketDataFactory,
    recorder_factory: RecorderFactory,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wall: Callable[[], datetime] | None = None,
) -> int:
    """Connect, run the steps, report; always closes the connection and the recorder.

    `args` carries `symbols`, `quote_seconds`, `no_history`, `record` (Path or None),
    `private`, `host`, `port`, `client_id` and `connect_timeout`. Returns the exit code.
    """

    def body(say: _Say, conn: IbConnection, md: IbMarketData, advice: list[str]) -> int:
        return run_steps(say, conn, md, advice, args, clock, sleep, wall)

    return run_session(args, PROBE_KIND, body, connection_factory=connection_factory,
                       market_data_factory=market_data_factory, recorder_factory=recorder_factory, out=out)
