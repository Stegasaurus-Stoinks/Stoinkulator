"""Extra tests for `IbMarketData`: deadlines under traffic, long streams, loss and restore.

Written by the tester of task T6 (issue 119). Same rules as `test_ib_market_data.py`:
no socket, the clock is injected, no real sleeps beyond a few milliseconds, and every
thread a test starts ends before the test returns. The fixtures are the ones of that file.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from ibapi.common import TickAttrib
from ibapi.contract import Contract

from MainStoinker.ib import events
from MainStoinker.ib import market_data as market_data_module
from MainStoinker.ib.connection import ConnectionLost, ConnectionRestored
from MainStoinker.ib.market_data import (
    CLOSED,
    DATA_LOST,
    END,
    ERROR,
    LOST,
    OUTCOMES,
    TIMEOUT,
    ConcurrentWait,
    DataLost,
    IbMarketData,
    Outcome,
    ResolveDataLost,
    ResolveTimeout,
)
from tests import test_ib_market_data as base
from tests.fake_ib import make_bar
from tests.test_ib_market_data import (
    AAPL,
    END_UTC,
    WAIT,
    Rig,
    details,
    error_played,
    flush,
)

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


def test_successive_waits_return_all_bars_so_far_after_a_loss(rig: Rig) -> None:
    """Contract (T6 fix round, replaces the old incremental one): every result holds ALL bars.

    A second `wait_history` never returns a partial tail, so a driver that retries after
    LOST just uses the last result.
    """
    rig.clock.tick = 0.5
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.drop(1100)
    flush(rig, 20)  # queued before the wait starts, so few polls are enough
    first = rig.md.wait_history(req_id, 20)
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
    assert [b.date for b in rest.bars] == ["1723815000", "1723815060"]  # the first is kept
    again = rig.md.wait_history(req_id, WAIT)  # END is sticky and the result the same
    assert again.outcome == END and again.bars == rest.bars


def test_a_data_lost_restore_is_visible_in_general_events(rig: Rig) -> None:
    rig.clock.tick = 0.5
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    flush(rig, 21)
    assert rig.md.wait_history(req_id, 20).outcome == LOST
    rig.fake.drop(1101)
    outcome = LOST
    for _ in range(5000):
        outcome = rig.md.wait_history(req_id, 0.5).outcome
        if outcome != LOST:
            break
        threading.Event().wait(0.001)
    assert outcome == DATA_LOST  # was TIMEOUT before the fix round: IB forgot the request
    restores = [e for e in rig.md.general_events if isinstance(e, ConnectionRestored)]
    assert [(r.code, r.data_lost) for r in restores] == [(1101, True)]


def test_a_large_history_answer_is_collected_whole(rig: Rig) -> None:
    count = market_data_module._UPDATE_LIMIT + 1_000  # more than any bounded box would keep
    bar = make_bar(1723815000, 10, 11, 9, 10.5, 1)

    def handler(req_id: int, *rest: Any) -> list[tuple[Any, ...]]:
        played: list[tuple[Any, ...]] = [("historicalData", req_id, bar)] * count
        played.append(("historicalDataEnd", req_id, "s", "e"))
        return played

    rig.fake.respond("reqHistoricalData", handler)
    rig.clock.tick = 0.0  # the clock must not decide this test
    result = rig.md.wait_history(rig.md.request_history(AAPL, END_UTC, "1 D"), LONG)
    assert result.outcome == END and len(result.bars) == count


def test_a_long_quote_stream_is_bounded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 500
    monkeypatch.setattr(market_data_module, "_QUOTE_LIMIT", limit)  # a small box, a short test
    rig.clock.tick = 0.0
    quotes = rig.md.request_quotes(AAPL)
    other = rig.md.request_history(AAPL, END_UTC, "1 D")
    ticks = 4 * limit
    attrib = TickAttrib()
    for i in range(ticks):
        rig.fake.push("tickPrice", quotes, 4, 100.0 + i, attrib)
    for i in range(1200):  # events of ids nobody here issued
        rig.fake.push("historicalDataEnd", 50_000 + i, "s", "e")
    rig.fake.push("historicalDataEnd", other, "s", "e")
    assert rig.md.wait_history(other, LONG).outcome == END  # pumps all of it into the boxes
    assert rig.md.quote_overflow == ticks - limit
    assert len(rig.md.general_events) == 1000
    # the oldest ticks went, the newest are kept, in order
    first = rig.md.next_event_for(quotes, WAIT)
    assert isinstance(first, events.TickPrice) and first.price == 100.0 + (ticks - limit)
    assert rig.md.cancel_quotes(quotes) is True
    with pytest.raises(ValueError, match="not an open"):
        rig.md.next_event_for(quotes, 1)


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
    for i in range(6_000):
        rig.fake.push("tickPrice", quotes, 4, 100.0 + i, TickAttrib())
    result = md.wait_history(silent, 1.0)
    assert result.outcome == TIMEOUT
    assert reads[0] < 5000, f"the 1 s wait read {reads[0]} events"


def test_volume_decimal_sizes_survive_a_quote_stream_unchanged(rig: Rig) -> None:
    quotes = rig.md.request_quotes(AAPL)
    rig.fake.push("tickSize", quotes, 5, Decimal("12.5"))
    event = rig.md.next_event_for(quotes, WAIT)
    assert isinstance(event, events.TickSize) and event.size == Decimal("12.5")


# -- T6 fix round ------------------------------------------------------------------------
# Item numbers refer to the fix-round brief: 1 deadline under traffic, 2 resolve lock before
# send, 5 all bars, 6 loss and restore, 7 outcomes, 8 cancels, 9 bounded state.


def settle(rig: Rig, predicate: Any) -> None:
    """Read the connection (nothing awaited) until `predicate()` holds."""
    for _ in range(5000):
        if predicate():
            return
        rig.md.wait_server_time(0.02)
        threading.Event().wait(0.001)
    raise AssertionError("the expected event never arrived")


def lose(rig: Rig) -> None:
    """Send code 1100 and wait until `rig.md` has read it."""
    rig.fake.drop(1100)
    settle(rig, lambda: any(isinstance(e, ConnectionLost) for e in rig.md.general_events))


def counting_md(rig: Rig) -> tuple[IbMarketData, list[int]]:
    """An `IbMarketData` whose clock is 1 s per 1000 events read from the connection."""
    reads = [0]
    original = rig.conn.next_event

    def counting(timeout: float = 0.0) -> Any:
        event = original(timeout)
        if event is not None:
            reads[0] += 1
        return event

    rig.conn.next_event = counting  # type: ignore[method-assign]
    return IbMarketData(rig.conn, clock=lambda: reads[0] / 1000.0, poll_interval=0.01), reads


@pytest.mark.parametrize("wait", ["resolve", "server_time", "stream"])
def test_every_wait_times_out_on_its_deadline_while_other_events_stream(
    rig: Rig, wait: str
) -> None:
    """Item 1 for the waits other than `wait_history` (that one has its own test above)."""
    md, reads = counting_md(rig)
    busy = md.request_quotes(AAPL)
    silent = md.request_quotes(AAPL)
    md.request_server_time()
    if wait == "resolve":
        # a request first reads what is already queued, so the traffic must arrive after it
        rig.fake.respond("reqContractDetails", lambda req_id, c: [
            ("tickPrice", busy, 4, 100.0 + i, TickAttrib()) for i in range(6_000)])
    else:
        for i in range(6_000):
            rig.fake.push("tickPrice", busy, 4, 100.0 + i, TickAttrib())
    if wait == "resolve":
        with pytest.raises(ResolveTimeout):
            md.resolve_stock("AAPL", 1.0)
    elif wait == "server_time":
        assert md.wait_server_time(1.0) is None
    else:
        assert md.next_event_for(silent, 1.0) is None
    assert reads[0] < 5000, f"the 1 s wait read {reads[0]} events"


def test_a_resolve_during_a_running_wait_sends_nothing_and_uses_no_request_id(rig: Rig) -> None:
    """Item 2: the refusal comes before the send and before an id is taken."""
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
    thread = threading.Thread(target=lambda: rig.md.wait_history(req_id, 1e6), daemon=True)
    thread.start()
    try:
        assert inside.wait(WAIT)
        for _ in range(3):
            with pytest.raises(ConcurrentWait):
                rig.md.resolve_stock("AAPL", timeout=1)
        assert not rig.fake.calls_named("reqContractDetails")
    finally:
        rig.fake.push("historicalDataEnd", req_id, "s", "e")
        thread.join(WAIT)
    assert not thread.is_alive()
    assert rig.conn.next_request_id() == req_id + 1  # the refused resolves took no ids


# item 5: all bars, late bars


def test_a_second_wait_after_a_timeout_returns_all_the_bars(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    first = rig.md.wait_history(req_id, 2)
    assert first.outcome == TIMEOUT and len(first.bars) == 1
    rig.fake.push("historicalData", req_id, make_bar(1723815060, 10, 11, 9, 10.5, 2))
    rig.fake.push("historicalDataEnd", req_id, "s", "e")
    second = rig.md.wait_history(req_id, WAIT)
    assert second.outcome == END
    assert [b.date for b in second.bars] == ["1723815000", "1723815060"]
    assert first.bars == second.bars[:1]  # an earlier result is not changed by later waits


def test_bars_after_the_end_marker_of_a_one_shot_request_are_counted_not_added(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    other = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.push("historicalDataEnd", req_id, "s", "e")
    rig.fake.push("historicalData", req_id, make_bar(1723815060, 10, 11, 9, 10.5, 2))
    rig.fake.push("historicalData", req_id, make_bar(1723815120, 10, 11, 9, 10.5, 3))
    rig.fake.push("historicalDataEnd", other, "s", "e")
    first = rig.md.wait_history(req_id, WAIT)
    assert first.outcome == END and len(first.bars) == 1 and first.late_bars == 0
    assert rig.md.wait_history(other, WAIT).outcome == END  # reads the two late bars on the way
    again = rig.md.wait_history(req_id, WAIT)
    assert again.outcome == END and again.late_bars == 2
    assert [b.date for b in again.bars] == ["1723815000"]


# item 6: loss and restore


def test_a_lost_connection_honours_the_timeout_and_polls_instead_of_spinning(rig: Rig) -> None:
    """While 1100 is in force a wait reads with the poll interval until its deadline."""
    now = [0.0]
    calls: list[float] = []
    original = rig.conn.next_event

    def scripted(timeout: float = 0.0) -> Any:
        calls.append(timeout)
        assert len(calls) < 1000, "the wait is spinning without the clock advancing"
        now[0] += timeout  # a real read of `timeout` seconds takes that long
        threading.Event().wait(0.001)
        return original(0.0)

    rig.conn.next_event = scripted  # type: ignore[method-assign]
    md = IbMarketData(rig.conn, clock=lambda: now[0], poll_interval=0.5)
    req_id = md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    while not any(isinstance(e, ConnectionLost) for e in md.general_events):
        md.wait_server_time(0.5)
    calls.clear()
    start = now[0]
    result = md.wait_history(req_id, 4)
    assert result.outcome == LOST
    assert now[0] - start >= 4
    assert 7 <= len(calls) <= 10  # 4 s in slices of 0.5 s
    assert all(c == 0.5 for c in calls[:-1]) and calls[-1] in (0.0, 0.5)


def test_a_wait_that_cannot_advance_any_clock_is_bounded_by_the_deadline_only(rig: Rig) -> None:
    """The loop makes one connection read per pass and checks the clock on each pass."""
    now = [0.0]
    reads = [0]
    original = rig.conn.next_event

    def scripted(timeout: float = 0.0) -> Any:
        reads[0] += 1
        assert reads[0] < 100, "more than one read per clock step"
        now[0] += 1.0
        return original(0.0)

    rig.conn.next_event = scripted  # type: ignore[method-assign]
    md = IbMarketData(rig.conn, clock=lambda: now[0], poll_interval=1.0)
    req_id = md.request_history(AAPL, END_UTC, "1 D")
    reads[0], now[0] = 0, 0.0  # the request read the queue once; count only the wait
    result = md.wait_history(req_id, 10)
    assert result.outcome == TIMEOUT and 10 <= reads[0] <= 12


def test_a_1102_restore_continues_the_same_wait(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    fired: list[int] = []

    def on_read(reads: int) -> None:
        if reads >= 40 and not fired:
            fired.append(reads)
            rig.fake.push("historicalData", req_id, make_bar(1723815000, 10, 11, 9, 10.5, 1))
            rig.fake.drop(1102)
            rig.fake.push("historicalDataEnd", req_id, "s", "e")

    rig.clock.on_read = on_read
    result = rig.md.wait_history(req_id, 1000)  # one call: the restore must not end it
    assert result.outcome == END and len(result.bars) == 1
    assert [(r.code, r.data_lost) for r in rig.md.general_events
            if isinstance(r, ConnectionRestored)] == [(1102, False)]


def test_a_1101_restore_marks_every_request_in_flight_and_only_those(rig: Rig) -> None:
    ended = rig.md.request_history(AAPL, END_UTC, "1 D")
    failed = rig.md.request_history(AAPL, END_UTC, "1 D")
    first = rig.md.request_history(AAPL, END_UTC, "1 D")
    second = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("historicalData", first, make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.fake.push("historicalDataEnd", ended, "s", "e")
    rig.fake.push("error", failed, 1_700_000_000_000, 162, "pacing violation", "")
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    before = rig.clock.now
    one = rig.md.wait_history(first, 1000)
    assert one.outcome == DATA_LOST and len(one.bars) == 1  # the bars so far are still there
    # the other request in flight sees it too, without its own wait having read the event
    assert rig.md.wait_history(second, 1000).outcome == DATA_LOST
    assert rig.clock.now - before < 5  # and without waiting
    # sticky: asked again it says the same at once
    assert rig.md.wait_history(first, 1000).outcome == DATA_LOST
    # requests that were not in flight are not touched
    assert rig.md.wait_history(ended, 5).outcome == END
    assert rig.md.wait_history(failed, 5).outcome == ERROR
    # a request made after the restore is a fresh one
    fresh = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.wait_history(fresh, 2).outcome == TIMEOUT
    # a dead request has nothing to cancel at IB
    assert rig.md.cancel_history(first) is False and rig.md.cancel_history(second) is False
    assert not rig.fake.calls_named("cancelHistoricalData")
    assert rig.md.cancel_history(fresh) is True


def test_a_close_while_lost_is_closed_and_a_deadline_while_lost_is_lost(rig: Rig) -> None:
    rig.clock.tick = 0.5  # few polls per simulated second: no real waiting
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    lose(rig)
    assert rig.md.wait_history(req_id, 3).outcome == LOST
    rig.fake.drop("connectionClosed")
    assert rig.md.wait_history(req_id, 1000).outcome == CLOSED


def test_resolve_stock_reports_data_lost_and_continues_after_1102(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: [
        error_played(-1, 1100, "lost"), error_played(-1, 1101, "data lost")])
    with pytest.raises(ResolveDataLost):
        rig.md.resolve_stock("AAPL", timeout=1000)

    def kept(req_id: int, contract: Any) -> list[tuple[Any, ...]]:
        return [error_played(-1, 1100, "lost"), error_played(-1, 1102, "kept"),
                ("contractDetails", req_id, details(265598)), ("contractDetailsEnd", req_id)]

    rig.fake.respond("reqContractDetails", kept)
    assert rig.md.resolve_stock("AAPL", timeout=1000).con_id == 265598


def test_resolve_stock_times_out_while_lost(rig: Rig) -> None:
    rig.fake.respond("reqContractDetails", lambda req_id, c: [error_played(-1, 1100, "lost")])
    before = rig.clock.now
    with pytest.raises(market_data_module.ResolveConnectionLost):
        rig.md.resolve_stock("AAPL", timeout=2)
    assert rig.clock.now - before >= 2


def test_server_time_reports_data_lost_once(rig: Rig) -> None:
    rig.md.request_server_time()
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    with pytest.raises(DataLost) as caught:
        rig.md.wait_server_time(1000)
    assert caught.value.req_id is None
    assert rig.md.poll_server_time(1) == TIMEOUT  # reported once, then an ordinary wait


def test_a_quote_stream_reports_data_lost_once_after_its_queued_ticks(rig: Rig) -> None:
    quotes = rig.md.request_quotes(AAPL)
    rig.fake.push("tickPrice", quotes, 4, 190.5, TickAttrib())
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    first = rig.md.next_event_for(quotes, 1000)
    assert isinstance(first, events.TickPrice)
    with pytest.raises(DataLost) as caught:
        rig.md.next_event_for(quotes, 1000)
    assert caught.value.req_id == quotes
    assert rig.md.poll_event(quotes, 1) == TIMEOUT  # once
    # IB may have kept the subscription: the cancel is sent (harmless if it did not)
    assert rig.md.cancel_quotes(quotes) is True
    assert [c.args for c in rig.fake.calls_named("cancelMktData")] == [(quotes,)]
    assert rig.md.cancel_quotes(quotes) is False  # and only once


def test_poll_event_says_why_nothing_came(rig: Rig) -> None:
    rig.clock.tick = 0.5
    quotes = rig.md.request_quotes(AAPL)
    hist = rig.md.request_history(AAPL, END_UTC, "1 D")
    with pytest.raises(ValueError, match="stream"):
        rig.md.poll_event(hist, 1)  # a one-shot history is read with wait_history
    assert rig.md.poll_event(quotes, 1) == TIMEOUT
    lose(rig)
    assert rig.md.poll_event(quotes, 2) == LOST
    rig.fake.drop("connectionClosed")
    assert rig.md.poll_event(quotes, 1000) == CLOSED


# item 7: outcomes


def test_outcomes_are_an_enum_listed_in_one_table_of_the_docstring() -> None:
    assert [o.value for o in OUTCOMES] == [
        "end", "error", "timeout", "closed", "lost", "data_lost"]
    assert isinstance(END, Outcome) and END == "end" and DATA_LOST is Outcome.DATA_LOST
    doc = market_data_module.__doc__ or ""
    for outcome in OUTCOMES:
        assert re.search(rf"^{outcome.value}\s", doc, re.MULTILINE), outcome


# item 8: cancels


def test_cancel_after_an_error_sends_nothing_frees_the_state_and_is_idempotent(rig: Rig) -> None:
    rig.fake.respond("reqHistoricalData", lambda req_id, *a: [
        error_played(req_id, 162, "Historical Market Data Service error message:pacing violation")])
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.wait_history(req_id, WAIT).outcome == ERROR
    assert rig.md.wait_history(req_id, WAIT).outcome == ERROR  # sticky: the request is dead
    assert rig.md.cancel_history(req_id) is False
    assert rig.md.cancel_history(req_id) is False
    assert not rig.fake.calls_named("cancelHistoricalData")
    with pytest.raises(ValueError, match="not an open"):
        rig.md.wait_history(req_id, 1)  # the per-id state is gone


def test_an_error_read_by_another_wait_also_makes_the_cancel_silent(rig: Rig) -> None:
    failed = rig.md.request_history(AAPL, END_UTC, "1 D")
    other = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("error", failed, 1_700_000_000_000, 366, "No historical data query found", "")
    rig.fake.push("historicalDataEnd", other, "s", "e")
    assert rig.md.wait_history(other, WAIT).outcome == END
    assert rig.md.cancel_history(failed) is False
    assert not rig.fake.calls_named("cancelHistoricalData")


def test_a_timed_out_request_is_still_cancelled_at_ib(rig: Rig) -> None:
    req_id = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.wait_history(req_id, 1).outcome == TIMEOUT
    assert rig.md.cancel_history(req_id) is True
    assert [c.args for c in rig.fake.calls_named("cancelHistoricalData")] == [(req_id,)]


# item 9: bounded state


def test_unread_live_history_updates_are_capped_and_counted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.clock.tick = 0.0
    limit = 500
    monkeypatch.setattr(market_data_module, "_UPDATE_LIMIT", limit)  # a small box, a short test
    live = rig.md.request_history(AAPL, None, "300 S", keep_up_to_date=True)
    other = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("historicalDataEnd", live, "s", "e")
    extra = 50
    for i in range(limit + extra):
        rig.fake.push("historicalDataUpdate", live, make_bar(1723815000, 10, 11, 9, 10.5, i))
    rig.fake.push("historicalDataEnd", other, "s", "e")
    assert rig.md.wait_history(other, 1e9).outcome == END  # reads everything on the way
    assert rig.md.update_overflow == extra and rig.md.quote_overflow == 0
    first = rig.md.next_event_for(live, WAIT)
    assert isinstance(first, events.HistoricalBar) and first.volume == Decimal(extra)  # newest kept
    assert rig.md.cancel_history(live) is True


def test_the_ids_of_cancelled_requests_are_pruned_but_cancel_stays_idempotent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market_data_module, "_DROPPED_LIMIT", 10)
    ids = [rig.md.request_history(AAPL, END_UTC, "1 D") for _ in range(30)]
    for req_id in ids:
        assert rig.md.cancel_history(req_id) is True
    assert len(rig.md._dropped) == 10
    assert rig.md.cancel_history(ids[0]) is False  # long forgotten, still a no-op
    assert rig.md.cancel_history(ids[-1]) is False
    rig.fake.push("historicalData", ids[0], make_bar(1723815000, 10, 11, 9, 10.5, 1))
    rig.md.request_server_time()
    rig.fake.push("currentTime", 1_723_815_000)
    assert rig.md.wait_server_time(WAIT) is not None
    assert rig.md.late_events == 1
    with pytest.raises(ValueError):
        rig.md.cancel_quotes(ids[-1] + 1000)  # an id never issued is still refused


# -- T6 fix round 2 ------------------------------------------------------------------------


@contextmanager
def a_wait_in_progress(rig: Rig) -> Iterator[int]:
    """A `wait_history` that runs on another thread; yields its request id."""
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
    thread = threading.Thread(target=lambda: rig.md.wait_history(req_id, 1e6), daemon=True)
    thread.start()
    try:
        assert inside.wait(WAIT)
        yield req_id
    finally:
        rig.fake.push("historicalDataEnd", req_id, "s", "e")
        thread.join(WAIT)
    assert not thread.is_alive()


# item 1: a restore that is still queued must not mark a later request


def test_a_request_after_a_queued_1101_restore_is_not_marked_data_lost(rig: Rig) -> None:
    a = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    assert rig.md.wait_history(a, 1).outcome == LOST
    rig.fake.drop(1101)  # nobody reads it: events are only routed inside a wait
    flush(rig, 1)
    b = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("historicalData", b, make_bar(1723815000, 10, 11, 9, 10.5, 5))
    rig.fake.push("historicalDataEnd", b, "s", "e")
    flush(rig, 2)
    result = rig.md.wait_history(b, WAIT)
    assert result.outcome == END and len(result.bars) == 1 and result.end is not None
    assert rig.md.wait_history(a, 1).outcome == DATA_LOST  # the request in flight did lose its data


@pytest.mark.parametrize("kind", ["quotes", "server_time"])
def test_a_quote_or_time_request_after_a_queued_1101_restore_is_not_marked(
    rig: Rig, kind: str
) -> None:
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    flush(rig, 3)
    if kind == "quotes":
        quotes = rig.md.request_quotes(AAPL)
        rig.fake.push("tickPrice", quotes, 4, 190.5, TickAttrib())
        assert isinstance(rig.md.next_event_for(quotes, WAIT), events.TickPrice)
    else:
        rig.md.request_server_time()
        rig.fake.push("currentTime", 1_723_815_000)
        assert rig.md.wait_server_time(WAIT) == datetime.fromtimestamp(1_723_815_000, tz=UTC)


def test_a_resolve_after_a_queued_1101_restore_is_not_marked_data_lost(rig: Rig) -> None:
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    flush(rig, 4)
    rig.fake.respond("reqContractDetails", lambda req_id, c: [
        ("contractDetails", req_id, details(265598)), ("contractDetailsEnd", req_id)])
    assert rig.md.resolve_stock("AAPL", timeout=WAIT).con_id == 265598


@pytest.mark.parametrize("kind", ["history", "quotes", "server_time", "resolve"])
def test_a_request_during_a_running_wait_is_refused_before_an_id_or_a_send(
    rig: Rig, kind: str
) -> None:
    with a_wait_in_progress(rig) as running:
        calls = len(rig.fake.calls)
        with pytest.raises(ConcurrentWait):
            if kind == "history":
                rig.md.request_history(AAPL, END_UTC, "2 D")
            elif kind == "quotes":
                rig.md.request_quotes(AAPL)
            elif kind == "server_time":
                rig.md.request_server_time()
            else:
                rig.md.resolve_stock("AAPL", timeout=1)
        assert len(rig.fake.calls) == calls  # nothing sent
    assert rig.conn.next_request_id() == running + 1  # and no id taken


# item 2: error is sticky and visible


def test_an_error_stays_an_error_when_an_end_marker_arrives_after_it(rig: Rig) -> None:
    a = rig.md.request_history(AAPL, END_UTC, "1 D")
    b = rig.md.request_history(AAPL, END_UTC, "2 D")
    rig.fake.push("error", a, 1_700_000_000_000, 162, "pacing violation", "")
    first = rig.md.wait_history(a, 1)
    assert first.outcome == ERROR
    rig.fake.push("historicalDataEnd", a, "s", "e")
    rig.fake.push("historicalDataEnd", b, "s", "e")
    assert rig.md.wait_history(b, WAIT).outcome == END  # routes the end marker of `a` on the way
    again = rig.md.wait_history(a, 1)
    assert again.outcome == ERROR and [e.code for e in again.failures] == [162]
    assert again.end is not None  # the marker is recorded, the outcome is not END
    assert rig.md.cancel_history(a) is False  # dead at IB: nothing to cancel
    assert not rig.fake.calls_named("cancelHistoricalData")


def test_an_error_before_the_end_marker_in_one_read_is_an_error(rig: Rig) -> None:
    a = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.push("error", a, 1_700_000_000_000, 162, "no data", "")
    rig.fake.push("historicalDataEnd", a, "s", "e")
    assert rig.md.wait_history(a, WAIT).outcome == ERROR


def test_an_error_on_a_live_history_stream_after_its_end_is_visible(rig: Rig) -> None:
    live = rig.md.request_history(AAPL, None, "1 D", keep_up_to_date=True)
    rig.fake.push("historicalData", live, make_bar(1723815000, 10, 11, 9, 10.5, 5))
    rig.fake.push("historicalDataEnd", live, "s", "e")
    rig.fake.push("historicalDataUpdate", live, make_bar(1723815060, 10, 11, 9, 10.5, 6))
    text = "Historical Market Data Service error message:API historical data query cancelled: 1"
    rig.fake.push("error", live, 1_700_000_000_000, 162, text, "")
    update = rig.md.poll_event(live, WAIT)
    assert isinstance(update, events.HistoricalBar) and update.volume == 6  # queued ones first
    assert [rig.md.poll_event(live, 1) for _ in range(3)] == [ERROR, ERROR, ERROR]  # sticky
    result = rig.md.wait_history(live, 1)
    assert result.outcome == ERROR and result.failures[0].message == text
    assert len(result.bars) == 1
    assert rig.md.cancel_history(live) is False  # IB already ended it
    assert not rig.fake.calls_named("cancelHistoricalData")
    with pytest.raises(ValueError, match="not an open"):
        rig.md.poll_event(live, 1)  # the state is freed


# item 3: timeouts


BAD_TIMEOUTS = [float("nan"), float("inf"), float("-inf"), -1, -0.5, True, False, "1", None]


@pytest.mark.parametrize("bad", BAD_TIMEOUTS, ids=repr)
def test_every_wait_refuses_a_bad_timeout_before_sending_anything(rig: Rig, bad: Any) -> None:
    history = rig.md.request_history(AAPL, END_UTC, "1 D")
    quotes = rig.md.request_quotes(AAPL)
    rig.md.request_server_time()
    calls = len(rig.fake.calls)
    next_id = rig.conn.next_request_id() + 1
    for call in (
        lambda: rig.md.wait_history(history, bad),
        lambda: rig.md.poll_event(quotes, bad),
        lambda: rig.md.next_event_for(quotes, bad),
        lambda: rig.md.poll_server_time(bad),
        lambda: rig.md.wait_server_time(bad),
        lambda: rig.md.resolve_stock("AAPL", bad),
    ):
        with pytest.raises(ValueError, match="timeout"):
            call()
    assert len(rig.fake.calls) == calls and not rig.fake.calls_named("reqContractDetails")
    assert rig.conn.next_request_id() == next_id  # resolve took no id


@pytest.mark.parametrize("good", [0, 0.0, 2, 0.25])
def test_a_timeout_of_zero_or_more_is_accepted(rig: Rig, good: float) -> None:
    history = rig.md.request_history(AAPL, END_UTC, "1 D")
    assert rig.md.wait_history(history, good).outcome == TIMEOUT


# item 4: server time


@pytest.mark.parametrize("where", ["queued", "filed"])
def test_a_late_server_time_answer_is_not_taken_for_the_next_request(
    rig: Rig, where: str
) -> None:
    rig.md.request_server_time()
    assert rig.md.poll_server_time(0.2) == TIMEOUT  # never answered in time
    rig.fake.push("currentTime", 1_000_000_000)  # the late answer of the first request
    if where == "filed":
        other = rig.md.request_history(AAPL, END_UTC, "1 D")
        rig.fake.push("historicalDataEnd", other, "s", "e")
        assert rig.md.wait_history(other, WAIT).outcome == END  # files the late answer
    else:
        flush(rig, 5)
    rig.md.request_server_time()
    rig.fake.push("currentTime", 1_700_000_000)
    assert rig.md.wait_server_time(WAIT) == datetime.fromtimestamp(1_700_000_000, tz=UTC)


# item 5: cancel after data lost


def test_a_live_history_request_is_cancelled_at_ib_after_data_lost(rig: Rig) -> None:
    live = rig.md.request_history(AAPL, None, "1 D", keep_up_to_date=True)
    oneshot = rig.md.request_history(AAPL, END_UTC, "1 D")
    rig.fake.drop(1100)
    rig.fake.drop(1101)
    assert rig.md.poll_event(live, 1000) == DATA_LOST
    assert rig.md.wait_history(oneshot, 1000).outcome == DATA_LOST
    assert rig.md.cancel_history(oneshot) is False  # a one-shot request: nothing is sent
    assert not rig.fake.calls_named("cancelHistoricalData")
    assert rig.md.cancel_history(live) is True  # a subscription IB may have kept
    assert [c.args for c in rig.fake.calls_named("cancelHistoricalData")] == [(live,)]
    assert rig.md.cancel_history(live) is False  # only once


# item 6: contracts built by hand


@pytest.mark.parametrize("primary", ["SMART", "nasdaq", "N", "NASDAQ!"])
def test_a_hand_built_contract_with_a_bad_primary_exchange_is_refused(
    rig: Rig, primary: str
) -> None:
    contract = Contract()
    contract.symbol, contract.secType, contract.exchange = "AAPL", "STK", "SMART"
    contract.currency, contract.primaryExchange = "USD", primary
    next_id = rig.conn.next_request_id() + 1
    for call in (
        lambda: rig.md.request_history(contract, END_UTC, "1 D"),
        lambda: rig.md.request_quotes(contract),
    ):
        with pytest.raises(ValueError, match="primary exchange"):
            call()
    assert not rig.fake.calls_named("reqHistoricalData")
    assert not rig.fake.calls_named("reqMktData")
    assert rig.conn.next_request_id() == next_id


def test_a_hand_built_contract_with_an_empty_primary_exchange_is_accepted(rig: Rig) -> None:
    contract = Contract()
    contract.symbol, contract.secType, contract.exchange = "AAPL", "STK", "SMART"
    contract.currency = "USD"
    assert rig.md.request_history(contract, END_UTC, "1 D") >= 0
