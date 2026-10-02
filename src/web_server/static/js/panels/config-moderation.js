import {
  loadConfig,
  loadChannels,
  loadCategories,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  mountChannelPicker,
  mountCategoryPicker,
  mountRolePicker,
  mountRoleMultiPicker,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";
import { esc } from "../api.js";
import { mountRoleDialStates } from "../role-dial-state.js";
import { mountPanelPoster } from "../panel-post.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels, categories, roles] = await Promise.all([loadConfig(), loadChannels(), loadCategories(), loadRoles()]);
    const m = config.moderation;
    const priv = config.privacy || {};
    let currentBehRetention = priv.behavioural_retention_enabled === "1" ? "1" : "0";
    let currentRemovalPurge = priv.guild_removal_purge_days || "";
    const removalPurgeEditable = priv.guild_removal_purge_editable !== false;
    const behDays = priv.behavioural_retention_days || "180";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Moderation</h2>
          <div class="subtitle">Jail, ticket, staff ping, warning, and logging settings</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Staff Roles</div>
            <div class="field">
              <label>Moderator Roles</label>
              <span data-picker="mod_role_ids"></span>
              <div class="field-hint">Holders get moderator powers: jailing, tickets, warnings and the dashboard's moderator pages.</div>
            </div>
            <div class="field">
              <label>Admin Roles</label>
              <span data-picker="admin_role_ids"></span>
              <div class="field-hint">Holders count as moderators, and are pinged on escalated tickets and warning alerts.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Jail</div>
            <div class="field">
              <label>Jailed Role</label>
              <span data-picker="jailed_role_id"></span>
              <div class="field-hint">The bot hides channels from this role, new ones too. "(none)" means one is made on the first jail.</div>
              <div data-role-state="jailed_role_id"></div>
            </div>
            <div class="field">
              <label>Jail Category</label>
              <span data-picker="jail_category_id"></span>
              <div class="field-hint">Where each jailed member's private channel is created. "(none)" puts it outside any category.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Tickets</div>
            <div class="field">
              <label>Ticket Category</label>
              <span data-picker="ticket_category_id"></span>
              <div class="field-hint">Where ticket channels are created. With "(none)", nobody can open a ticket or policy proposal.</div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="ticket_notify_on_create"${m.ticket_notify_on_create === "1" ? " checked" : ""} />
                Notify moderators when a ticket is opened
              </label>
              <div class="field-hint">DMs moderators about each new ticket, or pings Staff · Tickets once a moderator has opted in.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Staff Pings</div>
            <div class="field">
              <label>Staff Ping Channel</label>
              <span data-picker="staff_ping_channel_id"></span>
              <div class="field-hint">Where staff job pings post. "(disabled)" sends none, and the Mod Settings panel offers none.</div>
            </div>
            ${(m.staff_pings || []).map((p) => `
            <div class="field">
              <label>${esc(p.emoji)} ${esc(p.label)}</label>
              <span data-picker="staff-role-${esc(p.key)}"></span>
              <div class="field-hint">${esc(p.blurb)} Unset, made on first opt-in. "(none)" turns it off.</div>
              <div data-role-state="${esc(p.key)}"></div>
            </div>`).join("")}
            <div class="field">
              <label for="mod-qotd-deadline">QOTD Deadline (hour)</label>
              <input type="number" name="staff_ping_qotd_deadline_hour" id="mod-qotd-deadline" min="0" max="23" step="1" value="${esc(m.staff_ping_qotd_deadline_hour ?? "")}" style="max-width:120px;" />
              <div class="field-hint">Server hour, 0–23. Pings Staff · QOTD (or Spicy QOTD) if no question is up by then. Blank turns both off.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Warnings</div>
            <div class="field">
              <label for="mod-warning-threshold">Warning Threshold</label>
              <input type="number" name="warning_threshold" id="mod-warning-threshold" required min="1" max="99" step="1" value="${m.warning_threshold}" style="max-width:120px;" />
              <div class="field-hint">Alerts the Admin Roles in the log channel when a member reaches this many active warnings (1–99).</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Logging</div>
            <div class="field">
              <label>Log Channel</label>
              <span data-picker="log_channel_id"></span>
              <div class="field-hint">Where moderation actions and warning threshold alerts are posted. "(disabled)" posts none.</div>
            </div>
            <div class="field">
              <label>Transcript Channel</label>
              <span data-picker="transcript_channel_id"></span>
              <div class="field-hint">Where jail and ticket transcripts are posted. Falls back to the log channel when unset.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Data Retention</div>
            <div class="field">
              <label><input type="checkbox" name="behavioural_retention_enabled" id="mod-beh-retention"${currentBehRetention === "1" ? " checked" : ""} /> Delete role-ping records after ${esc(behDays)} days</label>
              <div class="field-hint">What the Ping Response report reads. The ${esc(behDays)}-day period is provisional, so leave this off until it is decided.</div>
            </div>
          </div>

          <div class="card" style="border-color: var(--red);">
            <div class="section-label" style="color: var(--red-text);">Danger Zone — When the bot is removed from a server</div>
            <div class="field">
              <label for="mod-removal-purge">Erase that server's data after</label>
              <select name="guild_removal_purge_days" id="mod-removal-purge"${removalPurgeEditable ? "" : " disabled"}>
                <option value="" ${currentRemovalPurge === "" ? "selected" : ""}>Never — keep everything (default)</option>
                <option value="0" ${currentRemovalPurge === "0" ? "selected" : ""}>Immediately</option>
                <option value="7" ${currentRemovalPurge === "7" ? "selected" : ""}>7 days</option>
                <option value="30" ${currentRemovalPurge === "30" ? "selected" : ""}>30 days</option>
                <option value="90" ${currentRemovalPurge === "90" ? "selected" : ""}>90 days</option>
              </select>
              <div class="field-hint"><strong>Applies to every server the bot is in.</strong> Erases a removed server's data permanently.${removalPurgeEditable ? "" : " Set this from the primary server."}</div>
            </div>
          </div>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <div class="card" data-poster="mod-settings"></div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const jailedRolePicker = mountRolePicker(
      form.querySelector('[data-picker="jailed_role_id"]'),
      roles, String(m.jailed_role_id || "0"), { label: "Jailed Role" },
    );
    const jailCategoryPicker = mountCategoryPicker(
      form.querySelector('[data-picker="jail_category_id"]'),
      categories, String(m.jail_category_id || "0"), { label: "Jail Category" },
    );
    const ticketCategoryPicker = mountCategoryPicker(
      form.querySelector('[data-picker="ticket_category_id"]'),
      categories, String(m.ticket_category_id || "0"), { label: "Ticket Category" },
    );
    const logChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="log_channel_id"]'),
      channels, String(m.log_channel_id || "0"), { label: "Log Channel" },
    );
    const transcriptChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="transcript_channel_id"]'),
      channels, String(m.transcript_channel_id || "0"), { label: "Transcript Channel" },
    );
    const staffChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="staff_ping_channel_id"]'),
      channels, String(m.staff_ping_channel_id || "0"), { label: "Staff Ping Channel" },
    );
    // Each job's role is sent only when it changed: a whole-form "0" on an
    // untouched picker would read as "(none)" and take the job off the Mod
    // Settings panel.
    const staffRolePickers = (m.staff_pings || []).map((p) => ({
      key: p.key,
      initial: String(p.role_id || "0"),
      picker: mountRolePicker(
        form.querySelector(`[data-picker="staff-role-${p.key}"]`),
        roles, String(p.role_id || "0"), { label: p.label },
      ),
    }));
    const modRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="mod_role_ids"]'),
      roles, m.mod_role_ids, { label: "Moderator Roles" },
    );
    const adminRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="admin_role_ids"]'),
      roles, m.admin_role_ids, { label: "Admin Roles" },
    );

    guardForm(form);
    mountRoleDialStates(container);

    // Where moderators pick their staff pings. Outside the form, like every
    // poster: its channel picker is not a setting the Save button sends.
    // Not awaited: it loads its own spec and channel list.
    mountPanelPoster(container.querySelector('[data-poster="mod-settings"]'), "mod-settings", {
      heading: "Mod Settings Panel",
      buttonLabel: "Post Panel",
    });

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const threshold = parseInt(fd.get("warning_threshold"), 10);
      if (!Number.isFinite(threshold) || threshold < 1 || threshold > 99) {
        showStatus(status, false, "Warning Threshold must be a number from 1 to 99");
        form.querySelector("[name=warning_threshold]").focus();
        return;
      }
      const deadline = String(fd.get("staff_ping_qotd_deadline_hour") ?? "").trim();
      const deadlineHour = Number(deadline);
      if (deadline !== "" && !(Number.isInteger(deadlineHour) && deadlineHour >= 0 && deadlineHour <= 23)) {
        showStatus(status, false, "QOTD Deadline must be an hour from 0 to 23, or blank");
        form.querySelector("[name=staff_ping_qotd_deadline_hour]").focus();
        return;
      }
      const staffRoles = {};
      for (const s of staffRolePickers) {
        const value = s.picker.getValue() || "0";
        if (value !== s.initial) staffRoles[s.key] = value;
      }
      try {
        await apiPut("/api/config/moderation", {
          // Ids stay strings; multi-pickers serialize to the same
          // comma-joined string the old multi-selects posted.
          jailed_role_id: jailedRolePicker.getValue() || "0",
          jail_category_id: jailCategoryPicker.getValue() || "0",
          ticket_category_id: ticketCategoryPicker.getValue() || "0",
          log_channel_id: logChannelPicker.getValue() || "0",
          transcript_channel_id: transcriptChannelPicker.getValue() || "0",
          mod_role_ids: modRolesPicker.getValues().join(","),
          admin_role_ids: adminRolesPicker.getValues().join(","),
          ticket_notify_on_create: fd.has("ticket_notify_on_create") ? "1" : "0",
          warning_threshold: threshold,
          staff_ping_channel_id: staffChannelPicker.getValue() || "0",
          staff_ping_qotd_deadline_hour: deadline,
          ...(Object.keys(staffRoles).length ? { staff_ping_roles: staffRoles } : {}),
        });
        for (const s of staffRolePickers) s.initial = s.picker.getValue() || "0";
        // Retention shares the privacy endpoint. Only call it on a real change
        // so a routine moderation save doesn't rewrite the key every time.
        const newBehRetention = form.querySelector("#mod-beh-retention").checked ? "1" : "0";
        if (newBehRetention !== currentBehRetention) {
          await apiPut("/api/config/privacy", { behavioural_retention_enabled: newBehRetention });
          currentBehRetention = newBehRetention;
        }
        // Same endpoint again, and again only on a real change: this one arms
        // the erasure of a whole server, so a routine moderation save must
        // never touch it.
        const newRemovalPurge = removalPurgeEditable
          ? form.querySelector("#mod-removal-purge").value
          : currentRemovalPurge;
        if (newRemovalPurge !== currentRemovalPurge) {
          await apiPut("/api/config/privacy", { guild_removal_purge_days: newRemovalPurge });
          currentRemovalPurge = newRemovalPurge;
        }
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the moderation settings." });
}
