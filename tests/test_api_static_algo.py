# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""The algo detail view and its candle chart (D14), exercised with node.

The pure helpers run as they are; the chart and the view run against a fake DOM, a fake chart library and a
fake `fetch` (the ones of `tests/test_api_static.py`, which also owns the shared fixtures).
"""

from __future__ import annotations

import calendar
import re
import shutil
import time
from pathlib import Path

import pytest

from tests.api_support import make_client
from tests.test_api_static import (
    FAKE_DOM,
    JS_FILES,
    JS_ROOT,
    STATIC,
    page_references,
    require_node,
    run_node,
)


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from MainStoinker.engine.store import SqliteStore

    path = tmp_path_factory.mktemp("static_algo") / "empty.db"
    with SqliteStore(path, env={}):
        pass
    return path


@pytest.fixture(scope="module")
def node_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of the dashboard modules that node runs as ES modules (no DOM, no network)."""
    require_node()
    work = tmp_path_factory.mktemp("node_algo")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


def seconds(text: str) -> int:
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


# --- the files ---------------------------------------------------------------------------------


def test_new_files_are_referenced_and_served(empty_db: Path) -> None:
    refs = {t for _k, t in page_references(STATIC / "js" / "views" / "algo.js")}
    assert "../charts/candles.js" in refs and "../charts/equity.js" in refs
    client = make_client(empty_db)
    for url in ("/js/charts/candles.js", "/js/views/algo.js", "/js/table.js", "/app.css"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert len(response.text) > 200


def test_no_selected_state_attribute_of_the_grid_pattern_anywhere() -> None:
    pages = [*JS_FILES, STATIC / "app.css", STATIC / "index.html"]
    assert len(pages) > 10
    for path in pages:
        assert "aria-selected" not in path.read_text(encoding="utf-8"), path.name


def test_algo_view_declares_its_chart_disposal_and_request_scope() -> None:
    text = (STATIC / "js" / "charts" / "candles.js").read_text(encoding="utf-8")
    assert re.search(r"lib\.createChart\([^\n]*\n\n?\s*function dispose", text)
    assert "ctx.onCleanup(dispose);" in text
    assert "SEAM" in text and "handle.chart" in text and "handle.series" in text
    view = (STATIC / "js" / "views" / "algo.js").read_text(encoding="utf-8")
    assert "ctx.child()" in view and "Promise.allSettled" in view and "request.abort()" in view


# --- candles.js: pure helpers ----------------------------------------------------------------


MARKER_SOURCE = """
import { buildMarkers, convertBars, focusRange, shortReason, snapIndex, tradesInWindow, describeCandles }
  from "./js/charts/candles.js";

const bars = ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35"].map((m) =>
  ({ time: "2024-01-02 " + m + ":00", open: "10", high: 11, low: 9.5, close: 10.5, volume: 100 }));
const times = convertBars(bars).map((b) => b.time);
const trades = [
  { key: 1, number: 1, side: "long", entry_time: "2024-01-02 09:31:00", exit_time: "2024-01-02 09:33:30",
    net: "5.0000", exit_reason: "target" },
  { key: 2, number: 2, side: "short", entry_time: "2024-01-02 09:32:00", exit_time: "2024-01-02 09:34:00",
    net: "-3.0000", exit_reason: "stop" },
  { key: 3, number: 3, side: "long", entry_time: "2024-01-01 15:59:00", exit_time: "2024-01-02 09:31:00",
    net: "0.0000", exit_reason: "end_of_data" },
  { key: 4, number: 4, side: "long", entry_time: "2024-01-03 09:31:00", exit_time: "2024-01-03 09:40:00",
    net: "1.0000", exit_reason: "target" },
];
const theme = { accent: "#0000ff", pos: "#00ff00", neg: "#ff0000", muted: "#888888", text: "#000000" };
const out = {
  times,
  plain: buildMarkers(trades, times, null, theme),
  picked: buildMarkers(trades, times, 2, theme),
  keyAsText: buildMarkers(trades, times, "2", theme).map((m) => m.size),
  unknownSelection: buildMarkers(trades, times, 99, theme).map((m) => m.size),
  none: buildMarkers(trades, [], null, theme),
  window: tradesInWindow(trades, times[0], times[times.length - 1]).map((t) => t.key),
  windowEmpty: tradesInWindow(trades, 0, 1).map((t) => t.key),
  windowBad: tradesInWindow(null, 0, 1),
  snap: [snapIndex(times, times[0] - 1), snapIndex(times, times[0]), snapIndex(times, times[2] + 30),
    snapIndex(times, times[5]), snapIndex(times, times[5] + 999), snapIndex([], 5)],
  reasons: [shortReason("stop"), shortReason("target"), shortReason("algo_exit"), shortReason("end_of_data"),
    shortReason("a_very_long_reason_name"), shortReason(null), shortReason(""), shortReason("__proto__")],
  focus: [focusRange(times, times[2], 60), focusRange(times, times[0], 3600), focusRange(times, times[5] + 5000, 60),
    focusRange([], times[0], 60), focusRange(times, "x", 60), focusRange([times[0]], times[0], 60)],
  label: [describeCandles(times, 3), describeCandles(times, 1), describeCandles([], 0)],
};
console.log(JSON.stringify(out));
"""


def test_candle_pure_helpers_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_pure", MARKER_SOURCE)
    assert isinstance(out, dict)
    t = [seconds(f"2024-01-02 09:{m}:00") for m in range(30, 36)]
    assert out["times"] == t
    # entry and exit of each trade that falls in the bars, sorted by time (stable), on the bar at or before it
    plain = out["plain"]
    assert [m["id"] for m in plain] == ["1:entry", "3:exit", "2:entry", "1:exit", "2:exit"]
    assert [m["time"] for m in plain] == [t[1], t[1], t[2], t[3], t[4]]  # 09:33:30 snaps to the 09:33 bar
    assert [m["time"] for m in plain] == sorted(m["time"] for m in plain)
    by_id = {m["id"]: m for m in plain}
    assert by_id["1:entry"] == {"time": t[1], "position": "belowBar", "shape": "arrowUp", "color": "#0000ff",
                                "size": 1, "id": "1:entry", "text": ""}  # a long enters on an up arrow below
    assert by_id["2:entry"]["position"] == "aboveBar" and by_id["2:entry"]["shape"] == "arrowDown"  # a short
    assert by_id["1:exit"]["shape"] == "circle" and by_id["1:exit"]["color"] == "#00ff00"  # a win
    assert by_id["1:exit"]["text"] == "+ target" and by_id["1:exit"]["position"] == "aboveBar"
    assert by_id["2:exit"]["color"] == "#ff0000" and by_id["2:exit"]["text"] == "- stop"  # a loss
    assert by_id["2:exit"]["position"] == "belowBar"
    assert by_id["3:exit"]["color"] == "#888888" and by_id["3:exit"]["text"] == "end"  # break even
    # trade 3 entered the day before: only its exit is on this chart; trade 4 is another day: nothing
    assert "3:entry" not in by_id and not any(m["id"].startswith("4:") for m in plain)
    # the selected trade is larger and numbered; the others are fainter
    picked = {m["id"]: m for m in out["picked"]}
    assert picked["2:entry"]["size"] == 2 and picked["2:entry"]["text"] == "#2"
    assert picked["2:exit"]["size"] == 2 and picked["2:exit"]["text"] == "#2 - stop"
    assert picked["2:exit"]["color"] == "#ff0000" and picked["2:entry"]["color"] == "#0000ff"
    assert picked["1:entry"]["size"] == 1 and picked["1:entry"]["color"] == "rgba(0, 0, 255, 0.55)"
    assert picked["1:exit"]["color"] == "rgba(0, 255, 0, 0.55)"
    assert picked["3:exit"]["color"] == "rgba(136, 136, 136, 0.55)"
    assert [m["time"] for m in out["picked"]] == sorted(m["time"] for m in out["picked"])
    assert out["keyAsText"] == [1, 1, 2, 1, 2]  # keys are compared as text
    assert out["unknownSelection"] == [1, 1, 1, 1, 1]  # a key that matches no trade selects nothing
    assert out["none"] == []
    assert out["window"] == [1, 2, 3]
    assert out["windowEmpty"] == [] and out["windowBad"] == []
    assert out["snap"] == [-1, 0, 2, 5, 5, -1]
    assert out["reasons"] == ["stop", "target", "exit", "end", "a_very_long…", "", "", "__proto__"]
    assert out["focus"][0] == {"from": t[2] - 60, "to": t[2] + 60}
    assert out["focus"][1] == {"from": t[0], "to": t[5]}  # kept inside the bars
    assert out["focus"][2] is None and out["focus"][3] is None and out["focus"][4] is None
    assert out["focus"][5] is None  # one bar: no range
    assert out["label"][0].startswith("Candlestick chart: 6 one-minute bars from 2024-01-02 09:30:00 to ")
    assert "3 entry and exit markers" in out["label"][0] and "1 entry or exit marker." in out["label"][1]
    assert out["label"][2] == "Candlestick chart: no bars."


def test_candle_bar_conversion_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_bars", """
import { convertBars } from "./js/charts/candles.js";
const bar = (time, close, extra = {}) => ({ time, open: close, high: close + 1, low: close - 1, close, volume: 5, ...extra });
const rows = [
  bar("2024-01-02 09:30:00", 10), bar("2024-01-02 09:31:00", 11), bar("2024-01-02 09:31:00", 12), // repeated
  bar("2024-01-02 09:29:00", 99), // goes back in time
  bar("garbage", 13), bar("2024-01-02 09:32:00", null), bar("2024-01-02 09:33:00", "x"),
  null, bar("2024-01-02T09:34:00", "14.5"),
];
console.log(JSON.stringify({ out: convertBars(rows), empty: convertBars([]), bad: convertBars(null) }));
""")
    assert isinstance(out, dict)
    got = out["out"]
    assert [b["time"] for b in got] == [seconds("2024-01-02 09:30:00"), seconds("2024-01-02 09:31:00"),
                                        seconds("2024-01-02 09:34:00")]
    assert [b["close"] for b in got] == [10, 12, 14.5]  # the last of a repeated time wins; a string is a number
    assert set(got[0]) == {"time", "open", "high", "low", "close"}
    assert all(a["time"] < b["time"] for a, b in zip(got, got[1:], strict=False))
    assert out["empty"] == [] and out["bad"] == []


# --- algo.js: pure helpers -------------------------------------------------------------------


def test_day_arithmetic_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_days", """
import { dayOfTime, dayWindow, initialDay, shiftDay, stepDay, buildTradeRows } from "./js/views/algo.js";
const first = "2024-01-02";
const last = "2024-03-01";
const rows = buildTradeRows([
  { entry_time: "2024-02-05 10:00:00", exit_time: "2024-02-05 10:05:00" },
  { entry_time: "2024-01-09 15:59:00", exit_time: "2024-01-10 09:31:00" },
  { entry_time: "garbage", exit_time: "2024-01-01 09:31:00" },
]);
console.log(JSON.stringify({
  dayOf: [dayOfTime("2024-01-02T09:30:00"), dayOfTime("2024-01-02 23:59:59"), dayOfTime("nope"), dayOfTime(null)],
  shift: [shiftDay("2024-01-31", 1), shiftDay("2024-03-01", -1), shiftDay("2023-12-31", 1), shiftDay("2024-01-01", -1),
    shiftDay("2024-02-28", 1), shiftDay("2023-02-28", 1), shiftDay("2024-02-29", 1), shiftDay("2024-13-01", 1),
    shiftDay("2024-1-1", 1), shiftDay(null, 1), shiftDay("2024-01-15", 0)],
  step: [stepDay("2024-01-02", -1, first, last), stepDay("2024-01-02", 1, first, last),
    stepDay("2024-03-01", 1, first, last), stepDay("2024-03-01", -1, first, last),
    stepDay("2024-01-03", -1, first, last), stepDay("2024-02-29", 1, first, last),
    stepDay("2024-01-02", -1, null, null), stepDay("bad", 1, first, last)],
  initial: [initialDay(rows, "2024-03-01T16:00:00"), initialDay([], "2024-03-01T16:00:00"), initialDay([], null),
    initialDay(rows.slice(2), "2024-03-01T16:00:00")],
  rows: rows.map((r) => [r.key, r.number, r.entryDay, r.entryChart === null, r.exitChart === null]),
  window: dayWindow("2024-01-02"),
}));
""")
    assert isinstance(out, dict)
    assert out["dayOf"] == ["2024-01-02", "2024-01-02", None, None]
    assert out["shift"] == ["2024-02-01", "2024-02-29", "2024-01-01", "2023-12-31", "2024-02-29", "2023-03-01",
                            "2024-03-01", None, None, None, "2024-01-15"]
    # bounded by the run's bar range: first day cannot step back, last day cannot step forward
    assert out["step"] == [None, "2024-01-03", None, "2024-02-29", "2024-01-02", "2024-03-01", "2024-01-01", None]
    assert out["initial"] == ["2024-01-09", "2024-03-01", None, "2024-03-01"]  # the earliest entry, else the last bar
    assert out["rows"] == [[1, 1, "2024-02-05", False, False], [2, 2, "2024-01-09", False, False],
                           [3, 3, None, True, False]]
    assert out["window"] == {"start": "2024-01-02 00:00:00", "end": "2024-01-02 23:59:59"}


def test_trade_rows_columns_and_summary_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_rows", """
import { TRADE_COLUMNS, barsState, buildTradeRows, selectLabel, summaryItems, trimDecimal } from "./js/views/algo.js";
const trade = { side: "long", quantity: "100.0000", entry_time: "2024-01-02 09:31:00", entry_price: "191.2350",
  exit_time: "2024-01-02 09:33:30", exit_price: "190.0000", net: "-125.5000", gross: "-125.0000", commission: "0.5000",
  exit_reason: "stop", synthetic: false, broker_ref: "1", symbol: "AAPL" };
const rows = buildTradeRows([trade, { ...trade, side: "short", quantity: "0.5000", net: "3.0000", synthetic: true, exit_reason: null }]);
const cells = (row) => TRADE_COLUMNS.map((c) => { const v = c.cell(row, 0); return typeof v === "object" ? v : String(v); });
const keys = (row) => TRADE_COLUMNS.map((c) => (c.sortKey ? c.sortKey(row) : "none"));
console.log(JSON.stringify({
  labels: TRADE_COLUMNS.map((c) => c.label), numeric: TRADE_COLUMNS.map((c) => Boolean(c.numeric)),
  sortable: TRADE_COLUMNS.every((c) => typeof c.sortKey === "function"),
  first: cells(rows[0]), second: cells(rows[1]), firstKeys: keys(rows[0]), secondKeys: keys(rows[1]),
  label: selectLabel(rows[1]),
  trim: [trimDecimal("100.0000"), trimDecimal("191.2350", 2), trimDecimal("190.0000", 2), trimDecimal("0.5"),
    trimDecimal(null), trimDecimal("abc"), trimDecimal("1e5"), trimDecimal("-3.1000", 2), trimDecimal("7", 2)],
  summary: summaryItems({ trade_count: 2, wins: 1, win_rate: "0.5000", net: "2.0000", commission: "0.0000",
    avg_win: "5.0000", avg_loss: "-3.0000", max_drawdown: "3.0000", final_equity: "10002.0000" }),
  summaryEmpty: summaryItems(null).map((i) => i.text),
  states: {
    ok: barsState({ symbol: "AAPL", available: true, reason: null, bars: [{}] }, "AAPL"),
    unverified: barsState({ symbol: "AAPL", available: true, reason: "unverified", bars: [{}] }, "AAPL"),
    empty: barsState({ symbol: "AAPL", available: true, reason: null, bars: [] }, "AAPL"),
    missing: barsState({ symbol: "AAPL", available: false, reason: "missing", bars: [] }, "AAPL"),
    changed: barsState({ symbol: "AAPL", available: false, reason: "changed", bars: [] }, "AAPL"),
    other: barsState({ symbol: "AAPL", available: false, reason: null, bars: [] }, "AAPL"),
    nothing: barsState(null, ""),
  },
}));
""")
    assert isinstance(out, dict)
    assert out["labels"] == ["#", "Side", "Quantity", "Entry time", "Entry price", "Exit time", "Exit price", "Net",
                             "Exit reason", "Synthetic"]
    assert out["numeric"] == [True, False, True, False, True, False, True, True, False, False]
    assert out["sortable"] is True
    assert out["first"] == ["1", "long", "100", "2024-01-02 09:31:00", "191.235", "2024-01-02 09:33:30", "190.00",
                            {"content": "-125.50", "className": "neg"}, "stop", "no"]
    assert out["second"][2] == "0.5" and out["second"][8] == "-" and out["second"][9] == "yes"
    assert out["firstKeys"] == [1, "long", 100, seconds("2024-01-02 09:31:00"), 191.235,
                                seconds("2024-01-02 09:33:30"), 190, -125.5, "stop", 0]
    assert out["secondKeys"][8] is None and out["secondKeys"][9] == 1  # a missing reason sorts last
    assert out["label"] == "Select trade 2, short, entered 2024-01-02 09:31:00"
    assert out["trim"] == ["100", "191.235", "190.00", "0.5", "-", "abc", "1e5", "-3.10", "7.00"]
    summary = {i["label"]: i for i in out["summary"]}
    assert summary["Net"] == {"label": "Net", "text": "+2.00", "className": "pos"}  # as in the overview table
    assert summary["Win %"]["text"] == "50.0" and summary["Max drawdown"]["text"] == "3.00"
    assert summary["Final equity"]["text"] == "10,002.00" and summary["Trades"]["text"] == "2"
    assert set(out["summaryEmpty"]) == {"-"}
    states = out["states"]
    assert states["ok"] == {"kind": "ok", "candles": True, "note": ""}
    assert states["unverified"]["candles"] is True and states["unverified"]["kind"] == "unverified"
    assert "could not be checked" in states["unverified"]["note"]
    assert states["empty"] == {"kind": "empty", "candles": False, "note": "No bars for AAPL on this day."}
    assert states["missing"]["candles"] is False and "data folder" in states["missing"]["note"]
    assert "trades are still listed" in states["missing"]["note"]
    assert states["changed"]["candles"] is False and "has changed since the run" in states["changed"]["note"]
    assert states["other"]["kind"] == "unavailable" and states["nothing"]["kind"] == "unavailable"


# --- the chart with a fake library --------------------------------------------------------------

FAKE_LIB = """
const charts = [];
const markerApis = [];
globalThis.LightweightCharts = {
  LineSeries: "line", BaselineSeries: "baseline", CandlestickSeries: "candles", LineType: { WithSteps: 1 },
  createChart(el, options) {
    const chart = { removed: 0, applied: 0, fits: 0, visible: [], series: [], options,
      addSeries(type, opts, pane) {
        const s = { type, opts, pane, data: null, applied: [], setData(d) { s.data = d; },
          applyOptions(o) { s.applied.push(o); } };
        chart.series.push(s); return s;
      },
      removeSeries(s) { chart.series.splice(chart.series.indexOf(s), 1); },
      panes: () => [{ setStretchFactor() {} }, { setStretchFactor() {} }],
      timeScale: () => ({ fitContent() { chart.fits += 1; }, setVisibleRange(r) { chart.visible.push(r); } }),
      applyOptions() { chart.applied += 1; }, remove() { chart.removed += 1; } };
    charts.push(chart);
    return chart;
  },
  createSeriesMarkers(series, list) {
    const api = { series, log: [list], setMarkers(l) { api.log.push(l); } };
    markerApis.push(api);
    return api;
  },
};
const schemeListeners = [];
globalThis.matchMedia = () => ({ matches: false,
  addEventListener: (t, f) => schemeListeners.push(f),
  removeEventListener: (t, f) => { const i = schemeListeners.indexOf(f); if (i >= 0) schemeListeners.splice(i, 1); } });
const find = (el, test, acc = []) => {
  for (const c of el.children) if (c instanceof FakeEl) { if (test(c)) acc.push(c); find(c, test, acc); }
  return acc;
};
const byClass = (el, name) => find(el, (c) => c.className.split(" ").includes(name));
"""  # noqa: E501


def test_candle_chart_handle_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_chart", FAKE_DOM + FAKE_LIB + """
const { createCandleChart } = await import("./js/charts/candles.js");
const cleanups = [];
const ctx = { onCleanup: (f) => cleanups.push(f) };
const box = document.createElement("div");
const handle = createCandleChart(box, { ctx });
const chart = charts[0];
const api = markerApis[0];
const registered = cleanups.length;
const themeSeen = [];
handle.onTheme((t) => themeSeen.push(t.dark));
const structure = box.children.map((c) => [c.tag, c.className]);
const canvas = byClass(box, "chart-canvas")[0];
const canvasKids = canvas.children.length;
const bars = ["09:30", "09:31", "09:32", "09:33"].map((m, i) => ({ time: "2024-01-02 " + m + ":00",
  open: 10 + i, high: 12 + i, low: 9, close: 11 + i, volume: 1 }));
const trades = [{ key: 1, number: 1, side: "long", entry_time: "2024-01-02 09:31:00", exit_time: "2024-01-02 09:33:00",
  net: "1.0000", exit_reason: "target" }];
handle.setBars(bars);
handle.setTrades(trades, 1);
handle.focusTime("2024-01-02 09:31:00");
handle.focusTime(null);
handle.setNote("careful"); const noted = byClass(box, "chart-note")[0].hidden; const noteText = byClass(box, "chart-note")[0].textContent;
handle.setNote(""); const unnoted = byClass(box, "chart-note")[0].hidden;
const drawn = { candles: chart.series[0].data.length, type: chart.series[0].type, opts: chart.series[0].opts,
  markers: api.log.at(-1).map((m) => m.id), canvasHidden: canvas.hidden, fits: chart.fits,
  visible: chart.visible, label: canvas.getAttribute("aria-label"), win: handle.window(),
  seam: [handle.chart === chart, handle.series === chart.series[0], handle.legend.tag] };
handle.setBars([]);
const emptied = { canvasHidden: canvas.hidden, markers: api.log.at(-1).length, win: handle.window() };
// a scheme change re-colours and notifies the listener; the removal is registered once
const staleListeners = schemeListeners.slice();
staleListeners[0]();
const afterScheme = { chartApplied: chart.applied, seriesApplied: chart.series[0].applied.length, themeSeen: themeSeen.slice(),
  listeners: schemeListeners.length };
handle.dispose(); handle.dispose(); cleanups.forEach((f) => f());
const before = { applied: chart.applied, series: chart.series[0].applied.length, log: api.log.length, visible: chart.visible.length };
staleListeners[0](); // a scheme change that was already queued
handle.setBars(bars); handle.setTrades(trades, 1); handle.focusTime(1); handle.setNote("x");
const after = { applied: chart.applied, series: chart.series[0].applied.length, log: api.log.length, visible: chart.visible.length };
let noLib = null;
try { createCandleChart(box, { ctx, lib: {} }); } catch (err) { noLib = err.message; }
console.log(JSON.stringify({ registered, structure, canvasKids, noted, noteText, unnoted, drawn, emptied, afterScheme,
  removed: chart.removed, listeners: schemeListeners.length, before, after, noLib }));
""")
    assert isinstance(out, dict)
    assert out["registered"] == 2  # dispose first, right after createChart, then the theme listener's remover
    assert out["structure"] == [["DIV", "chart-legend"], ["P", "chart-note muted"], ["DIV", "chart-canvas"]]
    assert out["canvasKids"] == 0  # the wrapper holds only what the library draws into it
    assert out["noted"] is False and out["noteText"] == "careful" and out["unnoted"] is True
    drawn = out["drawn"]
    assert drawn["candles"] == 4 and drawn["type"] == "candles" and drawn["canvasHidden"] is False
    assert drawn["opts"]["borderVisible"] is False and drawn["opts"]["upColor"] != drawn["opts"]["downColor"]
    assert drawn["markers"] == ["1:entry", "1:exit"]
    assert drawn["fits"] == 1
    assert drawn["visible"] == [{"from": seconds("2024-01-02 09:30:00"), "to": seconds("2024-01-02 09:33:00")}]
    assert drawn["label"].startswith("Candlestick chart: 4 one-minute bars") and "2 entry and exit" in drawn["label"]
    assert drawn["win"] == {"from": seconds("2024-01-02 09:30:00"), "to": seconds("2024-01-02 09:33:00")}
    assert drawn["seam"] == [True, True, "DIV"]
    assert out["emptied"] == {"canvasHidden": True, "markers": 0, "win": None}
    assert out["afterScheme"] == {"chartApplied": 1, "seriesApplied": 1, "themeSeen": [False], "listeners": 1}
    assert out["removed"] == 1 and out["listeners"] == 0  # disposed twice and cleaned up: one removal
    assert out["before"] == out["after"]  # nothing reaches the removed chart, not even a queued scheme change
    assert "not loaded" in out["noLib"]


VIEW_SOURCE = FAKE_DOM + FAKE_LIB + """
const { createNavigator } = await import("./js/lifecycle.js");
const { render } = await import("./js/views/algo.js");
const tick = () => new Promise((r) => setTimeout(r, 0));
const requests = [];
const base = { capital: "10000.0000", first_bar: "2024-01-02T09:30:00", last_bar: "2024-01-05T16:00:00" };
const stats = { symbol: "AAPL", algo: "EMACrossing", mode: "long", trade_count: 2, wins: 1, win_rate: "0.5000",
  net: "2.0000", commission: "0.0000", avg_win: "5.0000", avg_loss: "-3.0000", max_drawdown: "3.0000",
  final_equity: "10002.0000" };
const doc = (extra = {}, run = {}) => ({ run: { run_id: 1, status: "completed", incomplete: false, symbols: ["AAPL"],
  ...base, ...run }, accounts: { "EMA/1": stats, ...extra }, total: stats });
const trade = (extra) => ({ side: "long", quantity: "100.0000", entry_price: "10.0000", exit_price: "11.0000",
  gross: "1.0000", commission: "0.0000", net: "1.0000", exit_reason: "target", synthetic: false, broker_ref: "1",
  symbol: "AAPL", account: "EMA/1", ...extra });
const TRADES = [
  trade({ entry_time: "2024-01-02 09:31:00", exit_time: "2024-01-02 09:32:00" }),
  trade({ side: "short", entry_time: "2024-01-03 09:30:00", exit_time: "2024-01-03 09:31:00", net: "-3.0000",
    exit_reason: "stop" }),
];
const equity = { run_id: 1, algo: "EMA/1", step: true, capital: "10000.0000", stored_points: 2,
  points: [{ time: "2024-01-02 09:32:00", equity: "10001.0000" }, { time: "2024-01-03 09:31:00", equity: "10002.0000" }] };
const barsFor = (day, extra = {}) => ({ symbol: "AAPL", available: true, reason: null,
  bars: ["09:30", "09:31", "09:32"].map((m, i) => ({ time: day + " " + m + ":00", open: 10, high: 12, low: 9, close: 11 + i,
    volume: 5 })), ...extra });
function install(routes) {
  globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
    const u = new URL(url, "http://x");
    const req = { path: u.pathname, query: Object.fromEntries(u.searchParams), signal: init.signal, aborted: false };
    req.answer = (body, status = 200) => resolve({ ok: status < 400, status, statusText: "status", json: async () => body });
    requests.push(req);
    init.signal.addEventListener("abort", () => {
      req.aborted = true;
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    });
    const auto = routes[u.pathname];
    if (auto) { const [body, status] = auto(req); req.answer(body, status); }
  });
}
const barsRequests = () => requests.filter((r) => r.path.endsWith("/bars"));
const rowButtons = (container) => find(container, (c) => c.tag === "BUTTON" && c.className === "row-select");
const navigator = createNavigator();
async function open(params = { runId: 1, idname: "EMA/1" }) {
  const container = document.createElement("div");
  const done = navigator.run(render, params, { mount: () => container });
  await tick();
  return { container, done };
}
const ok = (body) => () => [body, 200];
"""


def test_selecting_a_trade_on_another_day_loads_it_once_and_aborts_the_older_request(
        node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view", VIEW_SOURCE + """
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity) });
const { container, done } = await open();
const firstQueries = barsRequests().map((r) => r.query);
const hasRun = requests.slice(0, 3).map((r) => [r.path, r.query.algo ?? null]);
const heading = find(container, (c) => c.tag === "H2")[0].textContent;
const links = find(container, (c) => c.tag === "A").map((a) => a.getAttribute("href"));
const dayLabel = () => byClass(container, "day-label")[0].textContent;
const dayButtons = () => find(byClass(container, "day-control")[0], (c) => c.tag === "BUTTON");
const out = { firstQueries, hasRun, heading, links, label0: dayLabel(),
  prevDisabled0: dayButtons()[0].getAttribute("aria-disabled"), nextDisabled0: dayButtons()[1].getAttribute("aria-disabled"), rowButtons: rowButtons(container).length };

