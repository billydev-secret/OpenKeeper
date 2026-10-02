/**
 * The priced dials — every number a member's coins are spent against.
 *
 * Lifted out of the 1,339-line "Shop & Perks" page, where these six cards sat
 * above an emoji approval queue, three catalogs and an order queue. Three
 * reasons that was wrong, all of them visible in the code rather than matters
 * of taste:
 *
 *  - ONE SAVE, SIX SUBJECTS. All six cards were inside a single <form> with a
 *    single submit, so adjusting a raffle ticket price also wrote the hoard tax
 *    rate. They are one form here too — but now the form is the whole page, so
 *    "Save" means what it says.
 *
 *  - A SHARED DIRTY BIT. `guardForm`'s unsaved-edits flag is a module global in
 *    config-helpers.js, and `showStatus(el, true, …)` clears it. The old page
 *    had 41 showStatus call sites: approving an emoji, or merely *starting* an
 *    image upload, cleared the warning that protected a half-typed tax rate, so
 *    navigating away lost it silently. Nothing else on this page can clear it.
 *
 *  - CADENCE. These are set at launch and revisited a few times a year — the
 *    services call a nonzero rate "the launch switch" and a price change
 *    notifies existing renters. That does not belong beside a queue an admin
 *    works through weekly.
 *
 * The PUT is a partial update, so sending only these keys is exactly what the
 * old page sent. Five other panels write the same endpoint the same way.
 */
import { api, apiPut, esc } from "../api.js";
import { showStatus, guardForm, mountAsync, guardedSubmit } from "../config-helpers.js";
import { DEFAULT_MAX, economyOffBanner } from "./economy-shop-shared.js";

// Each entry: [key, label, {hint, min, max}] — the bounds apply to both the input and the
// client-side check that names the offending field on save.
const PRICE_FIELDS = [
  ["price_role_color", "Role Color, Per Week", {
    hint: "Weekly rent for a custom color on a member's own role.",
  }],
  ["price_role_name", "Role Name, Per Week", {
    hint: "Weekly rent for a custom name on a member's own role.",
  }],
  ["price_role_icon", "Custom Role Icon, Per Week", {
    hint: "Needs Role Icons on the server. Weekly rent for an uploaded icon; catalog icons are priced on Shop & Perks.",
  }],
  ["price_role_preset", "Palette Color, Per Week", {
    hint: "Needs enhanced role colors. Weekly rent for a curated color; a color with its own price overrides this.",
  }],
  ["price_role_gradient", "Role Gradient, Per Week", {
    hint: "Needs enhanced role colors. Weekly rent for a two-color gradient the member picks. Price it above Palette Color.",
  }],
  ["price_role_holographic", "Role Holographic Shimmer, Per Week", {
    hint: "Needs enhanced role colors. Weekly rent for Discord's fixed shimmer; the member picks nothing.",
  }],
  ["price_voice_style", "Voice Room Lease, Per Week", {
    hint: "Only charged once Voice Style is checked on Shop & Perks. Weekly rent for Voice Control's rename and user limit.",
  }],
];

// One-shot buys rather than weekly rentals — cheap enough to be an impulse,
// which is the tier the rental ladder doesn't reach. Saved by the same form.
const CONSUMABLE_FIELDS = [
  ["price_quest_reroll", "Quest Reroll", {
    hint: "Charged per swap after the member's free daily one. 0 turns paid rerolls off.",
  }],
  ["quest_reroll_daily_cap", "Paid Rerolls Per Day", {
    hint: "How many paid rerolls one member may buy in a day, on top of the free one. 0 turns paid rerolls off.",
    max: 100,
  }],
  ["price_streak_shield", "Streak Shield", {
    hint: "One-time buy, spent automatically to save a streak the free grace day can't cover. One held at a time.",
  }],
  ["price_pin_of_day", "Pin of the Day", {
    hint: "Price of a mod-approved 24-hour pin. 0 turns Pin of the Day off. It also needs a Pin Channel on Economy Settings.",
  }],
  ["pin_expire_days", "Pin Review Window (days)", {
    hint: "Unreviewed pin requests expire and refund after this many days. 0 leaves them queued forever.",
    max: 365,
  }],
  ["price_flash_theme", "Themed Day", {
    hint: "What a themed day costs. 0 makes it free; the off switch is Sell themed days on Economy Settings.",
  }],
  ["theme_expire_days", "Theme Review Window (days)", {
    hint: "Unreviewed theme requests expire and refund after this many days. 0 leaves them queued forever.",
    max: 365,
  }],
  ["theme_hours", "Themed Day Length (hours)", {
    hint: "How long a theme holds the channel before the next in the queue goes up. 1–168 hours.",
    min: 1,
    max: 168,
  }],
];

