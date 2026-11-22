// Indicator overlays of the candle chart: each algo's own indicators (the series route), the stop and target
// orders of a realistic-fill run (the orders route) and the legend with one toggle per item, so a trade never
// appears out of nowhere. The pure core is exported separately so node can test it without a DOM or the chart
// library; createOverlays is the thin drawing layer on the candle chart's seam (charts/candles.js).
//
// Exports: HINDSIGHT_NOTE, FAILED_NOTE, NO_INDICATORS_NOTE, OWN_COLUMN_NOTE, barTimes, convertSeries,
// buildOrderLines, overlayColors, mixColors, planOverlays, createOverlays.
//
// Times are chart seconds (format.js dataTimeToChartTime). An indicator point is only ever placed on a candle
// time of the loaded window: the library adds every series time to the shared time scale, so a stray time would
// stretch the axis. Points that are not on a candle time are dropped and counted.
//
// barTimes(bars)       the strictly increasing candle times (chart seconds) of the bars, which are either the
//                      bars route's rows or the converted bars of candles.js convertBars.
// convertSeries(entry, times)  one series of the series route ({name, draw_type, position, points: [{time,
//                      value}]}) as {name, drawType, kind, drawn, unknownType, data, markers, count, dropped}:
//                        line         data = the points in time order ({time, value}), one per candle time;
//                        segment      data as a line, stepped, with a whitespace point ({time}) on the candle
//                                     after a point whose next point is not on the next candle of `times`;
//                                     neighbours in the bar list that are further apart in time than the
//                                     bars' own spacing (the smallest gap of `times`: a missing bar, a session
//                                     gap) cannot hold a whitespace point between them, so they start a new
//                                     entry of `pieces`, which are the arrays to draw (a series each; `data` is
//                                     their concatenation, `runs` the number of continuous runs). Adjacency is
//                                     never a fixed 60 seconds.
//                        (`pieces` of a line, marker and unknown type: [data] or [].)
//                        marker-up    markers = [{time, position: "belowBar", shape: "arrowUp"}] (a swing low);
//                        marker-down  markers = [{time, position: "aboveBar", shape: "arrowDown"}] (a swing high);
//                        marker-dot   markers = [{time, position: "inBar", shape: "circle"}];
//                        baseline     not drawn (drawn false);
//                        other        a plain line, unknownType true.
//                      `count` is the number of points kept, `dropped` those whose time is not a candle time.
// buildOrderLines(orderEvents, fills, times)  the stop and target lines of a realistic run, one record per
//                      order: {role: "stop" | "target", account, brokerRef, orderId, start, end, data,
//                      startsBeforeWindow, endsAfterWindow}. `orderEvents` and `fills` are the orders route's
//                      lists. Per order: its price over time from the placed and modified events, ending at its
//                      cancelled event or, when there is none, at the exit fill of the same (account,
//                      broker_ref) position (a stop closed by the synthetic end-of-data fill has no cancel
//                      event), or at the end of the window. `data` is a stepped line on candle times, clipped
//                      to the window: one that starts before it starts at the first bar with its then-current
//                      price; one entirely outside it is left out. Orders of a position that never had an entry
//                      fill never worked and are left out (when `fills` is a list).
// overlayColors(theme), mixColors(a, b, weight)  the colours, all derived from the page theme's own.
// planOverlays({series, orderEvents, fills, fillModel, runStatus, times, ordersError})
//                      -> {items, notes}: what to draw and say. An item is {key, name, kindText, shape, paint,
//                      lines: [{data, step, style, width}], markers, defaultVisible, count, dropped,
//                      unknownType, annotation}. Realistic fills (fillModel === "realistic"): the stop and
//                      target items come from the order events and are on by default; the algo's own StopPrice
//                      and tp series are listed but off, annotated as the algo's own column that can be stale.
//                      Any other fill model (legacy): there are no stop or target orders; StopPrice and tp are
//                      drawn and on.
//
// createOverlays(candleHandle, {ctx, container, lib, view}) -> {setData, clear, dispose}
//     candleHandle: charts/candles.js createCandleChart's handle (chart, series, onTheme, onBars).
//     container: where the legend goes (default: candleHandle.legend); it stays in normal flow and is not
//     tied to the candles, so it shows even when a day has no bars.
//     setData({series, seriesError, orderEvents, ordersError, fills, fillModel, runStatus, bars}) draws for the
//     bars given (the bars route's rows or converted bars): the previous drawing is replaced. Stale data (the
//     candle chart holds other bars by now) is not drawn. clear() removes every series, marker and legend
//     entry. Toggle states are kept by item key for the life of the overlays, across days.
//     Registers dispose with ctx.onCleanup at once. dispose() removes the added series, the marker
//     primitive and the listeners; it is safe to call twice, and after it nothing reaches the chart.
//     Markers go on the candle series through the overlays' own createSeriesMarkers instance: the library
//     attaches one primitive per call, so this coexists with the trade markers of candles.js.
import { errorPanel, h } from "../dom.js";
import { dataTimeToChartTime } from "../format.js";
import { convertBars, snapIndex } from "./candles.js";
import { isThemeOrHexColor, readTheme } from "./theme.js";