// the first bars request is still pending: select trade 2, which was made on another day
rowButtons(container)[1].fire("click");
out.afterSelect = { bars: barsRequests().length, firstAborted: barsRequests()[0].aborted, query: barsRequests()[1].query,
  label: dayLabel(), pressed: rowButtons(container).map((b) => b.getAttribute("aria-pressed")),
  current: find(container, (c) => c.tag === "TR").slice(1).map((tr) => tr.getAttribute("aria-current")),
  selected: rowButtons(container)[1].getAttribute("aria-label") };
barsRequests()[1].answer(barsFor("2024-01-03"));
await done;
await tick();
const candle = charts[1];
const markers = markerApis[0].log.at(-1);
out.afterLoad = { markers: markers.map((m) => [m.id, m.size, m.text, m.shape, m.position]),
  focus: candle.visible.slice(), series: candle.series[0].data.length, barsRequests: barsRequests().length };

// selecting trade 1 (the other day) loads once; selecting it again changes nothing on the wire
rowButtons(container)[0].fire("click");
out.toDayOne = { bars: barsRequests().length, secondAborted: barsRequests()[1].aborted, query: barsRequests()[2].query };
barsRequests()[2].answer(barsFor("2024-01-02"));
await tick();
const countBefore = barsRequests().length;
const focusBefore = candle.visible.length;
rowButtons(container)[0].fire("click");
await tick();
out.sameDay = { bars: barsRequests().length === countBefore, refocused: candle.visible.length === focusBefore + 1,
  markers: markerApis[0].log.at(-1).map((m) => [m.id, m.size]) };

