// Economy — Quests. The quest library, the authoring form (plus the AI idea
// generator), and the progress/settle controls for community goals — which
// are quests (`qtype = 'community'`) and belong with the rest of them rather
// than on the bank page they used to sit on. The remaining operational work
// — claim sign-off, grants, the ledger — lives on the Operations page. Gated
// by the economy manager role (or admin).
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import { showStatus, guardForm, loadChannels, mountChannelPicker, mountAsync, guardedSubmit } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { mountTabs } from "../tabs.js";
import { KIND_LABELS, CHANNEL_SCOPED_KINDS } from "./economy-sources-shared.js";

// Advisory reward bands (client-side hint only — the server saves any value).
const REWARD_BANDS = { daily: [10, 20], weekly: [25, 75], monthly: [50, 90] };

// Plain-language cadence per quest type (shown under the Type select).
const TYPE_HINTS = {
  daily: "Pays once per member per day, resetting at server midnight. Board Size sets how many dailies each member sees.",
  weekly: "Runs across the week, Monday to Sunday. With a game trigger, How Many Times must be 2 or more.",
  monthly: "A shared goal for the calendar month. It needs a game trigger, sizes itself and pays 40/70/100% tiers at month end.",
  community: "One shared goal for the server. Track it by hand, or add a game trigger and it runs itself in 40/70/100% tiers.",
  event: "Pays every time the trigger happens, with no claims or cap. One active event quest per trigger.",
};

// The per-member board dials. Each entry: [settings key, cadence, label].
// Personal-board size dials. Monthly left this set when it became a guild-wide
// community-measured goal (no personal board) — only daily/weekly draw one.
const BOARD_FIELDS = [
  ["quest_board_daily", "daily", "Daily"],
  ["quest_board_weekly", "weekly", "Weekly"],
];

// Econ config for the board dials, or null for manager-role holders (the
// config GET is admin-gated). Module-scoped because the library summary
// renders "pool → shown" on every refreshQuests(), and a board save has to
// move those numbers without a reload.
let boardCfg = null;

