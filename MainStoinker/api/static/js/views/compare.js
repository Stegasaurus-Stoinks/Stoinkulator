// The compare view: two stored runs side by side. Follows the view contract in main.js.
//
// Exports: render, METRICS, decimalDifference, integerDifference, percentPointDifference, headerRows,
// comparisonWarnings, buildStatRows, equityNote. Everything but render is pure (no DOM, no chart library)
// so node can test it.
//
// The route gives 2 to 5 run ids (routes.js); this view shows the first two, called A and B, and says so
// plainly when more were given. The statistics come from GET /api/compare?runs=a,b (the same figures as
// `report`), the account equity of each run from GET /api/runs/{id}/equity.
//
// Differences are computed exactly: money and win-rate strings are parsed to BigInt integers and
// subtracted, never through floating point; the shown figure is then rounded half away from zero by the
// same helpers as every other figure on the page (format.js).
import { ApiError, describeError, getJson, isAbortError } from "../api.js";
import { createEquityChart } from "../charts/equity.js";
import { errorPanel, h, loadingPanel, viewHeading } from "../dom.js";
import {
  formatDuration, formatMoney, formatNet, formatPercent, formatUtc, netClass, shortVersion,
} from "../format.js";
import { runHash, runsHash } from "../routes.js";
import { dataTable } from "../table.js";
import { displayStatus, runDurationSeconds, settingRows, statusNotes } from "./overview.js";

const DASH = "-";
const LEGACY_NOTE = "legacy (bar-close fills, no commission)";
const DECIMAL_RE = /^([+-]?)(\d+)(?:\.(\d*))?$/;
const INTEGER_RE = /^[+-]?\d+$/;

// A plain decimal string (or a number that prints as one) as {units, scale}: value = units / 10^scale.
// Anything else (empty, null, exponent form, text) gives null.
function parseDecimal(value) {
  if (value === null || value === undefined || value === "") return null;
  const m = DECIMAL_RE.exec(String(value).trim());
  if (!m) return null;
  const fraction = m[3] || "";
  const units = BigInt(m[2] + fraction);
  return { units: m[1] === "-" ? -units : units, scale: fraction.length };
}

// b - a as an exact decimal string with as many decimals as the longer input ("12.3450" - "2.0000" is
// "10.3450"; "5.0000" - "5.0000" is "0.0000", never "-0.0000"). null when either side is missing or is
// not a plain decimal.
export function decimalDifference(a, b) {
  const pa = parseDecimal(a);
  const pb = parseDecimal(b);
  if (!pa || !pb) return null;
  const scale = Math.max(pa.scale, pb.scale);
  const diff = pb.units * 10n ** BigInt(scale - pb.scale) - pa.units * 10n ** BigInt(scale - pa.scale);
  const digits = (diff < 0n ? -diff : diff).toString().padStart(scale + 1, "0");
  const cut = digits.length - scale;
  return (diff < 0n ? "-" : "") + digits.slice(0, cut) + (scale > 0 ? "." + digits.slice(cut) : "");
}

function parseInteger(value) {
  if (typeof value === "number") return Number.isSafeInteger(value) ? BigInt(value) : null;
  if (typeof value === "string" && INTEGER_RE.test(value.trim())) return BigInt(value.trim());
  return null;
}

// b - a for two integers (numbers or integer strings) as an integer string, or null.
export function integerDifference(a, b) {
  const x = parseInteger(a);
  const y = parseInteger(b);
  return x === null || y === null ? null : (y - x).toString();
}

// The difference of two win-rate fractions ("0.3750" and "0.2500") in percentage points, rounded to
// `digits` decimals half away from zero exactly like formatPercent: "0.3750" - "0.2500" gives "12.5".
// null when either side is missing (a run without trades has no win rate).
export function percentPointDifference(a, b, digits = 1) {
  const diff = decimalDifference(a, b);
  return diff === null ? null : formatPercent(diff, digits);
}

function signed(text) {
  if (text === null || text === undefined || text === DASH) return DASH;
  return text.startsWith("-") || /^0(\.0*)?$/.test(text) ? text : "+" + text;
}

function text(value) {
  return value === null || value === undefined || value === "" ? DASH : String(value);
}

// --- the header table ---------------------------------------------------------------------------

