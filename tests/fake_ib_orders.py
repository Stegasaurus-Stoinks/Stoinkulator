"""A scripted exchange for single orders: ``FakeOrderIbClient``, a sibling of ``FakeIbClient``.

It subclasses ``tests.fake_ib.FakeIbClient`` and so has the same handshake, the same
threading rules (everything TWS says is delivered by ``run()``, on that thread, with
arguments deep-copied), the same 504 answer when not connected and the same
``EWrapper`` signature check for every callback it plays. ``tests/fake_ib.py`` itself is
untouched: ``FakeIbClient`` keeps having no order-capable method, and only this class
adds them. Its own source imports neither ``ibapi.connection`` nor a socket module, and it
never opens a socket.

Order calls it implements (``ORDER_IMPLEMENTED``; signatures equal ``EClient``'s):
``placeOrder``, ``cancelOrder``, ``reqOpenOrders``, ``reqAllOpenOrders``,
``reqCompletedOrders``, ``reqExecutions``, ``reqPositions``, ``cancelPositions``.
Nothing else (no ``reqGlobalCancel``, no protobuf frames).

Brackets, OCA groups and outages (#231) work through ``placeOrder`` itself and a few scripting
calls. ``transmit=False`` orders are kept back, silently, until an order with ``transmit=True`` of
the same group arrives (IB: the last child). A child (``parentId`` set) is ``PreSubmitted`` and
*held* until its parent has a fill; a parent that ends ``Cancelled`` or ``Inactive`` with no fill
cancels its children; a parent cancelled after a partial fill leaves the children's size as
``child_size_on_partial_cancel`` says (``"keep"`` or ``"filled"``). Orders of one ``ocaGroup``:
the first fill cancels the working siblings. ``fill_race`` fills several orders before any
cancel arrives (stop and target both fill: an overfill). ``begin_outage()`` (error 1100) and
``end_outage(1101 | 1102)`` lose what the exchange says meanwhile (``lost_callbacks``); the
exchange state goes on, so ``reqOpenOrders``, ``reqExecutions`` and ``reqPositions`` re-sync it,
and ``reqExecutions`` replays every execution, those already seen included. Children never fill
by plan: they rest, and a test fills them with ``fill``.

Nothing sleeps and nothing reads a clock: time is ``clock_ms``, and "a delay" is a count
of ``tick()`` calls. ``drain()`` delivers what is queued on the calling thread without
ending the connection, so a test needs no thread; ``run_in_thread`` works as in the base.

Scenario table. Every row is "unverified" (from IB docs or the ibapi source, not shown
by a recording) until a replayed recording of an owner session shows it (#253, T27
updates this table). Status values: "observed in a recording" or "unverified".

=====================================================  =================================
Scenario                                                Status
=====================================================  =================================
MKT: PreSubmitted, Submitted, execDetails, Filled       unverified
Partial fills, one execId each, cumulative filled       unverified
Final status Cancelled after partial fills              unverified
Final status Inactive (rejected on arrival)             unverified
Resting LMT/STP order, filled or cancelled later        unverified
Reused order id gives error 103, no order created       unverified
Error 201, order rejected (orderStatus Inactive)        unverified
Error 202, order cancelled (orderStatus Cancelled)      unverified
Error 110, price not on the minimum variation           unverified
Error 321, read-only API refusal (text is a guess)      unverified
Error 10147, cancel of an unknown order id              unverified
Error 10148, cancel of an order in a state it can't     unverified
Error 104, modify of a filled order                     unverified
Error 105, modify that does not match the original      unverified
Error 161, cancel when not cancellable                  unverified
Error 399, order message (warning, order proceeds)      unverified
execDetails before commissionAndFeesReport              unverified
Commission delivered after a delay (ticks)              unverified
Commission missing for an execution                     unverified
Duplicate and out-of-order callbacks                    unverified
Execution correction: same order, new execId            unverified
reqOpenOrders (own client id only) + openOrderEnd       unverified
reqAllOpenOrders (all clients, TWS orders) + End        unverified
reqCompletedOrders + completedOrdersEnd                 unverified
reqExecutions + commission reports + execDetailsEnd    unverified
reqPositions + positionEnd                              unverified
whatIf: openOrder with orderState preview, no order     unverified
whatIf uses up its order id (a reused id is 103)        unverified
Unused id below the highest used id gives error 103     unverified
cancelOrder answers PendingCancel, Cancelled, error 202 unverified
openOrder/completedOrder Order carries orderId,         unverified
  clientId, permId (as the decoder sets them)
Late fill after a cancel (script flag only)             unverified
transmit=False held back silently until transmit=True   unverified
Children PreSubmitted until the parent has a fill       unverified
Parent Cancelled/Inactive/rejected cancels children     unverified
Children's size after a partial parent, rest cancelled  unverified (configurable)
OCA ocaType 1: any fill cancels working siblings        unverified
OCA ocaType 2/3: siblings shrink by the filled amount   unverified
OCA ocaType 0 or other with an ocaGroup: refused here   (the fake does not model it)
Bracket children implicitly OCA (flag, default off)     unverified
Children live at full size while parent part-filled     unverified (not configurable yet)
After a reconnect no orderStatus is pushed for open     unverified (safe direction)
  orders
Stop and target both fill before the cancel (overfill)  unverified
1100: callbacks lost; 1101, and 1102 by default, keep   unverified
  them lost; 1102 replays them only when scripted
Executions made during an outage replayed by reqExec    unverified
Correction changes filled quantity and position         unverified
Realised PnL UNSET on an opening fill                   unverified
=====================================================  =================================

Known differences from real IB: the order of ``execDetails`` against ``orderStatus`` is
fixed here (execDetails first) and can be changed per order with ``FillPlan.transform``;
a modification of an open order is not modelled (a reused id is always 103); permIds,
execIds and times are made up (``execId`` ends in ``.01.<revision>``); the realised PnL of a
commission report is UNSET_DOUBLE on an opening fill and a simple average-cost figure on a
closing one; positions use a simple average cost; ``reqExecutions`` filters on symbol, side,
account and client id only. Where IB's behaviour is unknown the fake takes the strict side: a
whatIf request uses up its id, and an unused id lower than the highest used one is 103.
``drain()`` and ``run_in_thread()`` are one or the other, never both.

The import test only reads this file's own source (no ``socket``, no ``ibapi.connection``);
the module's base, ``tests.fake_ib``, imports ``ibapi.client`` as the existing fake does,
which pulls ``ibapi.connection`` into ``sys.modules``. The behavioural guard is the test that
makes ``Connection`` raise and runs a whole scenario.

Accounts are synthetic (``DUX123456``); no real account id belongs in this file.
"""
import copy
import inspect
import queue
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ibapi.commission_and_fees_report import CommissionAndFeesReport
from ibapi.const import NO_VALID_ID, UNSET_DOUBLE
from ibapi.contract import Contract
from ibapi.errors import NOT_CONNECTED
from ibapi.execution import Execution, ExecutionFilter
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.order_state import OrderState

