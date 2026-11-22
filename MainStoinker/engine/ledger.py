"""Pure accounting for a trading run: orders, fills, positions, P&L, equity.

No I/O, no pandas, no module-level state. The ledger is told what happened by
a broker (through an `AccountRecorder` bound to one algo) and answers with
plain frozen records that the store and the reports consume. It never decides
anything: fills arrive with their price, commission and reason already set,
because the cost model lives in the broker.

Money and prices are `Decimal` quantized to 0.0001 with ROUND_HALF_EVEN, which
is exactly Python's `round(x, 4)` on the float. Capital is notional: nothing
here checks buying power.

Definitions, for one account (one algo):
- cash = capital + signed fill value - commissions paid;
- realised P&L = sum of net P&L of its closed round trips;
- unrealised P&L = open positions marked to the last close, minus the
  commission already paid on them;
- equity = cash + position value = capital + realised + unrealised.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

Side = Literal["buy", "sell"]
FillReason = Literal["entry", "stop", "target", "algo_exit", "end_of_data"]
OrderEventKind = Literal["placed", "modified", "cancelled"]

FILL_REASONS: tuple[str, ...] = ("entry", "stop", "target", "algo_exit", "end_of_data")
ZERO = Decimal("0.0000")
_QUANT = Decimal("0.0001")


def money(value: float | int | str | Decimal) -> Decimal:
    """Quantize a money amount or price to 0.0001 (equals `round(x, 4)` for floats)."""
    return Decimal(value).quantize(_QUANT, ROUND_HALF_EVEN)


def commission_per_share(quantity: float | Decimal, per_share: float | Decimal,
                         minimum: float | Decimal) -> Decimal:
    """Commission for one order: `per_share` x `quantity`, but at least `minimum`.

    A pure helper for brokers; the ledger itself only records the amount.
    """
    return max(money(Decimal(quantity) * Decimal(per_share)), money(minimum))


@dataclass(frozen=True)
class OrderEvent:
    """An order placed, modified or cancelled."""

    order_id: int
    account: str
    broker_ref: Hashable
    kind: OrderEventKind
    time: datetime
    side: Side | None = None
    quantity: Decimal | None = None
    order_type: str | None = None  # "market", "limit", "stop", "target", ...
    price: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    """One execution. `synthetic` marks a fill the ledger made up at end of data."""

    fill_id: int
    order_id: int | None
    account: str
    broker_ref: Hashable
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal
    time: datetime
    reason: FillReason
    synthetic: bool = False


@dataclass(frozen=True)
class RoundTrip:
    """A position from flat to flat. Prices are averages over the fills."""

    account: str
    broker_ref: Hashable
    symbol: str
    side: Literal["long", "short"]
    quantity: Decimal
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    gross: Decimal
    commission: Decimal
    net: Decimal
    duration: timedelta
    exit_reason: FillReason
    synthetic: bool


@dataclass(frozen=True)
class EquityPoint:
    """Equity at a mark. `account` is None for the whole-ledger total."""

    time: datetime
    account: str | None
    cash: Decimal
    realised: Decimal
    unrealised: Decimal
    equity: Decimal


@dataclass(frozen=True)
class OpenPosition:
    """Snapshot of an open position."""

    account: str
    broker_ref: Hashable
    symbol: str
    side: Literal["long", "short"]
    quantity: Decimal
    average_price: Decimal


@dataclass(frozen=True)
class AccountInfo:
    idname: str
    symbol: str
    capital: Decimal


class _Position:
    """Mutable working state of one open position (internal)."""

    def __init__(self, account: str, ref: Hashable, symbol: str, long: bool, time: datetime):
        self.account, self.ref, self.symbol, self.long = account, ref, symbol, long
        self.entry_time = time
        self.entry_qty = Decimal(0)
        self.entry_value = Decimal(0)
        self.exit_qty = Decimal(0)
        self.exit_value = Decimal(0)
        self.commission = Decimal(0)

    @property
    def open_qty(self) -> Decimal:
        return self.entry_qty - self.exit_qty

    @property
    def average_price(self) -> Decimal:
        return money(self.entry_value / self.entry_qty)

    def unrealised_gross(self, close: Decimal) -> Decimal:
        """Mark-to-market of the open quantity against its average entry cost."""
        open_qty = self.open_qty
        cost =self.entry_value / self.entry_qty * open_qty
        value = close * open_qty
        return money(value - cost if self.long else cost - value)


class _AccountState:
    def __init__(self, info: AccountInfo) -> None:
        self.info = info
        self.cash = info.capital
        self.realised = ZERO
        self.ignored_closes = 0
        self.positions: dict[Hashable, _Position] = {}


class AccountRecorder:
    """The ledger as seen by one algo's broker; carries the account identity."""

    def __init__(self, ledger: Ledger, idname: str) -> None:
        self._ledger = ledger
        self.idname = idname

    def order_placed(self, ref: Hashable, *, time: datetime, side: Side, quantity: float | Decimal,
                     order_type: str, price: float | Decimal | None = None) -> int:
        """Record a new order and return its run-wide order id."""
        return self._ledger._order_event(self.idname, ref, "placed", time, side=side,
                                         quantity=quantity, order_type=order_type, price=price)

    def order_modified(self, order_id: int, *, time: datetime, price: float | Decimal | None = None,
                       quantity: float | Decimal | None = None) -> None:
        self._ledger._order_event(self.idname, None, "modified", time, order_id=order_id,
                                  quantity=quantity, price=price)

    def order_cancelled(self, order_id: int, *, time: datetime) -> None:
        self._ledger._order_event(self.idname, None, "cancelled", time, order_id=order_id)

    def fill(self, ref: Hashable, *, side: Side, quantity: float | Decimal,
             price: float | Decimal, commission: float | Decimal, time: datetime,
             reason: FillReason, order_id: int | None = None) -> int:
        """Record an execution and return its run-wide fill id."""
        return self._ledger._fill(self.idname, ref, side, quantity, price, commission,
                                  time, reason, order_id, False)

    def ignored_close(self, ref: Hashable) -> None:
        """Count a close request the broker ignored because the position was already closed."""
        self._ledger._accounts[self.idname].ignored_closes += 1


