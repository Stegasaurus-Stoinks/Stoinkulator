"""PreToolUse hook: block shell commands that would launch the live trading program.

This repo places real orders through Interactive Brokers. Two files start the
live program and must never be executed by an agent (see CLAUDE.md):

* ``MainStoinker/MainStuff/this is how we do it dum dum.py`` (live entry script)
* ``Stonks data collect.bat`` (launcher: git pull, pip install, entry script)

Claude Code runs this script before each shell tool call (``Bash`` and
``PowerShell``) and passes one JSON object on stdin; the command line is
``tool_input.command``.

Exit-code contract:
    0  the tool call proceeds
    2  the tool call is BLOCKED; stderr is shown to the model as the reason
    any other code is a non-blocking hook error (the call proceeds)

A legacy-broker-script rule (the scripts are deleted; this is a backstop): a segment
with an interpreter token, or a path as command word, that names ``MainStoinker/testy/
<x>.py``, ``IBAPI_TESTING.py`` or ``MainStoinker.testy`` is blocked, and so is a bare
``testy.py`` after a ``cd`` into ``MainStoinker/testy``. Not followed: a ``cd`` made in
an earlier call. Like the entry script, ``python -m ruff check <that .py>`` is blocked.

A third rule blocks connecting ``MainStoinker`` commands (allowlist): running
the package (``-m MainStoinker``, ``MainStoinker/__main__.py``, the directory,
``runpy``) is blocked unless its first positional argument is one of
``backtest``, ``runs``, ``report``, ``serve`` (or there is none); anything under
``MainStoinker.ib`` run or imported from code is blocked; runpy/import/exec/
subprocess text that names MainStoinker is blocked, because the subcommand is
not visible there. Only the owner runs connecting commands. Any other
``-m MainStoinker.<x>`` is blocked (a module allowlist). Also blocked: a
redirect or comment the tokenizer does not follow (``>|``, ``<>``, ``>&``,
``&>``, ``<<``, ``<<<``, ``<# #>``) or an expression (``(``, ``[``, ``{``, ``@``,
``-replace``, ``-join``) before the subcommand; Python code that names
MainStoinker next to ``__main__``, ``sys.argv``, ``runpy``, ``pkgutil``,
``importlib``, ``__import__``, ``exec``, ``eval``, ``compile`` or ``ib``; ``-m``
followed by a name that is not a literal identifier when the command names the
package (or a glob that matches it); ``..`` in script paths is resolved; the
command is also judged after Unicode NFKC folding; a ForEach-Object loop may run
``backtest``/``runs``/``report``/``serve`` when the word is written out (xargs
may not). The rule joins
continuation lines (backslash, backtick or caret before a line break), splits
arguments on ``, ( ) @ [ ] { }`` too (PowerShell argument arrays), sees through
``xargs``/``env``/``timeout``/``start``/``conda run`` and the like, judges text
piped into a shell, decodes ``-EncodedCommand``, follows a ``cd`` into the
package directory, and blocks a command over 50,000 characters that mentions
the package. Blocked by design, even though they only print help:
``fetch-bars --help``, ``probe-data -h``, ``python -c "import MainStoinker.ib..."``.

The ONE allowance (``allowed_paper_data_command``, owner decision 2026-11-23): an agent
may run a DATA session against the PAPER account, written as one whole plain command
``<venv python> -P -m MainStoinker (fetch-bars|probe-data) <args>`` (``-P`` is mandatory:
the current directory is never put first on ``sys.path``). ``<venv python>``
is exactly ``.venv\\Scripts\\python.exe`` (or ``.venv/Scripts/python.exe``) or a
drive-absolute path ``X:\\...\\.venv\\Scripts\\python.exe`` (no ``..``, no ``=``, no other
colon; never a UNC or ``/c/...`` path), optionally double-quoted, optionally preceded by
``& ``. It is recognised by that path, not by identity: any python copied to such a
path, or another checkout's venv, would match. ``<args>`` are plain tokens (letters,
digits and ``_ , . : = \\ / -``, or a double-quoted token that may also hold spaces) that
include ``--paper`` and ``--no-prompt`` as exact standalone tokens; ``--port`` only
``7497``, ``--host`` only ``127.0.0.1``; a backslash only inside path VALUES, never in an
option token; ``--help`` and single-dash options are refused. The match is anchored on
the whole string: no separator, wrapper, redirect, expansion, quote other than ``"``,
newline, ``cd`` or variable assignment, and the live opt-in variable name nowhere. A
command that merely CONTAINS that shape is not covered and is judged by the rules above.
It is not a free pass: ``allowed_paper_data_command`` also judges the same text with
only the subcommand word replaced by ``backtest`` by ALL the rules here, and every other
rule has already judged the text unchanged, so a command blocked for any other reason
stays blocked. The allowance applies to the command as typed, never to its NFKC form or
to decoded text. What it permits is data requests against the paper account: ``--paper``
makes the command itself refuse any port but 7497, any host but 127.0.0.1 and any
non-paper account (see ``MainStoinker/ib/cli.py``). The hook does NOT check which code
is checked out: a paper session must be run from an unmodified, merged ``origin/revival``
checkout (a rule in CLAUDE.md, not enforced here). Order placement, the live account and
the live ports remain the owner's alone.
The same allowance has a SECOND module form (owner decision 2026-11-23, unattended live bar
watcher on the paper account): ``<venv python> -P -m MainStoinker.ib.watch_bars <args>``, with
the same interpreter rule, token rules and guarded options, and in addition ``--minutes``
exactly once, followed by an unquoted token of 1 to 3 digits (or ``--minutes=NNN``): a bounded
run (the watcher itself refuses 0 and over 420). ``--minutes`` joins the guarded names for this
form (no abbreviation, no other spelling). The module name is matched exactly and case-
sensitively: ``MainStoinker.ib.cli``, ``.ib.probe``, ``.ib``, ``.ib.watch_bars.x``, a trailing
dot, the script path and ``-c`` are not it. Words after the module are plain arguments: the
watcher has no positional argument, so ``-m MainStoinker.ib.watch_bars fetch-bars ...`` is
allowed by the hook and ends as a usage error of the watcher (exit 2, nothing built).
How the neutral re-judgement works for the module form: ``_paper_neutral`` replaces exactly
the module suffix ``.ib.watch_bars`` by `` backtest``, giving ``-m MainStoinker backtest
<args>`` with every argument unchanged. The rest of the hook judges that text, and finds it
harmless ONLY because ``backtest`` is an offline subcommand of the package: that is the same
neutral text as for the two subcommands, so a path under ``MainStoinker/ib`` in a value, a
second module, ``-c`` code, a wrapper, the entry script and the like still block it, and
every rule that runs before the allowance has already judged the ORIGINAL text.
Not seen: a script or file that itself imports and runs the package (written
in one call, run in another), ``bash script.sh``, aliases/functions, a module
name built without the package name appearing anywhere (``'Main'+'Stoinker'``,
a non-empty ``$(...)``), tools that run a command themselves (``rg --pre``),
copying the package.
When the hook itself fails (bad JSON, an exception), a request that mentions
the package is blocked, like one that mentions the entry script or launcher.

Reading, searching, diffing, linting and editing the protected files is
allowed; only EXECUTING them is blocked. Commit / issue / PR message text of
``git`` and ``gh`` commands (``-m``, ``--message``, ``-b``, ``--body``, ``-t``,
``--title`` arguments and ``cat``/``git``/``gh`` heredoc bodies) is not scanned;
everything else in the command still is. Unterminated quotes or heredocs are
never removed, they are scanned like any other text.

This is a tripwire against accidents, NOT a security boundary. Known bypasses:
copying or renaming the script, wrapper files that run it, globs, building the
path dynamically (variables, concatenation, encoding), and running the ``.py``
by file association (``& ".\\...dum dum.py"``, ``./MainStoinker/.../dum dum.py``),
which has no interpreter token and is NOT covered. Also not covered: ``find
-exec`` (entry script and launcher only), ``watch``, ``if exist`` and leading
redirects.

Accepted false positives (blocked although harmless):
* searching the entry script for an interpreter word, e.g.
  ``grep -n python "<entry>"`` (the word ``python``/``py`` plus the entry name);
* ``echo`` / ``printf`` of text containing an interpreter word and the entry
  script name;
* ``python -m ruff check "<entry>"`` and ``python tools/lint.py "<entry>"``;
* a double-quoted commit message containing ``$(...)`` or backticks is scanned,
  because those would execute;
* separators inside quotes still split segments, e.g.
  ``rg -n "start|call" "<launcher>"`` is blocked (second piece starts with
  ``call``).

Standard library only; must not import anything from the repo.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
import sys
import unicodedata
from fnmatch import fnmatchcase
from typing import BinaryIO

ENTRY_NAME = "dum dum.py"
LAUNCHER_NAME = "stonks data collect.bat"

# Lone '&' is a separator; '&&', '2>&1', '>&2', '&>' are not lone.
_SEGMENT_SPLIT = re.compile(
    r";|&&|\|\||\||\r\n|\n|\r|(?<![>&<])&(?![>&])"
)
_PYTHON_TOKEN = re.compile(r"^(?:python[\d.]*|pythonw|py|ipython)(?:\.exe)?$")
_RUNNER = re.compile(r"(?<![\w-])(?:uv|pipx) run(?![\w-])")
_CMD_SWITCH = re.compile(r"^/{1,2}[ck]")
_ASSIGN = re.compile(r"^\w+=\S*$")
_TRANSPARENT = frozenset(
    {"&", "call", "nohup", "env", "wsl", "sudo", "time", "exec"}
)
_LAUNCH_WORDS = frozenset(
    {
        "start",
        "start-process",
        "saps",
        "invoke-item",
        "ii",
        "invoke-expression",
        "iex",
        "bash",
        "sh",
        "powershell",
        "pwsh",
        ".",
        "source",
    }
)
_START_WORDS = frozenset({"start", "start-process", "saps"})
# Commands that only read, search, list or edit their arguments.
_READERS = frozenset(
    {
        "cat", "type", "head", "tail", "less", "more", "grep", "egrep", "fgrep",
        "rg", "sed", "awk", "wc", "ls", "dir", "stat", "file", "notepad", "code",
        "diff", "git", "gh", "ruff", "select-string", "sls", "get-content", "gc",
        "test-path", "get-childitem", "gci", "get-item", "gi", "get-filehash",
        "bat", "vim", "nano", "echo", "printf", "write-output", "copy", "cp",
        "mv", "move", "del", "rm", "md5sum", "sha256sum",
    }
)  # fmt: skip

# --- message / heredoc removal ------------------------------------------------
_DQ = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
_SQ = re.compile(r"'[^']*'")
_ANSI = re.compile(r"\$'(?:[^'\\]|\\.)*'", re.S)
_GIT_WORD = re.compile(r"(?<![\w./\\-])(?:git|gh)(?:\.exe)?(?=[ \t])", re.I)
_MSG_FLAG = re.compile(r"^(?:-[a-z]*[mbt]|--(?:message|body|title))$", re.I)
_MSG_FLAG_EQ = re.compile(r"^--(?:message|body|title)=$", re.I)
_PREFIX_RE = re.compile(
    r"\s*(?:(?:\w+=\S*|&|call|nohup|env|sudo|time|wsl|timeout\s+\d+)\s+)*"
)
_HEREDOC = re.compile(
    r"(?<!<)<<(-?)[ \t]*(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|([A-Za-z_]\w*))"
)
_CMD_PIECE_SPLIT = re.compile(r"[;&|]")
_MAX_MATCHES = 200

_BLOCK_MESSAGE = (
    "Blocked: this command would run the live trading entry script or the "
    "launcher batch file ({what}). Agents must never execute these because they "
    "connect to Interactive Brokers and place real orders (see CLAUDE.md safety "
    "rules). Reading, searching, diffing or editing the file is fine."
)
_LEGACY_MESSAGE = (
    "Blocked: legacy broker test script (connects to the broker when run or imported)."
)
_LEGACY_PATH = re.compile(r"mainstoinker/+(?:\./+)*testy/+[\w.-]+\.py\b|ibapi_testing\.py")
_LEGACY_MODULE = re.compile(r"mainstoinker\.testy\b")
_LEGACY_BARE = re.compile(r"(?<![\w./-])(?:\./+)?testy(?:_advanced)?\.py\b")
_CD_WORDS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})
_PARSE_BLOCK_MESSAGE = (
    "Blocked: the live-launch hook could not parse this tool request, and the "
    "request mentions a protected live-trading file (dum dum / data collect.bat) "
    "or the MainStoinker package. Agents must never execute these (see CLAUDE.md "
    "safety rules); reading or editing them is fine."
)


def _cmd_base(token: str) -> str:
    base = token.rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".exe") else base


def _quoted_at(raw: str, i: int) -> int | None:
    """End index of a quoted string starting at raw[i]; -1 if unterminated; None if none."""
    c = raw[i : i + 1]
    if c == '"':
        m = _DQ.match(raw, i)
    elif c == "'":
        m = _SQ.match(raw, i)
    elif c == "$" and raw[i + 1 : i + 2] == "'":
        m = _ANSI.match(raw, i)
    else:
        return None
    return m.end() if m else -1


def _command_position(raw: str, pos: int) -> bool:
    window = raw[max(0, pos - 200) : pos]
    idx = max(window.rfind(ch) for ch in ";|&\n\r(")
    if idx == -1 and pos > 200:
        return False
    return _PREFIX_RE.fullmatch(window[idx + 1 :]) is not None


def _first_word(piece: str) -> str:
    words = piece.split()
    i = 0
    while i < len(words) and (words[i].lower() in _TRANSPARENT or _ASSIGN.match(words[i])):
        i += 1
    return _cmd_base(words[i].lower().strip("\"'")) if i < len(words) else ""


def _consumer_is_textual(prefix: str) -> bool:
    """Heredoc body is plain text: git/gh/cat directly, or cat inside git/gh's $(...)."""
    seg = _CMD_PIECE_SPLIT.split(prefix)[-1]
    sub = max(seg.rfind("$("), seg.rfind("`"))
    if sub == -1:
        return _first_word(seg) in {"cat", "git", "gh"}
    inner = seg[sub + (2 if seg.startswith("$(", sub) else 1) :]
    return _first_word(inner) == "cat" and _first_word(seg[:sub]) in {"git", "gh"}


