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
    assert len(trips) == len(records) == doc["accounts"][EMA["idname"]]["summary"]["trade_count"]
    # the records come from the ledger's fills: ids, volumes and open prices agree
    assert [r["id"] for r in records] == [f"{EMA['idname']}{i}" for i in range(len(records))]
    for record, trip in zip(records, trips, strict=True):
        assert record["openTime"] == trip["entry_time"]
        assert record["volume"] == Decimal(trip["quantity"])
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
    summary_keys = ["trade_count", "wins", "commission", "net", "final_equity", "max_drawdown",
                    "ignored_events", "cancelled_entries", "synthetic_closes"]
    assert sorted(doc["summary"]) == sorted(summary_keys)
    account = doc["accounts"][EMA["idname"]]
    assert sorted(account["summary"]) == sorted(summary_keys)
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
        assert account["summary"]["trade_count"] == len(trades[idname]) == len(account["round_trips"])
        assert all(t["entry_time"] for t in account["round_trips"])
        net = sum(Decimal(t["net"]) for t in account["round_trips"])
        assert Decimal(account["summary"]["final_equity"]) == Decimal("5000") + net
    assert sum(len(v) for v in trades.values()) > 2
    assert any(len(doc["accounts"][i]["round_trips"]) > 0 for i in ("MSFT-functest",))
    total = doc["summary"]
    assert total["trade_count"] == sum(a["summary"]["trade_count"] for a in doc["accounts"].values())
    assert Decimal(total["net"]) == sum(Decimal(a["summary"]["net"]) for a in doc["accounts"].values())
    assert Decimal(total["final_equity"]) == Decimal("15000") + Decimal(total["net"])
    out = capsys.readouterr().out
    assert "MSFT-functest: " in out and "final equity" in out and "ledger.json" in out
    # each account trades its own symbol only
    for idname, symbol in (("AAPL-10/30EMA", "AAPL"), ("MSFT-10/30EMA", "MSFT")):
        assert {r["symbol"] for r in trades[idname]} <= {symbol}


# ---- verification additions: refusal everywhere, golden pins, multi-algo, hand-computed costs ----

ORC = {"idname": "AAPL-orc", "ticker": "AAPL", "RRRatio": 3}
FF = {"idname": "TSLA-ff", "ticker": "TSLA", "RRRatio": 3}
GOLDEN_PAPER = Path(__file__).parent / "golden_paper"
# golden file name -> (algo id, config, symbols)
FAST_CASES = {
    "emacrossing_aapl": ("EMACrossing", EMA, ["AAPL"]),
    "openingrangecandle_aapl": ("OpeningRangeCandle", ORC, ["AAPL"]),
    "fridayfrenzy_tsla": ("FridayFrenzy", FF, ["TSLA"]),
    "fvg_aapl": ("FVG", FVG, ["AAPL"]),
}


def single_realistic(tmp_path_factory, case: str, *extra: str) -> Env:
    algo_id, cfg, symbols = FAST_CASES[case]
    key = f"case-{case}-realistic" + "".join(extra)
    return full_run(tmp_path_factory, key, [{"ID": algo_id, "data": [cfg]}], symbols,
                    "--fills", "realistic", *extra)


def _live_in(position: str, mode: object) -> list[dict]:
    live = {**EMA_MSFT, "mode": mode}
    return {
        "first_entry": [{"ID": "EMACrossing", "data": [live]}, {"ID": "EMACrossing", "data": [EMA]}],
        "last_of_several": [{"ID": "EMACrossing", "data": [EMA]},
                            {"ID": "FunctionTest", "data": [{**FUNC_MSFT, "mode": "paper"}]},
                            {"ID": "EMACrossing", "data": [live]}],
        "second_config": [{"ID": "EMACrossing", "data": [EMA, live]}],
    }[position]


@pytest.mark.parametrize("position", ["first_entry", "last_of_several", "second_config"])
@pytest.mark.parametrize("mode", ["live", "Live", " live ", "LIVE", "live\n", None, 1, True, ["live"]])
@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_live_or_odd_mode_anywhere_refused_and_nothing_written(env, capsys, position, mode, fills):
    env.setup(_live_in(position, mode), ["AAPL", "MSFT"], rows=60)
    assert env.run("--fills", fills) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: algo 'MSFT-10/30EMA': ")
    assert len(captured.err.strip().splitlines()) == 1
    if mode == "live":
        assert captured.err.strip().endswith(
            "mode 'live' is not available yet (needs the live broker, milestone 1 Phase B)")
    assert not env.out.exists()
    assert list(env.start.iterdir()) == []  # no loggy.log / tradey.txt / algo.csv in the start dir
    assert not list(env.inputs.rglob("loggy.log")) and not list(env.inputs.rglob("tradey.txt"))


