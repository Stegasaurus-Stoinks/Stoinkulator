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

Unreplayable requests. A redaction marker line, a torn last line or a value that was
redacted inside a number or a bar (a conId, a price, a volume) does not make the
file unreadable: `parse_recording` returns the good events and `Recording.problems`
(line, reason, method); the affected request is marked (`RecordedRequest.problem`)
and asking the replay for it raises `ReplayUnavailable` with that reason. Only a bad
header raises `ValueError`.

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
# The recorder writes IB's server time (`currentTime`) as 0, it being the wall clock of the
# session. Replay hands back this fixed time for every `currentTime`, whatever the file says:
# 2000-01-01T00:00:00Z in epoch seconds, a value no real session of this system can produce.
SYNTHETIC_CURRENT_TIME = 946684800


class ReplayExhausted(AssertionError):
    """The code under test made a request the recording has no (more) answer for."""


class ReplayUnavailable(AssertionError):
    """The recording has an answer for this request, but it could not be replayed (see `Problem`)."""


@dataclass(frozen=True)
class RecordedEvent:
    seq: int
    t: float | None
    kind: str  # "request" or "raw"
    name: str
    args: list[Any]
    problem: str | None = None  # set when the line was a redaction marker or unreadable
    line: int = 0  # 1-based line of the file


@dataclass(frozen=True)
class Problem:
    """Something in the recording that stops a request from being replayed."""

    line: int  # 1-based line of the file
    reason: str
    method: str | None  # the request that is now unreplayable, when it is known


@dataclass
class RecordedRequest:
    method: str
    args: list[Any]  # raw JSON arguments, request id first when the method has one
    request_id: int | None
    responses: list[RecordedEvent] = field(default_factory=list)
    problem: str | None = None  # why the answer cannot be replayed

    @property
    def signature(self) -> list[Any]:
        return self.args[1:] if self.request_id is not None else self.args


@dataclass
class Recording:
    header: dict[str, Any]
    events: list[RecordedEvent]  # the good events only
    requests: list[RecordedRequest]
    handshake: list[RecordedEvent]
    unsolicited: list[RecordedEvent]
    problems: list[Problem] = field(default_factory=list)  # unreadable lines, unreplayable requests

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
    """Parse the text of a recording.

    Raises `ValueError` only for a bad header (empty file, not JSON, another format or
    version). A bad event line does not make the file unreplayable: a redaction marker
    line, a torn (truncated) last line, any unreadable line, and an event whose value
    was redacted inside a number or a bar are left out of `events` and reported in
    `Recording.problems`; the request they belong to is marked unreplayable
    (`RecordedRequest.problem`) and `Replay` raises `ReplayUnavailable` for it.
    A line that cannot be read does not say which request it belonged to: the most
    recent request is taken (so the report may over-state, never under-state).
    """
    numbered = [(no, ln) for no, ln in enumerate(text.split("\n"), 1) if ln.strip()]
    if not numbered:
        raise ValueError("empty recording")
    header = json.loads(numbered[0][1])
    if not isinstance(header, dict) or (
        header.get("format") != FORMAT_NAME or header.get("version") != FORMAT_VERSION
    ):
        raise ValueError(f"not a version {FORMAT_VERSION} {FORMAT_NAME} recording: {header!r}")
    last_line = numbered[-1][0]
    ordered: list[RecordedEvent] = []
    for no, ln in numbered[1:]:
        try:
            d = json.loads(ln)
            kind, name, args, seq, t = d["kind"], d["name"], d["args"], d["seq"], d["t"]
            if kind not in ("request", "raw") or not isinstance(name, str):
                raise ValueError("bad event")
        except (ValueError, KeyError, TypeError):
            reason = "torn last line" if no == last_line else "line is not a readable event"
            ordered.append(RecordedEvent(-1, None, "raw", "", [], reason, no))
            continue
        if not isinstance(args, list):
            marker = args if isinstance(args, str) and args.startswith("<redaction") else "?"
            ordered.append(RecordedEvent(seq, t, kind, name, [], f"redaction marker {marker}", no))
            continue
        ordered.append(RecordedEvent(seq, t, kind, name, args, None, no))
    return _pair(header, ordered)


