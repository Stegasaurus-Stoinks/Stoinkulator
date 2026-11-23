"""Tests for the live-launch PreToolUse hook script (.claude/hooks)."""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import itertools
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


# --- T10: connecting MainStoinker commands -------------------------------------
# Allowlist rule: a MainStoinker program run is blocked unless its first
# positional argument is backtest / runs / report / serve (or absent).

VENV_PY = ".venv\\Scripts\\python.exe"
SPACED_PY = '"C:\\Program Files\\Python 3.14\\python.exe"'
CONNECTING_SUBCOMMANDS = ["fetch-bars", "probe-data", "run", "frobnicate", "Fetch-Bars"]
OFFLINE_SUBCOMMANDS = ["backtest", "runs", "report", "serve"]

_INTERPRETER_PREFIXES = [
    "python",
    "python3",
    "python3.14",
    "py -3.14",
    "py",
    "pythonw",
    "PYTHON.EXE",
    VENV_PY,
    ".venv/Scripts/python.exe",
    f'"{VENV_PY}"',
    f"& \"{VENV_PY}\"",
    SPACED_PY,
    "C:/Users/x/AppData/Local/Python/pythoncore-3.14-64/python.exe",
    "python -X dev",
    "python -X utf8 -W ignore -u",
    "python -B -I",
    "uv run python",
    "uv run --python .venv/Scripts/python.exe python",
    "uv run",
    "pipx run python",
    "VAR=x python",
    "PYTHONPATH=. PYTHONUTF8=1 python",
    "env FOO=1 python",
    "nohup python",
    "timeout 5 python",
    "sudo python",
    "  python",
]


def _mod(prefix: str, sub: str, module: str = "MainStoinker") -> str:
    return f"{prefix} -m {module} {sub}".rstrip()


def test_blocks_connecting_subcommands() -> None:
    for sub in CONNECTING_SUBCOMMANDS:
        for prefix in _INTERPRETER_PREFIXES:
            cmd = _mod(prefix, sub)
            assert hook.decide(cmd)[0] is True, cmd
        forms = [
            f"python -m MainStoinker {sub} --symbol AAPL --month 2026-01",
            f"python -m  MainStoinker   {sub}",
            f"python\t-m MainStoinker {sub}",
            f"python -m mainstoinker {sub}",
            f"python -m MAINSTOINKER {sub.upper()}",
            f"python -mMainStoinker {sub}",
            f"python -Im MainStoinker {sub}",
            f"python -m MainStoinker.__main__ {sub}",
            f"python -m MainStoinker.engine.cli {sub}",
            f"python -X dev -m MainStoinker {sub}",
            f"python -W error -X utf8 -m MainStoinker {sub}",
            f"py -3.14 -m MainStoinker {sub}",
            f"python -m MainStoinker --verbose {sub}",
            f"python -m MainStoinker -- {sub}",
            f"python -m MainStoinker --db x.db {sub}",
            f'python -m MainStoinker "{sub}"',
            f"python -m MainStoinker '{sub}'",
            f"python -m MainStoinker {sub} 2>&1",
            f"python -m MainStoinker 2>&1 {sub}",
            f"python -m MainStoinker > out.txt {sub}",
            f"(python -m MainStoinker {sub})",
            f"$(python -m MainStoinker {sub})",
            f"{{ python -m MainStoinker {sub}; }}",
            f"python -m MainStoinker {sub} &",
            # chaining
            f"git status; python -m MainStoinker {sub}",
            f"git status && python -m MainStoinker {sub}",
            f"git status || python -m MainStoinker {sub}",
            f"git status | python -m MainStoinker {sub}",
            f"git status & python -m MainStoinker {sub}",
            f"echo hi\npython -m MainStoinker {sub}\necho done",
            f"python -m MainStoinker backtest ; python -m MainStoinker {sub}",
            f"python -m MainStoinker {sub} && git status",
            # environment-variable prefixes
            f'$env:STOINK_X="1"; python -m MainStoinker {sub}',
            f'$env:STOINK_X = "1"; & "{VENV_PY}" -m MainStoinker {sub}',
            f"set STOINK_X=1 && python -m MainStoinker {sub}",
            f"VAR=x python -m MainStoinker {sub}",
            # nesting
            f'bash -c "python -m MainStoinker {sub}"',
            f"bash -c 'git status; python -m MainStoinker {sub}'",
            f'bash -lc "python -m MainStoinker {sub}"',
            f'sh -c "{VENV_PY} -m MainStoinker {sub}"',
            f'cmd /c "python -m MainStoinker {sub}"',
            f'cmd.exe /d /s /c "python -m MainStoinker {sub}"',
            f'cmd //c "python -m MainStoinker {sub}"',
            f'powershell -Command "python -m MainStoinker {sub}"',
            f"powershell.exe -NoProfile -Command \"& '{VENV_PY}' -m MainStoinker {sub}\"",
            f'pwsh -c "python -m MainStoinker {sub}"',
            f'powershell -Command "$env:X=\'1\'; python -m MainStoinker {sub}"',
            f"bash -c \"bash -c 'python -m MainStoinker {sub}'\"",
            f'cmd /c powershell -Command "python -m MainStoinker {sub}"',
            f'eval "python -m MainStoinker {sub}"',
            f"xargs python -m MainStoinker {sub}",
            f"find . -name x -exec python -m MainStoinker {sub} ;",
            f'git -c alias.x="!python -m MainStoinker {sub}" x',
            # paths with spaces, both slashes, upper and lower case
            f'python "MainStoinker/__main__.py" {sub}',
            f'python "MainStoinker\\__main__.py" {sub}',
            f"python MainStoinker/__main__.py {sub}",
            f"python MAINSTOINKER\\__MAIN__.PY {sub}",
            f'python "C:\\Users\\Paul Wilda\\Documents\\Stonks\\MainStoinker\\__main__.py" {sub}',
            f'{VENV_PY} "C:/Users/Paul Wilda/OneDrive - 28 Gorilla/Stonks/MainStoinker/__main__.py" {sub}',
            f"& \"{VENV_PY}\" 'C:\\x y\\MainStoinker\\__main__.py' {sub}",
            f"python -X dev MainStoinker/__main__.py {sub}",
            f"uv run MainStoinker/__main__.py {sub}",
            f"python MainStoinker {sub}",
            f"python MainStoinker/ {sub}",
            f'python "C:\\x y\\MainStoinker" {sub}',
            f"python -u .\\MainStoinker {sub}",
            # the subcommand hidden from the text
            "python -m MainStoinker $SUB",
            'python -m MainStoinker "$@"',
            "python -m MainStoinker $env:SUB",
            "python -m MainStoinker %SUB%",
            "python -m MainStoinker $1",
            "python -m MainStoinker $(echo fetch-bars)",
            "python -m MainStoinker `echo fetch-bars`",
        ]
        for cmd in forms:
            assert hook.decide(cmd)[0] is True, cmd


def test_blocked_connecting_message_names_why_and_owner() -> None:
    blocked, reason = hook.decide("python -m MainStoinker fetch-bars")
    assert blocked
    assert "\n" not in reason.strip()
    assert "fetch-bars" in reason
    assert "owner" in reason


def test_blocks_connecting_main_payload(capsys: pytest.CaptureFixture[str]) -> None:
    for tool in ("Bash", "PowerShell"):
        assert hook.main(_payload("python -m MainStoinker probe-data", tool)) == 2
        assert "owner" in capsys.readouterr().err
    assert hook.main(_payload("python -m MainStoinker backtest --help")) == 0
    assert capsys.readouterr().err == ""


def test_allows_offline_subcommands() -> None:
    for sub in OFFLINE_SUBCOMMANDS:
        for prefix in _INTERPRETER_PREFIXES:
            cmd = _mod(prefix, sub)
            assert hook.decide(cmd) == (False, ""), cmd
        for cmd in (
            f"python -m MainStoinker {sub} --db x.db --json",
            f"git status && python -m mainstoinker {sub}",
            f'bash -c "python -m MainStoinker {sub}"',
            f'powershell -Command "python -m MainStoinker {sub}"',
            f"python MainStoinker/__main__.py {sub}",
            f'python "C:\\x y\\MainStoinker\\__main__.py" {sub}',
            f"python -X dev -m MainStoinker {sub} 2>&1",
            f"(python -m MainStoinker {sub})",
            f"python -m MainStoinker --version {sub}",
        ):
            assert hook.decide(cmd) == (False, ""), cmd
    for cmd in (
        "python -m MainStoinker",
        "python -m MainStoinker --help",
        "python -m MainStoinker -h",
        "python -m MainStoinker 2>&1",
        f"{VENV_PY} -m MainStoinker",
        "py -3.14 -m MainStoinker --help",
        'bash -c "python -m MainStoinker"',
        "python -m pytest tests/test_engine_cli.py",
        "python -m pytest -m slow",
        "python -m ruff check MainStoinker/__main__.py",
        "uv run ruff check MainStoinker/__main__.py",
        "uv run pytest MainStoinker/__main__.py",
        "ruff check tests MainStoinker/engine MainStoinker/__main__.py --select E,F,W,I,B",
        f"{VENV_PY} -m ruff check MainStoinker/engine",
        f"{VENV_PY} -m pytest MainStoinker",
        "python -m pytest MainStoinker/testy",
        "python tools/lint.py MainStoinker/engine",
    ):
        assert hook.decide(cmd) == (False, ""), cmd


