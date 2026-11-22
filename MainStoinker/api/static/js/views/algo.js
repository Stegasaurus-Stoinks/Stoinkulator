// The algo detail view: one algo of one run. Status, summary numbers, the algo's equity curve with its
// drawdown, a candle chart of the algo's symbol (one day at a time, with a day control) carrying the entry
// and exit of each trade, and the sortable trade table. Selecting a trade marks its row and its markers and
// scrolls the chart to it, loading that trade's day first when another day is shown. Follows the view
// contract in main.js.
//
// Exports: render, TRADE_COLUMNS, dayOfTime, shiftDay, stepDay, initialDay, dayWindow, barsState,
// buildTradeRows, selectLabel, summaryItems, trimDecimal. Everything but render is pure (no DOM, no
// network) so node can test it.
import { ApiError, getJson, isAbortError } from "../api.js";
import { createCandleChart } from "../charts/candles.js";
import { createEquityChart } from "../charts/equity.js";
import { errorPanel, h, loadingPanel, viewHeading } from "../dom.js";
import { chartTimeToDataTime, dataTimeToChartTime, netClass } from "../format.js";
import { runHash, runsHash } from "../routes.js";
import { dataTable, netCell } from "../table.js";
import { ALGO_COLUMNS, displayStatus, statusNotes } from "./overview.js";

const DASH = "-";

// "YYYY-MM-DD" of a naive market time (the bar range arrives with a "T"), or null.
export function dayOfTime(text) {
  const seconds = dataTimeToChartTime(text);
  const data = seconds === null ? null : chartTimeToDataTime(seconds);
  return data === null ? null : data.slice(0, 10);
}

const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

// The calendar day `delta` days after `day` ("YYYY-MM-DD"), by Date.UTC arithmetic so month and year ends
// roll over and no time zone is involved. Null when `day` is not a valid day.
export function shiftDay(day, delta) {
  const m = typeof day === "string" ? DAY_RE.exec(day) : null;
  if (!m) return null;
  const seconds = dataTimeToChartTime(day + " 00:00:00");
  if (seconds === null) return null;
  return chartTimeToDataTime(seconds + delta * 86400).slice(0, 10);
}

// The day one step (-1 or +1) from `day`, or null when that step leaves the run's bar range [first, last]
// (both "YYYY-MM-DD", either may be null: that side is unbounded). "YYYY-MM-DD" strings compare in date order.
export function stepDay(day, delta, first, last) {
  const next = shiftDay(day, delta);
  if (next === null) return null;
  if (first && next < first) return null;
  if (last && next > last) return null;
  return next;
}

// The day the chart opens on: the day of the algo's first trade (earliest entry), else the day of the run's
// last bar, else null. `rows` are buildTradeRows' rows.
export function initialDay(rows, lastBar) {
  let best = null;
  for (const row of rows) {
    if (row.entryChart !== null && (best === null || row.entryChart < best.entryChart)) best = row;
  }
  return best ? best.entryDay : dayOfTime(lastBar);
}

// The bars request of a day: the whole calendar day, naive data time.
export function dayWindow(day) {
  return { start: day + " 00:00:00", end: day + " 23:59:59" };
}

// What the bars route's answer means for the chart: {candles, note, kind}. `candles` says whether to draw
// the bars; `note` is the sentence to show above the chart ("" for none).
export function barsState(data, symbol) {
  const name = symbol || "this symbol";
  const bars = data && Array.isArray(data.bars) ? data.bars : [];
  if (!data || data.available !== true) {
    const reason = data ? data.reason : null;
    if (reason === "missing") {
      return {
        kind: "missing", candles: false,
        note: "No candles: the data folder or the bar file for " + name + " was not found, so the bars cannot "
          + "be shown. The trades are still listed below.",
      };
    }
    if (reason === "changed") {
      return {
        kind: "changed", candles: false,
        note: "No candles: the data file for " + name + " has changed since the run, so its bars may no longer "
          + "match the trades. The trades are still listed below.",
      };
    }
    return {
      kind: "unavailable", candles: false,
      note: "No candles: the server could not provide the bars for " + name + ". The trades are still listed "
        + "below.",
    };
  }
  const unverified = data.reason === "unverified"
    ? "These bars could not be checked against the data file the run used (no checksum was stored)."
    : "";
  if (bars.length === 0) {
    return { kind: "empty", candles: false, note: "No bars for " + name + " on this day." };
  }
  return { kind: unverified ? "unverified" : "ok", candles: true, note: unverified };
}

