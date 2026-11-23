"""Session recorder: an `IbConnection` observer that writes what IB said, minus account numbers.

`SessionRecorder(path, clock=...)` is passed to `IbConnection(observers=[...])`. It
writes JSON Lines: one header line, then one line per request (`on_request`) and
per raw callback (`on_raw`). The file is created exclusively (an existing file is
never overwritten), every line is written whole and flushed, so a crash leaves a
readable file. The caller decides the path; real sessions are recorded outside the
repository and outside OneDrive. This module reads no environment, opens no socket
and does not know the host: the header carries the port only.

Format (version 1)
------------------
Header: `{"format": "stoinkulator-ib-session", "version": 1, "redaction": N,
"ibapi": "10.37.2", "command": ..., "port": ..., "start_utc": ...}`. `start_utc`
is the one wall-clock value: a string the caller passes (the recorder never reads
the wall clock) so a recording can be matched to TWS's own log; it may be null.
Every other time is `t`, seconds since the recorder was built, taken from the
injected monotonic `clock`, so the same input gives the same bytes.

Event: `{"seq": n, "t": s, "kind": "request" | "raw", "name": ..., "args": [...]}`.
Arguments are JSON values: objects (`Contract`, `BarData`, `ContractDetails`, tick
attributes, any object with attributes) become dicts of their attributes with a
`"__class__"` key, `Decimal` becomes a string, bytes become `{"__bytes__": n,
"text": printable text}`, a value that cannot be serialised becomes
`"<unserialisable TypeName>"` (never `repr()`), anything nested too deeply or too
large becomes `"<truncated>"`.

Redaction (version 1): fail closed, at write time, before anything reaches disk
-----------------------------------------------------------------------------
Strings are first normalised (NFKC, so full-width and compatibility characters
become plain ones; control, format and line-separator characters other than tab,
newline and carriage return are dropped, which also defeats zero-width padding),
then, in order:

1. `private_values` given by the caller (user name, host name, machine name,
   home folder: the caller reads them, this module does not) are replaced by
   `REDACTED`, case-insensitively.
2. E-mail addresses, IPv4 and IPv6 addresses and file paths (`C:\\...`, `\\\\server\\...`,
   `/home/...`, `/Users/...`, `~/...`) are replaced by `EMAIL-REDACTED`,
   `IP-REDACTED`, `PATH-REDACTED`.
3. Every account id learned in this session is replaced by `ACCOUNT-n`, anywhere,
   as a substring, in any case. Ids are learned from the `managedAccounts`
   callback (every token of three or more letters and digits in any of its
   arguments) and from any value passed under an account-like name (below), when
   it contains a digit. The digit run (five or more digits) of a learned id is
   replaced too, so the bare digits of an id are caught once the id was learned. A
   number (int, float, Decimal) whose text contains such a digit run becomes
   the placeholder as well.
4. The pattern `ACCOUNT_PATTERN`: one or two letters, an optional `-` or `_`, then
   five or more digits, wherever they stand (also glued to other letters or
   digits). Every id it finds is learned too (step 3 then applies to it). This is
   IB's id shape (`U`, `DU`, `F`, `DF`, `I`, `DI` plus seven digits, longer for
   some institutional ids; with a sub-account suffix after a dot the suffix
   stays and the id before it goes). It works before
   `managedAccounts` has been seen. It can over-redact: any token of one or two
   letters and five or more digits (an ISIN, which is a country code plus ten
   digits, or a short code followed by a long number). The one exemption is an
   option symbol in OCC shape, `_OCC`: an optional root of up to six letters
   (IB pads it with spaces: `AAPL  240816C00220000`), six digits, `C` or `P`,
   eight digits, standing alone. The same placeholder `ACCOUNT-n` is used for the
   same id in any spelling.

Whatever is passed under an account-like name is replaced whole, whatever its type
and shape: dictionary keys and object attributes containing `account` or `acct`
(`account`, `acctNumber`, `accountsList`, `acctCode`, `accountName`...), and the
positional arguments of a callback or request whose `EWrapper` / `EClient`
parameter has such a name. Dictionary keys are redacted as strings.

An id split over several arguments of one event (`"U"`, `1234567`; or list items)
is caught by scanning the string and int arguments joined: if a match crosses a
boundary the event's string arguments, and the ints in the match, are replaced by
`<redacted>`. For this scan only, the digits must be five to nine and not look like
a date (`YYYYMMDD`), so that `US/Eastern` followed by `20240102:0400` is not taken
for an id. A final check scans the serialised arguments of every line; if
anything account-shaped is left the arguments are replaced by a marker. If
redaction itself fails the arguments are replaced by a marker naming the
exception type. Both are counted (`failures`).

Not covered: ids written in a look-alike alphabet (Cyrillic or Greek letters),
split across two *events*, split by something other than directly adjacent
arguments, or spelled out in words; private data that is not an account, an
address, a path, an e-mail address or one of `private_values`; a recording's
timing, which says when the owner traded. Over-redaction is accepted. The owner
reads a redacted recording before it is committed.

Threading: `on_request` and `on_raw` run on the reader, connect-helper and caller
threads. One lock covers redaction, numbering and the write. Nothing here calls
back into the connection, sleeps or blocks beyond one buffered write and flush;
an exception is caught and counted, never raised.
"""

