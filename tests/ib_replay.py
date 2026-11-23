"""Turn a session recording (`MainStoinker/ib/recorder.py`) into `FakeIbClient` responders.

    rec = load_recording(path)
    fake = FakeIbClient(wrapper, **handshake_kwargs(rec))   # next id, accounts, farm messages
    replay = install(fake, rec)                             # fake.respond(...) per recorded request
    ...                                                     # the code under test asks as it likes
    replay.assert_all_used()

Pairing. A recorded request is paired with the callbacks that carry its request
id (the first argument of every `EWrapper` callback whose first parameter is
`reqId` or `tickerId`: history bars and their end, contract details, head
timestamp, ticks, `error` with that id). When the code under test makes a request
of the same method, the paired callbacks are played with the *new* request id:
the driver's ids differ from the recorded ones, so the id is rewritten in the
first argument. Which recorded request answers which new one: the first unused
one of that method whose other arguments (contract, end time, duration, bar size,
what to show...) are equal, else the first unused one. A request with no recorded
answer left raises `ReplayExhausted` on the calling thread, so an unexpected extra
request fails the test that made it. Callbacks before the first request are the
handshake (`handshake_kwargs`); later callbacks that belong to no request (1100,
1102, `currentTime` without a request, errors with id -1) are `Recording.unsolicited`
and are pushed by the test when it wants them (`push_unsolicited`).

Decoding. A dict with `"__class__"` becomes the ibapi object of that name
(`BarData`, `Contract`, `ContractDetails`, `TickAttrib`, `ComboLeg`,
`DeltaNeutralContract`); `Decimal` fields (`BarData.volume`, `wap`, the sizes of
`ContractDetails`, and callback parameters annotated `Decimal`) are rebuilt from
strings. Account ids are the recorder's placeholders (`ACCOUNT-1`), so a replayed
`managedAccounts` carries no real id. Nothing here opens a socket or imports a
module that does.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData, TickAttrib
from ibapi.contract import ComboLeg, Contract, ContractDetails, DeltaNeutralContract
from ibapi.wrapper import EWrapper

from MainStoinker.ib.recorder import FORMAT_NAME, FORMAT_VERSION
from tests.fake_ib import FakeIbClient

_CLASSES: dict[str, type] = {
    c.__name__: c
    for c in (BarData, Contract, ContractDetails, TickAttrib, ComboLeg, DeltaNeutralContract)
}
_DECIMAL_FIELDS: dict[str, frozenset[str]] = {
    "BarData": frozenset({"volume", "wap"}),
    "ContractDetails": frozenset({"minSize", "sizeIncrement", "suggestedSizeIncrement"}),
}
_ID_NAMES = frozenset({"reqId", "tickerId"})
NO_REQUEST_ID = -1


class ReplayExhausted(AssertionError):
    """The code under test made a request the recording has no (more) answer for."""


@dataclass(frozen=True)
class RecordedEvent:
    seq: int
    t: float | None
    kind: str  # "request" or "raw"
    name: str
    args: list[Any]


@dataclass
class RecordedRequest:
    method: str
    args: list[Any]  # raw JSON arguments, request id first when the method has one
    request_id: int | None
    responses: list[RecordedEvent] = field(default_factory=list)

    @property
    def signature(self) -> list[Any]:
        return self.args[1:] if self.request_id is not None else self.args


@dataclass
class Recording:
    header: dict[str, Any]
    events: list[RecordedEvent]
    requests: list[RecordedRequest]
    handshake: list[RecordedEvent]
    unsolicited: list[RecordedEvent]

    def raw_events(self) -> list[RecordedEvent]:
        return [e for e in self.events if e.kind == "raw"]


# -- reading ------------------------------------------------------------------------


def _first_param(owner: type, name: str) -> str | None:
    fn = getattr(owner, name, None)
    if not callable(fn):
        return None
    params = list(inspect.signature(fn).parameters)
    return params[1] if len(params) > 1 else None


def _request_has_id(method: str) -> bool:
    return _first_param(EClient, method) in _ID_NAMES


def _callback_has_id(name: str) -> bool:
    return _first_param(EWrapper, name) in _ID_NAMES


def parse_recording(text: str) -> Recording:
    """Parse the text of a recording; raises `ValueError` on anything unexpected."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty recording")
    header = json.loads(lines[0])
    if header.get("format") != FORMAT_NAME or header.get("version") != FORMAT_VERSION:
        raise ValueError(f"not a version {FORMAT_VERSION} {FORMAT_NAME} recording: {header!r}")
    events: list[RecordedEvent] = []
    for ln in lines[1:]:
        d = json.loads(ln)
        if d["kind"] not in ("request", "raw") or not isinstance(d["args"], list):
            raise ValueError(f"bad recording line: {ln[:80]!r}")
        events.append(RecordedEvent(d["seq"], d["t"], d["kind"], d["name"], d["args"]))
    return _pair(header, events)


def load_recording(path: str | Path) -> Recording:
    return parse_recording(Path(path).read_text(encoding="utf-8"))


def _pair(header: dict[str, Any], events: list[RecordedEvent]) -> Recording:
    requests: list[RecordedRequest] = []
    by_id: dict[int, RecordedRequest] = {}
    last_by_method: dict[str, RecordedRequest] = {}
    handshake: list[RecordedEvent] = []
    unsolicited: list[RecordedEvent] = []
    for ev in events:
        if ev.kind == "request":
            has_id = _request_has_id(ev.name) and ev.args and isinstance(ev.args[0], int)
            req = RecordedRequest(ev.name, ev.args, ev.args[0] if has_id else None)
            requests.append(req)
            last_by_method[ev.name] = req
            if has_id and not ev.name.startswith("cancel"):
                by_id[ev.args[0]] = req
            continue
        target: RecordedRequest | None = None
        if _callback_has_id(ev.name) and ev.args and isinstance(ev.args[0], int):
            target = by_id.get(ev.args[0])
        elif ev.name == "currentTime":
            target = last_by_method.get("reqCurrentTime")
        if target is not None:
            target.responses.append(ev)
        elif not requests:
            handshake.append(ev)
        else:
            unsolicited.append(ev)
    return Recording(header, events, requests, handshake, unsolicited)