function fillText(run) {
  return run.fills === "legacy" ? LEGACY_NOTE : text(run.fills);
}

function durationText(run) {
  const seconds = runDurationSeconds(run);
  return seconds === null ? DASH : formatDuration(seconds);
}

function sameDecimal(a, b) {
  const aMissing = parseDecimal(a) === null;
  const bMissing = parseDecimal(b) === null;
  if (aMissing || bMissing) return aMissing === bMissing && text(a) === text(b);
  return /^0(\.0*)?$/.test(decimalDifference(a, b));
}

function costOf(run, field) {
  return run.costs && typeof run.costs === "object" ? run.costs[field] : null;
}

function normalTime(value) {
  const chart = value === null || value === undefined ? null : parseDataSeconds(value);
  return chart === null ? text(value) : String(chart);
}

function parseDataSeconds(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(value));
  if (!m) return null;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6])) / 1000;
}

function symbolsKey(run) {
  return Array.isArray(run.symbols) ? [...run.symbols].sort().join(",") : "";
}

// One entry per line of the header table. `shown(run)` is the text, `same(a, b)` whether the two runs agree,
// `unfair` the kind of difference that makes a comparison unfair (a warning), if any.
const HEADER_FIELDS = [
  { key: "status", label: "Status", shown: (r) => displayStatus(r), same: (a, b) => displayStatus(a) === displayStatus(b) },
  { key: "kind", label: "Kind", shown: (r) => text(r.kind), same: (a, b) => text(a.kind) === text(b.kind) },
  { key: "fills", label: "Fill model", shown: fillText, same: (a, b) => text(a.fills) === text(b.fills), unfair: "fills" },
  { key: "started", label: "Start (UTC)", shown: (r) => formatUtc(r.started_at), same: (a, b) => formatUtc(a.started_at) === formatUtc(b.started_at) },
  { key: "duration", label: "Duration", shown: durationText, same: (a, b) => durationText(a) === durationText(b) },
  { key: "version", label: "Code version", shown: (r) => shortVersion(r.code_version), same: (a, b) => text(a.code_version) === text(b.code_version) },
  { key: "capital", label: "Capital per algo", shown: (r) => settingValue(r, "Capital per algo"), same: (a, b) => sameDecimal(a.capital, b.capital), unfair: "capital" },
  { key: "commission", label: "Commission per share", shown: (r) => settingValue(r, "Commission per share"), same: (a, b) => sameDecimal(costOf(a, "commission_per_share"), costOf(b, "commission_per_share")), unfair: "costs" },
  { key: "minimum", label: "Minimum per order", shown: (r) => settingValue(r, "Minimum per order"), same: (a, b) => sameDecimal(costOf(a, "commission_minimum"), costOf(b, "commission_minimum")), unfair: "costs" },
  { key: "slippage", label: "Slippage per share", shown: (r) => settingValue(r, "Slippage per share"), same: (a, b) => sameDecimal(costOf(a, "slippage_per_share"), costOf(b, "slippage_per_share")), unfair: "costs" },
  { key: "range", label: "Bar range", shown: (r) => settingValue(r, "Bar range"), same: (a, b) => normalTime(a.first_bar) === normalTime(b.first_bar) && normalTime(a.last_bar) === normalTime(b.last_bar), unfair: "range" },
  { key: "symbols", label: "Symbols", shown: (r) => settingValue(r, "Symbols"), same: (a, b) => symbolsKey(a) === symbolsKey(b), unfair: "symbols" },
  { key: "folder", label: "Data folder", shown: (r) => settingValue(r, "Data folder"), same: (a, b) => text(a.data_directory) === text(b.data_directory) },
];

function settingValue(run, label) {
  const found = settingRows(run).find((row) => row[0] === label);
  return found ? found[1] : DASH;
}

// The lines of the header table: [{key, label, a, b, differs, unfair}], `a` and `b` being display text.
export function headerRows(runA, runB) {
  return HEADER_FIELDS.map((field) => ({
    key: field.key,
    label: field.label,
    a: field.shown(runA),
    b: field.shown(runB),
    differs: !field.same(runA, runB),
    unfair: field.unfair || null,
  }));
}

