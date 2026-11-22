"""Algo modes: which broker an algo trades through (routing only; results live in `stats`/`report`).

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
from types import MappingProxyType
from typing import Any, Literal

from .broker import Broker, SimBroker
from .ledger import AccountRecorder, Ledger
from .paper_broker import CostModel, PaperBroker
from .router import MappingRouter

DEFAULT_MODE = "backtest"
FillModel = Literal["legacy", "realistic"]
FILL_MODELS: tuple[str, ...] = ("legacy", "realistic")

# Modes that are recognised only in order to be refused.
_REFUSED = "live"
LIVE_REFUSAL = "mode {mode} is not available yet (needs the live broker, milestone 1 Phase B)"


class ModeError(ValueError):
    """A mode value that cannot be used; the message is one line."""


def parse_mode(idname: str, value: object) -> str:
    """Return the mode named by a config object's `"mode"` value.

    Raises `ModeError` for a value that is not a string, is unknown, or is the
    refused mode. A spelling of the refused mode that differs only by case or
    surrounding whitespace (`Live`, ` live `) gets the refusal message too;
    accepted modes are compared by exact equality only. User-supplied values
    appear in messages through `ascii()`, so a message is always encodable.
    Called during config validation, before anything is built.
    """
    who = f"algo {ascii(idname)}"
    if not isinstance(value, str):
        raise ModeError(f"{who}: mode must be a string, got {type(value).__name__}")
    if value.strip().lower() == _REFUSED:
        raise ModeError(f"{who}: " + LIVE_REFUSAL.format(mode=ascii(value)))
    if value not in BROKER_FACTORIES:
        known = ", ".join(sorted(BROKER_FACTORIES))
        raise ModeError(f"{who}: unknown mode {ascii(value)} (expected one of: {known})")
    return value


def _simulated_broker(account: AccountRecorder | None, fills: str, costs: CostModel) -> Broker:
    if fills == "legacy":
        return SimBroker(account=account)
    if fills == "realistic":
        if account is None:
            raise ValueError("realistic fills need a ledger account")
        return PaperBroker(account, costs)
    raise ValueError(f"unknown fill model {fills!r} (expected one of: {', '.join(FILL_MODELS)})")


# mode -> broker factory (read-only). Backtest and paper both trade through a simulated broker.
BROKER_FACTORIES: Mapping[str, Callable[[AccountRecorder | None, str, CostModel], Broker]] = (
    MappingProxyType({
        "backtest": _simulated_broker,
        "paper": _simulated_broker,
    })
)


class ModeRouter(MappingRouter):
    """A `MappingRouter` that also knows each algo's mode, broker and end-of-data cost.

    `modes` maps idname to its parsed mode (`backtest` / `paper`).
    """

    def __init__(self, brokers: Mapping[str, Broker], fills: str,
                 modes: Mapping[str, str]) -> None:
        super().__init__(brokers)
        self.brokers: dict[str, Broker] = dict(brokers)
        self.fills = fills
        self.modes: dict[str, str] = dict(modes)
        self._paper: dict[str, PaperBroker] = {
            idname: broker for idname, broker in brokers.items() if isinstance(broker, PaperBroker)
        }

    @property
    def end_commission(self) -> Callable[[str, Decimal], Decimal] | None:
        """`commission(idname, quantity)` for `Ledger.finish`; None (free) for legacy fills."""
        if self.fills != "realistic":
            return None
        paper = self._paper

        def commission(idname: str, quantity: Decimal) -> Decimal:
            return paper[idname].costs.commission(quantity)

        return commission

    def cancelled_entries(self, idname: str) -> int:
        """Entries the algo's broker cancelled before filling; 0 for brokers that do not count."""
        broker = self._paper.get(idname)
        return 0 if broker is None else broker.cancelled_entries


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
    parsed: dict[str, str] = {}
    for entry in entries:
        for cfg in entry["data"]:
            idname = cfg["idname"]
            mode = parse_mode(idname, cfg.get("mode", DEFAULT_MODE))
            factory = BROKER_FACTORIES.get(mode)
            if factory is None:  # pragma: no cover - parse_mode already refused it
                raise ModeError(f"algo {ascii(idname)}: no broker for its mode")
            if idname in brokers:
                raise ValueError(f"duplicate idname {ascii(idname)}")
            account = None if ledger is None else ledger.open_account(idname, cfg["ticker"], capital)
            brokers[idname] = factory(account, fills, costs)
            parsed[idname] = mode
    return ModeRouter(brokers, fills, parsed)


