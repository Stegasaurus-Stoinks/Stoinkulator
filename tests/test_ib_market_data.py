"""`MainStoinker/ib/market_data.py` and `contracts.py`, over a real `IbConnection` and the scripted fake.

Nothing opens a socket: the connection's client factory builds a `FakeIbClient`
whose wrapper is a real `DataOnlyClient` (never connected), so callbacks become
the same events as in production. Time is injected: `TickClock` moves a little
on every read and when the code sleeps, so timeouts are reached without real
waiting (the waits block for a couple of real milliseconds per poll at most).
"""

from __future__ import annotations

import ast
import inspect
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ibapi.common import TickAttrib
from ibapi.contract import Contract, ContractDetails

from MainStoinker.ib import bars as bars_module
from MainStoinker.ib import contracts as contracts_module
from MainStoinker.ib import events
from MainStoinker.ib import market_data as market_data_module
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import DATA_REQUESTS, IbConnection, NotConnected
from MainStoinker.ib.contracts import stock
from MainStoinker.ib.history import format_end_date_time
from MainStoinker.ib.market_data import (
    CLOSED,
    END,
    ERROR,
    LOST,
    TIMEOUT,
    ConcurrentWait,
    IbMarketData,
    ResolveAmbiguous,
    ResolveConnectionClosed,
    ResolveConnectionLost,
    ResolveError,
    ResolveFailed,
    ResolveNotFound,
    ResolveTimeout,
    StockInfo,
)
from tests.fake_ib import FakeIbClient, make_bar

WAIT = 5.0
FORBIDDEN_METHODS = (
    "placeOrder", "placeOrderProtoBuf", "cancelOrder", "cancelOrderProtoBuf", "reqGlobalCancel",
    "reqGlobalCancelProtoBuf", "exerciseOptions", "reqIds", "reqOpenOrders", "reqAllOpenOrders",
    "reqAutoOpenOrders", "reqExecutions", "reqCompletedOrders", "reqPositions",
    "reqAccountUpdates",
)
MARKET_DATA_DIR = Path(market_data_module.__file__).resolve().parent
NEW_MODULES = ("market_data.py", "contracts.py", "bars.py")


class TickClock:
    """A clock that moves by `tick` on every read and by the slept time on every sleep."""

    def __init__(self, tick: float = 0.05) -> None:
        self.now = 0.0
        self.tick = tick
        self.reads = 0
        self.on_read: Callable[[int], None] | None = None
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            value = self.now
            self.now += self.tick
            self.reads += 1
            reads = self.reads
        if self.on_read is not None:
            self.on_read(reads)
        return value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@dataclass
class Rig:
    conn: IbConnection
    md: IbMarketData
    fake: FakeIbClient
    clock: TickClock


@pytest.fixture
def rig() -> Iterator[Rig]:
    clock = TickClock()
    fakes: list[FakeIbClient] = []

    def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger))
        fakes.append(fake)
        return fake

    conn = IbConnection(
        "127.0.0.1", 7497, 21, env={}, client_factory=factory, clock=clock, sleep=clock.sleep,
        poll_interval=0.002,
    )
    conn.open()
    try:
        yield Rig(conn, IbMarketData(conn, clock=clock, poll_interval=0.01), fakes[-1], clock)
    finally:
        conn.close()


def details(con_id: int, symbol: str = "AAPL", primary: str = "NASDAQ",
            sec_type: str = "STK") -> ContractDetails:
    d = ContractDetails()
    d.contract = Contract()
    d.contract.conId = con_id
    d.contract.symbol = symbol
    d.contract.secType = sec_type
    d.contract.exchange = "SMART"
    d.contract.primaryExchange = primary
    d.contract.currency = "USD"
    d.minTick = 0.01
    d.timeZoneId = "US/Eastern"
    return d


def script_details(rig: Rig, *found: ContractDetails, end: bool = True) -> None:
    def handler(req_id: int, contract: Contract) -> list[tuple[Any, ...]]:
        played: list[tuple[Any, ...]] = [("contractDetails", req_id, d) for d in found]
        if end:
            played.append(("contractDetailsEnd", req_id))
        return played

    rig.fake.respond("reqContractDetails", handler)


