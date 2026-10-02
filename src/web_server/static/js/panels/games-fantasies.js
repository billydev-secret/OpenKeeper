import { mountGamePanel } from "./games-panel-shared.js";

// Fantasies & Dealbreakers has no question bank — every entry is typed by a
// member during a round — so this page is the enable switch plus the one
// pacing dial.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "fantasies", gameName: "Fantasies & Dealbreakers", gameIcon: "✨", hasBank: false,
    intro: "Members write the entries during each round, so there is no bank to fill here.",
    optSchema: [
      { key: "round_seconds", label: "Seconds per Entry", type: "number", default: 45, min: 0, max: 300,
        hint: "Each entry closes after this many seconds, or once everyone has voted. 0 turns the timer off." },
    ],
  });
}
