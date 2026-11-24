r"""Session recorder: an `IbConnection` observer that writes what IB said, minus account numbers.

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
"ibapi": "10.37.2", "command": ..., "port": ..., "start_utc": ..., "error_time":
"zeroed", "current_time": "zeroed"}`. `start_utc` is the one wall-clock value: a string
the caller passes (the recorder never reads the wall clock) so a recording can be matched
to TWS's own log; it may be null. Every other time is `t`, seconds since the recorder was
built, taken from the injected monotonic `clock`, so the same input gives the same bytes.
Two values IB itself brings would say when the session ran, and are replaced by 0 at write
time: `errorTime` (epoch milliseconds), the second argument of every `error` callback,
connect messages included, and `currentTime` (epoch seconds), the argument of the
server-time callback, which is the wall clock of TWS at the moment of the request. The
header says `"error_time": "zeroed"` and `"current_time": "zeroed"`. Replay does not use
the recorded values: `tests/ib_replay.py` hands back the fixed synthetic time
`SYNTHETIC_CURRENT_TIME` (946684800, 2000-01-01T00:00:00Z) for every `currentTime`.
The other 15 callbacks the data-only client forwards (`MainStoinker/ib/client.py`) were
searched for further arguments that are the wall clock of the session: `error` (its time
is the one above; its text is IB's own and is kept), `nextValidId`, `connectAck`,
`connectionClosed`, `managedAccounts`, `contractDetails`, `contractDetailsEnd`,
`marketDataType`, `tickPrice`, `tickSize` and `headTimestamp` carry none. What remains is
market data that is kept: bar times (`historicalData`, `historicalDataUpdate`), the head
timestamp, the start and end strings of `historicalDataEnd`, and the text of `tickString`
(last-trade and real-time-volume stamps). They say which period was fetched, not when the
session ran, EXCEPT that a request for data up to "now" (an empty end time) makes the
last bar, the end string and any live tick stamp close to the time of the session; the
owner must know that before committing a recording of such a request.
`command` is a string or a sequence of strings (argv); see "Paths" below.

Event: `{"seq": n, "t": s, "kind": "request" | "raw", "name": ..., "args": [...]}`.
Arguments are JSON values: objects (`Contract`, `BarData`, `ContractDetails`, tick
attributes, any object with attributes) become dicts of their attributes with a
`"__class__"` key, `Decimal` becomes a string, bytes become `{"__bytes__": n,
"text": printable text}`, a value that cannot be serialised becomes
`"<unserialisable TypeName>"` (never `repr()`), anything nested too deeply or too
large becomes `"<truncated>"`. A string longer than 64 KiB (`MAX_STRING`) is not
written: it becomes `"<omitted: N KiB>"`, so the cost of redacting one string is
bounded and every pattern below runs on short text only.

Redaction (version 2): fail closed, at write time, before anything reaches disk
-----------------------------------------------------------------------------
Strings are first normalised (NFKC, so full-width and compatibility characters
become plain ones; control, format and line-separator characters other than tab,
newline and carriage return are dropped, which also defeats zero-width padding),
then, in order:

1. `private_values` given by the caller are replaced by `REDACTED`, case-insensitively,
   as substrings. Besides each value, its path components (split at a backslash or a
   slash) and its space-separated words of three or more characters are removed too,
   and a value with spaces also matches with a dot, underscore or dash (or nothing)
   between its words. The CALLER (the command, a later task) must pass the user name,
   the machine name, the home folder and the name of the organisation / cloud-sync
   folder: this module reads no environment and cannot find them itself. Passing the
   home folder also removes the first and the last name wherever they appear.
2. Paths, e-mail addresses and IP addresses (see "Paths" below).
3. Every account id learned in this session is replaced by `ACCOUNT-n`, anywhere,
   as a substring, in any case. Ids are learned from the `managedAccounts`
   callback (every token of three or more letters and digits in any of its
   arguments) and from any value passed under an account-like name (below), when
   it contains a digit; these are never dropped. Ids found by the pattern (step 4)
   are learned too, but only the first `MAX_PATTERN_IDS` of them (the pattern still
   catches every later one by its shape; only a later bare digit run without letters
   would be missed). The digit run (five or more digits) of a learned id is
   replaced too, so the bare digits of an id are caught once the id was learned.
   Learned ids are matched with several small patterns (32 ids each); learning one id
   recompiles at most one of them.
4. The pattern `ACCOUNT_PATTERN`: one to three letters, then up to three separator
   characters of any kind (anything that is not a letter or a digit: space, dot,
   colon, slash, non-breaking space, comma, equals, pipe, dash, underscore...; a
   backslash followed by a quote, as JSON writes a quote inside a string, counts as one),
   then five or more digits, which may themselves have up to two separator characters
   of any kind between digits (`12:34 56`, `12 - 34`). It applies cold, before
   `managedAccounts` has been seen, as well as warm, wherever it stands (also glued to
   other letters or digits). Every id it finds is learned too (step 3 then applies to
   it), except that a hit whose digits are a calendar date is learned as written but its
   digit run is not learned alone. Once an id is learned, its digit run also matches with
   up to two separator characters of any kind between its digits. It covers
   IB's id shapes (`U`, `DU`, `F`, `DF`, `I`, `DI` plus seven digits, longer for
   some institutional ids; `DU`, one more upper-case letter and six digits, the
   three-letter form of some paper accounts; a sub-account suffix after a dot goes with
   the digits when it is a number). The three letters are taken whole; once an id is
   known (learned from `managedAccounts`, or from a pattern hit), its letters are also
   removed when up to three separator characters stand between them and the digits, so
   no letter of an id is left beside its placeholder. It over-redacts on purpose: any token of one to
   three letters and five or more digits (an ISIN, a version string such as a letter, a
   space and a dotted number, a time such as `at 14:30:00`). There are three exemptions, all
   narrow. (a) An option symbol in OCC shape, `_OCC`: an optional root of up to six
   letters (IB pads it with spaces: `AAPL  240816C00220000`), six digits, `C` or `P`,
   eight digits, standing alone. (b) A trading-hours date after a word: IB writes a
   closed day as `<date>:CLOSED;<next date>:0400-<next date>:2000`, and the pattern would
   take the last two letters of CLOSED, the semicolon and the next date for an id,
   learn the date, and remove it from every later string, so a normal session would
   no longer replay. A hit is therefore left alone, and never learned, when its letters
   are immediately preceded by another ASCII letter (the tail of a longer word of four or
   more letters; a letter outside ASCII, and the end of a placeholder such as
   `IP-REDACTED`, are not a word; the pattern takes up to three letters, so a word of three
   letters or fewer before a date is never exempt), its first
   run of digits is exactly eight and a valid calendar date `YYYYMMDD` (year 1990 to
   2100), and the hit goes on with nothing or with a colon and digits only. What this
   loosens: cold only, an id of eight digits that happen to form a valid date AND is
   written glued to a preceding letter is not caught by the pattern; warm it is still
   caught, because an id learned from `managedAccounts` is matched as a substring
   whatever its digits. (c) The `T03:04:05` of an ISO 8601 time after a digit (the
   caller's `start_utc`): a capital T, exactly hh:mm:ss, an optional fraction and
   offset. The same placeholder `ACCOUNT-n` is used for the same id in any spelling.

Names. A name that is a method of `EWrapper` (a raw event) or `EClient` (a request) is
code, not session data, and is written as it is; so are the attribute names of objects
from the `ibapi` package (`tradingHours`, `headTimestamp`). A word in `private_values`
that happens to occur inside one of them therefore does not rename it. Keys of a plain
dict come from data and are redacted like values, and take part (with their value) in
the scan for an id split over adjacent leaves.

E-mail addresses. `local@host` is replaced, except an exchange token as IB writes it in
error texts (`SYMBOL@EXCHANGE`, error 162): a local part of at most six upper-case
letters and digits and an all-upper-case host without a dot. `user@host` in lower or
mixed case, and any other form, is still replaced. The cost: an upper-case
`NAME@HOST` of that shape is kept as well; `private_values` (step 1) removes the user
and machine names before this rule runs, which is why the caller must pass them.

Numbers. A value whose Python type is `int`, `float` or `Decimal` (not `str`, not
`bytes`) and which is not under an account-like name is replaced only when its
digits EQUAL the digit run of a learned id exactly (an integral float equals the
int; a sign and a trailing `.0` do not matter). It is not replaced when it merely
contains such a run. Reasoning: IB delivers an account id as text, never as a
number, so a number that equals one is the id through some conversion, while a
number that contains the same digits by coincidence (a conId, an epoch time, a
volume: about one in three million) is not the account, and blanking it breaks
replay. Residual risk: an id turned into a number and then combined with other
digits stays in that number. Strings, bytes, anything under an account-like name,
and numbers rendered inside strings keep the substring rule: any string that
contains a learned id or digit run is replaced there.

Paths. Once a path start is recognised (a drive letter and a colon and a slash or
backslash, a UNC double backslash, a git-bash `/c/` form, `/home/`, `/Users/` and
the other system folders, `~`, `file:`), the match consumes the whole path: to the
end of the quoted string if the path began inside quotes, otherwise to the end of
the line or the next character that cannot be in a Windows path (`<>|"*?` or a
control character). Spaces do not end a path. Anything after an unquoted path on
the same line goes with it (over-redaction accepted). Each element of an argv
`command` that looks like a path (contains a separator) is replaced whole, and the
command is written with paths replaced, never raw. A mail address is `local@host`
where the host need not have a dot; an IPv4 address, an IPv6 address and an
IPv4-mapped IPv6 address (colons, then an IPv4 address) are replaced whole.

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
for an id. A final check walks the arguments about to be written; if anything
account-shaped is left (a learned id or the pattern in a string, a learned digit run
equal to a number) the arguments are replaced by a marker. If redaction itself fails
the arguments are replaced by a marker naming the exception type. Both are counted
(`failures`).

Not covered: ids written in a look-alike alphabet (Cyrillic or Greek letters),
split across two *events*, split by something other than directly adjacent
arguments (an id split over a dict key and its value is caught only for keys of a plain
dict, and only when the key ends in the letters of the id, with at most one dash or
underscore after them), encoded (percent, base64, HTML
entities, hex, reversed) or spelled out in words; more than three separators between
the letters and the digits cold, or more than two between digits (cold AND warm: a
learned digit run is matched with at most two separators between its digits too, so a gap
of three characters inside the digits is not caught even once `managedAccounts` was
seen); an eight-digit id that forms a valid date and is glued to a
preceding letter, cold (see step 4); private data that is not an account, an address,
a path, an e-mail address or one of `private_values`; a recording's timing, which says
when the owner traded (bar times are kept; for a request that ends "now" they also
say roughly when the session ran). Words of one or two letters in a name are not
removed by `private_values` (only the whole value and its words of three or more
characters are). Without `private_values`, names can survive in relative paths, paths
without a recognised start, host names and display names in mail headers. Over-redaction
is accepted. The owner reads a redacted recording and runs `scan_recording` on it
before it is committed.

Before committing a real recording
----------------------------------
1. Record with the user name, machine name, home folder and the OneDrive / organisation
   folder name passed as `private_values`, and the `ibapi` logger at WARNING.
2. Run `scan_recording` with `words` = your account number(s), their digits alone, your
   first and last name, the machine and folder names; read every finding.
3. Search the file yourself for the account digits with any separator between them, and
   for each word of one or two letters in your names (those are not removed).
4. Read the header (`command`, `start_utc`), every `error` line and every `currentTime`
   line by eye.
5. Replay it (`tests/ib_replay.py`): `Recording.problems` must be empty, and the hours
   strings and `historicalDataEnd` values must still hold their dates.
6. Commit only after reading the whole file once; when in doubt, do not commit it.

`scan_recording(path, words=())` is a standalone checker for the owner. It does not
use the `Redactor`'s patterns and reports, with line numbers and a short reason,
what a careful reader should look at (never the matched text itself, only its first
two characters and its length). It flags: letters and five or more digits; any run of
six or more digits outside the places the format names as numbers; digit groups joined
by one or two separators (`12:34 56`) with six or more digits; a value in a date position
that is not a valid date (`YYYYMMDD`, 1990 to 2100) or epoch (9, 10 or 13 digits up to
2100); a long digit run under a numeric field name whose value is not a number (a string
under `conId`, a list under `price`), except the strings the format writes for `Decimal`
fields and epochs; digit-only dict keys of five or more digits; e-mail addresses (not
`SYMBOL@EXCHANGE`), IP addresses, paths, and `words`. It cannot see: encodings (base64,
percent-encoding, HTML entities, hex), ids split over several arguments or events,
look-alike letters, private data that is none of the above. A clean scan is NOT a proof;
the owner must still search the file for the account digits by hand. The command-line
wiring for it is a later task.

Threading: `on_request` and `on_raw` run on the reader, connect-helper and caller
threads. One lock covers redaction, numbering and the write. Nothing here calls
back into the connection, sleeps or blocks beyond one buffered write and flush;
an exception is caught and counted, never raised.
"""

