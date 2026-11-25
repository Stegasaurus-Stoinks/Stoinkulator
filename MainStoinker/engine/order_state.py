"""Order state machine: transport events, one order's lifecycle, the `orderRef` codec, brackets.

Pure logic with no broker, socket or `ibapi` import. A transport turns what the broker
says into the event dataclasses below and feeds them in; nothing here sends anything.

Rules (decisions issue 230, task 226):

* Quantities come only from executions keyed by `exec_id`. A status never changes a
  quantity: it decides only "working" or "terminal". A `Filled` status without the
  executions to back it is a hint (`fill_unconfirmed`), not a fill.
* The state of an order is a pure function of the set of events it has seen, so a
  duplicate or out-of-order delivery changes nothing. The one order-dependent input is
  which terminal status came first when two contradictory ones arrive: the first stays.
* An execution is a fact and is always counted, even on an order that is already terminal
  (a cancel that lost the race against a fill): it is reported as `LATE` and the state only
  changes if it completes the order, which makes it FILLED.
* An overfill (more executed than ordered, or both exits of a bracket filled) is fatal:
  `fatal` is set and never cleared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# --------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Ack:
    """The broker has seen the order (an open-order callback)."""

    order_id: int


@dataclass(frozen=True)
class Status:
    """An order status line. Decides working or terminal only, never a quantity."""

    order_id: int
    status: str
    filled: float = 0.0  # what the broker says; informational, never used for quantities


@dataclass(frozen=True)
class Execution:
    order_id: int
    exec_id: str
    shares: int
    price: float
    time: str = ""


@dataclass(frozen=True)
class Commission:
    exec_id: str
    amount: float


@dataclass(frozen=True)
class Rejected:
    order_id: int
    code: int = 0
    message: str = ""


@dataclass(frozen=True)
class ConnectionState:
    state: str  # for example "ready", "lost"


Event = Ack | Status | Execution | Commission | Rejected | ConnectionState


class Applied(Enum):
    APPLIED = "applied"  # changed what we know
    DUPLICATE = "duplicate"  # already known, nothing changed
    LATE = "late"  # recorded, but the order was already terminal
    IGNORED = "ignored"  # not for this order, or not understood


# --------------------------------------------------------------------------- lifecycle


class OrderState(Enum):
    INTENT = "intent"
    PENDING_SUBMIT = "pending_submit"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INACTIVE = "inactive"


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.INACTIVE})

_WORKING_STATUSES = frozenset({"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"})
_CANCELLED_STATUSES = frozenset({"Cancelled", "ApiCancelled"})


class OrderLifecycle:
    """One order: intent, pending submit, working, partially filled, then exactly one terminal state."""

    def __init__(self, order_ref: str, side: str, quantity: int, order_id: int | None = None) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.order_ref = order_ref
        self.side = side
        self.quantity = quantity
        self.order_id = order_id
        self.executions: dict[str, Execution] = {}
        self.commissions: dict[str, float] = {}
        self.log: list[tuple[Event, Applied]] = []
        self.anomalies: list[str] = []
        self.status_fill_seen = False
        self._submitted = False
        self._working_seen = False
        self._terminal: OrderState | None = None  # first terminal status; never replaced

    # -- inputs

    def submit(self, order_id: int | None = None) -> None:
        """The intent has been handed to the broker (INTENT to PENDING_SUBMIT). Idempotent."""
        if order_id is not None:
            self.order_id = order_id
        self._submitted = True

    def resize(self, quantity: int) -> None:
        """Change the ordered quantity (protection sized to a partial fill)."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.quantity = quantity

    def apply(self, event: Event) -> Applied:
        outcome = self._apply(event)
        self.log.append((event, outcome))
        return outcome

    def _apply(self, event: Event) -> Applied:
        if isinstance(event, Execution):
            return self._execution(event)
        if isinstance(event, Commission):
            return self._commission(event)
        if isinstance(event, ConnectionState):
            return Applied.IGNORED
        if event.order_id != self.order_id:
            return Applied.IGNORED
        was_terminal = self.is_terminal
        if isinstance(event, Ack):
            return self._working(was_terminal)
        if isinstance(event, Rejected):
            return self._terminal_status(OrderState.REJECTED, was_terminal)
        return self._status(event, was_terminal)

    def _working(self, was_terminal: bool) -> Applied:
        if was_terminal:
            return Applied.LATE
        if self._working_seen:
            return Applied.DUPLICATE
        self._working_seen = True
        return Applied.APPLIED

    def _terminal_status(self, state: OrderState, was_terminal: bool) -> Applied:
        if self._terminal is not None:
            return Applied.DUPLICATE if self._terminal is state else Applied.LATE
        self._terminal = state
        return Applied.LATE if was_terminal else Applied.APPLIED

    def _status(self, event: Status, was_terminal: bool) -> Applied:
        name = event.status
        if name in _WORKING_STATUSES:
            return self._working(was_terminal)
        if name in _CANCELLED_STATUSES:
            return self._terminal_status(OrderState.CANCELLED, was_terminal)
        if name == "Inactive":
            return self._terminal_status(OrderState.INACTIVE, was_terminal)
        if name == "Filled":
            if self.status_fill_seen:
                return Applied.DUPLICATE
            self.status_fill_seen = True  # a hint only; executions decide
            return Applied.LATE if was_terminal else Applied.APPLIED
        self.anomalies.append(f"unknown status {name!r}")
        return Applied.IGNORED

    def _execution(self, event: Execution) -> Applied:
        if event.shares <= 0:
            self.anomalies.append(f"execution {event.exec_id} with non-positive shares")
            return Applied.IGNORED
        if event.order_id != self.order_id:
            return Applied.IGNORED
        known = self.executions.get(event.exec_id)
        if known is not None:
            if known != event:
                self.anomalies.append(f"execution {event.exec_id} repeated with different content")
            return Applied.DUPLICATE
        was_terminal = self.is_terminal
        self.executions[event.exec_id] = event
        return Applied.LATE if was_terminal else Applied.APPLIED

    def _commission(self, event: Commission) -> Applied:
        known = self.commissions.get(event.exec_id)
        if known is not None:
            if known != event.amount:
                self.anomalies.append(f"commission {event.exec_id} repeated with a different amount")
            return Applied.DUPLICATE
        self.commissions[event.exec_id] = event.amount  # may precede its execution
        return Applied.APPLIED

    # -- derived

    @property
    def filled_qty(self) -> int:
        return sum(e.shares for e in self.executions.values())

    @property
    def avg_price(self) -> float | None:
        qty = self.filled_qty
        if qty == 0:
            return None
        return sum(e.shares * e.price for e in self.executions.values()) / qty

    @property
    def commission(self) -> float:
        return sum(a for x, a in self.commissions.items() if x in self.executions)

    @property
    def overfilled(self) -> bool:
        return self.filled_qty > self.quantity

    @property
    def fill_unconfirmed(self) -> bool:
        """A Filled status was seen but the executions do not (yet) add up to the quantity."""
        return self.status_fill_seen and self.filled_qty < self.quantity

    @property
    def state(self) -> OrderState:
        filled = self.filled_qty
        if filled >= self.quantity:
            return OrderState.FILLED
        if self._terminal is not None:
            return self._terminal
        if filled > 0:
            return OrderState.PARTIALLY_FILLED
        if self._working_seen:
            return OrderState.WORKING
        if self._submitted:
            return OrderState.PENDING_SUBMIT
        return OrderState.INTENT

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


