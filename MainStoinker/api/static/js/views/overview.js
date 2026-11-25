// The run overview: status, settings, the account equity chart with its drawdown, the per-algo table
// (the columns of `report RUN`, with a TOTAL row) and notes. Follows the view contract in main.js.
//
// Exports: render, soFarNote, ALGO_COLUMNS, displayStatus, runDurationSeconds, buildRows, statusNotes, runNotes,
// settingRows. The last five are pure (no DOM) so node can test them.
import { ApiError, HEALTH_EVENT, getJson, isAbortError } from "../api.js";
import { createEquityChart } from "../charts/equity.js";
import { errorPanel, h, loadingPanel, viewHeading } from "../dom.js";
import {
  chartTimeToDataTime, dataTimeToChartTime, formatDuration, formatMoney, formatNet, formatPercent, formatUtc,
  shortVersion,
} from "../format.js";
import {
  OVERVIEW_REFRESH_MS, createPoller, createRefreshNote, isLiveRun, isRunning, liveCountsPanel, liveStatusPanel,
  positionsPanel,
} from "../live.js";
import { algoHash, runsHash } from "../routes.js";
import { dataTable, netCell } from "../table.js";

const LEGACY_NOTE = "legacy (bar-close fills, no commission)";
const DASH = "-";

// A run that never finished is "incomplete", otherwise its stored status.
export function displayStatus(run) {
  if (isLiveRun(run)) return run.liveness; // a live run: running, stale, completed or failed
  return run.incomplete ? "incomplete" : run.status;
}

// Seconds between the run's start and end (real instants, both with an offset), or null.
export function runDurationSeconds(run) {
  if (!run.ended_at || !run.started_at) return null;
  const seconds = (Date.parse(run.ended_at) - Date.parse(run.started_at)) / 1000;
  return Number.isFinite(seconds) ? Math.max(seconds, 0) : null;
}