from __future__ import annotations

import datetime
import enum
import inspect
import json
import math
import re
import threading
import types
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any, TextIO

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

FORMAT_NAME = "stoinkulator-ib-session"
FORMAT_VERSION = 1
REDACTION_VERSION = 3

MAX_STRING = 65536  # a longer string is not written, it is replaced by a note
MAX_PATTERN_IDS = 256  # ids learned from pattern hits; ids from managedAccounts / names are not capped
_TIER_SIZE = 32  # learned ids per compiled pattern

ACCOUNT_PATTERN = re.compile(
    r"[A-Za-z]{1,3}(?:\\[\"']|[^A-Za-z0-9]){0,3}\d(?:[^A-Za-z0-9]{0,2}\d){4,}"
)
_SPLIT_PATTERN = re.compile(r"[A-Za-z]{1,3}[-_]?\d{5,9}(?!\d)")
_DATE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
_OCC = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,6} *)?\d{6}[CcPp]\d{8}(?![A-Za-z0-9])")
_ACCOUNT_NAME = re.compile(r"acc(?:ount|t)", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9]+")
# Linear on any input: it starts only at the beginning of a run of address characters
# and the possessive quantifiers never give characters back.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]++(?:@[\w.+-]*+)+")
_EXCHANGE_TOKEN = re.compile(r"[A-Z0-9]{1,6}@[A-Z]+")  # AAPL@SMART in IB's error texts, not an address
_ISO_TIME = re.compile(r"T\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d(?::?\d\d)?)?")
_GAP = r"[^A-Za-z0-9]{0,2}"  # what a learned digit run tolerates between its digits
# what a learned id tolerates between its letters and its digits (as `ACCOUNT_PATTERN` does)
_LETTER_GAP = r"(?:\\[\"']|[^A-Za-z0-9]){0,3}"
_LETTERS_DIGITS = re.compile(r"([A-Za-z]{1,3})(\d{5,})(.*)", re.DOTALL)
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")
_IPV4_MAPPED = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?"
    r"|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?)(?![\w:])"
)
_PATH_START = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|\\\\"
    r"|(?<![\w/.])/[A-Za-z]/"
    r"|(?<![\w.])/(?:home|users|root|var|tmp|etc|opt|mnt|usr|private)/"
    r"|(?<![\w.])~(?=[\\/A-Za-z_])"
    r"|(?<![A-Za-z0-9])file:",
    re.IGNORECASE,
)
_PATH_STOP = re.compile("[<>|\"*?\\x00-\\x1f\\x7f]")
_PATHLIKE_TOKEN = re.compile(r"\S*[\\/]\S*")
_ARGV_FLAG = re.compile(r"(--?[\w-]+=)(.*)", re.DOTALL)
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
_KEEP_CONTROLS = "\t\n\r"
_MAX_DEPTH = 8
_MAX_NODES = 5000
_MAX_PLACEHOLDERS = 1000
_WIPED = "<redacted>"

