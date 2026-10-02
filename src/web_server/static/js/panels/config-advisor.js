import { api, apiPost, apiDelete, esc } from "../api.js";
import { apiPut, showStatus, guardForm, mountAsync, viewerIsAdmin, guardedSubmit } from "../config-helpers.js";
import { mountPanelPoster } from "../panel-post.js";

// Cheapest win first — mirrors advisor_gaps.STATUS_ORDER and the Home tile.
const SUGG_STATUS = {
  ready_but_off: { label: "Just switch on", cls: "sugg-ready" },
  partial: { label: "Half set up", cls: "sugg-partial" },
  unconfigured: { label: "Not set up", cls: "sugg-unset" },
};

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    let cfg;
    try {
      cfg = await api("/api/config/advisor");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Assistant settings failed to load: ${esc(err.message)}</div></div>`;
      return;
    }

    // The name itself is per-guild branding, edited on the Branding panel.
    const name = esc(cfg.assistant_name || "Billy-bot");

    // GET /api/help/suggestions stays admin-only on purpose — a list of what
    // this server hasn't set up is reconnaissance. So the card is omitted for a
    // moderator rather than drawn and left to 403 into an error box.
    const admin = viewerIsAdmin();

    const optionsFor = (selected) =>
      (cfg.models || [])
        .map(
          (m) =>
            `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.label)}</option>`,
        )
        .join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>${name}</h2>
          <div class="subtitle">The AI helper behind <code>/ask</code>, the Ask panel in Discord, and the ask box in the Help panel. Rename it under <strong>Branding</strong>.</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Models</div>
            <div class="field">
              <label for="ad-model">Model — Members</label>
              <select name="model" id="ad-model">${optionsFor(cfg.model)}</select>
              <div class="field-hint">Haiku (the default) is the fastest and cheapest. Every answer is billed to whoever hosts the bot.</div>
            </div>
            <div class="field">
              <label for="ad-staff-model">Model — Mods &amp; Admins</label>
              <select name="staff_model" id="ad-staff-model">${optionsFor(cfg.staff_model)}</select>
              <div class="field-hint">Answers anyone with a moderator or admin permission. Defaults to Sonnet 5; pick Haiku to treat everyone the same.</div>
            </div>
            <div class="field">
              <label for="ad-daily-cap">Questions per member per day</label>
              <input type="number" name="daily_cap" id="ad-daily-cap" min="0" max="500" step="1"
                value="${esc(String(cfg.daily_cap ?? 0))}" />
              <div class="field-hint"><strong>0 means no limit</strong>, the default. Counts every way a member can ask, and resets at midnight UTC.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">What ${name} Can Read</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="server_context" ${cfg.server_context ? "checked" : ""} />
                Let ${name} read this server as well as the manual
              </label>
              <div class="field-hint"><strong>Off by default.</strong> Adds channel topics and announcements the asker can see, plus all server docs. Never member data.</div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="config_tools" ${cfg.config_tools ? "checked" : ""} />
                Let ${name} look up settings when an admin asks
              </label>
              <div class="field-hint"><strong>On by default.</strong> Lets admins look up live settings and confirm changes. Works only with the box above on.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="card" data-poster="ask-panel"></div>

        ${!admin ? "" : `
        <div class="card" data-sec="suggestions">
          <div class="section-label">Suggested Setup</div>
          <div class="field-hint" style="margin-bottom:8px;">The Home tile's list of unconfigured features, in full. Dismissing one hides it for everyone until restored.</div>
          <div data-suggestions><div class="empty">Loading…</div></div>
        </div>`}
      </div>
    `;

    // Where members meet the assistant in Discord. Admin-only, like every
    // other panel poster, so it renders locked for a non-admin who lands here.
    mountPanelPoster(container.querySelector('[data-poster="ask-panel"]'), "ask-panel", {
      // Raw, not the pre-escaped `name`: mountPanelPoster escapes heading
      // itself, and an assistant called "Poppy's Helper" would double-escape.
      heading: `Post the Ask ${cfg.assistant_name || "Billy-bot"} Panel`,
      buttonLabel: "Post Panel",
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    guardForm(form);

    guardedSubmit(form, async () => {
      try {
        await apiPut("/api/config/advisor", {
          model: form.querySelector('select[name="model"]').value,
          staff_model: form.querySelector('select[name="staff_model"]').value,
          server_context: form.querySelector('input[name="server_context"]').checked,
          config_tools: form.querySelector('input[name="config_tools"]').checked,
          daily_cap: Number(form.querySelector('input[name="daily_cap"]').value) || 0,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    // Fired off after the settings render rather than awaited alongside them:
    // this card is advisory, and a failure here must not take the page down.
    if (admin) refreshSuggestions(container);
  }, { errorMsg: "Couldn’t load the advisor settings." });
}

// ── suggested setup (manage view) ────────────────────────────────────
//
// The Home tile can dismiss but has no room to list what was dismissed, so the
// way back lives here — the page that tile already links to. Dismissal is
// guild-level: it records that the server passed on a feature, not that one
// admin is tired of seeing it, so what's cleared here is cleared for everyone.

async function refreshSuggestions(container) {
  const host = container.querySelector("[data-suggestions]");
  if (!host) return;
  let rows;
  try {
    rows = (await api("/api/help/suggestions", { limit: 40, include_dismissed: "true" }))
      .suggestions;
  } catch (err) {
    host.innerHTML = `<div class="error">Setup suggestions failed to load: ${esc(err.message)}</div>`;
    return;
  }
  if (!rows.length) {
    host.innerHTML = `<div class="empty">Everything I track is already set up. Nice.</div>`;
    return;
  }
  host.innerHTML = rows.map((s) => {
    const st = SUGG_STATUS[s.status] || SUGG_STATUS.unconfigured;
    const needs = (s.missing || []).map((m) => m.label);
    return `
      <div class="sugg-manage-row${s.dismissed ? " sugg-dismissed" : ""}">
        <div class="sugg-row">
          <div class="sugg-head">
            <span class="sugg-name">${esc(s.label)}</span>
            <span class="sugg-badge ${st.cls}">${esc(st.label)}</span>
            ${s.dismissed ? `<span class="sugg-badge sugg-unset">Dismissed</span>` : ""}
          </div>
          <div class="sugg-blurb">${esc(s.blurb)}</div>
          ${needs.length ? `<div class="sugg-needs">Still needs: ${esc(needs.join(", "))}</div>` : ""}
          <div class="sugg-panel">${esc(s.panel)}</div>
        </div>
        <button type="button" class="btn" data-sugg-toggle="${esc(s.slug)}"
          data-sugg-dismissed="${s.dismissed ? "1" : ""}">
          ${s.dismissed ? "Restore" : "Dismiss"}
        </button>
      </div>`;
  }).join("");

  host.querySelectorAll("[data-sugg-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const slug = btn.dataset.suggToggle;
      const path = `/api/help/suggestions/${encodeURIComponent(slug)}/dismiss`;
      btn.disabled = true;
      try {
        if (btn.dataset.suggDismissed) await apiDelete(path);
        else await apiPost(path);
      } catch (_) {
        btn.disabled = false;
        return;
      }
      refreshSuggestions(container);
    });
  });
}
