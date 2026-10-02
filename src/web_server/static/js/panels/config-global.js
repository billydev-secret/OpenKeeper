import { api, esc } from "../api.js";
import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRoleMultiPicker,
  mountAsync,
  viewerIsAdmin,
  isPrimaryGuild,
  guardedSubmit,
} from "../config-helpers.js";

// A Discord user id is a snowflake: 17–20 digits, no other characters.
const SNOWFLAKE_RE = /^\d{17,20}$/;

// booster_swatch_dir is bot-global (stored at guild 0), so its card renders
// only on the primary guild — a secondary guild's admin was otherwise shown
// the host filesystem path and could overwrite the shared value. The PUT
// refuses it off-primary server-side regardless.

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    // Support Access is admin-only on both ends. A moderator reading this
    // page read-only must not be shown the toggle at all: its GET 403s, and
    // the old `.catch` fallback painted an unchecked box — "the developer has
    // no access" — which is a claim, not a blank. Skip the fetch and the card.
    const admin = viewerIsAdmin();
    const [config, channels, roles, supportResp] = await Promise.all([
      loadConfig(), loadChannels(), loadRoles(),
      admin
        ? api("/api/config/support-access").catch(() => ({ enabled: false }))
        : Promise.resolve(null),
    ]);
    const g = config.global;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Settings</h2>
          <div class="subtitle">Server-wide basics every other Dungeon Keeper feature builds on</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Time &amp; Reporting</div>
            <div class="field">
              <label for="cg-tz">Time Zone Offset (hours from UTC)</label>
              <input type="number" step="0.5" min="-12" max="14" required
                name="tz_offset_hours" id="cg-tz" value="${esc(g.tz_offset_hours)}"
                style="max-width:140px;" />
              <div class="field-hint">Sets when each day starts for quests, streaks and
                reports. Hours from UTC, such as -5 (US Eastern) or 5.5 (India).</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Staff Channel</div>
            <div class="field">
              <label>Moderator Channel</label>
              <span data-picker="mod_channel_id"></span>
              <div class="field-hint">Fallback for staff notices that have no channel of
                their own. "(disabled)" posts them nowhere.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Exemptions</div>
            <div class="field">
              <label>Bypass Roles</label>
              <span data-picker="bypass_role_ids"></span>
              <div class="field-hint"><strong>Trusted staff only.</strong> Holders skip the
                spoiler rule and GIF Guard. Empty exempts nobody.</div>
            </div>
            <div class="field">
              <label for="cg-bots">Recorded Bot User IDs</label>
              <input type="text" name="recorded_bot_user_ids" id="cg-bots"
                value="${esc((g.recorded_bot_user_ids || []).join(", "))}"
                placeholder="e.g. 123456789012345678, 234567890123456789" />
              <div class="field-hint">Members' @mentions of these bots are recorded; other
                bots' are not. Separate Discord user IDs with commas.</div>
            </div>
          </div>

          ${isPrimaryGuild() ? `
          <div class="card">
            <div class="section-label">Server File Paths</div>
            <div class="field">
              <label for="cg-swatch">Color Swatch Directory</label>
              <input type="text" name="booster_swatch_dir" id="cg-swatch"
                value="${esc(g.booster_swatch_dir || "")}"
                placeholder="e.g. /srv/dungeon-keeper/swatches" />
              <div class="field-hint">Fallback folder on the bot's host, used only while no
                swatches are uploaded under
                <a href="#/economy-sinks">Shop &amp; Perks → Color Palette</a>.</div>
            </div>
          </div>` : ""}

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        ${!admin ? "" : `
        <section class="form" data-support-card style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--rule)">
          <h3 style="margin:0 0 0.25rem">Support Access</h3>
          <div class="field-hint" style="margin-bottom:1rem">Lets the Dungeon Keeper
            developer into this dashboard with full admin access. Turn it off when
            they're done.</div>
          <div style="display:flex;align-items:center;gap:12px;">
            <label style="margin:0;cursor:pointer;display:flex;align-items:center;gap:8px;">
              <input type="checkbox" data-support-toggle ${supportResp.enabled ? "checked" : ""} />
              Allow the Dungeon Keeper developer to access this dashboard
            </label>
            <span data-support-status></span>
          </div>
        </section>`}

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--rule)">
          <h3 style="margin:0 0 0.25rem">Integrations</h3>
          <div class="field-hint" style="margin-bottom:1rem">Lets the bot read private
            Spotify playlists and update playlists. One authorization covers the whole
            bot.</div>
          <a href="/spotify/authorize" class="btn btn-secondary">Connect Spotify</a>
        </section>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const modChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="mod_channel_id"]'),
      channels, String(g.mod_channel_id || "0"), { label: "Moderator Channel" },
    );
    const bypassRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="bypass_role_ids"]'),
      roles, g.bypass_role_ids, { label: "Bypass Roles" },
    );

    guardForm(form);

    guardedSubmit(form, async () => {
      const fd = new FormData(form);

      const tz = parseFloat(fd.get("tz_offset_hours"));
      if (!Number.isFinite(tz) || tz < -12 || tz > 14) {
        showStatus(status, false, "Time Zone Offset must be a number from -12 to 14");
        form.querySelector("[name=tz_offset_hours]").focus();
        return;
      }

      const botIds = String(fd.get("recorded_bot_user_ids") || "")
        .split(",").map((s) => s.trim()).filter(Boolean);
      const badId = botIds.find((id) => !SNOWFLAKE_RE.test(id));
      if (badId) {
        showStatus(status, false,
          `Recorded Bot User IDs: "${badId}" is not a Discord user ID (17–20 digits)`);
        form.querySelector("[name=recorded_bot_user_ids]").focus();
        return;
      }

      try {
        await apiPut("/api/config/global", {
          tz_offset_hours: tz,
          // Snowflakes stay strings — parseInt rounds 19-digit ids.
          mod_channel_id: modChannelPicker.getValue() || "0",
          bypass_role_ids: bypassRolesPicker.getValues(),
          recorded_bot_user_ids: botIds,
          // Absent off-primary (the card isn't rendered): fd.get returns
          // null and the route treats null as "leave unchanged".
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const supportToggle = container.querySelector("[data-support-toggle]");
    const supportStatus = container.querySelector("[data-support-status]");
    if (supportToggle) supportToggle.addEventListener("change", async () => {
      try {
        await apiPut("/api/config/support-access", { enabled: supportToggle.checked });
        showStatus(supportStatus, true, supportToggle.checked ? "Access granted" : "Access revoked");
      } catch (err) {
        supportToggle.checked = !supportToggle.checked;
        showStatus(supportStatus, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the global settings." });
}