_ParamNames = tuple[str, ...]


class _NumText(str):
    """A number rendered as text (a `Decimal`): the final check treats it as a number."""

    __slots__ = ()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _account_name(name: str) -> bool:
    return _ACCOUNT_NAME.search(name) is not None


def _norm_key(text: str) -> str:
    return "".join(_TOKEN.findall(text)).upper()


def _omitted(length: int) -> str:
    return f"<omitted: {max(1, length // 1024)} KiB>"


def _normalise(text: str) -> str:
    """NFKC, no control or format characters; a string over `MAX_STRING` becomes a note."""
    if len(text) > MAX_STRING:
        return _omitted(len(text))
    text = unicodedata.normalize("NFKC", text.encode("utf-8", "replace").decode("utf-8"))
    if len(text) > MAX_STRING:
        return _omitted(len(text))
    return "".join(
        c for c in text
        if c in _KEEP_CONTROLS or unicodedata.category(c) not in _DROP_CATEGORIES
    )


def _occ_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _OCC.finditer(text)]


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < b and a < span[1] for a, b in spans)


def _is_date(digits: str) -> bool:
    """Whether `digits` is exactly a calendar date `YYYYMMDD` with a year from 1990 to 2100."""
    if len(digits) != 8 or not (digits.isascii() and digits.isdigit()):
        return False
    try:
        datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return False
    return 1990 <= int(digits[:4]) <= 2100


def _glued_date(text: str, m: re.Match[str]) -> bool:
    """The one exemption from `ACCOUNT_PATTERN`: a pattern hit that is the tail of a word
    followed by a date, as in `CLOSED;<date>:0400-...` of IB's trading hours.

    All of: the hit's letters are the tail of a longer word (they are immediately preceded by
    another ASCII letter, which is not the end of a placeholder such as `IP-REDACTED`;
    the pattern takes up to three letters, so only a word of four or more qualifies); its
    first run of digits is exactly eight and a valid date; what follows that run inside the
    hit is nothing or a colon and more digits (a time)."""
    start = m.start()
    hit = m.group(0)
    count = len(re.match(r"[A-Za-z]*", hit).group(0))  # type: ignore[union-attr]
    # an id glued to a placeholder (`IP-REDACTED<id>`): the placeholder's last letters are no word
    if any(text.startswith("REDACTED", p - 8) for p in range(max(start, 8), start + count + 1)):
        return False
    before = text[start - 1] if start > 0 else ""
    if not (before.isascii() and before.isalpha()):
        return False
    lead = re.search(r"\d+", hit)
    if lead is None or not _is_date(lead.group(0)):
        return False
    rest = hit[lead.end():]
    return rest == "" or rest.startswith(":")


