import { api, apiPost, apiDelete, esc } from "../api.js";
import { showStatus, guardForm, mountPicker, mountRolePicker, mountMemberPicker, loadMembers, lockUnlessAdmin, mountAsync, guardedSubmit } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { mountPanelPoster } from "../panel-post.js";
import { toast, confirmDialog } from "../ui.js";
import { mountRoleDialStates } from "../role-dial-state.js";

// Saveable-profile fields owners may persist between sessions.
const SAVEABLE_FIELDS = [
  ["name", "Room name"],
  ["limit", "User limit"],
  ["access", "Room access"],
  ["trusted", "Trust list"],
  ["blocked", "Block list"],
];

/**
 * Voice Control — how member-owned voice rooms behave (Config → Voice,
 * id config-voice-master, modReadOnly). The usage evidence lives on
 * Reports → Engagement → Voice Activity, cross-linked via `related:`.
 * lockUnlessAdmin stays as defense in depth even though the router already
 * holds the page read-only for a moderator; writes are refused server-side
 * regardless.
 */
export function mount(outer) {
  outer.innerHTML = `
    <div class="panel">
      <header>
        <h2>Voice Control</h2>
        <div class="subtitle">How member-owned voice rooms behave</div>
      </header>
      <!-- A plain div, not <section>: .panel > section gets boxed chrome,
           which would wrap every card below in one outer tile. Each
           large heading gets its own .card instead (Billy: separate boxes
           on the large headings), so this wrapper stays invisible. -->
      <div data-region="settings"></div>
    </div>
  `;
  const container = outer.querySelector('[data-region="settings"]');
  container.innerHTML = `<div class="empty">Loading Voice Control configuration…</div>`;

  return mountAsync(container, async () => {
    // No local catch: a failed load is an error, not an empty state, and
    // mountAsync's rejection path renders exactly that — with a Try again
    // button, which the `class="empty"` message here never offered.
    const [cfg, channels, roles] = await Promise.all([
      api("/api/voice-master/config"),
      api("/api/meta/channels?types=text,voice,category"),
      api("/api/meta/roles"),
    ]);

    const numField = (name, label, value, hint, { min = 0, max = null } = {}) => `
      <div class="field">
        <label for="vm-${name}">${esc(label)}</label>
        <input type="number" name="${name}" id="vm-${name}" required min="${min}"${max != null ? ` max="${max}"` : ""}
          step="1" value="${esc(String(value ?? 0))}" style="max-width:140px;" />
        ${hint ? `<div class="field-hint">${hint}</div>` : ""}
      </div>`;

    const toggleField = (name, label, checked, hint) => `
      <div class="field">
        <label style="display:flex; gap:6px; align-items:center;">
          <input type="checkbox" name="${name}"${checked ? " checked" : ""} /> ${esc(label)}
        </label>
        <div class="field-hint">${hint}</div>
      </div>`;

    container.innerHTML = `
      <div>
        <div class="section-label">Voice Control Settings</div>
        <div class="field-hint" style="margin-bottom:12px;">Member-owned voice channels created by joining the Hub</div>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Wiring</div>
            <div class="field">
              <label>Hub Channel</label>
              <span data-picker="hub_channel_id"></span>
              <div class="field-hint">The voice channel members join to spin up their own room. "(unset)" turns Voice Control off.</div>
            </div>
            <div class="field">
              <label>Target Category</label>
              <span data-picker="category_id"></span>
              <div class="field-hint">Category new rooms are created under. "(unset)" creates them outside any category.</div>
            </div>
            <div class="field">
              <label>Control Channel</label>
              <span data-picker="control_channel_id"></span>
              <div class="field-hint">Where the owner control panel posts, and where knock requests go when the owner's DMs are closed.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">New-Room Defaults</div>
            <div class="field">
              <label for="vm-default_name_template">Default Name Template</label>
              <input type="text" name="default_name_template" id="vm-default_name_template"
                value="${esc(cfg.default_name_template || "")}" />
              <div class="field-hint">Name given to a freshly created room. Tokens: {display_name}, {username}.</div>
            </div>
            ${numField("default_user_limit", "Default User Limit", cfg.default_user_limit,
              "How many members a new room admits. 0 means no cap.")}
            ${numField("default_bitrate", "Default Bitrate (bits per second)", cfg.default_bitrate,
              "Audio quality for new rooms, e.g. 64000. 0 uses the highest bitrate the server's boost tier allows.")}
            ${toggleField("post_inline_panel", "Post the control panel in each new room's chat", cfg.post_inline_panel,
              "Gives each new room its own copy of the owner control panel in its text chat.")}
          </div>

          <div class="card">
            <div class="section-label">Limits & Cleanup</div>
            ${numField("create_cooldown_s", "Create Cooldown (seconds)", cfg.create_cooldown_s,
              "A member must wait this long between creating rooms. 0 means no cooldown.")}
            ${numField("max_per_member", "Max Rooms per Member", cfg.max_per_member,
              "How many rooms one member can own at once, 1–10.", { min: 1 })}
            ${numField("owner_grace_s", "Owner-Disconnect Grace (seconds)", cfg.owner_grace_s,
              "After the owner disconnects, the room waits this long before offering the Claim button to whoever is left.")}
            ${numField("empty_grace_s", "Empty-Room Grace (seconds)", cfg.empty_grace_s,
              "An empty room is deleted after this long, giving members a window to hop back in.")}
          </div>

          <div class="card">
            <div class="section-label">Trust & Access</div>
            ${numField("trust_cap", "Trust List Cap", cfg.trust_cap,
              "Most members on an owner's trust list. Adding past it drops the oldest. 0 means no cap.")}
            ${numField("block_cap", "Block List Cap", cfg.block_cap,
              "Most members on an owner's block list. Adding past it drops the oldest. 0 means no cap.")}
            ${numField("trusted_prune_days", "Trusted-Entry Expiry (days)", cfg.trusted_prune_days,
              "Removes trusted members who haven't posted in the server for this many days. 0 keeps them forever.")}
            <div class="field">
              <label>Spectator Gate Role</label>
              <span data-picker="spectator_gate_role_id"></span>
              <div class="field-hint">Only members with this role can join spectator rooms. Left as (none), anyone can join, muted.</div>
              <div class="field-hint">No role yet? Offer <strong>Voice Spectator</strong> on <a href="#/onboarding">Discord Onboarding</a> and the bot creates it.</div>
              <div data-role-state="voice_master_spectator_gate_role_id"></div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Saved Layouts</div>
            ${toggleField("saves_enabled", "Save member layouts", !cfg.disable_saves,
              "Lets owners save their room setup and get it back next time. Off, every room starts from the defaults above.")}
            <div class="field">
              <label>Saveable Fields</label>
              <div style="display:flex; flex-wrap:wrap; gap:8px 16px;">
                ${SAVEABLE_FIELDS.map(([value, label]) => `
                  <label style="display:flex; gap:6px; align-items:center;">
                    <input type="checkbox" name="saveable_fields" value="${value}"${(cfg.saveable_fields || []).includes(value) ? " checked" : ""} /> ${esc(label)}
                  </label>`).join("")}
              </div>
              <div class="field-hint">Which parts of a room's setup owners can save. Unticked parts stop coming back, even if already saved.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="card" style="margin-top:20px;" data-poster="voice-control"></div>

        <div class="card" style="margin-top:20px;" data-live-rooms>
          <div class="section-label">Live Rooms</div>
          <div class="field-hint" style="margin-bottom:8px;">Rooms Voice Control is tracking right now. Each action is logged and posted to the mod log.</div>
          <div data-rooms-table></div>
          <form class="form" data-transfer-form hidden style="margin-top:12px;">
            <div class="field">
              <label data-transfer-label>Give this room to</label>
              <span data-picker="transfer_owner"></span>
              <div class="field-hint">The new owner gets full control of the room. The old owner keeps their saved layout.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button type="submit" class="btn btn-primary">Transfer Room</button>
              <button type="button" class="btn btn-ghost" data-transfer-cancel>Cancel</button>
              <span data-transfer-status></span>
            </div>
          </form>
          <div class="section-label" style="margin-top:16px;">Clear a Saved Layout</div>
          <form class="form" data-clear-form>
            <div class="field">
              <label>Member</label>
              <span data-picker="clear_member"></span>
              <div class="field-hint">Wipes their saved room name, limit, access, trust list and block list. Their next room starts from your defaults.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button type="submit" class="btn btn-danger">Clear Saved Layout</button>
              <span data-clear-status></span>
            </div>
          </form>
        </div>

        <div class="card" style="margin-top:20px;">
          <div class="section-label">Post How-To Guide</div>
          <form class="form" data-howto-form>
            <div class="field">
              <label>Guide Channel</label>
              <span data-picker="howto_channel_id"></span>
              <div class="field-hint">Posts a member-facing "how Voice Control works" embed here (e.g. in your lobby). Safe to re-run anytime.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button type="submit" class="btn">Post Guide</button>
              <span data-howto-status></span>
            </div>
          </form>
        </div>

        <div class="card" style="margin-top:20px;">
          <div class="section-label">Room-Name Blocklist</div>
          <div class="field-hint" style="margin-bottom:8px;">Room names containing any of these phrases are rejected. Matching ignores upper/lower case.</div>
          <form class="form" data-bl-form style="display:flex; gap:8px; align-items:center;">
            <label for="vm-bl-input" class="visually-hidden" style="position:absolute; left:-9999px;">Blocked phrase</label>
            <input type="text" id="vm-bl-input" placeholder="Phrase to block…" />
            <button type="submit" class="btn">Add</button>
          </form>
          <ul data-bl-list style="margin-top:10px;"></ul>
        </div>
      </div>`;

    // The owner panel goes to the Control Channel set above, not a picked one —
    // its buttons drive a flow the cog only watches there.
    mountPanelPoster(
      container.querySelector('[data-poster="voice-control"]'),
      "voice-control",
      { heading: "Post Owner Control Panel", buttonLabel: "Post Panel" },
    );

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const voiceOptions = channels
      .filter((c) => c.type === "voice")
      .map((c) => ({ id: String(c.id), label: `🔊 ${c.name}` }));
    const categoryOptions = channels
      .filter((c) => c.type === "category")
      .map((c) => ({ id: String(c.id), label: `📁 ${c.name}` }));
    const textOptions = channels
      .filter((c) => c.type === "text")
      .map((c) => ({ id: String(c.id), label: `#${c.name}` }));

    const hubPicker = mountPicker(
      form.querySelector('[data-picker="hub_channel_id"]'),
      voiceOptions, String(cfg.hub_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Hub Channel" },
    );
    const categoryPicker = mountPicker(
      form.querySelector('[data-picker="category_id"]'),
      categoryOptions, String(cfg.category_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Target Category" },
    );
    const controlPicker = mountPicker(
      form.querySelector('[data-picker="control_channel_id"]'),
      textOptions, String(cfg.control_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Control Channel" },
    );
    const spectatorPicker = mountRolePicker(
      form.querySelector('[data-picker="spectator_gate_role_id"]'),
      roles, String(cfg.spectator_gate_role_id || "0"),
      { emptyLabel: "(none — open to everyone)", label: "Spectator Gate Role" },
    );

    guardForm(form);
    mountRoleDialStates(container);

    // Client-side validation names the offending field (W-C5).
    const NUM_FIELDS = [
      ["default_user_limit", "Default User Limit", 0],
      ["default_bitrate", "Default Bitrate", 0],
      ["create_cooldown_s", "Create Cooldown", 0],
      ["max_per_member", "Max Rooms per Member", 1],
      ["trust_cap", "Trust List Cap", 0],
      ["block_cap", "Block List Cap", 0],
      ["owner_grace_s", "Owner-Disconnect Grace", 0],
      ["empty_grace_s", "Empty-Room Grace", 0],
      ["trusted_prune_days", "Trusted-Entry Expiry", 0],
    ];

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const nums = {};
      for (const [name, label, min] of NUM_FIELDS) {
        const v = parseInt(fd.get(name), 10);
        if (!Number.isFinite(v) || v < min) {
          showStatus(status, false, `${label} must be a number ≥ ${min}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        nums[name] = v;
      }
      const saveable = [...form.querySelectorAll('input[name="saveable_fields"]:checked')].map((el) => el.value);
      const payload = {
        // Snowflakes stay strings end-to-end; "0" is the unset sentinel.
        hub_channel_id: hubPicker.getValue() || "0",
        category_id: categoryPicker.getValue() || "0",
        control_channel_id: controlPicker.getValue() || "0",
        default_name_template: String(fd.get("default_name_template") || ""),
        ...nums,
        disable_saves: !fd.has("saves_enabled"),
        saveable_fields: saveable,
        post_inline_panel: fd.has("post_inline_panel"),
        spectator_gate_role_id: spectatorPicker.getValue() || "0",
      };
      try {
        await apiPost("/api/voice-master/config", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    // ── How-to guide ──────────────────────────────────────────────────
    const howtoForm = container.querySelector("[data-howto-form]");
    const howtoStatus = container.querySelector("[data-howto-status]");
    const howtoPicker = mountPicker(
      howtoForm.querySelector('[data-picker="howto_channel_id"]'),
      textOptions, "0",
      { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Guide Channel" },
    );

    guardedSubmit(howtoForm, async () => {
      const channel_id = howtoPicker.getValue() || "0";
      if (channel_id === "0") {
        showStatus(howtoStatus, false, "Pick a channel first");
        return;
      }
      try {
        await apiPost("/api/voice-master/post-howto", { channel_id });
        showStatus(howtoStatus, true, "Guide posted");
      } catch (err) {
        showStatus(howtoStatus, false, err.message);
      }
    });

    // ── Live rooms: force-delete, force-transfer, clear a saved layout ──
    // Admin-only on the server; lockUnlessAdmin below disables the controls for
    // a moderator, who can still read the list.
    const roomsEl = container.querySelector("[data-rooms-table]");
    const transferForm = container.querySelector("[data-transfer-form]");
    const transferLabel = container.querySelector("[data-transfer-label]");
    const transferStatus = container.querySelector("[data-transfer-status]");
    const clearForm = container.querySelector("[data-clear-form]");
    const clearStatus = container.querySelector("[data-clear-status]");
    let rooms = [];
    let transferRoom = null;
    // loadMembers() resolves to [] on failure; the pickers still search the
    // server as you type, so a failed prefetch never blocks the page.
    const members = await loadMembers();
    const transferPicker = mountMemberPicker(
      transferForm.querySelector('[data-picker="transfer_owner"]'), members, "0");
    const clearPicker = mountMemberPicker(
      clearForm.querySelector('[data-picker="clear_member"]'), members, "0");

    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");
    const roomActions = (_v, row) => `
      <button type="button" class="btn btn-sm" data-room-transfer="${esc(row.channel_id)}">Transfer</button>
      <button type="button" class="btn btn-danger btn-sm" data-room-delete="${esc(row.channel_id)}">Delete</button>`;

    function drawRooms() {
      renderSortableTable(roomsEl, {
        columns: [
          { key: "channel_name", label: "Room" },
          { key: "owner_name", label: "Owner", format: (v, r) => v || `Member ${r.owner_id}` },
          { key: "members_count", label: "In Room", cls: "num" },
          { key: "owner_in_channel", label: "Owner Present", format: (v) => (v ? "Yes" : "No") },
          { key: "created_at", label: "Created", format: when },
          { key: "channel_id", label: "Actions", html: true, format: roomActions },
        ],
        data: rooms,
        defaultSort: "created_at",
        defaultAsc: false,
        emptyMsg: "No live rooms right now.",
      });
    }

    async function loadRooms() {
      try {
        rooms = (await api("/api/voice-master/channels")).channels || [];
      } catch (err) {
        roomsEl.textContent = "Couldn’t load the live rooms: " + err.message;
        return;
      }
      drawRooms();
    }
    await loadRooms();

    const roomById = (id) => rooms.find((r) => r.channel_id === id);

    // Delegated: renderSortableTable replaces the markup on every sort.
    roomsEl.addEventListener("click", async (e) => {
      const del = e.target.closest("[data-room-delete]");
      const xfer = e.target.closest("[data-room-transfer]");
      if (xfer) {
        transferRoom = roomById(xfer.dataset.roomTransfer);
        if (!transferRoom) return;
        transferLabel.textContent = `Give “${transferRoom.channel_name}” to`;
        transferForm.hidden = false;
        transferStatus.textContent = "";
        return;
      }
      if (!del) return;
      const room = roomById(del.dataset.roomDelete);
      if (!room) return;
      const ok = await confirmDialog(
        `Delete “${room.channel_name}” now? Anyone inside is disconnected. This cannot be undone.`,
        { title: "Delete voice room", danger: true, confirmLabel: "Delete Room" },
      );
      if (!ok) return;
      try {
        await apiPost(`/api/voice-master/channels/${encodeURIComponent(room.channel_id)}/force-delete`, {});
        toast("Room deleted.", "success");
        await loadRooms();
      } catch (err) {
        toast("Delete failed: " + err.message, "error");
      }
    });

    container.querySelector("[data-transfer-cancel]").addEventListener("click", () => {
      transferForm.hidden = true;
      transferRoom = null;
    });

    guardedSubmit(transferForm, async () => {
      const newOwner = transferPicker.getValue();
      if (!transferRoom || !newOwner || newOwner === "0") {
        showStatus(transferStatus, false, "Pick the member who should own the room.");
        return;
      }
      const ok = await confirmDialog(
        `Make that member the owner of “${transferRoom.channel_name}”? The current owner loses control of it.`,
        { title: "Transfer voice room", danger: true, confirmLabel: "Transfer" },
      );
      if (!ok) return;
      try {
        await apiPost(
          `/api/voice-master/channels/${encodeURIComponent(transferRoom.channel_id)}/force-transfer`,
          { new_owner_id: newOwner },
        );
        showStatus(transferStatus, true);
        transferForm.hidden = true;
        transferRoom = null;
        await loadRooms();
      } catch (err) {
        showStatus(transferStatus, false, err.message);
      }
    });

    guardedSubmit(clearForm, async () => {
      const member = clearPicker.getValue();
      if (!member || member === "0") {
        showStatus(clearStatus, false, "Pick a member first.");
        return;
      }
      const ok = await confirmDialog(
        "Clear this member’s saved room layout? Their name, limit, access, trust and block lists are deleted.",
        { title: "Clear saved layout", danger: true, confirmLabel: "Clear Layout" },
      );
      if (!ok) return;
      try {
        await apiPost(`/api/voice-master/profiles/${encodeURIComponent(member)}/clear`, {});
        showStatus(clearStatus, true);
      } catch (err) {
        showStatus(clearStatus, false, err.message);
      }
    });

    // ── Name blocklist ────────────────────────────────────────────────
    const blForm = container.querySelector("[data-bl-form]");
    const blInput = container.querySelector("#vm-bl-input");
    const blList = container.querySelector("[data-bl-list]");

    function renderList(patterns) {
      blList.textContent = "";
      for (const p of patterns) {
        const li = document.createElement("li");
        li.textContent = p + " ";
        const del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-danger btn-sm";
        del.textContent = "Remove";
        del.addEventListener("click", async () => {
          try {
            await apiDelete("/api/voice-master/name-blocklist/" + encodeURIComponent(p));
            li.remove();
          } catch (err) {
            toast("Remove failed: " + err.message, "error");
          }
        });
        li.appendChild(del);
        blList.appendChild(li);
      }
    }
    renderList(cfg.name_blocklist || []);

    guardedSubmit(blForm, async () => {
      const pattern = blInput.value.trim();
      if (!pattern) return;
      try {
        await apiPost("/api/voice-master/name-blocklist", { pattern });
        blInput.value = "";
        const fresh = await api("/api/voice-master/config");
        renderList(fresh.name_blocklist || []);
      } catch (err) {
        toast("Add failed: " + err.message, "error");
      }
    });

    // Last, so the pickers' and blocklist rows' own controls are covered too.
    lockUnlessAdmin(container);
  }, { errorMsg: "Couldn’t load the voice settings." });
}
