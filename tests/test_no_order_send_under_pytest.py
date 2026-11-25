"""No order frame can leave a process during a pytest run (see tests/conftest.py).

Nothing here opens a real connection: refused ports are refused before the real
connect is reached, and the sendMsg test uses a fake transport.
"""

import os
import socket
import subprocess
import sys
import types

import pytest
from ibapi import connection as ibapi_connection

from tests import conftest
from tests.conftest import (
    IB_PORTS_REFUSED,
    BrokerConnectionBlockedUnderPytest,
    OrderFrameBlockedUnderPytest,
)

IB_PORTS = (7496, 7497, 4001, 4002)


def test_real_connection_sendmsg_raises():
    conn = ibapi_connection.Connection("127.0.0.1", 7497)
    sent: list[bytes] = []

    class FakeSocket:
        def send(self, data: bytes) -> int:
            sent.append(data)
            return len(data)

    # A fake transport proves nothing is written even if a socket were present.
    conn.socket = FakeSocket()
    with pytest.raises(OrderFrameBlockedUnderPytest):
        conn.sendMsg(b"\x00\x00\x00\x04abcd")
    assert sent == []
    assert issubclass(OrderFrameBlockedUnderPytest, RuntimeError)
    assert not issubclass(OrderFrameBlockedUnderPytest, OSError)


def test_child_process_inherits_the_broker_blocked_mark():
    """The patch lives in this process; a child gets the environment mark only."""
    out = subprocess.run(
        [sys.executable, "-I", "-c", "import os;print(os.environ.get('STOINK_BROKER_BLOCKED'))"],
        capture_output=True,
        text=True,
        check=True,
        env=dict(os.environ),
    )
    assert out.stdout.strip() == "1"


def test_refused_port_set_is_exactly_the_four_ib_ports():
    assert IB_PORTS_REFUSED == frozenset(IB_PORTS)


@pytest.mark.parametrize("port", IB_PORTS)
def test_ib_ports_refused_by_the_decision_function(port):
    # The guard's own decision function: no socket is touched.
    with pytest.raises(BrokerConnectionBlockedUnderPytest, match=str(port)):
        conftest._refuse_ib_port(("127.0.0.1", port))
    with pytest.raises(BrokerConnectionBlockedUnderPytest, match=str(port)):
        conftest._refuse_ib_port(("localhost", str(port)))


@pytest.mark.parametrize("address", [("127.0.0.1", 0), ("127.0.0.1", 8750), "/tmp/sock", None])
def test_decision_function_lets_other_addresses_through(address):
    conftest._refuse_ib_port(address)


def test_ib_ports_refused_at_socket_level(monkeypatch):
    """The patched connect, connect_ex and create_connection, end to end.

    A stand-in port (a local port-0 listener) is added to the refused set, so no
    connect is ever made to a real broker port.
    """
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        monkeypatch.setattr(conftest, "IB_PORTS_REFUSED", frozenset({port}))
        s = socket.socket()
        try:
            with pytest.raises(BrokerConnectionBlockedUnderPytest, match=str(port)):
                s.connect(("127.0.0.1", port))
            with pytest.raises(BrokerConnectionBlockedUnderPytest, match=str(port)):
                s.connect_ex(("127.0.0.1", port))
        finally:
            s.close()
        with pytest.raises(BrokerConnectionBlockedUnderPytest, match=str(port)):
            socket.create_connection(("127.0.0.1", port), timeout=0.01)
        with pytest.raises(BlockingIOError):
            listener.accept()  # the listener saw no connection
    finally:
        listener.close()


def test_unconfigure_restores_the_originals():
    """Configure then unconfigure on a throwaway config leaves the session block as it was."""
    before = (
        ibapi_connection.Connection.sendMsg,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
    )
    config = types.SimpleNamespace(stash=pytest.Stash())
    conftest.pytest_configure_order_frame_block(config)
    try:
        during = (
            ibapi_connection.Connection.sendMsg,
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
        )
        assert during[1:] != before[1:]  # wrapped again on top of the session block
    finally:
        conftest.pytest_unconfigure_order_frame_block(config)
    after = (
        ibapi_connection.Connection.sendMsg,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
    )
    assert after == before
    assert after[0] is conftest._blocked_send_msg


def test_port_zero_still_allowed():
    # Port 0 is not refused by the guard: it reaches the real connect, which
    # fails with an OSError of its own, never the guard's error.
    s = socket.socket()
    try:
        with pytest.raises(OSError) as exc:
            s.connect(("127.0.0.1", 0))
        assert not isinstance(exc.value, BrokerConnectionBlockedUnderPytest)
    finally:
        s.close()
    # A listener on port 0 (the API tests' pattern) stays usable.
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        assert srv.getsockname()[1] != 0
    finally:
        srv.close()