// the day control: the first day of the run cannot go back; next loads the next day
const before = dayButtons()[0].getAttribute("aria-disabled");
dayButtons()[0].fire("click"); // ignored: no request
const ignored = barsRequests().length;
dayButtons()[1].fire("click");
// next first asks for up to 7 calendar days after the shown day (inside the run's range), then shows the day found
out.search = { bars: barsRequests().length, query: barsRequests().at(-1).query, label: dayLabel() };
barsRequests().at(-1).answer(barsFor("2024-01-03"));
await tick();
out.next = { prevWasDisabled: before, ignored, bars: barsRequests().length, query: barsRequests().at(-1).query, label: dayLabel() };
barsRequests().at(-1).answer(barsFor("2024-01-03"));
await tick();
out.afterNext = { prevDisabled: dayButtons()[0].getAttribute("aria-disabled"), markerIds: markerApis[0].log.at(-1).map((m) => m.id) };

// the navigation ends: both charts are removed and a queued scheme change reaches nothing
const stale = schemeListeners.slice();
const snapshot = () => JSON.stringify([charts.map((c) => [c.removed, c.applied]), candle.series[0].applied.length,
  markerApis[0].log.length, candle.visible.length]);
const live = snapshot();
await navigator.run(async () => {}, {}, { mount: () => document.createElement("div") });
const removed = charts.map((c) => c.removed);
const listenersAfter = schemeListeners.length;
const frozen = snapshot();
stale.forEach((f) => f());
out.end = { removed, listenersAfter, staleChanged: snapshot() !== frozen, liveDiffers: live !== frozen, stale: stale.length };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    day1, day3 = "2024-01-02", "2024-01-03"
    assert out["hasRun"] == [["/api/runs/1", None], ["/api/runs/1/trades", "EMA/1"], ["/api/runs/1/equity", "EMA/1"]]
    assert out["firstQueries"] == [{"symbol": "AAPL", "start": f"{day1} 00:00:00", "end": f"{day1} 23:59:59"}]
    assert out["heading"] == "EMA/1, run 1"
    assert out["links"] == ["#/run/1", "#/"]
    assert out["label0"] == f"{day1} (data time)"
    assert out["prevDisabled0"] == "true" and out["nextDisabled0"] == "false"  # the run starts on that day
    assert out["rowButtons"] == 2
    after = out["afterSelect"]
    assert after["bars"] == 2 and after["firstAborted"] is True  # exactly one new request; the older one aborted
    assert after["query"] == {"symbol": "AAPL", "start": f"{day3} 00:00:00", "end": f"{day3} 23:59:59"}
    assert after["label"] == f"{day3} (data time)"
    assert after["pressed"] == ["false", "true"] and after["current"] == [None, "true"]
    assert after["selected"].startswith("Select trade 2, short, entered ")
    loaded = out["afterLoad"]
    assert loaded["barsRequests"] == 2 and loaded["series"] == 3
    # only trade 2 is on this day: its markers are the selected, numbered ones
    assert loaded["markers"] == [["2:entry", 2, "#2", "arrowDown", "aboveBar"],
                                 ["2:exit", 2, "#2 - stop", "circle", "belowBar"]]
    assert loaded["focus"] == [{"from": seconds(f"{day3} 09:30:00"), "to": seconds(f"{day3} 09:32:00")}]
    assert out["toDayOne"]["bars"] == 3
    assert out["toDayOne"]["query"]["start"] == f"{day1} 00:00:00"
    assert out["sameDay"]["bars"] is True and out["sameDay"]["refocused"] is True  # no request on the same day
    assert out["sameDay"]["markers"] == [["1:entry", 2], ["1:exit", 2]]
    assert out["search"] == {"bars": 4, "label": f"{day1} (data time)",
                             "query": {"symbol": "AAPL", "start": f"{day3} 00:00:00", "end": "2024-01-05 23:59:59"}}
    assert out["next"] == {"prevWasDisabled": "true", "ignored": 3, "bars": 5,
                           "query": {"symbol": "AAPL", "start": f"{day3} 00:00:00", "end": f"{day3} 23:59:59"},
                           "label": f"{day3} (data time)"}
    assert out["afterNext"]["prevDisabled"] == "false"
    assert out["afterNext"]["markerIds"] == ["2:entry", "2:exit"]  # trade 1 is not on this day
    end = out["end"]
    assert end["removed"] == [1, 1] and end["listenersAfter"] == 0
    assert end["staleChanged"] is False  # disposing then firing the scheme listener calls nothing
    assert end["stale"] >= 2  # the equity chart and the candle chart each watched the scheme


