// Hash router and the header health strip.
import { HEALTH_EVENT, getJson, h, describeError, showError } from "./api.js";
import { render as renderAlgo } from "./views/algo.js";
import { render as renderCompare } from "./views/compare.js";
import { render as renderOverview } from "./views/overview.js";
import { render as renderRuns } from "./views/runs.js";

function decode(segment) {
  try {
    return decodeURIComponent(segment);
  } catch (err) {
    return null;
  }
}

const ID_RE = /^\d+$/;

// "#/run/3/algo/AAPL%2FX" -> {view: "algo", params: {runId: 3, idname: "AAPL/X"}}.
// Anything that does not match a route is {view: "notfound", params: {path}}.
export function parseRoute(hash) {
  const path = String(hash || "").replace(/^#/, "");
  const notFound = { view: "notfound", params: { path: path || "/" } };
  const parts = path.split("/").slice(1); // the path starts with "/"
  if (path === "" || path === "/") return { view: "runs", params: {} };
  if (!path.startsWith("/")) return notFound;
  const seg = parts.map(decode);
  if (seg.includes(null)) return notFound;
  if (seg[0] === "run" && seg.length === 2 && ID_RE.test(seg[1])) {
    return { view: "overview", params: { runId: Number(seg[1]) } };
  }
  if (seg[0] === "run" && seg.length === 4 && seg[2] === "algo" && ID_RE.test(seg[1]) && seg[3] !== "") {
    return { view: "algo", params: { runId: Number(seg[1]), idname: seg[3] } };
  }
  if (seg[0] === "compare" && seg.length === 2) {
    const ids = seg[1].split(",");
    if (ids.length >= 2 && ids.every((id) => ID_RE.test(id))) {
      return { view: "compare", params: { runIds: ids.map(Number) } };
    }
  }
  return notFound;
}

// The hash for a route; idnames are encoded because they may contain "/" and other characters.
export function algoHash(runId, idname) {
  return "#/run/" + runId + "/algo/" + encodeURIComponent(idname);
}

function renderNotFound(container, params) {
  container.replaceChildren(
    h("h2", {}, "Not found"),
    h("div", { class: "panel" }, h("p", {}, "There is no page at "), h("p", { class: "mono" }, params.path),
      h("p", {}, h("a", { href: "#/" }, "Back to the runs"))),
  );
}

const VIEWS = {
  runs: renderRuns,
  overview: renderOverview,
  algo: renderAlgo,
  compare: renderCompare,
  notfound: renderNotFound,
};

function markNav(view) {
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === (view === "runs" ? "runs" : "")) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function route() {
  const container = document.getElementById("app");
  const { view, params } = parseRoute(window.location.hash);
  markNav(view);
  try {
    VIEWS[view](container, params);
  } catch (err) {
    showError(container, err);
  }
}

async function refreshHealth() {
  const strip = document.getElementById("health");
  try {
    const health = await getJson("/api/health");
    const incomplete = health.incomplete_runs || [];
    strip.replaceChildren(
      h("span", {}, "Database: reachable"),
      h("span", {}, "schema " + (health.schema_version ?? "-")),
      h("span", {}, health.run_count + (health.run_count === 1 ? " run" : " runs")),
      h("span", { class: incomplete.length ? "warn" : "" },
        incomplete.length ? incomplete.length + " incomplete" : "no incomplete runs"),
    );
  } catch (err) {
    strip.replaceChildren(h("span", { class: "bad" }, "Database: unreachable (" + describeError(err) + ")"));
  }
}

window.addEventListener("hashchange", route);
window.addEventListener(HEALTH_EVENT, refreshHealth);
refreshHealth();
route();
