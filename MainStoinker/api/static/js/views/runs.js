// The runs view: every stored run, newest first, with filters and a two-run compare.
// Follows the view contract documented at the top of main.js.
import { ApiError, HEALTH_EVENT, getJson, isAbortError } from "../api.js";
import { errorPanel, h, loadingPanel, viewHeading } from "../dom.js";
import {
  formatDuration, formatUtc, shortVersion,
} from "../format.js";
import {
  RUNS_REFRESH_MS, createPoller, createRefreshNote, isLiveRun, isRunning, livenessBadge,
} from "../live.js";
import { compareHash, runHash } from "../routes.js";
import { dataTable, netCell } from "../table.js";

// A run that never finished is shown as "incomplete", otherwise by its stored status. A live run (the server
// sent its liveness) is shown by its liveness: running, stale, completed or failed.
export function displayStatus(run) {
  if (isLiveRun(run)) return run.liveness;
  return run.incomplete ? "incomplete" : run.status;
}

export function filterRuns(runs, kind, status) {
  return runs.filter((r) => (!kind || r.kind === kind) && (!status || displayStatus(r) === status));
}

function optionList(label, values) {
  return [h("option", { value: "" }, label), ...values.map((v) => h("option", { value: v }, v))];
}

function unique(values) {
  return [...new Set(values)].sort();
}

const HINT_NEED_TWO = "Tick two runs to compare";
const HINT_CLEARED = "Ticks on hidden runs were cleared.";