export const HINDSIGHT_NOTE = "Swing markers are drawn where the swing happened. The algo only knew some bars later.";
export const FAILED_NOTE = "This run failed. Its indicators may be incomplete.";
export const NO_INDICATORS_NOTE = "No indicators are stored for this algo in this run. Runs stored before "
  + "indicator capture, and algos whose indicators were skipped at capture, have none.";
export const OWN_COLUMN_NOTE = "algo's own column, can be stale";
export const ORDERS_FAILED_NOTE = "The stop and target orders could not be loaded, so their lines are not drawn.";

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function barTimes(bars) {
  const list = Array.isArray(bars) ? bars : [];
  const converted = list.length > 0 && list.every((bar) => bar && typeof bar.time === "number") ? list : convertBars(list);
  const times = [];
  for (const bar of converted) {
    if (times.length > 0 && bar.time <= times[times.length - 1]) continue;
    times.push(bar.time);
  }
  return times;
}

const MARKER_KINDS = {
  "marker-up": { position: "belowBar", shape: "arrowUp", words: "swing low" },
  "marker-down": { position: "aboveBar", shape: "arrowDown", words: "swing high" },
  "marker-dot": { position: "inBar", shape: "circle", words: "marker" },
};

// The spacing of the loaded bars: the smallest gap between two neighbours (Infinity with fewer than two bars).
function barInterval(times) {
  let smallest = Infinity;
  for (let i = 1; i < times.length; i += 1) smallest = Math.min(smallest, times[i] - times[i - 1]);
  return smallest;
}

export function convertSeries(entry, times) {
  const name = entry && typeof entry.name === "string" ? entry.name : "";
  const drawType = entry && typeof entry.draw_type === "string" ? entry.draw_type : "";
  const out = {
    name, drawType, kind: drawType, drawn: true, unknownType: false, data: [], pieces: [], runs: 0, markers: [],
    count: 0, dropped: 0,
  };
  if (drawType === "baseline") {
    out.drawn = false;
    return out;
  }
  const isMarker = Object.hasOwn(MARKER_KINDS, drawType);
  if (!isMarker && drawType !== "line" && drawType !== "segment") {
    out.kind = "unknown";
    out.unknownType = true;
  }
  const indexOf = new Map(times.map((time, index) => [time, index]));
  const points = Array.isArray(entry && entry.points) ? entry.points : [];
  const kept = [];
  for (const point of points) {
    const time = point ? dataTimeToChartTime(point.time) : null;
    const value = point ? toNumber(point.value) : null;
    const index = time === null ? undefined : indexOf.get(time);
    if (index === undefined) {
      out.dropped += 1;
      continue;
    }
    if (value === null) continue;
    kept.push({ index, value });
  }
  kept.sort((a, b) => a.index - b.index); // stable: of repeated times the last stays last
  const unique = [];
  for (const point of kept) {
    if (unique.length > 0 && unique[unique.length - 1].index === point.index) unique[unique.length - 1] = point;
    else unique.push(point);
  }
  out.count = unique.length;
  if (isMarker) {
    const style = MARKER_KINDS[drawType];
    out.markers = unique.map((point) => ({ time: times[point.index], position: style.position, shape: style.shape }));
    return out;
  }
  const stepped = drawType === "segment";
  const interval = barInterval(times);
  let piece = [];
  let previous = null;
  for (const point of unique) {
    if (stepped && previous !== null) {
      if (point.index - previous.index > 1) {
        piece.push({ time: times[previous.index + 1] }); // whitespace on the next candle: the line is broken
        out.runs += 1;
      } else if (times[point.index] - times[previous.index] > interval) {
        // neighbours in the bar list but further apart than one bar (a missing bar, a session gap): there is
        // no candle time between them to hold a whitespace point, so the next run is a series of its own
        out.pieces.push(piece);
        piece = [];
        out.runs += 1;
      }
    }
    piece.push({ time: times[point.index], value: point.value });
    previous = point;
  }
  if (piece.length > 0) out.pieces.push(piece);
  out.runs += out.pieces.length > 0 ? 1 : 0;
  out.data = out.pieces.flat();
  return out;
}

