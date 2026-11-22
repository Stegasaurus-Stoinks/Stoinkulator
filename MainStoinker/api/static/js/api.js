// Fetching and error descriptions. No DOM here, so node can import it.
//
// Exports: HEALTH_EVENT, ApiError, isAbortError, getJson, describeError.
// DOM panels (loading, error, coming soon) and h() live in dom.js.

// Dispatched on window to make the header health strip reload.
export const HEALTH_EVENT = "stoink-health-refresh";

const UNEXPECTED = "The server sent an unexpected response.";
const NETWORK = "The server could not be reached.";

// kind: "http" (the server answered with an error status), "network" (no answer at all, status 0)
// or "unexpected" (a success status with a body that is not usable JSON).
export class ApiError extends Error {
  constructor(status, detail, kind = "http") {
    super(detail || "request failed");
    this.name = "ApiError";
    this.status = status;
    this.detail = detail || "request failed";
    this.kind = kind;
  }
}

// True for the error fetch raises when its AbortSignal fires. Views ignore it: the user has moved on.
export function isAbortError(err) {
  return Boolean(err) && err.name === "AbortError";
}

// GET a relative path and parse JSON. Empty parameters (undefined, null, "") are left out.
// options.signal is passed to fetch; an abort rejects with the fetch AbortError (see isAbortError).
// Failures reject with ApiError; a 2xx answer whose body is not JSON, or is JSON null, is an error too.
export async function getJson(path, params, options) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    query.append(key, String(value));
  }
  const text = query.toString();
  const signal = options && options.signal;
  let response;
  try {
    response = await fetch(text ? path + "?" + text : path, { headers: { Accept: "application/json" }, signal });
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new ApiError(0, NETWORK, "network");
  }
  let body = null;
  let parsed = true;
  try {
    body = await response.json();
  } catch (err) {
    if (isAbortError(err)) throw err;
    parsed = false;
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" && body.detail ? body.detail : response.statusText;
    throw new ApiError(response.status, detail || "request failed", "http");
  }
  if (!parsed || body === null || body === undefined) throw new ApiError(response.status, UNEXPECTED, "unexpected");
  return body;
}

export function describeError(err) {
  if (err instanceof ApiError) {
    return err.kind === "http" ? "Error " + err.status + ": " + err.detail : err.detail;
  }
  return "Unexpected error: " + (err && err.message ? err.message : String(err));
}
