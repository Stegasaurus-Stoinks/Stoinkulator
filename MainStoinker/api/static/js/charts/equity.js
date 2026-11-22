// The equity chart: a stepped equity line per series in the upper pane, the drawdown of each series in a
// lower pane. Used by the run overview; the compare view overlays two runs on it. The pure parts are
// exported separately so node can test them without a DOM or the chart library.
//
// Exports: EMPTY_NOTE, prepareSeries, drawdownSeries, buildChartData, describeSeries, createEquityChart.
//
// prepareSeries(points, capital)  -> [{time, equity, drawdown}] for the points of the API's equity route
//                    ({time, equity, ...}, both strings): time is chart seconds (format.js
//                    dataTimeToChartTime, never new Date(text)), equity and drawdown are numbers for
//                    drawing only. The drawdown of a point is -(runningPeak - equity), zero at a peak and
//                    negative below it, where the running peak starts at `capital` (the definition of
//                    engine/stats.py max_drawdown, over every valid point in order). A point with an
//                    unreadable time or equity is skipped. Times must be strictly increasing: of
//                    points with the same time only the last is kept; one that goes back in time is
//                    dropped. `capital` is a string, a number or null (null: the first equity is the first
//                    peak).
// drawdownSeries(prepared)        -> [{time, value}] the drawdown column of prepareSeries.
// buildChartData(entry, {relative}) -> {equity: [{time, value}], drawdown: [{time, value}]} for one entry.
//                    Absolute (default): value is the equity itself. relative: true: value is the equity
//                    minus the entry's own starting capital (profit and loss from zero; capital null: the
//                    first equity), so runs with different capital share one scale. The drawdown is money
//                    below the running peak in both modes, so it is already in the same unit.
// describeSeries(list)            -> text for the chart's accessible label: per entry the start equity,
//                    final equity and largest drawdown (or "no equity points stored").
// createEquityChart(element, {ctx, lib, view, relative}) -> {setSeries(list), dispose()}
//     list: [{label, points, capital, color?}], one or two entries (more are drawn too). `color`, when
//     given, must be a #rgb / #rrggbb colour or one of the theme's own colours (it reaches a style
//     attribute); anything else is ignored in favour of the theme colour of that series.
//     `relative` (default false): draw each series as profit and loss from zero (see buildChartData), for
//     the compare view, where two runs may have different capital. The overview keeps the default.
//     `lib` is the Lightweight Charts namespace (default: the page's global LightweightCharts); `view`
//     the window (default: the global).
//     Registers dispose with ctx.onCleanup right after createChart succeeds. dispose() removes the theme
//     listener and the chart; it is safe to call twice, and after it nothing reaches the removed chart.
//     Inside `element`, in this order: the legend and the "No equity points stored" note (in normal
//     flow, so the legend never covers the curve), then `.chart-canvas`, a wrapper that holds only the
//     chart and carries role="img" with the text label of describeSeries. A series with a single point
//     shows point markers. An empty list or entries without points show the note and hide the canvas.
import { h } from "../dom.js";
import { dataTimeToChartTime, formatMoney } from "../format.js";
import { chartOptions, hexToRgba, isThemeOrHexColor, readTheme, seriesColor, watchTheme } from "./theme.js";

export const EMPTY_NOTE = "No equity points stored";

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function prepareSeries(points, capital) {
  const list = Array.isArray(points) ? points : [];
  let peak = toNumber(capital);
  const out = [];
  for (const point of list) {
    const time = point ? dataTimeToChartTime(point.time) : null;
    const equity = point ? toNumber(point.equity) : null;
    if (time === null || equity === null) continue;
    if (peak === null || equity > peak) peak = equity;
    const gap = peak - equity;
    const entry = { time, equity, drawdown: gap === 0 ? 0 : -gap };
    const last = out.length > 0 ? out[out.length - 1] : null;
    if (last && time === last.time) out[out.length - 1] = entry;
    else if (last && time < last.time) continue;
    else out.push(entry);
  }
  return out;
}

export function drawdownSeries(prepared) {
  return prepared.map((p) => ({ time: p.time, value: p.drawdown }));
}

export function buildChartData(entry, { relative = false } = {}) {
  const prepared = prepareSeries(entry.points, entry.capital);
  let base = 0;
  if (relative && prepared.length > 0) {
    const capital = toNumber(entry.capital);
    base = capital === null ? prepared[0].equity : capital;
  }
  return {
    equity: prepared.map((p) => ({ time: p.time, value: p.equity - base })),
    drawdown: drawdownSeries(prepared),
  };
}

function money(value) {
  return formatMoney(value.toFixed(2));
}

