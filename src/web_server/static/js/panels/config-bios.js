import { api, apiPost, esc } from "../api.js";
import { confirmDialog } from "../ui.js";
import {
  apiDelete,
  apiPut,
  field as labeledField,
  loadCategories,
  loadChannels,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountCategoryPicker,
  guardedSubmit,
} from "../config-helpers.js";
import { mountTabs } from "../tabs.js";

// A bios channel can be correctly configured here and still reach nobody —
// deleting a role silently takes its channel permission overwrite with it.
// The server checks the live channel; this just shows what it found.
function renderChannelIssues(issues) {
  if (!issues || !issues.length) return "";
  const rows = issues
    .map((i) => {
      const where = i.channel_name ? ` (#${esc(i.channel_name)})` : "";
      return `<li><strong>${esc(i.label)}${where}:</strong> ${esc(i.message)}</li>`;
    })
    .join("");
  return `
    <div class="save-status save-err" role="alert" style="margin-bottom:0.75rem;">
      <div>Bios won't work properly until these are fixed:</div>
      <ul style="margin:0.4rem 0 0; padding-left:1.2rem;">${rows}</ul>
    </div>
  `;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Bios</h2>
        <div class="subtitle">A guided walk-through that helps members introduce themselves, and the questions it asks</div>
      </header>
      <div data-tabs></div>
    </div>
  `;

  // The tab strip + its lazy per-tab loading (each pane fetched only the
  // first time its tab is opened, a failed fetch turned into a Retry button
  // instead of a stuck "Loading…") lives in tabs.js — see its header comment
  // for the rejection-reaches-the-guard contract `render` below relies on.
  return mountTabs(container.querySelector("[data-tabs]"), [
    { key: "config", label: "Settings", render: renderConfigTab,
      errorMsg: "Couldn’t load the bios settings." },
    { key: "fields", label: "Profile Questions", render: renderFieldsTab,
      errorMsg: "Couldn’t load the profile questions." },
    { key: "questions", label: "Icebreakers", render: renderQuestionsTab,
      errorMsg: "Couldn’t load the icebreaker questions." },
  ], { ariaLabel: "Bios settings sections" });
}

// ── Config tab ──────────────────────────────────────────────────────

async function renderConfigTab(pane) {
  pane.innerHTML = `<div class="empty">Loading bios config…</div>`;
  const [config, channels, categories] = await Promise.all([
    api("/api/bios/config"),
    loadChannels(),
    loadCategories(),
  ]);

  const colorVal = (config.embed_color || "#C8763E").replace(/^#/, "");
  const colorHex = `#${colorVal}`;

  pane.innerHTML = `
    ${renderMetaWarning()}
    ${renderChannelIssues(config.channel_issues)}
    <form class="form form-cards" data-form></form>
    <div class="card" style="margin-top:12px;">
      <div class="section-label">Start Button</div>
      <p class="field-hint">Posts the <strong>📝 Create / Update Bio</strong> button in the bios channel, replacing any earlier one. Uses the saved wording above.</p>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
        <button type="button" class="btn btn-secondary" data-post-btn>Post the Start Button</button>
        <span data-post-status></span>
      </div>
    </div>
  `;

  const form = pane.querySelector("[data-form]");
  const card = (title) => {
    const el = document.createElement("div");
    el.className = "card";
    const lbl = document.createElement("div");
    lbl.className = "section-label";
    lbl.textContent = title;
    el.appendChild(lbl);
    form.appendChild(el);
    return el;
  };
  const mkInput = (name, attrs) => {
    const el = document.createElement("input");
    el.name = name;
    Object.assign(el, attrs);
    return el;
  };
  const mkTextarea = (name, attrs) => {
    const el = document.createElement("textarea");
    el.name = name;
    Object.assign(el, attrs);
    return el;
  };

  const wiringCard = card("Channels");
  const biosSlot = document.createElement("span");
  wiringCard.appendChild(labeledField(
    "Bios Channel",
    biosSlot,
    "Where finished bios are posted. \"(disabled)\" turns bios off, and /bio says they aren't set up.",
  ));
  // Snowflakes stay strings; "0" is the unset sentinel the API already expects.
  const biosPicker = mountChannelPicker(
    biosSlot, channels, String(config.bios_channel_id || "0"),
    { emptyValue: "0", emptyLabel: "(disabled)", label: "Bios Channel" },
  );

  const catSlot = document.createElement("span");
  wiringCard.appendChild(labeledField(
    "Category for Private Writing Rooms",
    catSlot,
    "Each member gets a temporary private room here, so the bot needs Manage Channels. \"(none)\" turns bios off.",
  ));
  const catPicker = mountCategoryPicker(
    catSlot, categories, String(config.wizard_category_id || "0"),
    { emptyValue: "0", emptyLabel: "(none)", label: "Category for Private Writing Rooms" },
  );

  const behaviorCard = card("How the Walk-Through Behaves");
  behaviorCard.appendChild(labeledField(
    "Icebreakers per Bio",
    mkInput("questions_per_bio", { type: "number", required: true, min: "1", max: "10", step: "1", value: String(config.questions_per_bio || 3), style: "max-width:140px;" }),
    "The most icebreakers a member can answer, chosen from the Icebreakers tab. 1–10.",
  ));
  behaviorCard.appendChild(labeledField(
    "Bio Color",
    mkInput("embed_color", { type: "color", value: colorHex }),
    "The colored bar down the side of every posted bio.",
  ));
  behaviorCard.appendChild(labeledField(
    "Give Up After (minutes)",
    mkInput("wizard_timeout", { type: "number", required: true, min: "1", max: "120", step: "1", value: String(config.wizard_timeout || 15), style: "max-width:140px;" }),
    "A member idle this long is timed out, the private room is deleted and nothing is saved. 1–120 minutes.",
  ));
  behaviorCard.appendChild(labeledField(
    "Keep the Room Open After Finishing (seconds)",
    mkInput("archive_grace", { type: "number", required: true, min: "0", max: "3600", step: "1", value: String(config.archive_grace || 60), style: "max-width:140px;" }),
    "Time to read the confirmation before the private room is deleted. 0 deletes it at once.",
  ));

  const copyCard = card("Wording on the Start Button");
  copyCard.appendChild(labeledField(
    "Heading",
    mkInput("trigger_title", { type: "text", maxLength: 256, value: config.trigger_title || "📝 Share your bio", style: "width:100%;" }),
    "The bold line above the button in your bios channel.",
  ));
  copyCard.appendChild(labeledField(
    "Message",
    mkTextarea("trigger_body", { rows: 3, maxLength: 2000, value: config.trigger_body || "", style: "width:100%;" }),
    "The text under the heading; say what a bio is for. Discord formatting works. Post the button again to apply edits.",
  ));

  const submit = document.createElement("div");
  submit.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;align-items:center;";
  submit.innerHTML = `<button type="submit" class="btn btn-primary">Save</button><span data-status></span>`;
  form.appendChild(submit);

  guardForm(form);

  const statusEl = pane.querySelector("[data-status]");
  guardedSubmit(form, async () => {
    const fd = new FormData(form);
    // Validate first so a blank box can't silently snap back to a default the
    // admin never chose (W-C5).
    const nums = {};
    for (const [name, label, min, max] of [
      ["questions_per_bio", "Icebreakers per Bio", 1, 10],
      ["wizard_timeout", "Give Up After", 1, 120],
      ["archive_grace", "Keep the Room Open After Finishing", 0, 3600],
    ]) {
      const raw = String(fd.get(name) ?? "").trim();
      const v = parseInt(raw, 10);
      if (raw === "" || !Number.isFinite(v) || v < min || v > max) {
        showStatus(statusEl, false, `${label} must be a whole number between ${min} and ${max}.`);
        form.querySelector(`[name="${name}"]`).focus();
        return;
      }
      nums[name] = v;
    }
    try {
      await apiPut("/api/bios/config", {
        bios_channel_id: String(biosPicker.getValue() || "0"),
        wizard_category_id: String(catPicker.getValue() || "0"),
        questions_per_bio: nums.questions_per_bio,
        embed_color: String(fd.get("embed_color") || "#C8763E"),
        wizard_timeout: nums.wizard_timeout,
        archive_grace: nums.archive_grace,
        trigger_title: String(fd.get("trigger_title") || "").trim(),
        trigger_body: String(fd.get("trigger_body") || "").trim(),
      });
      showStatus(statusEl, true);
    } catch (err) {
      showStatus(statusEl, false, err.message);
    }
  });

  const postBtn = pane.querySelector("[data-post-btn]");
  const postStatus = pane.querySelector("[data-post-status]");
  postBtn.addEventListener("click", async () => {
    postBtn.disabled = true;
    postStatus.className = "save-status";
    postStatus.textContent = "Posting…";
    try {
      const res = await apiPost("/api/bios/post-trigger-button");
      // One feedback channel per surface (W-C8): everything goes via showStatus.
      // A warning here means the post worked but nobody can reach it — report
      // that as a failure, because from a member's side it is one.
      const warnings = res?.warnings || [];
      if (warnings.length) showStatus(postStatus, false, `Posted, but: ${warnings[0]}`);
      else showStatus(postStatus, true, "Button posted");
    } catch (err) {
      showStatus(postStatus, false, err.message);
    } finally {
      postBtn.disabled = false;
    }
  });
}