// Plain-language warnings, one per kind of difference that makes the comparison unfair (fill model,
// capital, costs, bar range, symbols), then one for each run that did not complete.
export function comparisonWarnings(runA, runB) {
  const rows = headerRows(runA, runB);
  const differing = (kind) => rows.filter((row) => row.differs && row.unfair === kind);
  const pair = (key) => {
    const row = rows.find((r) => r.key === key);
    return "run A: " + row.a + ", run B: " + row.b;
  };
  const warnings = [];
  if (differing("fills").length > 0) {
    warnings.push("The fill models differ (" + pair("fills") + "). Prices, costs and trade counts are not comparable.");
  }
  if (differing("capital").length > 0) {
    warnings.push("The capital per algo differs (" + pair("capital") + "). Final equity and drawdown are on different "
      + "scales; the chart shows profit and loss instead.");
  }
  if (differing("costs").length > 0) {
    warnings.push("The costs differ (commission, minimum or slippage), so net and commission are not comparable.");
  }
  if (differing("range").length > 0) {
    warnings.push("The bar ranges differ (" + pair("range") + "). The runs covered different periods.");
  }
  if (differing("symbols").length > 0) {
    warnings.push("The symbols differ (" + pair("symbols") + "). The runs traded different markets.");
  }
  for (const [name, run] of [["A", runA], ["B", runB]]) {
    const note = statusNotes(run);
    if (note) warnings.push("Run " + name + ": " + note);
  }
  return warnings;
}

// --- the statistics table -----------------------------------------------------------------------

// The metric groups, in the order of `report`. type: "int" (a count), "money" (a 4-decimal string) or
// "percent" (a win-rate fraction string); colour: the shown value carries the pos/neg class.
export const METRICS = [
  { key: "trades", label: "Trades", field: "trade_count", type: "int" },
  { key: "wins", label: "Wins", field: "wins", type: "int" },
  { key: "winPercent", label: "Win %", field: "win_rate", type: "percent" },
  { key: "net", label: "Net", field: "net", type: "money", sign: true, colour: true },
  { key: "commission", label: "Commission", field: "commission", type: "money" },
  { key: "avgWin", label: "Avg win", field: "avg_win", type: "money" },
  { key: "avgLoss", label: "Avg loss", field: "avg_loss", type: "money" },
  { key: "maxDrawdown", label: "Max drawdown", field: "max_drawdown", type: "money" },
  { key: "finalEquity", label: "Final equity", field: "final_equity", type: "money", colour: false },
];

function runCell(metric, stats) {
  const value = stats ? stats[metric.field] : null;
  if (value === null || value === undefined || value === "") return { text: DASH, className: "" };
  if (metric.type === "int") return { text: String(value), className: "" };
  if (metric.type === "percent") return { text: formatPercent(value), className: "" };
  if (metric.sign) return { text: formatNet(value), className: netClass(value) };
  return { text: formatMoney(value), className: "" };
}

function diffCell(metric, statsA, statsB) {
  if (!statsA || !statsB) return { text: DASH, className: "" };
  const a = statsA[metric.field];
  const b = statsB[metric.field];
  if (metric.type === "int") return { text: signed(integerDifference(a, b)), className: "" };
  if (metric.type === "percent") return { text: signed(percentPointDifference(a, b)), className: "" };
  const diff = decimalDifference(a, b);
  if (diff === null) return { text: DASH, className: "" };
  return { text: formatNet(diff), className: metric.sign || metric.key === "finalEquity" ? netClass(diff) : "" };
}

function recordOf(value) {
  return value && typeof value === "object" ? value : null;
}

function runLabel(name, run) {
  const status = run ? displayStatus(run) : "completed";
  return "Run " + name + (status === "completed" ? "" : " (" + status + ")");
}

