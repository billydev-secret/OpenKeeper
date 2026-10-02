import { apiPost } from "../api.js";
import { loadConfig, apiPut, apiDelete, showStatus, escapeHtml, guardForm, mountAsync, guardedSubmit } from "../config-helpers.js";

const DEFAULT_ACCENT = "#5865F2";

// The images that frame the channel hub panels, the small strip that tops a
// game lobby, and the thin untitled pair that frames the other public cards.
// Each is its own form with
// its own Upload/Remove, so one never waits on (or discards) the other.
const ART_SLOTS = [
  {
    key: "banner",
    field: "panel_banner_url",
    label: "Banner",
    hint: "Across the top. A wide image works best, about 3:1 (1200 × 400).",
  },
  {
    key: "footer",
    field: "panel_footer_url",
    label: "Footer",
    hint: "Along the bottom, closing the panel. About 7.5:1 (1200 × 160), the Lobby Header's height.",
  },
  {
    key: "header",
    field: "panel_header_url",
    label: "Lobby Header",
    hint: "Tops game lobbies, about 7.5:1 (1200 × 160). Keep the middle 640 px plain: the bot draws the name and leaves there.",
  },
  {
    key: "card_header",
    field: "panel_card_header_url",
    label: "Card Header",
    hint: "A thinner strip, about 15:1 (1200 × 80), across the top of other cards the bot posts. Nothing is written on it.",
  },
  {
    key: "card_footer",
    field: "panel_card_footer_url",
    label: "Card Footer",
    hint: "Along the bottom of the same cards, the Card Header's height. It only shows beneath a Card Header.",
  },
];

function artSlotHtml(slot, url) {
  const id = `cb-art-${slot.key}`;
  return `
    <form class="form" data-art-form="${slot.key}">
      <div class="field">
        <label for="${id}">${slot.label} Image</label>
        <img data-art-preview src="${escapeHtml(url)}" alt="Current ${slot.label} image" style="max-width:100%;border-radius:8px;display:${url ? "block" : "none"}" />
        <input type="file" id="${id}" data-art-file accept="image/png,image/jpeg,image/gif,image/webp" />
        <div class="field-hint">${slot.hint} PNG, JPG, GIF or WEBP up to 8 MB; an animated GIF or WEBP plays. Discord shows the whole image, never a crop.</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <button type="submit" class="btn btn-primary">Upload ${slot.label}</button>
        <button type="button" class="btn" data-art-remove style="display:${url ? "inline-flex" : "none"}">Remove ${slot.label}</button>
        <span data-art-status></span>
      </div>
    </form>`;
}

