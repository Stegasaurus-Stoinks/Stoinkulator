// The equity chart: a stepped equity line per series in the upper pane, the drawdown of each series in a
// lower pane. Used by the run overview; the compare view overlays two runs on it. The pure parts are
// exported separately so node can test them without a DOM or the chart library.
//
// Exports: EMPTY_NOTE, prepareSeries, drawdownSeries, buildChartData, createEquityChart.
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
// buildChartData(entry)           -> {equity: [{time, value}], drawdown: [{time, value}]} for one entry.
// createEquityChart(element, {ctx, lib, view}) -> {setSeries(list), dispose()}
//     list: [{label, points, capital, color?}], one or two entries (more are drawn too). Registers
//     dispose with ctx.onCleanup at once. `lib` is the Lightweight Charts namespace (default: the page's
//     global LightweightCharts); `view` the window (default: the global).
//     An empty list or entries without points show "No equity points stored" instead of a chart.
import { h } from "../dom.js";
import { dataTimeToChartTime } from "../format.js";
import { chartOptions, hexToRgba, readTheme, seriesColor, watchTheme } from "./theme.js";

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

export function buildChartData(entry) {
  const prepared = prepareSeries(entry.points, entry.capital);
  return {
    equity: prepared.map((p) => ({ time: p.time, value: p.equity })),
    drawdown: drawdownSeries(prepared),
  };
}

const PRICE_FORMAT = { type: "price", precision: 2, minMove: 0.01 };

export function createEquityChart(element, { ctx, lib = globalThis.LightweightCharts, view = globalThis } = {}) {
  if (!lib || typeof lib.createChart !== "function") throw new Error("The chart library is not loaded.");
  const chart = lib.createChart(element, { autoSize: true, ...chartOptions(readTheme(view)) });
  let disposed = false;
  let entries = [];
  let drawn = []; // [{series, drawdown, entry, index}]
  const legend = h("div", { class: "chart-legend" });
  const note = h("p", { class: "chart-note muted", role: "status", hidden: true }, EMPTY_NOTE);
  element.append(legend, note);

  function colorOf(entry, index, theme) {
    return entry.color || seriesColor(theme, index);
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
    legend.replaceChildren(h("ul", {}, items),
      h("span", { class: "muted" }, "Upper pane: equity. Lower pane: drawdown below the running peak."));
  }

  function recolour(theme) {
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

  watchTheme(chart, ctx, recolour, view);

  function dispose() {
    if (disposed) return;
    disposed = true;
    try {
      chart.remove();
    } catch (err) {
      console.error("chart disposal failed", err);
    }
  }
  ctx.onCleanup(dispose);

  function setSeries(list) {
    if (disposed) return;
    clearSeries();
    entries = Array.isArray(list) ? list : [];
    const theme = readTheme(view);
    let any = false;
    entries.forEach((entry, index) => {
      const data = buildChartData(entry);
      if (data.equity.length === 0) return;
      any = true;
      const color = colorOf(entry, index, theme);
      const series = chart.addSeries(lib.LineSeries, {
        color, lineWidth: 2, lineType: lib.LineType.WithSteps, priceFormat: PRICE_FORMAT,
        title: entries.length > 1 ? entry.label : "", lastValueVisible: true, priceLineVisible: false,
      }, 0);
      series.setData(data.equity);
      const drawdown = chart.addSeries(lib.BaselineSeries, {
        baseValue: { type: "price", price: 0 }, priceFormat: PRICE_FORMAT,
        topLineColor: color, bottomLineColor: color, lineWidth: 1,
        topFillColor1: hexToRgba(color, 0.05), topFillColor2: hexToRgba(color, 0.05),
        bottomFillColor1: hexToRgba(color, 0.05), bottomFillColor2: hexToRgba(color, 0.4),
        lastValueVisible: false, priceLineVisible: false,
      }, 1);
      drawdown.setData(data.drawdown);
      drawn.push({ series, drawdown, entry, index });
    });
    note.hidden = any;
    legend.hidden = !any;
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