def _iso_time(text: str, m: re.Match[str]) -> bool:
    """The second exemption: the `T03:04:05` of an ISO 8601 date and time (the caller's
    `start_utc`): a capital T after a digit, then exactly hh:mm:ss, an optional fraction and offset."""
    start = m.start()
    return start > 0 and text[start - 1].isdigit() and _ISO_TIME.fullmatch(m.group(0)) is not None


def _exempt(text: str, m: re.Match[str]) -> bool:
    return _glued_date(text, m) or _iso_time(text, m)


def _pattern_hits(text: str) -> list[re.Match[str]]:
    """The `ACCOUNT_PATTERN` hits in `text` that count: not an option symbol, not a glued date."""
    occ = _occ_spans(text)
    return [
        m for m in ACCOUNT_PATTERN.finditer(text)
        if not _overlaps(m.span(), occ) and not _exempt(text, m)
    ]


def _email_sub(m: re.Match[str]) -> str:
    text = m.group(0)
    return text if _EXCHANGE_TOKEN.fullmatch(text) else "EMAIL-REDACTED"


def _scrub_paths(text: str) -> str:
    """Replace each path, whole: to the closing quote if it began inside quotes, else to
    the end of the line or the next character that cannot be in a Windows path."""
    out: list[str] = []
    pos = 0
    while True:
        m = _PATH_START.search(text, pos)
        if m is None:
            break
        start = m.start()
        quote = text[start - 1] if start > 0 and text[start - 1] in "\"'`" else None
        if quote is not None:
            end = text.find(quote, m.end())
            if end < 0:
                end = len(text)
        else:
            stop = _PATH_STOP.search(text, m.end())
            end = stop.start() if stop is not None else len(text)
        out.append(text[pos:start])
        out.append("PATH-REDACTED")
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _looks_like_path(text: str) -> bool:
    return (
        "\\" in text or "/" in text or text.startswith("~")
        or re.match(r"[A-Za-z]:", text) is not None or _PATH_START.search(text) is not None
    )


def _private_terms(values: Iterable[str]) -> list[str]:
    """Each value, its path components and its words of three or more characters."""
    terms: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = _normalise(raw).strip()
        if not value:
            continue
        terms.add(value)
        for component in re.split(r"[\\/]+", value):
            component = component.strip()
            if len(component) >= 3:
                terms.add(component)
            terms.update(w for w in component.split() if len(w) >= 3)
    return sorted(terms, key=len, reverse=True)