# --------------------------------------------------------------------------- orderRef codec

ORDER_REF_PREFIX = "stk1"
ORDER_REF_MAX_LEN = 32
ROLES = ("E", "S", "T", "X")  # entry, stop, target, exit
_FIELD = re.compile(r"[A-Za-z0-9_.\-]+")
_PAPER_ID = re.compile(r"D[UFI][A-Z]?[0-9]+")


@dataclass(frozen=True)
class OrderRef:
    run: str
    algo: str
    ref: str
    role: str


def _check_field(name: str, value: str) -> None:
    if not _FIELD.fullmatch(value):
        raise ValueError(f"orderRef {name} {value!r} must be non-empty and use letters, digits, _ . -")
    if _PAPER_ID.search(value):
        raise ValueError(f"orderRef {name} looks like an account id")


def encode_order_ref(run: str, algo: str, ref: str, role: str) -> str:
    """`stk1|<run>|<algo>|<ref>|<role>`, at most 32 characters, never an account id."""
    for name, value in (("run", run), ("algo", algo), ("ref", ref)):
        _check_field(name, value)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, not {role!r}")
    text = "|".join((ORDER_REF_PREFIX, run, algo, ref, role))
    if len(text) > ORDER_REF_MAX_LEN:
        raise ValueError(f"orderRef {text!r} is {len(text)} characters; the limit is {ORDER_REF_MAX_LEN}")
    return text


def decode_order_ref(text: str) -> OrderRef:
    """Inverse of `encode_order_ref`; `ValueError` for anything else (a hand-placed order)."""
    parts = text.split("|")
    if len(parts) != 5 or parts[0] != ORDER_REF_PREFIX or len(text) > ORDER_REF_MAX_LEN:
        raise ValueError(f"not an orderRef of this system: {text!r}")
    _, run, algo, ref, role = parts
    for name, value in (("run", run), ("algo", algo), ("ref", ref)):
        _check_field(name, value)
    if role not in ROLES:
        raise ValueError(f"not an orderRef of this system: {text!r}")
    return OrderRef(run, algo, ref, role)


# --------------------------------------------------------------------------- bracket

ENTRY, STOP, TARGET = "E", "S", "T"
_EXITS = (STOP, TARGET)


