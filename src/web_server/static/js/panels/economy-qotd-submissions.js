// Economy — QOTD. The feature's two dials: the role a mod tags to open the
// day's question, and the spicy role that does the same in age-restricted
// channels. Admin-only (`mod: "locked"` in the nav), because the config
// GET/PUT behind it are.
//
// The page id still says "submissions" because ids are frozen: until 2026-09
// it also held the paid sponsored-question queue, which was retired with
// `/qotd post` (the only thing that drained it; it never sold one). Before
// that, in 2026-08 (IA2), it absorbed the retired `economy-qotd` page, and
// `MOVED_PAGES` still redirects that deep link here.
import { api, apiPut, esc } from "../api.js";
import { showStatus, loadRoles, mountRolePicker, mountAsync, guardedSubmit } from "../config-helpers.js";
import { mountRoleDialStates } from "../role-dial-state.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading QOTD…</div></div>`;
  return mountAsync(container, async () => {
    const [cfg, roles] = await Promise.all([api("/api/economy/config"), loadRoles()]);
    render(container, cfg, roles);
  }, { errorMsg: "Couldn’t load QOTD." });
}

// The spicy role is the server's own, outside the Bot-Managed Roles registry,
// so it has no data-role-state line. Say so here if the saved role is gone:
// spicy questions would otherwise stop registering with nothing on the page
// to show why. Skipped when the role list didn't load, so a failed fetch never
// reads as a deletion.
function spicyRoleGone(cfg, roles) {
  const id = String(cfg.qotd_spicy_role_id || "0");
  return id !== "0" && roles.length > 0 && !roles.some((r) => String(r.id) === id);
}

function render(container, cfg, roles) {
  const unit = cfg.reward_qotd === 1 ? cfg.currency_name : cfg.currency_plural;
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>QOTD</h2>
        <div class="subtitle">Question of the Day: the roles that open one.</div>
      </header>
      <form class="form card" data-form>
        <div class="section-label">The QOTD role</div>
        <div class="field">
          <label>QOTD role</label>
          <span data-picker="qotd_ping_role_id"></span>
          <div class="field-hint">Any message from a mod that tags this role becomes
            that day's question. <em>(none)</em> turns QOTD off.</div>
          <div class="field-hint"><strong>Must be mentionable</strong> in Discord, or the
            bot needs the Mention @everyone, @here, and All Roles permission.</div>
          <div data-role-state="econ_qotd_ping_role_id"></div>
        </div>
        <div class="field">
          <label>Spicy QOTD role</label>
          <span data-picker="qotd_spicy_role_id"></span>
          <div class="field-hint"><strong>Age-restricted channels only:</strong> a mod tagging it opens
            the day's spicy question. <em>(none)</em> turns it off.</div>
          <div class="field-hint">Also offers <strong>Staff · Spicy QOTD</strong> on Mod Settings. The bot
            never creates this role.</div>
          ${spicyRoleGone(cfg, roles) ? `<div class="field-hint"><span class="badge badge-danger">Deleted</span>
            This role no longer exists, so spicy questions can't open. Pick
            another, or <em>(none)</em>.</div>` : ""}
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status></span>
        </div>
      </form>

      <section class="card">
        <div class="section-label">How It Works</div>
        <div class="field-hint">
          Each reply to the question pays <strong>${cfg.reward_qotd}</strong> ${esc(unit)},
          once per question, until the day rolls over. Change it on
          <a href="#/economy-income-sources">Income Sources</a>.
        </div>
      </section>
    </div>`;

  const form = container.querySelector("[data-form]");
  const status = form.querySelector("[data-status]");
  const pingRolePicker = mountRolePicker(
    form.querySelector('[data-picker="qotd_ping_role_id"]'),
    roles,
    String(cfg.qotd_ping_role_id),
  );
  const spicyRolePicker = mountRolePicker(
    form.querySelector('[data-picker="qotd_spicy_role_id"]'),
    roles,
    String(cfg.qotd_spicy_role_id),
  );
  // Whether that "(none)" is a decision, a blank, or a role that's been
  // deleted — same source as the Bot-Managed Roles page.
  mountRoleDialStates(container);
  guardedSubmit(form, async () => {
    try {
      await apiPut("/api/economy/config", {
        // String, not parseInt: a 19-digit snowflake loses its low digits as a
        // JS number. Pydantic coerces it back to int losslessly.
        qotd_ping_role_id: pingRolePicker.getValue() || "0",
        qotd_spicy_role_id: spicyRolePicker.getValue() || "0",
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