def load_recording(path: str | Path) -> Recording:
    return parse_recording(Path(path).read_text(encoding="utf-8"))


_NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "BarData": ("open", "high", "low", "close", "barCount"),
    "Contract": ("conId",),
    "ContractDetails": ("minTick", "underConId"),
}


def _numeric_problem(obj: Any, depth: int = 0) -> str | None:
    """A field that must be a number but is text (a value was redacted inside it)."""
    if depth > 6:
        return None
    if isinstance(obj, list | tuple):
        return next((p for p in (_numeric_problem(o, depth + 1) for o in obj) if p), None)
    cls = type(obj).__name__
    for attr in _NUMERIC_FIELDS.get(cls, ()):
        if isinstance(getattr(obj, attr, None), str):
            return f"{cls}.{attr} is text"
    if cls in _NUMERIC_FIELDS:
        return next(
            (p for p in (_numeric_problem(v, depth + 1) for v in vars(obj).values()) if p), None
        )
    return None


def _unreplayable_reason(ev: RecordedEvent) -> str | None:
    """Why a raw event cannot be played back, or None if it can."""
    fn = getattr(EWrapper, ev.name, None)
    if not callable(fn):
        return f"{ev.name}: not a callback of this ibapi"
    try:
        decoded = decode_callback(ev)
    except Exception as exc:
        return f"{ev.name}: a value cannot be decoded ({type(exc).__name__}), it was probably redacted"
    for param, value in zip(list(inspect.signature(fn).parameters.values())[1:], decoded, strict=False):
        if param.annotation in (int, float) and isinstance(value, str):
            return f"{ev.name}: parameter {param.name} is text, a number was redacted"
    problem = _numeric_problem(list(decoded))
    return f"{ev.name}: {problem}, a value was redacted inside a number" if problem else None


def _pair(header: dict[str, Any], ordered: list[RecordedEvent]) -> Recording:
    requests: list[RecordedRequest] = []
    by_id: dict[int, RecordedRequest] = {}
    last_by_method: dict[str, RecordedRequest] = {}
    handshake: list[RecordedEvent] = []
    unsolicited: list[RecordedEvent] = []
    problems: list[Problem] = []
    events: list[RecordedEvent] = []
    for ev in ordered:
        if ev.problem is not None:  # a marker or an unreadable line: no id to pair it by
            if ev.kind == "request" and ev.name:
                requests.append(RecordedRequest(ev.name, [], None, problem=ev.problem))
                problems.append(Problem(ev.line, ev.problem, ev.name))
            else:
                owner = requests[-1] if requests else None
                if owner is not None and owner.problem is None:
                    owner.problem = ev.problem
                problems.append(Problem(ev.line, ev.problem, owner.method if owner else None))
            continue
        events.append(ev)
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
        reason = _unreplayable_reason(ev) if ev.kind == "raw" else None
        if reason is not None:
            if target is not None and target.problem is None:
                target.problem = reason
            problems.append(Problem(ev.line, reason, target.method if target else None))
            if target is not None:
                target.responses.append(ev)
            continue
        if target is not None:
            target.responses.append(ev)
        elif not requests:
            handshake.append(ev)
        else:
            unsolicited.append(ev)
    return Recording(header, events, requests, handshake, unsolicited, problems)


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
    """The arguments of a recorded callback, with `Decimal` parameters rebuilt.

    `currentTime` is always the fixed `SYNTHETIC_CURRENT_TIME`."""
    fn = getattr(EWrapper, ev.name)
    params = list(inspect.signature(fn).parameters.values())[1:]
    if ev.name == "currentTime" and ev.args:
        return (SYNTHETIC_CURRENT_TIME,)  # the recorded value is zeroed; see the constant
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
            if req.responses or req.problem:
                self.pools.setdefault(req.method, []).append(req)
        self.used: list[tuple[RecordedRequest, list[Any]]] = []  # (recorded, new arguments)

    def remaining(self) -> int:
        return sum(1 for p in self.pools.values() for r in p if not r.problem)

    def assert_all_used(self) -> None:
        left = [(r.method, r.request_id) for p in self.pools.values() for r in p if not r.problem]
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
            if pick.problem is not None:
                raise ReplayUnavailable(
                    f"the recorded answer to {method} cannot be replayed: {pick.problem}"
                )
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