def test_refused_config_leaves_no_state_for_the_next_run(env, capsys):
    env.setup(ema_config(), ["AAPL"])
    assert env.run("--fills", "realistic") == 0
    first = (env.trades_bytes(), (env.out / "ledger.json").read_bytes())
    env.algos.write_text(json.dumps(ema_config(mode="live")), encoding="utf-8")
    assert env.run("--fills", "realistic") == 2
    capsys.readouterr()
    env.algos.write_text(json.dumps(ema_config()), encoding="utf-8")
    assert env.run("--fills", "realistic") == 0
    assert (env.trades_bytes(), (env.out / "ledger.json").read_bytes()) == first
    assert env.run() == 0  # legacy afterwards in the same process
    assert "status" in env.trades()[EMA["idname"]][0]  # legacy shape again


@pytest.mark.skipif(not any(GOLDEN_PAPER.glob("*.json")), reason="tests/golden_paper not merged yet")
@pytest.mark.parametrize("case", sorted(FAST_CASES))
def test_realistic_cli_output_matches_golden_paper(tmp_path_factory, case):
    """`ledger.json` shares its numbers and format with the golden: its per-algo
    `summary` and `round_trips` equal the golden's `summary` and `trades` exactly."""
    cfg = FAST_CASES[case][1]
    env = single_realistic(tmp_path_factory, case)
    golden = json.loads((GOLDEN_PAPER / f"{case}.json").read_text())
    doc = env.ledger()
    account = doc["accounts"][cfg["idname"]]
    assert account["summary"] == golden["summary"]
    assert account["round_trips"] == golden["trades"]
    assert doc["summary"] == golden["summary"]  # one algo: the total is the algo
    assert doc["capital"] == golden["capital"] and doc["costs"] == golden["costs"]
    assert account["symbol"] == golden["ticker"]
    records = env.trades()[cfg["idname"]]
    assert len(records) == golden["summary"]["trade_count"]
    assert all("status" not in record for record in records)


def test_multi_algo_run_equals_the_single_algo_runs(tmp_path_factory):
    cases = sorted(FAST_CASES)
    config = [{"ID": FAST_CASES[c][0], "data": [FAST_CASES[c][1]]} for c in cases]
    realistic = full_run(tmp_path_factory, "multi4-realistic", config, ["AAPL", "TSLA"],
                         "--fills", "realistic")
    legacy = full_run(tmp_path_factory, "multi4-legacy", config, ["AAPL", "TSLA"])
    doc, trades, legacy_trades = realistic.ledger(), realistic.trades(), legacy.trades()
    total_trades = 0
    total_net = Decimal(0)
    for case in cases:
        idname = FAST_CASES[case][1]["idname"]
        single = single_realistic(tmp_path_factory, case)
        assert doc["accounts"][idname] == single.ledger()["accounts"][idname]
        assert trades[idname] == single.trades()[idname]
        assert legacy_trades[idname] == json.loads((GOLDEN / f"{case}.json").read_text())
        total_trades += doc["accounts"][idname]["summary"]["trade_count"]
        total_net += Decimal(doc["accounts"][idname]["summary"]["net"])
    assert list(doc["accounts"]) == [FAST_CASES[c][1]["idname"] for c in cases]
    assert doc["summary"]["trade_count"] == total_trades
    assert Decimal(doc["summary"]["net"]) == total_net
    assert Decimal(doc["summary"]["final_equity"]) == Decimal(10000 * len(cases)) + total_net


def test_commission_minimum_zero_charges_per_share_on_every_fill(env):
    env.setup(ema_config(), ["AAPL"])
    assert env.run("--fills", "realistic", "--commission-per-share", "0", "--commission-min", "0") == 0
    free = env.ledger()["accounts"][EMA["idname"]]
    assert env.run("--fills", "realistic", "--commission-per-share", "0.01",
                   "--commission-min", "0") == 0
    costly = env.ledger()["accounts"][EMA["idname"]]
    assert len(free["round_trips"]) == len(costly["round_trips"]) > 1
    assert any(t["synthetic"] for t in costly["round_trips"])  # the end-of-data close pays too
    for a, b in zip(free["round_trips"], costly["round_trips"], strict=True):
        assert Decimal(a["commission"]) == 0 and Decimal(a["net"]) == Decimal(a["gross"])
        assert b["gross"] == a["gross"] and b["entry_price"] == a["entry_price"]
        # entry and exit, 0.01 per share each
        assert Decimal(b["commission"]) == Decimal(b["quantity"]) * Decimal("0.01") * 2
        assert Decimal(b["net"]) == Decimal(b["gross"]) - Decimal(b["commission"])
    assert Decimal(costly["summary"]["commission"]) == Decimal("0.10") * 2 * len(costly["round_trips"])


