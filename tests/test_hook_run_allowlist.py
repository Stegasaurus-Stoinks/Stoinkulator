"""The agent shape of `run` is an option allowlist (issue 292).

Every command here is a string fed to the hook's decision functions (never run). Allowed for an
agent, and nothing else: `--algos V`, `--bars V`, `--db V`, `--minutes N`, `--paper`, `--no-prompt`,
`--fills realistic`, `--stop-file V`. Any other option in any spelling, any single-dash form and any
stray word is refused, and an unknown flag never swallows the token after it.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import random
import types

import pytest

from tests.test_hook_paper_allowance import (
    ALLOWED,
    ALLOWED_RUN,
    ALLOWED_WATCH,
    REFUSED,
    REFUSED_RUN,
    REFUSED_WATCH,
    REPO_ROOT,
    RUN_ARGS,
    hook,
    run,
)

BEFORE_292 = (REPO_ROOT / "tests" / "fixtures" / "hook_baseline"
              / "block_live_launch_before_run_allowlist.py.txt")
# sha256 of the frozen copy of the hook as it was before the option allowlist (LF line endings)
BEFORE_292_SHA256 = "de4ed0314850df321196c7f9775dbb9e3f5d4cff7241b351b79472322d1b272b"


@functools.cache
def _before_292() -> types.ModuleType:
    data = BEFORE_292.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == BEFORE_292_SHA256, "the frozen pre-292 hook was edited"
    module = types.ModuleType("block_live_launch_before_292")
    module.__file__ = str(BEFORE_292)
    exec(compile(data.decode("utf-8"), module.__file__, "exec"), module.__dict__)
    return module


# the six required units, in the order of RUN_ARGS; each unit is one option with its value
UNITS = ["--algos C:/s/a.json", "--bars C:/s/bars", "--db C:/s/r.db", "--minutes 30",
         "--paper", "--no-prompt"]
OPTIONAL = ["--fills realistic", "--stop-file C:/s/stop.flag"]


def cmd(units: list[str]) -> str:
    return run(args=" ".join(units))


def is_refused(command: str) -> bool:
    return (
        hook.allowed_paper_data_command(command) is False
        and hook.decide(command)[0] is True
        and hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) == 2
    )


# -- allowed: every allowed option, in several orders ---------------------------------------------

def _orders() -> list[list[str]]:
    full = UNITS + OPTIONAL
    rng = random.Random(292)
    out = [list(UNITS), list(reversed(UNITS)), list(full), list(reversed(full))]
    for _ in range(40):
        pick = UNITS + [u for u in OPTIONAL if rng.random() < 0.6]
        rng.shuffle(pick)
        out.append(pick)
    return out


@pytest.mark.parametrize("units", _orders())
def test_every_allowed_option_passes_in_any_order(units: list[str]) -> None:
    command = cmd(units)
    assert hook.allowed_paper_data_command(command) is True, command
    assert hook.decide(command) == (False, "")


def test_the_equals_forms_of_the_valued_options_pass() -> None:
    units = ["--algos=C:/s/a.json", "--bars=C:/s/bars", "--db=C:/s/r.db", "--minutes=30", "--paper",
             "--no-prompt", "--fills=realistic", "--stop-file=C:/s/stop.flag"]
    assert hook.allowed_paper_data_command(cmd(units)) is True


# -- refused: every other option, in every position and spelling ---------------------------------

REFUSED_OPTIONS = [
    "allow-cold", "no-fetch", "warmup-days", "no-record", "out", "capital", "commission-per-share",
    "commission-min", "slippage-per-share", "port", "host", "client-id", "record", "connect-timeout",
    "quiet", "symbols", "start", "end", "backfill-minutes", "no-history", "quote-seconds", "settle-hours",
    "orders", "order", "orders-paper", "help", "version", "foo", "x", "o", "verbose", "dry-run",
]


def _positions(base: list[str]) -> dict[str, int]:
    # the six units: algos, bars, db, minutes, paper, no-prompt
    return {"first": 0, "middle": 2, "before --paper": 4, "before --no-prompt": 5, "last": len(base)}


def _insertions(extra: str) -> list[tuple[str, str]]:
    out = []
    for label, at in _positions(UNITS).items():
        units = [*UNITS[:at], extra, *UNITS[at:]]
        out.append((label, cmd(units)))
    return out


@pytest.mark.parametrize("name", REFUSED_OPTIONS)
def test_every_other_option_is_refused_in_every_position(name: str) -> None:
    count = 0
    for spelling in (f"--{name}", f"--{name} V", f"--{name}=V", f"--{name.upper()}", f"--{name.title()}=V",
                     f"--{name} C:/s/x", f"-{name}", f"-{name[0]}"):
        for label, command in _insertions(spelling):
            assert is_refused(command), (label, command)
            count += 1
    assert count == 8 * 5


@pytest.mark.parametrize("name", ["allow-cold", "no-fetch", "warmup-days", "no-record", "out", "capital"])
def test_the_named_options_with_a_value_and_a_second_unknown_flag_are_refused(name: str) -> None:
    for other in ("--foo", "--no-fetch", "--allow-cold", "--x=1"):
        for label, command in _insertions(f"--{name} {other}"):
            assert is_refused(command), (label, command)
        for label, command in _insertions(f"{other} --{name} 3"):
            assert is_refused(command), (label, command)


def test_the_issue_probe_pairs_are_refused() -> None:
    # found in the review of PR 291: these were allowed
    for command in (
        run(args=RUN_ARGS.replace("--paper", "--allow-cold --paper", 1)),
        run(args=RUN_ARGS.replace("--no-prompt", "--no-fetch --no-prompt", 1)),
        run(args=RUN_ARGS.replace("--paper --no-prompt", "--allow-cold --no-fetch")
            + " --paper --no-prompt"),
        run(args=RUN_ARGS + " --allow-cold --no-fetch"),
        run(args="--allow-cold --no-fetch " + RUN_ARGS),
        run(args=RUN_ARGS + " --warmup-days 3"),
        run(args=RUN_ARGS + " --warmup-days=3"),
        run(args="--warmup-days 3 " + RUN_ARGS),
        run(args=RUN_ARGS.replace("--minutes 415", "--warmup-days 3 --minutes 415")),
        run(args=RUN_ARGS.replace("--paper", "--warmup-days=3 --paper", 1)),
    ):
        assert is_refused(command), command


def test_an_unknown_flag_cannot_swallow_the_next_token() -> None:
    walk = hook._paper_run_allowlist
    good = RUN_ARGS.split()
    assert walk(good) is True
    for flag in ("--allow-cold", "--no-fetch", "--whatever", "--x=1"):
        for at in range(len(good) + 1):
            tokens = good[:at] + [flag] + good[at:]
            # a valued option's own value stays attached to it: only a flag position is tested
            if at and good[at - 1] in ("--algos", "--bars", "--db", "--minutes"):
                continue
            assert walk(tokens) is False, tokens
    # the token after an unknown flag is judged by itself: a stray word or a second unknown flag
    assert walk(good + ["--foo", "bar"]) is False
    assert walk(good + ["--foo", "--paper"]) is False
    assert walk(["--foo"] + good) is False


def test_abbreviations_and_prefixes_of_every_option_are_refused() -> None:
    count = 0
    names = ["--algos", "--bars", "--db", "--minutes", "--paper", "--no-prompt", "--fills", "--stop-file",
             "--allow-cold", "--no-fetch", "--warmup-days", "--no-record", "--out", "--capital"]
    for name in names:
        for k in range(1, len(name)):
            short = name[:k]
            if short == "--":
                short = "--"  # the bare double dash is refused too
            for tail in ("", " V", "=V"):
                for label, command in _insertions(short + tail):
                    assert is_refused(command), (label, command)
                    count += 1
    assert count > 500


def test_each_allowed_option_in_another_letter_case_is_refused() -> None:
    count = 0
    for name in ("--algos", "--bars", "--db", "--minutes", "--paper", "--no-prompt", "--fills",
                 "--stop-file"):
        for variant in (name.upper(), name.title(), name[:3] + name[3:].upper()):
            if variant == name:
                continue
            units = [u for u in UNITS + OPTIONAL if not u.startswith(name)]
            value = "" if name in ("--paper", "--no-prompt") else " realistic" if name == "--fills" else " 30"
            command = cmd([*units, variant + value])
            assert is_refused(command), command
            count += 1
    assert count >= 20


def test_duplicates_are_refused() -> None:
    count = 0
    for extra in ("--algos C:/s/b.json", "--bars C:/s/b2", "--db C:/s/r2.db", "--minutes 30", "--minutes 31",
                  "--algos=C:/s/b.json", "--bars=C:/s/b2", "--db=C:/s/r2.db", "--minutes=30", "--paper",
                  "--no-prompt", "--fills realistic", "--fills=realistic", "--stop-file C:/s/s2",
                  "--stop-file=C:/s/s2"):
        base = UNITS + (OPTIONAL if extra.startswith(("--fills", "--stop-file")) else [])
        for label, command in [(lbl, cmd([*base[:at], extra, *base[at:]]))
                               for lbl, at in _positions(base).items()]:
            assert is_refused(command), (label, command)
            count += 1
    assert count == 15 * 5


def test_missing_required_options_and_empty_or_option_like_values_are_refused() -> None:
    for drop in range(6):
        assert is_refused(cmd(UNITS[:drop] + UNITS[drop + 1 :])), UNITS[drop]
    for bad in ("--algos", "--bars", "--db", "--stop-file"):
        for value in ("", "--paper", "-x", '"-x"', "--orders"):
            units = [u for u in UNITS + OPTIONAL if not u.startswith(bad)]
            assert is_refused(cmd([*units, f"{bad} {value}".strip()])), (bad, value)
            assert is_refused(cmd([f"{bad} {value}".strip(), *units])), (bad, value)
        assert is_refused(cmd([*[u for u in UNITS if not u.startswith(bad)], f"{bad}="]))
    assert is_refused(cmd([*UNITS, "--stop-file="]))
    for stray in ("backtest", "fetch-bars", "x", "-x", "-paper", "--", '"--paper"', "'--paper'"):
        for label, command in _insertions(stray):
            assert is_refused(command), (label, command)


def test_the_fills_value_is_only_realistic_and_the_flags_take_no_value() -> None:
    for command in (cmd([*UNITS, "--fills legacy"]), cmd([*UNITS, "--fills=legacy"]),
                    cmd([*UNITS, "--fills"]), cmd([*UNITS[:4], "--paper=1", "--no-prompt"]),
                    cmd([*UNITS[:4], "--paper", "--no-prompt=1"]),
                    cmd([*UNITS[:4], "--paper true", "--no-prompt"])):
        assert is_refused(command), command


def test_the_order_rule_still_fires_for_every_spelling_and_position() -> None:
    count = 0
    for spelling in ("--orders", "--orders=paper", "--order", "--ORDERS", "--orders paper", "--ord",
                     "--orders-x", "--db=--orders", "--stop-file --orders", "--stop-file=C:/x/--orders"):
        for label, command in _insertions(spelling):
            assert hook._paper_match(command) is None, (label, command)
            assert is_refused(command), (label, command)
            count += 1
    assert count == 10 * 5


# -- nothing refused before is allowed now ---------------------------------------------------------

def _all_commands() -> list[str]:
    commands = list(dict.fromkeys(
        ALLOWED + ALLOWED_WATCH + ALLOWED_RUN + REFUSED + REFUSED_WATCH + REFUSED_RUN
    ))
    for name in REFUSED_OPTIONS:
        for spelling in (f"--{name}", f"--{name} V", f"--{name}=V", f"--{name.upper()}"):
            commands.extend(c for _, c in _insertions(spelling))
    commands.extend(cmd(u) for u in _orders())
    commands.extend(cmd(list(p)) for p in itertools.islice(itertools.permutations(UNITS), 0, 720, 9))
    return list(dict.fromkeys(commands))


def test_nothing_that_was_refused_before_is_allowed_now() -> None:
    old = _before_292()
    commands = _all_commands()
    assert len(commands) > 800
    loosened = [c for c in commands if old.decide(c)[0] and not hook.decide(c)[0]]
    assert loosened == []
    # and the function itself: whatever the old function refused, the new one refuses
    assert [c for c in commands
            if not old.allowed_paper_data_command(c) and hook.allowed_paper_data_command(c)] == []


def test_the_rows_that_changed_from_allowed_to_refused_are_exactly_the_listed_options() -> None:
    old = _before_292()
    changed = [c for c in _all_commands()
               if not old.decide(c)[0] and hook.decide(c)[0]]
    assert changed, "the allowlist refuses something that was allowed"
    for command in changed:
        # only `run` shapes with an option outside the allowlist
        assert " -m MainStoinker run " in command, command


def test_the_other_shapes_keep_their_verdict() -> None:
    old = _before_292()
    for command in ALLOWED + ALLOWED_WATCH + REFUSED + REFUSED_WATCH:
        assert hook.decide(command)[0] == old.decide(command)[0], command  # the message text differs


def test_claude_md_states_the_allowlist() -> None:
    text = " ".join((REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    for phrase in (
        "`--fills realistic` and `--stop-file F` are the only options of `run`",
        "The options of this `run` shape are an allowlist and nothing else: `--algos V`, `--bars V`, "
        "`--db V`, `--minutes N`, `--paper`, `--no-prompt`, `--fills realistic` and `--stop-file V`",
        "every other option is refused in any spelling",
        "and an unknown flag never takes the token after it as its value",
        "`--allow-cold`, `--no-fetch` and `--warmup-days` are owner-only and the hook refuses them "
        "for agents",
    ):
        assert phrase in text, phrase