function bandHint(qtype, reward) {
  const band = REWARD_BANDS[qtype];
  if (!band || reward === "" || reward == null) return "";
  const n = Number(reward);
  if (!Number.isFinite(n) || (n >= band[0] && n <= band[1])) return "";
  return `Outside the suggested ${qtype} band (${band[0]}–${band[1]}). Saves fine.`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading quests…</div></div>`;
  return mountAsync(container, async () => {
    // All three loads are independent — fetch them together rather than one
    // after another (W-D11). The quest list is handed straight to the first
    // render so the library doesn't flash "Loading…" a second time.
    const [channelsResult, cfgResult, questsResult] = await Promise.allSettled([
      loadChannels(),
      // Admin probe, same as Income Sources: the config GET is admin-gated, so
      // a success means this user may edit the board sizes. Manager-role
      // holders get a 403 and the read-only view.
      api("/api/economy/config"),
      api("/api/economy/quests"),
    ]);
    const channels = channelsResult.status === "fulfilled" ? channelsResult.value : [];
    boardCfg = cfgResult.status === "fulfilled" ? cfgResult.value : null;
    const economyOff = boardCfg != null && !boardCfg.enabled;
    const prefetched = questsResult.status === "fulfilled" ? questsResult.value.quests : null;
    return render(container, channels, boardCfg, economyOff, prefetched);
  }, { errorMsg: "Couldn’t load the quest settings." });
}

function boardSection(cfg) {
  if (!cfg) {
    // Manager-role view: the config GET is admin-gated, so we have no values
    // to show — describe the dial rather than printing a row of em-dashes.
    return `
      <div class="field-hint">
        Each member sees a few quests of each kind from its pool. Only admins
        can change how many.
      </div>`;
  }
  const fields = BOARD_FIELDS.map(([key, , label]) => `
    <div class="field">
      <label for="q-${key}">${label} Quests Shown</label>
      <input type="number" name="${key}" id="q-${key}" required value="${cfg[key]}"
             min="0" max="25" step="1" style="max-width:90px;" />
    </div>`).join("");
  return `
    <div class="field-hint" style="margin-bottom:8px;">
      How many quests of each kind a member sees.
      <strong>0 switches that kind off</strong>; a change waits for the next roll.
    </div>
    <form data-form-board class="form">
      <div class="field-row">${fields}</div>
      <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
        <button type="submit" class="btn btn-primary">Save</button>
        <span data-status-board></span>
      </div>
    </form>`;
}

// The page used to be one long scroll of four sections back to back; Billy
// asked for tabs to break it up. Library (browse/manage, with the Community
// Goals progress card nested underneath — it's progress on quests from that
// same library) is its own tab, Board Size and the New Quest / Edit form get
// one each. All three tabs' data was already fetched together in mount()
// above, so — unlike the lazy-fetch-per-tab pattern this helper is built
// for (see tabs.js's header comment and config-bios.js) — every tab is
// wired up immediately below rather than only on first click: nothing here
// is fetched over the network per tab, and the Library tab's "Edit" button
// dispatches a dk-edit-quest event the New Quest form must already be
// listening for, however the admin happens to be clicking around.
function render(container, channels, cfg, economyOff, prefetchedQuests) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Quests</h2>
        <div class="subtitle">Write and manage the quests members work through, community
          goals included. Approving finished quests happens on Claims.</div>
      </header>
      ${economyOff ? `<div class="empty" role="status" style="margin-bottom:12px;">
        The economy is off, so nothing below takes effect until you turn it on under
        <a href="#/economy-config">Economy Settings</a>.</div>` : ""}
      <div data-tabs></div>
    </div>`;

  function renderLibraryTab(pane) {
    pane.innerHTML = `
      <section class="card" data-sec="library">
        <div class="section-label">Quest Library</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Automatic payments such as logins and game wins are set on
          <a href="#/economy-income-sources">Income Sources</a>. Sign-offs are on
          <a href="#/economy-claims">Claims</a>.
        </div>
        <div class="field-hint" style="margin-bottom:8px;">
          🔁 <strong>New quests start switched off.</strong> One you switch on
          joins boards at the next roll; one you switch off leaves at once.
        </div>
        <div data-quest-slots class="field-hint" style="margin-bottom:6px;"></div>
        <div data-quests><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="community" style="display:none; margin-top:12px;">
        <div class="section-label">Community Goals</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Progress and payout for community goals. Settling pays every active
          member once; payouts show in the ledger on
          <a href="#/economy-bank-manager">Bank</a>.
        </div>
        <div data-community></div>
      </section>
    `;
    if (prefetchedQuests) {
      renderQuestList(container, prefetchedQuests);
    } else {
      refreshQuests(container);
    }
  }

  function renderBoardTab(pane) {
    pane.innerHTML = `
      <section class="card" data-sec="board">
        <div class="section-label">Board Size</div>
        ${boardSection(cfg)}
      </section>
    `;
    wireBoard(container);
  }

  function renderAuthorTab(pane) {
    pane.innerHTML = `
      <section class="card" data-sec="author">
        <div class="section-label" data-author-label>New Quest</div>
        <form data-form-quest class="form">
          <div class="field-row">
            <div class="field"><label for="q-title">Title</label>
              <input type="text" name="title" id="q-title" maxlength="256" required />
              <div class="field-hint">What members see at the top of the quest card.</div></div>
            <div class="field"><label for="q-type">Type</label>
              <select name="qtype" id="q-type">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="community">Community goal</option>
                <option value="event">Event (every time it happens)</option>
              </select>
              <div class="field-hint" data-type-hint style="max-width:420px;"></div></div>
          </div>
          <div class="field"><label for="q-description">Description</label>
            <textarea name="description" id="q-description" maxlength="2000" rows="2"></textarea>
            <div class="field-hint">A sentence or two of flavor, shown under the title.</div></div>
          <div class="field"><label for="q-criteria">What Counts as Done</label>
            <textarea name="criteria" id="q-criteria" maxlength="2000" rows="2"></textarea>
            <div class="field-hint">What the approving manager sees on the sign-off
              card. Members don't see it.</div></div>
          <div class="field-row">
            <div class="field"><label for="q-reward">Reward (coins)</label>
              <input type="number" name="reward" id="q-reward" required min="0" max="1000000" step="1" value="10" style="max-width:120px;" />
              <div class="field-hint" data-reward-hint style="color:var(--gold-solid);"></div></div>
            <div class="field"><label for="q-reward-xp">Bonus XP</label>
              <input type="number" name="reward_xp" id="q-reward-xp" required min="0" max="1000000" step="1" value="0" style="max-width:120px;" />
              <div class="field-hint">Leveling XP paid alongside the coins. The booster
                multiplier does not apply to XP.</div></div>
            <div class="field" data-community-target style="display:none;"><label for="q-community-target">Community Target</label>
              <input type="number" name="community_target" id="q-community-target" min="0" max="100000000" step="1" style="max-width:120px;" />
              <div class="field-hint">How far the whole server has to get before the goal
                pays out.</div></div>
            <div class="field" data-community-auto style="display:none;"><label>Community Target</label>
              <div class="field-hint">Sized automatically when the run starts, from recent
                server activity. Nothing to enter here.</div></div>
            <div class="field" data-rotate-field><label for="q-rotate">Rotation Tag</label>
              <input type="text" name="rotate_tag" id="q-rotate" maxlength="64" style="max-width:160px;" />
              <div class="field-hint">Optional. With one quest of a tag switched on, the
                next quest with that tag takes over at each daily roll.</div></div>
            <div class="field"><label for="q-pair">Pair Tag</label>
              <input type="text" name="pair_tag" id="q-pair" maxlength="64" style="max-width:160px;" />
              <div class="field-hint">Optional. Exactly two quests of one kind sharing a tag
                are drawn together, like &ldquo;host a game&rdquo; and &ldquo;play a game&rdquo;.</div></div>
          </div>
          <div class="field-row">
            <div class="field"><label for="q-starts">Starts (optional)</label>
              <input type="datetime-local" name="starts_at" id="q-starts" />
              <div class="field-hint">Daily, weekly and event quests only. Payouts wait
                until this time. Empty pays straight away.</div></div>
            <div class="field"><label for="q-ends">Ends (optional)</label>
              <input type="datetime-local" name="ends_at" id="q-ends" />
              <div class="field-hint">Daily, weekly and event quests only. Payouts stop
                after this time. Empty means no end date.</div></div>
          </div>

          <div class="field" data-completion-block>
            <label>How It Completes</label>
            <div style="display:flex; gap:14px; flex-wrap:wrap; margin:4px 0;">
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="manual" checked /> Member claims it</label>
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="phrase" /> Saying a phrase</label>
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="game" /> Playing a game</label>
            </div>
            <div class="field-hint" data-completion-hint></div>
          </div>
          <div class="field" data-trigger-words style="display:none;"><label for="q-words">Phrases That Count</label>
            <textarea name="trigger_words" id="q-words" maxlength="1000" rows="2" placeholder="e.g. good morning, gm"></textarea>
            <div class="field-hint">Separate phrases with commas or new lines. Matching
              ignores case and needs whole words.</div></div>
          <div class="field" data-trigger-channel style="display:none;"><label>Only in This Channel</label>
            <span data-picker="trigger-channel"></span>
            <div class="field-hint">Only posts in this channel or its threads count.
              &ldquo;(any channel)&rdquo; counts the whole server.</div></div>
          <div class="field" data-trigger-kind style="display:none;"><label for="q-kind">Which Event</label>
            <select name="trigger_kind" id="q-kind">${Object.entries(KIND_LABELS).map(([k, v]) =>
              `<option value="${k}">${esc(v)}</option>`).join("")}
            </select>
            <div class="field-hint" data-kind-hint></div></div>
          <div class="field" data-target-count style="display:none;"><label for="q-target">How Many Times</label>
            <input type="number" name="target_count" id="q-target" min="1" max="10000" step="1" value="1" style="max-width:110px;" />
            <div class="field-hint">On a daily, 1 finishes it the first time. Higher counts
              up with a progress bar; <strong>weekly quests need 2 or more</strong>.</div>
            <div class="field-hint" data-target-hint style="color:var(--yellow-text);"></div></div>

          <div class="field">
            <label style="display:flex; gap:6px; align-items:center;">
              <input type="checkbox" name="signoff" /> Have a manager approve it before paying
            </label>
            <div class="field-hint">Holds the payout on
              <a href="#/economy-claims">Claims</a> until a manager approves. Use it when
              the bot can't verify the quest.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary" data-submit-quest>Create Quest</button>
            <button type="button" class="btn" data-cancel-edit style="display:none;">Cancel</button>
            <span data-status-quest></span>
          </div>
        </form>
      </section>
    `;
    wireAuthoring(container, channels, () => tabsHandle.showTab("author"));
  }

  const tabsHandle = mountTabs(container.querySelector("[data-tabs]"), [
    { key: "library", label: "Quest Library", render: renderLibraryTab,
      errorMsg: "Couldn’t load the quest library." },
    { key: "board", label: "Board Size", render: renderBoardTab,
      errorMsg: "Couldn’t load the board settings." },
    { key: "author", label: "New Quest", render: renderAuthorTab,
      errorMsg: "Couldn’t load the quest form." },
  ], { ariaLabel: "Quest settings sections" });

  // Warm the New Quest tab right away (see the comment above this function) —
  // land back on Library, mountTabs' own default first tab, once it's done.
  tabsHandle.showTab("author");
  tabsHandle.showTab("library");

  return tabsHandle;
}