@pytest.mark.parametrize("case", ["openingrangecandle_aapl", "fridayfrenzy_tsla"])
def test_slippage_worsens_market_and_stop_fills_only(tmp_path_factory, case):
    free = ("--commission-per-share", "0", "--commission-min", "0")
    base = single_realistic(tmp_path_factory, case, *free)
    slipped = single_realistic(tmp_path_factory, case, *free, "--slippage-per-share", "0.01")
    idname = FAST_CASES[case][1]["idname"]
    a = base.ledger()["accounts"][idname]["round_trips"]
    b = slipped.ledger()["accounts"][idname]["round_trips"]
    assert len(a) == len(b) > 0
    assert {"stop", "target"} <= {t["exit_reason"] for t in a}
    penny = Decimal("0.01")
    for before, after in zip(a, b, strict=True):
        assert (before["entry_time"], before["exit_reason"]) == (after["entry_time"], after["exit_reason"])
        sign = 1 if before["side"] == "long" else -1
        # the entry is a market fill: a buyer pays more, a short seller receives less
        assert Decimal(after["entry_price"]) - Decimal(before["entry_price"]) == sign * penny
        move = Decimal(after["exit_price"]) - Decimal(before["exit_price"])
        if before["exit_reason"] in ("target", "end_of_data"):
            # limit fills and the synthetic close are not slipped
            assert move == 0, before["exit_reason"]
            expected_cost = penny
        else:
            assert move == -sign * penny, before["exit_reason"]
            expected_cost = 2 * penny
        assert Decimal(before["net"]) - Decimal(after["net"]) == expected_cost * Decimal(before["quantity"])


# ---- review fixes: stale ledger, output source, mode keys, ascii, money, router ----


def test_legacy_run_removes_a_stale_ledger_json(env, capsys):
    env.setup(ema_config(), ["AAPL"])
    assert env.run("--fills", "realistic") == 0
    assert (env.out / "ledger.json").exists()
    (env.out / "keep.txt").write_text("not ours", encoding="utf-8")
    capsys.readouterr()
    assert env.run() == 0
    assert not (env.out / "ledger.json").exists()
    assert (env.out / "keep.txt").read_text(encoding="utf-8") == "not ours"
    assert "removed stale" in capsys.readouterr().out
    assert "status" in env.trades()[EMA["idname"]][0]  # the legacy shape
    # nothing to remove: no message, no error
    assert env.run("--fills", "legacy") == 0
    assert "removed stale" not in capsys.readouterr().out
    # and a realistic run afterwards writes it again
    assert env.run("--fills", "realistic") == 0
    assert (env.out / "ledger.json").exists()


def _parse(env, *extra: str):
    return cli.build_parser().parse_args(
        ["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
         "--quiet", *extra])


def test_output_source_follows_the_fill_model_not_the_ledger(env, monkeypatch):
    """With a ledger attached to a legacy run (B8 will do that for every run),
    `trades.json` still comes from the algos' own trades and no `ledger.json`
    is written; the bytes equal the committed legacy golden."""
    attached: list[Ledger] = []

    class SpyLedger(Ledger):
        def __init__(self) -> None:
            super().__init__()
            attached.append(self)

    monkeypatch.setattr(cli, "Ledger", SpyLedger)
    env.setup(ema_config(), ["AAPL"], rows=None)
    assert cli.run_backtest_command(_parse(env), attach_ledger=True) == 0
    assert len(attached) == 1 and attached[0].accounts  # a ledger really was attached
    golden = json.loads((GOLDEN / "emacrossing_aapl.json").read_text())
    assert env.trades_bytes() == (json.dumps({EMA["idname"]: golden}, indent=1) + "\n").encode()
    assert not (env.out / "ledger.json").exists()
    # a realistic run through the same seam still writes both, from the ledger
    assert cli.run_backtest_command(_parse(env, "--fills", "realistic"), attach_ledger=True) == 0
    assert "status" not in env.trades()[EMA["idname"]][0]
    assert (env.out / "ledger.json").exists()


def test_ledger_json_carries_the_actual_fill_model(env, monkeypatch):
    env.setup(ema_config(), ["AAPL"], rows=200)
    seen: list[str] = []
    real = cli.ledger_document

    def spy(*args, **kwargs):
        seen.append(kwargs["fills"])
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "ledger_document", spy)
    assert env.run("--fills", "realistic") == 0
    assert seen == ["realistic"] == [env.ledger()["fills"]]