export function describeSeries(list) {
  const entries = Array.isArray(list) ? list : [];
  const head = "Equity over the run, stepped, with the drawdown below it";
  const parts = [];
  for (const entry of entries) {
    const prepared = prepareSeries(entry.points, entry.capital);
    const name = entry.label ? entry.label + ": " : "";
    if (prepared.length === 0) {
      parts.push(name + "no equity points stored");
      continue;
    }
    const worst = Math.min(...prepared.map((p) => p.drawdown));
    parts.push(name + "start equity " + money(prepared[0].equity) + ", final equity "
      + money(prepared[prepared.length - 1].equity) + ", largest drawdown " + money(-worst));
  }
  return parts.length === 0 ? head + ": no equity points stored." : head + ". " + parts.join(". ") + ".";
}

const PRICE_FORMAT = { type: "price", precision: 2, minMove: 0.01 };

export function createEquityChart(
  element, { ctx, lib = globalThis.LightweightCharts, view = globalThis, relative = false } = {}) {
  if (!lib || typeof lib.createChart !== "function") throw new Error("The chart library is not loaded.");
  let disposed = false;
  let unwatch = () => {};
  const legend = h("div", { class: "chart-legend" });
  const note = h("p", { class: "chart-note muted", role: "status", hidden: true }, EMPTY_NOTE);
  const canvas = h("div", { class: "chart-canvas", role: "img", "aria-label": describeSeries([]) });
  const chart = lib.createChart(canvas, { autoSize: true, ...chartOptions(readTheme(view)) });

  function dispose() {
    if (disposed) return;
    disposed = true;
    unwatch();
    try {
      chart.remove();
    } catch (err) {
      console.error("chart disposal failed", err);
    }
  }
  ctx.onCleanup(dispose);

  let entries = [];
  let drawn = []; // [{series, drawdown, entry, index}]
  element.append(legend, note, canvas);

  function colorOf(entry, index, theme) {
    return isThemeOrHexColor(entry.color, theme) ? entry.color : seriesColor(theme, index);
  }

  function clearSeries() {
    for (const item of drawn) {
      try {
        chart.removeSeries(item.series);
        chart.removeSeries(item.drawdown);
      } catch (err) {
        // the library already dropped it
      }
    }
    drawn = [];
  }

  function paintLegend(theme) {
    const items = entries.map((entry, index) => h("li", {},
      h("span", { class: "swatch", style: "background:" + colorOf(entry, index, theme) }),
      entry.label));
    const pane = relative
      ? "Upper pane: profit and loss since the start (equity minus starting capital). "
      : "Upper pane: equity. ";
    legend.replaceChildren(h("ul", {}, items),
      h("span", { class: "muted" }, pane + "Lower pane: drawdown below the running peak."));
  }

  function recolour(theme) {
    if (disposed) return;
    drawn.forEach((item) => {
      const color = colorOf(item.entry, item.index, theme);
      item.series.applyOptions({ color });
      item.drawdown.applyOptions({
        topLineColor: color, bottomLineColor: color,
        topFillColor1: hexToRgba(color, 0.05), topFillColor2: hexToRgba(color, 0.05),
        bottomFillColor1: hexToRgba(color, 0.05), bottomFillColor2: hexToRgba(color, 0.4),
      });
    });
    paintLegend(theme);
  }

  unwatch = watchTheme(chart, ctx, recolour, view);

  function setSeries(list) {
    if (disposed) return;
    clearSeries();
    entries = Array.isArray(list) ? list : [];
    const theme = readTheme(view);
    let any = false;
    entries.forEach((entry, index) => {
      const data = buildChartData(entry, { relative });
      if (data.equity.length === 0) return;
      any = true;
      const color = colorOf(entry, index, theme);
      const single = data.equity.length === 1; // a one-point line draws nothing without markers
      const series = chart.addSeries(lib.LineSeries, {
        color, lineWidth: 2, lineType: lib.LineType.WithSteps, priceFormat: PRICE_FORMAT,
        title: entries.length > 1 ? entry.label : "", lastValueVisible: true, priceLineVisible: false,
        pointMarkersVisible: single,
      }, 0);
      series.setData(data.equity);
      const drawdown = chart.addSeries(lib.BaselineSeries, {
        baseValue: { type: "price", price: 0 }, priceFormat: PRICE_FORMAT,
        topLineColor: color, bottomLineColor: color, lineWidth: 1,
        topFillColor1: hexToRgba(color, 0.05), topFillColor2: hexToRgba(color, 0.05),
        bottomFillColor1: hexToRgba(color, 0.05), bottomFillColor2: hexToRgba(color, 0.4),
        lastValueVisible: false, priceLineVisible: false, pointMarkersVisible: single,
      }, 1);
      drawdown.setData(data.drawdown);
      drawn.push({ series, drawdown, entry, index });
    });
    note.hidden = any;
    legend.hidden = !any;
    canvas.hidden = !any;
    canvas.setAttribute("aria-label", describeSeries(entries));
    if (any) {
      try {
        const panes = chart.panes();
        if (panes.length > 1) {
          panes[0].setStretchFactor(3);
          panes[1].setStretchFactor(1);
        }
      } catch (err) {
        // the pane sizes stay at the library's defaults
      }
      paintLegend(theme);
      chart.timeScale().fitContent();
    }
  }

  return { setSeries, dispose };
}
