"""Order limits and the kill switch: the last offline check before an order may be sent.

Pure logic, no broker, no client, no clock of its own. `OrderGuard.check(intent, state, now)`
returns a `Decision(allowed, reason, rate_exceeded)`. The caller (the real broker, a later task)
builds the `OrderIntent` and the `OrderState`, refuses to send anything not allowed, and calls
`SentLog.record_sent(intent, now)` for every message it really sends (a refused order consumes
nothing).

Fail closed: every limit is required (a missing, malformed or non-positive one is a
`LimitsConfigError`); an unreadable kill-switch path, a malformed intent, a malformed state or
any exception inside a check refuses the order.

Rules no setting can relax, for exits and protective orders as well as everything else:

- a position is never flipped,
- the quantity of a new exit or protective order is at most the confirmed open quantity (plus
  the pending entry it is a bracket child of) minus the exits already in flight,
- cancels and risk-reducing orders are allowed even when the kill switch is engaged.

Definitions the caller must follow:

- `OrderState.working_exits` lists every live order that reduces a position (sell side for a
  long, buy side for a short): plain exits, stops and targets. In flight per symbol is the sum of
  plain exits (no OCA group) in full plus, for each OCA group, its LARGEST leg once (one-cancels-
  all legs never fill together). The order a replacement replaces (`replaces_order`) and the
  intent's own OCA group are left out of that sum.
- A protective order is NEW, or a REPLACEMENT of the working order named in `replaces_order`.
  A bracket child names its pending parent in `parent_ref`; only then is it sized against the
  pending entry. A standalone protective order is sized against the confirmed position only.
- Every order message counts: the per-minute limit (6) and the session cap (60) include
  modifications and cancels. Past either, entries and replacements that do not strictly reduce
  risk are refused; a pure exit, a new protective order and a cancel are still allowed, the
  decision carries `rate_exceeded=True` and the kill switch latches `stop_entries`. A
  replacement strictly reduces risk only when the stop does not move away from the price (a
  lower price for a sell stop, a higher one for a buy stop) and its quantity covers the whole
  position; it must carry `old_price` and `new_price`, or it is refused.
- The caps latch: once the loss cap or the order cap (or a rate overrun) engages, the kill
  switch stays engaged for the life of the object, even if `session_pnl` recovers. `session_pnl`
  is whatever the caller supplies (realized plus unrealized is the safe choice). Caps cannot be
  raised above 60 orders / 200 USD, and the objects are frozen after construction.
- Quantities are whole positive numbers. Symbols are normalised (strip, upper-case); a state
  key or an allowed symbol that would change under normalisation is a configuration error.

Times are naive New York wall-clock times (the repository convention); an aware time is
converted to New York time.

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

#: Hard caps of the kill switch (decision O2). They can be lowered, never raised.
KILL_MAX_SESSION_ORDERS = 60
KILL_MAX_SESSION_LOSS = 200.0

#: A kill file longer than this engages the switch without being read.
KILL_FILE_MAX_BYTES = 4096


class LimitsConfigError(ValueError):
    """The limits or a mapping handed to the guard are missing, incomplete or malformed."""


class IntentKind(Enum):
    ENTRY = "entry"  # opens a position
    EXIT = "exit"  # closes (part of) a position
    PROTECTIVE = "protective"  # places or replaces a stop or target for a position
    CANCEL = "cancel"  # cancels an order


class KillLevel(Enum):
    STOP_ENTRIES = "stop_entries"
    FLATTEN = "flatten"  # stop entries and the caller flattens


_RANK = {None: 0, KillLevel.STOP_ENTRIES: 1, KillLevel.FLATTEN: 2}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    rate_exceeded: bool = False


def normalise_symbol(symbol: str) -> str:
    return symbol.strip().upper()


@dataclass(frozen=True)
class OrderIntent:
    """One order message the broker wants to send.

    `side` is the order side, "buy" or "sell". For an entry, buy opens a long and sell a short.
    For an exit or a protective order it is the side of the order itself (a sell for a long).
    `price` is the reference price of an entry (for the notional). A cancel needs neither side
    nor quantity.

    Protective orders: `parent_ref` names the pending entry of a bracket child; `oca_group` is
    the one-cancels-all group of the new order; `replaces_order` names the working order being
    replaced, and then `old_price` and `new_price` (the stop prices) are required.
    """

    kind: IntentKind
    symbol: str
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    parent_ref: str = ""
    oca_group: str = ""
    replaces_order: str = ""
    old_price: float | None = None
    new_price: float | None = None


@dataclass(frozen=True)
class WorkingExit:
    """A live order that reduces a position (see the module docstring)."""

    order_id: str
    symbol: str
    quantity: float
    oca_group: str = ""


@dataclass(frozen=True)
class PendingEntry:
    """An entry not yet filled. `quantity` is signed: positive long, negative short."""

    symbol: str
    quantity: float


@dataclass(frozen=True)
class SentView:
    """Read-only counts the guard consumes, produced by `SentLog.view`."""

    message_times: tuple[datetime, ...] = ()
    entries_today: int = 0
    session_orders: int = 0


@dataclass(frozen=True)
class OrderState:
    """What the broker knows when it asks. Odd values fail closed."""

    open_positions: Mapping[str, float] = field(default_factory=dict)  # confirmed, signed
    pending_entries: Mapping[str, PendingEntry] = field(default_factory=dict)  # by order ref
    working_exits: tuple[WorkingExit, ...] = ()
    sent: SentView = field(default_factory=SentView)
    session_pnl: float = 0.0  # negative is a loss


def _bad_number(value: object) -> bool:
    return (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    )


def _whole_positive(value: object) -> bool:
    return not _bad_number(value) and value > 0 and float(value).is_integer()  # type: ignore[operator,arg-type]


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
        if any(normalise_symbol(s) != s for s in symbols):
            raise LimitsConfigError(
                "limit allowed_symbols must be written upper-case without spaces"
            )
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


def decode_kill_text(raw: bytes) -> KillLevel:
    """Level of a kill file's bytes: `flatten` as the whole trimmed content (any case) means
    FLATTEN, anything else, including undecodable or oversized content, STOP_ENTRIES.

    Tolerates a UTF-8 BOM, UTF-16 LE/BE with a BOM (PowerShell 5.1 `echo flatten > file`),
    surrounding whitespace and NULs.
    """
    if len(raw) > KILL_FILE_MAX_BYTES:
        return KillLevel.STOP_ENTRIES
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return KillLevel.STOP_ENTRIES
    text = text.replace("\x00", "").replace("﻿", "").strip().lower()
    return KillLevel.FLATTEN if text == "flatten" else KillLevel.STOP_ENTRIES


def file_kill_check(path: Path) -> Callable[[], KillLevel | None]:
    """A kill-file check, read afresh at every call.

    No file: not engaged. Otherwise `decode_kill_text` of its bytes. A path that cannot be
    examined or read for any reason other than "not there" counts as engaged (`STOP_ENTRIES`).
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
            with open(target, "rb") as handle:
                raw = handle.read(KILL_FILE_MAX_BYTES + 1)
        except OSError:
            return KillLevel.STOP_ENTRIES
        return decode_kill_text(raw)

    return check