// One sentence for a run that did not complete normally, or "" for a completed run.
export function statusNotes(run) {
  if (run.status === "completed" && !run.incomplete) return "";
  if (run.incomplete || run.status === "running") {
    return "This run is incomplete: it is still marked running, or its process was killed. "
      + "The figures cover only what was stored.";
  }
  return "This run failed. It did not finish; the figures cover only what was stored.";
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function text(value) {
  return value === null || value === undefined || value === "" ? DASH : String(value);
}

// The columns of `report`: label, and for each of them how a stats record becomes display text and a
// sort key. `net` and `final_equity` etc. are 4-decimal strings, `win_rate` a fraction string or null.
export const ALGO_COLUMNS = [
  { key: "trades", label: "Trades", field: "trade_count", show: (v) => text(v) },
  { key: "wins", label: "Wins", field: "wins", show: (v) => text(v) },
  { key: "winPercent", label: "Win %", field: "win_rate", show: (v) => formatPercent(v) },
  { key: "net", label: "Net", field: "net", show: (v) => formatNet(v), net: true },
  { key: "commission", label: "Commission", field: "commission", show: (v) => formatMoney(v) },
  { key: "avgWin", label: "Avg win", field: "avg_win", show: (v) => formatMoney(v) },
  { key: "avgLoss", label: "Avg loss", field: "avg_loss", show: (v) => formatMoney(v) },
  { key: "maxDrawdown", label: "Max drawdown", field: "max_drawdown", show: (v) => formatMoney(v) },
  { key: "finalEquity", label: "Final equity", field: "final_equity", show: (v) => formatMoney(v) },
];

function statsRow(name, total, stats, extra) {
  const record = stats && typeof stats === "object" ? stats : {};
  const cells = {};
  const sort = {};
  const raw = {};
  for (const column of ALGO_COLUMNS) {
    const value = record[column.field];
    raw[column.key] = value === undefined ? null : value;
    cells[column.key] = column.show(value);
    sort[column.key] = numberOrNull(value);
  }
  return { name, total, cells, sort, raw, ...extra };
}

// The report document ({run, accounts, total}) as table rows: one per algo in the document's order,
// then TOTAL. A missing or null figure is "-" in the cell and null as a sort key.
export function buildRows(doc) {
  const accounts = doc && doc.accounts && typeof doc.accounts === "object" ? doc.accounts : {};
  const rows = Object.entries(accounts).map(([name, stats]) => statsRow(name, false, stats, {
    symbol: stats && stats.symbol ? stats.symbol : "",
    algoId: stats && stats.algo ? stats.algo : "",
    mode: stats && stats.mode ? stats.mode : "",
  }));
  rows.push(statsRow("TOTAL", true, doc ? doc.total : null, {}));
  return rows;
}

// The notes shown under the table, from the TOTAL statistics: only the ones that are not zero.
export function runNotes(doc) {
  const total = doc && doc.total && typeof doc.total === "object" ? doc.total : {};
  const notes = [];
  // Gaps in the replayed bars (a run stored before they were recorded has none of these fields).
  const run = doc && doc.run && typeof doc.run === "object" ? doc.run : {};
  const missing = run.missing_minutes && typeof run.missing_minutes === "object" ? run.missing_minutes : {};
  const gapped = Object.entries(missing).filter(([, n]) => numberOrNull(n) > 0);
  if (gapped.length > 0) {
    notes.push("Missing bars (skipped for those minutes): "
      + gapped.map(([symbol, n]) => symbol + " " + n + " of " + run.replayed_minutes + " minutes").join(", ") + ".");
  }
  const uncovered = numberOrNull(run.uncovered_minutes) || 0;
  if (uncovered > 0) {
    notes.push(uncovered + (uncovered === 1 ? " minute" : " minutes") + " with no bar for any symbol.");
  }
  // The gap rule (no new position on a bar that follows a gap); absent for older runs.
  const rule = run.gap_rule && typeof run.gap_rule === "object" ? run.gap_rule : {};
  const afterGap = rule.bars_after_gap && typeof rule.bars_after_gap === "object" ? rule.bars_after_gap : {};
  const gappedBars = Object.entries(afterGap).filter(([, n]) => numberOrNull(n) > 0);
  if (gappedBars.length > 0) {
    notes.push("Bars that follow a gap (entries signalled on them were refused): "
      + gappedBars.map(([symbol, n]) => symbol + " " + n).join(", ") + ".");
  }
  // an object {count, by_algo} here (a plain number is `live_counts`' shape, read by `gapRuleLines` in live.js)
  const refused = rule.suppressed_entries && typeof rule.suppressed_entries === "object" ? rule.suppressed_entries : {};
  const refusedCount = numberOrNull(refused.count) || 0;
  if (refusedCount > 0) {
    const byAlgo = Array.isArray(refused.by_algo) ? refused.by_algo : [];
    notes.push(refusedCount + (refusedCount === 1 ? " entry" : " entries") + " refused on bars that follow a gap"
      + (byAlgo.length > 0
        ? " (" + byAlgo.map((e) => String(e.algo) + " " + String(e.symbol) + ": " + String(e.count)).join(", ") + ")"
        : "") + ".");
  }
  const pending = rule.cancelled_pending_entries && typeof rule.cancelled_pending_entries === "object"
    ? rule.cancelled_pending_entries : {};
  const pendingCount = numberOrNull(pending.count) || 0;
  if (pendingCount > 0) {
    const byAlgo = Array.isArray(pending.by_algo) ? pending.by_algo : [];
    notes.push(pendingCount + (pendingCount === 1 ? " pending entry" : " pending entries")
      + " cancelled because the fill bar follows a gap"
      + (byAlgo.length > 0
        ? " (" + byAlgo.map((e) => String(e.algo) + " " + String(e.symbol) + ": " + String(e.count)).join(", ") + ")"
        : "") + ".");
  }
  if (Array.isArray(run.data_warnings)) {
    for (const warning of run.data_warnings) notes.push("Data warning: " + String(warning) + ".");
  }
  const synthetic = numberOrNull(total.synthetic_closes) || 0;
  if (synthetic > 0) {
    notes.push(synthetic + (synthetic === 1 ? " synthetic end-of-data close" : " synthetic end-of-data closes")
      + " (positions closed at the last bar; they are counted in the trades).");
  }
  const kinds = total.ignored_events && typeof total.ignored_events === "object" ? total.ignored_events : {};
  const ignored = Object.entries(kinds).filter(([, n]) => numberOrNull(n) > 0);
  if (ignored.length > 0) {
    const count = ignored.reduce((sum, [, n]) => sum + Number(n), 0);
    notes.push(count + (count === 1 ? " ignored event" : " ignored events") + " ("
      + ignored.map(([kind, n]) => kind + ": " + n).join(", ") + ").");
  }
  const cancelled = numberOrNull(total.cancelled_entries) || 0;
  if (cancelled > 0) {
    notes.push(cancelled + (cancelled === 1 ? " cancelled entry" : " cancelled entries") + ".");
  }
  return notes;
}

// A bar time (the server sends ISO text with a "T") as "YYYY-MM-DD HH:MM:SS" like every other market time on
// the page; text that cannot be parsed is shown as given.
function barTime(value) {
  if (value === null || value === undefined || value === "") return DASH;
  const chart = dataTimeToChartTime(value);
  const shown = chart === null ? null : chartTimeToDataTime(chart);
  return shown === null ? String(value) : shown;
}

// [label, value] pairs of the settings panel. Costs are shown as stored (4 decimals): a per-share
// figure such as 0.0050 must not be rounded to cents.
export function settingRows(run) {
  const costs = run.costs && typeof run.costs === "object" ? run.costs : {};
  const range = run.first_bar || run.last_bar
    ? barTime(run.first_bar) + " to " + barTime(run.last_bar) + " (data time)"
      + (isLiveRun(run) && run.liveness !== "completed" ? ", so far" : "")
    : DASH;
  const symbols = Array.isArray(run.symbols) && run.symbols.length > 0 ? run.symbols.join(", ") : DASH;
  return [
    ["Capital per algo", run.capital === null || run.capital === undefined ? DASH : formatMoney(run.capital)],
    ["Commission per share", text(costs.commission_per_share)],
    ["Minimum per order", text(costs.commission_minimum)],
    ["Slippage per share", text(costs.slippage_per_share)],
    ["Bar range", range],
    ["Symbols", symbols],
    ["Data folder", text(run.data_directory)],
  ];
}

function statusLine(run) {
  const duration = runDurationSeconds(run);
  const fills = run.fills === "realistic" ? "realistic" : run.fills === "legacy" ? LEGACY_NOTE : text(run.fills);
  const status = displayStatus(run);
  const item = (label, ...value) => h("li", {}, h("span", { class: "muted" }, label + " "), ...value);
  return h("ul", { class: "status-line" },
    item("Status", h("span", { class: "badge " + status }, status)),
    item("Kind", text(run.kind)),
    item("Fill model", fills),
    item("Start", formatUtc(run.started_at)),
    item("Duration", duration === null ? DASH : formatDuration(duration)),
    item("Code version", h("span", { class: "mono", title: run.code_version || "" }, shortVersion(run.code_version))));
}

function settingsPanel(run) {
  const list = h("dl", { class: "params" });
  for (const [label, value] of settingRows(run)) list.append(h("dt", {}, label), h("dd", {}, value));
  return h("section", { class: "panel", "aria-label": "Run settings" }, h("h3", {}, "Settings"), list);
}

function algoTable(doc, runId) {
  const rows = buildRows(doc);
  // The TOTAL row has no sort key, so a sorted column keeps it last in both directions.
  const keyed = (get) => (row) => (row.total ? null : get(row));
  const columns = [
    {
      label: "Algo",
      sortKey: keyed((row) => row.name),
      cell: (row) => (row.total
        ? { content: h("strong", {}, "TOTAL"), className: "total" }
        : {
          content: h("a", { href: algoHash(runId, row.name) }, row.name),
          title: [row.symbol, row.algoId, row.mode].filter(Boolean).join(" / "),
        }),
    },
    ...ALGO_COLUMNS.map((column) => ({
      label: column.label,
      numeric: true,
      sortKey: keyed((row) => row.sort[column.key]),
      cell: (row) => {
        const spec = column.net ? netCell(row.raw[column.key]) : { content: row.cells[column.key] };
        return row.total ? { ...spec, className: ((spec.className || "") + " total").trim() } : spec;
      },
    })),
  ];
  return dataTable({ columns, rows, caption: "Per-algo results", captionHidden: true, rowKey: (row) => row.name });
}

function chartPanel(chartBox, equity) {
  const stored = Number(equity.stored_points);
  const shown = Array.isArray(equity.points) ? equity.points.length : 0;
  const thinned = Number.isFinite(stored) && stored > shown
    ? " Showing " + shown + " of " + stored + " stored points."
    : "";
  return h("section", { class: "panel", "aria-label": "Account equity" },
    h("h3", {}, "Account equity and drawdown"),
    chartBox,
    h("p", { class: "muted chart-caption" },
      "All algos together; times are data time, as stored." + thinned));
}

function validDocument(value) {
  return Boolean(value) && typeof value === "object" && Boolean(value.run) && typeof value.run === "object"
    && Boolean(value.accounts) && typeof value.accounts === "object"
    && Boolean(value.total) && typeof value.total === "object";
}

// The status section's content: the status line and, for a run that did not complete normally, its note. A
// live run that is running is not "incomplete": the live status panel says where it stands.
function statusContent(run) {
  const warning = isRunning(run) ? "" : statusNotes(run);
  return [statusLine(run), warning ? h("p", { class: "warning", role: "note" }, warning) : null].filter(Boolean);
}

// The "so far" note of a live run that has not completed (running, stale or failed): its final equity and
// totals are the last stored figures. Null for a completed run.
export function soFarNote(run) {
  if (!isLiveRun(run) || run.liveness === "completed") return null;
  return h("p", { class: "muted so-far" },
    "So far: this run is " + String(run.liveness) + ", not completed. Final equity and the totals are the last stored figures.");
}

function notesSection(notes) {
  return h("section", { class: "panel", "aria-label": "Notes" }, h("h3", {}, "Notes"),
    h("ul", { class: "notes" }, notes.map((note) => h("li", {}, note))));
}

// What a live run adds under the status section, each part only when its key is present on the document.
function liveContent(doc) {
  return [
    isLiveRun(doc.run) ? liveStatusPanel(doc.run) : null,
    Array.isArray(doc.open_positions) ? positionsPanel(doc.open_positions) : null,
    doc.live_counts && typeof doc.live_counts === "object" ? liveCountsPanel(doc.live_counts) : null,
  ].filter(Boolean);
}

export async function render(container, params, ctx) {
  const runId = params.runId;
  container.replaceChildren(
    viewHeading("Run " + runId),
    h("p", {}, h("a", { href: runsHash() }, "Back to the runs list")),
    loadingPanel("run " + runId));

  let poller = null; // the timer of a running live run: created, with its cleanup, once the run is known to run
  const refreshNote = createRefreshNote();
  let liveBox = null; // the live parts and the refresh note; only for a run that has live keys
  let statusBox = null;
  let notesSlot = null;
  let soFarSlot = null;
  let table = null;
  let chart = null;

  const options = { signal: ctx.signal };
  const [docResult, equityResult] = await Promise.allSettled([
    getJson("/api/runs/" + runId, {}, options),
    getJson("/api/runs/" + runId + "/equity", {}, options),
  ]);
  if (!ctx.isCurrent()) return;
  for (const result of [docResult, equityResult]) {
    if (result.status === "rejected" && isAbortError(result.reason)) return;
  }

  const parts = [viewHeading("Run " + runId), h("p", {}, h("a", { href: runsHash() }, "Back to the runs list"))];

  // Without the run document there is nothing to show but why: one error panel. An unknown run, an
  // unreachable server and a 503 fail both requests the same way, so the equity error is not repeated.
  let doc = null;
  let docError = null;
  if (docResult.status === "fulfilled") {
    const value = docResult.value;
    if (validDocument(value)) {
      doc = value;
    } else {
      docError = new ApiError(200, "The server sent an unexpected response.", "unexpected");
    }
  } else {
    docError = docResult.reason;
  }
  if (!doc) {
    container.replaceChildren(...parts, errorPanel(docError));
    return;
  }

  statusBox = h("section", { class: "panel", "aria-label": "Run status" }, ...statusContent(doc.run));
  parts.push(statusBox);
  const live = liveContent(doc);
  if (live.length > 0) {
    liveBox = h("div", { class: "live-box" }, ...live);
    parts.push(liveBox);
  }
  parts.push(settingsPanel(doc.run));

  let chartBox = null;
  let equity = null;
  if (equityResult.status === "fulfilled") {
    const value = equityResult.value;
    if (value && Array.isArray(value.points)) {
      equity = value;
      chartBox = h("div", { class: "chart-box" });
      parts.push(chartPanel(chartBox, equity));
    } else {
      parts.push(errorPanel(new ApiError(200, "The server sent an unexpected response.", "unexpected")));
    }
  } else {
    parts.push(errorPanel(equityResult.reason));
  }

  table = algoTable(doc, runId);
  if (liveBox) soFarSlot = h("div", { class: "so-far-slot" }, ...[soFarNote(doc.run)].filter(Boolean));
  parts.push(h("section", { "aria-label": "Per-algo results" }, h("h3", {}, "Algos"),
    ...(soFarSlot ? [soFarSlot] : []), table.element));
  const notes = runNotes(doc);
  if (liveBox) {
    // a live run's notes change while it runs: a slot that holds the section (or nothing)
    notesSlot = h("div", { class: "live-notes" }, ...(notes.length > 0 ? [notesSection(notes)] : []));
    parts.push(notesSlot);
  } else if (notes.length > 0) {
    parts.push(notesSection(notes));
  }

  container.replaceChildren(...parts);

  if (chartBox) {
    try {
      chart = createEquityChart(chartBox, { ctx }); // registers its own disposal at once
      chart.setSeries([{
        label: "Account equity",
        points: equity.points,
        capital: equity.capital,
      }]);
    } catch (err) {
      chart = null;
      chartBox.replaceChildren(errorPanel(err));
    }
  }

  // A run that is running refreshes every OVERVIEW_REFRESH_MS: the status, the live parts, the algo table, the
  // notes and the equity curve are replaced in place; a failed refresh keeps all of them and shows the one note.
  let current = doc;
  let liveNodes = live;
  let noteShown = false;
  async function refreshLive() {
    const [docNext, equityNext] = await Promise.allSettled([
      getJson("/api/runs/" + runId, {}, options),
      getJson("/api/runs/" + runId + "/equity", {}, options),
    ]);
    if (!ctx.isCurrent()) return;
    for (const result of [docNext, equityNext]) {
      if (result.status === "rejected" && isAbortError(result.reason)) return;
    }
    const docOk = docNext.status === "fulfilled" && validDocument(docNext.value);
    const equityOk = equityNext.status === "fulfilled" && equityNext.value && Array.isArray(equityNext.value.points);
    if (docOk) {
      current = docNext.value;
      statusBox.replaceChildren(...statusContent(current.run));
      table.update({ rows: buildRows(current) });
      soFarSlot.replaceChildren(...[soFarNote(current.run)].filter(Boolean));
      const next = runNotes(current);
      notesSlot.replaceChildren(...(next.length > 0 ? [notesSection(next)] : []));
    }
    if (equityOk && chart) {
      chart.setSeries([{ label: "Account equity", points: equityNext.value.points, capital: equityNext.value.capital }]);
    }
    if (docOk && equityOk) refreshNote.ok();
    else refreshNote.fail();
    if (docOk || refreshNote.failed !== noteShown) {
      if (docOk) liveNodes = liveContent(current);
      noteShown = refreshNote.failed;
      liveBox.replaceChildren(...liveNodes, ...(noteShown ? [refreshNote.element] : []));
    }
    if (docOk && !isRunning(current.run)) poller.stop(); // the run ended: no more refreshes
    window.dispatchEvent(new Event(HEALTH_EVENT));
  }
  if (liveBox && isRunning(doc.run)) {
    poller = createPoller(ctx, { intervalMs: OVERVIEW_REFRESH_MS, tick: refreshLive }); // its stop is registered now
    poller.start();
  }
}
