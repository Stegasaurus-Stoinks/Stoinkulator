"""The `run` command (`MainStoinker/ib/run_cli.py`, issue 198), in process on the fake.

Everything is real except the broker connection: `run_cli.main` with the factories of the `Rig`
of `tests/test_ib_watch_bars.py` (`IbConnection` and `IbMarketData` over `FakeIbClient`, scripted
server time and stream, injected clock and sleep, no real sleep), the real session driver, engine
and `SqliteStore` under `tmp_path`. The only subprocess tests run `--help` or a usage error that
exits before any client exists. A refusal test hands the command factories that raise
(`forbid_building`), so a refusal that came too late fails at once instead of hanging.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from MainStoinker.engine.cli import main as engine_main
from MainStoinker.engine.live_store import LiveRunWriter
from MainStoinker.engine.store import RunSummary, SqliteStore
from MainStoinker.ib import run_cli
from tests import test_ib_live_run as live
from tests.no_build import FACTORY_NAMES, forbid_building
from tests.test_ib_live_run import (
    SYMBOLS,
    Rig,
    ema_entries,
    old_run,
    script_minutes,
    standard_script,
)
from tests.test_ib_watch_bars import (  # noqa: F401  (`real_home_untouched` is an autouse fixture)
    NoStdin,
    Tty,
    ep,
    nothing_built,
    real_home_untouched,
)

REPO = Path(__file__).resolve().parent.parent
DAY = date(2024, 8, 16)
rig = live.rig  # the fixture of the driver's tests: the rig, the month files and the scripted session


# -- helpers --------------------------------------------------------------------------------


def write_algos(tmp: Path, entries: list[dict[str, Any]] | None = None) -> Path:
    path = tmp / "algos.json"
    path.write_text(json.dumps(entries if entries is not None else ema_entries()), encoding="utf-8")
    return path


def argv_of(tmp: Path, *, minutes: str = "8", paper: bool = True, no_prompt: bool = True,
            record: bool = False, **options: str) -> list[str]:
    values = {"--algos": str(tmp / "algos.json"), "--bars": str(tmp / "bars"),
              "--db": str(tmp / "runs.db"), "--out": str(tmp / "live"), "--warmup-days": "1",
              "--stop-file": str(tmp / "STOP")}
    values.update({"--" + key.replace("_", "-"): value for key, value in options.items()})
    argv: list[str] = []
    for flag, value in values.items():
        argv += [flag, value]
    argv += ["--minutes", minutes]
    if paper:
        argv.append("--paper")
    if no_prompt:
        argv.append("--no-prompt")
    if not record:
        argv.append("--no-record")
    return argv


def go(rig: Rig, argv: list[str], *, tty: bool = True, stdin: Any = None, recorder: bool = True,
       entries: list[dict[str, Any]] | None = None) -> int:
    """`run_cli.main` on the rig. The algos file is written unless one exists."""
    if not (rig.tmp / "algos.json").exists():
        write_algos(rig.tmp, entries)

    def opener(spec: str, env: Any) -> SqliteStore:
        return SqliteStore(Path(spec), clock=rig.wall, env={})

    extra: dict[str, Any] = {"recorder_factory": rig.recorder_factory} if recorder else {}
    return run_cli.main(
        argv, env=rig.env, stdin=Tty(tty) if stdin is None else stdin, input_fn=rig.input_fn,
        out=rig.out, connection_factory=rig.connection_factory,
        market_data_factory=rig.market_data_factory, clock=rig, sleep=rig.sleep, wall=rig.wall,
        today=DAY, store_opener=opener, **extra)


def runs_of(tmp: Path) -> dict[int, RunSummary]:
    store = SqliteStore(tmp / "runs.db", env={})
    try:
        return {r.run_id: r for r in store.list_runs()}
    finally:
        store.close()


def last_line(rig: Rig) -> str:
    return rig.text.strip().splitlines()[-1]


def refused(rig: Rig, argv: list[str], **kw: Any) -> str:
    """Run `argv` with factories that raise: it must be refused (exit 2) before anything is built."""
    forbid_building(rig)
    assert go(rig, argv, **kw) == 2, rig.text
    nothing_built(rig)
    assert not rig.prompts
    return rig.text


def allow_building(rig: Rig) -> None:
    """Undo `forbid_building` (it set instance attributes that shadow the methods)."""
    for name in FACTORY_NAMES:
        rig.__dict__.pop(name, None)


def quick_session(rig: Rig) -> None:
    standard_script(rig, last=5, minutes=8)


# -- refusals: algo modes and fills ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["backtest", "live", "Paper", ""])
def test_refuses_backtest_and_live_modes(rig: Rig, mode: str) -> None:
    entries = ema_entries(mode=mode)
    write_algos(rig.tmp, entries)
    text = refused(rig, argv_of(rig.tmp))
    assert "Refused:" in text and "mode" in text  # `live` is refused by the config check itself
    assert not (rig.tmp / "runs.db").exists() and not (rig.tmp / "live").exists()


def test_refuses_an_algo_without_a_mode_and_one_bad_algo_among_good_ones(rig: Rig) -> None:
    entries = ema_entries()
    del entries[0]["data"][1]["mode"]  # absent means backtest
    write_algos(rig.tmp, entries)
    assert "MSFT-10/30EMA" in refused(rig, argv_of(rig.tmp))


def test_refuses_legacy_fills(rig: Rig) -> None:
    text = refused(rig, argv_of(rig.tmp, fills="legacy"))
    assert "realistic fills only" in text
    assert not (rig.tmp / "runs.db").exists()


@pytest.mark.parametrize("option", ["--capital=0", "--capital=-5", "--commission-per-share=-1",
                                    "--commission-min=abc", "--slippage-per-share=0.00001"])
def test_cost_options_are_checked_as_backtest_checks_them(rig: Rig, option: str) -> None:
    assert "Refused:" in refused(rig, [*argv_of(rig.tmp), option])


def test_cost_options_reach_the_stored_run_and_the_run_verifies(rig: Rig) -> None:
    quick_session(rig)
    argv = [*argv_of(rig.tmp), "--capital", "5000", "--commission-per-share", "0.01",
            "--commission-min", "2", "--slippage-per-share", "0.01"]
    assert go(rig, argv) == 0, rig.text
    store = SqliteStore(rig.tmp / "runs.db", env={})
    try:
        config = store.load_run(1).config
    finally:
        store.close()
    assert config["capital"] == "5000.0000"
    assert config["costs"] == {"commission_per_share": "0.0100", "commission_minimum": "2.0000",
                               "slippage_per_share": "0.0100"}
    assert "verify: VERIFIED" in rig.text
    assert engine_main(["report", "1", "--db", str(rig.tmp / "runs.db"), "--verify", "--bars",
                        str(rig.tmp / "bars")], env={}) == 0


# -- usage errors and limits ----------------------------------------------------------------


def parse_fails(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as stop:
        run_cli.parse_args(argv)
    assert stop.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize("minutes", ["0", "-1", "421", "600", "x", ""])
def test_minutes_outside_1_to_420_is_a_usage_error(minutes: str, capsys: pytest.CaptureFixture[str]) -> None:
    tmp = Path("unused")
    err = parse_fails(argv_of(tmp, minutes=minutes), capsys)
    assert "--minutes" in err


def test_no_prompt_requires_paper_and_minutes(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    tmp = rig.tmp
    assert "--no-prompt is valid only together with --paper" in parse_fails(
        [*argv_of(tmp, paper=False, no_prompt=True), "--port", "7497"], capsys)
    argv = [a for a in argv_of(tmp) if a not in ("--minutes", "8")]  # no --minutes at all
    assert "--minutes" in parse_fails(argv, capsys)
    forbid_building(rig)
    for bad in (argv_of(tmp, paper=False, no_prompt=True), argv):
        with pytest.raises(SystemExit):
            go(rig, bad)
    nothing_built(rig)
    # the limits themselves are accepted
    for good in ("1", "420"):
        assert run_cli.parse_args(argv_of(tmp, minutes=good)).minutes == int(good)


def test_the_parser_has_no_orders_option_and_matches_options_exactly(
        capsys: pytest.CaptureFixture[str]) -> None:
    flags = {a for action in run_cli.build_parser(True)._actions for a in action.option_strings}  # noqa: SLF001
    assert not [f for f in flags if "order" in f]
    assert {"--algos", "--bars", "--db", "--out", "--warmup-days", "--capital", "--commission-per-share",
            "--commission-min", "--slippage-per-share", "--paper", "--no-prompt", "--minutes",
            "--stop-file", "--record", "--no-record", "--client-id", "--fills"} <= flags
    # an abbreviation of --paper is not --paper: there is no port, so the parser wants one
    assert "--port" in parse_fails(
        ["--algos", "a", "--bars", "b", "--db", "d", "--pap", "--minutes", "5"], capsys)
    args = run_cli.parse_args(["--algos", "a", "--bars", "b", "--paper", "--minutes", "5"])
    assert (args.client_id, args.port, args.fills, args.warmup_days) == (20, 7497, "realistic", 2)


def test_help_and_usage_errors_build_no_client() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    base = [sys.executable, "-P", "-m", "MainStoinker", "run"]
    done = subprocess.run([*base, "--help"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    for word in ("--minutes", "--stop-file", "--no-record", "exit codes", "130", "09:25", "paper-live"):
        assert word in done.stdout, word
    assert "--orders" not in done.stdout
    # a usage error (--no-prompt without --paper) exits 2 at the parser, before anything exists
    bad = subprocess.run([*base, "--algos", "a", "--bars", "b", "--port", "7497", "--no-prompt",
                          "--minutes", "5"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert bad.returncode == 2
    assert "--no-prompt is valid only together with --paper" in bad.stderr
    over = subprocess.run([*base, "--algos", "a", "--bars", "b", "--paper", "--minutes", "421"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert over.returncode == 2 and "--minutes must be 1 to 420" in over.stderr


# -- refusals: port, host, opt-in, paper ----------------------------------------------------


def test_paper_refuses_other_port_host_and_optin_env(rig: Rig) -> None:
    for extra in (["--port", "7496"], ["--port", "4002"], ["--port", "4001"], ["--host", "10.0.0.5"],
                  ["--host", "localhost"]):
        rig.out.truncate(0)
        rig.out.seek(0)
        text = refused(rig, [*argv_of(rig.tmp), *extra])
        assert "Refused (--paper)" in text and "Nothing was started" in text, extra
    for value in ("1", "0", ""):  # present at all, even empty
        rig.out.truncate(0)
        rig.out.seek(0)
        rig.env["STOINK_ALLOW_LIVE_PORT"] = value
        assert "STOINK_ALLOW_LIVE_PORT is set" in refused(rig, argv_of(rig.tmp))
    assert not (rig.tmp / "runs.db").exists() and not (rig.tmp / "live").exists()


def test_without_paper_a_port_the_guard_refuses_is_refused_and_so_is_everything_else(rig: Rig) -> None:
    text = refused(rig, [*argv_of(rig.tmp, paper=False, no_prompt=False), "--port", "7496"])
    assert "STOINK_ALLOW_LIVE_PORT" in text
    rig.out.truncate(0)
    rig.out.seek(0)
    text = refused(rig, [*argv_of(rig.tmp, paper=False, no_prompt=False), "--port", "7497"])
    assert "paper only" in text


# -- the console gate ------------------------------------------------------------------------


def test_gate_requires_tty_and_port(rig: Rig) -> None:
    forbid_building(rig)
    argv = argv_of(rig.tmp, no_prompt=False)
    assert go(rig, argv, tty=False) == 2  # not a terminal: no prompt either
    assert "only the owner runs it" in rig.text and not rig.prompts
    for typed in ("", "y", "7496", "port"):
        rig.out.truncate(0)
        rig.out.seek(0)
        rig.prompts.clear()
        rig.typed = typed
        assert go(rig, argv) == 2
        assert len(rig.prompts) == 1 and "(7497)" in rig.prompts[0]
        assert "Nothing was started" in rig.text
    nothing_built(rig)
    assert not (rig.tmp / "runs.db").exists() or runs_of(rig.tmp) == {}
    assert not (rig.tmp / "live").exists()


def test_the_gate_describes_the_session_and_the_typed_port_starts_it(rig: Rig) -> None:
    quick_session(rig)
    rig.typed = "7497"
    assert go(rig, argv_of(rig.tmp, no_prompt=False)) == 0, rig.text
    assert len(rig.prompts) == 1
    for word in ("AAPL, MSFT, TSLA", "8 minutes", "no order is ever sent", "Only the owner runs it",
                 "Run database", "Live files", "Recording file", "none (--no-record)"):
        assert word in rig.text, word
    assert "Unattended" not in rig.text
    assert last_line(rig) == "Live session: completed, end reason minutes_elapsed (exit 0)"


def test_no_prompt_runs_unattended_with_the_banner_and_never_touches_stdin(rig: Rig) -> None:
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp), stdin=NoStdin()) == 0, rig.text
    assert not rig.prompts
    assert "paper mode: port 7497, account check on, no prompt" in rig.text
    assert "Unattended paper run: data only, paper account only." in rig.text
    assert "Only the owner runs it" not in rig.text


# -- a whole session ------------------------------------------------------------------------


def test_an_unattended_session_is_stored_and_verifiable(rig: Rig) -> None:
    quick_session(rig)
    client_ids: list[int] = []
    build = rig.connection_factory

    def factory(args: Any, observers: list[Any]) -> Any:
        client_ids.append(args.client_id)
        return build(args, observers)

    rig.connection_factory = factory  # type: ignore[method-assign]
    assert go(rig, argv_of(rig.tmp)) == 0, rig.text
    assert client_ids == [20]  # the engine's client id
    run = runs_of(rig.tmp)[1]
    assert (run.kind, run.fills, run.status, run.end_reason) == (
        "paper-live", "realistic", "completed", "minutes_elapsed")
    assert "verify: VERIFIED" in rig.text
    assert engine_main(["report", "1", "--db", str(rig.tmp / "runs.db"), "--verify", "--bars",
                        str(rig.tmp / "bars")], env={}) == 0
    assert last_line(rig) == "Live session: completed, end reason minutes_elapsed (exit 0)"
    assert (rig.tmp / "live" / "AAPL").is_dir()  # the live files, as the watcher writes them


def test_the_stop_file_ends_the_run_cleanly(rig: Rig) -> None:
    standard_script(rig, last=8, minutes=11)
    rig.at(rig.tt(36, 0.0), lambda: (rig.tmp / "STOP").write_text("stop", encoding="utf-8"))
    assert go(rig, argv_of(rig.tmp, minutes="11")) == 0, rig.text
    assert (runs_of(rig.tmp)[1].status, runs_of(rig.tmp)[1].end_reason) == ("completed", "stop_file")
    assert last_line(rig) == "Live session: completed, end reason stop_file (exit 0)"


def test_an_existing_stop_file_is_refused(rig: Rig) -> None:
    (rig.tmp / "STOP").write_text("old", encoding="utf-8")
    assert "stop file" in refused(rig, argv_of(rig.tmp))


# -- the runs an earlier session left ---------------------------------------------------------


def test_abandoned_runs_closed_at_start(rig: Rig) -> None:
    from datetime import UTC, datetime

    db = rig.tmp / "runs.db"
    old_run(db, kind="paper-live", touched=datetime(2024, 8, 16, 12, 0, tzinfo=UTC))  # stale
    old_run(db, kind="backtest", touched=datetime(2024, 8, 16, 13, 29, 50, tzinfo=UTC))  # not a live run
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp)) == 0, rig.text
    runs = runs_of(rig.tmp)
    assert (runs[1].status, runs[1].end_reason) == ("failed", "abandoned")
    assert runs[2].status == "running" and runs[2].kind == "backtest"
    assert (runs[3].status, runs[3].end_reason) == ("completed", "minutes_elapsed")  # a NEW run
    assert "run 1 was still marked running but its last write is stale" in rig.text


def test_another_running_paper_live_run_is_refused_after_the_stale_ones_are_closed(rig: Rig) -> None:
    from datetime import UTC, datetime

    db = rig.tmp / "runs.db"
    old_run(db, kind="paper-live", touched=datetime(2024, 8, 16, 12, 0, tzinfo=UTC))  # stale
    old_run(db, kind="paper-live", touched=datetime(2024, 8, 16, 13, 29, 30, tzinfo=UTC))  # recent
    text = refused(rig, argv_of(rig.tmp))
    runs = runs_of(rig.tmp)
    assert (runs[1].status, runs[1].end_reason) == ("failed", "abandoned")  # closed first
    assert runs[2].status == "running" and 3 not in runs  # left alone, no new run
    assert "another paper-live run is marked running" in text and "run 2" in text
    assert "run 1 was still marked running but its last write is stale" in text


# -- before the open --------------------------------------------------------------------------


def test_no_request_before_0930_server_time(rig: Rig) -> None:
    """A start at 09:25 (the owner's decision, issue 198) is accepted. The only request made before
    09:30 is the one-minute subscription of each symbol (IB rolls it into the session); no bar of
    an earlier time is asked for, and nothing before 09:30 is fed."""
    rig.server_epoch = ep(9, 25, 0)
    rig.backfills = {s: [[]] for s in SYMBOLS}  # IB has no bar of today yet
    script_minutes(rig, SYMBOLS, 0, 7)
    rig.end_at(13, symbols=3)
    assert go(rig, argv_of(rig.tmp, minutes="13")) == 0, rig.text
    assert sorted(rig.requests) == sorted((s, rig.req_ids[s], "60 S", True) for s in SYMBOLS)
    store = SqliteStore(rig.tmp / "runs.db", env={})
    try:
        times = [b.time.strftime("%H:%M") for b in store.load_bars(1, "AAPL")]
    finally:
        store.close()
    assert times and times[0] == "09:30" and all(t >= "09:30" for t in times)


# -- recording -------------------------------------------------------------------------------


def test_recording_on_by_default(rig: Rig) -> None:
    quick_session(rig)
    argv = argv_of(rig.tmp, record=True)  # neither --record nor --no-record: the default
    assert "--no-record" not in argv and "--record" not in argv
    assert go(rig, argv, recorder=False) == 0, rig.text  # the command's own recorder, on the fake
    found = sorted((rig.tmp / "home" / "Stoinkulator" / "recordings").glob("*.jsonl"))
    assert [p.name for p in found] == ["run-2024-08-16-001.jsonl"]
    assert "run" in found[0].read_text(encoding="utf-8").splitlines()[0]
    assert "Recording check: 0 finding(s)" in rig.text
    assert last_line(rig).startswith("Live session: completed")


def test_no_record_and_an_explicit_record_file(rig: Rig) -> None:
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp), recorder=False) == 0, rig.text
    assert not (rig.tmp / "home" / "Stoinkulator" / "recordings").exists()
    assert "Recording check" not in rig.text


def test_an_explicit_record_file_is_used_and_never_overwritten(rig: Rig) -> None:
    mine = rig.tmp / "rec" / "mine.jsonl"
    mine.parent.mkdir()
    mine.write_text("keep me", encoding="utf-8")
    argv = [*argv_of(rig.tmp, record=True), "--record", str(mine)]
    assert "exists; it is never overwritten" in refused(rig, argv)
    assert mine.read_text(encoding="utf-8") == "keep me"


def test_record_and_no_record_exclude_each_other(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert "not allowed with argument" in parse_fails([*argv_of(rig.tmp), "--record", "x.jsonl"], capsys)


# -- paths and the database -------------------------------------------------------------------


@pytest.mark.parametrize("where", [
    "repo_out", "onedrive_out", "repo_bars", "onedrive_bars", "repo_db", "onedrive_db", "repo_stop",
    "repo_record", "onedrive_record", "env_onedrive", "out_is_file", "out_under_file", "bars_missing",
    "bars_is_file", "db_is_dir", "db_under_file", "memory_db", "onedrive_home"])
def test_unusable_paths_are_refused_before_anything_is_built(rig: Rig, where: str) -> None:
    tmp = rig.tmp
    options: dict[str, str] = {}
    record = False
    extra: list[str] = []
    if where == "repo_out":
        options["out"] = str(REPO / "live_bars")
    elif where == "onedrive_out":
        options["out"] = str(tmp / "OneDrive - Org" / "live")
    elif where == "repo_bars":
        options["bars"] = str(REPO / "OfflineData")
    elif where == "onedrive_bars":
        (tmp / "OneDrive - Org" / "bars").mkdir(parents=True)
        options["bars"] = str(tmp / "OneDrive - Org" / "bars")
    elif where == "repo_db":
        options["db"] = str(REPO / "runs.db")
    elif where == "onedrive_db":
        options["db"] = str(tmp / "OneDrive - Org" / "runs.db")
    elif where == "repo_stop":
        options["stop_file"] = str(REPO / "STOP")
    elif where == "repo_record":
        extra = ["--record", str(REPO / "rec.jsonl")]
        record = True
    elif where == "onedrive_record":
        extra = ["--record", str(tmp / "OneDrive" / "rec.jsonl")]
        record = True
    elif where == "env_onedrive":
        rig.env["OneDrive"] = str(tmp / "synced")
        options["out"] = str(tmp / "synced" / "live")
    elif where == "out_is_file":
        (tmp / "live").write_text("x", encoding="utf-8")
    elif where == "out_under_file":
        (tmp / "afile").write_text("x", encoding="utf-8")
        options["out"] = str(tmp / "afile" / "live")
    elif where == "bars_missing":
        options["bars"] = str(tmp / "no-such-bars")
    elif where == "bars_is_file":
        (tmp / "somefile").write_text("x", encoding="utf-8")
        options["bars"] = str(tmp / "somefile")
    elif where == "db_is_dir":
        (tmp / "adir").mkdir()
        options["db"] = str(tmp / "adir")
    elif where == "db_under_file":
        (tmp / "afile").write_text("x", encoding="utf-8")
        options["db"] = str(tmp / "afile" / "runs.db")
    elif where == "memory_db":
        options["db"] = ":memory:"
    else:  # a home under OneDrive: the default folders would be there
        rig.env["USERPROFILE"] = str(tmp / "OneDrive - Org" / "me")
        rig.env["HOME"] = rig.env["USERPROFILE"]
    argv = [*argv_of(tmp, record=record, **options), *extra]
    if where == "onedrive_home":  # no --out: the default folder under the home is used
        position = argv.index("--out")
        del argv[position:position + 2]
    text = refused(rig, argv)
    assert "Refused" in text
    if where in ("repo_out", "onedrive_out", "env_onedrive"):
        assert not Path(options["out"]).exists()
    if where in ("repo_db", "onedrive_db"):
        assert not Path(options["db"]).exists()


def test_the_database_defaults_to_stoink_db_and_is_needed(rig: Rig) -> None:
    argv = [a for a in argv_of(rig.tmp) if a not in ("--db", str(rig.tmp / "runs.db"))]
    assert "--db is needed" in refused(rig, argv)
    rig.out.truncate(0)
    rig.out.seek(0)
    allow_building(rig)
    rig.env["STOINK_DB"] = str(rig.tmp / "env.db")
    quick_session(rig)
    assert go(rig, argv) == 0, rig.text
    assert (rig.tmp / "env.db").exists() and not (rig.tmp / "runs.db").exists()


def test_a_database_the_store_refuses_is_refused_before_anything_is_connected(rig: Rig) -> None:
    forbid_building(rig)

    def broken(spec: str, env: Any) -> SqliteStore:
        from MainStoinker.engine.store import StoreError

        raise StoreError("not a database")

    write_algos(rig.tmp)
    code = run_cli.main(argv_of(rig.tmp), env=rig.env, out=rig.out, clock=rig, sleep=rig.sleep,
                        wall=rig.wall, today=DAY, store_opener=broken,
                        connection_factory=rig.connection_factory,
                        market_data_factory=rig.market_data_factory)
    assert code == 2 and "the run database cannot be used: not a database" in rig.text
    nothing_built(rig)


def test_the_symbols_come_from_the_algos_and_at_most_five(rig: Rig) -> None:
    many = ema_entries(("AAPL", "MSFT", "TSLA", "A", "B", "C"))
    write_algos(rig.tmp, many)
    assert "6 symbols" in refused(rig, argv_of(rig.tmp))


# -- the end of a session is shielded from Ctrl-C ---------------------------------------------


def finish_interrupted(monkeypatch: pytest.MonkeyPatch, *, after_work: bool) -> dict[str, int]:
    real = LiveRunWriter.finish
    state = {"calls": 0}

    def finish(self: LiveRunWriter, *args: Any, **kw: Any) -> Any:
        state["calls"] += 1
        if state["calls"] == 1:
            if after_work:
                real(self, *args, **kw)  # the run is ended, then the interrupt arrives
            raise KeyboardInterrupt
        return real(self, *args, **kw)

    monkeypatch.setattr(LiveRunWriter, "finish", finish)
    return state


@pytest.mark.parametrize("after_work", [False, True])
def test_ctrl_c_inside_finish_does_not_leave_the_run_running(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, after_work: bool) -> None:
    state = finish_interrupted(monkeypatch, after_work=after_work)
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp)) == 130, rig.text
    assert state["calls"] == 2  # the end was run again after the interrupt
    run = runs_of(rig.tmp)[1]
    assert (run.status, run.end_reason) == ("completed", "minutes_elapsed")  # not `running`
    assert "Ctrl-C while the run is being ended" in rig.text
    assert last_line(rig) == "Live session: completed, end reason minutes_elapsed (exit 130)"


def test_ctrl_c_inside_end_live_and_verify_does_not_leave_the_run_running(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from MainStoinker.engine.engine import Engine

    real_end = Engine.end_live
    calls = {"n": 0}

    def end_live(self: Engine) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_end(self)

    monkeypatch.setattr(Engine, "end_live", end_live)
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp)) == 130, rig.text
    assert runs_of(rig.tmp)[1].status == "completed"
    assert last_line(rig).endswith("(exit 130)")


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(),
                    reason="a signal is delivered to the main thread")
def test_a_real_sigint_during_the_end_is_deferred_until_the_run_is_ended(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = LiveRunWriter.finish
    state = {"calls": 0}

    def finish(self: LiveRunWriter, *args: Any, **kw: Any) -> Any:
        state["calls"] += 1
        signal.raise_signal(signal.SIGINT)  # Ctrl-C, twice, during the end
        signal.raise_signal(signal.SIGINT)
        return real(self, *args, **kw)

    previous = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(LiveRunWriter, "finish", finish)
    quick_session(rig)
    assert go(rig, argv_of(rig.tmp)) == 130, rig.text
    assert state["calls"] == 1  # nothing was interrupted: the interrupt was only counted
    assert runs_of(rig.tmp)[1].status == "completed"
    assert signal.getsignal(signal.SIGINT) is previous  # the handler is put back
    assert last_line(rig) == "Live session: completed, end reason minutes_elapsed (exit 130)"


# -- another stored run blocks nothing but a live one; the last line -----------------------------


def test_the_last_line_names_the_status_and_reason_of_a_failed_run(rig: Rig) -> None:
    standard_script(rig, last=5, minutes=8)
    rig.interrupt_after = 6  # Ctrl-C reaches the watcher during the session
    assert go(rig, argv_of(rig.tmp)) == 130, rig.text
    assert runs_of(rig.tmp)[1].end_reason == "interrupted"
    assert last_line(rig) == "Live session: failed, end reason interrupted (exit 130)"


# -- import boundaries -------------------------------------------------------------------------


def child(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-P", "-c", code], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)


@pytest.mark.parametrize("command", ["backtest", "runs", "report"])
def test_offline_commands_still_never_import_ib(command: str) -> None:
    loaded = ("print(sorted(m for m in sys.modules\n"
              "             if m == 'ibapi' or m.startswith(('ibapi.', 'MainStoinker.ib'))))")
    done = child("import sys, runpy\nsys.argv = ['MainStoinker', %r, '--help']\n"
                 "try:\n    runpy.run_module('MainStoinker', run_name='__main__')\n"
                 "except SystemExit:\n    pass\n" % command + loaded)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout


def test_the_run_module_imports_nothing_that_can_place_an_order() -> None:
    source = (REPO / "MainStoinker" / "ib" / "run_cli.py").read_text(encoding="utf-8")
    for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "TradeTools", "apiApi", "IBKRHelper",
                 "Globals", "os.environ[", "setdefault", "putenv", "STOINK_ALLOW_LIVE_PORT=1"):
        assert word not in source, word
    assert "orders" not in {a for act in run_cli.build_parser()._actions for a in act.option_strings}  # noqa: SLF001
