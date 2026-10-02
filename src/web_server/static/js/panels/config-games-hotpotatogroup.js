import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Hot Potato (Group). The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "hot_potato_group",
    name: "Hot Potato (Group)",
    subtitle: "A whole lobby passes a ticking bomb around. Whoever holds it when it goes off is out.",
    lobby: true,
    cards: [
    { title: "Lobby", fields: [
      { name: "min_players", label: "Fewest Players to Start", min: 2, max: 50,
        hint: "A lobby can't start until this many people have joined." },
      { name: "max_players", label: "Most Players Per Lobby", min: 2, max: 50,
        hint: "A full lobby starts on its own, and nobody else can join." },
    ] },
    { title: "Bomb Timer", fields: [
      { name: "min_fuse", label: "Shortest Fuse (seconds)", min: 5, max: 600, step: "0.5",
        hint: "A round's bomb never goes off sooner than this." },
      { name: "max_fuse", label: "Longest Fuse (seconds)", min: 10, max: 600, step: "0.5",
        hint: "A round's bomb always goes off by this point. The exact moment is random, so nobody can count it out." },
      { name: "min_hold", label: "Must Hold For (seconds)", min: 0, max: 60, step: "0.5",
        hint: "How long a holder must wait before passing the bomb on; 0 is no wait. Must be shorter than the shortest fuse." },
    ] },
    ],
    checks: [
      { ordered: ["min_fuse", "max_fuse"], focus: "max_fuse",
        msg: "Longest Fuse cannot be shorter than Shortest Fuse" },
      { below: ["min_hold", "min_fuse"], focus: "min_hold",
        msg: "Must Hold For has to be shorter than Shortest Fuse, or nobody is ever allowed to pass the bomb" },
      { ordered: ["min_players", "max_players"], focus: "max_players",
        msg: "Most Players Per Lobby cannot be lower than Fewest Players to Start" },
    ],
  });
}
