// Shared chart colours and options, read from the page's CSS custom properties so a chart follows the
// light and dark themes of app.css. No DOM is needed to import this file: every function that reads the
// page takes the window object (default: the global) so node can test it with a stand-in.
//
// Exports: FALLBACK_THEMES, readTheme, hexToRgba, crosshairTime, chartOptions, seriesColor, isThemeOrHexColor,
// watchTheme.
//
// readTheme(view)          {dark, text, muted, border, surface, accent, pos, neg, warn}: the colours of
//                          the page right now (CSS properties --text, --muted, --border, --surface,
//                          --accent, --pos, --neg, --warn-text); a property that is missing falls back
//                          to FALLBACK_THEMES for the current light or dark scheme.
// chartOptions(theme)      options for createChart / chart.applyOptions: panel background, text and
//                          grid colours, time axis in data time (UTC clock, no seconds). The axis needs no
//                          tickMarkFormatter: the vendored library 5.3.0 formats its default tick marks
//                          with timeZone "UTC", which is the data wall clock (format.js dataTimeToChartTime).
//                          localization.timeFormatter (the crosshair label) is crosshairTime.
// crosshairTime(t)         chart seconds as "YYYY-MM-DD HH:MM" (data time); a value that is not a number is
//                          returned as text.
// isThemeOrHexColor(c, theme)  true for a #rgb / #rrggbb colour or one of the theme's own colours: the only
//                          colours a caller may pass on to a style attribute.
// seriesColor(theme, i)    the default colour of the i-th series on a chart (accent, then warn, ...).
// hexToRgba(color, alpha)  "#rrggbb" or "#rgb" as "rgba(r, g, b, alpha)"; anything else is returned as is.
// watchTheme(chart, ctx, onChange, view)
//                          re-applies chartOptions when prefers-color-scheme changes, then calls
//                          onChange(theme). The listener's removal is registered with ctx.onCleanup.
//                          Returns a function that removes the listener early (safe to call twice).

import { chartTimeToDataTime } from "../format.js";

export const FALLBACK_THEMES = {
  light: {
    text: "#1c2230", muted: "#5a6477", border: "#d5d9e2", surface: "#ffffff",
    accent: "#1f5fbf", pos: "#0b6b3a", neg: "#b3261e", warn: "#6b4a00",
  },
  dark: {
    text: "#e6e9f0", muted: "#9aa4b8", border: "#333a4d", surface: "#1a1e28",
    accent: "#6ea2f5", pos: "#5fd08c", neg: "#ff8a80", warn: "#f2d58a",
  },
};

const PROPERTIES = {
  text: "--text", muted: "--muted", border: "--border", surface: "--surface",
  accent: "--accent", pos: "--pos", neg: "--neg", warn: "--warn-text",
};

const SCHEME_QUERY = "(prefers-color-scheme: dark)";

function prefersDark(view) {
  try {
    return Boolean(view.matchMedia && view.matchMedia(SCHEME_QUERY).matches);
  } catch (err) {
    return false;
  }
}

export function readTheme(view = globalThis) {
  const dark = prefersDark(view);
  const fallback = FALLBACK_THEMES[dark ? "dark" : "light"];
  let style = null;
  try {
    if (view.getComputedStyle && view.document && view.document.documentElement) {
      style = view.getComputedStyle(view.document.documentElement);
    }
  } catch (err) {
    style = null;
  }
  const theme = { dark };
  for (const [key, property] of Object.entries(PROPERTIES)) {
    const value = style ? String(style.getPropertyValue(property) || "").trim() : "";
    theme[key] = value || fallback[key];
  }
  return theme;
}

export function hexToRgba(color, alpha) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(color).trim());
  if (!m) return color;
  const digits = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  const n = Number.parseInt(digits, 16);
  return "rgba(" + [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(", ") + ", " + alpha + ")";
}

export function crosshairTime(time) {
  const text = chartTimeToDataTime(time);
  return text === null ? String(time) : text.slice(0, 16);
}

const HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;
const THEME_COLOR_KEYS = ["text", "muted", "border", "surface", "accent", "pos", "neg", "warn"];

export function isThemeOrHexColor(color, theme) {
  if (typeof color !== "string") return false;
  if (HEX_COLOR.test(color)) return true;
  return THEME_COLOR_KEYS.some((key) => theme && theme[key] === color);
}

export function chartOptions(theme) {
  return {
    layout: {
      background: { type: "solid", color: theme.surface },
      textColor: theme.muted,
      panes: { separatorColor: theme.border },
    },
    grid: {
      vertLines: { color: hexToRgba(theme.border, 0.6) },
      horzLines: { color: hexToRgba(theme.border, 0.6) },
    },
    rightPriceScale: { borderColor: theme.border },
    localization: { timeFormatter: crosshairTime },
    timeScale: { borderColor: theme.border, timeVisible: true, secondsVisible: false },
    crosshair: {
      vertLine: { labelBackgroundColor: theme.muted },
      horzLine: { labelBackgroundColor: theme.muted },
    },
  };
}

export function seriesColor(theme, index) {
  const palette = [theme.accent, theme.warn, theme.pos, theme.neg, theme.text];
  return palette[index % palette.length];
}

export function watchTheme(chart, ctx, onChange, view = globalThis) {
  let query = null;
  try {
    query = view.matchMedia ? view.matchMedia(SCHEME_QUERY) : null;
  } catch (err) {
    query = null;
  }
  if (!query) return () => {};
  let removed = false;
  const listener = () => {
    if (removed) return;
    const theme = readTheme(view);
    chart.applyOptions(chartOptions(theme));
    if (onChange) onChange(theme);
  };
  const remove = () => {
    if (removed) return;
    removed = true;
    if (query.removeEventListener) query.removeEventListener("change", listener);
    else if (query.removeListener) query.removeListener(listener);
  };
  if (query.addEventListener) query.addEventListener("change", listener);
  else if (query.addListener) query.addListener(listener);
  ctx.onCleanup(remove);
  return remove;
}
