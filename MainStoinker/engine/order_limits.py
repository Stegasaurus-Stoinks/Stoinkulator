"""Order limits and the kill switch: the last offline check before an order may be sent.

Pure logic, no broker, no client, no clock of its own. `OrderGuard.check(intent, state, now)`
returns a `Decision(allowed, reason)`. The caller (the real broker, a later task) builds the
`OrderIntent` and the `OrderState` and refuses to send anything that is not allowed.

Fail closed: every limit is required (a missing, malformed or non-positive one is a
`LimitsConfigError`); an unreadable kill-switch path, a malformed intent, a malformed state or
any exception inside a check refuses the order. Nothing here can be relaxed by a setting:

- a position is never flipped,
- an exit is never larger than the confirmed open quantity minus the exits already in flight,
- cancels and protective orders are always allowed, even when the kill switch is engaged
  (they still must not flip or oversize).

Only entries are refused by the limits and by the kill switch. Times are naive New York
wall-clock times (the repository convention); an aware time is converted to New York time.

This module must not import anything from `MainStoinker.*` outside `MainStoinker.engine`,
nor `ibapi`.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

#: Name of the kill-switch file inside the owner's `Stoinkulator` folder (decision O11).
KILL_FILE_NAME = "STOP-ORDERS"

#: Recommended values for the first order sessions (decision O2). The run configuration may use
#: them; it must add `allowed_symbols`, which has no default. Every value is required.
FIRST_SESSION_LIMITS: Mapping[str, Any] = {
    "max_quantity": 10,  # FunctionTest hard-codes 10 shares
    "max_notional": 6000.0,  # USD per order
    "max_open_positions": 1,
    "max_entries_per_day": 20,
    "max_messages_per_minute": 6,  # modifications included
    "allowed_entry_sides": ("long",),  # no shorts before session C5 (decision O13)
    "entry_start": "09:35",
    "entry_end": "15:45",  # no new entries from this time on (decisions O3, O4)
}

#: Hard caps of the kill switch (decision O2).
KILL_MAX_SESSION_ORDERS = 60
KILL_MAX_SESSION_LOSS = 200.0


class LimitsConfigError(ValueError):
    """The limits are missing, incomplete or malformed. A broker must not be built."""


class IntentKind(Enum):
    ENTRY = "entry"  # opens a position
    EXIT = "exit"  # closes (part of) a position
    PROTECTIVE = "protective"  # places or modifies a stop or target for a position
    CANCEL = "cancel"  # cancels an order


class KillLevel(Enum):
    STOP_ENTRIES = "stop_entries"
    FLATTEN = "flatten"  # stop entries and the caller flattens


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    """One order message the broker wants to send.

    `side` is the order side, "buy" or "sell". For an entry, buy opens a long and sell a short.
    For an exit or a protective order it is the side of the order itself (a sell for a long).
    `price` is the reference price of the entry (needed for the notional). A cancel needs
    neither side nor quantity.
    """

    kind: IntentKind
    symbol: str
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0


@dataclass(frozen=True)
class OrderState:
    """What the broker knows when it asks. All fields have fail-closed meanings when odd."""

    open_positions: Mapping[str, float] = field(default_factory=dict)  # confirmed, signed
    pending_entries: Mapping[str, float] = field(default_factory=dict)  # entry qty not yet filled
    exits_in_flight: Mapping[str, float] = field(default_factory=dict)  # unfilled exit qty
    entries_today: int = 0  # entries already sent today, seeded across restarts
    message_times: tuple[datetime, ...] = ()  # when order messages were sent (recent)
    session_orders: int = 0
    session_pnl: float = 0.0  # negative is a loss


def _bad_number(value: object) -> bool:
    return (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    )


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LimitsConfigError(f"limit {name} must be a positive integer, got {value!r}")
    return value


def _positive_number(name: str, value: object) -> float:
    if _bad_number(value) or float(value) <= 0:  # type: ignore[arg-type]
        raise LimitsConfigError(f"limit {name} must be a positive number, got {value!r}")
    return float(value)  # type: ignore[arg-type]


def _parse_time(name: str, value: object) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            hh, mm = value.split(":")
            return time(int(hh), int(mm))
        except ValueError:
            pass
    raise LimitsConfigError(f"limit {name} must be a time like '09:35', got {value!r}")


@dataclass(frozen=True)
class OrderLimits:
    """Every field is required: a broker built without limits is a configuration error."""

    max_quantity: int
    max_notional: float
    max_open_positions: int
    max_entries_per_day: int
    max_messages_per_minute: int
    allowed_symbols: frozenset[str]
    allowed_entry_sides: frozenset[str]  # subset of {"long", "short"}
    entry_start: time
    entry_end: time

    def __post_init__(self) -> None:
        _positive_int("max_quantity", self.max_quantity)
        _positive_number("max_notional", self.max_notional)
        _positive_int("max_open_positions", self.max_open_positions)
        _positive_int("max_entries_per_day", self.max_entries_per_day)
        _positive_int("max_messages_per_minute", self.max_messages_per_minute)
        symbols = self.allowed_symbols
        if (
            not isinstance(symbols, frozenset)
            or not symbols
            or not all(isinstance(s, str) and s.strip() for s in symbols)
        ):
            raise LimitsConfigError("limit allowed_symbols must be a non-empty set of symbols")
        sides = self.allowed_entry_sides
        if (
            not isinstance(sides, frozenset)
            or not sides
            or not sides <= {"long", "short"}
        ):
            raise LimitsConfigError("limit allowed_entry_sides must be a subset of long, short")
        for name in ("entry_start", "entry_end"):
            if not isinstance(getattr(self, name), time):
                raise LimitsConfigError(f"limit {name} must be a time")
        if not self.entry_start < self.entry_end:
            raise LimitsConfigError("limit entry_start must be before entry_end")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> OrderLimits:
        """Build from a configuration mapping; anything missing or malformed raises."""
        if not isinstance(values, Mapping):
            raise LimitsConfigError("limits must be a mapping")
        names = (
            "max_quantity max_notional max_open_positions max_entries_per_day "
            "max_messages_per_minute allowed_symbols allowed_entry_sides entry_start entry_end"
        ).split()
        missing = [n for n in names if n not in values]
        if missing:
            raise LimitsConfigError(f"missing limits: {', '.join(missing)}")
        extra = sorted(set(values) - set(names))
        if extra:
            raise LimitsConfigError(f"unknown limits: {', '.join(extra)}")
        for name in ("allowed_symbols", "allowed_entry_sides"):
            raw = values[name]
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
                raise LimitsConfigError(f"limit {name} must be a list")
        return cls(
            max_quantity=values["max_quantity"],
            max_notional=values["max_notional"],
            max_open_positions=values["max_open_positions"],
            max_entries_per_day=values["max_entries_per_day"],
            max_messages_per_minute=values["max_messages_per_minute"],
            allowed_symbols=frozenset(values["allowed_symbols"]),
            allowed_entry_sides=frozenset(values["allowed_entry_sides"]),
            entry_start=_parse_time("entry_start", values["entry_start"]),
            entry_end=_parse_time("entry_end", values["entry_end"]),
        )


def default_kill_path() -> Path:
    """`%USERPROFILE%\\Stoinkulator\\STOP-ORDERS` (HOME when USERPROFILE is unset).

    Only builds the path; it never touches the file system. Raises `LimitsConfigError` when no
    home folder is known (the caller must not build a broker then).
    """
    home = (os.environ.get("USERPROFILE") or os.environ.get("HOME") or "").strip()
    if not home:
        raise LimitsConfigError("neither USERPROFILE nor HOME is set: no kill-switch path")
    return Path(home) / "Stoinkulator" / KILL_FILE_NAME


def file_kill_check(path: Path) -> Callable[[], KillLevel | None]:
    """A kill-file check, read afresh at every call.

    No file: not engaged. A file whose text is `flatten` (any case): `FLATTEN`. Any other file,
    empty or not: `STOP_ENTRIES`. A path that cannot be examined or read for any reason other
    than "not there" counts as engaged (`STOP_ENTRIES`): fail closed.
    """
    target = Path(path)

    def check() -> KillLevel | None:
        try:
            os.stat(target)
        except FileNotFoundError:
            # not there, unless an ancestor is a file (Windows reports that as not found too)
            for parent in target.parents:
                if parent.exists():
                    return None if parent.is_dir() else KillLevel.STOP_ENTRIES
            return None
        except OSError:
            return KillLevel.STOP_ENTRIES
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return KillLevel.STOP_ENTRIES
        return KillLevel.FLATTEN if text.strip().lower() == "flatten" else KillLevel.STOP_ENTRIES

    return check


class KillSwitch:
    """The file plus the hard caps. `level` is evaluated at every call; nothing is cached."""

    def __init__(
        self,
        file_check: Callable[[], KillLevel | None],
        *,
        max_session_orders: int = KILL_MAX_SESSION_ORDERS,
        max_session_loss: float = KILL_MAX_SESSION_LOSS,
    ) -> None:
        if not callable(file_check):
            raise LimitsConfigError("the kill switch needs a file check")
        self._file_check = file_check
        self.max_session_orders = _positive_int("max_session_orders", max_session_orders)
        self.max_session_loss = _positive_number("max_session_loss", max_session_loss)

    @classmethod
    def from_path(cls, path: Path | None = None, **caps: Any) -> KillSwitch:
        """A switch on the kill file at `path` (default: `default_kill_path()`)."""
        return cls(file_kill_check(default_kill_path() if path is None else path), **caps)

    def level(self, state: OrderState) -> tuple[KillLevel | None, str]:
        """The engaged level (None when not engaged) and why."""
        try:
            file_level = self._file_check()
        except Exception:  # noqa: BLE001 - an unreadable switch is an engaged switch
            return KillLevel.STOP_ENTRIES, "kill file check failed"
        if file_level is not None:
            if not isinstance(file_level, KillLevel):
                return KillLevel.STOP_ENTRIES, "kill file check returned something odd"
            return file_level, f"kill file engaged ({file_level.value})"
        pnl = state.session_pnl
        if _bad_number(pnl) or pnl <= -self.max_session_loss:
            return KillLevel.FLATTEN, f"session loss reached the cap of {self.max_session_loss:g}"
        count = state.session_orders
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return KillLevel.STOP_ENTRIES, "session order count unreadable"
        if count >= self.max_session_orders:
            return (
                KillLevel.STOP_ENTRIES,
                f"session order cap of {self.max_session_orders} reached",
            )
        return None, ""


def entries_sent_today(sent_entry_times: Iterable[datetime], today: date) -> int:
    """Seed for `OrderState.entries_today`: entries this program already sent on `today`."""
    return sum(1 for t in sent_entry_times if t.date() == today)


def _new_york_naive(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now
    return now.astimezone(NEW_YORK).replace(tzinfo=None)


def _refuse(reason: str) -> Decision:
    return Decision(False, reason)


_ALLOW = Decision(True, "ok")


class OrderGuard:
    """Limits plus kill switch. Build it with both; neither has a default."""

    def __init__(self, limits: OrderLimits, kill_switch: KillSwitch) -> None:
        if not isinstance(limits, OrderLimits):
            raise LimitsConfigError("a broker needs OrderLimits: none were given")
        if not isinstance(kill_switch, KillSwitch):
            raise LimitsConfigError("a broker needs a KillSwitch: none was given")
        self.limits = limits
        self.kill_switch = kill_switch

    def kill_level(self, state: OrderState) -> KillLevel | None:
        """The engaged kill level, or None. `FLATTEN` tells the caller to flatten."""
        return self.kill_switch.level(state)[0]

    def check(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        """Allow or refuse one order message. Never raises: an error refuses."""
        try:
            return self._check(intent, state, now)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return _refuse(f"check failed, order refused ({type(exc).__name__})")

    # -- internals ---------------------------------------------------------------------------

    def _check(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        if not isinstance(intent, OrderIntent) or not isinstance(intent.kind, IntentKind):
            return _refuse("malformed intent")
        if not isinstance(intent.symbol, str) or not intent.symbol:
            return _refuse("malformed intent: no symbol")
        if intent.kind is IntentKind.CANCEL:
            return _ALLOW
        if intent.side not in ("buy", "sell"):
            return _refuse(f"malformed intent: side {intent.side!r}")
        if _bad_number(intent.quantity) or intent.quantity <= 0:
            return _refuse(f"malformed intent: quantity {intent.quantity!r}")
        if intent.kind is IntentKind.ENTRY:
            return self._check_entry(intent, state, now)
        return self._check_reducing(intent, state)

    def _check_reducing(self, intent: OrderIntent, state: OrderState) -> Decision:
        """Exits and protective orders: never blocked by limits or the kill switch, but they
        must neither flip the position nor exceed it."""
        position = state.open_positions.get(intent.symbol, 0.0)
        pending = state.pending_entries.get(intent.symbol, 0.0)
        if _bad_number(position) or _bad_number(pending) or pending < 0:
            return _refuse("position state unreadable")
        if intent.kind is IntentKind.PROTECTIVE:
            # a stop or target may be placed with an entry that is still unfilled
            in_flight = 0.0
            if position == 0 and pending > 0:
                position = pending
        else:
            in_flight = state.exits_in_flight.get(intent.symbol, 0.0)
            if _bad_number(in_flight) or in_flight < 0:
                return _refuse("exits in flight unreadable")
        if position == 0:
            return _refuse("no open position to reduce: would open a position")
        reduces_with = "sell" if position > 0 else "buy"
        if intent.side != reduces_with:
            return _refuse("wrong direction: would add to or flip the position")
        available = abs(position) - in_flight
        if intent.quantity > available:
            return _refuse(
                f"exit of {intent.quantity:g} exceeds the confirmed open quantity "
                f"{abs(position):g} minus {in_flight:g} in flight"
            )
        return _ALLOW

    def _check_entry(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        lim = self.limits
        level, why = self.kill_switch.level(state)
        if level is not None:
            return _refuse(f"kill switch: {why}")
        symbol = intent.symbol
        if symbol not in lim.allowed_symbols:
            return _refuse(f"symbol {symbol} is not allowed")
        side = "long" if intent.side == "buy" else "short"
        if side not in lim.allowed_entry_sides:
            return _refuse(f"entry side {side} is not allowed")
        local = _new_york_naive(now)
        if not lim.entry_start <= local.time() < lim.entry_end:
            return _refuse(
                f"outside the entry window {lim.entry_start:%H:%M} to {lim.entry_end:%H:%M}"
            )
        if intent.quantity > lim.max_quantity:
            return _refuse(f"quantity {intent.quantity:g} exceeds the maximum {lim.max_quantity}")
        if _bad_number(intent.price) or intent.price <= 0:
            return _refuse("no usable reference price for the notional")
        notional = intent.quantity * intent.price
        if notional > lim.max_notional:
            return _refuse(f"notional {notional:.2f} exceeds the maximum {lim.max_notional:g}")
        held = state.open_positions.get(symbol, 0.0)
        if _bad_number(held):
            return _refuse("position state unreadable")
        if held != 0:
            flips = (held > 0) != (side == "long")
            return _refuse(
                "entry would flip the open position" if flips else "a position is already open"
            )
        open_now = {s for s, q in state.open_positions.items() if _bad_number(q) or q != 0}
        open_now |= {s for s, q in state.pending_entries.items() if _bad_number(q) or q != 0}
        if len(open_now) >= lim.max_open_positions:
            return _refuse(f"already {len(open_now)} open position(s), the maximum")
        entries = state.entries_today
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 0:
            return _refuse("entries today unreadable")
        if entries >= lim.max_entries_per_day:
            return _refuse(f"{entries} entries today, the daily maximum")
        # a message stamped in the future still counts: fail closed
        window = timedelta(seconds=60)
        recent = [t for t in state.message_times if local - _new_york_naive(t) < window]
        if len(recent) >= lim.max_messages_per_minute:
            return _refuse(f"{len(recent)} order messages in the last minute, the maximum")
        return _ALLOW
