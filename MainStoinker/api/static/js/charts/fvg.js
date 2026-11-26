// FVG zones on the candle chart of the algo detail view: the fair value gaps the detector finds in the bars
// on show (the fvg route, computed on request, nothing stored), drawn as shaded bands behind the candles.
// The pure parts are exported separately so node can test them without a DOM or the chart library.
//
// Exports: TIMEFRAMES, STORAGE_KEY, MAX_ZONES_NOTE, zoneSpans, hitZone, zoneLook, describeZone, readChoice,
// saveChoice, createFvgZones.
//
// Drawing: a series primitive of the chart library (`series.attachPrimitive`, version 5), which paints on the
// chart's own canvas in the pane's coordinates, so the bands follow zoom and scroll. No overlay element.
// Times are chart seconds (format.js dataTimeToChartTime). A zone is drawn from its `known_at` (never from
// `formed_time`, so the picture holds nothing the algo could not have known) to its `end_time`, or to the
// right edge of the chart while it is still live.
//
// zoneSpans(zones, times)   [{zone, start, end}] for the zones of the route: `start` and `end` are candle
//                           times (chart seconds) of `times`, the first candle at or after known_at / end_time;
//                           `end` is null for a zone that is live at the end of the data, or whose end is
//                           after the last candle (drawn to the right edge). A zone that is known only after
//                           the last candle, or ended before the first one, or has an unreadable time or price
//                           is left out.
// hitZone(spans, time, price)  the newest span that contains the point (a candle time and a price), or null.
// zoneLook(zone)            {live, dash, label, alpha, borderAlpha}: how a zone is drawn. Up zones are a solid
//                           outline with a "▲" label, down zones a dashed outline with a "▼" label; zones that
//                           are over (filled, invalidated, expired) are fainter, dotted and say their state.
// describeZone(zone)        the one-sentence description shown on hover or click.
// readChoice(view) / saveChoice(view, choice)  the viewer's choice {on, minutes} in localStorage (guarded; the
//                           page works without it). Off, 1 minute by default.
//
// createFvgZones(candleHandle, {ctx, container, fetchZones, lib, view}) -> {update, clear, dispose, enabled}
//     candleHandle: charts/candles.js createCandleChart's handle (chart, series, onBars, onTheme).
//     fetchZones({symbol, start, end, bar_minutes}, signal) -> a promise of the route's answer.
//     Adds one control to `container`: a checkbox "FVG zones" (off by default) and, beside it, the timeframe
//     (1, 5 or 15 min), then a status line and a details line. Nothing is requested while it is off.
//     update({symbol, start, end}) tells it which bars are on show; call it whenever the chart's bars are set
//     (a new day, a refresh of a live run). With the control on it asks the route for that window (the previous
//     request is aborted) and redraws; with it off it only remembers the window. clear() removes the zones
//     (the bars are gone). Registers dispose with ctx.onCleanup. When the library has no primitives it
//     returns an inert handle and adds nothing.
import { describeError, isAbortError } from "../api.js";
import { h } from "../dom.js";
import { dataTimeToChartTime } from "../format.js";
import { readTheme } from "./theme.js";

export const TIMEFRAMES = [1, 5, 15];
export const STORAGE_KEY = "stoinkulator.fvg.v1";
export const MAX_ZONES_NOTE = "More zones than can be shown: only the newest are drawn.";
const COLD_NOTE = "The data has no earlier day, so the ATR window starts cold here.";
const NON_CENT_NOTE = "No zones: some prices of these bars are not whole cents, which the detector refuses.";
const OVER = ["filled", "invalidated", "expired"];

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// Index of the first time at or after t in the sorted `times`, or -1.
function firstAtOrAfter(times, t) {
  let low = 0;
  let high = times.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (times[middle] < t) low = middle + 1;
    else high = middle;
  }
  return low < times.length ? low : -1;
}

export function zoneSpans(zones, times) {
  const list = Array.isArray(zones) ? zones : [];
  const candles = Array.isArray(times) ? times : [];
  const spans = [];
  if (candles.length === 0) return spans;
  for (const zone of list) {
    if (!zone) continue;
    const top = toNumber(zone.top);
    const bottom = toNumber(zone.bottom);
    const known = dataTimeToChartTime(zone.known_at);
    if (top === null || bottom === null || known === null) continue;
    const from = firstAtOrAfter(candles, known);
    if (from < 0) continue; // known only after the last candle: nothing to draw yet
    let end = null;
    if (zone.end_time !== null && zone.end_time !== undefined) {
      const ended = dataTimeToChartTime(zone.end_time);
      if (ended === null) continue;
      if (ended < candles[0]) continue; // over before the first candle on show
      const to = firstAtOrAfter(candles, ended);
      end = to < 0 ? null : candles[Math.max(to, from)];
    }
    spans.push({ zone, start: candles[from], end, top: Math.max(top, bottom), bottom: Math.min(top, bottom) });
  }
  return spans;
}

