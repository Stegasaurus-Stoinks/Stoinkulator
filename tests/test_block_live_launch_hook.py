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
        "python -m MainStoinker.api.serve",
        "python -m MainStoinker.engine.sim_broker",
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