export async function render(container, params, ctx) {
  const ticked = new Set();
  let runs = [];
  let kind = "";
  let status = "";
  let state = "loading"; // "loading" | "ok" | "error": what the body shows; filters never override it
  let error = null;
  let loading = false;
  let clearedTicks = false; // a filter hid ticked runs and their ticks were dropped
  let table = null; // the dataTable handle, kept so the user's sort survives filters and refreshes

  const body = h("div", {});
  const kindSelect = h("select", { id: "filter-kind", "aria-label": "Kind" });
  const statusSelect = h("select", { id: "filter-status", "aria-label": "Status" });
  const hint = h("span", { class: "muted", id: "compare-hint" }, HINT_NEED_TWO);
  const compare = h("button", {
    type: "button", class: "btn", id: "compare-button", "aria-disabled": "true", "aria-describedby": "compare-hint",
  }, "Compare");
  const refresh = h("button", { type: "button", id: "refresh-button" }, "Refresh");
  const count = h("span", { class: "muted", role: "status" });

  const toolbar = h("div", { class: "toolbar" },
    h("label", {}, "Kind", kindSelect),
    h("label", {}, "Status", statusSelect),
    count,
    h("span", { class: "spacer" }),
    hint,
    compare,
    refresh);

  // Compare is available only for exactly two ticked runs that are visible right now.
  function updateCompare() {
    const ids = [...ticked].sort((a, b) => a - b);
    const available = state === "ok" && ids.length === 2;
    compare.setAttribute("aria-disabled", available ? "false" : "true");
    let text = "";
    if (available) text = "";
    else if (state === "ok" && ids.length > 2) text = "Tick only two runs to compare (" + ids.length + " ticked)";
    else text = HINT_NEED_TWO;
    if (clearedTicks) text = (text ? text + ". " : "") + HINT_CLEARED;
    hint.textContent = text;
  }

  function row(run) {
    const id = run.run_id;
    const box = h("input", {
      type: "checkbox",
      "aria-label": "Select run " + id + " for comparison",
      checked: ticked.has(id),
      onchange: (event) => {
        clearedTicks = false;
        if (event.target.checked) ticked.add(id);
        else ticked.delete(id);
        updateCompare();
      },
    });
    return { run, box };
  }

  const columns = [
    { label: "Compare", cell: (r) => r.box },
    { label: "Id", numeric: true, cell: (r) => h("a", { href: runHash(r.run.run_id) }, String(r.run.run_id)) },
    { label: "Start (UTC)", cell: (r) => formatUtc(r.run.started_at) },
    { label: "Duration", numeric: true, cell: (r) => formatDuration(r.run.duration_seconds) },
    {
      label: "Status",
      cell: (r) => (isLiveRun(r.run)
        ? livenessBadge(r.run)
        : h("span", { class: "badge " + displayStatus(r.run) }, displayStatus(r.run))),
    },
    { label: "Kind", cell: (r) => r.run.kind },
    { label: "Fill model", cell: (r) => r.run.fills },
    { label: "Algos", numeric: true, cell: (r) => String(r.run.algo_count) },
    { label: "Trades", numeric: true, cell: (r) => String(r.run.trade_count) },
    { label: "Net", numeric: true, cell: (r) => netCell(r.run.net) },
    {
      label: "Code version",
      cell: (r) => ({ content: shortVersion(r.run.code_version), className: "mono", title: r.run.code_version || "" }),
    },
  ];

  function draw() {
    if (state === "loading") {
      count.textContent = "";
      body.replaceChildren(loadingPanel("runs"));
    } else if (state === "error") {
      count.textContent = "";
      body.replaceChildren(errorPanel(error));
    } else {
      const shown = filterRuns(runs, kind, status);
      const visible = new Set(shown.map((r) => r.run_id));
      for (const id of [...ticked]) {
        if (visible.has(id)) continue;
        ticked.delete(id); // a hidden row is not selected
        clearedTicks = true;
      }
      count.textContent = shown.length === runs.length
        ? runs.length + (runs.length === 1 ? " run" : " runs")
        : shown.length + " of " + runs.length + " runs";
      if (runs.length === 0) {
        body.replaceChildren(h("p", { class: "panel empty" }, "No runs are stored in this database yet."));
      } else if (shown.length === 0) {
        body.replaceChildren(h("p", { class: "panel empty" }, "No run matches these filters."));
      } else {
        const rows = shown.map(row);
        if (table) table.update({ rows });
        else table = dataTable({ columns, rows, caption: "Stored runs", captionHidden: true, rowKey: (r) => r.run.run_id });
        body.replaceChildren(table.element);
      }
    }
    updateCompare();
  }

  function fillFilters() {
    kindSelect.replaceChildren(...optionList("All kinds", unique(runs.map((r) => r.kind))));
    statusSelect.replaceChildren(...optionList("All statuses", unique(runs.map(displayStatus))));
    kindSelect.value = kind;
    statusSelect.value = status;
    if (kindSelect.value !== kind) kind = kindSelect.value;
    if (statusSelect.value !== status) status = statusSelect.value;
  }

  // The refresh button keeps keyboard focus while loading: it is aria-disabled, not disabled,
  // and clicks are ignored until the load ends.
  // A background load (the timer of a list with a running run) keeps the last good render while it runs and
  // when it fails: no "Loading..." panel, no error panel, only the one "refresh failed" note.
  async function load(userRefresh, background = false) {
    if (loading) return;
    loading = true;
    let keepRender = false;
    if (!background) {
      state = "loading";
      refresh.setAttribute("aria-disabled", "true");
      draw();
    }
    try {
      const data = await getJson("/api/runs", {}, { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      if (!Array.isArray(data.runs)) throw new ApiError(200, "The server sent an unexpected response.", "unexpected");
      // Newest first. When the server sent live runs it has ordered the list (running, stale, the rest, each
      // newest first) and that order is kept.
      runs = data.runs.some(isLiveRun) ? [...data.runs] : [...data.runs].sort((a, b) => b.run_id - a.run_id);
      const known = new Set(runs.map((r) => r.run_id));
      for (const id of [...ticked]) if (!known.has(id)) ticked.delete(id);
      fillFilters();
      state = "ok";
      note.ok();
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      if (background) {
        keepRender = true;
        note.fail();
      } else {
        error = err;
        state = "error";
      }
    } finally {
      loading = false;
      if (ctx.isCurrent()) {
        if (!background) refresh.setAttribute("aria-disabled", "false");
        if (!keepRender) draw();
        layout();
        syncPoller();
        if (userRefresh || (background && !keepRender)) {
          window.dispatchEvent(new Event(HEALTH_EVENT)); // the router did the first health request
        }
      }
    }
  }

  kindSelect.addEventListener("change", () => { kind = kindSelect.value; draw(); });
  statusSelect.addEventListener("change", () => { status = statusSelect.value; draw(); });
  refresh.addEventListener("click", () => {
    if (refresh.getAttribute("aria-disabled") === "true") return;
    load(true);
  });
  compare.addEventListener("click", () => {
    if (compare.getAttribute("aria-disabled") === "true") return;
    window.location.hash = compareHash([...ticked].sort((a, b) => a - b));
  });

  // The page: the "refresh failed" note (only after a failed background refresh) sits between toolbar and body.
  const heading = viewHeading("Runs");
  let placedNote = false; // re-placing nodes would drop keyboard focus, so only a change of the note does it
  function layout(force = false) {
    if (!force && note.failed === placedNote) return;
    placedNote = note.failed;
    container.replaceChildren(...[heading, toolbar, note.failed ? note.element : null, body].filter(Boolean));
  }

  // The list refreshes itself while at least one run is running. The timer is registered with the view's
  // cleanup at once; it stops when no run is running any more (checked after each load).
  const note = createRefreshNote();
  const poller = createPoller(ctx, { intervalMs: RUNS_REFRESH_MS, tick: () => load(false, true) });
  function syncPoller() {
    if (state === "ok" && runs.some(isRunning)) poller.start();
    else poller.stop();
  }

  layout(true);
  updateCompare();
  await load(false);
}