// The rows of the statistics table. `compare` is the /api/compare document, `ids` the two run ids asked
// for (A then B), `runs` the two headers (compare.runs; used only to label a failed or incomplete run).
// For every algo (the algos of run A in its order, then those only in run B) and then TOTAL there are
// three rows: run A, run B and B - A. Each row is {block, total, kind: "a" | "b" | "diff", label, cells},
// cells being {metricKey: {text, className}}. A run without the algo has "-" everywhere, and so has the
// difference row (no difference without both sides).
export function buildStatRows(compare, ids, runs = []) {
  const algos = recordOf(compare && compare.algos) || {};
  const [idA, idB] = ids.map(String);
  const names = Object.keys(algos);
  const has = (name, id) => recordOf(algos[name]) !== null && recordOf(algos[name][id]) !== null;
  const order = [...names.filter((n) => has(n, idA)), ...names.filter((n) => !has(n, idA))];
  const blocks = order.map((name) => ({
    block: name, total: false,
    a: recordOf(algos[name]) ? recordOf(algos[name][idA]) : null,
    b: recordOf(algos[name]) ? recordOf(algos[name][idB]) : null,
  }));
  const totals = recordOf(compare && compare.total) || {};
  blocks.push({ block: "TOTAL", total: true, a: recordOf(totals[idA]), b: recordOf(totals[idB]) });
  const rows = [];
  for (const { block, total, a, b } of blocks) {
    const make = (kind, label, pick) => {
      const cells = {};
      for (const metric of METRICS) cells[metric.key] = pick(metric);
      rows.push({ block, total, kind, label, cells });
    };
    make("a", runLabel("A", runs[0]), (m) => runCell(m, a));
    make("b", runLabel("B", runs[1]), (m) => runCell(m, b));
    make("diff", "B - A", (m) => diffCell(m, a, b));
  }
  return rows;
}

// --- the page -----------------------------------------------------------------------------------

const UNEXPECTED = () => new ApiError(200, "The server sent an unexpected response.", "unexpected");

function validCompare(doc) {
  return Boolean(doc) && Array.isArray(doc.runs) && doc.runs.length === 2 && doc.runs.every(recordOf)
    && recordOf(doc.algos) !== null && recordOf(doc.total) !== null;
}

function validEquity(doc) {
  return Boolean(doc) && Array.isArray(doc.points);
}

// The sentence under the chart.
export function equityNote(equities) {
  const thinned = [];
  equities.forEach((equity, index) => {
    if (!equity) return;
    const stored = Number(equity.stored_points);
    const shown = equity.points.length;
    if (Number.isFinite(stored) && stored > shown) {
      thinned.push("Run " + "AB"[index] + ": " + shown + " of " + stored + " stored points.");
    }
  });
  return "Profit and loss from each run's starting capital (equity minus capital), so runs with different capital "
    + "share one scale. All algos of a run together; times are data time, as stored."
    + (thinned.length > 0 ? " Showing " + thinned.join(" ") : "");
}

function headerTable(runA, runB, ids) {
  const rows = headerRows(runA, runB);
  const mark = (row) => (row.differs ? "differs" : "");
  const columns = [
    { label: "Setting", cell: (row) => ({ content: row.label, className: mark(row) }) },
    { label: runLabel("A", runA) + ", run " + ids[0], cell: (row) => ({ content: row.a, className: mark(row) }) },
    { label: runLabel("B", runB) + ", run " + ids[1], cell: (row) => ({ content: row.b, className: mark(row) }) },
    {
      label: "Same or differs",
      cell: (row) => ({ content: row.differs ? "Differs" : "Same", className: mark(row) }),
    },
  ];
  return dataTable({ columns, rows, caption: "The two runs side by side", captionHidden: true });
}

function statsTable(rows) {
  const columns = [
    {
      label: "Algo",
      cell: (row) => ({ content: row.total ? h("strong", {}, "TOTAL") : row.block, className: classes(row) }),
    },
    { label: "Run", cell: (row) => ({ content: row.label, className: classes(row) }) },
    ...METRICS.map((metric) => ({
      label: metric.label,
      numeric: true,
      cell: (row) => {
        const cell = row.cells[metric.key];
        return { content: cell.text, className: (classes(row) + " " + cell.className).trim() };
      },
    })),
  ];
  return dataTable({ columns, rows, caption: "Per-algo results of the two runs", captionHidden: true });
}

function classes(row) {
  return ["cmp-" + row.kind, row.kind === "a" ? "cmp-start" : "", row.total ? "total" : ""].filter(Boolean).join(" ");
}

function namedError(what, err) {
  return h("div", { class: "panel error", role: "alert" }, what + " could not be loaded. " + describeError(err));
}