const ROLES = ["stop", "target"];

function positionKey(account, ref) {
  return String(account) + "\u0001" + String(ref);
}

export function buildOrderLines(orderEvents, fills, times) {
  const events = Array.isArray(orderEvents) ? orderEvents : [];
  if (!Array.isArray(times) || times.length === 0) return [];
  const windowFirst = times[0];
  const windowLast = times[times.length - 1];
  const orders = new Map();
  for (const event of events) {
    if (!event) continue;
    const key = positionKey(event.account, event.order_id);
    const time = dataTimeToChartTime(event.time);
    if (event.kind === "placed") {
      const price = toNumber(event.price);
      if (ROLES.includes(event.order_type) && time !== null && price !== null) {
        orders.set(key, {
          role: event.order_type, account: event.account, brokerRef: event.broker_ref, orderId: event.order_id,
          changes: [{ time, price }], cancelled: null,
        });
      }
      continue;
    }
    const order = orders.get(key);
    if (!order || time === null) continue;
    if (event.kind === "modified") {
      const price = toNumber(event.price);
      if (price !== null) order.changes.push({ time, price });
    } else if (event.kind === "cancelled" && order.cancelled === null) {
      order.cancelled = time;
    }
  }

  const known = Array.isArray(fills);
  const entered = new Set();
  const exitedAt = new Map();
  if (known) {
    for (const fill of fills) {
      if (!fill) continue;
      const key = positionKey(fill.account, fill.broker_ref);
      const time = dataTimeToChartTime(fill.time);
      if (fill.reason === "entry") {
        entered.add(key);
      } else if (time !== null && (!exitedAt.has(key) || time > exitedAt.get(key))) {
        exitedAt.set(key, time);
      }
    }
  }

  const lines = [];
  for (const order of orders.values()) {
    const key = positionKey(order.account, order.brokerRef);
    if (known && !entered.has(key)) continue; // the entry never filled: this order never worked
    order.changes.sort((a, b) => a.time - b.time); // stable
    const start = order.changes[0].time;
    const end = order.cancelled !== null ? order.cancelled : (exitedAt.has(key) ? exitedAt.get(key) : null);
    if (start > windowLast || (end !== null && end < windowFirst)) continue;
    const last = end === null ? windowLast : Math.min(end, windowLast);
    const lastIndex = snapIndex(times, last);
    const byIndex = new Map();
    let price = null;
    for (const change of order.changes) {
      if (change.time > last) break;
      const index = Math.max(0, snapIndex(times, change.time)); // before the window: its first bar
      byIndex.set(index, change.price);
      price = change.price;
    }
    if (price === null) continue;
    if (!byIndex.has(lastIndex)) byIndex.set(lastIndex, price); // hold the price to where the order ended
    const data = [...byIndex.entries()].sort((a, b) => a[0] - b[0])
      .map(([index, value]) => ({ time: times[index], value }));
    lines.push({
      role: order.role, account: order.account, brokerRef: order.brokerRef, orderId: order.orderId,
      start, end, data, startsBeforeWindow: start < windowFirst, endsAfterWindow: end !== null && end > windowLast,
    });
  }
  return lines;
}

