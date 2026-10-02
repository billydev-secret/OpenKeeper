import { esc } from "../api.js";
import { mountGamePanel } from "./games-panel-shared.js";
import {
  loadConfig, loadChannels, mountChannelMultiPicker, apiPut, showStatus,
  guardForm, renderMetaWarning,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";

/**
 * The shared config panel behind the six nickname-stakes duel and lobby games
 * (Chicken, Hot Potato, Hot Potato Group, Musical Chairs, Pressure Cooker,
 * Quickdraw). Each Config > Games route keeps its own module and id (route ids
 * are frozen); the module is only a spec handed to `mountDuelPanel`.
 *
 * Every one of these games has the same Forfeit and Availability cards, and
 * differs in the cards above them and in a few cross-field rules. The server
 * side is the same shape: `_DUEL_GAMES` in routes/config.py.
 *
 * spec:
 *   id          config section key and API tail, e.g. "hot_potato" (the section
 *               is `games_<id>`, the route `/api/config/games-<id with dashes>`)
 *   name        display name, also the game-status card's name
 *   subtitle    the one line under the heading
 *   lobby       true for a game with a lobby and a host, false for a challenge
 *               duel; picks the Forfeit/Availability wording
 *   cards       [{ title, fields: [field...] }] shown above Forfeit. A field is
 *               { name, label, hint, min, max, step? } for a number (a `step`
 *               makes it a decimal) or { name, label, hint, checkbox: true }
 *   checks      [{ ordered: [lo, hi], focus, msg }] fails when payload[hi] is
 *               below payload[lo]; [{ below: [a, b], focus, msg }] fails when
 *               payload[a] is not strictly below payload[b]. The first failing
 *               check wins, in order, and focuses the field named by `focus`.
 */

const numField = ({ name, label, hint, min, max, step = "1" }, cfg) => `
  <div class="field">
    <label for="gc-${name}">${esc(label)}</label>
    <input type="number" name="${name}" id="gc-${name}" required
      min="${min}" max="${max}" step="${step}" value="${esc(cfg[name] ?? "")}" style="max-width:140px;" />
    <div class="field-hint">${esc(hint)}</div>
  </div>`;

const checkboxField = ({ name, label, hint }, cfg) => `
  <div class="field">
    <label style="display:flex; gap:6px; align-items:center;">
      <input type="checkbox" name="${name}" ${cfg[name] ? "checked" : ""} />
      ${esc(label)}
    </label>
    <div class="field-hint">${esc(hint)}</div>
  </div>`;

const fieldHTML = (f, cfg) => (f.checkbox ? checkboxField(f, cfg) : numField(f, cfg));

// The Forfeit and Availability cards every one of these games shares. Only the
// wording that depends on "lobby with a host" vs "challenge between two" moves.
function sharedFields(lobby) {
  const forfeit = [
    { name: "sentence_hours", label: "Nickname Lasts (hours)", min: 1, max: 8760,
      hint: "How long the loser wears the nickname before it's removed automatically." },
    { name: "max_nick_length", label: "Longest Nickname (characters)", min: 1, max: 32,
      hint: "Longer nicknames are refused. Discord's own cap is 32 characters." },
    { name: "max_stakes_length", label: "Longest Stakes Text (characters)", min: 1, max: 2000,
      hint: lobby
        ? "Longer stakes text is refused when a host opens a lobby."
        : "Longer stakes text is refused when someone issues a challenge." },
  ];
  const availability = lobby
    ? [
      { name: "cooldown_hours", label: "Wait Between Games (hours)", min: 0, max: 8760,
        hint: "Players wait this long between nickname games; 0 is no wait. Wagers and custom stakes are never held back." },
      { name: "challenge_limit_per_hour", label: "Games Started Per Person Per Hour", min: 0, max: 999,
        hint: "Games one person can open per hour; 0 is no limit. A spam brake, so keep it high on busy nights." },
    ]
    : [
      { name: "cooldown_hours", label: "Wait Before a Rematch (hours)", min: 0, max: 8760,
        hint: "The same pair can't stake nicknames again for this long; 0 is no wait. Wagers and custom stakes are never held back." },
      { name: "challenge_limit_per_hour", label: "Challenges Per Person Per Hour", min: 0, max: 999,
        hint: "Challenges one person can start per hour; 0 is no limit. A spam brake, so keep it high on busy nights." },
    ];
  return { forfeit, availability };
}

// A check reads the validated payload and returns true when it is violated.
const CHECK_KINDS = {
  // `ordered: [lo, hi]`: hi may not be below lo.
  ordered: ([lo, hi], p) => p[hi] < p[lo],
  // `below: [a, b]`: a has to be strictly less than b.
  below: ([a, b], p) => p[a] >= p[b],
};

const stripUnit = (label) => label.replace(/\s*\([^)]*\)\s*$/, "");

