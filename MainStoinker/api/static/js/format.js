// Pure formatting helpers. No DOM, no network, no clock.
//
// Exports: formatMoney, formatNet, netClass, formatPercent, formatDuration, formatUtc,
// formatDataTime, shortVersion.
// Money and percentages are decimal strings from the API; they are rounded with integer (BigInt)
// arithmetic, half away from zero (the CLI's ROUND_HALF_UP), never through floating point.

const DECIMAL_RE = /^([+-]?)(\d+)(?:\.(\d*))?$/;

function groupThousands(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// value * 10^shift rounded to `places` decimals, half away from zero, from a plain decimal string
// (or a number that prints as one). Returns {negative, whole, fraction, zero} with unsigned text parts
// ("-0.00" never occurs: a magnitude that rounds to zero is not negative), or null when the input
// is not a plain decimal.
function roundDecimal(value, shift, places) {
  const m = DECIMAL_RE.exec(String(value).trim());
  if (!m) return null;
  const frac = m[3] || "";
  const digits = BigInt(m[2] + frac); // value = digits / 10^frac.length
  const drop = frac.length - shift - places; // digits to discard to land on `places` decimals
  let scaled;
  if (drop <= 0) {
    scaled = digits * 10n ** BigInt(-drop);
  } else {
    const divisor = 10n ** BigInt(drop);
    scaled = digits / divisor;
    if ((digits % divisor) * 2n >= divisor) scaled += 1n;
  }
  const text = scaled.toString().padStart(places + 1, "0");
  const cut = text.length - places;
  return {
    negative: m[1] === "-" && scaled !== 0n,
    whole: text.slice(0, cut),
    fraction: text.slice(cut),
    zero: scaled === 0n,
  };
}

function money(value) {
  const r = roundDecimal(value, 0, 2);
  return r && { ...r, text: groupThousands(r.whole) + "." + r.fraction };
}

// "1234.5000" -> "1,234.50"; "-0.0040" -> "0.00". Anything else is returned as is, "-" if missing.
export function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "-";
  const r = money(value);
  if (!r) return String(value);
  return (r.negative ? "-" : "") + r.text;
}

// Net values carry an explicit sign: "+1,234.50", "-12.00", "0.00".
export function formatNet(value) {
  if (value === null || value === undefined || value === "") return "-";
  const r = money(value);
  if (!r) return String(value);
  if (r.zero) return r.text;
  return (r.negative ? "-" : "+") + r.text;
}

// "pos", "neg" or "" for styling a net value; the sign in the text carries the same information.
export function netClass(value) {
  if (value === null || value === undefined || value === "") return "";
  const r = money(value);
  if (!r || r.zero) return "";
  return r.negative ? "neg" : "pos";
}

// The win_rate fraction (a decimal string such as "0.0375", or null when there are no trades) as a
// percentage with `digits` decimals and no percent sign, rounded half up on the exact decimal string
// exactly like the `report` command: "0.0015" -> "0.2", "0.0375" -> "3.8", "0.5000" -> "50.0".
export function formatPercent(fraction, digits = 1) {
  if (fraction === null || fraction === undefined || fraction === "") return "-";
  const r = roundDecimal(fraction, 2, digits);
  if (!r) return String(fraction);
  return (r.negative ? "-" : "") + r.whole + (digits > 0 ? "." + r.fraction : "");
}

// Seconds (number or null) as "850 ms", "12.3 s", "3 m 05 s", "1 h 02 m".
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds === "" || Number.isNaN(Number(seconds))) return "-";
  const s = Math.max(Number(seconds), 0);
  if (s < 1) {
    const ms = Math.round(s * 1000);
    if (ms < 1000) return ms + " ms"; // 0.9996 s is "1.0 s", not "1000 ms"
  }
  if (s < 59.95) return s.toFixed(1) + " s"; // from 59.95 the tenth rounds up to a minute
  const whole = Math.round(s);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const sec = whole % 60;
  const two = (n) => String(n).padStart(2, "0");
  if (hours > 0) return hours + " h " + two(minutes) + " m";
  return minutes + " m " + two(sec) + " s";
}

const TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

function pad(n, width = 2) {
  return String(n).padStart(width, "0");
}

// A run start or end as sent by the API (ISO-8601 with an offset) in UTC: "2026-11-22 17:31:13 UTC".
// A non-zero offset is converted to UTC. A value that is not a timestamp with an explicit offset
// (naive, malformed or out of range) is shown as given, never labelled UTC.
export function formatUtc(iso) {
  if (!iso) return "-";
  const m = TIMESTAMP_RE.exec(String(iso).trim());
  if (!m || m[7] === undefined) return String(iso);
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  const wall = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (wall.getUTCFullYear() !== year || wall.getUTCMonth() !== month - 1 || wall.getUTCDate() !== day
      || hour > 23 || minute > 59 || second > 59) return String(iso);
  let offsetMinutes = 0;
  if (m[7].toUpperCase() !== "Z") {
    const digits = m[7].slice(1).replace(":", "");
    offsetMinutes = (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2) || 0)) * (m[7][0] === "-" ? -1 : 1);
  }
  const utc = new Date(wall.getTime() - offsetMinutes * 60000);
  return pad(utc.getUTCFullYear(), 4) + "-" + pad(utc.getUTCMonth() + 1) + "-" + pad(utc.getUTCDate()) + " "
    + pad(utc.getUTCHours()) + ":" + pad(utc.getUTCMinutes()) + ":" + pad(utc.getUTCSeconds()) + " UTC";
}

// A market time exactly as the API sends it (naive, never converted), labelled "data time".
export function formatDataTime(text) {
  if (!text) return "-";
  return String(text) + " (data time)";
}

// A code version in the form the `runs` command prints: the first 10 characters, then "+dirty" when
// the version carries a "-dirty" marker ("0123456789abcdef-dirty:d06a" -> "0123456789+dirty").
export function shortVersion(version) {
  if (!version) return "-";
  const text = String(version);
  const short = text.slice(0, 10);
  return text.includes("-dirty") ? short + "+dirty" : short;
}
