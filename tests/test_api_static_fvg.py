# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""The FVG zones control and drawing of the price chart (issue 276), exercised with node.

The pure parts run as they are; the control runs against a fake candle-chart handle (no library, no DOM
beyond the fake of `tests/test_api_static.py`); the wiring in the algo view runs with the fake chart library
of `tests/test_api_static_algo.py` extended with series primitives.
"""

from __future__ import annotations

import re
import shutil
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
from tests.test_api_static_algo import VIEW_SOURCE

FVG_JS = STATIC / "js" / "charts" / "fvg.js"


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from MainStoinker.engine.store import SqliteStore

    path = tmp_path_factory.mktemp("static_fvg") / "empty.db"
    with SqliteStore(path, env={}):
        pass
    return path


@pytest.fixture(scope="module")
def node_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    require_node()
    work = tmp_path_factory.mktemp("node_fvg")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


# --- the files -----------------------------------------------------------------------------------


def test_fvg_module_is_served_and_imported_by_the_algo_view(empty_db: Path) -> None:
    client = make_client(empty_db)
    seen = {served_path(STATIC / "js" / "views" / "algo.js", target)
            for _kind, target in page_references(STATIC / "js" / "views" / "algo.js")}
    assert "/js/charts/fvg.js" in seen
    response = client.get("/js/charts/fvg.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    for _kind, target in page_references(FVG_JS):
        assert served_path(FVG_JS, target).startswith("/js/")  # nothing external, nothing outside the dashboard


def test_fvg_module_has_no_html_injection_external_url_or_colour_literal() -> None:
    text = FVG_JS.read_text(encoding="utf-8")
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert word not in text
    assert not re.search(r"https?://|//cdn\.", text)
    assert not re.search(r"""["']#[0-9a-fA-F]{3,6}["']""", text)  # colours come from the page theme
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "fvg" not in page  # no new script tag: the module is imported by the algo view


# --- the pure parts ------------------------------------------------------------------------------

PURE = FAKE_DOM + """
const fvg = await import("./js/charts/fvg.js");
const { dataTimeToChartTime } = await import("./js/format.js");
const T0 = dataTimeToChartTime("2024-01-02 09:30:00");
const at = (m) => T0 + m * 60;
const text = (m) => "2024-01-02 09:" + String(30 + m).padStart(2, "0") + ":00";
const times = Array.from({ length: 20 }, (_, i) => at(i));
const zone = (id, extra = {}) => ({ id, direction: "up", top: 101.5, bottom: 101.0, midpoint: 101.25, size: 0.5,
  size_atr: 1.25, formed_time: text(4), known_at: text(5), state: "open", touches: 0, deepest_fill: 0,
  last_change_time: text(4), last_change_known_at: text(5), end_time: null, ...extra });
"""


def test_zone_spans_start_at_known_at_end_at_end_time_and_leave_out_what_is_not_there(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_spans", PURE + """
const m = (s) => (s.start - T0) / 60;
const e = (s) => (s.end === null ? null : (s.end - T0) / 60);
const spans = fvg.zoneSpans([
  zone(1),                                        // live: from known_at (minute 5, not formed_time 4) to the right edge
  zone(2, { end_time: text(9), state: "filled" }),
  zone(3, { known_at: text(19) }),                // known at the last candle
  zone(4, { known_at: text(20) }),                // known only after the last candle: nothing to draw
  zone(5, { known_at: text(1), end_time: "2024-01-02 09:00:00", state: "expired" }), // over before the first candle
  zone(6, { end_time: text(25), state: "filled" }), // ends after the last candle: to the right edge
  zone(7, { known_at: "garbage" }), zone(8, { top: "x" }), null,
  zone(9, { known_at: "2024-01-02 09:30:30", end_time: text(5), state: "filled" }), // between two candles: the next one
], times);
const ids = spans.map((s) => s.zone.id);
const inverted = fvg.zoneSpans([zone(1, { top: 100, bottom: 101 })], times)[0];
console.log(JSON.stringify({ ids, starts: spans.map(m), ends: spans.map(e), inverted: [inverted.top, inverted.bottom],
  none: fvg.zoneSpans([zone(1)], []), notList: fvg.zoneSpans(null, times) }));
""")
    assert isinstance(out, dict)
    assert out["ids"] == [1, 2, 3, 6, 9]
    assert out["starts"] == [5, 5, 19, 5, 1]
    assert out["ends"] == [None, 9, None, None, 5]
    assert out["inverted"] == [101, 100]
    assert out["none"] == [] and out["notList"] == []


def test_hit_zone_picks_the_newest_zone_containing_the_point(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_hit", PURE + """
const spans = fvg.zoneSpans([zone(1), zone(2, { top: 101.2, bottom: 100.9, known_at: text(7), end_time: text(12), state: "filled" })], times);
const id = (s) => (s ? s.zone.id : null);
console.log(JSON.stringify({
  both: id(fvg.hitZone(spans, at(8), 101.1)), oneOnly: id(fvg.hitZone(spans, at(8), 101.4)),
  afterEnd: id(fvg.hitZone(spans, at(13), 101.1)), beforeStart: id(fvg.hitZone(spans, at(4), 101.1)),
  above: id(fvg.hitZone(spans, at(8), 102)), edge: id(fvg.hitZone(spans, at(5), 101.0)),
  junk: [fvg.hitZone(spans, null, 101), fvg.hitZone(spans, at(8), NaN), fvg.hitZone(null, at(8), 101)].map(id),
}));
""")
    assert out == {"both": 2, "oneOnly": 1, "afterEnd": 1, "beforeStart": None, "above": None, "edge": 1,
                   "junk": [None, None, None]}


def test_up_and_down_zones_differ_by_more_than_colour_and_over_zones_are_fainter(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_look", PURE + """
const look = (extra) => fvg.zoneLook(zone(1, extra));
const up = look({}), down = look({ direction: "down" });
const filled = look({ state: "filled" }), touched = look({ state: "touched" });
console.log(JSON.stringify({ up, down, filled, touched, invalidated: look({ state: "invalidated", direction: "down" }), expired: look({ state: "expired" }) }));
""")
    assert isinstance(out, dict)
    up, down = out["up"], out["down"]
    assert up["dash"] != down["dash"] and up["label"] != down["label"]  # a border style and a label, not only a hue
    assert up["label"].startswith("▲") and down["label"].startswith("▼")
    assert out["touched"] == up  # touched is as strong as open
    for key in ("filled", "invalidated", "expired"):
        over = out[key]
        assert over["alpha"] < up["alpha"] and over["borderAlpha"] < up["borderAlpha"], key
        assert over["live"] is False and key in over["label"]
    assert out["filled"]["dash"] != up["dash"]


def test_describe_zone_names_every_field(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_describe", PURE + """
console.log(JSON.stringify({
  live: fvg.describeZone(zone(7, { touches: 2, deepest_fill: 0.456, state: "touched" })),
  over: fvg.describeZone(zone(8, { direction: "down", state: "filled", touches: 1, size_atr: null, end_time: text(9) })),
  none: fvg.describeZone(null),
}));
""")
    assert isinstance(out, dict)
    live = out["live"]
    for part in ("Up zone", "101.00 to 101.50", "top 101.50", "bottom 101.00", "size 0.50", "1.25 ATR", "State touched",
                 "2 touches", "deepest fill 46%", "Formed 2024-01-02 09:34", "known from 2024-01-02 09:35", "still live"):
        assert part in live, part
    assert "Down zone" in out["over"] and "1 touch," in out["over"] and "ATR" not in out["over"]
    assert "over at 2024-01-02 09:39" in out["over"]
    assert out["none"] == ""


def test_the_choice_is_kept_in_storage_and_the_page_works_without_it(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_storage", PURE + """
const store = new Map();
const view = { localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) } };
const broken = { localStorage: { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } } };
const first = fvg.readChoice(view);
fvg.saveChoice(view, { on: true, minutes: 15 });
const kept = fvg.readChoice(view);
store.set(fvg.STORAGE_KEY, "not json");
const bad = fvg.readChoice(view);
store.set(fvg.STORAGE_KEY, JSON.stringify({ on: "yes", minutes: 7 }));
const odd = fvg.readChoice(view);
let threw = false;
try { fvg.saveChoice(broken, { on: true, minutes: 5 }); } catch (err) { threw = true; }
console.log(JSON.stringify({ first, kept, bad, odd, brokenRead: fvg.readChoice(broken), none: fvg.readChoice({}), threw }));
""")
    assert out == {"first": {"on": False, "minutes": 1}, "kept": {"on": True, "minutes": 15},
                   "bad": {"on": False, "minutes": 1}, "odd": {"on": False, "minutes": 1},
                   "brokenRead": {"on": False, "minutes": 1}, "none": {"on": False, "minutes": 1}, "threw": False}


# --- the control, drawing and requests ---------------------------------------------------------

CONTROL = PURE + """
const { createNavigator } = await import("./js/lifecycle.js");
const tick = () => new Promise((r) => setTimeout(r, 0));
const find = (el, test, acc = []) => { for (const c of el.children) if (c instanceof FakeEl) { if (test(c)) acc.push(c); find(c, test, acc); } return acc; };
const byClass = (el, name) => find(el, (c) => c.className.split(" ").includes(name))[0];
async function makeCtx() {
  const navigator = createNavigator();
  let ctx = null;
  await navigator.run(async (_el, _params, c) => { ctx = c; }, {}, { mount: () => null });
  return { ctx, navigator };
}
function fakeHandle() {
  const h = { primitives: [], moves: [], clicks: [], updates: 0, detached: 0, barFns: [], themeFns: [], bars: [] };
  const series = {
    attachPrimitive(p) { h.primitives.push(p); p.attached({ chart, series, requestUpdate() { h.updates += 1; } }); },
    detachPrimitive(p) { h.detached += 1; p.detached(); },
    coordinateToPrice: (y) => 500 - y / 40,         // inverse of priceToCoordinate below
    priceToCoordinate: (p) => (500 - p) * 40,
  };
  const chart = {
    subscribeCrosshairMove: (f) => h.moves.push(f), unsubscribeCrosshairMove: (f) => h.moves.splice(h.moves.indexOf(f), 1),
    subscribeClick: (f) => h.clicks.push(f), unsubscribeClick: (f) => h.clicks.splice(h.clicks.indexOf(f), 1),
    timeScale: () => ({ timeToCoordinate: (t) => (t - T0) / 60 * 10 }),
  };
  Object.assign(h, { chart, series,
    onBars(f) { h.barFns.push(f); return () => h.barFns.splice(h.barFns.indexOf(f), 1); },
    onTheme(f) { h.themeFns.push(f); return () => h.themeFns.splice(h.themeFns.indexOf(f), 1); },
    setBars(n) { h.bars = times.slice(0, n).map((time) => ({ time })); h.barFns.slice().forEach((f) => f(h.bars)); } });
  return h;
}
function fetcher() {
  const calls = [];
  const fn = (query, signal) => new Promise((resolve, reject) => {
    const call = { query, signal, aborted: false, resolve, reject };
    signal.addEventListener("abort", () => { call.aborted = true; reject(Object.assign(new Error("aborted"), { name: "AbortError" })); });
    calls.push(call);
  });
  return { calls, fn };
}
const answer = (zones, extra = {}) => ({ symbol: "AAPL", available: true, reason: null, bar_minutes: 1, truncated: false, zones, ...extra });
const storage = (initial) => {
  const store = new Map(initial ? [[fvg.STORAGE_KEY, JSON.stringify(initial)]] : []);
  return { store, view: { localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) } } };
};
const toggle = (box, on) => { const c = byClass(box, "fvg-on"); c.checked = on; c.fire("change"); };
const pick = (box, minutes) => { const s = byClass(box, "fvg-timeframe"); s.value = String(minutes); s.fire("change"); };
const WINDOW = { symbol: "AAPL", start: "2024-01-02 00:00:00", end: "2024-01-02 23:59:59" };
async function make(initial) {
  const { ctx } = await makeCtx();
  const handle = fakeHandle();
  const { calls, fn } = fetcher();
  const box = document.createElement("div");
  const st = storage(initial);
  const zones = fvg.createFvgZones(handle, { ctx, container: box, fetchZones: fn, view: st.view });
  handle.setBars(20);
  return { ctx, handle, calls, box, st, zones };
}
function paint(s) {
  const log = [];
  const g = { save() {}, restore() {}, setLineDash: (d) => log.push(["dash", d]), fillRect: (...a) => log.push(["fill", ...a]),
    strokeRect: (...a) => log.push(["stroke", ...a]), fillText: (...a) => log.push(["text", ...a]) };
  const target = { useBitmapCoordinateSpace: (f) => f({ context: g, horizontalPixelRatio: 1, verticalPixelRatio: 1, bitmapSize: { width: 1000, height: 500 } }) };
  const view = s.handle.primitives[0].paneViews()[0];
  view.renderer().draw(target);
  return { log, zOrder: view.zOrder() };
}
"""


def test_off_by_default_makes_no_request_and_draws_nothing(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_off", CONTROL + """
const s = await make();
s.zones.update(WINDOW);
await tick();
s.handle.setBars(10);
s.zones.update(WINDOW);
await tick();
const box = byClass(s.box, "fvg-control");
console.log(JSON.stringify({ calls: s.calls.length, enabled: s.zones.enabled(), checked: byClass(s.box, "fvg-on").checked,
  timeframe: byClass(s.box, "fvg-timeframe").value, options: byClass(s.box, "fvg-timeframe").children.map((o) => o.textContent),
  painted: paint(s).log.length, attached: s.handle.primitives.length, label: box.textContent.includes("FVG zones"),
  stored: [...s.st.store.keys()] }));