def test_a_day_without_a_selected_trade_and_the_bars_messages_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view_bars", VIEW_SOURCE + """
const answers = {};
async function scenario(name, body, trades = TRADES) {
  requests.length = 0; charts.length = 0; markerApis.length = 0;
  install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades }), "/api/runs/1/equity": ok(equity) });
  const { container, done } = await open();
  barsRequests()[0].answer(body);
  await done;
  const canvas = byClass(container, "chart-canvas");
  const candle = charts[1];
  answers[name] = { note: byClass(container, "chart-note").map((n) => [n.hidden === true, n.textContent]).at(-1),
    candleCanvasHidden: canvas.at(-1).hidden, candles: candle.series[0].data.length,
    rows: rowButtons(container).length, errors: byClass(container, "error").length,
    query: barsRequests()[0].query.start };
}
await scenario("missing", { symbol: "AAPL", available: false, reason: "missing", bars: [] });
await scenario("changed", { symbol: "AAPL", available: false, reason: "changed", bars: [] });
await scenario("unverified", barsFor("2024-01-02", { reason: "unverified" }));
await scenario("empty", barsFor("2024-01-02", { bars: [] }));
await scenario("notrades", barsFor("2024-01-05"), []);
console.log(JSON.stringify(answers));
""")
    assert isinstance(out, dict)
    missing, changed, unverified = out["missing"], out["changed"], out["unverified"]
    assert missing["note"][0] is False and "data folder" in missing["note"][1]
    assert missing["candleCanvasHidden"] is True and missing["candles"] == 0
    assert missing["rows"] == 2 and missing["errors"] == 0  # the trades stay usable
    assert changed["note"][0] is False and "has changed since the run" in changed["note"][1]
    assert changed["candleCanvasHidden"] is True and changed["rows"] == 2
    assert unverified["note"][0] is False and "could not be checked" in unverified["note"][1]
    assert unverified["candleCanvasHidden"] is False and unverified["candles"] == 3  # candles shown with a note
    assert out["empty"]["note"][1] == "No bars for AAPL on this day." and out["empty"]["candleCanvasHidden"] is True
    # no trades: the chart opens on the day of the run's last bar
    assert out["notrades"]["query"] == "2024-01-05 00:00:00" and out["notrades"]["rows"] == 0


def test_unknown_algo_failed_run_and_failed_parts_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view_errors", VIEW_SOURCE + """
const texts = (container) => find(container, (c) => ["H2", "P", "A", "DIV", "LI"].includes(c.tag)).map((c) => c.textContent);
const result = {};