class _Frozen:
    """No attribute assignment once `_seal` was called (internals mutate their own objects)."""

    def __setattr__(self, name: str, value: object) -> None:
        if self.__dict__.get("_sealed"):
            raise AttributeError(f"{type(self).__name__} is frozen: cannot set {name}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is frozen: cannot delete {name}")

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)


class KillSwitch(_Frozen):
    """The file plus the hard caps. The file is evaluated at every call; the caps latch."""

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
        if self.max_session_orders > KILL_MAX_SESSION_ORDERS:
            raise LimitsConfigError(f"max_session_orders may not exceed {KILL_MAX_SESSION_ORDERS}")
        if self.max_session_loss > KILL_MAX_SESSION_LOSS:
            raise LimitsConfigError(f"max_session_loss may not exceed {KILL_MAX_SESSION_LOSS:g}")
        self._latch: dict[str, Any] = {"level": None, "why": ""}
        self._seal()

    @classmethod
    def from_path(cls, path: Path | None = None, **caps: Any) -> KillSwitch:
        """A switch on the kill file at `path` (default: `default_kill_path()`)."""
        return cls(file_kill_check(default_kill_path() if path is None else path), **caps)

    def engage(self, level: KillLevel, why: str) -> None:
        """Latch `level` for the life of this switch (never lowered)."""
        if _RANK[level] > _RANK[self._latch["level"]]:
            self._latch["level"] = level
            self._latch["why"] = why

    def level(self, state: OrderState) -> tuple[KillLevel | None, str]:
        """The engaged level (None when not engaged) and why."""
        try:
            pnl = state.session_pnl
            if _bad_number(pnl) or pnl <= -self.max_session_loss:
                self.engage(
                    KillLevel.FLATTEN, f"session loss reached the cap of {self.max_session_loss:g}"
                )
            count = state.sent.session_orders
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                self.engage(KillLevel.STOP_ENTRIES, "session order count unreadable")
            elif count >= self.max_session_orders:
                self.engage(
                    KillLevel.STOP_ENTRIES, f"session order cap of {self.max_session_orders} reached"
                )
        except Exception:  # noqa: BLE001 - an unreadable state is an engaged switch
            self.engage(KillLevel.STOP_ENTRIES, "session state unreadable")
        try:
            file_level = self._file_check()
        except Exception:  # noqa: BLE001 - an unreadable switch is an engaged switch
            file_level, file_why = KillLevel.STOP_ENTRIES, "kill file check failed"
        else:
            file_why = f"kill file engaged ({getattr(file_level, 'value', 'odd value')})"
            if file_level is not None and not isinstance(file_level, KillLevel):
                file_level = KillLevel.STOP_ENTRIES
        latched = self._latch["level"]
        if _RANK[file_level] > _RANK[latched]:
            return file_level, file_why
        return latched, self._latch["why"]