export function hitZone(spans, time, price) {
  if (!Array.isArray(spans) || typeof time !== "number" || typeof price !== "number"
      || !Number.isFinite(time) || !Number.isFinite(price)) return null;
  for (let i = spans.length - 1; i >= 0; i -= 1) {
    const span = spans[i];
    if (time < span.start || (span.end !== null && time > span.end)) continue;
    if (price >= span.bottom && price <= span.top) return span;
  }
  return null;
}

export function zoneLook(zone) {
  const up = zone && zone.direction === "up";
  const over = Boolean(zone) && OVER.includes(zone.state);
  const arrow = up ? "▲" : "▼";
  return {
    live: !over,
    dash: over ? [2, 3] : (up ? [] : [6, 4]),
    label: over ? arrow + " " + zone.state : arrow + (up ? " up" : " down"),
    alpha: over ? 0.07 : 0.2,
    borderAlpha: over ? 0.35 : 0.9,
  };
}

function priceText(value) {
  const n = toNumber(value);
  return n === null ? "?" : n.toFixed(2);
}

function minuteText(value) {
  return typeof value === "string" ? value.slice(0, 16) : "?";
}

export function describeZone(zone) {
  if (!zone) return "";
  const atr = toNumber(zone.size_atr);
  const fill = toNumber(zone.deepest_fill);
  const touches = toNumber(zone.touches);
  const direction = zone.direction === "up" ? "Up" : "Down";
  return direction + " zone " + priceText(zone.bottom) + " to " + priceText(zone.top)
    + " (top " + priceText(zone.top) + ", bottom " + priceText(zone.bottom) + "), size " + priceText(zone.size)
    + (atr === null ? "" : ", " + atr.toFixed(2) + " ATR")
    + ". State " + zone.state + ", " + (touches === null ? "?" : touches) + (touches === 1 ? " touch" : " touches")
    + (fill === null ? "" : ", deepest fill " + Math.round(fill * 100) + "%")
    + ". Formed " + minuteText(zone.formed_time) + ", known from " + minuteText(zone.known_at)
    + (zone.end_time ? ", over at " + minuteText(zone.end_time) : ", still live") + ".";
}

export function readChoice(view = globalThis) {
  const choice = { on: false, minutes: 1 };
  try {
    const stored = JSON.parse(view.localStorage.getItem(STORAGE_KEY));
    if (stored && typeof stored === "object") {
      choice.on = stored.on === true;
      if (TIMEFRAMES.includes(stored.minutes)) choice.minutes = stored.minutes;
    }
  } catch (err) {
    // no storage, or text that is not JSON: the defaults
  }
  return choice;
}

export function saveChoice(view, choice) {
  try {
    view.localStorage.setItem(STORAGE_KEY, JSON.stringify({ on: Boolean(choice.on), minutes: choice.minutes }));
  } catch (err) {
    // private window or blocked storage: the choice just is not kept
  }
}