IB_BLOCKED = [
    "python -m MainStoinker.ib",
    "python -m MainStoinker.ib.cli",
    "python -m MainStoinker.ib.cli fetch-bars",
    "python -m MainStoinker.ib.connection --help",
    "python -m mainstoinker.ib.cli",
    "python -m MAINSTOINKER.IB.CLI",
    "python -mMainStoinker.ib.cli",
    "python -X dev -m MainStoinker.ib.cli",
    "py -3.14 -m MainStoinker.ib.cli",
    f"{VENV_PY} -m MainStoinker.ib.cli",
    f'"{VENV_PY}" -m MainStoinker.ib.cli',
    f"& \"{VENV_PY}\" -m MainStoinker.ib.cli",
    f"{SPACED_PY} -m MainStoinker.ib.cli",
    "uv run -m MainStoinker.ib.cli",
    "uv run python -m MainStoinker.ib.cli",
    "pipx run python -m MainStoinker.ib.cli",
    "VAR=x python -m MainStoinker.ib.cli",
    '$env:X="1"; python -m MainStoinker.ib.cli',
    "git status; python -m MainStoinker.ib.cli",
    "git status && python -m MainStoinker.ib.cli",
    "git status || python -m MainStoinker.ib.cli",
    "git status | python -m MainStoinker.ib.cli",
    'bash -c "python -m MainStoinker.ib.cli"',
    'sh -c "python -m MainStoinker.ib.cli"',
    'cmd /c "python -m MainStoinker.ib.cli"',
    'powershell -Command "python -m MainStoinker.ib.cli"',
    'pwsh -c "python -m MainStoinker.ib.cli"',
    'python -c "import MainStoinker.ib.connection"',
    "python -c 'import MainStoinker.ib.connection'",
    'python -c "from MainStoinker.ib import connection"',
    'python -c "from MainStoinker.ib.connection import Conn; Conn().connect()"',
    'python -c "from MainStoinker import ib"',
    'python -c "import MainStoinker.ib"',
    'python -c "import MainStoinker . ib"',
    'python -c "import os; import MainStoinker.ib.cli as c"',
    'python -c "import os; from MainStoinker.ib import connection"',
    "python -c \"import importlib; importlib.import_module('MainStoinker.ib.connection')\"",
    "python -c \"__import__('MainStoinker.ib.connection')\"",
    "python -c \"import runpy; runpy.run_module('MainStoinker.ib.cli', run_name='__main__')\"",
    "python -c \"import runpy; runpy.run_path('MainStoinker/ib/cli.py')\"",
    "python MainStoinker/ib/cli.py",
    "python MainStoinker\\ib\\cli.py fetch-bars",
    '"C:\\Program Files\\Python\\python.exe" "C:\\Users\\Paul Wilda\\Stonks\\MainStoinker\\ib\\cli.py"',
    "python -X dev MainStoinker/ib/cli.py",
    "python - <<'EOF'\nimport MainStoinker.ib.connection\nEOF",
    "python <<EOF\nfrom MainStoinker.ib import connection\nEOF",
    'python -i -c "import MainStoinker.ib.connection"',
    f"bash -c \"{VENV_PY} -c 'import MainStoinker.ib.connection'\"",
    'git status; python -c "import MainStoinker.ib.connection"',
]


@pytest.mark.parametrize("command", IB_BLOCKED)
def test_blocks_ib_module_runs(command: str) -> None:
    blocked, reason = hook.decide(command)
    assert blocked is True, command
    assert "owner" in reason


# Runs of the package from code: the subcommand is not visible in the text,
# so the hook blocks them all rather than guess.
RUNPY_BLOCKED = [
    "python -c \"import runpy; runpy.run_module('MainStoinker', run_name='__main__')\"",
    "python -c \"import runpy; runpy.run_module('MainStoinker')\" fetch-bars",
    "python -c \"import runpy as r; r.run_module('MainStoinker', run_name='__main__')\"",
    "python -c \"import sys; sys.argv=['x','fetch-bars']; import runpy; "
    "runpy.run_module('MainStoinker', run_name='__main__')\"",
    "python -c \"import runpy; runpy.run_path('MainStoinker/__main__.py', run_name='__main__')\"",
    "python -c \"import runpy; runpy.run_path('MainStoinker\\\\__main__.py')\"",
    "python -c \"import importlib; importlib.import_module('MainStoinker.__main__')\"",
    'python -c "import MainStoinker.__main__"',
    "python -c \"exec(open('MainStoinker/__main__.py').read())\"",
    "python -c \"import subprocess; subprocess.run(['python','-m','MainStoinker','fetch-bars'])\"",
    "python -c \"import os; os.system('python -m MainStoinker fetch-bars')\"",
    "uv run python -c \"import runpy; runpy.run_module('MainStoinker')\"",
    f"{VENV_PY} -c \"import runpy; runpy.run_module('MainStoinker')\"",
    "python - <<'EOF'\nimport runpy\nrunpy.run_module('MainStoinker')\nEOF",
]


@pytest.mark.parametrize("command", RUNPY_BLOCKED)
def test_blocks_runpy_runs_of_the_package(command: str) -> None:
    assert hook.decide(command)[0] is True, command


def test_allows_pytest_and_ruff_on_ib_files() -> None:
    for cmd in (
        "python -m pytest tests/test_ib_connection.py",
        "python -m pytest tests/test_ib_connection.py -q -x",
        f"{VENV_PY} -m pytest tests/test_ib_connection.py",
        f"{VENV_PY} -m pytest MainStoinker/ib",
        "python -m pytest MainStoinker/ib/tests",
        "python -m pytest -m slow tests/test_ib_cli.py",
        "python -m pytest tests/ -k ib",
        "python -m pytest --cov=MainStoinker.ib tests/test_ib_connection.py",
        "python -m pytest tests/test_ib_connection.py::test_connect",
        "ruff check MainStoinker/ib",
        "ruff check MainStoinker\\ib tests",
        f"{VENV_PY.replace('python', 'ruff')} check MainStoinker/ib",
        f"{VENV_PY} -m ruff check MainStoinker/ib",
        "python -m ruff check MainStoinker/ib --select E,F,W,I,B",
        "python -m ruff format MainStoinker/ib/connection.py",
        "uv run ruff check MainStoinker/ib",
        "uv run pytest tests/test_ib_connection.py",
        "ruff check tests MainStoinker/engine MainStoinker/ib --select E,F,W,I,B",
        "python -m mypy MainStoinker/ib",
        "git add MainStoinker/ib/connection.py tests/test_ib_connection.py",
        "git diff -- MainStoinker/ib",
        "cat MainStoinker/ib/connection.py",
        "grep -rn connect MainStoinker/ib",
        'rg -n "import MainStoinker.ib" tests',
        'rg "from MainStoinker.ib import" .',
        "git grep -n MainStoinker.ib",
        'Select-String -Path tests/*.py -Pattern "MainStoinker.ib"',
        "Get-Content MainStoinker/ib/connection.py",
        "ls MainStoinker/ib",
        "code MainStoinker/ib/connection.py",
        "git status && python -m pytest tests/test_ib_connection.py",
        'bash -c "python -m pytest tests/test_ib_connection.py"',
        'powershell -Command "ruff check MainStoinker/ib"',
    ):
        assert hook.decide(cmd) == (False, ""), cmd


def test_commit_message_naming_fetch_bars_allowed() -> None:
    for cmd in (
        'git commit -m "Add fetch-bars: python -m MainStoinker fetch-bars writes monthly files"',
        'git commit -m "x" -m "python -m MainStoinker probe-data and MainStoinker.ib.cli"',
        'git commit -am "python -m MainStoinker run"',
        'git commit --message="python -m MainStoinker fetch-bars"',
        "git commit -q -F - <<'EOF'\npython -m MainStoinker fetch-bars\nEOF",
        "git commit -m \"$(cat <<'EOF'\nfix: python -m MainStoinker.ib.cli\nEOF\n)\"",
        'gh pr create --title "T10" --body "Blocks python -m MainStoinker fetch-bars"',
        'gh issue comment 123 --body "python -m MainStoinker run is blocked"',
        "gh pr create --body-file - <<'X'\npython -m MainStoinker run\nX",
        'echo "python -m MainStoinker fetch-bars"',
        "echo run python -m MainStoinker probe-data",
        'printf "python -m MainStoinker run"',
        'Write-Output "python -m MainStoinker fetch-bars"',
        'grep -rn "python -m MainStoinker fetch-bars" docs',
        "grep -n 'MainStoinker.ib' -r .",
        'rg -n "-m MainStoinker run" docs',
        'rg "MainStoinker fetch-bars"',
        'Select-String -Path docs/*.md -Pattern "-m MainStoinker run"',
        'git log --grep="fetch-bars"',
        'git log -S "python -m MainStoinker fetch-bars"',
        'git grep -n "python -m MainStoinker run"',
        "cat docs/specs/live-side.md",
        "head -50 MainStoinker/__main__.py",
        "git diff -- MainStoinker/__main__.py",
        "git show HEAD:MainStoinker/__main__.py",
        "ls MainStoinker",
        "python -m pytest tests -k fetch_bars",
        "python -m pytest tests/test_fetch_bars.py tests/test_probe_data.py",
        'echo "python -m MainStoinker fetch-bars"; git status',
    ):
        assert hook.decide(cmd) == (False, ""), cmd


def test_message_text_does_not_hide_a_real_run() -> None:
    for cmd in (
        'git commit -m "x" && python -m MainStoinker fetch-bars',
        'git commit -m "x"; python -m MainStoinker run',
        "gh pr view 1 && python -m MainStoinker probe-data",
        'git commit -m "$(python -m MainStoinker fetch-bars)"',
        'echo "$(python -m MainStoinker fetch-bars)"',
        "echo `python -m MainStoinker fetch-bars`",
        "echo hi | python -m MainStoinker fetch-bars",
        "echo hi; python -m MainStoinker fetch-bars",
        "echo (python -m MainStoinker fetch-bars)",
        "git commit -F - <<'EOF'\nmsg\nEOF\npython -m MainStoinker fetch-bars",
        "bash <<'EOF'\npython -m MainStoinker fetch-bars\nEOF",
        'git -c core.editor="python -m MainStoinker fetch-bars" commit',
        'git rebase --exec="python -m MainStoinker run" HEAD~1',
        'git commit -m "python -m MainStoinker fetch-bars',  # unterminated: scanned
    ):
        assert hook.decide(cmd)[0] is True, cmd


def test_existing_rules_still_fire_alongside_the_new_one() -> None:
    assert hook.decide(f'python "{ENTRY}"')[0] is True
    assert hook.decide(f'cmd /c "{BAT}"')[0] is True
    assert hook.decide("python -m MainStoinker backtest && python -m MainStoinker run")[0]


def test_connecting_rule_fails_closed_on_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(segment: str) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_scan_connecting", boom)
    assert hook.decide("python -m MainStoinker backtest")[0] is True
    assert hook.decide("git status") == (False, "")


def test_connecting_rule_is_fast_on_adversarial_input() -> None:
    for cmd in (
        "python -m MainStoinker " + "-x " * 100_000,
        "python -m MainStoinker " + "> " * 100_000,
        ("python -m MainStoinker backtest; " * 20_000),
        "MainStoinker " * 100_000,
        "python " + "MainStoinker/ib " * 50_000,
        "echo " + "mainstoinker.ib " * 50_000,
    ):
        start = time.perf_counter()
        hook.decide(cmd)
        assert time.perf_counter() - start < 5.0


# --- T10 fix round: review findings of PR 133 ------------------------------------
# Strings below are command text used as data; none of them is ever executed.

_EXPAND = (
    ("@P@", "MainStoinker"),
    ("@F@", "fetch-bars"),
    ("@R@", "probe-data"),
    ("@N@", "\n"),
    ("@T@", "\t"),
    ("@Q@", "'"),
    ("@BT@", "`"),
    ("@BS@", "\\"),
    ("@ZW@", "\u200b"),
)


def _x(raw: str) -> str:
    for key, value in _EXPAND:
        raw = raw.replace(key, value)
    return raw


def _table(text: str) -> list[tuple[str, str]]:
    """Lines of `V command` (V = B blocked / A allowed) -> (V, expanded command)."""
    rows = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            rows.append((line[0], line[2:]))
    return rows


