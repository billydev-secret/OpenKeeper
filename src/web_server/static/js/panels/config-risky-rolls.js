import { mountGamePanel } from "./games-panel-shared.js";
import { mountRoleDialStates } from "../role-dial-state.js";
import {
  loadConfig,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountRolePicker,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const r = config.risky || {};
    // Stored in seconds, edited in minutes — the unit is in the field label.
    const minMinutes = Math.round((r.min_game_seconds || 0) / 60);
    const chaseHours = r.chase_hours || 0;
    const fallbackHours = r.fallback_hours || 0;
    // Special-roll odds: each is the N of "1 in N rolls", 0 = never, and
    // 100 is the flat die every guild starts on.
    const SPECIAL_ROLLS = [
      { face: 69, label: "69 — the roller asks the whole room",
        hint: "Rolling 69 wins outright and turns the question into one for everyone, in a thread." },
      { face: 100, label: "100 — the winner asks the bottom two",
        hint: "A winning 100 puts the question to both the lowest and second-lowest rollers." },
      { face: 1, label: "1 — two players ask the loser",
        hint: "A losing 1 lets both the highest and second-highest rollers ask the loser a question." },
    ];
    const oddsValue = (face) => {
      const n = r[`odds_${face}`];
      return Number.isInteger(n) ? n : 100;
    };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Risky Rolls</h2>
          <div class="subtitle">A dice game members start with <code>/risky start</code></div>
        </header>
        ${renderMetaWarning()}
        <div class="card" data-region="status"></div>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Announcements</div>
            <div class="field">
              <label>Ping Role</label>
              <span data-picker="ping_role_id"></span>
              <div class="field-hint">Pinged when a round opens. Saved as &ldquo;(none)&rdquo;, the next round with its ping on makes a Risky Rolls role.</div>
              <div data-role-state="risky_ping_role_id"></div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Round Rules</div>
            <div class="field">
              <label for="rr-min">Minimum Round Length (minutes)</label>
              <input type="number" name="min_game_minutes" id="rr-min" required
                min="0" max="1440" step="1" value="${minMinutes}" style="max-width:140px;" />
              <div class="field-hint">How long a round stays open before it can be closed, so latecomers can join. 0 means no minimum.</div>
            </div>
            <div class="field">
              <label for="rr-max">Rounds Running at Once, Per Channel</label>
              <input type="number" name="max_games_per_channel" id="rr-max" required
                min="1" max="100" step="1" value="${r.max_games_per_channel || 10}" style="max-width:140px;" />
              <div class="field-hint">Most open rounds in one channel. <code>/risky start</code> is refused past this until one closes.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Special Rolls</div>
            <div class="field-hint">Chance of each number on every roll, as 1 in N: 100 is a fair die, lower is likelier, 0 is never. Players aren&rsquo;t told.</div>
            ${SPECIAL_ROLLS.map(({ face, label, hint }) => `
              <div class="field">
                <label for="rr-odds-${face}">${label}</label>
                <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
                  <span>1 in</span>
                  <input type="number" name="odds_${face}" id="rr-odds-${face}" required
                    min="0" max="1000" step="1" value="${oddsValue(face)}" style="max-width:120px;" />
                  <span>rolls</span>
                </div>
                <div class="field-hint">${hint}
                  <strong data-odds-effect="${face}"></strong></div>
              </div>`).join("")}
          </div>

          <div class="card">
            <div class="section-label">The Payoff</div>
            <div class="field">
              <label for="rr-chase">Chase the winner's question after N hours</label>
              <input type="number" name="chase_hours" id="rr-chase" required
                min="0" max="168" step="1" value="${chaseHours}" style="max-width:140px;" />
              <div class="field-hint">Hours before the winner gets a reminder to ask; the answerer gets one this long after the question posts. 0 sends none.</div>
            </div>
            <div class="field">
              <label for="rr-fallback">Fall back to a bank question after N hours</label>
              <input type="number" name="fallback_hours" id="rr-fallback" required
                min="0" max="168" step="1" value="${fallbackHours}" style="max-width:140px;" />
              <div class="field-hint">Hours before the bot posts a bank Truth for a winner who never asked. Set it past the reminder; 0 is off.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    // The on/off switch lives in games_game_config, not the risky config row,
    // so it rides the shared game-panel status section like every other game.
    // The label matches the Global Config list this game appears in, and the
    // hint names both readers — /risky start and the scheduler.
    mountGamePanel(container.querySelector('[data-region="status"]'), {
      gameType: "risky_roll", gameName: "Risky Rolls", gameIcon: "🎰",
      hasBank: false, bare: true,
      statusLabel: "Available on This Server",
      statusHint: "Off: /risky start refuses to open a round and scheduled rounds are skipped. Running rounds finish normally.",
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector('[data-form] [data-status]');

    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="ping_role_id"]'),
      roles, String(r.ping_role_id || "0"), { label: "Ping Role" },
    );

    // What a dial means for a real round: the chance that at least one of
    // 15 rollers (a typical round) lands the number. For 100 and 1 the roll
    // must also top or bottom the round, which it nearly always does.
    const TYPICAL_PLAYERS = 15;
    const describeOdds = (n) => {
      if (!Number.isInteger(n) || (n !== 0 && (n < 5 || n > 1000))) {
        return "Use 0, or a number from 5 to 1000.";
      }
      if (n === 0) return "Never comes up.";
      const pct = Math.round(100 * (1 - Math.pow(1 - 1 / n, TYPICAL_PLAYERS)));
      return `About ${pct}% of ${TYPICAL_PLAYERS}-player rounds.`;
    };
    for (const { face } of SPECIAL_ROLLS) {
      const input = form.querySelector(`[name=odds_${face}]`);
      const effect = form.querySelector(`[data-odds-effect="${face}"]`);
      const refresh = () => { effect.textContent = describeOdds(parseInt(input.value, 10)); };
      input.addEventListener("input", refresh);
      refresh();
    }

    guardForm(form);
    mountRoleDialStates(container);

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const mins = parseInt(fd.get("min_game_minutes"), 10);
      if (!Number.isFinite(mins) || mins < 0 || mins > 1440) {
        showStatus(status, false, "Minimum Round Length must be a number of minutes from 0 to 1440");
        form.querySelector("[name=min_game_minutes]").focus();
        return;
      }
      const maxGames = parseInt(fd.get("max_games_per_channel"), 10);
      if (!Number.isFinite(maxGames) || maxGames < 1 || maxGames > 100) {
        showStatus(status, false, "Rounds Running at Once must be a number from 1 to 100");
        form.querySelector("[name=max_games_per_channel]").focus();
        return;
      }
      const hourFields = [
        ["chase_hours", "Chase the winner's question"],
        ["fallback_hours", "Fall back to a bank question"],
      ];
      const hours = {};
      for (const [name, label] of hourFields) {
        const value = parseInt(fd.get(name), 10);
        if (!Number.isFinite(value) || value < 0 || value > 168) {
          showStatus(status, false, `${label} must be a number of hours from 0 to 168`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        hours[name] = value;
      }
      const odds = {};
      for (const { face } of SPECIAL_ROLLS) {
        const value = parseInt(fd.get(`odds_${face}`), 10);
        if (!Number.isFinite(value) || (value !== 0 && (value < 5 || value > 1000))) {
          showStatus(status, false, `The odds for ${face} must be 0 (never) or a number from 5 to 1000`);
          form.querySelector(`[name=odds_${face}]`).focus();
          return;
        }
        odds[`odds_${face}`] = value;
      }
      try {
        await apiPut("/api/config/risky", {
          // Role id stays a string; minutes are still converted back to the
          // seconds the API stores.
          ping_role_id: rolePicker.getValue() || "0",
          min_game_seconds: mins * 60,
          max_games_per_channel: maxGames,
          chase_hours: hours.chase_hours,
          fallback_hours: hours.fallback_hours,
          ...odds,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the Risky Rolls settings." });
}
