"""Trade with an optional broker (task A3).

A trade given a broker reports to it and otherwise behaves exactly like the
simulated path. It must never reach `ibapi`, even when LiveTrading is on.
"""
import logging
import random

import pandas as pd
import pytest

import MainStoinker.TradeTools.trade as trade_module
from MainStoinker.engine.broker import SimBroker
from MainStoinker.MainStuff import main_utils
from MainStoinker.MainStuff.Globals import config
from MainStoinker.TradeTools.trade import Trade
from tests.fake_ibapi import RecordingIBapi

T0 = pd.Timestamp("2024-08-19 07:00:00")
LONG, SHORT = 1, 0
LOG = logging.getLogger("test")


def bar(close, high=None, low=None, minutes=1):
    return {
        "date": T0 + pd.Timedelta(minutes=minutes),
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }


def make_trade(direction=LONG, price=100.0, broker=None):
    return Trade("AAPL", 10, "t0", price, T0, direction, LOG, broker=broker)


def summary(broker):
    return [(c.method, c.ref, dict(c.args)) for c in broker.calls]


@pytest.fixture
def live_fake(monkeypatch):
    """Recording fake in place of trade.ibapi, then live mode forced on."""
    fake_api = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake_api)
    assert trade_module.ibapi is fake_api
    monkeypatch.setattr(random, "randint", lambda a, b: 7)
    original = config.LiveTrading
    assert original is False
    object.__setattr__(config, "LiveTrading", True)
    try:
        yield fake_api
    finally:
        object.__setattr__(config, "LiveTrading", original)


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_broker_takes_precedence_over_live_flag(live_fake, direction):
    sign = 1 if direction else -1
    broker = SimBroker()

    trade = make_trade(direction, broker=broker)
    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(100 - 2 * sign)
    trade.create_tp(100 + 3 * sign, 10)
    trade.create_tp(100 + 4 * sign, 10)
    assert trade.check_stoploss(bar(100 + 0.5 * sign)) == 1
    assert trade.check_tp(bar(100 + 0.5 * sign)) == 0

    # trailing move on a second trade
    trailer = make_trade(direction, broker=broker)
    assert trailer.check_stoploss(bar(100 + 2.5 * sign)) == 1

    # manual stop-out
    stopped = make_trade(direction, broker=broker)
    assert stopped.check_stoploss(bar(100 - 2 * sign)) == 0

    # take-profit hit
    tp_trade = make_trade(direction, broker=broker)
    tp_trade.create_tp(100 + 3 * sign, 10)
    assert tp_trade.check_tp(bar(100 + 3 * sign, high=104, low=96)) == 1

    trade.close_position(100.5, bar(100.5)["date"])

    assert live_fake.calls == []
    methods = [c.method for c in broker.calls]
    assert methods.count("open_position") == 4
    assert methods.count("modify_stop") == 2
    assert methods.count("set_take_profit") == 3
    assert methods.count("close_position") == 3
    for t in (trade, trailer, stopped, tp_trade):
        assert not hasattr(t, "stopOrder")
        assert not hasattr(t, "contract")
        assert t.tpOrder == 0


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_simbroker_sequence_for_fixed_stop_tp_flow(monkeypatch, direction):
    monkeypatch.setattr(random, "randint", lambda a, b: 7)
    sign = 1 if direction else -1
    broker = SimBroker()

    trade = make_trade(direction, broker=broker)
    ref = trade.brokerRef
    trade.set_stopLossType("Fixed")
    trade.set_stopPrice(100 - 2 * sign)
    trade.create_tp(100 + 3 * sign, 10)
    trade.create_tp(100 + 4 * sign, 10)
    trade.set_stopLossType("Trailing")
    price = 100 + 5 * sign
    assert trade.check_stoploss(bar(price)) == 1  # trailing move
    new_stop = price - trade.stopDelta * sign
    trade.close_position(price, bar(price)["date"])

    assert summary(broker) == [
        (
            "open_position",
            ref,
            dict(
                symbol="AAPL",
                volume=10,
                direction=direction,
                price=100.0,
                time=T0,
                limit=False,
                stop_price=100 - 1.0 * sign,
                oca_group="ocat07",
            ),
        ),
        ("modify_stop", ref, dict(stop_price=100 - 2 * sign)),
        ("set_take_profit", ref, dict(price=100 + 3 * sign, quantity=10)),
        ("set_take_profit", ref, dict(price=100 + 4 * sign, quantity=10)),
        ("modify_stop", ref, dict(stop_price=new_stop)),
        ("close_position", ref, dict(price=price, time=bar(price)["date"])),
    ]


class StoppedOutBroker(SimBroker):
    def stop_is_working(self, ref):
        return False


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_stop_not_working_returns_0_without_closing(direction):
    broker = StoppedOutBroker()
    trade = make_trade(direction, broker=broker)
    # Price far through the manual stop: it would close if the check fell through.
    assert trade.check_stoploss(bar(50.0 if direction else 150.0)) == 0
    assert [c.method for c in broker.calls] == ["open_position"]
    assert not hasattr(trade, "closePrice")
    # Like the live "stop gone" path (which returns 0 and does not touch
    # status), nothing local changes. The broker path sets status and position
    # on open (issue 5), and the early return leaves them alone.
    assert trade.status == "Open"
    assert trade.position is True


