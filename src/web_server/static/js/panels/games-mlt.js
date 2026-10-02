import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "mlt", gameName: "Most Likely To", gameIcon: "👑", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Minimum Players", type: "number", default: 3, min: 2, max: 25,
        hint: "The game won't start until this many players have joined (2–25)." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 25, min: 0, max: 25,
        hint: "Latecomers are turned away once the lobby is this full. 0 means 25, the most the vote menu holds." },
      { key: "round_seconds", label: "Seconds per Round", type: "number", default: 0, min: 0, max: 300,
        hint: "Rounds advance themselves after this many seconds, and Next still skips early. 0 leaves pacing to the host." },
      { key: "max_rounds", label: "Rounds per Game", type: "number", default: 10, min: 0, max: 50,
        hint: "The game posts its recap and pays the room after this many rounds. 0 runs until the host presses End Game." },
    ],
  });
}
