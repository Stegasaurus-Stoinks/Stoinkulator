// Pure formatting helpers. No DOM, no network, no clock.

const MONEY_RE = /^([+-]?)(\d+)(?:\.(\d*))?$/;

function groupThousands(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// Round a decimal string to 2 places, half away from zero, with integer arithmetic only.
// Returns {negative, text} where text is the unsigned magnitude with a thousands separator,
// or null when the input is not a plain decimal string.
function roundedMagnitude(value) {
  const m = MONEY_RE.exec(String(value).trim());
  if (!m) return null;
  const negative = m[1] === "-";
  const frac = (m[3] || "").padEnd(3, "0");
  let cents = BigInt(m[2] + frac.slice(0, 2));
  if (frac.charCodeAt(2) >= 53) cents += 1n; // third decimal digit >= 5
  const digits = cents.toString().padStart(3, "0");
  const whole = groupThousands(digits.slice(0, -2));
  const text = whole + "." + digits.slice(-2);
  return { negative: negative && cents !== 0n, text, zero: cents === 0n };
}

// "1234.5000" -> "1,234.50"; "-0.0040" -> "0.00". Anything else is returned as is, "-" if missing.
export function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "-";
  const r = roundedMagnitude(value);
  if (!r) return String(value);
  return (r.negative ? "-" : "") + r.text;
}

// Net values carry an explicit sign: "+1,234.50", "-12.00", "0.00".
export function formatNet(value) {
  if (value === null || value === undefined || value === "") return "-";
  const r = roundedMagnitude(value);
  if (!r) return String(value);
  if (r.zero) return r.text;
  return (r.negative ? "-" : "+") + r.text;
}

// "pos", "neg" or "" for styling a net value; the sign in the text carries the same information.
export function netClass(value) {
  const r = roundedMagnitude(value ?? "");
  if (!r || r.zero) return "";
  return r.negative ? "neg" : "pos";
}

// The win_rate fraction (0..1, or null when there are no trades) as a percentage.
export function formatPercent(fraction, digits = 1) {
  if (fraction === null || fraction === undefined || Number.isNaN(Number(fraction))) return "-";
  return (Number(fraction) * 100).toFixed(digits) + "%";
}

// Seconds (number or null) as "850 ms", "12.3 s", "3 m 05 s", "1 h 02 m".
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
  const s = Number(seconds);
  if (s < 1) return Math.round(s * 1000) + " ms";
  if (s < 60) return s.toFixed(1) + " s";
  const whole = Math.round(s);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const sec = whole % 60;
  const two = (n) => String(n).padStart(2, "0");
  if (h > 0) return h + " h " + two(m) + " m";
  return m + " m " + two(sec) + " s";
}

const ISO_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/;

// A run start or end as sent by the API (UTC ISO-8601): "2026-11-22 17:31:13 UTC".
export function formatUtc(iso) {
  if (!iso) return "-";
  const m = ISO_RE.exec(String(iso));
  return m ? m[1] + " " + m[2] + " UTC" : String(iso);
}

// A market time exactly as the API sends it (naive, never converted), labelled "data time".
export function formatDataTime(text) {
  if (!text) return "-";
  return String(text) + " (data time)";
}

// A code version, shortened: a long hex id keeps 8 characters and a "-dirty" or "+dirty" marker;
// a trailing ":digest" is dropped (the full text stays available as the cell's title).
export function shortVersion(version) {
  if (!version) return "-";
  const m = /^([0-9a-f]{9,})([^:]*)/i.exec(String(version));
  return m ? m[1].slice(0, 8) + m[2] : String(version);
}
