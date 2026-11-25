"""Order state machine: transport events, one order's lifecycle, the `orderRef` codec, brackets.

Pure logic with no broker, socket or `ibapi` import. A transport turns what the broker
says into the event dataclasses below and feeds them in; nothing here sends anything.

GUIDING PRINCIPLE: where event order cannot be known, the state takes the conservative reading
(the position is treated as less protected, never more), flags it as an anomaly, and only an
explicit reconciliation call clears it: `BracketState.confirm_live(role, quantity)`, fed by the
caller from an open-orders snapshot (or a terminal status for the leg).

Rules (decisions issue 230, task 226, planner rulings on the PR 261 reviews):

* Quantities come only from executions keyed by `exec_id`. A status never changes a
  quantity: it decides only working or terminal. A `Filled` status without the executions
  to back it is a hint (`fill_unconfirmed`), not a fill.
* Everything is derived from the set of events seen, so duplicates and reordering change
  nothing; no input is arrival-ordered. Statuses carry no sequence number, so (N4):
  once `Inactive` has been seen the leg counts as not protecting whatever came before or after
  (anomaly `inactive_seen`); when statuses or acks report different quantities the smallest
  counts (anomaly `quantity_conflict`). Both last until `confirm_live` or a terminal status.
  A legitimate resize upwards therefore raises `quantity_conflict` until the caller confirms.
* Terminal statuses: Filled (by executions) > Rejected > Cancelled; a contradictory pair is
  kept deterministic and recorded as a `status_conflict` anomaly. Inactive is NOT final.
* The exec-id correction format (`X.NN`, highest suffix wins) is an ASSUMPTION to check against
  the first recordings (issue 253): if separate partial fills of one order differ only in the
  last segment they would be collapsed and shares under-counted.
* The caller owns the deadline of an unconfirmed request: `pending_cancels` / `pending_resizes`
  say a request is not yet confirmed, with no clock here (issue 238).
* An execution is a fact and is counted even on an order that is already terminal (a cancel
  that lost a race), and then reported as `LATE`. Executions are validated (positive whole
  shares, finite positive price, matching symbol and side when both are known); anything
  else is not counted and is recorded as an anomaly. An execId `X.NN` replaces an earlier
  execution with the base `X` (highest suffix wins, whatever the arrival order). Amounts are
  floats here; the store converts to integer `e4` values at its boundary.
* B1. A fatal flag latches only when an order's executions exceed the largest quantity it was
  ever ordered for, or exit executions exceed the entry's ORDERED quantity. Exits ahead of the
  entry's reported fills but within the ordered quantity are `exit_ahead_of_entry`, derived
  and not latched; it clears when the entry's executions arrive. Once the entry is terminal the
  same excess is `short_by` (N2): the account is short, `exits_exceed_entry` is raised and
  `is_closed` stays False (not latched: a late entry execution can still clear it).
* B2. Protection follows the position held (entry filled minus exits executed). Every live
  protective leg should have a remaining quantity of `held` (resize request). The other exit
  is cancelled only when held is 0 and the entry can fill no more, or when an exit has
  completely filled for the held quantity.
* B3/N3. `unprotected` is derived: held > 0 and the stop that certainly covers (live, not
  Inactive, no cancel sent, not uncertain) has less remaining than held. `needs_protection`
  (the quantity held; new stop or flatten is the caller's choice) is non-zero only when no
  stop could still fill. While an old stop is cancel-pending, Inactive or uncertain,
  `unprotected` and `awaiting_stop_resolution` are True and `needs_protection` is 0.
* S2/N1. Requests are a pure function of state: `pending_cancels` and `pending_resizes` stay set
  until the broker confirms (a status for a cancel, a status or ack reporting the new quantity
  for a resize). `cancel_requests()` / `resize_requests()` are those minus what the caller has
  marked as sent. A `Rejected` for a stop or target with a request outstanding makes the leg
  `uncertain` (request refused or order rejected: error codes are not interpreted here): it
  counts as not protecting, the request goes back to pending, anomaly `request_or_leg_rejected`.
  A resize means: new TOTAL quantity of the leg, so its remainder equals `held`.
* `orderRef` uniqueness across databases and resets is the caller's job (the shared sequence
  of issue 227); this module only checks the shape.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

# --------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Ack:
    """The broker has seen the order (an open-order callback)."""

    order_id: int
    quantity: int | None = None  # total quantity the broker holds, when the callback says


@dataclass(frozen=True)
class Status:
    """An order status line. Decides working or terminal only, never a quantity."""

    order_id: int
    status: str
    filled: float = 0.0  # what the broker says; informational, never used for quantities
    quantity: int | None = None  # total quantity, when known (confirms a resize)


@dataclass(frozen=True)
class Execution:
    order_id: int
    exec_id: str
    shares: int | float | Decimal
    price: float
    time: str = ""
    symbol: str | None = None
    side: str | None = None  # BUY or SELL (the transport maps the broker's words)


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
    INACTIVE = "inactive"  # not final: the broker may return it to Submitted


TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED})
LIVE_STATES = frozenset({OrderState.PENDING_SUBMIT, OrderState.WORKING, OrderState.PARTIALLY_FILLED})

_WORKING_STATUSES = frozenset({"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"})
_CANCELLED_STATUSES = frozenset({"Cancelled", "ApiCancelled"})
_SUFFIX = re.compile(r"^(?P<base>.*)\.(?P<n>\d+)$")


def _whole_shares(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float | Decimal):
        try:
            finite = math.isfinite(value) if isinstance(value, float) else value.is_finite()
            whole = finite and value == int(value)
        except (ValueError, OverflowError):
            return None
        return int(value) if whole and int(value) > 0 else None
    return None


def _good_price(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, OverflowError):
        return False


def _exec_key(exec_id: str) -> tuple[str, int]:
    match = _SUFFIX.match(exec_id)
    if match is None:
        return exec_id, -1
    return match["base"], int(match["n"])


@dataclass(frozen=True)
class OrderSnapshot:
    order_ref: str
    side: str
    quantity: int
    state: OrderState
    filled_qty: int
    avg_price: float | None
    commission: float
    fill_unconfirmed: bool
    anomalies: tuple[str, ...]


class OrderLifecycle:
    """One order: intent, pending submit, working, partially filled, then one terminal state."""

    def __init__(self, order_ref: str, side: str, quantity: int, order_id: int | None = None,
                 symbol: str | None = None) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.order_ref = order_ref
        self.side = side
        self.symbol = symbol
        self.order_id = order_id
        self._ordered = quantity
        self._reported: set[int] = set()  # quantities the broker has reported; the smallest counts
        self._quantities = {quantity}
        self._by_base: dict[str, tuple[int, Execution]] = {}
        self._exec_ids: set[str] = set()
        self._commissions: dict[str, float] = {}
        self._seen: set[Event] = set()
        self._log: list[tuple[Event, Applied]] = []
        self._anomalies: set[str] = set()
        self._status_fill_seen = False
        self._submitted = False
        self._working_seen = False
        self._inactive_ever = False  # sticky until confirm_live or a terminal status
        self._terminals: set[OrderState] = set()

    # -- inputs

    def submit(self, order_id: int | None = None) -> None:
        """The intent has been handed to the broker (INTENT to PENDING_SUBMIT). Idempotent."""
        if order_id is not None:
            self.order_id = order_id
        self._submitted = True

    def apply(self, event: Event) -> Applied:
        outcome = self._apply(event)
        self._log.append((event, outcome))
        return outcome

    def _apply(self, event: Event) -> Applied:
        if isinstance(event, ConnectionState):
            return Applied.IGNORED
        if isinstance(event, Commission):
            return self._commission(event)
        if event.order_id != self.order_id:
            return Applied.IGNORED
        was_terminal = self.is_terminal
        if isinstance(event, Execution):
            if event in self._seen:
                return Applied.DUPLICATE
            self._seen.add(event)
            return self._execution(event, was_terminal)
        before = (self._working_seen, self._inactive_ever, frozenset(self._reported),
                  frozenset(self._terminals), self._status_fill_seen)
        if isinstance(event, Rejected):
            self._terminals.add(OrderState.REJECTED)
        elif isinstance(event, Ack):
            self._working_seen = True
            self._confirm(event.quantity)
        elif self._status(event) is Applied.IGNORED:
            return Applied.IGNORED
        after = (self._working_seen, self._inactive_ever, frozenset(self._reported),
                 frozenset(self._terminals), self._status_fill_seen)
        if was_terminal:
            return Applied.LATE
        return Applied.APPLIED if after != before else Applied.DUPLICATE

    def _confirm(self, quantity: int | None) -> None:
        if quantity is not None and quantity > 0:
            self._reported.add(quantity)
            self._quantities.add(quantity)

    def confirm_live(self, quantity: int) -> None:
        """Reconciliation: an open-orders snapshot shows this order working with this total quantity.

        The only way (besides a terminal status) to clear `inactive_seen` and `quantity_conflict`.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.is_terminal:
            raise ValueError(f"order {self.order_ref} is {self.state.value}, not live")
        self._reported = {quantity}
        self._quantities.add(quantity)
        self._inactive_ever = False
        self._working_seen = True

    def _status(self, event: Status) -> Applied:
        name = event.status
        if name in _WORKING_STATUSES:
            self._working_seen = True
            self._confirm(event.quantity)
        elif name in _CANCELLED_STATUSES:
            self._terminals.add(OrderState.CANCELLED)
        elif name == "Inactive":
            self._inactive_ever = True
        elif name == "Filled":
            self._status_fill_seen = True  # a hint only; executions decide
        else:
            self._anomalies.add(f"unknown status {name!r}")
            return Applied.IGNORED
        return Applied.APPLIED

    def _execution(self, event: Execution, was_terminal: bool) -> Applied:
        shares = _whole_shares(event.shares)
        if shares is None or not _good_price(event.price):
            self._anomalies.add(
                f"bad execution {event.exec_id}: shares {event.shares!r}, price {event.price!r}")
            return Applied.IGNORED
        if event.symbol is not None and self.symbol is not None and event.symbol != self.symbol:
            self._anomalies.add(f"execution {event.exec_id} for symbol {event.symbol}, not {self.symbol}")
            return Applied.IGNORED
        if event.side is not None and event.side != self.side:
            self._anomalies.add(f"execution {event.exec_id} on side {event.side}, not {self.side}")
            return Applied.IGNORED
        clean = replace(event, shares=shares, price=float(event.price))
        self._exec_ids.add(event.exec_id)
        base, suffix = _exec_key(event.exec_id)
        current = self._by_base.get(base)
        if current is not None:
            cur_suffix, cur = current
            if (suffix, shares, clean.price) <= (cur_suffix, cur.shares, cur.price):
                if suffix == cur_suffix and cur != clean:
                    self._anomalies.add(f"execution {event.exec_id} repeated with different content")
                return Applied.DUPLICATE  # an older correction, or the same one again
            if suffix == cur_suffix:
                self._anomalies.add(f"execution {event.exec_id} repeated with different content")
        self._by_base[base] = (suffix, clean)
        return Applied.LATE if was_terminal else Applied.APPLIED

    def _commission(self, event: Commission) -> Applied:
        if isinstance(event.amount, bool) or not isinstance(event.amount, int | float) \
                or not math.isfinite(event.amount):
            self._anomalies.add(f"bad commission {event.exec_id}: {event.amount!r}")
            return Applied.IGNORED
        known = self._commissions.get(event.exec_id)
        if known is not None:
            if known != event.amount:
                self._anomalies.add(f"commission {event.exec_id} repeated with a different amount")
                self._commissions[event.exec_id] = max(known, event.amount)
            return Applied.DUPLICATE
        self._commissions[event.exec_id] = event.amount  # may precede its execution
        return Applied.APPLIED

    # -- derived

    def has_exec(self, exec_id: str) -> bool:
        """True if this order has seen the execution (even one a correction has replaced)."""
        return exec_id in self._exec_ids

    @property
    def quantity(self) -> int:
        """The smallest quantity the broker has reported (the conservative one), else as ordered."""
        return min(self._reported) if self._reported else self._ordered

    @property
    def max_quantity(self) -> int:
        """The largest quantity this order was ever ordered or confirmed for."""
        return max(self._quantities)

    @property
    def executions(self) -> Mapping[str, Execution]:
        """The counted executions (corrections applied), by exec id; read-only."""
        return MappingProxyType({e.exec_id: e for _, e in self._by_base.values()})

    @property
    def log(self) -> tuple[tuple[Event, Applied], ...]:
        return tuple(self._log)

    @property
    def filled_qty(self) -> int:
        return sum(int(e.shares) for _, e in self._by_base.values())

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled_qty, 0)

    @property
    def avg_price(self) -> float | None:
        qty = self.filled_qty
        if qty == 0:
            return None
        return sum(int(e.shares) * e.price for _, e in self._by_base.values()) / qty

    @property
    def commission(self) -> float:
        counted = {e.exec_id for _, e in self._by_base.values()}
        return sum(a for x, a in self._commissions.items() if x in counted)

    @property
    def overfilled(self) -> bool:
        return self.filled_qty > self.max_quantity

    @property
    def fill_unconfirmed(self) -> bool:
        """A Filled status was seen but the executions do not (yet) add up to the quantity."""
        return self._status_fill_seen and self.filled_qty < self.quantity

    @property
    def anomalies(self) -> tuple[str, ...]:
        found = set(self._anomalies)
        kinds = set(self._terminals)
        if self._inactive_ever:
            kinds.add(OrderState.INACTIVE)
        if len(kinds) > 1:
            found.add("status_conflict: " + ", ".join(sorted(k.value for k in kinds)))
        if self._inactive_ever and not self._terminals:
            found.add("inactive_seen")
        if len(self._reported) > 1:
            found.add("quantity_conflict: " + ", ".join(str(q) for q in sorted(self._reported)))
        return tuple(sorted(found))

    @property
    def state(self) -> OrderState:
        filled = self.filled_qty
        if filled >= self.quantity:
            return OrderState.FILLED
        if OrderState.REJECTED in self._terminals:
            return OrderState.REJECTED
        if OrderState.CANCELLED in self._terminals:
            return OrderState.CANCELLED
        if self._inactive_ever:
            return OrderState.INACTIVE
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

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    def snapshot(self) -> OrderSnapshot:
        return OrderSnapshot(self.order_ref, self.side, self.quantity, self.state, self.filled_qty,
                             self.avg_price, self.commission, self.fill_unconfirmed, self.anomalies)


