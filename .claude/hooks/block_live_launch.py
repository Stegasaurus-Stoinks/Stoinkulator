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
which has no interpreter token and is NOT covered.

Accepted false positives (blocked although harmless):
* searching the entry script for an interpreter word, e.g.
  ``grep -n python "<entry>"`` (the word ``python``/``py`` plus the entry name);
* ``echo`` / ``printf`` of text containing an interpreter word and the entry
  script name;
* ``python -m ruff check "<entry>"`` and ``python tools/lint.py "<entry>"``;
* a double-quoted commit message containing ``$(...)`` or backticks is scanned,
  because those would execute.

Standard library only; must not import anything from the repo.
"""

from __future__ import annotations

import json
import re
import sys
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
_CMD_PIECE_SPLIT = re.compile(r"[;&|(`]")
_MAX_MATCHES = 200

_BLOCK_MESSAGE = (
    "Blocked: this command would run the live trading entry script or the "
    "launcher batch file ({what}). Agents must never execute these because they "
    "connect to Interactive Brokers and place real orders (see CLAUDE.md safety "
    "rules). Reading, searching, diffing or editing the file is fine."
)
_PARSE_BLOCK_MESSAGE = (
    "Blocked: the live-launch hook could not parse this tool request, and the "
    "request mentions a protected live-trading file (dum dum / data collect.bat). "
    "Agents must never execute these (see CLAUDE.md safety rules); reading or "
    "editing them is fine."
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


def _consumer_is_textual(prefix: str) -> bool:
    piece = _CMD_PIECE_SPLIT.split(prefix)[-1]
    words = piece.split()
    i = 0
    while i < len(words) and (words[i].lower() in _TRANSPARENT or _ASSIGN.match(words[i])):
        i += 1
    if i >= len(words):
        return False
    return _cmd_base(words[i].lower().strip("\"'")) in {"cat", "git", "gh"}


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


_QUOTED_ANY = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', re.S)
_SEP_CHARS = re.compile(r"[;&|\r\n]")


def _mask_quoted_separators(command: str) -> str:
    """Make separators inside complete quotes inert so quoted text stays in one segment."""
    return _QUOTED_ANY.sub(lambda m: _SEP_CHARS.sub(" ", m.group(0)), command)


def _segments(command: str) -> list[str]:
    parts = _SEGMENT_SPLIT.split(_normalise(_mask_quoted_separators(command)))
    return [p.strip() for p in parts if p.strip()]


def _has_interpreter(segment: str) -> bool:
    if "exec(" in segment or "runpy" in segment or "debugpy" in segment:
        return True
    if _RUNNER.search(segment):
        return True
    for tok in segment.split():
        tok = tok.strip("(){}$`")
        if _PYTHON_TOKEN.match(tok.rsplit("/", 1)[-1]):
            return True
    return False


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
    # A path containing spaces can hide the real command word; prefer the last
    # token that names an .exe.
    cmd_tok = pre_tokens[0]
    for tok in pre_tokens:
        if tok.endswith(".exe"):
            cmd_tok = tok
    base = _cmd_base(cmd_tok)
    if base in _READERS:
        return False
    if base == "cmd":
        return any(_CMD_SWITCH.match(t) for t in words)
    if base in _LAUNCH_WORDS:
        if (
            base in _START_WORDS
            and len(pre_tokens) > 1
            and _cmd_base(pre_tokens[1]) in _READERS
        ):
            return False  # e.g. "start notepad <launcher>": opens an editor
        return True
    # Direct run: the segment is (a directory path ending in) the launcher.
    return pre.endswith("/") and ("/" in pre_tokens[0] or ":" in pre_tokens[0])


def decide(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for a shell command line."""
    cleaned = _strip_message_args(_strip_heredocs(command))
    for segment in _segments(cleaned):
        if ENTRY_NAME in segment and _has_interpreter(segment):
            return True, _BLOCK_MESSAGE.format(what="live entry script")
        if LAUNCHER_NAME in segment and _executes_launcher(segment):
            return True, _BLOCK_MESSAGE.format(what="launcher batch file")
    return False, ""


def _mentions_protected(text: str) -> bool:
    raw = text.lower()
    return "dum dum" in raw or "data collect.bat" in raw


def _on_error(stdin_text: str, why: str) -> int:
    if _mentions_protected(stdin_text):
        sys.stderr.write(_PARSE_BLOCK_MESSAGE + f" ({why})\n")
        return 2
    sys.stderr.write(f"block_live_launch hook: {why}; allowing the tool call.\n")
    return 0


def main(stdin_text: str) -> int:
    """Hook entry point; returns the process exit code and never raises."""
    try:
        try:
            data = json.loads(stdin_text)
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
