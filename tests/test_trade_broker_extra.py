"""Extra checks on Trade with an optional broker (A3 tester additions).

Covers what test_trade_broker.py does not: every public Trade method driven on
a broker trade with LiveTrading forced on, seeded random-walk equivalence of
the broker and no-broker paths, a SimBroker shared between algos, and the
number of random.randint calls per trade on all three paths.
"""
import logging
import random
from pathlib import Path

import pandas as pd
import pytest

import MainStoinker.TradeTools.trade as trade_module
from MainStoinker.engine.broker import SimBroker
from MainStoinker.MainStuff.Globals import config
from MainStoinker.TradeTools.trade import Trade
from tests import harness
from tests.fake_ibapi import RecordingIBapi

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0
LOG = logging.getLogger("test-extra")


def bar(close, high=None, low=None, minutes=1):
    return {
        "date": T0 + pd.Timedelta(minutes=minutes),
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }


def make_trade(direction=LONG, price=100.0, broker=None):
    return Trade("AAPL", 10, "t0", price, T0, direction, LOG, broker=broker)


class WatchedIBapi(RecordingIBapi):
    """Recording fake that also records reads of the attributes the live
    branches use to look at the broker."""

    def __init__(self):
        self.reads = []
        self._oo = None
        super().__init__()

    @property
    def all_openorders(self):
        self.reads.append("all_openorders")
        return self._oo

    @all_openorders.setter
    def all_openorders(self, value):
        self._oo = value


@pytest.fixture
def live_watched(monkeypatch):
    fake_api = WatchedIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    monkeypatch.setattr(random, "randint", lambda a, b: 7)
    original = config.LiveTrading
    assert original is False
    object.__setattr__(config, "LiveTrading", True)
    fake_api.reads.clear()
    try:
        yield fake_api
    finally:
        object.__setattr__(config, "LiveTrading", original)


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_every_public_method_with_broker_never_touches_ibapi(live_watched, direction):
    sign = 1 if direction else -1
    broker = SimBroker()

    trade = make_trade(direction, broker=broker)
    assert trade.in_position() is True  # issue 5: open sets status/position
    assert trade.get_status() == "Open"
    trade.set_stopDelta(1.5)
    trade.set_stopLossType("Fixed")
    assert trade.check_stoploss(bar(100 + 0.1 * sign)) == 1
    trade.set_stopLossType("Trailing")
    assert trade.check_stoploss(bar(100 + 5 * sign)) == 1  # trailing move
    trade.create_tp(100 + 9 * sign, 10)
    assert trade.check_tp(bar(100 + 5 * sign)) == 0
    trade.update_tp(100 + 10 * sign)  # issue 7: quantity defaults to volume
    assert trade.tp == 100 + 10 * sign
    assert trade.check_tp(bar(100 + 9 * sign, high=111, low=89)) == 1
    trade.close_position(100 + 6 * sign, bar(100)["date"])
    assert trade.in_position() is False
    assert trade.get_status() == "Closed"
    assert trade.get_profit() == 60  # issue 4: (6 * sign) * sign * volume 10
    trade.get_stats()
    assert trade.to_json()["profit"] == 60  # issue 3 and 4

    stopped = make_trade(direction, broker=broker)
    assert stopped.check_stoploss(bar(100 - 3 * sign)) == 0

    assert live_watched.calls == []
    assert live_watched.reads == []
    assert not hasattr(trade, "contract")
    assert not hasattr(trade, "parentOrder")


def _walk(seed, direction, n=200):
    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        price = round(price + rng.gauss(0, 0.45), 2)
        spread = round(rng.random() * 0.4, 2)
        out.append(bar(price, high=price + spread, low=price - spread, minutes=i + 1))
    return out