# -- decoding -----------------------------------------------------------------------


def decode(value: Any, cls_name: str | None = None, field_name: str | None = None) -> Any:
    """A recorded JSON value as the object ibapi would have delivered."""
    if isinstance(value, dict) and "__class__" in value:
        name = value["__class__"]
        cls = _CLASSES.get(name)
        if cls is None:
            return {k: decode(v, name, k) for k, v in value.items() if k != "__class__"}
        obj = cls()
        for k, v in value.items():
            if k != "__class__":
                setattr(obj, k, decode(v, name, k))
        return obj
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, str) and field_name in _DECIMAL_FIELDS.get(cls_name or "", ()):
        return Decimal(value)
    return value


def decode_callback(ev: RecordedEvent) -> tuple[Any, ...]:
    """The arguments of a recorded callback, with `Decimal` parameters rebuilt."""
    fn = getattr(EWrapper, ev.name)
    params = list(inspect.signature(fn).parameters.values())[1:]
    out = []
    for i, raw in enumerate(ev.args):
        value = decode(raw)
        if i < len(params) and params[i].annotation is Decimal and isinstance(value, str):
            value = Decimal(value)
        out.append(value)
    return tuple(out)


def _plain(value: Any) -> Any:
    """The comparable JSON form of a request argument made by the code under test."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    attrs = getattr(value, "__dict__", None)
    if attrs is None:
        return f"<{type(value).__name__}>"
    return {"__class__": type(value).__name__, **{k: _plain(v) for k, v in attrs.items()}}


def _matches(recorded: Any, actual: Any) -> bool:
    """Whether a request's arguments are those recorded: a recorded dict may name only some fields."""
    if isinstance(recorded, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches(v, actual[k]) for k, v in recorded.items()
        )
    if isinstance(recorded, list):
        return (
            isinstance(actual, list) and len(recorded) == len(actual)
            and all(_matches(r, a) for r, a in zip(recorded, actual, strict=True))
        )
    return bool(recorded == actual)


# -- installing -----------------------------------------------------------------------


def handshake_kwargs(rec: Recording) -> dict[str, Any]:
    """`FakeIbClient` constructor arguments that reproduce the recorded handshake."""
    kwargs: dict[str, Any] = {}
    farm: list[tuple[int, str]] = []
    for ev in rec.handshake:
        if ev.name == "nextValidId" and ev.args:
            kwargs["next_valid_id"] = ev.args[0]
        elif ev.name == "managedAccounts" and ev.args:
            kwargs["managed_accounts"] = ev.args[0]
        elif ev.name == "error" and len(ev.args) >= 4 and ev.args[0] == NO_REQUEST_ID:
            if isinstance(ev.args[2], int) and 2100 <= ev.args[2] < 2200:
                farm.append((ev.args[2], ev.args[3]))
    if farm:
        kwargs["farm_messages"] = tuple(farm)
    return kwargs


class Replay:
    """The responders `install` put on a fake, and what is left of the recording."""

    def __init__(self, rec: Recording) -> None:
        self.recording = rec
        self.pools: dict[str, list[RecordedRequest]] = {}
        for req in rec.requests:
            if req.responses:
                self.pools.setdefault(req.method, []).append(req)
        self.used: list[tuple[RecordedRequest, list[Any]]] = []  # (recorded, new arguments)

    def remaining(self) -> int:
        return sum(len(p) for p in self.pools.values())

    def assert_all_used(self) -> None:
        left = [(r.method, r.request_id) for p in self.pools.values() for r in p]
        assert not left, f"recorded requests never made again: {left}"

    def handler(self, method: str) -> Callable[..., list[tuple[Any, ...]]]:
        has_id = _request_has_id(method)

        def respond(*args: Any) -> list[tuple[Any, ...]]:
            pool = self.pools.get(method, [])
            plain = [_plain(a) for a in args]
            sig = plain[1:] if has_id else plain
            pick = next((r for r in pool if _matches(r.signature, sig)), None) or (
                pool[0] if pool else None
            )
            if pick is None:
                raise ReplayExhausted(f"no recorded answer left for {method}{args!r}")
            pool.remove(pick)
            self.used.append((pick, list(args)))
            new_id = args[0] if has_id else None
            played: list[tuple[Any, ...]] = []
            for ev in pick.responses:
                cb_args = list(decode_callback(ev))
                if (
                    new_id is not None and cb_args and _callback_has_id(ev.name)
                    and cb_args[0] == pick.request_id
                ):
                    cb_args[0] = new_id
                played.append((ev.name, *cb_args))
            return played

        return respond


def install(fake: FakeIbClient, rec: Recording) -> Replay:
    """Put a responder on `fake` for every recorded request method that was answered."""
    replay = Replay(rec)
    for method in replay.pools:
        fake.respond(method, replay.handler(method))
    return replay


def push_unsolicited(fake: FakeIbClient, rec: Recording) -> int:
    """Queue the callbacks that belonged to no request (1100, 1102...); returns how many."""
    for ev in rec.unsolicited:
        fake.push(ev.name, *decode_callback(ev))
    return len(rec.unsolicited)
