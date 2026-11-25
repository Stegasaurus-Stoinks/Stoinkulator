// What the views add for a run that is being written live (kind paper-live / live): the liveness badge,
// the open positions, the live counts, the refresh timer and the "refresh failed" note.
//
// Everything here appears ONLY when the server sent the matching key: `liveness` on a run (list entry or
// the `run` header of the detail document), `open_positions` and `live_counts` on the detail document. A
// backtest run carries none of them, so its pages are drawn as before.
//
// Exports: RUNS_REFRESH_MS, OVERVIEW_REFRESH_MS, ALGO_REFRESH_MS, DROP_LABELS, isLiveRun, isRunning,
// livenessBadge, liveStatusPanel, liveHealthItems, positionRows, positionsPanel, dropLabel, countRows, gapRuleLines,
// liveCountsPanel, suppressedCount, createPoller, createRefreshNote. All server text is shown as text nodes.
import { h } from "./dom.js";
import { chartTimeToDataTime, dataTimeToChartTime, formatUtc, trimDecimal } from "./format.js";
import { dataTable, netCell } from "./table.js";

export const RUNS_REFRESH_MS = 30000;
export const OVERVIEW_REFRESH_MS = 30000;
export const ALGO_REFRESH_MS = 60000;
export const REFRESH_FAILED_TEXT = "refresh failed";

const DASH = "-";
const LIVENESS_CLASSES = ["running", "stale", "completed", "failed"];

// The readable name of each drop reason of `live_counts` (the identifiers of engine/live_counts.py).
export const DROP_LABELS = {
  snapshot_only: "snapshot only",
  connection_lost: "connection lost",
  farm_break: "data farm break",
  stalled: "stalled stream",
  rerequest_gap: "re-request gap",
  previous_session: "previous session",
};

export function isLiveRun(run) {
  return Boolean(run) && typeof run === "object" && typeof run.liveness === "string";
}

export function isRunning(run) {
  return isLiveRun(run) && run.liveness === "running";
}

function present(value) {
  return value !== null && value !== undefined && value !== "";
}

// The badge of a live run: its liveness as text, the end reason of an ended run as the tooltip.
export function livenessBadge(run) {
  const known = LIVENESS_CLASSES.includes(run.liveness) ? run.liveness : "unknown";
  return h("span", {
    class: "badge live " + known,
    title: present(run.end_reason) ? "End reason: " + String(run.end_reason) : null,
  }, String(run.liveness));
}

// The "Live status" section of a live run: liveness, last write time and, once ended, the end reason.
export function liveStatusPanel(run) {
  const item = (label, ...value) => h("li", {}, h("span", { class: "muted" }, label + " "), ...value);
  return h("section", { class: "panel", "aria-label": "Live status" },
    h("ul", { class: "status-line" },
      item("Liveness", livenessBadge(run)),
      item("Last write", present(run.last_write_at) ? formatUtc(run.last_write_at) : DASH),
      present(run.end_reason) ? item("End reason", String(run.end_reason)) : null));
}

// A time of the market clock as "YYYY-MM-DD HH:MM:SS" (the API may send a "T"); anything else as given.
function dataTimeText(value) {
  if (!present(value)) return DASH;
  const seconds = dataTimeToChartTime(String(value));
  const shown = seconds === null ? null : chartTimeToDataTime(seconds);
  return shown === null ? String(value) : shown;
}

// The open positions as table rows (the order the server sent). `key` is the 1-based position in the list.
export function positionRows(positions) {
  const list = Array.isArray(positions) ? positions : [];
  return list.map((position, index) => ({ ...(position && typeof position === "object" ? position : {}), key: index + 1 }));
}

function text(value) {
  return present(value) ? String(value) : DASH;
}

// The open-positions section. "Last close" and "Unrealised" columns appear only when some position has them;
// a position without them shows a dash. A missing stop or target is a dash.
export function positionsPanel(positions) {
  const rows = positionRows(positions);
  if (rows.length === 0) {
    return h("section", { class: "panel", "aria-label": "Open positions" }, h("h3", {}, "Open positions"),
      h("p", { class: "empty" }, "No open positions."));
  }
  const columns = [
    { label: "Account", cell: (row) => text(row.account) },
    { label: "Symbol", cell: (row) => text(row.symbol) },
    { label: "Side", cell: (row) => text(row.side) },
    { label: "Quantity", numeric: true, cell: (row) => trimDecimal(row.quantity) },
    { label: "Entry price", numeric: true, cell: (row) => trimDecimal(row.entry_price, 2) },
    { label: "Entry time", cell: (row) => dataTimeText(row.entry_time) },
    { label: "Stop", numeric: true, cell: (row) => trimDecimal(row.stop, 2) },
    { label: "Target", numeric: true, cell: (row) => trimDecimal(row.target, 2) },
  ];
  if (rows.some((row) => present(row.last_close))) {
    columns.push({ label: "Last close", numeric: true, cell: (row) => trimDecimal(row.last_close, 2) });
  }
  if (rows.some((row) => present(row.unrealised))) {
    columns.push({
      label: "Unrealised",
      numeric: true,
      cell: (row) => (present(row.unrealised) ? netCell(row.unrealised) : { content: DASH }),
    });
  }
  const table = dataTable({ columns, rows, caption: "Open positions", captionHidden: true, rowKey: (row) => row.key });
  return h("section", { class: "panel", "aria-label": "Open positions" }, h("h3", {}, "Open positions"), table.element);
}

export function dropLabel(reason) {
  return Object.hasOwn(DROP_LABELS, reason) ? DROP_LABELS[reason] : String(reason);
}