""")
    assert out == {"calls": 0, "enabled": False, "checked": False, "timeframe": "1",
                   "options": ["1 min", "5 min", "15 min"], "painted": 0, "attached": 1, "label": True, "stored": []}


def test_turning_it_on_requests_the_shown_window_and_the_zones_are_drawn_from_known_at(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_on", CONTROL + """
const s = await make();
s.zones.update(WINDOW);
toggle(s.box, true);
await tick();
const asked = s.calls.map((c) => c.query);
s.calls[0].resolve(answer([
  zone(1, { top: 101.5, bottom: 101.0, known_at: text(5), formed_time: text(4) }),
  zone(2, { direction: "down", top: 99.5, bottom: 99.0, known_at: text(8), end_time: text(12), state: "invalidated" }),
]));
await tick();
const drawn = paint(s);
const status = byClass(s.box, "fvg-status").textContent;
console.log(JSON.stringify({ asked, drawn, status, stored: JSON.parse(s.st.store.get(fvg.STORAGE_KEY)) }));
""")
    assert isinstance(out, dict)
    assert out["asked"] == [{"symbol": "AAPL", "start": "2024-01-02 00:00:00", "end": "2024-01-02 23:59:59", "bar_minutes": 1}]
    log = out["drawn"]["log"]
    fills = [entry[1:] for entry in log if entry[0] == "fill"]
    # price y = (500 - p) * 40; time x = minute * 10: the up zone from minute 5 (known_at, not minute 4) to the right edge,
    # the down zone from minute 8 to minute 12
    assert fills == [[50, 15940, 950, 20], [80, 16020, 40, 20]]  # [x, y, width, height] in pixels
    assert out["drawn"]["zOrder"] == "bottom"
    assert [entry[1] for entry in log if entry[0] == "dash"] == [[], [], [2, 3], []]  # outline then label of each: solid for a live up zone, dotted for an invalidated one
    assert [entry[1] for entry in log if entry[0] == "text"] == ["▲ up", "▼ invalidated"]
    assert out["status"] == ""
    assert out["stored"] == {"on": True, "minutes": 1}


def test_timeframe_and_window_changes_refetch_and_the_older_request_is_aborted(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_refetch", CONTROL + """
const s = await make({ on: true, minutes: 5 });
s.zones.update(WINDOW);                       // on from storage: asks at once, for 5 minutes
await tick();
pick(s.box, 15);                              // the 5 minute request is aborted
await tick();
const next = { ...WINDOW, start: "2024-01-03 00:00:00", end: "2024-01-03 23:59:59" };
s.zones.update(next);                         // another day: the 15 minute request is aborted
await tick();
s.calls[2].resolve(answer([zone(1)]));
await tick();
s.calls[0].resolve(answer([zone(99)]));       // a late answer of the first request changes nothing (it was aborted)
await tick();
const painted = paint(s).log.filter((e) => e[0] === "fill").length;
console.log(JSON.stringify({ queries: s.calls.map((c) => [c.query.bar_minutes, c.query.start.slice(0, 10)]),
  aborted: s.calls.map((c) => c.aborted), painted, stored: JSON.parse(s.st.store.get(fvg.STORAGE_KEY)),
  selected: byClass(s.box, "fvg-timeframe").value }));
