# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""The indicator overlays of the candle chart (D15), exercised with node.

The pure core (series conversion, stop and target lines, the plan with its notes and defaults) runs as it is;
the drawing layer runs against the fake DOM and fake chart library of `tests/test_api_static.py` and
`tests/test_api_static_algo.py`, with the real `candles.js`.
"""

from __future__ import annotations

import calendar
import shutil
import time
from pathlib import Path

import pytest

from tests.api_support import make_client
from tests.test_api_static import (
    FAKE_DOM,
    JS_ROOT,
    STATIC,
    page_references,
    require_node,
    run_node,
    served_path,
)
from tests.test_api_static_algo import FAKE_LIB, VIEW_SOURCE


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from MainStoinker.engine.store import SqliteStore

    path = tmp_path_factory.mktemp("static_overlays") / "empty.db"
    with SqliteStore(path, env={}):
        pass
    return path


@pytest.fixture(scope="module")
def node_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    require_node()
    work = tmp_path_factory.mktemp("node_overlays")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


def seconds(text: str) -> int:
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


# --- the files ---------------------------------------------------------------------------------


def test_overlays_module_is_referenced_and_served(empty_db: Path) -> None:
    client = make_client(empty_db)
    seen: set[str] = set()
    for page in (STATIC / "js" / "views" / "algo.js", STATIC / "js" / "charts" / "overlays.js"):
        for _kind, target in page_references(page):
            seen.add(served_path(page, target))
    assert "/js/charts/overlays.js" in seen  # algo.js imports it
    assert {"/js/charts/candles.js", "/js/dom.js", "/js/format.js", "/js/charts/theme.js"} <= seen
    for url in sorted(seen):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["content-type"].startswith("text/javascript"), url
        assert response.headers["cache-control"] == "no-cache", url
    assert len(client.get("/js/charts/overlays.js").text) > 1000


def test_overlays_has_no_colour_literals_and_no_html_injection() -> None:
    text = (STATIC / "js" / "charts" / "overlays.js").read_text(encoding="utf-8")
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert word not in text
    import re

    assert not re.search(r"""["']#[0-9a-fA-F]{3,6}["']""", text)  # colours come from the page theme
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    block = css[css.index("/* Indicator overlays"):]
    assert "#" not in block.replace("/* Indicator overlays: legend with one toggle per item, notes, day note */", "")


# --- the pure core -----------------------------------------------------------------------------

BARS_SOURCE = """
const DAY1 = "2024-01-02", DAY2 = "2024-01-03";
const at = (day, hm) => day + " " + hm + ":00";
// 09:30 to 09:34, no 09:35 (a missing bar), 09:36, and the next day 09:30, 09:31 (a session gap)
const bars = [at(DAY1, "09:30"), at(DAY1, "09:31"), at(DAY1, "09:32"), at(DAY1, "09:33"), at(DAY1, "09:34"),
  at(DAY1, "09:36"), at(DAY2, "09:30"), at(DAY2, "09:31")].map((time) => ({ time, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1 }));
const pt = (time, value) => ({ time, value });
"""


def test_conversion_of_every_draw_type_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_convert", BARS_SOURCE + """
import { barTimes, convertSeries } from "./js/charts/overlays.js";
const times = barTimes(bars);
const day = (m) => at(DAY1, m);
const entry = (name, draw_type, points) => ({ name, draw_type, position: 0, points });
const results = {
  times,
  line: convertSeries(entry("MA20", "line", [pt(day("09:30"), 1), pt(day("09:31"), 2), pt(day("09:32"), 9), pt(day("09:33"), "3.5"), pt(day("09:33"), 4)]), times),
  lineOffCandle: convertSeries(entry("MA20", "line", [pt(at(DAY1, "09:30"), 1), pt("2024-01-02 09:32:30", 9), pt("garbage", 8), pt(at(DAY1, "09:35"), 7)]), times),
  // adjacent bars only: no break
  segRun: convertSeries(entry("StopPrice", "segment", [pt(day("09:30"), 5), pt(day("09:31"), 5), pt(day("09:32"), 6), pt(day("09:33"), 6), pt(day("09:34"), 6)]), times),
  // a bar skipped in the list (09:33), a missing bar (no 09:35), a session gap
  segBreaks: convertSeries(entry("StopPrice", "segment", [pt(day("09:30"), 5), pt(day("09:31"), 5), pt(day("09:32"), 6),
    pt(day("09:34"), 7), pt(day("09:36"), 8), pt(at(DAY2, "09:30"), 9), pt(at(DAY2, "09:31"), 9)]), times),
  // a line is never broken, even over a gap in the bars
  lineGap: convertSeries(entry("MA50", "line", [pt(day("09:30"), 5), pt(day("09:34"), 7), pt(day("09:36"), 8)]), times),
  up: convertSeries(entry("mins", "marker-up", [pt(day("09:31"), 100), pt(day("09:34"), 101), pt("2024-01-02 09:40:00", 5)]), times),
  down: convertSeries(entry("maxs", "marker-down", [pt(day("09:32"), 103)]), times),
  dot: convertSeries(entry("pivot", "marker-dot", [pt(day("09:33"), 103)]), times),
  baseline: convertSeries(entry("Trade", "baseline", [pt(day("09:33"), 1)]), times),
  unknown: convertSeries(entry("odd", "scatter-plot", [pt(day("09:30"), 1), pt(day("09:32"), 2)]), times),
  empty: convertSeries(entry("none", "line", []), times),
  junk: convertSeries(null, times),
};
console.log(JSON.stringify(results));
""")
    assert isinstance(out, dict)
    t = [seconds(f"2024-01-02 09:{m}:00") for m in ("30", "31", "32", "33", "34", "36")]
    d2 = [seconds("2024-01-03 09:30:00"), seconds("2024-01-03 09:31:00")]
    assert out["times"] == [*t, *d2]
    # line: points in time order on candle times; the repeated time keeps its last value
    line = out["line"]
    assert line["kind"] == "line" and line["drawn"] is True and line["unknownType"] is False
    assert line["data"] == [{"time": t[0], "value": 1}, {"time": t[1], "value": 2}, {"time": t[2], "value": 9}, {"time": t[3], "value": 4}]
    assert out["lineOffCandle"]["data"] == [{"time": t[0], "value": 1}]
    assert out["lineOffCandle"]["dropped"] == 3 and out["lineOffCandle"]["count"] == 1  # 09:32:30, garbage, 09:35
    for result in out.values():
        if isinstance(result, dict):
            for point in result["data"]:
                assert point["time"] in out["times"]  # only candle times are ever used
    # segment: nothing breaks inside a run of adjacent bars
    run = out["segRun"]
    assert run["kind"] == "segment" and run["runs"] == 1 and len(run["pieces"]) == 1
    assert all("value" in p for p in run["data"]) and len(run["data"]) == 5
    # a skipped bar gets a whitespace point on the candle after the last point; neighbours in the bar list that
    # are further apart in time (a missing bar, a session gap) start a series of their own
    breaks = out["segBreaks"]
    assert breaks["pieces"] == [
        [{"time": t[0], "value": 5}, {"time": t[1], "value": 5}, {"time": t[2], "value": 6}, {"time": t[3]},
         {"time": t[4], "value": 7}],
        [{"time": t[5], "value": 8}],
        [{"time": d2[0], "value": 9}, {"time": d2[1], "value": 9}],
    ]
    assert breaks["runs"] == 4 and breaks["count"] == 7
    assert out["lineGap"]["runs"] == 1 and len(out["lineGap"]["pieces"]) == 1 and len(out["lineGap"]["data"]) == 3
    assert not any("value" not in p for p in out["lineGap"]["data"])
    # markers
    assert out["up"]["markers"] == [{"time": t[1], "position": "belowBar", "shape": "arrowUp"},
                                    {"time": t[4], "position": "belowBar", "shape": "arrowUp"}]
    assert out["up"]["dropped"] == 1 and out["up"]["data"] == []
    assert out["down"]["markers"] == [{"time": t[2], "position": "aboveBar", "shape": "arrowDown"}]
    assert out["dot"]["markers"] == [{"time": t[3], "position": "inBar", "shape": "circle"}]
    # baseline is not drawn; an unknown type is a plain line flagged as unknown
    assert out["baseline"]["drawn"] is False and out["baseline"]["data"] == [] and out["baseline"]["markers"] == []
    unknown = out["unknown"]
    assert unknown["drawn"] is True and unknown["unknownType"] is True and unknown["kind"] == "unknown"
    assert unknown["data"] == [{"time": t[0], "value": 1}, {"time": t[2], "value": 2}]
    assert out["empty"]["data"] == [] and out["empty"]["count"] == 0
    assert out["junk"]["name"] == "" and out["junk"]["data"] == []


ORDERS_SOURCE = BARS_SOURCE + """
import { barTimes, buildOrderLines } from "./js/charts/overlays.js";
const minutes = [];
for (let m = 30; m <= 40; m += 1) minutes.push({ time: at(DAY1, "09:" + m), open: 1, high: 2, low: 0.5, close: 1.5, volume: 1 });
const T = barTimes(minutes);
const m = (n) => T[n - 30]; // the chart time of 09:<n>
const ev = (order_id, ref, kind, hm, extra = {}) => ({ order_id, account: "A/1", broker_ref: ref, kind, time: at(DAY1, "09:" + hm),
  side: "sell", quantity: "100.0000", order_type: extra.order_type ?? null, price: extra.price ?? null });
const fill = (ref, reason, hm, extra = {}) => ({ account: "A/1", broker_ref: ref, reason, time: at(DAY1, "09:" + hm), price: "1.0000", ...extra });
const events = [
  // position 1: the stop placed, moved twice, cancelled by the algo's exit; a target placed and cancelled with it
  ev(1, "1", "placed", "31", { order_type: "stop", price: "100.0000" }),
  ev(2, "1", "placed", "31", { order_type: "target", price: "110.0000" }),
  ev(1, "1", "modified", "33", { price: "101.0000" }),
  ev(1, "1", "modified", "35", { price: "102.0000" }),
  ev(1, "1", "cancelled", "37"), ev(2, "1", "cancelled", "37"),
  // position 2: a stop with no cancel event, closed by the synthetic end-of-data fill
  ev(3, "2", "placed", "38", { order_type: "stop", price: "50.0000" }),
  // position 3: a stop still working at the end of the window, no exit
  ev(4, "3", "placed", "36", { order_type: "stop", price: "20.0000" }),
  // position 4: its entry never filled, the order never worked
  ev(5, "4", "placed", "31", { order_type: "stop", price: "7.0000" }), ev(5, "4", "cancelled", "32"),
  // not a stop or target
  ev(6, "1", "placed", "31", { order_type: "exit" }),
];
const fills = [fill("1", "entry", "32"), fill("1", "algo_exit", "38"), fill("2", "entry", "39"),
  fill("2", "end_of_data", "40", { synthetic: true }), fill("3", "entry", "37")];
const line = (l) => l && ({ role: l.role, ref: l.brokerRef, start: l.start, end: l.end, data: l.data.map((p) => [p.time, p.value]),
  before: l.startsBeforeWindow, after: l.endsAfterWindow });
const pick = (lines, role, ref) => line(lines.find((l) => l.role === role && l.brokerRef === ref));
"""


def test_stop_and_target_lines_from_order_events_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_orders", ORDERS_SOURCE + """
const all = buildOrderLines(events, fills, T);
// the same positions seen through a window that starts after the stop was placed, and through two that miss them
const late = buildOrderLines(events, fills, T.slice(5)); // from 09:35
const after = buildOrderLines(events, fills, barTimes([{ time: at(DAY2, "09:30"), open: 1, high: 1, low: 1, close: 1 }, { time: at(DAY2, "09:31"), open: 1, high: 1, low: 1, close: 1 }]));
const before = buildOrderLines(events, fills, barTimes([{ time: at(DAY1, "09:10"), open: 1, high: 1, low: 1, close: 1 }, { time: at(DAY1, "09:20"), open: 1, high: 1, low: 1, close: 1 }]));
// the stop of position 1 ended before this window began
const tail = buildOrderLines(events, fills, T.slice(8)); // 09:38 to 09:40
console.log(JSON.stringify({
  T: T.length, roles: all.map((l) => l.role + ":" + l.brokerRef),
  stop1: pick(all, "stop", "1"), target1: pick(all, "target", "1"), stop2: pick(all, "stop", "2"), stop3: pick(all, "stop", "3"),
  lateStop1: pick(late, "stop", "1"), lateTarget1: pick(late, "target", "1"), lateRoles: late.map((l) => l.role + ":" + l.brokerRef),
  after: after.length, before: before.length, tailRoles: tail.map((l) => l.role + ":" + l.brokerRef),
  noFills: buildOrderLines(events, null, T).map((l) => l.role + ":" + l.brokerRef),
  junk: [buildOrderLines(null, null, T), buildOrderLines(events, fills, []), buildOrderLines([null, {}], [null], T)],
  times: Object.fromEntries(T.map((t, i) => [String(30 + i), t])),
}));
""")
    assert isinstance(out, dict)
    t = {int(k): v for k, v in out["times"].items()}
    # two positions in one window: stop and target of position 1, the stop of 2 and of 3; position 4 never worked
    assert out["roles"] == ["stop:1", "target:1", "stop:2", "stop:3"]
    # placed, modified twice, cancelled: the price at each change, held to the cancel (not to the exit fill at 09:38)
    assert out["stop1"] == {"role": "stop", "ref": "1", "start": t[31], "end": t[37], "before": False, "after": False,
                            "data": [[t[31], 100], [t[33], 101], [t[35], 102], [t[37], 102]]}
    assert out["target1"]["data"] == [[t[31], 110], [t[37], 110]]
    # placed, never cancelled: it ends at the position's exit fill (here the synthetic end-of-data fill)
    assert out["stop2"]["end"] == t[40] and out["stop2"]["data"] == [[t[38], 50], [t[40], 50]]
    # placed, never cancelled, no exit: it runs to the end of the window
    assert out["stop3"]["end"] is None and out["stop3"]["data"] == [[t[36], 20], [t[40], 20]]
    # a window that starts after the stop was placed: it starts at the first bar with the then-current price
    late = out["lateStop1"]
    assert late["before"] is True and late["start"] == t[31]
    assert late["data"] == [[t[35], 102], [t[37], 102]]
    assert out["lateTarget1"]["data"] == [[t[35], 110], [t[37], 110]]
    assert out["lateRoles"] == ["stop:1", "target:1", "stop:2", "stop:3"]
    # lines entirely outside the window are left out
    assert out["after"] == 1 and out["before"] == 0  # (the order with no end is still working the next day)
    assert out["tailRoles"] == ["stop:2", "stop:3"]  # position 1 ended at 09:37
    # without the fills nothing can be said about entries: only the cancelled-without-entry order is kept too
    assert out["noFills"] == ["stop:1", "target:1", "stop:2", "stop:3", "stop:4"]
    assert out["junk"] == [[], [], []]


# --- the plan: defaults, notes ------------------------------------------------------------------

PLAN_SOURCE = ORDERS_SOURCE + """
import { planOverlays, OWN_COLUMN_NOTE, HINDSIGHT_NOTE, FAILED_NOTE, NO_INDICATORS_NOTE } from "./js/charts/overlays.js";
const entry = (name, draw_type, position, points) => ({ name, draw_type, position, points });
const p = (hm, v) => ({ time: at(DAY1, "09:" + hm), value: v });
const emaSeries = { algo: "A/1", run_status: "completed", available: true, series: [
  entry("MA20", "line", 0, [p("31", 10), p("32", 11)]), entry("MA50", "line", 1, [p("31", 9), p("32", 9.5)]),
  entry("StopPrice", "segment", 2, [p("33", 100), p("34", 100)]), entry("Trade", "baseline", 3, [p("33", 1)]) ] };
const swings = { algo: "E/1", run_status: "completed", available: true, series: [
  entry("mins", "marker-up", 0, [p("31", 10)]), entry("maxs", "marker-down", 1, [p("32", 12)]), entry("TrendLine", "marker-dot", 2, [p("33", 11)]) ] };
const summary = (plan) => ({ items: plan.items.map((i) => ({ key: i.key, name: i.name, kind: i.kindText, on: i.defaultVisible, annotation: i.annotation,
  shape: i.shape, lines: i.lines.length, markers: i.markers.length, count: i.count, dropped: i.dropped })), notes: plan.notes });
"""


def test_defaults_legacy_versus_realistic_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_plan", PLAN_SOURCE + """
const base = { series: emaSeries, times: T, orderEvents: events, fills };
console.log(JSON.stringify({
  legacy: summary(planOverlays({ ...base, orderEvents: null, fillModel: "legacy" })),
  // the fill model decides, not whether order events exist: legacy with events draws no order lines
  legacyWithEvents: summary(planOverlays({ ...base, fillModel: "legacy" })),
  realistic: summary(planOverlays({ ...base, fillModel: "realistic" })),
  // realistic but the orders could not be loaded: no order items, the algo's columns stay off
  realisticNoOrders: summary(planOverlays({ ...base, orderEvents: null, fills: null, fillModel: "realistic", ordersError: new Error("x") })),
  unknownModel: summary(planOverlays({ ...base, fillModel: null })),
  emptyOrders: summary(planOverlays({ ...base, orderEvents: [], fills: [], fillModel: "realistic" })),
  own: OWN_COLUMN_NOTE,
}));
""")
    assert isinstance(out, dict)
    legacy = out["legacy"]
    assert [i["key"] for i in legacy["items"]] == ["series:MA20", "series:MA50", "series:StopPrice"]  # Trade is not drawn
    assert all(i["on"] for i in legacy["items"]) and not any(i["annotation"] for i in legacy["items"])
    assert [i["kind"] for i in legacy["items"]] == ["line", "line", "level"]
    assert out["legacyWithEvents"]["items"] == legacy["items"]
    realistic = out["realistic"]["items"]
    assert [i["key"] for i in realistic] == ["series:MA20", "series:MA50", "series:StopPrice", "orders:stop", "orders:target"]
    by_key = {i["key"]: i for i in realistic}
    assert by_key["series:MA20"]["on"] is True and by_key["series:MA50"]["on"] is True
    assert by_key["series:StopPrice"]["on"] is False  # listed, off by default, labelled
    assert out["own"] in by_key["series:StopPrice"]["annotation"] and "stale" in out["own"]
    assert by_key["orders:stop"]["on"] is True and by_key["orders:stop"]["kind"] == "stop (orders)"
    assert by_key["orders:target"]["on"] is True and by_key["orders:target"]["kind"] == "target (orders)"
    assert by_key["orders:stop"]["lines"] == 3 and by_key["orders:target"]["lines"] == 1  # one line per order
    no_orders = out["realisticNoOrders"]
    assert [i["key"] for i in no_orders["items"]] == ["series:MA20", "series:MA50", "series:StopPrice"]
    assert no_orders["items"][2]["on"] is False
    assert any("could not be loaded" in n for n in no_orders["notes"])
    assert all(i["on"] for i in out["unknownModel"]["items"])  # anything but "realistic" draws the algo's columns
    empty = {i["key"]: i for i in out["emptyOrders"]["items"]}
    assert empty["orders:stop"]["lines"] == 0 and empty["orders:stop"]["on"] is True
    assert "none on this day" not in empty["orders:stop"]["annotation"]  # the drawing layer words that


def test_notes_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_notes", PLAN_SOURCE + """
const times = T;
const notes = (args) => planOverlays({ times, fillModel: "legacy", ...args }).notes;
console.log(JSON.stringify({
  plain: notes({ series: emaSeries }),
  swings: notes({ series: swings }),
  swingsItems: summary(planOverlays({ times, fillModel: "legacy", series: swings })).items,
  failed: notes({ series: emaSeries, runStatus: "failed" }),
  failedBoth: notes({ series: swings, runStatus: "failed" }),
  unavailable: notes({ series: { algo: "A/1", run_status: "completed", available: false, series: [] } }),
  emptyList: notes({ series: { algo: "A/1", run_status: "completed", available: true, series: [] } }),
  noSeries: notes({ series: null }),
  fixed: { hindsight: HINDSIGHT_NOTE, failed: FAILED_NOTE, none: NO_INDICATORS_NOTE },
  dropped: summary(planOverlays({ times, fillModel: "legacy", series: { available: true, series: [entry("X", "line", 0, [p("31", 1), { time: "2024-01-02 09:31:30", value: 2 }])] } })).items,
}));
""")
    assert isinstance(out, dict)
    fixed = out["fixed"]
    assert fixed["hindsight"] == "Swing markers are drawn where the swing happened. The algo only knew some bars later."
    assert fixed["failed"] == "This run failed. Its indicators may be incomplete."
    assert fixed["none"].startswith("No indicators are stored for this algo in this run. Runs stored before indicator capture, and algos whose indicators were skipped at capture, have none.")
    assert out["plain"] == []
    assert out["swings"] == [fixed["hindsight"]]  # a marker-up or marker-down series is present
    assert [(i["name"], i["kind"], i["shape"], i["markers"]) for i in out["swingsItems"]] == [
        ("mins", "swing low", "marker-up", 1), ("maxs", "swing high", "marker-down", 1), ("TrendLine", "marker", "marker-dot", 1)]
    assert out["failed"] == [fixed["failed"]]
    assert out["failedBoth"] == [fixed["hindsight"], fixed["failed"]]
    assert out["unavailable"] == [fixed["none"]] and out["emptyList"] == [fixed["none"]]
    assert out["noSeries"] == []  # a failed request shows its error instead
    assert out["dropped"][0]["dropped"] == 1 and out["dropped"][0]["count"] == 1


# --- the drawing layer --------------------------------------------------------------------------

DRAW_SOURCE = FAKE_DOM + FAKE_LIB + """
LightweightCharts.LineStyle = { Solid: 0, Dotted: 1, Dashed: 2 };
LightweightCharts.LineType.Simple = 0;
const createMarkers = LightweightCharts.createSeriesMarkers;
LightweightCharts.createSeriesMarkers = (series, list) => { const api = createMarkers(series, list); api.detached = 0; api.detach = () => { api.detached += 1; }; return api; };
const { createCandleChart } = await import("./js/charts/candles.js");
const { createOverlays } = await import("./js/charts/overlays.js");
const { ApiError } = await import("./js/api.js");
const DAY1 = "2024-01-02", DAY2 = "2024-01-03";
const at = (day, hm) => day + " " + hm + ":00";
const barsOf = (day) => ["09:30", "09:31", "09:32", "09:33", "09:34"].map((hm, i) => ({ time: at(day, hm), open: 10, high: 12, low: 9, close: 11 + i, volume: 1 }));
const p = (day, hm, v) => ({ time: at(day, hm), value: v });
const entry = (name, draw_type, position, points) => ({ name, draw_type, position, points });
const seriesFor = (day) => ({ algo: "A/1", run_status: "completed", available: true, series: [
  entry("MA20", "line", 0, [p(day, "09:30", 10), p(day, "09:31", 11), p(day, "09:32", 12)]),
  entry("StopPrice", "segment", 1, [p(day, "09:31", 9), p(day, "09:32", 9)]),
  entry("mins", "marker-up", 2, [p(day, "09:33", 9.5)]),
  entry("Trade", "baseline", 3, [p(day, "09:31", 1)]) ] });
const ordersFor = (day) => ({ events: [
  { order_id: 1, account: "A/1", broker_ref: "1", kind: "placed", time: at(day, "09:31"), side: "sell", order_type: "stop", price: "9.0000" },
  { order_id: 2, account: "A/1", broker_ref: "1", kind: "placed", time: at(day, "09:31"), side: "sell", order_type: "target", price: "14.0000" },
  { order_id: 1, account: "A/1", broker_ref: "1", kind: "cancelled", time: at(day, "09:33") },
  { order_id: 2, account: "A/1", broker_ref: "1", kind: "cancelled", time: at(day, "09:33") } ],
  fills: [{ account: "A/1", broker_ref: "1", reason: "entry", time: at(day, "09:32") }, { account: "A/1", broker_ref: "1", reason: "algo_exit", time: at(day, "09:34") }] });
function make(opts = {}) {
  const cleanups = [];
  const ctx = { onCleanup: (f) => cleanups.push(f) };
  const box = document.createElement("div");
  const legendBox = document.createElement("div");
  const candles = createCandleChart(box, { ctx });
  const overlays = createOverlays(candles, { ctx, container: legendBox, ...opts });
  return { cleanups, ctx, box, legendBox, candles, overlays, chart: charts.at(-1), markers: markerApis.at(-1), candleMarkers: markerApis.at(-2) };
}
const draw = (s, day, extra = {}) => s.overlays.setData({ series: seriesFor(day), fillModel: "legacy", runStatus: "completed", bars: barsOf(day), ...extra });
const buttons = (s) => find(s.legendBox, (c) => c.tag === "BUTTON");
const lines = (s) => s.chart.series.filter((x) => x.type === "line");
const texts = (s) => find(s.legendBox, (c) => c.className.split(" ").includes("overlay-note")).map((n) => n.textContent);
"""


def test_the_legend_and_series_follow_the_data_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_draw", DRAW_SOURCE + """
const s = make();
const registered = s.cleanups.length; // candle chart's dispose, its theme remover, then the overlays' dispose
s.candles.setBars(barsOf(DAY1));
draw(s, DAY1);
const first = { series: s.chart.series.length, lines: lines(s).map((x) => [x.opts.lineType, x.opts.lineStyle, x.opts.visible, x.opts.priceLineVisible, x.data.length]),
  buttons: buttons(s).map((b) => [b.getAttribute("aria-pressed"), b.getAttribute("type"), b.textContent]),
  swatches: find(s.legendBox, (c) => c.className === "swatch" || c.className === "swatch-mark").map((c) => [c.className, c.getAttribute("style")]),
  markers: s.markers.log.at(-1).map((m) => [m.time, m.position, m.shape, m.size]),
  notes: texts(s), hidden: s.legendBox.children[0].hidden, inLegendBox: s.legendBox.children[0].className };
// toggling the level off hides its series; the marker series off removes its markers
const toggleLevel = buttons(s)[1];
toggleLevel.fire("click");
const afterLevel = { pressed: toggleLevel.getAttribute("aria-pressed"), visible: lines(s)[1].applied.at(-1) };
const swing = buttons(s)[2];
swing.fire("click");
const afterSwing = { pressed: swing.getAttribute("aria-pressed"), markers: s.markers.log.at(-1).length };
swing.fire("click");
const swingBack = s.markers.log.at(-1).length;
// the day changes: stale overlays go as soon as the candle chart holds other bars; the toggles are remembered
s.candles.setBars(barsOf(DAY2));
const cleared = { series: s.chart.series.length, markers: s.markers.log.at(-1).length, hidden: s.legendBox.children[0].hidden, buttons: buttons(s).length };
draw(s, DAY2);
const second = { buttons: buttons(s).map((b) => b.getAttribute("aria-pressed")), levelVisible: lines(s)[1].opts.visible, maVisible: lines(s)[0].opts.visible,
  times: lines(s)[0].data.map((d) => d.time) };
// bars that are not the chart's: nothing is drawn
s.candles.setBars(barsOf(DAY1));
draw(s, DAY2);
const stale = { series: s.chart.series.length, buttons: buttons(s).length };
console.log(JSON.stringify({ registered, first, afterLevel, afterSwing, swingBack, cleared, second, stale }));
""")
    assert isinstance(out, dict)
    assert out["registered"] == 3
    first = out["first"]
    # legacy fills: the algo's own columns are drawn and on; the baseline is not drawn; one series per line
    assert first["series"] == 1 + 2 and first["hidden"] is False and first["inLegendBox"] == "overlay-legend"
    assert [line[:4] for line in first["lines"]] == [[0, 0, True, False], [1, 0, True, False]]  # the level is stepped
    assert [b[0] for b in first["buttons"]] == ["true", "true", "true"]
    assert all(b[1] == "button" for b in first["buttons"])
    assert first["buttons"][0][2].startswith("MA20") and "line" in first["buttons"][0][2]
    assert "level" in first["buttons"][1][2] and "swing low" in first["buttons"][2][2]
    assert [sw[0] for sw in first["swatches"]] == ["swatch", "swatch", "swatch-mark"]
    assert all(sw[1] for sw in first["swatches"])
    t = seconds("2024-01-02 09:33:00")
    assert first["markers"] == [[t, "belowBar", "arrowUp", 1]]
    assert first["notes"] == ["Swing markers are drawn where the swing happened. The algo only knew some bars later."]
    assert out["afterLevel"] == {"pressed": "false", "visible": {"visible": False}}
    assert out["afterSwing"] == {"pressed": "false", "markers": 0} and out["swingBack"] == 1
    assert out["cleared"] == {"series": 1, "markers": 0, "hidden": True, "buttons": 0}
    second = out["second"]
    assert second["buttons"] == ["true", "false", "true"]  # the level stays off across the day change
    assert second["levelVisible"] is False and second["maVisible"] is True
    assert second["times"] == [seconds(f"2024-01-03 09:3{i}:00") for i in range(3)]
    assert out["stale"] == {"series": 1, "buttons": 0}


def test_realistic_run_draws_order_lines_and_lists_the_algos_columns_off_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_realistic", DRAW_SOURCE + """
const s = make();
s.candles.setBars(barsOf(DAY1));
const o = ordersFor(DAY1);
draw(s, DAY1, { fillModel: "realistic", orderEvents: o.events, fills: o.fills });
const names = buttons(s).map((b) => [b.getAttribute("aria-pressed"), b.textContent]);
const drawn = lines(s).map((x) => [x.opts.visible, x.opts.lineType, x.opts.lineWidth, x.data.map((d) => [d.time, d.value])]);
// the same data under legacy fills: no order lines, the column is on
s.candles.setBars(barsOf(DAY1));
draw(s, DAY1, { fillModel: "legacy", orderEvents: o.events, fills: o.fills });
const legacy = buttons(s).map((b) => [b.getAttribute("aria-pressed"), b.textContent]);
console.log(JSON.stringify({ names, drawn, legacy, nSeries: s.chart.series.length }));
""")
    assert isinstance(out, dict)
    names = out["names"]
    assert [n[0] for n in names] == ["true", "false", "true", "true", "true"]  # MA20, StopPrice (off), swing, stop, target
    assert "StopPrice" in names[1][1] and "algo's own column, can be stale" in names[1][1]
    assert "stop (orders)" in names[3][1] and "target (orders)" in names[4][1]
    t = [seconds(f"2024-01-02 09:3{i}:00") for i in range(5)]
    by_data = {tuple(map(tuple, d[3])): d for d in out["drawn"]}
    stop = by_data[((t[1], 9), (t[3], 9))]  # placed 09:31, cancelled 09:33, held between
    target = by_data[((t[1], 14), (t[3], 14))]
    assert stop[0] is True and stop[1] == 1 and target[0] is True  # stepped, on by default
    own = by_data[((t[1], 9), (t[2], 9))]
    assert own[0] is False  # the algo's own StopPrice: drawn but hidden
    assert [n[0] for n in out["legacy"]] == ["true", "true", "true"] and not any("orders" in n[1] for n in out["legacy"])


def test_legend_error_and_notes_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_errors", DRAW_SOURCE + """
const s = make();
s.candles.setBars(barsOf(DAY1));
const trade = { key: 1, number: 1, side: "long", entry_time: at(DAY1, "09:31"), exit_time: at(DAY1, "09:33"), net: "1.0000", exit_reason: "target" };
s.candles.setTrades([trade], null);
const tradeMarkers = () => s.candleMarkers.log.at(-1).map((m) => m.id);
// the series request failed: an error panel in the legend area, the candles and the trade markers keep working
s.overlays.setData({ series: null, seriesError: new ApiError(500, "boom"), fillModel: "legacy", bars: barsOf(DAY1) });
const errors = find(s.legendBox, (c) => c.className.split(" ").includes("error")).map((e) => e.textContent);
const afterError = { series: s.chart.series.length, candles: s.chart.series[0].data.length, trades: tradeMarkers(), hidden: s.legendBox.children[0].hidden };
s.candles.setTrades([trade], 1);
const stillWorks = tradeMarkers();
// no indicators stored for the algo; a failed run
s.overlays.setData({ series: { algo: "A/1", run_status: "failed", available: false, series: [] }, fillModel: "legacy", runStatus: "failed", bars: barsOf(DAY1) });
const none = { notes: texts(s), errors: find(s.legendBox, (c) => c.className.split(" ").includes("error")).length, buttons: buttons(s).length };
s.overlays.setData({ series: seriesFor(DAY1), fillModel: "legacy", runStatus: "failed", bars: barsOf(DAY1) });
const failed = texts(s);
// points off the candles are counted in the legend entry
s.overlays.setData({ series: { available: true, series: [entry("X", "line", 0, [p(DAY1, "09:30", 1), { time: at(DAY1, "09:30").replace("09:30:00", "09:30:30"), value: 2 }])] }, fillModel: "legacy", bars: barsOf(DAY1) });
const dropped = buttons(s)[0].textContent;
s.overlays.setData({ series: { available: true, series: [entry("Y", "scatter", 0, [p(DAY1, "09:30", 1)])] }, fillModel: "legacy", bars: barsOf(DAY1) });
const unknown = buttons(s)[0].textContent;
// an empty day clears everything
s.overlays.clear();
const emptied = { series: s.chart.series.length, hidden: s.legendBox.children[0].hidden, notes: texts(s).length };
console.log(JSON.stringify({ errors, afterError, stillWorks, none, failed, dropped, unknown, emptied }));
""")
    assert isinstance(out, dict)
    assert out["errors"] == ["Error 500: boom"]
    assert out["afterError"] == {"series": 1, "candles": 5, "trades": ["1:entry", "1:exit"], "hidden": False}
    assert out["stillWorks"] == ["1:entry", "1:exit"]
    assert out["none"]["notes"] == [
        "This run failed. Its indicators may be incomplete.",
        "No indicators are stored for this algo in this run. Runs stored before indicator capture, and algos whose indicators were skipped at capture, have none."]
    assert out["none"]["errors"] == 0 and out["none"]["buttons"] == 0
    assert out["failed"] == ["Swing markers are drawn where the swing happened. The algo only knew some bars later.",
                             "This run failed. Its indicators may be incomplete."]
    assert "1 point not on a bar, not drawn" in out["dropped"]
    assert "unknown type (scatter)" in out["unknown"]
    assert out["emptied"] == {"series": 1, "hidden": True, "notes": 0}


def test_theme_change_dispose_and_the_bars_hook_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_dispose", DRAW_SOURCE + """
const s = make();
const seen = [];
const remove = s.candles.onBars((bars) => seen.push(bars.length));
s.candles.setBars(barsOf(DAY1));
remove();
s.candles.setBars(barsOf(DAY2));
const hook = { seen, bars: seen.length };
const o = ordersFor(DAY2);
draw(s, DAY2, { fillModel: "realistic", orderEvents: o.events, fills: o.fills });
const added = lines(s);
const before = added.map((x) => x.applied.length);
// a colour scheme change re-applies the colours of every overlay series and re-sets the markers
const staleListeners = schemeListeners.slice();
staleListeners.forEach((f) => f());
const recolored = added.every((x, i) => x.applied.length > before[i] && "color" in x.applied.at(-1));
s.overlays.dispose();
s.overlays.dispose();
const afterDispose = { series: s.chart.series.length, detached: s.markers.detached, appliedBefore: added.map((x) => x.applied.length), markerCalls: s.markers.log.length };
// a scheme change that was already queued reaches nothing of the overlays
staleListeners.forEach((f) => f());
const calls = { applied: added.map((x) => x.applied.length), markerCalls: s.markers.log.length, series: s.chart.series.length };
// setData and clear after dispose do nothing
draw(s, DAY2); s.overlays.clear();
const afterCalls = { series: s.chart.series.length, markerCalls: s.markers.log.length };
// disposal is registered once and runs through the context
s.cleanups.forEach((f) => f());
console.log(JSON.stringify({ hook, recolored, nAdded: added.length, afterDispose, calls, afterCalls, detachedTwice: s.markers.detached,
  removedChart: s.chart.removed }));
""")
    assert isinstance(out, dict)
    assert out["hook"] == {"seen": [5], "bars": 1}  # called once with the converted bars; the removed hook is silent
    assert out["recolored"] is True and out["nAdded"] == 4  # MA20, the own StopPrice, the stop and the target
    assert out["afterDispose"]["series"] == 1 and out["afterDispose"]["detached"] == 1  # only the candles remain
    assert out["calls"]["applied"] == out["afterDispose"]["appliedBefore"]  # nothing after disposal
    assert out["calls"]["markerCalls"] == out["afterDispose"]["markerCalls"] and out["calls"]["series"] == 1
    assert out["afterCalls"] == {"series": 1, "markerCalls": out["afterDispose"]["markerCalls"]}
    assert out["detachedTwice"] == 1 and out["removedChart"] == 1


def test_the_library_must_provide_what_the_overlays_need_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_lib", DRAW_SOURCE + """
const s = make();
let message = null;
try { createOverlays(s.candles, { ctx: s.ctx, lib: {} }); } catch (err) { message = err.message; }
// a context that has already ended: the overlays are disposed at once and draw nothing
const ended = { onCleanup: (f) => f() };
const late = createOverlays(s.candles, { ctx: ended, container: document.createElement("div") });
s.candles.setBars(barsOf(DAY1));
late.setData({ series: seriesFor(DAY1), fillModel: "legacy", bars: barsOf(DAY1) });
console.log(JSON.stringify({ message, series: s.chart.series.length }));
""")
    assert isinstance(out, dict)
    assert "not loaded" in out["message"] and out["series"] == 1


# --- wiring in views/algo.js --------------------------------------------------------------------

WIRING_SOURCE = VIEW_SOURCE + """
LightweightCharts.LineStyle = { Solid: 0, Dotted: 1, Dashed: 2 };
LightweightCharts.LineType.Simple = 0;
const at = (day, hm) => day + " " + hm + ":00";
const seriesBody = (day) => ({ algo: "EMA/1", run_status: "completed", available: true, series: [
  { name: "MA20", draw_type: "line", position: 0, points: [{ time: at(day, "09:30"), value: 10 }, { time: at(day, "09:31"), value: 11 }, { time: at(day, "09:32"), value: 12 }] },
  { name: "StopPrice", draw_type: "segment", position: 1, points: [{ time: at(day, "09:31"), value: 9 }, { time: at(day, "09:32"), value: 9 }] },
  { name: "Trade", draw_type: "baseline", position: 2, points: [{ time: at(day, "09:31"), value: 1 }] } ] });
const ordersBody = { run_id: 1, algo: "EMA/1", order_events: [
  { order_id: 1, account: "EMA/1", broker_ref: "1", kind: "placed", time: "2024-01-02 09:30:00", side: "sell", order_type: "stop", price: "9.5000" },
  { order_id: 2, account: "EMA/1", broker_ref: "1", kind: "placed", time: "2024-01-02 09:30:00", side: "sell", order_type: "target", price: "14.0000" },
  { order_id: 1, account: "EMA/1", broker_ref: "1", kind: "modified", time: "2024-01-02 09:31:00", price: "9.8000" },
  { order_id: 1, account: "EMA/1", broker_ref: "1", kind: "cancelled", time: "2024-01-02 09:32:00" },
  { order_id: 2, account: "EMA/1", broker_ref: "1", kind: "cancelled", time: "2024-01-02 09:32:00" } ],
  fills: [{ account: "EMA/1", broker_ref: "1", reason: "entry", time: "2024-01-02 09:31:00" }, { account: "EMA/1", broker_ref: "1", reason: "algo_exit", time: "2024-01-02 09:32:00" }] };
const seriesRequests = () => requests.filter((r) => r.path.endsWith("/series"));
const orderRequests = () => requests.filter((r) => r.path.endsWith("/orders"));
const overlayBox = (container) => byClass(container, "overlay-box")[0];
const toggles = (container) => find(overlayBox(container), (c) => c.tag === "BUTTON").map((b) => [b.getAttribute("aria-pressed"), b.textContent]);
const lineSeries = () => charts[1].series.filter((s) => s.type === "line");
"""


def test_series_and_orders_are_requested_for_the_same_window_once_per_view_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "wiring_realistic", WIRING_SOURCE + """
install({ "/api/runs/1": ok(doc({}, { fills: "realistic" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": (req) => [seriesBody(req.query.start.slice(0, 10)), 200], "/api/runs/1/orders": ok(ordersBody) });
const { container, done } = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await done;
await tick();
const out = {
  barsQuery: barsRequests()[0].query, seriesQuery: seriesRequests()[0].query,
  orders: orderRequests().map((r) => [r.query, Boolean(r.signal)]),
  day1: { toggles: toggles(container), lines: lineSeries().map((s) => [s.opts.visible, s.data.length]), candles: charts[1].series[0].data.length },
};
// the legend sits before the chart, in its own box
const panel = byClass(container, "overlay-box")[0].parent;
out.order = panel.children.map((c) => c.className || c.tag);
// the algo's own column is switched on by hand; the other day keeps it on
find(overlayBox(container), (c) => c.tag === "BUTTON")[1].fire("click");
rowButtons(container)[1].fire("click"); // trade 2, on 2024-01-03
await tick();
barsRequests()[1].answer(barsFor("2024-01-03"));
await tick();
out.day2 = { seriesQuery: seriesRequests()[1].query, orders: orderRequests().length, series: seriesRequests().length,
  toggles: toggles(container), lines: lineSeries().map((s) => [s.opts.visible, s.data.length]) };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    window1 = {"start": "2024-01-02 00:00:00", "end": "2024-01-02 23:59:59"}
    assert out["barsQuery"] == {"symbol": "AAPL", **window1}
    assert out["seriesQuery"] == {"algo": "EMA/1", **window1}  # identical start and end
    assert out["orders"] == [[{"algo": "EMA/1"}, True]]  # once, with the navigation's signal
    day1 = out["day1"]
    names = [t[1] for t in day1["toggles"]]
    assert [t[0] for t in day1["toggles"]] == ["true", "false", "true", "true"]
    assert names[0].startswith("MA20") and names[1].startswith("StopPrice") and "stale" in names[1]
    assert "stop (orders)" in names[2] and "target (orders)" in names[3]
    assert len(day1["lines"]) == 4 and day1["candles"] == 3  # MA20, StopPrice, stop, target
    assert [line[0] for line in day1["lines"]] == [True, False, True, True]
    assert out["order"].index("overlay-box") < out["order"].index("chart-box")
    day2 = out["day2"]
    assert day2["seriesQuery"]["start"] == "2024-01-03 00:00:00" and day2["series"] == 2
    assert day2["orders"] == 1  # not per day
    assert [t[0] for t in day2["toggles"]][:2] == ["true", "true"]  # the switched-on column stays on
    assert len(day2["lines"]) == 2 and all(line[0] for line in day2["lines"])  # no order lines on that day


def test_a_legacy_run_requests_no_orders_and_draws_the_algos_columns_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "wiring_legacy", WIRING_SOURCE + """
install({ "/api/runs/1": ok(doc({}, { fills: "legacy" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": (req) => [seriesBody(req.query.start.slice(0, 10)), 200] });
const { container, done } = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await done;
await tick();
console.log(JSON.stringify({ orders: orderRequests().length, toggles: toggles(container), lines: lineSeries().length }));
""")
    assert isinstance(out, dict)
    assert out["orders"] == 0 and out["lines"] == 2
    assert [t[0] for t in out["toggles"]] == ["true", "true"]


def test_a_newer_day_aborts_bars_and_series_and_only_the_newest_draws_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "wiring_abort", WIRING_SOURCE + """
install({ "/api/runs/1": ok(doc({}, { fills: "legacy" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity) });
const { container, done } = await open();
const firstBars = barsRequests()[0];
const firstSeries = seriesRequests()[0];
rowButtons(container)[1].fire("click"); // another day while both are pending
const aborted = [firstBars.aborted, firstSeries.aborted];
const secondBars = barsRequests()[1];
const secondSeries = seriesRequests()[1];
firstSeries.answer(seriesBody("2024-01-02")); // late answers of the old day change nothing
firstBars.answer(barsFor("2024-01-02"));
await tick();
const stale = { lines: lineSeries().length, candles: (charts[1].series[0].data || []).length };
secondBars.answer(barsFor("2024-01-03"));
secondSeries.answer(seriesBody("2024-01-03"));
await done;
await tick();
const times = lineSeries()[0].data.map((d) => d.time);
console.log(JSON.stringify({ aborted, stale, lines: lineSeries().length, times, candles: charts[1].series[0].data.length,
  both: [secondBars.aborted, secondSeries.aborted] }));
""")
    assert isinstance(out, dict)
    assert out["aborted"] == [True, True]
    assert out["stale"] == {"lines": 0, "candles": 0}
    assert out["lines"] == 2 and out["candles"] == 3 and out["both"] == [False, False]
    assert out["times"] == [seconds(f"2024-01-03 09:3{i}:00") for i in range(3)]


def test_a_failed_series_request_leaves_candles_and_trades_and_empty_days_clear_overlays_with_node(
        node_workdir: Path) -> None:
    out = run_node(node_workdir, "wiring_failures", WIRING_SOURCE + """
const result = {};
install({ "/api/runs/1": ok(doc({}, { fills: "legacy" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": () => [{ detail: "boom" }, 500] });
let opened = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await opened.done;
await tick();
result.failed = { errors: byClass(overlayBox(opened.container), "error").map((e) => e.textContent), candles: charts[1].series[0].data.length,
  markers: markerApis[0].log.at(-1).map((m) => m.id), rows: rowButtons(opened.container).length,
  barsErrors: byClass(opened.container, "bars-error")[0].children.length };

// a day without bars: whatever was drawn before is cleared, even though the series arrive
requests.length = 0; charts.length = 0; markerApis.length = 0;
install({ "/api/runs/1": ok(doc({}, { fills: "legacy" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": (req) => [seriesBody(req.query.start.slice(0, 10)), 200] });
opened = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await opened.done;
await tick();
const drawn = toggles(opened.container).length;
for (const [name, body] of [["empty", barsFor("2024-01-03", { bars: [] })], ["missing", { symbol: "AAPL", available: false, reason: "missing", bars: [] }],
  ["changed", { symbol: "AAPL", available: false, reason: "changed", bars: [] }]]) {
  rowButtons(opened.container)[1].fire("click");
  barsRequests().at(-1).answer(body);
  await tick();
  result[name] = { toggles: toggles(opened.container).length, lines: lineSeries().length };
  rowButtons(opened.container)[0].fire("click"); // back to a day with bars
  barsRequests().at(-1).answer(barsFor("2024-01-02"));
  await tick();
  result[name].back = toggles(opened.container).length;
}
result.drawn = drawn;
console.log(JSON.stringify(result));
""")
    assert isinstance(out, dict)
    failed = out["failed"]
    assert failed["errors"] == ["Error 500: boom"] and failed["candles"] == 3 and failed["barsErrors"] == 0
    assert failed["markers"] == ["1:entry", "1:exit"] and failed["rows"] == 2
    assert out["drawn"] == 2
    for name in ("empty", "missing", "changed"):
        assert out[name]["toggles"] == 0 and out[name]["lines"] == 0, name
        assert out[name]["back"] == 2, name
