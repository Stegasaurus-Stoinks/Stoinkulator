"""Broker port guard.

Policy (allowlist): connecting to the broker is allowed on the paper-trading
ports (TWS paper 7497, IB Gateway paper 4002). Any other port, whether a known
live port or an unknown/custom one, is refused unless a human has set the
environment variable STOINK_ALLOW_LIVE_PORT to exactly "1" on that machine.

This module is pure: it reads nothing from the process environment itself; the
caller passes the environment mapping in.
"""

from collections.abc import Mapping

PAPER_PORTS: frozenset[int] = frozenset({7497, 4002})
# Only used to make the error message more specific; policy never depends on it.
KNOWN_LIVE_PORTS: frozenset[int] = frozenset({7496, 4001})
ALLOW_LIVE_ENV = "STOINK_ALLOW_LIVE_PORT"


class BrokerPortRefused(RuntimeError):
    """Raised when a non-paper broker port is used without human opt-in.

    Deliberately not an OSError: EClient.connect catches socket errors
    (OSError) and swallows them, which would hide the refusal.
    """


def _normalise_port(port: object) -> int | None:
    """Return port as an int, or None if it is not a valid integer port.

    bool is rejected (True == 1). A float is accepted only if integral
    (7497.0 -> 7497); 7497.5 is rejected. Strings must be plain integers.
    """
    if isinstance(port, bool):
        return None
    if isinstance(port, int):
        return port
    if isinstance(port, float):
        return int(port) if port.is_integer() else None
    if isinstance(port, str):
        try:
            return int(port)
        except ValueError:
            return None
    return None


def check_broker_port(port: int | str, env: Mapping[str, str]) -> None:
    """Raise BrokerPortRefused unless the port is paper or a human opted in."""
    number = _normalise_port(port)
    if number is None:
        raise BrokerPortRefused(
            f"Refusing to connect: broker port {port!r} is not a valid port "
            "number, so it cannot be treated as a paper-trading port."
        )
    if number in PAPER_PORTS:
        return
    if env.get(ALLOW_LIVE_ENV) == "1":
        return
    kind = "a known live-trading port" if number in KNOWN_LIVE_PORTS else "an unrecognised port"
    raise BrokerPortRefused(
        f"Refusing to connect: port {number} is not a paper-trading port "
        f"({kind}). Paper ports are {sorted(PAPER_PORTS)}. A human must set "
        f"{ALLOW_LIVE_ENV}=1 in the environment to opt in."
    )
