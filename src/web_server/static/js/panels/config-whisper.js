import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRolePicker,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadRoles(),
    ]);
    const w = config.whisper;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Whisper</h2>
          <div class="subtitle">Lets members send each other anonymous notes through Dungeon Keeper</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Where Whispers Go</div>
            <div class="field">
              <label>Whisper Channel</label>
              <span data-picker="channel_id"></span>
              <div class="field-hint">Holds the Send panel and feed, and no post names
                the sender. "(disabled)" turns Whisper off.</div>
            </div>
            <div class="field">
              <label>Log Channel</label>
              <span data-picker="log_channel_id"></span>
              <div class="field-hint"><strong>Staff only.</strong> Mirrors each reply
                and report, naming the sender. "(disabled)" turns the mirror off.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Who Can Whisper</div>
            <div class="field">
              <label>Required Role</label>
              <span data-picker="role_id"></span>
              <div class="field-hint">The opt-in role <code>/whisper optin</code>
                grants. Only holders can send or receive. "(none)" turns Whisper
                off.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">The Game</div>
            <div class="field">
              <label for="wh-guesses">Guesses Per Whisper</label>
              <input type="number" name="guesses_per_whisper" id="wh-guesses" required
                min="1" max="10" step="1" value="${w.guesses_per_whisper ?? 3}"
                style="max-width:140px;" />
              <div class="field-hint">Tries the recipient gets to name the sender. A
                change affects only whispers sent after you save.</div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="sender_feedback" ${w.sender_feedback ? "checked" : ""} />
                Tell the sender how the guessing goes
              </label>
              <div class="field-hint">DMs the sender a summary of the guesses on their
                whisper. Senders who opted out of Whisper get none.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Rate Limits</div>
            <div class="field">
              <label for="wh-cooldown">Wait Between Whispers (seconds)</label>
              <input type="number" name="cooldown_seconds" id="wh-cooldown" required
                min="0" max="86400" step="1" value="${w.cooldown_seconds ?? 30}"
                style="max-width:140px;" />
              <div class="field-hint">Seconds a member must wait between whispers. 0
                removes the wait and invites spam.</div>
            </div>
            <div class="field">
              <label for="wh-cap">Whispers Per Hour to the Same Person</label>
              <input type="number" name="hourly_cap_per_target" id="wh-cap" required
                min="1" max="1000" step="1" value="${w.hourly_cap_per_target ?? 5}"
                style="max-width:140px;" />
              <div class="field-hint">Most whispers one member can send the same person
                per hour. Keep it low to limit anonymous harassment.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const channelPicker = mountChannelPicker(
      form.querySelector('[data-picker="channel_id"]'),
      channels, String(w.channel_id || "0"), { label: "Whisper Channel" },
    );
    const logChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="log_channel_id"]'),
      channels, String(w.log_channel_id || "0"), { label: "Log Channel" },
    );
    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="role_id"]'),
      roles, String(w.role_id || "0"), { label: "Required Role" },
    );

    guardForm(form);

    guardedSubmit(form, async () => {
      const fd = new FormData(form);

      const cooldown = parseInt(fd.get("cooldown_seconds"), 10);
      if (!Number.isFinite(cooldown) || cooldown < 0 || cooldown > 86400) {
        showStatus(status, false, "Wait Between Whispers must be a number of seconds from 0 to 86400");
        form.querySelector("[name=cooldown_seconds]").focus();
        return;
      }
      const cap = parseInt(fd.get("hourly_cap_per_target"), 10);
      if (!Number.isFinite(cap) || cap < 1 || cap > 1000) {
        showStatus(status, false, "Whispers Per Hour to the Same Person must be a number from 1 to 1000");
        form.querySelector("[name=hourly_cap_per_target]").focus();
        return;
      }

      const guesses = parseInt(fd.get("guesses_per_whisper"), 10);
      if (!Number.isFinite(guesses) || guesses < 1 || guesses > 10) {
        showStatus(status, false, "Guesses Per Whisper must be a number from 1 to 10");
        form.querySelector("[name=guesses_per_whisper]").focus();
        return;
      }

      try {
        await apiPut("/api/config/whisper", {
          // Ids stay strings, same values the plain selects posted.
          channel_id: channelPicker.getValue() || "0",
          role_id: rolePicker.getValue() || "0",
          log_channel_id: logChannelPicker.getValue() || "0",
          cooldown_seconds: cooldown,
          hourly_cap_per_target: cap,
          guesses_per_whisper: guesses,
          sender_feedback: fd.get("sender_feedback") === "on",
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the whisper settings." });
}
