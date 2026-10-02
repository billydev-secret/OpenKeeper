import { mountDuelPanel } from "./games-duel-panel.js";

// Config > Games > Hot Potato. The shared panel is games-duel-panel.js.
export function mount(container) {
  return mountDuelPanel(container, {
    id: "hot_potato",
    name: "Hot Potato",
    subtitle: "Two players pass a ticking bomb back and forth. Whoever holds it when it goes off takes the forfeit.",
    lobby: false,
    cards: [
    { title: "Bomb Timer", fields: [
      { name: "min_timer", label: "Shortest Fuse (seconds)", min: 5, max: 600, step: "0.5",
        hint: "The bomb never goes off sooner than this after a round starts." },
      { name: "max_timer", label: "Longest Fuse (seconds)", min: 10, max: 600, step: "0.5",
        hint: "The bomb always goes off by this point. The exact moment is random, so nobody can count it out." },
      { name: "min_hold", label: "Shortest Hold (seconds)", min: 0, max: 60, step: "0.5",
        hint: "How long a holder must wait before passing the bomb back; 0 is no wait. Must be shorter than the shortest fuse." },
    ] },
    ],
    checks: [
      { ordered: ["min_timer", "max_timer"], focus: "max_timer",
        msg: "Longest Fuse cannot be shorter than Shortest Fuse" },
      { below: ["min_hold", "min_timer"], focus: "min_hold",
        msg: "Shortest Hold has to be shorter than Shortest Fuse, or nobody is ever allowed to pass the potato" },
    ],
  });
}
