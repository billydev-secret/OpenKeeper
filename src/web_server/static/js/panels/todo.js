import { api, apiPost, apiPut, apiDelete, esc, fmtTs, fmtAge } from "../api.js";
import { makeFilterStrip } from "../tab-strip.js";
import { renderLoading, renderEmpty, renderError, showRetryableError } from "../states.js";
import { syncHash } from "../report-helpers.js";
import { guardForm, loadChannels, mountAsync, mountChannelPicker, showStatus, guardedSubmit } from "../config-helpers.js";
import { confirmDialog, toast, bindRowActivation } from "../ui.js";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "09:30" ⇄ minutes since local midnight — the API's time_of_day unit. */
function timeToMinutes(value) {
  const [hh, mm] = String(value || "0:0").split(":").map(Number);
  return (hh || 0) * 60 + (mm || 0);
}

function minutesToTime(minutes) {
  const hh = Math.floor((minutes || 0) / 60);
  const mm = (minutes || 0) % 60;
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

/**
 * The todo list's sections, in reading order. A row is exactly one kind: a
 * shop order (paid work with a member's coins in escrow behind it), a chore a
 * recurring definition spawned, or an ordinary task. The glyphs are the
 * Discord board's own markers (board_logic ORDER_MARKER / CHORE_HEADING /
 * TASK_HEADING), so the two surfaces name a row the same way.
 */
const SECTIONS = [
  { key: "orders", label: "🎁 Shop orders", test: (t) => !!t.purchase_id },
  { key: "chores", label: "🔁 Chores", test: (t) => !!t.recurring_id },
  { key: "tasks", label: "📋 Tasks", test: () => true },
];

/** Split rows into SECTIONS, first match wins; empty sections are dropped. */
export function groupTodos(todos) {
  const groups = SECTIONS.map((s) => ({ ...s, rows: [] }));
  for (const t of todos) groups.find((g) => g.test(t)).rows.push(t);
  return groups.filter((g) => g.rows.length);
}

function renderList(todos, activeId, filter, cfg) {
  if (!todos.length) {
    return renderEmpty(cfg.empty[filter] || cfg.empty.all);
  }
  if (!cfg.sections) return renderRows(todos, activeId);
  return groupTodos(todos)
    .map((g) => `
      <div class="ticket-list-section" role="heading" aria-level="4" data-section="${esc(g.key)}">
        <span>${esc(g.label)}</span><span class="n">${g.rows.length}</span>
      </div>
      ${renderRows(g.rows, activeId)}`)
    .join("");
}

function renderRows(todos, activeId) {
  return todos
    .map((t) => {
      const cls = (t.completed_at || t.missed_at ? "low" : "med")
        + (t.id === activeId ? " active" : "");
      const age = fmtAge(Date.now() / 1000 - t.created_at) + " ago";
      const preview = t.task.length > 80 ? t.task.slice(0, 77) + "…" : t.task;
      const chip = t.completed_at
        ? '<span class="t-chip closed" style="margin-left:4px">Done</span>'
        : t.missed_at
          ? '<span class="t-chip closed" style="margin-left:4px">Missed</span>'
          : '<span class="t-chip open" style="margin-left:4px">Pending</span>';
      return `
      <div class="ticket-item ${esc(cls)}" data-todo-id="${esc(t.id)}"
           tabindex="0" role="button" aria-current="${t.id === activeId ? "true" : "false"}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(preview)}</div>
          <div class="row">
            <span>${esc(t.added_by_name || t.added_by)}</span>
            ${chip}
          </div>
        </div>
        <div class="right">
          <span class="id">#${esc(t.id)}</span>
          <span class="age">${esc(age)}</span>
        </div>
      </div>`;
    })
    .join("");
}

function renderDetail(t, completing, cfg) {
  if (!t) {
    return renderEmpty(`Select a ${cfg.noun} from the list to view its details.`);
  }
  const completedLine = t.completed_at
    ? `<div class="td-section">Completed</div>
       <div style="padding:4px 8px 8px;font-size:14px;color:var(--ink)">
         ${esc(fmtTs(t.completed_at))} by <b>${esc(t.completed_by_name || t.completed_by || "unknown")}</b>
       </div>`
    : "";
  const missedLine = t.missed_at
    ? `<div class="td-section">Missed</div>
       <div style="padding:4px 8px 8px;font-size:14px;color:var(--ink)">
         Written off at ${esc(fmtTs(t.missed_at))} when the next one came round.
         A recurring task resets rather than carrying over, so this day stays on
         the record as one nobody got to.
       </div>`
    : "";
  const completeBtn = !t.completed_at && !t.missed_at
    ? `<div class="td-actions">
        <span class="act-spacer"></span>
        <button class="act-btn" data-action="complete" ${completing ? "disabled" : ""}>
          ${completing ? "Completing…" : "Mark Complete"}
        </button>
       </div>`
    : "";
  const statusChip = t.completed_at
    ? '<span class="t-chip closed">Done</span>'
    : t.missed_at
      ? '<span class="t-chip closed">Missed</span>'
      : '<span class="t-chip open">Pending</span>';
  const descriptionBlock = t.description
    ? `<div class="td-section">Description</div>
       <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(t.description)}</div>`
    : "";
  const sourceBlock = t.source_message_url
    ? `<div class="td-section">Source</div>
       <div style="padding:4px 8px 8px;font-size:14px;">
         <a href="${esc(t.source_message_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--gold-solid);word-break:break-all">Jump to message ↗</a>
       </div>`
    : "";
  return `
    <div class="td-head">
      <div class="td-crumb">#${esc(t.id)} &nbsp;&middot;&nbsp; added ${esc(fmtAge(Date.now() / 1000 - t.created_at))} ago</div>
      <h3 class="td-title" style="word-break:break-word">${esc(t.task)}</h3>
      <div class="td-meta">
        <span class="pair"><span class="k">Added by</span><b>${esc(t.added_by_name || t.added_by)}</b></span>
        <span class="pair"><span class="k">Added</span><b>${esc(fmtTs(t.created_at))}</b></span>
        <span class="pair"><span class="k">Status</span>${statusChip}</span>
      </div>
    </div>
    <div class="td-body">
      <div class="td-section">${esc(cfg.nounTitle)}</div>
      <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(t.task)}</div>
      ${descriptionBlock}
      ${sourceBlock}
      ${completedLine}
      ${missedLine}
    </div>
    ${completeBtn}`;
}

const FILTERS = {
  // A written-off recurring row is closed, not outstanding — the same rule the
  // boards and the API's pending_count use.
  pending:   (t) => !t.completed_at && !t.missed_at,
  completed: (t) => !!t.completed_at,
  all:       () => true,
};

/**
 * Automatic sign-off triggers, mirroring VALID_AUTO_COMPLETE in
 * todo_recurring_service.py. "" is the off value — the service maps it (and a
 * missing field) onto no trigger, so an unpicked dropdown means hand-ticked.
 */
const AUTO_COMPLETE_OPTIONS = [
  ["", "Nothing — a mod ticks it off"],
  ["qotd", "A QOTD is posted"],
  ["spicy_qotd", "A spicy QOTD is posted"],
  ["game", "A game is run"],
];

/** Short label for the list row; "" for a chore nobody signs off automatically. */
function autoCompleteLabel(value) {
  const found = AUTO_COMPLETE_OPTIONS.find((o) => o[0] === (value || ""));
  return value ? (found ? found[1] : value) : "";
}

/** One row of the recurring-task list. */
function recurringRow(item) {
  const paused = item.status === "paused";
  const next = item.next_run_at && !paused
    ? `next ${esc(fmtTs(item.next_run_at))}`
    : paused ? "paused" : "not scheduled";
  // Scheduled fires never skip any more — they reset, writing the old one off
  // and spawning fresh. So this can only come from a Run now that found a copy
  // already on the list, which is deliberately the one path that still skips.
  const skipped = item.last_status === "skipped_pending"
    ? ' <span class="t-chip" title="Run now found a copy already on the list, so nothing was added. Scheduled runs reset instead.">run now skipped</span>'
    : "";
  const auto = item.auto_complete
    ? ` <span class="t-chip" title="The bot ticks this off by itself when ${esc(autoCompleteLabel(item.auto_complete).toLowerCase())}.">auto ✓</span>`
    : "";
  return `
    <tr data-recurring-id="${esc(item.id)}"${paused ? ' style="opacity:.6"' : ""}>
      <td>
        <div style="font-weight:600;word-break:break-word">${esc(item.task)}${auto}</div>
        ${item.description
          ? `<div style="font-size:12px;color:var(--ink-dim);word-break:break-word">${esc(item.description)}</div>`
          : ""}
      </td>
      <td style="white-space:nowrap">${esc(item.cadence)}</td>
      <td style="white-space:nowrap;font-size:12px;color:var(--ink-dim)">${next}${skipped}</td>
      <td style="white-space:nowrap;text-align:right">
        <button class="act-btn" data-act="run-now">Run now</button>
        <button class="act-btn" data-act="toggle">${paused ? "Resume" : "Pause"}</button>
        <button class="act-btn" data-act="edit">Edit</button>
        <button class="act-btn" data-act="delete">Delete</button>
      </td>
    </tr>`;
}

function recurringTable(items) {
  if (!items.length) {
    return renderEmpty(
      "No recurring tasks yet. Add one below, and it lands on the list each time it comes due."
    );
  }
  return `
    <div style="overflow-x:auto">
      <table class="table" data-recurring>
        <thead><tr><th>Task</th><th>Repeats</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${items.map(recurringRow).join("")}</tbody>
      </table>
    </div>`;
}

function recurringEditor(editing) {
  const weekly = (editing?.recurrence || "daily") === "weekly";
  const days = new Set(editing?.recur_days || []);
  return `
    <form data-recurring-form class="form" style="margin-top:12px">
      <div class="field">
        <label for="rec-task">Task</label>
        <input id="rec-task" type="text" data-rec="task" maxlength="500" required
               placeholder="Post QOTD" value="${esc(editing?.task || "")}" />
        <div class="field-hint">What a moderator needs to do. This is the text that lands on the list.</div>
      </div>
      <div class="field">
        <label for="rec-desc">Notes (optional)</label>
        <input id="rec-desc" type="text" data-rec="description" maxlength="1000"
               placeholder="Tag the QOTD role so it counts."
               value="${esc(editing?.description || "")}" />
      </div>
      <div class="field-row" style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="field">
          <label for="rec-recurrence">Repeats</label>
          <select id="rec-recurrence" data-rec="recurrence">
            <option value="daily"${weekly ? "" : " selected"}>Daily</option>
            <option value="weekly"${weekly ? " selected" : ""}>Weekly</option>
          </select>
        </div>
        <div class="field">
          <label for="rec-time">At</label>
          <input id="rec-time" type="time" data-rec="time"
                 value="${esc(minutesToTime(editing?.time_of_day ?? 540))}" />
          <div class="field-hint">Server local time.</div>
        </div>
      </div>
      <div class="field">
        <label for="rec-auto">Tick off automatically when</label>
        <select id="rec-auto" data-rec="auto_complete">
          ${AUTO_COMPLETE_OPTIONS.map(([value, label]) => `
            <option value="${esc(value)}"${(editing?.auto_complete || "") === value ? " selected" : ""}>${esc(label)}</option>
          `).join("")}
        </select>
        <div class="field-hint">
          “A game is run” means one a <em>moderator</em> starts. Set the time
          no later than when you'd do it, or the day is marked missed.
        </div>
      </div>
      <div class="field" data-rec-days style="${weekly ? "" : "display:none"}">
        <label>On these days</label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${WEEKDAYS.map((d, i) => `
            <label style="display:flex;align-items:center;gap:4px;font-weight:400">
              <input type="checkbox" data-rec-day="${i}"${days.has(i) ? " checked" : ""} /> ${d}
            </label>`).join("")}
        </div>
      </div>
      <div class="td-actions">
        <button type="submit" class="btn btn-primary" data-rec-save>
          ${editing ? "Save Changes" : "Add Recurring Task"}
        </button>
        ${editing ? '<button type="button" class="act-btn" data-rec-cancel>Cancel</button>' : ""}
        <span data-rec-status style="font-size:12px"></span>
      </div>
    </form>`;
}

/**
 * What makes this page the todo list. The bug list (bugs.js) mounts the same
 * page with its own copy of this object, so a fix to the list, the board card
 * or the change-card switch reaches both pages. Hint strings are trusted,
 * static markup written here, never data.
 */
export const TODO_LIST = {
  api: "/api/todos",
  // A function, not a route id: the URL-state test reads the literal id out of
  // each page's own source file.
  syncHash: (params) => syncHash("mod-todo", params),
  title: "Todo List",
  subtitleHtml: "Server-wide tasks added via <code>/todo</code>.",
  noun: "task",
  nounTitle: "Task",
  listHeading: "Tasks",
  empty: {
    completed: "Nothing completed yet. Tasks move here once someone marks them done.",
    pending: "No pending tasks. Add one below, or use /todo in Discord.",
    all: "No tasks yet. Add one below, or use /todo in Discord.",
  },
  loadError: "Couldn't load the todo list",
  mountError: "Couldn't load the todo board.",
  boardHintHtml: `
    <div class="field-hint" style="margin-bottom:8px">
      A live board at the bottom of a channel. Use a mod channel: it shows
      chores and requests nobody has reviewed.
    </div>
    <div class="field-hint" style="margin-bottom:8px">
      Add and Complete are moderator-only. Sign-Offs and Approvals need
      Administrator or the economy manager role.
    </div>`,
  board: {
    removeBody:
      "The board message will be deleted from its channel. Tasks and chores "
      + "themselves are untouched — but this is the only place in Discord anything "
      + "can be ticked off, so completing one will mean coming back to the dashboard.",
    // The board is the *only* Discord surface that can tick anything off, so
    // unposting it removes a capability, silently. It sat unposted for three
    // days in prod and was noticed by a mod failing to tick anything off.
    unpostedWarning:
      "Not posted, so nothing can be ticked off in Discord. /todo still adds "
      + "tasks; completing one needs the dashboard.",
  },
  changeCardsHint:
    "Arrivals batch into one card a minute; chores get one summary at noon. "
    + "Ticking things off is never announced.",
  recurring: true,
  filing: false,
  // Shop orders, chores and tasks under their own headings. The bug list
  // spreads this config and turns it back off: a bug is never either kind.
  sections: true,
};

export function mount(container, initialParams = {}) {
  return mountList(container, initialParams, TODO_LIST);
}

export function mountList(container, initialParams, cfg) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>${esc(cfg.title)}</h2>
        <div class="subtitle">${cfg.subtitleHtml}</div>
      </header>

      <div class="mod-stats" data-stats>
        <div class="mod-stat open"><div class="lbl">Pending</div><div class="v">—</div></div>
        <div class="mod-stat resolved"><div class="lbl">Completed</div><div class="v">—</div></div>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>${esc(cfg.listHeading)}</h3>
            <div class="ctrl-group" role="group" aria-label="Filter ${esc(cfg.listHeading.toLowerCase())}" data-filter-group>
              <button class="active" data-filter="pending">Pending</button>
              <button data-filter="completed">Completed</button>
              <button data-filter="all">All</button>
            </div>
          </div>
          <div class="ticket-list" data-list>
            ${renderLoading(`Loading ${cfg.listHeading.toLowerCase()}…`)}
          </div>
        </div>
        <div class="ticket-detail" data-detail>
          ${renderLoading(`Loading ${cfg.listHeading.toLowerCase()}…`)}
        </div>
      </section>

      <form class="todo-add" data-add-form style="display:flex;gap:8px;margin-top:12px;align-items:flex-start;">
        <input type="text" data-add-input maxlength="500" placeholder="Add a new ${esc(cfg.noun)}…" aria-label="New ${esc(cfg.noun)}"
               style="flex:1;padding:8px 10px;border:1px solid var(--rule);border-radius:4px;background:var(--bg-rail);color:var(--ink);font-size:13px;font-family:inherit;" />
        <button type="submit" class="act-btn" data-add-btn>Add</button>
        <span data-add-status style="font-size:12px;align-self:center;"></span>
      </form>

      <section class="card" data-board-card style="margin-top:20px">
        <div class="section-label">Discord Board</div>
        ${cfg.boardHintHtml}
        <div data-board-body>${renderLoading("Loading board…")}</div>
      </section>

      ${cfg.filing ? `
      <section class="card" style="margin-top:20px">
        <div class="section-label">Who Can File</div>
        <div class="field-hint" style="margin-bottom:8px">
          Applies to <code>/bug</code> and the board's 🐛 File a Bug button.
          Closing a bug is always moderator-only.
        </div>
        <div data-filing-body>${renderLoading("Loading…")}</div>
      </section>` : ""}

      ${cfg.recurring ? `
      <section class="card" style="margin-top:20px">
        <div class="section-label">Recurring Tasks</div>
        <div class="field-hint" style="margin-bottom:8px">
          Reminders the bot adds to the list on a schedule. An untouched one is
          marked missed when the next comes due.
        </div>
        <div data-recurring-body>${renderLoading("Loading recurring tasks…")}</div>
      </section>` : ""}
    </div>
  `;

  const statsEl = container.querySelector("[data-stats]");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const filterGroup = container.querySelector("[data-filter-group]");
  const addForm = container.querySelector("[data-add-form]");
  const addInput = container.querySelector("[data-add-input]");
  const addStatus = container.querySelector("[data-add-status]");

  const recurringBody = container.querySelector("[data-recurring-body]");
  const filingBody = container.querySelector("[data-filing-body]");

  const state = {
    filing: "mods",
    todos: [],
    filter: Object.keys(FILTERS).includes(initialParams.filter) ? initialParams.filter : "pending",
    activeId: initialParams.task ? Number(initialParams.task) : null,
    completing: false,
    board: null,
    canManageBoard: false,
    changeCards: false,
    channels: [],
    recurring: [],
    editingRecurringId: null,
    busy: false,
  };
  for (const btn of filterGroup.querySelectorAll("[data-filter]")) {
    const on = btn.dataset.filter === state.filter;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  /** Mirror the tab and selected task into the URL (W-D9). */
  function pushHash() {
    // The page's own id, as registered in app.js ("mod-todo", "bugs") — "todo"
    // rewrote the URL to a route that doesn't exist, so every render left a
    // hash that reloads/bookmarks to "Page Not Available".
    cfg.syncHash({
      filter: state.filter === "pending" ? "" : state.filter,
      task: state.activeId || "",
    });
  }

  function render() {
    const filtered = state.todos.filter(FILTERS[state.filter]);
    if (!filtered.find((t) => t.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId, state.filter, cfg);
    const active = state.todos.find((t) => t.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active, state.completing, cfg);
    pushHash();
  }

  // ── board card ──────────────────────────────────────────────────────
  //
  // One sticky board since migration 180. It was two — an all-todos board and a
  // mod chore board — which forced a server with one mod channel to choose, and
  // choosing is what happened: in prod the chore board was posted and the
  // all-todos board never was, leaving 25 open tasks with no Discord surface.
  // The single board carries both as headed sections.

  const BOARD_CARDS = [
    {
      kind: "all",
      stateKey: "board",
      selector: "[data-board-body]",
      pickerLabel: "Board Channel",
      postLabel: "Post Board",
      moveLabel: "Move / Repost Board",
      removeLabel: "Remove Board",
      removeTitle: "Remove the board?",
      postedToast: "Board posted.",
      removedToast: "Board removed.",
      lockedHint: "Only administrators can post or move the board.",
      jumpLabel: "jump to the board ↗",
      // removeBody and unpostedWarning differ per list.
      ...cfg.board,
    },
  ].map((card) => ({ ...card, body: container.querySelector(card.selector) }));

  const boardPickers = {};

  // The board is *edited* when it changes, and an edit does not mark a channel
  // unread — so a change is invisible until a mod looks. This posts a card
  // above the board instead, which both notifies and leaves a history. It sits
  // with the placement controls rather than in its own card because it is
  // meaningless without a posted board, and admin-gated for the same reason
  // placement is: it decides whether the bot talks.
  function changeCardsRow(locked, posted) {
    // Say so when there is no board. The cards are posted *above* the board and
    // the board is re-stuck under them, so with nothing posted a ticked box does
    // nothing at all — and a setting that reads as on while nothing happens is
    // worse than one that explains itself.
    const idle = state.changeCards && !posted
      ? `<div class="field-hint" style="margin-top:4px">
           Cards sit above the board, so none post until the board is up. Pick a
           channel above and press Post Board.
         </div>`
      : "";
    return `
      <label class="field-hint" style="display:flex;align-items:center;gap:8px;margin-top:10px">
        <input type="checkbox" data-act="change-cards" ${state.changeCards ? "checked" : ""}
               ${locked ? "disabled" : ""}>
        <span>Post a card above the board when something new arrives</span>
      </label>
      <div class="field-hint" style="margin-top:4px">${esc(cfg.changeCardsHint)}</div>
      ${idle}`;
  }

  function renderBoardCard(card) {
    const board = state[card.stateKey] || { posted: false, channel_id: "0" };
    const locked = !state.canManageBoard;
    // Keep a half-made selection across re-renders — refresh() also runs when
    // a task is added or completed, and resetting the picker under the admin
    // then reports "Pick a channel first" on a channel they had just chosen.
    const selected = boardPickers[card.kind]?.getValue?.()
      ?? String(board.channel_id || "0");
    const where = board.posted && board.jump_url
      ? `Posted: <a href="${esc(board.jump_url)}" target="_blank" rel="noopener noreferrer"
           style="color:var(--gold-solid)">${esc(card.jumpLabel)}</a>`
      : "Not posted yet.";
    // Shown to moderators as well as admins: a mod who cannot move the board
    // is exactly the person who needs to know why they have nowhere to tick a
    // task off. Placement stays admin-gated; the *fact* is not privileged.
    const warning = !board.posted && card.unpostedWarning
      ? `<div class="field-hint" data-board-warning
              style="margin-top:6px;color:var(--gold-solid)">⚠️ ${esc(card.unpostedWarning)}</div>`
      : "";
    card.body.innerHTML = `
      <div class="field">
        <label>${esc(card.pickerLabel)}</label>
        <span data-picker="board-channel"></span>
      </div>
      <div class="td-actions">
        <button class="btn btn-primary" data-act="board-save" ${locked ? "disabled" : ""}>
          ${esc(board.posted ? card.moveLabel : card.postLabel)}
        </button>
        ${board.posted
          ? `<button class="act-btn" data-act="board-remove" ${locked ? "disabled" : ""}>${esc(card.removeLabel)}</button>`
          : ""}
        <span data-board-status style="font-size:12px"></span>
      </div>
      <div class="field-hint" style="margin-top:6px">
        ${locked ? esc(card.lockedHint) : where}
      </div>
      ${warning}
      ${changeCardsRow(locked, board.posted)}`;

    boardPickers[card.kind] = mountChannelPicker(
      card.body.querySelector('[data-picker="board-channel"]'),
      state.channels,
      selected,
      { label: card.pickerLabel }
    );
    if (locked) {
      card.body.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
    }
  }

  function renderBoard() {
    BOARD_CARDS.forEach(renderBoardCard);
  }

  for (const card of BOARD_CARDS) {
    // Delegated, because renderBoardCard replaces the whole card's innerHTML —
    // a listener bound to the checkbox itself would die on the next refresh().
    card.body.addEventListener("change", async (e) => {
      const box = e.target.closest('input[data-act="change-cards"]');
      if (!box || box.disabled || state.busy) return;
      const status = card.body.querySelector("[data-board-status]");
      const enabled = box.checked;
      state.busy = true;
      box.disabled = true;
      try {
        await apiPut(`${cfg.api}/board/change-cards`, { enabled });
        state.changeCards = enabled;
        const posted = (state[card.stateKey] || {}).posted;
        toast(
          !enabled
            ? "Change cards off."
            : posted
              ? "Change cards on — the next new item posts one."
              : "Change cards on — nothing posts until the board is.",
          "success"
        );
        // Re-render so the "no board yet" note appears or clears with the box.
        // Safe from here: this listener is bound once, outside renderBoardCard.
        renderBoardCard(card);
      } catch (err) {
        // Put the box back where it was: leaving it showing the state the save
        // failed to reach is how a dial reads as on while nothing enforces it.
        box.checked = !enabled;
        showStatus(status, false, err.message);
      } finally {
        state.busy = false;
        box.disabled = false;
      }
    });

    card.body.addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn || btn.disabled || state.busy) return;
      const status = card.body.querySelector("[data-board-status]");
      const remove = btn.dataset.act === "board-remove";

      if (remove) {
        const ok = await confirmDialog(card.removeBody, {
          danger: true, title: card.removeTitle, confirmLabel: "Remove",
        });
        if (!ok) return;
      }
      const channelId = remove ? "0" : (boardPickers[card.kind]?.getValue?.() || "0");
      if (!remove && channelId === "0") {
        showStatus(status, false, "Pick a channel first.");
        return;
      }
      state.busy = true;
      btn.disabled = true;
      try {
        const res = await apiPut(
          `${cfg.api}/board`, { channel_id: channelId });
        toast(remove ? card.removedToast : card.postedToast, "success");
        // Posted, but another sticky panel already holds that channel's bottom.
        if (res && res.warning) toast(res.warning, "info");
        await refresh();
      } catch (err) {
        // A 409 here is the same-channel refusal, and its message explains
        // why — surface it rather than a generic failure.
        showStatus(status, false, err.message);
      } finally {
        state.busy = false;
        // The success path re-renders this button away; the error path doesn't,
        // so re-enable or a failed post leaves it dead until a page reload.
        btn.disabled = false;
      }
    });
  }

  // ── filing dial (bug list only) ─────────────────────────────────────
  //
  // Two states, one control. Admin-gated like the board: opening it makes a
  // write surface for every member. Moderators see it locked, so the person
  // wondering why members can't file can read the answer.

  const FILING_CHOICES = [
    ["mods", "Moderators only"],
    ["everyone", "Everyone — members wait five minutes between bugs"],
  ];

  function renderFiling() {
    if (!filingBody) return;
    const locked = !state.canManageBoard;
    filingBody.innerHTML = `
      <div class="field" role="radiogroup" aria-label="Who can file">
        ${FILING_CHOICES.map(([value, label]) => `
          <label style="display:flex;align-items:center;gap:8px;font-weight:400">
            <input type="radio" name="bug-filing" value="${esc(value)}"
                   ${state.filing === value ? "checked" : ""} ${locked ? "disabled" : ""}>
            <span>${esc(label)}</span>
          </label>`).join("")}
      </div>
      <div class="field-hint" style="margin-top:6px">
        ${locked ? "Only administrators can change who files." : ""}
        <span data-filing-status style="font-size:12px"></span>
      </div>`;
  }

  // Delegated for the same reason the change-card box is: renderFiling
  // replaces the markup, so a listener on the radio itself would die.
  filingBody?.addEventListener("change", async (e) => {
    const radio = e.target.closest('input[name="bug-filing"]');
    if (!radio || radio.disabled || state.busy) return;
    const status = filingBody.querySelector("[data-filing-status]");
    const previous = state.filing;
    state.busy = true;
    try {
      await apiPut(`${cfg.api}/filing`, { mode: radio.value });
      state.filing = radio.value;
      toast(
        radio.value === "everyone"
          ? "Any member can now file a bug."
          : "Only moderators can file bugs now.",
        "success"
      );
    } catch (err) {
      // Put the dial back: showing a state the save never reached reads as
      // enforced while nothing is.
      state.filing = previous;
      renderFiling();
      showStatus(filingBody.querySelector("[data-filing-status]") || status, false, err.message);
    } finally {
      state.busy = false;
    }
  });

  // ── recurring card ──────────────────────────────────────────────────

  function renderRecurring() {
    const editing = state.editingRecurringId
      ? state.recurring.find((r) => r.id === state.editingRecurringId) || null
      : null;
    recurringBody.innerHTML = recurringTable(state.recurring) + recurringEditor(editing);
    wireRecurringForm();
  }

  function readRecurringForm(form) {
    const recurrence = form.querySelector('[data-rec="recurrence"]').value;
    return {
      task: form.querySelector('[data-rec="task"]').value.trim(),
      description: form.querySelector('[data-rec="description"]').value.trim() || null,
      recurrence,
      auto_complete: form.querySelector('[data-rec="auto_complete"]').value || null,
      time_of_day: timeToMinutes(form.querySelector('[data-rec="time"]').value),
      recur_days: recurrence === "weekly"
        ? [...form.querySelectorAll("[data-rec-day]")]
            .filter((cb) => cb.checked)
            .map((cb) => Number(cb.dataset.recDay))
        : [],
    };
  }

  function wireRecurringForm() {
    const form = recurringBody.querySelector("[data-recurring-form]");
    if (!form) return;
    const status = form.querySelector("[data-rec-status]");

    // Weekday pickers only mean something for a weekly cadence.
    form.querySelector('[data-rec="recurrence"]').addEventListener("change", (e) => {
      form.querySelector("[data-rec-days]").style.display =
        e.target.value === "weekly" ? "" : "none";
    });

    // Unsaved-edits guard, same as every other config form — a half-filled
    // recurring task shouldn't vanish on a sidebar click.
    guardForm(form);

    form.querySelector("[data-rec-cancel]")?.addEventListener("click", () => {
      state.editingRecurringId = null;
      renderRecurring();
    });

    guardedSubmit(form, async () => {
      if (state.busy) return;
      const body = readRecurringForm(form);
      if (!body.task) {
        showStatus(status, false, "Give the task a name.");
        return;
      }
      if (body.recurrence === "weekly" && !body.recur_days.length) {
        showStatus(status, false, "Pick at least one day of the week.");
        return;
      }
      state.busy = true;
      try {
        if (state.editingRecurringId) {
          await apiPut(`/api/todos/recurring/${state.editingRecurringId}`, body);
        } else {
          await apiPost("/api/todos/recurring", body);
        }
        // Clears guardForm's dirty flag — without this the panel prompts about
        // unsaved changes on every navigation until it is remounted.
        showStatus(status, true, "Saved");
        state.editingRecurringId = null;
        await refreshRecurring();
      } catch (err) {
        showStatus(status, false, err.message);
      } finally {
        state.busy = false;
      }
    });
  }

  recurringBody?.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || state.busy) return;
    const row = btn.closest("tr[data-recurring-id]");
    if (!row) return;
    const id = Number(row.dataset.recurringId);
    const item = state.recurring.find((r) => r.id === id);
    const act = btn.dataset.act;

    if (act === "edit") {
      state.editingRecurringId = id;
      renderRecurring();
      recurringBody.querySelector('[data-rec="task"]')?.focus();
      return;
    }

    if (act === "delete") {
      const ok = await confirmDialog(
        `“${item?.task ?? "This task"}” will stop repeating. Any copy already on ` +
        "the list stays there — it's real outstanding work.",
        { danger: true, title: "Delete recurring task?", confirmLabel: "Delete" }
      );
      if (!ok) return;
    }

    state.busy = true;
    try {
      if (act === "delete") {
        await apiDelete(`/api/todos/recurring/${id}`);
      } else if (act === "toggle") {
        const action = item?.status === "paused" ? "resume" : "pause";
        await apiPost(`/api/todos/recurring/${id}/${action}`, {});
      } else if (act === "run-now") {
        const res = await apiPost(`/api/todos/recurring/${id}/run-now`, {});
        const already = res?.spawned === false;
        toast(
          already ? "Already on the list — nothing new added." : "Added to the list.",
          already ? "info" : "success"
        );
        await Promise.all([refresh(), refreshRecurring()]);
        return;
      }
      await refreshRecurring();
    } catch (err) {
      toast(`Couldn't do that — ${err.message}`, "error");
    } finally {
      state.busy = false;
    }
  });

  async function refreshRecurring() {
    try {
      const data = await api("/api/todos/recurring");
      state.recurring = data.items || [];
      renderRecurring();
    } catch (err) {
      showRetryableError(
        recurringBody, `Couldn't load recurring tasks — ${err.message}`, refreshRecurring,
      );
    }
  }

  function renderStats() {
    // Three states, not two. A row the daily reset wrote off is neither
    // pending nor completed, so deriving one count from the other would put
    // missed chores in whichever tile was computed by subtraction — and the
    // Pending tile would disagree with the Pending list directly below it.
    const pending = state.todos.filter(FILTERS.pending).length;
    const completed = state.todos.filter(FILTERS.completed).length;
    const missed = state.todos.filter((t) => !!t.missed_at).length;
    const missedTile = missed
      ? `<div class="mod-stat"><div class="lbl">Missed</div><div class="v">${missed}</div></div>`
      : "";
    statsEl.innerHTML = `
      <div class="mod-stat open"><div class="lbl">Pending</div><div class="v">${pending}</div></div>
      <div class="mod-stat resolved"><div class="lbl">Completed</div><div class="v">${completed}</div></div>
      ${missedTile}`;
  }

  async function refresh() {
    try {
      const data = await api(cfg.api);
      state.todos = data.todos || [];
      state.board = data.board || null;
      state.canManageBoard = !!data.can_manage_board;
      state.changeCards = !!data.change_cards;
      state.filing = data.filing || "mods";
      renderStats();
      render();
      renderBoard();
      renderFiling();
    } catch (err) {
      showRetryableError(listEl, `${cfg.loadError} — ${err.message}`, refresh);
      detailEl.innerHTML = "";
    }
  }

  guardedSubmit(addForm, async () => {
    const task = addInput.value.trim();
    if (!task) return;
    addStatus.textContent = "";
    try {
      await apiPost(cfg.api, { task });
      addInput.value = "";
      state.filter = "pending";
      filterGroup.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b.dataset.filter === "pending");
      });
      await refresh();
    } catch (err) {
      showStatus(addStatus, false, `Couldn't add that ${cfg.noun} — ${err.message}`);
    }
  });

  makeFilterStrip(filterGroup, (value) => {
    state.filter = value;
    state.activeId = null;
    render();
  });

  bindRowActivation(listEl, ".ticket-item", (row) => {
    state.activeId = Number(row.dataset.todoId);
    render();
  });

  detailEl.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act-btn[data-action='complete']");
    if (!btn || btn.disabled || state.completing || !state.activeId) return;
    state.completing = true;
    render();
    try {
      await apiPost(`${cfg.api}/${state.activeId}/complete`, {});
      await refresh();
    } catch (err) {
      state.completing = false;
      render();
      detailEl.insertAdjacentHTML(
        "beforeend",
        renderError(`Couldn't mark that ${cfg.noun} complete — ${err.message}. Try again.`)
      );
    } finally {
      state.completing = false;
    }
  });

  // mountAsync, not a bare `(async () => {…})()`: without a rejection handler a
  // failed first fetch left the "Loading…" state up forever plus an unhandled
  // rejection in the console (F1). Now it renders a real error with a retry.
  return mountAsync(container, async () => {
    // Channels first: the board card can't render its picker without them, and
    // a channel-load failure shouldn't block the task list from appearing.
    try {
      // Text channels only. /api/meta/channels also returns threads, but the
      // board lives by delete-and-repost and a thread can archive out from
      // under it — and guild.get_channel() doesn't resolve threads anyway, so
      // offering one would just 400 with "That channel doesn't exist here."
      state.channels = (await loadChannels()).filter((c) => c.type === "text");
    } catch {
      state.channels = [];
    }
    await Promise.all([refresh(), ...(cfg.recurring ? [refreshRecurring()] : [])]);
  }, { errorMsg: cfg.mountError });
}