def _private_pattern(terms: Sequence[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    parts = ["[\\s._-]*".join(re.escape(w) for w in t.split()) for t in terms]
    return re.compile("|".join(p for p in parts if p), re.IGNORECASE)


def _id_regex(token: str) -> str:
    """A digit run of five or more digits also matches with up to two separator characters
    of any kind between its digits; an id with letters is matched as written."""
    if len(token) >= 5 and token.isascii() and token.isdigit():
        return _GAP.join(re.escape(c) for c in token)
    head = _LETTERS_DIGITS.match(token)
    if head is not None:  # letters then a digit run: up to three separators may stand between them
        return re.escape(head.group(1)) + _LETTER_GAP + _id_regex(head.group(2)) + re.escape(head.group(3))
    return re.escape(token)


def _compile_ids(ids: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted(ids, key=len, reverse=True)
    return re.compile("|".join(_id_regex(i) for i in ordered), re.IGNORECASE)


def _number_digits(value: int | float | Decimal) -> str | None:
    """The digits of a number without sign, fraction zeros or exponent, or None."""
    try:
        if isinstance(value, float):
            if not math.isfinite(value):
                return None
            dec = Decimal(repr(value))
        elif isinstance(value, Decimal):
            dec = value
        else:
            return str(abs(value)) if value.bit_length() < 4000 else None
        if not dec.is_finite() or dec.adjusted() > 40:
            return None
        text = format(abs(dec), "f")
    except (ArithmeticError, ValueError):
        return None
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class Redactor:
    """Turns raw callback / request arguments into JSON values with no account data.

    Not thread-safe: `SessionRecorder` calls it under its lock. Tests use it
    directly. `known_accounts` is the number of ids learned so far.
    """

    def __init__(self, private_values: Iterable[str] = ()) -> None:
        self._private = _private_pattern(_private_terms(private_values))
        self._numbers: dict[str, int] = {}
        self._alias: dict[str, str] = {}  # normalised id or digit run -> normalised id
        self._tiers: list[tuple[list[str], re.Pattern[str]]] = []
        self._digit_set: set[str] = set()  # all-digit learned ids and digit runs
        self._pattern_learned = 0
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

    def learn(self, token: str, *, from_pattern: bool = False, runs: bool = True) -> None:
        """Remember `token` as an account id of this session.

        Ids found by the pattern (`from_pattern`) stop being learned after
        `MAX_PATTERN_IDS`; all others are always learned. With `runs=False` the id is
        learned as written but its digit run is not learned on its own (used for a pattern
        hit whose digits are a calendar date, so that dates are not removed elsewhere)."""
        key = _norm_key(_normalise(token))
        if len(key) < 3 or key in self._alias:
            return
        if from_pattern:
            if self._pattern_learned >= MAX_PATTERN_IDS:
                return
            self._pattern_learned += 1
        self._alias[key] = key
        new = [key]
        for run in re.findall(r"\d{5,}", key) if runs else ():
            if run not in self._alias:
                self._alias[run] = key
                new.append(run)
        self._digit_set.update(i for i in new if i.isdigit())
        if self._tiers and len(self._tiers[-1][0]) + len(new) <= _TIER_SIZE:
            ids = self._tiers[-1][0]
            ids.extend(new)
            self._tiers[-1] = (ids, _compile_ids(ids))
        else:
            self._tiers.append((list(new), _compile_ids(new)))

    def _learn_text(self, text: str, *, everything: bool) -> None:
        for tok in _TOKEN.findall(_normalise(text)):
            if len(tok) >= 3 and (everything or (len(tok) >= 5 and any(c.isdigit() for c in tok))):
                self.learn(tok)

    def _learned_spans(self, text: str) -> list[tuple[int, int, str]]:
        """Spans of learned ids in `text`, overlapping ones merged (with the longest id's text)."""
        spans: list[tuple[int, int, str]] = []
        for _ids, rx in self._tiers:
            spans.extend((m.start(), m.end(), m.group(0)) for m in rx.finditer(text))
        if len(spans) < 2:
            return spans
        spans.sort(key=lambda s: (s[0], -s[1]))
        merged: list[tuple[int, int, str]] = [spans[0]]
        for a, b, t in spans[1:]:
            la, lb, lt = merged[-1]
            if a < lb:
                merged[-1] = (la, max(lb, b), t if len(t) > len(lt) else lt)
            else:
                merged.append((a, b, t))
        return merged

    def _learned_sub(self, text: str) -> str:
        spans = self._learned_spans(text)
        if not spans:
            return text
        out: list[str] = []
        pos = 0
        for a, b, t in spans:
            out.append(text[pos:a])
            out.append(self._placeholder(_norm_key(t)))
            pos = b
        out.append(text[pos:])
        return "".join(out)

    # -- strings ---------------------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Redact one string (headers, command lines)."""
        return self._redact_text(_normalise(text))

    def scrub_command(self, command: str | Sequence[str]) -> str:
        """The command line with paths replaced whole, never raw.

        A string is scrubbed, then every whitespace-separated token with a path
        separator is replaced. A sequence is taken as argv: each element that looks
        like a path (also the value of `--flag=value`) is replaced whole."""
        if isinstance(command, str):
            return _PATHLIKE_TOKEN.sub("PATH-REDACTED", self.scrub(command))
        parts: list[str] = []
        for element in command:
            text = _normalise(str(element))
            flag = _ARGV_FLAG.fullmatch(text)
            prefix, value = (flag.group(1), flag.group(2)) if flag else ("", text)
            shown = "PATH-REDACTED" if _looks_like_path(value) else self._redact_text(value)
            parts.append((self._redact_text(prefix) if prefix else "") + shown)
        return " ".join(parts)

    def _redact_text(self, s: str) -> str:
        if len(s) > MAX_STRING:
            return _omitted(len(s))
        if self._private is not None:
            s = self._private.sub("REDACTED", s)
        s = _scrub_paths(s)
        s = _EMAIL.sub(_email_sub, s)
        s = _IPV4_MAPPED.sub("IP-REDACTED", s)
        s = _IPV6.sub("IP-REDACTED", s)
        s = _IPV4.sub("IP-REDACTED", s)
        s = self._learned_sub(s)
        occ = _occ_spans(s)

        def by_pattern(m: re.Match[str]) -> str:
            if _overlaps(m.span(), occ) or _exempt(s, m):
                return m.group(0)
            digits = re.sub(r"\D", "", m.group(0))
            # the same id elsewhere is then caught too, unless its digits are a date
            self.learn(m.group(0), from_pattern=True, runs=not _is_date(digits))
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
        if self._learned_spans(text):
            return True
        return bool(_pattern_hits(text))

    def leaks_in_value(self, value: Any, depth: int = 0) -> bool:
        """The last check before a write, on the JSON value itself: a string (or key) that
        holds a learned id or the pattern, a number equal to a learned digit run. Any
        type it does not know counts as a leak."""
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, int | float | _NumText):
            digits = _number_digits(Decimal(value) if isinstance(value, _NumText) else value)
            return digits in self._digit_set
        if isinstance(value, str):
            return self.leaks(value)
        if depth > _MAX_DEPTH + 2:
            return True
        if isinstance(value, list):
            return any(self.leaks_in_value(v, depth + 1) for v in value)
        if isinstance(value, dict):
            return any(
                not isinstance(k, str) or self.leaks(k) or self.leaks_in_value(v, depth + 1)
                for k, v in value.items()
            )
        return True

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
        digits = _number_digits(v)
        if digits is not None and digits in self._digit_set:
            return self._placeholder(digits)
        if isinstance(v, Decimal):
            return _NumText(text)
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
            if len(raw) > MAX_STRING:
                text = _omitted(len(raw))
            else:
                text = "".join(chr(b) for b in raw if 32 <= b < 127)
                text = self._force_text(text) if force else self._redact_text(text)
            return {"__bytes__": len(raw), "text": text}
        if isinstance(v, type | types.ModuleType | types.FunctionType | types.BuiltinFunctionType
                      | types.MethodType) or callable(v):
            return f"<unserialisable {type(v).__name__}>"
        if id(v) in path:
            return "<cycle>"
        path = path | {id(v)}
        if isinstance(v, Mapping):
            return self._walk_items(None, v.items(), force, depth, path, False)
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
        module = type(v).__module__
        from_ibapi = module == "ibapi" or module.startswith("ibapi.")
        return self._walk_items(type(v).__name__, attrs.items(), force, depth, path, from_ibapi)

    def _walk_items(
        self, cls: str | None, items: Iterable[tuple[Any, Any]], force: bool, depth: int,
        path: frozenset[int], code_names: bool,
    ) -> dict[str, Any]:
        """`code_names`: the keys are attribute names of an `ibapi` object, which come from
        code and are written as they are. Keys of a plain dict come from data: they are
        redacted and, like values, take part in the scan for an id split over adjacent leaves."""
        out: dict[str, Any] = {}
        if cls is not None:
            out["__class__"] = cls
        for key, value in items:
            key_text = key if isinstance(key, str) else str(key)
            norm = _normalise(str.__str__(key_text))
            sub_force = force or _account_name(norm)
            if code_names:
                shown = norm
            elif cls is None:
                index = len(self._leaves)
                self._leaves.append(norm)
                shown = _WIPED if norm and index in self._wipe else self._redact_text(norm)
            else:
                shown = self._redact_text(norm)
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

    def event_name(self, kind: str, name: str) -> str:
        """The name of an event as written: a method of `EWrapper` (raw) or `EClient`
        (request) is code, not session data, and is never rewritten; any other name is scrubbed."""
        owner = EWrapper if kind == "raw" else EClient
        if name.isidentifier() and not name.startswith("_") and callable(getattr(owner, name, None)):
            return name
        return self.scrub(name)

    def _run(self, args: Sequence[Any], forced: set[int]) -> list[Any]:
        self._leaves = []
        self._nodes = 0
        return [self._walk(a, i in forced, 0, frozenset()) for i, a in enumerate(args)]

    def sanitise(self, kind: str, name: str, args: Sequence[Any]) -> list[Any]:
        """The JSON-safe, account-free form of one event's arguments."""
        args = tuple(args)
        names = self._param_names(kind, name)
        forced = {i for i, n in enumerate(names[: len(args)]) if _account_name(n)}
        if kind == "raw":  # IB's own wall-clock stamps: zeroed, see the module docstring
            args = tuple(
                0 if names[i : i + 1] == ("errorTime",) or (name == "currentTime" and i == 0) else a
                for i, a in enumerate(args)
            )
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
        spans = [(a, b) for a, b, _t in self._learned_spans(text)]
        spans += [m.span() for m in _pattern_hits(text)]
        return spans

    def split_candidates(self, text: str) -> list[tuple[int, int]]:
        """Like `leaks_in_text`, but stricter about the pattern: used on joined arguments."""
        spans = [(a, b) for a, b, _t in self._learned_spans(text)]
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
    the command line, a string or argv (redacted like everything else, paths whole),
    `port` the port connected to (never the host), `start_utc` an ISO string the caller
    supplies, `private_values` strings to remove wherever they occur: the caller must
    pass the user name, the machine name, the home folder and the organisation folder
    name. Counters: `lines_written`, `failures` (events whose redaction failed or left a
    leak), `write_failures`, `dropped` (events after `close`).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float],
        command: str | Sequence[str] = "",
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
            "command": self._redactor.scrub_command(command),
            "port": port if type(port) is int else None,
            "start_utc": self._redactor.scrub(start_utc) if isinstance(start_utc, str) else None,
            "error_time": "zeroed",
            "current_time": "zeroed",
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
                shown_name = self._redactor.event_name(kind, str(name))
            except Exception:
                shown_name = "<unknown>"
            marker: str | None = None
            try:
                out = self._redactor.sanitise(kind, str(name), args)
                if self._redactor.leaks_in_value(out):
                    marker = "<redaction check failed>"
                else:
                    body = _json_text(out)
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


# -- the owner's checker --------------------------------------------------------------
#
# Deliberately written apart from `Redactor`: its own patterns, its own notion of a
# known numeric position. It reports where to look, never what it found.


@dataclass(frozen=True)
class Finding:
    """One thing a reader should look at. `shown` is at most the first two characters of
    the matched text and its length; `line` is 1-based, 0 for the file as a whole."""

    line: int
    reason: str
    shown: str


LONG_RUN_REASON = "run of six or more digits in an unknown position"
SEPARATED_RUN_REASON = "digit groups joined by separators, six or more digits in an unknown position"
DIGIT_KEY_REASON = "dict key made of five or more digits"
INVALID_DATE_REASON = "digits in a date position that are not a valid date or epoch"
_S_LETTERS_DIGITS = re.compile(r"(?<![A-Za-z])[A-Za-z]++[^A-Za-z0-9]?(?:\d[ .,\-_]?){4,}\d")
_S_OCC = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,6} *)?\d{6}[CcPp]\d{8}(?![A-Za-z0-9])")
_S_LONG_DIGITS = re.compile(r"\d{6,}")
_S_EMAIL = re.compile(r"[\w.+-]@[\w-]")
_S_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_S_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){1,7}(?![\w:])")
_S_PATH = re.compile(
    r"[A-Za-z]:[\\/]|\\\\|(?<![\w/.])/(?:[A-Za-z]|home|users|root|var|tmp|etc|opt|mnt|usr|private)/"
    r"|(?<![\w.])~[\\/]|(?<![A-Za-z0-9])file:",
    re.IGNORECASE,
)
# Fields the recording format names as numbers, epoch times or prices: a long digit run is expected there.
_S_NUMERIC_NAMES = frozenset({
    "seq", "t", "port", "version", "redaction", "__bytes__", "conId", "volume", "wap", "barCount",
    "date", "time", "currentTime", "headTimestamp", "timestamp", "price", "size", "open", "high",
    "low", "close", "count", "minTick", "minSize", "sizeIncrement", "suggestedSizeIncrement",
    "orderId", "reqId", "tickerId", "tickType", "errorCode", "errorTime", "bidSize", "askSize",
    "lastSize", "volume24Hr", "totalVolume",
})
# Fields that carry a date or a date and time as text: eight-digit dates are expected there.
_S_DATE_NAMES = frozenset({
    "startDateStr", "endDateStr", "endDateTime", "startDateTime", "tradingHours", "liquidHours",
    "lastTradeDateOrContractMonth", "lastTradeDate", "contractMonth", "realExpirationDate",
    "start_utc", "start", "end", "date",
})
# Numbers the format writes as text (`Decimal`): a plain number string is expected there.
_S_DECIMAL_TEXT_NAMES = frozenset({
    "volume", "wap", "size", "minSize", "sizeIncrement", "suggestedSizeIncrement", "bidSize",
    "askSize", "lastSize", "volume24Hr", "totalVolume",
})
# Fields that carry an epoch in seconds as text.
_S_EPOCH_NAMES = frozenset({"date", "headTimestamp"})
# A comma separated list of small numbers, whose digit groups are not an id.
_S_LIST_NAMES = frozenset({"marketRuleIds"})
_S_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_S_SEP_DIGITS = re.compile(r"\d+(?:[^A-Za-z0-9]{1,2}\d+)+")
_S_EXCHANGE_TOKEN = re.compile(r"[A-Z0-9]{1,6}@[A-Z]+")
_S_EMAIL_TOKEN = re.compile(r"(?<![\w.+-])[\w.+-]*+@[\w.+-]*+")  # linear: starts at a run's beginning
_S_DIGIT_KEY = re.compile(r"\d{5,}")
_S_EPOCH_LIMIT = 4102444800  # 2100-01-01
_TICK_LAST_TIMESTAMP = 45
_TICK_RT_VOLUME = 48
_S_TICK_EPOCH = re.compile(r"\d{9,10}")
# price;size;time (13 digits, ms);total volume;vwap;single trade: numbers only, empty fields allowed
_S_RT_NUMBER = r"(?:\d{1,9}(?:\.\d{1,9})?)?"
_S_RT_VOLUME = re.compile(
    rf"{_S_RT_NUMBER};{_S_RT_NUMBER};(\d{{13}});(?:\d{{1,12}})?;{_S_RT_NUMBER};(?:true|false)"
)