def test_mode_on_the_entry_is_an_error():
    config = [{"ID": "EMACrossing", "mode": "paper", "data": [EMA]}]
    with pytest.raises(CliError, match='"mode" belongs on each config in "data"'):
        validate_algo_config(config)


@pytest.mark.parametrize("key", ["Mode", "MODE", "mOdE"])
def test_mode_key_in_another_case_is_an_error(env, capsys, key):
    config = [{"ID": "EMACrossing", "data": [{**EMA, key: "live"}]}]
    with pytest.raises(CliError, match="must be spelled"):
        validate_algo_config(config)
    env.setup(config, ["AAPL"], rows=50)
    assert env.run() == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert not env.out.exists()


@pytest.mark.parametrize("value", ["Live", " live ", "LIVE", "live\n", "\tLive"])
def test_live_variants_get_the_live_refusal(value):
    with pytest.raises(modes.ModeError) as info:
        modes.parse_mode("a", value)
    assert "is not available yet (needs the live broker" in str(info.value)
    assert "unknown mode" not in str(info.value)


@pytest.mark.parametrize("value", ["Paper", " paper", "BACKTEST", "backtest "])
def test_other_variants_stay_unknown_modes(value):
    with pytest.raises(modes.ModeError, match="unknown mode"):
        modes.parse_mode("a", value)


@pytest.mark.parametrize("config", [
    ema_config(mode="☃"),
    ema_config(mode="live☃"),
    [{"ID": "EMACrossing", "data": [{**EMA, "ticker": "AA☃"}]}],
    [{"ID": "EMACrossing", "data": [{**EMA, "idname": "☃"}, {**EMA, "idname": "☃"}]}],
    [{"ID": "EMACrossing", "data": [{**EMA, "idname": "☃", "mode": "turbo"}]}],
    [{"ID": "☃", "data": [EMA]}],
    [{"ID": "EMACrossing", "data": [{**EMA, "MODE☃": 2, "Mode": 3}]}],
])
def test_error_messages_survive_a_cp1252_stderr(env, monkeypatch, config):
    import io

    env.setup(config, ["AAPL"], rows=50)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr("sys.stderr", stream)
    assert env.run() == 2  # no UnicodeEncodeError
    stream.flush()
    text = raw.getvalue().decode("cp1252")
    assert text.startswith("error: ") and "☃" not in text
    with pytest.raises(CliError) as info:
        validate_algo_config(config)
    str(info.value).encode("cp1252")  # the message itself is encodable


def test_unicode_message_values_are_shown_escaped():
    with pytest.raises(modes.ModeError, match=r"unknown mode '\\u2603'"):
        modes.parse_mode("a", "☃")


@pytest.mark.parametrize("text", [
    "-0", "-1", "+1", "1_000", "1 000", " 1", "1 ", "1\n", "", ".", "1.", "e5", "1e", "0x10",
    "٣", "1٣", "１", "nan", "inf", "Infinity", "1,5", "1/2", "--1", "1e+",
])
def test_parse_money_rejects_anything_but_plain_decimals(text):
    with pytest.raises(CliError, match="--x"):
        cli.parse_money(text, "--x")


@pytest.mark.parametrize("text, expected", [
    ("0", "0"), ("10", "10"), ("0.005", "0.005"), (".5", "0.5"), ("1e3", "1000"), ("1E+3", "1000"),
    ("0.0001", "0.0001"), ("5e-1", "0.5"), ("0.50", "0.50"),
])
def test_parse_money_accepts_plain_decimals(text, expected):
    assert cli.parse_money(text, "--x") == Decimal(expected)


def test_parse_money_extreme_exponents_are_errors_not_crashes():
    with pytest.raises(CliError, match="4 decimal"):
        cli.parse_money("1e-999999999999", "--x")
    with pytest.raises(CliError, match="too large"):
        cli.parse_money("1e999999999999", "--x")
    with pytest.raises(CliError, match="greater than 0"):
        cli.parse_money("0", "--capital", positive=True)


def test_router_keeps_modes_and_factories_are_read_only():
    entries = validate_algo_config([
        {"ID": "EMACrossing", "data": [EMA, {**EMA_MSFT, "mode": "paper"}]},
    ])
    router = modes.build_router(entries, fills="legacy", costs=CostModel(),
                                capital=Decimal(1), ledger=None)
    assert router.modes == {"AAPL-10/30EMA": "backtest", "MSFT-10/30EMA": "paper"}
    with pytest.raises(TypeError):
        modes.BROKER_FACTORIES["live"] = modes._simulated_broker  # type: ignore[index]
    assert set(modes.BROKER_FACTORIES) == {"backtest", "paper"}