// --- colours ---------------------------------------------------------------------------------------------

const HEX6 = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

function hexChannels(color) {
  const m = HEX6.exec(String(color).trim());
  if (!m) return null;
  const digits = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  const n = Number.parseInt(digits, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// `a` moved `weight` (0..1) of the way to `b`, as #rrggbb; `a` itself when either is not a hex colour.
export function mixColors(a, b, weight) {
  const x = hexChannels(a);
  const y = hexChannels(b);
  if (!x || !y) return a;
  const mixed = x.map((channel, i) => Math.round(channel + (y[i] - channel) * weight));
  return "#" + mixed.map((c) => c.toString(16).padStart(2, "0")).join("");
}

export function overlayColors(theme) {
  return {
    palette: [
      theme.warn,
      mixColors(theme.accent, theme.neg, 0.5),
      mixColors(theme.pos, theme.accent, 0.5),
      theme.text,
      theme.accent,
      theme.muted,
    ],
    stop: theme.neg,
    target: theme.pos,
    ownStop: mixColors(theme.neg, theme.muted, 0.4),
    ownTarget: mixColors(theme.pos, theme.muted, 0.4),
  };
}

// --- the plan --------------------------------------------------------------------------------------------

function ownRole(name) {
  const text = String(name).toLowerCase();
  if (text === "stopprice") return "stop";
  if (text === "tp") return "target";
  return null;
}

const ORDER_WORDS = { stop: "stop (orders)", target: "target (orders)" };
const ORDER_NAMES = { stop: "Stop", target: "Target" };

export function planOverlays({ series = null, orderEvents = null, fills = null, fillModel = null, runStatus = null, times = [],
  ordersError = null } = {}) {
  const realistic = fillModel === "realistic";
  const entries = series && series.available === true && Array.isArray(series.series) ? series.series : [];
  const items = [];
  let paletteIndex = 0;
  let swings = false;
  for (const entry of entries) {
    const converted = convertSeries(entry, times);
    if (!converted.drawn) continue;
    const marker = Object.hasOwn(MARKER_KINDS, converted.drawType) ? MARKER_KINDS[converted.drawType] : null;
    if (converted.drawType === "marker-up" || converted.drawType === "marker-down") swings = true;
    const own = ownRole(converted.name);
    let kindText = marker ? marker.words : converted.drawType === "segment" ? "level" : "line";
    if (converted.unknownType) kindText = "unknown type" + (converted.drawType ? " (" + converted.drawType + ")" : "") + ", drawn as a line";
    const item = {
      key: "series:" + converted.name,
      name: converted.name,
      kindText,
      shape: marker ? converted.drawType : "line",
      paint: own ? (realistic ? "own-" + own : own) : "palette",
      paletteIndex: own ? 0 : paletteIndex++,
      lines: marker ? [] : converted.pieces.map((data) => ({
        data, step: converted.drawType === "segment",
        style: own && realistic ? "dotted" : "solid", width: own && realistic ? 1 : 2,
      })),
      runs: converted.runs,
      markers: converted.markers,
      defaultVisible: !(own && realistic),
      count: converted.count,
      dropped: converted.dropped,
      unknownType: converted.unknownType,
      annotation: own && realistic ? "(" + OWN_COLUMN_NOTE + ")" : "",
    };
    items.push(item);
  }

  if (realistic && Array.isArray(orderEvents)) {
    const lines = buildOrderLines(orderEvents, fills, times);
    for (const role of ROLES) {
      const mine = lines.filter((line) => line.role === role);
      items.push({
        key: "orders:" + role, name: ORDER_NAMES[role], kindText: ORDER_WORDS[role], shape: "line", paint: role,
        paletteIndex: 0,
        lines: mine.map((line) => ({ data: line.data, step: true, style: "solid", width: 2 })),
        markers: [], defaultVisible: true, count: mine.reduce((sum, line) => sum + line.data.length, 0), dropped: 0,
        unknownType: false, annotation: "",
      });
    }
  }

  const notes = [];
  if (swings) notes.push(HINDSIGHT_NOTE);
  if (runStatus === "failed") notes.push(FAILED_NOTE);
  // `series` null means the request failed (the legend shows the error instead); otherwise an answer with no
  // series says why there is nothing to draw.
  if (series && (series.available !== true || entries.length === 0)) notes.push(NO_INDICATORS_NOTE);
  if (realistic && ordersError) notes.push(ORDERS_FAILED_NOTE);
  return { items, notes };
}

// --- the drawing layer ------------------------------------------------------------------------------------

function signature(times) {
  return times.length + ":" + (times.length > 0 ? times[0] + ":" + times[times.length - 1] : "");
}

function colorOf(item, colors) {
  if (item.paint === "palette") return colors.palette[item.paletteIndex % colors.palette.length];
  if (item.paint === "own-stop") return colors.ownStop;
  if (item.paint === "own-target") return colors.ownTarget;
  return colors[item.paint];
}

export function createOverlays(candleHandle, { ctx, container, lib = globalThis.LightweightCharts, view = globalThis } = {}) {
  if (!lib || typeof lib.LineSeries === "undefined" || typeof lib.createSeriesMarkers !== "function") {
    throw new Error("The chart library is not loaded.");
  }
  let disposed = false;
  let theme = readTheme(view);
  let latestSignature = null; // of the bars the candle chart holds now, once it has told us
  let drawnSignature = null; // of the bars the overlays were drawn for
  let drawn = []; // [{item, seriesList, button, swatch}]
  let markerApi = null;
  let removeTheme = () => {};
  let removeBars = () => {};
  const visibility = new Map();
  const toggles = h("ul", { class: "overlay-toggles", "aria-label": "Indicators" });
  const notes = h("div", { class: "overlay-notes" });
  const errors = h("div", { class: "overlay-errors" });
  const root = h("div", { class: "overlay-legend", hidden: true }, errors, toggles, notes);

  function dispose() {
    if (disposed) return;
    disposed = true;
    removeTheme();
    removeBars();
    removeSeriesQuietly();
    if (markerApi && typeof markerApi.detach === "function") {
      try {
        markerApi.detach();
      } catch (err) {
        // the chart is gone already
      }
    }
    markerApi = null;
    toggles.replaceChildren();
    notes.replaceChildren();
    errors.replaceChildren();
    root.hidden = true;
  }
  ctx.onCleanup(dispose);
  if (disposed) return { setData() {}, clear() {}, dispose }; // the navigation had already ended

  (container || candleHandle.legend).append(root);
  markerApi = lib.createSeriesMarkers(candleHandle.series, []);
  removeTheme = candleHandle.onTheme((next) => {
    if (disposed) return;
    theme = next;
    recolor();
  });
  removeBars = candleHandle.onBars((bars) => {
    if (disposed) return;
    latestSignature = signature(barTimes(bars));
    if (drawnSignature !== null && drawnSignature !== latestSignature) clear(); // never stay on stale bars
  });

  function removeSeriesQuietly() {
    for (const entry of drawn) {
      for (const series of entry.seriesList) {
        try {
          candleHandle.chart.removeSeries(series);
        } catch (err) {
          // the chart is gone already
        }
      }
    }
    drawn = [];
  }

  function isVisible(item) {
    return visibility.has(item.key) ? visibility.get(item.key) : item.defaultVisible;
  }

  function safeColor(color) {
    return isThemeOrHexColor(color, theme) ? color : theme.text;
  }

  function paintMarkers() {
    if (!markerApi) return;
    const colors = overlayColors(theme);
    const keyed = [];
    for (const entry of drawn) {
      if (entry.item.markers.length === 0 || !isVisible(entry.item)) continue;
      const color = colorOf(entry.item, colors);
      for (const marker of entry.item.markers) keyed.push({ ...marker, color, size: 1 });
    }
    const sorted = keyed.map((marker, index) => ({ marker, index }))
      .sort((x, y) => x.marker.time - y.marker.time || x.index - y.index).map((entry) => entry.marker);
    markerApi.setMarkers(sorted);
  }

  function recolor() {
    const colors = overlayColors(theme);
    for (const entry of drawn) {
      const color = colorOf(entry.item, colors);
      for (const series of entry.seriesList) series.applyOptions({ color });
      if (entry.swatch) {
        const property = entry.item.shape === "line" ? "background:" : "color:";
        entry.swatch.setAttribute("style", property + safeColor(color));
      }
    }
    paintMarkers();
  }

  function lineOptions(item, line, color, visible) {
    const types = lib.LineType || {};
    const styles = lib.LineStyle || {};
    return {
      color,
      lineWidth: line.width,
      lineType: line.step ? (types.WithSteps ?? 1) : (types.Simple ?? 0),
      lineStyle: line.style === "dotted" ? (styles.Dotted ?? 1) : (styles.Solid ?? 0),
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      visible,
    };
  }

  function entryText(item) {
    const parts = [];
    if (item.annotation) parts.push(item.annotation);
    if (item.dropped > 0) {
      parts.push(item.dropped + (item.dropped === 1 ? " point" : " points") + " not on a bar, not drawn");
    }
    if (item.count === 0 && item.dropped === 0) parts.push("none on this day");
    return parts.join("; ");
  }

  function toggle(entry) {
    const visible = !isVisible(entry.item);
    visibility.set(entry.item.key, visible);
    entry.button.setAttribute("aria-pressed", String(visible));
    for (const series of entry.seriesList) series.applyOptions({ visible });
    paintMarkers();
  }

  function clear() {
    if (disposed) return;
    removeSeriesQuietly();
    drawnSignature = null;
    if (markerApi) markerApi.setMarkers([]);
    toggles.replaceChildren();
    notes.replaceChildren();
    errors.replaceChildren();
    root.hidden = true;
  }

  function setData({ series = null, seriesError = null, orderEvents = null, ordersError = null, fills = null,
    fillModel = null, runStatus = null, bars = [] } = {}) {
    if (disposed) return;
    const times = barTimes(bars);
    if (times.length === 0 || (latestSignature !== null && latestSignature !== signature(times))) {
      clear(); // no bars, or bars that are not the chart's any more
      return;
    }
    removeSeriesQuietly();
    toggles.replaceChildren();
    notes.replaceChildren();
    errors.replaceChildren();
    const fromSeries = series && typeof series.run_status === "string" ? series.run_status : null;
    const status = runStatus === "failed" || fromSeries === "failed" ? "failed" : (runStatus || fromSeries);
    const plan = planOverlays({ series: seriesError ? null : series, orderEvents, fills, fillModel, runStatus: status, times, ordersError });
    const colors = overlayColors(theme);
    for (const item of plan.items) {
      const visible = isVisible(item);
      const color = colorOf(item, colors);
      const seriesList = item.lines.map((line) => {
        const created = candleHandle.chart.addSeries(lib.LineSeries, lineOptions(item, line, color, visible));
        created.setData(line.data);
        return created;
      });
      const swatch = h("span", {
        class: item.shape === "line" ? "swatch" : "swatch-mark", "aria-hidden": "true",
        style: (item.shape === "line" ? "background:" : "color:") + safeColor(color),
      }, item.shape === "marker-up" ? "▲" : item.shape === "marker-down" ? "▼" : item.shape === "marker-dot" ? "●" : null);
      const extra = entryText(item);
      const button = h("button", { type: "button", class: "overlay-toggle", "aria-pressed": String(visible) },
        swatch, h("span", { class: "overlay-name" }, item.name), " ",
        h("span", { class: "muted" }, item.kindText), extra ? h("span", { class: "muted" }, " " + extra) : null);
      const entry = { item, seriesList, button, swatch };
      button.addEventListener("click", () => toggle(entry));
      toggles.append(h("li", {}, button));
      drawn.push(entry);
    }
    if (seriesError) errors.append(errorPanel(seriesError));
    for (const text of plan.notes) notes.append(h("p", { class: "overlay-note muted", role: "note" }, text));
    drawnSignature = signature(times);
    paintMarkers();
    root.hidden = plan.items.length === 0 && plan.notes.length === 0 && !seriesError;
  }

  return { setData, clear, dispose };
}
