"""Tests for the live-launch PreToolUse hook script (.claude/hooks)."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "hooks" / "block_live_launch.py"

_spec = importlib.util.spec_from_file_location("block_live_launch", SCRIPT)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

ENTRY = "MainStoinker/MainStuff/this is how we do it dum dum.py"
ENTRY_WIN = ENTRY.replace("/", "\\")
BAT = "Stonks data collect.bat"
PS_ESCAPED = "MainStoinker\\MainStuff\\this` is` how` we` do` it` dum` dum.py"

BLOCKED = [
    f'python "{ENTRY}"',
    f'.venv\\Scripts\\python.exe "{ENTRY_WIN}"',
    f"& \".venv\\Scripts\\python.exe\" '{ENTRY_WIN}'",
    "python MainStoinker/MainStuff/this\\ is\\ how\\ we\\ do\\ it\\ dum\\ dum.py",
    f"python {PS_ESCAPED}",
    'cd MainStoinker/MainStuff && py "this is how we do it dum dum.py"',
    f'py -3.14 "{ENTRY}"',
    f'uv run "{ENTRY}"',
    f'uv run --python .venv/Scripts/python.exe "{ENTRY}"',
    f'python -m debugpy --listen 5678 "{ENTRY}"',
    f"python -c \"import runpy; runpy.run_path('{ENTRY}')\"",
    f"python -c \"exec(open('{ENTRY}').read())\"",
    f'PYTHON.EXE "{ENTRY.upper()}"',
    f'"C:\\Users\\Someone\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe" "{ENTRY}"',
    f'git status; python "{ENTRY}"',
    f'echo hi | python "{ENTRY}"',
    f"bash -c \"python '{ENTRY}'\"",
    f"powershell -Command \"python '{ENTRY}'\"",
    f'"{BAT}"',
    "./Stonks\\ data\\ collect.bat",
    f'.\\"{BAT}"',
    f"& '.\\{BAT}'",
    f'cmd /c "{BAT}"',
    f'cmd //c "{BAT}"',
    f'cmd.exe /c "{BAT}"',
    f'call "{BAT}"',
    f'start "" "{BAT}"',
    f'Start-Process "{BAT}"',
    f'Invoke-Item "{BAT}"',
    f'"C:\\Users\\Someone\\Documents\\Stonks\\{BAT}"',
    f'cd .. && "Stonks/{BAT}"',
    f'echo starting\npython "{ENTRY}"\necho done',
]

ALLOWED = [
    f'cat "{ENTRY}"',
    f'Get-Content "{ENTRY}"',
    f'type "{BAT}"',
    f'head -20 "{ENTRY}"',
    f'wc -l "{ENTRY}"',
    f'grep -n connect "{ENTRY}"',
    'rg "dum dum.py"',
    'rg -n "data collect.bat" .',
    f'git diff -- "{ENTRY}"',
    f'git log -p -- "{BAT}"',
    f'git show HEAD:"{BAT}"',
    f'git blame "{ENTRY}"',
    f'sed -n 1,5p "{BAT}"',
    f'.venv\\Scripts\\ruff.exe check "{ENTRY}"',
    ".venv\\Scripts\\python.exe -m pytest",
    ".venv/Scripts/python.exe -m pytest -m slow -q",
    "python --version",
    "py --version",
    f'git diff -- "{ENTRY}" && python -m pytest',
    "python -m pytest tests/test_block_live_launch_hook.py",
    "git status",
    "ls -la",
    "git push origin task/10-hook-script",
    "git show HEAD~1 --stat",
    'echo "do not run dum dum.py"',
    "copy notes.txt happy.py",
    "restart-service foo",
    f'git add "{BAT}" && git commit -m "edit launcher"',
    f'ls "{BAT}"',
    f'Test-Path "{BAT}"',
    f'code "{ENTRY}"',
]

# Accepted false positives: an interpreter token plus the protected path in one
# segment is blocked even when the program is only linting/reading the file.
FALSE_POSITIVES = [
    f'.venv\\Scripts\\python.exe -m ruff check "{ENTRY_WIN}"',
    f'python tools/lint.py "{ENTRY}"',
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocks_execution(command: str) -> None:
    blocked, reason = hook.decide(command)
    assert blocked, command
    assert reason


@pytest.mark.parametrize("command", ALLOWED)
def test_allows_non_execution(command: str) -> None:
    assert hook.decide(command) == (False, "")


@pytest.mark.parametrize("command", FALSE_POSITIVES)
def test_documented_false_positives(command: str) -> None:
    # Blocking these is accepted: the tripwire cannot tell a linter run through
    # python from a run of the script itself.
    assert hook.decide(command)[0] is True


@pytest.mark.parametrize(
    "command",
    [
        f'echo pytest "{ENTRY}"',
        f'ls copy happy.py "{ENTRY}"',
        f'git show HEAD "{ENTRY}"',
        f'git push "{ENTRY}"',
        f'bash-completion "{ENTRY}"',
        f'ls restart "{BAT}"',
        f'ls start/x "{BAT}"',
        f'ls foo.sh "{BAT}"',
        f'ls skiing "{BAT}"',
        f'ls notebook.ipynb "{ENTRY}"',
    ],
)
def test_short_token_traps_are_whole_word(command: str) -> None:
    assert hook.decide(command)[0] is False, command


def _payload(command: str, tool: str = "Bash") -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_main_blocks(tool: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.main(_payload(f'python "{ENTRY}"', tool)) == 2
    assert capsys.readouterr().err.strip()


def test_main_allows(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.main(_payload("git status")) == 0
    assert capsys.readouterr().err == ""


def test_main_non_shell_tool_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert hook.main(payload) == 0
    assert capsys.readouterr().err == ""


def test_main_read_of_entry_script_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": ENTRY}})
    assert hook.main(payload) == 0
    assert capsys.readouterr().err == ""


def test_main_non_string_command_follows_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = {"tool_name": "Bash", "tool_input": {"command": ["python", ENTRY]}}
    assert hook.main(json.dumps(bad)) == 2
    assert capsys.readouterr().err.strip()
    ok = {"tool_name": "Bash", "tool_input": {"command": 5}}
    assert hook.main(json.dumps(ok)) == 0
    assert capsys.readouterr().err.strip()


def test_main_non_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.main("not json at all") == 0
    assert capsys.readouterr().err.strip()
    assert hook.main(f"python {ENTRY}") == 2
    assert capsys.readouterr().err.strip()
    assert hook.main("") == 0
    assert capsys.readouterr().err.strip()


def test_main_decide_raises_follows_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(command: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "decide", boom)
    assert hook.main(_payload(f'python "{ENTRY}"')) == 2
    assert capsys.readouterr().err.strip()
    assert hook.main(_payload("git status")) == 0
    assert capsys.readouterr().err.strip()


def test_subprocess_roundtrip() -> None:
    blocked = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input=_payload(f'python "{ENTRY}"'),
        text=True,
        capture_output=True,
    )
    assert blocked.returncode == 2
    assert blocked.stderr.strip()
    allowed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input=_payload("git status"),
        text=True,
        capture_output=True,
    )
    assert allowed.returncode == 0


def test_script_uses_only_stdlib() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    assert modules
    assert modules <= set(sys.stdlib_module_names)