def test_help_mentions_the_mode_key_and_the_stale_ledger_rule(capsys):
    with pytest.raises(SystemExit):
        main(["backtest", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert '"mode": "backtest" (default) or "paper"' in text
    assert "not available yet" in text
    assert "deletes a ledger.json left in --out" in text


# ---- B7 review carry-over: write order, mode key spelling, duplicate keys --------------


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_stale_ledger_removed_before_trades_json_is_written(env, monkeypatch, fills):
    env.setup(ema_config(), ["AAPL"], rows=300)
    assert env.run("--fills", "realistic") == 0
    stale = (env.out / "ledger.json").read_bytes()
    seen: list[tuple[str, bool]] = []
    real = cli.write_json_atomic

    def spy(path, payload):
        seen.append((path.name, (env.out / "ledger.json").exists()))
        return real(path, payload)

    monkeypatch.setattr(cli, "write_json_atomic", spy)
    assert env.run("--fills", fills) == 0
    # trades.json is written with no ledger.json beside it; a ledger.json comes last
    assert seen[0] == ("trades.json", False)
    assert [name for name, _ in seen] == (
        ["trades.json"] if fills == "legacy" else ["trades.json", "ledger.json"])
    assert (env.out / "ledger.json").exists() == (fills == "realistic")
    if fills == "realistic":
        assert (env.out / "ledger.json").read_bytes() == stale  # same data, rewritten


@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_failed_write_never_leaves_a_ledger_from_another_run(env, monkeypatch, fills):
    env.setup(ema_config(), ["AAPL"], rows=300)
    assert env.run("--fills", "realistic") == 0
    old_trades = env.trades_bytes()

    def refuse(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_json_atomic", refuse)
    with pytest.raises(OSError, match="disk full"):
        env.run("--fills", fills)
    assert not (env.out / "ledger.json").exists()
    assert env.trades_bytes() == old_trades  # the old trades.json is untouched, not half-written


@pytest.mark.parametrize("key", [" mode", "mode ", "\tMODE\n", " Mode ", "mode "])
def test_mode_key_with_whitespace_is_an_error(env, capsys, key):
    config = [{"ID": "EMACrossing", "data": [{**EMA, key: "live"}]}]
    with pytest.raises(CliError, match="must be spelled"):
        validate_algo_config(config)
    env.setup(config, ["AAPL"], rows=50)
    assert env.run() == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1
    assert not env.out.exists()


def test_exact_mode_key_and_unrelated_keys_still_accepted():
    config = [{"ID": "EMACrossing", "data": [{**EMA, "mode": "paper", "modes": 1, "model": 2}]}]
    assert validate_algo_config(config)[0]["data"][0]["mode"] == "paper"


DUPLICATES = {
    "mode_in_a_config": (
        '[{"ID": "EMACrossing", "data": [{"idname": "AAPL-10/30EMA", "ticker": "AAPL", '
        '"mode": "live", "mode": "paper"}]}]', "'mode'"),
    "id_in_an_entry": (
        '[{"ID": "EMACrossing", "ID": "FVG", "data": []}]', "'ID'"),
    "key_in_a_nested_object": (
        '[{"ID": "EMACrossing", "data": [{"idname": "a", "ticker": "AAPL", '
        '"opts": {"x": 1, "x": 2}}]}]', "'x'"),
}


@pytest.mark.parametrize("case", sorted(DUPLICATES))
def test_duplicate_json_keys_in_the_config_are_an_error(env, capsys, case):
    text, key = DUPLICATES[case]
    env.setup(ema_config(), ["AAPL"], rows=50)
    env.algos.write_text(text, encoding="utf-8")
    assert env.run() == 2
    captured = capsys.readouterr()
    assert captured.err == f"error: duplicate key {key} in the algo config\n"
    assert captured.out == "" and not env.out.exists()


def test_duplicate_json_keys_message_is_ascii_safe(env, capsys):
    env.setup(ema_config(), ["AAPL"], rows=50)
    env.algos.write_text('[{"ID": "E", "data": [], "\\u2603": 1, "\\u2603": 2}]', encoding="utf-8")
    assert env.run() == 2
    assert capsys.readouterr().err == "error: duplicate key '\\u2603' in the algo config\n"


def test_distinct_keys_and_repeated_values_are_fine(env):
    env.setup([{"ID": "EMACrossing", "data": [EMA, EMA_MSFT]}], ["AAPL", "MSFT"], rows=300)
    assert env.run() == 0