// The primitive: paints the spans behind the candles, in the pane's bitmap coordinates.
function createPrimitive(getState) {
  let attachedParams = null;
  const renderer = {
    draw(target) {
      const state = getState();
      if (!attachedParams || state.spans.length === 0) return;
      const { chart, series } = attachedParams;
      target.useBitmapCoordinateSpace((scope) => {
        const { context, horizontalPixelRatio: hx, verticalPixelRatio: vy, bitmapSize } = scope;
        const scale = chart.timeScale();
        context.save();
        context.font = Math.round(11 * vy) + "px sans-serif";
        context.textBaseline = "top";
        for (const span of state.spans) {
          const x1 = scale.timeToCoordinate(span.start);
          const x2 = span.end === null ? null : scale.timeToCoordinate(span.end);
          const y1 = series.priceToCoordinate(span.top);
          const y2 = series.priceToCoordinate(span.bottom);
          if (x1 === null || y1 === null || y2 === null) continue;
          const left = Math.round(x1 * hx);
          const right = x2 === null ? bitmapSize.width : Math.max(Math.round(x2 * hx), left + 1);
          const top = Math.round(Math.min(y1, y2) * vy);
          const height = Math.max(Math.round(Math.abs(y2 - y1) * vy), 1);
          if (right < 0 || left > bitmapSize.width) continue;
          const look = zoneLook(span.zone);
          const color = span.zone.direction === "up" ? state.theme.accent : state.theme.warn;
          const picked = state.selected === span.zone.id && state.selectedMinutes === state.minutes;
          context.fillStyle = color;
          context.globalAlpha = picked ? Math.min(look.alpha + 0.15, 0.5) : look.alpha;
          context.fillRect(left, top, right - left, height);
          context.globalAlpha = look.borderAlpha;
          context.strokeStyle = color;
          context.lineWidth = Math.max(1, Math.round((picked ? 2 : 1) * hx));
          context.setLineDash(look.dash.map((n) => n * hx));
          context.strokeRect(left + 0.5, top + 0.5, Math.max(right - left - 1, 1), Math.max(height - 1, 1));
          if (height >= 14 * vy && right - left >= 40 * hx) {
            context.setLineDash([]);
            context.globalAlpha = look.live ? 1 : 0.6;
            context.fillStyle = color;
            context.fillText(look.label, left + 4 * hx, top + 2 * vy);
          }
        }
        context.restore();
      });
    },
  };
  const view = { zOrder: () => "bottom", renderer: () => renderer };
  return {
    attached(params) {
      attachedParams = params;
    },
    detached() {
      attachedParams = null;
    },
    updateAllViews() {},
    paneViews: () => [view],
    redraw() {
      if (attachedParams && typeof attachedParams.requestUpdate === "function") attachedParams.requestUpdate();
    },
  };
}

