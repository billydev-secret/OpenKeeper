// Tiny fetch wrapper. All endpoints are same-origin JSON.

/** Escape a string for safe insertion into innerHTML, including attributes.
 *  null/undefined render as "" (matches every panel-local variant this
 *  replaced; nobody wants a literal "null" in the page). */
export function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/**
 * Canonical timestamp formatter: "Jun 10, 3:42 PM", with the year added
 * when it isn't the current year. Accepts unix seconds, ISO strings, or Date.
 */
export function fmtTs(ts) {
  if (!ts) return "—";
  const d = ts instanceof Date ? ts : new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (isNaN(d.getTime())) return "—";
  const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleString(undefined, opts);
}

/** Compact age/duration from seconds: "5d 4h", "3h 12m", "45m", "30s". */
export function fmtAge(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/**
 * The one fetch core. Everything HTTP in the dashboard goes through here
 * (help.js's HTML fetch is the sole exception).
 *
 * - `params`: query-string object; null/undefined/"" entries are skipped.
 * - `body`: JSON-encoded with a Content-Type header. A FormData body is
 *   passed through untouched (the browser sets the multipart boundary).
 *   `body === undefined` sends no body and no Content-Type.
 * - `on401`: override the default `/login` redirect (wellness pages
 *   redirect to Discord OAuth instead).
 *
 * Errors throw `Error("<status>: <detail>")`, preferring the body's
 * `error` field (wellness endpoints), then `detail` (FastAPI — arrays of
 * validation errors are joined), then statusText.
 */
// A 422 from FastAPI names the offending field in `loc` and nowhere else:
// `msg` is "Input should be less than or equal to 23", which on a one-number
// form is fine and on a config page with six dials tells the admin a value is
// wrong without saying which box. `loc` is ["body", "max_timer"], so the last
// non-carrier segment is the field — and it matches the input's `name`, so it
// is greppable rather than a prettified guess at the visible label.
const LOC_CARRIERS = ["body", "query", "path", "header", "cookie"];

function validationMsg(e) {
  if (!e || typeof e !== "object") return String(e);
  const msg = e.msg || JSON.stringify(e);
  const loc = Array.isArray(e.loc) ? e.loc : [];
  const field = loc.filter(
    (p) => typeof p === "string" && !LOC_CARRIERS.includes(p),
  ).pop();
  return field ? `${field}: ${msg}` : msg;
}

export async function request(method, path, { params, body, on401 } = {}) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      url.searchParams.set(k, v);
    }
  }
  const opts = { method, credentials: "same-origin" };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  // The one sanctioned fetch in the dashboard. Everything the ban exists to
  // centralise — same-origin credentials, the 401 redirect, the error envelope
  // — is in the lines around this one, which is why no panel needs its own.
  // eslint-disable-next-line no-restricted-syntax
  const res = await fetch(url, opts);
  if (res.status === 401) {
    if (on401) on401(); else window.location = "/login";
    return new Promise(() => {}); // hang — page is navigating away
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const b = await res.json();
      if (b.error) detail = String(b.error);
      else if (b.detail) detail = Array.isArray(b.detail)
        ? b.detail.map(validationMsg).join("; ")
        : String(b.detail);
    } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export function api(path, params) { return request("GET", path, { params }); }
export function apiPost(path, body) { return request("POST", path, { body: body || {} }); }
export function apiPut(path, body) { return request("PUT", path, { body }); }
export function apiDelete(path) { return request("DELETE", path); }
