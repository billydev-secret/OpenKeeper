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

// One control on purpose: the channel list is the on/off switch. An empty
// list guards nothing, so there is no separate Enable toggle to disagree
// with it.
export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading settings…</div></div>`;

  return mountAsync(
    container,
    async () => {
      const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
      const gg = config.gif_guard || { channel_ids: [] };

      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>GIF Guard</h2>
            <div class="subtitle">Keep GIFs out of the channels you pick</div>
          </header>
          ${renderMetaWarning()}
          <form class="form" data-form>
            <div class="field">
              <label>Channels Without GIFs</label>
              <span data-picker="channels"></span>
              <div class="field-hint"><strong>The bot needs Manage Messages.</strong> Any message with a
                GIF is deleted, threads included. None selected is off.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button type="submit" class="btn btn-primary">Save</button>
              <span data-status></span>
            </div>
          </form>
        </div>
      `;

      const picker = mountChannelMultiPicker(
        container.querySelector('[data-picker="channels"]'),
        channels,
        gg.channel_ids || [],
        { label: "Channels Without GIFs" },
      );

      const form = container.querySelector("[data-form]");
      const status = container.querySelector("[data-status]");
      guardForm(form);

      guardedSubmit(form, async () => {
        try {
          await apiPut("/api/config/gif-guard", { channel_ids: picker.getValues() });
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    },
    { errorMsg: "Couldn’t load the GIF Guard settings." },
  );
}
