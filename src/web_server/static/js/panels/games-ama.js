import { mountGamePanel } from "./games-panel-shared.js";

// No question bank here, deliberately. Every AMA question is typed by a member
// during the game and asked anonymously; nothing in games_ama_cog ever reads
// games_question_bank, so the bank UI this page used to show curated rows the
// game could never serve.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: false,
    intro: "Members ask their questions anonymously during the game, so there is no bank to fill here.",
    optSchema: [
      { key: "hot_seat_ping_role_id", label: "Hot-Seat Ping Role", type: "role",
        hint: "Pinged when a new person takes the hot seat. (none) pings only Notify Me members. If unset, a role named AMA fills in." },
      { key: "questions_per_turn", label: "Questions per Turn", type: "number", default: 4, min: 1, max: 20,
        hint: "Questions the hot seat answers or passes before the seat rotates." },
    ],
  });
}
