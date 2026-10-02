// Shared loading/empty/error fragments for panel innerHTML.
// These exist so new code stops hand-rolling `<div class="empty">…` — only
// the highest-traffic panels were converted retroactively (see plan #13).
import { esc } from "./api.js";

/** Pass the full message: renderLoading("Loading jails…"). */
export function renderLoading(msg = "Loading…") {
  return `<div class="empty">${esc(msg)}</div>`;
}

export function renderEmpty(msg) {
  return `<div class="empty">${esc(msg)}</div>`;
}

/** Accepts an Error or a string. Renders with the .error style. */
export function renderError(err) {
  const msg = err instanceof Error ? err.message : String(err);
  return `<div class="error">Error: ${esc(msg)}</div>`;
}

// ── a fetch failure the reader can actually act on ──────────────────────
//
// `renderError` alone is a dead end: a bare <div class="error"> with nothing
// to click. That is fine for a failed *save* (the form is still there, the
// button still works) and wrong for a failed *fetch*, where the thing that
// would have re-run it is usually the markup the error just replaced. The
// 2026-09-26 audit judged all 45 fetch-error sites across 28 panels and found
// fifteen where the only way back was a browser reload — five of them saying
// so in their own copy ("Reload the page to try again").
//
// `mountAsync` already solved this for a panel's first load. These two are the
// same answer for one region of an already-mounted panel, and mountAsync now
// renders through `renderRetryableError` too, so the error state has one
// definition rather than two that drift.

/**
 * The markup: the error, the underlying reason, and a Try again button.
 *
 * `data-readonly-ok` keeps that button clickable while the router is holding
 * the page read-only — the retry re-runs a GET the reader was already allowed
 * to make, and disabling it would remove the only way out of the error.
 */
export function renderRetryableError(msg, detail) {
  const hint = detail
    ? `<div class="field-hint" style="margin:6px 0 10px;">${esc(
        detail instanceof Error ? detail.message : String(detail),
      )}</div>`
    : "";
  return (
    `<div class="panel-missing" data-readonly-ok>${renderError(msg)}${hint}` +
    '<button type="button" class="btn" data-retry>Try again</button></div>'
  );
}

/**
 * Write that into one region and wire the button to `onRetry`.
 *
 * `onRetry` is the loader that just failed — a zero-argument function, called
 * again on click. It owns repainting `el` on success, which every loader here
 * already does; until it resolves the region shows "Trying again…", so a
 * second click cannot fire the fetch twice. A retry that fails again lands
 * back on this same state, so the reader can keep trying while an outage
 * clears rather than getting one attempt.
 */
export function showRetryableError(el, msg, onRetry) {
  el.innerHTML = renderRetryableError(msg);
  el.querySelector("[data-retry]").addEventListener("click", () => {
    el.innerHTML = renderLoading("Trying again…");
    Promise.resolve()
      .then(onRetry)
      .catch((err) => showRetryableError(el, err, onRetry));
  });
}
