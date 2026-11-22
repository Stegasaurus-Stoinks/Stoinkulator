// Hash routes: parsing, building and page titles. Pure functions, no DOM, so node can test them.
//
// Exports: MIN_COMPARE, MAX_COMPARE, MAX_ID_DIGITS, parseRoute, runsHash, runHash, algoHash,
// compareHash, routeTitle.

export const MIN_COMPARE = 2;
export const MAX_COMPARE = 5;
export const MAX_ID_DIGITS = 18; // the API's rule for a run id in a path or list (routes_compare.py)

const ID_RE = /^[0-9]{1,18}$/;
const ID_REASON = "A run id is a positive whole number of at most 18 digits.";

function decode(segment) {
  try {
    return decodeURIComponent(segment);
  } catch (err) {
    return null;
  }
}

// A run id as a number, or null: ASCII digits only, at most 18 of them, positive, exactly representable.
function parseId(text) {
  if (!ID_RE.test(text)) return null;
  const value = Number(text);
  return value > 0 && Number.isSafeInteger(value) ? value : null;
}

// "#/run/3/algo/AAPL%2FX" -> {view: "algo", params: {runId: 3, idname: "AAPL/X"}}.
// "#/compare/3,4" -> {view: "compare", params: {runIds: [3, 4]}} (2 to 5 distinct ids).
// Anything else is {view: "notfound", params: {path, reason}}.
export function parseRoute(hash) {
  const path = String(hash || "").replace(/^#/, "");
  const notFound = (reason) => ({ view: "notfound", params: { path: path || "/", reason } });
  if (path === "" || path === "/") return { view: "runs", params: {} };
  if (!path.startsWith("/")) return notFound("The address must start with #/.");
  const seg = path.split("/").slice(1).map(decode);
  if (seg.includes(null)) return notFound("The address contains a broken percent escape.");
  if (seg[0] === "run" && seg.length === 2) {
    const runId = parseId(seg[1]);
    if (runId === null) return notFound(ID_REASON);
    return { view: "overview", params: { runId } };
  }
  if (seg[0] === "run" && seg.length === 4 && seg[2] === "algo") {
    const runId = parseId(seg[1]);
    if (runId === null) return notFound(ID_REASON);
    if (seg[3] === "") return notFound("The algo name is missing.");
    return { view: "algo", params: { runId, idname: seg[3] } };
  }
  if (seg[0] === "compare" && seg.length === 2) {
    const ids = seg[1].split(",").map(parseId);
    if (ids.includes(null)) return notFound(ID_REASON);
    if (new Set(ids).size !== ids.length) return notFound("Each run may appear only once.");
    if (ids.length < MIN_COMPARE || ids.length > MAX_COMPARE) {
      return notFound("Compare takes " + MIN_COMPARE + " to " + MAX_COMPARE + " different runs.");
    }
    return { view: "compare", params: { runIds: ids } };
  }
  return notFound("There is no such page.");
}

export function runsHash() {
  return "#/";
}

export function runHash(runId) {
  return "#/run/" + runId;
}

// Idnames are encoded because they may contain "/", "%", "#", "?" and spaces.
export function algoHash(runId, idname) {
  return "#/run/" + runId + "/algo/" + encodeURIComponent(idname);
}

export function compareHash(runIds) {
  return "#/compare/" + runIds.join(",");
}

// The document title for a parsed route.
export function routeTitle(route) {
  const suffix = " - Stoinkulator";
  const p = route.params;
  switch (route.view) {
    case "runs": return "Runs" + suffix;
    case "overview": return "Run " + p.runId + suffix;
    case "algo": return p.idname + ", run " + p.runId + suffix;
    case "compare": return "Compare runs " + p.runIds.join(", ") + suffix;
    default: return "Not found" + suffix;
  }
}
