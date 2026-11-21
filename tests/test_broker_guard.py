"""Tests for the broker port guard and its wiring into IBapi.connect.

No test here may open a socket: EClient.connect is always replaced by a
recorder before IBapi.connect is called.
"""

import pytest
from ibapi.client import EClient

from MainStoinker.DataCollection.apiApi import IBapi
from MainStoinker.Util.broker_guard import (
    ALLOW_LIVE_ENV,
    BrokerPortRefused,
    check_broker_port,
)

OPT_IN = {"STOINK_ALLOW_LIVE_PORT": "1"}


def test_env_constant():
    assert ALLOW_LIVE_ENV == "STOINK_ALLOW_LIVE_PORT"


@pytest.mark.parametrize("port", [7497, 4002])
def test_paper_ports_allowed_without_env(port):
    check_broker_port(port, {})


@pytest.mark.parametrize("port", [7496, 4001])
def test_known_live_ports_refused_without_env(port):
    with pytest.raises(BrokerPortRefused):
        check_broker_port(port, {})


@pytest.mark.parametrize("port", [5000, 0, 7498, -1])
def test_unknown_port_refused_without_env(port):
    with pytest.raises(BrokerPortRefused):
        check_broker_port(port, {})


def test_refusal_names_env_var_and_port():
    with pytest.raises(BrokerPortRefused) as exc:
        check_broker_port(7496, {})
    msg = str(exc.value)
    assert "STOINK_ALLOW_LIVE_PORT" in msg
    assert "7496" in msg
    assert "not a paper-trading port" in msg


@pytest.mark.parametrize("port", [7496, 4001, 5000])
def test_non_paper_ports_allowed_with_opt_in(port):
    check_broker_port(port, OPT_IN)


@pytest.mark.parametrize("value", ["0", "", "true", "yes", " 1", "01"])
def test_opt_in_requires_exact_value_1(value):
    with pytest.raises(BrokerPortRefused):
        check_broker_port(7496, {"STOINK_ALLOW_LIVE_PORT": value})


def test_refusal_is_not_an_oserror():
    assert not issubclass(BrokerPortRefused, OSError)
    assert issubclass(BrokerPortRefused, RuntimeError)


def test_numeric_string_port_is_normalised():
    check_broker_port("7497", {})
    with pytest.raises(BrokerPortRefused):
        check_broker_port("7496", {})
    check_broker_port("7496", OPT_IN)


class LyingInt(int):
    """An int subclass worth 7496 that claims to equal the paper port."""

    def __eq__(self, other):
        return other == 7497 or int(self) == other

    def __hash__(self):
        return hash(7497)


@pytest.mark.parametrize(
    "port",
    [
        "abc", "", None, True, False, [7497], b"7497",
        7497.5, 7497.0, float("nan"), "7497.0",
        " 7497", "7497 ", "7497\n", "+7497", "-7497", "7_497", "0x1D49", "７４９７",
        LyingInt(7496), LyingInt(7497),
    ],
)  # fmt: skip
def test_non_plain_port_is_refused_even_with_opt_in(port):
    # Only an exact int or an ASCII-digit string is a port. Everything else
    # is refused, so the value checked is always the value the socket uses.
    with pytest.raises(BrokerPortRefused):
        check_broker_port(port, OPT_IN)


def test_refusal_shows_a_wrong_opt_in_value():
    # cmd's `set VAR=1 && ...` stores "1 " with a trailing space.
    with pytest.raises(BrokerPortRefused) as exc:
        check_broker_port(7496, {"STOINK_ALLOW_LIVE_PORT": "1 "})
    assert "'1 '" in str(exc.value)


# ---- wiring ----


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return "sentinel"


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(EClient, "connect", lambda self, *a: rec(self, *a))
    monkeypatch.delenv("STOINK_ALLOW_LIVE_PORT", raising=False)
    return rec


def test_ibapi_overrides_connect():
    assert IBapi.connect is not EClient.connect
    assert "connect" in vars(IBapi)


@pytest.mark.parametrize("port", [7496, 4001, 5000])
def test_ibapi_refuses_live_port_before_any_connection(recorder, port):
    with pytest.raises(BrokerPortRefused):
        IBapi().connect("127.0.0.1", port, 1)
    assert recorder.calls == []


def test_ibapi_paper_port_delegates_unchanged(recorder):
    api = IBapi()
    result = api.connect("127.0.0.1", 7497, 123)
    assert result == "sentinel"
    assert recorder.calls == [(api, "127.0.0.1", 7497, 123)]


def test_ibapi_live_port_delegates_with_opt_in(recorder, monkeypatch):
    monkeypatch.setenv("STOINK_ALLOW_LIVE_PORT", "1")
    api = IBapi()
    api.connect("127.0.0.1", 7496, 5)
    assert recorder.calls == [(api, "127.0.0.1", 7496, 5)]


def test_super_connect_resolves_to_eclient():
    # The override's super() must reach EClient.connect, not another class.
    mro = IBapi.__mro__
    after = mro[mro.index(IBapi) + 1 :]
    owners = [c for c in after if "connect" in vars(c)]
    assert owners[0] is EClient


def test_guard_reads_process_environment(recorder, monkeypatch):
    api = IBapi()
    with pytest.raises(BrokerPortRefused):
        api.connect("127.0.0.1", 7496, 1)
    monkeypatch.setenv("STOINK_ALLOW_LIVE_PORT", "1")
    api.connect("127.0.0.1", 7496, 1)
    assert len(recorder.calls) == 1
    monkeypatch.delenv("STOINK_ALLOW_LIVE_PORT")
    with pytest.raises(BrokerPortRefused):
        api.connect("127.0.0.1", 7496, 1)
    assert len(recorder.calls) == 1