class Ledger:
    """Accounts, orders, fills, positions and the results derived from them."""

    def __init__(self) -> None:
        self._accounts: dict[str, _AccountState] = {}
        self._orders: dict[int, OrderEvent] = {}  # the "placed" event per order, for lookups
        self._order_events: list[OrderEvent] = []
        self._fills: list[Fill] = []
        self._round_trips: list[RoundTrip] = []
        self._equity: list[EquityPoint] = []
        self._marks: dict[str, Decimal] = {}
        self._next_order_id = 1
        self._next_fill_id = 1

    # --- setup -------------------------------------------------------------

    def open_account(self, idname: str, symbol: str, capital: float | Decimal) -> AccountRecorder:
        """Create the account for one algo and return its bound recorder."""
        if idname in self._accounts:
            raise ValueError(f"account {idname!r} already exists")
        info = AccountInfo(idname, symbol, money(capital))
        self._accounts[idname] = _AccountState(info)
        return AccountRecorder(self, idname)

    def recorder(self, idname: str) -> AccountRecorder:
        if idname not in self._accounts:
            raise KeyError(idname)
        return AccountRecorder(self, idname)

    # --- recording (called through AccountRecorder) -------------------------

    def _order_event(self, idname: str, ref: Hashable | None, kind: OrderEventKind,
                     time: datetime, *, side: Side | None = None,
                     quantity: float | Decimal | None = None, order_type: str | None = None,
                     price: float | Decimal | None = None, order_id: int | None = None) -> int:
        if kind == "placed":
            order_id = self._next_order_id
            self._next_order_id += 1
        else:
            prior = self._orders.get(order_id) if order_id is not None else None
            if prior is None or prior.account != idname:
                raise KeyError(f"unknown order {order_id} for account {idname!r}")
            ref = prior.broker_ref
        assert order_id is not None
        event = OrderEvent(order_id, idname, ref, kind, time, side,
                           None if quantity is None else money(quantity), order_type,
                           None if price is None else money(price))
        if kind == "placed":
            self._orders[order_id] = event
        self._order_events.append(event)
        return order_id

    def _fill(self, idname: str, ref: Hashable, side: Side, quantity: float | Decimal,
              price: float | Decimal, commission: float | Decimal, time: datetime,
              reason: FillReason, order_id: int | None, synthetic: bool) -> int:
        if reason not in FILL_REASONS:
            raise ValueError(f"unknown fill reason {reason!r}")
        if side not in ("buy", "sell"):
            raise ValueError(f"unknown side {side!r}")
        acct = self._accounts[idname]
        qty, px, comm = money(quantity), money(price), money(commission)
        if qty <= 0:
            raise ValueError("fill quantity must be positive")
        pos = acct.positions.get(ref)
        if reason == "entry":
            if pos is None:
                pos = _Position(idname, ref, acct.info.symbol, side == "buy", time)
                acct.positions[ref] = pos
            elif pos.long != (side == "buy"):
                raise ValueError("entry fill on the opposite side of the open position")
            pos.entry_qty += qty
            pos.entry_value += qty * px
        else:
            if pos is None:
                raise ValueError(f"{reason} fill with no open position for ref {ref!r}")
            if pos.long != (side == "sell"):
                raise ValueError("exit fill must be on the opposite side of the position")
            if qty > pos.open_qty:
                raise ValueError("exit fill larger than the open position")
            pos.exit_qty += qty
            pos.exit_value += qty * px
        pos.commission += comm
        acct.cash += money((-qty * px if side == "buy" else qty * px) - comm)

        fill_id = self._next_fill_id
        self._next_fill_id += 1
        self._fills.append(Fill(fill_id, order_id, idname, ref, acct.info.symbol, side, qty, px,
                                comm, time, reason, synthetic))
        if reason != "entry" and pos.open_qty == 0:
            self._close(acct, pos, time, reason, synthetic or reason == "end_of_data")
        return fill_id

    def _close(self, acct: _AccountState, pos: _Position, time: datetime, reason: FillReason,
               synthetic: bool) -> None:
        gross = money(pos.exit_value - pos.entry_value if pos.long
                      else pos.entry_value - pos.exit_value)
        commission = money(pos.commission)
        net = money(gross - commission)
        acct.realised = money(acct.realised + net)
        del acct.positions[pos.ref]
        self._round_trips.append(RoundTrip(
            acct.info.idname, pos.ref, pos.symbol, "long" if pos.long else "short",
            pos.entry_qty, pos.entry_time, time,
            money(pos.entry_value / pos.entry_qty), money(pos.exit_value / pos.exit_qty),
            gross, commission, net, time - pos.entry_time, reason, synthetic))

    # --- marking and finishing ---------------------------------------------

    def mark(self, time: datetime, closes_by_symbol: Mapping[str, float | Decimal]
             ) -> list[EquityPoint]:
        """Mark open positions to the given closes; return one point per account plus a total.

        A symbol missing from `closes_by_symbol` keeps its last mark, or its
        average entry price if it was never marked.
        """
        for symbol, close in closes_by_symbol.items():
            self._marks[symbol] = money(close)
        points = [self._point(time, idname) for idname in self._accounts]
        points.append(self._point(time, None))
        self._equity.extend(points)
        return points

    def finish(self, time: datetime, closes_by_symbol: Mapping[str, float | Decimal],
               commission: Callable[[Decimal], float | Decimal] | None = None) -> list[RoundTrip]:
        """Close every open position with a synthetic `end_of_data` fill at the given close.

        `commission(quantity)` supplies the charge per synthetic fill (the cost
        model is the broker's); without it the fill is free. A position whose
        symbol has no close is closed at its average entry price. Returns the
        round trips this call created.
        """
        before = len(self._round_trips)
        for acct in self._accounts.values():
            for pos in list(acct.positions.values()):
                qty = pos.open_qty
                close = closes_by_symbol.get(pos.symbol)
                price = money(close) if close is not None else pos.average_price
                self._marks[pos.symbol] = price
                comm = commission(qty) if commission else ZERO
                self._fill(acct.info.idname, pos.ref, "sell" if pos.long else "buy", qty, price,
                           comm, time, "end_of_data", None, True)
        return self._round_trips[before:]

    # --- results -----------------------------------------------------------

    def _select(self, account: str | None) -> list[_AccountState]:
        if account is None:
            return list(self._accounts.values())
        return [self._accounts[account]]

    def _unrealised(self, acct: _AccountState) -> Decimal:
        total = ZERO
        for pos in acct.positions.values():
            close = self._marks.get(pos.symbol, pos.average_price)
            total += pos.unrealised_gross(close) - money(pos.commission)
        return money(total)

    def _position_value(self, acct: _AccountState) -> Decimal:
        total = ZERO
        for pos in acct.positions.values():
            close = self._marks.get(pos.symbol, pos.average_price)
            total += close * pos.open_qty * (1 if pos.long else -1)
        return money(total)

    def _point(self, time: datetime, account: str | None) -> EquityPoint:
        accts = self._select(account)
        cash = money(sum((a.cash for a in accts), ZERO))
        value = money(sum((self._position_value(a) for a in accts), ZERO))
        return EquityPoint(time, account, cash, self.realised_pnl(account),
                           self.unrealised_pnl(account), money(cash + value))

    def cash(self, account: str | None = None) -> Decimal:
        return money(sum((a.cash for a in self._select(account)), ZERO))

    def realised_pnl(self, account: str | None = None) -> Decimal:
        return money(sum((a.realised for a in self._select(account)), ZERO))

    def unrealised_pnl(self, account: str | None = None) -> Decimal:
        return money(sum((self._unrealised(a) for a in self._select(account)), ZERO))

    def equity(self, account: str | None = None) -> Decimal:
        accts = self._select(account)
        return money(self.cash(account) + sum((self._position_value(a) for a in accts), ZERO))

    def ignored_closes(self, account: str | None = None) -> int:
        return sum(a.ignored_closes for a in self._select(account))

    @property
    def accounts(self) -> tuple[AccountInfo, ...]:
        return tuple(a.info for a in self._accounts.values())

    @property
    def orders(self) -> tuple[OrderEvent, ...]:
        return tuple(self._order_events)

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def round_trips(self) -> tuple[RoundTrip, ...]:
        return tuple(self._round_trips)

    @property
    def equity_points(self) -> tuple[EquityPoint, ...]:
        return tuple(self._equity)

    def open_positions(self) -> tuple[OpenPosition, ...]:
        return tuple(
            OpenPosition(p.account, p.ref, p.symbol, "long" if p.long else "short",
                         p.open_qty, p.average_price)
            for a in self._accounts.values() for p in a.positions.values())
