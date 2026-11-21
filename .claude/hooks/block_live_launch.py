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
allowed; only EXECUTING them is blocked.

This is a tripwire against accidents, NOT a security boundary. Known bypasses
include copying or renaming the script, wrapper files that run it, globs, and
building the path dynamically (variables, string concatenation, encoding).
Known false positives: any command with an interpreter token and the protected
path in the same segment is blocked, e.g. ``python -m ruff check <entry>``.

Standard library only; must not import anything from the repo.
"""

from __future__ import annotations

import json
import re
import sys

ENTRY_NAME = "dum dum.py"
LAUNCHER_NAME = "stonks data collect.bat"

_SEGMENT_SPLIT = re.compile(r";|&&|\|\||\||\r\n|\n|\r")
_PYTHON_TOKEN = re.compile(r"^(?:python[\d.]*|pythonw|py|ipython)(?:\.exe)?$")
_UV_RUN = re.compile(r"(?<![\w-])uv run(?![\w-])")
_CMD_RUN = re.compile(r"(?:^|[\s/])cmd(?:\.exe)?\s+/{1,2}[ck](?:\s|$)")
_LAUNCH_EXACT = frozenset(
    {"start", "start-process", "invoke-item", "ii", "invoke-expression", "iex"}
)
_SHELL_BASENAMES = frozenset({"bash", "sh"})

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


def _normalise(command: str) -> str:
    text = command.lower()
    text = text.replace("\\ ", " ").replace("` ", " ")
    text = text.replace("\\", "/")
    text = text.replace("'", "").replace('"', "")
    return re.sub(r"[ \t]+", " ", text)


def _segments(command: str) -> list[str]:
    parts = _SEGMENT_SPLIT.split(_normalise(command))
    return [p.strip() for p in parts if p.strip()]


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _has_interpreter(segment: str) -> bool:
    if "exec(" in segment or "runpy" in segment or "debugpy" in segment:
        return True
    if _UV_RUN.search(segment):
        return True
    return any(_PYTHON_TOKEN.match(_basename(tok)) for tok in segment.split())


def _executes_launcher(segment: str) -> bool:
    if _CMD_RUN.search(segment):
        return True
    for tok in segment.split():
        if tok in _LAUNCH_EXACT:
            return True
        base = _basename(tok)
        if base.endswith(".exe"):
            base = base[:-4]
        if base in _SHELL_BASENAMES:
            return True
    # The segment itself starts with the launcher (optionally after a call
    # operator and/or a directory path, which may contain spaces).
    seg = re.sub(r"^(?:&\s*|call\s+)+", "", segment)
    idx = seg.find(LAUNCHER_NAME)
    pre = seg[:idx].strip()
    if not pre:
        return True
    if pre.endswith("/") or seg[:idx].endswith("/"):
        first = pre.split()[0]
        return "/" in first or ":" in first
    return False


def decide(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for a shell command line."""
    for segment in _segments(command):
        if ENTRY_NAME in segment and _has_interpreter(segment):
            return True, _BLOCK_MESSAGE.format(what="live entry script")
        if LAUNCHER_NAME in segment and _executes_launcher(segment):
            return True, _BLOCK_MESSAGE.format(what="launcher batch file")
    return False, ""


def _on_error(stdin_text: str, why: str) -> int:
    raw = stdin_text.lower()
    if "dum dum" in raw or "data collect.bat" in raw:
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
        tool_input = data.get("tool_input") if isinstance(data, dict) else None
        if not isinstance(tool_input, dict):
            return _on_error(stdin_text, "no tool_input object in request")
        if "command" not in tool_input:
            return 0  # non-shell tool: nothing to check
        command = tool_input["command"]
        if not isinstance(command, str):
            return _on_error(stdin_text, "tool_input.command is not a string")
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


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))