def entries_sent_today(sent_entry_times: Iterable[datetime], today: date) -> int:
    """Entries this program already sent on the New York date `today`."""
    return sum(1 for t in sent_entry_times if _new_york_naive(t).date() == today)


def _new_york_naive(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now
    return now.astimezone(NEW_YORK).replace(tzinfo=None)


class SentLog:
    """Owns what was really sent: message times, entries per day, session order count.

    The broker calls `record_sent` once per message it actually sends; the guard only reads
    `view`. A refused order is never recorded, so it consumes nothing. The latches live in the
    `KillSwitch` (same lifetime as the guard).
    """

    _KEEP = timedelta(minutes=2)

    def __init__(self, earlier_entry_times: Iterable[datetime] = ()) -> None:
        """`earlier_entry_times`: entries this program already sent (any day; used for today)."""
        self._times: list[datetime] = []
        self._entries: dict[date, int] = {}
        self._orders = 0
        for t in earlier_entry_times:
            day = _new_york_naive(t).date()
            self._entries[day] = self._entries.get(day, 0) + 1

    def record_sent(self, intent: OrderIntent, now: datetime) -> None:
        local = _new_york_naive(now)
        self._times = [t for t in self._times if local - t < self._KEEP]
        self._times.append(local)
        self._orders += 1
        if intent.kind is IntentKind.ENTRY:
            day = local.date()
            self._entries[day] = self._entries.get(day, 0) + 1

    def view(self, now: datetime) -> SentView:
        local = _new_york_naive(now)
        return SentView(
            message_times=tuple(self._times),
            entries_today=self._entries.get(local.date(), 0),
            session_orders=self._orders,
        )


def _refuse(reason: str, rate: bool = False) -> Decision:
    return Decision(False, reason, rate)


class OrderGuard(_Frozen):
    """Limits plus kill switch. Build it with both; neither has a default."""

    def __init__(self, limits: OrderLimits, kill_switch: KillSwitch) -> None:
        if not isinstance(limits, OrderLimits):
            raise LimitsConfigError("a broker needs OrderLimits: none were given")
        if not isinstance(kill_switch, KillSwitch):
            raise LimitsConfigError("a broker needs a KillSwitch: none was given")
        self.limits = limits
        self.kill_switch = kill_switch
        self._seal()

    def kill_level(self, state: OrderState) -> KillLevel | None:
        """The engaged kill level, or None. `FLATTEN` tells the caller to flatten."""
        return self.kill_switch.level(state)[0]

    def check(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        """Allow or refuse one order message. Never raises: an error refuses."""
        try:
            return self._check(intent, state, now)
        except LimitsConfigError as exc:
            return _refuse(f"configuration error, order refused ({exc})")
        except Exception as exc:  # noqa: BLE001 - fail closed
            return _refuse(f"check failed, order refused ({type(exc).__name__})")

    # -- internals ---------------------------------------------------------------------------

    def _check(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        if not isinstance(intent, OrderIntent) or not isinstance(intent.kind, IntentKind):
            return _refuse("malformed intent")
        if not isinstance(intent.symbol, str) or not normalise_symbol(intent.symbol):
            return _refuse("malformed intent: no symbol")
        symbol = normalise_symbol(intent.symbol)
        if not isinstance(state, OrderState):
            return _refuse("malformed state")
        local = _new_york_naive(now)
        over = self._rate_exceeded(state, local)
        if over:
            self.kill_switch.engage(KillLevel.STOP_ENTRIES, over)
        self.kill_switch.level(state)  # latches the hard caps on every message
        if intent.kind is IntentKind.CANCEL:
            return Decision(True, "ok", bool(over))
        if intent.side not in ("buy", "sell"):
            return _refuse(f"malformed intent: side {intent.side!r}", bool(over))
        if not _whole_positive(intent.quantity):
            return _refuse(f"malformed intent: quantity {intent.quantity!r}", bool(over))
        if intent.kind is IntentKind.ENTRY:
            return self._check_entry(intent, symbol, state, local, bool(over))
        return self._check_reducing(intent, symbol, state, bool(over))

    def _rate_exceeded(self, state: OrderState, local: datetime) -> str:
        """Why the message rate is over (empty when it is not)."""
        limit = self.limits.max_messages_per_minute
        window = timedelta(seconds=60)
        # a message stamped in the future still counts: fail closed
        recent = sum(1 for t in state.sent.message_times if local - _new_york_naive(t) < window)
        if recent >= limit:
            return f"{recent} order messages in the last minute, the maximum is {limit}"
        count = state.sent.session_orders
        if isinstance(count, int) and not isinstance(count, bool) and (
            count >= self.kill_switch.max_session_orders
        ):
            return f"session order cap of {self.kill_switch.max_session_orders} reached"
        return ""

    def _normalised(self, state: OrderState) -> tuple[dict[str, float], dict[str, PendingEntry]]:
        positions: dict[str, float] = {}
        for key, qty in state.open_positions.items():
            if not isinstance(key, str) or normalise_symbol(key) != key or not key:
                raise LimitsConfigError(f"state symbol {key!r} is not normalised")
            positions[key] = qty
        pendings: dict[str, PendingEntry] = {}
        for ref, entry in state.pending_entries.items():
            if not isinstance(entry, PendingEntry) or not isinstance(entry.symbol, str):
                raise LimitsConfigError("pending entry is malformed")
            if normalise_symbol(entry.symbol) != entry.symbol or not entry.symbol:
                raise LimitsConfigError(f"state symbol {entry.symbol!r} is not normalised")
            pendings[ref] = entry
        return positions, pendings

    def _check_reducing(
        self, intent: OrderIntent, symbol: str, state: OrderState, over: bool
    ) -> Decision:
        """Exits and protective orders: never blocked by limits or the kill switch, but they
        must neither flip the position nor exceed it, and past the rate a replacement must
        strictly reduce risk."""
        positions, pendings = self._normalised(state)
        held = positions.get(symbol, 0.0)
        if _bad_number(held):
            return _refuse("position state unreadable", over)
        net = held
        if intent.kind is IntentKind.PROTECTIVE and intent.parent_ref:
            parent = pendings.get(intent.parent_ref)
            if parent is None or parent.symbol != symbol or _bad_number(parent.quantity):
                return _refuse("the named parent entry is not pending for this symbol", over)
            if parent.quantity == 0 or (held != 0 and (held > 0) != (parent.quantity > 0)):
                return _refuse("parent entry conflicts with the open position", over)
            net = held + parent.quantity
        if net == 0:
            return _refuse("no open position to reduce: would open a position", over)
        reduces_with = "sell" if net > 0 else "buy"
        if intent.side != reduces_with:
            return _refuse("wrong direction: would add to or flip the position", over)
        in_flight = self._in_flight(intent, symbol, state)
        available = abs(net) - in_flight
        if intent.quantity > available:
            return _refuse(
                f"order of {intent.quantity:g} exceeds the position {abs(net):g} "
                f"minus {in_flight:g} in flight",
                over,
            )
        if intent.kind is IntentKind.PROTECTIVE and intent.replaces_order:
            reducing = self._replacement_reduces_risk(intent, net)
            if reducing is None:
                return _refuse("replacement needs old_price and new_price", over)
            if over and not reducing:
                return _refuse("order rate exceeded and the replacement does not reduce risk", True)
        return Decision(True, "ok", over)

    @staticmethod
    def _in_flight(intent: OrderIntent, symbol: str, state: OrderState) -> float:
        plain = 0.0
        groups: dict[str, float] = {}
        for working in state.working_exits:
            if not isinstance(working, WorkingExit) or not isinstance(working.symbol, str):
                raise LimitsConfigError("working exit is malformed")
            if normalise_symbol(working.symbol) != working.symbol:
                raise LimitsConfigError(f"state symbol {working.symbol!r} is not normalised")
            if working.symbol != symbol:
                continue
            if _bad_number(working.quantity) or working.quantity <= 0:
                raise LimitsConfigError("working exit quantity unreadable")
            if intent.replaces_order and working.order_id == intent.replaces_order:
                continue
            if intent.oca_group and working.oca_group == intent.oca_group:
                continue
            if working.oca_group:
                groups[working.oca_group] = max(groups.get(working.oca_group, 0.0), working.quantity)
            else:
                plain += working.quantity
        return plain + sum(groups.values())

    @staticmethod
    def _replacement_reduces_risk(intent: OrderIntent, net: float) -> bool | None:
        old, new = intent.old_price, intent.new_price
        if old is None or new is None or _bad_number(old) or _bad_number(new):
            return None
        if old <= 0 or new <= 0:
            return None
        away = new < old if intent.side == "sell" else new > old
        return not away and intent.quantity >= abs(net)

    def _check_entry(
        self, intent: OrderIntent, symbol: str, state: OrderState, local: datetime, over: bool
    ) -> Decision:
        lim = self.limits
        level, why = self.kill_switch.level(state)
        if level is not None:
            return _refuse(f"kill switch: {why}", over)
        if symbol not in lim.allowed_symbols:
            return _refuse(f"symbol {symbol} is not allowed")
        side = "long" if intent.side == "buy" else "short"
        if side not in lim.allowed_entry_sides:
            return _refuse(f"entry side {side} is not allowed")
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
        positions, pendings = self._normalised(state)
        held = positions.get(symbol, 0.0)
        if _bad_number(held):
            return _refuse("position state unreadable")
        if held != 0:
            flips = (held > 0) != (side == "long")
            return _refuse(
                "entry would flip the open position" if flips else "a position is already open"
            )
        if any(entry.symbol == symbol for entry in pendings.values()):
            return _refuse("an entry on this symbol is already pending")
        open_now = {s for s, q in positions.items() if _bad_number(q) or q != 0}
        open_now |= {e.symbol for e in pendings.values()}
        if len(open_now) >= lim.max_open_positions:
            return _refuse(f"already {len(open_now)} open position(s), the maximum")
        entries = state.sent.entries_today
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 0:
            return _refuse("entries today unreadable")
        if entries >= lim.max_entries_per_day:
            return _refuse(f"{entries} entries today, the daily maximum")
        return Decision(True, "ok")
