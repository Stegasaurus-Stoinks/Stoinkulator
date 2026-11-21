"""Extra probes for the broker port guard: keyword calls and the real EClient path.

Connection is always replaced by a recorder; no socket can be opened.
"""

import pytest
from ibapi import client as ibclient

from MainStoinker.DataCollection import apiApi
from MainStoinker.DataCollection.apiApi import IBapi
from MainStoinker.Util.broker_guard import BrokerPortRefused


class FakeConnection:
    created = []

    def __init__(self, host, port):
        FakeConnection.created.append((host, port))

    def connect(self):
        raise AssertionError("connect must not be reached in these tests")


@pytest.fixture
def fake_connection(monkeypatch):
    FakeConnection.created = []
    monkeypatch.setattr(ibclient, "Connection", FakeConnection)
    monkeypatch.delenv("STOINK_ALLOW_LIVE_PORT", raising=False)
    return FakeConnection


def test_live_port_never_builds_a_connection_through_real_eclient(fake_connection):
    with pytest.raises(BrokerPortRefused):
        IBapi().connect("127.0.0.1", 7496, 1)
    assert fake_connection.created == []


def test_keyword_arguments_are_accepted_and_guarded(fake_connection):
    with pytest.raises(BrokerPortRefused):
        IBapi().connect(host="127.0.0.1", port=4001, clientId=1)
    assert fake_connection.created == []


def test_module_level_instance_uses_the_override():
    assert type(apiApi.ibapi).connect is IBapi.connect
