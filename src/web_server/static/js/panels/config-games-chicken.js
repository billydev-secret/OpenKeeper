import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Chicken. The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "chicken",
    name: "Chicken",
    subtitle: "A nerve game: the meter climbs toward a hidden crash point. Bail last to win; anyone still holding when it blows takes the forfeit.",
    lobby: true,
    cards: [
    { title: "Lobby", fields: [
      { name: "min_players", label: "Fewest Players to Start", min: 2, max: 50,
        hint: "A lobby can't start until this many people have joined." },
      { name: "max_players", label: "Most Players Per Lobby", min: 2, max: 50,
        hint: "A full lobby starts on its own, and nobody else can join." },
    ] },
    { title: "Crash", fields: [
      { name: "min_climb", label: "Earliest Crash (seconds)", min: 5, max: 600, step: "0.5",
        hint: "The meter never crashes sooner than this after the climb starts." },
      { name: "max_climb", label: "Latest Crash (seconds)", min: 5, max: 600, step: "0.5",
        hint: "The meter always crashes by this point. The bar is drawn to it, so a random early crash stops partway up." },
    ] },
    ],
    checks: [
      { ordered: ["min_players", "max_players"], focus: "max_players",
        msg: "Most Players Per Lobby cannot be lower than Fewest Players to Start" },
      { ordered: ["min_climb", "max_climb"], focus: "max_climb",
        msg: "Latest Crash cannot be sooner than Earliest Crash" },
    ],
  });
}
