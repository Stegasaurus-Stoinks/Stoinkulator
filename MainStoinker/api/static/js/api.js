// Fetching and the shared loading and error states.
// Server text (idnames, error details) is untrusted: nodes are built with textContent only.

// Dispatched on window to make the header health strip reload.
export const HEALTH_EVENT = "stoink-health-refresh";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || "request failed");
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// GET a relative path and parse JSON. Empty parameters (undefined, null, "") are left out.
export async function getJson(path, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    query.append(key, String(value));
  }
  const text = query.toString();
  let response;
  try {
    response = await fetch(text ? path + "?" + text : path, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new ApiError(0, "the server could not be reached");
  }
  let body = null;
  try {
    body = await response.json();
  } catch (err) {
    body = null;
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return body;
}

// Create an element: h("a", {href: "#/"}, "text", childNode). Text goes in as text nodes.
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
}

export function showLoading(container, what) {
  container.replaceChildren(h("p", { class: "panel muted", role: "status" }, "Loading " + (what || "") + "..."));
}

export function describeError(err) {
  if (err instanceof ApiError) {
    return err.status ? "Error " + err.status + ": " + err.detail : err.detail;
  }
  return "Unexpected error: " + (err && err.message ? err.message : String(err));
}

export function showError(container, err) {
  container.replaceChildren(h("div", { class: "panel error", role: "alert" }, describeError(err)));
}

// A panel that says a view is not built yet, listing the parsed route parameters.
export function comingSoon(container, title, params) {
  const list = h("dl", { class: "params" });
  for (const [key, value] of Object.entries(params || {})) {
    list.append(h("dt", {}, key), h("dd", { class: "mono" }, String(value)));
  }
  container.replaceChildren(
    h("h2", {}, title),
    h("div", { class: "panel" }, h("p", {}, "Coming soon."), list),
  );
}