# The reviewer's adversarial list: every row is a connecting command or a way
# to hide one. B = must be blocked.
ADV_TABLE = r"""
B python -m @P@ @F@
B python3 -m @P@ @F@
B py -3 -m @P@ @F@
B pythonw -m @P@ @F@
B .venv@BS@Scripts@BS@python.exe -m @P@ @F@
B ".venv@BS@Scripts@BS@python.exe" -m @P@ @F@
B ../.venv/Scripts/Python.EXE -m @P@ @F@
B uv run -m @P@ @F@
B uv run --module @P@ @F@
B pipx run --spec . python -m @P@ @F@
B poetry run python -m @P@ @F@
B conda run -n base python -m @P@ @F@
B hatch run python -m @P@ @F@
B start python -m @P@ @F@
B start "" python -m @P@ @F@
B cmd /c python -m @P@ @F@
B call python -m @P@ @F@
B wsl python3 -m @P@ @F@
B bash -lc "python -m @P@ @F@"
B sh -c @Q@python -m @P@ @F@@Q@
B pwsh -c "python -m @P@ @F@"
B Invoke-Expression "python -m @P@ @F@"
B iex "python -m @P@ @F@"
B Invoke-Expression ("python -m @P@ " + "@F@")
B & "python" -m @P@ @F@
B & $py -m @P@ @F@
B Start-Process python -ArgumentList "-m @P@ @F@"
B Start-Process python -ArgumentList @Q@-m@Q@,@Q@@P@@Q@,@Q@@F@@Q@
B Start-Process python -ArgumentList @Q@-m@Q@, @Q@@P@@Q@, @Q@@F@@Q@
B Start-Process -FilePath python -ArgumentList @("-m","@P@","@F@") -Wait
B & python @("-m","@P@","@F@")
B $a = @("-m","@P@","@F@"); & python @a
B python -I -m @P@ @F@
B python -X dev -W ignore -u -O -m @P@ @F@
B python -OO -B -m@P@ @F@
B python -m "@P@" "@F@"
B python -m Main"Stoinker" fetch"-bars"
B python -m @P@ $SUB
B python -m @P@ ${SUB}
B python -m @P@ $(cat sub.txt)
B python -m @P@ @BS@@N@  @F@
B python -m @P@ @BT@@N@  @F@
B python -m @P@ --symbols AAPL @BT@@N@  @F@
B python -m @P@ ^@N@@F@
B cmd /c "python -m @P@ ^@N@@F@"
A python -m @P@@N@@F@
B python@T@-m@T@@P@@T@@F@
B PYTHON -M MAINSTOINKER FETCH-BARS
B cd @P@ && python __main__.py @F@
B cd @P@; python . @F@
B cd @P@ && python ib/cli.py
B python __main__.py @F@
B python . @F@
B python ./@P@/. @F@
B python @P@/./__main__.py @F@
B .@BS@@P@@BS@__main__.py @F@
B ./@P@/__main__.py @F@
B & ".@BS@@P@@BS@__main__.py" @F@
B python -m @P@.__main__ @F@
B python -m @P@.engine.cli @F@
B python -c "import runpy; runpy.run_module(@Q@@P@@Q@, run_name=@Q@__main__@Q@)" @F@
B python -c "import importlib; importlib.import_module(@Q@@P@.__main__@Q@)" @F@
B python -c "__import__(@Q@@P@.__main__@Q@)" @F@
B python -c "exec(open(@Q@@P@/__main__.py@Q@).read())" @F@
B python -c "import sys; sys.argv=[@Q@x@Q@,@Q@@F@@Q@]; import @P@.__main__"
B python -c "from @P@.ib.cli import main; main([@Q@@F@@Q@])"
B python -c "import @P@.__main__" @F@
B python -c "from @P@ import __main__" @F@
B python -c "from @P@ import __main__ as m"
B python -c "import @P@.ib.cli as c; c.main([])"
B python -c "from @P@ import ib"
B python -c "from @P@ import (ib)"
B python -c "from @P@ import engine, ib"
B python -c "import @P@ .ib"
B python -m @P@.ib.cli
B python -m pdb @P@/__main__.py @F@
B python -m cProfile @P@/__main__.py @F@
B python -m cProfile -o x.prof @P@/__main__.py @F@
B python -m debugpy --listen 5678 @P@/__main__.py @F@
B python -m debugpy --listen 5678 -m @P@ @F@
B python -m coverage run @P@/__main__.py @F@
B python -m coverage run -m @P@ @F@
B coverage run @P@/__main__.py @F@
B coverage run -m @P@ @F@
B python -m pdb @P@/ib/cli.py
B python -m trace --count @P@/__main__.py @F@
B python -m runpy @P@ @F@
B echo @F@ | xargs python -m @P@
B printf "@F@\n" | xargs python -m @P@
B echo @F@ | xargs -I{} python -m @P@ {}
B find . -maxdepth 0 -exec python -m @P@ @F@ ;
B env python -m @P@ @F@
B env -i PATH=x python -m @P@ @F@
B nohup python -m @P@ @F@ &
B timeout 60 python -m @P@ @F@
B time python -m @P@ @F@
B echo "python -m @P@ @F@" | bash
B echo "python -m @P@ @F@" | sh
B echo "import runpy; runpy.run_module(@Q@@P@@Q@)" | python -
B echo "python -m @P@ @F@" | iex
B "python -m @P@ @F@" | Invoke-Expression
B bash <(echo "python -m @P@ @F@")
B echo "python -m @P@ @F@" > x.sh && bash x.sh
B echo "import @P@.ib.cli" > x.py && python x.py
B printf "python -m @P@ @F@" > x.sh; sh x.sh
B Set-Content x.ps1 "python -m @P@ @F@"; ./x.ps1
B @"@N@python -m @P@ @F@@N@"@ | iex
B python -m @P@ @F@ # comment
B git status; python -m @P@ run
B git commit -m "x" ; python -m @P@ run
B gh pr view 1 --json body | python -m @P@ @F@
B git config alias.fb "!python -m @P@ @F@" && git fb
B git -c alias.fb="!python -m @P@ @F@" fb
B vim -c "!python -m @P@ @F@" -c q
A rg --pre ./fb.sh x
B python -m @P@ --db x.db @F@
B python -m @P@ -- @F@
B python -m @P@ ""  @F@
B python -m @P@ $null @F@
B python -m @P@ 2>$null @F@
B python -m @P@ 2> nul @F@
B python -m @P@ >>log.txt @F@
B python -m @P@ @R@
B python -m @P@ run --port 7497
B python -m @P@ paper
B python -m @P@ backtest; python -m @P@ @F@
B python -m @P@ backtest || python -m @P@ @F@
B (python -m @P@ @F@)
B powershell -EncodedCommand cAB5AHQAaABvAG4AIAAtAG0AIABNAGEAaQBuAFMAdABvAGkAbgBrAGUAcgAgAGYAZQB0AGMAaAAtAGIAYQByAHMA
B python -m @P@ ba@ZW@cktest
B python -m Main@ZW@Stoinker @F@
"""  # noqa: E501

# Rows the reviewer listed as holes that are not (or cannot be) blocked by text:
#  * a bare line break after the module runs two commands, the second is not a
#    program (`python -m MainStoinker`, then `fetch-bars`): not a hole;
#  * `rg --pre ./fb.sh x` runs a script that this command does not name.
ADV_NOT_BLOCKED = {"python -m @P@@N@@F@", "rg --pre ./fb.sh x"}

# Commands that must stay allowed (the reviewer's false-positive list). The last
# six are blocked ON PURPOSE or accepted, see FP_STILL_BLOCKED.
FP_TABLE = r"""
A uv pip install --python .venv@BS@Scripts@BS@python.exe -r requirements-dev.txt
A uv pip install --python .venv@BS@Scripts@BS@python.exe -e .
A .venv@BS@Scripts@BS@python.exe -m pytest
A .venv@BS@Scripts@BS@python.exe -m pytest -m slow
A .venv@BS@Scripts@BS@ruff.exe check .
A .venv@BS@Scripts@BS@ruff.exe check tests @P@/engine @P@/__main__.py --select E,F,W,I,B
A ruff check tests conftest.py @P@/engine @P@/api @P@/__main__.py @P@/ib --select E,F,W,I,B
A .venv@BS@Scripts@BS@python.exe -m @P@ backtest --algos ALGOS.json --data OfflineData --out OUTDIR
A .venv@BS@Scripts@BS@python.exe -m @P@ backtest --algos ALGOS.json --data OfflineData --out OUTDIR --db runs.db --log
A .venv@BS@Scripts@BS@python.exe -m @P@ runs --db DB --json
A .venv@BS@Scripts@BS@python.exe -m @P@ runs --db DB
A .venv@BS@Scripts@BS@python.exe -m @P@ report RUN --db DB --json
A .venv@BS@Scripts@BS@python.exe -m @P@ report 3 --db "C:@BS@x y@BS@runs.db"
A .venv@BS@Scripts@BS@python.exe -m @P@ serve --port 8750
A .venv@BS@Scripts@BS@python.exe -m @P@ backtest --help
A python -m @P@ backtest --algos a.json --data OfflineData --out out 2>&1 | tail -20
A RUN=$(python -m @P@ runs --db x.db --json | python -c "import json,sys; print(json.load(sys.stdin)[0][@Q@id@Q@])")
A python -m @P@ report $RUN --db x.db
A python -m pytest tests/test_ib_cli.py
A python -m pytest tests/test_ib_cli.py -k "@F@ or @R@"
A python -m pytest tests/test_ib_cli.py -k "test_@F@_refuses_without_tty"
A python -m pytest tests/test_ib_cli.py::test_fetch_bars_help -q
A python -m pytest tests/test_ib_subprocess_block.py
A python -m pytest tests/test_ib_subprocess_block.py tests/test_ib_connection.py --cov=@P@.ib
A python -m pytest tests/test_ib_subprocess_block.py @P@/testy
A python -m ruff check @P@/ib tests/test_ib_subprocess_block.py --select E,F,W,I,B
A ruff check @P@/ib tests/test_ib_subprocess_block.py
A python -m pytest tests -k "subprocess and not slow" --cov=@P@
A python -m pytest -p no:cacheprovider tests/test_engine_cli.py -k "@P@ and run"
A python -m pytest tests/test_engine_cli.py -k "runs or report" --durations=5
A python -m pytest -n 6 -x tests/test_ib_fetch.py
A git add @P@/ib/cli.py @P@/__main__.py tests/test_ib_cli.py
A git diff revival...HEAD -- @P@/ib @P@/__main__.py
A git log --oneline -- @P@/ib/connection.py
A git show origin/task/125:@P@/ib/cli.py
A git commit -m "T13: add @F@ (python -m @P@ @F@)"
A git commit -m "Dispatch @F@ to @P@.ib.cli; run with python -m @P@ @F@ --port 7497"
A gh pr create --title "T13" --body "Run: python -m @P@ @F@ --symbols AAPL"
A gh pr comment 133 --body-file review.md
A gh issue view 125 --json body
A grep -n "@F@" @P@/__main__.py
A grep -rn "subprocess" @P@/ib
A rg -n "importlib|runpy" @P@
A rg -n "def main" @P@/ib/cli.py
A sed -n 1,80p @P@/ib/cli.py
A sed -n "/@F@/,/probe/p" @P@/__main__.py
A cat @P@/ib/cli.py
A ls @P@/ib
A head -40 @P@/__main__.py
A awk "/subprocess/" @P@/ib/connection.py
A python -c "import @P@.engine.cli as c; print(c.build_parser().format_help())"
A python -c "import @P@; print(@P@.__file__)"
A python -c "import importlib.metadata as m; print(m.version(@Q@numpy@Q@))" && ls @P@
A python tools/count_lines.py @P@/ib
A python -m compileall -q @P@/ib
A python -m py_compile @P@/ib/cli.py
A python -m mypy @P@/ib
A python -m pyright @P@/ib/cli.py
A python -m pytest --pdb tests/test_ib_cli.py
A echo "don@Q@t run python -m @P@ @F@"
A Get-Content @P@/ib/cli.py | Select-String "@F@"
A Select-String -Path @P@/__main__.py -Pattern "@R@"
A python -m @P@ @F@ --help
A python -m @P@ @R@ -h
A python -m @P@.ib.cli --help
A python -c "import @P@.ib.connection"
"""  # noqa: E501

