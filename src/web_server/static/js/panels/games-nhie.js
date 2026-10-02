import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "nhie", gameName: "Never Have I Ever", gameIcon: "⛔", hasBank: true,
    optSchema: [
      { key: "round_seconds", label: "Seconds per Round", type: "number", default: 0, min: 0, max: 300,
        hint: "Rounds advance themselves after this many seconds, and Next still skips early. 0 leaves pacing to the host." },
      { key: "max_rounds", label: "Rounds per Game", type: "number", default: 10, min: 0, max: 50,
        hint: "The game posts its recap and pays the room after this many rounds. 0 runs until the host presses End Game." },
    ],
  });
}