def _s_valid_date(run: str) -> bool:
    """Eight digits that are a calendar date, year 1990 to 2100."""
    if len(run) != 8 or not (run.isascii() and run.isdigit()):
        return False
    year, month, day = int(run[:4]), int(run[4:6]), int(run[6:])
    if not 1990 <= year <= 2100 or not 1 <= month <= 12:
        return False
    days = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31)
    return 1 <= day <= days[month - 1]


def _s_valid_epoch(run: str) -> bool:
    """Nine or ten digits of an epoch in seconds, or thirteen of one in milliseconds, up to 2100."""
    if not (run.isascii() and run.isdigit()):
        return False
    if len(run) in (9, 10):
        return int(run) <= _S_EPOCH_LIMIT
    return len(run) == 13 and int(run) <= _S_EPOCH_LIMIT * 1000


def _s_shown(text: str) -> str:
    n = len(text)
    head = text[: min(2, n - 1)] if n > 1 else ""
    return f"{head!r}.. ({n} chars)"


def _s_text_findings(
    text: str, line: int, name: str | None, words: Sequence[str], *, structured: bool,
) -> list[Finding]:
    found: list[Finding] = []
    occ = [m.span() for m in _S_OCC.finditer(text)]
    in_date = name in _S_DATE_NAMES
    for m in _S_LETTERS_DIGITS.finditer(text):
        if _overlaps(m.span(), occ):
            continue
        lead = re.search(r"\d+", m.group(0))
        if in_date and lead is not None and _s_valid_date(lead.group(0)):
            continue  # CLOSED;<date> in trading hours: a date after a word
        found.append(Finding(line, "letters then five or more digits", _s_shown(m.group(0))))
    for rx, reason in ((_S_IPV4, "IPv4 address"), (_S_PATH, "path")):
        m = rx.search(text)
        if m:
            found.append(Finding(line, reason, _s_shown(m.group(0))))
    for m in _S_EMAIL_TOKEN.finditer(text):
        token = m.group(0)
        if _S_EMAIL.search(token) and _S_EXCHANGE_TOKEN.fullmatch(token) is None:
            found.append(Finding(line, "e-mail address", _s_shown(token)))
            break
    for m6 in _S_IPV6.finditer(text):
        token = m6.group(0)
        if "::" in token or token.count(":") >= 7:
            found.append(Finding(line, "IPv6 address", _s_shown(token)))
            break
    if structured:
        if name in _S_DECIMAL_TEXT_NAMES and _S_PLAIN_NUMBER.fullmatch(text):
            whole_ok = True  # a Decimal written as text: a volume, a price, a size
        elif name in _S_EPOCH_NAMES and _s_valid_epoch(text):
            whole_ok = True  # the whole string is an epoch in seconds
        else:
            whole_ok = False
        if not whole_ok:
            for m in _S_LONG_DIGITS.finditer(text):
                run = m.group(0)
                if _overlaps(m.span(), occ):
                    continue  # an option symbol in OCC shape
                if in_date and _s_valid_date(run):
                    continue
                reason = INVALID_DATE_REASON if in_date and len(run) == 8 else LONG_RUN_REASON
                found.append(Finding(line, reason, _s_shown(run)))
            if not in_date and name not in _S_LIST_NAMES and name not in _S_DECIMAL_TEXT_NAMES:
                for m in _S_SEP_DIGITS.finditer(text):
                    digits = sum(c.isdigit() for c in m.group(0))
                    if digits >= 6 and not _overlaps(m.span(), occ):
                        found.append(Finding(line, SEPARATED_RUN_REASON, _s_shown(m.group(0))))
    low = text.casefold()
    for i, word in enumerate(words, 1):
        if word and word.casefold() in low:
            found.append(Finding(line, f"caller-supplied word number {i}", _s_shown(word)))
    return found