""")
    assert out == {"queries": [[5, "2024-01-02"], [15, "2024-01-02"], [15, "2024-01-03"]],
                   "aborted": [True, True, False], "painted": 1, "stored": {"on": True, "minutes": 15},
                   "selected": "15"}


def test_turning_it_off_clears_the_zones_aborts_the_request_and_asks_nothing_more(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_toggle_off", CONTROL + """
const s = await make({ on: true, minutes: 1 });
s.zones.update(WINDOW);
await tick();
s.calls[0].resolve(answer([zone(1)]));
await tick();
const before = paint(s).log.filter((e) => e[0] === "fill").length;
toggle(s.box, false);
await tick();
const after = paint(s).log.filter((e) => e[0] === "fill").length;
s.zones.update({ ...WINDOW, start: "2024-01-03 00:00:00", end: "2024-01-03 23:59:59" });   // a new day with it off: no request
s.handle.setBars(5);
await tick();
toggle(s.box, true);                         // back on: asks for the window on show
await tick();
console.log(JSON.stringify({ before, after, calls: s.calls.length, last: s.calls.at(-1).query.start.slice(0, 10), stored: JSON.parse(s.st.store.get(fvg.STORAGE_KEY)) }));
""")
    assert out == {"before": 1, "after": 0, "calls": 2, "last": "2024-01-03", "stored": {"on": True, "minutes": 1}}


def test_a_refresh_of_the_same_window_keeps_the_zones_and_new_bars_never_draw_stale_ones(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_refresh", CONTROL + """
const s = await make({ on: true, minutes: 1 });
s.zones.update(WINDOW);
await tick();
s.calls[0].resolve(answer([zone(1)]));
await tick();
s.zones.update(WINDOW);                       // a live refresh: same window, the zones stay while it loads
await tick();
const during = paint(s).log.filter((e) => e[0] === "fill").length;
s.calls[1].resolve(answer([zone(1), zone(2, { known_at: text(9) })]));
await tick();
const refreshed = paint(s).log.filter((e) => e[0] === "fill").length;
s.zones.update({ ...WINDOW, end: "2024-01-02 12:00:00" }); // another window: the old zones go at once
const other = paint(s).log.filter((e) => e[0] === "fill").length;
await tick();
s.zones.clear();                              // the bars are gone
const cleared = paint(s).log.filter((e) => e[0] === "fill").length;
console.log(JSON.stringify({ during, refreshed, other, cleared, calls: s.calls.length }));
""")
    assert out == {"during": 1, "refreshed": 2, "other": 0, "cleared": 0, "calls": 3}


def test_failures_and_notes_are_said_and_nothing_is_drawn(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_notes", CONTROL + """
const s = await make({ on: true, minutes: 1 });
const status = () => byClass(s.box, "fvg-status").textContent;
const run = async (settle) => { s.zones.update(WINDOW); await tick(); settle(s.calls.at(-1)); await tick(); return status(); };
const notes = {};
notes.failed = await run((c) => c.reject(Object.assign(new Error("x"), { name: "ApiError", status: 500, detail: "boom", kind: "http" })));
notes.empty = await run((c) => c.resolve(answer([])));
notes.unavailable = await run((c) => c.resolve(answer([], { available: false, reason: "changed" })));
notes.nonCent = await run((c) => c.resolve(answer([], { reason: "non_cent_price" })));
notes.truncated = await run((c) => c.resolve(answer([zone(1)], { truncated: true })));
notes.garbage = await run((c) => c.resolve({ nonsense: true }));
console.log(JSON.stringify({ notes, painted: paint(s).log.length }));
""")
    assert isinstance(out, dict)
    notes = out["notes"]
    assert "could not be loaded" in notes["failed"]
    assert notes["empty"] == "No zones in this window."
    assert "bars cannot be shown" in notes["unavailable"]
    assert "not whole cents" in notes["nonCent"]
    assert "only the newest" in notes["truncated"]
    assert "could not be loaded" in notes["garbage"]
    assert out["painted"] == 0  # the last answer was garbage: nothing is drawn


def test_hover_and_click_show_the_zone_under_the_pointer(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_hover", CONTROL + """
const s = await make({ on: true, minutes: 1 });
s.zones.update(WINDOW);
await tick();
s.calls[0].resolve(answer([zone(3, { touches: 1, state: "touched" })]));
await tick();
const detail = () => byClass(s.box, "fvg-detail");
const point = (minute, price) => ({ time: at(minute), point: { x: minute * 10, y: (500 - price) * 40 } });
const out = {};
out.start = { hidden: detail().hidden };
s.handle.moves[0](point(8, 101.2));
out.over = { hidden: detail().hidden, text: detail().textContent };
s.handle.moves[0](point(8, 105));            // above the zone
out.off = { hidden: detail().hidden };
s.handle.moves[0](point(3, 101.2));          // before it is known
out.before = { hidden: detail().hidden };
s.handle.clicks[0](point(8, 101.2));         // a click pins it
s.handle.moves[0](point(8, 105));
out.pinned = { hidden: detail().hidden, text: detail().textContent.slice(0, 20) };
s.handle.clicks[0](point(8, 105));           // a click elsewhere lets go
out.released = { hidden: detail().hidden };
s.handle.moves[0]({});                       // no point at all
s.handle.moves[0]({ time: at(8) });
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["start"]["hidden"] is True
    assert out["over"]["hidden"] is False and out["over"]["text"].startswith("Up zone 101.00 to 101.50")
    assert "State touched" in out["over"]["text"]
    assert out["off"]["hidden"] is True and out["before"]["hidden"] is True
    assert out["pinned"] == {"hidden": False, "text": "Up zone 101.00 to 10"}
    assert out["released"]["hidden"] is True


