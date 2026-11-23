"""The one allowance of the live-launch hook: an unattended paper DATA run.

Every command here is a string fed to the hook's decision functions (never run). The shape
is `<venv python> [-P] -m MainStoinker (fetch-bars|probe-data) <args>` with `--paper` and
`--no-prompt` among plain args, as ONE whole command; everything else stays as blocked as it
was (the frozen baseline test in `test_block_live_launch_hook.py` pins that independently).
"""

from __future__ import annotations

import functools
import hashlib
import io
import itertools
import json
import re
import types

import pytest

from tests.test_block_live_launch_hook import ENTRY, REPO_ROOT, SCRIPT

hook = types.ModuleType("block_live_launch_paper")
hook.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), hook.__dict__)

BEFORE = (REPO_ROOT / "tests" / "fixtures" / "hook_baseline"
          / "block_live_launch_before_paper_allowance.py.txt")
# sha256 of the frozen copy of the hook as it was before this allowance (LF line endings)
BEFORE_SHA256 = "4bcdc96d6d694c01fca32010b3cb0f245afe05b16bcd84034636883222f67a25"


@functools.cache
def _before() -> types.ModuleType:
    """The hook as it was before the allowance: every verdict it gave must still be given."""
    data = BEFORE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == BEFORE_SHA256, "the frozen pre-allowance hook was edited"
    module = types.ModuleType("block_live_launch_before_paper")
    module.__file__ = str(BEFORE)
    exec(compile(data.decode("utf-8"), module.__file__, "exec"), module.__dict__)
    return module


def not_looser(command: str) -> bool:
    """True when the hook blocks `command` unless the hook before the allowance let it through too."""
    return hook.decide(command)[0] or not _before().decide(command)[0]

ENV_NAME = "STOINK_ALLOW_LIVE_PORT"
PROJECT = "C:\\Users\\Paul Wilda\\OneDrive - 28 Gorilla\\Documents\\Stonks"
QP = f'"{PROJECT}\\.venv\\Scripts\\python.exe"'
QPF = f'"{PROJECT.replace(chr(92), "/")}/.venv/Scripts/python.exe"'
UP = ".venv\\Scripts\\python.exe"
UPF = ".venv/Scripts/python.exe"
MODULE = "-m MainStoinker"
DATES = "--start 2024-08-16 --end 2024-08-26"
FETCH_ARGS = f"--symbols AAPL,MSFT,TSLA {DATES} --paper --no-prompt"
PROBE_ARGS = "--symbols AAPL,MSFT --paper --no-prompt"


def fetch(py: str = UP, args: str = FETCH_ARGS, mid: str = "") -> str:
    return f"{py}{mid} {MODULE} fetch-bars {args}"


def probe(py: str = UP, args: str = PROBE_ARGS, mid: str = "") -> str:
    return f"{py}{mid} {MODULE} probe-data {args}"


ALLOWED = [
    fetch(),
    fetch(UPF),
    fetch(QP),
    fetch(QPF),
    fetch("& " + QP),
    fetch("& " + UP),
    fetch("&  " + UPF),
    fetch(QP, mid=" -P"),
    fetch("& " + QP, mid=" -P"),
    fetch(UP, mid=" -P"),
    fetch('".venv\\Scripts\\python.exe"'),
    fetch("C:/Stonks/.venv/Scripts/python.exe"),
    fetch("./.venv/Scripts/python.exe "),
    fetch(args=f'--symbols "AAPL,MSFT,TSLA" {DATES} --paper --no-prompt'),
    probe(args='--symbols "AAPL,MSFT" --paper --no-prompt'),
    fetch("..\\.venv\\Scripts\\python.exe"),
    probe(),
    probe(UPF),
    probe(QP),
    probe("& " + QP, mid=" -P"),
    fetch(args=f"--paper --no-prompt --symbols AAPL {DATES}"),
    fetch(args=f"--no-prompt --paper --symbols AAPL {DATES}"),
    fetch(args=f"{DATES} --symbols AAPL --no-prompt --paper"),
    fetch(args=f"--symbols AAPL,MSFT,TSLA {DATES} --paper --no-prompt --port 7497"),
    fetch(args=f"--port 7497 --symbols AAPL {DATES} --paper --no-prompt"),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --port=7497"),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --host 127.0.0.1 --port 7497"),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --host=127.0.0.1"),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --port 7497 --port 7497"),
    fetch(args=f'--symbols AAPL {DATES} --paper --no-prompt --out "{PROJECT}\\bars dir"'),
    fetch(args=f'--symbols AAPL {DATES} --paper --no-prompt --out "C:/Users/x y/Stoinkulator/bars"'),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --out C:\\Users\\x\\bars"),
    fetch(args=f'--symbols AAPL {DATES} --paper --no-prompt --record "C:\\Users\\x y\\rec.jsonl"'),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --no-record --settle-hours 1.5"),
    fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt --client-id 21 --connect-timeout 30"),
    probe(args="--paper --no-prompt"),
    probe(args="--paper --no-prompt --symbols AAPL --quote-seconds 10 --no-history"),
    probe(args='--paper --no-prompt --no-record --record-x "a b"'.replace(" --record-x", " --out")),
    "  " + fetch() + "  ",
    fetch() + "\n",
    "\t" + probe(QP) + "\r\n",
    fetch(args=FETCH_ARGS.replace(" ", "   ")),
]