from tests import fake_ib
from tests.fake_ib import FakeIbClient, Played, Recorder

SYNTHETIC_ACCOUNT = "DUX123456"
ORDER_IMPLEMENTED = (
    "placeOrder",
    "cancelOrder",
    "reqOpenOrders",
    "reqAllOpenOrders",
    "reqCompletedOrders",
    "reqExecutions",
    "reqPositions",
    "cancelPositions",
)
# Not connected: ibapi 10.37.2 ``placeOrder`` answers error(orderId, ...); ``cancelOrder`` and the
# requests answer error(NO_VALID_ID, ...).
_ORDER_ID_504 = frozenset({"placeOrder"})

OCA_TYPES = frozenset({1, 2, 3})  # 1 cancel, 2 and 3 reduce (unverified; 0 is not modelled)
DUPLICATE_ORDER_ID = 103
WARNING_CODES = frozenset({399})  # delivered, and the order goes on

# IB error codes the fake can play, with the text IB documents. All "from IB docs, unverified"
# until a recording confirms them. ``{id}`` is the order id.
REJECT_TEXTS: dict[int, str] = {
    201: "Order rejected - reason:",
    202: "Order Canceled - reason:",
    110: "The price does not conform to the minimum price variation for this contract.",
    321: "Error validating request:-'bN' : cause - The API interface is currently in Read-Only mode.",
    10147: "OrderId {id} that needs to be cancelled is not found.",
    10148: "OrderId {id} that needs to be cancelled cannot be cancelled, state: Filled.",
    104: "Cannot modify a filled order.",
    105: "Order being modified does not match the original order. Unable to modify.",
    161: "Cancel attempted when order is not in a cancellable state. Order permId = {id}",
    399: "Order Message: warning, the order goes on",
}
REJECT_CODES = tuple(REJECT_TEXTS)
_DUPLICATE_TEXT = "Duplicate order id."

TERMINAL = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})
FINALS = frozenset({"Filled", "Cancelled", "Inactive", "Submitted"})
_EXEC_ID_REVISION = re.compile(r"^(?P<head>.*\.)(?P<rev>\d+)$")


@dataclass(frozen=True)
class FillPlan:
    """What the exchange does with the next order placed.

    ``fills``: ``(shares, price)`` per execution; ``None`` fills the whole quantity at the
    fake's ``market_price``. ``final``: ``"Filled"`` (the fills must add up to the quantity),
    ``"Submitted"`` (what is left rests), ``"Cancelled"`` (after the fills) or ``"Inactive"``
    (no fills). ``commission``: per execution, ``None`` means IB never sends one.
    ``commission_delay``: in ``tick()`` calls; 0 means right after the execution.
    ``transform`` gets the callbacks of the placement (not the delayed commissions) and
    returns what is played: reorder, drop or duplicate them there.
    """

    fills: tuple[tuple[int | str | Decimal, float], ...] | None = None
    final: str = "Filled"
    commission: float | None = 1.0
    commission_delay: int = 0
    presubmitted: bool = True
    transform: Callable[[list[Played]], list[Played]] | None = None


