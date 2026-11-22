"""Algo modes, the refusal of `live`, and the `--fills` / cost options of the CLI.

Short runs use truncated bar files; the full-data runs (shared through a
module-level cache) check the realistic numbers against the paper broker's
reference values and legacy output against the committed golden.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import MainStoinker.engine.cli as cli
import MainStoinker.engine.modes as modes
from MainStoinker.engine.cli import CliError, main, validate_algo_config
from MainStoinker.engine.ledger import Ledger
from MainStoinker.engine.paper_broker import CostModel, PaperBroker
from tests import harness

GOLDEN = Path(__file__).parent / "golden"
EMA = {"idname": "AAPL-10/30EMA", "ticker": "AAPL", "short": "10", "long": "30"}
EMA_MSFT = {"idname": "MSFT-10/30EMA", "ticker": "MSFT", "short": "10", "long": "30"}
FUNC_MSFT = {"idname": "MSFT-functest", "ticker": "MSFT", "RRRatio": 3}
FVG = {"idname": "AAPL-fvg", "ticker": "AAPL", "RRRatio": 3}
ROWS = 900
LIVE_MESSAGE = (
    "error: algo 'AAPL-10/30EMA': mode 'live' is not available yet "
    "(needs the live broker, milestone 1 Phase B)"
)


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> None:
        self.inputs = root / "inputs"
        self.start = root / "start"
        self.out = root / "out"
        self.inputs.mkdir(parents=True)
        self.start.mkdir()
        self.algos = self.inputs / "algos.json"
        self.data = self.inputs / "OfflineData"
        if monkeypatch is not None:
            monkeypatch.chdir(self.start)

    def setup(self, algos: object, symbols: list[str], rows: int | None = ROWS) -> None:
        harness.stage_offline_data(self.inputs, symbols, max_rows=rows)
        self.algos.write_text(json.dumps(algos), encoding="utf-8")

    def run(self, *extra: str) -> int:
        return main(["backtest", "--algos", str(self.algos), "--data", str(self.data),
                     "--out", str(self.out), "--quiet", *extra])

    def trades_bytes(self) -> bytes:
        return (self.out / "trades.json").read_bytes()

    def trades(self) -> dict:
        return json.loads(self.trades_bytes())

    def ledger(self) -> dict:
        return json.loads((self.out / "ledger.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def ema_config(**extra: object) -> list[dict]:
    return [{"ID": "EMACrossing", "data": [{**EMA, **extra}]}]


# ---- modes in the config ------------------------------------------------------


def test_mode_defaults_to_backtest(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run() == 0
    default = env.trades_bytes()
    env.setup(ema_config(mode="backtest"), ["AAPL"])
    assert env.run() == 0
    assert env.trades_bytes() == default
    assert any(env.trades().values())
    assert modes.parse_mode("x", "backtest") == modes.DEFAULT_MODE == "backtest"


def test_paper_mode_same_trades_as_backtest(env):
    env.setup(ema_config(mode="backtest"), ["AAPL"])
    assert env.run() == 0
    backtest = env.trades_bytes()
    env.setup(ema_config(mode="paper"), ["AAPL"])
    assert env.run() == 0
    assert env.trades_bytes() == backtest
    # and with realistic fills
    assert env.run("--fills", "realistic") == 0
    paper_realistic = env.trades_bytes()
    env.setup(ema_config(mode="backtest"), ["AAPL"])
    assert env.run("--fills", "realistic") == 0
    assert env.trades_bytes() == paper_realistic


@pytest.mark.parametrize("mode", ["turbo", "", "Backtest", "PAPER", "Live", "live ", 5, None,
                                  True, ["live"], {"mode": "paper"}])
def test_invalid_mode_rejected(env, capsys, mode):
    env.setup(ema_config(mode=mode), ["AAPL"])
    assert env.run() == 2
    err = capsys.readouterr().err
    assert err.startswith("error: algo 'AAPL-10/30EMA': ")
    assert "mode" in err and len(err.strip().splitlines()) == 1
    assert not env.out.exists()


def test_non_string_mode_message(env, capsys):
    env.setup(ema_config(mode=5), ["AAPL"])
    assert env.run() == 2
    assert "mode must be a string, got int" in capsys.readouterr().err
    with pytest.raises(CliError, match="mode must be a string"):
        validate_algo_config(ema_config(mode=None))


def test_live_mode_refused_exit_2_one_line(env, capsys):
    env.setup(ema_config(mode="live"), ["AAPL"])
    assert env.run() == 2
    captured = capsys.readouterr()
    assert captured.err == LIVE_MESSAGE + "\n"
    assert captured.out == ""
    assert not env.out.exists()
    for fills in ("legacy", "realistic"):
        assert env.run("--fills", fills) == 2
        assert capsys.readouterr().err == LIVE_MESSAGE + "\n"


def test_live_mode_refused_in_validation_even_after_valid_entries():
    config = [
        {"ID": "EMACrossing", "data": [{**EMA, "mode": "paper"}]},
        {"ID": "EMACrossing", "data": [{**EMA_MSFT, "mode": "live"}]},
    ]
    with pytest.raises(CliError) as info:
        validate_algo_config(config)
    assert str(info.value) == (
        "algo 'MSFT-10/30EMA': mode 'live' is not available yet "
        "(needs the live broker, milestone 1 Phase B)"
    )


def test_live_mode_builds_nothing(env, monkeypatch, capsys):
    def refuse(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"{name} must not be called for a live config")
        return fail

    import MainStoinker.engine.broker as broker_module
    import MainStoinker.engine.feed as feed_module
    import MainStoinker.engine.ledger as ledger_module
    import MainStoinker.engine.paper_broker as paper_module

    for module, name in [
        (cli, "CsvBarFeed"), (cli, "load_algo"), (cli, "Ledger"), (cli, "Engine"),
        (cli, "build_router"), (cli, "NullSink"),
        (modes, "SimBroker"), (modes, "PaperBroker"), (modes, "Ledger"),
        (feed_module, "CsvBarFeed"), (broker_module, "SimBroker"),
        (paper_module, "PaperBroker"), (ledger_module, "Ledger"),
    ]:
        monkeypatch.setattr(module, name, refuse(f"{module.__name__}.{name}"))
    monkeypatch.setattr(Ledger, "open_account", refuse("Ledger.open_account"))
    env.setup([
        {"ID": "EMACrossing", "data": [{**EMA, "mode": "paper"}, {**EMA_MSFT, "mode": "live"}]},
    ], ["AAPL", "MSFT"], rows=50)
    for fills in ("legacy", "realistic"):
        assert env.run("--fills", fills) == 2
        assert "mode 'live' is not available yet" in capsys.readouterr().err
    assert not env.out.exists()


def test_modes_has_no_live_factory():
    assert set(modes.BROKER_FACTORIES) == {"backtest", "paper"}
    assert not any("live" in key.lower() for key in modes.BROKER_FACTORIES)
    with pytest.raises(modes.ModeError, match="not available yet"):
        modes.parse_mode("a", "live")
    entries = [{"ID": "EMACrossing", "data": [{**EMA, "mode": "live"}]}]
    for ledger in (None, Ledger()):
        with pytest.raises(modes.ModeError, match="not available yet"):
            modes.build_router(entries, fills="legacy", costs=CostModel(),
                               capital=Decimal(1000), ledger=ledger)
    # nothing was built for the refused config
    assert Ledger().accounts == ()


def test_mode_key_does_not_disturb_the_algo(env):
    """Unknown keys are ignored by algos, so a mode changes no trade."""
    plain = {**EMA}
    modded = {**EMA, "mode": "paper"}
    env.setup([{"ID": "EMACrossing", "data": [plain]}], ["AAPL"])
    assert env.run() == 0
    before = env.trades()
    env.setup([{"ID": "EMACrossing", "data": [modded]}], ["AAPL"])
    assert env.run() == 0
    assert env.trades() == before


# ---- build_router -------------------------------------------------------------


def test_build_router_one_broker_and_account_per_algo():
    entries = validate_algo_config([
        {"ID": "EMACrossing", "data": [EMA, EMA_MSFT, {**EMA, "idname": "AAPL-b", "mode": "paper"}]},
    ])
    ledger = Ledger()
    costs = CostModel(Decimal("0.01"), Decimal("2"), Decimal("0.02"))
    router = modes.build_router(entries, fills="realistic", costs=costs,
                                capital=Decimal("5000"), ledger=ledger)
    assert list(router.brokers) == ["AAPL-10/30EMA", "MSFT-10/30EMA", "AAPL-b"]
    assert len({id(b) for b in router.brokers.values()}) == 3
    assert all(isinstance(b, PaperBroker) and b.costs is costs for b in router.brokers.values())
    assert [(a.idname, a.symbol, a.capital) for a in ledger.accounts] == [
        ("AAPL-10/30EMA", "AAPL", Decimal("5000.0000")),
        ("MSFT-10/30EMA", "MSFT", Decimal("5000.0000")),
        ("AAPL-b", "AAPL", Decimal("5000.0000")),
    ]
    assert router.end_commission("AAPL-b", Decimal(10)) == Decimal("2.0000")
    assert router.end_commission("AAPL-b", Decimal(1000)) == Decimal("10.0000")


def test_build_router_legacy_uses_sim_brokers_and_free_end_commission():
    entries = validate_algo_config([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}])
    router = modes.build_router(entries, fills="legacy", costs=CostModel(),
                                capital=Decimal(1), ledger=None)
    assert all(type(b).__name__ == "SimBroker" for b in router.brokers.values())
    assert len({id(b) for b in router.brokers.values()}) == 2
    assert router.end_commission is None


def test_build_router_rejects_unknown_fill_model_and_missing_ledger():
    entries = validate_algo_config([{"ID": "EMACrossing", "data": [EMA]}])
    with pytest.raises(ValueError, match="need a ledger"):
        modes.build_router(entries, fills="realistic", costs=CostModel(),
                           capital=Decimal(1), ledger=None)
    with pytest.raises(ValueError, match="unknown fill model"):
        modes.build_router(entries, fills="fancy", costs=CostModel(),
                           capital=Decimal(1), ledger=Ledger())


# ---- options ------------------------------------------------------------------


@pytest.mark.parametrize("option, value", [
    ("--capital", "-1"),
    ("--capital", "0"),
    ("--capital", "0.0"),
    ("--capital", "abc"),
    ("--capital", ""),
    ("--capital", "NaN"),
    ("--capital", "Infinity"),
    ("--capital", "-Infinity"),
    ("--capital", "1e400"),
    ("--capital", "0.00001"),
    ("--capital", "1000.12345"),
    ("--commission-per-share", "-0.005"),
    ("--commission-per-share", "0.00005"),
    ("--commission-per-share", "nan"),
    ("--commission-per-share", "1/2"),
    ("--commission-min", "-1"),
    ("--commission-min", "1.00001"),
    ("--commission-min", "inf"),
    ("--slippage-per-share", "-0.01"),
    ("--slippage-per-share", "0.00001"),
    ("--slippage-per-share", "0.01x"),
])
def test_negative_capital_or_costs_rejected(env, capsys, option, value):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert env.run("--fills", "realistic", f"{option}={value}") == 2
    err = capsys.readouterr().err
    assert err.startswith(f"error: {option} ") and len(err.strip().splitlines()) == 1
    assert not env.out.exists()


def test_negative_capital_rejected_with_legacy_fills_too(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert env.run("--capital", "-5") == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert not env.out.exists()


@pytest.mark.parametrize("option, value", [
    ("--capital", "5000"),
    ("--commission-per-share", "0.01"),
    ("--commission-min", "2"),
    ("--slippage-per-share", "0.01"),
])
def test_cost_options_with_legacy_fills_are_an_error(env, capsys, option, value):
    env.setup(ema_config(), ["AAPL"], rows=100)
    assert env.run(option, value) == 2
    assert env.run("--fills", "legacy", option, value) == 2
    err = capsys.readouterr().err
    assert f"{option} only apply with --fills realistic" in err
    assert not env.out.exists()


def test_money_options_accept_four_decimals_and_zero_costs(env):
    env.setup(ema_config(), ["AAPL"], rows=200)
    assert env.run("--fills", "realistic", "--capital", "0.0001", "--commission-per-share", "0",
                   "--commission-min", "0.0000", "--slippage-per-share", "0.0001") == 0
    doc = env.ledger()
    assert doc["capital"] == "0.0001"
    assert doc["costs"] == {"commission_per_share": "0.0000", "commission_minimum": "0.0000",
                            "slippage_per_share": "0.0001"}


def test_money_parsed_from_text_not_float():
    assert cli.parse_money("0.1", "--x") == Decimal("0.1")
    assert cli.parse_money("0.0001", "--x") == Decimal("0.0001")
    assert cli.parse_money("1E+3", "--x") == Decimal("1000")
    assert cli.parse_money("0.00010", "--x") == Decimal("0.00010")
    with pytest.raises(CliError):
        cli.parse_money("0.00011", "--x")


def test_cost_options_reach_paper_broker(env, monkeypatch):
    made: list[PaperBroker] = []

    class Spy(PaperBroker):
        def __init__(self, account, costs=None):
            super().__init__(account, costs)
            made.append(self)

    monkeypatch.setattr(modes, "PaperBroker", Spy)
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=300)
    assert env.run("--fills", "realistic", "--capital", "2500.5", "--commission-per-share", "0.02",
                   "--commission-min", "1.5", "--slippage-per-share", "0.03") == 0
    assert len(made) == 2 and made[0] is not made[1]
    for broker in made:
        assert broker.costs == CostModel(Decimal("0.02"), Decimal("1.5"), Decimal("0.03"))
    doc = env.ledger()
    assert doc["capital"] == "2500.5000"
    assert doc["costs"] == {"commission_per_share": "0.0200", "commission_minimum": "1.5000",
                            "slippage_per_share": "0.0300"}
    assert doc["accounts"]["AAPL-10/30EMA"]["summary"]["final_equity"] != "10000.0000"
    # defaults when no option is given
    made.clear()
    assert env.run("--fills", "realistic") == 0
    assert made[0].costs == CostModel(Decimal("0.005"), Decimal("1.00"), Decimal("0"))
    assert env.ledger()["capital"] == "10000.0000"


def test_slippage_changes_the_result(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run("--fills", "realistic") == 0
    base = env.ledger()["summary"]["net"]
    assert env.run("--fills", "realistic", "--slippage-per-share", "0.05") == 0
    assert env.ledger()["summary"]["net"] != base


def test_help_lists_the_new_options(capsys):
    with pytest.raises(SystemExit) as info:
        main(["backtest", "--help"])
    assert info.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for flag in ("--fills {legacy,realistic}", "--capital", "--commission-per-share",
                 "--commission-min", "--slippage-per-share", "ledger.json"):
        assert flag in text
    assert "(default 10000)" in text and "(default 0.005)" in text
    assert "(default 1.00)" in text and "(default 0)" in text


def test_fills_choice_validated(env):
    env.setup(ema_config(), ["AAPL"], rows=100)
    with pytest.raises(SystemExit) as info:
        env.run("--fills", "fancy")
    assert info.value.code == 2


# ---- output -------------------------------------------------------------------


def test_legacy_fills_writes_no_ledger_and_same_bytes_as_default(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run() == 0
    default = env.trades_bytes()
    assert not (env.out / "ledger.json").exists()
    assert env.run("--fills", "legacy") == 0
    assert env.trades_bytes() == default
    assert not (env.out / "ledger.json").exists()


_runs: dict[str, Env] = {}


def full_run(tmp_path_factory, key: str, algos: list[dict], symbols: list[str], *extra: str) -> Env:
    if key not in _runs:
        env = Env(tmp_path_factory.mktemp(key.replace("/", "_")))
        env.setup(algos, symbols, rows=None)
        assert env.run(*extra) == 0
        _runs[key] = env
    return _runs[key]


def test_legacy_fills_output_byte_identical_to_golden(tmp_path_factory):
    env = full_run(tmp_path_factory, "ema-legacy", ema_config(mode="paper"), ["AAPL"])
    golden = json.loads((GOLDEN / "emacrossing_aapl.json").read_text())
    raw = env.trades_bytes()
    data = json.loads(raw)
    assert list(data) == [EMA["idname"]] and len(golden) > 0
    assert data[EMA["idname"]] == golden
    assert raw == (json.dumps({EMA["idname"]: golden}, indent=1) + "\n").encode()
    assert not (env.out / "ledger.json").exists()


def test_realistic_fills_trades_json_from_ledger(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run() == 0
    legacy_keys = set(env.trades()[EMA["idname"]][0])
    assert env.run("--fills", "realistic") == 0
    trades = env.trades()
    records = trades[EMA["idname"]]
    assert records and set(records[0]) == legacy_keys - {"status"}
    doc = env.ledger()
    trips = doc["accounts"][EMA["idname"]]["round_trips"]
    assert len(trips) == len(records) == doc["accounts"][EMA["idname"]]["summary"]["trades"]
    # the records come from the ledger's fills: ids, volumes and open prices agree
    assert [r["id"] for r in records] == [f"{EMA['idname']}{i}" for i in range(len(records))]
    for record, trip in zip(records, trips, strict=True):
        assert record["openTime"] == trip["entry_time"]
        assert record["volume"] == trip["quantity"]
        assert record["direction"] == (1 if trip["side"] == "long" else 0)
        if not trip["synthetic"]:
            assert record["closeTime"] == trip["exit_time"]
            assert record["closePrice"] == float(trip["exit_price"])
        else:
            assert record["closeTime"] == "None" and record["closePrice"] is None


def test_ledger_json_shape(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run("--fills", "realistic") == 0
    doc = env.ledger()
    assert list(doc) == ["fills", "costs", "capital", "summary", "accounts"]
    assert doc["fills"] == "realistic"
    assert doc["capital"] == "10000.0000"
    assert doc["costs"] == {"commission_per_share": "0.0050", "commission_minimum": "1.0000",
                            "slippage_per_share": "0.0000"}
    assert list(doc["summary"]) == ["trades", "net", "commission", "final_equity"]
    account = doc["accounts"][EMA["idname"]]
    assert list(account) == ["symbol", "summary", "round_trips"]
    assert account["symbol"] == "AAPL"
    trip = account["round_trips"][0]
    assert list(trip) == ["side", "quantity", "entry_time", "exit_time", "entry_price",
                          "exit_price", "gross", "commission", "net", "exit_reason", "synthetic"]
    for key in ("entry_price", "exit_price", "gross", "commission", "net"):
        assert len(trip[key].split(".")[1]) == 4
    # arithmetic inside the document
    net = sum(Decimal(t["net"]) for t in account["round_trips"])
    assert Decimal(account["summary"]["net"]) == net
    assert Decimal(account["summary"]["final_equity"]) == Decimal("10000") + net
    assert doc["summary"] == account["summary"]
    for t in account["round_trips"]:
        assert Decimal(t["net"]) == Decimal(t["gross"]) - Decimal(t["commission"])


def test_ledger_json_deterministic(env):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"])
    assert env.run("--fills", "realistic") == 0
    first = ((env.out / "ledger.json").read_bytes(), env.trades_bytes())
    assert env.run("--fills", "realistic") == 0
    assert ((env.out / "ledger.json").read_bytes(), env.trades_bytes()) == first
    assert first[0].endswith(b"\n")


def test_multi_algo_realistic_run(env, capsys):
    env.setup([
        {"ID": "FunctionTest", "data": [FUNC_MSFT]},
        {"ID": "EMACrossing", "data": [EMA, EMA_MSFT]},
    ], ["AAPL", "MSFT"], rows=600)
    assert env.run("--fills", "realistic", "--capital", "5000") == 0
    ids = ["MSFT-functest", "AAPL-10/30EMA", "MSFT-10/30EMA"]
    trades, doc = env.trades(), env.ledger()
    assert list(trades) == ids and list(doc["accounts"]) == ids
    assert {i: doc["accounts"][i]["symbol"] for i in ids} == {
        "MSFT-functest": "MSFT", "AAPL-10/30EMA": "AAPL", "MSFT-10/30EMA": "MSFT"}
    for idname in ids:
        account = doc["accounts"][idname]
        assert account["summary"]["trades"] == len(trades[idname]) == len(account["round_trips"])
        assert all(t["entry_time"] for t in account["round_trips"])
        net = sum(Decimal(t["net"]) for t in account["round_trips"])
        assert Decimal(account["summary"]["final_equity"]) == Decimal("5000") + net
    assert sum(len(v) for v in trades.values()) > 2
    assert any(len(doc["accounts"][i]["round_trips"]) > 0 for i in ("MSFT-functest",))
    total = doc["summary"]
    assert total["trades"] == sum(a["summary"]["trades"] for a in doc["accounts"].values())
    assert Decimal(total["net"]) == sum(Decimal(a["summary"]["net"]) for a in doc["accounts"].values())
    assert Decimal(total["final_equity"]) == Decimal("15000") + Decimal(total["net"])
    out = capsys.readouterr().out
    assert "MSFT-functest: " in out and "final equity" in out and "ledger.json" in out
    # each account trades its own symbol only
    for idname, symbol in (("AAPL-10/30EMA", "AAPL"), ("MSFT-10/30EMA", "MSFT")):
        assert {r["symbol"] for r in trades[idname]} <= {symbol}


# reference values (trades, net, commission) with the default costs and 10000 capital,
# as in tests/golden_paper.
REFERENCE = {
    "ema": ([{"ID": "EMACrossing", "data": [EMA]}], ["AAPL"], EMA["idname"], (35, "-57.3000", "70.0000")),
    "fvg": ([{"ID": "FVG", "data": [FVG]}], ["AAPL"], FVG["idname"], (42, "-94.6000", "84.0000")),
}


@pytest.mark.parametrize("key", ["ema", "fvg"])
def test_realistic_run_matches_paper_reference(tmp_path_factory, key):
    algos, symbols, idname, (trades, net, commission) = REFERENCE[key]
    env = full_run(tmp_path_factory, f"{key}-realistic", algos, symbols, "--fills", "realistic")
    summary = env.ledger()["accounts"][idname]["summary"]
    assert summary["trades"] == trades
    assert summary["net"] == net
    assert summary["commission"] == commission
    assert Decimal(summary["final_equity"]) == Decimal("10000") + Decimal(net)
    assert len(env.trades()[idname]) == trades