# --------------------------------------------------------------------------- orderRef codec

ORDER_REF_PREFIX = "stk1"
ORDER_REF_MAX_LEN = 32
ROLES = ("E", "S", "T", "X")  # entry, stop, target, exit
_FIELD = re.compile(r"[A-Za-z0-9_.\-]+")
_ACCOUNT_SHAPE = re.compile(r"[A-Za-z]{1,3}[0-9]{5,}")  # the recorder scrubber's shape


@dataclass(frozen=True)
class OrderRef:
    run: str
    algo: str
    ref: str
    role: str


def _check_field(name: str, value: str) -> None:
    if not _FIELD.fullmatch(value):
        raise ValueError(f"orderRef {name} {value!r} must be non-empty and use letters, digits, _ . -")
    if _ACCOUNT_SHAPE.fullmatch(value):
        raise ValueError(f"orderRef {name} looks like an account id")


def encode_order_ref(run: str, algo: str, ref: str, role: str) -> str:
    """`stk1|<run>|<algo>|<ref>|<role>`, at most 32 characters, never an account id.

    Uniqueness (across databases and resets) is the caller's job (issue 227's shared sequence).
    """
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
_LEG_ORDER = (ENTRY, STOP, TARGET)


@dataclass(frozen=True)
class BracketSnapshot:
    legs: tuple[tuple[str, OrderSnapshot], ...]
    held: int
    fatal: str | None
    unprotected: bool
    needs_protection: int
    awaiting_stop_resolution: bool
    exit_ahead_of_entry: bool
    short_by: int
    exits_exceed_entry: bool  # alarm: the account is short (short_by > 0)
    uncertain: tuple[str, ...]
    pending_cancels: tuple[str, ...]
    pending_resizes: tuple[tuple[str, int], ...]
    terminal_exit: str | None
    is_closed: bool
    anomalies: tuple[str, ...]
    connection: str


