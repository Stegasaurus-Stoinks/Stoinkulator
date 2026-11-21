"""The committed configuration must never be live.

The launch script pulls and runs whatever is on the branch, so a Config
default of LiveTrading=True merged by accident would trade real money.
These tests read the class defaults, not the instance the harness patches.
"""
import dataclasses

from MainStoinker.MainStuff.Globals import Config

DEFAULTS = {f.name: f.default for f in dataclasses.fields(Config)}


def test_live_trading_is_off_by_default():
    assert DEFAULTS["LiveTrading"] is False


def test_default_tws_port_is_the_paper_port():
    # TWS: 7497 paper / 7496 live. IB Gateway: 4002 paper / 4001 live.
    assert DEFAULTS["TWS_PORT"] in (7497, 4002)


def test_default_tws_host_is_local():
    assert DEFAULTS["TWS_HOST"] in ("127.0.0.1", "localhost")
