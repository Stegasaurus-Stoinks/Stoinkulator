// The runs view: every stored run, newest first, with filters and a two-run compare.
import { HEALTH_EVENT, getJson, h, showError, showLoading } from "../api.js";
import {
  formatDuration, formatNet, formatUtc, netClass, shortVersion,
} from "../format.js";

// A run that never finished is shown as "incomplete", otherwise by its stored status.
export function displayStatus(run) {
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

function headerCell(text, numeric) {
  return h("th", { scope: "col", class: numeric ? "num" : "" }, text);
}

export async function render(container) {
  const ticked = new Set();
  let runs = [];
  let kind = "";
  let status = "";

  const body = h("div", {});
  const title = h("h2", {}, "Runs");

  const kindSelect = h("select", { id: "filter-kind", "aria-label": "Kind" });
  const statusSelect = h("select", { id: "filter-status", "aria-label": "Status" });
  const compare = h("a", { class: "btn", id: "compare-button", role: "button", "aria-disabled": "true" }, "Compare");
  const refresh = h("button", { type: "button", id: "refresh-button" }, "Refresh");
  const count = h("span", { class: "muted", role: "status" });

  const toolbar = h("div", { class: "toolbar" },
    h("label", {}, "Kind", kindSelect),
    h("label", {}, "Status", statusSelect),
    count,
    h("span", { class: "spacer" }),
    compare,
    refresh);

  function updateCompare() {
    if (ticked.size === 2) {
      const [a, b] = [...ticked].sort((x, y) => x - y);
      compare.setAttribute("href", "#/compare/" + a + "," + b);
      compare.setAttribute("aria-disabled", "false");
      compare.removeAttribute("tabindex");
    } else {
      compare.removeAttribute("href");
      compare.setAttribute("aria-disabled", "true");
      compare.setAttribute("tabindex", "-1");
    }
  }

  function row(run) {
    const id = run.run_id;
    const box = h("input", {
      type: "checkbox",
      "aria-label": "Select run " + id + " for comparison",
      checked: ticked.has(id),
      onchange: (event) => {
        if (event.target.checked) ticked.add(id);
        else ticked.delete(id);
        updateCompare();
      },
    });
    const shown = displayStatus(run);
    return h("tr", {},
      h("td", {}, box),
      h("td", { class: "num" }, h("a", { href: "#/run/" + id }, String(id))),
      h("td", {}, formatUtc(run.started_at)),
      h("td", { class: "num" }, formatDuration(run.duration_seconds)),
      h("td", {}, h("span", { class: "badge " + shown }, shown)),
      h("td", {}, run.kind),
      h("td", {}, run.fills),
      h("td", { class: "num" }, String(run.algo_count)),
      h("td", { class: "num" }, String(run.trade_count)),
      h("td", { class: "num " + netClass(run.net) }, formatNet(run.net)),
      h("td", { class: "mono", title: run.code_version || "" }, shortVersion(run.code_version)));
  }

  function table(rows) {
    return h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { scope: "col" }, "Compare"),
          headerCell("Id", true),
          headerCell("Start (UTC)", false),
          headerCell("Duration", true),
          headerCell("Status", false),
          headerCell("Kind", false),
          headerCell("Fill model", false),
          headerCell("Algos", true),
          headerCell("Trades", true),
          headerCell("Net", true),
          headerCell("Code version", false))),
        h("tbody", {}, rows.map(row))));
  }

  function draw() {
    const shown = filterRuns(runs, kind, status);
    count.textContent = shown.length === runs.length
      ? runs.length + (runs.length === 1 ? " run" : " runs")
      : shown.length + " of " + runs.length + " runs";
    if (runs.length === 0) {
      body.replaceChildren(h("p", { class: "panel empty" }, "No runs are stored in this database yet."));
    } else if (shown.length === 0) {
      body.replaceChildren(h("p", { class: "panel empty" }, "No run matches these filters."));
    } else {
      body.replaceChildren(table(shown));
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

  async function load() {
    showLoading(body, "runs");
    refresh.disabled = true;
    try {
      const data = await getJson("/api/runs");
      runs = [...data.runs].sort((a, b) => b.run_id - a.run_id);
      const known = new Set(runs.map((r) => r.run_id));
      for (const id of [...ticked]) if (!known.has(id)) ticked.delete(id);
      fillFilters();
      draw();
    } catch (err) {
      runs = [];
      showError(body, err);
      count.textContent = "";
      updateCompare();
    } finally {
      refresh.disabled = false;
    }
    window.dispatchEvent(new Event(HEALTH_EVENT));
  }

  kindSelect.addEventListener("change", () => { kind = kindSelect.value; draw(); });
  statusSelect.addEventListener("change", () => { status = statusSelect.value; draw(); });
  refresh.addEventListener("click", load);
  compare.addEventListener("click", (event) => {
    if (compare.getAttribute("aria-disabled") === "true") event.preventDefault();
  });

  container.replaceChildren(title, toolbar, body);
  updateCompare();
  await load();
}