def _strip_heredocs(raw: str) -> str:
    """Drop heredoc bodies consumed by cat/git/gh; interpreters/shells stay scanned."""
    out: list[str] = []
    pos = 0
    count = 0
    n = len(raw)
    for m in _HEREDOC.finditer(raw):
        if m.start() < pos:
            continue
        count += 1
        if count > 50:
            break
        tag = m.group(2) or m.group(3) or m.group(4)
        eol = raw.find("\n", m.end())
        if eol == -1 or "|" in raw[m.end() : eol]:
            continue
        line_start = raw.rfind("\n", 0, m.start()) + 1
        if not _consumer_is_textual(raw[line_start : m.start()]):
            continue
        p = eol + 1
        term = -1
        while p <= n:
            e = raw.find("\n", p)
            if e == -1:
                e = n
            line = raw[p:e].rstrip("\r")
            if m.group(1):
                line = line.lstrip("\t")
            if line == tag:
                term = p
                break
            if e == n:
                break
            p = e + 1
        if term == -1:
            continue
        out.append(raw[pos : eol + 1])
        pos = term
    out.append(raw[pos:])
    return "".join(out)


def _strip_message_args(raw: str) -> str:
    """Remove quoted message/body/title arguments of git/gh commands."""
    spans: list[tuple[int, int]] = []
    scanned = 0
    count = 0
    n = len(raw)
    for m in _GIT_WORD.finditer(raw):
        if m.start() < scanned:
            continue
        count += 1
        if count > _MAX_MATCHES:
            break
        if not _command_position(raw, m.start()):
            continue
        i = m.end()
        while i < n:
            c = raw[i]
            if c in " \t":
                i += 1
                continue
            if c in ";|\n\r)":
                break
            if c == "&":
                if raw[i - 1 : i] in (">", "<") or raw[i + 1 : i + 2] == ">":
                    i += 1
                    continue
                break
            q = _quoted_at(raw, i)
            if q is not None:
                if q == -1:
                    i = n
                    break
                i = q
                continue
            j = i
            while j < n and raw[j] not in " \t;|&\n\r)\"'":
                if raw[j] == "$" and raw[j + 1 : j + 2] == "'":
                    break
                j += 1
            if j == i:
                j = i + 1
            word = raw[i:j]
            i = j
            k = -1
            if _MSG_FLAG.match(word):
                k = i
                while k < n and raw[k] in " \t":
                    k += 1
            elif _MSG_FLAG_EQ.match(word):
                k = i
            if k != -1 and k < n:
                q = _quoted_at(raw, k)
                if q is not None and q != -1:
                    arg = raw[k:q]
                    if not (arg[0] == '"' and ("$(" in arg or "`" in arg)):
                        spans.append((k, q))
                    i = q
        scanned = i
    if not spans:
        return raw
    out: list[str] = []
    pos = 0
    for a, b in spans:
        out.append(raw[pos:a])
        out.append('""')
        pos = b
    out.append(raw[pos:])
    return "".join(out)


