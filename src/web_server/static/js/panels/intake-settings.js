import { esc, loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, apiPost, showStatus, lockUnlessAdmin, mountAsync, guardedSubmit } from "../config-helpers.js";

// Auto-kind choices for a step. "" = manual (welcomers tick a button on the
// card); the rest tick themselves from bot events.
const AUTO_KINDS = [
  ["", "Manual (button on the card, unless it has a step code)"],
  ["greeted", "Auto: greeted in chat"],
  ["verified", "Auto: verification passed"],
  ["role_gained", "Auto: role gained"],
  ["grant", "Grant button (gives a role)"],
];

// Kinds that name a role: the picker shows for these, and a save without one
// is refused (a role-less step could never tick).
const ROLE_KINDS = new Set(["role_gained", "grant"]);

/**
 * One checklist editor (used once per stage). Rows re-render from a working
 * copy; `problem()` and `payload()` sync the inputs back first.
 */
function mountStepEditor(host, addButton, initial, roles) {
  const steps = (initial || []).map((s) => ({ ...s }));

  const render = () => {
    host.innerHTML = steps
      .map(
        (s, i) => `
      <div data-step-row data-key="${esc(s.key || "")}" style="display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:6px;">
        <input type="text" data-step-label value="${esc(s.label)}" maxlength="80" placeholder="Step label" aria-label="Step label" style="flex:2; min-width:15rem;" />
        <select data-step-auto aria-label="How this step completes" style="flex:2; min-width:10rem;">
          ${AUTO_KINDS.map(([v, t]) => `<option value="${v}" ${v === (s.auto || "") ? "selected" : ""}>${t}</option>`).join("")}
        </select>
        <select data-step-role aria-label="Role for this step" style="flex:2; min-width:10rem; ${ROLE_KINDS.has(s.auto) ? "" : "display:none;"}">
          ${roleSelect(roles, s.role_id)}
        </select>
        <input type="text" data-step-code value="${esc(s.code || "")}" maxlength="80" placeholder="Step code (optional)" aria-label="Step code" style="flex:2; min-width:10rem;" />
        <span style="white-space:nowrap;">
          <button type="button" class="btn" data-up ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="btn" data-down ${i === steps.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="btn" data-remove>✕</button>
        </span>
      </div>`
      )
      .join("");
  };

  // Pull edited values back into the working copy before any re-render.
  const sync = () => {
    host.querySelectorAll("[data-step-row]").forEach((row, i) => {
      steps[i].label = row.querySelector("[data-step-label]").value;
      steps[i].auto = row.querySelector("[data-step-auto]").value;
      steps[i].role_id = row.querySelector("[data-step-role]").value || "0";
      steps[i].code = row.querySelector("[data-step-code]").value;
    });
  };

  host.addEventListener("click", (e) => {
    const row = e.target.closest("[data-step-row]");
    if (!row) return;
    const i = Array.from(host.children).indexOf(row);
    if (e.target.matches("[data-remove]")) {
      sync();
      steps.splice(i, 1);
      render();
    } else if (e.target.matches("[data-up]") && i > 0) {
      sync();
      [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]];
      render();
    } else if (e.target.matches("[data-down]") && i < steps.length - 1) {
      sync();
      [steps[i], steps[i + 1]] = [steps[i + 1], steps[i]];
      render();
    }
  });

  // Show/hide the role picker as the kind changes.
  host.addEventListener("change", (e) => {
    if (!e.target.matches("[data-step-auto]")) return;
    const row = e.target.closest("[data-step-row]");
    row.querySelector("[data-step-role]").style.display = ROLE_KINDS.has(e.target.value) ? "" : "none";
  });

  addButton.addEventListener("click", () => {
    sync();
    steps.push({ key: "", label: "", auto: "", role_id: "0", code: "" });
    render();
    const rows = host.querySelectorAll("[data-step-label]");
    rows[rows.length - 1]?.focus();
  });

  render();

  return {
    /** First problem as a sentence, or "" when the list can be saved. */
    problem() {
      sync();
      // String check — snowflakes must never go through parseInt (precision).
      const bad = steps.find((s) => ROLE_KINDS.has(s.auto) && (!s.role_id || s.role_id === "0"));
      if (bad) return `“${bad.label || "unnamed step"}” needs a role`;
      if (steps.some((s) => !s.label.trim())) return "Every step needs a label";
      return "";
    },
    payload() {
      sync();
      return steps.map((s) => ({
        key: s.key || "",
        label: s.label.trim(),
        auto: s.auto || "",
        role_id: s.role_id || "0",
        code: (s.code || "").trim(),
      }));
    },
  };
}