// Weekly raffle: tickets in, a free-perk-week voucher out. The enable flag
// is a checkbox (the one non-numeric field on this page) because the winner
// is announced BY NAME — turning it on is a communications decision, not a
// price tweak.
const RAFFLE_FIELDS = [
  ["price_raffle_ticket", "Ticket Price", {
    hint: "What one ticket costs. The raffle stays off at 0, even when switched on.",
  }],
  ["raffle_max_tickets", "Tickets Per Member, Per Week", {
    hint: "The most tickets one member may buy in a week. Stops the richest wallet buying the win.",
    max: 10000,
  }],
];

// Evaporation dials: the weekly hoard tax (demurrage — the only sink that
// works on members who buy nothing) and the house rake on PvP wager pots.
// Both default 0 (off) — like the raffle, turning either on is a
// communications decision, so announce before setting a rate.
const DEMURRAGE_FIELDS = [
  ["demurrage_rate_pct", "Hoard Tax Rate (percent)", {
    hint: "Percent of a wallet above the floor taken at each weekly roll. 0 turns it off; 100 caps wallets at the floor.",
    max: 100,
  }],
  ["demurrage_threshold", "Protected Floor", {
    hint: "Balances at or below this are never taxed; only the part above it is.",
  }],
  ["wager_rake_pct", "Wager Rake (percent)", {
    hint: "The house's cut of each settled wager pot on the duel and group games. 0 keeps them winner-takes-all.",
    max: 50,
  }],
  ["bounty_rake_pct", "Bounty Rake (percent)", {
    hint: "The house's cut of an awarded bounty pot. 0 means the winner takes all; canceled or expired bounties are never raked.",
    max: 100,
  }],
];

// Sponsored emojis: weekly rentals opened by mod approval. The queue that
// approves them is its own page now (Approvals) — see the split note at the
// top of this file. Only the slot count is read while approving.
const EMOJI_FIELDS = [
  ["price_emoji", "Sponsored Emoji, Per Week", {
    hint: "Weekly rent for a sponsored custom emoji; the first week is held when they submit. 0 stops new sponsorships.",
  }],
  ["price_emoji_animated", "Sponsored Animated Emoji, Per Week", {
    hint: "Weekly rent for an animated sponsored emoji. Animated slots are scarcer, so this normally costs more.",
  }],
  ["emoji_sponsor_slots", "Sponsored Emoji Slots", {
    hint: "Most sponsorships open at once, awaiting review or live. Never uses the server's last free emoji slot.",
    max: 200,
  }],
  ["emoji_sponsor_expire_days", "Review Window (days)", {
    hint: "Unreviewed submissions expire and refund after this many days. 0 leaves them queued forever.",
    max: 365,
  }],
];

// Custom shop items are curated on Shop & Perks, one price per item — the
// only figure that belongs to the whole feature is how long an order may sit
// on staff before it refunds itself, which is the same dial the sponsored
// QOTD, pin and theme queues have here.
const ITEM_FIELDS = [
  ["shop_item_expire_days", "Order Review Window (days)", {
    hint: "An order staff haven't delivered or declined refunds the buyer after this many days. 0 leaves orders waiting.",    max: 365,
  }],
];

