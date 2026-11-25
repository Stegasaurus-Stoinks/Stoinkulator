# ruff: noqa: E501  (the embedded JavaScript is written for reading, not wrapped to the Python limit)
"""The dashboard for a run in progress (stage 2, T10), exercised with node.

Same style as the other static tests: the real modules run under node against the fake DOM and the fake chart
library of `tests/test_api_static.py` / `tests/test_api_static_algo.py`, with a fake `fetch` and fake timers.
Fixture documents are written here, in the shape the API of PR 215 serves them: a live run carries `liveness`,
`end_reason` and `last_write_at`; its detail document `open_positions` and `live_counts`. A backtest run carries
none of these, and its pages must be drawn exactly as before (`BACKTEST_TREES`, recorded from the code of
`origin/revival` before this change).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.test_api_static import FAKE_DOM, JS_ROOT, STATIC, page_references, require_node, run_node
from tests.test_api_static_algo import FAKE_LIB


@pytest.fixture(scope="module")
def node_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of the dashboard modules that node runs as ES modules (no DOM, no network)."""
    require_node()
    work = tmp_path_factory.mktemp("node_live")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


# The chart library of the algo tests, plus the two time-scale methods the live refresh needs.
LIB = FAKE_LIB + """
const createChartPlain = LightweightCharts.createChart;
let visibleLogical = { from: 2, to: 9 };
LightweightCharts.createChart = (el, options) => {
  const chart = createChartPlain(el, options);
  const scale = chart.timeScale;
  chart.logical = [];
  chart.timeScale = () => ({ ...scale(), getVisibleLogicalRange: () => visibleLogical,
    setVisibleLogicalRange: (range) => chart.logical.push(range) });
  return chart;
};
"""

# Fake timers, window, fetch and the navigator every scenario uses.
HARNESS = FAKE_DOM + LIB + """
const timers = [];
globalThis.setInterval = (fn, ms) => { timers.push({ fn, ms, cleared: false }); return timers.length; };
globalThis.clearInterval = (id) => { timers[id - 1].cleared = true; };
const activeTimers = () => timers.filter((t) => !t.cleared);
const dispatched = [];
globalThis.window = { dispatchEvent: (e) => dispatched.push(e.type), location: { hash: "" } };
const requests = [];
const routes = {};
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  const u = new URL(url, "http://x");
  requests.push({ path: u.pathname, query: Object.fromEntries(u.searchParams) });
  init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  const handler = routes[u.pathname];
  if (!handler) { resolve({ ok: false, status: 404, statusText: "nf", json: async () => ({ detail: "no route" }) }); return; }
  const [body, status] = handler();
  resolve({ ok: status < 400, status, statusText: "status", json: async () => body });
});
const count = (path) => requests.filter((r) => r.path === path).length;
const ok = (body) => () => [body, 200];
const boom = () => [{ detail: "boom" }, 500];
const { createNavigator } = await import("./js/lifecycle.js");
const navigator = createNavigator();
const tick = () => new Promise((r) => setTimeout(r, 0));
async function open(render, params) {
  const container = document.createElement("div");
  await navigator.run(render, params, { mount: () => container });
  await tick();
  return container;
}
const leave = () => navigator.run(async () => {}, {}, { mount: () => document.createElement("div") });
const texts = (nodes) => nodes.map((n) => n.textContent);
const rowTexts = (table) => table.all("tbody")[0].children.map((tr) => texts(tr.children));
const sectionOf = (container, label) => find(container, (c) => c.tag === "SECTION" && c.getAttribute("aria-label") === label)[0];
const refreshNotes = (container) => byClass(container, "refresh-note").length;
const errorPanels = (container) => find(container, (c) => c.className.split(" ").includes("error")).length;
"""


# --- the runs list ------------------------------------------------------------------------------

RUNS_SOURCE = HARNESS + """
const { render } = await import("./js/views/runs.js");
const live = (run_id, liveness, extra = {}) => ({ run_id, started_at: "2026-11-25T14:30:00+00:00", ended_at: null,
  duration_seconds: null, status: liveness === "running" || liveness === "stale" ? "running" : liveness,
  incomplete: liveness === "running" || liveness === "stale", kind: "paper-live", fills: "realistic",
  code_version: "abcdef0123456789", symbols: ["AAPL"], algo_count: 1, trade_count: 0, net: "0.0000", liveness,
  end_reason: null, last_write_at: "2026-11-25T14:35:00+00:00", ...extra });
const backtest = (run_id, extra = {}) => ({ run_id, started_at: "2026-11-20T10:00:00+00:00", ended_at: "2026-11-20T10:00:05+00:00",
  duration_seconds: 5, status: "completed", incomplete: false, kind: "backtest", fills: "realistic",
  code_version: "abcdef0123456789", symbols: ["AAPL"], algo_count: 1, trade_count: 2, net: "12.5000", ...extra });
let served = [live(9, "running"), live(8, "stale"), live(7, "failed", { end_reason: "connection closed" }), live(6, "completed", { end_reason: "session end" }), backtest(4)];
routes["/api/runs"] = () => [{ runs: served }, 200];
const badgeTexts = (container) => byClass(container, "badge").map((b) => b.textContent);
const ids = (container) => container.all("table")[0].all("tbody")[0].children.map((tr) => tr.children[1].textContent);
"""


