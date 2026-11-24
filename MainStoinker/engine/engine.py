"""The engine: runs algos over a bar feed with every dependency passed in.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`. It never sleeps, starts threads or reads
global state.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd

from .bars import BarStore
from .broker import Broker
from .feed import Bar, BarFeed
from .gaprule import GapGuardBroker, GapRule, GapRuleReport
from .ledger import Ledger
from .router import BarAwareBroker, BrokerRouter, SingleBrokerRouter
from .sink import EventSink


@dataclass(frozen=True)
class EngineConfig:
    """Engine settings. Empty for now: `run_backtest` needs none.

    Fields arrive with later tasks (live mode, logging, ...). Whether algo
    data is published is decided by the sink (`sink.wants_algo_data`).
    """


@dataclass(frozen=True)
class LivePosition:
    """A position still open when a live session ended (`Engine.end_live`).

    Read from the ledger. `quantity` and `entry_price` are the open quantity
    and its average entry price. `entry_time` is the time of the position's
    first entry fill. `stop` and `target` are the working protective orders as
    the ledger's order events show them (a modification counts); None when the
    broker records no such order (`SimBroker` records none) or it was
    cancelled. They are "if known", never a guess.
    """

    account: str
    broker_ref: Hashable
    symbol: str
    side: Literal["long", "short"]
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime | None
    stop: Decimal | None
    target: Decimal | None


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

    One engine serves one mode, once: `run_backtest` (pull, whole feed) or the
    live session `start_live` / `step` / `end_live` (push, see those methods).
    Mixing them raises `RuntimeError`.
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
        self._gap_rule = GapRule({symbol: None for symbol in symbols})
        self._routed: tuple[tuple[object, Broker], ...] = tuple(routed)
        # Per-run state, set by `_begin`.
        self._stores: dict[str, BarStore] = {}
        self._wants_algo_data = False
        self._last_time: Any = None
        self._last_bars: dict[str, Bar] = {}
        self._last_times: dict[str, Any] = {}
        # Live session state (see `start_live`).
        self._mode: Literal["backtest", "live"] | None = None
        self._live_state: Literal["running", "failed", "ended"] | None = None
        self._live_failure: BaseException | None = None
        self._live_last_time: Any = None
        self._on_step: Callable[[Any, tuple[str, ...]], None] | None = None

    @property
    def algos(self) -> tuple[object, ...]:
        """The algos in registration order (read-only)."""
        return self._algos

    @property
    def gap_report(self) -> GapRuleReport:
        """What the gap rule counted and refused so far (see `gaprule`)."""
        return self._gap_rule.report()

    def run_backtest(self) -> None:
        """Replay the whole feed once. A second call raises `RuntimeError`.

        Algos are stateful, so a re-run would double their trades.

        The gap rule (see `gaprule`) applies throughout: on a bar that follows a
        gap for its symbol an algo's entry orders are refused, everything else
        works. The refusals are in `gap_report` afterwards.

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
        if self._mode == "live":
            raise RuntimeError("this Engine is in live mode: run_backtest() is not available")
        self._ran = True
        self._mode = "backtest"
        # The gap rule (gaprule.py): while the replay runs, each algo talks to its
        # broker through a guard that refuses entries on a bar that follows a gap.
        # The algo's own broker is put back afterwards.
        for algo, routed_broker in self._routed:
            algo.broker = GapGuardBroker(  # type: ignore[attr-defined]
                routed_broker, str(getattr(algo, "name", algo)), self._gap_rule)
        try:
            self._replay()
        finally:
            for algo, routed_broker in self._routed:
                algo.broker = routed_broker  # type: ignore[attr-defined]

    # --- live session ------------------------------------------------------

    def start_live(
        self,
        warmup: Mapping[str, pd.DataFrame],
        *,
        on_step: Callable[[Any, tuple[str, ...]], None] | None = None,
    ) -> None:
        """Begin a live session: the push-style counterpart of `run_backtest`.

        `warmup` must hold a frame (with a `date` column) for every feed
        symbol, possibly empty; this is checked before anything is touched, a
        `ValueError` naming the problem otherwise. The feed is used only for
        its symbols. Installs the gap rule's `GapGuardBroker` on every algo
        (`end_live` puts the algos' own brokers back) and starts the gap rule
        from each frame's last bar, as `run_backtest` does. `on_step(timestamp,
        present_symbols)`, if given, is called after every non-empty `step`.

        One mode, once: raises `RuntimeError` after `run_backtest` or after an
        earlier `start_live` (also an ended session).
        """
        if self._mode is not None:
            raise RuntimeError(f"this Engine already ran in {self._mode} mode: "
                               "an Engine serves one mode, once")
        missing = [s for s in self._symbols if s not in warmup]
        if missing:
            raise ValueError(f"start_live: no warm-up frame for {missing} "
                             f"(every feed symbol needs one: {self._symbols})")
        bad = [s for s in self._symbols if "date" not in warmup[s].columns]
        if bad:
            raise ValueError(f"start_live: the warm-up frame of {bad} has no 'date' column")
        self._begin(warmup)
        try:
            self._guard_algos()
        except BaseException:
            self._unguard_algos()
            raise
        self._mode = "live"
        self._on_step = on_step
        self._live_last_time = None
        self._live_failure = None
        self._live_state = "running"

    def _guard_algos(self) -> None:
        for algo, routed_broker in self._routed:
            algo.broker = GapGuardBroker(  # type: ignore[attr-defined]
                routed_broker, str(getattr(algo, "name", algo)), self._gap_rule)

    def _unguard_algos(self) -> None:
        for algo, routed_broker in self._routed:
            algo.broker = routed_broker  # type: ignore[attr-defined]

    def step(self, timestamp: Any, bars: Mapping[str, Bar]) -> None:
        """Process one closed-bar batch of a live session (same rules as a backtest batch).

        Refused with nothing applied, as a `ValueError` (the engine stays
        usable): a timestamp not strictly after the previous step's, a symbol
        not in the feed, a bar whose `date` differs from `timestamp`. An empty
        batch is a no-op (not even its timestamp is recorded). `RuntimeError`
        if the session is not running.

        Failed state: if anything raises once the batch is being applied (an
        algo, a broker's `on_bar`, the ledger, the sink, or `on_step`), the
        batch may be half applied and must NOT be retried. The engine is then
        FAILED: every later `step` raises `RuntimeError` chaining the original
        error. `end_live` still works so the caller can close the run. The
        original exception propagates from this call. `on_step` runs after the
        step completed; its exception also fails the engine.
        """
        self._require_live("step")
        if self._live_state == "failed":
            raise RuntimeError("the live session failed in an earlier step and cannot "
                               "continue; call end_live()") from self._live_failure
        if not bars:
            return
        unknown = [s for s in bars if s not in self._by_symbol]
        if unknown:
            raise ValueError(f"step: unknown symbol(s) {unknown} (feed symbols: {self._symbols})")
        wrong = [s for s, b in bars.items() if b.date != timestamp]
        if wrong:
            raise ValueError(f"step: bar date of {wrong} differs from the batch timestamp "
                             f"{timestamp}")
        if self._live_last_time is not None and not timestamp > self._live_last_time:
            raise ValueError(f"step: timestamp {timestamp} is not after the previous step's "
                             f"{self._live_last_time}")
        try:
            self._live_last_time = timestamp
            present = self._step(timestamp, bars)
            if self._on_step is not None:
                self._on_step(timestamp, tuple(present))
        except BaseException as exc:
            self._live_state = "failed"
            self._live_failure = exc
            raise

    def end_live(self) -> tuple[LivePosition, ...]:
        """End the live session; return the positions still open.

        Works in the running and the failed state. Puts the algos' own brokers
        back, then calls `on_end(last bars)` once on each bar-aware broker (as
        a backtest's end does: pending entries are cancelled, positions the
        algo was exiting are filled at their requested price) unless no bar was
        ever stepped. Does NOT call `Ledger.finish`: open positions stay open
        and nothing synthetic is closed. The ledger's last equity point is the
        mark of the last step; it does not include fills made by `on_end`
        (`ledger.equity()` does). The result is read from the ledger after
        `on_end`, in account order; empty without a ledger.

        A second call raises `RuntimeError` (the session is over, and a second
        `on_end` must never happen); so does a call before `start_live`.
        """
        self._require_live("end_live")
        self._live_state = "ended"
        try:
            self._unguard_algos()
            self._end_bars()
        finally:
            self._on_step = None
        return self._open_positions()

    def _require_live(self, what: str) -> None:
        if self._mode != "live":
            raise RuntimeError(f"{what}() needs start_live() first")
        if self._live_state == "ended":
            raise RuntimeError(f"{what}(): the live session has ended")

    def _open_positions(self) -> tuple[LivePosition, ...]:
        ledger = self._ledger
        if ledger is None:
            return ()
        opened: dict[tuple[str, Hashable], datetime] = {}
        for fill in ledger.fills:
            opened.setdefault((fill.account, fill.broker_ref), fill.time)
        # Working protective orders per position, from the order events in order.
        levels: dict[tuple[str, Hashable], dict[str, Decimal | None]] = {}
        placed: dict[int, tuple[tuple[str, Hashable], str]] = {}
        for event in ledger.orders:
            if event.kind == "placed" and event.order_type in ("stop", "target"):
                key = (event.account, event.broker_ref)
                placed[event.order_id] = (key, event.order_type)
                levels.setdefault(key, {})[event.order_type] = event.price
            elif event.order_id in placed:
                key, kind = placed[event.order_id]
                if event.kind == "modified" and event.price is not None:
                    levels[key][kind] = event.price
                elif event.kind == "cancelled":
                    levels[key][kind] = None
        result = []
        for pos in ledger.open_positions():
            key = (pos.account, pos.broker_ref)
            lv = levels.get(key, {})
            result.append(LivePosition(
                pos.account, pos.broker_ref, pos.symbol, pos.side, pos.quantity,
                pos.average_price, opened.get(key), lv.get("stop"), lv.get("target")))
        return tuple(result)

    def _replay(self) -> None:
        self._begin({symbol: self._feed.warmup(symbol) for symbol in self._symbols})
        for timestamp, bars in self._feed:
            self._step(timestamp, bars)
        self._end_bars()
        self._finish_ledger()

    def _begin(self, warmups: Mapping[str, pd.DataFrame]) -> None:
        """Seed the per-run state: one `BarStore` per symbol from its warm-up frame.

        `warmups` must hold every feed symbol. Starts the gap rule from each
        frame's last bar time (None for an empty frame) and clears the
        last-bar bookkeeping. Calls nothing on the brokers, ledger or sink.
        """
        stores = {symbol: BarStore(warmups[symbol]) for symbol in self._symbols}
        self._gap_rule.start({s: (w["date"].iloc[-1] if len(w) else None)
                              for s, w in warmups.items()})
        self._stores = stores
        self._wants_algo_data = self._sink.wants_algo_data
        self._last_time = None
        self._last_bars = {}
        self._last_times = {}

    def _step(self, timestamp: Any, bars: Mapping[str, Bar]) -> list[str]:
        """Process one timestamp's batch; return the feed symbols that were present.

        Needs `_begin` first. Order: append every present bar (and tell the gap
        rule); `on_bar` on every bar-aware broker for each present symbol; update
        each present symbol's algos (the gap rule's `end_bar` runs in a `finally`)
        and publish to the sink; mark the ledger with the present closes. A batch
        with no known symbol does nothing and returns `[]`. An exception from an
        algo propagates after `end_bar`. The algos' brokers are not touched here:
        the caller owns the `GapGuardBroker` wrapping.
        """
        present = [symbol for symbol in self._symbols if symbol in bars]
        if not present:
            return present  # a feed should never yield this; nothing happened at this time
        self._last_time = timestamp
        for symbol in present:
            self._stores[symbol].append(bars[symbol])
            self._last_bars[symbol] = bars[symbol]
            self._last_times[symbol] = timestamp
            self._gap_rule.on_bar(symbol, bars[symbol].date)
        for broker in self._bar_brokers:
            for symbol in present:
                broker.on_bar(symbol, bars[symbol])
        for symbol in present:
            algo_data: list | None = [] if self._wants_algo_data else None
            try:
                for algo in self._by_symbol[symbol]:
                    algo.update(self._stores[symbol].view())  # type: ignore[attr-defined]
                    if algo_data is not None:
                        algo_data.append(algo.update_frontend())  # type: ignore[attr-defined]
            finally:
                self._gap_rule.end_bar(symbol)
            self._sink.on_bar(symbol, bars[symbol], algo_data)
        if self._ledger is not None:
            self._ledger.mark(timestamp, {s: bars[s].close for s in present})
        return present

    def _last_bars_in_feed_order(self) -> dict[str, Bar]:
        return {s: self._last_bars[s] for s in self._symbols if s in self._last_bars}

    def _end_bars(self) -> None:
        """Call `on_end` once on each bar-aware broker with each symbol's last bar.

        Does nothing if no step has processed a bar. Does not touch the ledger.
        """
        if self._last_time is None:
            return
        final = self._last_bars_in_feed_order()
        for broker in self._bar_brokers:
            broker.on_end(dict(final))

    def _finish_ledger(self) -> None:
        """`Ledger.finish` at the last timestamp (backtest only).

        No-op without a ledger or without any processed bar.
        """
        if self._last_time is None or self._ledger is None:
            return
        final = self._last_bars_in_feed_order()
        closes = {s: b.close for s, b in final.items()}
        # A symbol whose data ended before the run's last timestamp is closed at
        # its own last bar's time. When every symbol ended at the last timestamp
        # (aligned data) the call is the same as it always was.
        early = {s: t for s, t in self._last_times.items() if t != self._last_time}
        if early:
            self._ledger.finish(self._last_time, closes, self._end_commission,
                                symbol_times=early)
        else:
            self._ledger.finish(self._last_time, closes, self._end_commission)
