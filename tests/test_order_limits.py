"""Order limits and kill switch (issue 228): one test per limit, fail closed everywhere."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from MainStoinker.engine.order_limits import (
    FIRST_SESSION_LIMITS,
    KILL_FILE_NAME,
    Decision,
    ExitKind,
    IntentKind,
    KillLevel,
    KillSwitch,
    LimitsConfigError,
    OrderGuard,
    OrderIntent,
    OrderLimits,
    OrderState,
    PendingEntry,
    SentLog,
    SentView,
    WorkingExit,
    decode_kill_text,
    default_kill_path,
    entries_sent_today,
    file_kill_check,
)

NOON = datetime(2026, 11, 24, 12, 0, 0)
DAY = date(2026, 11, 24)


def limits(**over: object) -> OrderLimits:
    values = {**FIRST_SESSION_LIMITS, "allowed_symbols": ["AAPL", "MSFT"], **over}
    return OrderLimits.from_mapping(values)


def never_engaged() -> KillLevel | None:
    return None


def guard(kill=never_engaged, **over: object) -> OrderGuard:
    return OrderGuard(limits(**over), KillSwitch(kill))


def st(*, entries: int = 0, times: tuple = (), orders: int = 0, pnl: float = 0.0, **kw) -> OrderState:
    sent = SentView(message_times=times, entries_today=entries, session_orders=orders)
    return OrderState(sent=sent, session_pnl=pnl, **kw)


def entry(symbol: str = "AAPL", side: str = "buy", qty: float = 10, price: float = 100.0):
    return OrderIntent(IntentKind.ENTRY, symbol, side, qty, price)


def exit_(qty: float, side: str = "sell", symbol: str = "AAPL") -> OrderIntent:
    return OrderIntent(IntentKind.EXIT, symbol, side, qty)


def cancel(ref: str = "o1") -> OrderIntent:
    return OrderIntent(IntentKind.CANCEL, "AAPL", order_ref=ref)


def stop(order_id: str = "s1", qty: float = 10, oca: str = "", symbol: str = "AAPL",
         side: str = "sell") -> WorkingExit:
    return WorkingExit(order_id, symbol, qty, oca, ExitKind.STOP, side)


def prot(qty: float = 10, side: str = "sell", **kw: object) -> OrderIntent:
    return OrderIntent(IntentKind.PROTECTIVE, "AAPL", side, qty, **kw)  # type: ignore[arg-type]


LONG10 = {"AAPL": 10}


def msgs(n: int, second: int = 30) -> tuple[datetime, ...]:
    return tuple(datetime(2026, 11, 24, 11, 59, second) for _ in range(n))


def test_refuses_nothing_when_all_is_in_order() -> None:
    assert guard().check(entry(), OrderState(), NOON) == Decision(True, "ok")


def test_refuses_max_quantity() -> None:
    g = guard()
    assert g.check(entry(qty=10), OrderState(), NOON).allowed
    d = g.check(entry(qty=11, price=1.0), OrderState(), NOON)
    assert not d.allowed and "quantity" in d.reason


def test_refuses_max_notional() -> None:
    g = guard()
    assert g.check(entry(qty=10, price=600.0), OrderState(), NOON).allowed
    d = g.check(entry(qty=10, price=600.01), OrderState(), NOON)
    assert not d.allowed and "notional" in d.reason


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_refuses_entry_without_usable_price(price: float) -> None:
    assert not guard().check(entry(price=price), OrderState(), NOON).allowed


def test_refuses_max_open_positions() -> None:
    g = guard()
    held = OrderState(open_positions={"MSFT": 10})
    d = g.check(entry("AAPL"), held, NOON)
    assert not d.allowed and "open position" in d.reason
    pending = OrderState(pending_entries={"r1": PendingEntry("MSFT", 10)})
    assert not g.check(entry("AAPL"), pending, NOON).allowed
    assert guard(max_open_positions=2).check(entry("AAPL"), held, NOON).allowed


def test_refuses_max_entries_per_day() -> None:
    g = guard()
    assert g.check(entry(), st(entries=19), NOON).allowed
    d = g.check(entry(), st(entries=20), NOON)
    assert not d.allowed and "daily" in d.reason


def test_refuses_max_messages_per_minute() -> None:
    assert guard().check(entry(), st(times=msgs(5)), NOON).allowed
    g = guard()
    d = g.check(entry(), st(times=msgs(6)), NOON)
    assert not d.allowed and "minute" in d.reason and d.rate_exceeded
    # messages a minute old drop out; future ones count
    assert guard().check(entry(), st(times=msgs(9, second=0)), NOON).allowed
    future = tuple(datetime(2026, 11, 24, 12, 0, 30) for _ in range(6))
    assert not guard().check(entry(), st(times=future), NOON).allowed


def test_refuses_symbol_not_allowed() -> None:
    d = guard().check(entry("TSLA"), OrderState(), NOON)
    assert not d.allowed and "TSLA" in d.reason


def test_refuses_entry_side_not_allowed() -> None:
    short_only = guard(allowed_entry_sides=["short"])
    assert not short_only.check(entry(), OrderState(), NOON).allowed
    assert short_only.check(entry(side="sell"), OrderState(), NOON).allowed


def test_shorts_refused_by_default_limits() -> None:
    g = OrderGuard(
        OrderLimits.from_mapping({**FIRST_SESSION_LIMITS, "allowed_symbols": ["AAPL"]}),
        KillSwitch(never_engaged),
    )
    d = g.check(entry(side="sell"), OrderState(), NOON)
    assert not d.allowed and "short" in d.reason
    assert g.check(entry(side="buy"), OrderState(), NOON).allowed


def test_refuses_outside_entry_window() -> None:
    g = guard()
    day = (2026, 11, 24)
    assert not g.check(entry(), OrderState(), datetime(*day, 9, 34, 59)).allowed
    assert g.check(entry(), OrderState(), datetime(*day, 9, 35)).allowed
    assert g.check(entry(), OrderState(), datetime(*day, 15, 44, 59)).allowed
    assert not g.check(entry(), OrderState(), datetime(*day, 15, 45)).allowed
    assert not g.check(entry(), OrderState(), datetime(*day, 18, 0)).allowed


def test_entry_window_uses_new_york_time_for_aware_times() -> None:
    # 17:00 UTC on 2026-11-24 is 12:00 in New York (EST); 14:00 UTC is 09:00
    assert guard().check(
        entry(), OrderState(), datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
    ).allowed
    assert not guard().check(
        entry(), OrderState(), datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)
    ).allowed


def test_refuses_entry_that_would_flip_or_double_up() -> None:
    g = guard(max_open_positions=3, allowed_entry_sides=["long", "short"])
    flip = g.check(entry(side="sell"), OrderState(open_positions={"AAPL": 10}), NOON)
    assert not flip.allowed and "flip" in flip.reason
    again = g.check(entry(side="buy"), OrderState(open_positions={"AAPL": 5}), NOON)
    assert not again.allowed and "already open" in again.reason


def test_refuses_second_entry_on_a_pending_symbol() -> None:
    g = guard(max_open_positions=3)
    pending = OrderState(pending_entries={"r1": PendingEntry("AAPL", 10)})
    d = g.check(entry("AAPL"), pending, NOON)
    assert not d.allowed and "pending" in d.reason
    assert g.check(entry("MSFT"), pending, NOON).allowed


def test_symbols_are_normalised_everywhere() -> None:
    g = guard(max_open_positions=3)
    assert g.check(entry(" aapl "), OrderState(), NOON).allowed
    # an unnormalised state key or allowed symbol is a configuration error, never accepted
    assert not g.check(entry("AAPL"), OrderState(open_positions={"aapl": 10}), NOON).allowed
    bad = OrderState(pending_entries={"r": PendingEntry("aapl", 10)})
    assert "configuration" in g.check(entry("AAPL"), bad, NOON).reason
    for name in (" AAPL ", "aapl"):
        with pytest.raises(LimitsConfigError):
            limits(allowed_symbols=[name])


def test_risk_reducing_always_allowed_even_when_killed() -> None:
    engaged = guard(kill=lambda: KillLevel.FLATTEN)
    state = st(
        open_positions=LONG10,
        entries=99,
        orders=999,
        pnl=-10_000.0,
        times=msgs(5),
    )
    late = datetime(2026, 11, 24, 23, 0)
    assert not engaged.check(entry("MSFT"), state, late).allowed
    assert engaged.check(exit_(10), state, late).allowed
    assert engaged.check(prot(10), state, late).allowed
    assert engaged.check(cancel(), state, late).allowed
    short = OrderState(open_positions={"AAPL": -4})
    assert engaged.check(exit_(4, side="buy"), short, late).allowed


def test_no_flip_no_oversized_exit() -> None:
    g = guard()
    long_ = OrderState(open_positions=LONG10)
    assert g.check(exit_(10), long_, NOON).allowed
    over = g.check(exit_(11), long_, NOON)
    assert not over.allowed and "exceeds" in over.reason
    busy = OrderState(
        open_positions=LONG10, working_exits=(WorkingExit("e1", "AAPL", 6),)
    )
    assert g.check(exit_(4), busy, NOON).allowed
    assert not g.check(exit_(5), busy, NOON).allowed
    assert not g.check(exit_(1), OrderState(), NOON).allowed  # flat: would open a short
    assert not g.check(exit_(1, side="buy"), long_, NOON).allowed  # would add to the long
    assert not g.check(prot(11), long_, NOON).allowed
    for qty in (0, -1, float("nan")):
        assert not g.check(exit_(qty), long_, NOON).allowed
    assert not g.check(exit_(1, side=""), long_, NOON).allowed


def test_nan_position_refuses_exit() -> None:
    nan = OrderState(open_positions={"AAPL": float("nan")})
    for intent in (exit_(1), exit_(1, side="buy"), prot(1)):
        assert not guard().check(intent, nan, NOON).allowed
    d = guard().check(exit_(1, side="buy"), nan, NOON)
    assert "unreadable" in d.reason


def test_protective_counts_against_exits_in_flight() -> None:
    g = guard()
    held = {"open_positions": LONG10}
    market_exit = OrderState(working_exits=(WorkingExit("x", "AAPL", 10),), **held)
    assert not g.check(prot(10), market_exit, NOON).allowed
    # two plain stops of 10 may not both be placed
    one_stop = OrderState(working_exits=(WorkingExit("s1", "AAPL", 10),), **held)
    assert not g.check(prot(10), one_stop, NOON).allowed
    # a stop and a target in one OCA group count once (the largest leg)
    bracket = OrderState(
        working_exits=(
            WorkingExit("s1", "AAPL", 10, "oca1"),
            WorkingExit("t1", "AAPL", 8, "oca1"),
        ),
        **held,
    )
    assert not g.check(exit_(1), bracket, NOON).allowed  # the group already covers 10
    assert g.check(prot(10, oca_group="oca1"), bracket, NOON).allowed  # joins its own group
    # a replacement leaves out the order it replaces
    replace = prot(10, replaces_order="s1", old_price=95.0, new_price=96.0)
    one_stop = OrderState(working_exits=(stop("s1", 10),), **held)
    assert g.check(replace, one_stop, NOON).allowed
    # ... but not another working order
    mixed = OrderState(
        working_exits=(stop("s1", 10), WorkingExit("x", "AAPL", 4)), **held
    )
    assert not g.check(replace, mixed, NOON).allowed


def test_protective_bracket_child_sized_against_named_pending_parent() -> None:
    g = guard()
    pending = OrderState(pending_entries={"p1": PendingEntry("AAPL", 10)})
    assert g.check(prot(10, parent_ref="p1"), pending, NOON).allowed
    assert not g.check(prot(11, parent_ref="p1"), pending, NOON).allowed
    # standalone: never sized against a pending entry
    assert not g.check(prot(10), pending, NOON).allowed
    # unknown parent, or a parent of another symbol
    assert not g.check(prot(10, parent_ref="nope"), pending, NOON).allowed
    other = OrderState(pending_entries={"p1": PendingEntry("MSFT", 10)})
    assert not g.check(prot(10, parent_ref="p1"), other, NOON).allowed


def test_protective_against_pending_short_is_side_aware() -> None:
    g = guard()
    short = OrderState(pending_entries={"p1": PendingEntry("AAPL", -10)})
    assert g.check(prot(10, side="buy", parent_ref="p1"), short, NOON).allowed
    assert not g.check(prot(10, side="sell", parent_ref="p1"), short, NOON).allowed
    long_ = OrderState(pending_entries={"p1": PendingEntry("AAPL", 10)})
    assert not g.check(prot(10, side="buy", parent_ref="p1"), long_, NOON).allowed


def test_every_message_counts_toward_the_rate_and_latches_stop_entries() -> None:
    g = guard()
    state = st(open_positions=LONG10, times=msgs(6))
    # pure exit, cancel and a new protective order are allowed, flagged, and latch stop_entries
    d = g.check(exit_(10), state, NOON)
    assert d.allowed and d.rate_exceeded
    assert g.kill_level(OrderState()) is KillLevel.STOP_ENTRIES  # latched, state is quiet now
    assert not g.check(entry("MSFT"), OrderState(), NOON).allowed
    assert g.check(cancel(), state, NOON).rate_exceeded
    assert g.check(prot(10), state, NOON).rate_exceeded
    assert not guard().check(exit_(10), st(open_positions=LONG10, times=msgs(5)), NOON).rate_exceeded


def test_replacement_that_does_not_reduce_risk_is_refused_over_the_rate() -> None:
    state = st(open_positions=LONG10, times=msgs(6), working_exits=(stop(),))
    tighter = prot(10, replaces_order="s1", old_price=95.0, new_price=96.0)
    assert guard().check(tighter, state, NOON).allowed
    wider = prot(10, replaces_order="s1", old_price=95.0, new_price=94.0)
    assert not guard().check(wider, state, NOON).allowed
    smaller = prot(5, replaces_order="s1", old_price=95.0, new_price=96.0)
    assert not guard().check(smaller, state, NOON).allowed
    no_prices = prot(10, replaces_order="s1")
    assert not guard().check(
        no_prices, st(open_positions=LONG10, working_exits=(stop(),)), NOON
    ).allowed
    # a buy stop (short position) moves away when it goes up
    short = st(
        open_positions={"AAPL": -10},
        times=msgs(6),
        working_exits=(stop(side="buy"),),
    )
    up = prot(10, side="buy", replaces_order="s1", old_price=105.0, new_price=106.0)
    down = prot(10, side="buy", replaces_order="s1", old_price=105.0, new_price=104.0)
    assert not guard().check(up, short, NOON).allowed
    assert guard().check(down, short, NOON).allowed
    # under the rate a wider replacement is allowed
    assert guard().check(
        wider, st(open_positions=LONG10, working_exits=(stop(),)), NOON
    ).allowed


def test_session_cap_counts_all_messages() -> None:
    g = guard()
    d = g.check(exit_(10), st(open_positions=LONG10, orders=60), NOON)
    assert d.allowed and d.rate_exceeded
    assert not g.check(entry("MSFT"), OrderState(), NOON).allowed  # latched


def test_daily_count_seeded() -> None:
    today = date(2026, 11, 24)
    sent = [datetime(2026, 11, 24, 10, i) for i in range(20)]
    sent += [datetime(2026, 11, 23, 10, 0)] * 5  # yesterday does not count
    assert entries_sent_today(sent, today) == 20
    log = SentLog(sent, trading_day=DAY)
    g = guard()
    assert log.view(NOON).entries_today == 20
    assert not g.check(entry(), OrderState(sent=log.view(NOON)), NOON).allowed
    assert guard().check(
        entry(), OrderState(sent=SentLog(sent[:19], trading_day=DAY).view(NOON)), NOON
    ).allowed


def test_entries_sent_today_converts_aware_times_to_new_york() -> None:
    # 20:30 New York on 11-23 is 01:30 UTC on 11-24
    evening = datetime(2026, 11, 24, 1, 30, tzinfo=timezone.utc)
    assert entries_sent_today([evening], date(2026, 11, 23)) == 1
    assert entries_sent_today([evening], date(2026, 11, 24)) == 0
    assert SentLog([evening], trading_day=DAY).view(datetime(2026, 11, 23, 12, 0)).entries_today == 1


def test_sent_log_counts_what_is_recorded_only() -> None:
    log = SentLog(trading_day=DAY)
    g = guard()
    for i in range(3):
        now = datetime(2026, 11, 24, 12, 0, i)
        intent = entry() if i < 2 else cancel()
        assert g.check(intent, OrderState(sent=log.view(now)), now).allowed
        assert log.view(now).session_orders == i  # a check consumes nothing
        log.record_sent(intent, now)
    view = log.view(datetime(2026, 11, 24, 12, 0, 5))
    assert (view.session_orders, view.entries_today, len(view.message_times)) == (3, 2, 3)
    assert log.view(datetime(2026, 11, 25, 12, 0)).entries_today == 0
    # six messages in the minute: the next entry is refused through the log alone
    for i in range(3):
        log.record_sent(exit_(1), datetime(2026, 11, 24, 12, 0, 6 + i))
    later = datetime(2026, 11, 24, 12, 0, 20)
    assert not g.check(entry(), OrderState(sent=log.view(later)), later).allowed


def test_kill_file_checked_each_call(tmp_path: Path) -> None:
    kill = tmp_path / KILL_FILE_NAME
    g = OrderGuard(limits(), KillSwitch.from_path(kill))
    assert g.check(entry(), OrderState(), NOON).allowed
    kill.write_text("", encoding="utf-8")
    d = g.check(entry(), OrderState(), NOON)
    assert not d.allowed and "kill" in d.reason
    assert g.kill_level(OrderState()) is KillLevel.STOP_ENTRIES
    kill.write_text("FLATTEN\n", encoding="utf-8")
    assert g.kill_level(OrderState()) is KillLevel.FLATTEN
    kill.unlink()
    assert g.check(entry(), OrderState(), NOON).allowed
    assert g.kill_level(OrderState()) is None


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        (b"flatten", KillLevel.FLATTEN),
        (b"  FlAtTeN \r\n", KillLevel.FLATTEN),
        (b"\xef\xbb\xbfflatten\r\n", KillLevel.FLATTEN),
        ("flatten\r\n".encode("utf-16"), KillLevel.FLATTEN),  # PowerShell 5.1: LE with BOM
        (b"\xfe\xff\x00f\x00l\x00a\x00t\x00t\x00e\x00n", KillLevel.FLATTEN),  # BE with BOM
        (b"f\x00l\x00a\x00t\x00t\x00e\x00n\x00", KillLevel.FLATTEN),  # NULs, no BOM
        (b"", KillLevel.STOP_ENTRIES),
        (b"stop", KillLevel.STOP_ENTRIES),
        (b"flatten now", KillLevel.STOP_ENTRIES),
        (b"\xff\xfe\xfa", KillLevel.STOP_ENTRIES),  # undecodable
        (b"\x80\x81\x82", KillLevel.STOP_ENTRIES),
        (b"flatten" + b" " * 5000, KillLevel.STOP_ENTRIES),  # oversized
    ],
)
def test_kill_text_variants(raw: bytes, level: KillLevel) -> None:
    assert decode_kill_text(raw) is level


def test_kill_file_flatten_written_by_powershell(tmp_path: Path) -> None:
    kill = tmp_path / KILL_FILE_NAME
    kill.write_bytes("flatten\r\n".encode("utf-16"))
    assert file_kill_check(kill)() is KillLevel.FLATTEN


def test_kill_file_unreadable_path_refuses(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    g = OrderGuard(limits(), KillSwitch.from_path(blocker / KILL_FILE_NAME))
    assert not g.check(entry(), OrderState(), NOON).allowed
    folder = tmp_path / "dir"
    folder.mkdir()
    assert file_kill_check(folder)() is KillLevel.STOP_ENTRIES

    def boom() -> KillLevel | None:
        raise RuntimeError("disk gone")

    assert not guard(kill=boom).check(entry(), OrderState(), NOON).allowed
    assert not guard(kill=lambda: "go").check(entry(), OrderState(), NOON).allowed  # type: ignore[arg-type,return-value]


def test_kill_hard_caps() -> None:
    assert guard().check(entry(), st(orders=59, pnl=-199.99), NOON).allowed
    g = guard()
    d = g.check(entry(), st(orders=60), NOON)
    assert not d.allowed and "cap" in d.reason
    assert g.kill_level(OrderState()) is KillLevel.STOP_ENTRIES
    g2 = guard()
    assert not g2.check(entry(), st(pnl=-200.0), NOON).allowed
    assert g2.kill_level(OrderState()) is KillLevel.FLATTEN
    assert not guard().check(entry(), st(pnl=float("nan")), NOON).allowed


def test_loss_and_order_caps_latch() -> None:
    g = guard()
    assert not g.check(entry(), st(pnl=-250.0), NOON).allowed
    # the loss recovers: still engaged for the life of the guard
    assert not g.check(entry(), st(pnl=-150.0), NOON).allowed
    assert g.kill_level(st(pnl=0.0)) is KillLevel.FLATTEN
    g2 = guard()
    assert not g2.check(entry(), st(orders=61), NOON).allowed
    assert not g2.check(entry(), st(orders=0), NOON).allowed
    # the file is not latched: removing it clears it
    flag = {"on": True}
    g3 = guard(kill=lambda: KillLevel.STOP_ENTRIES if flag["on"] else None)
    assert not g3.check(entry(), OrderState(), NOON).allowed
    flag["on"] = False
    assert g3.check(entry(), OrderState(), NOON).allowed


def test_caps_cannot_be_raised_and_objects_are_frozen() -> None:
    for caps in (
        {"max_session_orders": 61},
        {"max_session_loss": 200.01},
        {"max_session_loss": float("nan")},
        {"max_session_loss": float("inf")},
        {"max_session_loss": 0},
        {"max_session_orders": 0},
        {"max_session_orders": True},
    ):
        with pytest.raises(LimitsConfigError):
            KillSwitch(never_engaged, **caps)
    KillSwitch(never_engaged, max_session_orders=10, max_session_loss=50.0)
    kill = KillSwitch(never_engaged)
    g = OrderGuard(limits(), kill)
    with pytest.raises(AttributeError):
        kill.max_session_loss = float("nan")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        kill.max_session_orders = 10_000  # type: ignore[misc]
    with pytest.raises(AttributeError):
        g.limits = limits(max_quantity=1000)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        g.kill_switch = KillSwitch(never_engaged)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        limits().max_quantity = 5  # type: ignore[misc]


@pytest.mark.parametrize("qty", [0.5, 9.99, 0, -1, True, float("nan"), "3"])
def test_quantities_must_be_whole_positive(qty: object) -> None:
    g = guard()
    assert not g.check(entry(qty=qty, price=1.0), OrderState(), NOON).allowed  # type: ignore[arg-type]
    assert not g.check(exit_(qty), OrderState(open_positions=LONG10), NOON).allowed  # type: ignore[arg-type]


def test_integral_float_quantity_is_accepted() -> None:
    assert guard().check(entry(qty=10.0), OrderState(), NOON).allowed


def test_default_kill_path_follows_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_kill_path() == tmp_path / "Stoinkulator" / "STOP-ORDERS"
    assert not (tmp_path / "Stoinkulator").exists()  # building the path writes nothing
    monkeypatch.delenv("USERPROFILE")
    monkeypatch.delenv("HOME")
    with pytest.raises(LimitsConfigError):
        default_kill_path()


def test_missing_limits_is_config_error() -> None:
    full = {**FIRST_SESSION_LIMITS, "allowed_symbols": ["AAPL"]}
    OrderLimits.from_mapping(full)
    for name in full:
        partial = {k: v for k, v in full.items() if k != name}
        with pytest.raises(LimitsConfigError):
            OrderLimits.from_mapping(partial)
    with pytest.raises(LimitsConfigError):
        OrderLimits.from_mapping({})
    with pytest.raises(LimitsConfigError):
        OrderLimits.from_mapping({**full, "surprise": 1})
    with pytest.raises(TypeError):  # fields have no defaults
        OrderLimits()  # type: ignore[call-arg]
    with pytest.raises(LimitsConfigError):
        OrderGuard(None, KillSwitch(never_engaged))  # type: ignore[arg-type]
    with pytest.raises(LimitsConfigError):
        OrderGuard(limits(), None)  # type: ignore[arg-type]
    with pytest.raises(LimitsConfigError):
        KillSwitch(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        {"max_quantity": 0},
        {"max_quantity": True},
        {"max_quantity": "10"},
        {"max_quantity": 2.5},
        {"max_notional": float("nan")},
        {"max_notional": -1},
        {"max_open_positions": 0},
        {"max_entries_per_day": None},
        {"max_messages_per_minute": 0},
        {"allowed_symbols": []},
        {"allowed_symbols": "AAPL"},
        {"allowed_symbols": [""]},
        {"allowed_entry_sides": []},
        {"allowed_entry_sides": ["sideways"]},
        {"entry_start": "9.35"},
        {"entry_end": "09:00"},
        {"entry_start": "16:00", "entry_end": "15:45"},
    ],
)
def test_malformed_limits_is_config_error(bad: dict) -> None:
    with pytest.raises(LimitsConfigError):
        limits(**bad)


def test_malformed_intent_and_state_refuse() -> None:
    g = guard()
    assert not g.check("order", OrderState(), NOON).allowed  # type: ignore[arg-type]
    assert not g.check(OrderIntent(IntentKind.ENTRY, "", "buy", 1, 1.0), OrderState(), NOON).allowed
    assert not g.check(entry(side="long"), OrderState(), NOON).allowed
    assert not g.check(entry(qty=float("nan")), OrderState(), NOON).allowed
    assert not g.check(entry(), st(entries="3"), NOON).allowed  # type: ignore[arg-type]
    assert not g.check(entry(), OrderState(), "noon").allowed  # type: ignore[arg-type]
    assert not g.check(entry(), None, NOON).allowed  # type: ignore[arg-type]
    assert g.check(cancel(), OrderState(), NOON).allowed


# -- second review round ---------------------------------------------------------------------


def test_exit_may_not_carry_oca_replacement_or_parent() -> None:
    g = guard()
    target = WorkingExit("t1", "AAPL", 10, "B1", ExitKind.TARGET, "sell")
    bracket = OrderState(
        open_positions=LONG10,
        working_exits=(stop("s1", 10, "B1"), target),
        pending_entries={"p1": PendingEntry("AAPL", 10)},
    )
    assert not g.check(exit_(10), bracket, NOON).allowed  # the group already covers 10
    for extra in ({"oca_group": "B1"}, {"replaces_order": "s1"}, {"parent_ref": "p1"}):
        intent = OrderIntent(IntentKind.EXIT, "AAPL", "sell", 10, **extra)  # type: ignore[arg-type]
        d = g.check(intent, bracket, NOON)
        assert not d.allowed and "may not carry" in d.reason


def test_replacement_must_name_a_working_stop_or_target_of_the_same_symbol() -> None:
    g = guard(max_open_positions=2)
    base = {"open_positions": {"AAPL": 10, "MSFT": 10}}
    plain = OrderState(working_exits=(WorkingExit("x", "AAPL", 10),), **base)
    sized = {"old_price": 95.0, "new_price": 96.0, "replaces_order": "x"}
    assert not g.check(prot(10, **sized), plain, NOON).allowed  # a plain exit is no stop
    assert not g.check(prot(10, **{**sized, "replaces_order": "ghost"}), plain, NOON).allowed
    other = OrderState(working_exits=(stop("s9", 10, symbol="MSFT"),), **base)
    assert not g.check(prot(10, **{**sized, "replaces_order": "s9"}), other, NOON).allowed
    ok = OrderState(working_exits=(stop("s1", 10),), **base)
    assert g.check(prot(10, **{**sized, "replaces_order": "s1"}), ok, NOON).allowed
    # a replacement keeps the kind and the side
    as_target = prot(10, protective_kind=ExitKind.TARGET, **{**sized, "replaces_order": "s1"})
    assert not g.check(as_target, ok, NOON).allowed
    wrong_side = OrderState(working_exits=(stop("s1", 10, side="buy"),), **base)
    assert not g.check(prot(10, **{**sized, "replaces_order": "s1"}), wrong_side, NOON).allowed


def test_oca_group_is_per_symbol() -> None:
    g = guard(max_open_positions=2)
    base = {"open_positions": {"AAPL": 10, "MSFT": 10}}
    state = OrderState(working_exits=(stop("m1", 10, "G", "MSFT"),), **base)
    d = g.check(prot(10, oca_group="G"), state, NOON)
    assert not d.allowed and "another symbol" in d.reason
    assert g.check(prot(10, oca_group="H"), state, NOON).allowed
    spanning = OrderState(
        working_exits=(stop("m1", 10, "G", "MSFT"), stop("a1", 10, "G", "AAPL")), **base
    )
    d = g.check(prot(10), spanning, NOON)
    assert not d.allowed and "configuration" in d.reason


def test_bracket_child_refused_when_the_symbol_holds_a_position() -> None:
    g = guard()
    filled = OrderState(open_positions=LONG10, pending_entries={"p1": PendingEntry("AAPL", 10)})
    d = g.check(prot(20, parent_ref="p1"), filled, NOON)
    assert not d.allowed and "already holds" in d.reason
    # held 10 against a pending short parent of -5: no child gets through either way
    conflict = OrderState(open_positions=LONG10, pending_entries={"p1": PendingEntry("AAPL", -5)})
    assert not g.check(prot(5, parent_ref="p1"), conflict, NOON).allowed
    assert not g.check(prot(5, side="buy", parent_ref="p1"), conflict, NOON).allowed


def test_target_replacement_over_the_rate_is_refused() -> None:
    target = WorkingExit("t1", "AAPL", 10, "", ExitKind.TARGET, "sell")
    state = st(open_positions=LONG10, times=msgs(6), working_exits=(target,))
    for new in (105.0, 150.0):  # toward and away from the price
        move = prot(
            10, protective_kind=ExitKind.TARGET, replaces_order="t1", old_price=110.0, new_price=new
        )
        assert not guard().check(move, state, NOON).allowed
    calm = st(open_positions=LONG10, working_exits=(target,))
    far = prot(
        10, protective_kind=ExitKind.TARGET, replaces_order="t1", old_price=110.0, new_price=150.0
    )
    assert guard().check(far, calm, NOON).allowed


def test_cancel_needs_only_an_order_ref() -> None:
    g = guard()
    junk = OrderState(
        open_positions={"msft": float("nan")},
        sent=SentView(message_times=(None,)),  # type: ignore[arg-type]
    )
    d = g.check(cancel(), junk, None)  # type: ignore[arg-type]
    assert d.allowed and d.state_errors
    assert g.check(cancel(), None, "noon").allowed  # type: ignore[arg-type]
    assert not g.check(OrderIntent(IntentKind.CANCEL, "AAPL"), OrderState(), NOON).allowed
    assert not g.check(cancel(" "), OrderState(), NOON).allowed
    assert g.kill_level(OrderState()) is KillLevel.STOP_ENTRIES  # the bad state latched


def test_exit_depends_only_on_its_own_symbol_and_reports_the_rest() -> None:
    base = {"open_positions": {"AAPL": 10, "msft": 0}}
    g = guard()
    d = g.check(exit_(10), OrderState(**base), NOON)
    assert d.allowed and d.state_errors
    assert g.kill_level(OrderState()) is KillLevel.STOP_ENTRIES
    bad_exit = OrderState(
        open_positions=LONG10, working_exits=(WorkingExit("x", "MSFT", float("nan")),)
    )
    d = guard().check(exit_(10), bad_exit, NOON)
    assert d.allowed and d.state_errors
    bad_times = st(open_positions=LONG10, times=(None,))  # type: ignore[arg-type]
    d = guard().check(exit_(10), bad_times, NOON)
    assert d.allowed and d.state_errors and d.rate_exceeded
    # problems in its OWN symbol still refuse
    own = OrderState(open_positions={"aapl": 10})
    assert not guard().check(exit_(10), own, NOON).allowed
    own_exit = OrderState(open_positions=LONG10, working_exits=(WorkingExit("x", "aapl", 1),))
    assert not guard().check(exit_(10), own_exit, NOON).allowed
    # entries and protective orders stay fully fail-closed
    assert not guard().check(entry("MSFT"), OrderState(**base), NOON).allowed
    assert not guard().check(prot(10), OrderState(**base), NOON).allowed
    assert not guard().check(prot(10), bad_times, NOON).allowed


def test_first_session_limits_is_read_only() -> None:
    with pytest.raises(TypeError):
        FIRST_SESSION_LIMITS["max_quantity"] = 1000  # type: ignore[index]


def test_sent_log_has_no_public_mutable_attributes() -> None:
    log = SentLog(trading_day=DAY)
    public = [n for n in dir(log) if not n.startswith("_")]
    assert sorted(public) == ["latch", "note_latch", "record_sent", "view"]
    with pytest.raises(AttributeError):
        log.surprise = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        log.latch = None  # type: ignore[misc]


def test_sent_log_seeded_for_a_restart_on_the_same_day() -> None:
    earlier = datetime(2026, 11, 24, 11, 59, 40)
    log = SentLog(
        [], trading_day=DAY, session_orders=60, message_times=[earlier] * 3, latch=KillLevel.FLATTEN
    )
    view = log.view(NOON)
    assert (view.session_orders, len(view.message_times), view.latch) == (
        60,
        3,
        KillLevel.FLATTEN,
    )
    g = guard()
    assert not g.check(entry(), OrderState(sent=view), NOON).allowed
    assert g.kill_level(OrderState()) is KillLevel.FLATTEN  # the carried latch engaged
    log2 = SentLog(trading_day=DAY, session_orders=59)
    log2.record_sent(entry(), NOON)
    assert log2.view(NOON).session_orders == 60
    for bad in (-1, True, 1.5):
        with pytest.raises(LimitsConfigError):
            SentLog(trading_day=DAY, session_orders=bad)  # type: ignore[arg-type]
    with pytest.raises(LimitsConfigError):
        SentLog(trading_day=DAY, latch="flatten")  # type: ignore[arg-type]
    with pytest.raises(LimitsConfigError):
        SentLog(trading_day=DAY, message_times=["noon"])  # type: ignore[list-item]


def test_kill_switch_seeded_latch_never_unengages() -> None:
    kill = KillSwitch(never_engaged, seeded_latch=KillLevel.STOP_ENTRIES)
    assert kill.latched is KillLevel.STOP_ENTRIES
    assert kill.level(OrderState())[0] is KillLevel.STOP_ENTRIES
    kill.engage(KillLevel.FLATTEN, "x")
    kill.engage(KillLevel.STOP_ENTRIES, "y")  # cannot lower
    assert kill.level(OrderState())[0] is KillLevel.FLATTEN
    assert KillSwitch(never_engaged, seeded_latch=None).level(OrderState())[0] is None
    for bad in ("flatten", False, 0):
        with pytest.raises(LimitsConfigError):
            KillSwitch(never_engaged, seeded_latch=bad)  # type: ignore[arg-type]
    # a view without the latch cannot clear one that is engaged
    assert kill.level(OrderState(sent=SentView(latch=None)))[0] is KillLevel.FLATTEN


def test_record_sent_always_counts() -> None:
    log = SentLog(trading_day=DAY)
    log.record_sent(entry(), NOON)
    log.record_sent(entry(), None)  # type: ignore[arg-type]
    log.record_sent("junk", "noon")  # type: ignore[arg-type]
    view = log.view(NOON)
    assert view.session_orders == 3
    assert view.latch is KillLevel.STOP_ENTRIES
    assert view.entries_today == 1 + 1  # the second entry uses the latest known time
    fresh = SentLog(trading_day=DAY)
    fresh.record_sent(entry(), None)  # type: ignore[arg-type]
    assert fresh.view(NOON).session_orders == 1
    assert fresh.view(NOON).latch is KillLevel.STOP_ENTRIES
    log.note_latch(KillLevel.FLATTEN)
    log.note_latch(KillLevel.STOP_ENTRIES)
    assert log.latch is KillLevel.FLATTEN


# -- third review round ----------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["junk", None, 5, WorkingExit("x", None, 1), WorkingExit("x", "msft", 1)])  # type: ignore[arg-type]
def test_exit_refused_when_a_working_exit_has_no_readable_symbol(junk: object) -> None:
    state = OrderState(open_positions=LONG10, working_exits=(junk,))  # type: ignore[arg-type]
    d = guard().check(exit_(10), state, NOON)
    assert not d.allowed


def test_exit_skips_a_malformed_exit_only_when_it_names_another_valid_symbol() -> None:
    state = OrderState(
        open_positions=LONG10, working_exits=(WorkingExit("x", "MSFT", float("nan")),)
    )
    assert guard().check(exit_(10), state, NOON).allowed


@pytest.mark.parametrize(
    "extra",
    [
        {"side": "sell"},
        {"quantity": 5},
        {"quantity": float("nan")},
        {"price": 1.0},
        {"oca_group": "g"},
        {"parent_ref": "p"},
        {"replaces_order": "s1"},
        {"old_price": 1.0},
        {"new_price": 1.0},
    ],
)
def test_cancel_carrying_order_fields_is_refused(extra: dict) -> None:
    d = guard().check(OrderIntent(IntentKind.CANCEL, "AAPL", order_ref="o1", **extra), OrderState(), NOON)
    assert not d.allowed and "carries order fields" in d.reason


def test_cancel_symbol_is_optional_but_valid_when_given() -> None:
    g = guard()
    assert g.check(OrderIntent(IntentKind.CANCEL, "", order_ref="o1"), OrderState(), NOON).allowed
    assert not g.check(
        OrderIntent(IntentKind.CANCEL, "  ", order_ref="o1"), OrderState(), NOON
    ).allowed
    assert not g.check(
        OrderIntent(IntentKind.CANCEL, 5, order_ref="o1"), OrderState(), NOON  # type: ignore[arg-type]
    ).allowed


@pytest.mark.parametrize("bad", ["flatten", 0, False, object(), []])
def test_note_latch_with_unknown_value_is_a_config_error(bad: object) -> None:
    log = SentLog(trading_day=DAY)
    with pytest.raises(LimitsConfigError):
        log.note_latch(bad)  # type: ignore[arg-type]
    assert log.latch is None
    log.note_latch(None)
    assert log.latch is None


def test_entry_with_unusable_time_still_counts_for_the_trading_day() -> None:
    log = SentLog(trading_day=DAY)
    log.record_sent(entry(), None)  # type: ignore[arg-type]
    view = log.view(NOON)
    assert view.entries_today == 1 and view.session_orders == 1
    assert view.latch is KillLevel.STOP_ENTRIES
    with pytest.raises(LimitsConfigError):
        SentLog(trading_day="2026-11-24")  # type: ignore[arg-type]
    with pytest.raises(LimitsConfigError):
        SentLog(trading_day=NOON)  # type: ignore[arg-type]


def test_message_dated_another_day_counts_and_latches() -> None:
    log = SentLog(trading_day=DAY)
    log.record_sent(entry(), datetime(2026, 11, 23, 12, 0))
    view = log.view(NOON)
    assert view.session_orders == 1 and view.entries_today == 1
    assert view.latch is KillLevel.STOP_ENTRIES
    same = SentLog(trading_day=DAY)
    same.record_sent(entry(), NOON)
    assert same.view(NOON).latch is None


def test_guard_records_its_latches_in_the_log() -> None:
    log = SentLog(trading_day=DAY)
    g = guard()
    state = OrderState(sent=log.view(NOON), session_pnl=-250.0)
    assert not g.check(entry(), state, NOON, log).allowed
    assert log.latch is KillLevel.FLATTEN
    # a later check with a healthy state keeps it in the log, and a restart re-seeds from it
    assert not g.check(entry(), OrderState(sent=log.view(NOON)), NOON, log).allowed
    view = log.view(NOON)
    restarted = SentLog(
        trading_day=DAY,
        session_orders=view.session_orders,
        message_times=view.message_times,
        latch=view.latch,
    )
    fresh = guard()
    d = fresh.check(entry(), OrderState(sent=restarted.view(NOON)), NOON, restarted)
    assert not d.allowed
    assert fresh.kill_level(OrderState()) is KillLevel.FLATTEN
    # without the log nothing is recorded; a refusal for any other reason records nothing
    other = SentLog(trading_day=DAY)
    guard().check(entry("TSLA"), OrderState(), NOON, other)
    assert other.latch is None
    # a rate overrun seen with an exit is recorded too
    rate_log = SentLog(trading_day=DAY)
    guard().check(exit_(10), st(open_positions=LONG10, times=msgs(6)), NOON, rate_log)
    assert rate_log.latch is KillLevel.STOP_ENTRIES