def _s_walk(
    value: Any, line: int, name: str | None, words: Sequence[str], depth: int = 0,
) -> list[Finding]:
    if depth > 40:
        return [Finding(line, "nesting too deep to scan", "")]
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        text = repr(value)
        if isinstance(value, float):  # IB's "unset" value and tiny fractions have long digit runs
            text = "" if "e" in text else text.partition(".")[0]
        if name in _S_NUMERIC_NAMES or not _S_LONG_DIGITS.search(text):
            return []
        return [Finding(line, LONG_RUN_REASON, _s_shown(text))]
    if isinstance(value, str):
        return _s_text_findings(value, line, name, words, structured=True)
    out: list[Finding] = []
    if isinstance(value, list):
        # a list under a numeric name is not a number: its items get no numeric exemption
        child = None if name in _S_NUMERIC_NAMES else name
        for v in value:
            out.extend(_s_walk(v, line, child, words, depth + 1))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_s_text_findings(str(k), line, None, words, structured=False))
            if _S_DIGIT_KEY.fullmatch(str(k)):
                out.append(Finding(line, DIGIT_KEY_REASON, _s_shown(str(k))))
            out.extend(_s_walk(v, line, k if isinstance(k, str) else None, words, depth + 1))
    else:
        out.append(Finding(line, "unexpected JSON value", ""))
    return out


