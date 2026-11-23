"""Contract builders for the data-only IB code.

Only plain US stocks: security type STK, routed through SMART, priced in USD.
`primaryExchange` is left empty (IB then picks the listing) or set to the
primary exchange IB itself resolved, never to "SMART": SMART is a router, not a
primary exchange, and a SMART primary makes IB ask for a disambiguation or
reject the request (the legacy helper did exactly that; it is not copied).
"""

from __future__ import annotations

import re

from ibapi.contract import Contract

from MainStoinker.ib.history import check_symbol

SEC_TYPE = "STK"
ROUTING_EXCHANGE = "SMART"
CURRENCY = "USD"

_PRIMARY = re.compile(r"[A-Z0-9]{2,12}")


def check_primary_exchange(primary_exchange: str) -> str:
    """`primary_exchange` if it is empty or a plausible exchange code, else `ValueError`.

    "SMART" is refused: it is the routing exchange, never a primary one.
    """
    if not isinstance(primary_exchange, str):
        raise ValueError(f"primary exchange must be a string, got {primary_exchange!r}")
    if primary_exchange == "":
        return primary_exchange
    if primary_exchange == ROUTING_EXCHANGE:
        raise ValueError("primary exchange cannot be SMART (a router, not a primary exchange)")
    if not _PRIMARY.fullmatch(primary_exchange):
        raise ValueError(f"invalid primary exchange {ascii(primary_exchange)}")
    return primary_exchange


def stock(symbol: str, primary_exchange: str = "", con_id: int = 0) -> Contract:
    """A new STK / SMART / USD `Contract` for `symbol`.

    `primary_exchange` is empty or the resolved primary exchange (never SMART);
    `con_id` is 0 (not yet resolved) or IB's contract id. The symbol must pass
    `ib.history.check_symbol`. A fresh object is returned every call, so callers
    can never share and mutate one.
    """
    check_symbol(symbol)
    if symbol != symbol.upper():
        raise ValueError(f"invalid symbol {ascii(symbol)}: IB symbols are upper case")
    check_primary_exchange(primary_exchange)
    if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id < 0:
        raise ValueError(f"con_id must be an int of 0 or more, got {con_id!r}")
    contract = Contract()
    contract.symbol = symbol
    contract.secType = SEC_TYPE
    contract.exchange = ROUTING_EXCHANGE
    contract.currency = CURRENCY
    contract.primaryExchange = primary_exchange
    contract.conId = con_id
    return contract