// ── board size ───────────────────────────────────────────────────────

function wireBoard(container) {
  const form = container.querySelector("[data-form-board]");
  if (!form) return; // manager-role view: read-only, nothing to wire
  guardForm(form);
  guardedSubmit(form, async () => {
    const status = form.querySelector("[data-status-board]");
    const payload = {};
    for (const [key, , label] of BOARD_FIELDS) {
      const input = form.querySelector(`[name="${key}"]`);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < 0 || n > 25) {
        showStatus(status, false, `${label} Quests Shown must be a number from 0 to 25`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    try {
      await apiPut("/api/economy/config", payload);
    } catch (err) {
      // showStatus takes (el, ok, msg) — the arguments used to be reversed
      // here, so a failed save rendered a blank success line.
      showStatus(status, false, err.message);
      return;
    }
    // Keep the cached config in step so the library summary's "→ N shown"
    // reflects the save without a reload.
    boardCfg = { ...boardCfg, ...payload };
    showStatus(status, true);
    refreshQuests(container);
  });
}

// ── community goals ──────────────────────────────────────────────────

// Progress + settlement for `qtype = 'community'` quests. This card used to
// live on the bank page; it fetches nothing of its own, taking the quest list
// the library already loaded.
function renderCommunity(container, quests) {
  const sec = container.querySelector("[data-sec='community']");
  const host = sec.querySelector("[data-community]");
  const community = quests.filter((q) => q.qtype === "community");
  // The whole card hides when there are no community goals — an empty
  // placeholder card was just noise.
  sec.style.display = community.length ? "" : "none";
  if (!community.length) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = community.map((q) => {
    const stateBits = `${q.community_current || 0} / ${q.community_target ?? "—"} ${q.community_completed_at ? "· completed" : ""} ${q.community_settled_at ? "· settled" : ""}`;
    // Auto-tracking weeklies (trigger_kind set) run themselves: member
    // activity moves the counter and the scheduler pays the tiers — the
    // manual controls would 422, so they aren't rendered.
    const controls = q.trigger_kind
      ? `<div class="field-hint">⚙️ Auto-tracked (${esc(q.trigger_kind)}): activity moves the counter and the scheduler settles the tiers${q.active ? "" : " · waiting in rotation"}</div>`
      : `<div class="field-row" style="align-items:flex-end;">
        <div class="field"><label>Set progress</label>
          <input type="number" min="0" step="1" aria-label="Set progress" data-cprogress="${q.id}" value="${q.community_current || 0}" style="max-width:120px;" /></div>
        <div class="field"><button class="btn" data-cprogress-save="${q.id}">Save</button></div>
        <div class="field"><button class="btn btn-primary" data-csettle="${q.id}">Settle Payout</button></div>
        <span class="save-status" data-cstatus="${q.id}"></span>
      </div>`;
    return `
    <div class="community-goal" data-cgoal="${q.id}" style="margin:10px 0; padding:8px 0; border-top:1px solid var(--rule);">
      <strong>${esc(q.title)}</strong>
      <div class="field-hint">${stateBits}</div>
      ${controls}
    </div>`;
  }).join("");

  host.querySelectorAll("[data-cprogress-save]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.cprogressSave;
      const val = parseInt(host.querySelector(`[data-cprogress="${id}"]`).value, 10) || 0;
      const status = host.querySelector(`[data-cstatus="${id}"]`);
      try {
        await apiPost(`/api/economy/quests/${id}/progress`, { current: val });
        showStatus(status, true);
        refreshQuests(container);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-csettle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.csettle;
      const status = host.querySelector(`[data-cstatus="${id}"]`);
      if (!(await confirmDialog("Settle this community quest now? Every active member is paid once; re-settling only pays members missed earlier.", { confirmLabel: "Settle" }))) return;
      try {
        const res = await apiPost(`/api/economy/quests/${id}/settle`, {});
        showStatus(status, true, `Paid ${res.paid_count}`);
        toast(`Paid ${res.paid_count} member(s)`, "success");
        // The ledger this writes to lives on Operations, so there is nothing
        // else on this page to refresh beyond the goal's own state.
        refreshQuests(container);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}


// ── quest library ────────────────────────────────────────────────────

function questVerification(q) {
  const pay = q.signoff ? "sign-off" : "instant";
  if (q.trigger_kind && KIND_LABELS[q.trigger_kind]) {
    const times = (q.target_count || 1) > 1 ? ` ×${q.target_count}` : "";
    return `<span title="${esc(KIND_LABELS[q.trigger_kind])}">${esc(KIND_LABELS[q.trigger_kind].split(" ")[0])} game trigger${times}</span> · ${pay}`;
  }
  if (q.trigger_words) {
    return `<span title="${esc(q.trigger_words)}">🗣️ phrase</span> · ${pay}`;
  }
  if (q.qtype === "community") return `manager settles · ${pay}`;
  return `member claims · ${pay}`;
}

// "pool → what one member actually sees" per cadence. The old version quoted
// slot caps of 1/5/5 that no longer exist (daily is uncapped outright, weekly
// stops at POOL_CAP); pool-vs-board is the number that actually shapes the
// experience.
function renderSlotSummary(container, quests) {
  const host = container.querySelector("[data-quest-slots]");
  if (!host) return;
  const active = quests.filter((q) => q.active);
  const parts = BOARD_FIELDS.map(([key, qtype, label]) => {
    const pool = active.filter((q) => q.qtype === qtype).length;
    if (!boardCfg) return `${label.toLowerCase()} ${pool} active`;
    if (boardCfg[key] === 0) return `${label.toLowerCase()} ${pool} active (off)`;
    // A board bigger than the pool just means "the whole pool" — show what a
    // member actually sees, not the dial.
    return `${label.toLowerCase()} ${pool} active → ${Math.min(boardCfg[key], pool)} shown`;
  });
  const kinds = active
    .filter((q) => q.qtype === "event" && q.trigger_kind)
    .map((q) => (KIND_LABELS[q.trigger_kind] || q.trigger_kind).split(" ")[0]);
  parts.push(`event ${kinds.length ? kinds.join(" ") : "none"}`);
  host.textContent = `Pool: ${parts.join(" · ")}`;
}

async function refreshQuests(container) {
  const host = container.querySelector("[data-quests]");
  let quests;
  try {
    quests = (await api("/api/economy/quests")).quests;
  } catch (err) {
    host.innerHTML = `<div class="error">The quest library failed to load: ${esc(err.message)}</div>`;
    return;
  }
  renderQuestList(container, quests);
}

// Split out of refreshQuests so the first paint can use the list fetched in
// parallel at mount instead of firing a second request.
function renderQuestList(container, quests) {
  const host = container.querySelector("[data-quests]");
  renderSlotSummary(container, quests);
  // Community goals are rows in this same list, so they render off this fetch
  // rather than a second one — and any edit that refreshes the library moves
  // the goal card with it. Called before the empty-list return below: an empty
  // library still has to hide the card.
  renderCommunity(container, quests);
  if (!quests.length) {
    host.innerHTML = `<div class="empty">No quests yet. Write your first one in the form below.</div>`;
    return;
  }
  const rows = quests.map((q) => {
    const status = `dk-quest-status-${q.id}`;
    return `
      <tr data-quest-row="${q.id}">
        <td>${esc(q.title)}</td>
        <td>${esc(q.qtype)}</td>
        <td>${q.reward}${q.reward_xp > 0 ? ` <span class="field-hint" title="Bonus XP">+${q.reward_xp}xp</span>` : ""}</td>
        <td>${questVerification(q)}</td>
        <td>
          <label style="display:inline-flex; gap:4px; align-items:center;">
            <input type="checkbox" data-active-toggle="${q.id}"${q.active ? " checked" : ""} /> In rotation
          </label>
          <span id="${status}" class="save-status" style="margin-left:6px;"></span>
        </td>
        <td>
          <button class="btn btn-ghost btn-sm" data-edit-quest="${q.id}">Edit</button>
          <button class="btn btn-ghost btn-sm" data-del-quest="${q.id}">Delete</button>
        </td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Title</th><th>Type</th><th>Reward</th><th>How It Completes</th><th>In Rotation</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-edit-quest]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const quest = quests.find((q) => String(q.id) === String(btn.dataset.editQuest));
      if (quest) container.dispatchEvent(new CustomEvent("dk-edit-quest", { detail: quest }));
    });
  });

  host.querySelectorAll("[data-active-toggle]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.activeToggle;
      const status = host.querySelector(`#dk-quest-status-${id}`);
      try {
        await apiPost(`/api/economy/quests/${id}/active`, { active: cb.checked });
        showStatus(status, true);
        refreshQuests(container);
      } catch (err) {
        cb.checked = !cb.checked; // revert
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-del-quest]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.delQuest;
      const ok = await confirmDialog(
        "The quest is removed for good, along with any turned-down or expired claims "
        + "against it. This cannot be undone. To retire a quest while keeping its "
        + "history, clear \"In rotation\" instead.",
        { title: "Delete this quest?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/economy/quests/${id}`);
        toast("Quest deleted", "success");
        refreshQuests(container);
      } catch (err) {
        toast(err.message, "error"); // 409 when paid claims exist
      }
    });
  });
}

// ── authoring form ───────────────────────────────────────────────────

function toEpoch(v) {
  if (!v) return null;
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms / 1000;
}

function fromEpoch(sec) {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const COMPLETION_HINTS = {
  manual: "Members claim it themselves from /bank quests.",
  phrase: "Saying one of the phrases in chat completes it automatically.",
  game: "Completes on its own when the event happens. Daily and weekly quests pay once per period; event quests pay every time.",
};

function wireAuthoring(container, channels, switchToAuthorTab) {
  const form = container.querySelector("[data-form-quest]");
  const status = form.querySelector("[data-status-quest]");
  const rewardInput = form.querySelector("[name=reward]");
  const qtypeSel = form.querySelector("[name=qtype]");
  const hint = form.querySelector("[data-reward-hint]");
  const typeHint = form.querySelector("[data-type-hint]");
  const communityField = form.querySelector("[data-community-target]");
  const rotateField = form.querySelector("[data-rotate-field]");
  const completionBlock = form.querySelector("[data-completion-block]");
  const completionHint = form.querySelector("[data-completion-hint]");
  const wordsField = form.querySelector("[data-trigger-words]");
  const channelField = form.querySelector("[data-trigger-channel]");
  const kindField = form.querySelector("[data-trigger-kind]");
  const kindHint = form.querySelector("[data-kind-hint]");
  const kindSel = form.querySelector("[name=trigger_kind]");
  const targetField = form.querySelector("[data-target-count]");
  const targetInput = form.querySelector("[name=target_count]");
  const targetHint = form.querySelector("[data-target-hint]");
  const submitBtn = form.querySelector("[data-submit-quest]");
  const cancelBtn = form.querySelector("[data-cancel-edit]");
  const authorLabel = container.querySelector("[data-author-label]");
  const triggerPicker = mountChannelPicker(
    form.querySelector('[data-picker="trigger-channel"]'), channels, "0",
    { emptyLabel: "(any channel)", label: "Only in This Channel" },
  );
  guardForm(form);
  let editingId = null;

  const completion = () =>
    form.querySelector("[name=completion]:checked")?.value || "manual";
  const setCompletion = (value) => {
    const radio = form.querySelector(`[name=completion][value="${value}"]`);
    if (radio) radio.checked = true;
  };

  const updateHint = () => { hint.textContent = bandHint(qtypeSel.value, rewardInput.value); };
  const communityAuto = form.querySelector("[data-community-auto]");
  const updateKindHint = () => {
    const qtype = qtypeSel.value;
    kindHint.textContent = qtype === "event"
      ? "Pays every time it happens, once per member per game, card or round."
      : qtype === "community"
        ? "Every member's action counts toward the shared goal. The weekly scheduler runs it and pays 40/70/100% tiers."
        : qtype === "monthly"
          ? "Every member's action counts toward one server-wide goal, paid in 40/70/100% tiers at month end."
          : "Auto-completes the quest the first time it happens each period.";
  };
  const updateCommunity = () => {
    const qtype = qtypeSel.value;
    const isCommunity = qtype === "community";
    const isMonthly = qtype === "monthly";
    const isEvent = qtype === "event";
    typeHint.textContent = TYPE_HINTS[qtype] || "";
    rotateField.style.display = qtype === "daily" ? "" : "none";
    // An event quest IS its game trigger; community may be manual (the old
    // Operations-driven goal) or game-triggered (the auto-tracking weekly);
    // monthly is a guild-wide goal that is ALWAYS game-triggered (auto-tracked
    // only). None of these three are ever phrase-completed.
    if (isEvent || isMonthly) setCompletion("game");
    if (isCommunity && completion() === "phrase") setCompletion("manual");
    form.querySelectorAll("[name=completion]").forEach((r) => {
      r.disabled =
        (isEvent && r.value !== "game") ||
        (isMonthly && r.value !== "game") ||
        (isCommunity && r.value === "phrase");
    });
    const mode = completion();
    completionHint.textContent = COMPLETION_HINTS[mode] || "";
    // Manual community goals take a hand-set target; auto-tracking community
    // goals AND all monthly goals are sized by the scheduler at kickoff.
    communityField.style.display = isCommunity && mode !== "game" ? "" : "none";
    communityAuto.style.display =
      (isCommunity && mode === "game") || isMonthly ? "" : "none";
    const channelScoped =
      mode === "phrase" ||
      (mode === "game" && CHANNEL_SCOPED_KINDS.has(kindSel.value));
    wordsField.style.display = mode === "phrase" ? "" : "none";
    channelField.style.display = channelScoped ? "" : "none";
    kindField.style.display = mode === "game" ? "" : "none";
    // The per-member "How Many Times" count only applies to daily/weekly board
    // quests — events pay every occurrence, and community/monthly are measured
    // by the guild-wide auto-sized target instead.
    const countable = mode === "game" && ["daily", "weekly"].includes(qtype);
    targetField.style.display = countable ? "" : "none";
    updateKindHint();
    updateTargetHint();
  };
  // A weekly triggered quest can't be one-shot — the server rejects a target of
  // 1, so warn before the save round-trips (matches _check_target_count).
  const updateTargetHint = () => {
    const oneShot = completion() === "game"
      && qtypeSel.value === "weekly"
      && Number(targetInput.value) <= 1;
    targetHint.textContent = oneShot
      ? "A weekly game quest must count 2 or more. Only dailies can finish on the first action."
      : "";
  };
  rewardInput.addEventListener("input", updateHint);
  qtypeSel.addEventListener("change", () => { updateHint(); updateCommunity(); });
  kindSel.addEventListener("change", updateCommunity);
  targetInput.addEventListener("input", updateTargetHint);
  form.querySelectorAll("[name=completion]").forEach((r) =>
    r.addEventListener("change", updateCommunity));
  updateHint();
  updateCommunity();

  const exitEditMode = () => {
    editingId = null;
    form.reset();
    triggerPicker.setValue("0");
    authorLabel.textContent = "New Quest";
    submitBtn.textContent = "Create Quest";
    cancelBtn.style.display = "none";
    updateHint();
    updateCommunity();
  };
  cancelBtn.addEventListener("click", exitEditMode);

  container.addEventListener("dk-edit-quest", (e) => {
    // The form lives behind the New Quest tab now — bring it into view
    // before filling it in, the same way this used to scroll it into view.
    switchToAuthorTab();
    const q = e.detail;
    editingId = q.id;
    form.querySelector("[name=title]").value = q.title || "";
    form.querySelector("[name=description]").value = q.description || "";
    form.querySelector("[name=criteria]").value = q.criteria || "";
    qtypeSel.value = q.qtype;
    rewardInput.value = q.reward ?? 0;
    form.querySelector("[name=reward_xp]").value = q.reward_xp ?? 0;
    form.querySelector("[name=signoff]").checked = !!q.signoff;
    form.querySelector("[name=rotate_tag]").value = q.rotate_tag || "";
    form.querySelector("[name=pair_tag]").value = q.pair_tag || "";
    form.querySelector("[name=starts_at]").value = fromEpoch(q.starts_at);
    form.querySelector("[name=ends_at]").value = fromEpoch(q.ends_at);
    form.querySelector("[name=community_target]").value = q.community_target ?? "";
    form.querySelector("[name=trigger_words]").value = q.trigger_words || "";
    triggerPicker.setValue(q.trigger_channel_id || "0");
    if (q.trigger_kind) kindSel.value = q.trigger_kind;
    targetInput.value = q.target_count ?? 1;
    setCompletion(q.trigger_kind ? "game" : (q.trigger_words ? "phrase" : "manual"));
    authorLabel.textContent = `Editing: ${q.title}`;
    submitBtn.textContent = "Save Changes";
    cancelBtn.style.display = "";
    updateHint();
    updateCommunity();
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  guardedSubmit(form, async () => {
    const qtype = qtypeSel.value;
    const mode = completion();

    // Blank or silly numbers used to slip through as 0 or a 422 naming no
    // field — check them here and say which one is wrong (W-C5).
    const titleInput = form.querySelector("[name=title]");
    if (!titleInput.value.trim()) {
      showStatus(status, false, "Give the quest a Title");
      titleInput.focus();
      return;
    }
    const NUMS = [
      ["reward", "Reward", 0, 1000000],
      ["reward_xp", "Bonus XP", 0, 1000000],
    ];
    const nums = {};
    for (const [name, label, min, max] of NUMS) {
      const input = form.querySelector(`[name=${name}]`);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < min || n > max) {
        showStatus(status, false, `${label} must be a whole number from ${min} to ${max}`);
        input.focus();
        return;
      }
      nums[name] = n;
    }

    const body = {
      title: titleInput.value.trim(),
      description: form.querySelector("[name=description]").value,
      criteria: form.querySelector("[name=criteria]").value,
      qtype,
      reward: nums.reward,
      reward_xp: nums.reward_xp,
      signoff: form.querySelector("[name=signoff]").checked,
      rotate_tag: form.querySelector("[name=rotate_tag]").value.trim(),
      pair_tag: form.querySelector("[name=pair_tag]").value.trim(),
      starts_at: toEpoch(form.querySelector("[name=starts_at]").value),
      ends_at: toEpoch(form.querySelector("[name=ends_at]").value),
      // Always sent so an edit that switches completion mode clears the
      // other mode's fields instead of leaving them behind.
      trigger_words: "",
      trigger_channel_id: null,
      trigger_kind: "",
      target_count: 1,
    };
    if (qtype === "monthly") {
      // Guild-wide monthly goal: always kind-tracked, the scheduler sizes the
      // target at kickoff — no manual target, no per-user count.
      body.trigger_kind = kindSel.value;
      body.community_target = null;
      if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
        const trigCh = triggerPicker.getValue();
        body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
      }
    } else if (qtype === "community") {
      if (mode === "game") {
        // Auto-tracking weekly: the scheduler sizes the target at kickoff.
        body.trigger_kind = kindSel.value;
        body.community_target = null;
        if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
          const trigCh = triggerPicker.getValue();
          body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
        }
      } else {
        const targetEl = form.querySelector("[name=community_target]");
        const t = targetEl.value;
        if (t !== "") {
          const n = parseInt(t, 10);
          if (!Number.isFinite(n) || n < 0 || n > 100000000) {
            showStatus(status, false, "Community Target must be a whole number from 0 to 100000000");
            targetEl.focus();
            return;
          }
          body.community_target = n;
        } else {
          body.community_target = null;
        }
      }
    } else if (mode === "game") {
      body.trigger_kind = kindSel.value;
      if (["daily", "weekly"].includes(qtype)) {
        body.target_count = Math.max(1, parseInt(targetInput.value, 10) || 1);
      }
      if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
        const trigCh = triggerPicker.getValue();
        body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
      }
    } else if (mode === "phrase") {
      body.trigger_words = form.querySelector("[name=trigger_words]").value.trim();
      const trigCh = triggerPicker.getValue();
      body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
    }
    try {
      if (editingId != null) {
        await apiPut(`/api/economy/quests/${editingId}`, body);
        showStatus(status, true, "Saved");
      } else {
        await apiPost("/api/economy/quests", body);
        showStatus(status, true, "Created");
      }
      exitEditMode();
      refreshQuests(container);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
