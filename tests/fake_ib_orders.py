"""A scripted exchange for single orders: ``FakeOrderIbClient``, a sibling of ``FakeIbClient``.

It subclasses ``tests.fake_ib.FakeIbClient`` and so has the same handshake, the same
threading rules (everything TWS says is delivered by ``run()``, on that thread, with
arguments deep-copied), the same 504 answer when not connected and the same
``EWrapper`` signature check for every callback it plays. ``tests/fake_ib.py`` itself is
untouched: ``FakeIbClient`` keeps having no order-capable method, and only this class
adds them. It never imports ``ibapi.connection`` and never opens a socket.

Order calls it implements (``ORDER_IMPLEMENTED``; signatures equal ``EClient``'s):
``placeOrder``, ``cancelOrder``, ``reqOpenOrders``, ``reqAllOpenOrders``,
``reqCompletedOrders``, ``reqExecutions``, ``reqPositions``, ``cancelPositions``.
Nothing else (no ``reqGlobalCancel``, no protobuf frames, no brackets, no OCA groups: #231).

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
cancelOrder answers PendingCancel, Cancelled, error 202 unverified
=====================================================  =================================

Known differences from real IB: the order of ``execDetails`` against ``orderStatus`` is
fixed here (execDetails first) and can be changed per order with ``FillPlan.transform``;
a modification of an open order is not modelled (a reused id is always 103); permIds,
execIds and times are made up (``execId`` ends in ``.01.<revision>``); the realised PnL of a
commission report is 0; positions use a simple average cost; ``reqExecutions`` filters on
symbol, side, account and client id only; whatIf does not consume the order id.

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
from ibapi.const import NO_VALID_ID
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
_ORDER_ID_504 = frozenset({"placeOrder", "cancelOrder"})  # client.py: these carry the order id

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
        self.perm_id = perm_id
        self.client_id = client_id
        self.status = "PreSubmitted"
        self.filled = Decimal(0)
        self.notional = 0.0
        self.last_price = 0.0
        self.fill_count = 0

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
        self._held: list[tuple[int, Played]] = []
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
        """Deliver everything queued, on the calling thread, and stay connected. Returns the count."""
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
    ) -> None:
        """The exchange fills (part of) a resting order."""
        with self._lock:
            rec = self._orders[order_id]
            played, held = self._fill(rec, _dec(shares), price, commission, commission_delay)
            if rec.remaining == 0:
                rec.status = "Filled"
        self._play(played, held)

    def exchange_cancel(self, order_id: int) -> None:
        """The exchange cancels a resting order (orderStatus Cancelled, no 202)."""
        with self._lock:
            rec = self._orders[order_id]
            rec.status = "Cancelled"
            played = [self._status_played(rec)]
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

    # -- the order surface ---------------------------------------------------
    @_recorded_order
    def placeOrder(self, orderId: int, contract: Contract, order: Order):
        with self._lock:
            if order.whatIf:
                self.whatif_requests.append(orderId)
                self._play([self._whatif_played(orderId, contract, order)], [])
                return
            if orderId in self._used_ids:
                self._error(orderId, DUPLICATE_ORDER_ID, _DUPLICATE_TEXT)
                return
            self._used_ids.add(orderId)
            self._next_id = max(self._next_id, orderId + 1)
            code = self._errors["place"].pop(0) if self._errors["place"] else None
            rec = _OrderRec(
                orderId, copy.deepcopy(contract), copy.deepcopy(order), self._perm(), self.clientId or 0
            )
            if code is not None and code not in WARNING_CODES:
                self._orders[orderId] = rec
                self._reject(rec, code)
                return
            self._orders[orderId] = rec
            if code is not None:
                self._error(orderId, code, REJECT_TEXTS[code].format(id=orderId))
            plan = self._plans.pop(0) if self._plans else self._default_plan(order)
            played, held = self._lifecycle(rec, plan)
        self._play(played, held)

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
        return ("openOrder", rec.order_id, rec.contract, rec.order, self._state(rec))

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
        return ("openOrder", order_id, copy.deepcopy(contract), copy.deepcopy(order), state)

    def _reject(self, rec: _OrderRec, code: int) -> None:
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
        self._play(played, [])

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
        self._apply_position(ex.acctNumber, rec.contract, shares if side == "BOT" else -shares, price)
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
        item: Played = ("commissionAndFeesReport", report)
        if delay > 0:
            held.append((self._tick + delay, item))
        else:
            played.append(item)

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
