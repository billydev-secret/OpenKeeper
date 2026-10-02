import { mountGamePanel } from "./games-panel-shared.js";

// Hot Takes has no question bank — every take is typed by a member in the
// lobby — so this page is the enable switch plus the one pacing dial.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "hottakes", gameName: "Hot Takes", gameIcon: "🔥", hasBank: false,
    intro: "Members write the takes during the game, so there is no bank to fill here.",
    optSchema: [
      { key: "round_seconds", label: "Seconds per Take", type: "number", default: 45, min: 0, max: 300,
        hint: "Each take closes after this many seconds, or once everyone has voted. 0 turns the timer off." },
    ],
  });
}