def _normalise(command: str) -> str:
    text = command.lower()
    text = text.replace("\\ ", " ").replace("` ", " ")
    text = text.replace("\\", "/")
    text = text.replace("'", "").replace('"', "")
    return re.sub(r"[ \t]+", " ", text)


def _segments(command: str) -> list[str]:
    parts = _SEGMENT_SPLIT.split(_normalise(command))
    return [p.strip() for p in parts if p.strip()]


def _has_interpreter(segment: str) -> bool:
    if "exec(" in segment or "runpy" in segment or "debugpy" in segment:
        return True
    if _RUNNER.search(segment):
        return True
    for tok in re.split(r"[\s=]+", segment):
        tok = tok.strip("(){}$`")
        if _PYTHON_TOKEN.match(tok.rsplit("/", 1)[-1]):
            return True
    return False


def _git_runs_args(words: list[str]) -> bool:
    """True for git forms that execute their arguments (bisect run, -x, foreach, -c k=v)."""
    for i, w in enumerate(words):
        if w in ("bisect", "-x", "foreach") or w.startswith("--exec"):
            return True
        if w == "-c" and i + 1 < len(words) and "=" in words[i + 1]:
            return True
    return False


def _runs_legacy_script(segment: str, in_testy: bool) -> bool:
    """True when an interpreter (or a path as command word) names a legacy broker script."""
    seg = segment
    if ".." in seg:  # resolve a/../ per token (linear)
        seg = " ".join(posixpath.normpath(t) if ".." in t else t for t in seg.split())
    if not (
        _LEGACY_PATH.search(seg)
        or _LEGACY_MODULE.search(seg)
        or (in_testy and _LEGACY_BARE.search(seg))
    ):
        return False
    words = [w for w in seg.split() if w not in _TRANSPARENT and not _ASSIGN.match(w)]
    return _has_interpreter(seg) or bool(words and words[0].endswith(".py"))


def _executes_launcher(segment: str) -> bool:
    """True when the launcher is in command position or run by a launch word."""
    words = segment.split()
    i = 0
    while i < len(words):
        w = words[i]
        if w in _TRANSPARENT or _ASSIGN.match(w):
            i += 1
        elif w == "timeout" and i + 1 < len(words) and words[i + 1].isdigit():
            i += 2
        else:
            break
    rest = " ".join(words[i:])
    idx = rest.find(LAUNCHER_NAME)
    if idx == -1:
        return False
    pre = rest[:idx]
    pre_tokens = pre.split()
    if not pre_tokens:
        return True
    # A path containing spaces can hide the real command word, so the last
    # ".exe" token is a second candidate. cmd / launch words win over readers.
    bases = [_cmd_base(pre_tokens[0])]
    exes = [t for t in pre_tokens if t.endswith(".exe")]
    if exes:
        bases.append(_cmd_base(exes[-1]))
    if "cmd" in bases:
        return any(_CMD_SWITCH.match(t) for t in words)
    if any(b in _LAUNCH_WORDS for b in bases):
        if bases[0] in _START_WORDS and any(
            _cmd_base(t) in _READERS for t in pre_tokens[1:3]
        ):
            return False  # "start notepad <launcher>": opens an editor
        return True
    if "git" in bases and _git_runs_args(words):
        return any(
            _cmd_base(t) in _LAUNCH_WORDS - {".", "source"}
            or (_cmd_base(t) == "cmd" and any(_CMD_SWITCH.match(u) for u in words))
            for t in words[1:]
        )
    if any(b in _READERS for b in bases):
        return False
    # Direct run: the segment is (a directory path ending in) the launcher.
    return pre.endswith("/") and ("/" in pre_tokens[0] or ":" in pre_tokens[0])


