"""No test may ever open a broker connection (see tests/conftest.py)."""

import socket

import pytest
from ibapi import connection as ibapi_connection
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from MainStoinker.DataCollection.apiApi import IBapi
from tests.conftest import BrokerConnectionBlockedUnderPytest


@pytest.fixture
def socket_spy(monkeypatch):
    """Record, and refuse, any real socket connect attempted by the test."""
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("real socket connect attempted")

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket, "create_connection", spy)
    return calls


def test_connection_connect_raises_under_pytest(socket_spy):
    with pytest.raises(BrokerConnectionBlockedUnderPytest, match="127.0.0.1:7497"):
        ibapi_connection.Connection("127.0.0.1", 7497).connect()
    assert socket_spy == []


def test_eclient_connect_raises_on_paper_port(socket_spy):
    client = EClient(EWrapper())
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        client.connect("127.0.0.1", 7497, 1)
    assert socket_spy == []


def test_ibapi_instance_connect_raises(socket_spy):
    app = IBapi()
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        app.connect("127.0.0.1", 7497, 1)
    assert socket_spy == []


def test_block_ignores_live_opt_in_env(socket_spy, monkeypatch):
    monkeypatch.setenv("STOINK_ALLOW_LIVE_PORT", "1")
    client = EClient(EWrapper())
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        client.connect("127.0.0.1", 7496, 1)
    assert socket_spy == []


def test_blocked_error_is_not_an_oserror():
    assert not issubclass(BrokerConnectionBlockedUnderPytest, OSError)
    assert issubclass(BrokerConnectionBlockedUnderPytest, RuntimeError)
