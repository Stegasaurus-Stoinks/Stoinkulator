"""Algo modes: which broker an algo trades through, and the results document.

Each config object may carry `"mode"`: `backtest` (the default) or `paper`.
Both use a simulated broker; they differ only in the data they will run on
(stored bars now, live bars once a live data feed exists), so for now they map
to the same factory. A third mode that trades real money is deliberately not
here: `parse_mode` refuses it by name with a clear message, and nothing in this
module can build a live broker, so a config naming it can never reach one.
There is no entry for it in `BROKER_FACTORIES` and none may be added without an
approved, human-reviewed task.

`build_router` creates one broker and one ledger account per algo, keyed by
idname. Brokers are never shared between algos, because a `PaperBroker`
books every fill under its account's single symbol.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from .broker import Broker, SimBroker
from .ledger import AccountRecorder, Ledger, money
from .paper_broker import CostModel, PaperBroker
from .router import MappingRouter

DEFAULT_MODE = "backtest"
FillModel = Literal["legacy", "realistic"]
FILL_MODELS: tuple[str, ...] = ("legacy", "realistic")

# Modes that are recognised only in order to be refused.
_REFUSED = "live"
LIVE_REFUSAL = "mode '{mode}' is not available yet (needs the live broker, milestone 1 Phase B)"


class ModeError(ValueError):
    """A mode value that cannot be used; the message is one line."""


def parse_mode(idname: str, value: object) -> str:
    """Return the mode named by a config object's `"mode"` value.

    Raises `ModeError` for a value that is not a string, is unknown, or is the
    refused mode. Called during config validation, before anything is built.
    """
    if not isinstance(value, str):
        raise ModeError(f"algo '{idname}': mode must be a string, got {type(value).__name__}")
    if value == _REFUSED:
        raise ModeError(f"algo '{idname}': " + LIVE_REFUSAL.format(mode=value))
    if value not in BROKER_FACTORIES:
        known = ", ".join(sorted(BROKER_FACTORIES))
        raise ModeError(f"algo '{idname}': unknown mode {value!r} (expected one of: {known})")
    return value


def _simulated_broker(account: AccountRecorder | None, fills: str, costs: CostModel) -> Broker:
    if fills == "legacy":
        return SimBroker(account=account)
    if fills == "realistic":
        if account is None:
            raise ValueError("realistic fills need a ledger account")
        return PaperBroker(account, costs)
    raise ValueError(f"unknown fill model {fills!r} (expected one of: {', '.join(FILL_MODELS)})")


# mode -> broker factory. Backtest and paper both trade through a simulated broker.
BROKER_FACTORIES: Mapping[str, Callable[[AccountRecorder | None, str, CostModel], Broker]] = {
    "backtest": _simulated_broker,
    "paper": _simulated_broker,
}


class ModeRouter(MappingRouter):
    """A `MappingRouter` that also knows each algo's broker and end-of-data cost."""

    def __init__(self, brokers: Mapping[str, Broker], fills: str) -> None:
        super().__init__(brokers)
        self.brokers: dict[str, Broker] = dict(brokers)
        self.fills = fills

    @property
    def end_commission(self) -> Callable[[str, Decimal], Decimal] | None:
        """`commission(idname, quantity)` for `Ledger.finish`; None (free) for legacy fills."""
        if self.fills != "realistic":
            return None
        brokers = self.brokers

        def commission(idname: str, quantity: Decimal) -> Decimal:
            return brokers[idname].costs.commission(quantity)  # type: ignore[attr-defined]

        return commission


def build_router(
    entries: Sequence[Mapping[str, Any]],
    *,
    fills: str,
    costs: CostModel,
    capital: Decimal,
    ledger: Ledger | None,
) -> ModeRouter:
    """One broker (and, with a ledger, one account of `capital`) per algo config.

    `entries` is the validated algo config. With `fills="legacy"` each broker is
    a `SimBroker`; with `"realistic"` a `PaperBroker` with `costs`, which needs
    `ledger`. A config whose mode has no factory (including the refused one)
    raises `ModeError`; nothing is built for it.
    """
    brokers: dict[str, Broker] = {}
    for entry in entries:
        for cfg in entry["data"]:
            idname = cfg["idname"]
            factory = BROKER_FACTORIES.get(parse_mode(idname, cfg.get("mode", DEFAULT_MODE)))
            if factory is None:  # pragma: no cover - parse_mode already refused it
                raise ModeError(f"algo '{idname}': no broker for its mode")
            if idname in brokers:
                raise ValueError(f"duplicate idname {idname!r}")
            account = None if ledger is None else ledger.open_account(idname, cfg["ticker"], capital)
            brokers[idname] = factory(account, fills, costs)
    return ModeRouter(brokers, fills)


def _qty(value: Decimal) -> int | str:
    return int(value) if value == value.to_integral_value() else str(value)


def ledger_document(
    ledger: Ledger, idnames: Sequence[str], *, costs: CostModel, capital: Decimal
) -> dict[str, Any]:
    """The `ledger.json` content: fill model, costs, capital, per-algo and account results.

    Money is a string with 4 decimals. Algos appear in `idnames` order; round
    trips in the order the ledger closed them.
    """
    accounts: dict[str, Any] = {}
    for idname in idnames:
        trips = [t for t in ledger.round_trips if t.account == idname]
        symbol = next(a.symbol for a in ledger.accounts if a.idname == idname)
        accounts[idname] = {
            "symbol": symbol,
            "summary": _summary(trips, ledger.equity(idname)),
            "round_trips": [
                {
                    "side": t.side,
                    "quantity": _qty(t.quantity),
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                    "entry_price": str(t.entry_price),
                    "exit_price": str(t.exit_price),
                    "gross": str(t.gross),
                    "commission": str(t.commission),
                    "net": str(t.net),
                    "exit_reason": t.exit_reason,
                    "synthetic": t.synthetic,
                }
                for t in trips
            ],
        }
    return {
        "fills": "realistic",
        "costs": {
            "commission_per_share": str(money(costs.commission_per_share)),
            "commission_minimum": str(money(costs.commission_minimum)),
            "slippage_per_share": str(money(costs.slippage_per_share)),
        },
        "capital": str(money(capital)),
        "summary": _summary(list(ledger.round_trips), ledger.equity()),
        "accounts": accounts,
    }


def _summary(trips: Sequence[Any], equity: Decimal) -> dict[str, Any]:
    return {
        "trades": len(trips),
        "net": str(money(sum((t.net for t in trips), Decimal(0)))),
        "commission": str(money(sum((t.commission for t in trips), Decimal(0)))),
        "final_equity": str(equity),
    }
