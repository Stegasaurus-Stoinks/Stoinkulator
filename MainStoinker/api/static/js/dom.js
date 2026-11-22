// DOM building helpers shared by every view. Server text (idnames, error details) is untrusted:
// nodes are built with text nodes and setAttribute only, never with HTML strings.
//
// Exports: h, clear, viewHeading, loadingPanel, showLoading, errorPanel, showError, comingSoon.
import { describeError } from "./api.js";

// Create an element: h("a", {href: "#/"}, "text", childNode). Text goes in as text nodes.
// Attributes: "class" sets className, "onclick" style keys add listeners, false/null/undefined are skipped.
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

// The heading of a view. The router moves keyboard focus to it after each navigation,
// so it is focusable by script (tabindex="-1") but not a tab stop.
export function viewHeading(text) {
  return h("h2", { tabindex: "-1" }, text);
}

export function loadingPanel(what) {
  return h("p", { class: "panel muted", role: "status" }, what ? "Loading " + what + "..." : "Loading...");
}

export function showLoading(container, what) {
  container.replaceChildren(loadingPanel(what));
}

export function errorPanel(err) {
  return h("div", { class: "panel error", role: "alert" }, describeError(err));
}

export function showError(container, err) {
  container.replaceChildren(errorPanel(err));
}

// A panel that says a view is not built yet, listing the parsed route parameters.
export function comingSoon(container, title, params) {
  const list = h("dl", { class: "params" });
  for (const [key, value] of Object.entries(params || {})) {
    list.append(h("dt", {}, key), h("dd", { class: "mono" }, String(value)));
  }
  container.replaceChildren(
    viewHeading(title),
    h("div", { class: "panel" }, h("p", {}, "Coming soon."), list),
  );
}