# Still blocked. By the owner's ruling (they only print help, but a command
# that can connect is judged by its name, not by its flags): the last four.
# Accepted false positives of a command-text hook: the first two.
FP_STILL_BLOCKED = {
    "python tools/count_lines.py @P@/ib",
    'echo "don@Q@t run python -m @P@ @F@"',
    "python -m @P@ @F@ --help",
    "python -m @P@ @R@ -h",
    "python -m @P@.ib.cli --help",
    'python -c "import @P@.ib.connection"',
}

CONT_TABLE = r"""
A python -m @P@ @BS@@N@  backtest --algos a.json --data OfflineData --out out
A .venv/Scripts/python.exe -m @P@ backtest @BS@@N@  --algos a.json @BS@@N@  --data OfflineData --out out
A python -m @P@ @BT@@N@  backtest --algos a.json --data OfflineData --out out
B python -m @P@ @BT@@N@  @F@ --symbols AAPL MSFT @BT@@N@  --start 2024-01-01
B & .venv@BS@Scripts@BS@python.exe -m @P@ @BT@@N@    @F@ @BT@@N@    --symbols AAPL
B python -m @P@ @BS@@N@@F@
B python -m @P@ @BS@
B python -m @P@ ^@N@@F@
B cmd /c python -m @P@ ^^ @F@
B python -m @P@ ``@F@
B python -m @P@ backtest@N@python -m @P@ @F@
B python -m @P@ --json @F@
B python -m @P@ -h @F@
B python -m @P@ --help @F@
"""


@pytest.mark.parametrize(("verdict", "raw"), _table(ADV_TABLE))
def test_reviewer_adversarial_table(verdict: str, raw: str) -> None:
    command = _x(raw)
    expect_blocked = verdict == "B"
    assert hook.decide(command)[0] is expect_blocked, command
    if raw in ADV_NOT_BLOCKED:
        assert verdict == "A"


@pytest.mark.parametrize(("verdict", "raw"), _table(FP_TABLE))
def test_reviewer_false_positive_table(verdict: str, raw: str) -> None:
    command = _x(raw)
    expect_blocked = raw in FP_STILL_BLOCKED
    assert hook.decide(command)[0] is expect_blocked, command


@pytest.mark.parametrize(("verdict", "raw"), _table(CONT_TABLE))
def test_reviewer_continuation_table(verdict: str, raw: str) -> None:
    command = _x(raw)
    assert hook.decide(command)[0] is (verdict == "B"), command


def test_claude_md_commands_are_allowed() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = text.split("## Commands", 1)[1]
    block = section.split("```")[1]
    commands = []
    for line in block.splitlines():
        line = line.split("  #", 1)[0].strip()
        if line:
            commands.append(line.replace("[", "").replace("]", ""))
    assert len(commands) >= 9
    assert any("-m MainStoinker backtest" in c for c in commands)
    for command in commands:
        assert hook.decide(command) == (False, ""), command


# 1. continuation lines ------------------------------------------------------------


def test_powershell_and_cmd_continuations_do_not_hide_the_subcommand() -> None:
    for command in (
        "python -m MainStoinker `\n  fetch-bars --symbols AAPL MSFT `\n  --start 2024-01-01",
        "python -m MainStoinker `\r\n  fetch-bars `\r\n  --symbols AAPL",
        "& .venv\\Scripts\\python.exe -m MainStoinker `\n    probe-data `\n    --symbols AAPL",
        "python -m MainStoinker \\\n  fetch-bars --symbol AAPL",
        "python -m MainStoinker \\  \n  fetch-bars",
        "python -m MainStoinker ^\nfetch-bars",
        "cmd /c \"python -m MainStoinker ^\nfetch-bars\"",
        "python -m MainStoinker `\n`\nfetch-bars",
    ):
        assert hook.decide(command)[0] is True, command


def test_bash_multiline_offline_command_is_allowed_again() -> None:
    for command in (
        "python -m MainStoinker \\\n  backtest --algos a.json --data OfflineData --out out",
        "python -m MainStoinker \\\r\n  backtest --algos a.json \\\r\n  --data OfflineData",
        ".venv/Scripts/python.exe -m MainStoinker backtest \\\n  --algos a.json \\\n  --out out",
        "python -m MainStoinker `\n  report 3 --db x.db",
        "python -m MainStoinker ^\nruns --db x.db",
    ):
        assert hook.decide(command) == (False, ""), command


def test_continuations_make_the_older_rules_stricter_only() -> None:
    # The entry script and launcher rules also see the joined text: a run split
    # over lines by a continuation is blocked (it was not before).
    for command in (
        f'python \\\n  "{ENTRY}"',
        f'python `\n  "{ENTRY}"',
        f'cmd /c ^\n"{BAT}"',
        f'call `\n"{BAT}"',
    ):
        assert hook.decide(command)[0] is True, command
    # and they still allow what they allowed
    assert hook.decide(f'cat \\\n "{ENTRY}"') == (False, "")
    assert hook.decide(f'Get-Content `\n "{ENTRY}"') == (False, "")


# 2. PowerShell argument arrays and similar ---------------------------------------


def test_powershell_argument_arrays_are_tokenised() -> None:
    for command in (
        "Start-Process python -ArgumentList '-m','MainStoinker','fetch-bars'",
        "Start-Process python -ArgumentList '-m', 'MainStoinker', 'fetch-bars'",
        'Start-Process -FilePath python -ArgumentList @("-m","MainStoinker","fetch-bars") -Wait',
        '& python @("-m","MainStoinker","fetch-bars")',
        '$a = @("-m","MainStoinker","fetch-bars"); & python @a',
        '$a = @("-m","MainStoinker","fetch-bars")\n& python @a',
        "$args2 = '-m','MainStoinker','probe-data'; & $py @args2",
        '$a = @("-m","MainStoinker","run"); Start-Process python -ArgumentList $a',
        '[string[]]$a = @("-m","MainStoinker","fetch-bars")',
        '& python @("-m", "MainStoinker", @("fetch-bars"))',
        "& python {-m,MainStoinker,fetch-bars}",
    ):
        assert hook.decide(command)[0] is True, command
    for command in (
        '$a = @("-m","MainStoinker","backtest","--algos","a.json"); & python @a',
        "Start-Process python -ArgumentList '-m','MainStoinker','report','3'",
        '& python @("-m","MainStoinker","runs","--db","x.db")',
    ):
        assert hook.decide(command) == (False, ""), command


# 3. other -m modules do not switch the script check off --------------------------


def test_other_modules_do_not_switch_off_the_script_path_check() -> None:
    for tool in ("pdb", "cProfile", "debugpy --listen 5678", "trace --count", "coverage run", "runpy"):
        for target in ("MainStoinker/__main__.py fetch-bars", "MainStoinker/ib/cli.py"):
            command = f"python -m {tool} {target}"
            assert hook.decide(command)[0] is True, command
    for command in (
        "python -m cProfile -o x.prof MainStoinker/__main__.py fetch-bars",
        "coverage run MainStoinker/__main__.py fetch-bars",
        "coverage run --branch MainStoinker/__main__.py fetch-bars",
        "python -m coverage run MainStoinker/ib/cli.py",
        "python -m runpy MainStoinker fetch-bars",
        "python -m pdb MainStoinker/__main__.py run",
        "python -X dev -m trace -t MainStoinker/__main__.py probe-data",
    ):
        assert hook.decide(command)[0] is True, command


def test_lint_and_test_runners_may_name_package_paths() -> None:
    for command in (
        "python -m pytest MainStoinker/__main__.py",
        "python -m pytest MainStoinker/ib tests/test_ib_cli.py",
        "python -m ruff check MainStoinker/__main__.py MainStoinker/ib",
        ".venv\\Scripts\\ruff.exe check tests MainStoinker/engine MainStoinker/__main__.py",
        "python -m mypy MainStoinker/ib/cli.py",
        "python -m pyright MainStoinker/ib/cli.py",
        "python -m black --check MainStoinker/ib",
        "python -m isort MainStoinker/ib/cli.py",
        "python -m flake8 MainStoinker/ib",
        "python -m pylint MainStoinker/ib/cli.py",
        "python -m compileall MainStoinker/ib",
        "python -m py_compile MainStoinker/ib/cli.py",
        "python -m coverage report",
        "python -m coverage html",
        "python -m coverage xml",
        "python -m coverage combine",
        "python -m coverage erase",
        "coverage report --include=MainStoinker/ib/*",
        "coverage html",
        "coverage run -m pytest tests/test_ib_cli.py",
    ):
        assert hook.decide(command) == (False, ""), command
    assert "coverage" not in hook._HARMLESS_TOOLS
    assert hook._COVERAGE_READ == {"report", "html", "xml", "combine", "erase"}


# 4. the error path fails closed for the package ----------------------------------