// ── Fields tab ──────────────────────────────────────────────────────

async function renderFieldsTab(pane) {
  pane.innerHTML = `<div class="empty">Loading fields…</div>`;
  let state = { template_version: 0, fields: [], headline_warning: false };

  async function refresh() {
    state = await api("/api/bios/fields");
    render();
  }

  function render() {
    const warningHtml = state.headline_warning
      ? `<div class="save-status save-err" role="alert" style="margin-bottom:0.5rem;">
           No question is set as the title yet, so the first one in the list is used
           as each bio's title. Open a short-answer question with Edit and tick
           "Use as the bio's title" to choose one.
         </div>`
      : "";

    const typeLabels = {
      short: "Short answer",
      paragraph: "Long answer",
      choice: "Pick from a list",
    };

    // Every row behaves the same way now: nothing here saves on its own —
    // use Edit to change a question, or the arrows to reorder it (W-C8).
    const rows = state.fields.map((f, i) => `
      <tr data-id="${f.id}" data-active="${f.active ? "1" : "0"}">
        <td>${i + 1}</td>
        <td><span class="label-cell">${esc(f.label)}</span></td>
        <td>${esc(typeLabels[f.field_type] || f.field_type)}</td>
        <td>${f.required ? "Yes" : "No"}</td>
        <td>${f.is_headline ? "Title" : ""}</td>
        <td>${f.active ? "In use" : "<em>Retired</em>"}</td>
        <td>
          <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            <button type="button" class="btn btn-secondary" data-up aria-label="Move ${esc(f.label)} up" ${i === 0 ? "disabled" : ""}>↑</button>
            <button type="button" class="btn btn-secondary" data-down aria-label="Move ${esc(f.label)} down" ${i === state.fields.length - 1 ? "disabled" : ""}>↓</button>
            <button type="button" class="btn btn-secondary" data-edit>Edit</button>
            ${f.active ? `<button type="button" class="btn btn-danger" data-retire>Retire</button>` : ""}
            <span data-row-status></span>
          </div>
        </td>
      </tr>
    `).join("");

    const emptyRow = `<tr><td colspan="7"><em>No profile questions yet. Add one below.</em></td></tr>`;

    pane.innerHTML = `
      <div class="subtitle" style="margin-bottom:0.5rem;">The questions every member answers, in the order asked. Retiring one keeps existing answers. (Version ${state.template_version}.)</div>
      ${warningHtml}
      <div style="overflow-x:auto;">
        <table class="table">
          <thead><tr><th>#</th><th>Question</th><th>Answer Type</th><th>Must Answer</th><th>Bio Title</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>${rows || emptyRow}</tbody>
        </table>
      </div>
      <div style="margin-top:1rem; display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
        <button type="button" class="btn btn-primary" data-add>Add a Question</button>
        <span data-add-status></span>
      </div>
      <div data-editor></div>
    `;
    wire();
  }

  function wire() {
    pane.querySelector("[data-add]").addEventListener("click", () => openEditor(null));

    pane.querySelectorAll("tr[data-id]").forEach((row) => {
      const id = parseInt(row.dataset.id, 10);
      const status = row.querySelector("[data-row-status]");
      const upBtn = row.querySelector("[data-up]");
      const downBtn = row.querySelector("[data-down]");

      if (upBtn) upBtn.addEventListener("click", () => move(id, -1));
      if (downBtn) downBtn.addEventListener("click", () => move(id, +1));

      row.querySelector("[data-edit]").addEventListener("click", () => {
        const f = state.fields.find((x) => x.id === id);
        if (f) openEditor(f);
      });
      const retireBtn = row.querySelector("[data-retire]");
      if (retireBtn) {
        retireBtn.addEventListener("click", async () => {
          const f = state.fields.find((x) => x.id === id);
          const ok = await confirmDialog(
            `Stop asking "${f ? f.label : "this question"}"? Bios that already answered it keep their answer, `
            + "but nobody is asked it again until you switch it back on.",
            { title: "Retire Question", danger: true, confirmLabel: "Retire" },
          );
          if (!ok) return;
          try {
            await apiDelete(`/api/bios/fields/${id}`);
            await refresh();
          } catch (err) {
            showStatus(status, false, err.message);
          }
        });
      }
    });
  }

  async function move(id, delta) {
    const ids = state.fields.map((f) => f.id);
    const idx = ids.indexOf(id);
    if (idx < 0) return;
    const target = idx + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[idx], ids[target]] = [ids[target], ids[idx]];
    const status = pane.querySelector(`tr[data-id="${id}"] [data-row-status]`);
    try {
      await apiPost("/api/bios/fields/reorder", { ordered_ids: ids });
      await refresh();
    } catch (err) {
      // One feedback channel per surface (W-C8) — inline status, not a toast.
      if (status) showStatus(status, false, `Couldn't reorder: ${err.message}`);
    }
  }

  function openEditor(field) {
    const editor = pane.querySelector("[data-editor]");
    const isNew = field === null;
    const f = field || { label: "", field_type: "short", choices: [], required: false, is_headline: false, max_len: 1024, active: true, hint: "" };
    const choicesText = (f.choices || []).join("\n");
    editor.innerHTML = `
      <div class="card" style="margin-top:1rem;">
        <div class="section-label">${isNew ? "Add a Question" : "Edit Question"}</div>
        <form class="form" data-editor-form>
          <div class="field">
            <label for="bio-fe-label">Question</label>
            <input type="text" name="label" id="bio-fe-label" required value="${esc(f.label)}" maxlength="128" style="width:100%;" />
            <div class="field-hint">What members are asked, such as "Pronouns" or "How you found us".</div>
          </div>
          <div class="field">
            <label for="bio-fe-hint">Example Answer <span style="opacity:0.6;">(optional)</span></label>
            <input type="text" name="hint" id="bio-fe-hint" value="${esc(f.hint || "")}" maxlength="256" style="width:100%;" placeholder="Hollow Knight, or that one indie nobody has heard of" />
            <div class="field-hint">Shown under the question so members know the kind of answer you're after. Leave blank for none.</div>
          </div>
          <div class="field">
            <label for="bio-fe-type">Answer Type</label>
            <select name="field_type" id="bio-fe-type">
              <option value="short" ${f.field_type === "short" ? "selected" : ""}>Short answer — one line</option>
              <option value="paragraph" ${f.field_type === "paragraph" ? "selected" : ""}>Long answer — several lines</option>
              <option value="choice" ${f.field_type === "choice" ? "selected" : ""}>Pick from a list you write</option>
            </select>
          </div>
          <div class="field" data-choices-field>
            <label for="bio-fe-choices">The Options to Choose From</label>
            <textarea name="choices" id="bio-fe-choices" rows="4" style="width:100%;">${esc(choicesText)}</textarea>
            <div class="field-hint">One option per line. Up to 5 options appear as buttons; more than that becomes a drop-down list.</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="required" ${f.required ? "checked" : ""} /> Members Must Answer This</label>
            <div class="field-hint">Unchecked, members can skip the question and it's left off their bio.</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="is_headline" ${f.is_headline ? "checked" : ""} /> Use as the Bio's Title</label>
            <div class="field-hint">This answer becomes the heading of the bio. Short answers only, and one question at a time.</div>
          </div>
          <div class="field">
            <label for="bio-fe-maxlen">Longest Answer Allowed (characters)</label>
            <input type="number" name="max_len" id="bio-fe-maxlen" required min="1" max="4096" step="1" value="${f.max_len || 1024}" style="width:7rem;" />
            <div class="field-hint">Anything longer is refused when the member submits. Between 1 and 4096.</div>
          </div>
          ${isNew ? "" : `
            <div class="field">
              <label><input type="checkbox" name="active" ${f.active ? "checked" : ""} /> Ask This Question</label>
              <div class="field-hint">Unchecked, the question is retired: nobody is asked it again, but existing answers stay on their bios.</div>
            </div>
          `}
          <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">${isNew ? "Add Question" : "Save"}</button>
            <button type="button" class="btn btn-secondary" data-cancel>Cancel</button>
            <span data-editor-status></span>
          </div>
        </form>
      </div>
    `;
    const form = editor.querySelector("[data-editor-form]");
    const status = editor.querySelector("[data-editor-status]");
    const typeSel = form.querySelector("select[name=field_type]");
    const choicesField = editor.querySelector("[data-choices-field]");

    const updateChoicesVisibility = () => {
      choicesField.style.display = typeSel.value === "choice" ? "" : "none";
    };
    typeSel.addEventListener("change", updateChoicesVisibility);
    updateChoicesVisibility();

    editor.querySelector("[data-cancel]").addEventListener("click", () => {
      editor.innerHTML = "";
    });

    guardForm(form);

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const label = String(fd.get("label") || "").trim();
      if (!label) {
        showStatus(status, false, "Question cannot be empty.");
        form.querySelector('[name="label"]').focus();
        return;
      }
      const rawMax = String(fd.get("max_len") ?? "").trim();
      const maxLen = parseInt(rawMax, 10);
      if (rawMax === "" || !Number.isFinite(maxLen) || maxLen < 1 || maxLen > 4096) {
        showStatus(status, false, "Longest Answer Allowed must be a whole number between 1 and 4096.");
        form.querySelector('[name="max_len"]').focus();
        return;
      }
      if (String(fd.get("field_type")) === "choice"
        && !String(fd.get("choices") || "").split("\n").some((s) => s.trim())) {
        showStatus(status, false, "Add at least one option for members to choose from.");
        form.querySelector('[name="choices"]').focus();
        return;
      }
      const body = {
        label,
        field_type: String(fd.get("field_type") || "short"),
        choices: String(fd.get("choices") || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        required: fd.get("required") === "on",
        is_headline: fd.get("is_headline") === "on",
        max_len: maxLen,
        hint: String(fd.get("hint") || "").trim(),
      };
      if (!isNew) body.active = fd.get("active") === "on";
      try {
        if (isNew) {
          await apiPost("/api/bios/fields", body);
        } else {
          await apiPut(`/api/bios/fields/${field.id}`, body);
        }
        editor.innerHTML = "";
        await refresh();
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  await refresh();
}

// ── Questions tab ───────────────────────────────────────────────────

async function renderQuestionsTab(pane) {
  pane.innerHTML = `<div class="empty">Loading icebreakers…</div>`;
  let questions = [];

  async function refresh() {
    questions = await api("/api/bios/questions");
    render();
  }

  function render() {
    const rows = questions.map((q) => `
      <tr data-id="${q.id}">
        <td><input type="text" data-prompt aria-label="Icebreaker question" value="${esc(q.prompt)}" style="width: 100%;" /></td>
        <td><label style="display:flex;gap:6px;align-items:center;"><input type="checkbox" data-active ${q.active ? "checked" : ""} /> In use</label></td>
        <td>
          <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            <button type="button" class="btn btn-secondary" data-save>Save</button>
            <button type="button" class="btn btn-danger" data-retire>Retire</button>
            <span data-row-status></span>
          </div>
        </td>
      </tr>
    `).join("");

    pane.innerHTML = `
      <div class="subtitle" style="margin-bottom:0.5rem;">Light questions picked at random for each bio; the Settings tab sets how many. Each row saves on its own.</div>
      <form data-add-form class="form card" style="margin-bottom:1rem;">
        <div class="section-label">Add an Icebreaker</div>
        <div class="field">
          <label for="bio-q-prompt">Question</label>
          <input type="text" name="prompt" id="bio-q-prompt" placeholder="What's the last song that made you cry?" required style="width:100%;" />
          <div class="field-hint">Keep it short and easy to answer. Members see it partway through writing their bio.</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
          <button type="submit" class="btn btn-primary">Add Icebreaker</button>
          <span data-add-status></span>
        </div>
      </form>
      <div style="overflow-x:auto;">
        <table class="table">
          <thead><tr><th>Question</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="3"><em>No icebreakers yet. Add one above.</em></td></tr>`}</tbody>
        </table>
      </div>
    `;
    wire();
  }

  function wire() {
    const addForm = pane.querySelector("[data-add-form]");
    guardForm(addForm);
    guardedSubmit(addForm, async (e) => {
      const form = e.target;
      const fd = new FormData(form);
      const statusEl = pane.querySelector("[data-add-status]");
      const prompt = String(fd.get("prompt") || "").trim();
      if (!prompt) {
        showStatus(statusEl, false, "Question cannot be empty.");
        form.querySelector('[name="prompt"]').focus();
        return;
      }
      try {
        await apiPost("/api/bios/questions", { prompt });
        await refresh();
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    pane.querySelectorAll("tr[data-id]").forEach((row) => {
      const id = row.dataset.id;
      const status = row.querySelector("[data-row-status]");
      guardForm(row);
      row.querySelector("[data-save]").addEventListener("click", async () => {
        const promptEl = row.querySelector("[data-prompt]");
        const prompt = promptEl.value.trim();
        if (!prompt) {
          showStatus(status, false, "Question cannot be empty.");
          promptEl.focus();
          return;
        }
        try {
          await apiPut(`/api/bios/questions/${id}`, {
            prompt,
            active: row.querySelector("[data-active]").checked,
          });
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
      row.querySelector("[data-retire]").addEventListener("click", async () => {
        const q = questions.find((x) => String(x.id) === String(id));
        const ok = await confirmDialog(
          `Stop asking "${q ? q.prompt : "this icebreaker"}"? Bios that already answered it keep their answer, `
          + "but nobody new is asked it.",
          { title: "Retire Icebreaker", danger: true, confirmLabel: "Retire" },
        );
        if (!ok) return;
        try {
          await apiDelete(`/api/bios/questions/${id}`);
          await refresh();
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    });
  }

  await refresh();
}