def test_runs_list_badges_order_and_the_refresh_timer(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_runs", RUNS_SOURCE + """
const container = await open(render, {});
const out = { badges: badgeTexts(container), ids: ids(container), timers: activeTimers().map((t) => t.ms) };
const badges = byClass(container, "badge");
out.titles = badges.map((b) => b.getAttribute("title"));
out.classes = badges.map((b) => b.className);
// a tick reloads the list without a "Loading..." panel and keeps the table element
const table = container.all("table")[0];
out.beforeTick = count("/api/runs");
const pending = activeTimers()[0].fn();
out.duringTick = { table: container.all("table")[0] === table, loading: container.all("p").filter((p) => p.textContent.startsWith("Loading")).length };
await pending;
out.afterTick = { requests: count("/api/runs"), table: container.all("table")[0] === table, health: dispatched.length };
// the run ends: the next tick shows it and the timer stops
served = [live(9, "completed", { end_reason: "stop file" }), live(8, "stale"), backtest(4)];
await activeTimers()[0].fn();
out.ended = { badges: badgeTexts(container), timers: activeTimers().length, cleared: timers[0].cleared };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["badges"] == ["running", "stale", "failed", "completed", "completed"]  # the backtest's badge is its status
    assert out["ids"] == ["9", "8", "7", "6", "4"]  # the served order: running, stale, then the rest
    assert out["timers"] == [30000]
    assert out["titles"] == [None, None, "End reason: connection closed", "End reason: session end", None]
    assert out["classes"][:4] == ["badge live running", "badge live stale", "badge live failed", "badge live completed"]
    assert out["beforeTick"] == 1
    assert out["duringTick"] == {"table": True, "loading": 0}
    assert out["afterTick"] == {"requests": 2, "table": True, "health": 1}
    assert out["ended"] == {"badges": ["completed", "stale", "completed"], "timers": 0, "cleared": True}


def test_runs_list_timer_is_cleared_when_the_view_is_left_and_never_set_without_a_running_run(
        node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_runs_leave", RUNS_SOURCE + """
const out = {};
await open(render, {});
out.running = activeTimers().length;
await leave();
out.afterLeave = activeTimers().length;
// leaving makes a tick that was already queued do nothing
const queued = timers[0].fn;
const before = count("/api/runs");
await queued();
out.queuedTick = count("/api/runs") - before;
// no run is running: no timer at all (a stale run does not count)
served = [live(8, "stale"), live(7, "failed", { end_reason: "x" }), backtest(4)];
const before2 = timers.length;
await open(render, {});
out.noneRunning = timers.length - before2;
// only backtests
served = [backtest(4), backtest(3)];
await open(render, {});
out.backtestsOnly = timers.length - before2;
console.log(JSON.stringify(out));
""")
    assert out == {"running": 1, "afterLeave": 0, "queuedTick": 0, "noneRunning": 0, "backtestsOnly": 0}


def test_runs_list_failed_refresh_keeps_the_last_render_with_one_note(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_runs_fail", RUNS_SOURCE + """
const container = await open(render, {});
const lastGood = JSON.stringify(rowTexts(container.all("table")[0]));
const out = { start: refreshNotes(container) };
routes["/api/runs"] = boom;
await activeTimers()[0].fn();
out.first = { notes: refreshNotes(container), text: byClass(container, "refresh-note")[0].textContent, rows: JSON.stringify(rowTexts(container.all("table")[0])) === lastGood,
  errors: errorPanels(container), timers: activeTimers().length };
await activeTimers()[0].fn();
out.second = { notes: refreshNotes(container), errors: errorPanels(container) };
routes["/api/runs"] = () => [{ runs: served }, 200];
await activeTimers()[0].fn();
out.recovered = { notes: refreshNotes(container), rows: JSON.stringify(rowTexts(container.all("table")[0])) === lastGood };
console.log(JSON.stringify(out));
""")
    assert out["start"] == 0
    assert out["first"] == {"notes": 1, "text": "refresh failed", "rows": True, "errors": 0, "timers": 1}
    assert out["second"] == {"notes": 1, "errors": 0}  # the failures do not stack
    assert out["recovered"] == {"notes": 0, "rows": True}  # cleared by the next success


def test_runs_list_escapes_the_end_reason_and_keeps_filters_working(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_runs_escape", RUNS_SOURCE + """
served = [live(7, "failed", { end_reason: "<script>alert(1)</script>" }), live(6, "running")];
const container = await open(render, {});
const scripts = find(container, (c) => c.tag === "SCRIPT").length;
const failed = byClass(container, "badge")[0];
// the status filter offers the liveness values and filters by them
const options = container.all("option").map((o) => o.textContent);
console.log(JSON.stringify({ scripts, title: failed.getAttribute("title"), text: failed.textContent, options }));
""")
    assert out["scripts"] == 0
    assert out["title"] == "End reason: <script>alert(1)</script>"  # an attribute value, never markup
    assert out["text"] == "failed"
    assert "running" in out["options"] and "failed" in out["options"]


# --- the overview of a live run -----------------------------------------------------------------

OVERVIEW_SOURCE = HARNESS + """
const { render } = await import("./js/views/overview.js");
const stats = { symbol: "AAPL", algo: "EMACrossing", mode: "long", trade_count: 0, wins: 0, win_rate: null, net: "0.0000",
  commission: "0.0000", avg_win: null, avg_loss: null, max_drawdown: "0.0000", final_equity: "10000.0000" };
const POSITIONS = [
  { account: "EMA/1", symbol: "AAPL", side: "long", quantity: "100.0000", entry_price: "191.2350", entry_time: "2026-11-25T09:35:00", stop: null, target: null },
  { account: "EMA/1", symbol: "MSFT", side: "short", quantity: "50.0000", entry_price: "400.0000", entry_time: "2026-11-25T09:36:00", stop: "405.0000", target: "390.5000", last_close: "399.0000", unrealised: "48.5000" },
];
const COUNTS = { fed: { AAPL: 30, MSFT: 28 }, late: { MSFT: 1 },
  dropped: { AAPL: { connection_lost: 2, snapshot_only: 1, farm_break: 4, stalled: 5, rerequest_gap: 6, previous_session: 7 }, MSFT: { "<script>x</script>": 3 } },
  gap_rule: { bars_after_gap: { AAPL: 2 }, suppressed_entries: 1 }, other: { symbol: { "AAPL:weird": 4 } } };
const detail = (runExtra = {}, extra = {}) => ({
  run: { run_id: 5, status: "running", incomplete: true, kind: "paper-live", fills: "realistic", started_at: "2026-11-25T14:30:00+00:00",
    ended_at: null, code_version: "abcdef0123456789", symbols: ["AAPL", "MSFT"], first_bar: "2026-11-25T09:30:00", last_bar: "2026-11-25T09:40:00",
    data_directory: "-", replayed_minutes: 11, missing_minutes: {}, uncovered_minutes: 0, gap_rule: null, data_warnings: [], capital: "10000.0000",
    costs: { commission_per_share: "0.0050", commission_minimum: "1.0000", slippage_per_share: "0.0000" },
    liveness: "running", end_reason: null, last_write_at: "2026-11-25T14:40:00+00:00", ...runExtra },
  accounts: { "EMA/1": stats }, total: stats, open_positions: POSITIONS, live_counts: COUNTS, ...extra });
const equity = { run_id: 5, step: true, capital: "10000.0000", stored_points: 1, points: [{ time: "2026-11-25 09:30:00", equity: "10000.0000" }] };
let served = detail();
routes["/api/runs/5"] = () => [served, 200];
routes["/api/runs/5/equity"] = ok(equity);
const ENDED = () => detail({ status: "completed", incomplete: false, ended_at: "2026-11-25T21:00:00+00:00", liveness: "completed", end_reason: "session end" }, { open_positions: [] });
"""


def test_overview_of_a_live_run_shows_positions_counts_and_status(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_overview", OVERVIEW_SOURCE + """
const container = await open(render, { runId: 5 });
const positions = sectionOf(container, "Open positions");
const counts = sectionOf(container, "Live counts");
const status = sectionOf(container, "Live status");
const out = {
  timers: activeTimers().map((t) => t.ms),
  status: status.textContent, badge: byClass(status, "badge")[0].className,
  runStatus: sectionOf(container, "Run status").textContent,
  headers: texts(positions.all("th")), rows: rowTexts(positions.all("table")[0]),
  countHeaders: texts(counts.all("th")), countRows: rowTexts(counts.all("table")[0]),
  lines: texts(counts.all("li")),
  order: container.children.map((c) => c.tag + "." + c.className + "|" + (c.getAttribute("aria-label") || "")),
};
// no unrealised or last close anywhere: those columns are not shown
served = detail({}, { open_positions: [POSITIONS[0]] });
const other = await open(render, { runId: 5 });
out.plainHeaders = texts(sectionOf(other, "Open positions").all("th"));
out.plainRows = rowTexts(sectionOf(other, "Open positions").all("table")[0]);
served = detail({}, { open_positions: [] });
out.emptyText = sectionOf(await open(render, { runId: 5 }), "Open positions").textContent;
served = detail({}, { live_counts: { fed: {}, dropped: {}, late: {}, gap_rule: null } });
out.noCounts = sectionOf(await open(render, { runId: 5 }), "Live counts").textContent;
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["timers"] == [30000]
    assert "Liveness running" in out["status"] and "Last write 2026-11-25 14:40:00 UTC" in out["status"]
    assert "End reason" not in out["status"]
    assert out["badge"] == "badge live running"
    assert "running" in out["runStatus"] and "incomplete" not in out["runStatus"]  # not "incomplete": it is live
    assert out["headers"] == ["Account", "Symbol", "Side", "Quantity", "Entry price", "Entry time", "Stop", "Target",
                              "Last close", "Unrealised"]
    assert out["rows"] == [
        ["EMA/1", "AAPL", "long", "100", "191.235", "2026-11-25 09:35:00", "-", "-", "-", "-"],  # null stop and target, no unrealised
        ["EMA/1", "MSFT", "short", "50", "400.00", "2026-11-25 09:36:00", "405.00", "390.50", "399.00", "+48.50"],
    ]
    assert out["countHeaders"] == ["Symbol", "Minutes fed", "Late (not fed)", "Dropped"]
    assert out["countRows"] == [
        ["AAPL", "30", "0", "connection lost 2, snapshot only 1, data farm break 4, stalled stream 5, re-request gap 6, previous session 7"],
        ["MSFT", "28", "1", "<script>x</script> 3"],
    ]
    assert out["lines"] == [
        "Bars that follow a gap (entries signalled on them were refused): AAPL 2.",
        "1 entry refused on bars that follow a gap.",
        "Other counts (symbol): AAPL:weird 4.",
    ]
    order = out["order"]
    assert order.index("DIV.live-box|") == order.index("SECTION.panel|Run status") + 1  # right under the status
    assert order.index("SECTION.panel|Run settings") == order.index("DIV.live-box|") + 1
    assert out["plainHeaders"] == ["Account", "Symbol", "Side", "Quantity", "Entry price", "Entry time", "Stop", "Target"]
    assert out["plainRows"] == [["EMA/1", "AAPL", "long", "100", "191.235", "2026-11-25 09:35:00", "-", "-"]]
    assert "No open positions." in out["emptyText"]
    assert "No minutes counted yet." in out["noCounts"]


def test_overview_refreshes_every_30_seconds_then_stops_when_the_run_ends_or_the_view_closes(
        node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_overview_timer", OVERVIEW_SOURCE + """
const container = await open(render, { runId: 5 });
const out = { timers: activeTimers().map((t) => t.ms), requests: count("/api/runs/5") };
// a tick: new positions and counts replace the old ones in place
served = detail({}, { open_positions: [POSITIONS[1]], live_counts: { ...COUNTS, fed: { AAPL: 31, MSFT: 29 } } });
await activeTimers()[0].fn();
out.tick = { requests: count("/api/runs/5"), rows: rowTexts(sectionOf(container, "Open positions").all("table")[0]).length,
  fed: rowTexts(sectionOf(container, "Live counts").all("table")[0]).map((r) => r[1]), timers: activeTimers().length, health: dispatched.length };
// the run ends: the last refresh shows it and the timer is cleared
served = ENDED();
await activeTimers()[0].fn();
out.ended = { timers: activeTimers().length, cleared: timers[0].cleared, status: sectionOf(container, "Live status").textContent,
  positions: sectionOf(container, "Open positions").textContent, badge: byClass(container, "badge").map((b) => [b.textContent, b.getAttribute("title")]) };
// a second view that is left while its timer runs
served = detail();
const before = timers.length;
await open(render, { runId: 5 });
out.second = { added: timers.length - before, active: activeTimers().length };
await leave();
out.afterLeave = activeTimers().length;
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["timers"] == [30000] and out["requests"] == 1
    assert out["tick"] == {"requests": 2, "rows": 1, "fed": ["31", "29"], "timers": 1, "health": 1}
    ended = out["ended"]
    assert ended["timers"] == 0 and ended["cleared"] is True
    assert "Liveness completed" in ended["status"] and "End reason session end" in ended["status"]
    assert "No open positions." in ended["positions"]
    assert ["completed", "End reason: session end"] in ended["badge"]
    assert out["second"] == {"added": 1, "active": 1}
    assert out["afterLeave"] == 0


def test_overview_failed_refresh_keeps_the_last_render_with_one_note(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_overview_fail", OVERVIEW_SOURCE + """
const container = await open(render, { runId: 5 });
const snapshot = () => JSON.stringify([rowTexts(sectionOf(container, "Open positions").all("table")[0]),
  rowTexts(sectionOf(container, "Live counts").all("table")[0]), sectionOf(container, "Live status").textContent]);
const good = snapshot();
const out = {};
for (const [name, handlers] of [["doc", { "/api/runs/5": boom }], ["equity", { "/api/runs/5/equity": boom }], ["both", { "/api/runs/5": boom, "/api/runs/5/equity": boom }]]) {
  const saved = { ...routes };
  Object.assign(routes, handlers);
  await activeTimers()[0].fn();
  await activeTimers()[0].fn();
  out[name] = { notes: refreshNotes(container), errors: errorPanels(container), same: snapshot() === good, h2: container.all("h2").length };
  Object.assign(routes, saved);
  await activeTimers()[0].fn();
  out[name].cleared = refreshNotes(container);
}
// an answer that is not a run document counts as a failure too
routes["/api/runs/5"] = ok({ nothing: true });
await activeTimers()[0].fn();
out.junk = { notes: refreshNotes(container), same: snapshot() === good };
console.log(JSON.stringify(out));
""")
    for name in ("doc", "equity", "both"):
        assert out[name] == {"notes": 1, "errors": 0, "same": True, "h2": 1, "cleared": 0}, name
    assert out["junk"] == {"notes": 1, "same": True}


def test_live_text_is_escaped(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_escape", OVERVIEW_SOURCE + """
const bad = "<script>alert(1)</script>";
const evil = { ...POSITIONS[1], symbol: bad, account: "<img src=x onerror=alert(1)>" };
served = detail({ end_reason: bad, liveness: "failed", status: "failed", incomplete: false }, {
  open_positions: [evil], live_counts: { fed: { [bad]: 1 }, late: {}, dropped: { [bad]: { [bad]: 2 } }, gap_rule: { bars_after_gap: { [bad]: 3 }, suppressed_entries: 2 }, other: { [bad]: { [bad]: 1 } } } });
const container = await open(render, { runId: 5 });
const elements = find(container, () => true).map((e) => e.tag);
const alltext = container.textContent;
console.log(JSON.stringify({ scripts: elements.filter((t) => t === "SCRIPT" || t === "IMG").length, occurrences: alltext.split(bad).length - 1,
  badge: byClass(container, "badge").map((b) => b.getAttribute("title")) }));
""")
    assert out["scripts"] == 0  # nothing became an element
    assert out["occurrences"] >= 7  # end reason, symbol, count symbol and reason, gap line, other counts: all as text
    assert "End reason: <script>alert(1)</script>" in out["badge"]


# --- the algo view of a live run ----------------------------------------------------------------

ALGO_SOURCE = OVERVIEW_SOURCE + """
const { render: renderAlgo } = await import("./js/views/algo.js");
const barList = (n) => Array.from({ length: n }, (_, i) => ({ time: "2026-11-25 09:" + String(30 + i).padStart(2, "0") + ":00", open: 10, high: 12, low: 9, close: 11 + i, volume: 5 }));
let barsServed = { symbol: "AAPL", available: true, reason: null, bars: barList(3) };
let tradesServed = { trades: [] };
let docServed = detail();
routes["/api/runs/5"] = () => [docServed, 200];
routes["/api/runs/5/trades"] = () => [tradesServed, 200];
routes["/api/runs/5/bars"] = () => [barsServed, 200];
const candleChart = () => charts[charts.length - 1];
const chartNotes = (container) => byClass(container, "chart-note").map((n) => n.textContent + (n.hidden ? " [hidden]" : ""));
const algoParams = { runId: 5, idname: "EMA/1" };
"""


def test_algo_view_reloads_the_visible_day_every_60_seconds_and_keeps_the_users_zoom(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_algo_zoom", ALGO_SOURCE + """
const container = await open(renderAlgo, algoParams);
const chart = candleChart();
const canvas = byClass(container, "chart-canvas")[1];
const out = { timers: activeTimers().map((t) => t.ms), bars: chart.series[0].data.length, fits0: chart.fits, dayQuery: requests.find((r) => r.path.endsWith("/bars")).query };
// not zoomed: the new bar is fitted in as before
barsServed = { ...barsServed, bars: barList(4) };
await activeTimers()[0].fn();
out.unzoomed = { bars: chart.series[0].data.length, fits: chart.fits, logical: chart.logical.length, requests: ["/bars", "/trades", "/series"].map((p) => count("/api/runs/5" + p)) };
// the user zooms: the next refresh puts the visible range back instead of fitting
canvas.fire("wheel", {});
barsServed = { ...barsServed, bars: barList(5) };
tradesServed = { trades: [{ side: "long", quantity: "100.0000", entry_price: "10.0000", exit_price: "11.0000", gross: "1.0000", commission: "0.0000", net: "1.0000",
  exit_reason: "target", synthetic: false, broker_ref: "1", symbol: "AAPL", account: "EMA/1", entry_time: "2026-11-25 09:31:00", exit_time: "2026-11-25 09:33:00" }] };
const fitsBefore = chart.fits;
await activeTimers()[0].fn();
out.zoomed = { bars: chart.series[0].data.length, fitsAdded: chart.fits - fitsBefore, logical: chart.logical.slice(), focused: chart.visible.length,
  tradeRows: rowTexts(container.all("table")[0]).length, health: dispatched.length };
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["timers"] == [60000] and out["bars"] == 3
    assert out["dayQuery"] == {"symbol": "AAPL", "start": "2026-11-25 00:00:00", "end": "2026-11-25 23:59:59"}
    assert out["unzoomed"] == {"bars": 4, "fits": out["fits0"] + 1, "logical": 0, "requests": [2, 2, 2]}
    zoomed = out["zoomed"]
    assert zoomed["bars"] == 5 and zoomed["fitsAdded"] == 0  # not fitted: the user's view stays
    assert zoomed["logical"] == [{"from": 2, "to": 9}]
    assert zoomed["focused"] == 0 and zoomed["tradeRows"] == 1  # the new trade is listed, the view did not jump to it
    assert zoomed["health"] == 2


def test_algo_view_of_a_live_run_without_bars_says_no_bars_yet_and_picks_them_up(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_algo_nobars", ALGO_SOURCE + """
docServed = detail({ first_bar: null, last_bar: null });
const container = await open(renderAlgo, algoParams);
const out = { notes: chartNotes(container), barRequests: count("/api/runs/5/bars"), errors: errorPanels(container), timers: activeTimers().map((t) => t.ms),
  label: byClass(container, "day-label")[0].textContent };
// the first bar arrives: the next refresh shows that day
docServed = detail();
await activeTimers()[0].fn();
out.after = { notes: chartNotes(container).filter((n) => n.startsWith("No bars") || n.startsWith("This run")), bars: candleChart().series[0].data.length,
  barRequests: count("/api/runs/5/bars"), label: byClass(container, "day-label")[0].textContent, timers: activeTimers().length };
// a live run whose bars route answers an empty list for the day (available: true) is "no bars yet", not an error
barsServed = { symbol: "AAPL", available: true, reason: null, bars: [] };
const second = await open(renderAlgo, algoParams);
out.empty = { notes: chartNotes(second), errors: errorPanels(second) };
console.log(JSON.stringify(out));
""")
    assert "No bars yet." in out["notes"]
    assert out["barRequests"] == 0 and out["errors"] == 0 and out["timers"] == [60000]
    assert out["label"] == "no day"
    assert out["after"]["bars"] == 3 and out["after"]["barRequests"] == 1
    assert out["after"]["notes"] == []  # "No bars yet." is gone
    assert out["after"]["label"] == "2026-11-25 (data time)" and out["after"]["timers"] == 1
    assert "No bars yet." in out["empty"]["notes"] and out["empty"]["errors"] == 0


def test_algo_view_timer_stops_when_the_run_ends_or_the_view_closes(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_algo_stop", ALGO_SOURCE + """
const container = await open(renderAlgo, algoParams);
const out = { start: activeTimers().length };
docServed = ENDED();
await activeTimers()[0].fn();
out.ended = { timers: activeTimers().length, status: sectionOf(container, "Live status").textContent, run: sectionOf(container, "Run status").textContent };
docServed = detail();
await open(renderAlgo, algoParams);
out.second = activeTimers().length;
await leave();
out.afterLeave = activeTimers().length;
// a run that is not running (a failed one) never starts a timer; neither does a backtest
docServed = detail({ liveness: "failed", status: "failed", incomplete: false, end_reason: "connection closed" });
const before = timers.length;
const failed = await open(renderAlgo, algoParams);
out.failedRun = { added: timers.length - before, status: sectionOf(failed, "Live status").textContent };
console.log(JSON.stringify(out));
""")
    assert out["start"] == 1
    assert out["ended"]["timers"] == 0
    assert "Liveness completed" in out["ended"]["status"] and "End reason session end" in out["ended"]["status"]
    assert out["second"] == 1 and out["afterLeave"] == 0
    assert out["failedRun"]["added"] == 0 and "End reason connection closed" in out["failedRun"]["status"]


def test_algo_view_failed_refresh_keeps_the_last_render_with_one_note(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_algo_fail", ALGO_SOURCE + """
const container = await open(renderAlgo, algoParams);
const chart = candleChart();
const barsBefore = chart.series[0].data.length;
const baseErrors = errorPanels(container); // the 404 of the unserved series and orders routes shows in the legend
const out = {};
for (const path of ["/api/runs/5/bars", "/api/runs/5/trades", "/api/runs/5"]) {
  const saved = routes[path];
  routes[path] = boom;
  barsServed = { ...barsServed, bars: barList(6) };
  await activeTimers()[0].fn();
  await activeTimers()[0].fn();
  out[path] = { notes: refreshNotes(container), errors: Math.max(0, errorPanels(container) - baseErrors), bars: chart.series[0].data.length, timers: activeTimers().length, h2: container.all("h2").length };
  routes[path] = saved;
  await activeTimers()[0].fn();
  out[path].cleared = refreshNotes(container);
  barsServed = { ...barsServed, bars: barList(3) };
}
out.barsBefore = barsBefore;
console.log(JSON.stringify(out));
""")
    assert out["barsBefore"] == 3
    for path in ("/api/runs/5/bars", "/api/runs/5/trades", "/api/runs/5"):
        failed = out[path]
        assert failed["notes"] == 1 and failed["errors"] == 0 and failed["timers"] == 1 and failed["h2"] == 1, path
        assert failed["cleared"] == 0, path
    assert out["/api/runs/5/bars"]["bars"] == 3  # the failed bars refresh left the last good bars


# --- a backtest is drawn as before ---------------------------------------------------------------

# The sha256 of the JSON dump (tag, sorted attributes, children, text) of the rendered pages of NOCHANGE_SOURCE,
# recorded by running that same script against the dashboard code of origin/revival (before this change).
BACKTEST_TREES = {
    "runs": "49327c471a1a46e89e27b81503b63f1d9314afb092c71468727536a956644dbc",
    "overview": "1844528608adb5e1ae881f747ad07ea6772b75e704fe8ebf2d2f66556d595f6b",
    "algo": "ea93b33d0dc8470c5e2dfb949dac7b846b28f86adfe4b6789530e181aa8d1924",
    "overviewRunning": "1a4397ac38ed3d5d20ef56ddf73ee5459df3d5b4c967772ed78c705935799603",
}

NOCHANGE_SOURCE = HARNESS + """
import { createHash } from "node:crypto";
const dump = (node) => {
  if (!(node instanceof FakeEl)) return node.data;
  return [node.tag, Object.fromEntries(Object.entries(node.attrs).sort(([a], [b]) => (a < b ? -1 : 1))), ...node.children.map(dump)];
};
const digest = (node) => createHash("sha256").update(JSON.stringify(dump(node))).digest("hex");
const { render: renderRuns } = await import("./js/views/runs.js");
const { render: renderOverview } = await import("./js/views/overview.js");
const { render: renderAlgo } = await import("./js/views/algo.js");
const bt = (run_id, status, extra = {}) => ({ run_id, started_at: "2026-11-20T10:00:00+00:00", ended_at: status === "running" ? null : "2026-11-20T10:00:05+00:00",
  duration_seconds: status === "running" ? null : 5, status, incomplete: status === "running", kind: "backtest", fills: "realistic",
  code_version: "abcdef0123456789", symbols: ["AAPL"], algo_count: 1, trade_count: 2, net: "12.5000", ...extra });
routes["/api/runs"] = ok({ runs: [bt(3, "completed"), bt(2, "failed"), bt(1, "running", { kind: "paper" })] });
const stats = { symbol: "AAPL", algo: "EMACrossing", mode: "long", trade_count: 2, wins: 1, win_rate: "0.5000", net: "2.0000", commission: "0.0000",
  avg_win: "5.0000", avg_loss: "-3.0000", max_drawdown: "3.0000", final_equity: "10002.0000" };
const doc = (status) => ({ run: { run_id: 1, status, incomplete: status === "running", kind: "backtest", fills: "realistic", started_at: "2026-11-20T10:00:00+00:00",
  ended_at: status === "running" ? null : "2026-11-20T10:00:05+00:00", code_version: "abcdef0123456789", symbols: ["AAPL"], first_bar: "2024-01-02T09:30:00",
  last_bar: "2024-01-03T16:00:00", data_directory: "OfflineData", replayed_minutes: 780, missing_minutes: { AAPL: 3 }, uncovered_minutes: 1,
  gap_rule: { bars_after_gap: { AAPL: 1 }, suppressed_entries: { count: 1, by_algo: [{ algo: "EMA/1", symbol: "AAPL", count: 1 }] } },
  data_warnings: ["a warning"], capital: "10000.0000", costs: { commission_per_share: "0.0050", commission_minimum: "1.0000", slippage_per_share: "0.0000" } },
  accounts: { "EMA/1": stats }, total: { ...stats, synthetic_closes: 1, cancelled_entries: 1, ignored_events: { x: 2 } } });
routes["/api/runs/1"] = ok(doc("completed"));
routes["/api/runs/1/equity"] = ok({ run_id: 1, step: true, capital: "10000.0000", stored_points: 2, points: [{ time: "2024-01-02 09:32:00", equity: "10001.0000" }, { time: "2024-01-03 09:31:00", equity: "10002.0000" }] });
const tr = (extra) => ({ side: "long", quantity: "100.0000", entry_price: "10.0000", exit_price: "11.0000", gross: "1.0000", commission: "0.0000", net: "1.0000",
  exit_reason: "target", synthetic: false, broker_ref: "1", symbol: "AAPL", account: "EMA/1", ...extra });
routes["/api/runs/1/trades"] = ok({ trades: [tr({ entry_time: "2024-01-02 09:31:00", exit_time: "2024-01-02 09:32:00" }), tr({ side: "short", entry_time: "2024-01-03 09:30:00", exit_time: "2024-01-03 09:31:00", net: "-3.0000", exit_reason: "stop" })] });
routes["/api/runs/1/bars"] = ok({ symbol: "AAPL", available: true, reason: null, bars: ["09:30", "09:31", "09:32"].map((m, i) => ({ time: "2024-01-02 " + m + ":00", open: 10, high: 12, low: 9, close: 11 + i, volume: 5 })) });
const out = {};
out.runs = digest(await open(renderRuns, {}));
out.overview = digest(await open(renderOverview, { runId: 1 }));
out.algo = digest(await open(renderAlgo, { runId: 1, idname: "EMA/1" }));
// and again for a run that did not finish
routes["/api/runs/1"] = ok(doc("running"));
out.overviewRunning = digest(await open(renderOverview, { runId: 1 }));
out.timers = timers.length;
console.log(JSON.stringify(out));
"""


def test_backtest_pages_are_drawn_exactly_as_before(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_nochange", NOCHANGE_SOURCE)
    assert isinstance(out, dict)
    assert out["timers"] == 0  # no timer for a backtest, not even an unfinished one
    assert {k: out[k] for k in BACKTEST_TREES} == BACKTEST_TREES


# --- the shell and the helpers -----------------------------------------------------------------


def test_health_strip_shows_live_state_only_when_present(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_health", FAKE_DOM + """
const { liveHealthItems } = await import("./js/live.js");
const shown = (health) => liveHealthItems(health).map((n) => [n.tag, n.className, n.textContent]);
console.log(JSON.stringify({
  running: shown({ live_state: "running", running_runs: [1], stale_runs: [] }), stale: shown({ live_state: "stale" }), idle: shown({ live_state: "idle" }),
  absent: shown({ run_count: 3, incomplete_runs: [] }), nullish: shown(null), junk: shown({ live_state: "<script>" }), number: shown({ live_state: 3 }),
}));
""")
    assert out["running"] == [["SPAN", "live-state running", "live: running"]]
    assert out["stale"] == [["SPAN", "live-state stale", "live: stale"]]
    assert out["idle"] == [["SPAN", "live-state idle", "live: idle"]]
    assert out["absent"] == [] and out["nullish"] == [] and out["number"] == []
    assert out["junk"] == [["SPAN", "live-state", "live: <script>"]]  # text, and no class from the server
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    assert "liveHealthItems(health)" in main


def test_poller_is_registered_with_the_scope_and_stops_with_it(node_workdir: Path) -> None:
    out = run_node(node_workdir, "live_poller", FAKE_DOM + """
const timers = [];
globalThis.setInterval = (fn, ms) => { timers.push({ fn, ms, cleared: false }); return timers.length; };
globalThis.clearInterval = (id) => { timers[id - 1].cleared = true; };
const { createPoller } = await import("./js/live.js");
const { createScope } = await import("./js/lifecycle.js");
const scope = createScope();
let calls = 0;
let release = null;
const ctx = { isCurrent: () => !scope.ended, onCleanup: (fn) => scope.onCleanup(fn) };
const poller = createPoller(ctx, { intervalMs: 5000, tick: () => { calls += 1; return new Promise((r) => { release = r; }); } });
const out = { before: timers.length };
poller.start(); poller.start();
out.started = { timers: timers.length, ms: timers[0].ms, active: poller.active };
const first = timers[0].fn(); // a tick that is still running...
timers[0].fn(); // ...is not overlapped
out.overlap = calls;
release(); await first;
timers[0].fn(); out.afterRelease = calls; release();
scope.dispose(); // the view is left: the registered stop clears the timer
out.disposed = { cleared: timers[0].cleared, active: poller.active };
poller.start(); // a poller of a finished scope never starts
out.restart = timers.length;
// a scope that ended before start()
const dead = createScope(); dead.dispose();
const late = createPoller({ isCurrent: () => !dead.ended, onCleanup: (fn) => dead.onCleanup(fn) }, { intervalMs: 1, tick() {} });
late.start();
out.late = { timers: timers.length, active: late.active };
// a tick that throws is logged and the timer keeps going
const live = createScope();
const errors = []; console.error = (...a) => errors.push(a[0]);
const throwing = createPoller({ isCurrent: () => !live.ended, onCleanup: (fn) => live.onCleanup(fn) }, { intervalMs: 1, tick: async () => { throw new Error("x"); } });
throwing.start(); await timers.at(-1).fn(); await timers.at(-1).fn();
out.throwing = { logged: errors.length, active: throwing.active };
console.log(JSON.stringify(out));
""")
    assert out["before"] == 0
    assert out["started"] == {"timers": 1, "ms": 5000, "active": True}  # start is idempotent
    assert out["overlap"] == 1 and out["afterRelease"] == 2
    assert out["disposed"] == {"cleared": True, "active": False}
    assert out["restart"] == 1
    assert out["late"] == {"timers": 1, "active": False}
    assert out["throwing"] == {"logged": 2, "active": True}


def test_live_module_is_referenced_and_the_views_use_the_lifecycle_for_their_timers() -> None:
    for view in ("runs", "overview", "algo"):
        refs = {t for _k, t in page_references(STATIC / "js" / "views" / f"{view}.js")}
        assert "../live.js" in refs, view
        source = (STATIC / "js" / "views" / f"{view}.js").read_text(encoding="utf-8")
        assert "createPoller(ctx" in source, view  # the timer is created with the view's ctx
        assert "setInterval" not in source and "setTimeout" not in source, view  # never a bare timer in a view
    live = (STATIC / "js" / "live.js").read_text(encoding="utf-8")
    assert "ctx.onCleanup(stop)" in live
