"""A paper broker with a realistic fill model and trading costs.

`PaperBroker` implements `Broker` and `BarAwareBroker`. One instance serves one
algo and reports every order, modification, cancel and fill to that algo's
ledger account (`AccountRecorder`). Money is `Decimal`; bar prices are floats.

State of a position, per ref:

    pending --(next bar, same session)--> open --(algo close)--> exiting
       |                                    |                       |
       |                                    +--(stop / target)--+   |
       +--(other session / no bar / algo close)--> cancelled        v
                                                              closed <-+

Fill rules (the engine calls `on_bar` for every feed symbol before any algo
sees the bar; positions of other symbols are ignored):

- Entry: a market entry requested during bar t fills at the open of bar t+1,
  plus slippage against the trader. If bar t+1 is on another calendar date,
  or there is none (`on_end`), the entry is cancelled and counted
  (`cancelled_entries`). `limit=True` raises `NotImplementedError`. When the
  gap rule is attached (`set_gap_watch`) and bar t+1 follows a gap for the
  symbol, the entry is cancelled the same way (and reported to the gap rule);
  stops, targets and exits are not affected by gaps.
- Stops and targets rest from the fill bar on and are checked against the
  range of every bar, the fill bar included. A long stop triggers when
  `low <= stop` and fills at `min(open, stop)`, with slippage. A long target
  triggers when `high > target` (strict, as `Trade.check_tp`) and fills at
  `max(open, target)`, without slippage. Shorts mirror. A bar that covers both
  fills the stop. Stop and target changes made while the entry is pending
  apply at the fill. Stop prices are not rounded to the tick.
- Algo close of an open position (state `exiting`): fills at the open of the
  next bar, plus slippage. If the next bar is in another session, or `on_end`
  comes first, it fills at the price `Trade` passed (the requesting bar's
  close, no slippage) with the requesting bar's time. While exiting, the
  bracket no longer rests: the algo's close supersedes it, so the stop and
  target orders are cancelled at the request. A close on a pending entry
  cancels it. A close on an exiting, closed or cancelled position is recorded
  as ignored.
- `modify_stop` / `set_take_profit` on a position that is exiting, closed or
  cancelled are recorded as ignored. `set_take_profit` with a quantity other
  than the position's raises `ValueError` (partial targets are unsupported).
- `on_end` cancels pending entries and fills exiting positions at their
  requested price. Open positions stay open: the engine's `Ledger.finish`
  closes them synthetically, and its `end_commission` should be built from
  `CostModel.commission`.

Accepted consequence: a broker learns that bar t was the last of its session
only when the first bar of the next session arrives. A close requested on that
last bar is then filled at bar t's close with time t, after the equity point
for t was stored. That point therefore lacks the exit's commission; the next
point has it.

Commission is `max(per_share * qty, minimum)` per fill (each entry and exit).
Slippage is per share on market and stop fills.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from .feed import Bar
from .ledger import AccountRecorder, FillReason, Side
from .ledger import commission_per_share as _commission_for

if TYPE_CHECKING:
    import pandas as pd

PositionState = Literal["pending", "open", "exiting", "closed", "cancelled"]


def _decimal(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class CostModel:
    """Trading costs. Numbers given as float or int are converted to `Decimal`."""

    commission_per_share: Decimal = Decimal("0.005")
    commission_minimum: Decimal = Decimal("1.00")
    slippage_per_share: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("commission_per_share", "commission_minimum", "slippage_per_share"):
            value = _decimal(getattr(self, name))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0, got {value}")
            object.__setattr__(self, name, value)

    def commission(self, quantity: float | Decimal) -> Decimal:
        """Commission for one order of `quantity` shares."""
        return _commission_for(quantity, self.commission_per_share, self.commission_minimum)


class _Position:
    """Working state of one position (internal)."""

    def __init__(self, ref: Hashable, symbol: str, long: bool, qty: float, stop: float,
                 time: datetime) -> None:
        self.ref, self.symbol, self.long, self.qty, self.stop = ref, symbol, long, qty, stop
        self.tp: float | None = None
        self.state: PositionState = "pending"
        self.req_time = time  # time of the entry request, later of the close request
        self.exit_price = 0.0  # the price Trade passed with the close request
        self.entry_id: int | None = None
        self.stop_id: int | None = None
        self.tp_id: int | None = None
        self.exit_id: int | None = None

    @property
    def entry_side(self) -> Side:
        return "buy" if self.long else "sell"

    @property
    def exit_side(self) -> Side:
        return "sell" if self.long else "buy"


class PaperBroker:
    """Simulated broker for one algo; see the module docstring for the fill model."""

    def __init__(self, account: AccountRecorder, costs: CostModel | None = None) -> None:
        self._account = account
        self._costs = costs if costs is not None else CostModel()
        self._positions: dict[Hashable, _Position] = {}
        self._live: dict[Hashable, _Position] = {}  # pending, open or exiting
        self._last_time: dict[str, datetime] = {}
        self._next_ref = 1
        self._cancelled_entries = 0
        self._follows_gap: Callable[[str], bool] | None = None
        self._on_gap_cancel: Callable[[str], None] | None = None

    def set_gap_watch(self, follows_gap: Callable[[str], bool],
                      on_cancel: Callable[[str], None]) -> None:
        """Cancel a pending entry whose fill bar follows a gap (issue 206).

        `follows_gap(symbol)` says whether the bar being processed for `symbol`
        follows a gap; `on_cancel(symbol)` is called for each pending entry
        cancelled because of it. Set by the gap rule's guard; without it nothing
        changes.
        """
        self._follows_gap = follows_gap
        self._on_gap_cancel = on_cancel

    @property
    def costs(self) -> CostModel:
        return self._costs

    @property
    def cancelled_entries(self) -> int:
        """Entries cancelled before filling (session end, end of data, or an algo close)."""
        return self._cancelled_entries

    def position_state(self, ref: Hashable) -> PositionState:
        return self._get(ref).state

    # --- Broker protocol ---------------------------------------------------

    def open_position(
        self,
        *,
        symbol: str,
        volume: float,
        direction: int,
        price: float,
        time: pd.Timestamp,
        limit: bool,
        stop_price: float,
        oca_group: str,
    ) -> Hashable:
        if limit:
            raise NotImplementedError("PaperBroker does not support limit entries")
        if volume <= 0:
            raise ValueError(f"volume must be positive, got {volume}")
        ref = self._next_ref
        self._next_ref += 1
        pos = _Position(ref, symbol, bool(direction), volume, float(stop_price), time)
        self._positions[ref] = pos
        self._live[ref] = pos
        pos.entry_id = self._account.order_placed(
            ref, time=time, side=pos.entry_side, quantity=volume, order_type="entry")
        pos.stop_id = self._account.order_placed(
            ref, time=time, side=pos.exit_side, quantity=volume, order_type="stop",
            price=_decimal(float(stop_price)))
        return ref

    def modify_stop(self, ref: Hashable, stop_price: float) -> None:
        pos = self._get(ref)
        time = self._now(pos)
        if pos.state not in ("pending", "open"):
            self._account.ignored(ref, time=time, kind="modify_stop")
            return
        pos.stop = float(stop_price)
        assert pos.stop_id is not None
        self._account.order_modified(pos.stop_id, time=time, price=_decimal(pos.stop))

    def set_take_profit(self, ref: Hashable, price: float, quantity: float) -> None:
        pos = self._get(ref)
        if float(quantity) != float(pos.qty):
            raise ValueError(f"partial take-profit unsupported: quantity {quantity} "
                             f"differs from the position's {pos.qty}")
        time = self._now(pos)
        if pos.state not in ("pending", "open"):
            self._account.ignored(ref, time=time, kind="take_profit")
            return
        pos.tp = float(price)
        if pos.tp_id is None:
            pos.tp_id = self._account.order_placed(
                ref, time=time, side=pos.exit_side, quantity=pos.qty, order_type="target",
                price=_decimal(pos.tp))
        else:
            self._account.order_modified(pos.tp_id, time=time, price=_decimal(pos.tp))

    def close_position(self, ref: Hashable, *, price: float, time: pd.Timestamp) -> None:
        pos = self._get(ref)
        if pos.state == "pending":
            self._cancel_entry(pos, time)
        elif pos.state == "open":
            pos.state = "exiting"
            pos.exit_price = float(price)
            pos.req_time = time
            self._cancel(pos.stop_id, time)
            self._cancel(pos.tp_id, time)
            pos.stop_id = pos.tp_id = None
            pos.exit_id = self._account.order_placed(
                ref, time=time, side=pos.exit_side, quantity=pos.qty, order_type="exit")
        else:
            self._account.ignored(ref, time=time, kind="close")

    def stop_is_working(self, ref: Hashable) -> bool:
        return self._get(ref).state in ("pending", "open")

    # --- BarAwareBroker ----------------------------------------------------

    def on_bar(self, symbol: str, bar: Bar) -> None:
        self._last_time[symbol] = bar.date
        for pos in list(self._live.values()):
            if pos.symbol == symbol:
                self._step(pos, bar)

    def on_end(self, last_bars: Mapping[str, Bar]) -> None:
        for pos in list(self._live.values()):
            if pos.state == "pending":
                self._cancel_entry(pos, self._now(pos))
            elif pos.state == "exiting":
                self._fill_requested_exit(pos)

    # --- internals ---------------------------------------------------------

    def _get(self, ref: Hashable) -> _Position:
        try:
            return self._positions[ref]
        except KeyError:
            raise KeyError(f"unknown position ref {ref!r}") from None

    def _now(self, pos: _Position) -> datetime:
        last = self._last_time.get(pos.symbol)
        return pos.req_time if last is None else max(last, pos.req_time)

    def _cancel(self, order_id: int | None, time: datetime) -> None:
        if order_id is not None:
            self._account.order_cancelled(order_id, time=time)

    def _cancel_entry(self, pos: _Position, time: datetime) -> None:
        pos.state = "cancelled"
        self._cancelled_entries += 1
        for order_id in (pos.entry_id, pos.stop_id, pos.tp_id):
            self._cancel(order_id, time)
        pos.entry_id = pos.stop_id = pos.tp_id = None
        del self._live[pos.ref]

    def _price(self, raw: float, side: Side, *, slippage: bool) -> Decimal:
        """`raw` as Decimal, moved against the trader by the slippage if it applies."""
        price = Decimal(repr(float(raw)))
        if slippage:
            slip = self._costs.slippage_per_share
            price = price + slip if side == "buy" else price - slip
        return price

    def _fill(self, pos: _Position, side: Side, price: Decimal, time: datetime,
              reason: FillReason, order_id: int | None) -> None:
        self._account.fill(pos.ref, side=side, quantity=pos.qty, price=price,
                           commission=self._costs.commission(pos.qty), time=time,
                           reason=reason, order_id=order_id)

    def _close_out(self, pos: _Position, time: datetime) -> None:
        """Position is flat: cancel what still rests."""
        for order_id in (pos.stop_id, pos.tp_id):
            self._cancel(order_id, time)
        pos.state = "closed"
        pos.stop_id = pos.tp_id = pos.exit_id = None
        del self._live[pos.ref]

    def _fill_requested_exit(self, pos: _Position) -> None:
        price = self._price(pos.exit_price, pos.exit_side, slippage=False)
        self._fill(pos, pos.exit_side, price, pos.req_time, "algo_exit", pos.exit_id)
        self._close_out(pos, pos.req_time)

    def _step(self, pos: _Position, bar: Bar) -> None:
        time = bar.date
        if pos.state == "pending":
            if time.date() != pos.req_time.date():
                self._cancel_entry(pos, time)
                return
            if self._follows_gap is not None and self._follows_gap(pos.symbol):
                # Gap rule: the fill bar follows a gap, so the entry never fills.
                self._cancel_entry(pos, time)
                if self._on_gap_cancel is not None:
                    self._on_gap_cancel(pos.symbol)
                return
            price = self._price(bar.open, pos.entry_side, slippage=True)
            self._fill(pos, pos.entry_side, price, time, "entry", pos.entry_id)
            pos.entry_id = None
            pos.state = "open"
        elif pos.state == "exiting":
            if time.date() != pos.req_time.date():
                self._fill_requested_exit(pos)
                return
            price = self._price(bar.open, pos.exit_side, slippage=True)
            self._fill(pos, pos.exit_side, price, time, "algo_exit", pos.exit_id)
            self._close_out(pos, time)
            return
        self._check_bracket(pos, bar)

    def _check_bracket(self, pos: _Position, bar: Bar) -> None:
        """Stop first, then target, against the bar's range."""
        if pos.long:
            stopped = bar.low <= pos.stop
            stop_raw = min(bar.open, pos.stop)
            targeted = pos.tp is not None and bar.high > pos.tp
            target_raw = max(bar.open, pos.tp) if pos.tp is not None else 0.0
        else:
            stopped = bar.high >= pos.stop
            stop_raw = max(bar.open, pos.stop)
            targeted = pos.tp is not None and bar.low < pos.tp
            target_raw = min(bar.open, pos.tp) if pos.tp is not None else 0.0
        if stopped:
            price = self._price(stop_raw, pos.exit_side, slippage=True)
            self._fill(pos, pos.exit_side, price, bar.date, "stop", pos.stop_id)
            pos.stop_id = None
            self._close_out(pos, bar.date)
        elif targeted:
            price = self._price(target_raw, pos.exit_side, slippage=False)
            self._fill(pos, pos.exit_side, price, bar.date, "target", pos.tp_id)
            pos.tp_id = None
            self._close_out(pos, bar.date)
