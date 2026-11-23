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
    # Placed at 09:31 (decision bar), the entry fills on 09:32; modified at 09:33 and 09:35 (first checked on
    # 09:34 and 09:36); cancelled at 09:37 (it rested through that bar, so the line ends there, not at the
    # exit fill at 09:38). The line starts on the entry fill bar, not on the decision bar.
    assert out["stop1"] == {"role": "stop", "ref": "1", "start": t[32], "end": t[37], "before": False, "after": False,
                            "data": [[t[32], 100], [t[34], 101], [t[36], 102], [t[37], 102]]}
    assert out["target1"]["data"] == [[t[32], 110], [t[37], 110]]
    # placed at 09:38, never cancelled: it works from 09:39 (the entry fill) to the position's exit fill (here
    # the synthetic end-of-data fill)
    assert out["stop2"]["end"] == t[40] and out["stop2"]["data"] == [[t[39], 50], [t[40], 50]]
    # placed at 09:36, entry fill 09:37, never cancelled, no exit: it runs to the end of the window
    assert out["stop3"]["end"] is None and out["stop3"]["data"] == [[t[37], 20], [t[40], 20]]
    # a window that starts after the stop was placed: it starts at the first bar with the then-current price;
    # the 09:35 change (the window's first bar) applies from the next bar
    late = out["lateStop1"]
    assert late["before"] is True and late["start"] == t[35]
    assert late["data"] == [[t[35], 101], [t[36], 102], [t[37], 102]]
    assert out["lateTarget1"]["data"] == [[t[35], 110], [t[37], 110]]
    assert out["lateRoles"] == ["stop:1", "target:1", "stop:2", "stop:3"]
    # lines entirely outside the window are left out
    assert out["after"] == 1 and out["before"] == 0  # (the order with no end is still working the next day)
    assert out["tailRoles"] == ["stop:2", "stop:3"]  # position 1 ended at 09:37
    # without the fills nothing can be said about entries: only the cancelled-without-entry order is kept too
    assert out["noFills"] == ["stop:1", "target:1", "stop:2", "stop:3", "stop:4"]
    assert out["junk"] == [[], [], []]


TIMING_SOURCE = ORDERS_SOURCE + """
import { planOverlays } from "./js/charts/overlays.js";
const view = (l) => ({ role: l.role, ref: l.brokerRef, start: l.start, end: l.end, data: l.data.map((p) => [p.time, p.value]) });
const byRef = (lines, role, ref) => { const l = lines.find((x) => x.role === role && x.brokerRef === ref); return l ? view(l) : null; };
const evs = [
  // position 1 (the reviewer's case): the stop is placed on 09:31, the entry fills on 09:32, and the stop is
  // moved on 09:34 and again on 09:35 (to a level inside the range of the 09:35 bar). Nothing fills on 09:35;
  // the stop fills on 09:36.
  ev(1, "1", "placed", "31", { order_type: "stop", price: "100.0000" }),
  ev(1, "1", "modified", "34", { price: "103.0000" }),
  ev(1, "1", "modified", "35", { price: "104.5000" }),
  // position 2: placed early, but the entry fills only on 09:35; a move before the entry
  ev(2, "2", "placed", "31", { order_type: "stop", price: "50.0000" }),
  ev(2, "2", "modified", "33", { price: "52.0000" }),
  // position 3: moved and cancelled on the same bar 09:36 (the move would first apply on 09:37)
  ev(3, "3", "placed", "31", { order_type: "stop", price: "10.0000" }),
  ev(3, "3", "modified", "36", { price: "11.0000" }), ev(3, "3", "cancelled", "36"),
  // position 4: moved on the last bar of the window (it would first apply after the window)
  ev(4, "4", "placed", "31", { order_type: "stop", price: "10.0000" }),
  ev(4, "4", "modified", "40", { price: "12.0000" }),
  // position 5: stop and target placed on 09:38, the entry fills on 09:39, both cancelled on 09:39: one bar
  ev(5, "5", "placed", "38", { order_type: "stop", price: "7.0000" }),
  ev(6, "5", "placed", "38", { order_type: "target", price: "9.0000" }),
  ev(5, "5", "cancelled", "39"), ev(6, "5", "cancelled", "39"),
];
const fls = [fill("1", "entry", "32"), fill("1", "stop", "36"), fill("2", "entry", "35"), fill("3", "entry", "32"),
  fill("3", "algo_exit", "37"), fill("4", "entry", "32"), fill("5", "entry", "39"), fill("5", "algo_exit", "40")];
const timing = buildOrderLines(evs, fls, T);
"""


