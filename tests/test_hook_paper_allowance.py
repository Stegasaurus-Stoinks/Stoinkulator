"""The one allowance of the live-launch hook: an unattended paper DATA run.

Every command here is a string fed to the hook's decision functions (never run). The shape
is `<venv python> -P -m MainStoinker (fetch-bars|probe-data) <args>` with `--paper` and
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


def fetch(py: str = UP, args: str = FETCH_ARGS, mid: str = " -P") -> str:
    return f"{py}{mid} {MODULE} fetch-bars {args}"


def probe(py: str = UP, args: str = PROBE_ARGS, mid: str = " -P") -> str:
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
    fetch(".venv/Scripts/python.exe "),
    fetch(args=f'--symbols "AAPL,MSFT,TSLA" {DATES} --paper --no-prompt'),
    probe(args='--symbols "AAPL,MSFT" --paper --no-prompt'),
    fetch('"D:\\work dir\\Stonks\\.venv\\Scripts\\python.exe"'),
    fetch("C:\\.venv\\Scripts\\python.exe"),
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

    UP = ".venv\\Scripts\\python.exe -P"  # noqa: N806 - here every literal command carries the mandatory -P
    base = fetch()
    base_p = probe()
    # -P is mandatory: the current directory must never come first on sys.path
    for py in (".venv\\Scripts\\python.exe", ".venv/Scripts/python.exe", QP, "& " + QP, QPF):
        add(fetch(py, mid=""), probe(py, mid=""))
    add(
        fetch(mid=" -p"), fetch(mid=" -PP"), fetch(mid=" -P -P"), fetch(mid=" -Pm"), fetch(mid=" -P x"),
        fetch(mid=" \\-P"), fetch(mid=" -\\P"), fetch(mid=' "-P"'), fetch(mid=" --P"), fetch(mid=" -P,"),
        fetch(mid=" -Ｐ"), fetch(mid=" ‑P"),
        f"{UP[:-3]} -m MainStoinker -P fetch-bars {FETCH_ARGS}",
        f"{UP[:-3]} -u -m MainStoinker fetch-bars {FETCH_ARGS}",
        f"{UP[:-3]} -I -m MainStoinker fetch-bars {FETCH_ARGS}",
        # a quoted value whose backslash-stripped form starts with a dash is an option in bash
        fetch(args=f'{FETCH_ARGS} --out "\\-x"'),
        fetch(args=f'{FETCH_ARGS} "\\--port" 7496'),
        fetch(args=f'{FETCH_ARGS} "\\\\--port=7496"'),
        fetch(args=f'{FETCH_ARGS} --out "\\\\-x"'),
    )
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
    # review of PR 165, finding 3: a backslash in an option token (Git Bash drops it, so the
    # real option would be --port / --host while the hook sees another word), and the PowerShell
    # forms that glue a value to an option name
    ok = f"--symbols AAPL {DATES}"
    for odd in ("\\--port 7496", "--po\\rt=7496", "\\--host 8.8.8.8", "--host\\=8.8.8.8", "-\\-port 7496",
                "--port\\ 7496", "--port\\", "\\--port", "\\--port=7497", "--port=\\7496",
                "--ho\\st 8.8.8.8",
                "-\\-host 8.8.8.8", "--port,7496", "--port:7496", "--port.7496", "--port;7496",
                "--host,8.8.8.8", "--host:8.8.8.8", "--host.8.8.8.8", "--port7497", "--host127.0.0.1",
                "--port-7496", "--port,7497", "--port:7497", "--port.7497", "--host:127.0.0.1",
                "--paper,x", "--paper:x", "--paperx", "--paper=1", "--no-prompt,x", "--no-prompt:x",
                "--no-prompt=1", "--no-promptx", "--help", "--help=x", "-h", "--HELP"):
        add(fetch(args=f"{ok} --paper --no-prompt {odd}"), fetch(args=f"{ok} {odd} --paper --no-prompt"))
    for spelled in ("\\--paper", "--pa\\per", "--paper\\", "-\\-paper", "--\\paper", "\\--no-prompt",
                    "--no-pr\\ompt", "--no-prompt\\", "-\\-no-prompt", "--no\\-prompt"):
        # as the required token: it must not count as one; and next to the real one
        add(fetch(args=f"{ok} {spelled} --no-prompt"), fetch(args=f"{ok} --paper {spelled}"),
            fetch(args=f"{ok} --paper --no-prompt {spelled}"))
    add(fetch(args=f"{ok} \\--paper \\--no-prompt"), fetch(args=f"{ok} --pa\\per --no-pr\\ompt"))
    # review of PR 165, finding 4 and 5: what may stand in the interpreter position
    for py in ("PYTHONSTARTUP=x/.venv/Scripts/python.exe", "X=/c/.venv/Scripts/python.exe",
               "X=.venv/Scripts/python.exe", "A=1 .venv/Scripts/python.exe", "--x/.venv/Scripts/python.exe",
               "-x/.venv/Scripts/python.exe", "=/.venv/Scripts/python.exe", "=.venv/Scripts/python.exe",
               "\\\\evil\\share\\.venv\\Scripts\\python.exe", '"\\\\evil\\share\\.venv\\Scripts\\python.exe"',
               "//evil/share/.venv/Scripts/python.exe", '"//evil/share/.venv/Scripts/python.exe"',
               "/c/Windows/System32/.venv/Scripts/python.exe", "/.venv/Scripts/python.exe",
               "\\.venv\\Scripts\\python.exe", "./.venv/Scripts/python.exe", ".\\.venv\\Scripts\\python.exe",
               "..\\.venv\\Scripts\\python.exe", "../.venv/Scripts/python.exe",
               "C:\\a\\..\\.venv\\Scripts\\python.exe", "C:/a/../.venv/Scripts/python.exe",
               "C:\\..\\.venv\\Scripts\\python.exe", "C:\\a\\b:c\\.venv\\Scripts\\python.exe",
               "C:\\a:\\.venv\\Scripts\\python.exe", ".venv\\Scripts\\python.exe:x",
               "C:.venv\\Scripts\\python.exe", "C:\\x=y\\.venv\\Scripts\\python.exe",
               '"C:\\x=y\\.venv\\Scripts\\python.exe"', "sub\\.venv\\Scripts\\python.exe",
               "sub/.venv/Scripts/python.exe", "CC:\\x\\.venv\\Scripts\\python.exe",
               "1:\\x\\.venv\\Scripts\\python.exe", "C:x\\.venv\\Scripts\\python.exe",
               "MainStoinker\\ib\\.venv\\Scripts\\python.exe", "C:\\.venv\\.venv\\Scripts\\python.exe:",
               '"C:\\x\\.venv\\Scripts\\python.exe" "-x"'):
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
    assert text.count("_paper_neutral(") == 2  # its definition and its one call, in the function


def test_the_block_message_teaches_the_allowed_form() -> None:
    reason = hook.decide(f"{UP} -m MainStoinker fetch-bars --port 7497")[1]
    for word in ("--paper", "--no-prompt", ".venv", "7497", "127.0.0.1", "fetch-bars|probe-data", "PAPER"):
        assert word in reason
    # the form shown parses in PowerShell as typed: relative, no quotes around the interpreter
    assert ".venv\\Scripts\\python.exe -P -m MainStoinker fetch-bars|probe-data" in reason
    assert '"<project>' not in reason
    shown = ".venv\\Scripts\\python.exe -P -m MainStoinker fetch-bars --symbols \"AAPL,MSFT\" "
    assert hook.allowed_paper_data_command(shown + "--start 2024-08-16 --end 2024-08-26 --paper --no-prompt")
    assert ENV_NAME not in reason


def test_the_commands_shown_in_claude_md_are_the_allowed_shape() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    block = text.split("## Commands", 1)[1].split("```")[1]
    paper = [ln.split("   #")[0].strip() for ln in block.splitlines() if "--no-prompt" in ln]
    assert len(paper) == 4  # the `run` line of issue 199 is the fourth
    assert any(" fetch-bars " in c for c in paper) and any(" probe-data " in c for c in paper)
    assert any(" -m MainStoinker.ib.watch_bars " in c and " --minutes " in c for c in paper)
    assert any(" -m MainStoinker run " in c and " --minutes " in c for c in paper)
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
        "A paper session is run only by the main session, from the main checkout at an unmodified "
        "`origin/revival` commit",
        "clean `git status` for tracked files, `HEAD` equal to or contained in `origin/revival`",
        "and no untracked `.py` files in the repo root or under `MainStoinker/`",
        "`.venv\\Scripts\\python.exe -P -m MainStoinker fetch-bars|probe-data <args>`",
        "never from a task branch or worktree and never with uncommitted changes",
        "code that connects must have been reviewed and merged before it talks to TWS",
        "The hook cannot check this; the rule does.",
        "(`D[UFI][A-Z]?[0-9]+`): it was confirmed against the owner's paper account in an attended "
        "`--paper` session on 2026-11-23, and it still rests on \"no live Interactive Brokers "
        "account id begins with D\"",
        "`D[UFI][A-Z]?[0-9]+`, confirmed against the owner's paper account on 2026-11-23",
        "Agents may also run the live bar watcher, `python -m MainStoinker.ib.watch_bars`, with "
        "`--paper --no-prompt --minutes N` (N from 1 to 420: a bounded run, data only), under the "
        "same rules (main session only, clean `origin/revival` checkout, one broker session at a "
        "time, exit code 5 means not a paper account: stop, tell the owner)",
        "The hook does not check which code is checked out or which interpreter file it is.",
        "Agents may also run the unattended paper session, `python -m MainStoinker run`, with "
        "`--paper --no-prompt --minutes N` (N from 1 to 420: a bounded run) and `--algos`, `--bars` "
        "and `--db` each given explicitly once, under the same rules (main session only, clean "
        "unmodified `origin/revival` checkout, one broker session at a time, paper only, data from "
        "TWS and simulated fills only: it places no orders; exit code 5 means not a paper account: "
        "stop, tell the owner)",
        "the database path is the owner's: agents use only the path the owner named for the session",
        "Any `--orders` option of `run` (stage 3) is the owner's alone",
    ):
        assert phrase in text, phrase


# -- the property: any forbidden character or wrapper, anywhere, is refused -------------------

FORBIDDEN_CHARS = [
    ";", "&&", "||", "|", "&", "`", "$", "%X%", "$(x)", "${x}", "<", ">", ">>", "2>&1", "(", ")", "{", "}",
    "[", "]", "'", '"', "^", "*", "?", "~", "!", "#", "@", "\n", "\r", "\r\n", "\0", "\\\n", "`\n", "^\n",
    "\\", ",--", ",\\--",
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


@pytest.mark.parametrize("original", PROPERTY_BASES)
def test_a_backslash_inside_any_option_token_is_refused(original: str) -> None:
    tokens = SPLIT.findall(original.strip())
    count = 0
    for i, tok in enumerate(tokens):
        if not tok.startswith("--"):
            continue
        for k in range(1, len(tok)):
            mutated = " ".join([*tokens[:i], tok[:k] + "\\" + tok[k:], *tokens[i + 1 :]])
            assert hook.allowed_paper_data_command(mutated) is False, mutated
            assert not_looser(mutated), mutated
            count += 1
    assert count > 30


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


# =============================================================================================
# The second module form: `<venv python> -P -m MainStoinker.ib.watch_bars <args>` with
# `--paper --no-prompt` and `--minutes N` (the unattended, bounded live bar watcher).
# =============================================================================================

REVIVAL = (REPO_ROOT / "tests" / "fixtures" / "hook_baseline"
           / "block_live_launch_before_watcher_allowance.py.txt")
# sha256 of the frozen copy of the hook as it was on `revival` before the watcher form (LF endings)
REVIVAL_SHA256 = "ba355197dc400960d6f5ac9b0ea1049a4baf48ae822784c3776cd949039d5319"
WATCHER_MODULE = "MainStoinker.ib.watch_bars"
WATCH_ARGS = '--symbols "AAPL,MSFT,TSLA" --minutes 30 --paper --no-prompt'


@functools.cache
def _revival() -> types.ModuleType:
    """The hook as it was on `revival` before the watcher form."""
    data = REVIVAL.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == REVIVAL_SHA256, "the frozen revival hook was edited"
    module = types.ModuleType("block_live_launch_revival")
    module.__file__ = str(REVIVAL)
    exec(compile(data.decode("utf-8"), module.__file__, "exec"), module.__dict__)
    return module


def watch(py: str = UP, args: str = WATCH_ARGS, mid: str = " -P", module: str = WATCHER_MODULE) -> str:
    return f"{py}{mid} -m {module} {args}"


def is_watcher_shape(command: str) -> bool:
    m = hook._paper_match(command)
    return m is not None and m.group("mod") is not None


def only_the_watcher_shape_got_looser(command: str) -> bool:
    """Blocked now, or blocked on `revival` too, or exactly the new shape (and allowed by the rule)."""
    if hook.decide(command)[0] or _revival().decide(command)[0]:
        return True
    return is_watcher_shape(command) and hook.allowed_paper_data_command(command)


ALLOWED_WATCH = [
    watch(),
    watch(UPF),
    watch(QP),
    watch(QPF),
    watch("& " + QP),
    watch("& " + UP),
    watch("&  " + UPF),
    watch('".venv\\Scripts\\python.exe"'),
    watch("C:/Stonks/.venv/Scripts/python.exe"),
    watch('"D:\\work dir\\Stonks\\.venv\\Scripts\\python.exe"'),
    watch(args="--paper --no-prompt --minutes 30 --symbols AAPL"),
    watch(args="--minutes 30 --no-prompt --paper --symbols AAPL"),
    watch(args="--no-prompt --minutes 30 --paper"),
    watch(args="--symbols AAPL,MSFT,TSLA --minutes=390 --paper --no-prompt"),
    watch(args="--paper --no-prompt --minutes=1"),
    watch(args="--paper --no-prompt --minutes 420"),
    watch(args="--paper --no-prompt --minutes 1"),
    watch(args="--paper --no-prompt --minutes 007"),  # the hook takes 1-3 digits: the watcher judges it
    watch(args="--paper --no-prompt --minutes 999"),  # ... and refuses it itself (usage error)
    watch(args="--symbols AAPL MSFT TSLA --minutes 30 --paper --no-prompt"),
    watch(args=f'{WATCH_ARGS} --out "{PROJECT}\\live dir"'),
    watch(args=f'{WATCH_ARGS} --out "C:/Users/x y/Stoinkulator/live"'),
    watch(args=f"{WATCH_ARGS} --out C:\\Users\\x\\live --no-record"),
    watch(args=f'{WATCH_ARGS} --record "C:\\Users\\x y\\rec.jsonl"'),
    watch(args=f"{WATCH_ARGS} --quiet"),
    watch(args=f"{WATCH_ARGS} --port 7497"),
    watch(args=f"{WATCH_ARGS} --port=7497 --host=127.0.0.1"),
    watch(args=f"{WATCH_ARGS} --host 127.0.0.1 --port 7497"),
    watch(args=f"{WATCH_ARGS} --client-id 22 --connect-timeout 30 --backfill-minutes 10"),
    watch(args=f"{WATCH_ARGS} --backfill-minutes=10 --quiet --no-record"),
    watch(args=f'--quiet --minutes 5 --symbols "AAPL" --paper --no-prompt --out "{PROJECT}\\x y"'),
    "  " + watch() + "  ",
    watch() + "\n",
    "\t" + watch(QP) + "\r\n",
]


def refused_watch_table() -> list[str]:
    out: list[str] = []

    def add(*items: str) -> None:
        out.extend(items)

    ok = "--symbols AAPL"
    plain = ".venv\\Scripts\\python.exe"
    UP = f"{plain} -P"  # noqa: N806 - here every literal command carries the mandatory -P
    # -P is mandatory
    for py in (plain, UPF, QP, "& " + QP, QPF):
        add(watch(py, mid=""))
    add(watch(mid=" -p"), watch(mid=" -PP"), watch(mid=" -P -P"), watch(mid=" -u"), watch(mid=' "-P"'),
        watch(mid=" -Ｐ"), f"{plain} -u -m {WATCHER_MODULE} {WATCH_ARGS}",
        f"{plain} -m {WATCHER_MODULE} -P {WATCH_ARGS}", f"{plain} -m {WATCHER_MODULE} {WATCH_ARGS}")
    # the required options
    add(watch(args=f"{ok} --minutes 30 --no-prompt"), watch(args=f"{ok} --minutes 30 --paper"),
        watch(args=f"{ok} --minutes 30"), watch(args=f"{ok} --paper --no-prompt"),
        watch(args="--paper --no-prompt"), watch(args="--minutes 30 --paper"),
        watch(args=f"{ok} --minutes 30 --pape --no-prompt"),
        watch(args=f"{ok} --minutes 30 --paper --no-promp"),
        watch(args=f"{ok} --minutes 30 --paper=1 --no-prompt"),
        watch(args=f"{ok} --minutes 30 --paper --no-prompt=1"),
        watch(args=f'{ok} --minutes 30 "--paper" --no-prompt'),
        watch(args=f'{ok} --minutes 30 --paper "--no-prompt"'),
        watch(args=f"{ok} --minutes 30 --PAPER --no-prompt"))
    # --minutes: absent, malformed, too long, abbreviated, repeated
    for bad in ("1000", "1234", "0000", "abc", "3a", "a3", "-5", "+5", '"30"', '""', "3.5", "0x1F", "30,",
                "3,0", "30;", "--paper", "x", "\\30", "30\\", "٣٠", "３０"):
        add(watch(args=f"{ok} --paper --no-prompt --minutes {bad}"),
            watch(args=f"{ok} --paper --no-prompt --minutes={bad}"))
    add(watch(args=f"{ok} --paper --no-prompt --minutes"),
        watch(args=f"{ok} --paper --no-prompt --minutes="),
        watch(args=f"--minutes {ok} --paper --no-prompt"),
        watch(args=f"{ok} --paper --no-prompt --minutes 30 --minutes 30"),
        watch(args=f"{ok} --paper --no-prompt --minutes 30 --minutes 5000"),
        watch(args=f"{ok} --paper --no-prompt --minutes 5000 --minutes 30"),
        watch(args=f"{ok} --paper --no-prompt --minutes=30 --minutes 30"),
        watch(args=f'{ok} --paper --no-prompt "--minutes" 30'),
        watch(args=f'{ok} --paper --no-prompt "--minutes=30"'),
        watch(args=f"{ok} --paper --no-prompt --minute 30"),
        watch(args=f"{ok} --paper --no-prompt --minut 30"),
        watch(args=f"{ok} --paper --no-prompt --min 30"),
        watch(args=f"{ok} --paper --no-prompt --mi 30"),
        watch(args=f"{ok} --paper --no-prompt --m 30"),
        watch(args=f"{ok} --paper --no-prompt --MINUTES 30"),
        watch(args=f"{ok} --paper --no-prompt --Minutes=30"),
        watch(args=f"{ok} --paper --no-prompt --minutes,5"),
        watch(args=f"{ok} --paper --no-prompt --minutes:5"),
        watch(args=f"{ok} --paper --no-prompt --minutes.5"),
        watch(args=f"{ok} --paper --no-prompt --minutes30"),
        watch(args=f"{ok} --paper --no-prompt --minutesx 30"),
        watch(args=f"{ok} --paper --no-prompt --minutes\\ 30"),
        watch(args=f"{ok} --paper --no-prompt --min\\utes 30"),
        watch(args=f"{ok} --paper --no-prompt \\--minutes 30"),
        watch(args=f"{ok} --paper --no-prompt -minutes 30"),
        watch(args=f"{ok} --paper --no-prompt --backfill-minutes 10"),
        watch(args=f"{ok} --paper --no-prompt --backfill-minutes=30"),
        watch(args=f"{ok} --paper --no-prompt --minutes 30 --minutes,5"))
    # every other module of the package (and spellings of this one); a module that is not the
    # package at all is not for this rule to judge: see `test_other_modules_keep_their_verdict`
    # every other module (and spellings of this one)
    for module in ("MainStoinker.ib.cli", "MainStoinker.ib.probe", "MainStoinker.ib", "MainStoinker.ib.",
                   "MainStoinker.ib.watch_bars.x", "MainStoinker.ib.watch_bars.",
                   "MainStoinker.ib.watch_bars..", "MainStoinker.IB.watch_bars",
                   "MainStoinker.ib.Watch_Bars", "MainStoinker.ib.WATCH_BARS",
                   "mainstoinker.ib.watch_bars", "MAINSTOINKER.IB.WATCH_BARS", "MainStoinker.ib.watch-bars",
                   "MainStoinker.ib.watch_bar", "MainStoinker.ib.watch_bars2", "MainStoinker.ib.watch_barsx",
                   "MainStoinker.ib.recorder", "MainStoinker.ib.connection", "MainStoinker.ib.client",
                   "MainStoinker.ib.history", "MainStoinker.ib.market_data", "MainStoinker.engine.cli",
                   "MainStoinker.__main__", "MainStoinker.testy", "MainStoinker..ib.watch_bars",
                   "MainStoinker.ib..watch_bars", "MainStoinker.ib/watch_bars", "MainStoinker/ib/watch_bars",
                   "MainStoinker.ib.watch_bars.watch_bars", "MainStoinker .ib.watch_bars"):
        add(watch(module=module))
    add(f"{UP} -mMainStoinker.ib.watch_bars {WATCH_ARGS}",
        f"{UP} --module {WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} --module={WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} -m {WATCHER_MODULE} -m {WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} -m MainStoinker -m {WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} -m {WATCHER_MODULE} -m MainStoinker.ib.cli {WATCH_ARGS}",
        f"{UP} {WATCH_ARGS} -m {WATCHER_MODULE}",
        f"{UP} -m {WATCHER_MODULE}",
        f"{UP} -m {WATCHER_MODULE} ",
        f"{UP} MainStoinker/ib/watch_bars.py {WATCH_ARGS}",
        f"{UP} MainStoinker\\ib\\watch_bars.py {WATCH_ARGS}",
        f"{UP} ./MainStoinker/ib/watch_bars.py {WATCH_ARGS}",
        f'{UP} -c "import MainStoinker.ib.watch_bars" {WATCH_ARGS}',
        f'{UP} -c "from MainStoinker.ib import watch_bars; watch_bars.main()" {WATCH_ARGS}',
        f"{UP} -c x -m {WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} -m {WATCHER_MODULE} -c x {WATCH_ARGS}",
        f"{UP} -m runpy {WATCHER_MODULE} {WATCH_ARGS}")
    # the subcommand shapes mixed with the module form
    add(f"{UP} -m MainStoinker watch_bars {WATCH_ARGS}",
        f"{UP} -m MainStoinker watch-bars {WATCH_ARGS}",
        f"{UP} -m MainStoinker watch {WATCH_ARGS}",
        f"{UP} -m MainStoinker.ib.watch_bars.fetch-bars {WATCH_ARGS}",
        f"{UP} -m MainStoinker fetch-bars.ib.watch_bars {WATCH_ARGS}",
        f"{UP} -m MainStoinker fetch-bars -m {WATCHER_MODULE} {WATCH_ARGS}",
        f"{UP} -m MainStoinker.ib.watch_bars -m MainStoinker fetch-bars {WATCH_ARGS}")
    # ports and hosts
    for bad in ("7496", "4001", "4002", "74970", "07497", "0", "", "7498"):
        add(watch(args=f"{WATCH_ARGS} --port {bad}"))
    add(watch(args=f"{WATCH_ARGS} --port=7496"), watch(args=f"{WATCH_ARGS} --port=4002"),
        watch(args=f"{WATCH_ARGS} --port=4001"),
        watch(args=f"{WATCH_ARGS} --port 7497 --port 7496"),
        watch(args=f"{WATCH_ARGS} --port 7496 --port 7497"),
        watch(args=f"{WATCH_ARGS} --por 7497"), watch(args=f"{WATCH_ARGS} --po 7496"),
        watch(args=f"{WATCH_ARGS} --host localhost"), watch(args=f"{WATCH_ARGS} --host 127.0.0.2"),
        watch(args=f"{WATCH_ARGS} --host 8.8.8.8"), watch(args=f"{WATCH_ARGS} --host=localhost"),
        watch(args=f"{WATCH_ARGS} --host 0.0.0.0"), watch(args=f"{WATCH_ARGS} --host"),
        watch(args=f"{WATCH_ARGS} --hos 127.0.0.1"), watch(args=f"{WATCH_ARGS} --port,7496"),
        watch(args=f"{WATCH_ARGS} --host:8.8.8.8"), watch(args=f"{WATCH_ARGS} --port\\=7496"),
        watch(args=f"{WATCH_ARGS} --help"), watch(args=f"{WATCH_ARGS} -h"),
        watch(args=f"{WATCH_ARGS} --HELP"),
        watch(args=f"{WATCH_ARGS} -x"), watch(args=f"{WATCH_ARGS} --"))
    # other interpreters
    for py in ("python", "python.exe", "py", "uv run python", "pythonw", ".venv\\Scripts\\pythonw.exe",
               "venv\\Scripts\\python.exe", "'.venv/Scripts/python.exe'",
               "\\\\evil\\share\\.venv\\Scripts\\python.exe", "../.venv/Scripts/python.exe",
               "X=1 .venv/Scripts/python.exe", "C:\\a\\..\\.venv\\Scripts\\python.exe",
               "call .venv\\Scripts\\python.exe", "start .venv\\Scripts\\python.exe"):
        add(watch(py))
    # other reasons to block, hidden in a value the shape tolerates
    add(watch(args=f'{WATCH_ARGS} --out "{ENTRY}"'),
        watch(args=f'{WATCH_ARGS} --out "C:\\x\\Stonks data collect.bat"'),
        watch(args=f'{WATCH_ARGS} --out "C:\\x\\MainStoinker\\ib\\live"'),
        watch(args=f'{WATCH_ARGS} --out "C:\\x -m MainStoinker.ib.cli"'),
        watch(args=f'{WATCH_ARGS} --out "C:\\x -c y"'),
        watch(args=f'{WATCH_ARGS} --out "C:\\x\\"'),
        watch(args=f'{WATCH_ARGS} "MainStoinker/testy/testy.py"'),
        f"{UP} -m MainStoinker.testy.x {WATCH_ARGS}")
    # the live opt-in name anywhere
    base = watch()
    for name in (ENV_NAME, ENV_NAME.lower(), "ALLOW_LIVE_PORT"):
        add(f"{name}=1 {base}", f"$env:{name}='1'; {base}", f"set {name}=1 && {base}",
            f"export {name}=1; {base}", f"env {name}=1 {base}", f"{base} --out {name}",
            f'{base} --out "x {name}"', f"{base} {name}=1", f"{base} --symbols AAPL,{name}",
            f"{UP} -m {WATCHER_MODULE} {WATCH_ARGS} --record C:\\{name}\\r.jsonl")
    # separators, wrappers, prefixes
    for suffix in ("; echo x", " && echo x", " || echo x", " | more", " & echo x", " > out.txt", " 2>&1",
                   " < in.txt", " `", " $x", " $(x)", " %X%", " ^", " (x)", " {x}", " 'x'", " [x]", " *",
                   " ?", " #c", " @x", "\necho x", "\r\necho x", " \\\n", " --out\\\n", " \\", ";", "&"):
        add(watch() + suffix)
    for prefix in ("cd x; ", "cd x && ", "cd x\n", "Set-Location x; ", "VAR=x ", "$env:A=1; ", "env ",
                   "start ", "cmd /c ", "powershell -c ", "bash -c ", "sh -c ", "call ", "time ", "nohup ",
                   "timeout 5 ", "xargs ", "sudo ", "echo hi | ", "echo hi; ", "Start-Process ", "iex ",
                   "wsl ", "exec ", "command ", "& & ", ". ", "source ", "(", "{ ", "@(", "$(",
                   "`", "!", "echo x &&"):
        add(prefix + watch())
    for wrapper in ('cmd /c "{c}"', "cmd /c '{c}'", 'powershell -c "{c}"', 'bash -c "{c}"',
                    'echo "{c}" | bash', '"{c}"', "'{c}'", "$({c})", "`{c}`", "({c})", "{{ {c} }}",
                    "& {{ {c} }}", "start '{c}'", "eval '{c}'", "<<EOF\n{c}\nEOF", "bash <<< '{c}'"):
        add(wrapper.format(c=watch()))
    # a second command next to it, and the older shapes with the watcher module as an argument
    add(watch() + f'\n{UP} "{ENTRY}"', watch() + "\n" + watch(), watch() + "\n" + fetch(),
        fetch() + "\n" + watch(), f'{watch()}; "Stonks data collect.bat"',
        watch() + ' & python "MainStoinker/testy/testy.py"',
        fetch(args=f"{FETCH_ARGS} -m {WATCHER_MODULE}"), probe(args=f"{PROBE_ARGS} -m {WATCHER_MODULE}"))
    # look-alikes and continuation lines
    add(watch().replace("watch_bars", "watch\u2011bars"), watch().replace("watch_bars", "w\u0430tch_bars"),
        watch().replace("MainStoinker", "Main\u200bStoinker"),
        watch().replace("--minutes", "--min\u2011utes"),
        watch().replace("--minutes", "\uff0d\uff0dminutes"), watch().replace("30", "\uff13\uff10"),
        watch().replace("--paper", "\uff0d\uff0dpaper"), watch().replace(" ", "\u00a0", 1),
        watch() + " \\\n", watch() + " `\necho x", watch(args=WATCH_ARGS + " \\\n--port 7496"),
        watch(args="--paper \\\n--no-prompt --minutes 30"),
        watch(args="--paper --no-prompt --minutes \\\n30"))
    return [c for c in dict.fromkeys(out) if c not in ALLOWED_WATCH]


REFUSED_WATCH = refused_watch_table()


def test_the_watcher_tables_are_big_enough() -> None:
    assert len(set(ALLOWED_WATCH)) >= 20
    assert len(set(REFUSED_WATCH)) >= 60
    assert not set(ALLOWED_WATCH) & set(REFUSED_WATCH)
    assert not set(ALLOWED_WATCH) & set(ALLOWED) and not set(REFUSED_WATCH) & set(ALLOWED)


@pytest.mark.parametrize("command", ALLOWED_WATCH)
def test_allowed_watcher_spellings(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is True
    assert hook.decide(command) == (False, "")
    assert hook.main(json.dumps({"tool_name": "PowerShell", "tool_input": {"command": command}})) == 0
    assert is_watcher_shape(command)
    assert _revival().decide(command)[0] is True  # blocked before: the new shape is the only change


@pytest.mark.parametrize("command", REFUSED_WATCH)
def test_refused_watcher_variants(command: str) -> None:
    assert hook.decide(command)[0] is True, command
    assert hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) == 2
    assert hook.allowed_paper_data_command(command) is False, command
    assert only_the_watcher_shape_got_looser(command)


@pytest.mark.parametrize("command", REFUSED + REFUSED_WATCH + ALLOWED + ALLOWED_WATCH)
def test_only_the_new_shape_is_looser_than_revival(command: str) -> None:
    now, before = hook.decide(command)[0], _revival().decide(command)[0]
    if before is False:
        assert now is False  # the old verdicts stay as they were
    if now is False and before is True:
        assert is_watcher_shape(command) and hook.allowed_paper_data_command(command)


@pytest.mark.parametrize("module", ["Other.ib.watch_bars", "xMainStoinker.ib.watch_bars", ".ib.watch_bars",
                                    "ib.watch_bars"])
def test_other_modules_are_not_the_rule_and_keep_their_verdict(module: str) -> None:
    command = watch(module=module)
    assert hook.allowed_paper_data_command(command) is False
    assert hook.decide(command) == _revival().decide(command)


def test_the_module_without_minutes_stays_refused_and_with_it_is_allowed() -> None:
    row = f"{UP} -P -m {WATCHER_MODULE} --paper --no-prompt"  # the row of REFUSED, unchanged
    assert row in REFUSED and hook.decide(row)[0] is True
    assert hook.decide(row + " --minutes 30") == (False, "")
    assert hook.decide(row + " --minutes 30 --minutes 31")[0] is True


def test_the_watcher_module_with_a_subcommand_word_is_plain_arguments() -> None:
    # `-m MainStoinker.ib.watch_bars fetch-bars ...`: `fetch-bars` is one more plain argument. The
    # hook allows it (the shape is exact and the neutral text is harmless); the watcher has no
    # positional argument, so it ends as a usage error (exit 2) before anything is built.
    command = watch(args=f"fetch-bars {WATCH_ARGS}")
    assert hook.decide(command) == (False, "")
    match = hook._paper_match(command)
    assert match is not None and match.group("mod") == ".ib.watch_bars" and match.group("sub") is None
    # and `-m MainStoinker watch_bars ...` is no shape at all
    assert hook.decide(f"{UP} -P -m MainStoinker watch_bars {WATCH_ARGS}")[0] is True


def test_the_module_name_is_exact_and_case_sensitive() -> None:
    for module in ("MainStoinker.ib.cli", "MainStoinker.ib.probe", "MainStoinker.ib",
                   "mainstoinker.ib.watch_bars", "MainStoinker.IB.watch_bars",
                   "MainStoinker.ib.watch_bars.x", "MainStoinker.ib.watch_bars."):
        assert hook._paper_match(watch(module=module)) is None, module
        assert hook._paper_neutral(watch(module=module)) is None, module


def test_the_neutral_text_of_the_module_form_is_the_offline_subcommand() -> None:
    for command in (ALLOWED_WATCH[0], ALLOWED_WATCH[4], ALLOWED_WATCH[9], "  " + ALLOWED_WATCH[2] + "\n"):
        neutral = hook._paper_neutral(command)
        assert neutral == command.replace(f"-m {WATCHER_MODULE} ", "-m MainStoinker backtest ", 1)
        assert hook._decide(neutral, 1) == (False, "")
        # harmless only for that reason: the same text with the module kept is blocked
        assert hook._decide(command, 1)[0] is True
    assert hook._paper_neutral(REFUSED_WATCH[0]) is None


def test_other_blocks_still_apply_to_the_allowed_watcher_shape() -> None:
    for tail in (f' --out "{ENTRY}"', ' --out "C:\\x\\Stonks data collect.bat"',
                 ' --out "C:\\x\\MainStoinker\\ib\\bars"', ' "MainStoinker/testy/testy.py"',
                 ' --out "C:\\x -m MainStoinker.ib.cli"'):
        command = ALLOWED_WATCH[0] + tail
        assert hook.decide(command)[0] is True
        assert hook.allowed_paper_data_command(command) is False


def test_the_allowance_is_still_off_for_decoded_and_folded_watcher_text() -> None:
    import base64

    encoded = base64.b64encode(ALLOWED_WATCH[0].encode("utf-16-le")).decode()
    assert hook.decide(f"powershell -EncodedCommand {encoded}")[0] is True
    assert hook.decide(ALLOWED_WATCH[0].replace("--paper", "\uff0d\uff0dpaper"))[0] is True


def test_the_function_is_still_called_in_one_place_and_the_message_names_the_watcher() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert len(re.findall(r"allowed_paper_data_command\(", text)) == 2
    assert text.count("_paper_neutral(") == 2
    reason = hook.decide(f"{UP} -P -m {WATCHER_MODULE} --port 7497")[1]
    for word in ("-P -m MainStoinker.ib.watch_bars", "--minutes", "--paper", "--no-prompt", "7497"):
        assert word in reason
    assert ENV_NAME not in reason
    assert ".venv\\Scripts\\python.exe -P -m MainStoinker fetch-bars|probe-data" in reason


WATCH_PROPERTY_BASES = [ALLOWED_WATCH[0], ALLOWED_WATCH[4], ALLOWED_WATCH[13], ALLOWED_WATCH[20]]


@pytest.mark.parametrize("original", WATCH_PROPERTY_BASES)
def test_any_forbidden_character_or_wrapper_anywhere_is_refused_for_the_watcher(original: str) -> None:
    variants = _variants(original)
    assert len(variants) > 500
    for label, mutated in variants:
        if _benign(label, mutated, original):
            assert hook.allowed_paper_data_command(mutated), label
            continue
        assert hook.allowed_paper_data_command(mutated) is False, (label, mutated)
        # not looser than `revival`: blocked now, or `revival` let it through too (a split such as
        # `-m ; MainStoinker.ib.watch_bars` runs no module at all)
        assert hook.decide(mutated)[0] or not _revival().decide(mutated)[0], (label, mutated)


@pytest.mark.parametrize("original", WATCH_PROPERTY_BASES)
def test_a_backslash_inside_any_option_token_is_refused_for_the_watcher(original: str) -> None:
    tokens = SPLIT.findall(original.strip())
    count = 0
    for i, tok in enumerate(tokens):
        if not tok.startswith("--"):
            continue
        for k in range(1, len(tok)):
            mutated = " ".join([*tokens[:i], tok[:k] + "\\" + tok[k:], *tokens[i + 1 :]])
            assert hook.allowed_paper_data_command(mutated) is False, mutated
            assert hook.decide(mutated)[0] or not _revival().decide(mutated)[0], mutated
            count += 1
    assert count > 30


def test_every_prefix_of_a_guarded_option_name_is_refused_for_the_watcher() -> None:
    # no abbreviation of a guarded option (the watcher turns abbreviations off too)
    count = 0
    for name in ("--paper", "--no-prompt", "--minutes", "--port", "--host"):
        for k in range(3, len(name)):
            short = name[:k]
            for command in (watch(args=f"{WATCH_ARGS} {short} 7497"), watch(args=f"{WATCH_ARGS} {short}=1")):
                assert hook.allowed_paper_data_command(command) is False, command
                count += 1
    assert count > 15


# =============================================================================================
# The third subcommand word: `<venv python> -P -m MainStoinker run <args>` (issue 199, the
# unattended `run` on the paper account). `--paper --no-prompt`, `--minutes` once (1 to 3 digits),
# `--algos`, `--bars` and `--db` once each with an explicit value, `--fills` only `realistic`,
# no `--help`, and nothing that is or abbreviates `--orders`.
# =============================================================================================

BEFORE_RUN = (REPO_ROOT / "tests" / "fixtures" / "hook_baseline"
              / "block_live_launch_before_run_allowance.py.txt")
# sha256 of the frozen copy of the hook as it was before the `run` allowance (LF line endings)
BEFORE_RUN_SHA256 = "e944e43ec8db57cbedb7c6acfe5bd31dc26ea22d40d14623106f6755d2cb6ede"


@functools.cache
def _before_run() -> types.ModuleType:
    """The hook as it was before the `run` allowance."""
    data = BEFORE_RUN.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == BEFORE_RUN_SHA256, "the frozen pre-run hook was edited"
    module = types.ModuleType("block_live_launch_before_run")
    module.__file__ = str(BEFORE_RUN)
    exec(compile(data.decode("utf-8"), module.__file__, "exec"), module.__dict__)
    return module


RUN_BASE = "--algos C:/s/algos.json --bars C:/s/bars --db C:/s/runs.db"
RUN_ARGS = f"{RUN_BASE} --minutes 415 --paper --no-prompt"


def run(py: str = UP, args: str = RUN_ARGS, mid: str = " -P", sub: str = "run") -> str:
    return f"{py}{mid} {MODULE} {sub} {args}"


def is_run_shape(command: str) -> bool:
    m = hook._paper_match(command)
    return m is not None and m.group("sub") == "run"


def only_the_run_shape_got_looser(command: str) -> bool:
    """Blocked now, or blocked before the `run` allowance too, or exactly the new shape."""
    if hook.decide(command)[0] or _before_run().decide(command)[0]:
        return True
    return is_run_shape(command) and hook.allowed_paper_data_command(command)


ALLOWED_RUN = [
    run(),
    run(UPF),
    run(QP),
    run(QPF),
    run("& " + QP),
    run("& " + UP),
    run('".venv\\Scripts\\python.exe"'),
    run("C:/Stonks/.venv/Scripts/python.exe"),
    run('"D:\\work dir\\Stonks\\.venv\\Scripts\\python.exe"'),
    run(args="--paper --no-prompt --minutes 415 --db C:/s/runs.db --bars C:/s/bars --algos C:/s/a.json"),
    run(args=f"{RUN_BASE} --paper --no-prompt --minutes 1"),
    run(args=f"{RUN_BASE} --paper --no-prompt --minutes 420"),
    run(args=f"{RUN_BASE} --paper --no-prompt --minutes=30"),
    run(args=f"{RUN_BASE} --paper --no-prompt --minutes 007"),
    run(args="--algos=C:/s/a.json --bars=C:/s/bars --db=C:/s/runs.db --minutes 9 --paper --no-prompt"),
    run(args="--db sqlite:///C:/s/runs.db --algos C:/s/a.json --bars C:/s/bars "
             "--minutes 9 --paper --no-prompt"),
    run(args=f"{RUN_ARGS} --fills realistic"),
    run(args=f"{RUN_ARGS} --fills=realistic"),
    run(args='--algos "C:/my dir/a.json" --bars "C:/my dir/bars" --db "C:/my dir/r.db" '
             "--minutes 5 --paper --no-prompt"),
    run(args=f"{RUN_ARGS} --stop-file C:/s/stop.flag --fills realistic"),
    run(args=f"{RUN_ARGS} --stop-file=C:/s/stop.flag"),
    run(args="--algos C:\\s\\a.json --bars C:\\s\\bars --db C:\\s\\r.db --minutes 5 --paper --no-prompt"),
    "  " + run() + "\n",
]


def refused_run_table() -> list[str]:
    out: list[str] = []

    def add(*items: str) -> None:
        out.extend(items)

    def a(args: str, **kw: str) -> str:
        return run(args=args, **kw)

    # the interpreter and the module
    for py in ("python", "python.exe", "py", "py -3", "uv run python", "pipx run", "pythonw",
               ".venv\\Scripts\\pythonw.exe", "venv\\Scripts\\python.exe", "..\\.venv\\Scripts\\python.exe",
               "\\\\srv\\x\\.venv\\Scripts\\python.exe", "/c/x/.venv/Scripts/python.exe",
               "C:/x/../.venv/Scripts/python.exe", "VAR=1 .venv\\Scripts\\python.exe"):
        add(run(py))
    add(
        f"{UP} -m MainStoinker run {RUN_ARGS}",  # no -P
        run(mid=" -P -P"), run(mid=" -u"), run(mid=" -I"), run(mid=" -P -I"),
        f"{UP} -P -mMainStoinker run {RUN_ARGS}",
        f"{UP} -P --module MainStoinker run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker.ib.run_cli {RUN_ARGS}",
        f"{UP} -P -m MainStoinker.ib.live_run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker.engine.cli run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker.__main__ run {RUN_ARGS}",
        f"{UP} -P -m mainstoinker run {RUN_ARGS}",
        f"{UP} -P -m MAINSTOINKER run {RUN_ARGS}",
        f'{UP} -P -c "import MainStoinker.ib.run_cli" {RUN_ARGS}',
        f"{UP} -P -c print(1) -m MainStoinker run {RUN_ARGS}",
        f"{UP} -P MainStoinker/ib/run_cli.py {RUN_ARGS}",
        f"{UP} -P MainStoinker\\ib\\run_cli.py {RUN_ARGS}",
        f"{UP} -P MainStoinker/__main__.py run {RUN_ARGS}",
        f"{UP} -P MainStoinker run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker run {RUN_ARGS} -m MainStoinker.ib.run_cli",
        f"{UP} -P -m MainStoinker run {RUN_ARGS} -c x",
    )
    # the subcommand word: exact, case-sensitive, nothing glued, quoted or escaped
    for sub in ("Run", "RUN", "run2", "run-", "r\\un", "ru n", '"run"', "'run'", "`run`", "run\u200b",
                "\u0440un", "run.", ".run", "run,", "run;", "rn", "ru", "runn", "live", "paper", "watch"):
        add(run(sub=sub))
    add(
        f"{UP} -P -m MainStoinker {RUN_ARGS}",
        f"{UP} -P -m MainStoinker run backtest {RUN_ARGS}",
        f"{UP} -P -m MainStoinker run\t{RUN_ARGS}",
        f"{UP} -P -m MainStoinker\trun {RUN_ARGS}",
        f"{UP} -P -m MainStoinker\u00a0run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker run\u00a0{RUN_ARGS}",
        f"{UP} -P\t-m MainStoinker run {RUN_ARGS}",
        f"{UP} -P -m MainStoinker run",
    )
    # the flags
    add(
        a(f"{RUN_BASE} --minutes 415 --no-prompt"),
        a(f"{RUN_BASE} --minutes 415 --paper"),
        a(f"{RUN_BASE} --minutes 415"),
        a(f"{RUN_BASE} --minutes 415 --paper=1 --no-prompt"),
        a(f"{RUN_BASE} --minutes 415 --paper --no-prompt=1"),
        a(f"{RUN_BASE} --minutes 415 --pape --no-prompt"),
        a(f"{RUN_BASE} --minutes 415 --paper --no-p"),
        a(f"{RUN_BASE} --minutes 415 --no-prompt --pap"),
        a(f"{RUN_BASE} --minutes 415 --PAPER --no-prompt"),
        a(f'{RUN_BASE} --minutes 415 "--paper" --no-prompt'),
        a(f"{RUN_BASE} --minutes 415 -paper --no-prompt"),
        a(f"{RUN_BASE} --minutes 415 --paper --no-prompt --paper=false"),
    )
    # --minutes: exactly once, 1 to 3 plain digits
    add(
        a(f"{RUN_BASE} --paper --no-prompt"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes 30 --minutes 31"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes=30 --minutes 30"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes=30 --minutes=30"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes 1000"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes 0001"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes -5"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes +5"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes 3x"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes 3.0"),
        a(f'{RUN_BASE} --paper --no-prompt --minutes "30"'),
        a(f"{RUN_BASE} --paper --no-prompt --minutes="),
        a(f"{RUN_BASE} --paper --no-prompt --minute 30"),
        a(f"{RUN_BASE} --paper --no-prompt --min 30"),
        a(f"{RUN_BASE} --paper --no-prompt --m 30"),
        a(f"{RUN_BASE} --paper --no-prompt --MINUTES 30"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes,30"),
        a(f"{RUN_BASE} --paper --no-prompt --minutesx 30"),
        a(f"{RUN_BASE} --paper --no-prompt --minutes:30"),
        a(f"{RUN_BASE} --paper --no-prompt -minutes 30"),
    )
    # --algos, --bars, --db: each exactly once, explicit, with a value
    for name in ("--algos", "--bars", "--db"):
        rest = " ".join(p for p in ("--algos C:/s/a.json", "--bars C:/s/bars", "--db C:/s/r.db")
                        if not p.startswith(name))
        flags = "--minutes 30 --paper --no-prompt"
        add(
            a(f"{rest} {flags}"),  # missing: no reliance on a default (STOINK_DB and the like)
            a(f"{RUN_BASE} {flags} {name} C:/s/y"),  # given twice
            a(f"{RUN_BASE} {flags} {name}=C:/s/y"),
            a(f"{rest} {flags} {name}"),  # no value at all
            a(f"{rest} {flags} {name}="),
            a(f"{rest} {flags} {name} --fills"),
            a(f"{rest} {flags} {name} --minutes"),
            a(f'{rest} {flags} "{name}" C:/s/x'),
            a(f"{rest} {flags} {name.upper()} C:/s/x"),
            a(f"{rest} {flags} {name}x C:/s/x"),
            a(f"{rest} {flags} {name[:-1]} C:/s/x"),
        )
    add(
        a("--algos --bars C:/s/b --db C:/s/r.db --minutes 30 --paper --no-prompt"),
        a("--algos C:/s/a.json --bars --db C:/s/r.db --minutes 30 --paper --no-prompt"),
        a("--algos C:/s/a.json --bars C:/s/b --db --minutes 30 --paper --no-prompt"),
        a("--algos C:/s/a.json --bars C:/s/b --db -x --minutes 30 --paper --no-prompt"),
        a("--alg C:/s/a.json --bars C:/s/b --db C:/s/r.db --minutes 30 --paper --no-prompt"),
        a("--algos C:/s/a.json --bar C:/s/b --db C:/s/r.db --minutes 30 --paper --no-prompt"),
        a("--algos C:/s/a.json --bars C:/s/b --d C:/s/r.db --minutes 30 --paper --no-prompt"),
    )
    # --port and --host
    for bad in ("7496", "4002", "4001", "74970", "0", "", "7498", "7497.0", "7497,7496"):
        add(a(f"{RUN_ARGS} --port {bad}".rstrip()))
    add(
        a(f"{RUN_ARGS} --port 7497 --port 7496"),
        a(f"{RUN_ARGS} --port=7496"),
        a(f"{RUN_ARGS} --port=4002"),
        a(f"{RUN_ARGS} --port"),
        a(f"{RUN_ARGS} --po 7497"),
        a(f"{RUN_ARGS} --PORT 7497"),
        a(f"{RUN_ARGS} --host localhost"),
        a(f"{RUN_ARGS} --host 127.0.0.2"),
        a(f"{RUN_ARGS} --host=localhost"),
        a(f"{RUN_ARGS} --host"),
        a(f"{RUN_ARGS} --hos 127.0.0.1"),
        a(f"{RUN_ARGS} --host 127.0.0.1 --host localhost"),
    )
    # --fills: only `realistic`, only once
    add(
        a(f"{RUN_ARGS} --fills legacy"),
        a(f"{RUN_ARGS} --fills=legacy"),
        a(f"{RUN_ARGS} --fills"),
        a(f"{RUN_ARGS} --fills="),
        a(f"{RUN_ARGS} --fills Realistic"),
        a(f"{RUN_ARGS} --fills realistic --fills realistic"),
        a(f"{RUN_ARGS} --fills realistic --fills legacy"),
        a(f'{RUN_ARGS} --fills "realistic"'),
        a(f"{RUN_ARGS} --fill realistic"),
        a(f"{RUN_ARGS} --fil realistic"),
        a(f"{RUN_ARGS} --f realistic"),
        a(f"{RUN_ARGS} --fillsx realistic"),
        a(f"{RUN_ARGS} --FILLS realistic"),
    )
    # --orders (stage 3): never, in any spelling; --help and -h
    for opt in ("--orders", "--orders=on", "--orders on", "--orders paper", "--orders live", "--orders=",
                "--order", "--order=1", "--order paper", "--ORDERS", "--Orders=1", "--orde", "--ord", "--or",
                "--o", "--orders-paper", "--orders_paper", "--ordersx", "--orders,on", "--orders:on",
                '"--orders"', '"--orders=1"', "--orders\\ x", "--ord\\ers", "-orders", "-o", "--"):
        add(a(f"{RUN_ARGS} {opt}"), a(f"{opt} {RUN_ARGS}"))
    add(
        a(f"{RUN_ARGS} --out C:/x/--orders"),  # also as a value
        a(f"{RUN_ARGS} --stop-file --orders"),
        a(f"{RUN_ARGS} --db=--orders"),
        a(f"{RUN_ARGS} --help"),
        a(f"{RUN_ARGS} -h"),
        a(f"{RUN_ARGS} --hel"),
        a(f"{RUN_ARGS} --h"),
        a(f"{RUN_ARGS} --HELP"),
        a(f"{RUN_ARGS} --help=1"),
        a("--help"),
        a("-h"),
    )
    # issue 292: these were ALLOWED before the option allowlist (the owner can add one back)
    add(
        a(f"{RUN_ARGS} --port 7497"),
        a(f"{RUN_ARGS} --port=7497 --host 127.0.0.1"),
        a(f"{RUN_ARGS} --host=127.0.0.1"),
        a(f"{RUN_ARGS} --out C:/s/live --stop-file C:/s/stop.flag --warmup-days 3 --no-record"),
        a(f"{RUN_ARGS} --record C:/s/rec.jsonl --client-id 20 --connect-timeout 30"),
        a(f"{RUN_ARGS} --capital 100000 --commission-per-share 0.005 --slippage-per-share 0.01"),
    )
    # shell syntax, wrappers, the live opt-in name
    for tail in (" ; echo", " && echo", " || echo", " | cat", " & echo", " > out.txt", " 2>&1", " `x`",
                 " $(x)", " $x", " %x%", " (x)", " {x}", " [x]", " @x", " *", " ?", " ~", " !", " #x", "\nx",
                 " \\\nx",
                 " ^\nx", " <x", "\t&& x"):
        add(run() + tail)
    for head in ("cd x;", "cd x &&", "Set-Location x;", "VAR=x", "A=1 B=2", "$env:A=1;", "set A=1&&", "start",
                 "cmd /c", "powershell -c", "pwsh -c", "bash -c", "sh -c", "call", "env", "nohup", "time",
                 "timeout 5", "xargs", "sudo", "Start-Process", "iex", "wsl", "exec", "command"):
        add(f"{head} {run()}")
    add(
        f"{ENV_NAME}=1 {run()}",
        f"$env:{ENV_NAME}=1; {run()}",
        f"set {ENV_NAME}=1&& {run()}",
        run(args=f"{RUN_ARGS} --out C:/s/{ENV_NAME}"),
        run(args=f"{RUN_ARGS} {ENV_NAME.lower()}"),
        run(args=f"{RUN_ARGS} {ENV_NAME}=1"),
        run(args=f'{RUN_ARGS} --out "C:/s/{ENV_NAME.title()}"'),
    )
    # chaining after, or before, an allowed command; another connecting subcommand next to it
    add(
        run() + " ; " + run(),
        run() + " && " + fetch(),
        fetch() + " && " + run(),
        run() + " | " + run(),
        run() + " & " + run(),
        run() + " ; python -m MainStoinker.ib.run_cli " + RUN_ARGS,
        run() + " fetch-bars",
        run() + " -m MainStoinker.ib.watch_bars",
        run(sub="run backtest"),
        run(args=f'{RUN_ARGS} --out "{ENTRY}"'),
        run(args=f'{RUN_ARGS} --out "C:\\x\\Stonks data collect.bat"'),
        run(args=f'{RUN_ARGS} --out "C:\\x\\MainStoinker\\ib\\bars"'),
        run(args=f'{RUN_ARGS} "MainStoinker/testy/testy.py"'),
        run(args=f'{RUN_ARGS} --out "C:\\x -m MainStoinker.ib.cli"'),
        run(args=f'{RUN_ARGS} --out "a --orders b"'),
        run(args=f'{RUN_ARGS} --out "a -x b"'),
    )
    return [c for c in dict.fromkeys(out) if c not in ALLOWED_RUN]


REFUSED_RUN = refused_run_table()


def test_the_run_tables_are_big_enough() -> None:
    assert len(set(ALLOWED_RUN)) >= 20
    assert len(set(REFUSED_RUN)) >= 40
    assert not set(ALLOWED_RUN) & set(REFUSED_RUN)
    assert not set(ALLOWED_RUN) & set(ALLOWED + ALLOWED_WATCH + REFUSED + REFUSED_WATCH)
    assert not set(REFUSED_RUN) & set(ALLOWED + ALLOWED_WATCH)


@pytest.mark.parametrize("command", ALLOWED_RUN)
def test_allowed_run_spellings(command: str) -> None:
    assert hook.allowed_paper_data_command(command) is True
    assert hook.decide(command) == (False, "")
    assert hook.main(json.dumps({"tool_name": "PowerShell", "tool_input": {"command": command}})) == 0
    assert is_run_shape(command)
    assert _before_run().decide(command)[0] is True  # blocked before: this shape is the only change


@pytest.mark.parametrize("command", REFUSED_RUN)
def test_refused_run_variants(command: str) -> None:
    assert hook.decide(command)[0] is True, command
    assert hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) == 2
    assert hook.allowed_paper_data_command(command) is False, command
    assert only_the_run_shape_got_looser(command)


EVERY_ROW = REFUSED + REFUSED_WATCH + REFUSED_RUN + ALLOWED + ALLOWED_WATCH + ALLOWED_RUN


@pytest.mark.parametrize("command", EVERY_ROW)
def test_only_the_run_shape_is_looser_than_before_the_run_allowance(command: str) -> None:
    now, before = hook.decide(command)[0], _before_run().decide(command)[0]
    if before is False:
        assert now is False  # the old verdicts stay as they were
    if now is False and before is True:
        assert is_run_shape(command) and hook.allowed_paper_data_command(command)


def test_every_older_row_keeps_its_verdict() -> None:
    for command in REFUSED + REFUSED_WATCH:
        assert hook.decide(command)[0] == _before_run().decide(command)[0], command
    for command in ALLOWED + ALLOWED_WATCH:
        assert hook.decide(command) == (False, "")


@pytest.mark.parametrize("sub", ["fetch-bars", "probe-data", "backtest", "runs", "report", "serve"])
def test_other_subcommands_keep_their_verdict_with_the_run_options(sub: str) -> None:
    command = run(sub=sub)
    assert hook.decide(command)[0] == _before_run().decide(command)[0]
    assert (hook.allowed_paper_data_command(command) is True) == (sub in ("fetch-bars", "probe-data"))


def test_the_neutral_text_of_the_run_form_is_the_offline_subcommand() -> None:
    for command in (ALLOWED_RUN[0], ALLOWED_RUN[4], ALLOWED_RUN[8], ALLOWED_RUN[-1]):
        neutral = hook._paper_neutral(command)
        assert neutral == command.replace(" -m MainStoinker run ", " -m MainStoinker backtest ", 1)
        assert hook._decide(neutral, 1) == (False, "")
        assert hook._decide(command, 1)[0] is True  # harmless only because of the replaced word
    assert hook._paper_neutral(REFUSED_RUN[0]) is None


def test_the_run_form_without_its_options_stays_refused_and_with_them_is_allowed() -> None:
    assert hook.decide(f"{UP} -P -m MainStoinker run --paper --no-prompt")[0] is True
    assert hook.decide(f"{UP} -P -m MainStoinker run --paper --no-prompt --minutes 30")[0] is True
    assert hook.decide(run()) == (False, "")


def test_a_run_with_the_order_word_is_refused_by_the_function_and_by_the_hook() -> None:
    for opt in ("--orders", "--orders=paper", "--order", "--ORDERS=1", "--orders paper"):
        command = run(args=f"{RUN_ARGS} {opt}")
        assert hook.allowed_paper_data_command(command) is False, command
        assert hook.decide(command)[0] is True, command
        assert hook._paper_match(command) is None, command


def test_other_blocks_still_apply_to_the_allowed_run_shape() -> None:
    for tail in (f' --out "{ENTRY}"', ' --out "C:\\x\\Stonks data collect.bat"',
                 ' --out "C:\\x\\MainStoinker\\ib\\bars"', ' "MainStoinker/testy/testy.py"'):
        command = ALLOWED_RUN[0] + tail
        assert hook.decide(command)[0] is True
        assert hook.allowed_paper_data_command(command) is False


def test_the_allowance_is_still_off_for_decoded_and_folded_run_text() -> None:
    import base64

    encoded = base64.b64encode(ALLOWED_RUN[0].encode("utf-16-le")).decode()
    assert hook.decide(f"powershell -EncodedCommand {encoded}")[0] is True
    assert hook.decide(ALLOWED_RUN[0].replace("--paper", "\uff0d\uff0dpaper"))[0] is True
    assert hook.decide(ALLOWED_RUN[0].replace("run", "\uff52\uff55\uff4e", 1))[0] is True


def test_the_function_is_still_called_in_one_place_and_the_message_names_run() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert len(re.findall(r"allowed_paper_data_command\(", text)) == 2
    assert text.count("_paper_neutral(") == 2
    reason = hook.decide(f"{UP} -P -m MainStoinker run --port 7497")[1]
    for word in ("-P -m MainStoinker run", "--algos", "--bars", "--db", "--minutes", "--orders", "--paper"):
        assert word in reason
    assert ENV_NAME not in reason


RUN_PROPERTY_BASES = [ALLOWED_RUN[0], ALLOWED_RUN[4], ALLOWED_RUN[8], ALLOWED_RUN[10]]


@pytest.mark.parametrize("original", RUN_PROPERTY_BASES)
def test_any_forbidden_character_or_wrapper_anywhere_is_refused_for_run(original: str) -> None:
    variants = _variants(original)
    assert len(variants) > 500
    for label, mutated in variants:
        if _benign(label, mutated, original):
            assert hook.allowed_paper_data_command(mutated), label
            continue
        assert hook.allowed_paper_data_command(mutated) is False, (label, mutated)
        assert hook.decide(mutated)[0] or not _before_run().decide(mutated)[0], (label, mutated)


@pytest.mark.parametrize("original", RUN_PROPERTY_BASES)
def test_a_backslash_inside_any_option_token_is_refused_for_run(original: str) -> None:
    tokens = SPLIT.findall(original.strip())
    count = 0
    for i, tok in enumerate(tokens):
        if not tok.startswith("--"):
            continue
        for k in range(1, len(tok)):
            mutated = " ".join([*tokens[:i], tok[:k] + "\\" + tok[k:], *tokens[i + 1 :]])
            assert hook.allowed_paper_data_command(mutated) is False, mutated
            assert hook.decide(mutated)[0] or not _before_run().decide(mutated)[0], mutated
            count += 1
    assert count > 30


def test_every_prefix_of_a_guarded_option_name_is_refused_for_run() -> None:
    count = 0
    for name in ("--paper", "--no-prompt", "--minutes", "--port", "--host", "--algos", "--bars", "--db",
                 "--fills", "--orders", "--help"):
        for k in range(3, len(name)):
            short = name[:k]
            for command in (run(args=f"{RUN_ARGS} {short} 7497"), run(args=f"{RUN_ARGS} {short}=1"),
                            run(args=f"{RUN_ARGS} {short}")):
                assert hook.allowed_paper_data_command(command) is False, command
                count += 1
    assert count > 40
