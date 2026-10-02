import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Quickdraw. The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "quickdraw",
    name: "Quickdraw",
    subtitle: "Two players wait for the signal. The faster reaction wins, and drawing early loses.",
    lobby: false,
    cards: [
    { title: "The Draw", fields: [
      { name: "min_delay", label: "Shortest Wait for the Signal (seconds)", min: 0.5, max: 120, step: "0.5",
        hint: "The signal never comes sooner than this after the round starts." },
      { name: "max_delay", label: "Longest Wait for the Signal (seconds)", min: 1, max: 120, step: "0.5",
        hint: "The signal always comes by this point. The exact moment is random, so nobody can time it." },
      { name: "draw_window", label: "Time to React (seconds)", min: 1, max: 120, step: "0.5",
        hint: "How long players have to fire after the signal. If nobody does, the round is voided and nobody loses." },
    ] },
    ],
    checks: [
      { ordered: ["min_delay", "max_delay"], focus: "max_delay",
        msg: "Longest Wait for the Signal cannot be shorter than Shortest Wait for the Signal" },
    ],
  });
}
