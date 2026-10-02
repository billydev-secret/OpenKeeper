import { syncHash } from "../report-helpers.js";
import { mountList, TODO_LIST } from "./todo.js";

/**
 * The bug list: the todo page mounted over the bug list's own routes, with its
 * own board, its own change-card switch and the filing dial, and no recurring
 * tasks (a bug list carries no chores). The list, detail pane and board card
 * are todo.js's, so a fix there reaches this page too.
 */
const BUG_LIST = {
  ...TODO_LIST,
  api: "/api/bugs",
  syncHash: (params) => syncHash("bugs", params),
  title: "Bug List",
  subtitleHtml: "Bugs filed via <code>/bug</code> or the bug board, kept apart from the todo list.",
  noun: "bug",
  nounTitle: "Bug",
  listHeading: "Bugs",
  empty: {
    completed: "Nothing closed yet. Bugs move here once a moderator marks them done.",
    pending: "No open bugs. Add one below, or use /bug in Discord.",
    all: "No bugs yet. Add one below, or use /bug in Discord.",
  },
  loadError: "Couldn't load the bug list",
  mountError: "Couldn't load the bug list.",
  boardHintHtml: `
    <div class="field-hint" style="margin-bottom:8px">
      <strong>Use a channel of its own.</strong> Two boards in one channel keep
      pushing each other up.
    </div>`,
  board: {
    removeBody:
      "The bug board message will be deleted from its channel. The bugs "
      + "stay, but they can only be closed here on the dashboard afterward.",
    unpostedWarning:
      "Not posted, so no bug can be closed in Discord. /bug still files them; "
      + "closing one needs the dashboard.",
  },
  changeCardsHint:
    "New bugs batch into one card a minute. The card quotes the bug but never "
    + "says who filed it.",
  recurring: false,
  filing: true,
  // A bug is never a shop order or a chore, so the todo list's sections would
  // file every bug under one "Tasks" heading. Spelled out because the spread
  // above would otherwise inherit it.
  sections: false,
};

export function mount(container, initialParams = {}) {
  return mountList(container, initialParams, BUG_LIST);
}