from __future__ import annotations

import enum
import inspect
import json
import math
import re
import threading
import types
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any, TextIO

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

FORMAT_NAME = "stoinkulator-ib-session"
FORMAT_VERSION = 1
REDACTION_VERSION = 1

ACCOUNT_PATTERN = re.compile(r"[A-Za-z]{1,2}[-_]?\d{5,}")
_SPLIT_PATTERN = re.compile(r"[A-Za-z]{1,2}[-_]?\d{5,9}(?!\d)")
_DATE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
_OCC = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,6} *)?\d{6}[CcPp]\d{8}(?![A-Za-z0-9])")
_JSON_ESCAPE = re.compile(r"\\.")  # `\n` followed by digits must not look like a letter and digits
_ACCOUNT_NAME = re.compile(r"acc(?:ount|t)", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")
_IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?"
    r"|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?)(?![\w:])"
)
_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]*"
    r"|\\\\[^\s\"'<>|\\]+\\[^\s\"'<>|]*"
    r"|(?<![\w/.])~[\\/][^\s\"'<>|]*"
    r"|(?<![\w.])/(?:home|Users|root|var|tmp|etc|opt|mnt|usr|private)/[^\s\"'<>|]*"
)
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
_KEEP_CONTROLS = "\t\n\r"
_MAX_DEPTH = 8
_MAX_NODES = 5000
_MAX_PLACEHOLDERS = 1000
_WIPED = "<redacted>"

_ParamNames = tuple[str, ...]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _account_name(name: str) -> bool:
    return _ACCOUNT_NAME.search(name) is not None


def _norm_key(text: str) -> str:
    return "".join(_TOKEN.findall(text)).upper()


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.encode("utf-8", "replace").decode("utf-8"))
    return "".join(
        c for c in text
        if c in _KEEP_CONTROLS or unicodedata.category(c) not in _DROP_CATEGORIES
    )


def _occ_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _OCC.finditer(text)]


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < b and a < span[1] for a, b in spans)