def test_order_lines_work_from_the_bar_after_the_decision_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_timing", TIMING_SOURCE + """
console.log(JSON.stringify({ times: Object.fromEntries(T.map((t, i) => [String(30 + i), t])),
  stop1: byRef(timing, "stop", "1"), stop2: byRef(timing, "stop", "2"), stop3: byRef(timing, "stop", "3"),
  stop4: byRef(timing, "stop", "4"), stop5: byRef(timing, "stop", "5"), target5: byRef(timing, "target", "5") }));
""")
    assert isinstance(out, dict)
    t = {int(k): v for k, v in out["times"].items()}
    # the reviewer's case: the move made on 09:35 is first checked on 09:36, so the level drawn on 09:35 is the
    # old one (103), the new one (104.5) starts on the fill bar; the line starts on the entry fill bar 09:32
    stop1 = out["stop1"]
    assert stop1["start"] == t[32] and stop1["end"] == t[36]
    assert stop1["data"] == [[t[32], 100], [t[35], 103], [t[36], 104.5]]
    # a line never starts before the entry fill bar; a move before it holds the then-current price
    assert out["stop2"]["start"] == t[35] and out["stop2"]["data"] == [[t[35], 52], [t[40], 52]]
    # a change whose effective bar is past the end of the line is not drawn
    assert out["stop3"]["data"] == [[t[32], 10], [t[36], 10]] and out["stop3"]["end"] == t[36]
    assert out["stop4"]["data"] == [[t[32], 10], [t[40], 10]]  # the 09:40 move would apply after the window
    # one bar only: a single point
    assert out["stop5"]["data"] == [[t[39], 7]] and out["target5"]["data"] == [[t[39], 9]]


def test_a_one_bar_level_is_a_circle_marker_at_its_price_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_one_bar", TIMING_SOURCE + """
const plan = planOverlays({ times: T, orderEvents: evs, fills: fls, fillModel: "realistic" });
const item = (role) => plan.items.find((i) => i.key === "orders:" + role);
const brief = (i) => ({ lines: i.lines.map((l) => [l.style, l.width, l.data.length]), markers: i.markers, count: i.count });
console.log(JSON.stringify({ t39: T[9], stop: brief(item("stop")), target: brief(item("target")) }));
""")
    assert isinstance(out, dict)
    stop, target = out["stop"], out["target"]
    # stop lines are dashed and target lines dotted, two pixels wide; the one-bar level has no line
    assert len(stop["lines"]) == 4 and all(line[:2] == ["dashed", 2] for line in stop["lines"])
    assert target["lines"] == []
    assert stop["markers"] == [{"time": out["t39"], "position": "atPriceMiddle", "price": 7, "shape": "circle", "text": "stop"}]
    assert target["markers"] == [{"time": out["t39"], "position": "atPriceMiddle", "price": 9, "shape": "circle", "text": "target"}]
    assert target["count"] == 1


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
const drawn = lines(s).map((x) => [x.opts.visible, x.opts.lineType, x.opts.lineWidth, x.data.map((d) => [d.time, d.value]), x.opts.lineStyle, x.opts.color]);
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
    stop = by_data[((t[2], 9), (t[3], 9))]  # placed 09:31, entry fills 09:32, cancelled 09:33
    target = by_data[((t[2], 14), (t[3], 14))]
    assert stop[0] is True and stop[1] == 1 and target[0] is True  # stepped, on by default
    assert stop[2] == 2 and target[2] == 2  # line width 2
    assert stop[4] == 2 and target[4] == 1  # a stop is dashed, a target dotted (LineStyle.Dashed, Dotted)
    assert stop[5] != target[5]
    own = by_data[((t[1], 9), (t[2], 9))]  # its own column, drawn on the bars it holds
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


def test_stepping_a_day_while_the_shown_day_loads_does_not_abort_it_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "wiring_step_while_loading", WIRING_SOURCE + """
const routes = () => ({ "/api/runs/1": ok(doc({}, { fills: "legacy" })), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": (req) => [seriesBody(req.query.start.slice(0, 10)), 200] });
const dayButtons = (container) => find(byClass(container, "day-control")[0], (c) => c.tag === "BUTTON");
const result = {};
async function scenario(name, answerSearch) {
  requests.length = 0; charts.length = 0; markerApis.length = 0;
  install(routes());
  const { container, done } = await open();
  const day = barsRequests()[0], daySeries = seriesRequests()[0];
  dayButtons(container)[1].fire("click"); // Next, while 2024-01-02 is still loading
  const search = barsRequests()[1];
  const during = { day: day.aborted, series: daySeries.aborted, search: search.query.start };
  answerSearch(search);
  await tick();
  const afterSearch = { day: day.aborted, series: daySeries.aborted, requests: barsRequests().length };
  if (!day.aborted) day.answer(barsFor("2024-01-02"));
  await tick();
  const second = barsRequests()[2];
  if (second) second.answer(barsFor("2024-01-03"));
  await done;
  await tick();
  result[name] = { during, afterSearch, label: byClass(container, "day-label")[0].textContent, candles: charts[1].series[0].data.length,
    toggles: toggles(container).length, lines: lineSeries().length, errors: byClass(container, "bars-error")[0].children.length };
}
await scenario("nothing", (search) => search.answer({ symbol: "AAPL", available: true, reason: null, bars: [] }));
await scenario("failed", (search) => search.answer({ detail: "boom" }, 500));
await scenario("found", (search) => search.answer({ symbol: "AAPL", available: true, reason: null, bars: barsFor("2024-01-03").bars }));
console.log(JSON.stringify(result));
""")
    assert isinstance(out, dict)
    for name in ("nothing", "failed"):
        run = out[name]
        assert run["during"]["day"] is False and run["during"]["series"] is False, name  # the search did not abort the day
        assert run["afterSearch"]["day"] is False and run["afterSearch"]["series"] is False, name
        assert run["afterSearch"]["requests"] == 2, name  # no other day was requested
        assert run["label"] == "2024-01-02 (data time)" and run["candles"] == 3, name  # it finished normally
        assert run["toggles"] == 2 and run["lines"] == 2, name  # with its overlays
    assert out["failed"]["errors"] == 1 and out["nothing"]["errors"] == 0
    found = out["found"]
    assert found["during"]["day"] is False and found["afterSearch"]["day"] is True  # replaced only once a day is found
    assert found["label"] == "2024-01-03 (data time)" and found["candles"] == 3 and found["lines"] == 2


def test_a_run_with_only_the_baseline_series_says_nothing_is_drawable_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_nothing_drawable", DRAW_SOURCE + """
import { planOverlays, NO_DRAWABLE_NOTE } from "./js/charts/overlays.js";
const baselineOnly = { algo: "A/1", run_status: "completed", available: true, series: [entry("Trade", "baseline", 0, [p(DAY1, "09:31", 1)])] };
const s = make();
s.candles.setBars(barsOf(DAY1));
s.overlays.setData({ series: baselineOnly, fillModel: "legacy", runStatus: "completed", bars: barsOf(DAY1) });
console.log(JSON.stringify({ hidden: s.legendBox.children[0].hidden, notes: texts(s), buttons: buttons(s).length, text: NO_DRAWABLE_NOTE,
  planNotes: planOverlays({ series: baselineOnly, times: [1, 2], fillModel: "legacy" }).notes,
  // a real indicator next to it: no such note
  withLine: planOverlays({ series: seriesFor(DAY1), times: [1, 2], fillModel: "legacy" }).notes }));
""")
    assert isinstance(out, dict)
    assert out["text"] == "No drawable indicators are stored for this algo in this run."
    assert out["hidden"] is False and out["notes"] == [out["text"]] and out["buttons"] == 0
    assert out["planNotes"] == [out["text"]] and out["text"] not in out["withLine"]


def test_a_one_bar_order_level_is_drawn_as_a_marker_by_the_drawing_layer_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_one_bar_draw", DRAW_SOURCE + """
const s = make();
s.candles.setBars(barsOf(DAY1));
const o = ordersFor(DAY1);
// the stop is placed on 09:31, the entry fills on 09:32 and the stop is cancelled on 09:32: it worked on one bar
const events = o.events.filter((e) => e.order_id === 1 && e.kind !== "cancelled").concat([{ order_id: 1, account: "A/1", broker_ref: "1", kind: "cancelled", time: at(DAY1, "09:32") }]);
draw(s, DAY1, { fillModel: "realistic", orderEvents: events, fills: o.fills });
const stopButton = buttons(s).find((b) => b.textContent.startsWith("Stop ")); // not the algo's own StopPrice
const read = () => s.markers.log.at(-1).filter((m) => m.position === "atPriceMiddle").map((m) => [m.time, m.price, m.shape, m.text, m.color]);
const on = read();
stopButton.fire("click");
const off = read();
console.log(JSON.stringify({ on, off, label: stopButton.textContent, lines: lines(s).length, t: barsOf(DAY1).map((b) => b.time) }));
""")
    assert isinstance(out, dict)
    assert len(out["on"]) == 1
    time_, price, shape, text, color = out["on"][0]
    assert time_ == seconds("2024-01-02 09:32:00") and price == 9 and shape == "circle" and text == "stop" and color
    assert out["off"] == []  # hidden with its legend item
    assert "none on this day" not in out["label"]


def test_overlay_colours_never_equal_the_entry_marker_or_the_candle_colours_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "overlay_palette", """
import { overlayColors } from "./js/charts/overlays.js";
import { FALLBACK_THEMES } from "./js/charts/theme.js";
const result = {};
for (const [name, theme] of Object.entries(FALLBACK_THEMES)) {
  const c = overlayColors(theme);
  result[name] = { theme, palette: c.palette, stop: c.stop, target: c.target, ownStop: c.ownStop, ownTarget: c.ownTarget };
}
console.log(JSON.stringify(result));
""")
    assert isinstance(out, dict)
    for name, got in out.items():
        theme = got["theme"]
        everything = [*got["palette"], got["stop"], got["target"], got["ownStop"], got["ownTarget"]]
        assert theme["accent"] not in everything, name  # the entry marker colour (candles.js buildMarkers)
        assert got["stop"] != theme["neg"] and got["target"] != theme["pos"], name  # nor the exit markers and candles
        assert theme["neg"] not in everything and theme["pos"] not in everything, name
        assert got["stop"] != got["target"], name
        assert len(set(got["palette"])) == len(got["palette"]), name


def test_a_legend_item_that_is_off_keeps_text_contrast_in_both_themes() -> None:
    import re

    from tests.test_api_static import contrast

    css = (STATIC / "app.css").read_text(encoding="utf-8")
    block = css[css.index("/* Indicator overlays"):]
    # no opacity anywhere in the overlay block: "off" is shown by the unpressed look, not by fading text
    assert "opacity" not in block
    off = re.search(r'\.overlay-toggle\[aria-pressed="false"\]\s*\{([^}]*)\}', block)
    assert off is not None and "background: var(--bg)" in off.group(1)
    assert "line-through" in block and "repeating-linear-gradient" in block  # struck-through name, striped swatch
    dark_start = css.index("prefers-color-scheme: dark")
    for theme, part in (("light", css[:dark_start]), ("dark", css[dark_start:css.index("* { box-sizing")])):
        values = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", part))
        # the button's own text (--text) and the muted text inside it (--muted), on the off background (--bg)
        for foreground in ("text", "muted"):
            assert contrast(values[foreground], values["bg"]) >= 4.5, (theme, foreground)


# --- the stop and target lines against the real stored runs (slow) ------------------------------

CROSSING_SCRIPT = """
import fs from "node:fs";
import { buildOrderLines } from "./js/charts/overlays.js";
import { convertBars } from "./js/charts/candles.js";
import { dataTimeToChartTime } from "./js/format.js";
const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const key = (account, ref) => account + "|" + ref;
function check(conv, events, fills) {
  const times = conv.map((b) => b.time);
  const indexOf = new Map(times.map((t, i) => [t, i]));
  const side = new Map(events.filter((e) => e.kind === "placed").map((e) => [key(e.account, e.order_id), e.side]));
  const fillBars = new Map();
  for (const f of fills) {
    const set = fillBars.get(key(f.account, f.broker_ref)) || new Set();
    set.add(dataTimeToChartTime(f.time));
    fillBars.set(key(f.account, f.broker_ref), set);
  }
  const result = { lines: 0, singlePoints: 0, bars: 0, crossings: [] };
  for (const line of buildOrderLines(events, fills, times)) {
    result.lines += 1;
    if (line.data.length === 1) result.singlePoints += 1;
    const long = side.get(key(line.account, line.orderId)) === "sell"; // the exit side of a long position
    const first = indexOf.get(line.data[0].time);
    const last = indexOf.get(line.data[line.data.length - 1].time);
    const fillsOfPosition = fillBars.get(key(line.account, line.brokerRef)) || new Set();
    let level = null, next = 0;
    for (let i = first; i < last; i += 1) { // every bar of the line but its end bar
      while (next < line.data.length && indexOf.get(line.data[next].time) <= i) { level = line.data[next].value; next += 1; }
      if (fillsOfPosition.has(times[i])) continue; // a fill happened on that bar
      const bar = conv[i];
      result.bars += 1;
      const crosses = line.role === "stop"
        ? (long ? bar.low <= level : bar.high >= level)
        : (long ? bar.high > level : bar.low < level);
      if (crosses) result.crossings.push([line.role, line.brokerRef, times[i], level, bar.low, bar.high]);
    }
  }
  return result;
}
const out = {};
for (const [name, set] of Object.entries(input)) {
  const conv = convertBars(set.bars);
  const whole = check(conv, set.events, set.fills);
  // the same, one day at a time as the algo view loads them (lines clipped to each day's bars)
  const days = new Map();
  for (const bar of conv) { const d = Math.floor(bar.time / 86400); (days.get(d) || days.set(d, []).get(d)).push(bar); }
  const perDay = { lines: 0, bars: 0, crossings: [] };
  for (const bars of days.values()) {
    const r = check(bars, set.events, set.fills);
    perDay.lines += r.lines; perDay.bars += r.bars; perDay.crossings.push(...r.crossings);
  }
  out[name] = { whole, perDay, totalBars: conv.length };
}
console.log(JSON.stringify(out));
"""


@pytest.mark.slow
def test_no_bar_inside_a_drawn_stop_or_target_line_crosses_its_level_with_node(
        tmp_path_factory: pytest.TempPathFactory, node_workdir: Path) -> None:
    """Real stored runs (EMACrossing and OpeningRangeCandle on AAPL, Elliot on MSFT, realistic fills): on
    every bar of a drawn line but its end bar, with no fill on it, the bar's range must not reach the level
    drawn on that bar. A line drawn from the decision bar (the old timing) fails this for Elliot."""
    import json
    from datetime import date, timedelta

    from MainStoinker.engine.cli import main
    from tests.test_cli_modes import EMA, ORC, Env
    from tests.test_cli_store import ELLIOT_MSFT

    root = tmp_path_factory.mktemp("overlay-crossings")
    env = Env(root)
    env.setup([{"ID": "EMACrossing", "data": [EMA]}, {"ID": "OpeningRangeCandle", "data": [ORC]},
               {"ID": "Elliot", "data": [ELLIOT_MSFT]}], ["AAPL", "MSFT"], rows=None)
    database = root / "runs.db"
    assert main(["backtest", "--algos", str(env.algos), "--data", str(env.data), "--out", str(env.out),
                 "--quiet", "--fills", "realistic", "--db", str(database)]) == 0
    client = make_client(database)
    run = client.get("/api/runs/1").json()["run"]
    first = date.fromisoformat(run["first_bar"][:10])
    last = date.fromisoformat(run["last_bar"][:10])
    gathered: dict[str, dict[str, object]] = {}
    for name, symbol in (("AAPL-10/30EMA", "AAPL"), ("AAPL-orc", "AAPL"), ("MSFT-elliot", "MSFT")):
        bars: list[dict] = []
        day = first - timedelta(days=7)  # the run's bars start after the warm-up days: start early
        while day <= last:
            window = {"symbol": symbol, "start": f"{day} 00:00:00", "end": f"{day + timedelta(days=6)} 23:59:59"}
            body = client.get("/api/runs/1/bars", params=window).json()
            assert body["available"] is True
            bars.extend(body["bars"])
            day += timedelta(days=7)
        orders = client.get("/api/runs/1/orders", params={"algo": name}).json()
        assert orders["order_events"] and orders["fills"], name
        gathered[name] = {"bars": bars, "events": orders["order_events"], "fills": orders["fills"]}
    data_file = node_workdir / "crossings_input.json"
    data_file.write_text(json.dumps(gathered), encoding="utf-8")
    script = node_workdir / "crossings.mjs"
    script.write_text(CROSSING_SCRIPT, encoding="utf-8")
    import subprocess

    from tests.test_api_static import NODE

    assert NODE is not None
    done = subprocess.run([NODE, str(script), str(data_file)], capture_output=True, encoding="utf-8",
                          timeout=300, cwd=node_workdir)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    print("overlay crossing check (bars inside drawn lines, crossings, total bars):")
    for name, result in out.items():
        whole, per_day = result["whole"], result["perDay"]
        print(f"  {name}: whole range {len(whole['crossings'])} of {whole['bars']} bars in {whole['lines']} lines "
              f"({whole['singlePoints']} one-point); per day {len(per_day['crossings'])} of {per_day['bars']}; "
              f"{result['totalBars']} bars loaded")
    for name, result in out.items():
        assert result["whole"]["lines"] > 0 and result["whole"]["bars"] > 0, name
        assert result["whole"]["crossings"] == [], name
        assert result["perDay"]["crossings"] == [], name

