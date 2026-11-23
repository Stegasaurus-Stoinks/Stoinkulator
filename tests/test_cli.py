"""The `python -m MainStoinker backtest` command line.

Inputs are built under tmp_path/inputs; the command is started from an empty
tmp_path/start so that "nothing written to the start directory" is checkable.
Only one test runs the full AAPL data (shared by the two golden comparisons);
everything else is truncated.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from ibapi import client as ibapi_client

import MainStoinker.engine.cli as cli
import MainStoinker.TradeTools.trade as trade_module
from MainStoinker.engine.algos import _EngineFileHandler
from MainStoinker.engine.cli import CliError, main, validate_algo_config
from MainStoinker.MainStuff.Globals import config
from tests import harness
from tests.fake_ibapi import RecordingIBapi
from tests.test_golden_backtests import CASES

GOLDEN = Path(__file__).parent / "golden"
EMA = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}
EMA_MSFT = {"idname": "MSFT-10/30EMA", "ticker": "MSFT", "short": "10", "long": "30"}
FUNC_MSFT = {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3}
ROWS = 900  # enough replay bars for EMACrossing to trade a few times


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.inputs = root / "inputs"
        self.start = root / "start"
        self.out = root / "out"
        self.inputs.mkdir()
        self.start.mkdir()
        self.algos = self.inputs / "algos.json"
        self.data = self.inputs / "OfflineData"
        monkeypatch.chdir(self.start)

    def stage(self, symbols: list[str], max_rows: int | None = ROWS) -> None:
        harness.stage_offline_data(self.inputs, symbols, max_rows=max_rows)

    def write_algos(self, obj: object) -> None:
        self.algos.write_text(json.dumps(obj), encoding="utf-8")

    def args(self, *extra: str) -> list[str]:
        return ["backtest", "--algos", str(self.algos), "--data", str(self.data),
                "--out", str(self.out), *extra]

    def run(self, *extra: str) -> int:
        return main(self.args(*extra))

    def trades(self) -> dict:
        return json.loads((self.out / "trades.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def single(env: Env, algo_id: str = "EMACrossing", cfg: dict = EMA, rows: int | None = ROWS):
    env.stage([cfg["ticker"]], rows)
    env.write_algos([{"ID": algo_id, "data": [cfg]}])


# ---- full-data golden comparison (one full run, shared) ---------------------

_full_run: dict = {}


def full_aapl_run(env: Env) -> bytes:
    """trades.json bytes of the full EMACrossing/AAPL run (computed once per session)."""
    if "bytes" not in _full_run:
        algo_id, cfg = next(p.values for p in CASES if p.id == "emacrossing_aapl")
        assert cfg == EMA
        single(env, algo_id, cfg, rows=None)
        assert env.run("--quiet") == 0
        _full_run["bytes"] = (env.out / "trades.json").read_bytes()
    return _full_run["bytes"]


def test_cli_emacrossing_matches_golden(env):
    data = json.loads(full_aapl_run(env))
    golden = json.loads((GOLDEN / "emacrossing_aapl.json").read_text())
    assert data[EMA["idname"]] == golden
    assert len(golden) > 0


def test_cli_output_is_byte_identical_to_golden_formatting(env):
    raw = full_aapl_run(env)
    golden_bytes = (GOLDEN / "emacrossing_aapl.json").read_bytes()
    data = json.loads(raw)
    # The golden fixture writes json.dumps(indent=1) + "\n" with write_text, which
    # may translate newlines on this platform; compare modulo newline translation.
    assert raw.endswith(b"\n")
    assert list(data) == [EMA["idname"]]
    assert json.dumps(data[EMA["idname"]], indent=1).encode() + b"\n" == golden_bytes.replace(
        b"\r\n", b"\n"
    )
    # trades.json is the same formatting with the idname as the single key.
    assert raw == (json.dumps(data, indent=1) + "\n").encode("utf-8")


# ---- behaviour ---------------------------------------------------------------


def test_cli_two_algos_two_symbols_keys_in_config_order(env, capsys):
    env.stage(["AAPL", "MSFT"], 600)
    env.write_algos([
        {"ID": "FunctionTest", "data": [FUNC_MSFT]},
        {"ID": "EMACrossing", "data": [EMA, EMA_MSFT]},
    ])
    assert env.run("--quiet") == 0
    data = env.trades()
    assert list(data) == ["MSFT-functest", "AAPL-10/30EMA", "MSFT-10/30EMA"]
    assert all(isinstance(v, list) for v in data.values())
    out = capsys.readouterr().out
    assert "replayed" in out and "MSFT-functest: " in out and "trades.json" in out
    # symbols are the tickers in first-appearance order (MSFT first)
    assert "for MSFT, AAPL" in out


def test_cli_never_connects(env, monkeypatch):
    calls: list[str] = []

    def spy(name):
        def refuse(*args, **kwargs):
            calls.append(name)
            raise RuntimeError(f"unexpected connection attempt: {name}")
        return refuse

    monkeypatch.setattr(ibapi_client.EClient, "connect", spy("EClient.connect"))
    monkeypatch.setattr(socket.socket, "connect", spy("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", spy("create_connection"))
    single(env)
    assert env.run("--quiet") == 0
    assert calls == []


def test_cli_does_not_touch_legacy_runtime(env, monkeypatch):
    from MainStoinker.DataCollection.apiApi import ibapi
    from MainStoinker.MainStuff import main_utils
    from MainStoinker.TradeTools.ticker import Ticker

    def refuse(*args, **kwargs):
        raise AssertionError("legacy runtime used")

    monkeypatch.setattr(ibapi, "startData", refuse)
    monkeypatch.setattr(Ticker, "__init__", refuse)
    monkeypatch.setattr(main_utils, "backtesting_data_blast", refuse)
    single(env)
    assert env.run("--quiet") == 0


def test_cli_ignores_live_flags(env, monkeypatch):
    fake = RecordingIBapi()
    monkeypatch.setattr(trade_module, "ibapi", fake)
    original = config.LiveTrading
    assert original is False
    object.__setattr__(config, "LiveTrading", True)
    try:
        single(env)
        assert env.run("--quiet") == 0
    finally:
        object.__setattr__(config, "LiveTrading", original)
    assert sum(len(v) for v in env.trades().values()) > 0  # trades really happened
    assert fake.calls == []


def test_cli_leaves_no_files_in_start_directory(env):
    single(env)
    start = Path.cwd()
    assert env.run("--quiet") == 0
    assert Path.cwd() == start
    assert list(env.start.iterdir()) == []
    assert not (env.out / "loggy.log").exists()
    assert (env.out / "trades.json").is_file()
    # EMACrossing calls Trade.get_stats when it closes a trade; that file lands in --out.
    assert any(env.trades().values())
    assert (env.out / "tradey.txt").is_file()


def test_cwd_restored_after_error(env):
    single(env)
    env.write_algos([{"ID": "NoSuchAlgo", "data": [EMA]}])
    start = Path.cwd()
    assert env.run() == 2
    assert Path.cwd() == start
    assert list(env.start.iterdir()) == []


def test_cli_log_option_writes_backtest_log_and_closes_it(env):
    single(env)
    assert env.run("--quiet", "--log") == 0
    log = env.out / "backtest.log"
    assert log.is_file()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines
    assert " - algo:AAPL-10/30EMA:AAPL - INFO - " in lines[0]
    logger = logging.getLogger("algo:AAPL-10/30EMA:AAPL")
    assert not [h for h in logger.handlers if isinstance(h, _EngineFileHandler)]
    log.unlink()  # raises PermissionError on Windows if the handler is still open
    assert not (env.out / "loggy.log").exists()
    assert list(env.start.iterdir()) == []


def test_cli_without_log_creates_no_log_files(env):
    single(env)
    assert env.run("--quiet") == 0
    assert not [p.name for p in env.out.iterdir() if p.name.endswith(".log")]
    assert list(env.start.iterdir()) == []


def test_cli_creates_missing_out_dir_with_parents(env):
    single(env)
    env.out = env.root / "a" / "b" / "c"
    assert env.run("--quiet") == 0
    assert (env.out / "trades.json").is_file()
    assert not list(env.out.glob("*.tmp"))


def test_cli_relative_paths_resolve_against_start_dir(env):
    single(env)
    algos = os.path.relpath(env.algos, env.start)
    data = os.path.relpath(env.data, env.start)
    argv = ["backtest", "--algos", algos, "--data", data, "--out", "results", "--quiet"]
    assert main(argv) == 0
    assert (env.start / "results" / "trades.json").is_file()
    assert [p.name for p in env.start.iterdir()] == ["results"]


# ---- input errors -------------------------------------------------------------


def _bad(env, capsys, word):
    assert env.run() == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1, err
    assert err.startswith("error: ")
    assert word in err
    assert "Traceback" not in err
    assert not (env.out / "trades.json").exists()
    assert list(env.start.iterdir()) == []


def test_error_missing_algos_file(env, capsys):
    env.stage(["AAPL"])
    _bad(env, capsys, "cannot read algos file")


def test_error_invalid_json(env, capsys):
    env.stage(["AAPL"])
    env.algos.write_text("[{not json", encoding="utf-8")
    _bad(env, capsys, "not valid JSON")


@pytest.mark.parametrize(
    "obj, word",
    [
        ({"ID": "EMACrossing"}, "must be a JSON list"),
        ([{"data": [EMA]}], '"ID"'),
        ([{"ID": "EMACrossing", "data": "x"}], "must be a list"),
        ([{"ID": "EMACrossing", "data": [{"idname": "a"}]}], '"ticker"'),
        ([{"ID": "EMACrossing", "data": [{"ticker": "AAPL"}]}], '"idname"'),
        ([{"ID": "../x", "data": [EMA]}], "invalid algo id"),
        ([{"ID": "EMACrossing", "data": [EMA]}] * 2, "duplicate idname"),
        ([], "empty"),
    ],
    ids=["not-list", "no-ID", "data-not-list", "no-ticker", "no-idname", "bad-id", "duplicate",
         "empty"],
)
def test_error_bad_config(env, capsys, obj, word):
    env.stage(["AAPL"])
    env.write_algos(obj)
    _bad(env, capsys, word)


def test_error_unknown_algo_id(env, capsys):
    env.stage(["AAPL"])
    env.write_algos([{"ID": "NoSuchAlgo", "data": [EMA]}])
    _bad(env, capsys, "unknown algo")


def test_error_missing_data_dir(env, capsys):
    env.write_algos([{"ID": "EMACrossing", "data": [EMA]}])
    _bad(env, capsys, "data directory not found")


def test_error_missing_data_file(env, capsys):
    env.stage(["AAPL"])  # MSFT is configured but not staged
    env.write_algos([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}])
    _bad(env, capsys, "not found")


def test_misaligned_data_skips_the_missing_minute(env, capsys):
    # Was an error ("not aligned") before decision 6: now the symbol is skipped for that minute.
    env.stage(["AAPL", "MSFT"])
    path = env.data / "_MSFT_offlinedata_"
    frame = pd.read_csv(path, index_col=0)
    frame.drop(index=frame.index[600]).to_csv(path)
    env.write_algos([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}])
    assert env.run() == 0
    out = capsys.readouterr().out
    assert "MSFT: no bar at 1 of " in out


def test_error_negative_warmup(env, capsys):
    single(env)
    assert env.run("--warmup-days", "-1") == 2
    assert "warmup" in capsys.readouterr().err


# ---- config validation (pure) ---------------------------------------------------


def test_validate_returns_normalised_config():
    parsed = [{"ID": "EMACrossing", "data": [EMA, EMA_MSFT], "extra": 1}]
    validated = validate_algo_config(parsed)
    assert validated == [{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}]
    assert cli.config_symbols(validated) == ["AAPL", "MSFT"]


@pytest.mark.parametrize(
    "parsed, word",
    [
        (None, "list"),
        ({}, "list"),
        ([], "empty"),
        (["x"], "not a JSON object"),
        ([{"data": []}], '"ID"'),
        ([{"ID": "A"}], '"data"'),
        ([{"ID": 5, "data": []}], "invalid algo id"),
        ([{"ID": "_Private", "data": [EMA]}], "invalid algo id"),
        ([{"ID": "../x", "data": [EMA]}], "invalid algo id"),
        ([{"ID": "A", "data": {}}], "must be a list"),
        ([{"ID": "A", "data": ["x"]}], "not a JSON object"),
        ([{"ID": "A", "data": [{"idname": "a"}]}], '"ticker"'),
        ([{"ID": "A", "data": [{"ticker": "AAPL"}]}], '"idname"'),
        ([{"ID": "A", "data": [{"idname": "", "ticker": "AAPL"}]}], "non-empty"),
        ([{"ID": "A", "data": [{"idname": "a", "ticker": "../AAPL"}]}], "invalid ticker"),
        ([{"ID": "A", "data": [{"idname": "a", "ticker": "X"}, {"idname": "a", "ticker": "Y"}]}],
         "duplicate"),
        ([{"ID": "A", "data": []}], "no algo configs"),
    ],
)
def test_validate_rejects(parsed, word):
    with pytest.raises(CliError, match=word):
        validate_algo_config(parsed)


# ---- exceptions in the run ---------------------------------------------------------


def test_unexpected_exception_propagates_and_cleans_up(env, monkeypatch):
    loaded = []
    real = cli.load_algo

    def load_and_break(*args, **kwargs):
        algo = real(*args, **kwargs)

        def boom(frame):
            raise RuntimeError("boom")

        algo.update = boom
        loaded.append(algo)
        return algo

    monkeypatch.setattr(cli, "load_algo", load_and_break)
    single(env)
    start = Path.cwd()
    with pytest.raises(RuntimeError, match="boom"):
        env.run("--quiet", "--log")
    assert Path.cwd() == start
    assert loaded
    for algo in loaded:
        assert not [h for h in algo.logger.handlers if isinstance(h, _EngineFileHandler)]
    assert not (env.out / "trades.json").exists()
    (env.out / "backtest.log").unlink()  # handler closed: deletable on Windows
    assert list(env.start.iterdir()) == []


# ---- argparse -----------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--help"], ["backtest", "--help"]])
def test_cli_help(argv, capsys):
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 0
    text = capsys.readouterr().out
    assert "backtest" in text
    if argv[0] == "backtest":
        for option in ("--algos", "--data", "--out", "--warmup-days", "--log"):
            assert option in text


def test_no_subcommand_exits_2(capsys):
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


# ---- subprocesses (two spawns) -------------------------------------------------------


def test_cli_subprocess_smoke(env):
    single(env, rows=600)
    result = subprocess.run(
        [sys.executable, "-m", "MainStoinker", "backtest", "--algos", str(env.algos),
         "--data", str(env.data), "--out", "results", "--quiet"],
        cwd=env.start, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads((env.start / "results" / "trades.json").read_text(encoding="utf-8"))
    assert list(data) == [EMA["idname"]]
    assert [p.name for p in env.start.iterdir()] == ["results"]


def test_cli_module_import_is_side_effect_free(tmp_path):
    code = (
        "import json, runpy, sys\n"
        "import MainStoinker.engine.cli\n"
        "legacy = ('MainStoinker.DataCollection.apiApi', 'MainStoinker.Util.SocketIO_Client',\n"
        "          'MainStoinker.MainStuff.Globals', 'ibapi')\n"
        "found = {'import': [m for m in legacy if m in sys.modules]}\n"
        "for argv in (['MainStoinker', '--help'], ['MainStoinker', 'backtest', '--help']):\n"
        "    sys.argv = argv\n"
        "    try:\n"
        "        runpy.run_module('MainStoinker', run_name='__main__')\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0, exc.code\n"
        "    found[' '.join(argv)] = [m for m in legacy if m in sys.modules]\n"
        "print(json.dumps(found))\n"
    )
    work = tmp_path / "w"
    work.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=work, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found == {"import": [], "MainStoinker --help": [], "MainStoinker backtest --help": []}
    assert list(work.iterdir()) == []