def refused_table() -> list[str]:
    out: list[str] = []

    def add(*items: str) -> None:
        out.extend(items)

    base = fetch()
    base_p = probe()
    # missing or altered flags
    add(
        fetch(args=f"--symbols AAPL {DATES} --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --paper"),
        fetch(args=f"--symbols AAPL {DATES}"),
        fetch(args=f"--symbols AAPL {DATES} --pape --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --pap --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --p --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --paper --no-promp"),
        fetch(args=f"--symbols AAPL {DATES} --paper --no-prompts"),
        fetch(args=f"--symbols AAPL {DATES} --paper --no-prompt=1"),
        fetch(args=f"--symbols AAPL {DATES} --paper=true --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --PAPER --no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --paper --No-Prompt"),
        fetch(args=f'--symbols "--paper" {DATES} --no-prompt'),
        fetch(args=f'--symbols "--paper" {DATES} "--no-prompt"'),
        fetch(args=f'--symbols AAPL {DATES} "--paper" "--no-prompt"'),
        fetch(args=f'--symbols AAPL {DATES} --paper "--no-prompt"'),
        fetch(args=f'--symbols AAPL {DATES} --out "x --paper --no-prompt"'),
        fetch(args=f"--symbols AAPL {DATES} -paper -no-prompt"),
        fetch(args=f"--symbols AAPL {DATES} --paper -- --no-prompt"),
        probe(args="--no-prompt"),
        probe(args="--paper"),
        probe(args="--symbols AAPL"),
        probe(args='--symbols "--paper" --no-prompt'),
        probe(args="--pape --no-prompt"),
    )
    # ports
    for bad in ("7496", "4001", "4002", "74970", "07497", "7497.0", "7497,7496", "0", "", "7498"):
        add(fetch(args=f"{FETCH_ARGS} --port {bad}".replace("--port ", "--port " if bad else "--port")))
    add(
        fetch(args=f"{FETCH_ARGS} --port 7497 --port 7496"),
        fetch(args=f"{FETCH_ARGS} --port 7496 --port 7497"),
        fetch(args=f"{FETCH_ARGS} --port=7496"),
        fetch(args=f"{FETCH_ARGS} --port=4002"),
        fetch(args=f"{FETCH_ARGS} --port=74970"),
        fetch(args=f"{FETCH_ARGS} --port="),
        fetch(args=f"{FETCH_ARGS} --port"),
        fetch(args=f'{FETCH_ARGS} --port "7497"'),
        fetch(args=f'{FETCH_ARGS} "--port" 7496'),
        fetch(args=f'{FETCH_ARGS} "--port=7496"'),
        fetch(args=f"{FETCH_ARGS} --por 7497"),
        fetch(args=f"{FETCH_ARGS} --po 7496"),
        fetch(args=f"{FETCH_ARGS} --PORT 7497"),
        fetch(args=f"{FETCH_ARGS} --symbols AAPL,--port,7496"),
        fetch(args=f"{FETCH_ARGS} --out x,--port"),
        probe(args=f"{PROBE_ARGS} --port 7496"),
        probe(args=f"{PROBE_ARGS} --port 4001"),
        probe(args=f"{PROBE_ARGS} --port=4002"),
        probe(args=f"{PROBE_ARGS} --port 7497 --port 7496"),
    )
    # hosts
    add(
        fetch(args=f"{FETCH_ARGS} --host localhost"),
        fetch(args=f"{FETCH_ARGS} --host 127.0.0.2"),
        fetch(args=f"{FETCH_ARGS} --host=localhost"),
        fetch(args=f'{FETCH_ARGS} --host "127.0.0.1"'),
        fetch(args=f"{FETCH_ARGS} --host"),
        fetch(args=f"{FETCH_ARGS} --host 127.0.0.1 --host localhost"),
        fetch(args=f"{FETCH_ARGS} --hos 127.0.0.1"),
        fetch(args=f"{FETCH_ARGS} --h 127.0.0.1"),
        probe(args=f"{PROBE_ARGS} --host 0.0.0.0"),
    )
    # other interpreters and paths
    for py in ("python", "python.exe", "py", "py -3", "python3", "pythonw", "PYTHON.EXE", "uv run python",
               "uv run", "pipx run", "venv\\Scripts\\python.exe", ".venv\\Scripts\\pythonw.exe",
               ".venv\\Scripts\\python", ".venv\\bin\\python.exe", "x.venv\\Scripts\\python.exe",
               ".venv\\Scripts\\python.exe.bak", ".venv\\Scripts\\Python.EXE",
               ".venv\\Scripts\\python.exe\\x",
               '"C:\\Python314\\python.exe"', "C:\\Python314\\python.exe", ".venv\\Scripts\\activate.bat",
               "& & .venv\\Scripts\\python.exe", "&.venv\\Scripts\\python.exe",
               "call .venv\\Scripts\\python.exe",
               "start .venv\\Scripts\\python.exe", "'.venv/Scripts/python.exe'",
               "& '.venv/Scripts/python.exe'", '"' + ".venv\\Scripts\\python.exe -P" + '"'):
        add(fetch(py))
    # a second -P, wrong flags before -m, -c code, other modules and subcommands
    add(
        f"{UP} -P -P {MODULE} fetch-bars {FETCH_ARGS}",
        f"{UP} -u {MODULE} fetch-bars {FETCH_ARGS}",
        f"{UP} -I {MODULE} fetch-bars {FETCH_ARGS}",
        f"{UP} -m MainStoinker -m MainStoinker.ib.watch_bars fetch-bars {FETCH_ARGS}",
        f"{UP} -c print(1) {MODULE} fetch-bars {FETCH_ARGS}",
        f'{UP} -c "import MainStoinker.ib.watch_bars" {FETCH_ARGS}',
        f'{UP} -c "import runpy" {MODULE} fetch-bars {FETCH_ARGS}',
        f"{UP} -m MainStoinker.ib.watch_bars --paper --no-prompt",
        f"{UP} -m MainStoinker.ib.cli --paper --no-prompt",
        f"{UP} -m MainStoinker.ib --paper --no-prompt",
        f"{UP} -m MainStoinker.engine.cli fetch-bars {FETCH_ARGS}",
        f"{UP} -m MainStoinker.__main__ fetch-bars {FETCH_ARGS}",
        f"{UP} -m mainstoinker fetch-bars {FETCH_ARGS}",
        f"{UP} -m MAINSTOINKER fetch-bars {FETCH_ARGS}",
        f"{UP} -mMainStoinker fetch-bars {FETCH_ARGS}",
        f"{UP} --module MainStoinker fetch-bars {FETCH_ARGS}",
        f"{UP} -m MainStoinker run {FETCH_ARGS}",
        f"{UP} -m MainStoinker fetch-bar {FETCH_ARGS}",
        f"{UP} -m MainStoinker Fetch-Bars {FETCH_ARGS}",
        f"{UP} -m MainStoinker fetch_bars {FETCH_ARGS}",
        f"{UP} -m MainStoinker fetch-bars2 {FETCH_ARGS}",
        f"{UP} -m MainStoinker watch {FETCH_ARGS}",
        f"{UP} -m MainStoinker {FETCH_ARGS}",
        f"{UP} -m MainStoinker fetch-bars --help",
        f"{UP} -m MainStoinker probe-data -h",
        f"{UP} -m MainStoinker fetch-bars",
        f"{UP} {MODULE} fetch-bars {FETCH_ARGS} -m MainStoinker.ib.watch_bars",
        f"{UP} {MODULE} fetch-bars {FETCH_ARGS} -c x",
        f"{UP} MainStoinker fetch-bars {FETCH_ARGS}",
        f"{UP} MainStoinker/__main__.py fetch-bars {FETCH_ARGS}",
        f"{UP} MainStoinker\\ib\\watch_bars.py {FETCH_ARGS}",
        f'{UP} "{ENTRY}" {FETCH_ARGS}',
    )
    # other reasons to block, hidden in a value the shape tolerates
    add(
        fetch(args=f'{FETCH_ARGS} --out "{ENTRY}"'),
        fetch(args=f'{FETCH_ARGS} "{ENTRY}"'),
        fetch(args=f'{FETCH_ARGS} --out "C:\\x\\Stonks data collect.bat"'),
        fetch(args=f'{FETCH_ARGS} --out "C:\\x\\MainStoinker\\ib\\bars"'),
        fetch(args=f'{FETCH_ARGS} --out "C:\\x -m MainStoinker.ib"'),
        fetch(args=f'{FETCH_ARGS} --out "C:\\x -c y"'),
        fetch(args=f'{FETCH_ARGS} --out "C:\\x\\"'),
        fetch(args=f'{FETCH_ARGS} --out ""'),
        fetch(args=f'{FETCH_ARGS} --out "-x"'),
        fetch(args=f'{FETCH_ARGS} --out "a,-b"'),
        fetch(args=f"{FETCH_ARGS} --out C:\\x\\"),
        fetch(args=f"{FETCH_ARGS} -x"),
        fetch(args=f"{FETCH_ARGS} -"),
        fetch(args=f"{FETCH_ARGS} --"),
        fetch(args=f"{FETCH_ARGS} --symbols a,-b"),
        f'{UP} -m MainStoinker.testy.x fetch-bars {FETCH_ARGS}',
    )
    # separators, redirects, expansions and wrappers around or inside the command
    for suffix in (
        "; echo x", " && echo x", " || echo x", " | more", " & echo x", " > out.txt", " >> out.txt",
        " 2>&1", " < in.txt", " `", " $x", " $(x)", " ${x}", " %X%", " ^", " (x)", " {x}", " 'x'", " [x]",
        " *", " ?", " ~", " !", " #c", " @x", "\necho x", "\r\necho x", "\0", " \\\n", " `\n", " ^\n",
        " --out\\\n", " \\", "\n", ";", "&",
    ):
        if suffix in ("\n",):
            continue  # a trailing newline is outer whitespace, tested on its own
        add(fetch() + suffix)
    for prefix in (
        "cd x; ", "cd x && ", "cd x\n", "cd x\r\n", "Set-Location x; ", "pushd x; ", "VAR=x ", "A=1 B=2 ",
        "$env:A=1; ", "$env:A=1\n", "set A=1&& ", "set A=1\n", "export A=1; ", "env A=1 ", "env ", "start ",
        "start \"\" ", "cmd /c ", "cmd.exe /c ", "cmd //c ", "powershell -c ", "powershell -Command ",
        "pwsh -c ", "bash -c ", "sh -c ", "call ", "time ", "nohup ", "timeout 5 ", "xargs ", "sudo ",
        "echo hi | ", "echo hi; ", "Start-Process ", "Invoke-Expression ", "iex ", "wsl ", "exec ",
        "command ", "& & ", "& cd x; ", ". ", "source ", "(", "{ ", "@(", "$(", "`", "!", "~", "echo x &&",
        "echo x\n",
    ):
        add(prefix + fetch(), prefix + probe(QP))
    for wrapper in ('cmd /c "{c}"', "cmd /c '{c}'", 'powershell -c "{c}"', "powershell -Command '{c}'",
                    'bash -c "{c}"', "bash -c '{c}'", 'echo "{c}" | cmd', 'echo "{c}" | bash',
                    '"{c}"', "'{c}'", "$({c})", "`{c}`", "({c})", "{{ {c} }}", "& {{ {c} }}", "start '{c}'",
                    "@'\n{c}\n'@ | iex", "<<EOF\n{c}\nEOF", "bash <<< '{c}'", "eval '{c}'", "eval \"{c}\""):
        add(wrapper.format(c=fetch()))
    # a second command, legacy script and launcher
    add(
        fetch() + f'\n{UP} "{ENTRY}"',
        f'{UP} "{ENTRY}"\n' + fetch(),
        fetch() + "\n" + fetch(),
        fetch() + "\r\n" + probe(),
        f'{fetch()}; "Stonks data collect.bat"',
        f'"Stonks data collect.bat"; {fetch()}',
        f'cmd /c "Stonks data collect.bat" && {fetch()}',
        f'python "{ENTRY}"; {fetch()}',
        fetch() + f' & python "{ENTRY}"',
        f"{UP} MainStoinker/testy/testy.py; {fetch()}",
        fetch() + "\n" + "python MainStoinker/testy/testy.py",
        f"{UP} -m MainStoinker.testy {FETCH_ARGS}",
    )
    # the live opt-in variable anywhere (every shell's syntax, and inside the allowed shape)
    for name in (ENV_NAME, ENV_NAME.lower(), "Stoink_Allow_Live_Port", "ALLOW_LIVE_PORT"):
        add(
            f"{name}=1 {base}",
            f"{name}=1 {base_p}",
            f"$env:{name}='1'; {base}",
            f'$env:{name}="1"; {base}',
            f"set {name}=1 && {base}",
            f"set {name}=1\n{base}",
            f"export {name}=1; {base}",
            f"export {name}=1\n{base}",
            f"env {name}=1 {base}",
            f"(Set-Item Env:{name} 1); {base}",
            f"[Environment]::SetEnvironmentVariable('{name}','1'); {base}",
            f'cmd /c "set {name}=1&& {base}"',
            f'{base} --symbols {name}',
            f'{base} --out {name}',
            f'{base} --out "x {name}"',
            f"{base} --symbols AAPL,{name}",
            f"{base} {name}=1",
            f"{UP.replace('.venv', name)} {MODULE} fetch-bars {FETCH_ARGS}",
            f"{base_p} --record C:\\{name}\\r.jsonl",
        )
    # look-alikes of letters, dashes and spaces
    for fake in ("\u2011\u2011paper", "\u2212\u2212paper", "\uff0d\uff0dpaper", "\u2013\u2013paper",
                 "\u2014paper", "--p\u0430per", "--\u0440aper", "--pap\u0435r", "\uff0d-paper",
                 "--\uff50aper", "--pa\u200bper", "--paper\u200b", "\u200b--paper", "--pa\u00adper"):
        add(fetch(args=f"--symbols AAPL {DATES} {fake} --no-prompt"))
    for fake in ("\u2011\u2011no-prompt", "\uff0d\uff0dno-prompt", "--no\u2011prompt", "--no\u2212prompt",
                 "--no-pr\u043empt", "--no-prompt\u200b"):
        add(fetch(args=f"--symbols AAPL {DATES} --paper {fake}"))
    add(
        fetch().replace("MainStoinker", "Main\u200bStoinker"),
        fetch().replace("MainStoinker", "\uff2dainStoinker"),
        fetch().replace("fetch-bars", "fetch\u2011bars"),
        fetch().replace("fetch-bars", "fetch\uff0dbars"),
        fetch().replace("fetch-bars", "f\u0435tch-bars"),
        fetch().replace("python.exe", "pyth\u043en.exe"),
        fetch().replace("python.exe", "\uff50ython.exe"),
        fetch().replace(" ", "\u00a0", 1),
        fetch().replace("--paper ", "--paper\u00a0"),
        fetch().replace("--paper ", "--paper\u2003"),
        fetch().replace(" -m ", " \u2212m "),
        fetch().replace(" -m ", " \u2011m "),
        fetch().replace(".venv", ".v\u0435nv"),
        fetch().replace("--paper", "--paper\u0301"),
        fetch().replace("7497", "7497") + " --port \uff17\uff14\uff19\uff17",
        fetch() + " --port 7\u0663\u0664\u0669",
    )
    # continuation lines and here-strings
    add(
        fetch() + " \\\n",
        fetch() + " \\\n" + "echo x",
        fetch() + " `\necho x",
        fetch() + " ^\necho x",
        fetch() + " \\\r\n",
        fetch() + "\\\n",
        fetch(args=FETCH_ARGS + " \\\n--port 7496"),
        fetch(args=FETCH_ARGS + " `\n--port 7496"),
        fetch(args="--paper \\\n--no-prompt --symbols AAPL " + DATES),
        fetch() + " @'\nx\n'@",
        fetch() + " <<EOF\nx\nEOF",
        fetch() + " <<<x",
    )
    return list(dict.fromkeys(out))


