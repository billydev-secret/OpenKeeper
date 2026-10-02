// Member Data — the three GDPR paths plus Discord message deletion, on one
// page, acting on one member chosen once at the top.
//
// Every operation here used to live in `scripts/` or in `/delete_user`, which
// this panel replaces. They are gathered because they answer one question —
// "what does this server hold about this member, and what do we do about it" —
// and because running an erasure by hand from a shell on the production box is
// the worst possible place for it.
//
// Two things shape the code more than they look like they should:
//
// * **The subject is usually someone who left.** An access or erasure request
//   arrives *after* the member is gone, so the picker leans on `memberSearch`,
//   which covers departed members and matches a bare id. A picker limited to
//   the current member list would miss almost every real subject.
// * **A subject change invalidates everything on screen.** Rather than teach
//   four tabs to refresh themselves, the tab strip is rebuilt — every pane was
//   about the previous member, and a stale count beside a Purge button is the
//   one thing this page must never show.
import { api, apiPost, apiPut, esc } from "../api.js";
import { confirmDialog, toast } from "../ui.js";
import {
  loadMembers,
  memberNames,
  mountAsync,
  mountMemberPicker,
  onPickerChange,
  renderMetaWarning,
  isPrimaryGuild,
} from "../config-helpers.js";
import { mountTabs } from "../tabs.js";
import { renderSortableTable } from "../table.js";
import { syncHash } from "../report-helpers.js";

const PANEL_ID = "member-data";
const TAB_KEYS = ["export", "erase", "anonymise", "messages", "departures"];
const SNOWFLAKE_RE = /^\d{17,20}$/;

// Measured 2026-09-13 against a copy of the pre-anonymise production snapshot:
// 12.4s to rewrite 186,991 rows across 108 tables. The bot is locked out of
// writes for that whole window, so the confirm says so rather than letting an
// admin discover it as an outage.
const ANONYMISE_LOCK_HINT =
  "The bot can't write to its database while this runs, about 15 seconds for "
  + "a long-standing member.";

// booster_swatch_dir's posture (config-global): a bot-global control renders
// only on the primary guild. Anonymise rewrites the member's id in *every*
// guild the bot serves, and one of them is an economy someone else runs.

