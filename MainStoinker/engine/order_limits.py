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

Contract for the callers (the real broker, issues 238 and 242):

- A REPLACEMENT is sent under the same broker order id as the working order it names
  (`replaces_order`); the guard checks that name against `working_exits` (same symbol, same
  side, a stop or target, same kind). In IB a placeOrder with an unknown id is a NEW order.
- Replacement is "strictly reducing risk" for STOPS only. A target replacement over the rate is
  refused (targets are never urgent).
- `parent_ref` (bracket child) is refused when the symbol already holds a position, and the
  pending quantity of the parent must be the full REMAINING entry: reduce `PendingEntry.quantity`
  as the entry fills (or drop the entry once filled).
- An OCA group belongs to one symbol; a state with a group spanning symbols is a configuration
  error and an intent joining another symbol's group is refused.
- Over the rate, cancels and risk-reducing protective orders have no ceiling BY DESIGN (they
  must never be trapped). The caller MUST treat `Decision.rate_exceeded` as "protect, then end
  the session" (issue 238), or a runaway loop is unbounded.
- A cancel needs only a valid `order_ref` and is allowed whatever the state or the time look
  like. An EXIT depends only on its own symbol's position and working exits plus the kill
  switch: malformed unrelated state does not refuse it, but is reported in
  `Decision.state_errors` and latches `stop_entries`. Entries and protective orders are fully
  fail-closed. An EXIT may not carry `oca_group`, `replaces_order` or `parent_ref`.
- A session is one New York trading day. After a restart on the same day seed `SentLog` with
  that day's order count, message times and engaged latch (`KillSwitch` takes `seeded_latch`
  and never un-engages).

Out of scope: in-process bypasses (`object.__setattr__`, private attributes, monkeypatching).
The freezing here stops accidents and honest mistakes in code that is reviewed; code that can run
arbitrary Python in the process can already send an order without this module.

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
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

#: Name of the kill-switch file inside the owner's `Stoinkulator` folder (decision O11).
KILL_FILE_NAME = "STOP-ORDERS"

#: Recommended values for the first order sessions (decision O2). The run configuration may use
#: them; it must add `allowed_symbols`, which has no default. Every value is required.
FIRST_SESSION_LIMITS: Mapping[str, Any] = MappingProxyType({
    "max_quantity": 10,  # FunctionTest hard-codes 10 shares
    "max_notional": 6000.0,  # USD per order
    "max_open_positions": 1,
    "max_entries_per_day": 20,
    "max_messages_per_minute": 6,  # modifications included
    "allowed_entry_sides": ("long",),  # no shorts before session C5 (decision O13)
    "entry_start": "09:35",
    "entry_end": "15:45",  # no new entries from this time on (decisions O3, O4)
})

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


class ExitKind(Enum):
    PLAIN = "plain"  # an exit order (market or limit) that is not a stop or target
    STOP = "stop"
    TARGET = "target"


_RANK = {None: 0, KillLevel.STOP_ENTRIES: 1, KillLevel.FLATTEN: 2}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    rate_exceeded: bool = False
    state_errors: tuple[str, ...] = ()


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
    protective_kind: ExitKind = ExitKind.STOP  # STOP or TARGET, for PROTECTIVE intents
    order_ref: str = ""  # the order a CANCEL cancels