def _s_known_tick_string(args: list[Any]) -> bool:
    """`tickString(reqId, tickType, value)` whose value has the exact shape of its tick type:
    type 45 (last timestamp) is an epoch in seconds; type 48 (RTVolume) is
    `price;size;time in ms;total volume;vwap;single trade flag`. Anything else is not known."""
    if len(args) != 3 or not isinstance(args[2], str):
        return False
    if type(args[0]) is not int or type(args[1]) is not int:
        return False
    tick_type, value = args[1], args[2]
    if tick_type == _TICK_LAST_TIMESTAMP:
        return _S_TICK_EPOCH.fullmatch(value) is not None and _s_valid_epoch(value)
    if tick_type == _TICK_RT_VOLUME:
        m = _S_RT_VOLUME.fullmatch(value)
        return m is not None and _s_valid_epoch(m.group(1))
    return False


def _s_event_findings(event: dict[str, Any], line: int, words: Sequence[str]) -> list[Finding]:
    out: list[Finding] = []
    kind, name, args = event.get("kind"), event.get("name"), event.get("args")
    if kind not in ("request", "raw") or not isinstance(name, str) or not isinstance(args, list):
        out.append(Finding(line, "event line without kind, name and argument list", ""))
    for key in ("seq", "t"):
        if key in event:
            out.extend(_s_walk(event[key], line, key, words))
    owner = EWrapper if kind == "raw" else EClient
    fn = getattr(owner, name, None) if isinstance(name, str) and not name.startswith("_") else None
    if isinstance(name, str):
        # the name of an EWrapper / EClient method is code: a caller word inside it is no finding
        own_words = () if callable(fn) and name.isidentifier() else words
        out.extend(_s_text_findings(name, line, None, own_words, structured=False))
    if not isinstance(args, list):
        out.extend(_s_walk(args, line, None, words))
        return out
    params: list[str] = []
    if callable(fn):
        try:
            params = list(inspect.signature(fn).parameters)[1:]
        except (TypeError, ValueError):
            params = []
    for i, arg in enumerate(args):
        if kind == "raw" and name == "tickString" and i == 2 and _s_known_tick_string(args):
            # a last-trade stamp or a real-time volume string: known numeric shapes, only the words count
            out.extend(_s_text_findings(arg, line, None, words, structured=False))
            continue
        out.extend(_s_walk(arg, line, params[i] if i < len(params) else None, words))
    return out


def scan_recording(path: str | Path, words: Iterable[str] = ()) -> list[Finding]:
    """Check a written recording for what must not be in it; an empty list means nothing found.

    Flags, per line, with line number and a short reason (the matched text is never
    shown in full: at most its first two characters and its length): letters, at most
    one separator and five or more digits (an OCC option symbol excepted, and a date
    after a word in a date-named field); a run of six or more digits outside the places
    the format names as numbers (epoch times, conId, prices, volumes, dates), also under
    a numeric field name when the value is text or a list (except the text the format
    writes for `Decimal` fields and epochs); digit groups joined by one or two
    separators with six or more digits (outside date fields); a value in a date field
    that is not a valid date or epoch; a dict key of five or more digits only; an e-mail
    address (not `SYMBOL@EXCHANGE`), IP address or path; any of `words`
    (case-insensitive: user name, machine name, folder names, the account number; not
    held against the name of an `EWrapper` / `EClient` method); any line that is not
    valid JSON. The value of a `tickString` of type 45 (an epoch in seconds) or 48 (real-time
    volume, `price;size;time;volume;vwap;flag`, numbers only) is a known numeric shape and is
    held against `words` only, so market data does not drown the findings (a bare digit run
    inside such a value, as a size or a volume, is therefore NOT flagged; a letter-prefixed
    id shape makes the value an unknown shape, which is scanned); any other
    shape of those, and every other tick type, is scanned as text. It fails closed: a file
    that cannot be read, is not UTF-8, is empty or
    has no session header is itself a finding.

    It cannot see: encodings (base64, percent-encoding, HTML entities, hex), an id split
    over several arguments or events, look-alike letters, or private data of no
    recognisable shape. A clean result is not proof: the owner must still search the
    file for the account digits by hand (see "Before committing a real recording").

    It uses its own patterns, not the `Redactor`'s, so a blind spot of the recorder is
    not shared. The command-line wiring is a later task.
    """
    wordlist = [w for w in words if isinstance(w, str)]
    try:
        raw = Path(path).read_bytes()
    except Exception as exc:
        return [Finding(0, f"file could not be read: {type(exc).__name__}", "")]
    findings: list[Finding] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding(0, "file is not valid UTF-8", ""))
        text = raw.decode("utf-8", "replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return [*findings, Finding(0, "file is empty", "")]
    for number, content in enumerate(lines, 1):
        try:
            parsed = json.loads(content)
        except (ValueError, RecursionError):
            findings.append(Finding(number, "line is not valid JSON", _s_shown(content)))
            findings.extend(_s_text_findings(content, number, None, wordlist, structured=False))
            continue
        if not isinstance(parsed, dict):
            findings.append(Finding(number, "line is not a JSON object", ""))
            findings.extend(_s_text_findings(content, number, None, wordlist, structured=False))
        elif number == 1:
            if parsed.get("format") != FORMAT_NAME:
                findings.append(Finding(1, "first line is not a session header", ""))
            findings.extend(_s_walk(parsed, number, None, wordlist))
        else:
            findings.extend(_s_event_findings(parsed, number, wordlist))
    seen: set[tuple[int, str]] = set()
    unique: list[Finding] = []
    for f in findings:
        if (f.line, f.reason) not in seen:
            seen.add((f.line, f.reason))
            unique.append(f)
    return unique
