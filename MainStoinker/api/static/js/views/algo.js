// The algo detail view: one algo of one run. Status, summary numbers, the algo's equity curve with its
// drawdown, a candle chart of the algo's symbol (one day at a time, with a day control) carrying the entry
// and exit of each trade, and the sortable trade table. Selecting a trade marks its row and its markers and
// scrolls the chart to it, loading that trade's day first when another day is shown. The algo's indicators
// (the series route, for the same window as the bars), the stop and target lines of a realistic run (the
// orders route, once per view) and their legend are drawn by charts/overlays.js. Follows the view contract in
// main.js.
//
// Previous/next day jump to the nearest day that has bars: one bars request covers up to 7 calendar days
// (stepWindow), the nearest day with a bar (nearestBarDay) is then shown. A trade that exits on another day
// says so (exitNote) with a button to show that day; so does one shown on its exit day (entryNote). The search
// for the nearest day runs in a scope of its own: it never aborts the day being shown, which is replaced only
// when a day is found.
//
// Exports: render, TRADE_COLUMNS, dayOfTime, shiftDay, stepDay, stepWindow, nearestBarDay, exitNote, entryNote, initialDay,
// dayWindow, barsState, buildTradeRows, selectLabel, summaryItems, trimDecimal. Everything but render is pure
// (no DOM, no network) so node can test it.
import { ApiError, HEALTH_EVENT, getJson, isAbortError } from "../api.js";
import { createCandleChart } from "../charts/candles.js";
import { createEquityChart } from "../charts/equity.js";
import { createFvgZones } from "../charts/fvg.js";
import { createOverlays } from "../charts/overlays.js";
import { errorPanel, h, loadingPanel, viewHeading } from "../dom.js";
import { chartTimeToDataTime, dataTimeToChartTime, netClass, trimDecimal } from "../format.js";
import { ALGO_REFRESH_MS, createPoller, createRefreshNote, isLiveRun, isRunning, liveStatusPanel } from "../live.js";
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

export const SEARCH_DAYS = 7; // the longest span of one bars request (the route's limit)

// The bars request that looks for the nearest day with bars `delta` (-1 or +1) from `day`: up to 7 calendar
// days that way, kept inside the run's bar range [first, last] ("YYYY-MM-DD", either may be null).
// {start, end} like dayWindow, or null when that side of the range is empty.
export function stepWindow(day, delta, first, last) {
  const near = shiftDay(day, delta);
  const far = shiftDay(day, delta * SEARCH_DAYS);
  if (near === null || far === null) return null;
  let low = delta > 0 ? near : far;
  let high = delta > 0 ? far : near;
  if (first && low < first) low = first;
  if (last && high > last) high = last;
  if (low > high) return null;
  return { start: low + " 00:00:00", end: high + " 23:59:59" };
}

// The day of the first (delta > 0) or last (delta < 0) of the bars the bars route sent, or null when none
// has a readable time.
export function nearestBarDay(bars, delta) {
  let best = null;
  for (const bar of Array.isArray(bars) ? bars : []) {
    const day = bar ? dayOfTime(bar.time) : null;
    if (day === null) continue;
    if (best === null || (delta > 0 ? day < best : day > best)) best = day;
  }
  return best;
}

// The note for a selected trade shown on its entry day when it exits on another: {text, day} or null.
export function exitNote(row, shownDay) {
  if (!row || !shownDay || row.entryDay !== shownDay || !row.exitDay || row.exitDay === shownDay) return null;
  return { text: "Exits on " + row.exitDay + ".", day: row.exitDay };
}

// The note for a selected trade shown on its exit day when it entered on another: {text, day} or null.
export function entryNote(row, shownDay) {
  if (!row || !shownDay || row.exitDay !== shownDay || !row.entryDay || row.entryDay === shownDay) return null;
  return { text: "Entered on " + row.entryDay + ".", day: row.entryDay };
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

export const NO_BARS_YET = "No bars yet.";

// What the bars route's answer means for the chart: {candles, note, kind}. `candles` says whether to draw
// the bars; `note` is the sentence to show above the chart ("" for none).
export function barsState(data, symbol, live = false) {
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
    // A live run that has stored no bar yet is not an error: its first bar has not arrived.
    if (live) return { kind: "empty", candles: false, note: NO_BARS_YET };
    return { kind: "empty", candles: false, note: "No bars for " + name + " on this day." };
  }
  return { kind: unverified ? "unverified" : "ok", candles: true, note: unverified };
}