class Redactor:
    """Turns raw callback / request arguments into JSON values with no account data.

    Not thread-safe: `SessionRecorder` calls it under its lock. Tests use it
    directly. `known_accounts` is the number of ids learned so far.
    """

    def __init__(self, private_values: Iterable[str] = ()) -> None:
        values = sorted(
            {_normalise(v) for v in private_values if isinstance(v, str) and v.strip()},
            key=len, reverse=True,
        )
        self._private = (
            re.compile("|".join(re.escape(v) for v in values), re.IGNORECASE) if values else None
        )
        self._numbers: dict[str, int] = {}
        self._alias: dict[str, str] = {}  # normalised id or digit run -> normalised id
        self._learned_re: re.Pattern[str] | None = None
        self._digit_runs: tuple[str, ...] = ()
        self._leaves: list[str] = []  # text of every str and int leaf, in walk order
        self._wipe: set[int] = set()
        self._wipe_strings = False
        self._nodes = 0
        self._names: dict[tuple[str, str], _ParamNames] = {}

    @property
    def known_accounts(self) -> int:
        return len({v for v in self._alias.values()})

    # -- placeholders and learning -------------------------------------------------

    def _placeholder(self, key: str) -> str:
        key = self._alias.get(key, key)
        number = self._numbers.get(key)
        if number is None:
            if len(self._numbers) >= _MAX_PLACEHOLDERS:
                return "ACCOUNT-OVERFLOW"
            number = self._numbers[key] = len(self._numbers) + 1
        return f"ACCOUNT-{number}"

    def learn(self, token: str) -> None:
        """Remember `token` as an account id of this session."""
        key = _norm_key(_normalise(token))
        if len(key) < 3 or key in self._alias:
            return
        self._alias[key] = key
        for run in re.findall(r"\d{5,}", key):
            self._alias.setdefault(run, key)
        ids = sorted(self._alias, key=len, reverse=True)
        self._learned_re = re.compile("|".join(re.escape(i) for i in ids), re.IGNORECASE)
        self._digit_runs = tuple(i for i in ids if i.isdigit())

    def _learn_text(self, text: str, *, everything: bool) -> None:
        for tok in _TOKEN.findall(_normalise(text)):
            if len(tok) >= 3 and (everything or (len(tok) >= 5 and any(c.isdigit() for c in tok))):
                self.learn(tok)

    # -- strings ---------------------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Redact one string (headers, command lines)."""
        return self._redact_text(_normalise(text))

    def _redact_text(self, s: str) -> str:
        if self._private is not None:
            s = self._private.sub("REDACTED", s)
        s = _EMAIL.sub("EMAIL-REDACTED", s)
        s = _PATH.sub("PATH-REDACTED", s)
        s = _IPV6.sub("IP-REDACTED", s)
        s = _IPV4.sub("IP-REDACTED", s)
        if self._learned_re is not None:
            s = self._learned_re.sub(lambda m: self._placeholder(_norm_key(m.group(0))), s)
        occ = _occ_spans(s)

        def by_pattern(m: re.Match[str]) -> str:
            if _overlaps(m.span(), occ):
                return m.group(0)
            self.learn(m.group(0))  # the same id elsewhere in this session is then caught too
            return self._placeholder(_norm_key(m.group(0)))

        return ACCOUNT_PATTERN.sub(by_pattern, s)

    def _force_text(self, s: str) -> str:
        """A value under an account-like name: each comma or semicolon separated part
        becomes one placeholder (a list of ids stays a list; a name becomes one id)."""
        s = _normalise(s)
        self._learn_text(s, everything=False)
        return re.sub(
            r"[^,;]*[^,;\s][^,;]*",
            lambda m: self._placeholder(_norm_key(m.group(0)) or "EMPTY"),
            s,
        )

    def leaks(self, text: str) -> bool:
        """Whether `text` still holds something account-shaped (the last check before a write)."""
        if self._learned_re is not None and self._learned_re.search(text):
            return True
        occ = _occ_spans(text)
        return any(not _overlaps(m.span(), occ) for m in ACCOUNT_PATTERN.finditer(text))

    # -- walking values --------------------------------------------------------------

    def _number(self, v: int | float | Decimal, force: bool) -> Any:
        text = str(v)
        if force:
            return self._placeholder(_norm_key(text) or "EMPTY")
        index = len(self._leaves)
        # Only ints take part in the split-id scan: a float or Decimal is never half an id.
        self._leaves.append(text if isinstance(v, int) else "")
        if index in self._wipe:
            return _WIPED
        for run in self._digit_runs:
            if run in text:
                return self._placeholder(run)
        if isinstance(v, Decimal):
            return text
        if isinstance(v, float):
            return v if math.isfinite(v) else text
        return int(v)

    def _string(self, v: str, force: bool) -> str:
        text = _normalise(str.__str__(v))
        if force:
            return self._force_text(text) if text else text
        index = len(self._leaves)
        self._leaves.append(text)
        if text and (self._wipe_strings or index in self._wipe):
            return _WIPED
        return self._redact_text(text)

    def _walk(self, v: Any, force: bool, depth: int, path: frozenset[int]) -> Any:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, enum.Enum) and not isinstance(v, int | str):
            v = v.name
        if isinstance(v, int | float | Decimal):
            return self._number(v, force)
        if isinstance(v, str):
            return self._string(v, force)
        self._nodes += 1
        if self._nodes > _MAX_NODES or depth > _MAX_DEPTH:
            return "<truncated>"
        if isinstance(v, bytes | bytearray | memoryview):
            raw = bytes(v)
            text = "".join(chr(b) for b in raw if 32 <= b < 127)
            return {
                "__bytes__": len(raw),
                "text": self._force_text(text) if force else self._redact_text(text),
            }
        if isinstance(v, type | types.ModuleType | types.FunctionType | types.BuiltinFunctionType
                      | types.MethodType) or callable(v):
            return f"<unserialisable {type(v).__name__}>"
        if id(v) in path:
            return "<cycle>"
        path = path | {id(v)}
        if isinstance(v, Mapping):
            return self._walk_items(None, v.items(), force, depth, path)
        if isinstance(v, list | tuple):
            return [self._walk(x, force, depth + 1, path) for x in v]
        if isinstance(v, set | frozenset):
            items = [self._walk(x, force, depth + 1, path) for x in v]
            return sorted(items, key=_json_text)
        if isinstance(v, BaseException):
            return {
                "__class__": type(v).__name__,
                "args": [self._walk(x, force, depth + 1, path) for x in v.args],
            }
        attrs = _attributes(v)
        if attrs is None:
            return f"<unserialisable {type(v).__name__}>"
        return self._walk_items(type(v).__name__, attrs.items(), force, depth, path)

    def _walk_items(
        self, cls: str | None, items: Iterable[tuple[Any, Any]], force: bool, depth: int,
        path: frozenset[int],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if cls is not None:
            out["__class__"] = cls
        for key, value in items:
            key_text = key if isinstance(key, str) else str(key)
            sub_force = force or _account_name(_normalise(key_text))
            shown = self._redact_text(_normalise(str.__str__(key_text)))
            n = 2
            unique = shown
            while unique in out:
                unique = f"{shown}#{n}"
                n += 1
            out[unique] = self._walk(value, sub_force, depth + 1, path)
        return out

    # -- one event ---------------------------------------------------------------------

    def _param_names(self, kind: str, name: str) -> _ParamNames:
        cached = self._names.get((kind, name))
        if cached is not None:
            return cached
        owner = EWrapper if kind == "raw" else EClient
        fn = getattr(owner, name, None) if not name.startswith("_") else None
        names: _ParamNames = ()
        if callable(fn):
            try:
                names = tuple(p for p in inspect.signature(fn).parameters)[1:]
            except (TypeError, ValueError):
                names = ()
        self._names[(kind, name)] = names
        return names

    def _run(self, args: Sequence[Any], forced: set[int]) -> list[Any]:
        self._leaves = []
        self._nodes = 0
        return [self._walk(a, i in forced, 0, frozenset()) for i, a in enumerate(args)]

    def sanitise(self, kind: str, name: str, args: Sequence[Any]) -> list[Any]:
        """The JSON-safe, account-free form of one event's arguments."""
        args = tuple(args)
        names = self._param_names(kind, name)
        forced = {i for i, n in enumerate(names[: len(args)]) if _account_name(n)}
        self._wipe, self._wipe_strings = set(), False
        if name == "managedAccounts":
            for a in args:
                self._learn_text(_flat_text(a), everything=True)
        out: list[Any] = []
        for _ in range(3):  # ids learned while walking are applied by walking again
            known = len(self._alias)
            out = self._run(args, forced)
            if len(self._alias) == known:
                break
        spans = _cross_leaf_matches(self._leaves, self.split_candidates)
        if spans:
            self._wipe = spans
            self._wipe_strings = True
            out = self._run(args, forced)
            self._wipe, self._wipe_strings = set(), False
        return out

    def leaks_in_text(self, text: str) -> list[tuple[int, int]]:
        """Spans in `text` of a learned id or of the account pattern (OCC symbols excepted)."""
        spans = [m.span() for m in self._learned_re.finditer(text)] if self._learned_re else []
        occ = _occ_spans(text)
        spans += [m.span() for m in ACCOUNT_PATTERN.finditer(text) if not _overlaps(m.span(), occ)]
        return spans

    def split_candidates(self, text: str) -> list[tuple[int, int]]:
        """Like `leaks_in_text`, but stricter about the pattern: used on joined arguments."""
        spans = [m.span() for m in self._learned_re.finditer(text)] if self._learned_re else []
        occ = _occ_spans(text)
        for m in _SPLIT_PATTERN.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if not _overlaps(m.span(), occ) and not _DATE.fullmatch(digits):
                spans.append(m.span())
        return spans


def _flat_text(value: Any, depth: int = 0) -> str:
    """Every string in `value`, space separated (used only to learn ids)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return "".join(chr(b) for b in value if 32 <= b < 127)
    if depth > _MAX_DEPTH:
        return ""
    if isinstance(value, Mapping):
        return " ".join(_flat_text(v, depth + 1) for v in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return " ".join(_flat_text(v, depth + 1) for v in value)
    return ""


def _attributes(obj: Any) -> dict[str, Any] | None:
    """Instance attributes (dict and slots) of a plain object, or None if it has none to read."""
    found: dict[str, Any] = {}
    seen_any = False
    try:
        found.update(vars(obj))
        seen_any = True
    except TypeError:
        pass
    for cls in type(obj).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            if isinstance(slot, str) and slot not in ("__dict__", "__weakref__"):
                try:
                    found[slot] = getattr(obj, slot)
                    seen_any = True
                except AttributeError:
                    continue
    return found if seen_any else None


def _cross_leaf_matches(
    leaves: list[str], finder: Callable[[str], list[tuple[int, int]]],
) -> set[int]:
    """Leaves (by index) touched by a match that crosses a leaf boundary of the joined text."""
    bounds: list[tuple[int, int, int]] = []  # (start, end, leaf index)
    parts: list[str] = []
    pos = 0
    for index, text in enumerate(leaves):
        if text:
            bounds.append((pos, pos + len(text), index))
            parts.append(text)
            pos += len(text)
    if len(bounds) < 2:
        return set()
    touched: set[int] = set()
    for a, b in finder("".join(parts)):
        inside = [i for s, e, i in bounds if s < b and a < e]
        if len(inside) > 1:
            touched.update(inside)
    return touched


class SessionRecorder:
    """An `IbConnection` observer that records the session to `path` (see module docstring).

    `clock` is a monotonic seconds clock (inject a fake one in tests). `command` is
    the command line (redacted like everything else), `port` the port connected to
    (never the host), `start_utc` an ISO string the caller supplies, `private_values`
    strings to remove wherever they occur (user name, host name, home folder).
    Counters: `lines_written`, `failures` (events whose redaction failed or left a
    leak), `write_failures`, `dropped` (events after `close`).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float],
        command: str = "",
        port: int | None = None,
        start_utc: str | None = None,
        private_values: Iterable[str] = (),
        ibapi_version: str | None = None,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._redactor = Redactor(private_values)
        self._seq = 0
        self._closed = False
        self._broken = False
        self.lines_written = 0
        self.failures = 0
        self.write_failures = 0
        self.dropped = 0
        self._start = float(clock())
        version = ibapi_version if ibapi_version is not None else _ibapi_version()
        self._file: TextIO = open(path, "x", encoding="utf-8", newline="\n")
        header = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "redaction": REDACTION_VERSION,
            "ibapi": version,
            "command": self._redactor.scrub(command),
            "port": port if type(port) is int else None,
            "start_utc": self._redactor.scrub(start_utc) if isinstance(start_utc, str) else None,
        }
        self._write(_json_text(header))

    # -- observer hooks ----------------------------------------------------------

    def on_request(self, name: str, args: tuple[Any, ...]) -> None:
        self._record("request", name, args)

    def on_raw(self, name: str, args: tuple[Any, ...]) -> None:
        self._record("raw", name, args)

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._file.flush()
                self._file.close()
            except (OSError, ValueError):
                self.write_failures += 1

    def __enter__(self) -> SessionRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------------

    def _record(self, kind: str, name: str, args: tuple[Any, ...]) -> None:
        try:
            self._record_locked(kind, name, args)
        except Exception:  # never into the connection; counted
            with self._lock:
                self.failures += 1

    def _record_locked(self, kind: str, name: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            if self._closed or self._broken:
                self.dropped += 1
                return
            self._seq += 1
            try:
                t: float | None = round(float(self._clock()) - self._start, 6)
            except Exception:
                t = None
                self.failures += 1
            try:
                shown_name = self._redactor.scrub(str(name))
            except Exception:
                shown_name = "<unknown>"
            marker: str | None = None
            try:
                body = _json_text(self._redactor.sanitise(kind, str(name), args))
                if self._redactor.leaks(_JSON_ESCAPE.sub(" ", body)):
                    marker = "<redaction check failed>"
            except Exception as exc:
                marker = f"<redaction failed: {type(exc).__name__}>"
            if marker is not None:
                self.failures += 1
                body = _json_text(marker)
            self._write(
                f'{{"seq":{self._seq},"t":{_json_text(t)},"kind":{_json_text(kind)},'
                f'"name":{_json_text(shown_name)},"args":{body}}}'
            )

    def _write(self, line: str) -> None:
        try:
            self._file.write(line + "\n")
            self._file.flush()
            self.lines_written += 1
        except (OSError, ValueError):
            self.write_failures += 1
            self._broken = True


def _ibapi_version() -> str:
    try:
        return metadata.version("ibapi")
    except metadata.PackageNotFoundError:
        return "unknown"
