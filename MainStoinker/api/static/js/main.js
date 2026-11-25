// Hash router, view lifecycle, page title and focus, and the header health strip.
//
// This file, api.js, lifecycle.js and dom.js are the shell: later view tasks do not edit them.
// Shared modules a view imports (never from this file, which exports nothing):
//   api.js        getJson, ApiError, isAbortError, describeError, HEALTH_EVENT
//   dom.js        h, clear, viewHeading, loadingPanel, showLoading, errorPanel, showError, comingSoon
//   table.js      dataTable, netCell, compareValues, sortRows
//   format.js     formatMoney, formatNet, netClass, formatPercent, formatDuration, formatUtc,
//                 formatDataTime, shortVersion, dataTimeToChartTime, chartTimeToDataTime
//   routes.js     parseRoute, runsHash, runHash, algoHash, compareHash, routeTitle (route helpers)
//
// VIEW CONTRACT. A view module exports
//
//     export async function render(container, params, ctx) { ... }
//
//   container  a fresh <div class="view"> the router created inside <main id="app"> for this
//              navigation, already showing "Loading...". The view owns it. When the navigation ends the
//              router removes it from the document, so a late write by a superseded view lands in a
//              detached node and is harmless. Replace its content with container.replaceChildren(...).
//   params     the parsed route parameters (routes.js parseRoute): {runId}, {runId, idname}, {runIds}.
//   ctx        {signal, isCurrent, onCleanup, child}:
//     ctx.signal        this navigation's AbortSignal, aborted as soon as another navigation starts.
//     ctx.isCurrent()   false once another navigation started.
//     ctx.onCleanup(fn) register a disposal function. It is called when this navigation ends, however
//                       the view ended: resolved, rejected, aborted or still pending. Reverse order of
//                       registration, each at most once; one that throws is logged and does not stop the
//                       others. If the navigation already ended, fn runs immediately.
//     ctx.child()       returns {signal, isCurrent(), abort(), onCleanup(fn)}: a request scope inside
//                       the view, aborted when the navigation ends or when abort() is called.
//
// Rules:
//   1. Pass a signal to every request: getJson(path, params, {signal: ctx.signal}), or the signal of a
//      child scope (rule 6).
//   2. Create a disposable thing (chart, ResizeObserver, timer, window or document listener) and
//      register its disposal AT ONCE, before the first await:
//          const chart = createChart(el);  ctx.onCleanup(() => chart.remove());
//      A cleanup returned from render is accepted too (it is treated as one more onCleanup), but it only
//      exists once the view has resolved, so it is too late for anything created before an await.
//   3. After every await check ctx.isCurrent() before writing to anything shared or calling a library
//      that is not abortable (timers, requestAnimationFrame, import(), chart calls). A rejected
//      abortable fetch needs no check: catch it with isAbortError and return.
//   4. Never write to `document` (title, body, head, other elements) outside the container, except
//      through the shell's exports: the router sets document.title and moves focus itself.
//   5. Build the heading with viewHeading(text) (an <h2 tabindex="-1">); after the view settles the
//      router moves keyboard focus to the first <h2> of the container.
//   6. A request inside the view that supersedes itself (a selected trade loads a new window; a Refresh
//      button) uses a child scope; call abort() right before starting the replacement. abort() takes no
//      argument: a reason would make the fetch reject with the reason, and isAbortError only recognises
//      a plain AbortError.
//          let request = null;
//          async function load(id) {
//            if (request) request.abort();
//            const mine = request = ctx.child();
//            try {
//              const data = await getJson("/api/...", {id}, {signal: mine.signal});
//              if (!mine.isCurrent()) return;
//              draw(data);
//            } catch (err) {
//              if (isAbortError(err) || !mine.isCurrent()) return;
//              show(errorPanel(err));
//            }
//          }
//   7. Several independent requests run through Promise.allSettled, not Promise.all: draw each part that
//      succeeded and put an errorPanel(err) where a part failed (skip rejections that isAbortError
//      recognises), so one failing request does not blank the whole view.
//   8. A rejection that escapes render shows the shared error panel (never a stuck "Loading..."); an
//      AbortError is ignored.
//   9. The router refreshes the health strip once per navigation. A view that reloads data on a user's
//      request dispatches HEALTH_EVENT on window afterwards.
import { HEALTH_EVENT, ApiError, describeError, getJson, isAbortError } from "./api.js";
import { errorPanel, h, loadingPanel, viewHeading } from "./dom.js";
import { createNavigator } from "./lifecycle.js";
import { liveHealthItems } from "./live.js";
import { parseRoute, routeTitle } from "./routes.js";
import { render as renderAlgo } from "./views/algo.js";
import { render as renderCompare } from "./views/compare.js";
import { render as renderOverview } from "./views/overview.js";
import { render as renderRuns } from "./views/runs.js";

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
function focusHeading(app, container) {
  const active = document.activeElement;
  if (active && active !== document.body && active !== app) return;
  const heading = container.querySelector("h2") || app;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: true });
}

async function route() {
  const app = document.getElementById("app");
  const first = firstNavigation;
  firstNavigation = false;
  const parsed = parseRoute(window.location.hash);
  markNav(parsed.view);
  document.title = routeTitle(parsed);
  refreshHealth();
  let mounted = null;
  const result = await viewNavigator.run(VIEWS[parsed.view], parsed.params, {
    mount() {
      mounted = h("div", { class: "view" }, loadingPanel(""));
      app.replaceChildren(mounted); // the previous view's element is removed by its own unmount
      if (!first) {
        window.scrollTo(0, 0);
        app.focus({ preventScroll: true }); // the activated link is gone; do not fall back to <body>
      }
      return mounted;
    },
    unmount(element) {
      element.remove();
    },
    onError(err, element) {
      element.replaceChildren(viewHeading("Something went wrong"), errorPanel(err));
    },
  });
  if (result.current && !first && mounted) focusHeading(app, mounted);
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
      ...liveHealthItems(health), // nothing unless the server sent live_state
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