REFUSED = refused_table()

# Offline subcommands keep their existing treatment: not allowed BY THIS RULE, and the
# existing logic lets them run, as before.
OFFLINE_NOT_THIS_RULE = [
    f"{UP} -m MainStoinker serve --paper --no-prompt",
    f"{UP} -m MainStoinker backtest --paper --no-prompt",
    f"{UP} -m MainStoinker runs --paper --no-prompt",
    f"{UP} -m MainStoinker report x --paper --no-prompt",
]


# -- the table ------------------------------------------------------------------------------


def test_the_tables_are_big_enough() -> None:
    assert len(set(ALLOWED)) >= 25
    assert len(set(REFUSED)) >= 80
    assert not set(ALLOWED) & set(REFUSED)


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_spellings(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is True
    assert hook.decide(command) == (False, "")


@pytest.mark.parametrize("command", REFUSED)
def test_refused_variants(command: str) -> None:
    assert hook.decide(command)[0] is True, command
    assert hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) == 2


@pytest.mark.parametrize("command", REFUSED)
def test_refused_variants_are_not_allowed_by_the_function_either(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is False, command


@pytest.mark.parametrize("command", OFFLINE_NOT_THIS_RULE)
def test_offline_subcommands_keep_their_existing_treatment(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is False
    assert hook.decide(command)[0] is _before().decide(command)[0] is False


# Not the package, so not for this rule to judge: refused by the rule, verdict unchanged.
NOT_THE_PACKAGE = [
    f"{UP} -m Other fetch-bars {FETCH_ARGS}",
    fetch().replace("MainStoinker", "MainStоinker"),
]


@pytest.mark.parametrize("command", NOT_THE_PACKAGE)
def test_other_modules_are_not_allowed_by_the_rule_and_keep_their_verdict(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is False
    assert hook.decide(command) == _before().decide(command)


@pytest.mark.parametrize("command", REFUSED)
def test_nothing_refused_here_became_allowed(command: str) -> None:
    assert not_looser(command)


def test_every_allowed_spelling_was_blocked_before() -> None:
    assert [c for c in ALLOWED if not _before().decide(c)[0]] == []


def test_stray_plain_words_are_left_to_argparse() -> None:
    # The shape cannot tell a stray word from a value; `cli.py` rejects it as a usage error.
    for tail in (" probe-data", " --testy.py", " start", " word"):
        assert hook.allowed_paper_data_command(ALLOWED[0] + tail) is True


# -- independent of the tool name and the working directory -------------------------------


@pytest.mark.parametrize("tool", ["Bash", "PowerShell", "bash", "Other", None])
@pytest.mark.parametrize("cwd", [None, "C:\\anywhere", REPO_ROOT.as_posix()])
def test_the_verdict_does_not_depend_on_tool_name_or_cwd(tool: str | None, cwd: str | None) -> None:
    for command, expected in ((ALLOWED[0], 0), (ALLOWED[3], 0), (REFUSED[0], 2)):
        data: dict[str, object] = {"tool_input": {"command": command}}
        if tool is not None:
            data["tool_name"] = tool
        if cwd is not None:
            data["cwd"] = cwd
        assert hook.main(json.dumps(data)) == expected


def test_the_hook_process_entry_allows_the_shape_and_blocks_the_rest() -> None:
    def run(command: str) -> int:
        raw = json.dumps({"tool_name": "PowerShell", "tool_input": {"command": command}}).encode()
        return hook.run_stdin(io.BytesIO(raw))

    assert run(ALLOWED[4]) == 0
    assert run(REFUSED[0]) == 2


# -- only the connecting reason is suppressed ----------------------------------------------


def test_the_subcommand_word_is_the_only_thing_the_allowance_changes() -> None:
    for command in (ALLOWED[0], ALLOWED[4], ALLOWED[13], "  " + ALLOWED[2] + "\n"):
        neutral = hook._paper_neutral(command)
        assert neutral is not None
        sub = "fetch-bars" if " fetch-bars " in command else "probe-data"
        assert neutral == command.replace(f" {sub} ", " backtest ", 1)
    assert hook._paper_neutral(REFUSED[0]) is None


def test_other_blocks_still_apply_to_the_allowed_shape() -> None:
    for tail in (f' --out "{ENTRY}"', ' --out "C:\\x\\Stonks data collect.bat"',
                 ' --out "C:\\x\\MainStoinker\\ib\\bars"', ' "MainStoinker/testy/testy.py"'):
        command = ALLOWED[0] + tail
        assert hook.decide(command)[0] is True
        assert hook.allowed_paper_data_command(command) is False


def test_the_allowance_is_off_for_decoded_and_folded_text() -> None:
    allowed = ALLOWED[0]
    # decoded -EncodedCommand text is judged with the allowance off
    import base64

    encoded = base64.b64encode(allowed.encode("utf-16-le")).decode()
    assert hook.decide(f"powershell -EncodedCommand {encoded}")[0] is True
    # a full-width look-alike must not fold into the allowed shape
    folded = allowed.replace("--paper", "\uff0d\uff0dpaper")
    assert hook.decide(folded)[0] is True


def test_a_trailing_newline_is_outer_whitespace_but_a_continuation_is_not() -> None:
    assert hook.allowed_paper_data_command(ALLOWED[0] + "\n") is True
    assert hook.allowed_paper_data_command(ALLOWED[0] + " \\\n") is False
    assert hook.allowed_paper_data_command("\n\n" + ALLOWED[0]) is True
    assert hook.allowed_paper_data_command(ALLOWED[0].replace(" -m", "\n-m")) is False


def test_the_function_is_called_in_one_place_in_the_decision_flow() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    calls = re.findall(r"allowed_paper_data_command\(", text)
    assert len(calls) == 2  # its definition and the one call in `_decide_text`
    assert text.count("_paper_neutral(") == 3  # definition, one call in the function, one in the flow


def test_the_block_message_teaches_the_allowed_form() -> None:
    reason = hook.decide(f"{UP} -m MainStoinker fetch-bars --port 7497")[1]
    for word in ("--paper", "--no-prompt", ".venv", "7497", "127.0.0.1", "fetch-bars|probe-data", "PAPER"):
        assert word in reason
    assert ENV_NAME not in reason


def test_the_commands_shown_in_claude_md_are_the_allowed_shape() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    block = text.split("## Commands", 1)[1].split("```")[1]
    paper = [ln.split("   #")[0].strip() for ln in block.splitlines() if "--no-prompt" in ln]
    assert len(paper) == 2
    assert any(" fetch-bars " in c for c in paper) and any(" probe-data " in c for c in paper)
    for command in paper:
        assert hook.allowed_paper_data_command(command), command


def test_claude_md_states_the_paper_rules() -> None:
    text = " ".join((REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    for phrase in (
        "`python -m MainStoinker fetch-bars` or `probe-data` with `--paper --no-prompt`",
        "No other way of connecting (no script, no `-c` code, no other module, no test)",
        "the live ports and `STOINK_ALLOW_LIVE_PORT` are the owner's alone",
        "One broker session at a time, started by the main session, never by parallel subagents",
        "Exit code 5 (`fetch-bars`) or 4 (`probe-data`)",
        "Order placement on the paper account is NOT allowed yet",
        "Tests never connect.",
    ):
        assert phrase in text, phrase


# -- the property: any forbidden character or wrapper, anywhere, is refused -------------------

FORBIDDEN_CHARS = [
    ";", "&&", "||", "|", "&", "`", "$", "%X%", "$(x)", "${x}", "<", ">", ">>", "2>&1", "(", ")", "{", "}",
    "[", "]", "'", '"', "^", "*", "?", "~", "!", "#", "@", "\n", "\r", "\r\n", "\0", "\\\n", "`\n", "^\n",
    "\t", "\u2011", "\uff0d", "\u00a0", "\u200b", "<<EOF", "<<<", "@'", "'@", "|iex", "\x0b", "\x0c",
]
START_WRAPPERS = [
    "cd x;", "cd x &&", "Set-Location x;", "VAR=x", "A=1 B=2", "$env:A=1;", "set A=1&&", "start", "cmd /c",
    "cmd.exe /c", "powershell -c", "powershell -Command", "pwsh -c", "bash -c", "sh -c", "call", "env",
    "nohup", "time", "timeout 5", "xargs", "sudo", "Start-Process", "iex", "wsl", "exec", "command",
    f"{ENV_NAME}=1", f"$env:{ENV_NAME}=1;", f"set {ENV_NAME}=1&&", f"export {ENV_NAME}=1;",
]
SPLIT = re.compile(r'"[^"]*"|\S+')


def _variants(command: str) -> list[tuple[str, str]]:
    """(label, mutated command): the item inserted at the start, the end, between tokens, glued."""
    tokens = SPLIT.findall(command.strip())
    out: list[tuple[str, str]] = []
    for item in FORBIDDEN_CHARS:
        for i in range(len(tokens) + 1):
            out.append((f"{item!r} as a token at {i}", " ".join([*tokens[:i], item, *tokens[i:]])))
        out.append((f"{item!r} glued at the very start", item + " ".join(tokens)))
        out.append((f"{item!r} glued at the very end", " ".join(tokens) + item))
        for i in range(len(tokens)):
            mutated = list(tokens)
            mutated[i] = item + mutated[i]
            out.append((f"{item!r} glued before token {i}", " ".join(mutated)))
            mutated = list(tokens)
            mutated[i] = mutated[i] + item
            out.append((f"{item!r} glued after token {i}", " ".join(mutated)))
    for item in START_WRAPPERS:
        out.append((f"wrapper {item!r} at the start", item + " " + " ".join(tokens)))
    for item in (ENV_NAME, ENV_NAME.lower(), f"{ENV_NAME}=1"):
        for i in range(len(tokens) + 1):
            out.append((f"{item!r} as a token at {i}", " ".join([*tokens[:i], item, *tokens[i:]])))
    return out


def _benign(label: str, mutated: str, original: str) -> bool:
    """The few insertions that leave an allowed command allowed: outer whitespace, and the
    call operator written as the prefix."""
    stripped = mutated.strip(" \t\r\n")
    if stripped == original.strip():
        return True  # only whitespace added at the very start or end
    if label.startswith("'&' as a token at 0") and not original.lstrip().startswith("&"):
        return True  # `& <python>`: the call operator is part of the shape
    return False


PROPERTY_BASES = [ALLOWED[0], ALLOWED[4], ALLOWED[7], ALLOWED[13], ALLOWED[27]]


@pytest.mark.parametrize("original", PROPERTY_BASES)
def test_any_forbidden_character_or_wrapper_anywhere_is_refused(original: str) -> None:
    variants = _variants(original)
    assert len(variants) > 500
    for label, mutated in variants:
        if _benign(label, mutated, original):
            assert hook.allowed_paper_data_command(mutated), label
            continue
        assert hook.allowed_paper_data_command(mutated) is False, (label, mutated)
        assert not_looser(mutated), (label, mutated)  # the hook before the allowance blocked it too


def test_every_allowed_spelling_survives_each_character_at_each_place_only_as_refused() -> None:
    count = 0
    for original in ALLOWED:
        for char, i in itertools.product(["`", "$", ";", "|", ">", "<", "(", ")", "\n"], (0, 3, 6)):
            tokens = SPLIT.findall(original.strip())
            i = min(i, len(tokens))
            mutated = " ".join([*tokens[:i], char, *tokens[i:]])
            if char == "\n" and i in (0, len(tokens)):
                continue  # leading and trailing whitespace is trimmed
            assert hook.allowed_paper_data_command(mutated) is False, mutated
            count += 1
    assert count > 300