# --- connecting MainStoinker commands -----------------------------------------
# Allowlist: only these subcommands of ``python -m MainStoinker`` are known to
# work offline. Any other first positional argument (fetch-bars, probe-data,
# run, a future command, a variable) is blocked, as is anything under
# ``MainStoinker.ib``. The rule works on text that went through
# ``_normalise_connect``: continuation lines joined, lower-cased, forward-slashed,
# unquoted, ``/./`` and ``//`` collapsed. Everything here is linear in the size
# of the command.
_OFFLINE_SUBCOMMANDS = frozenset({"backtest", "runs", "report", "serve"})
# Module names that run the CLI: the subcommand allowlist applies to them.
_PACKAGE_MODULES = frozenset(
    {
        "mainstoinker",
        "mainstoinker.__main__",
        "mainstoinker.engine.cli",
        "mainstoinker.engine.__main__",
    }
)
# Any other ``-m mainstoinker.<x>`` is blocked, so a future connecting module is
# blocked by default (``serve`` runs as ``python -m MainStoinker serve``).
_SHELL_WRAPPERS = frozenset(
    {"bash", "sh", "zsh", "dash", "cmd", "powershell", "pwsh", "eval"}
)
# Shells and interpreters that run text piped into them.
_PIPE_CONSUMERS = frozenset(
    {
        "bash", "sh", "zsh", "dash", "cmd", "pwsh", "powershell", "iex",
        "invoke-expression",
    }
)  # fmt: skip
_WRAPPER_SWITCH = re.compile(r"^(?:-[a-z]*c|-command|-c|/[ck]|//[ck])$", re.I)
_MODULE_FLAG = re.compile(r"^(?:-[a-z]*m|--module)$")
_MODULE_ATTACHED = re.compile(r"^-[a-z]*m(mainstoinker\S*)$")
_CODE_FLAG = re.compile(r"^-[a-z]*c$")
_C_FLAG_IN_TEXT = re.compile(r"(?:^|\s)-[a-z]*c(?=\s|$)")
_REDIRECT = re.compile(r"^(?:\d*[<>]|&>)")
_REDIRECT_BARE = re.compile(r"^(?:\d*>>?|\d*<|&>>?)$")
# Redirect and comment operators the tokenizer does not follow (``>|``, ``<>``,
# ``>&``, ``&>``, ``<<``, ``<<<``, ``<# #>``): the word after one may be a file,
# a delimiter or a comment, so a word before the subcommand slot is not trusted.
_ODD_OP = re.compile(r"^(?:\d*>\||\d*<>|\d*>&(?![\d-])|&>|\d*<<|<#|#>)")
_TOKEN_RE = re.compile(r"[^\s,()@\[\]{}]+")
_EXPR_CHARS = re.compile(r"[(@\[{]")
# ``-m`` followed by this is a module written out literally.
_LITERAL_MODULE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")
_ATTACHED_DYNAMIC = re.compile(r"^-[a-z]*m(\S*[$%!*?\[`]\S*)$")
_GLOB_CHARS = re.compile(r"[*?\[]")
# PowerShell operators that rewrite text: ``"backtest" -replace ...``.
_PS_TEXT_OPS = frozenset(
    {"-replace", "-ireplace", "-creplace", "-join", "-split", "-isplit", "-csplit"}
)
# Words in Python code that, next to the package name, may run its CLI or
# import ``__main__`` / ``ib`` without the dotted name written out.
_CODE_RUNNERS = re.compile(
    r"__main__|sys\s*\.\s*argv|runpy|pkgutil|importlib|__import__|\.ib|(?<![a-z0-9_])ib\."
    r"|(?<![a-z0-9_])(?:exec|eval|compile)"
)
_CONNECT_SPLIT = re.compile(
    r";|&&|\|\||(?<!>)\||\r\n|\n|\r|(?<![>&<])&(?![>&])"
)
_DOTDOT_TOKEN = re.compile(r"[^\s;|&()]+")
_PY_C_ARG = re.compile(r"(?<![\w-])-[a-z]*c[ \t]+(?=[\"'])", re.I)
_IB_DOTTED = re.compile(r"mainstoinker\s*\.\s*ib(?![\w])")
_IB_WORD = re.compile(r"(?<![\w])ib(?![\w])")
_IB_PATH = re.compile(r"mainstoinker/ib(?:/|(?![\w]))")
# Code context only (a ``-c`` argument, a heredoc body, a line starting with
# ``import``/``from``): marker words that run or import something by name.
_CODE_WORD = re.compile(
    r"(?<!\w)(?:runpy|run_module|run_path|import_module|__import__|importlib|subprocess)(?!\w)"
    r"|(?<!\w)(?:exec|eval)\s*\("
    r"|(?<!\w)os\s*\.\s*(?:system|exec\w*|spawn\w*|popen)"
)
# Call and attribute shapes of the same words; unlike the bare words they do
# not occur in file names (``test_ib_subprocess_block.py``) or test selectors.
_CODE_SHAPE = re.compile(
    r"(?<!\w)(?:runpy|importlib|subprocess)\s*\.\s*(?!py(?!\w))\w"
    r"|(?<!\w)(?:run_module|run_path|import_module|__import__|exec|eval)\s*\("
    r"|(?<!\w)os\s*\.\s*(?:system|exec\w*|spawn\w*|popen)"
)
# Read-only commands whose arguments are text, not code (``sed``, ``awk`` and
# ``vim -c`` can run commands, so they do not count).
_TEXT_COMMANDS = _READERS - {"sed", "awk", "vim"}
_EXECUTING_TEXT = re.compile(
    r"\$\(|`|\(\s*(?:&\s*)?[^\s(]*(?:python|py\b|uv |pipx )"
)
_SCRIPT_REDIRECT = re.compile(r">>?\s*[^\s|&;<>]*\.(?:sh|ps1|bat|cmd|py)(?=\s|$)")
_ECHO_WRITERS = frozenset({"echo", "printf", "write-output"})
# Lint and test runners, which may name package paths. ``coverage`` counts only
# for the subcommands that run nothing.
_HARMLESS_TOOLS = frozenset(
    {
        "ruff", "pytest", "py.test", "mypy", "pyright", "flake8", "black",
        "isort", "pylint", "compileall", "py_compile",
    }
)  # fmt: skip
_COVERAGE_READ = frozenset({"report", "html", "xml", "combine", "erase"})
# Words that feed arguments to the next command, so the arguments are not visible.
_LOOP_FEEDERS = frozenset({"foreach-object", "foreach", "%"})
_STREAM_FEEDERS = frozenset({"xargs", "parallel"})
_FEEDERS = _LOOP_FEEDERS | _STREAM_FEEDERS
# Transparent prefixes: the command behind them is the one judged.
_CONNECT_PREFIXES = frozenset(
    {
        "&", "call", "nohup", "env", "wsl", "sudo", "time", "exec", "start",
        "xargs", "command", "builtin",
    }
)  # fmt: skip
_CD_WORDS = frozenset({"cd", "chdir", "pushd", "set-location", "sl", "push-location"})
_PY_SCRIPT = re.compile(r"\.py$")
_IB_DIR_ARG = re.compile(r"^(?:\./)?ib(?:/|$)")
_ENCODED_FLAG = re.compile(r"(?<![\w-])-(e[a-z]*)\s+[\"']?([^\s\"';|&]+)", re.I)
_ZERO_WIDTH = re.compile("[­​-‏ -‮⁠-⁤﻿]")
_CONTINUATION = re.compile(r"[\\`^][ \t]*\r?\n")
_ESCAPES = re.compile(r"[\"'`\\^]")
# A bare ``__main__.py`` or ``.`` word: a python run that may be inside the package.
_WEAK_HINT = re.compile(r"(?:^|\s)(?:\./?|(?:\./)?__main__\.py)(?=\s|$)")
_BACKSTOP_LEN = 50_000
_MAX_DEPTH = 6
_CONNECT_MESSAGE = (
    "Blocked: {what}. It can connect to Interactive Brokers (only backtest, "
    "runs, report and serve are allowed) and only the owner runs connecting "
    "commands (see CLAUDE.md safety rules). The one exception is a DATA session on "
    "the PAPER account, written as ONE plain command and nothing else: "
    ".venv\\Scripts\\python.exe -P -m MainStoinker fetch-bars|probe-data "
    "<args> with --paper and --no-prompt among the args (--port only 7497, --host only "
    "127.0.0.1; quote a symbol list: --symbols \"AAPL,MSFT\"; no --help); the live bar "
    "watcher is the same with -P -m MainStoinker.ib.watch_bars <args> and --minutes N "
    "(1 to 3 digits) as well. A "
    "drive-absolute path ending in .venv\\Scripts\\python.exe also works, in double "
    "quotes if it has spaces. No ; && || | & ` $ % < > ( ) quotes other than \", newline, cd, "
    "variable assignment or wrapper (start, cmd /c, powershell -c, bash -c) may appear, "
    "and the live opt-in variable name may not appear at all."
)


class _Ctx:
    """State carried from segment to segment of one command."""

    def __init__(self) -> None:
        self.in_pkg = False  # an earlier cd/pushd went into a MainStoinker directory
        self.in_ib = False  # ... into MainStoinker/ib
        self.fed_loop = False  # a ForEach-Object / foreach / % came before
        self.fed_stream = False  # an xargs / parallel came before
        self.mentions = False  # the command names MainStoinker somewhere


def _join_continuations(text: str) -> str:
    """A backslash, backtick or caret before a line break joins the two lines."""
    return _CONTINUATION.sub(" ", text)


def _resolve_dotdot(m: re.Match[str]) -> str:
    parts: list[str] = []
    for part in m.group().split("/"):
        if part == ".." and parts and parts[-1] not in ("", ".."):
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _normalise_connect(command: str, resolve: bool = True) -> str:
    # ``$()`` is an empty command substitution: ``Main$()Stoinker`` is the name.
    text = _normalise(command).replace("$()", "")
    text = re.sub(r"/(?:\./)+", "/", text)
    text = re.sub(r"/\.(?=\s|$)", "/", text)
    text = re.sub(r"/{2,}", "/", text)
    if resolve and "/.." in text:
        text = _DOTDOT_TOKEN.sub(_resolve_dotdot, text)  # a/b/.. is a
    return text


def _connect_segments(command: str, resolve: bool = True) -> list[tuple[str, str]]:
    """(segment, separator after it) for the normalised command."""
    norm = _normalise_connect(command, resolve)
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _CONNECT_SPLIT.finditer(norm):
        seg = norm[pos : m.start()].strip()
        if seg:
            out.append((seg, m.group()))
        pos = m.end()
    seg = norm[pos:].strip()
    if seg:
        out.append((seg, ""))
    return out