export function mountDuelPanel(container, spec) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    // `|| {}`: a config payload missing this section (a fresh guild, a partial
    // response) used to throw on the first cfg.<field> read, and the panel hung
    // on "Loading configuration…" forever. Undefined fields render blank instead.
    const cfg = config[`games_${spec.id}`] || {};
    const { forfeit, availability } = sharedFields(spec.lobby);
    const cards = [
      ...(spec.cards || []),
      { title: "Forfeit", fields: forfeit, denylist: true },
      { title: "Availability", fields: availability, channels: true },
    ];
    const numbered = cards.flatMap((c) => c.fields).filter((f) => !f.checkbox);
    const boxes = cards.flatMap((c) => c.fields).filter((f) => f.checkbox);

    const cardHTML = (c) => `
          <div class="card">
            <div class="section-label">${esc(c.title)}</div>
            ${c.fields.map((f) => fieldHTML(f, cfg)).join("")}
            ${c.denylist ? `
            <div class="field">
              <label for="gc-nick_denylist">Extra Banned Words</label>
              <input type="text" name="nick_denylist" id="gc-nick_denylist"
                value="${esc((cfg.nick_denylist || []).join(", "))}"
                style="width:100%;max-width:420px;box-sizing:border-box;" />
              <div class="field-hint">Comma-separated words refused in nicknames and stakes text, on top of the built-in slur list. Whole words only.</div>
            </div>` : ""}
            ${c.channels ? `
            <div class="field">
              <label>Allowed Channels</label>
              <div data-picker="channel_allowlist"></div>
              <div class="field-hint">Empty means <strong>every channel</strong>. Unlike party games, this ignores the Games &rsaquo; Global Config channel list.</div>
            </div>` : ""}
          </div>`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>${esc(spec.name)}</h2>
          <div class="subtitle">${esc(spec.subtitle)}</div>
        </header>
        <div data-region="status"></div>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          ${cards.map(cardHTML).join("\n")}

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    mountGamePanel(container.querySelector('[data-region="status"]'), {
      gameType: spec.id,
      gameName: spec.name,
      bare: true,
      statusHint: `Off: nobody can start a new ${spec.name} game. Running games finish normally.`,
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const allowlist = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_allowlist"]'), channels, cfg.channel_allowlist,
      { label: "Allowed Channels" },
    );

    guardForm(form);

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const payload = { channel_allowlist: allowlist.getValues() };
      for (const b of boxes) {
        payload[b.name] = form.querySelector(`input[name="${b.name}"]`).checked;
      }
      payload.nick_denylist = String(fd.get("nick_denylist") || "")
        .split(",").map((s) => s.trim()).filter(Boolean);
      for (const f of numbered) {
        const isFloat = f.step !== undefined;
        const n = isFloat ? parseFloat(fd.get(f.name)) : parseInt(fd.get(f.name), 10);
        if (!Number.isFinite(n) || n < f.min || n > f.max) {
          showStatus(status, false, `${stripUnit(f.label)} must be a number from ${f.min} to ${f.max}`);
          form.querySelector(`[name=${f.name}]`).focus();
          return;
        }
        payload[f.name] = n;
      }
      for (const check of spec.checks || []) {
        const kind = Object.keys(CHECK_KINDS).find((k) => check[k]);
        if (CHECK_KINDS[kind](check[kind], payload)) {
          showStatus(status, false, check.msg);
          form.querySelector(`[name=${check.focus}]`).focus();
          return;
        }
      }
      try {
        await apiPut(`/api/config/games-${spec.id.replaceAll("_", "-")}`, payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: `Couldn’t load the ${spec.name} settings.` });
}