function counted(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

// One row per symbol: minutes fed, late (not fed) and the drops with their readable reasons.
export function countRows(counts) {
  const record = counts && typeof counts === "object" ? counts : {};
  const fed = record.fed && typeof record.fed === "object" ? record.fed : {};
  const late = record.late && typeof record.late === "object" ? record.late : {};
  const dropped = record.dropped && typeof record.dropped === "object" ? record.dropped : {};
  const symbols = [...new Set([...Object.keys(fed), ...Object.keys(late), ...Object.keys(dropped)])].sort();
  return symbols.map((symbol) => {
    const reasons = dropped[symbol] && typeof dropped[symbol] === "object" ? dropped[symbol] : {};
    const drops = Object.entries(reasons).filter(([, n]) => counted(n) > 0)
      .map(([reason, n]) => dropLabel(reason) + " " + counted(n));
    return { symbol, fed: counted(fed[symbol]), late: counted(late[symbol]), dropped: drops.join(", ") || DASH };
  });
}

// The number of refused entries from either shape the API sends: a plain number, or {count, by_algo}.
export function suppressedCount(value) {
  return counted(value && typeof value === "object" ? value.count : value);
}

// The gap-rule sentences, worded like the overview's notes: bars that follow a gap per symbol, entries refused.
export function gapRuleLines(rule) {
  const record = rule && typeof rule === "object" ? rule : {};
  const lines = [];
  const afterGap = record.bars_after_gap && typeof record.bars_after_gap === "object" ? record.bars_after_gap : {};
  const gapped = Object.entries(afterGap).filter(([, n]) => counted(n) > 0);
  if (gapped.length > 0) {
    lines.push("Bars that follow a gap (entries signalled on them were refused): "
      + gapped.map(([symbol, n]) => symbol + " " + counted(n)).join(", ") + ".");
  }
  // `live_counts.gap_rule.suppressed_entries` is a number, `run.gap_rule.suppressed_entries` an object with `count`.
  const refused = suppressedCount(record.suppressed_entries);
  if (refused > 0) {
    lines.push(refused + (refused === 1 ? " entry" : " entries") + " refused on bars that follow a gap.");
  }
  return lines;
}

// The live-counts section: the per-symbol table, the gap-rule sentences and any count the page does not know.
export function liveCountsPanel(counts) {
  const record = counts && typeof counts === "object" ? counts : {};
  const rows = countRows(record);
  const lines = gapRuleLines(record.gap_rule);
  const other = record.other && typeof record.other === "object" ? record.other : {};
  for (const [scope, keyed] of Object.entries(other)) {
    const pairs = keyed && typeof keyed === "object" ? Object.entries(keyed) : [];
    if (pairs.length > 0) lines.push("Other counts (" + scope + "): " + pairs.map(([k, n]) => k + " " + n).join(", ") + ".");
  }
  const parts = [h("h3", {}, "Live counts")];
  if (record.provisional === true) {
    parts.push(h("p", { class: "muted so-far" }, "So far: the run is not finished, these counts are updated while it runs."));
  }
  if (rows.length === 0) {
    parts.push(h("p", { class: "empty" }, "No minutes counted yet."));
  } else {
    const columns = [
      { label: "Symbol", cell: (row) => row.symbol },
      { label: "Minutes fed", numeric: true, cell: (row) => String(row.fed) },
      { label: "Late (not fed)", numeric: true, cell: (row) => String(row.late) },
      { label: "Dropped", cell: (row) => row.dropped },
    ];
    parts.push(dataTable({
      columns, rows, caption: "Live counts per symbol", captionHidden: true, rowKey: (row) => row.symbol,
    }).element);
  }
  if (lines.length > 0) parts.push(h("ul", { class: "notes" }, lines.map((line) => h("li", {}, line))));
  return h("section", { class: "panel", "aria-label": "Live counts" }, ...parts);
}

const LIVE_STATES = ["running", "stale", "idle"];

// The header health strip's live part: [] unless /api/health carried `live_state` (it does only when the
// database has live runs), else one span "live: running" (or stale, idle).
export function liveHealthItems(health) {
  if (!health || typeof health.live_state !== "string") return [];
  const known = LIVE_STATES.includes(health.live_state) ? health.live_state : "";
  return [h("span", { class: ("live-state " + known).trim() }, "live: " + health.live_state)];
}

// A repeating refresh tied to a view. Its stop is registered with ctx.onCleanup at once, so it runs when the
// view is left however the view ended; start() does nothing once the view is no longer current. The timer
// functions are read from globalThis when used (a test replaces them).
//   start()  begin ticking (idempotent); stop()  end it (idempotent); active  whether a timer is set.
// A tick is skipped while the previous tick's promise is still pending.
export function createPoller(ctx, { intervalMs, tick }) {
  let timer = null;
  let busy = false;
  function stop() {
    if (timer === null) return;
    globalThis.clearInterval(timer);
    timer = null;
  }
  ctx.onCleanup(stop);
  function start() {
    if (timer !== null || !ctx.isCurrent()) return;
    timer = globalThis.setInterval(async () => {
      if (!ctx.isCurrent()) {
        stop();
        return;
      }
      if (busy) return;
      busy = true;
      try {
        await tick();
      } catch (err) {
        console.error("refresh failed", err);
      } finally {
        busy = false;
      }
    }, intervalMs);
  }
  return { start, stop, get active() { return timer !== null; } };
}

// The one "refresh failed" note of a view: fail() / ok() set whether it is shown, `element` is the node to
// place when `failed` is true. The view keeps its last good render; this is the only sign of the failure.
export function createRefreshNote() {
  const element = h("p", { class: "refresh-note muted", role: "status" }, REFRESH_FAILED_TEXT);
  const state = { failed: false, element };
  return {
    element,
    get failed() { return state.failed; },
    fail() { state.failed = true; },
    ok() { state.failed = false; },
  };
}
