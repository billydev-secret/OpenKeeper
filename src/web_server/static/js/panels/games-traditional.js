import { mountGamePanel } from "./games-panel-shared.js";

// Traditional Truth or Dare. Every bank question carries exactly one of the
// four categories (enforced by the required dropdown + the server), so the
// in-game "Ask Question" button can hand each player a question in a category
// they opted into.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "traditional", gameName: "Traditional Truth or Dare", gameIcon: "🎲", hasBank: true,
    bankHint: "File every question under one of the four categories. <strong>NSFW</strong> ones only reach players who opted into an NSFW category.",
    bankCategories: [
      { value: "sfw_truth", label: "SFW Truth" },
      { value: "sfw_dare", label: "SFW Dare" },
      { value: "nsfw_truth", label: "NSFW Truth" },
      { value: "nsfw_dare", label: "NSFW Dare" },
    ],
    optSchema: [
      { key: "idle_minutes", label: "Quiet Minutes Before Auto-Close", type: "number", default: 60, min: 0, max: 1440,
        hint: "A game with nothing pressed for this long ends itself with the usual recap and payout. 0 turns it off." },
    ],
  });
}