export async function render(container, params, ctx) {
  const all = params.runIds;
  const ids = all.slice(0, 2);
  const title = "Compare runs " + ids[0] + " and " + ids[1];
  const top = () => [
    viewHeading(title),
    h("p", { class: "compare-links" },
      h("a", { href: runHash(ids[0]) }, "Run " + ids[0] + " overview"), " ",
      h("a", { href: runHash(ids[1]) }, "Run " + ids[1] + " overview"), " ",
      h("a", { href: runsHash() }, "Back to the runs list")),
    all.length > 2 ? h("p", { class: "panel compare-note", role: "note" },
      "Only two runs are compared at a time: run " + ids[0] + " (A) and run " + ids[1] + " (B), the first two of "
      + all.length + ". Left out: " + all.slice(2).map((id) => "run " + id).join(", ") + ".") : null,
  ].filter(Boolean);
  container.replaceChildren(...top(), loadingPanel("runs " + ids[0] + " and " + ids[1]));

  const options = { signal: ctx.signal };
  const [docResult, equityA, equityB] = await Promise.allSettled([
    getJson("/api/compare", { runs: ids.join(",") }, options),
    getJson("/api/runs/" + ids[0] + "/equity", {}, options),
    getJson("/api/runs/" + ids[1] + "/equity", {}, options),
  ]);
  if (!ctx.isCurrent()) return;
  for (const result of [docResult, equityA, equityB]) {
    if (result.status === "rejected" && isAbortError(result.reason)) return;
  }

  // Without the compare document there is nothing to show but why: one error panel.
  let doc = null;
  let docError = null;
  if (docResult.status === "fulfilled") {
    if (validCompare(docResult.value)) doc = docResult.value;
    else docError = UNEXPECTED();
  } else {
    docError = docResult.reason;
  }
  if (!doc) {
    container.replaceChildren(...top(), errorPanel(docError));
    return;
  }

  const [runA, runB] = doc.runs;
  const parts = top();

  const warnings = comparisonWarnings(runA, runB);
  parts.push(h("section", { class: "panel", "aria-label": "Differences that matter" },
    h("h3", {}, "Before you compare"),
    warnings.length > 0
      ? h("ul", { class: "warnings compare-warnings" }, warnings.map((w) => h("li", {}, w)))
      : h("p", { class: "muted" },
        "The runs used the same fill model, capital, costs, bar range and symbols.")));
  parts.push(h("section", { "aria-label": "The two runs" }, h("h3", {}, "The two runs"),
    headerTable(runA, runB, ids).element));

  const labels = [runA, runB].map((run, index) => "Run " + "AB"[index] + " (" + text(run.fills) + ")");
  const equities = [equityA, equityB].map((result) => (
    result.status === "fulfilled" && validEquity(result.value) ? result.value : null));
  const chartBox = h("div", { class: "chart-box" });
  const drawable = equities.some(Boolean);
  const equityErrors = [equityA, equityB].map((result, index) => {
    if (equities[index]) return null;
    const err = result.status === "rejected" ? result.reason : UNEXPECTED();
    return namedError("The equity of run " + "AB"[index] + " (run " + ids[index] + ")", err);
  }).filter(Boolean);
  parts.push(h("section", { class: "panel", "aria-label": "Account equity" },
    h("h3", {}, "Account equity and drawdown"),
    equityErrors,
    drawable ? chartBox : null,
    drawable ? h("p", { class: "muted chart-caption" }, equityNote(equities)) : null));

  parts.push(h("section", { "aria-label": "Statistics" }, h("h3", {}, "Statistics"),
    statsTable(buildStatRows(doc, ids, [runA, runB])).element,
    h("p", { class: "muted chart-caption" },
      "The B - A rows are exact differences of the stored figures. Win % differs in percentage points. "
      + "A dash means the run has no such algo or no figure.")));

  container.replaceChildren(...parts);

  if (drawable) {
    try {
      const chart = createEquityChart(chartBox, { ctx, relative: true }); // registers its own disposal at once
      chart.setSeries(equities.map((equity, index) => (equity
        ? { label: labels[index], points: equity.points, capital: equity.capital }
        : null)).filter(Boolean));
    } catch (err) {
      chartBox.replaceChildren(errorPanel(err));
    }
  }
}