def _command_index(words: list[str]) -> int:
    """Index of the command word after assignments and transparent prefixes."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in _CONNECT_PREFIXES or _ASSIGN.match(w):
            i += 1
        elif w == "timeout" and i + 1 < len(words) and re.fullmatch(r"\d+[smhd]?", words[i + 1]):
            i += 2
        else:
            break
    return i


def _is_text_segment(words: list[str]) -> bool:
    i = _command_index(words)
    if i >= len(words) or any(w in _FEEDERS for w in words[:i]):
        return False
    return _cmd_base(words[i].strip("\"'")) in _TEXT_COMMANDS


def _git_runs_args_connect(words: list[str]) -> bool:
    """Like ``_git_runs_args``, plus aliases (``alias.x=!cmd``) that run a command."""
    if _git_runs_args(words):
        return True
    return any(w.startswith("alias.") for w in words) and any("!" in w for w in words)


def _tokenise(segment: str) -> tuple[list[str], list[bool]]:
    """Tokens of a segment, and per token whether ``, ( ) @ [ ] { }`` came right before it."""
    toks: list[str] = []
    marks: list[bool] = []
    pos = 0
    for m in _TOKEN_RE.finditer(segment):
        marks.append(_EXPR_CHARS.search(segment, pos, m.start()) is not None)
        toks.append(m.group())
        pos = m.end()
    return toks, marks


class _Positionals:
    """first[i]: index of the first positional argument at or after i, else None.

    An option or a redirect is skipped, a bare redirect also skips its target.
    An operator the tokenizer does not follow, or a word that follows an
    expression character (``("backtest","x")[1]``), counts as the positional
    itself, so the subcommand check sees it and blocks. Built backwards once per
    segment so that every lookup is O(1).
    """

    def __init__(self, toks: list[str], marks: list[bool]) -> None:
        n = len(toks)
        first: list[int | None] = [None] * (n + 2)
        for i in range(n - 1, -1, -1):
            t = toks[i]
            if _ODD_OP.match(t) or marks[i]:
                first[i] = i
            elif _REDIRECT_BARE.match(t):
                first[i] = first[i + 2]
            elif _REDIRECT.match(t) or t.startswith("-"):
                first[i] = first[i + 1]
            elif t.rstrip(")}`;"):
                first[i] = i
            else:
                first[i] = first[i + 1]
        self.first = first
        self.marks = marks

    def __len__(self) -> int:
        return len(self.first)

    def __getitem__(self, i: int) -> int | None:
        return self.first[i]


def _check_subcommand(
    toks: list[str], table: _Positionals, start: int, what: str
) -> str | None:
    idx = table[start] if start < len(table) else None
    if idx is None:
        return None
    sub = toks[idx].rstrip(")}`;")
    if _ODD_OP.match(sub):
        return f"`{what}` followed by a redirect or comment the hook cannot follow (`{sub}`)"
    if table.marks[idx]:
        return f"`{what}` with an expression in the subcommand position (`{sub}`)"
    if sub in _OFFLINE_SUBCOMMANDS:
        return None
    return f"`{what}` with subcommand `{sub}`"


def _literal_offline_sub(toks: list[str], table: _Positionals, start: int) -> bool:
    """A ForEach-Object loop may run the package only with the subcommand written out."""
    idx = table[start] if start < len(table) else None
    if idx is None or _check_subcommand(toks, table, start, "") is not None:
        return False
    # ``backtest $_``: a variable right after the subcommand is the loop's value.
    return not (idx + 1 < len(toks) and toks[idx + 1].startswith("$"))


def _first_harmless(toks: list[str]) -> int | None:
    """Index of the first lint/test tool; a script path after it is not run."""
    n = len(toks)
    for k, t in enumerate(toks):
        if _PY_SCRIPT.search(t) or t == "-c":
            return None  # something else is run before any tool is named
        prev = toks[k - 1] if k else ""
        if prev.startswith("-") and prev != "-m":
            continue
        base = _cmd_base(t)
        if base in _HARMLESS_TOOLS:
            return k
        if base == "coverage" and k + 1 < n and toks[k + 1] in _COVERAGE_READ:
            return k
    return None


def _ib_from_import(text: str) -> bool:
    """``from mainstoinker import ... ib`` up to the next ``;``, in linear time."""
    key = "from mainstoinker import "
    pos = 0
    while True:
        j = text.find(key, pos)
        if j == -1:
            return False
        k = text.find(";", j)
        end = len(text) if k == -1 else k
        if _IB_WORD.search(text, j + len(key), end):
            return True
        pos = end  # later occurrences before `end` scan a part of the same span


def _scan_code(text: str) -> str | None:
    """Judge text that is code (a ``-c`` argument or heredoc body)."""
    if _CODE_WORD.search(text):
        return "running MainStoinker from code (runpy, import, exec, subprocess)"
    if _IB_DOTTED.search(text) or _ib_from_import(text) or _IB_PATH.search(text):
        return "importing MainStoinker.ib (broker code)"
    if "mainstoinker.__main__" in text or "mainstoinker/__main__" in text:
        return "importing MainStoinker.__main__"
    if "mainstoinker" in text and _CODE_RUNNERS.search(text):
        return (
            "code that names MainStoinker and uses __main__, sys.argv, runpy, "
            "pkgutil, importlib, __import__, exec, eval, compile or ib"
        )
    return None


def _python_code_args(text: str) -> list[str]:
    """Quoted ``-c`` arguments of python commands, whole (they may contain ``;``)."""
    out: list[str] = []
    for count, m in enumerate(_PY_C_ARG.finditer(text)):
        if count >= 50:
            break
        end = _quoted_at(text, m.end())
        if end is None:
            continue
        window = text[max(0, m.start() - 400) : m.start()]
        cut = max(window.rfind(ch) for ch in ";|&\n\r(")
        if _has_interpreter(_normalise(window[cut + 1 :])):
            out.append(text[m.end() : len(text) if end == -1 else end])
    return out


_QUOTE = re.compile(r"[\"']")
_PIPE_TO_RUNNER = re.compile(
    r"[ \t]*\|(?!\|)[ \t]*(?:\S*[\\/])?"
    r"(?:python[\d.]*|pythonw|py|ipython|bash|sh|zsh|dash|cmd|pwsh|powershell|iex|invoke-expression)"
    r"(?:\.exe)?(?![\w.-])",
    re.I,
)


def _piped_code_args(text: str) -> list[str]:
    """Quoted strings piped straight into a shell or interpreter, whole."""
    out: list[str] = []
    pos = 0
    while len(out) < 50:
        m = _QUOTE.search(text, pos)
        if m is None:
            break
        end = _quoted_at(text, m.start())
        if end is None or end == -1:
            break  # unterminated: the rest is not split into strings
        if _PIPE_TO_RUNNER.match(text, end):
            out.append(text[m.start() + 1 : end - 1])
        pos = end
    return out


def _scan_connecting(segment: str) -> str | None:
    toks, marks = _tokenise(segment)
    n = len(toks)
    table = _Positionals(toks, marks)
    feeder = next((k for k, t in enumerate(toks) if t in _FEEDERS), None)
    stream = next((k for k, t in enumerate(toks) if t in _STREAM_FEEDERS), None)
    last_text_op = next(
        (k for k in range(n - 1, -1, -1) if toks[k] in _PS_TEXT_OPS), None
    )

    def fed(i: int, start: int) -> str | None:
        # xargs / parallel append their input to the command: always blocked.
        # A ForEach-Object loop is fine when the subcommand is written out.
        if feeder is None or feeder >= i:
            return None
        if (stream is not None and stream < i) or not _literal_offline_sub(
            toks, table, start
        ):
            return "arguments fed by xargs/ForEach-Object to MainStoinker are not visible"
        return None

    # Module runs: -m MainStoinker[.x] / -mMainStoinker, whatever the interpreter.
    for i, tok in enumerate(toks):
        mod = None
        rest_at = i + 1
        if _MODULE_FLAG.match(tok):
            if i + 1 < n:
                mod = toks[i + 1]
                rest_at = i + 2
        elif tok.startswith("--module="):
            mod = tok[9:]
        else:
            attached = _MODULE_ATTACHED.match(tok)
            if attached:
                mod = attached.group(1)
        if mod is None:
            continue
        mod = mod.strip("'`;")
        if mod == "runpy" or (mod != "mainstoinker" and not mod.startswith("mainstoinker.")):
            if mod == "runpy":
                return "`-m runpy` runs a module by name (MainStoinker is named)"
            continue
        why = fed(i, rest_at)
        if why:
            return why
        if mod == "mainstoinker.ib" or mod.startswith("mainstoinker.ib."):
            return f"`-m {mod}` runs the broker (MainStoinker.ib) code"
        if mod in _PACKAGE_MODULES:
            why = _check_subcommand(toks, table, rest_at, "-m MainStoinker")
            if why:
                return why
            if last_text_op is not None and last_text_op >= rest_at:
                return "a PowerShell text operator (-replace, -join) rewrites MainStoinker arguments"
        else:
            return f"`-m {mod}` is not an offline entry point of MainStoinker"
    # Script runs: .../MainStoinker/__main__.py, the package directory and files
    # under MainStoinker/ib. Another -m module (pdb, cProfile, coverage run) does
    # not switch this off; only a lint/test tool named before the path does.
    # With no interpreter token the path must be the command itself.
    cmd0 = _command_index(toks)
    has_py = _has_interpreter(segment) or "coverage" in toks
    stop = _first_harmless(toks)
    for i, tok in enumerate(toks):
        if stop is not None and i > stop:
            break
        if tok.endswith("mainstoinker/__main__.py"):
            # The entry file of the package: judged whatever runs it.
            why = fed(i, i + 1) or _check_subcommand(
                toks, table, i + 1, "MainStoinker/__main__.py"
            )
            if why:
                return why
        elif not (has_py or i == cmd0):
            continue  # only a run counts: an interpreter, or the path as command
        elif tok in ("mainstoinker", "mainstoinker/") or tok.endswith(
            ("/mainstoinker", "/mainstoinker/")
        ):
            why = fed(i, i + 1) or _check_subcommand(
                toks, table, i + 1, "the MainStoinker directory"
            )
            if why:
                return why
        elif _IB_PATH.search(tok):
            return "running a file under MainStoinker/ib (broker code)"
    # Code: runpy / import of the package, or of MainStoinker.ib. The marker
    # words count only as code (a -c argument, a line starting with import or
    # from) or in call/attribute form, never in file names or test selectors.
    c_flag = _C_FLAG_IN_TEXT.search(segment) if has_py else None
    code_region = segment[c_flag.end() :] if c_flag else ""
    is_import_line = segment.startswith(("import ", "from "))
    marker = bool(_CODE_SHAPE.search(segment)) or bool(
        (code_region or is_import_line) and _CODE_WORD.search(code_region or segment)
    )
    if marker:
        return "running MainStoinker from code (runpy, import, exec, subprocess)"
    if c_flag or is_import_line:
        why = _scan_code(segment)
        if why:
            return why
    return None


def _note_cd(words: list[str], ctx: _Ctx) -> None:
    """Remember a cd/pushd into a directory named MainStoinker (or its ib)."""
    i = _command_index(words)
    if i >= len(words) or _cmd_base(words[i]) not in _CD_WORDS:
        return
    for target in words[i + 1 :]:
        parts = [p for p in target.split("/") if p]
        if "mainstoinker" in parts:
            ctx.in_pkg = True
            if "ib" in parts[parts.index("mainstoinker") + 1 :]:
                ctx.in_ib = True
        elif ctx.in_pkg and "ib" in parts:
            ctx.in_ib = True


def _scan_in_package(segment: str, ctx: _Ctx, weak: bool = False) -> str | None:
    """A python run from inside the package directory (``python __main__.py``).

    ``weak``: no cd into the package was seen (the working directory of an
    earlier call is invisible), so only ``python __main__.py <x>`` and
    ``python . <x>`` with a non-offline ``<x>`` count.
    """
    toks, marks = _tokenise(segment)
    table = _Positionals(toks, marks)
    start = None
    for k, t in enumerate(toks):
        if _PYTHON_TOKEN.match(t.rsplit("/", 1)[-1]):
            start = k
            break
        if t == "run" and k and toks[k - 1] in ("uv", "pipx") and start is None:
            start = k
    if start is None:
        return None
    where = "from inside the MainStoinker directory"
    for k in range(start + 1, len(toks)):
        t = toks[k]
        if t == "-c":
            return None
        mod = None
        if t == "-m" and k + 1 < len(toks):
            mod = toks[k + 1]
        elif t.startswith("-m") and len(t) > 2:
            mod = t[2:]
        if mod is not None:
            if weak:
                return None
            if mod == "ib" or mod.startswith("ib."):
                return f"`-m {mod}` {where} runs the broker (MainStoinker.ib) code"
            if mod in ("__main__", "engine.cli", "engine.__main__"):
                return _check_subcommand(toks, table, k + 2, f"-m {mod} {where}")
            return None
        pkg_script = t in (".", "./", "__main__.py", "./__main__.py")
        if weak:
            if _PY_SCRIPT.search(t) and not pkg_script:
                return None  # a different script is run
            script = pkg_script
        else:
            script = (
                pkg_script
                or _PY_SCRIPT.search(t) is not None
                or _IB_DIR_ARG.match(t) is not None
            )
        if not script:
            continue
        if weak:
            return _check_subcommand(toks, table, k + 1, f"python {t}")
        if ctx.in_ib or _IB_DIR_ARG.match(t):
            return f"running a file {where} under MainStoinker/ib (broker code)"
        return _check_subcommand(toks, table, k + 1, f"python {t} {where}")
    return None


_MODULE_HINT = re.compile(r"(?:^|\s)-[a-z]*m|--module")
_GLOB_SAMPLES = ("mainstoinker", "mainstoinker.ib", "mainstoinker.ib.cli", "mainstoinker.__main__")


def _dynamic_module(segment: str, mentions: bool) -> str | None:
    """``-m`` followed by a name that is not a literal dotted identifier.

    Such a name (``$M``, ``%M%``, ``Main$(x)Stoinker``, a glob) may be built into
    ``MainStoinker`` at run time: blocked when the command names the package
    anywhere, or when a glob could match the package.
    """
    toks = segment.split()  # whole words: ``[M]ainStoinker`` is one name
    for i, tok in enumerate(toks):
        mod = None
        if _MODULE_FLAG.match(tok):
            if i + 1 < len(toks):
                mod = toks[i + 1]
        else:
            attached = _ATTACHED_DYNAMIC.match(tok)
            if attached:
                mod = attached.group(1)
        if mod is None or _LITERAL_MODULE.match(mod.rstrip(")}")):
            continue
        glob = _GLOB_CHARS.search(mod) is not None and (
            len(mod) > 200 or any(fnmatchcase(s, mod) for s in _GLOB_SAMPLES)
        )
        if mentions or glob:
            return f"`-m {mod}` is not a literal module name and could be MainStoinker"
    return None


def _connecting_reason(
    segment: str,
    text_ok: bool = True,
    ctx: _Ctx | None = None,
    as_code: bool = False,
    depth: int = 0,
) -> str | None:
    """Why this segment runs a connecting MainStoinker command, else None."""
    if ctx is None:
        ctx = _Ctx()
    words = segment.split()
    _note_cd(words, ctx)
    for w in words:
        if w in _LOOP_FEEDERS:
            ctx.fed_loop = True
        elif w in _STREAM_FEEDERS:
            ctx.fed_stream = True
    if _MODULE_HINT.search(segment) and _has_interpreter(segment):
        is_text = (
            text_ok
            and not as_code
            and _is_text_segment(words)
            and not _SCRIPT_REDIRECT.search(segment)
        )
        why = None if is_text else _dynamic_module(segment, ctx.mentions)
        if why:
            return why
    weak = "mainstoinker" not in segment and not ctx.in_pkg
    if weak and not _WEAK_HINT.search(segment):
        return None
    i0 = _command_index(words)
    base = _cmd_base(words[i0].strip("\"'")) if i0 < len(words) else ""
    if base in _SHELL_WRAPPERS:
        for i, tok in enumerate(words):
            if _WRAPPER_SWITCH.match(tok) and i > 0:
                if depth >= _MAX_DEPTH:
                    return "shell wrappers nested too deeply to analyse"
                inner = " ".join(words[i + 1 :])
                return _connecting_reason(inner, text_ok, ctx, as_code, depth + 1)
    if base in _ECHO_WRITERS and _SCRIPT_REDIRECT.search(segment):
        text_ok = False  # text written into a script is code to run later
        as_code = True
    if as_code:
        text_ok = False
    if (
        text_ok
        and _is_text_segment(words)
        and not _EXECUTING_TEXT.search(segment)
        and not (base == "git" and _git_runs_args_connect(words))
    ):
        return None
    if weak:
        return _scan_in_package(segment, ctx, weak=True)
    why = _scan_connecting(segment)
    if not why and (ctx.fed_loop or ctx.fed_stream):
        # An earlier ForEach-Object / xargs feeds this command: its arguments
        # are not all visible. A loop is accepted when the subcommand is written
        # out; xargs and parallel are blocked whatever the arguments are.
        why = _scan_connecting(("xargs " if ctx.fed_stream else "% ") + segment)
    if why:
        return why
    if as_code:
        why = _scan_code(segment)
        if why:
            return why
    if ctx.in_pkg:
        return _scan_in_package(segment, ctx)
    return None


def _is_pipe_consumer(segment: str) -> bool:
    words = segment.split()
    i = _command_index(words)
    if i >= len(words):
        return False
    base = _cmd_base(words[i].strip("\"'"))
    return base in _PIPE_CONSUMERS or _PYTHON_TOKEN.match(base) is not None


def _heredoc_bodies(text: str) -> list[str]:
    """Bodies of the heredocs that are still in the text (not cat/git/gh ones)."""
    bodies: list[str] = []
    n = len(text)
    for count, m in enumerate(_HEREDOC.finditer(text)):
        if count >= 50:
            break
        tag = m.group(2) or m.group(3) or m.group(4)
        eol = text.find("\n", m.end())
        if eol == -1:
            continue
        p = eol + 1
        end = n
        while p <= n:
            e = text.find("\n", p)
            if e == -1:
                e = n
            line = text[p:e].rstrip("\r")
            if m.group(1):
                line = line.lstrip("\t")
            if line == tag:
                end = p
                break
            if e == n:
                break
            p = e + 1
        bodies.append(text[eol + 1 : end])
    return bodies


def _judge_connecting(text: str, resolve: bool = True) -> str | None:
    """The T10 rule over a command whose continuation lines are joined."""
    segs = _connect_segments(text, resolve)
    # An unbalanced quote means message removal may not have covered the text:
    # then nothing counts as "just text" any more.
    text_ok = text.count('"') % 2 == 0 and text.count("'") % 2 == 0
    # A pipeline that ends in a shell or interpreter runs the text it is fed.
    feeds = [False] * len(segs)
    for i in range(len(segs) - 2, -1, -1):
        if segs[i][1] == "|" and (_is_pipe_consumer(segs[i + 1][0]) or feeds[i + 1]):
            feeds[i] = True
    ctx = _Ctx()
    ctx.mentions = "mainstoinker" in _normalise_connect(text, resolve)
    for i, (segment, _sep) in enumerate(segs):
        try:
            why = _connecting_reason(segment, text_ok, ctx, feeds[i])
        except Exception:  # noqa: BLE001 - a rule bug must fail closed
            why = (
                "the hook could not analyse a MainStoinker command"
                if "mainstoinker" in segment or ctx.in_pkg
                else None
            )
        if why:
            return why
    for body in _heredoc_bodies(text):
        if "mainstoinker" in body.lower():
            why = _scan_code(_normalise_connect(body, resolve))
            if why:
                return why
    # A ``-c`` argument is code as a whole, also past the ``;`` that split it.
    for code in _python_code_args(text) + _piped_code_args(text):
        norm = _normalise_connect(code, resolve)
        if "mainstoinker" in norm:
            why = _scan_code(norm)
            if why:
                return why
    return None


def _encoded_values(command: str) -> list[str]:
    values: list[str] = []
    for count, m in enumerate(_ENCODED_FLAG.finditer(command)):
        if count >= 50:
            break
        flag = m.group(1).lower()
        if flag == "ec" or "encodedcommand".startswith(flag):
            values.append(m.group(2))
    return values


def _decode_encoded(value: str) -> str | None:
    """PowerShell -EncodedCommand: base64 of UTF-16LE text; None if undecodable."""
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded, validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError):
        return None


# --- the one allowance: an unattended DATA session on the PAPER account ---------
# One anchored regular expression over the whole (trimmed) command, then explicit
# token checks. Plain tokens use only the characters below; a double-quoted token
# may also hold spaces (the project path has some). Nothing else is ever matched.
_PAPER_CHARS = r"A-Za-z0-9_,.:=\\/-"
_PAPER_TOKEN = rf'(?:"[ {_PAPER_CHARS}]*"|[{_PAPER_CHARS}]+)'
# ``MainStoinker`` is followed by ONE of: a subcommand word (``sub``), or exactly the
# watcher module suffix (``mod``). Case-sensitive; a trailing ``.x`` or ``.`` cannot match
# because the arguments must start after a space.
_PAPER_COMMAND = re.compile(
    rf"(?:& +)?(?P<py>{_PAPER_TOKEN}) +-P +-m +MainStoinker"
    rf"(?: +(?P<sub>fetch-bars|probe-data)|(?P<mod>\.ib\.watch_bars))"
    rf"(?P<args>(?: +{_PAPER_TOKEN})+)"
)
_PAPER_MINUTES_VALUE = re.compile(r"[0-9]{1,3}")
_PAPER_WATCHER_GUARDED = ("--minutes",)
_PAPER_TOKEN_RE = re.compile(_PAPER_TOKEN)
# The interpreter: the project venv, written relative (``.venv\Scripts\python.exe``) or as
# a drive-absolute path (``X:\...\.venv\Scripts\python.exe``, no other colon). Never a UNC
# or ``/c/...`` path, never ``..``, never an assignment or an option.
_PAPER_PYTHON_REL = re.compile(r"\.venv[\\/]Scripts[\\/]python\.exe")
_PAPER_PYTHON_ABS = re.compile(r"[A-Za-z]:[\\/](?:[^:]*[\\/])?\.venv[\\/]Scripts[\\/]python\.exe")
_PAPER_QUOTED_OPTION = re.compile(r" -\S")
# After a comma, PowerShell starts a new argument; a backslash may precede a dash in bash.
_PAPER_COMMA_OPTION = re.compile(r",[\\-]")
_PAPER_GUARDED = ("--port", "--host", "--paper", "--no-prompt")
_PAPER_PORT = "7497"
_PAPER_HOST = "127.0.0.1"
_PAPER_MAX_LEN = 4000
# The live opt-in variable: its name may not appear anywhere in an allowed command.
_PAPER_FORBIDDEN_NAME = "allow_live_port"


def _paper_token_ok(token: str) -> bool:
    """Per-token rules the character class cannot express."""
    if token.startswith('"'):
        inner = token[1:-1]
        return not (
            not inner
            or inner.replace("\\", "").startswith("-")  # a quoted option is not looked at as one
            or inner.endswith("\\")  # bash reads backslash-quote as an escaped quote
            or _PAPER_QUOTED_OPTION.search(inner)
            or _PAPER_COMMA_OPTION.search(inner)
        )
    # A backslash at the end can swallow the next space; ``a,--port`` is an array of two
    # arguments in PowerShell. Bash drops a backslash before a letter or a dash, so a
    # backslash may appear in path VALUES only: never first, and never in an option name.
    if token.endswith("\\") or token.startswith("\\") or _PAPER_COMMA_OPTION.search(token):
        return False
    if token.replace("\\", "").startswith("-"):
        return token.startswith("--") and "\\" not in token.split("=", 1)[0]
    return True


def _paper_python_ok(path: str) -> bool:
    if ".." in path or "=" in path or path.startswith("-"):
        return False
    return bool(_PAPER_PYTHON_REL.fullmatch(path) or _PAPER_PYTHON_ABS.fullmatch(path))


def _paper_match(command: str) -> re.Match[str] | None:
    """The match of the allowed shape on the trimmed command, else None."""
    text = command.strip(" \t\r\n")
    if not text or len(text) > _PAPER_MAX_LEN or _PAPER_FORBIDDEN_NAME in text.lower():
        return None
    m = _PAPER_COMMAND.fullmatch(text)
    if m is None:
        return None
    py = m.group("py")
    path = py[1:-1] if py.startswith('"') else py
    if not _paper_token_ok(py) or not _paper_python_ok(path):
        return None
    tokens = _PAPER_TOKEN_RE.findall(m.group("args"))
    if not all(_paper_token_ok(t) for t in tokens):
        return None
    if "--paper" not in tokens or "--no-prompt" not in tokens:  # exact, standalone, unquoted
        return None
    watcher = m.group("mod") is not None
    guarded = _PAPER_GUARDED + (_PAPER_WATCHER_GUARDED if watcher else ())
    minutes: list[str | None] = []  # the value of each ``--minutes`` (watcher form only)
    for i, tok in enumerate(tokens):
        if tok.startswith('"') or not tok.startswith("--"):
            continue
        name = tok.split("=", 1)[0]
        if name.lower() == "--help":  # keeps the rule to real runs
            return None
        # an abbreviation or a differently cased spelling of a guarded option
        if any(g.startswith(name.lower()) and name != g for g in guarded):
            return None
        # a guarded name followed by anything but ``=`` (``--port,7496``, ``--port:7496``,
        # ``--paperx``); the ``=`` forms of the flags and of port and host are checked below
        if any(tok.startswith(g) and tok != g and tok[len(g)] != "=" for g in guarded):
            return None
        if watcher and name == "--minutes":
            minutes.append(tok[len("--minutes=") :] if "=" in tok else
                           (tokens[i + 1] if i + 1 < len(tokens) else None))
        if tok.startswith(("--paper=", "--no-prompt=")):
            return None
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok == "--port" and following != _PAPER_PORT:
            return None
        if tok == "--host" and following != _PAPER_HOST:
            return None
        if name == "--port" and tok != "--port" and tok != f"--port={_PAPER_PORT}":
            return None
        if name == "--host" and tok != "--host" and tok != f"--host={_PAPER_HOST}":
            return None
    if watcher:
        # a bounded run: ``--minutes`` exactly once, with 1 to 3 plain digits (the watcher
        # itself refuses 0 and anything over 420)
        if len(minutes) != 1 or minutes[0] is None or not _PAPER_MINUTES_VALUE.fullmatch(minutes[0]):
            return None
    return m


def allowed_paper_data_command(command: str) -> bool:
    """True only for the one allowed shape (see the header): a whole plain command that runs
    ``fetch-bars`` or ``probe-data`` of the project package, or the module
    ``MainStoinker.ib.watch_bars`` with ``--minutes N``, with ``--paper --no-prompt``, and
    that the rest of this hook would not block once its connecting part is neutral."""
    neutral = _paper_neutral(command)
    # depth 1: the allowance itself is off in this judgement, so it cannot recurse
    return neutral is not None and not _decide(neutral, 1)[0]


def _paper_neutral(command: str) -> str | None:
    """The allowed command with only the connecting part replaced by ``backtest``, else None.

    ``-m MainStoinker fetch-bars|probe-data`` becomes ``-m MainStoinker backtest``, and the
    watcher form ``-m MainStoinker.ib.watch_bars`` becomes ``-m MainStoinker backtest``: the
    same offline command, with every argument unchanged. The whole hook judges this text
    (``allowed_paper_data_command``), so every OTHER reason to block the command still
    applies: only "connecting subcommand of ``-m MainStoinker``" or "``-m`` of the module
    ``MainStoinker.ib.watch_bars``" is suppressed, and the module is matched exactly.
    """
    m = _paper_match(command)
    if m is None:
        return None
    lead = len(command) - len(command.lstrip(" \t\r\n"))
    if m.group("mod") is not None:
        return command[: lead + m.start("mod")] + " backtest" + command[lead + m.end("mod") :]
    return command[: lead + m.start("sub")] + "backtest" + command[lead + m.end("sub") :]


def decide(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for a shell command line."""
    return _decide(command, 0)