function wireArtSlot(container, slot) {
  const form = container.querySelector(`[data-art-form="${slot.key}"]`);
  const file = form.querySelector("[data-art-file]");
  const preview = form.querySelector("[data-art-preview]");
  const remove = form.querySelector("[data-art-remove]");
  const status = form.querySelector("[data-art-status]");
  const endpoint = `/api/config/branding/${slot.key}`;
  guardForm(form);

  function show(url) {
    preview.src = url;
    preview.style.display = url ? "block" : "none";
    remove.style.display = url ? "inline-flex" : "none";
  }

  guardedSubmit(form, async () => {
    if (file.files.length === 0) {
      showStatus(status, false, "Pick an image first.");
      return;
    }
    const fd = new FormData();
    fd.append("file", file.files[0]);
    try {
      const data = await apiPost(endpoint, fd);
      show(data[slot.field] || "");
      file.value = "";
      showStatus(status, true, "Uploaded");
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });

  remove.addEventListener("click", async () => {
    try {
      await apiDelete(endpoint);
      show("");
      showStatus(status, true, "Removed");
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading branding…</div></div>`;

  return mountAsync(container, async () => {
    const config = await loadConfig();
    const bi = config.bot_identity || { nick: "", avatar_url: "" };
    const br = config.branding || { accent_mode: "avatar", accent_hex: "" };
    const mode = br.accent_mode === "custom" ? "custom" : "avatar";
    // Defaults come from the server so the placeholder can never drift from
    // the name the bot actually falls back to.
    const defaultCasinoName = br.default_casino_name || "Golden Meadow";
    const defaultAssistantName = br.default_assistant_name || "Billy-bot";
    const pickerValue = br.accent_hex || DEFAULT_ACCENT;
    const artUrls = {
      banner: br.panel_banner_url || "",
      footer: br.panel_footer_url || "",
      header: br.panel_header_url || "",
      card_header: br.panel_card_header_url || "",
      card_footer: br.panel_card_footer_url || "",
    };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Branding</h2>
          <div class="subtitle">This server's bot name, avatar, embed accent color, panel art, and feature names</div>
        </header>

        <form class="form card" data-identity-form>
          <div class="section-label">Bot Identity in This Server</div>
          <div class="field-hint" style="margin-bottom:12px;">Dungeon Keeper can wear a different name and avatar in every server it joins. These settings apply here only.</div>
          <img data-avatar-preview src="${escapeHtml(bi.avatar_url)}" alt="Current bot avatar" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:1rem;display:${bi.avatar_url ? "block" : "none"}" />
          <div class="field">
            <label for="cb-nick">Nickname</label>
            <input type="text" id="cb-nick" data-nick value="${escapeHtml(bi.nick)}" maxlength="32" placeholder="Dungeon Keeper" />
            <div class="field-hint">The name members see on the bot in this server. Leave blank to fall back to its default name.</div>
          </div>
          <div class="field">
            <label for="cb-avatar-url">Avatar Image URL</label>
            <input type="url" id="cb-avatar-url" data-avatar-url placeholder="https://example.com/image.png" />
            <div class="field-hint">A public link to a PNG, JPG, or GIF. If you also pick a file below, the file wins.</div>
          </div>
          <div class="field">
            <label for="cb-avatar-file">Upload an Avatar Image</label>
            <input type="file" id="cb-avatar-file" data-avatar-file accept="image/*" />
            <div class="field-hint">Uploading replaces the bot's avatar in this server as soon as you press Save.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save Identity</button>
            <span data-identity-status></span>
          </div>
        </form>

        <form class="form card" data-accent-form style="margin-top:12px;">
          <div class="section-label">Embed Accent Color</div>
          <div class="field-hint" style="margin-bottom:1rem">The bar on neutral embeds such as confessions. Wins, warnings, leaderboards and game phases keep their own colors.</div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="avatar" ${mode === "avatar" ? "checked" : ""} />
              Match the Bot Avatar
            </label>
            <div class="field-hint">Picks the most vivid color out of this server's bot avatar. If the avatar is black and white, the accent comes out gray.</div>
          </div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="custom" ${mode === "custom" ? "checked" : ""} />
              Pick a Color
            </label>
            <div style="display:flex;align-items:center;gap:12px;margin-top:.5rem">
              <input type="color" id="cb-accent-hex" data-accent-hex value="${escapeHtml(pickerValue)}" aria-label="Custom accent color" style="width:52px;height:36px;padding:0;border:none;background:none;cursor:pointer" />
              <code data-accent-hex-label>${escapeHtml(pickerValue.toUpperCase())}</code>
            </div>
            <div class="field-hint">One deliberate color everywhere. Changing the swatch selects this option.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save Accent Color</button>
            <span data-accent-status></span>
          </div>
        </form>

        <div class="form card" style="margin-top:12px;">
          <div class="section-label">Panel Art</div>
          <div class="field-hint">Art for panels and cards that stay in a channel. Each image is optional; draw any border at the far edges.</div>
          ${ART_SLOTS.map((slot) => artSlotHtml(slot, artUrls[slot.key])).join("")}
          <div class="field" style="margin-top:12px;">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="checkbox" data-art-title ${br.panel_banner_title ? "checked" : ""} />
              Title in the Banner
            </label>
            <div class="field-hint">Writes each panel's name in gold across the banner's foot. Needs a banner; keep its bottom third clear. <span data-art-title-status></span></div>
          </div>
        </div>

        <form class="form card" data-names-form style="margin-top:12px;">
          <div class="section-label">Feature Names</div>
          <div class="field-hint" style="margin-bottom:1rem">Rename these two features for this server. Blank keeps the built-in name.</div>
          <div class="field">
            <label for="cb-casino-name">Casino Name</label>
            <input type="text" id="cb-casino-name" data-casino-name maxlength="60" value="${escapeHtml(br.casino_name || "")}" placeholder="${escapeHtml(defaultCasinoName)}" />
            <div class="field-hint">Shown in the casino hub and payout guide. Blank uses <strong>${escapeHtml(defaultCasinoName)}</strong>.</div>
          </div>
          <div class="field">
            <label for="cb-assistant-name">AI Assistant Name</label>
            <input type="text" id="cb-assistant-name" data-assistant-name maxlength="60" value="${escapeHtml(br.assistant_name || "")}" placeholder="${escapeHtml(defaultAssistantName)}" />
            <div class="field-hint">The name <code>/ask</code> uses in its replies and the Help panel's ask box. Blank uses <strong>${escapeHtml(defaultAssistantName)}</strong>.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-names-status></span>
          </div>
        </form>

        <section class="form card" style="margin-top:12px;">
          <div class="section-label">Quote Card Border</div>
          <div class="field-hint">Upload a custom frame that wraps this server's quote cards. <a href="#/config-quote-border">Open the Quote Tool&nbsp;→</a></div>
        </section>
      </div>
    `;

    // ── Bot identity (reuses existing /api/config/bot-identity) ──────────────
    const identityForm = container.querySelector("[data-identity-form]");
    const identityStatus = container.querySelector("[data-identity-status]");
    const avatarPreview = container.querySelector("[data-avatar-preview]");
    guardForm(identityForm);

    guardedSubmit(identityForm, async () => {
      const nickInput = container.querySelector("[data-nick]");
      const avatarUrlInput = container.querySelector("[data-avatar-url]");
      const avatarFileInput = container.querySelector("[data-avatar-file]");

      const fd = new FormData();
      fd.append("nick", nickInput.value);
      if (avatarFileInput.files.length > 0) {
        fd.append("avatar_file", avatarFileInput.files[0]);
      } else if (avatarUrlInput.value.trim()) {
        fd.append("avatar_url", avatarUrlInput.value.trim());
      }

      try {
        const data = await apiPost("/api/config/bot-identity", fd);
        if (data.avatar_url) {
          avatarPreview.src = data.avatar_url;
          avatarPreview.style.display = "block";
        }
        nickInput.value = data.nick || "";
        avatarUrlInput.value = "";
        avatarFileInput.value = "";
        showStatus(identityStatus, true, "Saved");
      } catch (err) {
        showStatus(identityStatus, false, err.message);
      }
    });

    // ── Accent color ─────────────────────────────────────────────────────────
    const accentForm = container.querySelector("[data-accent-form]");
    const accentPicker = container.querySelector("[data-accent-hex]");
    const accentLabel = container.querySelector("[data-accent-hex-label]");
    const accentStatus = container.querySelector("[data-accent-status]");
    guardForm(accentForm);

    accentPicker.addEventListener("input", () => {
      accentLabel.textContent = accentPicker.value.toUpperCase();
      // Choosing a color implies you want it — switch to custom.
      const customRadio = container.querySelector('input[name="accent_mode"][value="custom"]');
      customRadio.checked = true;
    });

    // ── Panel art ────────────────────────────────────────────────────────────
    // Upload and Remove each take effect on their own press; the bot redraws
    // the live panels as soon as either lands.
    for (const slot of ART_SLOTS) wireArtSlot(container, slot);

    // Saves on the tick, like Upload and Remove beside it.
    const artTitle = container.querySelector("[data-art-title]");
    const artTitleStatus = container.querySelector("[data-art-title-status]");
    artTitle.addEventListener("change", async () => {
      try {
        await apiPut("/api/config/branding", { panel_banner_title: artTitle.checked });
        showStatus(artTitleStatus, true, "Saved");
      } catch (err) {
        artTitle.checked = !artTitle.checked;
        showStatus(artTitleStatus, false, err.message);
      }
    });

    // ── Feature names ────────────────────────────────────────────────────────
    const namesForm = container.querySelector("[data-names-form]");
    const namesStatus = container.querySelector("[data-names-status]");
    guardForm(namesForm);

    guardedSubmit(namesForm, async () => {
      try {
        // Blank clears the override — the bot falls back to the built-in name.
        await apiPut("/api/config/branding", {
          casino_name: container.querySelector("[data-casino-name]").value.trim(),
          assistant_name: container.querySelector("[data-assistant-name]").value.trim(),
        });
        showStatus(namesStatus, true, "Saved");
      } catch (err) {
        showStatus(namesStatus, false, err.message);
      }
    });

    guardedSubmit(accentForm, async () => {
      const selectedMode = container.querySelector('input[name="accent_mode"]:checked').value;
      try {
        await apiPut("/api/config/branding", {
          accent_mode: selectedMode,
          accent_hex: accentPicker.value,
        });
        showStatus(accentStatus, true, "Saved");
      } catch (err) {
        showStatus(accentStatus, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the branding settings." });
}