def _scenario_default_stops(make):
    return [make(LONG).stopPrice, make(SHORT).stopPrice], None


def _scenario_trailing_follows_and_never_loosens(make):
    t = make(LONG)
    return [t.check_stoploss(bar(102.5)), t.check_stoploss(bar(101.8))], t


def _scenario_trailing_follows_short(make):
    t = make(SHORT)
    return [t.check_stoploss(bar(97.5)), t.check_stoploss(bar(98.2))], t


def _scenario_fixed_does_not_trail(make):
    t = make(LONG)
    t.set_stopLossType("Fixed")
    return [t.check_stoploss(bar(105.0))], t


def _scenario_long_stop_hit(make):
    t = make(LONG)
    return [t.check_stoploss(bar(98.0))], t


def _scenario_short_stop_hit(make):
    t = make(SHORT)
    return [t.check_stoploss(bar(102.0))], t


def _scenario_tp_closes_long(make):
    t = make(LONG)
    t.create_tp(103.0, 10)
    return [t.check_tp(bar(102.0, high=102.5)), t.check_tp(bar(102.8, high=103.2))], t


def _scenario_tp_closes_short(make):
    t = make(SHORT)
    t.create_tp(97.0, 10)
    return [t.check_tp(bar(98.0, low=97.5)), t.check_tp(bar(97.2, low=96.8))], t


def _scenario_long_profit(make):
    t = make(LONG)
    t.close_position(104.0, T0 + pd.Timedelta(minutes=5))
    return [t.get_profit()], t


def _scenario_short_fixed_stop_by_algo(make):
    t = make(SHORT)
    t.set_stopLossType("Fixed")
    t.set_stopPrice(103.0)
    results = [
        t.check_stoploss(bar(95.0)),
        t.check_stoploss(bar(103.0)),
        t.check_stoploss(bar(103.5)),
    ]
    return results, t


SCENARIOS = [
    _scenario_default_stops,
    _scenario_trailing_follows_and_never_loosens,
    _scenario_trailing_follows_short,
    _scenario_fixed_does_not_trail,
    _scenario_long_stop_hit,
    _scenario_short_stop_hit,
    _scenario_tp_closes_long,
    _scenario_tp_closes_short,
    _scenario_long_profit,
    _scenario_short_fixed_stop_by_algo,
]

ATTRS = ("stopPrice", "stopDelta", "tp", "closePrice", "closeTime", "status", "position")


def _observe(trade):
    if trade is None:
        return None
    return {a: (getattr(trade, a) if hasattr(trade, a) else "<absent>") for a in ATTRS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__[10:])
def test_sim_scenarios_identical_with_and_without_broker(scenario):
    plain_results, plain_trade = scenario(lambda d: make_trade(d))
    broker = SimBroker()
    brk_results, brk_trade = scenario(lambda d: make_trade(d, broker=broker))
    assert brk_results == plain_results
    assert _observe(brk_trade) == _observe(plain_trade)
    if brk_trade is not None:
        assert plain_trade.broker is None
        assert brk_trade.broker is broker


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_known_defects_unchanged_with_broker(direction):
    trade = make_trade(direction, broker=SimBroker())
    # issue 5: open sets status and position
    assert trade.status == "Open"
    assert trade.position is True
    assert not hasattr(trade, "stopOrder")
    # issue 3: to_json no longer needs stopOrder
    assert trade.to_json()["stoplossID"] is None
    trade.close_position(104.0, T0 + pd.Timedelta(minutes=5))
    assert trade.to_json()["stoplossID"] is None
    # issue 4: profit is (close - open) * volume, sign flipped for a short
    assert trade.to_json()["profit"] == (40.0 if direction else -40.0)


def test_broker_argument_is_keyword_compatible():
    positional = Trade("AAPL", 10, "t0", 100.0, T0, LONG, LOG)
    assert positional.broker is None
    with_limit = Trade("AAPL", 10, "t0", 100.0, T0, LONG, LOG, True)
    assert with_limit.broker is None
    assert with_limit.limitOrder is True
    assert not hasattr(positional, "brokerRef")


def test_parent_algo_passes_its_broker_to_trades():
    cfg = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}
    algo = main_utils.algo_starter("EMACrossing", cfg)
    assert algo.broker is None
    algo.curStockData = pd.Series({"close": 100.0, "date": T0})

    plain = algo.open_trade(10, 1)
    assert plain.broker is None
    assert not hasattr(plain, "brokerRef")

    broker = SimBroker()
    algo.broker = broker
    trade = algo.open_trade(10, 1)
    assert trade.broker is broker
    assert [c.method for c in broker.calls] == ["open_position"]
    assert broker.calls[0].ref == trade.brokerRef
    assert broker.calls[0].args["symbol"] == "AAPL"
