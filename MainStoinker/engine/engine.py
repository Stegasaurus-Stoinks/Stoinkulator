"""The engine: runs algos over a bar feed with every dependency passed in.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`. It never sleeps, starts threads or reads
global state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .bars import BarStore
from .broker import Broker
from .feed import BarFeed
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
    """

    def __init__(
        self,
        config: EngineConfig,
        feed: BarFeed,
        broker: Broker,
        sink: EventSink,
        algos: Sequence[object],
    ) -> None:
        self._config = config
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
        for algo in self._algos:
            algo.broker = broker  # type: ignore[attr-defined]
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
        """
        if self._ran:
            raise RuntimeError("run_backtest() can only be called once per Engine")
        self._ran = True
        stores = {symbol: BarStore(self._feed.warmup(symbol)) for symbol in self._symbols}
        wants_algo_data = self._sink.wants_algo_data
        for _timestamp, bars in self._feed:
            for symbol in self._symbols:
                stores[symbol].append(bars[symbol])
            for symbol in self._symbols:
                algo_data: list | None = [] if wants_algo_data else None
                for algo in self._by_symbol[symbol]:
                    algo.update(stores[symbol].view())  # type: ignore[attr-defined]
                    if algo_data is not None:
                        algo_data.append(algo.update_frontend())  # type: ignore[attr-defined]
                self._sink.on_bar(symbol, bars[symbol], algo_data)
