"""The compare view (`views/compare.js`) and the equity chart's fit after a resize, run with node.

Same approach as `test_api_static.py`: the modules run as ES modules against a fake DOM and a fake chart
library, with the real functions of the page; the decimal differences are checked against `Decimal`.
"""

from __future__ import annotations

import copy
import json
import shutil
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from tests.test_api_static import FAKE_CHART_LIB, FAKE_DOM, JS_ROOT, require_node, run_node


@pytest.fixture(scope="module")
def compare_work(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of the dashboard modules that node runs as ES modules."""
    require_node()
    work = tmp_path_factory.mktemp("node_compare")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


def js(source: str, **values: object) -> str:
    """`source` with each `__NAME__` replaced by the JSON of the value (no brace doubling needed)."""
    for name, value in values.items():
        source = source.replace(f"__{name.upper()}__", json.dumps(value))
    return source


# --- the exact differences ---------------------------------------------------------------------

MONEY_CASES = [
    ("0.0000", "0.0000"), ("12.3450", "2.0000"), ("-5.0000", "-7.5000"), ("-0.0001", "0.0001"),
    ("100000.0000", "99987.6550"), ("99999999999999999999.9999", "0.0001"),
    ("-99999999999999999999.9999", "99999999999999999999.9999"), ("1", "2.5"), ("0", "-0.0000"),
    ("0.1000", "0.3000"), ("123456789012345678.1234", "123456789012345678.1234"), ("-0.0040", "0.0000"),
    ("5.5", "5.5000"), ("-1.2500", "-1.2499"),
]
RATE_CASES = [
    ("0.3750", "0.2500"), ("0.0000", "0.0015"), ("0.0015", "0.0000"), ("0.2500", "0.3750"),
    ("0.5000", "0.5000"), ("1.0000", "0.0000"), ("0.0375", "0.0000"), ("0.3333", "0.6667"),
    ("0.1005", "0.1000"), ("0.1000", "0.1005"),
]
INT_CASES = [(0, 0), (8, 3), (3, 8), (-2, 5), (12345678901234, 1), ("7", "9"), ("-4", 4)]


def test_exact_differences_match_decimal(compare_work: Path) -> None:
    out = run_node(compare_work, "differences", js("""
import { decimalDifference, integerDifference, percentPointDifference } from "./js/views/compare.js";
const money = __MONEY__, rates = __RATES__, ints = __INTS__;
console.log(JSON.stringify({
  money: money.map(([a, b]) => decimalDifference(a, b)),
  rates: rates.map(([a, b]) => percentPointDifference(a, b)),
  rates2: rates.map(([a, b]) => percentPointDifference(a, b, 2)),
  ints: ints.map(([a, b]) => integerDifference(a, b)),
  missing: [decimalDifference(null, "1.0000"), decimalDifference("1.0000", ""), decimalDifference("x", "1"),
    decimalDifference("1e3", "1"), decimalDifference(undefined, undefined), percentPointDifference(null, "0.5000"),
    percentPointDifference("0.5000", null), integerDifference(null, 1), integerDifference(1.5, 2),
    integerDifference("1.5", "2")],
}));
""", money=MONEY_CASES, rates=RATE_CASES, ints=INT_CASES))  # noqa: E501
    assert isinstance(out, dict)
    for (a, b), got in zip(MONEY_CASES, out["money"], strict=True):
        want = Decimal(b) - Decimal(a)
        assert Decimal(got) == want, (a, b, got)
        assert not got.startswith("-") or Decimal(got) != 0, (a, b, got)  # never "-0.0000"
        places = max(len(a.partition(".")[2]), len(b.partition(".")[2]))
        assert len(got.partition(".")[2]) == places, (a, b, got)
    for digits, key in ((1, "rates"), (2, "rates2")):
        for (a, b), got in zip(RATE_CASES, out[key], strict=True):
            want = ((Decimal(b) - Decimal(a)) * 100).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)  # noqa: E501
            assert Decimal(got) == want, (a, b, digits, got, want)
            assert len(got.partition(".")[2]) == digits and got != f"-{'0.' + '0' * digits}", (a, b, got)
    for (a, b), got in zip(INT_CASES, out["ints"], strict=True):
        assert got == str(int(b) - int(a)), (a, b, got)
    assert out["missing"] == [None] * 10


# --- the header table and the warnings ---------------------------------------------------------

REALISTIC = {
    "run_id": 3, "status": "completed", "incomplete": False, "kind": "backtest", "fills": "realistic",
    "started_at": "2026-11-22T17:31:13+00:00", "ended_at": "2026-11-22T17:31:25+00:00",
    "code_version": "0123456789abcdef", "symbols": ["AAPL", "MSFT"],
    "first_bar": "2024-01-02T09:30:00", "last_bar": "2024-01-05T16:00:00", "data_directory": "OfflineData",
    "capital": "100000.0000",
    "costs": {"commission_per_share": "0.0050", "commission_minimum": "1.0000", "slippage_per_share": "0.0100"},  # noqa: E501
}
LEGACY = {
    **REALISTIC, "run_id": 4, "fills": "legacy", "capital": None, "costs": None,
    "started_at": "2026-11-22T18:00:00+00:00", "ended_at": "2026-11-22T18:00:03+00:00",
}


def header_source(a: dict, b: dict) -> str:
    return js("""
import { comparisonWarnings, headerRows } from "./js/views/compare.js";
const a = __A__, b = __B__;
const rows = headerRows(a, b);
console.log(JSON.stringify({ rows, warnings: comparisonWarnings(a, b) }));
""", a=a, b=b)


def differing(rows: list[dict]) -> set[str]:
    return {r["key"] for r in rows if r["differs"]}


def test_header_rows_mark_every_difference(compare_work: Path) -> None:
    out = run_node(compare_work, "header_legacy", header_source(REALISTIC, LEGACY))
    assert isinstance(out, dict)
    by_key = {r["key"]: r for r in out["rows"]}
    assert [r["label"] for r in out["rows"]] == [
        "Status", "Kind", "Fill model", "Start (UTC)", "Duration", "Code version", "Capital per algo",
        "Commission per share", "Minimum per order", "Slippage per share", "Bar range", "Symbols", "Data folder"]  # noqa: E501
    # legacy: null capital and costs are "-" and differ from the realistic run's figures
    assert by_key["capital"]["a"] == "100,000.00" and by_key["capital"]["b"] == "-"
    assert by_key["commission"]["a"] == "0.0050" and by_key["commission"]["b"] == "-"
    assert by_key["fills"]["b"] == "legacy (bar-close fills, no commission)"
    assert by_key["range"]["a"] == "2024-01-02 09:30:00 to 2024-01-05 16:00:00 (data time)"
    assert by_key["started"]["a"] == "2026-11-22 17:31:13 UTC" and by_key["duration"]["a"] == "12.0 s"
    # only a setting both runs have and that differs is marked; the legacy run's missing capital and costs,
    # and the start time and duration (which always differ), are "not compared"
    assert differing(out["rows"]) == {"fills"}
    not_compared = {r["key"] for r in out["rows"] if not r["compared"]}
    assert not_compared == {"started", "duration", "capital", "commission", "minimum", "slippage"}
    assert by_key["fills"]["unfair"] == "fills" and by_key["started"]["unfair"] is None


def test_identical_settings_give_no_warnings_and_numeric_equality(compare_work: Path) -> None:
    other = {**REALISTIC, "run_id": 9, "capital": "100000", "symbols": ["MSFT", "AAPL"],
             "costs": {"commission_per_share": "0.005", "commission_minimum": "1", "slippage_per_share": "0.01"},  # noqa: E501
             "first_bar": "2024-01-02 09:30:00", "started_at": "2026-11-23T09:00:00+00:00",
             "ended_at": "2026-11-23T09:00:12+00:00"}
    out = run_node(compare_work, "header_same", header_source(REALISTIC, other))
    assert isinstance(out, dict)
    assert out["warnings"] == []  # 100000 = 100000.0000, 0.005 = 0.0050, symbol order, "T" or space
    assert differing(out["rows"]) == set()  # the start time differs but is never compared
    assert {r["key"] for r in out["rows"] if not r["compared"]} == {"started", "duration"}


@pytest.mark.parametrize(("change", "word", "keys"), [
    ({"fills": "legacy"}, "fill models differ", {"fills"}),
    ({"capital": "50000.0000"}, "capital per algo differs", {"capital"}),
    ({"costs": {"commission_per_share": "0.0100", "commission_minimum": "1.0000",
                "slippage_per_share": "0.0100"}}, "costs differ", {"commission"}),
    ({"first_bar": "2024-01-03T09:30:00"}, "bar ranges differ", {"range"}),
    ({"last_bar": "2024-01-04T16:00:00"}, "bar ranges differ", {"range"}),
    ({"symbols": ["AAPL"]}, "symbols differ", {"symbols"}),
])
def test_each_unfair_difference_gives_one_warning(compare_work: Path, change: dict, word: str,
                                                  keys: set[str]) -> None:
    out = run_node(compare_work, "header_one", header_source(REALISTIC, {**REALISTIC, "run_id": 5, **change}))
    assert isinstance(out, dict)
    assert len(out["warnings"]) == 1 and word in out["warnings"][0], out["warnings"]
    assert differing(out["rows"]) == keys


def test_failed_and_incomplete_runs_are_warned_about(compare_work: Path) -> None:
    out = run_node(compare_work, "header_failed", header_source(
        {**REALISTIC, "status": "failed"}, {**REALISTIC, "status": "running", "incomplete": True}))
    assert isinstance(out, dict)
    assert len(out["warnings"]) == 2
    assert out["warnings"][0].startswith("Run A: This run failed")
    assert out["warnings"][1].startswith("Run B: This run is incomplete")
    assert "status" in differing(out["rows"])


def warnings_source(a: dict, b: dict, options: dict) -> str:
    return js("""
import { comparisonWarnings } from "./js/views/compare.js";
console.log(JSON.stringify(comparisonWarnings(__A__, __B__, __OPTIONS__)));
""", a=a, b=b, options=options)


TWO_ALGOS = {"onlyA": [], "onlyB": [], "countA": 2, "countB": 2}
CAPITAL_20K = {"capital": "20000.0000"}


def test_legacy_against_realistic_gets_notes_not_differences(compare_work: Path) -> None:
    both = {"algos": TWO_ALGOS, "equities": [CAPITAL_20K, CAPITAL_20K]}
    out = run_node(compare_work, "warn_legacy", warnings_source(REALISTIC, LEGACY, both))
    assert out == [
        "The fill models differ (run A: realistic, run B: legacy (bar-close fills, no commission)). "
        "Prices, costs and trade counts are not comparable.",
        "Run B stores no capital setting (legacy fills use a nominal 10,000.00 per algo).",
        "Run B stores no cost settings (legacy fills charge no commission).",
    ]
    # the legacy run first: the note names run A
    swapped = run_node(compare_work, "warn_legacy_swapped", warnings_source(LEGACY, REALISTIC, both))
    assert isinstance(swapped, list)
    assert "Run A stores no capital setting (legacy fills use a nominal 10,000.00 per algo)." in swapped
    assert "Run A stores no cost settings (legacy fills charge no commission)." in swapped
    # without a readable nominal figure no number is given, whatever the reason
    for name, options in [("none", {}), ("no_equity", {"algos": TWO_ALGOS, "equities": [None, None]}),
                          ("no_algos", {"algos": {**TWO_ALGOS, "countB": 0}, "equities": [CAPITAL_20K] * 2}),
                          ("bad_capital", {"algos": TWO_ALGOS,
                                           "equities": [CAPITAL_20K, {"capital": "n/a"}]})]:
        source = warnings_source(REALISTIC, LEGACY, options)
        plain = run_node(compare_work, "warn_plain_" + name, source)
        assert isinstance(plain, list)
        assert "Run B stores no capital setting." in plain, (name, plain)
        assert not any("nominal" in w or "differs (run A: 100,000.00" in w for w in plain), (name, plain)


def test_capital_and_cost_differences_need_both_sides(compare_work: Path) -> None:
    out = run_node(compare_work, "warn_capital", warnings_source(
        REALISTIC, {**REALISTIC, "capital": "50000.0000"}, {}))
    assert isinstance(out, list) and len(out) == 1
    assert out[0].startswith("The capital per algo differs (run A: 100,000.00, run B: 50,000.00)")
    # two legacy runs: neither stores capital or costs, so there is nothing to say
    none = run_node(compare_work, "warn_two_legacy", warnings_source(LEGACY, {**LEGACY, "run_id": 7}, {}))
    assert none == []
    # one cost setting missing on one side of two realistic runs: not a difference
    partial = {**REALISTIC, "costs": {"commission_per_share": "0.0050", "commission_minimum": None,
                                      "slippage_per_share": "0.0100"}}
    got = run_node(compare_work, "warn_partial", warnings_source(REALISTIC, partial, {}))
    assert got == []


def test_different_algo_sets_are_warned_about(compare_work: Path) -> None:
    out = run_node(compare_work, "warn_algos", js("""
import { algoSets, comparisonWarnings } from "./js/views/compare.js";
const doc = __DOC__;
const sets = algoSets(doc, [3, 4]);
const sameSets = algoSets({ algos: { X: { 3: { net: "1" }, 4: { net: "2" } } } }, [3, 4]);
console.log(JSON.stringify({
  sets, sameSets,
  warnings: comparisonWarnings(doc.runs[0], { ...doc.runs[0], run_id: 4 }, { algos: sets }),
  same: comparisonWarnings(doc.runs[0], { ...doc.runs[0], run_id: 4 }, { algos: sameSets }),
  oneSided: comparisonWarnings(doc.runs[0], { ...doc.runs[0], run_id: 4 }, {
    algos: { onlyA: ["MSFT"], onlyB: [], countA: 2, countB: 1 } }),
}));
""", doc=COMPARE_DOC))
    assert isinstance(out, dict)
    assert out["sets"] == {"onlyA": ["B-algo"], "onlyB": ["C-algo"], "countA": 2, "countB": 2}
    assert out["sameSets"] == {"onlyA": [], "onlyB": [], "countA": 1, "countB": 1}
    assert out["warnings"] == [
        "The runs have different algos (only in run A: B-algo; only in run B: C-algo). "
        "The TOTAL rows and the equity curves then cover different algos."]
    assert out["same"] == []
    assert out["oneSided"] == [
        "The runs have different algos (only in run A: MSFT; only in run B: none). "
        "The TOTAL rows and the equity curves then cover different algos."]


def test_final_equity_difference_is_coloured_only_on_one_capital(compare_work: Path) -> None:
    out = run_node(compare_work, "capital_colour", js("""
import { buildStatRows, capitalsComparable } from "./js/views/compare.js";
const doc = __DOC__, [a, b] = doc.runs;
const eqs = (x, y) => [{ capital: x }, { capital: y }];
const colour = (options) => buildStatRows(doc, [3, 4], doc.runs, options)[2].cells;
const abs = (r) => Object.fromEntries(Object.entries(r).map(([k, c]) => [k, c.className]));
console.log(JSON.stringify({
  sameStored: capitalsComparable(a, { ...b, capital: "100000" }, []),
  differStored: capitalsComparable(a, { ...b, capital: "5.0000" }, eqs("1", "1")),
  oneStored: capitalsComparable(a, b, eqs("20000.0000", "20000.0000")),
  bothAbsentEqual: capitalsComparable(b, { ...b }, eqs("20000.0000", "20000")),
  bothAbsentUnequal: capitalsComparable(b, { ...b }, eqs("20000.0000", "30000.0000")),
  bothAbsentNoEquity: capitalsComparable(b, { ...b }, [null, null]),
  on: abs(colour({ colourFinal: true })), off: abs(colour({})),
}));
""", doc=COMPARE_DOC))
    assert isinstance(out, dict)
    assert out["sameStored"] is True and out["differStored"] is False and out["oneStored"] is False
    assert out["bothAbsentEqual"] is True and out["bothAbsentUnequal"] is False
    assert out["bothAbsentNoEquity"] is False
    assert out["on"]["finalEquity"] == "pos" and out["on"]["net"] == "pos"
    assert out["off"]["finalEquity"] == "" and out["off"]["net"] == "pos"  # net stays coloured
    for key in ("commission", "maxDrawdown", "avgWin", "avgLoss", "trades", "wins", "winPercent"):
        assert out["on"][key] == "" and out["off"][key] == "", key


# --- the statistics rows -----------------------------------------------------------------------


def stats(trades: int, wins: int, rate: str | None, net: str, final: str, **extra: str | None) -> dict:
    return {"symbol": "AAPL", "algo": "X", "mode": "long", "trade_count": trades, "wins": wins,
            "win_rate": rate, "net": net, "commission": "8.0000", "avg_win": "5.0000",
            "avg_loss": "-6.2000", "max_drawdown": "30.0000", "final_equity": final, **extra}


COMPARE_DOC = {
    "runs": [REALISTIC, LEGACY],
    "algos": {
        "A-algo": {"3": stats(8, 3, "0.3750", "-12.3450", "99987.6550"),
                   "4": stats(10, 6, "0.6000", "40.0000", "100040.0000")},
        "B-algo": {"3": stats(0, 0, None, "0.0000", "100000.0000", avg_win=None, avg_loss=None),
                   "4": None},
        "C-algo": {"3": None, "4": stats(2, 1, "0.5000", "1.5000", "100001.5000")},
    },
    "total": {"3": stats(8, 3, "0.3750", "-12.3450", "199987.6550"),
              "4": stats(12, 7, "0.5833", "41.5000", "200041.5000")},
}


def test_stat_rows_have_three_rows_per_block_with_exact_differences(compare_work: Path) -> None:
    out = run_node(compare_work, "stat_rows", js("""
import { buildStatRows, METRICS } from "./js/views/compare.js";
const doc = __DOC__;
const text = (row) => Object.fromEntries(Object.entries(row.cells).map(([k, c]) => [k, c.text]));
const rows = buildStatRows(doc, [3, 4], doc.runs, { colourFinal: true });
// the same document with the ids swapped, and a document without algos
const swapped = buildStatRows(doc, [4, 3], [doc.runs[1], doc.runs[0]]);
console.log(JSON.stringify({
  metrics: METRICS.map((m) => m.label),
  shape: rows.map((r) => [r.block, r.kind, r.label, r.total]),
  a: text(rows[0]), b: text(rows[1]), diff: text(rows[2]), diffClasses: Object.fromEntries(
    Object.entries(rows[2].cells).map(([k, c]) => [k, c.className])),
  noTrades: text(rows[3]), missingB: [text(rows[4]), text(rows[5])], missingA: [text(rows[6]), text(rows[7])],
  swapped: swapped.map((r) => [r.block, r.label]),
  none: buildStatRows({ algos: {}, total: {} }, [3, 4]).map((r) => [r.block, r.label, text(r)["net"]]),
}));
""", doc=COMPARE_DOC))
    assert isinstance(out, dict)
    assert out["metrics"] == ["Trades", "Wins", "Win %", "Net", "Commission", "Avg win", "Avg loss",
                              "Max drawdown", "Final equity"]
    labels = [("a", "Run A"), ("b", "Run B"), ("diff", "B - A")]
    assert out["shape"] == [[name, kind, label, name == "TOTAL"]
                            for name in ("A-algo", "B-algo", "C-algo", "TOTAL") for kind, label in labels]
    assert out["a"] == {"trades": "8", "wins": "3", "winPercent": "37.5", "net": "-12.35", "commission": "8.00",  # noqa: E501
                        "avgWin": "5.00", "avgLoss": "-6.20", "maxDrawdown": "30.00", "finalEquity": "99,987.66"}  # noqa: E501
    # B - A: counts as integers, win % in percentage points, money exact then rounded half away from zero
    assert out["diff"] == {"trades": "+2", "wins": "+3", "winPercent": "+22.5", "net": "+52.35",
                           "commission": "0.00", "avgWin": "0.00", "avgLoss": "0.00",
                           "maxDrawdown": "0.00", "finalEquity": "+52.35"}
    assert out["diffClasses"]["net"] == "pos" and out["diffClasses"]["finalEquity"] == "pos"
    assert out["diffClasses"]["trades"] == "" and out["diffClasses"]["commission"] == ""
    assert out["noTrades"]["winPercent"] == "-" and out["noTrades"]["avgWin"] == "-"
    dash = {key: "-" for key in out["a"]}
    assert out["missingB"][1] == dash and out["missingA"][0] == dash  # the run without the algo
    assert out["noTrades"]["net"] == "0.00"
    # a missing algo has no difference either: the B - A rows of B-algo and C-algo are all dashes
    # asked as 4,3 the algos of run 4 come first: A-algo, C-algo, then B-algo (only in run 3)
    assert [row[0] for row in out["swapped"][::3]] == ["A-algo", "C-algo", "B-algo", "TOTAL"]
    assert out["swapped"][3:6] == [["C-algo", "Run A"], ["C-algo", "Run B"], ["C-algo", "B - A"]]
    assert out["none"] == [["TOTAL", "Run A", "-"], ["TOTAL", "Run B", "-"], ["TOTAL", "B - A", "-"]]


def test_missing_algo_difference_rows_are_dashes(compare_work: Path) -> None:
    out = run_node(compare_work, "stat_missing", js("""
import { buildStatRows } from "./js/views/compare.js";
const doc = __DOC__;
const rows = buildStatRows(doc, [3, 4], doc.runs);
const find = (block, kind) => rows.find((r) => r.block === block && r.kind === kind);
console.log(JSON.stringify({
  b: Object.values(find("B-algo", "diff").cells).map((c) => c.text),
  c: Object.values(find("C-algo", "diff").cells).map((c) => c.text),
  order: [...new Set(rows.map((r) => r.block))],
}));
""", doc=COMPARE_DOC))
    assert isinstance(out, dict)
    assert set(out["b"]) == {"-"} and set(out["c"]) == {"-"}
    assert out["order"] == ["A-algo", "B-algo", "C-algo", "TOTAL"]  # run A's algos first, then the rest


# --- the view ----------------------------------------------------------------------------------

EQUITY_3 = {"run_id": 3, "step": True, "capital": "100000.0000", "stored_points": 3, "points": [
    {"time": "2024-01-02 09:30:00", "equity": "100000.0000"},
    {"time": "2024-01-02 09:31:00", "equity": "99950.0000"},
    {"time": "2024-01-02 09:32:00", "equity": "99987.6550"}]}
EQUITY_4 = {"run_id": 4, "step": True, "capital": "200000.0000", "stored_points": 2, "points": [
    {"time": "2024-01-02 09:30:00", "equity": "200000.0000"},
    {"time": "2024-01-02 09:31:00", "equity": "200040.0000"}]}

VIEW_SOURCE = FAKE_DOM + FAKE_CHART_LIB + """
const { render } = await import("./js/views/compare.js");
const doc = __DOC__, equity = { 3: __E3__, 4: __E4__ };
function respond(body, status = 200) {
  return Promise.resolve({ ok: status < 400, status, statusText: "x", json: async () => body });
}
const seen = [];
globalThis.fetch = (url) => {
  seen.push(url);
  const m = /\\/api\\/runs\\/(\\d+)\\/equity/.exec(url);
  if (globalThis.fail === "doc" && !m) return respond({ detail: "no run 3" }, 404);
  if (globalThis.fail === "bad" && !m) return respond({ runs: [] });
  if (globalThis.fail === "down") return Promise.reject(new TypeError("Failed to fetch"));
  if (m && (globalThis.fail === "equity4" || globalThis.fail === "both") && m[1] === "4") return respond({ detail: "equity 4 broke" }, 500);
  if (m && (globalThis.fail === "equity3" || globalThis.fail === "both") && m[1] === "3") return respond({ detail: "equity 3 broke" }, 500);
  return respond(m ? equity[m[1]] : doc);
};
const out = {};
for (const fail of ["", "doc", "bad", "down", "equity4", "equity3", "both"]) {
  globalThis.fail = fail;
  globalThis.lastChart = null;
  seen.length = 0;
  const cleanups = [];
  const ctx = { signal: new AbortController().signal, isCurrent: () => true, onCleanup: (f) => cleanups.push(f) };
  const box = document.createElement("div");
  await render(box, { runIds: __IDS__ }, ctx);
  const chart = globalThis.lastChart;
  const legend = box.querySelector(".chart-legend");
  out[fail || "ok"] = {
    h2: box.all("h2").map((x) => x.textContent), links: box.all("a").map((a) => a.getAttribute("href")),
    errors: box.all("div").filter((d) => d.className.includes("error")).map((d) => d.textContent),
    tables: box.all("table").length, charts: box.all("div").filter((d) => d.className === "chart-box").length,
    sections: box.all("section").length, notes: box.all("p").filter((p) => p.className.includes("compare-note"))
      .map((p) => p.textContent), cleanups: cleanups.length, seen: seen.slice().sort(),
    series: chart ? chart.series.map((s) => [s.type, s.data.map((d) => d.value)]) : null,
    legend: legend ? legend.textContent : null,
    chartText: box.all("p").filter((p) => p.className.includes("chart-caption")).map((p) => p.textContent),
    warnings: box.all("ul").filter((u) => u.className.includes("compare-warnings"))
      .flatMap((u) => u.children.map((li) => li.textContent)),
    headers: box.all("table").map((t) => t.all("th").map((th) => th.textContent)),
    rows: box.all("table").map((t) => t.all("tbody")[0].children.map((tr) => tr.children.map((td) => td.textContent))),
    colours: chart ? chart.series.map((s) => s.opts.color ?? s.opts.topLineColor) : null,
    differsCells: box.all("td").filter((td) => td.className.split(" ").includes("differs")).length,
  };
}
console.log(JSON.stringify(out));
"""  # noqa: E501


def rounded(values: list[float]) -> list[float]:
    """Chart values are floats for drawing only; compare them to the cent and beyond."""
    return [round(v, 6) for v in values]


def run_view(compare_work: Path, name: str, ids: list[int], doc: dict | None = None) -> dict:
    source = js(VIEW_SOURCE, doc=doc or COMPARE_DOC, e3=EQUITY_3, e4=EQUITY_4, ids=ids)
    out = run_node(compare_work, name, source)
    assert isinstance(out, dict)
    return out


def test_view_draws_header_chart_and_statistics(compare_work: Path) -> None:
    ok = run_view(compare_work, "view_ok", [3, 4])["ok"]
    assert ok["h2"] == ["Compare runs 3 and 4"] and ok["errors"] == [] and ok["notes"] == []
    assert ok["links"] == ["#/run/3", "#/run/4", "#/"]
    assert ok["seen"] == ["/api/compare?runs=3%2C4", "/api/runs/3/equity", "/api/runs/4/equity"]
    assert ok["tables"] == 2 and ok["charts"] == 1 and ok["cleanups"] >= 1
    # one line and a drawdown pane per run, each as profit and loss from its own starting capital
    assert [s[0] for s in ok["series"]] == ["line", "baseline", "line", "baseline"]
    assert rounded(ok["series"][0][1]) == [0.0, -50.0, -12.345] and rounded(ok["series"][2][1]) == [0.0, 40.0]
    assert "Run A (realistic)" in ok["legend"] and "Run B (legacy)" in ok["legend"]
    assert "profit and loss" in ok["chartText"][0].lower() and "starting capital" in ok["chartText"][0]
    assert ok["headers"][0] == ["Setting", "Run A, run 3", "Run B, run 4", "Same or differs"]
    assert ok["headers"][1][:2] == ["Algo", "Run"]
    assert len(ok["rows"][0]) == 13 and len(ok["rows"][1]) == 12  # 13 settings; 3 algo-blocks x 3 + TOTAL x 3
    marked = sum(row[3] == "Differs" for row in ok["rows"][0])
    assert marked == 1 and ok["differsCells"] == 4 * marked  # the marker is text, not colour only
    # start, duration and the legacy run's missing capital and costs say "Not compared" in the last column
    assert sum(row[3] == "Not compared" for row in ok["rows"][0]) == 6
    assert ok["colours"][0] != ok["colours"][2] and ok["colours"][0] == ok["colours"][1]
    # run B of the fixture is a legacy run in the header: the warning list says so
    assert any("fill models differ" in w for w in ok["warnings"])


def test_compare_document_failing_gives_only_one_error_panel(compare_work: Path) -> None:
    out = run_view(compare_work, "view_doc_fails", [3, 4])
    for case, message in [("doc", "Error 404: no run 3"), ("down", "The server could not be reached."),
                          ("bad", "The server sent an unexpected response.")]:
        failed = out[case]
        assert failed["errors"] == [message], case
        assert failed["h2"] == ["Compare runs 3 and 4"], case
        assert failed["links"] == ["#/run/3", "#/run/4", "#/"], case
        shown = (failed["tables"], failed["charts"], failed["sections"], failed["series"])
        assert shown == (0, 0, 0, None), case
        assert failed["cleanups"] == 0, case


def test_one_failing_equity_keeps_the_chart_with_the_other_run(compare_work: Path) -> None:
    out = run_view(compare_work, "view_equity_fails", [3, 4])
    four = out["equity4"]
    assert four["errors"] == ["The equity of run B (run 4) could not be loaded. Error 500: equity 4 broke"]
    assert four["charts"] == 1 and four["tables"] == 2
    assert [s[0] for s in four["series"]] == ["line", "baseline"]
    assert rounded(four["series"][0][1]) == [0.0, -50.0, -12.345]
    assert "Run A (realistic)" in four["legend"] and "Run B" not in four["legend"]
    three = out["equity3"]
    # run B drawn alone keeps run B's colour, not the first colour
    assert three["colours"] == out["ok"]["colours"][2:] and three["colours"][0] != out["ok"]["colours"][0]
    assert four["colours"] == out["ok"]["colours"][:2]
    assert three["errors"] == ["The equity of run A (run 3) could not be loaded. Error 500: equity 3 broke"]
    assert rounded(three["series"][0][1]) == [0.0, 40.0] and "Run B (legacy)" in three["legend"]
    both = out["both"]
    assert len(both["errors"]) == 2 and both["charts"] == 0 and both["series"] is None
    assert both["tables"] == 2  # the header and statistics tables stay
    assert "run A" in both["errors"][0] and "run B" in both["errors"][1]


def test_more_than_two_ids_say_which_two_are_shown(compare_work: Path) -> None:
    out = run_view(compare_work, "view_many", [3, 4, 8, 9])
    ok = out["ok"]
    assert ok["h2"] == ["Compare runs 3 and 4"]
    assert ok["seen"] == ["/api/compare?runs=3%2C4", "/api/runs/3/equity", "/api/runs/4/equity"]
    assert len(ok["notes"]) == 1
    assert "run 3 (A) and run 4 (B)" in ok["notes"][0] and "first two of 4" in ok["notes"][0]
    assert "run 8, run 9" in ok["notes"][0]
    assert out["doc"]["notes"] == ok["notes"]  # also shown when the document fails


def test_a_failed_run_is_labelled_in_its_column_header(compare_work: Path) -> None:
    doc = copy.deepcopy(COMPARE_DOC)
    doc["runs"][1] = {**LEGACY, "status": "failed"}
    ok = run_view(compare_work, "view_failed_run", [3, 4], doc)["ok"]
    assert ok["headers"][0][2] == "Run B (failed), run 4"
    assert any(w.startswith("Run B: This run failed") for w in ok["warnings"])
    assert ok["rows"][1][1][1] == "Run B (failed)"


# --- charts/equity.js: fit after a resize --------------------------------------------------------

SIZE_CHART_LIB = FAKE_CHART_LIB.replace(
    "timeScale: () => ({ fitContent: () => calls.push([\"fit\"]) }),",
    "timeScale: () => timeScale,").replace(
    "const calls = [];",
    """const calls = [];
const sizeHandlers = [];
const timeScale = { fitContent: () => calls.push(["fit"]),
  subscribeSizeChange: (f) => sizeHandlers.push(f),
  unsubscribeSizeChange: (f) => sizeHandlers.splice(sizeHandlers.indexOf(f), 1) };
globalThis.calls = calls; globalThis.sizeHandlers = sizeHandlers;""")


def test_equity_chart_fits_the_whole_series_after_a_resize(compare_work: Path) -> None:
    out = run_node(compare_work, "equity_fit", FAKE_DOM + SIZE_CHART_LIB + """
const { createEquityChart } = await import("./js/charts/equity.js");
const cleanups = [];
const ctx = { onCleanup: (f) => cleanups.push(f) };
const box = document.createElement("div");
const handle = createEquityChart(box, { ctx });
const canvas = box.querySelector(".chart-canvas");
const fits = () => calls.filter((c) => c[0] === "fit").length;
const pts = [{ time: "2024-01-02 09:30:00", equity: "100.0000" }, { time: "2024-01-02 09:31:00", equity: "90.0000" }];
const out = { subscribed: sizeHandlers.length, beforeData: 0 };
sizeHandlers[0](); out.beforeData = fits(); // a resize before any data: nothing to fit
handle.setSeries([{ label: "A", points: pts, capital: "100.0000" }]);
out.afterData = fits();
sizeHandlers[0](); out.afterResize = fits(); // the width changed: fitted again
// the user zooms: a resize leaves the view alone
canvas.fire("wheel", {});
sizeHandlers[0](); out.afterZoom = fits();
handle.setSeries([{ label: "A", points: pts, capital: "100.0000" }]); // new data: whole series again
out.afterNewData = fits();
// a hover, a click with a little jitter (4 px or less) and a one-finger touch are not zooms
canvas.fire("pointermove", { buttons: 0, clientX: 50, clientY: 50 });
sizeHandlers[0](); out.afterHover = fits();
canvas.fire("pointerdown", { buttons: 1, clientX: 10, clientY: 10 });
canvas.fire("pointermove", { buttons: 1, clientX: 14, clientY: 10 }); // exactly 4 px
canvas.fire("pointermove", { buttons: 1, clientX: 13, clientY: 12 });
canvas.fire("pointerup", { buttons: 0 });
sizeHandlers[0](); out.afterClick = fits();
canvas.fire("touchstart", { touches: [{}] });
canvas.fire("touchmove", { touches: [{}] }); // one finger scrolling the page
sizeHandlers[0](); out.afterOneFinger = fits();
// a button-down move without a recorded press is no drag either
canvas.fire("pointermove", { buttons: 1, clientX: 400, clientY: 400 });
sizeHandlers[0](); out.afterStray = fits();
// a pointer dragged more than 4 px with a button down is
canvas.fire("pointerdown", { buttons: 1, clientX: 10, clientY: 10 });
canvas.fire("pointermove", { buttons: 1, clientX: 30, clientY: 10 });
sizeHandlers[0](); out.afterDrag = fits();
handle.setSeries([{ label: "A", points: pts, capital: "100.0000" }]);
out.afterNewData2 = fits();
canvas.fire("touchmove", { touches: [{}, {}] }); // two fingers: pinch
sizeHandlers[0](); out.afterPinch = fits();
handle.dispose(); handle.dispose();
sizeHandlers.length === 0 || (out.leaked = sizeHandlers.length);
out.afterDispose = sizeHandlers.length;
out.cleanups = cleanups.length;
console.log(JSON.stringify(out));
""")  # noqa: E501
    assert isinstance(out, dict)
    assert out["subscribed"] == 1 and out["beforeData"] == 0
    assert out["afterData"] == 1 and out["afterResize"] == 2
    assert out["afterZoom"] == 2  # the user's own zoom is not fought
    assert out["afterNewData"] == 3 and out["afterHover"] == 4
    assert out["afterClick"] == 5 and out["afterOneFinger"] == 6 and out["afterStray"] == 7
    assert out["afterDrag"] == 7  # the drag stopped the refit
    assert out["afterNewData2"] == 8 and out["afterPinch"] == 8  # new data refits; a pinch stops it again
    assert out["afterDispose"] == 0 and "leaked" not in out  # the size listener is removed with the chart
    assert out["cleanups"] >= 1  # the disposal is registered with ctx at creation


def test_equity_chart_sets_a_tiny_min_bar_spacing_beside_the_theme_options(compare_work: Path) -> None:
    out = run_node(compare_work, "equity_spacing", FAKE_DOM + FAKE_CHART_LIB + """
const { createEquityChart, EQUITY_MIN_BAR_SPACING } = await import("./js/charts/equity.js");
const { chartOptions, readTheme } = await import("./js/charts/theme.js");
createEquityChart(document.createElement("div"), { ctx: { onCleanup() {} } });
const created = globalThis.lastChart.options;
console.log(JSON.stringify({ created: created.timeScale, constant: EQUITY_MIN_BAR_SPACING,
  theme: chartOptions(readTheme(globalThis)).timeScale, autoSize: created.autoSize }));
""")  # noqa: E501
    assert isinstance(out, dict)
    assert out["constant"] == 0.01 and out["autoSize"] is True
    # the theme's own timeScale options are all still there, plus the minimum; the shared theme has no minimum
    assert "minBarSpacing" not in out["theme"] and out["theme"]["timeVisible"] is True
    assert out["created"] == {**out["theme"], "minBarSpacing": 0.01}


def test_equity_chart_colour_index_picks_the_theme_colour_of_that_index(compare_work: Path) -> None:
    out = run_node(compare_work, "equity_colour_index", FAKE_DOM + FAKE_CHART_LIB + """
const { createEquityChart } = await import("./js/charts/equity.js");
const pts = [{ time: "2024-01-02 09:30:00", equity: "1.0000" }, { time: "2024-01-02 09:31:00", equity: "2.0000" }];
const handle = createEquityChart(document.createElement("div"), { ctx: { onCleanup() {} } });
const colours = (list) => { handle.setSeries(list); return globalThis.lastChart.series.map((s) => s.opts.color ?? s.opts.topLineColor); };
const entry = (extra) => ({ label: "X", points: pts, capital: "1.0000", ...extra });
console.log(JSON.stringify({
  position: colours([entry({}), entry({})]), alone: colours([entry({})]),
  aloneAsB: colours([entry({ colorIndex: 1 })]), bothIndexed: colours([entry({ colorIndex: 0 }), entry({ colorIndex: 1 })]),
  invalid: colours([entry({ colorIndex: 7 })]), hex: colours([entry({ colorIndex: 1, color: "#abcdef" })]),
}));
""")  # noqa: E501
    assert isinstance(out, dict)
    first, second = out["position"][0], out["position"][2]
    assert first != second and out["alone"] == [first, first]
    assert out["aloneAsB"] == [second, second]  # run B alone keeps run B's colour
    assert out["bothIndexed"] == [first, first, second, second]
    assert out["invalid"] == [first, first]  # only 0 and 1 are accepted
    assert out["hex"][0] == "#abcdef"  # an explicit colour still wins


def test_equity_chart_without_size_events_still_works(compare_work: Path) -> None:
    out = run_node(compare_work, "equity_fit_plain", FAKE_DOM + FAKE_CHART_LIB + """
const { createEquityChart } = await import("./js/charts/equity.js");
const ctx = { onCleanup() {} };
const handle = createEquityChart(document.createElement("div"), { ctx });
handle.setSeries([{ label: "A", points: [{ time: "2024-01-02 09:30:00", equity: "1.0000" }], capital: "1.0000" }]);
handle.dispose();
console.log(JSON.stringify({ fits: calls.filter((c) => c[0] === "fit").length }));
""")  # noqa: E501
    assert out == {"fits": 1}
