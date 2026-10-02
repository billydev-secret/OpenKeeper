import {
  loadConfig,
  loadChannels,
  mountChannelMultiPicker,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([
      loadConfig(),
      loadChannels(),
    ]);
    const g = config.greeting_watch || {
      enabled: false, channel_ids: "", window_minutes: 10,
      extra_words: "",
    };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Greeting Watch</h2>
          <div class="subtitle">Flags “good morning” and “hello” messages that go unanswered.</div>
        </header>
        ${renderMetaWarning()}
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${g.enabled ? "checked" : ""} /> Enable Greeting Watch</label>
            <div class="field-hint">Pings <strong>Staff · Greetings</strong> when a greeting goes unanswered. Needs a <a href="#/config-moderation">Staff Ping Channel</a> and a mod who has switched it on.</div>
          </div>
          <div class="field">
            <label>Watched Channels</label>
            <span data-picker="channels"></span>
            <div class="field-hint">Your “main chat” channel(s) to watch. Nothing selected means nothing is watched.</div>
          </div>
          <div class="field">
            <label for="configgreeti-extra-words">Extra Greeting Words</label>
            <input type="text" name="extra_words" placeholder="henlo, good yawn, o7" / id="configgreeti-extra-words">
            <div class="field-hint">Comma-separated in-jokes the built-in list misses. Matched at the start of a message. Empty uses only the built-ins.</div>
          </div>
          <div class="field">
            <label for="configgreeti-window-minutes">Unanswered Window (minutes)</label>
            <input type="number" name="window_minutes" min="1" max="180" value="${g.window_minutes}" required / id="configgreeti-window-minutes">
            <div class="field-hint">Minutes to wait before flagging a greeting. A reply, @mention, hello from someone else or reaction counts as an answer.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const channelsPicker = mountChannelMultiPicker(
      container.querySelector('[data-picker="channels"]'),
      channels,
      String(g.channel_ids || "").split(",").filter(Boolean),
      { label: "Watched Channels" },
    );

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    // Set via DOM, not the template string, so stored tokens containing
    // quotes or angle brackets can't break out of the value attribute.
    form.querySelector('input[name="extra_words"]').value = g.extra_words || "";
    guardForm(form);

    guardedSubmit(form, async () => {
      const window_minutes = parseInt(
        form.querySelector('input[name="window_minutes"]').value, 10,
      );
      if (!Number.isFinite(window_minutes) || window_minutes < 1 || window_minutes > 180) {
        showStatus(status, false, "Unanswered Window must be between 1 and 180 minutes.");
        return;
      }
      try {
        // Ids stay strings; the multi-picker serializes to the comma-joined
        // string the server's CSV field expects.
        await apiPut("/api/config/greeting-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_ids: channelsPicker.getValues().join(","),
          window_minutes,
          extra_words: form.querySelector('input[name="extra_words"]').value,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the greeting watch settings." });
}