// the run exists but has no such algo: one clear message, a link back to the run, no chart, no bars request
requests.length = 0; charts.length = 0;
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": () => [{ detail: "no algo nope in run 1" }, 404],
  "/api/runs/1/equity": () => [{ detail: "no algo nope in run 1" }, 404] });
let opened = await open({ runId: 1, idname: "nope" });
await opened.done;
result.unknown = { texts: texts(opened.container), errors: byClass(opened.container, "error").length,
  charts: charts.length, bars: barsRequests().length,
  links: find(opened.container, (c) => c.tag === "A").map((a) => a.getAttribute("href")),
  panels: byClass(opened.container, "panel").length };

// a prototype name is not an algo either
requests.length = 0;
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": () => [{ detail: "x" }, 404], "/api/runs/1/equity": () => [{ detail: "x" }, 404] });
opened = await open({ runId: 1, idname: "__proto__" });
await opened.done;
result.proto = { panels: byClass(opened.container, "panel").length, errors: byClass(opened.container, "error").length };

// the run document fails: the heading, the back links and exactly one error panel
requests.length = 0; charts.length = 0;
install({ "/api/runs/1": () => [{ detail: "no run 1" }, 404], "/api/runs/1/trades": () => [{ detail: "no run 1" }, 404],
  "/api/runs/1/equity": () => [{ detail: "no run 1" }, 404] });
