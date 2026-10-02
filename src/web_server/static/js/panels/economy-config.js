import { api, esc } from "../api.js";
import {
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  loadChannels,
  loadRoles,
  loadMembers,
  mountMemberPicker,
  mountChannelPicker,
  mountRolePicker,
  mountAsync,
  onPickerChange,
  guardedSubmit,
} from "../config-helpers.js";
import { mountPanelPoster } from "../panel-post.js";
import { mountRoleDialStates } from "../role-dial-state.js";

// Faucet rates are edited on the Income Sources page and perk-shop prices on the
// Sinks page — this page keeps the wiring and branding.
//
// All three economy channel panels are posted from here rather than from the
// page that sets what each one shows: they're the same action three times over,
// and an admin placing them does it once, in one sitting, when setting the
// economy up. There were four until 2026-08-18, when the how-to guide folded
// into the leaderboard panel — one panel, with the guide behind an ❓ button.

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [cfg, channels, roles, members] = await Promise.all([
      api("/api/economy/config"),
      loadChannels(),
      loadRoles(),
      loadMembers(),
    ]);
    render(container, cfg, channels, roles, members);
  }, { errorMsg: "Couldn’t load the economy settings." });
}

function render(container, cfg, channels, roles, members) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Economy Settings</h2>
        <div class="subtitle">Where the economy lives and what the currency is called.
          Prices are set on <a href="#/pricing">Pricing</a>, what members can buy on
          <a href="#/economy-sinks">Shop &amp; Perks</a>, and earnings on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>
      ${renderMetaWarning()}

      <form class="form form-cards" data-form>
        <div class="card">
        <div class="section-label">Core</div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="enabled"${cfg.enabled ? " checked" : ""} />
            Run an economy on this server
          </label>
          <div class="field-hint">Turns earning and spending on or off. Balances are kept while it&rsquo;s off.</div>
        </div>
        <div class="field">
          <label>Bank Channel</label>
          <span data-picker="bank_channel_id"></span>
          <div class="field-hint">Public economy notices, such as a role-slot shortage or a DM the bot couldn&rsquo;t deliver. Unset, they go unsent.</div>
        </div>
        <div class="field">
          <label>Paid Request Reviews</label>
          <span data-picker="approvals_channel_id"></span>
          <div class="field-hint"><strong>Staff only.</strong> Approve/decline cards for paid requests, quoting unreviewed text. Unset, they stay on the todo board.</div>
        </div>
        <div class="field">
          <label>Register Channel</label>
          <span data-picker="register_channel_id"></span>
          <div class="field-hint">A feed of every currency movement and what it was for. Unset turns it off; past entries are never replayed.</div>
        </div>
        <div class="field">
          <label>Default Channel</label>
          <span data-picker="default_channel_id"></span>
          <div class="field-hint">Fallback for the Coin Drops, Pin of the Day, Themed Days and Bounty channels. Setting it can turn drops and bounties on.</div>
        </div>
        <div class="field">
          <label>Manager Role</label>
          <span data-picker="manager_role_id"></span>
          <div class="field-hint">Can grant and remove currency and approve paid requests, so keep it small. &ldquo;(none)&rdquo; leaves this to admins.</div>
        </div>
        <div class="field">
          <label>Notifications Role</label>
          <span data-picker="game_role_id"></span>
          <div class="field-hint">The role the 🔔 button hands out, for quest and streak DMs. Saved as <em>(none)</em>, notifications are off.</div>
          <div data-role-state="econ_game_role_id"></div>
        </div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="login_card_live_updates"${cfg.login_card_live_updates ? " checked" : ""} />
            Keep the daily streak DM up to date
          </label>
          <div class="field-hint">Updates the morning streak DM as quests are finished. It edits in place and never pings twice.</div>
        </div>
        <div class="field">
          <label>Community Weekly Host</label>
          <span data-picker="community_host_user_id"></span>
          <div class="field-hint">Gets the community-weekly beat sheets by DM to post in their own voice. Empty sends them to the server owner.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-set-daily">Daily Clean-Sweep Bonus</label>
            <input type="number" name="quest_set_bonus_daily" id="ec-set-daily" required
              min="0" max="1000000" step="1"
              value="${cfg.quest_set_bonus_daily}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-set-weekly">Weekly Clean-Sweep Bonus</label>
            <input type="number" name="quest_set_bonus_weekly" id="ec-set-weekly" required
              min="0" max="1000000" step="1"
              value="${cfg.quest_set_bonus_weekly}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Extra coins for finishing every quest on a member&rsquo;s board that day or week. 0 turns it off.</div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="transfers_enabled"${cfg.transfers_enabled ? " checked" : ""} />
            Let members send currency to each other
          </label>
          <div class="field-hint">Lets members send currency to each other. Off, it only moves through payouts and purchases.</div>
        </div>
        <div class="field">
          <label for="ec-booster">Booster Multiplier</label>
          <input type="number" name="booster_multiplier" id="ec-booster" required
            value="${cfg.booster_multiplier}" min="1" max="10" step="0.1" style="max-width:140px;" />
          <div class="field-hint">Multiplies everything a booster earns. 1 is no bonus; 1.5 earns half as much again.</div>
        </div>

        </div>

        <div class="card">
        <div class="section-label">Coin Drops</div>
        <div class="field">
          <label>Drop Channel</label>
          <span data-picker="drops_channel_id"></span>
          <div class="field-hint">Where coin pouches drop for the first member to claim. Unset uses the Default Channel; &ldquo;(disabled)&rdquo; keeps drops off.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-drop-min">Smallest Drop (coins)</label>
            <input type="number" name="drops_min_coins" id="ec-drop-min" required
              min="0" max="1000000" step="1"
              value="${cfg.drops_min_coins}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-drop-max">Largest Drop (coins)</label>
            <input type="number" name="drops_max_coins" id="ec-drop-max" required
              min="0" max="1000000" step="1"
              value="${cfg.drops_max_coins}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-drop-per-day">Drops Per Day (average)</label>
            <input type="number" name="drops_per_day" id="ec-drop-per-day" required
              min="0" max="48" step="1"
              value="${cfg.drops_per_day}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-drop-expire">Expires After (minutes)</label>
            <input type="number" name="drops_expire_minutes" id="ec-drop-expire" required
              min="1" max="1440" step="1"
              value="${cfg.drops_expire_minutes}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Each pouch pays a random amount in this range. Drops per day is an average, and an unclaimed pouch expires unpaid.</div>

        </div>

        <div class="card">
        <div class="section-label">Pin of the Day</div>
        <div class="field">
          <label>Pin Channel</label>
          <span data-picker="pin_channel_id"></span>
          <div class="field-hint">Paid, mod-approved pins that last 24 hours; needs a price on <a href="#/pricing">Pricing</a>. The bot needs Manage Messages here.</div>
        </div>

        </div>

        <div class="card">
        <div class="section-label">Themed Days</div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="flash_theme_enabled"${cfg.flash_theme_enabled ? " checked" : ""} />
            Sell themed days
          </label>
          <div class="field-hint">Sells themed days. A price of 0 makes them free. Set the price and day length on <a href="#/pricing">Pricing</a>.</div>
        </div>
        <div class="field">
          <label>Theme Channel</label>
          <span data-picker="theme_channel_id"></span>
          <div class="field-hint">Where each theme is announced and pinned; needs Manage Messages. Unset uses the Default Channel.</div>
        </div>
        </div>

        <div class="card">
        <div class="section-label">Community Bounty</div>
        <div class="field">
          <label>Bounty Board Channel</label>
          <span data-picker="bounty_channel_id"></span>
          <div class="field-hint">Home of the Bounty Board panel and its cards. Unset uses the Default Channel; &ldquo;(disabled)&rdquo; keeps bounties off.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-bounty-min">Smallest Contribution (coins)</label>
            <input type="number" name="bounty_min_stake" id="ec-bounty-min" required
              min="1" max="1000000" step="1"
              value="${cfg.bounty_min_stake}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-bounty-open">Open Bounties Per Member</label>
            <input type="number" name="bounty_max_open" id="ec-bounty-open" required
              min="0" max="1000" step="1"
              value="${cfg.bounty_max_open}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-bounty-expire">Expires After (days)</label>
            <input type="number" name="bounty_expire_days" id="ec-bounty-expire" required
              min="0" max="365" step="1"
              value="${cfg.bounty_expire_days}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">The minimum applies to opening and chipping in. 0 means no limit, or no expiry. Expired bounties refund in full.</div>
        </div>

        <div class="card">
        <div class="section-label">Branding</div>
        <div class="field-row">
          <div class="field">
            <label for="ec-cur-name">Currency Name (one)</label>
            <input type="text" name="currency_name" id="ec-cur-name" value="${esc(cfg.currency_name)}" maxlength="32" placeholder="e.g. coin" />
          </div>
          <div class="field">
            <label for="ec-cur-plural">Currency Name (many)</label>
            <input type="text" name="currency_plural" id="ec-cur-plural" value="${esc(cfg.currency_plural)}" maxlength="32" placeholder="e.g. coins" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Used in every balance, price and payout message.</div>
        <div class="field-row">
          <div class="field">
            <label for="ec-cur-emoji">Currency Emoji</label>
            <input type="text" name="currency_emoji" id="ec-cur-emoji" value="${esc(cfg.currency_emoji)}" maxlength="64" />
            <div class="field-hint">Shown next to every amount. A standard emoji or one of this server&rsquo;s own.</div>
          </div>
          <div class="field">
            <label for="ec-wallet">Wallet Name</label>
            <input type="text" name="wallet_name" id="ec-wallet" value="${esc(cfg.wallet_name)}" maxlength="32" placeholder="e.g. wallet" />
            <div class="field-hint">What members&rsquo; balance is called, such as wallet, purse or vault.</div>
          </div>
        </div>
        <div class="field">
          <label for="ec-icon-url">Currency Icon Address</label>
          <input type="text" name="currency_icon_url" id="ec-icon-url" value="${esc(cfg.currency_icon_url)}" maxlength="512" placeholder="https://example.com/coin.png" />
          <div class="field-hint">An https:// image address for the economy cards&rsquo; thumbnail. Leave empty for none.</div>
        </div>
        </div>

        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status></span>
        </div>
      </form>

      <div class="card">
        <div class="section-label">Post to Discord</div>
        <div class="field-hint" style="margin-bottom:10px;">Re-posting to the same channel refreshes a panel in place. Each needs the economy switched on.</div>
        <div class="field" data-poster="economy-panel"></div>
        <div class="field" data-poster="economy-shop"></div>
        <div class="field" data-poster="economy-bounty"></div>
      </div>
    </div>`;

  // Spelled out rather than looped over a key list: tests/panels/test_panel_registry.py
  // reads these call sites to check every registered panel is actually drawn
  // somewhere, and a computed key is invisible to it.
  const slot = (key) => container.querySelector(`[data-poster="${key}"]`);
  mountPanelPoster(slot("economy-panel"), "economy-panel");
  mountPanelPoster(slot("economy-shop"), "economy-shop");
  mountPanelPoster(slot("economy-bounty"), "economy-bounty");

  const form = container.querySelector("[data-form]");
  const status = form.querySelector("[data-status]");

  const channelPicker = mountChannelPicker(
    form.querySelector('[data-picker="bank_channel_id"]'),
    channels,
    String(cfg.bank_channel_id),
    { label: "Bank Channel" },
  );
  const registerChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="register_channel_id"]'),
    channels,
    String(cfg.register_channel_id),
    { label: "Register Channel" },
  );
  const defaultChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="default_channel_id"]'),
    channels,
    String(cfg.default_channel_id),
    { label: "Default Channel" },
  );
  // A channel picker for one of the four fields below that falls back to the
  // Default Channel above while its own saved value is unset (0) — and keeps
  // following the Default Channel picker live as it changes, until the admin
  // touches this one directly (including picking "(disabled)" to opt out).
  // A field that already has a real saved value never follows: that's what
  // keeps an existing configuration posting exactly where it posts today.
  function mountDefaultingChannelPicker(key, label) {
    const raw = String(cfg[key] || "0");
    const followsDefault = raw === "0";
    let lastFollowed = followsDefault && defaultChannelPicker.getValue() !== "0"
      ? defaultChannelPicker.getValue()
      : raw;
    const picker = mountChannelPicker(
      form.querySelector(`[data-picker="${key}"]`),
      channels,
      lastFollowed,
      { label },
    );
    if (followsDefault) {
      let following = true;
      picker.el.addEventListener("focusout", () => {
        setTimeout(() => {
          if (following && picker.getValue() !== lastFollowed) following = false;
        }, 200);
      });
      onPickerChange(defaultChannelPicker, () => {
        if (!following) return;
        lastFollowed = defaultChannelPicker.getValue() || "0";
        picker.setValue(lastFollowed);
      });
    }
    return picker;
  }
  const approvalsChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="approvals_channel_id"]'),
    channels,
    String(cfg.approvals_channel_id),
    { label: "Paid Request Reviews" },
  );
  const rolePicker = mountRolePicker(
    form.querySelector('[data-picker="manager_role_id"]'),
    roles,
    String(cfg.manager_role_id),
    { label: "Manager Role" },
  );
  const gameRolePicker = mountRolePicker(
    form.querySelector('[data-picker="game_role_id"]'),
    roles,
    String(cfg.game_role_id),
    { label: "Notifications Role" },
  );
  const dropsChannelPicker = mountDefaultingChannelPicker("drops_channel_id", "Drop Channel");
  const pinChannelPicker = mountDefaultingChannelPicker("pin_channel_id", "Pin Channel");
  const themeChannelPicker = mountDefaultingChannelPicker("theme_channel_id", "Theme Channel");
  const bountyChannelPicker = mountDefaultingChannelPicker("bounty_channel_id", "Bounty Board Channel");
  // mountMemberPicker rather than a bare mountPicker: /api/meta/members is a
  // bounded page now, and this field holds a saved id. The helper wires the
  // server-side search (so anyone is still pickable) and resolves a saved host
  // who has since left, which would otherwise show as a bare snowflake.
  const hostPicker = mountMemberPicker(
    form.querySelector('[data-picker="community_host_user_id"]'),
    members,
    String(cfg.community_host_user_id || "0"),
    {
      emptyLabel: "(server owner)",
      placeholder: "Search members…",
      label: "Community Weekly Host",
    },
  );

  guardForm(form);
  // "(none)" on this dial is now honoured (2026-09-03), so the admin has to be
  // able to see which "(none)" they are looking at.
  mountRoleDialStates(container);

  // [name, visible label, min, max] — a blank box used to post NaN and come
  // back as a raw 422 naming no field (W-C5).
  const numKeys = [
    ["booster_multiplier", "Booster Multiplier", 1, 10],
    ["quest_set_bonus_daily", "Daily Clean-Sweep Bonus", 0, 1000000],
    ["quest_set_bonus_weekly", "Weekly Clean-Sweep Bonus", 0, 1000000],
    ["drops_min_coins", "Smallest Drop", 0, 1000000],
    ["drops_max_coins", "Largest Drop", 0, 1000000],
    ["drops_per_day", "Drops Per Day", 0, 48],
    ["drops_expire_minutes", "Expires After (drops)", 1, 1440],
    ["bounty_min_stake", "Smallest Contribution", 1, 1000000],
    ["bounty_max_open", "Open Bounties Per Member", 0, 1000],
    ["bounty_expire_days", "Expires After (bounties)", 0, 365],
  ];
  const floatKeys = new Set(["booster_multiplier"]);
  const strKeys = [
    "currency_name",
    "currency_plural",
    "currency_emoji",
    "currency_icon_url",
    "wallet_name",
  ];

  guardedSubmit(form, async () => {
    const payload = {
      enabled: form.querySelector("[name=enabled]").checked,
      transfers_enabled: form.querySelector("[name=transfers_enabled]").checked,
      login_card_live_updates: form.querySelector("[name=login_card_live_updates]").checked,
      flash_theme_enabled: form.querySelector("[name=flash_theme_enabled]").checked,
      // All snowflakes go as strings: parseInt on a 19-digit id silently
      // rounds it (parseInt("1526000000000000013") === 1526000000000000000),
      // which repoints the setting at a role/channel that doesn't exist.
      // Pydantic coerces the string to int losslessly server-side.
      bank_channel_id: channelPicker.getValue() || "0",
      register_channel_id: registerChannelPicker.getValue() || "0",
      default_channel_id: defaultChannelPicker.getValue() || "0",
      approvals_channel_id: approvalsChannelPicker.getValue() || "0",
      drops_channel_id: dropsChannelPicker.getValue() || "0",
      pin_channel_id: pinChannelPicker.getValue() || "0",
      theme_channel_id: themeChannelPicker.getValue() || "0",
      bounty_channel_id: bountyChannelPicker.getValue() || "0",
      manager_role_id: rolePicker.getValue() || "0",
      game_role_id: gameRolePicker.getValue() || "0",
      community_host_user_id: hostPicker.getValue() || "0",
    };
    for (const [key, label, min, max] of numKeys) {
      const input = form.querySelector(`[name=${key}]`);
      const n = floatKeys.has(key) ? parseFloat(input.value) : parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < min || n > max) {
        showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    if (payload.drops_max_coins < payload.drops_min_coins) {
      showStatus(status, false, "Largest Drop cannot be smaller than Smallest Drop");
      form.querySelector("[name=drops_max_coins]").focus();
      return;
    }
    for (const key of strKeys) {
      payload[key] = form.querySelector(`[name=${key}]`).value;
    }
    try {
      const res = await apiPut("/api/economy/config", payload);
      // The save succeeded either way, so report success FIRST — that is what
      // disarms the unsaved-edits guard for this form. A warning (the Paid
      // Request Reviews channel it just stored is readable by @everyone) then
      // replaces the "Saved" line without re-arming it. It is shown rather
      // than blocking, because whether that's acceptable is the admin's call,
      // not the bot's.
      showStatus(status, true);
      if (res && res.warning) showStatus(status, false, res.warning);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
