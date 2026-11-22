"""Tests for the live-launch PreToolUse hook script (.claude/hooks)."""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "hooks" / "block_live_launch.py"

# Load by compiling the source so no __pycache__ is written into .claude/hooks.
hook = types.ModuleType("block_live_launch")
hook.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), hook.__dict__)

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


def test_main_ignores_leading_byte_order_mark(capsys: pytest.CaptureFixture[str]) -> None:
    # Windows PowerShell prepends a BOM when piping text to a native program.
    assert hook.main("﻿" + _payload(f'python "{ENTRY}"')) == 2
    assert hook.main("﻿" + _payload("git status")) == 0
    assert "not valid JSON" not in capsys.readouterr().err


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


ANSI_BODY = (
    "gh pr create --base revival --title \"t\" --body "
    "$'line1\\npython \"" + ENTRY + "\"\\ncmd /c \"" + BAT + "\"'"
)
HEREDOC_TEXT = (
    "python \"" + ENTRY + "\"\ncmd /c \"" + BAT + "\"\n"
)

NEW_ALLOWED = [
    # item 1: git/gh message text is not code
    'git commit -m "fix launcher: python now runs dum dum.py"',
    f'git commit -m "Add hook" -m "Blocks python on dum dum.py and cmd /c {BAT}"',
    'git commit -m "line1\n\npython dum dum.py is blocked\n"',
    "git commit -q -F - <<'EOF'\n" + HEREDOC_TEXT + "EOF",
    "git commit -m \"$(cat <<'EOF'\nfix: python dum dum.py\nEOF\n)\"",
    'gh issue create -R o/r --title "python dum dum.py crashes" --body '
    f'"Running python on dum dum.py crashes; do not start {BAT}"',
    ANSI_BODY,
    f'gh issue comment 10 --body "Hook blocks cmd /c {BAT}"',
    'git commit --message="python dum dum.py; x" --author=me',
    'git commit -am "python dum dum.py"',
    "cat <<'EOF'\n" + HEREDOC_TEXT + "EOF",
    # item 5: launcher words only count in command position
    f'rg -n "start" "{BAT}"',
    f'grep -c "sh" "{BAT}"',
    f'Select-String -Path "{BAT}" -Pattern "start"',
    f'grep -n "cmd /c" "{BAT}"',
    f'notepad "{BAT}"',
    f'start notepad "{BAT}"',
    f'/usr/bin/cat ./"{BAT}"',
    f'"C:/Program Files/Git/usr/bin/cat.exe" "./{BAT}"',
    # item 4: redirects are not separators
    "git status 2>&1",
    "git status >&2",
]

NEW_BLOCKED = [
    # item 1: only the message is exempt, other code is still scanned
    f'git commit -m "x" && python "{ENTRY}"',
    f'git commit -m "x"; "{BAT}"',
    f'gh pr view 1 && cmd /c "{BAT}"',
    f"python - <<'EOF'\nimport runpy; runpy.run_path(\"{ENTRY}\")\nEOF",
    f"bash <<'EOF'\npython \"{ENTRY}\"\nEOF",
    f"sh <<EOF\npython \"{ENTRY}\"\nEOF",
    f"pwsh <<EOF\npython \"{ENTRY}\"\nEOF",
    f'git commit -m "$(python "{ENTRY}")"',
    f'git commit -m "x" "$(python {ENTRY})"',
    f'git commit -m "x"\npython "{ENTRY}"',
    # unterminated quote / heredoc: not removed, so still scanned
    f'git commit -m "python {ENTRY}',
    f"git commit -F - <<'EOF'\npython \"{ENTRY}\"\n",
    # item 2
    f'cmd /s /c "{BAT}"',
    f'cmd /d /c "{BAT}"',
    f'cmd /q /c "{BAT}"',
    f'cmd.exe /d /s /c "{BAT}"',
    # item 3
    f"powershell -Command \"& '.\\{BAT}'\"",
    f"powershell.exe -NoProfile -Command \"& '.\\{BAT}'\"",
    f"pwsh -c \"& '.\\{BAT}'\"",
    f'powershell -Command "{BAT}"',
    # item 4
    f'git pull & "{BAT}"',
    f'echo x & "{BAT}"',
    f'echo x&python "{ENTRY}"',
    f'python "{ENTRY}" 2>&1',
    f'& "{BAT}"',
    f'& ".venv\\Scripts\\python.exe" "{ENTRY_WIN}"',
    # item 5
    f"Start-Process cmd -ArgumentList '/c', '{BAT}'",
    f'Start-Process -FilePath "{BAT}"',
    f'cmd /c start "" "{BAT}"',
    f'nohup "./{BAT}" &',
    f'saps "{BAT}"',
    f'env FOO=1 "./{BAT}"',
    f'timeout 5 "./{BAT}"',
    f'sudo bash "./{BAT}"',
    f'"C:\\Program Files\\Git\\bin\\bash.exe" "./{BAT}"',
    f'(python "{ENTRY}")',
    f'$(python "{ENTRY}")',
    f'pipx run "{ENTRY}"',
]