opened = await open();
await opened.done;
result.failed = { errors: byClass(opened.container, "error").map((e) => e.textContent), panels: byClass(opened.container, "panel").length,
  h2: find(opened.container, (c) => c.tag === "H2").length, links: find(opened.container, (c) => c.tag === "A").length,
  charts: charts.length, bars: barsRequests().length };

// the trades and the equity fail, the run document is fine: one error panel each, the candles still load
requests.length = 0; charts.length = 0; markerApis.length = 0;
install({ "/api/runs/1": ok(doc({}, { status: "failed" })), "/api/runs/1/trades": () => [{ detail: "boom" }, 500],
  "/api/runs/1/equity": () => [{ detail: "boom" }, 500] });
opened = await open();
barsRequests()[0].answer(barsFor("2024-01-05"));
await opened.done;
result.parts = { errors: byClass(opened.container, "error").map((e) => e.textContent), query: barsRequests()[0].query.start,
  charts: charts.length, warning: byClass(opened.container, "warning").map((w) => w.textContent),
  badge: byClass(opened.container, "badge").map((b) => b.className + ":" + b.textContent) };

// an incomplete run says so
requests.length = 0; charts.length = 0;
install({ "/api/runs/1": ok(doc({}, { status: "running", incomplete: true })), "/api/runs/1/trades": ok({ trades: [] }),
  "/api/runs/1/equity": ok(equity) });
opened = await open();
barsRequests()[0].answer(barsFor("2024-01-05"));
await opened.done;
result.incomplete = { warning: byClass(opened.container, "warning").map((w) => w.textContent),
  badge: byClass(opened.container, "badge").map((b) => b.textContent) };