def _state(t):
    return {
        a: (getattr(t, a) if hasattr(t, a) else "<absent>")
        for a in ("stopPrice", "stopDelta", "tp", "closePrice", "closeTime", "status", "position")
    }


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("direction", [LONG, SHORT])
@pytest.mark.parametrize("stop_type", ["Trailing", "Fixed"])
@pytest.mark.parametrize("with_tp", [True, False])
def test_random_walk_equivalence_and_call_log(seed, direction, stop_type, with_tp):
    sign = 1 if direction else -1
    broker = SimBroker()
    plain = make_trade(direction)
    brk = make_trade(direction, broker=broker)
    for t in (plain, brk):
        t.set_stopLossType(stop_type)
        if stop_type == "Fixed":
            t.set_stopPrice(100 - 2.5 * sign)
        if with_tp:
            t.create_tp(100 + 4 * sign, 10)
    assert _state(plain) == _state(brk)

    stop_changes = 1 if stop_type == "Fixed" else 0
    closed = False
    for b in _walk(seed, direction):
        before = plain.stopPrice
        outcome = []
        for t in (plain, brk):
            r = []
            if with_tp:
                r.append(t.check_tp(b))
                if r[-1]:
                    outcome.append(r)
                    continue
            r.append(t.check_stoploss(b))
            outcome.append(r)
        assert outcome[0] == outcome[1]
        assert _state(plain) == _state(brk)
        if plain.stopPrice != before:
            stop_changes += 1
        if (with_tp and outcome[0][0] == 1) or outcome[0][-1] == 0:
            closed = True
            break

    methods = [c.method for c in broker.calls]
    assert methods.count("open_position") == 1
    assert methods.count("modify_stop") == stop_changes
    assert methods.count("set_take_profit") == (1 if with_tp else 0)
    assert methods.count("close_position") == (1 if closed else 0)
    assert hasattr(plain, "closePrice") == closed


def _calls_for_ref(broker, ref):
    return [c for c in broker.calls if c.ref == ref]


def test_shared_simbroker_refs_are_distinct_per_trade():
    cfgs = [
        {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"},
        {"idname": "MSFT-10/30EMA", "ticker": "MSFT", "short": "10", "long": "30"},
    ]
    for sym in ("AAPL", "MSFT"):
        harness.stage_offline_data(Path.cwd(), [sym], max_rows=1200)
    broker = SimBroker()
    algos = harness.run_offline_backtest("EMACrossing", cfgs, broker=broker)
    trades = [t for a in algos for t in a.trades]
    assert trades, "expected at least one trade"
    refs = [t.brokerRef for t in trades]
    assert len(set(refs)) == len(refs)
    opens = [c for c in broker.calls if c.method == "open_position"]
    assert len(opens) == len(trades)
    by_ref = {t.brokerRef: t for t in trades}
    for c in broker.calls:
        assert c.ref in by_ref
        t = by_ref[c.ref]
        if c.method == "open_position":
            assert c.args["symbol"] == t.symbol
            assert c.args["time"] == t.openTime
        if c.method == "close_position":
            assert c.args["price"] == t.closePrice
            assert c.args["time"] == t.closeTime


def _count_randint(monkeypatch):
    n = {"calls": 0}

    def counting(a, b):
        n["calls"] += 1
        return 7

    monkeypatch.setattr(random, "randint", counting)
    return n


def test_randint_called_once_per_trade_without_broker(monkeypatch):
    n = _count_randint(monkeypatch)
    make_trade(LONG)
    assert n["calls"] == 1


def test_randint_called_once_per_trade_with_broker(monkeypatch):
    n = _count_randint(monkeypatch)
    make_trade(LONG, broker=SimBroker())
    assert n["calls"] == 1


def test_randint_called_once_per_trade_live(monkeypatch):
    fake_api = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    n = _count_randint(monkeypatch)
    object.__setattr__(config, "LiveTrading", True)
    try:
        make_trade(LONG)
    finally:
        object.__setattr__(config, "LiveTrading", False)
    assert n["calls"] == 1
