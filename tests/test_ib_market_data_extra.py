"""Extra tests for `IbMarketData`: deadlines under traffic, long streams, loss and restore.

Written by the tester of task T6 (issue 119). Same rules as `test_ib_market_data.py`:
no socket, the clock is injected, no real sleeps beyond a few milliseconds, and every
thread a test starts ends before the test returns. The fixtures are the ones of that file.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

import pytest
from ibapi.common import TickAttrib

from MainStoinker.ib import events
from MainStoinker.ib.connection import ConnectionRestored
from MainStoinker.ib.market_data import (
    END,
    ERROR,
    LOST,
    TIMEOUT,
    ConcurrentWait,
    IbMarketData,
)
from tests import test_ib_market_data as base
from tests.fake_ib import make_bar
from tests.test_ib_market_data import AAPL, END_UTC, WAIT, Rig, error_played

rig = base.rig  # the fixture of that file, under the same name

LONG = 1e9  # a timeout that never ends a wait on its own


def test_history_arguments_match_the_real_eclient_signature(rig: Rig) -> None:
    """Every positional argument lands in the parameter ibapi 10.37.2 names for it."""
    import inspect

    from ibapi.client import EClient

    rig.md.request_history(AAPL, END_UTC, "2 D", use_rth=False)
    (call,) = rig.fake.calls_named("reqHistoricalData")
    params = list(inspect.signature(EClient.reqHistoricalData).parameters)[1:]
    sent = dict(zip(params, call.args, strict=True))
    assert list(sent) == [
        "reqId", "contract", "endDateTime", "durationStr", "barSizeSetting", "whatToShow",
        "useRTH", "formatDate", "keepUpToDate", "chartOptions",
    ]
    assert (sent["endDateTime"], sent["durationStr"], sent["barSizeSetting"]) == (
        "20240816-20:00:00", "2 D", "1 min")
    assert (sent["whatToShow"], sent["useRTH"], sent["formatDate"]) == ("TRADES", 0, 2)
    assert sent["keepUpToDate"] is False and sent["chartOptions"] == []

    rig.md.request_quotes(AAPL)
    (quote,) = rig.fake.calls_named("reqMktData")
    params = list(inspect.signature(EClient.reqMktData).parameters)[1:]
    sent = dict(zip(params, quote.args, strict=True))
    assert sent["genericTickList"] == "" and sent["snapshot"] is False
    assert sent["regulatorySnapshot"] is False and sent["mktDataOptions"] == []


def test_request_quotes_cannot_be_talked_into_a_snapshot(rig: Rig) -> None:
    for kwargs in (
        {"regulatory_snapshot": True}, {"regulatorySnapshot": True}, {"snapshot": True},
        {"generic_tick_list": "233"},
    ):
        with pytest.raises(TypeError):
            rig.md.request_quotes(AAPL, **kwargs)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        rig.md.request_quotes(AAPL, True)  # type: ignore[call-arg]
    assert not rig.fake.calls_named("reqMktData")


def test_an_error_for_another_request_does_not_end_this_wait(rig: Rig) -> None:
    first = rig.md.request_history(AAPL, END_UTC, "1 D")
    second = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("historicalData", first, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    text = "Historical Market Data Service error message:Historical data request pacing violation"
    rig.fake.push("error", second, 1_700_000_000_000, 162, text, "")
    rig.fake.push("historicalDataEnd", first, "s", "e")
    one = rig.md.wait_history(first, WAIT)
    assert one.outcome == END and one.errors == () and len(one.bars) == 1
    two = rig.md.wait_history(second, WAIT)
    assert two.outcome == ERROR
    assert "pacing violation" in two.failures[0].message


def test_error_366_ends_the_wait_as_an_error(rig: Rig) -> None:
    rig.fake.respond("reqHistoricalData", lambda req_id, *a: [
        error_played(req_id, 366, "No historical data query found for ticker id:" + str(req_id))])
    result = rig.md.wait_history(rig.md.request_history(AAPL, END_UTC, "1 D"), WAIT)
    assert result.outcome == ERROR and result.failures[0].code == 366


def test_successive_waits_are_incremental_after_a_loss(rig: Rig) -> None:
    """Contract: a second `wait_history` returns only what came after the first one.

    A driver that retries after LOST must add the bars of every result itself; the last
    result alone is a truncated day.
    """
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.drop(1100)
    first = rig.md.wait_history(req_id, WAIT)
    assert first.outcome == LOST and [b.date for b in first.bars] == ["1723815000"]
    rig.fake.drop(1102)
    rig.fake.push("historicalData", req_id, make_bar(1723815060, 10, 11, 9, 10.5, 2))
    rig.fake.push("historicalDataEnd", req_id, "s", "e")
    rest = None
    for _ in range(5000):
        rest = rig.md.wait_history(req_id, WAIT)
        if rest.outcome != LOST:
            break
        threading.Event().wait(0.001)
    assert rest is not None and rest.outcome == END
    assert [b.date for b in rest.bars] == ["1723815060"]  # the first bar is not repeated


def test_a_data_lost_restore_is_visible_in_general_events(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    assert rig.md.wait_history(req_id, WAIT).outcome == LOST
    rig.fake.drop(1101)
    outcome = LOST
    for _ in range(5000):
        outcome = rig.md.wait_history(req_id, 0.5).outcome
        if outcome != LOST:
            break
        threading.Event().wait(0.001)
    assert outcome == TIMEOUT  # nothing answers a request IB has forgotten
    restores = [e for e in rig.md.general_events if isinstance(e, ConnectionRestored)]
    assert [(r.code, r.data_lost) for r in restores] == [(1101, True)]


def test_a_large_history_answer_is_collected_whole(rig: Rig) -> None:
    count = 100_000
    bar = make_bar(1723815000, 10, 11, 9, 10.5, 1)

    def handler(req_id: int, *rest: Any) -> list[tuple[Any, ...]]:
        played: list[tuple[Any, ...]] = [("historicalData", req_id, bar)] * count
        played.append(("historicalDataEnd", req_id, "s", "e"))
        return played

    rig.fake.respond("reqHistoricalData", handler)
    rig.clock.tick = 0.0  # the clock must not decide this test
    result = rig.md.wait_history(rig.md.request_history(AAPL, END_UTC, "1 D"), LONG)
    assert result.outcome == END and len(result.bars) == count


def test_a_long_quote_stream_is_bounded(rig: Rig) -> None:
    rig.clock.tick = 0.0
    quotes = rig.md.request_quotes(AAPL)
    other = rig.md.request_history(AAPL, END_UTC, "1 D")
    ticks = 100_000
    attrib = TickAttrib()
    for i in range(ticks):
        rig.fake.push("tickPrice", quotes, 4, 100.0 + i, attrib)
    for i in range(2000):  # events of ids nobody here issued
        rig.fake.push("historicalDataEnd", 50_000 + i, "s", "e")
    rig.fake.push("historicalDataEnd", other, "s", "e")
    assert rig.md.wait_history(other, LONG).outcome == END  # pumps all of it into the boxes
    assert rig.md.quote_overflow == ticks - 10_000
    assert len(rig.md.general_events) == 1000
    # the oldest ticks went, the newest are kept, in order
    first = rig.md.next_event_for(quotes, WAIT)
    assert isinstance(first, events.TickPrice) and first.price == 100.0 + (ticks - 10_000)
    assert rig.md.cancel_quotes(quotes) is True
    with pytest.raises(ValueError, match="not an open"):
        rig.md.next_event_for(quotes, 1)


@pytest.mark.xfail(
    strict=True,
    reason="T6 defect: resolve_stock sends reqContractDetails before it checks for a running wait",
)
def test_concurrent_resolve_is_refused_before_anything_is_sent(rig: Rig) -> None:
    rig.clock.tick = 0.0
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    inside = threading.Event()
    main_id = threading.get_ident()
    reads = [0]

    def on_read(_: int) -> None:
        if threading.get_ident() != main_id:
            reads[0] += 1
            if reads[0] >= 3:
                inside.set()

    rig.clock.on_read = on_read
    results: list[Any] = []
    thread = threading.Thread(target=lambda: results.append(rig.md.wait_history(req_id, 1e6)),
                              daemon=True)
    thread.start()
    try:
        assert inside.wait(WAIT)
        with pytest.raises(ConcurrentWait):
            rig.md.resolve_stock("AAPL", timeout=1)
        sent = len(rig.fake.calls_named("reqContractDetails"))
    finally:
        rig.fake.push("historicalDataEnd", req_id, "s", "e")
        thread.join(WAIT)
    assert not thread.is_alive()
    assert sent == 0, "resolve_stock sent reqContractDetails and only then raised ConcurrentWait"


@pytest.mark.xfail(
    strict=True,
    reason="T6 defect: _pump_until checks the deadline only when the connection is idle",
)
def test_a_wait_times_out_on_its_deadline_even_while_other_events_stream(rig: Rig) -> None:
    """The timeout is a deadline of the injected clock, not an idle timeout.

    One simulated second passes per 1000 events read from the connection. A history wait
    of 1.0 s for a request nobody answers must end near 1000 events, not after the whole
    quote stream (which, live, never ends).
    """
    reads = [0]
    original = rig.conn.next_event

    def counting(timeout: float = 0.0) -> Any:
        event = original(timeout)
        if event is not None:
            reads[0] += 1
        return event

    rig.conn.next_event = counting  # type: ignore[method-assign]
    md = IbMarketData(rig.conn, clock=lambda: reads[0] / 1000.0, poll_interval=0.01)
    quotes = md.request_quotes(AAPL)
    silent = md.request_history(AAPL, END_UTC, "1 D")
    for i in range(20_000):
        rig.fake.push("tickPrice", quotes, 4, 100.0 + i, TickAttrib())
    result = md.wait_history(silent, 1.0)
    assert result.outcome == TIMEOUT
    assert reads[0] < 5000, f"the 1 s wait read {reads[0]} events"


def test_volume_decimal_sizes_survive_a_quote_stream_unchanged(rig: Rig) -> None:
    quotes = rig.md.request_quotes(AAPL)
    rig.fake.push("tickSize", quotes, 5, Decimal("12.5"))
    event = rig.md.next_event_for(quotes, WAIT)
    assert isinstance(event, events.TickSize) and event.size == Decimal("12.5")