/**
 * Intake Cards — card behaviour and the bot-synced procedure reference
 * (Config → Members, id config-intake, modReadOnly). The queue those cards
 * form lives on Reports → Greeter → Intake Queue, cross-linked via
 * `related:`. lockUnlessAdmin stays as defense in depth; writes are refused
 * server-side regardless.
 */
export function mount(outer) {
  outer.innerHTML = `
    <div class="panel">
      <header>
        <h2>Intake Cards</h2>
        <div class="subtitle">The procedure newcomers follow, and the card that tracks it</div>
      </header>
      <section data-region="settings"></section>
    </div>
  `;
  return mountSettings(outer.querySelector('[data-region="settings"]'));
}

export function mountSettings(container) {
  container.innerHTML = `<div class="empty">Loading config…</div>`;

  return mountAsync(container, async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const c = config.intake || { enabled: false, channel_id: "0", completion_code: "", stale_hours: 24, steps: [] };
    // Greeting is watched where newcomers are talked to, not where the cards
    // post — spell out which channel that resolves to so nobody assumes the
    // card channel is being watched.
    // Three states, not two: a channel we can name, a configured channel we
    // can't name (the channel list failed to load — saying "none set" there
    // would send an admin to fix a setting that is fine), and none set.
    const greetChannelId = c.greet_channel_id || "0";
    const greetChannel = channels.find((ch) => ch.id === greetChannelId);
    const greetHow = `Set in Welcome &amp; Leave.`;
    let greetChannelHint;
    if (greetChannel) {
      greetChannelHint = `Greeter or mod mentions and replies in <strong>#${esc(greetChannel.name)}</strong> tick “greeted in chat” steps. ${greetHow}`;
    } else if (greetChannelId !== "0") {
      greetChannelHint = `Greeter or mod mentions and replies in channel <strong>${esc(greetChannelId)}</strong> tick “greeted in chat” steps. ${greetHow}`;
    } else {
      greetChannelHint = `<strong>No channel set.</strong> “Greeted in chat” steps can’t tick. Set a greeter chat or welcome channel in Welcome &amp; Leave.`;
    }

    container.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:24px;">
      <div>
        <div class="section-label">Card Settings</div>
        <div class="field-hint" style="margin-bottom:12px;">A welcome checklist for each newcomer. The open cards form the queue on <a href="#/intake-report">Intake Queue</a>.</div>
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""} /> Enable intake cards</label>
            <div class="field-hint">Each join posts a card pinging the greeter role, in place of the Arrival Line. Needs a card or greeter chat channel.</div>
          </div>
          <div class="field">
            <label for="intakesettin-channel-id">Card channel</label>
            <select name="channel_id" id="intakesettin-channel-id">${channelSelect(channels, c.channel_id)}</select>
            <div class="field-hint">Where cards post. Leave on (disabled) to use the greeter chat channel from Welcome &amp; Leave.</div>
          </div>
          <div class="field">
            <label>Greeting watched in</label>
            <div class="field-hint">${greetChannelHint}</div>
          </div>
          <div class="field">
            <label for="intakesettin-verified-role-id">Verified role</label>
            <select name="verified_role_id" id="intakesettin-verified-role-id">${roleSelect(roles, c.verified_role_id || "0")}</select>
            <div class="field-hint">Set this if your verification bot grants a role on a pass. (none) counts only removal of the Unverified role.</div>
          </div>
          <div class="field">
            <label for="intakesettin-completion-code">Completion code</label>
            <input type="text" name="completion_code" value="${esc(c.completion_code)}" maxlength="80" placeholder="e.g. DK-7734" / id="intakesettin-completion-code">
            <div class="field-hint">A greeter or mod posting this while mentioning or replying to a newcomer closes their card. Blank turns it off.</div>
          </div>
          <div class="field">
            <label for="intakesettin-stale-hours">Stale nudge after (hours)</label>
            <input type="number" name="stale_hours" min="1" max="720" value="${Number(c.stale_hours) || 24}" / id="intakesettin-stale-hours">
            <div class="field-hint">A card with no ticked step for this many hours gets one greeter-role nudge, and never a second.</div>
          </div>
          <div class="field">
            <label>Checklist steps</label>
            <div data-steps></div>
            <button type="button" class="btn" data-add-step>+ Add step</button>
            <div class="field-hint">New cards copy this list; open ones keep theirs except step codes, which apply live. Grant steps add a Give role button.</div>
            <div class="field-hint"><strong>Step code</strong> (optional): a phrase a greeter or mod posts, mentioning or replying to the newcomer, to tick this step.</div>
          </div>
          <div class="section-label" style="margin-top:12px;">Stage 2 — after Level 5</div>
          <div class="field">
            <label><input type="checkbox" name="stage2_enabled" ${c.stage2_enabled ? "checked" : ""} /> Open a Stage 2 card on Level 5 approval</label>
            <div class="field-hint">Approve on a Level 5 card posts a second card; greeters grant NSFW from it. Needs intake cards on.</div>
          </div>
          <div class="field">
            <label for="intakesettin-stage2-optin-role-id">Only for members with role</label>
            <select name="stage2_optin_role_id" id="intakesettin-stage2-optin-role-id">${roleSelect(roles, c.stage2_optin_role_id || "0")}</select>
            <div class="field-hint">Members without this role get no Level 5 promotion card and no Stage 2. (none) lets every member through.</div>
          </div>
          <div class="field">
            <label>Stage 2 checklist steps</label>
            <div data-steps2></div>
            <button type="button" class="btn" data-add-step2>+ Add step</button>
            <div class="field-hint">Same step types as above. Codes can’t contain each other or the completion code, across both lists.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
      <div>
        <div class="section-label">Procedure Reference</div>
        <div class="field-hint" style="margin-bottom:12px;">Edit your welcome procedure here and the bot syncs the channel. Each question gets its own message to copy.</div>
        <form class="form" data-ref-form>
          <div class="field">
            <label for="intakesettin-ref-channel-id">Reference channel</label>
            <select name="ref_channel_id" id="intakesettin-ref-channel-id">${channelSelect(channels, c.reference_channel_id || "0")}</select>
            <div class="field-hint">The channel the bot keeps in sync, e.g. #welcome-procedure. Consider locking it to bot posts. (disabled) means no sync.</div>
          </div>
          <div class="field">
            <label>Blocks</label>
            <div data-blocks></div>
            <button type="button" class="btn" data-add-block>+ Add block</button>
            <button type="button" class="btn" data-import>Import from channel</button>
            <div class="field-hint">Question lists post one message per line. Import (empty editor only) turns the channel’s messages into text blocks.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save &amp; sync channel</button><span data-ref-status></span></div>
        </form>
      </div>
      </div>
    `;

    const stepsHost = container.querySelector("[data-steps]");
    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const stage1 = mountStepEditor(stepsHost, container.querySelector("[data-add-step]"), c.steps, roles);
    const stage2 = mountStepEditor(
      container.querySelector("[data-steps2]"),
      container.querySelector("[data-add-step2]"),
      c.stage2_steps,
      roles,
    );

    // ── Procedure reference blocks ──────────────────────────────────
    let blocks = (c.reference_blocks || []).map((b) => ({ ...b }));
    const refForm = container.querySelector("[data-ref-form]");
    const refStatus = container.querySelector("[data-ref-status]");
    const blocksHost = container.querySelector("[data-blocks]");

    const renderBlocks = () => {
      blocksHost.innerHTML = blocks
        .map(
          (b, i) => `
        <div data-block-row style="border:1px solid var(--rule); border-radius:6px; padding:8px; margin-bottom:8px;">
          <div class="row wrap" style="gap:6px; margin-bottom:6px;">
            <select data-block-kind style="min-width:10rem;">
              <option value="text" ${b.kind !== "questions" ? "selected" : ""}>Text section</option>
              <option value="questions" ${b.kind === "questions" ? "selected" : ""}>Question list</option>
            </select>
            <input type="text" data-block-title value="${esc(b.title || "")}" maxlength="120" placeholder="Title (optional)" style="flex:2; min-width:10rem;" />
            <span style="white-space:nowrap;">
              <button type="button" class="btn" data-block-up ${i === 0 ? "disabled" : ""}>↑</button>
              <button type="button" class="btn" data-block-down ${i === blocks.length - 1 ? "disabled" : ""}>↓</button>
              <button type="button" class="btn" data-block-remove>✕</button>
            </span>
          </div>
          <textarea data-block-body rows="${b.kind === "questions" ? 6 : 4}" style="width:100%;" placeholder="${b.kind === "questions" ? "One question per line" : "Section text"}">${esc(b.body || "")}</textarea>
        </div>`
        )
        .join("");
    };

    const syncBlocks = () => {
      blocksHost.querySelectorAll("[data-block-row]").forEach((row, i) => {
        blocks[i].kind = row.querySelector("[data-block-kind]").value;
        blocks[i].title = row.querySelector("[data-block-title]").value;
        blocks[i].body = row.querySelector("[data-block-body]").value;
      });
    };

    blocksHost.addEventListener("click", (e) => {
      const row = e.target.closest("[data-block-row]");
      if (!row) return;
      const i = Array.from(blocksHost.children).indexOf(row);
      if (e.target.matches("[data-block-remove]")) {
        syncBlocks();
        blocks.splice(i, 1);
        renderBlocks();
      } else if (e.target.matches("[data-block-up]") && i > 0) {
        syncBlocks();
        [blocks[i - 1], blocks[i]] = [blocks[i], blocks[i - 1]];
        renderBlocks();
      } else if (e.target.matches("[data-block-down]") && i < blocks.length - 1) {
        syncBlocks();
        [blocks[i], blocks[i + 1]] = [blocks[i + 1], blocks[i]];
        renderBlocks();
      }
    });

    container.querySelector("[data-add-block]").addEventListener("click", () => {
      syncBlocks();
      blocks.push({ kind: "text", title: "", body: "" });
      renderBlocks();
    });

    container.querySelector("[data-import]").addEventListener("click", async () => {
      if (blocks.length) {
        showStatus(refStatus, false, "Import only works while the editor is empty");
        return;
      }
      const channelId = refForm.querySelector('select[name="ref_channel_id"]').value || "0";
      if (channelId === "0") {
        showStatus(refStatus, false, "Pick the reference channel first");
        return;
      }
      try {
        const res = await apiPost("/api/config/intake/reference/import", { channel_id: channelId });
        blocks = (res.blocks || []).map((b) => ({ ...b }));
        renderBlocks();
        showStatus(refStatus, true, `Imported ${blocks.length} blocks. Review, split the question lists, then save.`);
      } catch (err) {
        showStatus(refStatus, false, err.message);
      }
    });

    renderBlocks();

    guardedSubmit(refForm, async () => {
      syncBlocks();
      try {
        const res = await apiPut("/api/config/intake/reference", {
          channel_id: refForm.querySelector('select[name="ref_channel_id"]').value || "0",
          blocks: blocks.map((b) => ({
            kind: b.kind || "text",
            title: (b.title || "").trim(),
            body: b.body || "",
          })),
        });
        const s = res.sync || {};
        let counts = `${s.posted || 0} posted, ${s.edited || 0} edited, ${s.deleted || 0} deleted`;
        // Messages someone deleted in Discord: worth calling out, since the
        // repair re-sends the rest of the channel to keep it in order.
        if (s.repaired) counts += `, ${s.repaired} restored`;
        if (!s.synced) {
          showStatus(refStatus, true, `Saved, but the channel was not synced (${s.reason || "unknown"})`);
        } else if (s.incomplete) {
          // Discord rejected part of the plan; the next save retries it.
          showStatus(refStatus, false, `Saved, but the channel is only partly synced (${counts}). Save again to retry.`);
        } else {
          showStatus(refStatus, true, `Saved. Channel synced (${counts}).`);
        }
      } catch (err) {
        showStatus(refStatus, false, err.message);
      }
    });

    guardedSubmit(form, async () => {
      const problem = stage1.problem() || stage2.problem();
      if (problem) {
        showStatus(status, false, problem);
        return;
      }
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/intake", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_id: fd.get("channel_id") || "0",
          verified_role_id: fd.get("verified_role_id") || "0",
          completion_code: fd.get("completion_code") || "",
          stale_hours: parseFloat(fd.get("stale_hours")) || 24,
          steps: stage1.payload(),
          stage2_enabled: form.querySelector('input[name="stage2_enabled"]').checked,
          stage2_optin_role_id: fd.get("stage2_optin_role_id") || "0",
          stage2_steps: stage2.payload(),
        });
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    // Last, so every control both forms built — including the dynamically
    // rendered step and block rows — is covered.
    lockUnlessAdmin(container);
  }, { errorMsg: "Couldn’t load the intake settings." });
}