// "100.0000" -> "100", "191.2350" -> "191.235", with at least `places` decimals kept ("190.0000" -> "190.00"
// at places 2). Text that is not a plain decimal is returned as given, "-" when missing.
export function trimDecimal(value, places = 0) {
  if (value === null || value === undefined || value === "") return DASH;
  const m = /^([+-]?\d+)(?:\.(\d*))?$/.exec(String(value).trim());
  if (!m) return String(value);
  let fraction = m[2] || "";
  while (fraction.length > places && fraction.endsWith("0")) fraction = fraction.slice(0, -1);
  while (fraction.length < places) fraction += "0";
  return fraction ? m[1] + "." + fraction : m[1];
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// The trades route's trades as table rows. `key` and `number` are the 1-based position in the order the
// server sent (unique, stable across sorting); the raw fields are kept; entryChart, exitChart (chart
// seconds) and entryDay are derived from the naive data times.
export function buildTradeRows(trades) {
  const list = Array.isArray(trades) ? trades : [];
  return list.map((trade, index) => {
    const entryChart = dataTimeToChartTime(trade.entry_time);
    return {
      ...trade,
      key: index + 1,
      number: index + 1,
      entryChart,
      exitChart: dataTimeToChartTime(trade.exit_time),
      entryDay: entryChart === null ? null : dayOfTime(trade.entry_time),
    };
  });
}

function timeText(value) {
  return value === null || value === undefined || value === "" ? DASH : String(value);
}

export const TRADE_COLUMNS = [
  { label: "#", numeric: true, cell: (row) => String(row.number), sortKey: (row) => row.number },
  { label: "Side", cell: (row) => timeText(row.side), sortKey: (row) => row.side ?? null },
  { label: "Quantity", numeric: true, cell: (row) => trimDecimal(row.quantity), sortKey: (row) => numberOrNull(row.quantity) },
  { label: "Entry time", cell: (row) => timeText(row.entry_time), sortKey: (row) => row.entryChart },
  { label: "Entry price", numeric: true, cell: (row) => trimDecimal(row.entry_price, 2), sortKey: (row) => numberOrNull(row.entry_price) },
  { label: "Exit time", cell: (row) => timeText(row.exit_time), sortKey: (row) => row.exitChart },
  { label: "Exit price", numeric: true, cell: (row) => trimDecimal(row.exit_price, 2), sortKey: (row) => numberOrNull(row.exit_price) },
  { label: "Net", numeric: true, cell: (row) => netCell(row.net), sortKey: (row) => numberOrNull(row.net) },
  { label: "Exit reason", cell: (row) => timeText(row.exit_reason), sortKey: (row) => row.exit_reason ?? null },
  { label: "Synthetic", cell: (row) => (row.synthetic ? "yes" : "no"), sortKey: (row) => (row.synthetic ? 1 : 0) },
];

// The label of a row's select button: "Select trade 3, long, entered 2024-01-02 09:35:00".
export function selectLabel(row) {
  return "Select trade " + row.number + ", " + timeText(row.side) + ", entered " + timeText(row.entry_time);
}

// The algo's summary figures from its `accounts` entry of the run document, formatted like the overview's
// per-algo table: [{label, text, className}].
export function summaryItems(stats) {
  const record = stats && typeof stats === "object" ? stats : {};
  return ALGO_COLUMNS.map((column) => ({
    label: column.label,
    text: column.show(record[column.field]),
    className: column.net ? netClass(record[column.field]) : "",
  }));
}

function backLinks(runId) {
  return h("p", { class: "back-links" },
    h("a", { href: runHash(runId) }, "Back to run " + runId),
    h("a", { href: runsHash() }, "Back to the runs list"));
}

function unexpected() {
  return new ApiError(200, "The server sent an unexpected response.", "unexpected");
}

function validDocument(value) {
  return Boolean(value) && typeof value === "object" && value.run && typeof value.run === "object"
    && value.accounts && typeof value.accounts === "object";
}

function statusPanel(run) {
  const status = displayStatus(run);
  const warning = statusNotes(run);
  return h("section", { class: "panel", "aria-label": "Run status" },
    h("ul", { class: "status-line" },
      h("li", {}, h("span", { class: "muted" }, "Run status "), h("span", { class: "badge " + status }, status))),
    warning ? h("p", { class: "warning", role: "note" }, warning) : null);
}

function summaryPanel(stats) {
  const list = h("dl", { class: "params summary" });
  for (const item of summaryItems(stats)) {
    list.append(h("dt", {}, item.label), h("dd", { class: item.className }, item.text));
  }
  const symbol = [stats.symbol, stats.algo, stats.mode].filter(Boolean).join(" / ");
  return h("section", { class: "panel", "aria-label": "Algo summary" }, h("h3", {}, "Summary"),
    symbol ? h("p", { class: "muted" }, symbol) : null, list);
}

function equityPanel(box, equity) {
  const stored = Number(equity.stored_points);
  const shown = Array.isArray(equity.points) ? equity.points.length : 0;
  const thinned = Number.isFinite(stored) && stored > shown
    ? " Showing " + shown + " of " + stored + " stored points."
    : "";
  return h("section", { class: "panel", "aria-label": "Algo equity" },
    h("h3", {}, "Equity and drawdown"), box,
    h("p", { class: "muted chart-caption" }, "This algo only; times are data time, as stored." + thinned));
}

export async function render(container, params, ctx) {
  const runId = params.runId;
  const idname = params.idname;
  const heading = () => viewHeading(idname + ", run " + runId);
  container.replaceChildren(heading(), backLinks(runId), loadingPanel("algo " + idname));

  const options = { signal: ctx.signal };
  const [docResult, tradesResult, equityResult] = await Promise.allSettled([
    getJson("/api/runs/" + runId, {}, options),
    getJson("/api/runs/" + runId + "/trades", { algo: idname }, options),
    getJson("/api/runs/" + runId + "/equity", { algo: idname }, options),
  ]);
  if (!ctx.isCurrent()) return;
  for (const result of [docResult, tradesResult, equityResult]) {
    if (result.status === "rejected" && isAbortError(result.reason)) return;
  }

  // Without the run document there is nothing to show but why: one error panel (the trades and equity
  // requests fail the same way for an unknown run or an unreachable server, so they are not repeated).
  let doc = null;
  let docError = null;
  if (docResult.status === "fulfilled") {
    if (validDocument(docResult.value)) doc = docResult.value;
    else docError = unexpected();
  } else {
    docError = docResult.reason;
  }
  if (!doc) {
    container.replaceChildren(heading(), backLinks(runId), errorPanel(docError));
    return;
  }

  const stats = Object.hasOwn(doc.accounts, idname) ? doc.accounts[idname] : null;
  if (!stats || typeof stats !== "object") {
    container.replaceChildren(heading(), backLinks(runId),
      h("div", { class: "panel", role: "status" },
        h("p", {}, "Run " + runId + " has no algo named \"" + idname + "\"."),
        h("p", {}, h("a", { href: runHash(runId) }, "Back to run " + runId + " to see its algos"))));
    return;
  }

  const run = doc.run;
  const symbol = typeof stats.symbol === "string" && stats.symbol ? stats.symbol : "";
  const firstDay = dayOfTime(run.first_bar);
  const lastDay = dayOfTime(run.last_bar);
  const parts = [heading(), backLinks(runId), statusPanel(run), summaryPanel(stats)];

  // The equity curve.
  let equityBox = null;
  let equity = null;
  if (equityResult.status === "fulfilled") {
    const value = equityResult.value;
    if (value && Array.isArray(value.points)) {
      equity = value;
      equityBox = h("div", { class: "chart-box" });
      parts.push(equityPanel(equityBox, equity));
    } else {
      parts.push(errorPanel(unexpected()));
    }
  } else {
    parts.push(errorPanel(equityResult.reason));
  }

  // The trades.
  let rows = [];
  let tradesFailed = false;
  if (tradesResult.status === "fulfilled" && tradesResult.value && Array.isArray(tradesResult.value.trades)) {
    rows = buildTradeRows(tradesResult.value.trades);
  } else {
    tradesFailed = true;
  }
  const rowsByKey = new Map(rows.map((row) => [String(row.key), row]));

  // The candle chart and its day control.
  const dayLabel = h("span", { class: "day-label", role: "status" });
  const previousButton = h("button", { type: "button", onclick: () => step(-1) }, "Previous day");
  const nextButton = h("button", { type: "button", onclick: () => step(1) }, "Next day");
  const barsError = h("div", { class: "bars-error" });
  const candleBox = h("div", { class: "chart-box" });
  const candleParts = [h("h3", {}, "Price and trades"),
    symbol
      ? h("div", { class: "toolbar day-control", role: "group", "aria-label": "Day shown" },
        previousButton, dayLabel, nextButton)
      : h("p", { class: "muted" }, "This algo has no symbol recorded, so there are no candles to show."),
    barsError, candleBox];
  if (symbol) {
    candleParts.push(h("p", { class: "muted chart-caption" },
      symbol + ", 1-minute bars, one day at a time; times are data time, as stored."));
  }
  parts.push(h("section", { class: "panel", "aria-label": "Price chart" }, ...candleParts));

  // The trade table.
  let table = null;
  if (tradesFailed) {
    parts.push(h("section", { "aria-label": "Trades" }, h("h3", {}, "Trades"),
      errorPanel(tradesResult.status === "rejected" ? tradesResult.reason : unexpected())));
  } else if (rows.length === 0) {
    parts.push(h("section", { "aria-label": "Trades" }, h("h3", {}, "Trades"),
      h("p", { class: "panel empty" }, "This algo made no trades in this run.")));
  } else {
    table = dataTable({
      columns: TRADE_COLUMNS, rows, caption: "Trades of " + idname, captionHidden: true,
      sort: { column: 0, direction: "asc" }, rowKey: (row) => row.key, selectedKey: null,
      onSelect: (row) => select(row.key), selectLabel,
    });
    parts.push(h("section", { "aria-label": "Trades" }, h("h3", {}, "Trades"), table.element));
  }

  container.replaceChildren(...parts);

  if (equityBox) {
    try {
      const chart = createEquityChart(equityBox, { ctx }); // registers its own disposal at once
      chart.setSeries([{ label: idname, points: equity.points, capital: equity.capital }]);
    } catch (err) {
      equityBox.replaceChildren(errorPanel(err));
    }
  }

  let candles = null;
  if (symbol) {
    try {
      candles = createCandleChart(candleBox, { ctx }); // registers its own disposal at once
    } catch (err) {
      candleBox.replaceChildren(errorPanel(err));
    }
  }

  let day = initialDay(rows, run.last_bar);
  let selectedKey = null;
  let request = null;

  function paintDay() {
    dayLabel.textContent = day ? day + " (data time)" : "no day";
    previousButton.disabled = !day || stepDay(day, -1, firstDay, lastDay) === null;
    nextButton.disabled = !day || stepDay(day, 1, firstDay, lastDay) === null;
  }

  function showError(err) {
    barsError.replaceChildren(errorPanel(err));
  }

  function selectedRow() {
    return selectedKey === null ? null : rowsByKey.get(String(selectedKey)) || null;
  }

  function drawTrades() {
    candles.setTrades(rows, selectedKey);
    const row = selectedRow();
    if (row && row.entryDay === day && row.entryChart !== null) candles.focusTime(row.entryChart);
  }

  // Latest wins: a newer day aborts the older request, and only the newest may draw.
  async function loadDay(next) {
    if (!candles) return;
    if (request) request.abort();
    const mine = request = ctx.child();
    day = next;
    paintDay();
    barsError.replaceChildren();
    candles.setNote("Loading bars...");
    try {
      const window = dayWindow(day);
      const data = await getJson("/api/runs/" + runId + "/bars",
        { symbol, start: window.start, end: window.end }, { signal: mine.signal });
      if (!mine.isCurrent()) return;
      const state = barsState(data, symbol);
      candles.setBars(state.candles ? data.bars : []);
      candles.setNote(state.note);
      drawTrades();
    } catch (err) {
      if (isAbortError(err) || !mine.isCurrent()) return;
      candles.setBars([]);
      candles.setNote("");
      showError(err);
    }
  }

  function step(delta) {
    const next = day ? stepDay(day, delta, firstDay, lastDay) : null;
    if (next !== null) loadDay(next);
  }

  function select(key) {
    const row = rowsByKey.get(String(key));
    if (!row) return;
    selectedKey = row.key;
    if (table) table.update({ selectedKey });
    if (!candles) return;
    if (row.entryDay !== null && row.entryDay !== day) loadDay(row.entryDay); // draws and scrolls when it lands
    else drawTrades();
  }

  paintDay();
  if (candles) {
    if (day) {
      await loadDay(day);
    } else {
      candles.setBars([]);
      candles.setNote("This run stored no bars, so there is no day to show.");
    }
  }
}