// Live auctions (/bank auction start). Guard-rails rather than prices: a mod
// names the prize and the length, and these bound what any of them can do.
const AUCTION_FIELDS = [
  ["auction_min_bid", "Opening Bid", {
    hint: "The lowest first bid an auction will take. Minimum 1.",
    min: 1,
  }],
  ["auction_min_increment", "Minimum Raise", {
    hint: "The least each new bid must add to the standing high bid.",
    min: 1,
  }],
  ["auction_soft_close_seconds", "Anti-Snipe Window (seconds)", {
    hint: "A bid this close to the end resets the clock to this many seconds. 0 turns it off; 300 is five minutes.",
    max: 3600,
  }],
  ["auction_max_duration_hours", "Longest Auction (hours)", {
    hint: "Longest auction a mod can start, in hours, 1 to 168 (a week).",
    min: 1,
    max: 168,
  }],
];

const ALL_NUM_FIELDS = [
  ...PRICE_FIELDS, ...CONSUMABLE_FIELDS, ...EMOJI_FIELDS, ...RAFFLE_FIELDS,
  ...ITEM_FIELDS, ...AUCTION_FIELDS, ...DEMURRAGE_FIELDS,
];

// Every numeric field is capped somewhere so a typo can't create a price no
// member could ever pay; DEFAULT_MAX applies where the field has no natural
// ceiling of its own.

function fieldMax(opts) {
  return opts && opts.max != null ? opts.max : DEFAULT_MAX;
}

// Almost every dial here treats 0 as "off" or "free", so 0 is the floor by
// default. A field opts into a higher one when 0 is genuinely meaningless for
// it — a themed day cannot last zero hours — so the page can refuse it with a
// named message instead of letting the API answer with a 422.
function fieldMin(opts) {
  return opts && opts.min != null ? opts.min : 0;
}

/**
 * The Shop & Perks checkbox governing this price, or null if it has none.
 *
 * Only the eight switchable shop lines have one, and their keys line up by
 * construction (`price_role_icon` -> `shop_role_icon_enabled`). Anything else
 * — rakes, sponsor prices, auction guard-rails — is priced but not switchable,
 * and returns null so it renders exactly as before.
 */
function saleKeyFor(key) {
  const SWITCHABLE = new Set([
    "price_role_color", "price_role_name", "price_role_preset",
    "price_role_gradient", "price_role_holographic", "price_role_icon",
    "price_voice_style", "price_streak_shield",
  ]);
  return SWITCHABLE.has(key) ? `shop_${key.slice("price_".length)}_enabled` : null;
}

function numField(key, label, opts = {}, pricing, cfg) {
  const { hint } = opts;
  const hintHtml = hint ? `<div class="field-hint">${esc(hint)}</div>` : "";
  const suggested = pricing && pricing.hints ? pricing.hints[key] : null;
  const median = pricing ? Math.round(pricing.median || 0) : 0;
  const suggest = suggested != null
    ? `<div class="field-hint">Suggested: about ${suggested}, based on a median weekly income of ${median}.</div>`
    : "";
  // A read-only echo of the Shop & Perks checkbox, never a second control:
  // two places to switch one thing off is the ambiguity this feature removed.
  // Without it, a price sitting here for something nobody can buy reads as a
  // live setting, and the two pages quietly disagree.
  const saleKey = saleKeyFor(key);
  const offNote = saleKey && cfg && !cfg[saleKey]
    ? `<div class="field-hint"><strong>Not on sale.</strong> Members can’t buy this until
        you check it under What’s On Sale on <a href="#/economy-sinks">Shop &amp; Perks</a>.</div>`
    : "";
  return `
    <div class="field">
      <label for="sink-${key}">${esc(label)}</label>
      <input type="number" name="${key}" id="sink-${key}" required
        min="${fieldMin(opts)}" max="${fieldMax(opts)}" step="1" style="max-width:140px;" />
      ${offNote}
      ${hintHtml}
      ${suggest}
    </div>`;
}