@dataclass(frozen=True)
class WorkingExit:
    """A live order that reduces a position (see the module docstring)."""

    order_id: str
    symbol: str
    quantity: float
    oca_group: str = ""
    kind: ExitKind = ExitKind.PLAIN
    side: str = ""  # "buy" or "sell"


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
    latch: KillLevel | None = None  # engaged latch carried over (seeded) from `SentLog`


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
        seeded_latch: KillLevel | None = None,
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
        if seeded_latch is not None:
            if not isinstance(seeded_latch, KillLevel):
                raise LimitsConfigError("a seeded latch must be a KillLevel or None")
            self.engage(seeded_latch, "latch carried over from earlier today")
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

    @property
    def latched(self) -> KillLevel | None:
        """The latched level (the file is not part of it); persist it with `SentLog`."""
        return self._latch["level"]  # type: ignore[no-any-return]

    def level(self, state: OrderState) -> tuple[KillLevel | None, str]:
        """The engaged level (None when not engaged) and why."""
        try:
            carried = state.sent.latch
            if carried is not None:
                if isinstance(carried, KillLevel):
                    self.engage(carried, "latch carried over from earlier today")
                else:
                    self.engage(KillLevel.STOP_ENTRIES, "carried latch unreadable")
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
    """Owns what was really sent: message times, entries per day, session order count, latch.

    The broker calls `record_sent` once per message it actually sends; the guard only reads
    `view`. A refused order is never recorded, so it consumes nothing. A session is one New York
    trading day: after a restart build the log with that day's session order count, message
    times and latch (`KillSwitch.latched` is what to persist; `note_latch` copies it here), so
    the 60-order cap and an engaged latch survive. It has no public mutable attributes.
    """

    __slots__ = ("_day", "_entries", "_latch", "_orders", "_times")
    _KEEP = timedelta(minutes=2)

    def __init__(
        self,
        earlier_entry_times: Iterable[datetime] = (),
        *,
        trading_day: date,
        session_orders: int = 0,
        message_times: Iterable[datetime] = (),
        latch: KillLevel | None = None,
    ) -> None:
        """`trading_day`: the New York day of this session (entries whose time is unusable
        count for it). `earlier_entry_times`: entries this program already sent (any day)."""
        if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
            raise LimitsConfigError("trading_day must be a date")
        self._day = trading_day
        if isinstance(session_orders, bool) or not isinstance(session_orders, int) or (
            session_orders < 0
        ):
            raise LimitsConfigError("seeded session_orders must be a non-negative integer")
        if latch is not None and not isinstance(latch, KillLevel):
            raise LimitsConfigError("a seeded latch must be a KillLevel or None")
        self._times: list[datetime] = []
        self._entries: dict[date, int] = {}
        self._orders = session_orders
        self._latch = latch
        try:
            for t in message_times:
                self._times.append(_new_york_naive(t))
            for t in earlier_entry_times:
                day = _new_york_naive(t).date()
                self._entries[day] = self._entries.get(day, 0) + 1
        except (AttributeError, TypeError) as exc:
            raise LimitsConfigError("seeded times must be datetimes") from exc

    @property
    def latch(self) -> KillLevel | None:
        return self._latch

    def note_latch(self, level: KillLevel | None) -> None:
        """Keep the strongest latch seen (never lowers)."""
        if level is not None and not isinstance(level, KillLevel):
            raise LimitsConfigError("a latch must be a KillLevel or None")
        if level is not None and _RANK[level] > _RANK[self._latch]:
            self._latch = level

    def record_sent(self, intent: OrderIntent, now: datetime) -> None:
        """Count one message really sent. It always counts. With an unusable `now` it uses the
        latest known time; a message dated another day than the trading day counts for the
        trading day. Both latch `stop_entries`."""
        self._orders += 1
        try:
            local: datetime | None = _new_york_naive(now)
            local - timedelta(0)  # type: ignore[operator]  # raises for anything but a datetime
        except Exception:  # noqa: BLE001 - count first, then latch
            local = max(self._times) if self._times else None
            self.note_latch(KillLevel.STOP_ENTRIES)
        day = self._day
        if local is not None:
            self._times = [t for t in self._times if local - t < self._KEEP]
            self._times.append(local)
            if local.date() != self._day:
                self.note_latch(KillLevel.STOP_ENTRIES)
        if getattr(intent, "kind", None) is IntentKind.ENTRY:
            self._entries[day] = self._entries.get(day, 0) + 1

    def view(self, now: datetime) -> SentView:
        local = _new_york_naive(now)
        return SentView(
            message_times=tuple(self._times),
            entries_today=self._entries.get(local.date(), 0),
            session_orders=self._orders,
            latch=self._latch,
        )