console.log(JSON.stringify(result));
""")
    assert isinstance(out, dict)
    unknown = out["unknown"]
    assert any('has no algo named "nope"' in text for text in unknown["texts"])
    assert unknown["errors"] == 0 and unknown["charts"] == 0 and unknown["bars"] == 0
    assert unknown["links"] == ["#/run/1", "#/", "#/run/1"] and unknown["panels"] == 1
    assert out["proto"] == {"panels": 1, "errors": 0}
    failed = out["failed"]
    assert failed["errors"] == ["Error 404: no run 1"] and failed["panels"] == 1  # one panel, not three
    assert failed["h2"] == 1 and failed["links"] == 2 and failed["charts"] == 0 and failed["bars"] == 0
    parts = out["parts"]
    assert parts["errors"] == ["Error 500: boom", "Error 500: boom"] and parts["query"] == "2024-01-05 00:00:00"
    assert parts["charts"] == 1  # the candle chart; the equity chart has no data to draw
    assert any("This run failed" in w for w in parts["warning"]) and parts["badge"] == ["badge failed:failed"]
    assert any("incomplete" in w for w in out["incomplete"]["warning"])
    assert out["incomplete"]["badge"] == ["incomplete"]


# --- D15 review fixes: selection off the shown day, trades over days, day stepping, focus, signs, refit ---


def test_selection_on_another_day_fades_nothing_and_spans_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_selection", """
import { buildMarkers, convertBars, focusSpanRange, resultSign } from "./js/charts/candles.js";
import { buildTradeRows, exitNote, nearestBarDay, stepWindow } from "./js/views/algo.js";
const bars = ["09:30", "09:31", "09:32", "09:33"].map((m) => ({ time: "2024-01-02 " + m + ":00", open: 1, high: 2, low: 1, close: 1 }));
const times = convertBars(bars).map((b) => b.time);
const trades = [
  { key: 1, number: 1, side: "long", entry_time: "2024-01-02 09:31:00", exit_time: "2024-01-02 09:32:00", net: "2.0000", exit_reason: "stop" },
  { key: 2, number: 2, side: "long", entry_time: "2024-01-03 09:31:00", exit_time: "2024-01-03 09:35:00", net: "-1.0000", exit_reason: "target" },
  { key: 3, number: 3, side: "short", entry_time: "2024-01-02 09:33:00", exit_time: "2024-01-04 09:31:00", net: "0.0000", exit_reason: "algo_exit" },
];
const theme = { accent: "#0000ff", pos: "#00ff00", neg: "#ff0000", muted: "#888888", text: "#000000" };
const summarize = (list) => list.map((m) => [m.id, m.size, m.color, m.text]);
const rows = buildTradeRows(trades);
console.log(JSON.stringify({
  plain: summarize(buildMarkers(trades, times, null, theme)),
  elsewhere: summarize(buildMarkers(trades, times, 2, theme)), // selected trade is not on this day
  overnight: summarize(buildMarkers(trades, times, 3, theme)), // entry here, exit on another day
  signs: [resultSign("1.5"), resultSign("-0.0001"), resultSign("0.0000"), resultSign(null), resultSign("x")],
  span: [focusSpanRange(times, times[1], times[2]), focusSpanRange(times, times[2], times[1]), focusSpanRange([], 1, 2),
    focusSpanRange(times, "a", 2), focusSpanRange(times, times[0] - 99999, times[0] - 99000)],
  exits: [exitNote(rows[2], "2024-01-02"), exitNote(rows[2], "2024-01-04"), exitNote(rows[0], "2024-01-02"), exitNote(null, "2024-01-02"), exitNote(rows[2], null)],
  rows: rows.map((r) => [r.entryDay, r.exitDay]),
  window: [stepWindow("2024-01-05", 1, "2024-01-02", "2024-01-15"), stepWindow("2024-01-05", -1, "2024-01-02", "2024-01-15"),
    stepWindow("2024-01-09", -1, "2024-01-02", "2024-01-15"), stepWindow("2024-01-15", 1, "2024-01-02", "2024-01-15"),
    stepWindow("2024-01-02", -1, "2024-01-02", "2024-01-15"), stepWindow("2024-01-14", 1, "2024-01-02", "2024-01-15"),
    stepWindow("2024-01-05", 1, null, null), stepWindow("bad", 1, null, null)],
  nearest: [nearestBarDay([{ time: "2024-01-08 09:30:00" }, { time: "2024-01-09 09:30:00" }], 1),
    nearestBarDay([{ time: "2024-01-08 09:30:00" }, { time: "2024-01-09 09:30:00" }], -1), nearestBarDay([], 1),
    nearestBarDay([{ time: "x" }, null], 1), nearestBarDay(null, -1)],
}));
""")
    assert isinstance(out, dict)
    t = [seconds(f"2024-01-02 09:3{i}:00") for i in range(4)]
    assert [m[0] for m in out["plain"]] == ["1:entry", "1:exit", "3:entry"]
    # the selection is on another day: nothing is faded, nothing is enlarged
    assert out["elsewhere"] == [[m[0], 1, m[2], ""] if m[0] != "1:exit" else [m[0], 1, m[2], m[3]] for m in out["plain"]]
    assert all(not m[2].startswith("rgba") for m in out["elsewhere"])
    # an overnight trade selected on its entry day: its entry is highlighted, the others fade
    over = {m[0]: m for m in out["overnight"]}
    assert over["3:entry"][1] == 2 and over["3:entry"][3] == "#3" and over["1:entry"][2].startswith("rgba")
    assert [out["plain"][1][3]] == ["+ stop"]
    assert out["signs"] == ["+", "-", "", "", ""]
    assert out["span"][0] == {"from": t[0], "to": t[3]}  # the margin is clamped to the bars
    assert out["span"][1] == out["span"][0] and out["span"][2] is None and out["span"][3] is None and out["span"][4] is None
    assert out["exits"] == [{"text": "Exits on 2024-01-04.", "day": "2024-01-04"}, None, None, None, None]
    assert out["rows"] == [["2024-01-02", "2024-01-02"], ["2024-01-03", "2024-01-03"], ["2024-01-02", "2024-01-04"]]
    # day stepping: at most 7 calendar days (the route's limit), inside the run's bar range
    w = out["window"]
    assert w[0] == {"start": "2024-01-06 00:00:00", "end": "2024-01-12 23:59:59"}
    assert w[1] == {"start": "2024-01-02 00:00:00", "end": "2024-01-04 23:59:59"}
    assert w[2] == {"start": "2024-01-02 00:00:00", "end": "2024-01-08 23:59:59"}
    assert w[3] is None and w[4] is None
    assert w[5] == {"start": "2024-01-15 00:00:00", "end": "2024-01-15 23:59:59"}
    assert w[6] == {"start": "2024-01-06 00:00:00", "end": "2024-01-12 23:59:59"} and w[7] is None
    assert out["nearest"] == ["2024-01-08", "2024-01-09", None, None, None]


def test_candle_legend_shows_profit_and_loss_in_their_colours_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_legend", FAKE_DOM + FAKE_LIB + """
const { createCandleChart, MARKER_LEGEND } = await import("./js/charts/candles.js");
const box = document.createElement("div");
createCandleChart(box, { ctx: { onCleanup() {} } });
const items = byClass(box, "marker-key")[0].children.map((li) => [li.children[0].className, li.textContent]);
console.log(JSON.stringify({ items, n: MARKER_LEGEND.length }));
""")
    assert isinstance(out, dict)
    classes = [item[0] for item in out["items"]]
    assert classes == ["marker-shape", "marker-shape", "marker-shape pos", "marker-shape neg"]  # real colours, by class
    assert "+" in out["items"][2][1] and "-" in out["items"][3][1]
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".marker-shape.pos { color: var(--pos); }" in css and ".marker-shape.neg { color: var(--neg); }" in css


def test_the_chart_refits_on_resize_unless_the_user_zoomed_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "candles_refit", FAKE_DOM + FAKE_LIB + """
const create = LightweightCharts.createChart;
LightweightCharts.createChart = (el, options) => {
  const chart = create(el, options);
  const handlers = [];
  let width = 800;
  const scale = chart.timeScale;
  chart.handlers = handlers;
  chart.resize = (w) => { width = w; handlers.slice().forEach((f) => f()); };
  chart.timeScale = () => ({ ...scale(), width: () => width,
    subscribeSizeChange: (f) => handlers.push(f), unsubscribeSizeChange: (f) => { const i = handlers.indexOf(f); if (i >= 0) handlers.splice(i, 1); } });
  return chart;
};
const { createCandleChart } = await import("./js/charts/candles.js");
const cleanups = [];
const box = document.createElement("div");
const handle = createCandleChart(box, { ctx: { onCleanup: (f) => cleanups.push(f) } });
const chart = charts[0];
const canvas = byClass(box, "chart-canvas")[0];
const bars = ["09:30", "09:31", "09:32"].map((m) => ({ time: "2024-01-02 " + m + ":00", open: 1, high: 2, low: 1, close: 1 }));
const log = {};
let w = 800;
const resize = () => { w += 100; chart.resize(w); return chart.fits; };
handle.setBars(bars);
log.fitted = chart.fits;
log.resized = resize(); // not zoomed: fits again
chart.resize(w); log.sameWidth = chart.fits; // a height-only change: nothing
canvas.fire("pointerdown", { buttons: 1, clientX: 0, clientY: 0 });
canvas.fire("pointermove", { buttons: 1, clientX: 3, clientY: 0 });
log.smallDrag = resize(); // 3 px is not a zoom: fits again
canvas.fire("pointermove", { buttons: 1, clientX: 5, clientY: 0 });
log.bigDrag = resize(); // more than 4 px with a button down: the user moved the chart
handle.setBars(bars);
log.refitByBars = chart.fits; // new bars fit again and clear the state
log.afterBars = resize();
canvas.fire("pointerup", {});
canvas.fire("pointermove", { buttons: 0, clientX: 90, clientY: 0 }); // no button: nothing
log.hover = resize();
canvas.fire("touchstart", { touches: [{}] });
log.oneFinger = resize();
canvas.fire("touchstart", { touches: [{}, {}] });
log.twoFingers = resize();
handle.setBars(bars);
canvas.fire("wheel", {});
log.wheel = resize() - chart.fits;
handle.setBars(bars);
const beforeFocus = resize();
handle.focusTime("2024-01-02 09:31:00");
log.focus = resize() === beforeFocus; // a focus is the user's view too
handle.setBars([]); // nothing to fit
log.empty = chart.fits;
chart.resize(5000);
log.emptyResize = chart.fits === log.empty;
cleanups.forEach((f) => f());
log.handlers = chart.handlers.length;
log.listeners = Object.values(canvas.listeners).flat().length > 0; // the fake keeps them: removal is only on real elements
console.log(JSON.stringify(log));
""")
    assert isinstance(out, dict)
    assert out["fitted"] == 1 and out["resized"] == 2 and out["sameWidth"] == 2
    assert out["smallDrag"] == 3 and out["bigDrag"] == 3  # the big drag stops the refit
    assert out["refitByBars"] == 4 and out["afterBars"] == 5
    assert out["hover"] == 6 and out["oneFinger"] == 7 and out["twoFingers"] == 7
    assert out["wheel"] == 0 and out["focus"] is True and out["emptyResize"] is True
    assert out["handlers"] == 0


def test_day_buttons_stay_focusable_at_the_ends_and_step_to_the_nearest_day_with_bars(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view_steps", VIEW_SOURCE + """
const range = { first_bar: "2024-01-02T09:30:00", last_bar: "2024-01-15T16:00:00" };
const friday = [trade({ entry_time: "2024-01-05 09:31:00", exit_time: "2024-01-05 09:32:00" })];
install({ "/api/runs/1": ok(doc({}, range)), "/api/runs/1/trades": ok({ trades: friday }), "/api/runs/1/equity": ok(equity) });
const { container, done } = await open();
barsRequests()[0].answer(barsFor("2024-01-05"));
await done;
const buttons = () => find(byClass(container, "day-control")[0], (c) => c.tag === "BUTTON");
const dayLabel = () => byClass(container, "day-label")[0].textContent;
const note = () => { const n = byClass(container, "day-note")[0]; return [n.hidden === true, n.textContent]; };
const out = { start: barsRequests()[0].query.start, label: dayLabel() };
const next = (n) => barsRequests()[n].query;
// Next from Friday: one request for the 7 days after it; Monday is the nearest day with bars
buttons()[1].fire("click");
out.search = { count: barsRequests().length, query: next(1), note: note() };
barsRequests()[1].answer({ symbol: "AAPL", available: true, reason: null, bars: [...barsFor("2024-01-08").bars, ...barsFor("2024-01-10").bars] });
await tick();
out.shown = { count: barsRequests().length, query: next(2), label: dayLabel() };
barsRequests()[2].answer(barsFor("2024-01-08"));
await tick();
out.landed = { label: dayLabel(), note: note() };
// Previous: the 7 days before it inside the run's range; the last day with bars there is shown
buttons()[0].fire("click");
out.back = { query: next(3) };
barsRequests()[3].answer({ symbol: "AAPL", available: true, reason: null, bars: [...barsFor("2024-01-02").bars, ...barsFor("2024-01-05").bars] });
await tick();
out.backShown = { query: next(4), label: dayLabel() };
barsRequests()[4].answer(barsFor("2024-01-05"));
await tick();
// Next again, but there are no bars in the week: say so and stay
const before = barsRequests().length;
buttons()[1].fire("click");
barsRequests()[before].answer({ symbol: "AAPL", available: true, reason: null, bars: [] });
await tick();
out.none = { requests: barsRequests().length - before, label: dayLabel(), note: note() };
// the data is not available: its reason is shown, still on the same day
buttons()[1].fire("click");
barsRequests().at(-1).answer({ symbol: "AAPL", available: false, reason: "missing", bars: [] });
await tick();
out.missing = { label: dayLabel(), note: note() };
// a newer press aborts a search in flight
buttons()[0].fire("click");
const first = barsRequests().at(-1);
buttons()[1].fire("click");
out.abort = { aborted: first.aborted, count: barsRequests().length };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["start"] == "2024-01-05 00:00:00" and out["label"] == "2024-01-05 (data time)"
    assert out["search"]["count"] == 2
    assert out["search"]["query"] == {"symbol": "AAPL", "start": "2024-01-06 00:00:00", "end": "2024-01-12 23:59:59"}
    assert out["search"]["note"][0] is False and "Looking for the next day" in out["search"]["note"][1]
    assert out["shown"]["count"] == 3 and out["shown"]["query"]["start"] == "2024-01-08 00:00:00"
    assert out["shown"]["query"]["end"] == "2024-01-08 23:59:59" and out["shown"]["label"] == "2024-01-08 (data time)"
    assert out["landed"] == {"label": "2024-01-08 (data time)", "note": [True, ""]}
    assert out["back"]["query"] == {"symbol": "AAPL", "start": "2024-01-02 00:00:00", "end": "2024-01-07 23:59:59"}
    assert out["backShown"]["query"]["start"] == "2024-01-05 00:00:00" and out["backShown"]["label"] == "2024-01-05 (data time)"
    assert out["none"]["requests"] == 1 and out["none"]["label"] == "2024-01-05 (data time)"
    assert out["none"]["note"][0] is False and "No next day with bars" in out["none"]["note"][1]
    assert "Staying on 2024-01-05" in out["none"]["note"][1]
    assert out["missing"]["label"] == "2024-01-05 (data time)" and "data folder" in out["missing"]["note"][1]
    assert out["abort"]["aborted"] is True and out["abort"]["count"] >= 2


