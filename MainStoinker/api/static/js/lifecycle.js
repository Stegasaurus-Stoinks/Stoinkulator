// The view lifecycle, separated from the DOM so node can test it. main.js documents the contract.
//
// Exports: createScope, createNavigator.
import { isAbortError } from "./api.js";

function callQuietly(fn) {
  try {
    fn();
  } catch (err) {
    console.error("view cleanup failed", err);
  }
}

// A scope owns an AbortSignal and a list of disposal functions.
//   onCleanup(fn)  register fn; it runs at most once, when the scope ends, in reverse order of
//                  registration; a function that throws is logged and does not stop the others.
//                  Registering on a scope that already ended runs fn immediately.
//   dispose()      ends the scope (idempotent): aborts the signal WITHOUT a reason (so the fetch
//                  rejects with a plain AbortError that isAbortError recognises), then runs the cleanups.
//   remove(fn)     forgets a registered function without running it (used for child scopes).
export function createScope() {
  const controller = new AbortController();
  let cleanups = [];
  let ended = false;
  return {
    signal: controller.signal,
    get ended() {
      return ended;
    },
    onCleanup(fn) {
      if (typeof fn !== "function") return;
      if (ended) callQuietly(fn);
      else cleanups.push(fn);
    },
    remove(fn) {
      cleanups = cleanups.filter((candidate) => candidate !== fn);
    },
    dispose() {
      if (ended) return;
      ended = true;
      controller.abort();
      while (cleanups.length > 0) callQuietly(cleanups.pop());
    },
  };
}

// One navigator per page. run(view, params, hooks) starts a navigation:
//   1. the previous navigation is torn down: its signal is aborted, then its cleanups run (reverse
//      order), whether its view resolved, rejected or is still pending;
//   2. hooks.mount() creates the element this navigation renders into and returns it (the router
//      appends a fresh child of #app); hooks.unmount(element) is called on teardown, after every cleanup
//      the view registered, so a superseded view that writes late writes into a detached node;
//   3. view(element, params, ctx) is awaited; ctx = {signal, isCurrent, onCleanup, child};
//   4. a function the view returns is registered as one more onCleanup;
//   5. a rejection is passed to hooks.onError(err, element) unless this navigation is no longer
//      current or the error is an AbortError.
// Resolves to {current, failed}: whether this navigation is still the latest, and whether the view failed.
export function createNavigator() {
  let active = null;

  async function run(view, params, hooks = {}) {
    const previous = active;
    const nav = { scope: createScope() };
    active = nav;
    if (previous) previous.scope.dispose();
    const isCurrent = () => active === nav && !nav.scope.ended;
    const element = hooks.mount ? hooks.mount() : null;
    if (hooks.unmount) nav.scope.onCleanup(() => hooks.unmount(element)); // registered first: runs last
    const ctx = {
      signal: nav.scope.signal,
      isCurrent,
      onCleanup: (fn) => nav.scope.onCleanup(fn),
      // A request scope inside this navigation: aborted when the navigation ends or when abort() is
      // called (right before starting the request that replaces it).
      child() {
        const scope = createScope();
        const entry = () => scope.dispose();
        nav.scope.onCleanup(entry);
        return {
          signal: scope.signal,
          isCurrent: () => !scope.ended && isCurrent(),
          abort() {
            nav.scope.remove(entry);
            scope.dispose();
          },
          onCleanup: (fn) => scope.onCleanup(fn),
        };
      },
    };
    try {
      const returned = await view(element, params, ctx);
      if (typeof returned === "function") nav.scope.onCleanup(returned);
      return { current: active === nav, failed: false };
    } catch (err) {
      if (active !== nav || isAbortError(err)) return { current: active === nav, failed: false };
      if (hooks.onError) hooks.onError(err, element);
      return { current: true, failed: true };
    }
  }

  return { run };
}
