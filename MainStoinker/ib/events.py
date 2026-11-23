"""Event types the data-only IB client turns ibapi callbacks into.

Frozen dataclasses of plain Python values. This module imports nothing from
`ibapi`, so code that only consumes events never loads the IB client.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ConnectAck:
    """The socket handshake finished (`connectAck`)."""


@dataclass(frozen=True)
class ConnectionClosed:
    """TWS closed the connection, or the client disconnected."""


@dataclass(frozen=True)
class NextValidId:
    order_id: int


@dataclass(frozen=True)
class ManagedAccounts:
    """Comma-separated account ids as TWS sends them (a recorder must redact)."""

    accounts: str


@dataclass(frozen=True)
class IbError:
    """An `error` callback. `code` and `message` are IB's; `error_time_ms` is
    the timestamp IB 10.37 sends before them (epoch milliseconds)."""

    req_id: int
    code: int
    message: str
    error_time_ms: int = 0
    advanced_order_reject_json: str = ""


@dataclass(frozen=True)
class HistoricalBar:
    """One bar from `historicalData` (`update` False) or `historicalDataUpdate`."""

    req_id: int
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal
    wap: Decimal
    bar_count: int
    update: bool = False


@dataclass(frozen=True)
class HistoricalDataEnd:
    req_id: int
    start: str
    end: str


@dataclass(frozen=True)
class ContractDetailsEvent:
    req_id: int
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    local_symbol: str
    long_name: str
    min_tick: float
    time_zone_id: str
    trading_hours: str
    liquid_hours: str


@dataclass(frozen=True)
class ContractDetailsEnd:
    req_id: int


@dataclass(frozen=True)
class HeadTimestamp:
    req_id: int
    timestamp: str


@dataclass(frozen=True)
class MarketDataType:
    req_id: int
    market_data_type: int


@dataclass(frozen=True)
class TickPrice:
    req_id: int
    tick_type: int
    price: float
    can_auto_execute: bool
    past_limit: bool
    pre_open: bool


@dataclass(frozen=True)
class TickSize:
    req_id: int
    tick_type: int
    size: Decimal


@dataclass(frozen=True)
class TickString:
    req_id: int
    tick_type: int
    value: str


@dataclass(frozen=True)
class CurrentTime:
    """IB server time, epoch seconds."""

    time: int


Event = (
    ConnectAck | ConnectionClosed | NextValidId | ManagedAccounts | IbError
    | HistoricalBar | HistoricalDataEnd | ContractDetailsEvent | ContractDetailsEnd
    | HeadTimestamp | MarketDataType | TickPrice | TickSize | TickString | CurrentTime
)
