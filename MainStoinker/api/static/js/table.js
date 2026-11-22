// A data table builder shared by the views.
//
// Exports: netCell, compareValues, sortRows, nextSort, dataTable.
//
// const table = dataTable({columns, rows, caption, captionHidden, sort, rowKey, selectedKey, onSelect,
//                          selectLabel})
// returns a handle {element, update({rows, selectedKey}), getSort()}:
//   table.element   the scrollable wrapper to put in the page (the same element for the table's life);
//   table.update()  re-renders with new rows and/or a new selectedKey (a key that is absent from the
//                   argument is left as it is). The user's sort (the column they clicked) is kept across
//                   updates, rows are re-sorted stably, and a focused row keeps focus. Changing only
//                   selectedKey does not rebuild the rows. Call update, do not build a second table;
//   table.getSort() the current {column, direction} (column is -1 when unsorted).
// A column is {label, numeric, cell, sortKey}:
//   label    header text;
//   numeric  right-aligned tabular figures (header and cells);
//   cell     (row, index) => Node | string | number | null | {content, className, title};
//   sortKey  optional (row) => number | string | null; makes the column sortable (click, Enter or Space
//            on the header button; the sorted header carries aria-sort); null sorts last.
// sort: {column, direction} is the initial order (column is an index, direction "asc" or "desc").
// Selection (all optional): rowKey(row) gives a row's identity (string or number; keys are compared as
// text), selectedKey is the key of the selected row, onSelect(row) is called when the row's button is
// pressed, or the row is clicked with the mouse (not on a link, button or input inside it). The table
// stays a plain table (no grid role). With onSelect, the first cell of every row holds a real
// <button class="row-select"> around that cell's content: it is the keyboard and screen-reader way to
// select, and it carries aria-pressed ("true" on the selected row, "false" on the others).
// selectLabel(row) gives the button its accessible name (aria-label), e.g. "Select trade 3"; without it
// the cell content names the button. The selected row (selectedKey matches its rowKey) carries
// aria-current="true"; other rows carry no aria-current. Rows are not focusable and have no key
// handler; no selected-state attribute of the grid pattern is used (it is not valid on a plain table
// row). With onSelect every row has class "selectable".
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

// The sort after the user activates the header of column `index`: a new column starts ascending,
// the sorted column toggles. Pure: returns a new {column, direction}.
export function nextSort(sort, index) {
  const direction = sort.column === index && sort.direction === "asc" ? "desc" : "asc";
  return { column: index, direction };
}

function isSpec(value) {
  return value !== null && typeof value === "object" && !(value instanceof Node) && "content" in value;
}

const INTERACTIVE = "a, button, input, select, textarea, label, summary";

function sameKey(a, b) {
  return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
}

export function dataTable({
  columns, rows, caption, captionHidden = false, sort = null, rowKey = null, selectedKey = null, onSelect = null,
  selectLabel = null,
}) {
  let current = rows;
  let selected = selectedKey;
  let sortState = { column: sort ? sort.column : -1, direction: sort && sort.direction === "desc" ? "desc" : "asc" };
  const rowElements = new Map(); // String(key) -> <tr>, for the rows on show
  const rowButtons = new Map(); // String(key) -> the row's <button> (only with onSelect)
  const tbody = h("tbody", {});
  const headers = columns.map((column, index) => headerCell(column, index));

  function headerCell(column, index) {
    const cls = column.numeric ? "num" : "";
    if (!column.sortKey) return h("th", { scope: "col", class: cls }, column.label);
    const button = h("button", {
      type: "button",
      class: "sort",
      onclick: () => {
        sortState = nextSort(sortState, index);
        paint();
      },
    }, column.label, h("span", { class: "sort-mark", "aria-hidden": "true" }));
    return h("th", { scope: "col", class: cls }, button);
  }

  function markSelected(tr, button, key) {
    const on = sameKey(key, selected);
    if (on) tr.setAttribute("aria-current", "true");
    else tr.removeAttribute("aria-current");
    if (button) button.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function bodyRow(row, rowIndex) {
    const key = rowKey ? rowKey(row) : null;
    const attrs = {};
    let button = null;
    if (onSelect) {
      attrs.class = "selectable";
      attrs.onclick = (event) => {
        const inner = event.target && event.target.closest ? event.target.closest(INTERACTIVE) : null;
        if (inner && tr.contains(inner)) return; // a link or a checkbox in the row does its own thing
        onSelect(row);
      };
    }
    const tr = h("tr", attrs, columns.map((column, index) => {
      const made = column.cell(row, rowIndex);
      const spec = isSpec(made) ? made : { content: made };
      const cls = [column.numeric ? "num" : "", spec.className || ""].filter(Boolean).join(" ");
      let content = spec.content;
      if (onSelect && index === 0) {
        button = h("button", {
          type: "button",
          class: "row-select",
          "aria-pressed": "false",
          "aria-label": selectLabel ? selectLabel(row) : null,
          onclick: () => onSelect(row),
        }, content);
        content = button;
      }
      return h("td", { class: cls, title: spec.title }, content);
    }));
    if (rowKey) {
      rowElements.set(String(key), tr);
      if (button) rowButtons.set(String(key), button);
      markSelected(tr, button, key);
    }
    return tr;
  }

  function paint() {
    let focusedKey = null;
    for (const [key, button] of rowButtons) if (button === document.activeElement) focusedKey = key;
    rowElements.clear();
    rowButtons.clear();
    const sortKey = sortState.column >= 0 && columns[sortState.column] ? columns[sortState.column].sortKey : null;
    const ordered = sortKey ? sortRows(current, sortKey, sortState.direction) : current;
    tbody.replaceChildren(...ordered.map(bodyRow));
    if (focusedKey !== null && rowButtons.has(focusedKey)) rowButtons.get(focusedKey).focus({ preventScroll: true });
    columns.forEach((column, index) => {
      if (!column.sortKey) return;
      const sorted = index === sortState.column;
      if (sorted) headers[index].setAttribute("aria-sort", sortState.direction === "asc" ? "ascending" : "descending");
      else headers[index].removeAttribute("aria-sort");
      headers[index].querySelector(".sort-mark").textContent = sorted ? (sortState.direction === "asc" ? " ▲" : " ▼") : "";
    });
  }

  const table = h("table", {},
    caption ? h("caption", { class: captionHidden ? "visually-hidden" : "" }, caption) : null,
    h("thead", {}, h("tr", {}, headers)),
    tbody);
  const element = h("div", { class: "table-wrap" }, table);
  paint();

  return {
    element,
    update(change = {}) {
      if ("selectedKey" in change) selected = change.selectedKey;
      if (change.rows) {
        current = change.rows;
        paint();
      } else if ("selectedKey" in change) {
        for (const [key, tr] of rowElements) markSelected(tr, rowButtons.get(key) || null, key);
      }
    },
    getSort: () => ({ ...sortState }),
  };
}