export function createFvgZones(candleHandle, { ctx, container, fetchZones, view = globalThis } = {}) {
  const inert = { update() {}, clear() {}, dispose() {}, enabled: () => false };
  if (!candleHandle || !candleHandle.series || typeof candleHandle.series.attachPrimitive !== "function"
      || typeof fetchZones !== "function") return inert;
  let disposed = false;
  const choice = readChoice(view);
  let theme = readTheme(view);
  let times = [];
  let zones = [];
  let spans = [];
  let zonesFor = null; // {symbol, start, end, minutes} of the zones held
  let wanted = null; // {symbol, start, end}: the bars on show
  let request = null;
  let hover = null;
  let pinned = null; // a clicked span
  const drawState = { spans, theme, selected: null, selectedMinutes: 1, minutes: choice.minutes };
  const primitive = createPrimitive(() => drawState);
  candleHandle.series.attachPrimitive(primitive);

  const checkbox = h("input", { type: "checkbox", class: "fvg-on" });
  const select = h("select", { class: "fvg-timeframe", "aria-label": "FVG zones timeframe" },
    TIMEFRAMES.map((m) => h("option", { value: String(m) }, m + " min")));
  checkbox.checked = choice.on;
  select.value = String(choice.minutes);
  const status = h("span", { class: "fvg-status muted", role: "status" });
  const detail = h("p", { class: "fvg-detail muted", role: "status", hidden: true });
  const root = h("div", { class: "fvg-control" },
    h("div", { class: "fvg-row" },
      h("label", { class: "fvg-on-label" }, checkbox, " FVG zones"), " ",
      h("label", { class: "fvg-timeframe-label" }, "Timeframe ", select), " ", status),
    detail);
  container.append(root);

  function paint() {
    drawState.spans = spans;
    drawState.theme = theme;
    drawState.minutes = choice.minutes;
    drawState.selected = pinned ? pinned.zone.id : null;
    drawState.selectedMinutes = zonesFor ? zonesFor.minutes : choice.minutes;
    primitive.redraw();
    const shown = pinned || hover;
    detail.textContent = shown ? describeZone(shown.zone) : "";
    detail.hidden = !shown;
  }

  function setStatus(text) {
    status.textContent = text || "";
  }

  function respan() {
    spans = zoneSpans(zones, times);
    hover = null;
    if (pinned) pinned = spans.find((s) => s.zone.id === pinned.zone.id) || null;
    paint();
  }

  function drop() {
    if (request) request.abort();
    request = null;
    zones = [];
    zonesFor = null;
    pinned = null;
    setStatus("");
    respan();
  }

  function load() {
    if (disposed || !choice.on || !wanted) return;
    const asked = { ...wanted, minutes: choice.minutes };
    const same = zonesFor && zonesFor.symbol === asked.symbol && zonesFor.start === asked.start
      && zonesFor.end === asked.end && zonesFor.minutes === asked.minutes;
    if (!same) { // another window or timeframe: the old zones would be wrong; a refresh keeps them meanwhile
      zones = [];
      pinned = null;
      respan();
    }
    if (request) request.abort();
    const mine = request = ctx.child();
    setStatus("Loading zones...");
    Promise.resolve().then(() => fetchZones({
      symbol: asked.symbol, start: asked.start, end: asked.end, bar_minutes: asked.minutes,
    }, mine.signal)).then((data) => {
      if (disposed || !mine.isCurrent()) return;
      const list = data && Array.isArray(data.zones) ? data.zones : null;
      if (list === null) throw new Error("The server sent an unexpected response.");
      zones = list;
      zonesFor = asked;
      let note = "";
      if (data.available !== true) note = "No zones: the bars cannot be shown, so neither can their zones.";
      else if (data.reason === "non_cent_price") note = NON_CENT_NOTE;
      else if (data.truncated === true) note = MAX_ZONES_NOTE;
      else if (list.length === 0) note = "No zones in this window.";
      if (data.available === true && data.warm === false) note = (note ? note + " " : "") + COLD_NOTE;
      setStatus(note);
      respan();
    }).catch((err) => {
      if (isAbortError(err) || disposed || !mine.isCurrent()) return;
      zones = [];
      zonesFor = null;
      setStatus("FVG zones could not be loaded. " + describeError(err));
      respan();
    });
  }

  function changed() {
    saveChoice(view, choice);
    if (choice.on) load();
    else drop();
  }
  checkbox.addEventListener("change", () => {
    choice.on = checkbox.checked;
    changed();
  });
  select.addEventListener("change", () => {
    const minutes = Number(select.value);
    if (!TIMEFRAMES.includes(minutes)) return;
    choice.minutes = minutes;
    changed();
  });

  function pointAt(param) {
    if (!param || !param.point || typeof param.time !== "number") return null;
    let price = null;
    try {
      price = candleHandle.series.coordinateToPrice(param.point.y);
    } catch (err) {
      price = null;
    }
    return hitZone(spans, param.time, price);
  }
  const onMove = (param) => {
    if (disposed) return;
    const next = pointAt(param);
    if ((next && next.zone.id) === (hover && hover.zone.id)) return;
    hover = next;
    paint();
  };
  const onClick = (param) => {
    if (disposed) return;
    const hit = pointAt(param);
    pinned = hit && !(pinned && pinned.zone.id === hit.zone.id) ? hit : null;
    paint();
  };
  const chart = candleHandle.chart;
  let subscribed = false;
  if (chart && typeof chart.subscribeCrosshairMove === "function" && typeof chart.subscribeClick === "function") {
    chart.subscribeCrosshairMove(onMove);
    chart.subscribeClick(onClick);
    subscribed = true;
  }
  const removeBars = candleHandle.onBars((bars) => {
    if (disposed) return;
    times = (Array.isArray(bars) ? bars : []).map((bar) => bar.time).filter((t) => typeof t === "number");
    if (times.length === 0) {
      zones = [];
      zonesFor = null;
      pinned = null;
    }
    respan();
  });
  const removeTheme = candleHandle.onTheme((next) => {
    if (disposed) return;
    theme = next;
    paint();
  });

  function dispose() {
    if (disposed) return;
    disposed = true;
    removeBars();
    removeTheme();
    if (request) request.abort();
    try {
      if (subscribed) {
        chart.unsubscribeCrosshairMove(onMove);
        chart.unsubscribeClick(onClick);
      }
      candleHandle.series.detachPrimitive(primitive);
    } catch (err) {
      // the chart is gone already
    }
    if (typeof root.remove === "function") root.remove();
  }
  ctx.onCleanup(dispose);

  return {
    update(next) {
      if (disposed) return;
      wanted = next && next.symbol && next.start && next.end
        ? { symbol: next.symbol, start: next.start, end: next.end } : null;
      if (!wanted) return;
      if (choice.on) load();
    },
    clear() {
      if (disposed) return;
      wanted = null;
      drop();
    },
    dispose,
    enabled: () => choice.on,
  };
}