def test_dispose_removes_everything_and_later_calls_do_nothing(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_dispose", CONTROL + """
const s = await make({ on: true, minutes: 1 });
s.zones.update(WINDOW);
await tick();
const aborted = s.calls[0].aborted;
s.zones.dispose();
s.zones.dispose();
s.zones.update(WINDOW);
s.zones.clear();
await tick();
console.log(JSON.stringify({ aborted: s.calls[0].aborted, calls: s.calls.length, detached: s.handle.detached,
  moves: s.handle.moves.length, clicks: s.handle.clicks.length, bars: s.handle.barFns.length, themes: s.handle.themeFns.length }));
""")
    assert out == {"aborted": True, "calls": 1, "detached": 1, "moves": 0, "clicks": 0, "bars": 0, "themes": 0}


def test_a_library_without_primitives_gets_no_control(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_inert", CONTROL + """
const { ctx } = await makeCtx();
const box = document.createElement("div");
const handle = { series: {}, chart: {}, onBars: () => () => {}, onTheme: () => () => {} };
const zones = fvg.createFvgZones(handle, { ctx, container: box, fetchZones: () => Promise.resolve({}), view: {} });
zones.update(WINDOW); zones.clear(); zones.dispose();
console.log(JSON.stringify({ children: box.children.length, enabled: zones.enabled() }));
""")
    assert out == {"children": 0, "enabled": False}


# --- wired into the algo view ----------------------------------------------------------------------

WITH_PRIMITIVES = """
const primitives = [];
{
  const make = globalThis.LightweightCharts.createChart;
  globalThis.LightweightCharts.createChart = (el, options) => {
    const chart = make(el, options);
    const add = chart.addSeries;
    chart.addSeries = (type, opts, pane) => {
      const series = add(type, opts, pane);
      series.attachPrimitive = (p) => { primitives.push(p); p.attached({ chart, series, requestUpdate() {} }); };
      series.detachPrimitive = (p) => p.detached();
      series.coordinateToPrice = (y) => y;
      series.priceToCoordinate = (p) => p;
      return series;
    };
    chart.subscribeCrosshairMove = () => {}; chart.unsubscribeCrosshairMove = () => {};
    chart.subscribeClick = () => {}; chart.unsubscribeClick = () => {};
    return chart;
  };
}
const fvgRequests = () => requests.filter((r) => r.path.endsWith("/fvg"));
const setStorage = (initial) => {
  const store = new Map(initial ? [["stoinkulator.fvg.v1", JSON.stringify(initial)]] : []);
  globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
  return store;
};
"""


def test_the_algo_view_requests_zones_only_while_the_control_is_on_and_for_the_day_shown(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_view", VIEW_SOURCE + WITH_PRIMITIVES + """
const store = setStorage(null);
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": ok({ available: true, series: [] }) });
const { container } = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await tick();
const out = { box: byClass(container, "fvg-control").length, attached: primitives.length };
out.offRequests = fvgRequests().length;                    // off by default: none, though the bars are drawn
const checkbox = byClass(container, "fvg-on")[0];
checkbox.checked = true; checkbox.fire("change");
await tick();
out.onRequests = fvgRequests().map((r) => r.query);
fvgRequests()[0].answer({ symbol: "AAPL", available: true, reason: null, bar_minutes: 1, truncated: false, zones: [] });
await tick();
const select = byClass(container, "fvg-timeframe")[0];
select.value = "5"; select.fire("change");
await tick();
out.five = fvgRequests().at(-1).query;
out.stored = JSON.parse(store.get("stoinkulator.fvg.v1"));
// selecting a trade of the other day shows that day: asks again for that day
rowButtons(container)[1].fire("click");
await tick();
barsRequests().at(-1).answer(barsFor("2024-01-03"));
await tick();
out.afterStep = fvgRequests().at(-1).query;
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["box"] == 1 and out["attached"] == 1 and out["offRequests"] == 0
    assert out["onRequests"] == [{"symbol": "AAPL", "start": "2024-01-02 00:00:00", "end": "2024-01-02 23:59:59",
                                  "bar_minutes": "1"}]
    assert out["five"]["bar_minutes"] == "5" and out["stored"] == {"on": True, "minutes": 5}
    assert out["afterStep"]["start"].startswith("2024-01-03") and out["afterStep"]["bar_minutes"] == "5"


def test_the_algo_view_with_the_control_off_requests_no_zones_through_days_and_refreshes(node_workdir: Path) -> None:
    out = run_node(node_workdir, "fvg_view_off", VIEW_SOURCE + WITH_PRIMITIVES + """
setStorage(null);
install({ "/api/runs/1": ok(doc()), "/api/runs/1/trades": ok({ trades: TRADES }), "/api/runs/1/equity": ok(equity),
  "/api/runs/1/series": ok({ available: true, series: [] }) });
const { container } = await open();
barsRequests()[0].answer(barsFor("2024-01-02"));
await tick();
rowButtons(container)[1].fire("click");
await tick();
barsRequests().at(-1).answer(barsFor("2024-01-03"));
await tick();
console.log(JSON.stringify({ fvg: fvgRequests().length, bars: barsRequests().length }));
""")
    assert isinstance(out, dict)
    assert out["fvg"] == 0 and out["bars"] >= 2