def _decide(command: str, depth: int) -> tuple[bool, str]:
    """The command as typed, then its NFKC form (full-width look-alikes): a block in either wins."""
    # The paper allowance applies to the command as typed only: not to decoded text,
    # and not to the folded form below (a look-alike letter must not become a flag).
    result = _decide_text(command, depth, allow_paper=depth == 0)
    if result[0]:
        return result
    folded = unicodedata.normalize("NFKC", command).casefold()
    if folded != command.casefold():
        return _decide_text(folded, depth)
    return result


def _decide_text(command: str, depth: int, allow_paper: bool = False) -> tuple[bool, str]:
    low = command.lower()
    # Quotes, backticks, backslashes and carets inside the name do not hide it.
    deobfuscated = _ESCAPES.sub("", low).replace("$()", "")
    mentions = "mainstoinker" in low or "mainstoinker" in deobfuscated
    # Backstop: nobody types this by accident, and it is not analysed.
    if mentions and len(command) > _BACKSTOP_LEN:
        return True, _CONNECT_MESSAGE.format(
            what="a very long command that mentions MainStoinker is not analysed"
        )
    # Invisible characters could split the name: look at the text without them.
    if _ZERO_WIDTH.search(command) and "mainstoinker" in _ZERO_WIDTH.sub("", low):
        return True, _CONNECT_MESSAGE.format(
            what="invisible characters in a command that names MainStoinker"
        )
    if mentions and "mainstoinker" not in _normalise_connect(command):
        return True, _CONNECT_MESSAGE.format(
            what="the name MainStoinker is split by escape characters"
        )
    cleaned = _strip_message_args(_strip_heredocs(command))
    joined = _join_continuations(cleaned)
    # The two older rules look at the text as typed and with continuation lines
    # joined: the joined form can only add blocks.
    for text in (cleaned,) if joined == cleaned else (cleaned, joined):
        in_testy = False
        for segment in _segments(text):
            words = segment.split()
            if words and words[0] in _CD_WORDS:
                in_testy = segment.rstrip("/").endswith("mainstoinker/testy")
            elif _runs_legacy_script(segment, in_testy):
                return True, _LEGACY_MESSAGE
            if ENTRY_NAME in segment and _has_interpreter(segment):
                return True, _BLOCK_MESSAGE.format(what="live entry script")
            if LAUNCHER_NAME in segment and _executes_launcher(segment):
                return True, _BLOCK_MESSAGE.format(what="launcher batch file")
    # PowerShell -EncodedCommand: judge the decoded text.
    undecodable = False
    if "powershell" in low or "pwsh" in low:
        for value in _encoded_values(command):
            decoded = _decode_encoded(value)
            if decoded is None:
                undecodable = True
            elif depth < 3:
                result = _decide(decoded, depth + 1)
                if result[0]:
                    return result
    if undecodable and mentions:
        return True, _CONNECT_MESSAGE.format(
            what="an -EncodedCommand value that cannot be decoded, next to MainStoinker"
        )
    if allow_paper and allowed_paper_data_command(command):
        # THE one place of the paper allowance. Every rule above judged the command as
        # typed and let it through; the function has just judged the same text with only
        # the subcommand word replaced by an offline one (the connecting rule included).
        return False, ""
    try:
        why = _judge_connecting(joined)
        if why is None and ".." in joined:
            why = _judge_connecting(joined, resolve=False)  # as typed, too
    except Exception:  # noqa: BLE001 - a rule bug must fail closed
        why = "the hook could not analyse a MainStoinker command" if mentions else None
    if why:
        return True, _CONNECT_MESSAGE.format(what=why)
    return False, ""