def test_day_buttons_use_aria_disabled_and_ignore_activation_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view_focus", VIEW_SOURCE + """
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity) });
const { container, done } = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await done;
const [previous, next] = find(byClass(container, "day-control")[0], (c) => c.tag === "BUTTON");
previous.focus();
const count = barsRequests().length;
const click = previous.fire("click");
console.log(JSON.stringify({ disabledAttr: [previous.hasAttribute("disabled"), next.hasAttribute("disabled")],
  aria: [previous.getAttribute("aria-disabled"), next.getAttribute("aria-disabled")],
  focused: document.activeElement === previous, requests: barsRequests().length - count }));
""")
    assert isinstance(out, dict)
    assert out["disabledAttr"] == [False, False] and out["aria"] == ["true", "false"]
    assert out["focused"] is True and out["requests"] == 0


def test_a_trade_over_two_days_and_the_focus_span_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "algo_view_overnight", VIEW_SOURCE + """
const late = (day, ms) => ms.map((m) => ({ time: day + " " + m + ":00", open: 10, high: 12, low: 9, close: 11, volume: 5 }));
const trades = [
  trade({ entry_time: "2024-01-02 15:59:00", exit_time: "2024-01-03 09:31:00", net: "-2.0000", exit_reason: "stop" }),
  trade({ entry_time: "2024-01-02 15:50:00", exit_time: "2024-01-02 15:52:00" }),
];
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades }), "/api/runs/1/equity": ok(equity) });
const { container, done } = await open();
const minutes = []; for (let m = 30; m < 60; m += 1) minutes.push("15:" + m);
barsRequests()[0].answer({ symbol: "AAPL", available: true, reason: null, bars: late("2024-01-02", [...minutes, "16:00"]) });
await done;
const candle = charts[1];
const dayNote = () => { const n = byClass(container, "day-note")[0]; return [n.hidden === true, n.textContent]; };
const markerIds = () => markerApis[0].log.at(-1).map((m) => m.id + ":" + m.size);
const out = { before: dayNote() };
// the overnight trade: entry here, exit on the next day, with a note and a button
rowButtons(container)[0].fire("click");
out.selected = { note: dayNote(), buttons: find(byClass(container, "day-note")[0], (c) => c.tag === "BUTTON").map((b) => b.textContent),
  markers: markerIds(), visible: candle.visible.at(-1), requests: barsRequests().length };
// the trade that is over within the day: zoomed from its entry to its exit with a margin, no note
rowButtons(container)[1].fire("click");
out.span = { visible: candle.visible.at(-1), note: dayNote() };
// back to the overnight trade, then press the button: the next day, the exit marker, no note, nothing faded
rowButtons(container)[0].fire("click");
find(byClass(container, "day-note")[0], (c) => c.tag === "BUTTON")[0].fire("click");
out.toExit = { requests: barsRequests().length, query: barsRequests().at(-1).query.start };
barsRequests().at(-1).answer(barsFor("2024-01-03"));
await tick();
out.exitDay = { markers: markerIds(), note: dayNote(), visible: candle.visible.at(-1),
  buttons: find(byClass(container, "day-note")[0], (c) => c.tag === "BUTTON").map((b) => b.textContent) };
// the mirror note has a button that shows the entry day again, where the note says "Exits on"
find(byClass(container, "day-note")[0], (c) => c.tag === "BUTTON")[0].fire("click");
out.toEntry = { requests: barsRequests().length, query: barsRequests().at(-1).query.start };
barsRequests().at(-1).answer(barsFor("2024-01-02"));
await tick();
out.entryDay = { note: dayNote() };
// a trade of another day entirely (neither its entry nor its exit day is shown): no note
rowButtons(container)[1].fire("click");
out.other = { note: dayNote() };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["before"][0] is True
    selected = out["selected"]
    assert selected["note"][0] is False and selected["note"][1].startswith("Exits on 2024-01-03.")
    assert selected["buttons"] == ["Show 2024-01-03"]
    assert selected["markers"] == ["2:entry:1", "2:exit:1", "1:entry:2"]  # by time; the entry highlighted, the rest faded
    assert selected["requests"] == 1  # the entry day is shown already
    # the exit is on another day: an hour around the entry, kept inside the day's bars
    assert selected["visible"] == {"from": seconds("2024-01-02 15:30:00"), "to": seconds("2024-01-02 16:00:00")}
    span = out["span"]
    assert span["visible"] == {"from": seconds("2024-01-02 15:45:00"), "to": seconds("2024-01-02 15:57:00")}
    assert span["note"][0] is True
    assert out["toExit"] == {"requests": 2, "query": "2024-01-03 00:00:00"}
    exit_day = out["exitDay"]
    assert exit_day["markers"] == ["1:exit:2"]  # not faded
    # on the exit day a note says where it entered, with a button to show that day
    assert exit_day["note"][0] is False and exit_day["note"][1].startswith("Entered on 2024-01-02.")
    assert exit_day["buttons"] == ["Show 2024-01-02"]
    assert out["toEntry"] == {"requests": 3, "query": "2024-01-02 00:00:00"}
    assert out["entryDay"]["note"][1].startswith("Exits on 2024-01-03.")
    assert out["other"]["note"][0] is True
