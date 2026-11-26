"""Hand-built bars and a small rig for the FVGZone tests.

Prices are written as the zone lies relative to a base price `b`:

    c1 = (b, b+.10, b-.05, b+.05)        high1 = b+.10
    c2 = (b+.05, b+.60, b+.05, b+.55)
    c3 = (b+.55, b+.70, b+.30, b+.65)    low3 = b+.30   -> up zone [b+.10, b+.30]
    retest = (b+.60, b+.62, b+.25, b+.45)  low <= top, close above top

`mirror` turns any bars into the exact mirror image about a price, which turns
an up zone into a down zone and a long into a short.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from MainStoinker.engine.algos import load_algo
from MainStoinker.engine.bars import BarStore
from MainStoinker.engine.broker import SimBroker
from MainStoinker.engine.feed import COLUMNS, Bar

D1, D2, D3 = "2024-08-19", "2024-08-20", "2024-08-21"
MIRROR_AT = 200.0  # price that maps to itself


@dataclass(frozen=True)
class B:
    """One 1-minute bar: `at` is the bar label (its start)."""

    at: pd.Timestamp
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: int = 1000


def stamp(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}")


def r2(x: float) -> float:
    return round(x, 2)


def run_of(day: str, start: str, ohlc: Sequence[tuple], vol: int = 1000) -> list[B]:
    """Consecutive minutes from `start`; each item is (o, h, l, c) or (o, h, l, c, volume)."""
    t0 = stamp(day, start)
    return [B(t0 + pd.Timedelta(minutes=i), *map(r2, x[:4]), *(x[4:] or (vol,)))
            for i, x in enumerate(ohlc)]


def quiet(n: int, base: float = 100.0, vol: int = 1000) -> list[tuple]:
    return [(base, base + .10, base - .10, base, vol)] * n


def up_setup(b: float) -> list[tuple]:
    """c1, c2, c3 of an up zone [b+.10, b+.30]."""
    return [(b, b + .10, b - .05, b + .05), (b + .05, b + .60, b + .05, b + .55),
            (b + .55, b + .70, b + .30, b + .65)]


def retest_up(b: float) -> tuple:
    """Dips into the zone [b+.10, b+.30] and closes above it at b+.45."""
    return (b + .60, b + .62, b + .25, b + .45)


def mirror(bars: Sequence[B], about: float = MIRROR_AT) -> list[B]:
    return [B(x.at, r2(about - x.o), r2(about - x.l), r2(about - x.h), r2(about - x.c), x.v)
            for x in bars]


def minutes_between(start: str, end: str) -> int:
    return int((stamp(D1, end) - stamp(D1, start)) / pd.Timedelta(minutes=1))


def scenario(day: str, retest_at: str, after: Sequence[tuple] = (), base: float = 100.0,
             context: Sequence[tuple] | None = None) -> list[B]:
    """Quiet bars from 09:30, then c1 c2 c3 and the retest bar at `retest_at`, then `after`."""
    n_quiet = minutes_between("09:30", retest_at) - 3
    ctx = list(context) if context is not None else quiet(n_quiet, base)
    if len(ctx) != n_quiet:
        raise ValueError(f"context must have {n_quiet} bars, got {len(ctx)}")
    return run_of(day, "09:30", [*ctx, *up_setup(base), retest_up(base), *after])


def empty_store() -> BarStore:
    dtypes = {"date": "datetime64[us]", "time": "int64", "open": "float64", "high": "float64",
              "low": "float64", "close": "float64", "volume": "int64"}
    return BarStore(pd.DataFrame({c: pd.Series(dtype=dtypes[c]) for c in COLUMNS}))


class Rig:
    """An FVGZone algo on a `SimBroker`, fed bar by bar the way the engine does."""

    def __init__(self, **cfg) -> None:
        self.cfg = {"idname": "t-fvgzone", "ticker": "AAPL", **cfg}
        self.algo = load_algo("FVGZone", self.cfg)
        self.algo.broker = SimBroker()
        self.store = empty_store()

    def feed(self, bars: Sequence[B]) -> Rig:
        for x in bars:
            self.store.append(Bar(x.at, int(x.at.timestamp()), x.o, x.h, x.l, x.c, x.v))
            self.algo.update(self.store.view())
        return self

    @property
    def trades(self) -> list:
        return self.algo.trades


def summary(trade) -> dict:
    """What a test needs to know about one trade."""
    done = trade.status == "Closed"
    return {
        "side": int(bool(trade.direction)), "at": trade.openTime, "price": trade.openPrice,
        "stop": trade.stopPrice, "tp": trade.tp, "shares": trade.volume,
        "exit_at": trade.closeTime if done else None,
        "exit": trade.closePrice if done else None,
    }


def mirrored(s: dict, about: float = MIRROR_AT) -> dict:
    """The expected summary of the mirror image of a trade."""
    out = dict(s, side=1 - s["side"])
    for key in ("price", "stop", "tp", "exit"):
        if s[key] is not None:
            out[key] = r2(about - s[key])
    return out
