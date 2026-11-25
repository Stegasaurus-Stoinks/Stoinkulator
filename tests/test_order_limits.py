"""Order limits and kill switch (issue 228): one test per limit, fail closed everywhere."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from MainStoinker.engine.order_limits import (
    FIRST_SESSION_LIMITS,
    KILL_FILE_NAME,
    Decision,
    IntentKind,
    KillLevel,
    KillSwitch,
    LimitsConfigError,
    OrderGuard,
    OrderIntent,
    OrderLimits,
    OrderState,
    default_kill_path,
    entries_sent_today,
    file_kill_check,
)

NOON = datetime(2026, 11, 24, 12, 0, 0)


def limits(**over: object) -> OrderLimits:
    values = {**FIRST_SESSION_LIMITS, "allowed_symbols": ["AAPL", "MSFT"], **over}
    return OrderLimits.from_mapping(values)


def never_engaged() -> KillLevel | None:
    return None


def guard(kill=never_engaged, **over: object) -> OrderGuard:
    return OrderGuard(limits(**over), KillSwitch(kill))


def entry(symbol: str = "AAPL", side: str = "buy", qty: float = 10, price: float = 100.0):
    return OrderIntent(IntentKind.ENTRY, symbol, side, qty, price)


def exit_(qty: float, side: str = "sell", symbol: str = "AAPL") -> OrderIntent:
    return OrderIntent(IntentKind.EXIT, symbol, side, qty)


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
    pending = OrderState(pending_entries={"MSFT": 10})
    assert not g.check(entry("AAPL"), pending, NOON).allowed
    assert guard(max_open_positions=2).check(entry("AAPL"), held, NOON).allowed


def test_refuses_max_entries_per_day() -> None:
    g = guard()
    assert g.check(entry(), OrderState(entries_today=19), NOON).allowed
    d = g.check(entry(), OrderState(entries_today=20), NOON)
    assert not d.allowed and "daily" in d.reason


def test_refuses_max_messages_per_minute() -> None:
    g = guard()
    sent = tuple(datetime(2026, 11, 24, 11, 59, 10 + i) for i in range(6))
    assert g.check(entry(), OrderState(message_times=sent[:5]), NOON).allowed
    d = g.check(entry(), OrderState(message_times=sent), NOON)
    assert not d.allowed and "minute" in d.reason
    old = tuple(datetime(2026, 11, 24, 11, 58, 59) for _ in range(9))
    assert g.check(entry(), OrderState(message_times=old), NOON).allowed
    future = tuple(datetime(2026, 11, 24, 12, 0, 30) for _ in range(6))
    assert not g.check(entry(), OrderState(message_times=future), NOON).allowed


def test_refuses_symbol_not_allowed() -> None:
    d = guard().check(entry("TSLA"), OrderState(), NOON)
    assert not d.allowed and "TSLA" in d.reason


def test_refuses_entry_side_not_allowed() -> None:
    assert guard(allowed_entry_sides=["short"]).check(entry(), OrderState(), NOON).allowed is False
    assert guard(allowed_entry_sides=["short"]).check(
        entry(side="sell"), OrderState(), NOON
    ).allowed


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


def test_risk_reducing_always_allowed_even_when_killed() -> None:
    engaged = guard(kill=lambda: KillLevel.FLATTEN)
    state = OrderState(
        open_positions={"AAPL": 10},
        entries_today=99,
        session_orders=999,
        session_pnl=-10_000.0,
        message_times=tuple(datetime(2026, 11, 24, 11, 59, 59) for _ in range(50)),
    )
    late = datetime(2026, 11, 24, 23, 0)
    assert not engaged.check(entry("MSFT"), state, late).allowed
    assert engaged.check(exit_(10), state, late).allowed
    assert engaged.check(OrderIntent(IntentKind.PROTECTIVE, "AAPL", "sell", 10), state, late).allowed
    assert engaged.check(OrderIntent(IntentKind.CANCEL, "AAPL"), state, late).allowed
    # a protective order may go out with an entry that is not filled yet
    pending = OrderState(pending_entries={"AAPL": 10})
    assert engaged.check(
        OrderIntent(IntentKind.PROTECTIVE, "AAPL", "sell", 10), pending, late
    ).allowed
    # a short position is reduced with a buy
    short = OrderState(open_positions={"AAPL": -4})
    assert engaged.check(exit_(4, side="buy"), short, late).allowed


def test_no_flip_no_oversized_exit() -> None:
    g = guard()
    long_ = OrderState(open_positions={"AAPL": 10})
    assert g.check(exit_(10), long_, NOON).allowed
    over = g.check(exit_(11), long_, NOON)
    assert not over.allowed and "exceeds" in over.reason
    # exits in flight reduce what may still be sent
    busy = OrderState(open_positions={"AAPL": 10}, exits_in_flight={"AAPL": 6})
    assert g.check(exit_(4), busy, NOON).allowed
    assert not g.check(exit_(5), busy, NOON).allowed
    # flat: a sell would open a short
    assert not g.check(exit_(1), OrderState(), NOON).allowed
    # wrong direction: a buy would add to a long
    assert not g.check(exit_(1, side="buy"), long_, NOON).allowed
    # a protective order larger than the position is refused too
    prot = OrderIntent(IntentKind.PROTECTIVE, "AAPL", "sell", 11)
    assert not g.check(prot, long_, NOON).allowed
    # malformed quantities
    for qty in (0, -1, float("nan")):
        assert not g.check(exit_(qty), long_, NOON).allowed
    assert not g.check(exit_(1, side=""), long_, NOON).allowed


def test_daily_count_seeded() -> None:
    today = date(2026, 11, 24)
    sent = [datetime(2026, 11, 24, 10, i) for i in range(20)]
    sent += [datetime(2026, 11, 23, 10, 0)] * 5  # yesterday does not count
    seed = entries_sent_today(sent, today)
    assert seed == 20
    g = guard()
    assert not g.check(entry(), OrderState(entries_today=seed), NOON).allowed
    assert g.check(entry(), OrderState(entries_today=entries_sent_today(sent[:19], today)), NOON).allowed


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


def test_kill_file_unreadable_path_refuses(tmp_path: Path) -> None:
    # a file where the folder should be: the path cannot be examined as "not there"
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    g = OrderGuard(limits(), KillSwitch.from_path(blocker / KILL_FILE_NAME))
    assert not g.check(entry(), OrderState(), NOON).allowed
    # a directory in place of the file cannot be read
    folder = tmp_path / "dir"
    folder.mkdir()
    assert file_kill_check(folder)() is KillLevel.STOP_ENTRIES
    # a check that raises
    def boom() -> KillLevel | None:
        raise RuntimeError("disk gone")

    assert not guard(kill=boom).check(entry(), OrderState(), NOON).allowed
    # a check that returns rubbish
    assert not guard(kill=lambda: "go").check(entry(), OrderState(), NOON).allowed  # type: ignore[arg-type,return-value]


def test_kill_hard_caps() -> None:
    g = guard()
    assert g.check(entry(), OrderState(session_orders=59, session_pnl=-199.99), NOON).allowed
    d = g.check(entry(), OrderState(session_orders=60), NOON)
    assert not d.allowed and "cap" in d.reason
    assert g.kill_level(OrderState(session_orders=60)) is KillLevel.STOP_ENTRIES
    assert not g.check(entry(), OrderState(session_pnl=-200.0), NOON).allowed
    assert g.kill_level(OrderState(session_pnl=-200.0)) is KillLevel.FLATTEN
    assert not g.check(entry(), OrderState(session_pnl=float("nan")), NOON).allowed


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
    assert not g.check(entry(), OrderState(entries_today="3"), NOON).allowed  # type: ignore[arg-type]
    assert not g.check(entry(), OrderState(), "noon").allowed  # type: ignore[arg-type]
    assert not g.check(entry(), None, NOON).allowed  # type: ignore[arg-type]
    # a cancel needs nothing else
    assert g.check(OrderIntent(IntentKind.CANCEL, "AAPL"), OrderState(), NOON).allowed
