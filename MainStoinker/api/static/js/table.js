// A data table builder shared by the views.
//
// Exports: netCell, compareValues, sortRows, dataTable.
//
// dataTable({columns, rows, caption, captionHidden, sort}) returns a scrollable wrapper element.
// A column is {label, numeric, cell, sortKey}:
//   label    header text;
//   numeric  right-aligned tabular figures (header and cells);
//   cell     (row, index) => Node | string | number | null | {content, className, title};
//   sortKey  optional (row) => number | string | null; makes the column sortable (click, Enter or Space
//            on the header button; the sorted header carries aria-sort); null sorts last.
// sort: {column, direction} is the initial order (column is an index, direction "asc" or "desc").
// caption: the table's accessible name; captionHidden hides it visually but keeps it for screen readers.
import { h } from "./dom.js";
import { formatNet, netClass } from "./format.js";

// A signed, coloured net value as a cell spec: text carries the sign, the class carries the colour.
export function netCell(value) {
  return { content: formatNet(value), className: netClass(value) };
}

// Ascending order of two sort keys: numbers numerically, anything else as text; null last.
export function compareValues(a, b) {
  const aMissing = a === null || a === undefined || Number.isNaN(a);
  const bMissing = b === null || b === undefined || Number.isNaN(b);
  if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "en", { numeric: true });
}

// A sorted copy (stable). Missing values stay last in both directions.
export function sortRows(rows, sortKey, direction) {
  const sign = direction === "desc" ? -1 : 1;
  const keyed = rows.map((row, index) => ({ row, index, key: sortKey(row) }));
  keyed.sort((x, y) => {
    const xm = x.key === null || x.key === undefined || Number.isNaN(x.key);
    const ym = y.key === null || y.key === undefined || Number.isNaN(y.key);
    if (xm || ym) return xm === ym ? x.index - y.index : xm ? 1 : -1;
    return sign * compareValues(x.key, y.key) || x.index - y.index;
  });
  return keyed.map((entry) => entry.row);
}

function isSpec(value) {
  return value !== null && typeof value === "object" && !(value instanceof Node) && "content" in value;
}

export function dataTable({ columns, rows, caption, captionHidden = false, sort = null }) {
  let sortIndex = sort ? sort.column : -1;
  let direction = sort && sort.direction === "desc" ? "desc" : "asc";
  const tbody = h("tbody", {});
  const headers = columns.map((column, index) => headerCell(column, index));

  function headerCell(column, index) {
    const cls = column.numeric ? "num" : "";
    if (!column.sortKey) return h("th", { scope: "col", class: cls }, column.label);
    const button = h("button", { type: "button", class: "sort", onclick: () => toggle(index) },
      column.label, h("span", { class: "sort-mark", "aria-hidden": "true" }));
    return h("th", { scope: "col", class: cls }, button);
  }

  function toggle(index) {
    direction = index === sortIndex && direction === "asc" ? "desc" : "asc";
    sortIndex = index;
    paint();
  }

  function bodyRow(row, rowIndex) {
    return h("tr", {}, columns.map((column) => {
      const made = column.cell(row, rowIndex);
      const spec = isSpec(made) ? made : { content: made };
      const cls = [column.numeric ? "num" : "", spec.className || ""].filter(Boolean).join(" ");
      return h("td", { class: cls, title: spec.title }, spec.content);
    }));
  }

  function paint() {
    const key = sortIndex >= 0 && columns[sortIndex] ? columns[sortIndex].sortKey : null;
    const ordered = key ? sortRows(rows, key, direction) : rows;
    tbody.replaceChildren(...ordered.map(bodyRow));
    columns.forEach((column, index) => {
      if (!column.sortKey) return;
      const sorted = index === sortIndex;
      if (sorted) headers[index].setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");
      else headers[index].removeAttribute("aria-sort");
      headers[index].querySelector(".sort-mark").textContent = sorted ? (direction === "asc" ? " ▲" : " ▼") : "";
    });
  }

  const table = h("table", {},
    caption ? h("caption", { class: captionHidden ? "visually-hidden" : "" }, caption) : null,
    h("thead", {}, h("tr", {}, headers)),
    tbody);
  paint();
  return h("div", { class: "table-wrap" }, table);
}