def _mentions_protected(text: str) -> bool:
    raw = text.lower()
    legacy = ("ibapi_testing", "testy.py", "testy_advanced.py")
    return "dum dum" in raw or "data collect.bat" in raw or any(n in raw for n in legacy)


def _mentions_package(text: str) -> bool:
    return "mainstoinker" in text.lower()


def _on_error(stdin_text: str, why: str) -> int:
    if _mentions_protected(stdin_text) or _mentions_package(stdin_text):
        sys.stderr.write(_PARSE_BLOCK_MESSAGE + f" ({why})\n")
        return 2
    sys.stderr.write(f"block_live_launch hook: {why}; allowing the tool call.\n")
    return 0


def main(stdin_text: str) -> int:
    """Hook entry point; returns the process exit code and never raises."""
    try:
        try:
            # Some shells prepend a byte-order mark when piping text.
            data = json.loads(stdin_text.lstrip("﻿"))
        except ValueError:
            return _on_error(stdin_text, "stdin is not valid JSON")
        if not isinstance(data, dict):
            return _on_error(stdin_text, "request is not a JSON object")
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return _on_error(stdin_text, "no tool_input object in request")
        tool_name = data.get("tool_name")
        is_shell = isinstance(tool_name, str) and tool_name.lower() in (
            "bash",
            "powershell",
        )
        if "command" not in tool_input:
            if is_shell:
                return _on_error(stdin_text, "shell tool call without a command")
            return 0  # non-shell tool: nothing to check
        command = tool_input["command"]
        if not isinstance(command, str):
            return _on_error(stdin_text, "tool_input.command is not a string")
        if is_shell and not command.strip():
            if _mentions_protected(stdin_text):
                return _on_error(stdin_text, "empty command")
            return 0
        blocked, reason = decide(command)
        if blocked:
            sys.stderr.write(reason + "\n")
            return 2
        return 0
    except Exception as exc:  # noqa: BLE001 - must never raise
        try:
            return _on_error(stdin_text, f"unexpected error: {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            return 0


def run_stdin(stream: BinaryIO) -> int:
    """Read bytes from `stream`, decode leniently and run `main`; never raises."""
    raw = b""
    try:
        raw = stream.read()
        return main(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - exit 1 would let the call proceed
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else ""
            return _on_error(text, f"could not read stdin: {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            return 0


if __name__ == "__main__":
    try:
        code = run_stdin(sys.stdin.buffer)
    except BaseException:  # noqa: BLE001
        code = 0
    sys.exit(code)
