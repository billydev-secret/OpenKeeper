import { mountAsync } from "../config-helpers.js";
import { esc } from "../api.js";
import { mountPanelPoster } from "../panel-post.js";
import { renderSortableTable } from "../table.js";

// Server Features: the one hub members use for their own tools (bug-list row
// 259). The hub has no settings of its own — every row opens a feature that
// is configured on its own page — so this page is the Post control plus a map
// from each row to that page. Every row always shows on the posted hub; a row
// whose feature isn't set up answers the press with that feature's own
// "not set up" line.
const ROWS = [
  { group: "Get Help", row: "🎫 Open a Ticket", opens: "The ticket form", page: "mod-tickets", pageLabel: "Tickets" },
  { group: "Get Help", row: "🤖 Ask (your assistant)", opens: "The Ask question box and private chat", page: "config-advisor", pageLabel: "AI Assistant" },
  { group: "Your Settings", row: "🔔 Roles & Pings", opens: "The bot's ping roles, plus the roles in Discord's Customize page", page: "onboarding", pageLabel: "Discord Onboarding" },
  { group: "Your Settings", row: "📨 Ask to DM", opens: "The DM request form (the DM Requests panel's Open Form)", page: "config-dms", pageLabel: "DM Permissions" },
  { group: "Your Settings", row: "📬 DM Settings", opens: "The member's DM mode and connections", page: "config-dms", pageLabel: "DM Permissions" },
  { group: "Your Settings", row: "💤 Take a Break", opens: "The Take a Break card with its Start button", page: "take-a-break", pageLabel: "Take a Break" },
  { group: "About You", row: "ℹ️ Your Info", opens: "The member's own info card (the same one /info shows): level, rank, activity, roles and opt-ins", page: "config-xp", pageLabel: "XP & Leveling" },
  { group: "Safety & Privacy", row: "🛡️ No Contact", opens: "The member's own no-contact list, with Add and Remove", page: "no-contact", pageLabel: "No-Contact List" },
  { group: "Safety & Privacy", row: "📜 My Data", opens: "The member's data panel: what's stored, and what they can delete", page: "member-data", pageLabel: "Member Data" },
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;

  return mountAsync(container, async () => {
    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Help &amp; Settings</h2>
          <div class="subtitle">One panel where members open a ticket, ask for help, and change their own settings</div>
        </header>

        <div class="section-label">What the Panel Holds</div>
        <div class="card" data-readonly-ok>
          <div class="field-hint" style="margin-bottom:10px;">
            Each button opens a private card for whoever presses it. A row whose
            feature isn't set up says so when pressed.
          </div>
          <div style="overflow-x:auto;" data-rows></div>
        </div>

        <div data-poster="server-features"></div>
      </div>
    `;

    // `order` keeps the hub's own row order until a header is clicked.
    renderSortableTable(container.querySelector("[data-rows]"), {
      columns: [
        { key: "group", label: "Group" },
        { key: "row", label: "Row" },
        { key: "opens", label: "Opens" },
        { key: "pageLabel", label: "Set up on", html: true,
          format: (v, r) => `<a href="#/${esc(r.page)}">${esc(v)}</a>` },
      ],
      data: ROWS.map((r, order) => ({ ...r, order })),
      defaultSort: "order",
      defaultAsc: true,
    });

    mountPanelPoster(container.querySelector('[data-poster="server-features"]'), "server-features", {
      heading: "Post the Help & Settings Panel",
      buttonLabel: "Post Panel",
    });
  });
}
