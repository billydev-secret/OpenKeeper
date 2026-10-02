import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Musical Chairs. The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "musical_chairs",
    name: "Musical Chairs",
    subtitle: "The music stops, everyone grabs a chair, and one player is knocked out each round.",
    lobby: true,
    cards: [
    { title: "Lobby", fields: [
      { name: "min_players", label: "Fewest Players to Start", min: 3, max: 50,
        hint: "A lobby can't start until this many people have joined." },
      { name: "max_players", label: "Most Players Per Lobby", min: 3, max: 50,
        hint: "A full lobby starts on its own, and nobody else can join." },
    ] },
    { title: "Each Round", fields: [
      { name: "min_music", label: "Shortest Music (seconds)", min: 2, max: 300, step: "0.5",
        hint: "The music never stops sooner than this." },
      { name: "max_music", label: "Longest Music (seconds)", min: 3, max: 300, step: "0.5",
        hint: "The music always stops by this point. The exact moment is random, so nobody can time it." },
      { name: "scramble_window", label: "Time to Grab a Chair (seconds)", min: 2, max: 300, step: "0.5",
        hint: "How long players have to sit down after the music stops. Anyone who hasn't sat by then is out." },
      { name: "false_start_elim", label: "Knock out players who sit too early", checkbox: true,
        hint: "Off, sitting early is ignored and the player can try again." },
    ] },
    ],
    checks: [
      { ordered: ["min_music", "max_music"], focus: "max_music",
        msg: "Longest Music cannot be shorter than Shortest Music" },
      { ordered: ["min_players", "max_players"], focus: "max_players",
        msg: "Most Players Per Lobby cannot be lower than Fewest Players to Start" },
    ],
  });
}