class _OrderRec:
    def __init__(self, order_id: int, contract: Contract, order: Order, perm_id: int, client_id: int):
        self.order_id = order_id
        self.contract = contract
        self.order = order
        # What TWS puts on the wire and the decoder sets on the Order it hands to openOrder
        # and completedOrder (orderdecoder.py): the ids, on the fake's own copy.
        order.orderId = order_id
        order.clientId = client_id
        order.permId = perm_id
        self.perm_id = perm_id
        self.client_id = client_id
        self.status = "PreSubmitted"
        self.filled = Decimal(0)
        self.notional = 0.0
        self.last_price = 0.0
        self.fill_count = 0
        self.orig_total = _dec(order.totalQuantity)
        self.held = False  # a child waiting for its parent's first fill
        self.sized = False  # the partial-parent size rule has been applied

    @property
    def total(self) -> Decimal:
        return _dec(self.order.totalQuantity)

    @property
    def remaining(self) -> Decimal:
        return self.total - self.filled

    @property
    def avg_price(self) -> float:
        return self.notional / float(self.filled) if self.filled else 0.0


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _recorded_order(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Record the call, answer 504 when not connected, else run it (the order twin of fake_ib's)."""
    sig = inspect.signature(fn)
    name = fn.__name__

    def wrapper(self: "FakeOrderIbClient", *a: Any, **k: Any) -> Any:
        bound = sig.bind(self, *a, **k)
        bound.apply_defaults()
        args = tuple(copy.deepcopy(v) for v in list(bound.args)[1:])
        self._record(fake_ib.Call(name, args))
        if not self._connected():
            req_id = args[0] if name in _ORDER_ID_504 else NO_VALID_ID
            self._deliver(
                "error", (req_id, self._clock_ms(), NOT_CONNECTED.code(), NOT_CONNECTED.msg())
            )
            return None
        return fn(self, *bound.args[1:], **bound.kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = fn.__qualname__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


class FakeOrderIbClient(FakeIbClient):
    """``FakeIbClient`` plus single-order scripting; see the module docstring."""

    def __init__(
        self,
        wrapper: Any,
        *,
        managed_accounts: str = SYNTHETIC_ACCOUNT,
        market_price: float = 100.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(wrapper, managed_accounts=managed_accounts, **kwargs)
        self.market_price = market_price
        self.whatif_values: dict[str, Any] = {
            "initMarginBefore": "0.0",
            "maintMarginBefore": "0.0",
            "equityWithLoanBefore": "100000.0",
            "initMarginChange": "1500.0",
            "maintMarginChange": "1200.0",
            "equityWithLoanChange": "-1.0",
            "initMarginAfter": "1500.0",
            "maintMarginAfter": "1200.0",
            "equityWithLoanAfter": "99999.0",
            "commissionAndFees": 1.0,
            "minCommissionAndFees": 1.0,
            "maxCommissionAndFees": 1.0,
            "commissionAndFeesCurrency": "USD",
        }
        self.whatif_requests: list[int] = []
        self._orders: dict[int, _OrderRec] = {}
        self._used_ids: set[int] = set()
        self._plans: list[FillPlan] = []
        self._errors: dict[str, list[int]] = {"place": [], "cancel": []}
        self._executions: list[tuple[Contract, Execution]] = []
        self._commissions: dict[str, CommissionAndFeesReport] = {}
        self._positions: dict[tuple[str, str], list[Any]] = {}
        self._realized: dict[str, float] = {}
        self._held: list[tuple[int, Played]] = []
        self._untransmitted: dict[int, tuple[Contract, Order]] = {}
        self.child_size_on_partial_cancel = "keep"  # or "filled" (unverified: IB's rule)
        # Unverified: do the children of one parent cancel each other without an ocaGroup?
        self.implicit_bracket_oca = False
        self.lost_callbacks: list[Played] = []  # what the exchange said while nobody listened
        self._outage = False
        self._outage_buffer: list[Played] = []
        self._tick = 0
        self._next_perm_id = 1_000_001
        self._next_exec = 1
        self._next_id = self.next_valid_id

    # -- scripting -----------------------------------------------------------
    def allocate_id(self) -> int:
        """The next order id a test can use (the seeded ``nextValidId``, then one more each call)."""
        with self._lock:
            order_id = self._next_id
            self._next_id += 1
            return order_id

    def next_plan(self, plan: FillPlan) -> None:
        """The next ``placeOrder`` follows ``plan`` (queued: one plan per order, in order)."""
        if plan.final not in FINALS:
            raise ValueError(f"final must be one of {sorted(FINALS)}, not {plan.final!r}")
        with self._lock:
            self._plans.append(plan)

    def script_error(self, on: str, code: int) -> None:
        """The next ``placeOrder`` (``on="place"``) or ``cancelOrder`` (``on="cancel"``) draws ``code``."""
        if on not in self._errors:
            raise ValueError(f"on must be 'place' or 'cancel', not {on!r}")
        if code not in REJECT_TEXTS:
            raise ValueError(f"no text for error {code}; known: {sorted(REJECT_TEXTS)}")
        with self._lock:
            self._errors[on].append(code)

    def seed_open_order(
        self,
        contract: Contract,
        order: Order,
        *,
        order_id: int,
        client_id: int = 0,
        status: str = "Submitted",
    ) -> None:
        """An order the API did not place (TWS by hand, client id 0, or another client).

        ``reqAllOpenOrders`` lists it; ``reqOpenOrders`` does not unless ``client_id`` is ours.
        """
        with self._lock:
            rec = _OrderRec(order_id, copy.deepcopy(contract), copy.deepcopy(order), self._perm(), client_id)
            rec.status = status
            self._orders[order_id] = rec
            self._used_ids.add(order_id)
            self._next_id = max(self._next_id, order_id + 1)

    def seed_position(self, contract: Contract, quantity: int | str | Decimal, avg_cost: float,
                      account: str | None = None) -> None:
        """A position that exists before the session (what ``reqPositions`` reports)."""
        with self._lock:
            acct = account or self._account()
            self._positions[(acct, contract.symbol)] = [copy.deepcopy(contract), _dec(quantity), avg_cost]

    def tick(self, n: int = 1) -> None:
        """Advance the delay clock; commissions that came due are queued for ``run``/``drain``."""
        with self._lock:
            self._tick += n
            due = [item for item in self._held if item[0] <= self._tick]
            self._held = [item for item in self._held if item[0] > self._tick]
        for _, played in due:
            self._push_played(played)

    def release_held(self) -> None:
        """Queue every delayed callback now."""
        with self._lock:
            due, self._held = self._held, []
        for _, played in due:
            self._push_played(played)

    def drain(self) -> int:
        """Deliver everything queued, on the calling thread, and stay connected. Returns the count.

        One or the other: ``drain()`` or ``run_in_thread()``, never both (two consumers of one
        queue); it raises while a thread started by ``run_in_thread`` is alive.
        """
        if any(t.is_alive() for t in self._threads):
            raise RuntimeError("drain() while a run_in_thread() thread is alive: two queue consumers")
        n = 0
        while True:
            try:
                name, args = self._q.get_nowait()
            except queue.Empty:
                return n
            if name == fake_ib._WAKE:
                continue
            self._deliver(name, args)
            n += 1

    def fill(
        self,
        order_id: int,
        shares: int | str | Decimal,
        price: float,
        *,
        commission: float | None = 1.0,
        commission_delay: int = 0,
        late_fill: bool = False,
    ) -> None:
        """The exchange fills (part of) a resting order.

        A terminal order (Filled, Cancelled, Inactive) is refused with ``ValueError`` unless
        ``late_fill=True``: the script says "a fill that crossed the cancel".
        """
        with self._lock:
            rec = self._orders[order_id]
            if rec.status in TERMINAL and not late_fill:
                raise ValueError(f"order {order_id} is {rec.status}: pass late_fill=True for a late fill")
            if rec.held:
                raise ValueError(f"order {order_id} is held until its parent {rec.order.parentId} fills")
            played, held = self._fill(rec, _dec(shares), price, commission, commission_delay)
            if rec.remaining == 0:
                rec.status = "Filled"
            played += self._after(rec)
        self._play(played, held)

    def fill_race(
        self,
        fills: Iterable[tuple[int, int | str | Decimal, float]],
        *,
        commission: float | None = 1.0,
    ) -> None:
        """Several resting orders fill before any cancel between them arrives.

        ``fills`` is ``(order_id, shares, price)`` each. For a stop and a target of one OCA group
        this is the overfill: both execute, the group cancels nothing that is already done, and
        the position ends past flat. (Unverified: how often IB lets it happen.)
        """
        with self._lock:
            recs: list[_OrderRec] = []
            played: list[Played] = []
            held: list[tuple[int, Played]] = []
            for order_id, shares, price in fills:
                rec = self._orders[order_id]
                if rec.status in TERMINAL or rec.held:
                    raise ValueError(f"order {order_id} is {rec.status}{' (held)' if rec.held else ''}")
                p, h = self._fill(rec, _dec(shares), price, commission, 0)
                played += p
                held += h
                recs.append(rec)
            for rec in recs:
                played += self._after(rec)
        self._play(played, held)

    def begin_outage(self) -> None:
        """Error 1100 now; from here the exchange's callbacks reach nobody (``lost_callbacks``)."""
        self.drop(1100)
        with self._lock:
            self._outage = True

    def end_outage(self, code: int = 1102, *, replay: bool = False) -> None:
        """Error 1101 (data lost) or 1102 (data maintained).

        What was lost stays lost unless ``replay=True`` is scripted with 1102 (unverified: that
        IB redelivers order callbacks after 1102 is not known; the default is the strict side).
        """
        if code not in (1101, 1102):
            raise ValueError(f"end_outage takes 1101 or 1102, not {code}")
        if replay and code != 1102:
            raise ValueError("replay needs 1102: 1101 means data lost")
        replay_flag = replay
        with self._lock:
            self._outage = False
            replay, self._outage_buffer = self._outage_buffer, []
        do_replay = replay_flag
        self.drop(code)
        if code == 1102 and do_replay:
            for item in replay:
                self.push(item[0], *item[1:])

    def exchange_cancel(self, order_id: int) -> None:
        """The exchange cancels a resting order (orderStatus Cancelled, no 202)."""
        with self._lock:
            rec = self._orders[order_id]
            rec.status = "Cancelled"
            played = [self._status_played(rec)]
            played += self._after(rec)
        self._play(played, [])

    def correct_execution(
        self,
        exec_id: str,
        *,
        price: float | None = None,
        shares: int | str | Decimal | None = None,
        commission: float | None = None,
        commission_delay: int = 0,
    ) -> str:
        """Play an execution correction: same order, new ``execId`` (revision + 1). Returns it."""
        with self._lock:
            contract, original = next((c, e) for c, e in self._executions if e.execId == exec_id)
            match = _EXEC_ID_REVISION.match(exec_id)
            assert match is not None, exec_id
            new = copy.deepcopy(original)
            new.execId = f"{match['head']}{int(match['rev']) + 1:02d}"
            if price is not None:
                new.price = price
            if shares is not None:
                new.shares = _dec(shares)
            self._executions.append((copy.deepcopy(contract), new))
            self._realized[new.execId] = self._realized.get(exec_id, UNSET_DOUBLE)
            rec = self._orders.get(original.orderId)
            if rec is not None:  # the correction replaces the original: filled quantity and position follow
                rec.filled += new.shares - original.shares
                rec.notional += float(new.shares) * new.price - float(original.shares) * original.price
                new.cumQty = rec.filled
                new.avgPrice = rec.avg_price
                sign = 1 if new.side == "BOT" else -1
                self._apply_position(
                    new.acctNumber, contract, sign * (new.shares - original.shares), new.price
                )
            played: list[Played] = [("execDetails", -1, contract, new)]
            held: list[tuple[int, Played]] = []
            amount = commission if commission is not None else (
                self._commissions[exec_id].commissionAndFees if exec_id in self._commissions else None
            )
            if amount is not None:
                self._add_commission(new.execId, amount, played, held, commission_delay)
        self._play(played, held)
        return new.execId

    # -- inspection ----------------------------------------------------------
    def order_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._orders)

    def status_of(self, order_id: int) -> str:
        with self._lock:
            return self._orders[order_id].status

    def untransmitted_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._untransmitted)

    # -- the order surface ---------------------------------------------------
    @_recorded_order
    def placeOrder(self, orderId: int, contract: Contract, order: Order):
        if order.ocaGroup and order.ocaType not in OCA_TYPES:
            raise ValueError(f"ocaType {order.ocaType} is not modelled: use one of {sorted(OCA_TYPES)}")
        with self._lock:
            # Strict side of two unknowns (unverified): an id that is used, or lower than the
            # highest id used, is 103; a whatIf request uses its id up like any other.
            if orderId in self._used_ids or (self._used_ids and orderId < max(self._used_ids)):
                self._error(orderId, DUPLICATE_ORDER_ID, _DUPLICATE_TEXT)
                return
            self._used_ids.add(orderId)
            self._next_id = max(self._next_id, orderId + 1)
            if order.whatIf:
                self.whatif_requests.append(orderId)
                self._play([self._whatif_played(orderId, contract, order)], [])
                return
            if not order.transmit:
                self._untransmitted[orderId] = (copy.deepcopy(contract), copy.deepcopy(order))
                return  # TWS has been told nothing yet (unverified: no callback at all)
            code = self._errors["place"].pop(0) if self._errors["place"] else None
            recs = self._take_group(orderId, contract, order)
            played: list[Played] = []
            held: list[tuple[int, Played]] = []
            for n, rec in enumerate(recs):
                self._orders[rec.order_id] = rec
                rec.held = bool(rec.order.parentId)
                if n == 0 and code is not None:
                    if code not in WARNING_CODES:
                        played += self._reject(rec, code)
                        continue
                    text = REJECT_TEXTS[code].format(id=rec.order_id)
                    played.append(self._error_played(rec.order_id, code, text))
                if rec.held:
                    continue
                plan = self._plans.pop(0) if self._plans else self._default_plan(rec.order)
                p, h = self._lifecycle(rec, plan)
                played += p
                held += h
            for rec in recs:
                if rec.held and rec.status == "PreSubmitted":
                    played += [self._open_played(rec), self._status_played(rec)]
            in_batch = {r.order_id for r in recs}
            parents = sorted({r.order.parentId for r in recs if r.order.parentId} - in_batch)
            for rec in [*recs, *(self._orders[pid] for pid in parents)]:
                played += self._after(rec)
        self._play(played, held)

    def _take_group(self, order_id: int, contract: Contract, order: Order) -> list[_OrderRec]:
        """The transmitted order and the held-back ones of its group, parents before children."""
        root = order.parentId or order_id
        items = [(oid, c, o) for oid, (c, o) in sorted(self._untransmitted.items())
                 if oid == root or o.parentId == root]
        for oid, _, _ in items:
            del self._untransmitted[oid]
        items.append((order_id, copy.deepcopy(contract), copy.deepcopy(order)))
        known = {oid for oid, _, _ in items} | set(self._orders)
        for _, _, o in items:
            if o.parentId and o.parentId not in known:
                raise ValueError(f"parentId {o.parentId} is not an order the fake has seen")
        items.sort(key=lambda t: (t[2].parentId != 0, t[0]))
        return [_OrderRec(oid, c, o, self._perm(), self.clientId or 0) for oid, c, o in items]

    @_recorded_order
    def cancelOrder(self, orderId: int, orderCancel: OrderCancel):
        with self._lock:
            code = self._errors["cancel"].pop(0) if self._errors["cancel"] else None
            if code is not None:
                self._error(orderId, code, REJECT_TEXTS[code].format(id=orderId))
                return
            rec = self._orders.get(orderId)
            if rec is None:
                self._error(orderId, 10147, REJECT_TEXTS[10147].format(id=orderId))
                return
            if rec.status in TERMINAL:
                self._error(orderId, 161, REJECT_TEXTS[161].format(id=rec.perm_id))
                return
            rec.status = "PendingCancel"
            played = [self._status_played(rec)]
            rec.status = "Cancelled"
            played.append(self._status_played(rec))
            played.append(self._error_played(orderId, 202, REJECT_TEXTS[202]))
            rec.held = False
            played += self._after(rec)
        self._play(played, [])

    @_recorded_order
    def reqOpenOrders(self):
        self._play(self._open_orders(own_only=True), [])

    @_recorded_order
    def reqAllOpenOrders(self):
        self._play(self._open_orders(own_only=False), [])

    @_recorded_order
    def reqCompletedOrders(self, apiOnly: bool):
        with self._lock:
            played: list[Played] = []
            for rec in sorted(self._orders.values(), key=lambda r: r.order_id):
                if rec.status not in TERMINAL or (apiOnly and rec.client_id == 0):
                    continue
                state = self._state(rec)
                state.completedStatus = rec.status
                played.append(("completedOrder", rec.contract, rec.order, state))
            played.append(("completedOrdersEnd",))
        self._play(played, [])

    @_recorded_order
    def reqExecutions(self, reqId: int, execFilter: ExecutionFilter):
        with self._lock:
            played: list[Played] = []
            sent: list[str] = []
            for contract, ex in self._executions:
                if execFilter.symbol and contract.symbol != execFilter.symbol:
                    continue
                if execFilter.side and ex.side != execFilter.side:
                    continue
                if execFilter.acctCode and ex.acctNumber != execFilter.acctCode:
                    continue
                if execFilter.clientId and ex.clientId != execFilter.clientId:
                    continue
                played.append(("execDetails", reqId, contract, ex))
                sent.append(ex.execId)
            for exec_id in sent:
                if exec_id in self._commissions:
                    played.append(("commissionAndFeesReport", self._commissions[exec_id]))
            played.append(("execDetailsEnd", reqId))
        self._play(played, [])

    @_recorded_order
    def reqPositions(self):
        with self._lock:
            played: list[Played] = [
                ("position", acct, contract, qty, avg)
                for (acct, _), (contract, qty, avg) in sorted(self._positions.items())
                if qty != 0
            ]
            played.append(("positionEnd",))
        self._play(played, [])

    @_recorded_order
    def cancelPositions(self):
        return None

    # -- internals -----------------------------------------------------------
    def _account(self) -> str:
        return self.managed_accounts.split(",")[0]

    def _perm(self) -> int:
        perm = self._next_perm_id
        self._next_perm_id += 1
        return perm

    def _default_plan(self, order: Order) -> FillPlan:
        if order.orderType == "MKT":
            return FillPlan()
        return FillPlan(fills=(), final="Submitted")

    def _error_played(self, req_id: int, code: int, text: str) -> Played:
        return ("error", req_id, self._clock_ms(), code, text, "")

    def _error(self, req_id: int, code: int, text: str) -> None:
        self._play([self._error_played(req_id, code, text)], [])

    def _push_played(self, played: Played) -> None:
        if played[0] == "commissionAndFeesReport":
            report = played[1]
            with self._lock:
                self._commissions[report.execId] = copy.deepcopy(report)
        with self._lock:
            if self._outage or not self._connected():  # nobody listens: the exchange goes on
                self.lost_callbacks.append(copy.deepcopy(played))
                if self._outage:
                    self._outage_buffer.append(copy.deepcopy(played))
                return
        self.push(played[0], *played[1:])

    def _play(self, played: Iterable[Played], held: list[tuple[int, Played]]) -> None:
        with self._lock:
            self._held.extend(held)
        for item in played:
            self._push_played(item)

    def _state(self, rec: _OrderRec) -> OrderState:
        state = OrderState()
        state.status = rec.status
        return state

    def _open_played(self, rec: _OrderRec) -> Played:
        # Copies: a later change of the record must not rewrite a callback already queued.
        return (
            "openOrder", rec.order_id, copy.deepcopy(rec.contract), copy.deepcopy(rec.order),
            self._state(rec),
        )

    def _status_played(self, rec: _OrderRec) -> Played:
        return (
            "orderStatus",
            rec.order_id,
            rec.status,
            rec.filled,
            rec.remaining,
            rec.avg_price,
            rec.perm_id,
            getattr(rec.order, "parentId", 0) or 0,
            rec.last_price,
            rec.client_id,
            "",
            0.0,
        )

    def _open_orders(self, *, own_only: bool) -> list[Played]:
        with self._lock:
            played: list[Played] = []
            for rec in sorted(self._orders.values(), key=lambda r: r.order_id):
                if rec.status in TERMINAL or (own_only and rec.client_id != (self.clientId or 0)):
                    continue
                played.append(self._open_played(rec))
                played.append(self._status_played(rec))
            played.append(("openOrderEnd",))
        return played

    def _whatif_played(self, order_id: int, contract: Contract, order: Order) -> Played:
        state = OrderState()
        state.status = "PreSubmitted"
        for key, value in self.whatif_values.items():
            setattr(state, key, value)
        shown = copy.deepcopy(order)
        shown.orderId = order_id
        shown.clientId = self.clientId or 0
        return ("openOrder", order_id, copy.deepcopy(contract), shown, state)

    def _reject(self, rec: _OrderRec, code: int) -> list[Played]:
        text = REJECT_TEXTS[code].format(id=rec.order_id)
        played: list[Played] = [self._error_played(rec.order_id, code, text)]
        if code == 201:
            rec.status = "Inactive"
            played.append(self._status_played(rec))
        elif code == 202:
            rec.status = "Cancelled"
            played.append(self._status_played(rec))
        else:
            del self._orders[rec.order_id]
            rec.status = "Inactive"  # gone; its children are cancelled all the same
        return played

    # -- brackets and OCA (everything here is unverified against real IB) ----
    def _after(self, rec: _OrderRec) -> list[Played]:
        """What follows a change of ``rec``: its children's fate, then its OCA siblings'."""
        return self._settle_children(rec) + self._oca(rec)

    def _settle_children(self, parent: _OrderRec) -> list[Played]:
        if self.child_size_on_partial_cancel not in ("keep", "filled"):
            raise ValueError("child_size_on_partial_cancel is 'keep' or 'filled'")
        played: list[Played] = []
        children = sorted(
            (c for c in self._orders.values() if c.order.parentId == parent.order_id),
            key=lambda c: c.order_id,
        )
        for child in children:
            if child.status in TERMINAL:
                continue
            if parent.filled > 0:
                if child.held:
                    child.held = False
                    child.status = "Submitted"
                    played += [self._open_played(child), self._status_played(child)]
                partial_cancel = parent.status == "Cancelled" and parent.remaining > 0
                if partial_cancel and not child.sized and self.child_size_on_partial_cancel == "filled":
                    child.sized = True
                    played += self._shrink(child, parent.filled)
            elif parent.status in TERMINAL:
                child.held = False
                child.status = "Cancelled"
                played.append(self._status_played(child))
        return played

    def _shrink(self, rec: _OrderRec, size: Decimal) -> list[Played]:
        """Resize a working order, never below what it has filled; nothing left ends it."""
        size = max(_dec(size), rec.filled)
        if size == rec.total and rec.remaining > 0:
            return []
        rec.order.totalQuantity = size
        rec.held = False
        if rec.remaining == 0:
            rec.status = "Filled" if rec.filled > 0 else "Cancelled"
        return [self._open_played(rec), self._status_played(rec)]

    def _oca_group(self, rec: _OrderRec) -> str:
        if rec.order.ocaGroup:
            return rec.order.ocaGroup
        if self.implicit_bracket_oca and rec.order.parentId:
            return f"implicit-parent-{rec.order.parentId}"
        return ""

    def _oca(self, rec: _OrderRec) -> list[Played]:
        group = self._oca_group(rec)
        if not group or rec.filled <= 0:
            return []
        kind = rec.order.ocaType if rec.order.ocaGroup else 1
        members = sorted(self._orders.values(), key=lambda r: r.order_id)
        played: list[Played] = []
        for other in members:
            if other is rec or self._oca_group(other) != group or other.status in TERMINAL:
                continue
            if kind == 1:
                other.held = False
                other.status = "Cancelled"
                played.append(self._status_played(other))
            else:  # 2 and 3: it shrinks by what the others filled (unverified)
                gone = sum((m.filled for m in members if m is not other and self._oca_group(m) == group),
                           Decimal(0))
                played += self._shrink(other, other.orig_total - gone)
        return played

    def _lifecycle(self, rec: _OrderRec, plan: FillPlan) -> tuple[list[Played], list[tuple[int, Played]]]:
        fills = plan.fills
        if fills is None:
            fills = ((rec.total, self.market_price),)
        total = sum((_dec(s) for s, _ in fills), Decimal(0))
        if plan.final == "Filled" and total != rec.total:
            raise ValueError(f"fills add up to {total}, the order is {rec.total}: 'Filled' needs them equal")
        if plan.final == "Inactive" and fills:
            raise ValueError("an Inactive order has no fills")
        if plan.final == "Cancelled" and total >= rec.total:
            raise ValueError("a Cancelled order must have an unfilled rest")
        if total > rec.total:
            raise ValueError(f"fills add up to {total}, more than the order's {rec.total}")
        played: list[Played] = []
        held: list[tuple[int, Played]] = []
        if plan.final == "Inactive":
            rec.status = "Inactive"
            played += [self._open_played(rec), self._status_played(rec)]
            return self._transform(plan, played), held
        if plan.presubmitted:
            played += [self._open_played(rec), self._status_played(rec)]
        rec.status = "Submitted"
        played += [self._open_played(rec), self._status_played(rec)]
        for shares, price in fills:
            p, h = self._fill(rec, _dec(shares), price, plan.commission, plan.commission_delay)
            played += p
            held += h
        if plan.final == "Filled":
            rec.status = "Filled"
        elif plan.final == "Cancelled":
            rec.status = "Cancelled"
            played.append(self._status_played(rec))
        return self._transform(plan, played), held

    @staticmethod
    def _transform(plan: FillPlan, played: list[Played]) -> list[Played]:
        return plan.transform(played) if plan.transform else played

    def _fill(
        self, rec: _OrderRec, shares: Decimal, price: float, commission: float | None, delay: int
    ) -> tuple[list[Played], list[tuple[int, Played]]]:
        if shares <= 0 or shares > rec.remaining:
            raise ValueError(f"cannot fill {shares} of order {rec.order_id}: {rec.remaining} remain")
        rec.filled += shares
        rec.notional += float(shares) * price
        rec.last_price = price
        rec.fill_count += 1
        side = "BOT" if rec.order.action.upper() == "BUY" else "SLD"
        ex = Execution()
        ex.execId = f"0000e0d5.{self._next_exec:08x}.01.01"
        self._next_exec += 1
        ex.time = self._exec_time()
        ex.acctNumber = rec.order.account or self._account()
        ex.exchange = "ISLAND"
        ex.side = side
        ex.shares = shares
        ex.price = price
        ex.permId = rec.perm_id
        ex.clientId = rec.client_id
        ex.orderId = rec.order_id
        ex.cumQty = rec.filled
        ex.avgPrice = rec.avg_price
        ex.orderRef = rec.order.orderRef
        self._executions.append((copy.deepcopy(rec.contract), ex))
        delta = shares if side == "BOT" else -shares
        self._realized[ex.execId] = self._realized_pnl(ex.acctNumber, rec.contract, delta, price)
        self._apply_position(ex.acctNumber, rec.contract, delta, price)
        if rec.remaining == 0:
            rec.status = "Filled"
        played: list[Played] = [("execDetails", -1, rec.contract, ex), self._status_played(rec)]
        held: list[tuple[int, Played]] = []
        if commission is not None:
            self._add_commission(ex.execId, commission, played, held, delay)
        return played, held

    def _add_commission(
        self, exec_id: str, amount: float, played: list[Played], held: list[tuple[int, Played]], delay: int
    ) -> None:
        report = CommissionAndFeesReport()
        report.execId = exec_id
        report.commissionAndFees = amount
        report.currency = "USD"
        report.realizedPNL = self._realized.get(exec_id, UNSET_DOUBLE)
        item: Played = ("commissionAndFeesReport", report)
        if delay > 0:
            held.append((self._tick + delay, item))
        else:
            played.append(item)

    def _realized_pnl(self, account: str, contract: Contract, delta: Decimal, price: float) -> float:
        """TWS sends UNSET_DOUBLE on an opening fill; a closing fill carries the realised PnL."""
        _, qty, avg = self._positions.get((account, contract.symbol), [contract, Decimal(0), 0.0])
        if qty == 0 or (qty > 0) == (delta > 0):
            return UNSET_DOUBLE
        closed = min(abs(delta), abs(qty))
        return float(closed) * (price - avg) * (1 if qty > 0 else -1)

    def _apply_position(self, account: str, contract: Contract, delta: Decimal, price: float) -> None:
        key = (account, contract.symbol)
        _, qty, avg = self._positions.get(key, [contract, Decimal(0), 0.0])
        new = qty + delta
        if new == 0:
            avg = 0.0
        elif qty == 0 or (qty > 0) != (new > 0):  # opened, or flipped
            avg = price
        elif abs(new) > abs(qty):  # added to it
            avg = (float(abs(qty)) * avg + float(abs(delta)) * price) / float(abs(new))
        self._positions[key] = [copy.deepcopy(contract), new, avg]

    def _exec_time(self) -> str:
        return time.strftime("%Y%m%d-%H:%M:%S", time.gmtime(self._clock_ms() / 1000))


class OrderRecorder(Recorder):
    """``Recorder`` plus the order callbacks the fake plays."""

    def openOrder(self, orderId, contract, order, orderState):
        self._put("openOrder", orderId, contract, order, orderState)

    def openOrderEnd(self):
        self._put("openOrderEnd")

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId,
                    lastFillPrice, clientId, whyHeld, mktCapPrice):
        self._put("orderStatus", orderId, status, filled, remaining, avgFillPrice, permId, parentId,
                  lastFillPrice, clientId, whyHeld, mktCapPrice)

    def execDetails(self, reqId, contract, execution):
        self._put("execDetails", reqId, contract, execution)

    def execDetailsEnd(self, reqId):
        self._put("execDetailsEnd", reqId)

    def commissionAndFeesReport(self, commissionAndFeesReport):
        self._put("commissionAndFeesReport", commissionAndFeesReport)

    def completedOrder(self, contract, order, orderState):
        self._put("completedOrder", contract, order, orderState)

    def completedOrdersEnd(self):
        self._put("completedOrdersEnd")

    def position(self, account, contract, position, avgCost):
        self._put("position", account, contract, position, avgCost)

    def positionEnd(self):
        self._put("positionEnd")
