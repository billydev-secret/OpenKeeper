import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Pressure Cooker. The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "pressure",
    name: "Pressure Cooker",
    subtitle: "A head-to-head duel where the loser wears a nickname the winner picks.",
    lobby: false,
    cards: [
    ],
    checks: [
    ],
  });
}
