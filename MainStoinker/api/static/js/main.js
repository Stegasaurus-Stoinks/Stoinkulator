// Hash router, view lifecycle, page title and focus, and the header health strip.
//
// Exports (stable; later tasks may not edit this file or api.js): parseRoute and algoHash, re-exported
// from routes.js. Shared modules a view may import: api.js (getJson, ApiError, isAbortError,
// describeError, HEALTH_EVENT), dom.js (h, clear, viewHeading, loadingPanel, showLoading, errorPanel,
// showError, comingSoon), table.js (dataTable, netCell, sortRows), format.js, routes.js (hash builders).
//
// VIEW CONTRACT. A view module exports
//
//     export async function render(container, params, ctx) { ... }
//
//   container  the <main id="app"> element; the router has already replaced its content with a
//              "Loading..." panel. The view owns it until the next navigation.
//   params     the parsed route parameters (see routes.js parseRoute).
//   ctx        {signal, isCurrent}: signal is this navigation's AbortSignal, aborted as soon as
//              another navigation starts; isCurrent() is false once another navigation started.
//
// Rules:
//   1. Pass ctx.signal to every request: getJson(path, params, {signal: ctx.signal}).
//   2. After every await, check ctx.isCurrent() (or rely on an aborted fetch rejecting) BEFORE writing
//      to the container or to anything else shared. A superseded view must not draw.
//   3. Build a heading with viewHeading(text) (an <h2 tabindex="-1">). After the view settles the router
//      moves keyboard focus to the first <h2> of the container. The router sets document.title itself.
//   4. The view may return a cleanup function (synchronously, as the value of the async function);
//      the router calls it on the next navigation, after aborting the signal and before the next view
//      renders. Dispose charts, observers and window or document listeners there. If the view was
//      superseded before it resolved, the cleanup is called right away.
//   5. Errors: let a rejection escape and the router draws the shared error panel (never a stuck
//      "Loading..."). An AbortError (isAbortError) is ignored. A view that handles its own errors
//      for a part of its page should use errorPanel(err) so all errors look alike.
//   6. The header health strip is refreshed once per navigation by the router. A view that reloads
//      data on request may dispatch HEALTH_EVENT on window to refresh it again.
import { HEALTH_EVENT, ApiError, describeError, getJson, isAbortError } from "./api.js";
import { errorPanel, h, loadingPanel, viewHeading } from "./dom.js";
import { createNavigator } from "./lifecycle.js";
import { algoHash, parseRoute, routeTitle } from "./routes.js";
import { render as renderAlgo } from "./views/algo.js";
import { render as renderCompare } from "./views/compare.js";
import { render as renderOverview } from "./views/overview.js";
import { render as renderRuns } from "./views/runs.js";

export { algoHash, parseRoute };

function renderNotFound(container, params) {
  container.replaceChildren(
    viewHeading("Not found"),
    h("div", { class: "panel" },
      h("p", {}, "There is no page at"),
      h("p", { class: "mono" }, params.path),
      params.reason ? h("p", {}, params.reason) : null,
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

const viewNavigator = createNavigator();
let firstNavigation = true;

// Keyboard focus goes to the view heading once the view has settled, unless the user has already
// moved it somewhere else. Never scrolls (the page is scrolled to the top on navigation).
function focusHeading(container) {
  const active = document.activeElement;
  if (active && active !== document.body && active !== container) return;
  const heading = container.querySelector("h2") || container;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: true });
}

async function route() {
  const container = document.getElementById("app");
  const first = firstNavigation;
  firstNavigation = false;
  const parsed = parseRoute(window.location.hash);
  markNav(parsed.view);
  document.title = routeTitle(parsed);
  refreshHealth();
  const result = await viewNavigator.run(VIEWS[parsed.view], container, parsed.params, {
    prepare() {
      container.replaceChildren(loadingPanel(""));
      if (!first) {
        window.scrollTo(0, 0);
        container.focus({ preventScroll: true }); // the activated link is gone; do not fall back to <body>
      }
    },
    onError(err) {
      container.replaceChildren(viewHeading("Something went wrong"), errorPanel(err));
    },
  });
  if (result.current && !first) focusHeading(container);
}

// Latest wins: a newer health request aborts the older one, and only the newest may draw.
let healthController = null;

async function refreshHealth() {
  const strip = document.getElementById("health");
  if (healthController) healthController.abort();
  const controller = new AbortController();
  healthController = controller;
  try {
    const health = await getJson("/api/health", {}, { signal: controller.signal });
    if (healthController !== controller) return;
    const incomplete = Array.isArray(health.incomplete_runs) ? health.incomplete_runs : [];
    const runCount = Number(health.run_count);
    strip.replaceChildren(
      h("span", {}, "Database: reachable"),
      h("span", {}, "schema " + (health.schema_version ?? "-")),
      h("span", {}, Number.isFinite(runCount) ? runCount + (runCount === 1 ? " run" : " runs") : "runs: -"),
      h("span", { class: incomplete.length ? "warn" : "" },
        incomplete.length ? incomplete.length + " incomplete" : "no incomplete runs"),
    );
  } catch (err) {
    if (isAbortError(err) || healthController !== controller) return;
    let text;
    if (err instanceof ApiError && err.kind === "network") text = "Server: not reachable";
    else if (err instanceof ApiError && err.status === 503) text = "Database: unavailable";
    else text = "Health check failed (" + describeError(err) + ")";
    strip.replaceChildren(h("span", { class: "bad" }, text));
  }
}

window.addEventListener("hashchange", route);
window.addEventListener(HEALTH_EVENT, refreshHealth);
route();