// trimDecimal lives in format.js (the live panels use it too); re-exported here for the callers of this view.
export { trimDecimal };

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// The trades route's trades as table rows. `key` and `number` are the 1-based position in the order the
// server sent (unique, stable across sorting); the raw fields are kept; entryChart, exitChart (chart
// seconds), entryDay and exitDay are derived from the naive data times.
export function buildTradeRows(trades) {
  const list = Array.isArray(trades) ? trades : [];
  return list.map((trade, index) => {
    const entryChart = dataTimeToChartTime(trade.entry_time);
    const exitChart = dataTimeToChartTime(trade.exit_time);
    return {
      ...trade,
      key: index + 1,
      number: index + 1,
      entryChart,
      exitChart,
      entryDay: entryChart === null ? null : dayOfTime(trade.entry_time),
      exitDay: exitChart === null ? null : dayOfTime(trade.exit_time),
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

function statusContent(run) {
  const status = displayStatus(run);
  const warning = isRunning(run) ? "" : statusNotes(run); // a running live run is not "incomplete"
  return [
    h("ul", { class: "status-line" },
      h("li", {}, h("span", { class: "muted" }, "Run status "), h("span", { class: "badge " + status }, status))),
    warning ? h("p", { class: "warning", role: "note" }, warning) : null].filter(Boolean);
}

function statusPanel(run) {
  return h("section", { class: "panel", "aria-label": "Run status" }, ...statusContent(run));
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

  // The timer of a running live run (created, with its cleanup, once the page is drawn and the run is known to
  // run). It reloads the bars, trades and indicators of the shown day: refreshLive, below.
  let poller = null;
  const refreshNote = createRefreshNote();
  let liveBox = null; // the live status and the refresh note; only for a run that has a liveness

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
  let firstDay = dayOfTime(run.first_bar); // a live run's last day grows: a refresh updates both
  let lastDay = dayOfTime(run.last_bar);
  const statusBox = statusPanel(run);
  const parts = [heading(), backLinks(runId), statusBox];
  if (isLiveRun(run)) {
    liveBox = h("div", { class: "live-box" }, liveStatusPanel(run)); // replaced, with the note, on a refresh
    parts.push(liveBox);
  }
  parts.push(summaryPanel(stats));

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
  let rowsByKey = new Map(rows.map((row) => [String(row.key), row]));

  // The candle chart and its day control.
  const dayLabel = h("span", { class: "day-label", role: "status" });
  // Previous and next stay focusable at the ends (aria-disabled, activation ignored): a disabled button
  // under the keyboard focus would drop it to the page.
  const previousButton = h("button", { type: "button", "aria-disabled": "false", onclick: () => step(-1) },
    "Previous day");
  const nextButton = h("button", { type: "button", "aria-disabled": "false", onclick: () => step(1) },
    "Next day");
  const dayNote = h("div", { class: "day-note muted", role: "status", hidden: true });
  const barsError = h("div", { class: "bars-error" });
  const overlayBox = h("div", { class: "overlay-box" });
  const candleBox = h("div", { class: "chart-box" });
  const candleParts = [h("h3", {}, "Price and trades"),
    symbol
      ? h("div", { class: "toolbar day-control", role: "group", "aria-label": "Day shown" },
        previousButton, dayLabel, nextButton)
      : h("p", { class: "muted" }, "This algo has no symbol recorded, so there are no candles to show."),
    dayNote, barsError, overlayBox, candleBox];
  if (symbol) {
    candleParts.push(h("p", { class: "muted chart-caption" },
      symbol + ", 1-minute bars, one day at a time; times are data time, as stored."));
  }
  parts.push(h("section", { class: "panel", "aria-label": "Price chart" }, ...candleParts));

  // The trade table.
  let table = null;
  const makeTable = () => dataTable({
    columns: TRADE_COLUMNS, rows, caption: "Trades of " + idname, captionHidden: true,
    sort: { column: 0, direction: "asc" }, rowKey: (row) => row.key, selectedKey: null,
    onSelect: (row) => select(row.key), selectLabel,
  });
  const tradesSection = h("section", { "aria-label": "Trades" }, h("h3", {}, "Trades"));
  if (tradesFailed) {
    tradesSection.append(errorPanel(tradesResult.status === "rejected" ? tradesResult.reason : unexpected()));
  } else if (rows.length === 0) {
    tradesSection.append(h("p", { class: "panel empty" }, "This algo made no trades in this run."));
  } else {
    table = makeTable();
    tradesSection.append(table.element);
  }
  parts.push(tradesSection);

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

  // The indicator overlays and their legend; a failure to build them leaves the candles and trades working.
  let overlays = null;
  if (candles) {
    try {
      overlays = createOverlays(candles, { ctx, container: overlayBox }); // registers its own disposal at once
    } catch (err) {
      overlayBox.replaceChildren(errorPanel(err));
    }
  }
  // The FVG zones (off by default; asks the fvg route only while on). A failure to build it leaves the rest working.
  let fvg = null;
  if (candles) {
    try {
      fvg = createFvgZones(candles, {
        ctx, container: overlayBox,
        fetchZones: (query, signal) => getJson("/api/runs/" + runId + "/fvg", { symbol, ...query }, { signal }),
      });
    } catch (err) {
      console.error("FVG zones unavailable", err);
    }
  }
  // The orders of a realistic run, requested once for the view (not per day): the stop and target lines.
  const realistic = run.fills === "realistic";
  let ordersRequest = null;
  if (candles && overlays && realistic) {
    ordersRequest = getJson("/api/runs/" + runId + "/orders", { algo: idname }, { signal: ctx.signal });
    ordersRequest.catch(() => {}); // awaited per day below; this keeps an unawaited failure from being unhandled
  }

  const live = isLiveRun(run);
  let lastBar = run.last_bar; // the last bar the run header named; a refresh of a live run updates it
  let day = initialDay(rows, lastBar);
  let selectedKey = null;
  let request = null;
  let dayPending = false; // a day is loading: a refresh would only repeat that load
  let refreshScope = null; // the request scope of a running refresh
  let liveStatus = live ? liveStatusPanel(run) : null;
  let noteShown = false;
  let stepSearch = null; // the scope of a running search for the previous / next day

  function paintDay() {
    dayLabel.textContent = day ? day + " (data time)" : "no day";
    previousButton.setAttribute("aria-disabled", String(!day || stepDay(day, -1, firstDay, lastDay) === null));
    nextButton.setAttribute("aria-disabled", String(!day || stepDay(day, 1, firstDay, lastDay) === null));
  }

  function showError(err) {
    barsError.replaceChildren(errorPanel(err));
  }

  // A line of text under the day control, with an optional button; "" hides it.
  function setDayNote(text, action) {
    dayNote.textContent = text || "";
    if (text && action) dayNote.append(h("button", { type: "button", class: "day-note-button", onclick: action.run }, action.label));
    dayNote.hidden = !text;
  }

  function selectedRow() {
    return selectedKey === null ? null : rowsByKey.get(String(selectedKey)) || null;
  }

  // `focus` false (a refresh of a live run) redraws the markers without moving the user's view to the trade.
  function drawTrades(focus = true) {
    candles.setTrades(rows, selectedKey);
    const row = selectedRow();
    const exits = exitNote(row, day);
    const entered = entryNote(row, day);
    const other = exits || entered;
    if (other) setDayNote(other.text, { label: "Show " + other.day, run: () => loadDay(other.day) });
    else setDayNote("");
    if (!row || !focus) return;
    if (row.entryDay === day && row.entryChart !== null) {
      if (row.exitDay === day && row.exitChart !== null) candles.focusSpan(row.entryChart, row.exitChart);
      else candles.focusTime(row.entryChart);
    } else if (row.exitDay === day && row.exitChart !== null) {
      candles.focusTime(row.exitChart);
    }
  }

  // The orders of the run: {events, fills} or {error}; a failure of the (shared) request is the same each day.
  async function loadOrders() {
    if (!ordersRequest) return { events: null, fills: null, error: null };
    try {
      const data = await ordersRequest;
      if (!data || !Array.isArray(data.order_events) || !Array.isArray(data.fills)) throw unexpected();
      return { events: data.order_events, fills: data.fills, error: null };
    } catch (err) {
      if (isAbortError(err)) throw err;
      return { events: null, fills: null, error: err };
    }
  }

  // Draws the overlays of the day once the series (and, for a realistic run, the orders) are there. Never
  // rejects: a failed series request is shown in the legend, anything else beside the chart, and the
  // candles and trades keep working. Only the scope that requested the bars may draw.
  async function drawOverlays(mine, seriesRequest, bars) {
    try {
      let seriesData = null;
      let seriesError = null;
      try {
        seriesData = await seriesRequest;
      } catch (err) {
        if (isAbortError(err) || !mine.isCurrent()) return;
        seriesError = err;
      }
      const orders = await loadOrders();
      if (!mine.isCurrent()) return;
      overlays.setData({
        series: seriesData, seriesError, orderEvents: orders.events, ordersError: orders.error, fills: orders.fills,
        fillModel: run.fills, runStatus: run.status, bars,
      });
    } catch (err) {
      if (isAbortError(err) || !mine.isCurrent()) return;
      showError(err);
    }
  }

  // Latest wins: a newer day aborts the older request, and only the newest may draw. The series (same window
  // as the bars) is requested in the same scope: a newer day aborts both.
  async function loadDay(next) {
    if (!candles) return;
    if (request) request.abort();
    if (refreshScope) refreshScope.abort(); // a refresh of the day on show is moot once another day is shown
    if (stepSearch) stepSearch.abort(); // showing a day (by any route) ends a search for another
    stepSearch = null;
    const mine = request = ctx.child();
    dayPending = true;
    day = next;
    paintDay();
    barsError.replaceChildren();
    setDayNote("");
    candles.setNote("Loading bars...");
    try {
      const window = dayWindow(day);
      const seriesRequest = overlays
        ? getJson("/api/runs/" + runId + "/series", { algo: idname, start: window.start, end: window.end },
          { signal: mine.signal })
        : null;
      if (seriesRequest) seriesRequest.catch(() => {}); // awaited after the bars; see below
      const data = await getJson("/api/runs/" + runId + "/bars",
        { symbol, start: window.start, end: window.end }, { signal: mine.signal });
      if (!mine.isCurrent()) return;
      const state = barsState(data, symbol, live);
      candles.setBars(state.candles ? data.bars : []);
      candles.setNote(state.note);
      if (fvg) {
        if (state.candles) fvg.update({ symbol, start: window.start, end: window.end });
        else fvg.clear();
      }
      drawTrades();
      if (!overlays) return;
      if (!state.candles) {
        overlays.clear();
        return;
      }
      drawOverlays(mine, seriesRequest, data.bars); // not awaited: the candles and trades do not wait for it
    } catch (err) {
      if (isAbortError(err) || !mine.isCurrent()) return;
      candles.setBars([]);
      candles.setNote("");
      if (overlays) overlays.clear();
      if (fvg) fvg.clear();
      showError(err);
    } finally {
      if (request === mine) dayPending = false;
    }
  }

  // A live run that is running: every ALGO_REFRESH_MS the run, its trades and the bars, indicators and orders of
  // the day on show are loaded again and drawn over the old ones. The user's zoom and scroll position are kept
  // (setBars with keepView, markers redrawn without a focus). A part that fails keeps its last render; the one
  // "refresh failed" note says so until the next success. The timer stops when the run is no longer running.
  async function refreshLive() {
    if (dayPending) return; // a day is loading: that load is as fresh as a refresh
    if (refreshScope) refreshScope.abort();
    const mine = refreshScope = ctx.child();
    const options = { signal: mine.signal };
    const shown = day;
    const span = shown ? dayWindow(shown) : null;
    const seriesRequest = shown && overlays
      ? getJson("/api/runs/" + runId + "/series", { algo: idname, start: span.start, end: span.end }, options)
      : null;
    if (seriesRequest) seriesRequest.catch(() => {}); // awaited by drawOverlays
    if (shown && overlays && realistic) {
      // the orders change while the run goes on: asked again (on the view's signal, which a later refresh does not abort)
      ordersRequest = getJson("/api/runs/" + runId + "/orders", { algo: idname }, { signal: ctx.signal });
      ordersRequest.catch(() => {});
    }
    const [docNext, tradesNext, barsNext] = await Promise.allSettled([
      getJson("/api/runs/" + runId, {}, options),
      getJson("/api/runs/" + runId + "/trades", { algo: idname }, options),
      shown && candles
        ? getJson("/api/runs/" + runId + "/bars", { symbol, start: span.start, end: span.end }, options)
        : Promise.resolve(null),
    ]);
    if (!mine.isCurrent()) return;
    for (const result of [docNext, tradesNext, barsNext]) {
      if (result.status === "rejected" && isAbortError(result.reason)) return;
    }
    let failed = false;
    let ended = false;
    const docOk = docNext.status === "fulfilled" && validDocument(docNext.value);
    if (docOk) {
      const next = docNext.value.run;
      statusBox.replaceChildren(...statusContent(next));
      firstDay = dayOfTime(next.first_bar);
      lastDay = dayOfTime(next.last_bar);
      lastBar = next.last_bar;
      paintDay();
      ended = !isRunning(next);
      if (isLiveRun(next)) liveStatus = liveStatusPanel(next);
    } else {
      failed = true;
    }
    if (tradesNext.status === "fulfilled" && tradesNext.value && Array.isArray(tradesNext.value.trades)) {
      rows = buildTradeRows(tradesNext.value.trades);
      rowsByKey = new Map(rows.map((row) => [String(row.key), row]));
      if (table) {
        table.update({ rows });
      } else if (rows.length > 0) {
        table = makeTable();
        tradesSection.replaceChildren(h("h3", {}, "Trades"), table.element);
      }
    } else {
      failed = true;
    }
    if (candles && shown) {
      if (barsNext.status === "fulfilled" && barsNext.value && Array.isArray(barsNext.value.bars)) {
        const data = barsNext.value;
        const state = barsState(data, symbol, true);
        candles.setBars(state.candles ? data.bars : [], { keepView: true });
        candles.setNote(state.note);
        if (fvg) {
          if (state.candles) fvg.update({ symbol, start: span.start, end: span.end });
          else fvg.clear();
        }
        drawTrades(false);
        if (overlays) {
          if (state.candles) drawOverlays(mine, seriesRequest, data.bars);
          else overlays.clear();
        }
      } else {
        failed = true;
      }
    } else if (candles && !shown && docOk) {
      // no bar was stored when the page opened: the first one may have arrived
      const first = initialDay(rows, lastBar);
      if (first) {
        refreshScope = null;
        await loadDay(first);
      }
    }
    if (failed) refreshNote.fail();
    else refreshNote.ok();
    if (liveBox && (docOk || refreshNote.failed !== noteShown)) {
      noteShown = refreshNote.failed;
      liveBox.replaceChildren(liveStatus, ...(noteShown ? [refreshNote.element] : []));
    }
    if (ended) poller.stop(); // the run is no longer running: this was the last refresh
    window.dispatchEvent(new Event(HEALTH_EVENT));
  }

  // Previous / next day: the nearest day that has bars. One bars request covers up to 7 calendar days that way.
  async function step(delta) {
    if (!candles || !day) return;
    const direction = delta > 0 ? "next" : "previous";
    if ((delta > 0 ? nextButton : previousButton).getAttribute("aria-disabled") === "true") return;
    const search = stepWindow(day, delta, firstDay, lastDay);
    const none = "No " + direction + " day with bars within 7 days of " + day + " in this run's bar range. "
      + "Staying on " + day + ".";
    if (!search) {
      setDayNote(none);
      return;
    }
    // A scope of its own: the day on show keeps loading (or stays drawn) while the search runs, and is
    // replaced only when a day is found (loadDay then aborts and replaces it).
    if (stepSearch) stepSearch.abort();
    const mine = stepSearch = ctx.child();
    setDayNote("Looking for the " + direction + " day with bars...");
    try {
      const data = await getJson("/api/runs/" + runId + "/bars",
        { symbol, start: search.start, end: search.end }, { signal: mine.signal });
      if (!mine.isCurrent()) return;
      const found = data && data.available === true ? nearestBarDay(data.bars, delta) : null;
      if (found === null) {
        if (stepSearch === mine) stepSearch = null;
        setDayNote(data && data.available !== true ? barsState(data, symbol).note : none);
        return;
      }
      await loadDay(found); // aborts the search scope and the shown day's request; clears the note
    } catch (err) {
      if (isAbortError(err) || !mine.isCurrent()) return;
      if (stepSearch === mine) stepSearch = null;
      setDayNote("");
      showError(err);
    }
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
      candles.setNote(live ? NO_BARS_YET : "This run stored no bars, so there is no day to show.");
    }
  }
  if (isRunning(run)) {
    poller = createPoller(ctx, { intervalMs: ALGO_REFRESH_MS, tick: refreshLive }); // its stop is registered now
    poller.start();
  }
}