def script_history(rig: Rig, *epochs: int, end: bool = True) -> None:
    def handler(req_id: int, *rest: Any) -> list[tuple[Any, ...]]:
        played: list[tuple[Any, ...]] = [
            ("historicalData", req_id, make_bar(e, 10, 11, 9, 10.5, 100)) for e in epochs
        ]
        if end:
            played.append(("historicalDataEnd", req_id, "start", "end"))
        return played

    rig.fake.respond("reqHistoricalData", handler)


def error_played(req_id: int, code: int, text: str) -> tuple[Any, ...]:
    return ("error", req_id, 1_700_000_000_000, code, text, "")


AAPL = stock("AAPL", "NASDAQ", 265598)
END_UTC = datetime(2024, 8, 16, 20, 0, tzinfo=UTC)


# -- the narrow face ----------------------------------------------------------


def test_market_data_has_no_order_methods(rig: Rig) -> None:
    for name in FORBIDDEN_METHODS:
        assert not hasattr(IbMarketData, name), name
        assert not hasattr(rig.md, name), name
    for name, value in vars(rig.md).items():
        if name.startswith("_"):
            continue
        assert not isinstance(value, IbConnection | FakeIbClient | DataOnlyClient), name
    assert not hasattr(rig.md, "connection") and not hasattr(rig.md, "client")
    public = {n for n in dir(IbMarketData) if not n.startswith("_")}
    assert public == {
        "request_history", "cancel_history", "set_market_data_type", "request_quotes",
        "cancel_quotes", "request_server_time", "wait_history", "next_event_for",
        "wait_server_time", "resolve_stock", "general_events", "poll_event", "poll_server_time",
    }


def test_new_modules_name_nothing_about_trading_or_accounts() -> None:
    banned = ("order", "execution", "position", "account", "exercise", "globalcancel")
    for name in NEW_MODULES:
        source = (MARKET_DATA_DIR / name).read_text(encoding="utf-8")
        assert "os.environ" not in source and "environ" not in source, name
        assert "socket" not in source.lower().replace("sockets", ""), name
        tree = ast.parse(source)
        seen: set[str] = set()
        docstrings = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.FunctionDef | ast.ClassDef) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if isinstance(node, ast.Name):
                seen.add(node.id)
            elif isinstance(node, ast.Attribute):
                seen.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                seen.add(node.name)
            elif isinstance(node, ast.arg):
                seen.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen.add(node.value)
        for word in seen:
            assert not any(b in word.lower() for b in banned), (name, word)


