"""No order frame can leave a process during a pytest run (see tests/conftest.py).

Nothing here opens a real connection: refused ports are refused before the real
connect is reached, and the sendMsg test uses a fake transport.
"""

import os
import socket
import subprocess
import sys

import pytest
from ibapi import connection as ibapi_connection

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


@pytest.mark.parametrize("port", IB_PORTS)
def test_ib_ports_refused_at_socket_level(port):
    assert port in IB_PORTS_REFUSED
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
