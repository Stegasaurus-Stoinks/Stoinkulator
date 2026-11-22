// The view lifecycle, separated from the DOM so node can test it. main.js documents the contract.
//
// Exports: createNavigator.
import { isAbortError } from "./api.js";

function callQuietly(fn) {
  try {
    fn();
  } catch (err) {
    console.error("view cleanup failed", err);
  }
}

// One navigator per page. run(view, container, params, hooks) starts a navigation:
//   1. the previous navigation is aborted (its signal fires) and its cleanup function is called;
//   2. hooks.prepare() runs (the router clears the container there);
//   3. view(container, params, ctx) is awaited, ctx = {signal, isCurrent};
//   4. a returned function becomes the cleanup for the next navigation (called at once when this
//      navigation was already superseded while the view was still loading);
//   5. a rejection is passed to hooks.onError(err) unless this navigation is no longer current or
//      the error is an AbortError.
// Resolves to {current, failed}: whether this navigation is still the latest, and whether the view failed.
export function createNavigator() {
  let active = null;

  function teardown(nav) {
    nav.controller.abort();
    const cleanup = nav.cleanup;
    nav.cleanup = null;
    if (cleanup) callQuietly(cleanup);
  }

  async function run(view, container, params, hooks = {}) {
    const previous = active;
    const nav = { controller: new AbortController(), cleanup: null };
    active = nav;
    if (previous) teardown(previous);
    const ctx = { signal: nav.controller.signal, isCurrent: () => active === nav };
    if (hooks.prepare) hooks.prepare();
    try {
      const returned = await view(container, params, ctx);
      if (typeof returned === "function") {
        if (active === nav) nav.cleanup = returned;
        else callQuietly(returned);
      }
      return { current: active === nav, failed: false };
    } catch (err) {
      if (active !== nav || isAbortError(err)) return { current: active === nav, failed: false };
      if (hooks.onError) hooks.onError(err);
      return { current: true, failed: true };
    }
  }

  return { run };
}