def test_error_path_fails_closed_when_the_request_names_the_package(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = "python -m MainStoinker fetch-bars"
    # stdin that is not valid JSON
    assert hook.main("{not json " + cmd) == 2
    assert "MainStoinker" in capsys.readouterr().err
    assert hook.main("{not json " + cmd.upper()) == 2
    capsys.readouterr()
    # no tool_input
    assert hook.main(json.dumps({"tool_name": "Bash", "cmd": cmd})) == 2
    # command that is not a string
    assert hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": [cmd]}})) == 2
    assert hook.main(json.dumps({"tool_name": "PowerShell", "tool_input": {"command": [cmd]}})) == 2
    capsys.readouterr()

    # an internal exception anywhere
    def boom(text: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_strip_message_args", boom)
    for tool in ("Bash", "PowerShell"):
        assert hook.main(_payload(cmd, tool)) == 2
    capsys.readouterr()
    # ... which does not block a request that does not name the package
    assert hook.main(_payload("git status")) == 0
    assert hook.main("{not json git status") == 0
    capsys.readouterr()


def test_run_stdin_fails_closed_for_the_package() -> None:
    raw = b"{not json python -m MainStoinker fetch-bars"
    assert hook.run_stdin(io.BytesIO(raw)) == 2

    class Broken(io.BytesIO):
        def read(self, *args: object) -> bytes:
            raise OSError("broken pipe")

    assert hook.run_stdin(Broken(raw)) == 0  # nothing was read, nothing to judge


# 5. running from inside the package, path variants -------------------------------


def test_runs_from_inside_the_package_directory_are_judged() -> None:
    for command in (
        "cd MainStoinker && python __main__.py fetch-bars",
        "cd MainStoinker; python . fetch-bars",
        "cd MainStoinker && python ib/cli.py",
        "cd MainStoinker\npython __main__.py probe-data",
        "Set-Location .\\MainStoinker; python __main__.py fetch-bars",
        "Set-Location -Path .\\MainStoinker\n& python . fetch-bars",
        "pushd MainStoinker && python tool.py fetch-bars",
        "pushd MainStoinker\\ib && python cli.py",
        "cd MainStoinker/ib && python anything.py",
        "cd MainStoinker && cd ib && python cli.py",
        'cd "C:\\Users\\x y\\Stonks\\MainStoinker" && python __main__.py run',
        "cd /d C:\\x\\MainStoinker && python __main__.py fetch-bars",
        "cd MainStoinker && uv run __main__.py fetch-bars",
        "cd MainStoinker && python -m ib.cli",
        "cd MainStoinker && python -m engine.cli fetch-bars",
        'bash -c "cd MainStoinker" ; python ib/cli.py',
        "git status && cd MainStoinker && python . fetch-bars",
        "python __main__.py fetch-bars",
        "python . fetch-bars",
        "python -X dev ./__main__.py probe-data",
    ):
        assert hook.decide(command)[0] is True, command
    for command in (
        "cd MainStoinker && python __main__.py backtest --algos a.json",
        "cd MainStoinker && python . serve",
        "cd MainStoinker; python __main__.py",
        "cd MainStoinker; python .",
        "cd MainStoinker && python -m pytest tests",
        "cd MainStoinker && python tool.py",
        "cd MainStoinker/engine && python -m pytest",
        "cd MainStoinker && ls && git status",
        "python tools/build.py . release",
        "python -m pytest .",
        "python -m ruff check .",
        "cd tests && python helper.py fetch-bars",
    ):
        assert hook.decide(command) == (False, ""), command


def test_direct_runs_without_an_interpreter_and_path_spellings() -> None:
    for command in (
        ".\\MainStoinker\\__main__.py fetch-bars",
        "./MainStoinker/__main__.py fetch-bars",
        "MainStoinker/__main__.py fetch-bars",
        '& ".\\MainStoinker\\__main__.py" fetch-bars',
        "& '.\\MainStoinker\\__main__.py' probe-data",
        "python MainStoinker/./__main__.py fetch-bars",
        "python MainStoinker//__main__.py fetch-bars",
        "python ./MainStoinker/./__main__.py fetch-bars",
        "python ./MainStoinker/. fetch-bars",
        "python .\\MainStoinker\\.\\ fetch-bars",
        "python MainStoinker/ib//cli.py",
        "python MainStoinker/./ib/cli.py",
        "start .\\MainStoinker\\__main__.py fetch-bars",
        "coverage run MainStoinker/__main__.py fetch-bars",
    ):
        assert hook.decide(command)[0] is True, command
    for command in (
        ".\\MainStoinker\\__main__.py backtest --algos a.json",
        "MainStoinker/__main__.py",
        "python MainStoinker/./__main__.py backtest",
        "python ./MainStoinker/. report 3",
        "cat MainStoinker/./__main__.py",
        "Get-Content .\\MainStoinker\\__main__.py",
    ):
        assert hook.decide(command) == (False, ""), command


# 6. transparent prefixes, xargs, text piped into a shell -------------------------

_PREFIXES = [
    "xargs",
    "env",
    "env -i PATH=x",
    "nohup",
    "timeout 60",
    "time",
    "start",
    'start ""',
    "call",
    "conda run -n base",
    "poetry run",
    "hatch run",
    "wsl",
]


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_transparent_prefixes_are_judged_through(prefix: str) -> None:
    for sub in CONNECTING_SUBCOMMANDS:
        for command in (
            f"{prefix} python -m MainStoinker {sub}",
            f"echo x | {prefix} python -m MainStoinker {sub}",
            f"{prefix} python MainStoinker/__main__.py {sub}",
            f"{prefix} bash -c 'python -m MainStoinker {sub}'",
        ):
            assert hook.decide(command)[0] is True, command
    if prefix != "xargs":
        assert hook.decide(f"{prefix} python -m MainStoinker backtest") == (False, "")


def test_xargs_and_foreach_feeding_the_package_is_blocked_whatever_the_arguments() -> None:
    for command in (
        "echo fetch-bars | xargs python -m MainStoinker",
        "echo backtest | xargs python -m MainStoinker",
        "echo x | xargs -I{} python -m MainStoinker backtest {}",
        "ls | xargs -n1 python MainStoinker/__main__.py",
        "echo x | xargs -I{} python -m MainStoinker report {} --db x.db",
        "'a','b' | ForEach-Object { python -m MainStoinker backtest $_ }",
        "1..3 | % { & python -m MainStoinker report $_ }",
        "Get-Content subs.txt | foreach { python -m MainStoinker $_ }",
        "echo x | xargs python -m MainStoinker.ib.cli",
        "echo x | xargs echo MainStoinker | xargs python -m MainStoinker",
    ):
        assert hook.decide(command)[0] is True, command
    for command in (
        "echo x | xargs echo done",
        "git ls-files | xargs grep -n MainStoinker",
        "git ls-files MainStoinker | xargs wc -l",
        "'a','b' | ForEach-Object { Write-Output $_ }",
    ):
        assert hook.decide(command) == (False, ""), command


@pytest.mark.parametrize(
    "shell", ["bash", "sh", "zsh", "iex", "Invoke-Expression", "cmd", "pwsh", "powershell", "python -"]
)
def test_text_piped_into_a_shell_is_judged(shell: str) -> None:
    for text in (
        "python -m MainStoinker fetch-bars",
        "python -m MainStoinker probe-data --symbols AAPL",
        "python -m MainStoinker.ib.cli",
        "python MainStoinker/ib/cli.py",
        "python -m MainStoinker run",
    ):
        assert hook.decide(f'echo "{text}" | {shell}')[0] is True, (shell, text)
        assert hook.decide(f'printf "{text}" | {shell}')[0] is True, (shell, text)
        assert hook.decide(f'Write-Output "{text}" | {shell}')[0] is True, (shell, text)
    assert hook.decide(f'echo "import MainStoinker.ib.cli" | {shell}')[0] is True
    # offline text, or text that does not name the package, may be piped
    assert hook.decide(f'echo "python -m MainStoinker backtest" | {shell}') == (False, "")
    assert hook.decide(f'echo "python -m pytest" | {shell}') == (False, "")
    # the same text piped to something that does not run it
    assert hook.decide('echo "python -m MainStoinker fetch-bars" | tee notes.txt') == (False, "")
    assert hook.decide('echo "python -m MainStoinker fetch-bars" | wc -c') == (False, "")


def test_text_written_into_a_script_file_is_judged() -> None:
    for command in (
        'echo "python -m MainStoinker fetch-bars" > run.sh',
        'printf "python -m MainStoinker probe-data" >> go.ps1',
        'echo "import MainStoinker.ib.cli" > x.py',
        'Write-Output "python -m MainStoinker fetch-bars" > run.bat',
    ):
        assert hook.decide(command)[0] is True, command
    for command in (
        'echo "python -m MainStoinker fetch-bars" > notes.txt',
        'echo "python -m MainStoinker backtest" > run.sh',
        'echo "see MainStoinker" > x.py',
    ):
        assert hook.decide(command) == (False, ""), command


def test_git_alias_that_runs_a_command_is_judged() -> None:
    assert hook.decide('git config alias.fb "!python -m MainStoinker fetch-bars"')[0]
    assert hook.decide("git config --global alias.fb '!python -m MainStoinker run'")[0]
    assert hook.decide('git config user.name "MainStoinker bot"') == (False, "")
    assert hook.decide("git log --oneline -- MainStoinker/ib") == (False, "")


# 7. PowerShell -EncodedCommand ---------------------------------------------------


def _enc(text: str) -> str:
    return base64.b64encode(text.encode("utf-16-le")).decode("ascii")


@pytest.mark.parametrize("flag", ["-EncodedCommand", "-encodedcommand", "-enc", "-ec", "-e", "-EncodedC"])
@pytest.mark.parametrize("shell", ["powershell", "pwsh", "powershell.exe -NoProfile"])
def test_encoded_command_is_decoded_and_judged(shell: str, flag: str) -> None:
    for text in (
        "python -m MainStoinker fetch-bars",
        "python -m MainStoinker.ib.cli",
        "& python MainStoinker/ib/cli.py",
        f'python "{ENTRY}"',
        'bash -c "python -m MainStoinker probe-data"',
    ):
        assert hook.decide(f"{shell} {flag} {_enc(text)}")[0] is True, (shell, flag, text)
        assert hook.decide(f'{shell} {flag} "{_enc(text)}"')[0] is True
        assert hook.decide(f"{shell} -NoProfile {flag} {_enc(text)} -NonInteractive")[0] is True
    for text in ("python -m MainStoinker backtest --algos a.json", "Get-Date", "python -m pytest"):
        assert hook.decide(f"{shell} {flag} {_enc(text)}") == (False, ""), (shell, flag, text)


def test_undecodable_encoded_command_next_to_the_package_is_blocked() -> None:
    assert hook.decide("powershell -EncodedCommand !!notbase64!! ; echo MainStoinker")[0] is True
    assert hook.decide("powershell -enc QQ ; python -m MainStoinker backtest")[0] is True
    odd = base64.b64encode(b"abc").decode("ascii")  # odd number of bytes: not UTF-16
    assert hook.decide(f"powershell -enc {odd} MainStoinker")[0] is True
    # no mention of the package: nothing to judge
    assert hook.decide("powershell -enc !!notbase64!!") == (False, "")
    # an option that merely starts with -e is not an encoded command
    assert hook.decide("powershell -ExecutionPolicy Bypass -File x.ps1 MainStoinker") == (False, "")
    assert hook.decide("grep -e MainStoinker README.md") == (False, "")
    assert hook.decide("rg -e MainStoinker docs") == (False, "")


# 8. marker words count only in code context ---------------------------------------


def test_marker_words_in_file_names_and_selectors_are_not_code() -> None:
    for command in (
        "python -m pytest tests/test_ib_subprocess_block.py --cov=MainStoinker",
        "python -m pytest tests/test_ib_subprocess_block.py tests/test_ib_cli.py --cov=MainStoinker.ib",
        "python -m ruff check tests/test_ib_subprocess_block.py MainStoinker/ib",
        "python -m ruff check MainStoinker/ib tests/test_ib_subprocess_block.py --select E,F,W,I,B",
        'python -m pytest -k "subprocess and fetch_bars"',
        'python -m pytest tests -k "subprocess and not slow" --cov=MainStoinker',
        'python -m pytest -k "importlib or runpy or exec or eval" MainStoinker/testy',
        "python -m pytest tests/test_runpy_helpers.py tests/test_importlib_shim.py --cov=MainStoinker",
        "python -m mypy tests/test_os_system_guard.py MainStoinker/ib",
        "python -m pytest tests/test_eval_exec.py MainStoinker/engine",
    ):
        assert hook.decide(command) == (False, ""), command


def test_marker_words_still_count_in_code_context() -> None:
    for command in (
        "python -c \"import subprocess; subprocess.run(['python','-m','MainStoinker','fetch-bars'])\"",
        "python -c \"import runpy; runpy.run_module('MainStoinker')\"",
        "python -c \"import importlib; importlib.import_module('MainStoinker.__main__')\"",
        "python -c \"exec(open('MainStoinker/__main__.py').read())\"",
        "python -c \"eval(compile(open('MainStoinker/__main__.py').read(), 'x', 'exec'))\"",
        "python -c \"import os; os.system('python -m MainStoinker fetch-bars')\"",
        "python -c \"import os; os.popen('python -m MainStoinker run')\"",
        "python -c \"__import__('MainStoinker.__main__')\"",
        "python -c \"import subprocess\" MainStoinker",
        "python - <<'EOF'\nimport subprocess\nsubprocess.run(['MainStoinker'])\nEOF",
        "python - <<'EOF'\nimport runpy\nx = 'MainStoinker'\nrunpy.run_module(x)\nEOF",
        "python <<EOF\nimport importlib\nimportlib.import_module('MainStoinker.' + 'ib')\nEOF",
        "bash <<'EOF'\necho MainStoinker.ib\npython -m MainStoinker.ib.cli\nEOF",
        "python -c 'import runpy' -m MainStoinker run",
    ):
        assert hook.decide(command)[0] is True, command


# 9. no quadratic behaviour --------------------------------------------------------


def _timed(command: str) -> tuple[bool, float]:
    start = time.perf_counter()
    blocked = hook.decide(command)[0]
    return blocked, time.perf_counter() - start


_TIMING_BOUND = 3.0  # the real figures are a few hundredths of a second


def test_decisions_are_fast_on_the_reviewers_timing_shapes() -> None:
    p = "MainStoinker"
    shapes = {
        "harmless_5k": "python " + "x " * 5_000 + p,
        "harmless_20k": "python " + "x " * 20_000 + p,
        "paren_text_5k": "echo " + "(" * 5_000 + " " + p,
        "paren_text_20k": "echo " + "(" * 20_000 + " " + p,
        "from_import": "python -c " + ("from " + p + " import " + "a," * 50) * 2_000,
        "dir_token": "python " + "a/" * 50_000 + p,
        "segments": ("python -m " + p + " backtest; ") * 20_000,
    }
    for name, command in shapes.items():
        _, took = _timed(command)
        assert took < _TIMING_BOUND, (name, took)


def test_decisions_are_fast_on_other_long_shapes() -> None:
    p = "mainstoinker"
    shapes = {
        "module_flags": "python " + "-mmainstoinker " * 3_000,
        "module_flags_ok": "python -m mainstoinker backtest " + "-mmainstoinker " * 3_000,
        "nested_bash": "bash -c " * 6_000 + p,
        "from_imports": "python -c " + "from mainstoinker import a," * 1_500,
        "parens": "python " + "(" * 40_000 + " " + p,
        "heredocs": ("python <<EOF\n" * 3_000) + p,
        "pipes": "echo mainstoinker " + "| cat " * 8_000,
        "cds": ("cd mainstoinker; " * 2_500) + "python . x",
        "continuations": ("python \\\n" * 5_000) + p,
        "backslash_spaces": "\\" + " " * 40_000 + p,
        "encoded": "powershell " + "-e abcd " * 5_000 + p,
        "no_mention_words": "x " * 100_000,
        "no_mention_parens": "(" * 200_000,
        "no_mention_git": "git commit -m x; " * 12_000,
        "no_mention_heredocs": "cat <<EOF\n" * 20_000,
        "no_mention_dots": ". " * 100_000,
        "no_mention_python_dots": "python . " * 20_000,
        "no_mention_quotes": "'" * 200_000,
        "no_mention_continuations": "\\\n" * 100_000,
        "no_mention_strings": '"a" ' * 50_000,
    }
    for name, command in shapes.items():
        _, took = _timed(command)
        assert took < _TIMING_BOUND, (name, took)


def test_long_command_that_mentions_the_package_is_blocked_without_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*args: object) -> None:
        raise AssertionError("a command over the limit must not be analysed")

    monkeypatch.setattr(hook, "_judge_connecting", must_not_run)
    monkeypatch.setattr(hook, "_strip_message_args", must_not_run)
    long_text = "python -m MainStoinker backtest " + "x " * 30_000
    assert len(long_text) > 50_000
    blocked, reason = hook.decide(long_text)
    assert blocked is True
    assert "owner" in reason
    assert hook.decide("MAINSTOINKER " + "y" * 60_000)[0] is True
    assert hook.decide("Main'Stoinker' " + "y" * 60_000)[0] is True
    monkeypatch.undo()
    # a long command that does not mention the package is analysed as usual
    assert hook.decide("echo " + "y " * 100_000) == (False, "")
    # at the limit the command is analysed (and this one is allowed)
    at_limit = "python -m MainStoinker backtest " + "x" * (50_000 - 32)
    assert len(at_limit) == 50_000
    assert hook.decide(at_limit) == (False, "")


# 10. the rule is pinned -----------------------------------------------------------


def test_offline_allowlist_is_exactly_these_four_words() -> None:
    assert hook._OFFLINE_SUBCOMMANDS == frozenset({"backtest", "runs", "report", "serve"})
    for word in ("paper", "live", "trade", "run", "fetch-bars", "probe-data", "stage2"):
        assert hook.decide(f"python -m MainStoinker {word}")[0] is True, word
        assert hook.decide(f"python MainStoinker/__main__.py {word} --x")[0] is True, word
        assert hook.decide(f"python -m MainStoinker.engine.cli {word}")[0] is True, word
    for word in sorted(hook._OFFLINE_SUBCOMMANDS):
        assert hook.decide(f"python -m MainStoinker {word}") == (False, "")


def test_adding_a_word_to_the_allowlist_is_visible_to_these_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widened = frozenset({"backtest", "runs", "report", "serve", "paper"})
    monkeypatch.setattr(hook, "_OFFLINE_SUBCOMMANDS", widened)
    # the pin above would fail, and the behaviour it protects has changed
    assert hook._OFFLINE_SUBCOMMANDS != frozenset({"backtest", "runs", "report", "serve"})
    assert hook.decide("python -m MainStoinker paper") == (False, "")
    monkeypatch.setattr(hook, "_OFFLINE_SUBCOMMANDS", frozenset({"backtest", "runs", "report"}))
    assert hook.decide("python -m MainStoinker serve")[0] is True


def test_shell_wrapper_unwrapping_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # Text inside a wrapper is judged as the command it is: an echo is text...
    text_in_wrapper = (
        'bash -c "echo python -m MainStoinker fetch-bars"',
        "sh -c 'echo run python -m MainStoinker probe-data'",
        'cmd /c "echo python -m MainStoinker fetch-bars"',
        'powershell -Command "Write-Output \'python -m MainStoinker run\'"',
    )
    # ... and a cd inside a wrapper still counts for the commands after it.
    cd_in_wrapper = (
        'bash -c "cd MainStoinker" ; python ib/cli.py',
        'cmd /c "cd MainStoinker" && python ib/cli.py',
        'powershell -Command "Set-Location MainStoinker"; python tool.py fetch-bars',
    )
    for command in text_in_wrapper:
        assert hook.decide(command) == (False, ""), command
    for command in cd_in_wrapper:
        assert hook.decide(command)[0] is True, command
    assert hook._connecting_reason("bash -c echo python -m mainstoinker fetch-bars") is None
    # With the unwrapping switched off every one of these verdicts flips.
    monkeypatch.setattr(hook, "_SHELL_WRAPPERS", frozenset())
    for command in text_in_wrapper:
        assert hook.decide(command)[0] is True, command
    for command in cd_in_wrapper:
        assert hook.decide(command) == (False, ""), command


def test_wrapped_connecting_commands_stay_blocked_at_any_depth() -> None:
    for command in (
        'bash -c "bash -c \'bash -c \\"python -m MainStoinker fetch-bars\\"\'"',
        'cmd /c powershell -Command "bash -c \'python -m MainStoinker run\'"',
        "bash -c " * 20 + "'python -m MainStoinker fetch-bars'",
        "sh -c " * 8 + "python -m MainStoinker backtest",
    ):
        assert hook.decide(command)[0] is True, command


# 11. module allowlist -------------------------------------------------------------


def test_only_known_offline_modules_may_run_with_m() -> None:
    assert hook._PACKAGE_MODULES == frozenset(
        {
            "mainstoinker",
            "mainstoinker.__main__",
            "mainstoinker.engine.cli",
            "mainstoinker.engine.__main__",
        }
    )
    # No other module is allowed: `serve` runs as `python -m MainStoinker serve`.
    assert not hasattr(hook, "_OTHER_OFFLINE_MODULES")
    for module in (
        "MainStoinker.engine.live",
        "MainStoinker.engine.paper",
        "MainStoinker.engine.broker",
        "MainStoinker.api",
        "MainStoinker.api.live",
        "MainStoinker.api.serve.extra",
        "MainStoinker.api.serve",
        "MainStoinker.engine.sim_broker",
        "MainStoinker.live",
        "MainStoinker.MainStuff.main_utils",
        "MainStoinker.DataCollection.apiApi",
        "MainStoinker.TradeTools.trade",
        "MainStoinker.Util.IBKRHelper",
        "MainStoinker.Algos.Elliot.Elliot_Algo",
        "MainStoinker.ibx",
        "MainStoinker.",
        "mainstoinker.something_new",
    ):
        for command in (
            f"python -m {module}",
            f"python -m {module} backtest",
            f"python -m {module} --help",
            f"python -m{module}",
            f"uv run --module {module}",
            f"VAR=1 python -m {module.upper()}",
        ):
            assert hook.decide(command)[0] is True, command
    for command in (
        "python -m MainStoinker",
        "python -m MainStoinker.__main__ backtest",
        "python -m MainStoinker.engine.cli report 3",
        "python -m MainStoinker serve --db runs.db --port 8750",
    ):
        assert hook.decide(command) == (False, ""), command


# --- nothing blocked by revival is allowed now -------------------------------------


BASELINE = REPO_ROOT / "tests" / "fixtures" / "hook_baseline" / "block_live_launch_revival.py.txt"
# sha256 of the frozen copy of revival's hook (line endings as LF). Changing the
# fixture means changing this value in review, so it cannot be edited quietly.
BASELINE_SHA256 = "76dcde8ff9e8b74cb69879f8d82c12c9443d4cc3202df78729aec253ecdd86aa"


def _load_revival_hook() -> types.ModuleType:
    """The frozen copy of revival's hook; never skips (no git, no network)."""
    data = BASELINE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == BASELINE_SHA256, (
        "the frozen baseline hook was edited"
    )
    module = types.ModuleType("revival_block_live_launch")
    module.__file__ = str(BASELINE)
    exec(compile(data.decode("utf-8"), module.__file__, "exec"), module.__dict__)
    return module


def _generated_forms() -> set[str]:
    def f(s: str) -> str:
        return s.replace("~", "\\").replace("^", '"')

    entry_win = ENTRY.replace("/", "\\")
    interpreters = [
        f(x)
        for x in (
            "python", "python3", "py", "py -3.14", "pythonw", "PYTHON.EXE",
            ".venv~Scripts~python.exe", "^.venv~Scripts~python.exe^",
            "& ^.venv/Scripts/python.exe^", "uv run", "uv run python", "pipx run",
            "python -X dev", "python -u", "VAR=1 python", "nohup python",
            "env python", "python -m debugpy --listen 5678",
        )
    ]  # fmt: skip
    paths = [
        f("^") + ENTRY + f("^"),
        f("^") + entry_win + f("^"),
        "'" + ENTRY + "'",
        ENTRY.replace(" ", f("~ ")),
        f("^") + ENTRY.upper() + f("^"),
        f("^C:~x y~") + entry_win + f("^"),
    ]
    wraps = [
        f(x)
        for x in (
            "{}", "git status; {}", "git status && {}", "echo hi | {}", "bash -c ^{}^",
            "powershell -Command ^{}^", "cmd /c ^{}^", "(  {} )",
        )
    ]  # fmt: skip
    forms = set()
    for interp, path, wrap in itertools.product(interpreters, paths, wraps):
        inner = interp + " " + path
        if '"' in wrap:
            inner = inner.replace('"', "'")
        forms.add(wrap.format(inner))
    bats = [
        f(x)
        for x in (
            "^" + BAT + "^", ".~^" + BAT + "^", "& ^.~" + BAT + "^",
            "./" + BAT.replace(" ", "~ "), "^C:~x y~" + BAT + "^", "^" + BAT.upper() + "^",
        )
    ]  # fmt: skip
    bat_wraps = [
        f(x)
        for x in (
            "{}", "cmd /c {}", "cmd.exe /c {}", "cmd //c {}", "call {}", "start ^^ {}",
            "Start-Process {}", "Invoke-Item {}", "git status; {}", "git status && cmd /c {}",
            "nohup {}", "timeout 5 {}",
        )
    ]  # fmt: skip
    for bat, wrap in itertools.product(bats, bat_wraps):
        forms.add(wrap.format(bat))
    return forms


def test_nothing_blocked_by_revival_is_allowed_by_this_hook() -> None:
    revival = _load_revival_hook()
    corpus: set[str] = set()
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            corpus.add(node.value)
    for table in (ADV_TABLE, FP_TABLE, CONT_TABLE):
        corpus.update(_x(raw) for _verdict, raw in _table(table))
    corpus.update(BLOCKED)
    corpus.update(_generated_forms())
    assert len(corpus) > 1_000
    blocked_by_revival = [c for c in sorted(corpus) if revival.decide(c)[0]]
    assert len(blocked_by_revival) > 500
    loosened = [c for c in blocked_by_revival if not hook.decide(c)[0]]
    assert loosened == []
    # the new rule only adds blocks
    newly_blocked = [c for c in sorted(corpus) if hook.decide(c)[0] and not revival.decide(c)[0]]
    assert len(newly_blocked) > 100


# --- T10 fix round 3: closures from the second review ---------------------------------
# Every row is a command string used as data. The rows in FIX3_BLOCKED were all
# allowed by the previous head of this PR; the other tables pin verdicts that
# must not change.


def _fw(text: str) -> str:
    """ASCII letters as full-width look-alikes (NFKC turns them back)."""
    return "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in text)


FIX3_BLOCKED = {
    # 1. redirect operators and comments the tokenizer did not follow
    "redirects": [
        "python -m MainStoinker >| backtest fetch-bars",
        "python -m MainStoinker 2>| backtest fetch-bars",
        "python -m MainStoinker >|out.txt backtest fetch-bars",
        "python -m MainStoinker <> backtest fetch-bars",
        "python -m MainStoinker 3<> backtest fetch-bars",
        "python -m MainStoinker >& backtest fetch-bars",
        "python -m MainStoinker 2>& backtest fetch-bars",
        "python -m MainStoinker <<< backtest fetch-bars",
        "python -m MainStoinker << backtest fetch-bars",
        "python -m MainStoinker <<- backtest fetch-bars",
        "python -m MainStoinker <# backtest #> fetch-bars",
        "python -m MainStoinker <# runs #> probe-data",
        "python MainStoinker/__main__.py >| backtest fetch-bars",
        "python MainStoinker/__main__.py <# backtest #> fetch-bars",
        "bash -c 'python -m MainStoinker >| backtest fetch-bars'",
        'powershell -Command "python -m MainStoinker <# backtest #> fetch-bars"',
        "git status && python -m MainStoinker >& backtest probe-data",
    ],
    # 2. an expression in the subcommand position
    "expressions": [
        'python -m MainStoinker ("backtest","fetch-bars")[1]',
        'python -m MainStoinker ("backtest" -replace "backtest","fetch-bars")',
        'python -m MainStoinker @("backtest","fetch-bars")[1]',
        'python -m MainStoinker --json ("runs","probe-data")[1]',
        "python -m MainStoinker {backtest}",
        'iex ("python -m MainStoinker backtest" -replace "backtest","fetch-bars")',
        'iex ("python -m MainStoinker backtest" + (" fetch-bars" -join ""))',
        'python -m MainStoinker report ("x" -join "y") -join "fetch-bars"',
        'python MainStoinker/__main__.py ("backtest","fetch-bars")[1]',
    ],
    # 3. import of the package __main__ or ib without the dotted name
    "code": [
        "python -c \"import sys; sys.argv=['x','fetch-bars']; from MainStoinker import __main__\"",
        "python -c \"import pkgutil; pkgutil.resolve_name('MainStoinker.__main__')\"",
        "python -c \"import sys, pkgutil; sys.argv=['x','fetch-bars']; "
        "pkgutil.resolve_name('MainStoinker.__main__')\"",
        "python -c \"import sys; sys.argv=['x','probe-data']; import MainStoinker\"",
        "python -c \"m=__import__; m('MainStoinker.__main__')\"",
        'echo "from MainStoinker import __main__" | python - fetch-bars',
        "echo \"import sys; sys.argv=['x','fetch-bars']; import MainStoinker\" | python -",
        "python - fetch-bars <<EOF\nfrom MainStoinker import __main__\nEOF",
        "python - <<'EOF'\nimport sys\nsys.argv = ['x', 'fetch-bars']\nimport MainStoinker\nEOF",
        "python <<EOF\nfrom MainStoinker import __main__\nEOF",
        "bash -c 'echo \"from MainStoinker import __main__\" | python -'",
    ],
    # 4. the package name built at run time in the module position
    "module": [
        '$M="MainStoinker"; python -m $M fetch-bars',
        "M=MainStoinker; python -m $M fetch-bars",
        '$m = "MainStoinker"\npython -m $m fetch-bars',
        "export M=MainStoinker && python -m ${M} fetch-bars",
        'cmd /c "set M=MainStoinker&& python -m %M% fetch-bars"',
        'cmd /c "set M=MainStoinker&& setlocal enabledelayedexpansion&& python -m !M! fetch-bars"',
        '$env:M = "MainStoinker"; python -m $env:M fetch-bars',
        "python -m ($M) fetch-bars; echo MainStoinker",
        "python -m$M fetch-bars; echo MainStoinker",
        "python -m MainSto* fetch-bars",
        "python -m Main*er fetch-bars",
        "python -m MainStoinke? fetch-bars",
        "python -m [M]ainStoinker fetch-bars",
        "python -m Main$()Stoinker fetch-bars",
        'python -m "Main$()Stoinker" fetch-bars',
        "python -m Main$()Stoinker.ib.cli",
        "python -X dev -m $M fetch-bars; echo MainStoinker",
        'bash -c "M=MainStoinker; python -m \\$M fetch-bars"',
    ],
    # 5. `..` in a script path
    "dotdot": [
        "python MainStoinker/engine/.. fetch-bars",
        "python MainStoinker/engine/../__main__.py fetch-bars",
        'python "MainStoinker\\engine\\.." fetch-bars',
        "python ./MainStoinker/api/../ fetch-bars",
        "python MainStoinker/engine/../engine/.. fetch-bars",
        "python C:/repo/MainStoinker/engine/.. fetch-bars",
        "MainStoinker/engine/../__main__.py fetch-bars",
    ],
    # 6. Unicode look-alikes
    "unicode": [
        f'python -c "import MainStoinker.{_fw("ib")}.cli"',
        f'python -c "import {_fw("MainStoinker")}.ib.cli"',
        f"python -m {_fw('MainStoinker')} fetch-bars",
        f"python -m {_fw('MainStoinker')}.ib.cli",
        f"python {_fw('MainStoinker')}/__main__.py fetch-bars",
        f'python -c "from {_fw("MainStoinker")} import __main__"',
    ],
}

# Blocked before and still blocked (not counted as new blocks). The loop rows
# pin the one item that allows more: a ForEach-Object loop needs the subcommand
# written out, everything else about loops and xargs stays blocked.
FIX3_STILL_BLOCKED = [
    "$s | ForEach-Object { python -m MainStoinker $_ }",
    "$s | ForEach-Object { python -m MainStoinker $_ --db x.db }",
    "$s | % { python -m MainStoinker fetch-bars }",
    "$s | ForEach-Object { python -m MainStoinker.ib.cli backtest }",
    "$s | ForEach-Object { python -m MainStoinker }",
    "$s | ForEach-Object { python -m MainStoinker backtest $_ }",
    "foreach ($s in 'backtest','fetch-bars') { python -m MainStoinker $s }",
    "$s | ForEach-Object {\n  python -m MainStoinker $_\n}",
    "echo fetch-bars | xargs python -m MainStoinker",
    "echo backtest | xargs python -m MainStoinker backtest",
    "ls | xargs echo; python -m MainStoinker backtest",
    "python -m $(echo MainStoinker) fetch-bars",
    "python -m `echo MainStoinker` fetch-bars",
    "python MainStoinker/ib/.. probe-data",
    'python -c "from MainStoinker import __main__" fetch-bars',
    "python -c \"from MainStoinker import *; sys.argv=['x']\"",
    "python -c \"import MainStoinker, sys; sys.argv=['x','fetch-bars']\"",
    "python -c \"from MainStoinker import engine; x = 1; compile('x','y','exec')\"",
    "python <<EOF\nimport pkgutil\npkgutil.resolve_name('MainStoinker.__main__')\nEOF",
    "python -m MainStoinker &> backtest fetch-bars",
    "python -m MainStoinker 2>&1 fetch-bars",
    "python -m MainStoinker @('fetch-bars')",
    "python -m MainStoinker $(echo fetch-bars)",
    "python -m MainStoinker ${env:SUB}",
    "python -m MainStoinker $sub",
    "python -m MainStoinker %SUB%",
    "python -m MainStoinker !SUB!",
    "python -m MainStoinker `fetch-bars`",
    "python -m MainStoinker (Get-Content sub.txt)",
    "python -c \"import importlib; importlib.import_module('MainStoinker.__main__')\"",
    'python -c "import MainStoinker.ib.cli"',
    "python -c \"import runpy; runpy.run_module('MainStoinker')\"",
    "python - <<EOF\nimport MainStoinker.__main__\nEOF",
]

FIX3_ALLOWED = [
    # redirects after the subcommand, and understood forms before it
    "python -m MainStoinker backtest --algos a.json --data OfflineData --out out >| log.txt",
    "python -m MainStoinker backtest --out out 2>&1",
    "python -m MainStoinker backtest --out out >& log.txt",
    "python -m MainStoinker backtest --out out &> log.txt",
    "python -m MainStoinker backtest <<< ''",
    "python -m MainStoinker 2>&1 backtest",
    "python -m MainStoinker > log.txt backtest",
    "python -m MainStoinker backtest <# fetch-bars #>",
    "python -m MainStoinker runs --db x.db <# note #>",
    # parentheses and expressions that are not in the subcommand position
    "(python -m MainStoinker backtest --out out)",
    "$r = (python -m MainStoinker report 3 --db x.db --json)",
    "& python -m MainStoinker backtest --algos (Get-Item a.json).FullName --out out",
    "python -m MainStoinker report 3 --db x.db | ConvertFrom-Json",
    "python -m MainStoinker serve --db x.db --port 8750",
    'iex "python -m MainStoinker backtest --algos a.json --data OfflineData --out out"',
    # Python code naming the package, without the marker words
    'python -c "import MainStoinker; print(MainStoinker.__file__)"',
    'python -c "from MainStoinker.engine import SimBroker; print(SimBroker)"',
    "python -c \"import json; print(json.dumps({'pkg': 'MainStoinker'}))\"",
    'python -c "print(1)"',
    'echo "from MainStoinker.engine import SimBroker" | python -',
    "python - <<EOF\nfrom MainStoinker.engine import SimBroker\nprint(SimBroker)\nEOF",
    "python -c \"import sys; sys.argv=['x']; print(sys.argv)\"",
    # the module position
    'python -m pytest -m "not slow" MainStoinker/engine',
    "python -m pytest -m slow MainStoinker",
    "M=other; python -m $M",
    "$m = 'pytest'; python -m $m tests",
    "python -m pytest tests/ -k 'a or b' --cov=MainStoinker",
    "python -m Main$()Stoinker backtest",
    "git commit -m 'fix hook' -- MainStoinker/engine/foo.py",
    'git commit -m "hook: block globs" MainStoinker/engine/foo.py',
    "python -m mypy MainSto*/engine",
    "python tools/count.py -m x MainStoinker/engine",
    # `..` that stays offline
    "python MainStoinker/engine/.. backtest --out out",
    "python MainStoinker/engine/../__main__.py runs --db x.db",
    "python MainStoinker/engine/.. --help",
    'python "MainStoinker\\engine\\.." report 3 --db x.db',
    "python tools/x.py MainStoinker/engine/..",
    "python -m pytest MainStoinker/engine/../testy",
    # Unicode
    f"python -m {_fw('MainStoinker')} backtest",
    f"python -m {_fw('pytest')} tests",
    "echo \uff46\uff45\uff54\uff43\uff48-\uff42\uff41\uff52\uff53",
    # loops with the subcommand written out
    "$algos | ForEach-Object { python -m MainStoinker backtest --algos $_ --data OfflineData --out out }",
    "Get-ChildItem cfg/*.json | ForEach-Object "
    "{ python -m MainStoinker backtest --algos $_.FullName --data OfflineData --out out }",
    "foreach ($a in Get-ChildItem cfg) { python -m MainStoinker backtest --algos $a.FullName --out out }",
    "1..3 | % { python -m MainStoinker report 1 --db x.db }",
    "$algos | ForEach-Object {\n  python -m MainStoinker backtest --algos $_ --out out\n}",
    'rg -n "foreach" MainStoinker/api/static; python -m MainStoinker runs --db x.db',
    "$s | ForEach-Object { Write-Output $_ }",
    "$s | ForEach-Object { python -m MainStoinker backtest --out o >| log.txt }",
    "echo x | xargs echo done",
]

_FIX3_BLOCKED_ROWS = [(g, c) for g, rows in FIX3_BLOCKED.items() for c in rows]


@pytest.mark.parametrize(("group", "command"), _FIX3_BLOCKED_ROWS)
def test_fix3_blocked(group: str, command: str) -> None:
    assert hook.decide(command)[0] is True, (group, command)
    assert hook.main(_payload(command)) == 2


@pytest.mark.parametrize("command", FIX3_STILL_BLOCKED)
def test_fix3_still_blocked(command: str) -> None:
    assert hook.decide(command)[0] is True, command


@pytest.mark.parametrize("command", FIX3_ALLOWED)
def test_fix3_allowed(command: str) -> None:
    assert hook.decide(command) == (False, ""), command
    assert hook.main(_payload(command)) == 0


def test_fix3_claude_md_commands_stay_allowed() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    block = text.split("```", 2)[1]
    commands = [ln.split("   #")[0].strip() for ln in block.splitlines() if ln.strip()]
    assert any("MainStoinker backtest" in c for c in commands)
    for command in commands:
        command = command.replace("[--allow-remote]", "").replace("[--json]", "")
        assert hook.decide(command) == (False, ""), command
    assert hook.decide("python -m MainStoinker serve --db DB --port 8750 --allow-remote") == (False, "")


def test_fix3_serve_module_forms_are_blocked_but_the_subcommand_is_allowed() -> None:
    assert not hasattr(hook, "_OTHER_OFFLINE_MODULES")
    assert hook.decide("python -m MainStoinker.api.serve")[0] is True
    assert hook.decide("python -m MainStoinker.engine.sim_broker")[0] is True
    assert hook.decide("python -m MainStoinker serve --db x.db") == (False, "")
    assert hook.decide(f"{VENV_PY} -m MainStoinker serve --db DB --port 8750") == (False, "")


def test_fix3_dotdot_is_resolved_for_script_paths() -> None:
    assert hook._normalise_connect("python MainStoinker/engine/.. x") == "python mainstoinker x"
    assert hook._normalise_connect("python MainStoinker/ib/../__main__.py") == (
        "python mainstoinker/__main__.py"
    )
    assert hook._normalise_connect("python MainStoinker/engine/..", resolve=False) == (
        "python mainstoinker/engine/.."
    )


def test_fix3_loop_feeders_allow_only_a_written_out_offline_subcommand() -> None:
    ok = "$a | ForEach-Object { python -m MainStoinker backtest --algos $_ --out o }"
    assert hook.decide(ok) == (False, "")
    # the loop value as the subcommand, or right after it, is not accepted
    assert hook.decide(ok.replace("backtest --algos $_", "$_ --algos x"))[0] is True
    assert hook.decide(ok.replace("backtest --algos $_", "backtest $_"))[0] is True
    # xargs and parallel are blocked whatever is written
    for feeder in ("xargs", "parallel"):
        assert hook.decide(f"ls | {feeder} python -m MainStoinker backtest")[0] is True
    assert hook._LOOP_FEEDERS == frozenset({"foreach-object", "foreach", "%"})
    assert hook._STREAM_FEEDERS == frozenset({"xargs", "parallel"})


def test_fix3_expression_in_the_subcommand_slot_is_not_a_subcommand() -> None:
    toks, marks = hook._tokenise("python -m mainstoinker (backtest,fetch-bars)[1]")
    assert toks[3] == "backtest"
    assert marks[3] is True and marks[2] is False
    assert hook._ODD_OP.match(">|") and hook._ODD_OP.match("2>|") and hook._ODD_OP.match("<#")
    assert not hook._ODD_OP.match("2>&1") and not hook._ODD_OP.match(">&2")
    assert not hook._ODD_OP.match(">") and not hook._ODD_OP.match("2>")


def test_fix3_code_runners_need_the_package_name() -> None:
    for code in (
        "from mainstoinker import __main__",
        "import sys; sys.argv = ['x']; import mainstoinker",
        "import pkgutil; mainstoinker",
        "mainstoinker; importlib",
        "mainstoinker; __import__('x')",
        "mainstoinker; exec('x')",
        "mainstoinker; eval('x')",
        "mainstoinker; compile('x', 'y', 'exec')",
        "from mainstoinker import x; x.ib.y",
        "mainstoinker; runpy",
    ):
        assert hook._scan_code(code) is not None, code
    for code in (
        "sys.argv = ['x']",
        "import pkgutil",
        "import mainstoinker",
        "print(mainstoinker.engine)",
    ):
        assert hook._scan_code(code) is None, code


def test_fix3_new_rules_are_linear_in_time() -> None:
    p = "MainStoinker"
    shapes = {
        "odd_ops": "python -m " + p + " " + ">| " * 20_000,
        "exprs": "python -m " + p + " " + "(" * 20_000 + "backtest",
        "dotdot": "python " + "a/../" * 8_000 + p + " backtest",
        "dotdot_many_tokens": "python " + "x/.. " * 8_000 + p,
        "c_args": ("python -c 'a' " * 3_000) + p,
        "c_args_py": 'python -c "' + "; ".join(["x"] * 5_000) + '" ' + p,
        "modules": "python " + "-m $M " * 6_000 + p,
        "text_ops": "python -m " + p + " backtest " + "-replace " * 8_000,
        "globs": "python -m " + "*?[" * 15_000 + " " + p,
        "fullwidth": 'python -c "' + _fw(p) * 3_000 + '"',
        "loops": ("foreach { python -m " + p + " backtest }; ") * 3_000,
    }
    for name, command in shapes.items():
        _, took = _timed(command)
        assert took < _TIMING_BOUND, (name, took)