export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading prices…</div></div>`;

  return mountAsync(container, async () => {
    // Two requests, not the old page's nine: the catalogs and the queues moved
    // out, so a slow icon-catalog fetch can no longer hold up the prices.
    const [cfg, metrics] = await Promise.all([
      api("/api/economy/config"),
      api("/api/economy/metrics").catch(() => null),
    ]);
    const pricing = metrics && metrics.hints && Object.keys(metrics.hints).length
      ? { hints: metrics.hints, median: metrics.median_income }
      : null;
    render(container, cfg, pricing);
    wirePrices(container, cfg);
  }, { errorMsg: "Couldn’t load the perk prices." });
}

function render(container, cfg, pricing) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Pricing</h2>
        <div class="subtitle">What every perk costs, and the rates that take currency
          back out. The things members buy are curated on
          <a href="#/economy-sinks">Shop &amp; Perks</a>; what they earn is set on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>
      ${economyOffBanner(cfg)}

      <form class="form form-cards" data-price-form>
        <div class="card">
          <div class="section-label">Perk Prices</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
          <div class="field" style="margin-top:8px;">
            <label style="display:flex;gap:6px;align-items:center;">
              <input type="checkbox" name="mod_perk_comp" /> Moderators get perks free
            </label>
            <div class="field-hint">Anyone with a mod or admin role, or Manage Server, gets
              every perk above free. Nothing is charged or recorded.</div>
          </div>
        </div>

        <div class="card">
          <div class="section-label">Consumables</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${CONSUMABLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
        </div>

        <div class="card">
          <div class="section-label">Weekly Raffle</div>
          <div class="field" style="margin-bottom:8px;">
            <label style="display:flex;gap:6px;align-items:center;">
              <input type="checkbox" name="raffle_enabled" /> Run a weekly raffle
            </label>
            <div class="field-hint">Lets members buy tickets and draws a winner at the
              weekly roll. Needs a ticket price above 0.</div>
          </div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            ${RAFFLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
          <div class="field-hint">
            The prize is a voucher for one perk payment, never coins. The winner is told
            by DM if they hold the Notifications Role.
          </div>
        </div>

        <div class="card">
          <div class="section-label">Hoard Tax and Rakes</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${DEMURRAGE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
          <div class="field-hint">
            The three rates start at 0, which is off. Collections show in the register
            feed, so announce a rate before setting one.
          </div>
        </div>

        <div class="card">
          <div class="section-label">Sponsored Emojis</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${EMOJI_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
        </div>

        <div class="card">
          <div class="section-label">Custom Shop Items</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${ITEM_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
          <div class="field-hint">
            Items and their prices are set on <a href="#/economy-sinks">Shop &amp; Perks</a>.
            Orders waiting on staff are worked from <a href="#/shop-approvals">Approvals</a>.
          </div>
        </div>

        <div class="card">
          <div class="section-label">Live Auctions</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${AUCTION_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing, cfg)).join("")}
          </div>
          <div class="field-hint">
            A mod opens one with <code>/bank auction start</code>. There is no on/off
            switch; these four bound every auction.
          </div>
        </div>

        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-price-status></span>
        </div>
      </form>
    </div>
  `;
}

function wirePrices(container, cfg) {
  const form = container.querySelector("[data-price-form]");
  const status = form.querySelector("[data-price-status]");
  for (const [key, , opts] of ALL_NUM_FIELDS) {
    // A value stored above a since-lowered ceiling (the auction length was
    // once allowed up to 720h) would fail the whole page's save; show it
    // clamped, which is what the bot already enforces.
    form.querySelector(`[name=${key}]`).value = Math.min(cfg[key], fieldMax(opts));
  }
  form.querySelector("[name=raffle_enabled]").checked = !!cfg.raffle_enabled;
  form.querySelector("[name=mod_perk_comp]").checked = !!cfg.mod_perk_comp;

  guardForm(form);

  guardedSubmit(form, async () => {
    const payload = {};
    // A blank box used to become NaN, then null, and came back as a raw
    // "Input should be a valid integer" naming no field. Check here and say
    // exactly which field is wrong (W-C5).
    for (const [key, label, opts] of ALL_NUM_FIELDS) {
      const input = form.querySelector(`[name=${key}]`);
      const max = fieldMax(opts);
      const min = fieldMin(opts);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < min || n > max) {
        showStatus(status, false, `${label} must be a whole number from ${min} to ${max}`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    payload.raffle_enabled = form.querySelector("[name=raffle_enabled]").checked;
    payload.mod_perk_comp = form.querySelector("[name=mod_perk_comp]").checked;
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