def _doc(*lines: str) -> str:
    return "\n".join(lines)


R3_BLOCKED = [
    # separators in/near quotes must still split
    _doc("# don't run this yet", f"& '.\\{BAT}'"),
    f'Get-ChildItem "C:\\Users\\x\\"; & ".\\{BAT}"',
    f'ls "C:\\x\\" ; "./{BAT}"',
    f'dir "C:\\x\\" & "{BAT}"',
    f"echo it's done; & '.\\{BAT}'",
    f'cmd /c "type x.exe & {BAT}"',
    # accepted false positive: the '|' splits and the 2nd piece starts with call
    f'rg -n "start|call" "{BAT}"',
    # heredoc fed to an interpreter / shell through $(cat ...) is code
    _doc(
        "python -c \"$(cat <<'EOF'",
        f'import runpy; runpy.run_path("{ENTRY}")',
        "EOF",
        ')"',
    ),
    _doc("bash -c \"$(cat <<'EOF'", f'python "{ENTRY}"', "EOF", ')"'),
    _doc("bash -c \"$(cat <<'EOF'", f'./"{BAT}"', "EOF", ')"'),
    _doc("eval \"$(cat <<'EOF'", f'python "{ENTRY}"', "EOF", ')"'),
    # git forms that execute their arguments
    f"git rebase -i -x \"cmd /c '{BAT}'\"",
    f'git bisect run cmd /c "{BAT}"',
    f"git submodule foreach 'cmd /c \"{BAT}\"'",
    f"git rebase --exec=\"python '{ENTRY}'\"",
    f'git -c core.editor="python {ENTRY}" commit',
    # sourcing
    f'. "./{BAT}"',
    f'source "./{BAT}"',
    f'. ".\\{BAT}"',
    # spaced exe paths: cmd / launch words beat the reader heuristic
    f'"C:\\Windows\\System32\\cmd.exe" /c "{BAT}"',
    f"\"C:\\Program Files\\Git\\bin\\bash.exe\" -c \"./'{BAT}'\"",
]

R3_ALLOWED = [
    'rg -n "python|dum dum.py"',
    'grep -nE "py;|dum dum.py" -r .',
    f'Select-String -Path "{ENTRY}" -Pattern "connect|python"',
    _doc(
        "git commit -m \"$(cat <<'EOF'",
        f'fix: python "{ENTRY}" and cmd /c "{BAT}"',
        "EOF",
        ')"',
    ),
    _doc("git commit -q -F - <<'EOF'", f'python "{ENTRY}"', "EOF"),
    _doc(
        "gh pr create --body \"$(cat <<'EOF'",
        f'python "{ENTRY}"; cmd /c "{BAT}"',
        "EOF",
        ')"',
    ),
    _doc("gh pr create --body-file - <<'X'", f'python "{ENTRY}"', "X"),
    _doc("cat <<'EOF'", f'python "{ENTRY}"', f'cmd /c "{BAT}"', "EOF"),
    # accepted wrapper-file bypass: body written to a file, run on a later line
    _doc("cat <<EOF > run.sh", f'python "{ENTRY}"', "EOF", "bash run.sh"),
    f'git add "{BAT}"',
    f'git diff -- "{BAT}"',
    f'git log -p -- "{BAT}"',
    f'git mv "{BAT}" x.bat',
    f'git commit -m "x" "{BAT}"',
    f'git show HEAD:"{BAT}"',
    f'git grep -n "cmd /c" -- "{BAT}"',
    f'PYTHONPATH=. .venv/Scripts/ruff.exe check "{ENTRY}"',
    f'PYTHONUTF8=1 git diff -- "{ENTRY}"',
    f'cd . && ls "{BAT}"',
    f'Start-Process -FilePath notepad -ArgumentList "{BAT}"',
    "git add .",
    "git -c user.name=x add .",
]


@pytest.mark.parametrize("command", R3_BLOCKED)
def test_round3_blocked(command: str) -> None:
    assert hook.decide(command)[0] is True, command


@pytest.mark.parametrize("command", R3_ALLOWED)
def test_round3_allowed(command: str) -> None:
    assert hook.decide(command) == (False, ""), command