def _refuse(reason: str, rate: bool = False, errors: tuple[str, ...] = ()) -> Decision:
    return Decision(False, reason, rate, errors)


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

    def check(
        self, intent: OrderIntent, state: OrderState, now: datetime, log: SentLog | None = None
    ) -> Decision:
        """Allow or refuse one order message. Never raises: an error refuses.

        Pass the `SentLog` as `log`: any latch this guard has engaged (by this call or an
        earlier one) is then recorded in it, so it survives a restart seeded from the log.
        """
        try:
            return self._check(intent, state, now)
        except LimitsConfigError as exc:
            return _refuse(f"configuration error, order refused ({exc})")
        except Exception as exc:  # noqa: BLE001 - fail closed
            return _refuse(f"check failed, order refused ({type(exc).__name__})")
        finally:
            if log is not None:
                log.note_latch(self.kill_switch.latched)

    # -- internals ---------------------------------------------------------------------------

    def _check(self, intent: OrderIntent, state: OrderState, now: datetime) -> Decision:
        if not isinstance(intent, OrderIntent) or not isinstance(intent.kind, IntentKind):
            return _refuse("malformed intent")
        if intent.kind is IntentKind.CANCEL:
            return self._check_cancel(intent, state, now)
        if not isinstance(intent.symbol, str) or not normalise_symbol(intent.symbol):
            return _refuse("malformed intent: no symbol")
        symbol = normalise_symbol(intent.symbol)
        errors: list[str] = []
        lenient = intent.kind is IntentKind.EXIT
        over = self._rate(state, now, errors, lenient)
        if over:
            self.kill_switch.engage(KillLevel.STOP_ENTRIES, over)
        self._level(state, errors, lenient)
        flag = bool(over)
        if intent.side not in ("buy", "sell"):
            return _refuse(f"malformed intent: side {intent.side!r}", flag, tuple(errors))
        if not _whole_positive(intent.quantity):
            return _refuse(f"malformed intent: quantity {intent.quantity!r}", flag, tuple(errors))
        if intent.kind is IntentKind.ENTRY:
            return self._check_entry(intent, symbol, state, _new_york_naive(now), flag)
        return self._check_reducing(intent, symbol, state, flag, errors, lenient)

    def _check_cancel(self, intent: OrderIntent, state: object, now: object) -> Decision:
        """A cancel needs only a valid order ref; nothing else can refuse it."""
        ref = intent.order_ref
        if not isinstance(ref, str) or not ref.strip():
            return _refuse("a cancel needs an order ref")
        if (
            intent.side != ""
            or intent.quantity != 0.0
            or intent.price != 0.0
            or intent.parent_ref != ""
            or intent.oca_group != ""
            or intent.replaces_order != ""
            or intent.old_price is not None
            or intent.new_price is not None
        ):
            return _refuse("a cancel is only a kind and an order ref: it carries order fields")
        if not isinstance(intent.symbol, str) or (
            intent.symbol != "" and not normalise_symbol(intent.symbol)
        ):
            return _refuse("a cancel's symbol, when given, must be a valid symbol")
        errors: list[str] = []
        over = self._rate(state, now, errors, True)  # type: ignore[arg-type]
        if over:
            self.kill_switch.engage(KillLevel.STOP_ENTRIES, over)
        self._level(state, errors, True)  # type: ignore[arg-type]
        return Decision(True, "ok", bool(over), tuple(errors))

    def _level(self, state: OrderState, errors: list[str], lenient: bool) -> None:
        """Latch the hard caps; unreadable state is reported (and fails closed unless lenient)."""
        try:
            self.kill_switch.level(state)
        except Exception as exc:  # noqa: BLE001
            if not lenient:
                raise
            errors.append(f"kill switch state unreadable ({type(exc).__name__})")
        if errors:
            self.kill_switch.engage(KillLevel.STOP_ENTRIES, "malformed state: " + errors[0])

    def _rate(self, state: OrderState, now: datetime, errors: list[str], lenient: bool) -> str:
        """Why the message rate is over (empty when it is not). When the data is unreadable:
        raise (fail closed) or, for exits and cancels, count it as over and report it."""
        try:
            return self._rate_exceeded(state, _new_york_naive(now))
        except Exception as exc:  # noqa: BLE001
            if not lenient:
                raise
            errors.append(f"rate data unreadable ({type(exc).__name__})")
            return "order rate data unreadable"

    def _rate_exceeded(self, state: OrderState, local: datetime) -> str:
        limit = self.limits.max_messages_per_minute
        window = timedelta(seconds=60)
        # a message stamped in the future still counts: fail closed
        recent = sum(1 for t in state.sent.message_times if local - _new_york_naive(t) < window)
        if recent >= limit:
            return f"{recent} order messages in the last minute, the maximum is {limit}"
        count = state.sent.session_orders
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= self.kill_switch.max_session_orders
        ):
            return f"session order cap of {self.kill_switch.max_session_orders} reached"
        return ""

    @staticmethod
    def _valid_symbol_key(key: object) -> str:
        if not isinstance(key, str) or not key or normalise_symbol(key) != key:
            raise LimitsConfigError(f"state symbol {key!r} is not normalised")
        return key

    def _normalised(
        self, state: OrderState
    ) -> tuple[dict[str, float], dict[str, PendingEntry], list[WorkingExit]]:
        """Strict reading of the whole state: anything odd is a configuration error."""
        positions: dict[str, float] = {}
        for key, qty in state.open_positions.items():
            positions[self._valid_symbol_key(key)] = qty
        pendings: dict[str, PendingEntry] = {}
        for ref, entry in state.pending_entries.items():
            if not isinstance(entry, PendingEntry):
                raise LimitsConfigError("pending entry is malformed")
            self._valid_symbol_key(entry.symbol)
            pendings[ref] = entry
        exits = [self._valid_exit(w) for w in state.working_exits]
        groups: dict[str, set[str]] = {}
        for w in exits:
            if w.oca_group:
                groups.setdefault(w.oca_group, set()).add(w.symbol)
        if any(len(symbols) > 1 for symbols in groups.values()):
            raise LimitsConfigError("an OCA group spans more than one symbol")
        return positions, pendings, exits

    def _valid_exit(self, w: object) -> WorkingExit:
        if not isinstance(w, WorkingExit) or not isinstance(w.kind, ExitKind):
            raise LimitsConfigError("working exit is malformed")
        self._valid_symbol_key(w.symbol)
        if _bad_number(w.quantity) or w.quantity <= 0:
            raise LimitsConfigError("working exit quantity unreadable")
        return w

    def _lenient_view(
        self, state: OrderState, symbol: str, errors: list[str]
    ) -> tuple[float, list[WorkingExit]]:
        """An exit's view of the state: its own symbol's position and working exits. Problems
        in other symbols' entries are reported, not fatal; problems in its own refuse."""
        held = 0.0
        for key, qty in state.open_positions.items():
            try:
                self._valid_symbol_key(key)
            except LimitsConfigError:
                if isinstance(key, str) and normalise_symbol(key) == symbol:
                    raise
                errors.append(f"state key {key!r} is not normalised")
                continue
            if key == symbol:
                held = qty
        own: list[WorkingExit] = []
        for w in state.working_exits:
            # skipped only when it clearly names ANOTHER valid, normalised symbol
            if not isinstance(w, WorkingExit):
                raise LimitsConfigError("a working exit has no readable symbol")
            self._valid_symbol_key(w.symbol)
            if w.symbol == symbol:
                own.append(self._valid_exit(w))
                continue
            try:
                self._valid_exit(w)
            except LimitsConfigError:
                errors.append("a working exit of another symbol is malformed")
        return held, own

    def _check_reducing(
        self,
        intent: OrderIntent,
        symbol: str,
        state: OrderState,
        over: bool,
        errors: list[str],
        lenient: bool,
    ) -> Decision:
        """Exits and protective orders: never blocked by limits or the kill switch, but they
        must neither flip the position nor exceed it, and past the rate a stop replacement must
        strictly reduce risk."""
        exit_ = intent.kind is IntentKind.EXIT
        pendings: dict[str, PendingEntry] = {}
        if lenient:
            held, own = self._lenient_view(state, symbol, errors)
            everyone = own
            if errors:
                self.kill_switch.engage(KillLevel.STOP_ENTRIES, "malformed state: " + errors[0])
        else:
            positions, pendings, everyone = self._normalised(state)
            held = positions.get(symbol, 0.0)
            own = [w for w in everyone if w.symbol == symbol]
        err = tuple(errors)
        if exit_ and (intent.oca_group or intent.replaces_order or intent.parent_ref):
            return _refuse("an exit may not carry an OCA group, a replacement or a parent", over, err)
        if _bad_number(held):
            return _refuse("position state unreadable", over, err)
        net = held
        if not exit_:
            refusal = self._protective_refusal(intent, symbol, held, pendings, everyone)
            if refusal is not None:
                return _refuse(refusal, over, err)
            if intent.parent_ref:
                net = pendings[intent.parent_ref].quantity
        if net == 0:
            return _refuse("no open position to reduce: would open a position", over, err)
        reduces_with = "sell" if net > 0 else "buy"
        if intent.side != reduces_with:
            return _refuse("wrong direction: would add to or flip the position", over, err)
        in_flight = self._in_flight(intent, own)
        available = abs(net) - in_flight
        if intent.quantity > available:
            return _refuse(
                f"order of {intent.quantity:g} exceeds the position {abs(net):g} "
                f"minus {in_flight:g} in flight",
                over,
                err,
            )
        if not exit_ and intent.replaces_order:
            reducing = self._replacement_reduces_risk(intent, net)
            if reducing is None:
                return _refuse("replacement needs old_price and new_price", over, err)
            if over and not reducing:
                return _refuse(
                    "order rate exceeded and the replacement does not reduce risk", True, err
                )
        return Decision(True, "ok", over, err)

    @staticmethod
    def _protective_refusal(
        intent: OrderIntent,
        symbol: str,
        held: float,
        pendings: dict[str, PendingEntry],
        everyone: list[WorkingExit],
    ) -> str | None:
        """Why a protective intent is malformed for this state, or None."""
        if intent.protective_kind not in (ExitKind.STOP, ExitKind.TARGET):
            return "a protective order is a stop or a target"
        if intent.parent_ref:
            if held != 0:
                return (
                    "a bracket child is only sent with its entry: the symbol already holds "
                    "a position"
                )
            parent = pendings.get(intent.parent_ref)
            if parent is None or parent.symbol != symbol or _bad_number(parent.quantity):
                return "the named parent entry is not pending for this symbol"
            if parent.quantity == 0:
                return "the parent entry has nothing pending"
        if intent.oca_group and any(
            w.oca_group == intent.oca_group and w.symbol != symbol for w in everyone
        ):
            return "the OCA group belongs to another symbol"
        if intent.replaces_order:
            target = next((w for w in everyone if w.order_id == intent.replaces_order), None)
            if (
                target is None
                or target.symbol != symbol
                or target.kind not in (ExitKind.STOP, ExitKind.TARGET)
                or target.kind is not intent.protective_kind
                or target.side != intent.side
            ):
                return (
                    "replaces_order must name a working stop or target of this symbol, "
                    "same side and kind"
                )
        return None

    @staticmethod
    def _in_flight(intent: OrderIntent, own: list[WorkingExit]) -> float:
        """Reducing quantity already working for the symbol: plain orders in full, one OCA
        group once (its largest leg). Only a protective intent leaves out the order it
        replaces and its own OCA group."""
        protective = intent.kind is IntentKind.PROTECTIVE
        plain = 0.0
        groups: dict[str, float] = {}
        for w in own:
            if protective and intent.replaces_order and w.order_id == intent.replaces_order:
                continue
            if protective and intent.oca_group and w.oca_group == intent.oca_group:
                continue
            if w.oca_group:
                groups[w.oca_group] = max(groups.get(w.oca_group, 0.0), w.quantity)
            else:
                plain += w.quantity
        return plain + sum(groups.values())

    @staticmethod
    def _replacement_reduces_risk(intent: OrderIntent, net: float) -> bool | None:
        """Stops only: True when the stop does not move away from the price and covers the
        position; None when prices are missing; a target replacement never reduces risk."""
        old, new = intent.old_price, intent.new_price
        if old is None or new is None or _bad_number(old) or _bad_number(new):
            return None
        if old <= 0 or new <= 0:
            return None
        if intent.protective_kind is not ExitKind.STOP:
            return False
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
        positions, pendings, _exits = self._normalised(state)
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
