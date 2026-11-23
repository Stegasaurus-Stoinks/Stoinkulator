// The candle chart of the algo detail view: 1-minute candlesticks of one window of bars with the entry and
// exit of each trade marked on them. The pure parts are exported separately so node can test them without a
// DOM or the chart library.
//
// Exports: MARKER_LEGEND, convertBars, snapIndex, tradesInWindow, shortReason, resultSign, buildMarkers,
// focusRange, focusSpanRange, describeCandles, createCandleChart.
//
// Times are chart seconds (format.js dataTimeToChartTime, never new Date(text)): the library draws its axis
// in UTC, which is then the data's own wall clock.
//
// convertBars(bars)       -> [{time, open, high, low, close}] from the bars route ({time, open, high, low,
//                    close, volume}; time is the naive data time text). Strictly increasing in time: of rows
//                    with the same time the last is kept, a row that goes back in time is dropped, and a row
//                    with an unreadable time or price is skipped.
// snapIndex(times, t)     index in the sorted `times` of the last bar at or before t, or -1.
// tradesInWindow(trades, first, last)  the trades (as the trades route sends them) with an entry or an exit
//                    time inside [first, last] (chart seconds), order kept.
// shortReason(reason)     the exit reason as short marker text ("algo_exit" -> "exit", "end_of_data" -> "end").
// resultSign(net)         "+" for a profit, "-" for a loss, "" for zero or an unreadable net.
// buildMarkers(trades, times, selectedKey, theme)  -> the markers for the trades that fall inside the loaded
//                    bars, sorted by time (stable), each placed on the last bar at or before its time.
//                    `trades` are trade records that also carry `key` and `number` (the view's row ids);
//                    `selectedKey` is the key of the selected trade (or null); `theme` is charts/theme.js'
//                    readTheme. Per trade:
//                      entry  a long: arrowUp below the bar; a short: arrowDown above the bar; the accent
//                             colour; no text;
//                      exit   a circle on the exit bar (above the bar for a long, below it for a short),
//                             green for a profit, red for a loss, muted for zero, text = the sign of the
//                             result then shortReason ("+ stop", "- exit"), so profit and loss are not
//                             shown by colour alone;
//                    only the markers whose own time is inside the loaded bars are built (a trade that
//                    enters on another day shows just its exit here). The selected trade's markers are
//                    larger (size 2) and their text starts with "#<number>"; with a selection the other
//                    markers are drawn fainter. "Has a selection" is decided only from the trades that have
//                    a marker inside the loaded bars, so a selection on another day fades nothing here.
// focusRange(times, time, halfWidth)  {from, to} (chart seconds) centred on `time`, `halfWidth` seconds on
//                    each side, kept inside the loaded bars; null when there are no bars or the range would
//                    be empty.
// focusSpanRange(times, from, to)  {from, to} covering from..to with a margin (10% of the span, at least
//                    5 minutes) on each side, kept inside the loaded bars; null as focusRange.
// describeCandles(times, markerCount)  the accessible label of the chart canvas.
//
// createCandleChart(element, {ctx, lib, view}) -> handle
//     lib: the Lightweight Charts namespace (default: the page's global LightweightCharts); view: the window
//     (default: the global).
//     Inside `element`, in this order: the legend (in normal flow, so it never covers the candles), the note,
//     then `.chart-canvas`, a wrapper that holds only the chart and carries role="img" with a text label.
//     Registers dispose with ctx.onCleanup right after createChart succeeds. dispose() removes the theme
//     listener and the chart; it is safe to call twice, and after it nothing reaches the removed chart.
//     handle methods:
//       setBars(bars)                 draw the bars of the route's response (see convertBars) and fit them;
//                                     an empty list hides the canvas. Markers are rebuilt for the new bars.
//       setTrades(trades, selectedKey) the trade records (with key and number, see buildMarkers) and the
//                                     selected key (or null); redraws the markers.
//       focusTime(time)               scroll and zoom the chart to `time` (chart seconds, or a data time
//                                     text) with a window of about two hours around it; ignored when it is
//                                     not a time or no bars are loaded.
//       focusSpan(from, to)           the same for a span (a trade from entry to exit), with a margin.
//       onBars(fn)                    call fn(bars) after every setBars with the converted bars (what the
//                                     chart now holds, [] when emptied); returns a function that removes
//                                     fn. Overlays use it so they never stay drawn against stale bars.
//       setNote(text)                 a status line above the chart ("" hides it): why there are no candles,
//                                     or a caution about the ones shown.
//       dispose()
//     The time scale is refitted when the chart's width changes (a resize, a phone turned), unless the
//     user has zoomed it (wheel, two-finger touch, a pointer drag of more than 4 px) or a focus was
//     applied; setBars fits again and clears that state.
//     SEAM for the indicator overlays (charts/overlays.js, wired by views/algo.js):
//       handle.chart       the Lightweight Charts chart: add overlay series with handle.chart.addSeries(...)
//                          (pane 0 shares the price scale of the candles) and remove them again; the chart is
//                          removed by dispose, which also drops what was added to it.
//       handle.series      the candlestick series (attach price lines, or markers of its own, to it).
//       handle.legend      the legend element: append the overlay toggles to it.
//       handle.onTheme(fn) call fn(theme) after the page's colour scheme changed (the chart's own options and
//                          the candles are already re-coloured); returns a function that removes fn.
//       handle.window()    {from, to} chart seconds of the loaded bars, or null: the time range an overlay's
//                          data must be requested for.
import { h } from "../dom.js";
import { chartTimeToDataTime, dataTimeToChartTime } from "../format.js";
import { chartOptions, hexToRgba, readTheme, watchTheme } from "./theme.js";