@pytest.mark.parametrize("command", NEW_ALLOWED)
def test_review_round_allowed(command: str) -> None:
    assert hook.decide(command) == (False, ""), command


@pytest.mark.parametrize("command", NEW_BLOCKED)
def test_review_round_blocked(command: str) -> None:
    assert hook.decide(command)[0] is True, command


@pytest.mark.parametrize(
    "command",
    [
        f'echo "python {ENTRY}"',
        f'printf "python {ENTRY}"',
        f'grep -n python "{ENTRY}"',
    ],
)
def test_more_documented_false_positives(command: str) -> None:
    # Accepted: an interpreter word plus the entry name in one segment.
    assert hook.decide(command)[0] is True


def test_file_association_run_is_not_covered() -> None:
    # Documented gap: no interpreter token, so the tripwire does not see it.
    assert hook.decide(f'& ".\\{ENTRY_WIN}"')[0] is False
    assert hook.decide(f"./{ENTRY}")[0] is False


def test_decide_is_fast_on_adversarial_input() -> None:
    esc = '\\"' * 100_000
    for cmd in (
        'git commit -m "' + "a" * 200_000,
        "& " * 100_000,
        "git " * 50_000,
        "<<a\n" * 50_000,
        "git commit -m '" * 10_000,
        '"' + esc,
        'git commit -m "' + esc,
        "git commit -m $'" + esc,
        "git commit --body=\"" + esc + "\" -m \"" + esc,
    ):
        start = time.perf_counter()
        hook.decide(cmd)
        # Generous: only meant to catch quadratic blow-ups (tens of seconds).
        assert time.perf_counter() - start < 5.0


def test_main_tool_input_variants(capsys: pytest.CaptureFixture[str]) -> None:
    for tool_input in ([], None, "text", ["x"], 5):
        plain = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
        assert hook.main(plain) == 0
        assert capsys.readouterr().err.strip()
        named = json.dumps({"tool_name": "Bash", "tool_input": tool_input, "x": ENTRY})
        assert hook.main(named) == 2
        assert capsys.readouterr().err.strip()
    missing = json.dumps({"tool_name": "Bash"})
    assert hook.main(missing) == 0
    assert capsys.readouterr().err.strip()
    assert hook.main(json.dumps({"tool_name": "Bash", "note": ENTRY})) == 2
    assert capsys.readouterr().err.strip()


def test_main_top_level_json_variants(capsys: pytest.CaptureFixture[str]) -> None:
    for text in ("[]", "5", '"str"', "null", "true"):
        assert hook.main(text) == 0
        assert capsys.readouterr().err.strip()
    for text in (json.dumps([ENTRY]), json.dumps(ENTRY)):
        assert hook.main(text) == 2
        assert capsys.readouterr().err.strip()


def test_main_shell_tool_without_command(capsys: pytest.CaptureFixture[str]) -> None:
    for tool in ("Bash", "PowerShell"):
        no_cmd = json.dumps({"tool_name": tool, "tool_input": {}})
        assert hook.main(no_cmd) == 0
        assert capsys.readouterr().err.strip()  # warning
        named = json.dumps({"tool_name": tool, "tool_input": {"description": ENTRY}})
        assert hook.main(named) == 2
        assert capsys.readouterr().err.strip()
        for empty in ("", "  \n"):
            ok = json.dumps({"tool_name": tool, "tool_input": {"command": empty}})
            assert hook.main(ok) == 0
            assert capsys.readouterr().err == ""
        named_empty = json.dumps(
            {"tool_name": tool, "tool_input": {"command": " ", "description": ENTRY}}
        )
        assert hook.main(named_empty) == 2
        assert capsys.readouterr().err.strip()


class _BrokenStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        raise OSError("boom")


def test_run_stdin_handles_bad_bytes_and_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    allowed = b'{"tool_name": "Bash", "tool_input": {"command": "ls \xff\xfe"}}'
    assert hook.run_stdin(io.BytesIO(allowed)) == 0
    assert capsys.readouterr().err == ""
    blocked = (
        b'{"tool_name": "Bash", "tool_input": {"command": "python \\"'
        + ENTRY.encode()
        + b'\\" \xff"}}'
    )
    assert hook.run_stdin(io.BytesIO(blocked)) == 2
    capsys.readouterr()
    assert hook.run_stdin(io.BytesIO(b"\xff\xfe not json")) == 0
    assert capsys.readouterr().err.strip()
    assert hook.run_stdin(io.BytesIO(b"\xff dum dum")) == 2
    capsys.readouterr()
    assert hook.run_stdin(_BrokenStream(b"")) == 0
    assert capsys.readouterr().err.strip()


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