class BracketState:
    """Entry, stop and target as one unit (market entry, GTC stop, GTC limit target)."""

    def __init__(self, run: str, algo: str, ref: str, side: str, quantity: int,
                 has_target: bool = True, symbol: str | None = None) -> None:
        self.run, self.algo, self.ref = run, algo, ref
        exit_side = "SELL" if side == "BUY" else "BUY"
        self._legs: dict[str, OrderLifecycle] = {
            ENTRY: OrderLifecycle(encode_order_ref(run, algo, ref, ENTRY), side, quantity,
                                  symbol=symbol),
            STOP: OrderLifecycle(encode_order_ref(run, algo, ref, STOP), exit_side, quantity,
                                 symbol=symbol),
        }
        if has_target:
            self._legs[TARGET] = OrderLifecycle(
                encode_order_ref(run, algo, ref, TARGET), exit_side, quantity, symbol=symbol)
        self.connection = ""
        self._ids: dict[int, str] = {}
        self._orphans: dict[str, Commission] = {}
        self._anomalies: set[str] = set()
        self._sent_cancel: set[str] = set()
        self._sent_resize: dict[str, tuple[int, int]] = {}  # role: (wanted, leg quantity when sent)
        self._uncertain: set[str] = set()
        self._reject_notes: dict[str, set[str]] = {}
        self._fatal_latched = False

    # -- wiring

    @property
    def legs(self) -> Mapping[str, OrderLifecycle]:
        return MappingProxyType(self._legs)

    def bind(self, role: str, order_id: int) -> None:
        """Record the broker order id of a leg (and mark it submitted)."""
        if role not in self._legs:
            raise ValueError(f"no leg {role!r} in this bracket")
        if order_id in self._ids:
            raise ValueError(f"order id {order_id} is already bound to leg {self._ids[order_id]}")
        if role in self._ids.values():
            raise ValueError(f"leg {role} is already bound")
        self._ids[order_id] = role
        self._legs[role].submit(order_id)

    def mark_cancel_sent(self, role: str) -> None:
        if role not in self._legs:
            raise ValueError(f"no leg {role!r} in this bracket")
        self._sent_cancel.add(role)

    def mark_resized(self, role: str, quantity: int) -> None:
        """Note that a modify to this total quantity was sent. It takes effect when confirmed."""
        if role not in _EXITS or role not in self._legs:
            raise ValueError(f"only a stop or target can be resized, not {role!r}")
        leg = self._legs[role]
        if quantity <= leg.filled_qty or quantity - leg.filled_qty > self.held:
            raise ValueError(
                f"resize of {role} to {quantity} leaves {quantity - leg.filled_qty} against {self.held} held")
        self._sent_resize[role] = (quantity, leg.quantity)

    def confirm_live(self, role: str, quantity: int) -> None:
        """Reconciliation from an open-orders snapshot: this stop or target is working with this total
        quantity. Clears `uncertain`, `inactive_seen` and `quantity_conflict` for the leg and puts any
        request that was marked as sent back to pending (the snapshot shows it did not take effect)."""
        if role not in _EXITS or role not in self._legs:
            raise ValueError(f"only a stop or target can be confirmed live, not {role!r}")
        leg = self._legs[role]
        if quantity <= leg.filled_qty:
            raise ValueError(f"quantity {quantity} does not exceed the {leg.filled_qty} already filled")
        leg.confirm_live(quantity)
        self._uncertain.discard(role)
        self._reject_notes.pop(role, None)
        self._sent_cancel.discard(role)
        self._sent_resize.pop(role, None)

    # -- events

    def apply(self, event: Event) -> Applied:
        if isinstance(event, ConnectionState):
            self.connection = event.state
            return Applied.APPLIED
        if isinstance(event, Commission):
            return self._commission(event)
        role = self._ids.get(event.order_id)
        if role is None:
            if isinstance(event, Execution):
                self._anomalies.add(f"execution {event.exec_id} for unknown order id {event.order_id}")
            return Applied.IGNORED
        leg = self._legs[role]
        if (isinstance(event, Rejected) and role in _EXITS
                and (role in self._sent_cancel or role in self._sent_resize)):
            # An error for a stop or target with a request outstanding: was the request refused or
            # the order rejected? Not decided here (error codes are mapped later). The leg is
            # uncertain, counts as not protecting and the request goes back to pending, whatever
            # else has arrived, until `confirm_live` or a terminal status.
            self._sent_cancel.discard(role)
            self._sent_resize.pop(role, None)
            self._uncertain.add(role)
            self._reject_notes.setdefault(role, set()).add(
                f"request_or_leg_rejected: leg {role} code {event.code}")
            return Applied.APPLIED
        outcome = leg.apply(event)
        if isinstance(event, Execution):
            orphan = self._orphans.pop(event.exec_id, None)
            if orphan is not None:
                leg.apply(orphan)
        if self._fatal_conditions():
            self._fatal_latched = True
        return outcome

    def _commission(self, event: Commission) -> Applied:
        for leg in self._legs.values():
            if leg.has_exec(event.exec_id):
                return leg.apply(event)
        if event.exec_id in self._orphans:
            return Applied.DUPLICATE
        self._orphans[event.exec_id] = event
        return Applied.APPLIED

    # -- derived

    @property
    def entry(self) -> OrderLifecycle:
        return self._legs[ENTRY]

    @property
    def entry_filled_qty(self) -> int:
        return self.entry.filled_qty

    @property
    def exit_filled_qty(self) -> int:
        return sum(self._legs[r].filled_qty for r in _EXITS if r in self._legs)

    @property
    def held(self) -> int:
        """Shares held in the entry's direction: entry filled minus exits executed (never negative)."""
        return max(self.entry_filled_qty - self.exit_filled_qty, 0)

    position_qty = held

    @property
    def exit_ahead_of_entry(self) -> bool:
        """Exits executed beyond the entry's reported fills while the entry is still live; clears
        when the entry's executions arrive."""
        return self.exit_filled_qty > self.entry_filled_qty and not self.entry.is_terminal

    @property
    def short_by(self) -> int:
        """Exits executed beyond the entry's fills once the entry can fill no more: the account is
        short by this much (within the ordered quantity; beyond it the flag is fatal). Not latched."""
        if not self.entry.is_terminal:
            return 0
        return max(self.exit_filled_qty - self.entry_filled_qty, 0)

    @property
    def uncertain(self) -> tuple[str, ...]:
        """Stop or target legs whose state is in doubt after a rejection with a request outstanding."""
        return tuple(r for r in _EXITS if r in self._uncertain and not self._legs[r].is_terminal)

    def _fatal_conditions(self) -> list[str]:
        found = [f"overfill: leg {r} executed {leg.filled_qty} of {leg.max_quantity}"
                 for r in _LEG_ORDER if r in self._legs and (leg := self._legs[r]).overfilled]
        if self.exit_filled_qty > self.entry.max_quantity:
            found.append(f"overfill: exits executed {self.exit_filled_qty} "
                         f"against an entry ordered {self.entry.max_quantity}")
        return found

    @property
    def fatal(self) -> str | None:
        found = self._fatal_conditions()
        if found:
            return "; ".join(found)
        return "overfill (latched; a correction has since cleared it)" if self._fatal_latched else None

    @property
    def terminal_exit(self) -> str | None:
        """The one exit leg that completely filled (`S` or `T`), else None."""
        done = [r for r in _EXITS if r in self._legs and self._legs[r].state is OrderState.FILLED]
        return done[0] if len(done) == 1 else None

    def _stop_protects(self) -> int:
        """Shares the stop certainly covers: nothing while it is cancel-pending, Inactive or uncertain."""
        stop = self._legs[STOP]
        if not stop.is_live or STOP in self._sent_cancel or STOP in self.uncertain:
            return 0
        return stop.remaining

    def _stop_may_fill(self) -> bool:
        """A stop that could still execute (live or Inactive), so a second stop would double up."""
        return self._legs[STOP].state in LIVE_STATES or self._legs[STOP].state is OrderState.INACTIVE

    @property
    def unprotected(self) -> bool:
        return self.held > 0 and self._stop_protects() < self.held

    @property
    def awaiting_stop_resolution(self) -> bool:
        """Held shares, and a stop that may still fill but certainly covers nothing (cancel pending,
        Inactive or uncertain): do not place another stop yet."""
        return self.held > 0 and self._stop_may_fill() and self._stop_protects() == 0

    @property
    def needs_protection(self) -> int:
        """Quantity to protect anew (new stop or flatten, the caller's choice). Non-zero only when no
        stop could still fill; while one might, see `awaiting_stop_resolution`."""
        if self.held > 0 and not self._stop_may_fill():
            return self.held
        return 0

    @property
    def is_closed(self) -> bool:
        return (self.entry.is_terminal and self.held == 0 and self.short_by == 0
                and all(leg.is_terminal for leg in self._legs.values()))

    @property
    def pending_cancels(self) -> tuple[str, ...]:
        """Legs that should be cancelled; each stays here until its status says it is terminal."""
        held, entry = self.held, self.entry
        wanted: list[str] = []
        if held == 0 and entry.is_terminal:
            wanted = list(_EXITS)
        elif (held == 0 and entry.filled_qty > 0 and not self.exit_ahead_of_entry
              and any(self._legs[r].state is OrderState.FILLED for r in _EXITS if r in self._legs)):
            wanted = [ENTRY, *_EXITS]
        return tuple(r for r in _LEG_ORDER if r in wanted and r in self._legs
                     and not self._legs[r].is_terminal)

    @property
    def pending_resizes(self) -> tuple[tuple[str, int], ...]:
        """(role, new total quantity) so that each live protective leg's remainder equals `held`."""
        if self.held == 0 or self.fatal is not None:
            return ()
        cancelling = set(self.pending_cancels) | self._sent_cancel
        found = []
        for role in _EXITS:
            leg = self._legs.get(role)
            if leg is None or not leg.is_live or role in cancelling:
                continue
            want = leg.filled_qty + self.held
            if leg.quantity != want:
                found.append((role, want))
        return tuple(found)

    def cancel_requests(self) -> list[str]:
        """`pending_cancels` minus those marked as sent."""
        return [r for r in self.pending_cancels if r not in self._sent_cancel]

    def resize_requests(self) -> list[tuple[str, int]]:
        """`pending_resizes` minus those marked as sent for the same quantity."""
        return [(r, q) for r, q in self.pending_resizes
                if self._sent_resize.get(r) != (q, self._legs[r].quantity)]

    @property
    def anomalies(self) -> tuple[str, ...]:
        found = set(self._anomalies)
        for role in self.uncertain:
            found.update(self._reject_notes.get(role, ()))
        for role in _LEG_ORDER:
            if role in self._legs:
                found.update(f"leg {role}: {a}" for a in self._legs[role].anomalies)
        return tuple(sorted(found))

    def snapshot(self) -> BracketSnapshot:
        return BracketSnapshot(
            tuple((r, self._legs[r].snapshot()) for r in _LEG_ORDER if r in self._legs),
            self.held, self.fatal, self.unprotected, self.needs_protection,
            self.awaiting_stop_resolution, self.exit_ahead_of_entry, self.short_by,
            self.short_by > 0, self.uncertain, self.pending_cancels, self.pending_resizes,
            self.terminal_exit, self.is_closed, self.anomalies, self.connection)