function countsTable(counts) {
  const rows = Object.entries(counts || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!rows.length) {
    return `<div class="empty">Nothing stored for this member here.</div>`;
  }
  return `
    <table class="data-table">
      <thead><tr><th>Where</th><th style="text-align:right;">Rows</th></tr></thead>
      <tbody>
        ${rows.map(([name, n]) => `
          <tr><td>${esc(name)}</td><td style="text-align:right;">${esc(String(n))}</td></tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function noteList(notes) {
  if (!notes || !notes.length) return "";
  return `<ul style="margin:0.4rem 0 0; padding-left:1.2rem;">
    ${notes.map((n) => `<li>${esc(String(n))}</li>`).join("")}
  </ul>`;
}

function needSubject(pane) {
  pane.innerHTML = `<div class="empty">Choose a member above to begin.</div>`;
}

export function mount(container, initialParams = {}) {
  let subjectId = SNOWFLAKE_RE.test(String(initialParams.user || ""))
    ? String(initialParams.user)
    : "";
  let activeKey = TAB_KEYS.includes(initialParams.tab) ? initialParams.tab : "export";
  let tabs = null;
  let picker = null;
  // Set by the Delete Messages tab while it polls; cleared on teardown so a
  // panel the admin has navigated away from stops asking.
  let pollTimer = null;

  function pushHash() {
    syncHash(PANEL_ID, { tab: activeKey, user: subjectId });
  }

  async function subjectName() {
    if (!subjectId) return "this member";
    try {
      const lookup = await memberNames([subjectId]);
      return lookup(subjectId) || `member ${subjectId}`;
    } catch (_) {
      return `member ${subjectId}`;
    }
  }

  // ── Export (Art 15 / 20) ────────────────────────────────────────────
  //
  // Counts, never row content: the payload behind them is the whole of a
  // member's data and a browser is the wrong place for it. The file arrives
  // as a download instead.
  async function renderExport(pane) {
    if (!subjectId) return needSubject(pane);
    pane.innerHTML = `<div class="empty">Reading what this server holds…</div>`;
    const data = await api(`/api/privacy/export?user_id=${encodeURIComponent(subjectId)}`);

    const review = data.review_required || [];
    pane.innerHTML = `
      <div class="card">
        <div class="section-label">What this server holds</div>
        <p class="field-hint">
          ${esc(String(data.total_rows))} rows in this server. Download is the
          file an access request is answered with.
        </p>
        ${countsTable(data.counts)}
        ${noteList(data.notes)}
      </div>
      ${review.length ? `
        <div class="save-status save-err" role="alert" style="margin-top:12px;">
          <div><strong>Read these before the file leaves the building.</strong>
          They contain another member's words as well, so someone has to decide
          what to redact before sharing.</div>
          <ul style="margin:0.4rem 0 0; padding-left:1.2rem;">
            ${review.map((t) => `<li>${esc(String(t))}</li>`).join("")}
          </ul>
        </div>
      ` : ""}
      <div style="margin-top:12px;">
        <button class="btn btn-primary" data-download>Download JSON</button>
      </div>
    `;

    pane.querySelector("[data-download]").addEventListener("click", () => {
      window.location = `/api/privacy/export/download?user_id=${encodeURIComponent(subjectId)}`;
    });
  }

  // ── Erase (Art 17) ──────────────────────────────────────────────────
  async function renderErase(pane) {
    if (!subjectId) return needSubject(pane);
    pane.innerHTML = `<div class="empty">Working out what would go…</div>`;
    const data = await api(`/api/privacy/export?user_id=${encodeURIComponent(subjectId)}`);

    pane.innerHTML = `
      <div class="card">
        <div class="section-label">What erasing would remove</div>
        <p class="field-hint">
          Includes rows an erasure <strong>keeps</strong>: the coin ledger,
          sanction history, DM consent records and no-contact orders.
        </p>
        ${countsTable(data.counts)}
      </div>
      <div class="card" style="margin-top:12px;">
        <label style="display:flex; gap:0.5rem; align-items:center;">
          <input type="checkbox" data-keep-messages>
          <span>Keep their message activity log</span>
        </label>
        <div class="field-hint">
          Keeps the record that they posted (channel, time, replies,
          reactions, mentions), never text. Unticked erases it too.
        </div>
        <div style="margin-top:12px;">
          <button class="btn btn-danger" data-erase>Erase this member's data</button>
        </div>
        <div class="save-status" data-status style="margin-top:0.5rem;"></div>
      </div>
    `;

    const statusEl = pane.querySelector("[data-status]");
    pane.querySelector("[data-erase]").addEventListener("click", async () => {
      const keepMessages = pane.querySelector("[data-keep-messages]").checked;
      const who = await subjectName();
      const ok = await confirmDialog(
        `This permanently deletes ${who}'s data from this server. It cannot be `
        + `undone, and there is no confirmation step after this one.`
        + (keepMessages ? " Their message activity log will be left in place." : ""),
        { title: "Erase member data", danger: true, confirmLabel: "Erase" },
      );
      if (!ok) return;
      try {
        const res = await apiPost("/api/privacy/purge", {
          user_id: subjectId,
          keep_messages: keepMessages,
        });
        toast(`Erased ${res.rows} records, ${res.message_rows} of them message log entries.`, "success");
        // The counts above now describe a member who no longer has them.
        openTab("erase", true);
      } catch (err) {
        statusEl.className = "save-status save-err";
        statusEl.textContent = err?.message || "Couldn't erase that member's data.";
      }
    });
  }

  // ── Anonymise ───────────────────────────────────────────────────────
  //
  // Cross-guild by design — a surrogate has to be the same everywhere or the
  // aggregates it exists to protect stop adding up — which is exactly why it
  // is offered on the primary guild only.
  async function renderAnonymise(pane) {
    if (!isPrimaryGuild()) {
      pane.innerHTML = `
        <div class="card">
          <div class="section-label">Not available on this server</div>
          <p class="field-hint">
            Anonymising rewrites a member's id in <strong>every</strong>
            server this bot is in, so it only runs from the main server.
          </p>
        </div>
      `;
      return;
    }
    if (!subjectId) return needSubject(pane);

    pane.innerHTML = `<div class="empty">Working out what would change…</div>`;
    const plan = await api(
      `/api/privacy/anonymize/plan?user_id=${encodeURIComponent(subjectId)}`,
    );

    // Nothing to rewrite. Either this member was never here, or — much more
    // likely, since the pane re-renders itself after a run — they have already
    // been anonymised and no longer exist under the id in the picker.
    const nothingToDo = !plan.total_rows;
    // total_rows sums three sweeps; the table below itemises only the first.
    // Quoting the total over that table would read as a table that does not
    // add up, so each subtotal is named.
    const sumCounts = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);
    const idRows = sumCounts(plan.id_rewrites);
    const textRows = sumCounts(plan.text_rewrites) + sumCounts(plan.text_cleared);

    pane.innerHTML = `
      <div class="card">
        <div class="section-label">What anonymising would change</div>
        ${nothingToDo ? `
          <p class="field-hint">
            There's nothing left to rewrite for this member. If you just
            anonymised them, that means it worked.
          </p>
        ` : `
          <p class="field-hint">
            Replaces their id and name with number
            <strong>${esc(String(plan.surrogate_id))}</strong> and clears their
            own text, permanently.
            ${plan.username ? `Currently stored as <strong>${esc(plan.username)}</strong>.` : ""}
          </p>
          <p class="field-hint">
            <strong>${esc(String(plan.total_rows))} rows</strong> change:
            ${esc(String(idRows))} have the member's id replaced (listed
            below)${textRows ? `, ${esc(String(textRows))} have text rewritten
            or cleared` : ""}.
          </p>
          ${countsTable(plan.id_rewrites)}
        `}
      </div>
      ${nothingToDo ? "" : `
        <div class="card" style="margin-top:12px;">
          <button class="btn btn-danger" data-anonymise>Anonymise this member</button>
          <div class="save-status" data-status style="margin-top:0.5rem;"></div>
        </div>
      `}
      <div class="card" style="margin-top:12px;">
        <div class="section-label">Clear missed text</div>
        <p class="field-hint">
          Run after anonymising a member to re-check the text. Enter the
          replacement number, not a member id.
        </p>
        <div class="field">
          <label for="dk-sweep-surrogate">Replacement number</label>
          <input type="number" id="dk-sweep-surrogate" data-surrogate min="1" step="1"
            style="width:160px;">
        </div>
        <div style="margin-top:12px; display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button class="btn" data-sweep-preview>Preview</button>
          <button class="btn btn-danger" data-sweep-apply>Clear it</button>
        </div>
        <div class="save-status" data-sweep-status style="margin-top:0.5rem;"></div>
        <div data-sweep-result style="margin-top:12px;"></div>
      </div>
    `;

    const statusEl = pane.querySelector("[data-status]");
    pane.querySelector("[data-anonymise]")?.addEventListener("click", async () => {
      const who = await subjectName();
      const ok = await confirmDialog(
        `This replaces ${who}'s id and name with the number ${plan.surrogate_id} `
        + `across every server this bot is in, and clears the text they wrote. `
        + `It cannot be undone. ${ANONYMISE_LOCK_HINT}`,
        { title: "Anonymise member", danger: true, confirmLabel: "Anonymise" },
      );
      if (!ok) return;
      try {
        const res = await apiPost("/api/privacy/anonymize", {
          user_id: subjectId,
          surrogate_id: plan.surrogate_id,
        });
        toast(`Anonymised ${res.total_rows} rows as number ${res.surrogate_id}.`, "success");
        // Re-plan: the member no longer exists under the id in the picker, so
        // the pane must stop offering to anonymise them again.
        openTab("anonymise", true);
      } catch (err) {
        statusEl.className = "save-status save-err";
        statusEl.textContent = err?.message || "Couldn't anonymise that member.";
      }
    });

    const sweepStatus = pane.querySelector("[data-sweep-status]");
    const sweepResult = pane.querySelector("[data-sweep-result]");
    const sweepInput = pane.querySelector("[data-surrogate]");

    async function runSweep(dryRun) {
      const surrogate = parseInt(sweepInput.value, 10);
      if (!Number.isInteger(surrogate) || surrogate < 1) {
        sweepStatus.className = "save-status save-err";
        sweepStatus.textContent = "Enter the replacement number first.";
        return;
      }
      if (!dryRun) {
        const ok = await confirmDialog(
          `This permanently clears text belonging to replacement number `
          + `${surrogate}. Preview it first if you haven't. It cannot be undone.`,
          { title: "Clear missed text", danger: true, confirmLabel: "Clear it" },
        );
        if (!ok) return;
      }
      sweepStatus.className = "save-status";
      sweepStatus.textContent = dryRun ? "Checking…" : "Clearing…";
      try {
        const res = await apiPost(
          `/api/privacy/anonymize/sweep?dry_run=${dryRun ? "true" : "false"}`,
          { surrogate_id: surrogate },
        );
        sweepStatus.className = "save-status save-ok";
        sweepStatus.textContent = dryRun
          ? `${res.total_rows} rows would be cleared.`
          : `Cleared ${res.total_rows} rows.`;
        sweepResult.innerHTML = countsTable(res.text_cleared);
      } catch (err) {
        sweepStatus.className = "save-status save-err";
        sweepStatus.textContent = err?.message || "Couldn't check for missed text.";
        sweepResult.innerHTML = "";
      }
    }

    pane.querySelector("[data-sweep-preview]").addEventListener("click", () => runSweep(true));
    pane.querySelector("[data-sweep-apply]").addEventListener("click", () => runSweep(false));
  }

  // ── Discord messages ────────────────────────────────────────────────
  //
  // The one operation here that touches no stored data at all: it deletes the
  // member's messages from Discord itself. A full-server scan runs for
  // minutes, so the bot runs it as a background job and this polls.
  async function renderMessages(pane) {
    if (!subjectId) return needSubject(pane);

    pane.innerHTML = `
      <div class="card">
        <div class="section-label">Delete their Discord messages</div>
        <p class="field-hint">
          Deletes their Discord messages only; the bot's records stay (use
          Erase). Scanning every channel takes minutes.
        </p>
        <div class="field">
          <label for="dk-del-mode">What to delete</label>
          <select id="dk-del-mode" data-mode style="width:220px;">
            <option value="all">Everything they posted</option>
            <option value="media">Images and files only</option>
            <option value="text">Text posts only</option>
          </select>
        </div>
        <div style="margin-top:12px;">
          <button class="btn btn-danger" data-start>Start deleting</button>
        </div>
        <div class="save-status" data-status style="margin-top:0.5rem;"></div>
        <div data-progress style="margin-top:12px;"></div>
      </div>
    `;

    const statusEl = pane.querySelector("[data-status]");
    const progressEl = pane.querySelector("[data-progress]");
    const startBtn = pane.querySelector("[data-start]");

    function renderProgress(s) {
      if (!s || s.phase === "idle") { progressEl.innerHTML = ""; return; }
      if (s.phase === "scanning") {
        progressEl.innerHTML = `<div class="empty">Looking through the server:
          ${esc(String(s.done))} of ${esc(String(s.total))} channels,
          ${esc(String(s.found))} messages found so far.</div>`;
      } else if (s.phase === "deleting") {
        progressEl.innerHTML = `<div class="empty">Deleting:
          ${esc(String(s.done))} of ${esc(String(s.total))}.</div>`;
      } else if (s.phase === "done") {
        progressEl.innerHTML = `<div class="save-status save-ok">${esc(String(s.summary || "Finished."))}</div>`;
      } else if (s.phase === "failed") {
        progressEl.innerHTML = `<div class="save-status save-err">That run stopped before it finished. Nothing else was deleted.</div>`;
      }
    }

    function stopPolling() {
      clearTimeout(pollTimer);
      pollTimer = null;
    }

    // Re-armed after each reply rather than on a fixed interval, so a slow or
    // failed status call can never stack requests on a busy bot.
    async function poll() {
      let s;
      try {
        s = await api(`/api/privacy/messages/status?user_id=${encodeURIComponent(subjectId)}`);
      } catch (_) {
        // A dropped status call is not a failed run — keep asking.
        pollTimer = setTimeout(poll, 3000);
        return;
      }
      renderProgress(s);
      if (s.phase === "scanning" || s.phase === "deleting") {
        pollTimer = setTimeout(poll, 2000);
      } else {
        stopPolling();
        startBtn.disabled = false;
      }
    }

    startBtn.addEventListener("click", async () => {
      const mode = pane.querySelector("[data-mode]").value;
      const who = await subjectName();
      const ok = await confirmDialog(
        `This deletes ${who}'s messages from this server's channels. Discord `
        + `does not keep a copy, so they cannot be brought back.`,
        { title: "Delete Discord messages", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      statusEl.textContent = "";
      try {
        await apiPost("/api/privacy/messages/delete", { user_id: subjectId, mode });
        startBtn.disabled = true;
        stopPolling();
        poll();
      } catch (err) {
        statusEl.className = "save-status save-err";
        statusEl.textContent = err?.message || "Couldn't start deleting.";
      }
    });

    // A run started before this page was opened — or before a reload — is
    // still going on the bot; show it rather than offering to start a second.
    const existing = await api(
      `/api/privacy/messages/status?user_id=${encodeURIComponent(subjectId)}`,
    );
    renderProgress(existing);
    if (existing.phase === "scanning" || existing.phase === "deleting") {
      startBtn.disabled = true;
      pollTimer = setTimeout(poll, 2000);
    }

    return { unmount: stopPolling };
  }

  // ── When members leave ──────────────────────────────────────────────
  //
  // Not about the member in the picker: this server's switch for the automatic
  // departure clean-up, and the queue of members it is about to reach. It is
  // on this page because it runs the same three tools above, unasked.
  async function renderDepartures(pane) {
    pane.innerHTML = `<div class="empty">Loading…</div>`;
    const data = await api("/api/privacy/departures");
    const jobs = data.jobs || [];
    let nameOf = () => "";
    if (jobs.length) {
      try {
        nameOf = await memberNames(jobs.map((j) => j.user_id));
      } catch (_) { /* fall back to ids */ }
    }
    const minutes = Math.round((data.settle_seconds || 0) / 60);
    const scopeHint = data.primary
      ? "A member still in another of the bot's servers is anonymised here only."
      : "Only this server's records are anonymised.";
    const when = (ts) => new Date(ts * 1000).toLocaleString();
    // Both member-controlled strings on this table — a display name and the
    // error text off a failed job — are plain columns, so table.js escapes
    // them; the status is built here as text rather than markup for the same
    // reason.
    const rows = jobs.map((j) => ({
      ...j,
      member: nameOf(j.user_id) || j.user_id,
      status: j.state === "failed"
        ? `Stopped after ${j.attempts} tries: ${j.last_error}`
        : j.attempts
          ? `Retrying (${j.attempts} of ${data.max_attempts}): ${j.last_error}`
          : `Runs after ${when(j.next_at)}`,
    }));

    pane.innerHTML = `
      <div class="card">
        <div class="section-label">Clean up after members who leave</div>
        <label style="display:flex; gap:0.5rem; align-items:center;">
          <input type="checkbox" data-dial ${data.enabled ? "checked" : ""}>
          <span>Anonymise members automatically when they leave</span>
        </label>
        <p class="field-hint">
          <strong>Can't be undone.</strong> About ${esc(String(minutes))}
          minutes after someone leaves, their messages and Guess Who photos go
          and they're anonymised.
        </p>
        <p class="field-hint">${esc(scopeHint)}</p>
        <p class="field-hint">
          Jailed members and anyone on a ticket are skipped. Rejoining before it
          runs cancels it.
        </p>
        <div style="margin-top:12px;">
          <button class="btn btn-primary" data-save-dial>Save</button>
        </div>
        <div class="save-status" data-dial-status style="margin-top:0.5rem;"></div>
      </div>
      <div class="card" style="margin-top:12px;">
        <div class="section-label">Waiting</div>
        <div data-queue-table style="overflow-x:auto;"></div>
        <div class="save-status" data-queue-status style="margin-top:0.5rem;"></div>
      </div>
    `;

    renderSortableTable(pane.querySelector("[data-queue-table]"), {
      columns: [
        { key: "member", label: "Member" },
        { key: "left_at", label: "Left", format: (v) => when(v) },
        { key: "status", label: "Status" },
        // The only markup column, and it interpolates nothing but the
        // server-issued id it escapes itself.
        { key: "user_id", label: "", cls: "nowrap", html: true,
          format: (v, row) =>
            (row.state === "failed"
              ? `<button type="button" class="btn btn-sm" data-retry="${esc(String(v))}">Retry</button> `
              : "")
            + `<button type="button" class="btn btn-sm" data-drop="${esc(String(v))}">Leave alone</button>` },
      ],
      data: rows,
      defaultSort: "left_at",
      defaultAsc: true,
      emptyMsg: "Nobody is waiting.",
    });

    const dialStatus = pane.querySelector("[data-dial-status]");
    pane.querySelector("[data-save-dial]").addEventListener("click", async () => {
      const enabled = pane.querySelector("[data-dial]").checked;
      if (enabled === data.enabled) {
        dialStatus.className = "save-status";
        dialStatus.textContent = "Nothing to save.";
        return;
      }
      const ok = await confirmDialog(
        enabled
          ? "From now on, everyone who leaves this server (unless jailed or on a "
            + "ticket) has their messages deleted and is anonymised a few minutes "
            + "later. It cannot be undone for them. Members who already left are "
            + "not affected."
          : "Turn it off? Anyone still waiting on the list is left alone.",
        { title: "When members leave", danger: enabled, confirmLabel: enabled ? "Turn on" : "Turn off" },
      );
      if (!ok) return;
      try {
        await apiPut("/api/privacy/departures", { enabled });
        toast(enabled ? "Members who leave will be anonymised." : "Turned off.", "success");
        openTab("departures", true);
      } catch (err) {
        dialStatus.className = "save-status save-err";
        dialStatus.textContent = err?.message || "Couldn't save that.";
      }
    });

    const queueStatus = pane.querySelector("[data-queue-status]");
    async function act(path, userId, done) {
      try {
        await apiPost(path, { user_id: userId });
        toast(done, "success");
        openTab("departures", true);
      } catch (err) {
        queueStatus.className = "save-status save-err";
        queueStatus.textContent = err?.message || "Couldn't do that.";
      }
    }
    // Delegated: table.js rebuilds the table on every sort, so a listener bound
    // to a button would stop working the first time a header is clicked.
    pane.addEventListener("click", (e) => {
      const retry = e.target.closest("[data-retry]");
      const drop = e.target.closest("[data-drop]");
      if (retry) act("/api/privacy/departures/retry", retry.dataset.retry, "Put back in line.");
      else if (drop) act("/api/privacy/departures/drop", drop.dataset.drop, "Taken off the list.");
    });
  }

  const TABS = [
    { key: "export", label: "Export", render: renderExport,
      errorMsg: "Couldn't read what this server holds for that member." },
    { key: "erase", label: "Erase", render: renderErase,
      errorMsg: "Couldn't work out what erasing would remove." },
    { key: "anonymise", label: "Anonymise", render: renderAnonymise,
      errorMsg: "Couldn't work out what anonymising would change." },
    { key: "messages", label: "Discord Messages", render: renderMessages,
      errorMsg: "Couldn't reach the bot to check for a running deletion." },
    { key: "departures", label: "When Members Leave", render: renderDepartures,
      errorMsg: "Couldn't load the settings for members who leave." },
  ];

  let tabsEl = null;

  function buildTabs() {
    clearTimeout(pollTimer);
    pollTimer = null;
    tabs?.unmount();
    tabs = mountTabs(tabsEl, TABS, {
      ariaLabel: "Member data tools",
      initial: activeKey,
      onShow: (key) => { activeKey = key; pushHash(); },
    });
  }

  // Force one tab to re-render. mountTabs remembers which panes it has already
  // loaded, so showTab alone won't redraw the one already on screen.
  function openTab(key, force) {
    if (force && activeKey === key) buildTabs();
    else tabs?.showTab(key);
  }

  return mountAsync(container, async () => {
    const members = await loadMembers();

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Member Data</h2>
          <div class="subtitle">Answer an access request, erase a member's data, or clear their messages</div>
        </header>
        ${renderMetaWarning()}
        <div class="card">
          <div class="field">
            <label>Member</label>
            <span data-subject-slot></span>
          </div>
          <div class="field-hint">
            Search by name or paste their id. Members who have left show up too.
          </div>
        </div>
        <div data-tabs style="margin-top:12px;"></div>
      </div>
    `;

    tabsEl = container.querySelector("[data-tabs]");
    picker = mountMemberPicker(
      container.querySelector("[data-subject-slot]"), members, subjectId,
      { label: "Member", placeholder: "Search or paste an id…" },
    );
    onPickerChange(picker, () => {
      const val = picker.getValue();
      subjectId = SNOWFLAKE_RE.test(String(val || "")) ? String(val) : "";
      pushHash();
      // Everything on screen was about the previous member.
      buildTabs();
    });

    buildTabs();
    pushHash();

    return {
      unmount() {
        clearTimeout(pollTimer);
        pollTimer = null;
        tabs?.unmount();
        picker?.destroy();
      },
    };
  }, { errorMsg: "Couldn't load the member data tools." });
}
