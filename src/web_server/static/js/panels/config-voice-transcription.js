import {
  loadConfig, loadChannels,
  mountChannelMultiPicker,
  guardForm, renderMetaWarning,
  apiPost, apiPut, showStatus, esc,
  mountAsync,
  guardedSubmit,
} from "../config-helpers.js";

const MODELS = [
  { value: "base.en", label: "base.en — recommended, more accurate" },
  { value: "tiny.en", label: "tiny.en — faster, less accurate" },
];

function modelSelect(selected) {
  const sel = selected || "base.en";
  return MODELS.map(m =>
    `<option value="${m.value}" ${sel === m.value ? "selected" : ""}>${m.label}</option>`
  ).join("");
}

// Whisper model files live in a local cache the bot loads offline; a model
// that isn't cached can't be used until it's fetched. Render one row per model
// with its cache state and a Download button for any that are missing.
function modelsWidget(models) {
  const rows = (models || []).map(m => `
    <div class="vt-model-row" data-model="${esc(m.name)}"
         style="display:flex;align-items:center;gap:.6rem;padding:.35rem 0;flex-wrap:wrap">
      <code style="min-width:5.5rem">${esc(m.name)}</code>
      <span class="vt-model-state">${
        m.cached
          ? `<span style="color:var(--green-text)">✓ Downloaded</span>`
          : `<span style="color:var(--yellow-text)">Not downloaded</span>`
      }</span>
      <span style="flex:1"></span>
      <button type="button" class="btn btn-sm vt-dl-btn"
              ${m.cached ? "disabled" : ""}>Download</button>
    </div>`).join("");

  return `
    <div class="field">
      <label>Model Files</label>
      <div class="vt-models">${rows}</div>
      <div class="field-hint">Turning transcription on needs the chosen model to read <em>Downloaded</em>. base.en is about 150&nbsp;MB.</div>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const vt = config.voice_transcription || {};

    const unavailable = vt.available === false
      ? `<div class="field-hint" style="color:var(--yellow-text)">Nothing is transcribed: faster-whisper isn't installed on the bot's host. Ask the host to install it and restart.</div>`
      : "";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Voice Transcription</h2>
          <div class="subtitle">Turns Discord voice messages into text with a speech model that runs on your own server.</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>

          <div class="card">
            <div class="section-label">Transcription</div>
            ${unavailable}
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="enabled" ${vt.enabled ? "checked" : ""} />
                Transcribe voice messages
              </label>
              <div class="field-hint">Posts a text transcript under each voice message in the channels below.</div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="delete_after_transcribe" ${vt.delete_after_transcribe ? "checked" : ""} />
                Delete the voice message once transcribed
              </label>
              <div class="field-hint"><strong>Can't be undone.</strong> Removes the recording once its transcript posts. The bot needs Manage Messages there.</div>
            </div>
            <div class="field">
              <label>Channels to Transcribe</label>
              <span data-picker="channel_ids"></span>
              <div class="field-hint">Only voice messages in these channels are transcribed. Empty transcribes them everywhere.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Speech Model</div>
            <div class="field">
              <label for="vt-model">Model</label>
              <select name="model_name" id="vt-model">${modelSelect(vt.model_name)}</select>
              <div class="field-hint">base.en produces better transcripts; tiny.en
                finishes sooner on a slow host. Both understand English only.</div>
            </div>
            ${vt.available === false ? "" : modelsWidget(vt.models)}
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

    const channelsPicker = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_ids"]'),
      channels,
      vt.channel_ids,
      { label: "Channels to Transcribe" },
    );

    guardForm(form);

    // Per-model download buttons.
    container.querySelectorAll(".vt-model-row").forEach((row) => {
      const btn = row.querySelector(".vt-dl-btn");
      const state = row.querySelector(".vt-model-state");
      const modelName = row.dataset.model;
      btn?.addEventListener("click", async () => {
        btn.disabled = true;
        const prev = state.innerHTML;
        state.innerHTML = `<span style="color:var(--ink-mute)">Downloading…</span>`;
        try {
          const res = await apiPost("/api/config/voice-transcription/download", { model_name: modelName });
          if (res.cached) {
            state.innerHTML = `<span style="color:var(--green-text)">✓ Downloaded</span>`;
          } else {
            state.innerHTML = prev;
            btn.disabled = false;
          }
        } catch (err) {
          state.innerHTML = `<span style="color:var(--yellow-text)">${esc(err.message || "Download failed")}</span>`;
          btn.disabled = false;
        }
      });
    });

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/voice-transcription", {
          // Was an On/Off <select> posting "1"/"0"; the boolean sent to the
          // API is unchanged.
          enabled: form.querySelector('input[name="enabled"]').checked,
          model_name: fd.get("model_name"),
          // Still a list of id strings, exactly as the multi-select posted.
          channel_ids: channelsPicker.getValues(),
          delete_after_transcribe: form.querySelector('input[name="delete_after_transcribe"]').checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the voice transcription settings." });
}