def test_new_modules_import_only_allowed_ibapi_parts() -> None:
    for name in NEW_MODULES:
        tree = ast.parse((MARKET_DATA_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("ibapi"):
                    assert node.module == "ibapi.contract", (name, node.module)
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith("ibapi") for a in node.names), name


def test_market_data_uses_only_data_requests(rig: Rig) -> None:
    script_details(rig, details(265598))
    script_history(rig, 1723815000)
    info = rig.md.resolve_stock("AAPL")
    rig.md.set_market_data_type(3)
    hist = rig.md.request_history(info.contract(), END_UTC, "1 D")
    rig.md.wait_history(hist, 5)
    rig.md.cancel_history(hist)
    live = rig.md.request_history(info.contract(), None, "300 S", keep_up_to_date=True)
    rig.md.cancel_history(live)
    quotes = rig.md.request_quotes(info.contract())
    rig.md.cancel_quotes(quotes)
    rig.md.request_server_time()
    names = set(rig.fake.call_names)
    assert names >= {
        "reqContractDetails", "reqMarketDataType", "reqHistoricalData", "cancelHistoricalData",
        "reqMktData", "cancelMktData", "reqCurrentTime",
    }
    lifecycle = {"connect", "run", "disconnect"}  # the connection's own calls, not requests
    assert names - lifecycle <= DATA_REQUESTS
    for forbidden in FORBIDDEN_METHODS:
        assert forbidden not in names


# -- contracts -----------------------------------------------------------------


def test_stock_contract_is_stk_smart_usd_and_never_smart_primary() -> None:
    c = stock("AAPL")
    assert (c.symbol, c.secType, c.exchange, c.currency) == ("AAPL", "STK", "SMART", "USD")
    assert c.primaryExchange == "" and c.conId == 0
    resolved = stock("MSFT", "NASDAQ", 272093)
    assert (resolved.primaryExchange, resolved.conId, resolved.exchange) == (
        "NASDAQ", 272093, "SMART")
    assert stock("AAPL") is not stock("AAPL")
    with pytest.raises(ValueError, match="SMART"):
        stock("AAPL", "SMART")


@pytest.mark.parametrize("symbol", ["", "aapl", "A B", "../X", "CON", "NUL", "AAPL.", "A\\B", None])
def test_stock_refuses_invalid_symbols(symbol: Any) -> None:
    with pytest.raises(ValueError):
        stock(symbol)


@pytest.mark.parametrize("primary", ["smart", "N", "NAS DAQ", "NASDAQ;", None, 5])
def test_stock_refuses_invalid_primary_exchange(primary: Any) -> None:
    with pytest.raises(ValueError):
        stock("AAPL", primary)


@pytest.mark.parametrize("con_id", [-1, True, 1.5, "7"])
def test_stock_refuses_invalid_con_id(con_id: Any) -> None:
    with pytest.raises(ValueError):
        stock("AAPL", "", con_id)


def test_contracts_module_has_no_other_builders() -> None:
    assert contracts_module.SEC_TYPE == "STK"
    assert bars_module.DATA_TIME_ZONE == "America/New_York"


# -- history -------------------------------------------------------------------


def test_history_request_arguments(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    (call,) = rig.fake.calls_named("reqHistoricalData")
    rid, contract, end, duration, bar_size, what, rth, fmt, keep, options = call.args
    assert rid == req_id
    assert (contract.symbol, contract.secType, contract.exchange, contract.currency) == (
        "AAPL", "STK", "SMART", "USD")
    assert (contract.primaryExchange, contract.conId) == ("NASDAQ", 265598)
    assert end == "20240816-20:00:00" == format_end_date_time(END_UTC)
    assert (duration, bar_size, what, rth, fmt, keep, options) == (
        "1 D", "1 min", "TRADES", 1, 2, False, [])
    assert type(rth) is int and type(fmt) is int and keep is False


def test_history_end_time_is_converted_to_utc_from_any_zone(rig: Rig) -> None:
    ny = timezone(timedelta(hours=-4))
    rig.md.request_history(AAPL, datetime(2024, 8, 16, 16, 0, tzinfo=ny), "1 D", use_rth=False)
    args = rig.fake.calls_named("reqHistoricalData")[0].args
    assert args[2] == "20240816-20:00:00" and args[6] == 0


def test_keep_up_to_date_uses_empty_end_time(rig: Rig) -> None:
    rig.md.request_history(AAPL, None, "300 S", keep_up_to_date=True)
    args = rig.fake.calls_named("reqHistoricalData")[0].args
    assert args[2] == "" and args[8] is True and args[7] == 2
    with pytest.raises(ValueError, match="keep_up_to_date"):
        rig.md.request_history(AAPL, END_UTC, "300 S", keep_up_to_date=True)
    assert len(rig.fake.calls_named("reqHistoricalData")) == 1  # the refusal sent nothing


def test_history_without_end_time_asks_for_now(rig: Rig) -> None:
    rig.md.request_history(AAPL, None, "1 D")
    assert rig.fake.calls_named("reqHistoricalData")[0].args[2] == ""


@pytest.mark.parametrize("duration", ["", "1D", "1 d", "0 D", "1 X", "1 D ", "-1 D", "1 day", None, 5])
def test_history_refuses_a_malformed_duration(rig: Rig, duration: Any) -> None:
    with pytest.raises(ValueError, match="duration"):
        rig.md.request_history(AAPL, END_UTC, duration)
    assert not rig.fake.calls_named("reqHistoricalData")


def test_history_refuses_bad_arguments_without_sending(rig: Rig) -> None:
    with pytest.raises(ValueError):
        rig.md.request_history(AAPL, datetime(2024, 8, 16, 20, 0), "1 D")  # naive
    with pytest.raises(ValueError):
        rig.md.request_history(AAPL, END_UTC, "1 D", use_rth=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rig.md.request_history(AAPL, END_UTC, "1 D", keep_up_to_date="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rig.md.request_history("AAPL", END_UTC, "1 D")  # type: ignore[arg-type]
    option = Contract()
    option.secType = "OPT"
    with pytest.raises(ValueError, match="stock"):
        rig.md.request_history(option, END_UTC, "1 D")
    assert not rig.fake.calls_named("reqHistoricalData")


def test_wait_history_collects_bars_and_end(rig: Rig) -> None:
    script_history(rig, 1723815000, 1723815060)
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    result = rig.md.wait_history(req_id, 5)
    assert result.outcome == END and result.req_id == req_id
    assert [b.date for b in result.bars] == ["1723815000", "1723815060"]
    assert result.bars[0].volume == Decimal(100)
    assert result.end == events.HistoricalDataEnd(req_id, "start", "end")
    assert result.errors == () and result.failures == ()


def test_wait_history_error_162_is_reported_with_its_text(rig: Rig) -> None:
    text = "Historical Market Data Service error message:HMDS query returned no data: AAPL@SMART Trades"
    rig.fake.respond("reqHistoricalData", lambda req_id, *a: [error_played(req_id, 162, text)])
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    result = rig.md.wait_history(req_id, 5)
    assert result.outcome == ERROR and result.bars == () and result.end is None
    (err,) = result.failures
    assert (err.code, err.message, err.req_id) == (162, text, req_id)


def test_wait_history_notice_does_not_end_the_wait(rig: Rig) -> None:
    def handler(req_id: int, *rest: Any) -> list[tuple[Any, ...]]:
        return [
            error_played(req_id, 2174, "Warning: time zone"),
            ("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 100)),
            ("historicalDataEnd", req_id, "s", "e"),
        ]

    rig.fake.respond("reqHistoricalData", handler)
    result = rig.md.wait_history(rig.md.request_history(AAPL, END_UTC, "1 D"), 5)
    assert result.outcome == END and len(result.bars) == 1
    assert [e.code for e in result.errors] == [2174] and result.failures == ()


def test_wait_history_times_out_on_the_injected_clock(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    before = rig.clock.now
    result = rig.md.wait_history(req_id, 30)
    assert result.outcome == TIMEOUT and result.bars == ()
    assert 30 <= rig.clock.now - before < 40  # simulated seconds, not real ones


def test_wait_history_keeps_partial_bars_on_timeout(rig: Rig) -> None:
    script_history(rig, 1723815000, end=False)
    result = rig.md.wait_history(rig.md.request_history(AAPL, END_UTC, "1 D"), 3)
    assert result.outcome == TIMEOUT and len(result.bars) == 1 and result.end is None


def test_wait_history_reports_a_connection_that_closes(rig: Rig) -> None:
    script_history(rig, 1723815000, end=False)
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop("connectionClosed")
    result = rig.md.wait_history(req_id, 30)
    assert result.outcome == CLOSED and len(result.bars) == 1
    assert rig.md.wait_history(req_id, 30).outcome == CLOSED  # and stays closed


def test_wait_history_reports_lost_connectivity_and_resumes_after_restore(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    before = rig.clock.now
    assert rig.md.wait_history(req_id, 30).outcome == LOST
    assert rig.clock.now - before >= 30  # T6 fix round: the timeout is honoured while lost
    rig.fake.drop(1102)
    rig.fake.push("historicalDataEnd", req_id, "s", "e")
    # The restore and the end marker are delivered by the reader thread a moment later; a wait
    # that is still before its deadline keeps reading until they arrive.
    outcome = LOST
    for _ in range(5000):
        outcome = rig.md.wait_history(req_id, 30).outcome
        if outcome != LOST:
            break
        time.sleep(0.001)
    assert outcome == END


def test_events_of_other_requests_are_neither_consumed_nor_lost(rig: Rig) -> None:
    first = rig.md.request_history(AAPL, END_UTC, "1 D")
    second = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("historicalData", first, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.push("historicalData", second, make_bar(1723815060, 10, 11, 9, 10.5, 2))
    rig.fake.push("historicalDataEnd", second, "s", "e")
    rig.fake.push("historicalData", first, make_bar(1723815120, 10, 11, 9, 10.5, 3))
    rig.fake.push("historicalDataEnd", first, "s", "e")
    later = rig.md.wait_history(second, 5)
    earlier = rig.md.wait_history(first, 5)
    assert [b.volume for b in later.bars] == [Decimal(2)] and later.outcome == END
    assert [b.volume for b in earlier.bars] == [Decimal(1), Decimal(3)] and earlier.outcome == END


def test_next_event_for_streams_updates_after_the_end_marker(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, None, "300 S", keep_up_to_date=True)
    rig.fake.push("historicalDataEnd", req_id, "s", "e")
    rig.fake.push("historicalDataUpdate", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 4))
    assert rig.md.wait_history(req_id, 5).outcome == END
    update = rig.md.next_event_for(req_id, 5)
    assert isinstance(update, events.HistoricalBar) and update.update is True
    assert rig.md.next_event_for(req_id, 1) is None


def test_wait_for_an_unknown_request_id_fails_loudly(rig: Rig) -> None:
    with pytest.raises(ValueError, match="not an open"):
        rig.md.wait_history(999, 1)
    with pytest.raises(ValueError, match="not an open"):
        rig.md.next_event_for(999, 1)
    quotes = rig.md.request_quotes(AAPL)
    with pytest.raises(ValueError, match="history"):
        rig.md.wait_history(quotes, 1)


def test_a_second_wait_while_one_runs_raises_concurrent_wait(rig: Rig) -> None:
    rig.clock.tick = 0.0  # the first wait blocks until its event arrives
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    other = rig.md.request_history(AAPL, END_UTC, "2 D")
    inside = threading.Event()
    main_id = threading.get_ident()
    waiter_reads = [0]

    def on_read(reads: int) -> None:
        # the waiter's reads 1 and 2 build the deadline; from the third it holds the wait lock
        if threading.get_ident() != main_id:
            waiter_reads[0] += 1
            if waiter_reads[0] >= 3:
                inside.set()

    rig.clock.on_read = on_read
    results: list[Any] = []
    thread = threading.Thread(
        target=lambda: results.append(rig.md.wait_history(req_id, 100)), daemon=True)
    thread.start()
    try:
        assert inside.wait(WAIT)
        with pytest.raises(ConcurrentWait):
            rig.md.wait_history(other, 1)
        with pytest.raises(ConcurrentWait):
            rig.md.wait_server_time(1)
        with pytest.raises(ConcurrentWait):
            rig.md.next_event_for(other, 1)
    finally:
        rig.fake.push("historicalDataEnd", req_id, "s", "e")
        thread.join(WAIT)
    assert not thread.is_alive() and results[0].outcome == END
    # the lock is released: a wait for the other request works now
    rig.fake.push("historicalDataEnd", other, "s", "e")
    assert rig.md.wait_history(other, 5).outcome == END


def test_cancelled_requests_drop_late_events_and_unknown_ids_go_to_general(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.cancel_history(req_id) is True
    rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.push("historicalDataEnd", 4242, "s", "e")  # an id this object never issued
    rig.md.request_server_time()
    rig.fake.push("currentTime", 1_723_815_000)
    assert rig.md.wait_server_time(5) == datetime.fromtimestamp(1_723_815_000, tz=UTC)
    assert rig.md.late_events == 1
    assert events.HistoricalDataEnd(4242, "s", "e") in rig.md.general_events


# -- cancels -------------------------------------------------------------------


def test_cancel_history_is_idempotent(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, None, "300 S", keep_up_to_date=True)
    assert rig.md.cancel_history(req_id) is True
    assert rig.md.cancel_history(req_id) is False
    assert [c.args for c in rig.fake.calls_named("cancelHistoricalData")] == [(req_id,)]


def test_cancel_of_a_finished_history_request_sends_nothing(rig: Rig) -> None:
    script_history(rig, 1723815000)
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.wait_history(req_id, 5).outcome == END
    assert rig.md.cancel_history(req_id) is False
    assert not rig.fake.calls_named("cancelHistoricalData")


def test_cancels_after_the_connection_closed_are_silent(rig: Rig) -> None:
    hist = rig.md.request_history(AAPL, None, "300 S", keep_up_to_date=True)
    quotes = rig.md.request_quotes(AAPL)
    rig.conn.close()
    assert rig.md.cancel_history(hist) is False
    assert rig.md.cancel_quotes(quotes) is False
    assert rig.md.cancel_history(hist) is False and rig.md.cancel_quotes(quotes) is False
    with pytest.raises(NotConnected):
        rig.md.request_history(AAPL, END_UTC, "1 D")
    with pytest.raises(NotConnected):
        rig.md.request_quotes(AAPL)
    with pytest.raises(NotConnected):
        rig.md.set_market_data_type(3)


def test_a_failed_request_leaves_no_state_behind(rig: Rig) -> None:
    rig.conn.close()
    with pytest.raises(NotConnected):
        rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.cancel_history(1) is False  # the failed id counts as cancelled: nothing sent
    assert not rig.fake.calls_named("cancelHistoricalData")
    with pytest.raises(ValueError):
        rig.md.wait_history(1, 1)  # and it is not open


def test_cancel_refuses_ids_it_did_not_issue_or_of_the_other_kind(rig: Rig) -> None:
    quotes = rig.md.request_quotes(AAPL)
    hist = rig.md.request_history(AAPL, END_UTC, "1 D")
    with pytest.raises(ValueError):
        rig.md.cancel_history(quotes)
    with pytest.raises(ValueError):
        rig.md.cancel_quotes(hist)
    with pytest.raises(ValueError):
        rig.md.cancel_quotes(12345)
    assert not rig.fake.calls_named("cancelMktData") and not rig.fake.calls_named(
        "cancelHistoricalData")


# -- quotes and market data type -------------------------------------------------


def test_quotes_never_regulatory_snapshot(rig: Rig) -> None:
    req_id = rig.md.request_quotes(AAPL)
    (call,) = rig.fake.calls_named("reqMktData")
    rid, contract, generic, snapshot, regulatory, options = call.args
    assert rid == req_id and contract.symbol == "AAPL"
    assert generic == "" and snapshot is False and regulatory is False and options == []
    assert list(inspect.signature(IbMarketData.request_quotes).parameters) == ["self", "contract"]
    assert rig.md.cancel_quotes(req_id) is True
    assert [c.args for c in rig.fake.calls_named("cancelMktData")] == [(req_id,)]
    assert rig.md.cancel_quotes(req_id) is False


def test_quote_events_reach_next_event_for(rig: Rig) -> None:
    req_id = rig.md.request_quotes(AAPL)
    rig.fake.push("tickPrice", req_id, 4, 190.5, TickAttrib())
    rig.fake.push("tickSize", req_id, 5, Decimal(300))
    first = rig.md.next_event_for(req_id, 5)
    second = rig.md.next_event_for(req_id, 5)
    assert isinstance(first, events.TickPrice) and first.price == 190.5
    assert isinstance(second, events.TickSize) and second.size == Decimal(300)


@pytest.mark.parametrize("value", [1, 2, 3, 4])
def test_set_market_data_type_accepts_ib_values(rig: Rig, value: int) -> None:
    rig.md.set_market_data_type(value)
    assert rig.fake.calls_named("reqMarketDataType")[-1].args == (value,)


@pytest.mark.parametrize("value", [0, 5, -1, True, 3.0, "3", None])
def test_set_market_data_type_refuses_other_values(rig: Rig, value: Any) -> None:
    with pytest.raises(ValueError, match="market data type"):
        rig.md.set_market_data_type(value)
    assert not rig.fake.calls_named("reqMarketDataType")


def test_server_time(rig: Rig) -> None:
    rig.md.request_server_time()
    assert rig.fake.calls_named("reqCurrentTime")
    assert rig.md.wait_server_time(1) is None  # nothing answered: timeout on the injected clock
    rig.fake.push("currentTime", 1_733_149_800)
    assert rig.md.wait_server_time(5) == datetime(2024, 12, 2, 14, 30, tzinfo=UTC)


# -- resolve_stock -----------------------------------------------------------------


def test_resolve_stock_returns_con_id(rig: Rig) -> None:
    script_details(rig, details(265598))
    info = rig.md.resolve_stock("AAPL")
    assert info == StockInfo("AAPL", 265598, "NASDAQ", "USD", 0.01, "US/Eastern")
    (call,) = rig.fake.calls_named("reqContractDetails")
    sent = call.args[1]
    assert (sent.symbol, sent.secType, sent.exchange, sent.currency, sent.primaryExchange) == (
        "AAPL", "STK", "SMART", "USD", "")
    contract = info.contract()
    assert (contract.conId, contract.primaryExchange, contract.exchange) == (
        265598, "NASDAQ", "SMART")
    assert info.contract() is not info.contract()


def test_resolve_stock_the_same_contract_twice_is_one_match(rig: Rig) -> None:
    script_details(rig, details(265598), details(265598))
    assert rig.md.resolve_stock("AAPL").con_id == 265598


def test_resolve_stock_no_match(rig: Rig) -> None:
    script_details(rig)
    with pytest.raises(ResolveNotFound, match="no contract"):
        rig.md.resolve_stock("AAPL")


def test_resolve_stock_ambiguous(rig: Rig) -> None:
    script_details(rig, details(1, primary="NYSE"), details(2, primary="ARCA"))
    with pytest.raises(ResolveAmbiguous) as caught:
        rig.md.resolve_stock("AAPL")
    assert caught.value.matches == ((1, "NYSE"), (2, "ARCA"))
    assert caught.value.symbol == "AAPL"


def test_resolve_stock_ib_error_by_request_id(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: [
        error_played(req_id, 321, "Error validating request")])
    with pytest.raises(ResolveError, match="321") as caught:
        rig.md.resolve_stock("AAPL")
    assert caught.value.code == 321
    assert not isinstance(caught.value, ResolveNotFound)


def test_resolve_stock_error_200_is_not_found(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: [
        error_played(req_id, 200, "No security definition has been found for the request")])
    with pytest.raises(ResolveNotFound, match="200"):
        rig.md.resolve_stock("ZZZZ")


def test_resolve_stock_ignores_errors_of_other_requests_and_notices(rig: Rig) -> None:
    def handler(req_id: int, contract: Contract) -> list[tuple[Any, ...]]:
        return [
            error_played(req_id + 1000, 321, "somebody else's error"),
            error_played(-1, 2104, "Market data farm connection is OK:usfarm"),
            error_played(req_id, 2174, "a notice for this request"),
            ("contractDetails", req_id, details(265598)),
            ("contractDetailsEnd", req_id),
        ]

    rig.fake.respond("reqContractDetails", handler)
    assert rig.md.resolve_stock("AAPL").con_id == 265598


def test_resolve_stock_timeout(rig: Rig) -> None:
    before = rig.clock.now
    with pytest.raises(ResolveTimeout):
        rig.md.resolve_stock("AAPL", timeout=20)
    assert 20 <= rig.clock.now - before < 30


def test_resolve_stock_incomplete_answer_times_out(rig: Rig) -> None:
    script_details(rig, details(265598), end=False)
    with pytest.raises(ResolveTimeout):
        rig.md.resolve_stock("AAPL", timeout=5)


def test_resolve_stock_connection_closing_mid_request(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: rig.fake.drop("connectionClosed"))
    with pytest.raises(ResolveConnectionClosed):
        rig.md.resolve_stock("AAPL", timeout=60)


def test_resolve_stock_connection_lost_mid_request(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: rig.fake.drop(1100))
    with pytest.raises(ResolveConnectionLost):
        rig.md.resolve_stock("AAPL", timeout=60)


def test_resolve_stock_failure_kinds_are_distinct() -> None:
    kinds = (ResolveNotFound, ResolveAmbiguous, ResolveError, ResolveTimeout,
             ResolveConnectionClosed, ResolveConnectionLost)
    for kind in kinds:
        assert issubclass(kind, ResolveFailed)
        assert [k for k in kinds if issubclass(kind, k)] == [kind]


def test_resolve_stock_refuses_a_different_instrument(rig: Rig) -> None:
    script_details(rig, details(5, symbol="MSFT"))
    with pytest.raises(ResolveError, match="not this stock"):
        rig.md.resolve_stock("AAPL")
    script_details(rig, details(5, sec_type="OPT"))
    with pytest.raises(ResolveError, match="not this stock"):
        rig.md.resolve_stock("AAPL")
    script_details(rig, details(5, primary="SMART"))
    with pytest.raises(ResolveError, match="unusable"):
        rig.md.resolve_stock("AAPL")


def test_resolve_stock_invalid_symbol_sends_nothing(rig: Rig) -> None:
    with pytest.raises(ValueError):
        rig.md.resolve_stock("a b")
    assert not rig.fake.calls_named("reqContractDetails")


def test_resolve_stock_late_answer_after_timeout_is_dropped(rig: Rig) -> None:
    with pytest.raises(ResolveTimeout):
        rig.md.resolve_stock("AAPL", timeout=2)
    rig.fake.push("contractDetailsEnd", 1)
    rig.md.request_server_time()
    rig.fake.push("currentTime", 1_733_149_800)
    assert rig.md.wait_server_time(5) is not None
    assert rig.md.late_events == 1


def test_poll_interval_is_validated(rig: Rig) -> None:
    for bad in (0, -1, 11, True, "1", None):
        with pytest.raises(ValueError):
            IbMarketData(rig.conn, poll_interval=bad)  # type: ignore[arg-type]