class BracketState:
    """Entry, stop and target as one unit (market entry, GTC stop, GTC limit target)."""

    def __init__(self, run: str, algo: str, ref: str, side: str, quantity: int,
                 has_target: bool = True) -> None:
        self.run, self.algo, self.ref = run, algo, ref
        exit_side = "SELL" if side == "BUY" else "BUY"
        self.legs: dict[str, OrderLifecycle] = {
            ENTRY: OrderLifecycle(encode_order_ref(run, algo, ref, ENTRY), side, quantity),
            STOP: OrderLifecycle(encode_order_ref(run, algo, ref, STOP), exit_side, quantity),
        }
        if has_target:
            self.legs[TARGET] = OrderLifecycle(
                encode_order_ref(run, algo, ref, TARGET), exit_side, quantity)
        self.connection = ""
        self.fatal: str | None = None
        self._ids: dict[int, str] = {}
        self._orphans: dict[str, Commission] = {}
        self._cancel_sent: set[str] = set()
        self._exit_role: str | None = None  # the exit leg that executed first

    def bind(self, role: str, order_id: int) -> None:
        """Record the broker order id of a leg (and mark it submitted)."""
        leg = self.legs[role]
        self._ids = {i: r for i, r in self._ids.items() if r != role}
        self._ids[order_id] = role
        leg.submit(order_id)

    def apply(self, event: Event) -> Applied:
        if isinstance(event, ConnectionState):
            self.connection = event.state
            return Applied.APPLIED
        if isinstance(event, Commission):
            return self._commission(event)
        role = self._ids.get(event.order_id)
        if role is None:
            return Applied.IGNORED
        outcome = self.legs[role].apply(event)
        if isinstance(event, Execution):
            if role in _EXITS and self._exit_role is None and outcome is not Applied.DUPLICATE:
                self._exit_role = role
            orphan = self._orphans.pop(event.exec_id, None)
            if orphan is not None:
                self.legs[role].apply(orphan)
        self._check_fatal()
        return outcome

    def _commission(self, event: Commission) -> Applied:
        for leg in self.legs.values():
            if event.exec_id in leg.executions:
                return leg.apply(event)
        if event.exec_id in self._orphans:
            return Applied.DUPLICATE
        self._orphans[event.exec_id] = event
        return Applied.APPLIED

    def _check_fatal(self) -> None:
        if self.fatal is not None:
            return
        for role, leg in self.legs.items():
            if leg.overfilled:
                self.fatal = f"overfill: leg {role} executed {leg.filled_qty} of {leg.quantity}"
                return
        exit_qty = sum(self.legs[r].filled_qty for r in _EXITS if r in self.legs)
        if exit_qty > self.entry_filled_qty:
            self.fatal = f"overfill: exits executed {exit_qty} against an entry of {self.entry_filled_qty}"

    # -- derived

    @property
    def entry(self) -> OrderLifecycle:
        return self.legs[ENTRY]

    @property
    def entry_filled_qty(self) -> int:
        return self.entry.filled_qty

    @property
    def exit_filled_qty(self) -> int:
        return sum(self.legs[r].filled_qty for r in _EXITS if r in self.legs)

    @property
    def position_qty(self) -> int:
        """Shares held in the entry's direction (never negative)."""
        return max(self.entry_filled_qty - self.exit_filled_qty, 0)

    @property
    def desired_protection_qty(self) -> int:
        """Protection covers exactly what the entry has filled so far."""
        return self.entry_filled_qty

    @property
    def terminal_exit(self) -> str | None:
        """The one exit leg that ended the position, `S` or `T`, once it is completely filled."""
        if self._exit_role is not None and self.legs[self._exit_role].state is OrderState.FILLED:
            return self._exit_role
        return None

    @property
    def unprotected(self) -> bool:
        """A position is open but the stop is gone."""
        return self.position_qty > 0 and self.legs[STOP].is_terminal

    @property
    def is_closed(self) -> bool:
        return (self.entry.is_terminal and self.position_qty == 0
                and all(leg.is_terminal for leg in self.legs.values()))

    def _entry_dead_unfilled(self) -> bool:
        return self.entry.is_terminal and self.entry_filled_qty == 0

    def cancel_requests(self) -> list[str]:
        """Roles whose broker order should be cancelled now (each is listed until marked sent)."""
        wanted: list[str] = []
        if self._entry_dead_unfilled():  # rejected or cancelled parent: no children
            wanted = [r for r in _EXITS if r in self.legs]
        elif self._exit_role is not None:  # one exit has executed: the other one must go
            wanted = [r for r in _EXITS if r in self.legs and r != self._exit_role]
        elif self.entry.is_terminal and self.position_qty == 0:
            wanted = [r for r in _EXITS if r in self.legs]
        return [r for r in wanted
                if not self.legs[r].is_terminal and r not in self._cancel_sent]

    def mark_cancel_sent(self, role: str) -> None:
        self._cancel_sent.add(role)

    def resize_requests(self) -> list[tuple[str, int]]:
        """(role, quantity) of live protective legs whose size differs from the filled quantity."""
        want = self.desired_protection_qty
        if want == 0 or self.fatal is not None:
            return []
        return [(r, want) for r in _EXITS
                if r in self.legs and not self.legs[r].is_terminal and self.legs[r].quantity != want
                and r not in self._cancel_sent and self._exit_role is None]

    def mark_resized(self, role: str, quantity: int) -> None:
        self.legs[role].resize(quantity)