export const MARKER_LEGEND = [
  { shape: "▲", text: "long entry (below the bar)" },
  { shape: "▼", text: "short entry (above the bar)" },
  { shape: "●", className: "pos", text: "profitable exit: + and its reason" },
  { shape: "●", className: "neg", text: "losing exit: - and its reason" },
];

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function convertBars(bars) {
  const list = Array.isArray(bars) ? bars : [];
  const out = [];
  for (const bar of list) {
    const time = bar ? dataTimeToChartTime(bar.time) : null;
    const open = bar ? toNumber(bar.open) : null;
    const high = bar ? toNumber(bar.high) : null;
    const low = bar ? toNumber(bar.low) : null;
    const close = bar ? toNumber(bar.close) : null;
    if (time === null || open === null || high === null || low === null || close === null) continue;
    const entry = { time, open, high, low, close };
    const last = out.length > 0 ? out[out.length - 1] : null;
    if (last && time === last.time) out[out.length - 1] = entry;
    else if (last && time < last.time) continue;
    else out.push(entry);
  }
  return out;
}

export function snapIndex(times, t) {
  let low = 0;
  let high = times.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (times[middle] <= t) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

function insideWindow(t, first, last) {
  return t !== null && t >= first && t <= last;
}

export function tradesInWindow(trades, first, last) {
  const list = Array.isArray(trades) ? trades : [];
  return list.filter((trade) => trade && (
    insideWindow(dataTimeToChartTime(trade.entry_time), first, last)
    || insideWindow(dataTimeToChartTime(trade.exit_time), first, last)));
}

const SHORT_REASONS = { algo_exit: "exit", end_of_data: "end" };
const MAX_REASON = 12;

export function shortReason(reason) {
  if (reason === null || reason === undefined || reason === "") return "";
  const text = String(reason);
  if (Object.hasOwn(SHORT_REASONS, text)) return SHORT_REASONS[text];
  return text.length > MAX_REASON ? text.slice(0, MAX_REASON - 1) + "…" : text;
}

function sameKey(a, b) {
  return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
}

export function resultSign(net) {
  const n = toNumber(net);
  return n === null || n === 0 ? "" : n > 0 ? "+" : "-";
}

export function buildMarkers(trades, times, selectedKey, theme) {
  const list = Array.isArray(trades) ? trades : [];
  if (times.length === 0) return [];
  const first = times[0];
  const last = times[times.length - 1];
  const hasSelection = selectedKey !== null && selectedKey !== undefined
    && list.some((trade) => trade && sameKey(trade.key, selectedKey) && (
      insideWindow(dataTimeToChartTime(trade.entry_time), first, last)
      || insideWindow(dataTimeToChartTime(trade.exit_time), first, last)));
  const markers = [];
  for (const trade of list) {
    if (!trade) continue;
    const selected = hasSelection && sameKey(trade.key, selectedKey);
    const faint = hasSelection && !selected;
    const tint = (color) => (faint ? hexToRgba(color, 0.55) : color);
    const prefix = selected ? "#" + trade.number : "";
    const long = trade.side !== "short";
    const entryTime = dataTimeToChartTime(trade.entry_time);
    if (insideWindow(entryTime, first, last)) {
      markers.push({
        time: times[snapIndex(times, entryTime)],
        position: long ? "belowBar" : "aboveBar",
        shape: long ? "arrowUp" : "arrowDown",
        color: tint(theme.accent),
        size: selected ? 2 : 1,
        id: trade.key + ":entry",
        text: prefix,
      });
    }
    const exitTime = dataTimeToChartTime(trade.exit_time);
    if (insideWindow(exitTime, first, last)) {
      const net = toNumber(trade.net);
      const color = net === null || net === 0 ? theme.muted : net > 0 ? theme.pos : theme.neg;
      markers.push({
        time: times[snapIndex(times, exitTime)],
        position: long ? "aboveBar" : "belowBar",
        shape: "circle",
        color: tint(color),
        size: selected ? 2 : 1,
        id: trade.key + ":exit",
        text: [prefix, resultSign(trade.net), shortReason(trade.exit_reason)].filter(Boolean).join(" "),
      });
    }
  }
  const keyed = markers.map((marker, index) => ({ marker, index }));
  keyed.sort((x, y) => x.marker.time - y.marker.time || x.index - y.index);
  return keyed.map((entry) => entry.marker);
}

export function focusRange(times, time, halfWidth) {
  if (times.length === 0 || typeof time !== "number" || !Number.isFinite(time)) return null;
  const first = times[0];
  const last = times[times.length - 1];
  const from = Math.max(first, time - halfWidth);
  const to = Math.min(last, time + halfWidth);
  return from < to ? { from, to } : null;
}

const MIN_MARGIN = 300; // seconds

export function focusSpanRange(times, from, to) {
  if (times.length === 0 || typeof from !== "number" || typeof to !== "number"
      || !Number.isFinite(from) || !Number.isFinite(to)) return null;
  const low = Math.min(from, to);
  const high = Math.max(from, to);
  const margin = Math.max(MIN_MARGIN, (high - low) * 0.1);
  const start = Math.max(times[0], low - margin);
  const end = Math.min(times[times.length - 1], high + margin);
  return start < end ? { from: start, to: end } : null;
}

export function describeCandles(times, markerCount) {
  if (times.length === 0) return "Candlestick chart: no bars.";
  const range = chartTimeToDataTime(times[0]) + " to " + chartTimeToDataTime(times[times.length - 1]);
  const marks = markerCount === 1 ? "1 entry or exit marker" : markerCount + " entry and exit markers";
  return "Candlestick chart: " + times.length + " one-minute bars from " + range + " (data time), with "
    + marks + ".";
}

const PRICE_FORMAT = { type: "price", precision: 2, minMove: 0.01 };
const FOCUS_HALF_WIDTH = 3600; // seconds on each side of a focused time
const DRAG_PIXELS = 4; // a pointer drag longer than this counts as the user moving the chart

function candleOptions(theme) {
  return {
    upColor: theme.pos,
    downColor: theme.neg,
    wickUpColor: theme.pos,
    wickDownColor: theme.neg,
    borderVisible: false,
    priceFormat: PRICE_FORMAT,
    lastValueVisible: false,
    priceLineVisible: false,
  };
}

export function createCandleChart(element, { ctx, lib = globalThis.LightweightCharts, view = globalThis } = {}) {
  if (!lib || typeof lib.createChart !== "function") throw new Error("The chart library is not loaded.");
  let disposed = false;
  let unwatch = () => {};
  const zoomListeners = []; // [type, fn] of the canvas listeners that tell a user zoom from a resize
  const legend = h("div", { class: "chart-legend" },
    h("ul", { class: "marker-key" }, MARKER_LEGEND.map((item) => h("li", {},
      h("span", { class: "marker-shape" + (item.className ? " " + item.className : ""), "aria-hidden": "true" },
        item.shape), item.text))),
    h("span", { class: "muted" }, "The selected trade's markers are larger and numbered."));
  const note = h("p", { class: "chart-note muted", role: "status", hidden: true });
  const canvas = h("div", { class: "chart-canvas", role: "img", "aria-label": describeCandles([], 0) });
  const chart = lib.createChart(canvas, { autoSize: true, ...chartOptions(readTheme(view)) });

  function dispose() {
    if (disposed) return;
    disposed = true;
    unwatch();
    for (const [type, fn] of zoomListeners) {
      if (typeof canvas.removeEventListener === "function") canvas.removeEventListener(type, fn);
    }
    try {
      if (typeof chart.timeScale().unsubscribeSizeChange === "function") {
        chart.timeScale().unsubscribeSizeChange(onSizeChange);
      }
    } catch (err) {
      // the chart is going away anyway
    }
    try {
      chart.remove();
    } catch (err) {
      console.error("chart disposal failed", err);
    }
  }
  ctx.onCleanup(dispose);

  element.append(legend, note, canvas);
  let theme = readTheme(view);
  const series = chart.addSeries(lib.CandlestickSeries, candleOptions(theme));
  const markerApi = lib.createSeriesMarkers(series, []);
  let times = [];
  let trades = [];
  let selected = null;
  const themeListeners = [];
  const barListeners = [];

  // The time scale follows the chart's width until the user (or a focus) takes it over.
  let userZoomed = false;
  let lastWidth = null;
  let dragStart = null;
  function listen(type, fn, options) {
    canvas.addEventListener(type, fn, options);
    zoomListeners.push([type, fn]);
  }
  listen("wheel", () => { userZoomed = true; }, { passive: true });
  listen("touchstart", (event) => {
    if (event && event.touches && event.touches.length >= 2) userZoomed = true;
  }, { passive: true });
  listen("pointerdown", (event) => {
    dragStart = event && event.buttons ? { x: event.clientX, y: event.clientY } : null;
  });
  listen("pointermove", (event) => {
    if (!dragStart || !event || !event.buttons) return;
    if (Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > DRAG_PIXELS) userZoomed = true;
  });
  for (const type of ["pointerup", "pointercancel"]) listen(type, () => { dragStart = null; });

  function onSizeChange() {
    if (disposed) return;
    let width = null;
    try {
      width = chart.timeScale().width();
    } catch (err) {
      width = null;
    }
    if (width !== null && width === lastWidth) return; // a height-only change
    lastWidth = width;
    if (!userZoomed && times.length > 0) chart.timeScale().fitContent();
  }

  try {
    if (typeof chart.timeScale().subscribeSizeChange === "function") chart.timeScale().subscribeSizeChange(onSizeChange);
  } catch (err) {
    console.error("size change subscription failed", err);
  }

  function paintMarkers() {
    const markers = buildMarkers(trades, times, selected, theme);
    markerApi.setMarkers(markers);
    canvas.setAttribute("aria-label", describeCandles(times, markers.length));
  }

  unwatch = watchTheme(chart, ctx, (next) => {
    if (disposed) return;
    theme = next;
    series.applyOptions(candleOptions(theme));
    paintMarkers();
    for (const fn of themeListeners.slice()) fn(theme);
  }, view);

  function setBars(bars) {
    if (disposed) return;
    const converted = convertBars(bars);
    times = converted.map((bar) => bar.time);
    series.setData(converted);
    canvas.hidden = converted.length === 0;
    legend.hidden = converted.length === 0;
    paintMarkers();
    userZoomed = false;
    if (converted.length > 0) chart.timeScale().fitContent();
    for (const fn of barListeners.slice()) {
      try {
        fn(converted);
      } catch (err) {
        console.error("bars listener failed", err);
      }
    }
  }

  function setTrades(list, selectedKey) {
    if (disposed) return;
    trades = Array.isArray(list) ? list : [];
    selected = selectedKey === undefined ? null : selectedKey;
    paintMarkers();
  }

  function focusTime(time) {
    if (disposed) return;
    const seconds = typeof time === "string" ? dataTimeToChartTime(time) : time;
    showRange(focusRange(times, seconds, FOCUS_HALF_WIDTH));
  }

  function showRange(range) {
    if (!range) return;
    userZoomed = true; // a resize must not undo a focus
    try {
      chart.timeScale().setVisibleRange(range);
    } catch (err) {
      // the library refused the range: the chart stays where it is
    }
  }

  function focusSpan(from, to) {
    if (disposed) return;
    const start = typeof from === "string" ? dataTimeToChartTime(from) : from;
    const end = typeof to === "string" ? dataTimeToChartTime(to) : to;
    showRange(focusSpanRange(times, start, end));
  }

  function setNote(text) {
    if (disposed) return;
    note.textContent = text || "";
    note.hidden = !text;
  }

  return {
    setBars,
    setTrades,
    focusTime,
    focusSpan,
    setNote,
    dispose,
    chart,
    series,
    legend,
    onBars(fn) {
      barListeners.push(fn);
      return () => {
        const at = barListeners.indexOf(fn);
        if (at >= 0) barListeners.splice(at, 1);
      };
    },
    onTheme(fn) {
      themeListeners.push(fn);
      return () => {
        const at = themeListeners.indexOf(fn);
        if (at >= 0) themeListeners.splice(at, 1);
      };
    },
    window: () => (times.length > 0 ? { from: times[0], to: times[times.length - 1] } : null),
  };
}
