"""The engine: runs algos over a bar feed with every dependency passed in.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`. It never sleeps, starts threads or reads
global state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .bars import BarStore
from .broker import Broker
from .feed import Bar, BarFeed
from .ledger import Ledger
from .router import BarAwareBroker, BrokerRouter, SingleBrokerRouter
from .sink import EventSink


@dataclass(frozen=True)
class EngineConfig:
    """Engine settings. Empty for now: `run_backtest` needs none.

    Fields arrive with later tasks (live mode, logging, ...). Whether algo
    data is published is decided by the sink (`sink.wants_algo_data`).
    """


class Engine:
    """Replays a `BarFeed` through algos, reporting each bar to an `EventSink`.

    Algos are legacy-style objects with `ticker`, `broker`, `update(frame)`
    and `update_frontend()`. On construction every algo gets `algo.broker =
    broker`, and algos are grouped by `algo.ticker` in registration order. An
    algo whose ticker is not in the feed raises `ValueError`; so does the same
    algo object appearing twice. A feed symbol with no algo is allowed: its
    bars are still stored and published to the sink (with `[]` as algo data
    when the sink wants it, else `None`).

    The fourth argument is a `Broker` (shared by every algo, wrapped in a
    `SingleBrokerRouter`) or a `BrokerRouter`. An object that satisfies both
    protocols is treated as a `Broker`, so anything that worked before routers
    existed behaves identically. Anything else raises `TypeError`. Every
    routed value must be a `Broker`, checked for all algos before any algo is
    touched (a router error such as `MappingRouter`'s `KeyError` is also
    raised before that point).

    `ledger`, if given, is marked after the algos at every post-warm-up
    timestamp and finished after the last one. `end_commission` is passed to
    `Ledger.finish` as its per-account `commission(idname, quantity)`
    callable, charged on the synthetic end-of-data closes; the engine has no
    cost model of its own, so the caller supplies it. Default None: free.
    """

    def __init__(
        self,
        config: EngineConfig,
        feed: BarFeed,
        broker: Broker | BrokerRouter,
        sink: EventSink,
        algos: Sequence[object],
        *,
        ledger: Ledger | None = None,
        end_commission: Callable[[str, Decimal], float | Decimal] | None = None,
    ) -> None:
        # Without a broker, Trade falls back to the legacy global live-trading
        # flag and the ibapi order calls. The engine must never allow that.
        router: BrokerRouter
        if isinstance(broker, Broker):
            router = SingleBrokerRouter(broker)
        elif isinstance(broker, BrokerRouter):
            router = broker
        else:
            raise TypeError(f"Engine requires a Broker, got {type(broker).__name__}")
        self._config = config
        self._ledger = ledger
        self._end_commission = end_commission
        self._feed = feed
        self._broker = broker
        self._sink = sink
        self._algos: tuple[object, ...] = tuple(algos)
        symbols = feed.symbols()
        by_symbol: dict[str, list] = {symbol: [] for symbol in symbols}
        seen: set[int] = set()
        for algo in self._algos:
            ticker = algo.ticker  # type: ignore[attr-defined]
            if ticker not in by_symbol:
                raise ValueError(
                    f"algo {getattr(algo, 'name', algo)!r} is on ticker {ticker!r}, "
                    f"which is not in the feed (symbols: {symbols})"
                )
            if id(algo) in seen:
                raise ValueError(f"algo {getattr(algo, 'name', algo)!r} was passed twice")
            seen.add(id(algo))
            by_symbol[ticker].append(algo)
        routed = [(algo, router.broker_for(algo)) for algo in self._algos]
        for algo, routed_broker in routed:
            if not isinstance(routed_broker, Broker):
                raise TypeError(
                    f"router gave algo {getattr(algo, 'name', algo)!r} a "
                    f"{type(routed_broker).__name__}, which is not a Broker"
                )
        for algo, routed_broker in routed:
            algo.broker = routed_broker  # type: ignore[attr-defined]
        distinct = {id(b): b for _, b in routed}
        self._bar_brokers: tuple[BarAwareBroker, ...] = tuple(
            b for b in distinct.values() if isinstance(b, BarAwareBroker)
        )
        self._symbols = symbols
        self._by_symbol = by_symbol
        self._ran = False

    @property
    def algos(self) -> tuple[object, ...]:
        """The algos in registration order (read-only)."""
        return self._algos

    def run_backtest(self) -> None:
        """Replay the whole feed once. A second call raises `RuntimeError`.

        Algos are stateful, so a re-run would double their trades.

        Per batch: every symbol's bar is appended to its store first (feed
        symbol order); then, per symbol in feed order, each algo on that
        symbol is updated with the full frame in registration order. When the
        sink wants algo data, `update_frontend()` is called right after each
        algo's `update` (as the legacy `Ticker.update_algos` does), then the
        sink gets one `on_bar` for the symbol. Warm-up bars update no algo.

        Per timestamp the order is: append every symbol's bar; `on_bar(symbol,
        bar)` on every distinct bar-aware broker (first-seen order among the
        algos) for every symbol in feed order; update the algos; then
        `ledger.mark(timestamp, closes)` if there is a ledger. Warm-up bars
        reach no broker and no mark. After the last timestamp, `on_end(
        last_bars)` is called once on each distinct bar-aware broker, then
        `ledger.finish(last_timestamp, last_closes, end_commission)`. A feed
        with no post-warm-up bars does none of that: no marks, no `on_end`, no
        `finish`.

        A batch holds only the symbols that have a bar at that timestamp
        (decision 6). A symbol absent from a batch is skipped there: no bar is
        appended, no broker gets `on_bar` for it, none of its algos is updated
        (so `update_frontend` is not called either), the sink gets no `on_bar`
        for it, and the ledger is marked with the present closes only (the
        absent symbol keeps its last mark). Its algos next see a frame whose
        last two rows are more than a minute apart; nothing is invented. A
        batch with no known symbol at all is ignored. `on_end` gets the last
        bar of each symbol that had any, in feed symbol order, and
        `ledger.finish` runs at the last timestamp with those closes; a symbol
        whose last bar is earlier than that has its synthetic close stamped at
        its own last bar (`symbol_times`), at its own last close.
        """
        if self._ran:
            raise RuntimeError("run_backtest() can only be called once per Engine")
        self._ran = True
        stores = {symbol: BarStore(self._feed.warmup(symbol)) for symbol in self._symbols}
        wants_algo_data = self._sink.wants_algo_data
        last_time = None
        last_bars: dict[str, Bar] = {}
        last_times: dict[str, Any] = {}
        for timestamp, bars in self._feed:
            present = [symbol for symbol in self._symbols if symbol in bars]
            if not present:
                continue  # a feed should never yield this; nothing happened at this time
            last_time = timestamp
            for symbol in present:
                stores[symbol].append(bars[symbol])
                last_bars[symbol] = bars[symbol]
                last_times[symbol] = timestamp
            for broker in self._bar_brokers:
                for symbol in present:
                    broker.on_bar(symbol, bars[symbol])
            for symbol in present:
                algo_data: list | None = [] if wants_algo_data else None
                for algo in self._by_symbol[symbol]:
                    algo.update(stores[symbol].view())  # type: ignore[attr-defined]
                    if algo_data is not None:
                        algo_data.append(algo.update_frontend())  # type: ignore[attr-defined]
                self._sink.on_bar(symbol, bars[symbol], algo_data)
            if self._ledger is not None:
                self._ledger.mark(timestamp, {s: bars[s].close for s in present})
        if last_time is None:
            return
        final = {s: last_bars[s] for s in self._symbols if s in last_bars}
        for broker in self._bar_brokers:
            broker.on_end(dict(final))
        if self._ledger is not None:
            closes = {s: b.close for s, b in final.items()}
            # A symbol whose data ended before the run's last timestamp is closed at
            # its own last bar's time. When every symbol ended at the last timestamp
            # (aligned data) the call is the same as it always was.
            early = {s: t for s, t in last_times.items() if t != last_time}
            if early:
                self._ledger.finish(last_time, closes, self._end_commission,
                                    symbol_times=early)
            else:
                self._ledger.finish(last_time, closes, self._end_commission)
