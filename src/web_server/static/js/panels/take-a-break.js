import {
  loadChannels, loadRoles, memberNames, mountChannelPicker, mountRolePicker,
  showStatus, guardForm, guardedSubmit, mountAsync, lockUnlessAdmin, roleName,
} from "../config-helpers.js";
import { api, apiPost, apiPut, esc, fmtTs } from "../api.js";
import { toast, confirmDialog } from "../ui.js";
import { mountPanelPoster } from "../panel-post.js";
import { renderSortableTable } from "../table.js";

// Take a Break. Settings run top to bottom in the order an admin sets them up:
// the break channel (which is also the on switch), then the optional Spicy
// Lite swap, then the panel members press. The "On a Break" list at the bottom
// is the one part a moderator acts on, so it stays live on a read-only page.
export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;

  return mountAsync(container, async () => {
    const [data, channels, roles] = await Promise.all([
      api("/api/breaks"), loadChannels(), loadRoles(),
    ]);
    const cfg = data.config;
    const spicyName = cfg.spicy_role_id !== "0" ? roleName(roles, cfg.spicy_role_id) : "";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Take a Break</h2>
          <div class="subtitle">Let members step away: roles, role pings and bot DMs off until they come back</div>
        </header>

        <div data-settings>
          <div class="section-label">Break Channel</div>
          <div class="card">
            <div class="field-hint" style="margin-bottom:10px;">
              The only channel a member on a break can see. ${cfg.enabled
                ? "Breaks are <strong>on</strong>."
                : "Breaks are <strong>off</strong> until this is set up."}
            </div>
            <div class="field">
              <label>Channel</label>
              <div data-break-channel></div>
            </div>
            <button type="button" class="btn btn-primary" data-setup>Set Up Break Channel</button>
            <span data-setup-status class="field-hint"></span>
          </div>

          <form class="form" data-form>
            <div class="section-label">Spicy Lite</div>
            <div class="card">
              <div class="field">
                <label>Spicy Lite Role</label>
                <div data-lite-role></div>
                <div class="field-hint">Given in place of
                  ${spicyName ? `<strong>${esc(spicyName)}</strong>` : "the spicy role"}
                  during a break. <em>(none)</em> means it comes off with every other role.</div>
              </div>
              <div style="display:flex; gap:8px; align-items:center;">
                <button type="submit" class="btn btn-primary">Save</button>
                <span data-status></span>
              </div>
            </div>
          </form>

          <div data-poster="break-panel"></div>
        </div>

        <div class="section-label">On a Break</div>
        <div class="card" data-readonly-ok>
          <div class="field-hint" style="margin-bottom:10px;">
            <strong>End Break</strong> restores a member's saved roles, as if they pressed
            <em>👋 Back</em>. For someone who left, it clears them.
          </div>
          <div data-list style="overflow-x:auto;"></div>
        </div>
      </div>
    `;

    const channelPicker = mountChannelPicker(
      container.querySelector("[data-break-channel]"), channels, cfg.break_channel_id,
    );
    const litePicker = mountRolePicker(
      container.querySelector("[data-lite-role]"), roles, cfg.spicy_lite_role_id,
      { label: "Spicy Lite Role" },
    );

    container.querySelector("[data-setup]").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const el = container.querySelector("[data-setup-status]");
      const channelId = channelPicker.getValue();
      if (!channelId || channelId === "0") return toast("Pick a channel first.", "error");
      btn.disabled = true;
      el.textContent = "Setting up…";
      try {
        const res = await apiPost("/api/breaks/setup", { channel_id: channelId });
        el.textContent = res.note || "Break channel set up and On a Break panel posted.";
      } catch (err) {
        el.textContent = "";
        toast(err.message, "error");
      } finally {
        btn.disabled = false;
      }
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    guardForm(form);
    guardedSubmit(form, async () => {
      try {
        await apiPut("/api/breaks/config", { spicy_lite_role_id: litePicker.getValue() });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    mountPanelPoster(container.querySelector('[data-poster="break-panel"]'), "break-panel", {
      heading: "Post the Take a Break Panel",
      buttonLabel: "Post Panel",
    });

    lockUnlessAdmin(container.querySelector("[data-settings]"));

    const listEl = container.querySelector("[data-list]");
    async function renderList(rows) {
      const name = await memberNames(rows.map((r) => r.user_id));
      renderSortableTable(listEl, {
        columns: [
          { key: "member", label: "Member" },
          { key: "started_at", label: "Since", format: (v) => fmtTs(v) },
          {
            key: "roles", label: "Saved Roles",
            format: (v, r) => `${v}${r.swapped_spicy ? " · on Spicy Lite" : ""}`,
          },
          {
            key: "user_id", label: "", html: true,
            format: (v) => `<button type="button" class="btn btn-sm" data-end="${esc(v)}">End Break</button>`,
          },
        ],
        data: rows.map((r) => ({ ...r, member: name(r.user_id) })),
        defaultSort: "started_at",
        defaultAsc: true,
        emptyMsg: "Nobody is on a break.",
      });
    }
    listEl.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-end]");
      if (!btn) return;
      const who = btn.closest("tr").querySelector("td").textContent;
      const ok = await confirmDialog(
        `${who} gets their saved roles back and can see the server again.`,
        { title: "End this break?", confirmLabel: "End Break" },
      );
      if (!ok) return;
      btn.disabled = true;
      try {
        const res = await apiPost(`/api/breaks/${btn.dataset.end}/end`, {});
        toast(res.note || "Break ended.", "success");
        renderList((await api("/api/breaks")).breaks);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
      }
    });
    await renderList(data.breaks);
  }, { errorMsg: "Couldn’t load the Take a Break settings." });
}
